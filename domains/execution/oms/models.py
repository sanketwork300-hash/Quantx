"""The order, its states, and the transitions between them.

Build spec §25 names six states and the fields an order must track. The states
are exactly those six: a seventh invented here — `EXPIRED`, `PENDING_CANCEL`,
`SUSPENDED` — would be a state no broker in the system reports, and code
downstream would start branching on something that never happens.

The transitions are a declared table rather than a set of `if` statements spread
through the service. An order that goes from `FILLED` back to `ACKNOWLEDGED`
is not a rare edge case to be tolerated; it means two writers raced, and the
loud failure is worth more than the tidy recovery.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from domains.research.costs import TradeCost


class OrderError(ValueError):
    """The order, or something asked of it, does not make sense."""


class IllegalTransition(OrderError):
    """A state change no order lifecycle permits."""


class OrderStatus(StrEnum):
    """The six states of build spec §25, and no others."""

    #: Accepted by the platform, not yet acknowledged by a broker.
    NEW = "NEW"
    #: The broker has the order and has given it an identifier.
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"

    @property
    def is_terminal(self) -> bool:
        return self in _TERMINAL

    @property
    def is_open(self) -> bool:
        """Still able to trade. A partially filled order is still working."""
        return self in {OrderStatus.NEW, OrderStatus.ACKNOWLEDGED, OrderStatus.PARTIALLY_FILLED}


_TERMINAL = frozenset({OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED})

#: What each state may become. A partial fill may become another partial fill:
#: two child fills against one order are two transitions, and collapsing them
#: would lose the second one's timestamp.
TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.NEW: frozenset(
        {
            OrderStatus.ACKNOWLEDGED,
            OrderStatus.REJECTED,
            OrderStatus.CANCELLED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
        }
    ),
    OrderStatus.ACKNOWLEDGED: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
        }
    ),
    OrderStatus.PARTIALLY_FILLED: frozenset(
        {OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED, OrderStatus.CANCELLED}
    ),
    OrderStatus.FILLED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
}


def check_transition(current: OrderStatus, target: OrderStatus) -> None:
    """Raise unless ``current -> target`` is a transition an order can make."""
    if target not in TRANSITIONS[current]:
        raise IllegalTransition(
            f"an order cannot go from {current} to {target}. "
            + (
                f"{current} is terminal."
                if current.is_terminal
                else f"from {current} it may become: "
                f"{', '.join(sorted(str(item) for item in TRANSITIONS[current]))}."
            )
        )


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        return 1 if self is OrderSide.BUY else -1

    @classmethod
    def for_quantity(cls, quantity: Decimal) -> OrderSide:
        if quantity == 0:
            raise OrderError("an order for zero quantity has no side")
        return cls.BUY if quantity > 0 else cls.SELL


class OrderType(StrEnum):
    """Only the two order types the platform can honestly simulate.

    A stop order's trigger is an exchange behaviour, and a paper broker that
    invented one would be asserting when the exchange would have fired it.
    Adapters declare what they support through :class:`BrokerCapability`, so an
    order type a broker cannot take is refused rather than translated.
    """

    MARKET = "MARKET"
    LIMIT = "LIMIT"


class TimeInForce(StrEnum):
    #: Rests until the end of the trading session, then is the broker's to expire.
    DAY = "DAY"
    #: Fills what it can immediately; the remainder is cancelled.
    IMMEDIATE_OR_CANCEL = "IOC"


class OrderVenue(StrEnum):
    """Which side of the wall an order is on.

    Not decoration and not derivable from the broker name: this is the field the
    live-trading gate reads, and the one that makes "was this real money?"
    answerable from the order row alone, forever, without joining to a config
    file that has since changed.
    """

    PAPER = "PAPER"
    LIVE = "LIVE"


@dataclass(frozen=True, slots=True)
class OrderRequest:
    """What the caller asked for. Never mutated by anything downstream."""

    instrument_id: uuid.UUID
    side: OrderSide
    quantity: Decimal
    order_type: OrderType
    #: Required for ``LIMIT``, refused for ``MARKET`` — a market order with a
    #: price is two instructions, and honouring either one silently is worse
    #: than refusing.
    limit_price: Decimal | None = None
    time_in_force: TimeInForce = TimeInForce.DAY
    #: The caller's idempotency key. Two submissions with one key are one order.
    client_order_id: str | None = None
    strategy_tag: str | None = None
    #: The price the decision was taken against, if the caller has one. Without
    #: it there is no slippage figure, and ``None`` is reported rather than a
    #: number measured against a reference invented after the fact.
    decision_price: Decimal | None = None
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise OrderError(
                "quantity is the size of the order and is always positive; direction is the side"
            )
        if self.order_type is OrderType.LIMIT and self.limit_price is None:
            raise OrderError("a limit order needs a limit price")
        if self.order_type is OrderType.MARKET and self.limit_price is not None:
            raise OrderError(
                "a market order with a limit price is two different instructions; send one of them"
            )
        if self.limit_price is not None and self.limit_price <= 0:
            raise OrderError("a limit price must be positive")

    @property
    def signed_quantity(self) -> Decimal:
        return self.quantity * self.side.sign


class RejectionReason(StrEnum):
    """Why an order did not reach a broker, or was refused by one.

    Every rejection carries one of these plus a sentence. The enum is what
    downstream code branches on; the sentence is what a person reads.
    """

    # -- risk gate
    KILL_SWITCH_ENGAGED = "KILL_SWITCH_ENGAGED"
    NO_RISK_LIMITS = "NO_RISK_LIMITS_CONFIGURED"
    ORDER_NOTIONAL_LIMIT = "ORDER_NOTIONAL_LIMIT"
    POSITION_LIMIT = "POSITION_QUANTITY_LIMIT"
    GROSS_EXPOSURE_LIMIT = "GROSS_EXPOSURE_LIMIT"
    NET_EXPOSURE_LIMIT = "NET_EXPOSURE_LIMIT"
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    ORDER_RATE_LIMIT = "ORDER_RATE_LIMIT"
    PRICE_BAND = "PRICE_OUTSIDE_DECLARED_BAND"
    INSUFFICIENT_CASH = "INSUFFICIENT_CASH"
    # -- market data
    NO_QUOTE = "NO_QUOTE"
    QUOTE_STALE = "QUOTE_STALE"
    NO_TWO_SIDED_MARKET = "NO_TWO_SIDED_MARKET"
    # -- routing
    INSTRUMENT_UNKNOWN = "INSTRUMENT_NOT_IN_MASTER"
    CAPABILITY_MISSING = "BROKER_CAPABILITY_MISSING"
    LIVE_TRADING_DISABLED = "LIVE_TRADING_DISABLED"
    BROKER_REJECTED = "BROKER_REJECTED"
    BROKER_UNREACHABLE = "BROKER_UNREACHABLE"


@dataclass(frozen=True, slots=True)
class Rejection:
    reason: RejectionReason
    detail: str
    #: The check's own numbers, so a limit breach can be shown rather than
    #: described: ``{"limit": "500000", "would_be": "620000"}``.
    observed: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "reason": str(self.reason),
            "detail": self.detail,
            "observed": self.observed,
        }


class FillFlag(StrEnum):
    """What was true about the market when a fill was decided.

    These travel with the fill for its lifetime. They are the difference between
    "the paper account bought 100 at 250.10" and "the paper account assumed it
    could buy 100 at 250.10, against a quote that reported no depth".
    """

    #: Always present on a paper fill. There was no counterparty.
    COUNTERFACTUAL = "PAPER_FILL_COUNTERFACTUAL"
    #: The quote carried no size on the touched side, so the fill quantity was
    #: not tested against any depth anybody published.
    DEPTH_NOT_REPORTED = "DEPTH_NOT_REPORTED"
    #: The fill was capped by the size on the quote.
    LIMITED_BY_DEPTH = "LIMITED_BY_DEPTH"
    #: Filled at the last trade because the policy allowed it. A trade print is
    #: not a quote and a fill against one is not a fill against a market.
    LAST_TRADE_NOT_A_QUOTE = "LAST_TRADE_NOT_A_QUOTE"
    #: No cost schedule was supplied: this fill's P&L contribution is gross.
    COSTS_NOT_MODELLED = "COSTS_NOT_MODELLED"


@dataclass(frozen=True, slots=True)
class OrderFill:
    """One execution against one order.

    ``price`` is what the paper or live broker filled at. ``reference_price`` is
    what it was measured against and where it came from — two separate fields
    because a slippage number whose baseline is unrecorded cannot be checked.
    """

    id: uuid.UUID
    order_id: uuid.UUID
    quantity: Decimal
    price: Decimal
    filled_at: datetime
    #: Which observed field the price came from, e.g. ``MARKET_ASK``.
    price_basis: str
    cost: TradeCost
    reference_price: Decimal | None = None
    reference_basis: str | None = None
    #: The exchange timestamp of the quote this was decided against. Absent for
    #: a live fill, where the broker reports its own execution time instead.
    quote_exchange_timestamp: datetime | None = None
    broker_trade_id: str | None = None
    flags: tuple[FillFlag, ...] = ()

    @property
    def notional(self) -> Decimal:
        return self.price * self.quantity

    @property
    def slippage_against_reference(self) -> Decimal | None:
        """Adverse movement against the stated reference, or ``None``.

        Signed so the direction survives: positive is worse than the reference
        for the side that traded. There is no figure at all without a reference,
        which is the point — "slippage" against nothing is not a measurement.
        """
        if self.reference_price is None:
            return None
        sign = Decimal(1) if self.quantity > 0 else Decimal(-1)
        return (self.price - self.reference_price) * sign * abs(self.quantity)

    def to_dict(self) -> dict:
        slippage = self.slippage_against_reference
        return {
            "id": str(self.id),
            "order_id": str(self.order_id),
            "quantity": format(self.quantity, "f"),
            "price": format(self.price, "f"),
            "filled_at": self.filled_at.isoformat(),
            "price_basis": self.price_basis,
            "reference_price": (
                format(self.reference_price, "f") if self.reference_price is not None else None
            ),
            "reference_basis": self.reference_basis,
            "slippage_against_reference": (format(slippage, "f") if slippage is not None else None),
            "quote_exchange_timestamp": (
                self.quote_exchange_timestamp.isoformat()
                if self.quote_exchange_timestamp is not None
                else None
            ),
            "broker_trade_id": self.broker_trade_id,
            "cost": self.cost.to_dict(),
            "flags": [str(flag) for flag in self.flags],
        }


@dataclass(frozen=True, slots=True)
class Order:
    """An order and everything that has happened to it."""

    id: uuid.UUID
    account_id: uuid.UUID
    user_id: uuid.UUID
    instrument_id: uuid.UUID
    client_order_id: str
    side: OrderSide
    quantity: Decimal
    order_type: OrderType
    time_in_force: TimeInForce
    status: OrderStatus
    venue: OrderVenue
    broker: str
    created_at: datetime
    updated_at: datetime
    limit_price: Decimal | None = None
    broker_order_id: str | None = None
    filled_quantity: Decimal = Decimal(0)
    #: Quantity-weighted mean of the fills. Derived, and named so: it is not a
    #: price anybody quoted.
    average_fill_price: Decimal | None = None
    fees: Decimal = Decimal(0)
    decision_price: Decimal | None = None
    submitted_at: datetime | None = None
    acknowledged_at: datetime | None = None
    closed_at: datetime | None = None
    rejection: Rejection | None = None
    strategy_tag: str | None = None
    parent_order_id: uuid.UUID | None = None
    fills: tuple[OrderFill, ...] = ()
    metadata: dict = field(default_factory=dict)

    @property
    def remaining_quantity(self) -> Decimal:
        return self.quantity - self.filled_quantity

    @property
    def signed_filled_quantity(self) -> Decimal:
        return self.filled_quantity * self.side.sign

    @property
    def slippage_against_decision(self) -> Decimal | None:
        """Total adverse movement from the decision price, or ``None``.

        ``None`` when the caller stated no decision price. Build spec §25 lists
        slippage among an order's fields; it does not follow that a number can
        be produced without a baseline, and inventing one — the arrival mid, the
        first fill — would make every order look good against itself.
        """
        if self.decision_price is None or not self.fills:
            return None
        sign = Decimal(self.side.sign)
        return sum(
            ((fill.price - self.decision_price) * sign * fill.quantity for fill in self.fills),
            Decimal(0),
        )

    def to_dict(self) -> dict:
        slippage = self.slippage_against_decision
        return {
            "id": str(self.id),
            "account_id": str(self.account_id),
            "instrument_id": str(self.instrument_id),
            "client_order_id": self.client_order_id,
            "side": str(self.side),
            "quantity": format(self.quantity, "f"),
            "order_type": str(self.order_type),
            "time_in_force": str(self.time_in_force),
            "status": str(self.status),
            "venue": str(self.venue),
            "broker": self.broker,
            "limit_price": (
                format(self.limit_price, "f") if self.limit_price is not None else None
            ),
            "broker_order_id": self.broker_order_id,
            "filled_quantity": format(self.filled_quantity, "f"),
            "remaining_quantity": format(self.remaining_quantity, "f"),
            "average_fill_price": (
                format(self.average_fill_price, "f")
                if self.average_fill_price is not None
                else None
            ),
            "fees": format(self.fees, "f"),
            "decision_price": (
                format(self.decision_price, "f") if self.decision_price is not None else None
            ),
            "slippage_against_decision": (format(slippage, "f") if slippage is not None else None),
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "submitted_at": self.submitted_at.isoformat() if self.submitted_at else None,
            "acknowledged_at": (self.acknowledged_at.isoformat() if self.acknowledged_at else None),
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
            "rejection": self.rejection.to_dict() if self.rejection else None,
            "strategy_tag": self.strategy_tag,
            "parent_order_id": str(self.parent_order_id) if self.parent_order_id else None,
            "fills": [fill.to_dict() for fill in self.fills],
        }
