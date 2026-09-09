"""Pre-trade risk checks.

Build spec §25 puts risk validation between the order generator and the OMS, and
§23 puts it between the signal and the order manager. It is the same gate: every
order passes through it, paper and live alike, and an order that a paper account
would have refused must not become tradable by being sent to a real broker.

Two rules shape the whole module.

**A gate refuses; it never adjusts.** An order that breaches a position limit is
rejected with the limit and the number that breached it, not silently cut to the
largest size that would have passed. A resized order is an order the user did not
send, and the account would then hold a position nobody decided on.

**Every limit is the user's.** There is no default maximum position, no assumed
price band, no inferred loss limit. Exchange price bands and broker exposure
rules are real numbers that this platform does not have, and build spec 1.1
forbids inventing them; what is checked here are limits somebody typed in, and
the absence of limits is itself reported.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from domains.execution.oms.models import (
    OrderRequest,
    OrderSide,
    OrderVenue,
    Rejection,
    RejectionReason,
)
from domains.market_data.models import Quote


@dataclass(frozen=True, slots=True)
class RiskLimits:
    """Limits as the account's owner set them.

    Every field is optional and every one defaults to *no limit*. That is not a
    permissive default masquerading as a safe one: :func:`evaluate` reports an
    unlimited account, and the live-trading gate in Phase 7 refuses to arm one.
    Paper trading with no limits is a legitimate thing to want; live trading
    with none is not, and the difference belongs at the point of arming rather
    than buried in a default here.
    """

    #: Largest notional (price x quantity) a single order may carry.
    max_order_notional: Decimal | None = None
    #: Largest absolute position in any one instrument, after this order.
    max_position_quantity: Decimal | None = None
    #: Sum of absolute position values after this order.
    max_gross_exposure: Decimal | None = None
    #: Absolute value of the signed sum of position values after this order.
    max_net_exposure: Decimal | None = None
    #: Loss over the current session at which no further orders are accepted.
    #: Compared against a figure the caller supplies, so what counts as "today"
    #: and what counts as a loss are the caller's to define and to state.
    max_daily_loss: Decimal | None = None
    #: Orders per rolling minute.
    max_orders_per_minute: int | None = None
    #: How far a limit price may sit from the reference price, as a fraction.
    #: A fat-finger guard the user declares — not an exchange price band, which
    #: is an exchange rule the platform does not hold.
    max_price_deviation: Decimal | None = None
    #: Refuse an order whose cash cost exceeds the account's cash.
    require_sufficient_cash: bool = False

    @property
    def any_set(self) -> bool:
        return (
            any(
                value is not None
                for value in (
                    self.max_order_notional,
                    self.max_position_quantity,
                    self.max_gross_exposure,
                    self.max_net_exposure,
                    self.max_daily_loss,
                    self.max_orders_per_minute,
                    self.max_price_deviation,
                )
            )
            or self.require_sufficient_cash
        )

    def to_dict(self) -> dict:
        def fmt(value: Decimal | None) -> str | None:
            return format(value, "f") if value is not None else None

        return {
            "max_order_notional": fmt(self.max_order_notional),
            "max_position_quantity": fmt(self.max_position_quantity),
            "max_gross_exposure": fmt(self.max_gross_exposure),
            "max_net_exposure": fmt(self.max_net_exposure),
            "max_daily_loss": fmt(self.max_daily_loss),
            "max_orders_per_minute": self.max_orders_per_minute,
            "max_price_deviation": fmt(self.max_price_deviation),
            "require_sufficient_cash": self.require_sufficient_cash,
            "any_set": self.any_set,
        }


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    """The account as it stands before this order.

    Prices are passed in rather than fetched: the gate is pure, and a check that
    went looking for its own market data could evaluate an order against a
    different market than the one the fill will use.
    """

    cash: Decimal
    #: Signed quantity per instrument. Negative is short.
    positions: dict[uuid.UUID, Decimal] = field(default_factory=dict)
    #: Marks used for exposure. An instrument absent here is reported as
    #: unpriced rather than valued at zero.
    prices: dict[uuid.UUID, Decimal] = field(default_factory=dict)
    #: Realised plus unrealised loss over the session, positive for a loss.
    #: Supplied by the caller because the definition of the session is theirs.
    session_loss: Decimal | None = None
    orders_in_last_minute: int = 0
    #: Contract multipliers. An instrument absent from this map is valued at a
    #: multiplier of one and named in ``multipliers_assumed``.
    multipliers: dict[uuid.UUID, Decimal] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GateCheck:
    """One check and what it saw. Kept whether it passed or not.

    A gate that reports only its failures cannot answer "was this checked?",
    which is the question that matters after something goes wrong.
    """

    name: str
    passed: bool
    #: ``True`` when there was no limit to check against.
    not_configured: bool = False
    detail: str = ""
    observed: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "passed": self.passed,
            "not_configured": self.not_configured,
            "detail": self.detail,
            "observed": self.observed,
        }


@dataclass(frozen=True, slots=True)
class GateDecision:
    """The gate's answer, with every check it ran."""

    allowed: bool
    checks: tuple[GateCheck, ...]
    rejection: Rejection | None = None
    #: Instruments whose exposure could not be valued, and which therefore did
    #: not contribute to the gross and net checks. Named rather than treated as
    #: zero: an unpriced position is not a flat one.
    unpriced: tuple[uuid.UUID, ...] = ()
    #: Instruments valued with a multiplier of one because none was recorded.
    multipliers_assumed: tuple[uuid.UUID, ...] = ()

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "checks": [check.to_dict() for check in self.checks],
            "rejection": self.rejection.to_dict() if self.rejection else None,
            "unpriced": [str(item) for item in self.unpriced],
            "multipliers_assumed": [str(item) for item in self.multipliers_assumed],
        }


