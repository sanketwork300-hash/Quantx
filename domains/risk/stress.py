"""Applying a scenario to a portfolio, and attributing the result.

Each group's contribution is its own **fully repriced** P&L — not its share of
a delta-allocated total. That distinction is the one that matters: a short
put's contribution to a 10% sell-off is what the option is actually worth
afterwards, which is not what its delta predicted.

The decomposition is exact here, and the reason is worth stating because it will
stop being true. Portfolio value is a sum over positions, and each position is
repriced from its own anchors, so the group P&Ls add up to the total with
nothing left over. The equivalent-but-costlier construction — rerun the whole
scenario with one group held flat and difference the totals — gives the same
numbers, and a test asserts that it does. A portfolio-level quantity that is not
a sum over positions, such as the netted margin arriving in Phase 6, will not
decompose this cleanly, and the residual reported here is the check that will
notice when that day comes.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from domains.risk.exposure import ExposureSet, PositionExposure
from domains.risk.revaluation import RevaluationResult, resolve_scenario, revalue
from domains.scenarios.models import Scenario


class ContributionDimension(StrEnum):
    UNDERLYING = "UNDERLYING"
    EXPIRY = "EXPIRY"
    ASSET_CLASS = "ASSET_CLASS"
    STRATEGY_TAG = "STRATEGY_TAG"
    INSTRUMENT = "INSTRUMENT"


@dataclass(frozen=True, slots=True)
class RiskContribution:
    dimension: str
    key: str
    positions: int
    base_value: float
    contribution: float
    #: Share of the total move. Omitted rather than shown as a misleading 0%
    #: when the total is itself near zero.
    share: float | None

    def to_dict(self) -> dict:
        return {
            "dimension": self.dimension,
            "key": self.key,
            "positions": self.positions,
            "base_value": self.base_value,
            "contribution": self.contribution,
            "share": self.share,
        }


@dataclass(frozen=True, slots=True)
class ContributionBreakdown:
    dimension: str
    contributions: tuple[RiskContribution, ...]
    total_pnl: float
    #: total - sum(contributions) - ungrouped. Zero by construction while
    #: portfolio value is a sum over independently repriced positions; reported
    #: anyway, because it is the assertion that this is still true. The
    #: ungrouped part is subtracted rather than left in, so a dimension that
    #: some positions do not carry does not masquerade as a modelling residual.
    residual: float
    #: Positions that carry no key for this dimension, and so appear in no
    #: bucket. An index leg has no expiry; it is left out rather than filed
    #: under a fabricated one, and counted here so the omission is visible.
    ungrouped_positions: int = 0
    ungrouped_pnl: float = 0.0

    def to_dict(self) -> dict:
        return {
            "dimension": self.dimension,
            "total_pnl": self.total_pnl,
            "residual": self.residual,
            "residual_note": (
                "Zero while a portfolio's value is a sum over positions repriced "
                "independently, which is the case for every method in this phase. "
                "It is reported as the check on that, not as a plug."
            ),
            "ungrouped_positions": self.ungrouped_positions,
            "ungrouped_pnl": self.ungrouped_pnl,
            "contributions": [item.to_dict() for item in self.contributions],
        }


@dataclass(frozen=True, slots=True)
class StressResult:
    scenario_id: str
    scenario_name: str
    scenario_source: str
    revaluation: RevaluationResult
    breakdowns: tuple[ContributionBreakdown, ...]
    excluded_positions: int
    excluded_value: float

    def to_dict(self, include_positions: bool = True) -> dict:
        return {
            "scenario": {
                "id": self.scenario_id,
                "name": self.scenario_name,
                "source": self.scenario_source,
            },
            **self.revaluation.to_dict(include_positions=include_positions),
            "contributions": [item.to_dict() for item in self.breakdowns],
            "excluded_positions": self.excluded_positions,
            "excluded_reported_value": self.excluded_value,
        }


def _expiry_of(exposure: PositionExposure) -> str | None:
    parts = exposure.canonical_key.split(":")
    return parts[3] if len(parts) > 3 else None


#: Keyed off the exposure, for the hold-one-flat oracle.
_KEY_OF = {
    ContributionDimension.UNDERLYING: lambda e: e.underlying_key,
    ContributionDimension.EXPIRY: _expiry_of,
    ContributionDimension.ASSET_CLASS: lambda e: str(e.asset_class),
    ContributionDimension.STRATEGY_TAG: lambda e: e.strategy_tag,
    ContributionDimension.INSTRUMENT: lambda e: str(e.instrument_id),
}

#: Keyed off the revaluation row, which already carries the grouping keys.
_POSITION_KEY_OF = {
    ContributionDimension.UNDERLYING: lambda p: p.underlying_key,
    ContributionDimension.EXPIRY: lambda p: p.expiry_key,
    ContributionDimension.ASSET_CLASS: lambda p: p.asset_class,
    ContributionDimension.STRATEGY_TAG: lambda p: p.strategy_tag,
    ContributionDimension.INSTRUMENT: lambda p: p.canonical_key,
}


def apply_scenario(
    exposures: ExposureSet,
    scenario: Scenario,
    time_decay_days: float = 0.0,
    dimensions: tuple[ContributionDimension, ...] = (
        ContributionDimension.UNDERLYING,
        ContributionDimension.EXPIRY,
        ContributionDimension.ASSET_CLASS,
        ContributionDimension.STRATEGY_TAG,
    ),
) -> StressResult:
    shocks = resolve_scenario(scenario, exposures.underlying_keys())
    full = revalue(exposures, shocks, time_decay_days=time_decay_days)

    breakdowns = tuple(_breakdown(full, dimension) for dimension in dimensions)
    return StressResult(
        scenario_id=str(scenario.id),
        scenario_name=scenario.name,
        scenario_source=str(scenario.source),
        revaluation=full,
        breakdowns=tuple(b for b in breakdowns if b.contributions),
        excluded_positions=len(exposures.excluded),
        excluded_value=exposures.excluded_reported_value,
    )


def _breakdown(full: RevaluationResult, dimension: ContributionDimension) -> ContributionBreakdown:
    key_of = _POSITION_KEY_OF[dimension]
    groups: dict[str, list] = {}
    ungrouped: list = []
    for position in full.positions:
        key = key_of(position)
        if key is None:
            ungrouped.append(position)
        else:
            groups.setdefault(str(key), []).append(position)

    total_pnl = full.pnl
    contributions = [
        RiskContribution(
            dimension=str(dimension),
            key=key,
            positions=len(members),
            base_value=sum(item.base_value for item in members),
            contribution=sum(item.pnl for item in members),
            share=(
                sum(item.pnl for item in members) / total_pnl if abs(total_pnl) > 1e-9 else None
            ),
        )
        for key, members in sorted(groups.items())
    ]
    contributions.sort(key=lambda item: item.contribution)

    ungrouped_pnl = sum(item.pnl for item in ungrouped)
    return ContributionBreakdown(
        dimension=str(dimension),
        contributions=tuple(contributions),
        total_pnl=total_pnl,
        residual=total_pnl - sum(item.contribution for item in contributions) - ungrouped_pnl,
        ungrouped_positions=len(ungrouped),
        ungrouped_pnl=ungrouped_pnl,
    )


def contributions_by_holding_flat(
    exposures: ExposureSet,
    shocks: dict,
    dimension: ContributionDimension,
    time_decay_days: float = 0.0,
) -> dict[str, float]:
    """The costly construction of the same numbers, kept as a test oracle.

    Reruns the whole scenario once per group with that group standing still.
    Not used in production — it is O(groups x positions) for an answer the cheap
    path gets exactly — but a test compares the two, so the claim that the
    decomposition is exact is checked rather than asserted.
    """
    key_of = _KEY_OF[dimension]
    total = revalue(exposures, shocks, time_decay_days=time_decay_days).pnl

    groups: dict[str, set] = {}
    for exposure in exposures.exposures:
        key = key_of(exposure)
        if key is not None:
            groups.setdefault(str(key), set()).add(exposure.position_id)

    return {
        key: total
        - revalue(
            exposures,
            shocks,
            time_decay_days=time_decay_days,
            include=lambda exposure, ids=ids: exposure.position_id not in ids,
        ).pnl
        for key, ids in sorted(groups.items())
    }
