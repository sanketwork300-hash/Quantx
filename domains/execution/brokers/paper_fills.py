"""What a paper order does when it meets a quote.

Pure, synchronous and side-effect free: a quote and an order go in, a decision
comes out. Everything that makes paper trading arguable lives here, in one place
where it can be read and tested, rather than being distributed through a service
that also talks to a database.

The governing rule is the one that makes ``Quote.mid_price`` return ``None``
rather than fall back to the last trade. A fill is the platform asserting that a
trade could have happened at a price. The evidence for that assertion is the
quote, and where the quote does not support it the honest answer is no fill and
a reason — not a fill at whatever number happened to be available.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from domains.execution.oms.models import (
    FillFlag,
    OrderSide,
    OrderType,
    RejectionReason,
    TimeInForce,
)
from domains.market_data.models import Quote


class PaperFillPolicy(StrEnum):
    """What evidence a paper fill is allowed to rest on.

    The default is the strict one. A permissive fill policy makes a paper
    account look better than the market would have been, and a default that
    flatters is the kind that never gets changed.
    """

    #: Fill only against a two-sided quote, at the touched side.
    QUOTE_ONLY = "QUOTE_ONLY"
    #: Also fill against the last traded price when there is no two-sided
    #: market. Every such fill is flagged ``LAST_TRADE_NOT_A_QUOTE``.
    ALLOW_LAST_TRADE = "ALLOW_LAST_TRADE"


#: What the fill price came from. Stored on every fill, because "250.10" does
#: not say whether it was an ask somebody published or a print from an hour ago.
class PriceBasis(StrEnum):
    MARKET_ASK = "MARKET_ASK"
    MARKET_BID = "MARKET_BID"
    LAST_TRADE = "LAST_TRADE"
    LIMIT_PRICE = "LIMIT_PRICE"


@dataclass(frozen=True, slots=True)
class FillDecision:
    """The outcome of one order meeting one quote.

    Exactly one of three things is true, and the type says which: a quantity
    filled, nothing filled but the order still resting, or a refusal with a
    reason. There is no fourth case where something happened and nothing says
    what.
    """

    #: Quantity to fill now. Zero means nothing filled on this quote.
    quantity: Decimal = Decimal(0)
    price: Decimal | None = None
    price_basis: PriceBasis | None = None
    reference_price: Decimal | None = None
    reference_basis: str | None = None
    #: True when the order should stay open and be re-offered the next quote.
    rests: bool = False
    rejection: RejectionReason | None = None
    detail: str = ""
    flags: tuple[FillFlag, ...] = ()

    @property
    def fills(self) -> bool:
        return self.quantity > 0

    @property
    def cancel_remainder(self) -> bool:
        """Nothing more will happen to this order and it was not refused.

        The immediate-or-cancel case, which is neither a fill nor a rejection:
        the instruction was honoured exactly and what could not trade at once
        is withdrawn. Without this the caller has to infer a cancellation from
        the absence of two other things, and inference is where states go wrong.
        """
        return not self.fills and not self.rests and self.rejection is None


@dataclass(frozen=True, slots=True)
class FillContext:
    """The order as the fill engine needs to see it.

    Deliberately not the persisted :class:`Order`: the engine has no business
    reading a status or an account id, and taking only these fields is what
    keeps it testable without a database.
    """

    side: OrderSide
    order_type: OrderType
    remaining_quantity: Decimal
    time_in_force: TimeInForce = TimeInForce.DAY
    limit_price: Decimal | None = None
    metadata: dict = field(default_factory=dict)


def decide_fill(
    order: FillContext,
    quote: Quote | None,
    as_of: datetime,
    *,
    policy: PaperFillPolicy = PaperFillPolicy.QUOTE_ONLY,
    max_quote_age_seconds: float = 30.0,
) -> FillDecision:
    """Decide what happens to one paper order against one quote.

    ``max_quote_age_seconds`` is a declared tolerance, not a constant with
    opinions. The same staleness that is acceptable when *valuing* a position —
    where a stale mark is still the best observation of something that exists —
    is not acceptable when *filling* one, because a fill against an old quote
    asserts liquidity that nobody has published since.
    """
    if order.remaining_quantity <= 0:
        return FillDecision(
            rejection=RejectionReason.BROKER_REJECTED,
            detail="the order has nothing left to fill",
        )

    if quote is None:
        return _no_fill(
            order,
            RejectionReason.NO_QUOTE,
            "no quote is held for this instrument, so there is no price to fill against",
        )

    age = quote.age_seconds(as_of)
    if age > max_quote_age_seconds:
        return _no_fill(
            order,
            RejectionReason.QUOTE_STALE,
            f"the newest quote is {age:,.0f}s old, beyond the {max_quote_age_seconds:,.0f}s "
            "tolerance; filling against it would assert liquidity nobody has published since",
        )

    touch, touch_basis, depth = _touch(order.side, quote)
    flags: list[FillFlag] = [FillFlag.COUNTERFACTUAL]

    if touch is None:
        if policy is not PaperFillPolicy.ALLOW_LAST_TRADE:
            return _no_fill(
                order,
                RejectionReason.NO_TWO_SIDED_MARKET,
                "the quote has no "
                + ("ask" if order.side is OrderSide.BUY else "bid")
                + " to trade against. Filling at the last traded price is a different "
                "policy and has to be asked for: a trade print is not a quote",
            )
        if quote.last_price is None or quote.last_price <= 0:
            return _no_fill(
                order,
                RejectionReason.NO_QUOTE,
                "the quote carries neither a tradable side nor a last traded price",
            )
        touch, touch_basis, depth = quote.last_price, PriceBasis.LAST_TRADE, None
        flags.append(FillFlag.LAST_TRADE_NOT_A_QUOTE)

    # A limit order fills only where its price permits, and at the touch rather
    # than at its own limit: a buy limit at 260 against an ask of 250 pays 250.
    # Booking it at the limit would invent 10 of cost the market never charged.
    if order.order_type is OrderType.LIMIT:
        if order.limit_price is None:
            raise ValueError("a limit order reached the fill engine with no limit price")
        marketable = (
            touch <= order.limit_price
            if order.side is OrderSide.BUY
            else touch >= order.limit_price
        )
        if not marketable:
            if order.time_in_force is TimeInForce.IMMEDIATE_OR_CANCEL:
                return FillDecision(
                    rejection=None,
                    rests=False,
                    detail=(
                        f"immediate-or-cancel: the {touch_basis.value.lower()} of "
                        f"{touch} does not meet the limit of {order.limit_price}, "
                        "so nothing filled and the remainder is cancelled"
                    ),
                )
            return FillDecision(
                rests=True,
                detail=(
                    f"resting: the {touch_basis.value.lower()} of {touch} does not "
                    f"meet the limit of {order.limit_price}"
                ),
            )

    quantity = order.remaining_quantity
    if depth is None:
        # Nobody published a size on this side. The full quantity fills, and the
        # flag is what stops that reading as evidence the depth was there.
        flags.append(FillFlag.DEPTH_NOT_REPORTED)
    elif depth <= 0:
        return _no_fill(
            order,
            RejectionReason.NO_TWO_SIDED_MARKET,
            f"the {touch_basis.value.lower()} side reports a size of {depth}",
        )
    elif depth < quantity:
        quantity = depth
        flags.append(FillFlag.LIMITED_BY_DEPTH)

    partial = quantity < order.remaining_quantity
    rests = partial and order.time_in_force is not TimeInForce.IMMEDIATE_OR_CANCEL

    return FillDecision(
        quantity=quantity,
        price=touch,
        price_basis=touch_basis,
        reference_price=quote.mid_price,
        reference_basis="QUOTE_MID" if quote.mid_price is not None else None,
        rests=rests,
        detail=(
            f"filled {quantity} of {order.remaining_quantity} at the "
            f"{touch_basis.value.lower()}"
            + (f", capped by the {depth} on offer" if FillFlag.LIMITED_BY_DEPTH in flags else "")
        ),
        flags=tuple(flags),
    )


def _touch(side: OrderSide, quote: Quote) -> tuple[Decimal | None, PriceBasis, Decimal | None]:
    """The price a taker of this side pays, and the size published behind it."""
    if side is OrderSide.BUY:
        price = quote.ask_price if _tradable(quote.ask_price) else None
        return price, PriceBasis.MARKET_ASK, quote.ask_size
    price = quote.bid_price if _tradable(quote.bid_price) else None
    return price, PriceBasis.MARKET_BID, quote.bid_size


def _tradable(price: Decimal | None) -> bool:
    return price is not None and price > 0


def _no_fill(order: FillContext, reason: RejectionReason, detail: str) -> FillDecision:
    """No fill now. A market order is refused; a resting limit order waits.

    The distinction matters: a market order that cannot fill has failed and the
    caller needs to know now, whereas a day limit order that cannot fill yet is
    doing exactly what it was sent to do.
    """
    if order.order_type is OrderType.LIMIT and order.time_in_force is TimeInForce.DAY:
        return FillDecision(rests=True, detail=detail)
    return FillDecision(rejection=reason, detail=detail)
