"""What a trade costs, and the refusal to guess.

Indian equity and derivative trading carries brokerage, exchange transaction
charges, SEBI turnover fees, STT/CTT, stamp duty and GST. Every one of those is
set by an exchange, a regulator or a broker; several differ by segment, by side,
and by whether a position is squared off intraday; and all of them change.

**This platform does not know them, and will not invent them.** Build spec 1.1
puts exchange rules and broker fee schedules in the same category as contract
multipliers: an unknown one is declared, not defaulted to a plausible number. A
backtest run with fabricated Indian tax rates would produce a net return that is
wrong in a way nobody could detect from the output.

So a cost schedule is **supplied**, component by component, each naming its own
basis. A backtest with no schedule is not a backtest with zero costs — it is a
**gross** backtest, and it says so on every figure it produces. That distinction
is the whole point of this module: silently assuming free trading is the single
most common way a backtest reports returns that do not exist.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

#: Money is rounded to paise/cents at each component, as a broker's contract
#: note does. Carrying twelve decimals of a tax through a year of trades would
#: accumulate a difference from the statement it is meant to reproduce.
MONEY = Decimal("0.01")


class CostBasis(StrEnum):
    """What a component is charged on.

    Named rather than inferred, because "0.0003" means very different money
    depending on whether it multiplies turnover, quantity or the trade count.
    """

    #: A fraction of the traded notional (price x quantity).
    TURNOVER = "TURNOVER"
    #: A fixed amount per unit traded.
    PER_UNIT = "PER_UNIT"
    #: A fixed amount per order, regardless of size.
    PER_ORDER = "PER_ORDER"
    #: A fraction of the sum of other components — how GST on brokerage and
    #: exchange charges is levied.
    ON_OTHER_COMPONENTS = "ON_OTHER_COMPONENTS"


class CostSide(StrEnum):
    """Which side a component applies to.

    STT on Indian equity delivery is charged on both sides; on intraday it is
    charged on the sell alone. A model that could not express that would have to
    approximate one of them.
    """

    BOTH = "BOTH"
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True, slots=True)
class CostComponent:
    """One line on a contract note."""

    name: str
    basis: CostBasis
    rate: Decimal
    side: CostSide = CostSide.BOTH
    #: Cap per order, where the schedule has one (brokerage commonly does).
    maximum: Decimal | None = None
    minimum: Decimal | None = None
    #: Components this one is charged on, for ``ON_OTHER_COMPONENTS``. Empty
    #: means all of them.
    applies_to: tuple[str, ...] = ()

    def applies(self, is_buy: bool) -> bool:
        if self.side is CostSide.BOTH:
            return True
        return (self.side is CostSide.BUY) == is_buy

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "basis": str(self.basis),
            "rate": format(self.rate, "f"),
            "side": str(self.side),
            "maximum": format(self.maximum, "f") if self.maximum is not None else None,
            "minimum": format(self.minimum, "f") if self.minimum is not None else None,
            "applies_to": list(self.applies_to),
        }


@dataclass(frozen=True, slots=True)
class ChargedComponent:
    name: str
    amount: Decimal

    def to_dict(self) -> dict:
        return {"name": self.name, "amount": format(self.amount, "f")}


@dataclass(frozen=True, slots=True)
class TradeCost:
    """What one trade paid, itemised."""

    components: tuple[ChargedComponent, ...] = ()
    #: True when no schedule was supplied. Every figure derived from this trade
    #: is then a **gross** figure, and says so rather than reading as net.
    modelled: bool = True

    @property
    def total(self) -> Decimal:
        return sum((item.amount for item in self.components), Decimal(0))

    def to_dict(self) -> dict:
        return {
            "total": format(self.total, "f"),
            "modelled": self.modelled,
            "components": [item.to_dict() for item in self.components],
        }


@dataclass(frozen=True, slots=True)
class CostSchedule:
    """A named set of components, supplied by whoever knows the rates.

    ``name`` and ``source`` are not decoration: a net return is only meaningful
    alongside a statement of what was deducted from it, and that statement has
    to travel with the result into the experiment record.
    """

    name: str
    components: tuple[CostComponent, ...] = ()
    #: Where the rates came from. Recorded verbatim into provenance so a later
    #: reader can check them against the schedule that was actually in force.
    source: str = "unspecified"

    @property
    def models_costs(self) -> bool:
        return bool(self.components)

    def charge(self, price: Decimal, quantity: Decimal, is_buy: bool) -> TradeCost:
        """Itemise the cost of one trade.

        Components charged on other components are evaluated last, so a GST-like
        line sees the brokerage and exchange charges it is levied on.
        """
        if not self.components:
            return TradeCost(components=(), modelled=False)

        turnover = abs(price * quantity)
        units = abs(quantity)
        charged: list[ChargedComponent] = []
        derived: list[CostComponent] = []

        for component in self.components:
            if not component.applies(is_buy):
                continue
            if component.basis is CostBasis.ON_OTHER_COMPONENTS:
                derived.append(component)
                continue
            if component.basis is CostBasis.TURNOVER:
                amount = turnover * component.rate
            elif component.basis is CostBasis.PER_UNIT:
                amount = units * component.rate
            else:
                amount = component.rate
            charged.append(ChargedComponent(component.name, _bound(amount, component)))

        for component in derived:
            base = sum(
                (
                    item.amount
                    for item in charged
                    if not component.applies_to or item.name in component.applies_to
                ),
                Decimal(0),
            )
            charged.append(
                ChargedComponent(component.name, _bound(base * component.rate, component))
            )

        return TradeCost(components=tuple(charged), modelled=True)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "source": self.source,
            "models_costs": self.models_costs,
            "components": [item.to_dict() for item in self.components],
        }


def _bound(amount: Decimal, component: CostComponent) -> Decimal:
    value = amount
    if component.maximum is not None:
        value = min(value, component.maximum)
    if component.minimum is not None:
        value = max(value, component.minimum)
    return value.quantize(MONEY, rounding=ROUND_HALF_UP)


#: The absence of a cost model, named so it cannot be mistaken for one.
#:
#: Not a claim that trading is free. It answers "what did this strategy's
#: *decisions* earn against the observed prices", which is the part of the
#: question that does not depend on a fee schedule nobody has supplied. Every
#: result computed with it is labelled gross.
NO_COST_MODEL = CostSchedule(
    name="none",
    components=(),
    source=(
        "no cost schedule was supplied. This is not zero cost: results are gross "
        "of brokerage, exchange charges, statutory levies and taxes, and are "
        "labelled as such."
    ),
)


def schedule_from_components(
    name: str, components: Sequence[dict], source: str = "unspecified"
) -> CostSchedule:
    """Build a schedule from plain dicts, as an API request supplies them."""
    return CostSchedule(
        name=name,
        source=source,
        components=tuple(
            CostComponent(
                name=str(item["name"]),
                basis=CostBasis(item["basis"]),
                rate=Decimal(str(item["rate"])),
                side=CostSide(item.get("side", CostSide.BOTH)),
                maximum=(
                    Decimal(str(item["maximum"])) if item.get("maximum") is not None else None
                ),
                minimum=(
                    Decimal(str(item["minimum"])) if item.get("minimum") is not None else None
                ),
                applies_to=tuple(item.get("applies_to") or ()),
            )
            for item in components
        ),
    )


@dataclass(frozen=True, slots=True)
class SlippageModel:
    """How far from the reference price a fill is assumed to happen.

    Deliberately crude and deliberately explicit. A backtest on daily bars has
    no order book, so any slippage figure is an assumption; this one is a stated
    number of basis points against the fill price, adverse by construction, and
    it is recorded on the result rather than buried in it.

    Zero is a legitimate choice and is labelled the same way the absent cost
    schedule is: not a claim that fills are free, a statement that slippage was
    not modelled.
    """

    basis_points: Decimal = Decimal(0)
    source: str = "unspecified"

    @property
    def models_slippage(self) -> bool:
        return self.basis_points != 0

    def apply(self, price: Decimal, is_buy: bool) -> Decimal:
        """Move the price against the trader."""
        if not self.basis_points:
            return price
        adjustment = price * self.basis_points / Decimal(10_000)
        return price + adjustment if is_buy else price - adjustment

    def to_dict(self) -> dict:
        return {
            "basis_points": format(self.basis_points, "f"),
            "models_slippage": self.models_slippage,
            "source": self.source,
        }


NO_SLIPPAGE_MODEL = SlippageModel(
    basis_points=Decimal(0),
    source=(
        "no slippage was modelled. Fills are at the reference price, which no "
        "real order achieves; results are optimistic by an unmeasured amount."
    ),
)