def evaluate(
    request: OrderRequest,
    limits: RiskLimits,
    account: AccountSnapshot,
    quote: Quote | None,
    *,
    venue: OrderVenue,
    kill_switch_engaged: bool = False,
    kill_switch_reason: str = "",
    as_of: datetime | None = None,
) -> GateDecision:
    """Run every check, then answer.

    Deliberately not short-circuited. An order that breaches three limits is
    more interesting than one that breaches the first limit in the list, and a
    gate that stops at the first failure makes the user fix them one at a time.
    The *rejection* reported is the first failure, so the branchable reason is
    stable; the full list travels beside it.
    """
    del as_of  # every check here is a level, not a rate over time
    checks: list[GateCheck] = []

    checks.append(
        GateCheck(
            name="kill_switch",
            passed=not kill_switch_engaged,
            detail=(
                kill_switch_reason or "the kill switch is engaged for this account"
                if kill_switch_engaged
                else "the kill switch is not engaged"
            ),
        )
    )

    # An account with no limits is a legitimate thing to want on paper and not
    # on live money, so the venue decides whether the absence is a refusal.
    limits_required = venue is OrderVenue.LIVE
    checks.append(
        GateCheck(
            name="limits_configured",
            passed=limits.any_set or not limits_required,
            not_configured=not limits.any_set,
            detail=(
                "risk limits are set"
                if limits.any_set
                else "this account has no risk limits set. Live orders are refused "
                "without them: an unlimited live account is not a decision anybody "
                "makes deliberately"
                if limits_required
                else "this account has no risk limits set, so only the checks that "
                "need none were run"
            ),
        )
    )

    reference = _reference_price(request, quote)
    notional = (
        abs(reference * request.quantity) * _multiplier(account, request.instrument_id)
        if reference is not None
        else None
    )

    checks.append(_order_notional(limits, notional, reference))
    checks.append(_price_band(request, limits, quote))
    checks.append(_position(request, limits, account))

    exposure, unpriced, assumed = _post_trade_exposure(request, account, reference)
    checks.append(_gross(limits, exposure))
    checks.append(_net(limits, exposure))
    checks.append(_daily_loss(limits, account))
    checks.append(_order_rate(limits, account))
    checks.append(_cash(request, limits, account, notional))

    failure = next((check for check in checks if not check.passed), None)
    rejection = (
        Rejection(
            reason=_REASONS[failure.name],
            detail=failure.detail,
            observed=failure.observed,
        )
        if failure is not None
        else None
    )
    return GateDecision(
        allowed=failure is None,
        checks=tuple(checks),
        rejection=rejection,
        unpriced=unpriced,
        multipliers_assumed=assumed,
    )


_REASONS: dict[str, RejectionReason] = {
    "kill_switch": RejectionReason.KILL_SWITCH_ENGAGED,
    "limits_configured": RejectionReason.NO_RISK_LIMITS,
    "order_notional": RejectionReason.ORDER_NOTIONAL_LIMIT,
    "price_band": RejectionReason.PRICE_BAND,
    "position_quantity": RejectionReason.POSITION_LIMIT,
    "gross_exposure": RejectionReason.GROSS_EXPOSURE_LIMIT,
    "net_exposure": RejectionReason.NET_EXPOSURE_LIMIT,
    "daily_loss": RejectionReason.DAILY_LOSS_LIMIT,
    "order_rate": RejectionReason.ORDER_RATE_LIMIT,
    "cash": RejectionReason.INSUFFICIENT_CASH,
}


