"""Value at Risk by three methods, each honest about what it assumes.

The methods do not agree, and they are not supposed to. Historical and Monte
Carlo **fully reprice** the book under every scenario, so they stay valid for a
nonlinear position. Parametric scales a covariance by a normal quantile, which
is a statement about a linear portfolio and is returned as such — with a warning
attached whenever the book contains an option, and never as the only measure.

The horizon is handled by taking overlapping windows out of the actual return
series rather than multiplying a one-day number by the square root of time.
Square-root scaling assumes independent increments with no drift, which is
exactly what a stressed market stops having.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import StrEnum

import numpy as np

from domains.risk.exposure import ExposureSet
from domains.risk.factors import FactorKind, FactorPanel, FactorWarning
from domains.risk.revaluation import revalue_many
from quant.simulation.paths import Distribution, FactorModel, simulate_factor_returns
from quant.statistics.covariance import sample_covariance
from quant.statistics.var import (
    TailRisk,
    bootstrap_interval,
    historical_tail_risk,
    losses_from_pnl,
    parametric_tail_risk,
)

DEFAULT_CONFIDENCES = (0.95, 0.99)


class VaRMethod(StrEnum):
    HISTORICAL = "HISTORICAL"
    PARAMETRIC = "PARAMETRIC"
    MONTE_CARLO = "MONTE_CARLO"


class RiskWarning(StrEnum):
    PARAMETRIC_ON_OPTIONS = "RISK_PARAMETRIC_ON_NONLINEAR_BOOK"
    NO_FACTOR_HISTORY = "RISK_NO_FACTOR_HISTORY"
    POSITIONS_EXCLUDED = "RISK_POSITIONS_EXCLUDED"
    SINGLE_UNDERLYING = "RISK_SINGLE_UNDERLYING_NO_DIVERSIFICATION"


@dataclass(frozen=True, slots=True)
class VaRResult:
    method: VaRMethod
    horizon_days: int
    base_value: float
    tail_risks: tuple[TailRisk, ...]
    scenarios: int
    assumptions: dict
    panel: dict
    #: Bootstrap intervals for the point estimate, keyed by confidence level.
    estimate_intervals: dict[str, tuple[float, float]]
    worst_scenario_dates: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "method": str(self.method),
            "horizon_days": self.horizon_days,
            "base_value": self.base_value,
            "scenarios": self.scenarios,
            "tail_risk": [item.to_dict() for item in self.tail_risks],
            "estimate_intervals": {
                key: {"low": low, "high": high, "interval": 0.90}
                for key, (low, high) in self.estimate_intervals.items()
            },
            "assumptions": self.assumptions,
            "factor_panel": self.panel,
            "worst_scenario_dates": list(self.worst_scenario_dates),
            "warnings": list(self.warnings),
        }


def _shock_maps(
    panel: FactorPanel, underlying_keys: tuple[str, ...]
) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray], bool]:
    """Split a factor panel into the spot and volatility moves each key sees."""
    spot: dict[str, np.ndarray] = {}
    vol: dict[str, np.ndarray] = {}
    for index, factor in enumerate(panel.factors):
        column = panel.returns[:, index]
        if factor.kind is FactorKind.SPOT_RETURN:
            spot[factor.target] = column
        else:
            vol[factor.target] = column

    rows = panel.observations
    zeros = np.zeros(rows, dtype=float)
    for key in underlying_keys:
        spot.setdefault(key, zeros)
    vol_modelled = any(key in vol for key in underlying_keys)
    return spot, vol, vol_modelled


def _common_warnings(exposures: ExposureSet, vol_modelled: bool) -> list[str]:
    warnings: list[str] = []
    if exposures.excluded:
        warnings.append(RiskWarning.POSITIONS_EXCLUDED)
    if not vol_modelled and any(item.is_option for item in exposures.exposures):
        warnings.append(FactorWarning.VOLATILITY_HELD_CONSTANT)
    if len(exposures.underlying_keys()) == 1:
        warnings.append(RiskWarning.SINGLE_UNDERLYING)
    return warnings


def historical_var(
    exposures: ExposureSet,
    panel: FactorPanel,
    confidences: tuple[float, ...] = DEFAULT_CONFIDENCES,
    horizon_days: int = 1,
    seed: int = 20_260_924,
) -> VaRResult:
    """Reprice the book under every historical move the platform holds."""
    keys = exposures.underlying_keys()
    spot, vol, vol_modelled = _shock_maps(panel, keys)
    pnl = revalue_many(exposures, spot, vol)
    losses = losses_from_pnl(pnl)

    warnings = _common_warnings(exposures, vol_modelled)
    warnings.extend(panel.warnings)

    worst = np.argsort(-losses)[: min(5, losses.size)]
    dates: tuple[str, ...] = ()
    if panel.dates and losses.size == len(panel.dates):
        dates = tuple(panel.dates[int(i)].isoformat() for i in worst)

    return VaRResult(
        method=VaRMethod.HISTORICAL,
        horizon_days=horizon_days,
        base_value=exposures.base_value,
        tail_risks=tuple(historical_tail_risk(losses, level) for level in confidences),
        scenarios=int(losses.size),
        assumptions={
            "repricing": "full",
            "distribution": "the empirical sample; none is assumed",
            "horizon": (
                f"{horizon_days}-day moves taken from the series itself"
                if horizon_days > 1
                else "one observation interval, which is one ingested chain to the next"
            ),
            "volatility": (
                "shocked by its own observed history"
                if vol_modelled
                else "held constant; no volatility history was available"
            ),
        },
        panel=panel.to_dict(),
        estimate_intervals={
            f"{level:.2f}": bootstrap_interval(losses, level, seed=seed) for level in confidences
        },
        worst_scenario_dates=dates,
        warnings=tuple(dict.fromkeys(warnings)),
    )


def parametric_var(
    exposures: ExposureSet,
    panel: FactorPanel,
    confidences: tuple[float, ...] = DEFAULT_CONFIDENCES,
    horizon_days: int = 1,
) -> VaRResult:
    """Covariance times a normal quantile, on a delta-and-vega linearisation.

    Included because it is the standard measure and because seeing it disagree
    with the full repricing is informative. It is never returned alone for a
    book with options in it.
    """
    exposure_vector = []
    for factor in panel.factors:
        members = [e for e in exposures.exposures if e.underlying_key == factor.target]
        if factor.kind is FactorKind.SPOT_RETURN:
            # Sensitivity to a 1.0 return, i.e. a 100% move: delta * spot.
            exposure_vector.append(sum(e.greeks.delta * e.spot for e in members))
        else:
            # Sensitivity to 1.0 of volatility: vega is quoted per 0.01.
            exposure_vector.append(sum(e.greeks.vega_per_vol_point / 0.01 for e in members))

    weights = np.asarray(exposure_vector, dtype=float)
    estimate = sample_covariance(panel.names, panel.returns)
    variance = float(weights @ estimate.covariance @ weights)
    std_pnl = float(np.sqrt(max(variance, 0.0)))
    mean_pnl = float(weights @ estimate.mean)

    has_options = any(item.is_option for item in exposures.exposures)
    warnings = _common_warnings(exposures, vol_modelled=True)
    warnings.extend(panel.warnings)
    warnings.extend(estimate.warnings)
    if has_options:
        warnings.append(RiskWarning.PARAMETRIC_ON_OPTIONS)

    return VaRResult(
        method=VaRMethod.PARAMETRIC,
        horizon_days=horizon_days,
        base_value=exposures.base_value,
        tail_risks=tuple(
            parametric_tail_risk(-mean_pnl, std_pnl, level, panel.observations)
            for level in confidences
        ),
        scenarios=panel.observations,
        assumptions={
            "repricing": "none; a first-order expansion in delta and vega",
            "distribution": "multivariate normal factor returns",
            "covariance": estimate.to_dict(),
            "validity": (
                "Approximately linear exposures only. This book contains options, "
                "so the estimate ignores convexity and is reported for comparison "
                "with the full repricing, not in place of it."
                if has_options
                else "Linear exposures, which this book is."
            ),
        },
        panel=panel.to_dict(),
        estimate_intervals={},
        warnings=tuple(dict.fromkeys(warnings)),
    )


def monte_carlo_var(
    exposures: ExposureSet,
    panel: FactorPanel,
    paths: int = 10_000,
    seed: int = 20_260_924,
    confidences: tuple[float, ...] = DEFAULT_CONFIDENCES,
    horizon_days: int = 1,
    distribution: Distribution = Distribution.NORMAL,
    degrees_of_freedom: float = 5.0,
) -> VaRResult:
    """Simulate factor moves from the estimated covariance, then fully reprice.

    The drift is set to zero rather than estimated. A mean return from a few
    dozen observations is indistinguishable from noise, and letting it into a
    one-day risk number would put a trend nobody measured into the answer.
    """
    keys = exposures.underlying_keys()
    estimate = sample_covariance(panel.names, panel.returns)
    model = FactorModel(
        factors=estimate.factors,
        mean=np.zeros(len(estimate.factors)),
        covariance=estimate.covariance,
        distribution=distribution,
        degrees_of_freedom=degrees_of_freedom,
    )
    simulation = simulate_factor_returns(model, paths=paths, seed=seed)

    spot: dict[str, np.ndarray] = {}
    vol: dict[str, np.ndarray] = {}
    for index, factor in enumerate(panel.factors):
        column = simulation.draws[:, index]
        if factor.kind is FactorKind.SPOT_RETURN:
            spot[factor.target] = column
        else:
            vol[factor.target] = column

    zeros = np.zeros(simulation.paths, dtype=float)
    for key in keys:
        spot.setdefault(key, zeros)

    losses = losses_from_pnl(revalue_many(exposures, spot, vol))
    vol_modelled = any(key in vol for key in keys)

    warnings = _common_warnings(exposures, vol_modelled)
    warnings.extend(panel.warnings)
    warnings.extend(estimate.warnings)

    return VaRResult(
        method=VaRMethod.MONTE_CARLO,
        horizon_days=horizon_days,
        base_value=exposures.base_value,
        tail_risks=tuple(historical_tail_risk(losses, level) for level in confidences),
        scenarios=simulation.paths,
        assumptions={
            "repricing": "full",
            "factor_model": model.to_dict(),
            "simulation": simulation.to_dict(),
            "drift": "zero; a mean estimated from this many observations is noise",
            "reproducibility": (
                f"Seed {seed} with {simulation.paths} antithetic paths. The same "
                "seed and the same factor panel reproduce these numbers exactly."
            ),
        },
        panel=panel.to_dict(),
        estimate_intervals={
            f"{level:.2f}": bootstrap_interval(losses, level, seed=seed + 1)
            for level in confidences
        },
        warnings=tuple(dict.fromkeys(warnings)),
    )


def worst_dates(panel: FactorPanel, losses: np.ndarray, count: int = 5) -> tuple[date, ...]:
    order = np.argsort(-losses)[:count]
    return tuple(panel.dates[int(i)] for i in order if int(i) < len(panel.dates))
