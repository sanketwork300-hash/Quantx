"""What one more position would do to a book's risk and margin.

Phase 11. Every measure here is a *pair* — the book as it stands and the book
with the proposed order in it — computed from the same snapshot, over the same
factor panel, with the same seed, on the same shock grid. That is what makes the
difference attributable to the order rather than to two runs disagreeing about
the market, the sample or the draw.

Two rules the code enforces rather than documents.

**A zero difference must mean a zero difference.** If the proposed position
cannot be repriced it lands in the exclusion list, the two exposure sets are
identical, and every delta below is exactly zero — which reads as "this order
adds no risk" and is the most dangerous sentence this module could produce. So
the combination reports whether the order actually joined the repriceable book,
and the caller refuses rather than reporting the zeros.

**One panel, not two.** The factor panel is built once over the *combined*
book and used for both sides. Building one panel per side would let a new
underlying change the sample the current book is measured on, and the difference
would then contain that change as well as the order.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from domains.portfolio.models import PositionGreeks
from domains.risk.exposure import ExposureSet
from domains.risk.factors import FactorPanel
from domains.risk.margin import MarginModel, MarginResult
from domains.risk.stress import ContributionDimension, StressResult, apply_scenario
from domains.risk.var import VaRMethod, VaRResult
from domains.risk.vulnerability import VulnerabilityResult, scan_vulnerability
from domains.scenarios.models import Scenario

INCREMENTAL_MODEL_VERSION = "incremental-risk@1.0.0"


class IncrementalWarning(StrEnum):
    ORDER_NOT_REPRICEABLE = "INCREMENTAL_ORDER_NOT_REPRICEABLE"
    ORDER_ON_A_NEW_UNDERLYING = "INCREMENTAL_ORDER_ON_A_NEW_UNDERLYING"
    SHARED_PANEL = "INCREMENTAL_ONE_PANEL_FOR_BOTH_SIDES"


@dataclass(frozen=True, slots=True)
class Movement:
    """One quantity, before and after. The change is derived, never supplied."""

    name: str
    unit: str
    current: float | None
    proposed: float | None

    @property
    def change(self) -> float | None:
        if self.current is None or self.proposed is None:
            return None
        return self.proposed - self.current

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "unit": self.unit,
            "current": self.current,
            "proposed": self.proposed,
            "change": self.change,
        }


@dataclass(frozen=True, slots=True)
class CombinedBook:
    """The current book, the order's own exposure, and the two together."""

    current: ExposureSet
    proposed: ExposureSet
    #: False when the proposed position could not be repriced and therefore did
    #: not enter ``proposed`` at all. Every delta would then be zero, which is
    #: why the caller must check this before reporting any of them.
    order_is_repriceable: bool
    order_exclusion_reason: str | None = None
    order_is_on_a_new_underlying: bool = False

    def to_dict(self) -> dict:
        return {
            "order_is_repriceable": self.order_is_repriceable,
            "order_exclusion_reason": self.order_exclusion_reason,
            "order_is_on_a_new_underlying": self.order_is_on_a_new_underlying,
            "current_positions": len(self.current.exposures),
            "proposed_positions": len(self.proposed.exposures),
            "current_base_value": self.current.base_value,
            "proposed_base_value": self.proposed.base_value,
        }


def combine(current: ExposureSet, order: ExposureSet) -> CombinedBook:
    """Append the order's exposure to the book, keeping every exclusion.

    ``order`` is the exposure set of a single hypothetical position, built by
    exactly the code that builds the book's own — so an order that cannot be
    repriced fails here for the same reason and with the same vocabulary a
    stored position would.
    """
    repriceable = bool(order.exposures)
    reason = str(order.excluded[0].reason) if order.excluded else None
    new_underlying = repriceable and any(
        exposure.underlying_key not in set(current.underlying_keys())
        for exposure in order.exposures
    )
    return CombinedBook(
        current=current,
        proposed=ExposureSet(
            exposures=(*current.exposures, *order.exposures),
            excluded=(*current.excluded, *order.excluded),
            base_currency=current.base_currency,
        ),
        order_is_repriceable=repriceable,
        order_exclusion_reason=reason,
        order_is_on_a_new_underlying=new_underlying,
    )


# ------------------------------------------------------------------- measures
@dataclass(frozen=True, slots=True)
class IncrementalGreeks:
    current: PositionGreeks
    proposed: PositionGreeks

    @property
    def movements(self) -> tuple[Movement, ...]:
        return (
            Movement(
                "delta", "base currency per unit of spot", self.current.delta, self.proposed.delta
            ),
            Movement("gamma", "delta per unit of spot", self.current.gamma, self.proposed.gamma),
            Movement(
                "vega_per_vol_point",
                "base currency per volatility point",
                self.current.vega_per_vol_point,
                self.proposed.vega_per_vol_point,
            ),
            Movement(
                "theta_per_day",
                "base currency per calendar day",
                self.current.theta_per_day,
                self.proposed.theta_per_day,
            ),
            Movement(
                "rho_per_bp",
                "base currency per basis point",
                self.current.rho_per_bp,
                self.proposed.rho_per_bp,
            ),
        )

    def to_dict(self) -> dict:
        return {
            "current": self.current.to_dict(),
            "proposed": self.proposed.to_dict(),
            "movements": [item.to_dict() for item in self.movements],
        }