def _reference_price(request: OrderRequest, quote: Quote | None) -> Decimal | None:
    """The price this order is measured against, in order of directness.

    A limit order's own limit is used when there is no quote, because it is the
    worst price the order can pay and a notional check against it cannot
    understate the order. Nothing here falls back to the last trade.
    """
    if quote is not None:
        if quote.mid_price is not None:
            return quote.mid_price
        touch = quote.ask_price if request.side is OrderSide.BUY else quote.bid_price
        if touch is not None and touch > 0:
            return touch
    return request.limit_price


def _multiplier(account: AccountSnapshot, instrument_id: uuid.UUID) -> Decimal:
    return account.multipliers.get(instrument_id, Decimal(1))


def _order_notional(
    limits: RiskLimits, notional: Decimal | None, reference: Decimal | None
) -> GateCheck:
    if limits.max_order_notional is None:
        return GateCheck("order_notional", True, not_configured=True, detail="no limit set")
    if notional is None:
        return GateCheck(
            "order_notional",
            False,
            detail=(
                "a notional limit is set but this order has no price to measure "
                "against: there is no quote and it is not a limit order"
            ),
        )
    passed = notional <= limits.max_order_notional
    return GateCheck(
        "order_notional",
        passed,
        detail=(
            f"order notional {notional} is within the limit of {limits.max_order_notional}"
            if passed
            else f"order notional {notional} exceeds the limit of {limits.max_order_notional}"
        ),
        observed={
            "notional": format(notional, "f"),
            "limit": format(limits.max_order_notional, "f"),
            "reference_price": format(reference, "f") if reference is not None else None,
        },
    )


def _price_band(request: OrderRequest, limits: RiskLimits, quote: Quote | None) -> GateCheck:
    if limits.max_price_deviation is None:
        return GateCheck("price_band", True, not_configured=True, detail="no band set")
    if request.limit_price is None:
        return GateCheck(
            "price_band", True, detail="a market order has no price to compare to the band"
        )
    anchor = quote.mid_price if quote is not None else None
    if anchor is None or anchor <= 0:
        return GateCheck(
            "price_band",
            True,
            not_configured=True,
            detail=(
                "the band could not be applied: there is no two-sided quote to "
                "measure the limit price against"
            ),
        )
    deviation = abs(request.limit_price - anchor) / anchor
    passed = deviation <= limits.max_price_deviation
    return GateCheck(
        "price_band",
        passed,
        detail=(
            f"limit price sits {deviation:.4f} from the quote mid, within the "
            f"declared band of {limits.max_price_deviation}"
            if passed
            else f"limit price sits {deviation:.4f} from the quote mid of {anchor}, "
            f"outside the declared band of {limits.max_price_deviation}"
        ),
        observed={
            "deviation": f"{deviation:.6f}",
            "band": format(limits.max_price_deviation, "f"),
            "anchor": format(anchor, "f"),
            "anchor_basis": "QUOTE_MID",
        },
    )


def _position(request: OrderRequest, limits: RiskLimits, account: AccountSnapshot) -> GateCheck:
    if limits.max_position_quantity is None:
        return GateCheck("position_quantity", True, not_configured=True, detail="no limit set")
    current = account.positions.get(request.instrument_id, Decimal(0))
    after = current + request.signed_quantity
    passed = abs(after) <= limits.max_position_quantity
    return GateCheck(
        "position_quantity",
        passed,
        detail=(
            f"position would be {after}, within the limit of {limits.max_position_quantity}"
            if passed
            else f"position would be {after} against a limit of "
            f"{limits.max_position_quantity}. The order is refused whole rather "
            "than cut to fit: a resized order is one nobody sent"
        ),
        observed={
            "current": format(current, "f"),
            "after": format(after, "f"),
            "limit": format(limits.max_position_quantity, "f"),
        },
    )


@dataclass(frozen=True, slots=True)
class _Exposure:
    gross: Decimal
    net: Decimal
    priced: bool


def _post_trade_exposure(
    request: OrderRequest, account: AccountSnapshot, reference: Decimal | None
) -> tuple[_Exposure, tuple[uuid.UUID, ...], tuple[uuid.UUID, ...]]:
    positions = dict(account.positions)
    positions[request.instrument_id] = (
        positions.get(request.instrument_id, Decimal(0)) + request.signed_quantity
    )

    gross = Decimal(0)
    net = Decimal(0)
    unpriced: list[uuid.UUID] = []
    assumed: list[uuid.UUID] = []
    for instrument_id, quantity in positions.items():
        price = account.prices.get(instrument_id)
        if price is None and instrument_id == request.instrument_id:
            price = reference
        if price is None:
            if quantity != 0:
                unpriced.append(instrument_id)
            continue
        if instrument_id not in account.multipliers:
            assumed.append(instrument_id)
        value = quantity * price * _multiplier(account, instrument_id)
        gross += abs(value)
        net += value
    return (
        _Exposure(gross=gross, net=net, priced=not unpriced),
        tuple(sorted(unpriced, key=str)),
        tuple(sorted(assumed, key=str)),
    )


