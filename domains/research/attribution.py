"""Where a backtest's money came from and went.

An equity curve says what happened. Attribution says *why*, and the honest form
of it is an identity that closes:

```
final equity - initial equity
    = realised P&L + unrealised P&L - trading costs
```

Every term is measured, none is a residual absorbing the others, and the sum is
checked. `reconciles` is on every report: an attribution that does not add up to
the equity change is not an approximation of the truth, it is a bug, and saying
so is more useful than presenting three plausible numbers.

**Slippage is reported but is not a term in that identity**, and getting this
wrong is easy. A fill is booked at the price it actually paid — slippage
included — so the price P&L already has slippage inside it. Subtracting it again
would double-count, and the residual would silently absorb the difference. What
the slippage figure answers is a different question: *how much of the price P&L
was given up against the reference price*, which is worth knowing precisely
because it is an assumption rather than a fee.

**Greek attribution is deliberately absent from this phase.** Splitting an option
book's P&L into delta, gamma, theta and vega needs a repriced surface at every
step, which the platform has but which a bar-series backtest does not produce.
Reporting those terms as zero for an equity strategy would be worse than not
reporting them: zero theta reads as "no time decay", not as "not applicable".
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from domains.research.models import Fill

ATTRIBUTION_MODEL_VERSION = "backtest-attribution@1.0.0"

#: Money reconciles to the paise. Anything larger is an accounting error rather
#: than rounding, and the report says which.
TOLERANCE = Decimal("0.01")


@dataclass(frozen=True, slots=True)
class InstrumentAttribution:
    """One instrument's contribution."""

    instrument_id: uuid.UUID
    realised_pnl: Decimal
    unrealised_pnl: Decimal
    costs: Decimal
    slippage: Decimal
    traded_notional: Decimal
    fills: int

    @property
    def gross_pnl(self) -> Decimal:
        return self.realised_pnl + self.unrealised_pnl

    @property
    def net_pnl(self) -> Decimal:
        """Gross less fees. Slippage is already inside ``gross_pnl``."""
        return self.gross_pnl - self.costs

    def to_dict(self) -> dict:
        return {
            "instrument_id": str(self.instrument_id),
            "realised_pnl": format(self.realised_pnl, "f"),
            "unrealised_pnl": format(self.unrealised_pnl, "f"),
            "gross_pnl": format(self.gross_pnl, "f"),
            "costs": format(self.costs, "f"),
            "slippage_against_reference": format(self.slippage, "f"),
            "net_pnl": format(self.net_pnl, "f"),
            "traded_notional": format(self.traded_notional, "f"),
            "fills": self.fills,
        }


@dataclass(frozen=True, slots=True)
class AttributionReport:
    """The decomposition, and whether it closes."""

    initial_equity: Decimal
    final_equity: Decimal
    realised_pnl: Decimal
    unrealised_pnl: Decimal
    costs: Decimal
    #: Given up against the reference price. Reported, not subtracted — see the
    #: module docstring and :attr:`attributed`.
    slippage: Decimal
    instruments: tuple[InstrumentAttribution, ...] = ()
    model_version: str = ATTRIBUTION_MODEL_VERSION

    @property
    def equity_change(self) -> Decimal:
        return self.final_equity - self.initial_equity

    @property
    def attributed(self) -> Decimal:
        """The identity's right-hand side.

        Slippage is **not** here: it is already inside the fill prices that
        produced the realised and unrealised P&L, and subtracting it again would
        double-count it into the residual.
        """
        return self.realised_pnl + self.unrealised_pnl - self.costs

    @property
    def residual(self) -> Decimal:
        """What the terms failed to explain. Should be zero to the paise."""
        return self.equity_change - self.attributed

    @property
    def reconciles(self) -> bool:
        return abs(self.residual) <= TOLERANCE

    def to_dict(self) -> dict:
        return {
            "initial_equity": format(self.initial_equity, "f"),
            "final_equity": format(self.final_equity, "f"),
            "equity_change": format(self.equity_change, "f"),
            "realised_pnl": format(self.realised_pnl, "f"),
            "unrealised_pnl": format(self.unrealised_pnl, "f"),
            "costs": format(self.costs, "f"),
            "slippage_against_reference": format(self.slippage, "f"),
            "attributed": format(self.attributed, "f"),
            "residual": format(self.residual, "f"),
            "reconciles": self.reconciles,
            "model_version": self.model_version,
            "instruments": [item.to_dict() for item in self.instruments],
            "identity": (
                "equity_change = realised_pnl + unrealised_pnl - costs. Slippage is "
                "already inside the fill prices and is reported separately rather "
                "than subtracted a second time."
            ),
            "not_attributed": {
                "greeks": (
                    "Delta, gamma, theta and vega attribution needs a repriced surface "
                    "at every step, which a bar-series backtest does not produce. The "
                    "terms are absent rather than zero: a zero theta would read as 'no "
                    "time decay' rather than as 'not applicable'."
                ),
                "factors": (
                    "Sector and factor attribution needs an exposure model this phase "
                    "does not build."
                ),
            },
        }


def attribute(
    initial_equity: Decimal,
    final_equity: Decimal,
    fills: Sequence[Fill],
    realised_by_instrument: dict[uuid.UUID, Decimal],
    unrealised_by_instrument: dict[uuid.UUID, Decimal],
) -> AttributionReport:
    """Decompose the equity change into the terms that produced it.

    ``slippage`` is separated from ``costs`` on purpose: one is a fee schedule
    and the other is an execution assumption, and a strategy whose returns
    depend on which is which should be able to see both. It is reported beside
    the reconciliation rather than inside it, because the fill prices already
    contain it.
    """
    instruments = sorted(set(realised_by_instrument) | set(unrealised_by_instrument), key=str)
    per_instrument: list[InstrumentAttribution] = []

    for instrument_id in instruments:
        instrument_fills = [fill for fill in fills if fill.instrument_id == instrument_id]
        per_instrument.append(
            InstrumentAttribution(
                instrument_id=instrument_id,
                realised_pnl=realised_by_instrument.get(instrument_id, Decimal(0)),
                unrealised_pnl=unrealised_by_instrument.get(instrument_id, Decimal(0)),
                costs=sum((fill.cost.total for fill in instrument_fills), Decimal(0)),
                slippage=sum((fill.slippage_cost for fill in instrument_fills), Decimal(0)),
                traded_notional=sum((abs(fill.notional) for fill in instrument_fills), Decimal(0)),
                fills=len(instrument_fills),
            )
        )

    return AttributionReport(
        initial_equity=initial_equity,
        final_equity=final_equity,
        realised_pnl=sum(realised_by_instrument.values(), Decimal(0)),
        unrealised_pnl=sum(unrealised_by_instrument.values(), Decimal(0)),
        costs=sum((fill.cost.total for fill in fills), Decimal(0)),
        slippage=sum((fill.slippage_cost for fill in fills), Decimal(0)),
        instruments=tuple(per_instrument),
    )