@dataclass(frozen=True, slots=True)
class IncrementalVaR:
    """The same estimator, twice, on one panel."""

    method: VaRMethod
    current: VaRResult
    proposed: VaRResult
    warnings: tuple[str, ...] = field(default=())

    @property
    def movements(self) -> tuple[Movement, ...]:
        by_confidence = {item.confidence: item for item in self.current.tail_risks}
        movements: list[Movement] = []
        for tail in self.proposed.tail_risks:
            before = by_confidence.get(tail.confidence)
            movements.append(
                Movement(
                    f"value_at_risk_{tail.confidence:g}",
                    "base currency loss",
                    before.value_at_risk if before else None,
                    tail.value_at_risk,
                )
            )
            movements.append(
                Movement(
                    f"expected_shortfall_{tail.confidence:g}",
                    "base currency loss",
                    before.expected_shortfall if before else None,
                    tail.expected_shortfall,
                )
            )
        return tuple(movements)

    def to_dict(self) -> dict:
        return {
            "method": str(self.method),
            "horizon_days": self.proposed.horizon_days,
            "scenarios": self.proposed.scenarios,
            "current": self.current.to_dict(),
            "proposed": self.proposed.to_dict(),
            "movements": [item.to_dict() for item in self.movements],
            "warnings": list(self.warnings),
            "interpretation": (
                "Both sides are the same estimator run over one factor panel "
                "built from the combined book, with the same draws where the "
                "method draws any. The change is therefore the order's "
                "contribution and not the difference between two samples."
            ),
        }


@dataclass(frozen=True, slots=True)
class IncrementalStress:
    scenario_name: str
    current: StressResult
    proposed: StressResult

    @property
    def movements(self) -> tuple[Movement, ...]:
        return (
            Movement(
                "scenario_pnl",
                "base currency",
                self.current.revaluation.pnl,
                self.proposed.revaluation.pnl,
            ),
            Movement(
                "shocked_value",
                "base currency",
                self.current.revaluation.shocked_value,
                self.proposed.revaluation.shocked_value,
            ),
        )

    def to_dict(self) -> dict:
        return {
            "scenario": self.scenario_name,
            "current": self.current.to_dict(include_positions=False),
            "proposed": self.proposed.to_dict(include_positions=False),
            "movements": [item.to_dict() for item in self.movements],
        }


@dataclass(frozen=True, slots=True)
class IncrementalMargin:
    """Estimated margin, buffer and utilisation, before and after.

    Never a broker number and never a liquidation level: the same model that
    Phase 6 ships, run twice, and the disclaimer travels with it.
    """

    model: str
    current: VulnerabilityResult
    proposed: VulnerabilityResult

    @property
    def movements(self) -> tuple[Movement, ...]:
        return (
            Movement(
                "estimated_margin",
                self.proposed.currency,
                self.current.base.estimated_margin,
                self.proposed.base.estimated_margin,
            ),
            Movement(
                "estimated_buffer",
                self.proposed.currency,
                self.current.base_buffer,
                self.proposed.base_buffer,
            ),
            Movement(
                "estimated_utilisation",
                "fraction of eligible capital",
                self.current.base_utilisation,
                self.proposed.base_utilisation,
            ),
            Movement(
                "worst_loss_on_the_grid",
                self.proposed.currency,
                self.current.base.worst_loss,
                self.proposed.base.worst_loss,
            ),
        )

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "current": self.current.to_dict(include_ladder=False),
            "proposed": self.proposed.to_dict(include_ladder=True),
            "movements": [item.to_dict() for item in self.movements],
            "disclaimer": self.proposed.base.disclaimer,
        }


# -------------------------------------------------------------------- drivers
def greeks_of(exposures: ExposureSet) -> PositionGreeks:
    """The book's Greeks, summed from the exposures that carry them."""
    total = PositionGreeks()
    for exposure in exposures.exposures:
        total = total + exposure.greeks
    return total


def incremental_var(
    book: CombinedBook,
    panel: FactorPanel,
    method: VaRMethod,
    run,
) -> IncrementalVaR:
    """Run one VaR estimator over both sides of ``book``.

    ``run`` is the estimator, already bound to its confidences, horizon, seed
    and distribution, so the two calls cannot differ in any of them.
    """
    current = run(book.current, panel)
    proposed = run(book.proposed, panel)
    warnings = [IncrementalWarning.SHARED_PANEL]
    if book.order_is_on_a_new_underlying:
        warnings.append(IncrementalWarning.ORDER_ON_A_NEW_UNDERLYING)
    return IncrementalVaR(
        method=method,
        current=current,
        proposed=proposed,
        warnings=tuple(dict.fromkeys((*current.warnings, *proposed.warnings, *warnings))),
    )


def incremental_stress(
    book: CombinedBook, scenario: Scenario, time_decay_days: float = 0.0
) -> IncrementalStress:
    dimensions = (ContributionDimension.UNDERLYING, ContributionDimension.ASSET_CLASS)
    return IncrementalStress(
        scenario_name=scenario.name,
        current=apply_scenario(
            book.current, scenario, time_decay_days=time_decay_days, dimensions=dimensions
        ),
        proposed=apply_scenario(
            book.proposed, scenario, time_decay_days=time_decay_days, dimensions=dimensions
        ),
    )


def incremental_margin(
    book: CombinedBook,
    model: MarginModel,
    eligible_capital: float | None,
    ladder: tuple[float, ...],
    vol_co_shock: float,
) -> IncrementalMargin:
    def scan(exposures: ExposureSet) -> VulnerabilityResult:
        return scan_vulnerability(
            exposures,
            model,
            eligible_capital=eligible_capital,
            ladder=ladder,
            vol_co_shock=vol_co_shock,
        )

    return IncrementalMargin(
        model=model.identifier, current=scan(book.current), proposed=scan(book.proposed)
    )


def margin_of(exposures: ExposureSet, model: MarginModel) -> MarginResult:
    return model.calculate(exposures)