def _gross(limits: RiskLimits, exposure: _Exposure) -> GateCheck:
    if limits.max_gross_exposure is None:
        return GateCheck("gross_exposure", True, not_configured=True, detail="no limit set")
    passed = exposure.gross <= limits.max_gross_exposure
    return GateCheck(
        "gross_exposure",
        passed,
        detail=(
            f"gross exposure would be {exposure.gross}, within {limits.max_gross_exposure}"
            if passed
            else f"gross exposure would be {exposure.gross}, over {limits.max_gross_exposure}"
        )
        + ("" if exposure.priced else "; some positions could not be priced and are excluded"),
        observed={
            "after": format(exposure.gross, "f"),
            "limit": format(limits.max_gross_exposure, "f"),
            "all_positions_priced": exposure.priced,
        },
    )


def _net(limits: RiskLimits, exposure: _Exposure) -> GateCheck:
    if limits.max_net_exposure is None:
        return GateCheck("net_exposure", True, not_configured=True, detail="no limit set")
    passed = abs(exposure.net) <= limits.max_net_exposure
    return GateCheck(
        "net_exposure",
        passed,
        detail=(
            f"net exposure would be {exposure.net}, within {limits.max_net_exposure}"
            if passed
            else f"net exposure would be {exposure.net}, over {limits.max_net_exposure}"
        )
        + ("" if exposure.priced else "; some positions could not be priced and are excluded"),
        observed={
            "after": format(exposure.net, "f"),
            "limit": format(limits.max_net_exposure, "f"),
            "all_positions_priced": exposure.priced,
        },
    )


def _daily_loss(limits: RiskLimits, account: AccountSnapshot) -> GateCheck:
    if limits.max_daily_loss is None:
        return GateCheck("daily_loss", True, not_configured=True, detail="no limit set")
    if account.session_loss is None:
        return GateCheck(
            "daily_loss",
            False,
            detail=(
                "a daily loss limit is set but no session loss was supplied. The "
                "check is not skipped: an unmeasured loss limit is not a limit"
            ),
            observed={"limit": format(limits.max_daily_loss, "f")},
        )
    passed = account.session_loss <= limits.max_daily_loss
    return GateCheck(
        "daily_loss",
        passed,
        detail=(
            f"session loss {account.session_loss} is within {limits.max_daily_loss}"
            if passed
            else f"session loss {account.session_loss} has reached the limit of "
            f"{limits.max_daily_loss}; no further orders are accepted"
        ),
        observed={
            "session_loss": format(account.session_loss, "f"),
            "limit": format(limits.max_daily_loss, "f"),
        },
    )


def _order_rate(limits: RiskLimits, account: AccountSnapshot) -> GateCheck:
    if limits.max_orders_per_minute is None:
        return GateCheck("order_rate", True, not_configured=True, detail="no limit set")
    passed = account.orders_in_last_minute < limits.max_orders_per_minute
    return GateCheck(
        "order_rate",
        passed,
        detail=(
            f"{account.orders_in_last_minute} orders in the last minute, under "
            f"{limits.max_orders_per_minute}"
            if passed
            else f"{account.orders_in_last_minute} orders already placed in the last "
            f"minute, at the limit of {limits.max_orders_per_minute}"
        ),
        observed={
            "in_last_minute": account.orders_in_last_minute,
            "limit": limits.max_orders_per_minute,
        },
    )


def _cash(
    request: OrderRequest,
    limits: RiskLimits,
    account: AccountSnapshot,
    notional: Decimal | None,
) -> GateCheck:
    """Cash for a buy, when the account asked to be held to it.

    Off by default and deliberately crude when on: it compares the order's
    notional against cash. It is **not** a margin check. A margin requirement is
    a broker's formula, the platform does not hold one, and a number produced
    here that looked like margin would be exactly the fabrication build spec 1.1
    exists to prevent.
    """
    if not limits.require_sufficient_cash:
        return GateCheck("cash", True, not_configured=True, detail="not enforced")
    if request.side is OrderSide.SELL:
        return GateCheck("cash", True, detail="a sell does not consume cash on this check")
    if notional is None:
        return GateCheck(
            "cash", False, detail="the order has no price, so its cash cost is unknown"
        )
    passed = notional <= account.cash
    return GateCheck(
        "cash",
        passed,
        detail=(
            f"cash cost {notional} is within the balance of {account.cash}"
            if passed
            else f"cash cost {notional} exceeds the balance of {account.cash}. This is a "
            "cash check and not a margin check: the platform holds no broker's "
            "margin formula and will not invent one"
        ),
        observed={"cost": format(notional, "f"), "cash": format(account.cash, "f")},
    )
