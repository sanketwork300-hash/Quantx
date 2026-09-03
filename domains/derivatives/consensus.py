"""Model consensus: several models, one dispersion, no single "true" price.

Every model here is wrong. Black-Scholes-Merton assumes a volatility that the
smile says does not exist; the local-volatility PDE reproduces today's surface
exactly and gets tomorrow's dynamics wrong; Heston has the right kind of
dynamics and only approximately the right surface; Monte Carlo carries a
standard error. Picking one of them and calling its output *the* value would
hide the only thing the comparison actually establishes.

So this module never returns a single price. It returns

* ``reference_value`` — the **median** of the models that produced a number,
  labelled as a reference and not as a value the contract is worth,
* ``reference_range`` — the interval the models actually spanned,
* ``model_dispersion`` — how far apart they were, absolutely and relatively,
* every individual model's value, method and diagnostics,
* a ``confidence`` score whose contributions are enumerable, so a low score
  always comes with the reason it is low.

If the models disagree by five percent, the user sees that they disagree by five
percent. That disagreement is the most honest thing the platform can say about
the contract, and it is deliberately more prominent in the output than the
median is.

A model that cannot run does not fail the consensus. It returns ``None`` with a
named reason, the consensus continues over the remaining models, and the reduced
model count is visible in the confidence score rather than silently absorbed.

There is no ``best_model`` field and there never will be: choosing between
models is a judgement about which set of wrong assumptions is least wrong for
one contract on one day, and the platform is not in a position to make it.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

import numpy as np

from domains.reports.warnings import AnalyticalWarning
from quant.numerical.pde import GridSpec, PDEError, solve_local_vol_pde
from quant.pricing.black_scholes import bsm_price
from quant.pricing.heston import HestonError, HestonParameters, heston_price
from quant.pricing.monte_carlo import MonteCarloError, monte_carlo_price
from quant.statistics.scoring import ratio_penalty_score, weighted_geometric_mean

CONSENSUS_MODEL_VERSION = "consensus@1.0.0"

#: Relative dispersion at which model agreement scores one half. Five percent is
#: not a claim that models agreeing to 4.9% are fine; it is the scale on which
#: the agreement contribution is measured, and it is reported in the provenance
#: so a reader can rescale it.
#:
#: The transform is the quadratic ratio penalty the quality engine uses for
#: spreads, not a linear ramp to zero: there is no dispersion at which model
#: agreement becomes *exactly* worthless, and a ramp would score a 5.1% spread
#: and a 50% spread identically at zero, which through a geometric mean would
#: collapse the whole confidence to zero for both.
AGREEMENT_REFERENCE_DISPERSION = 0.05

#: Below this price a *relative* dispersion is meaningless — two models both
#: rounding a near-worthless option differently produce a 100% disagreement that
#: says nothing. Relative figures are withheld rather than reported as huge.
MIN_PRICE_FOR_RELATIVE = 1e-6


class ConsensusWarningCode:
    NO_MODEL_PRODUCED_A_VALUE = "CONSENSUS_NO_MODEL_PRODUCED_A_VALUE"
    SINGLE_MODEL = "CONSENSUS_SINGLE_MODEL"
    MODEL_UNAVAILABLE = "CONSENSUS_MODEL_UNAVAILABLE"
    WIDE_DISPERSION = "CONSENSUS_WIDE_DISPERSION"
    NEGLIGIBLE_PRICE = "CONSENSUS_NEGLIGIBLE_PRICE"


class PricingModelKind(StrEnum):
    BLACK_SCHOLES_MERTON = "BLACK_SCHOLES_MERTON"
    LOCAL_VOL_PDE = "LOCAL_VOL_PDE"
    HESTON = "HESTON"
    MONTE_CARLO = "MONTE_CARLO"


@dataclass(frozen=True, slots=True)
class ConsensusInputs:
    """One contract, and the market state every model reads from.

    All four models see the *same* spot, rate, dividend and maturity. That is
    the point of the comparison: if they were each given their own inputs the
    dispersion would measure the inputs rather than the models.
    """

    spot: float
    strike: float
    tau: float
    rate: float
    dividend: float
    is_call: bool = True

    #: The fitted surface's reference implied volatility at this contract.
    #: Absent when the surface could not produce one, in which case the two
    #: models that need a single volatility report themselves unavailable.
    reference_volatility: float | None = None

    #: ``sigma_loc(S, tau)`` from the Dupire surface, when one was built.
    local_volatility: Callable[[np.ndarray, float], np.ndarray] | None = None

    #: Calibrated Heston parameters, when a calibration exists.
    heston: HestonParameters | None = None

    grid: GridSpec | None = None
    paths: int = 100_000
    seed: int = 20_260_924

    def to_dict(self) -> dict:
        return {
            "spot": self.spot,
            "strike": self.strike,
            "time_to_expiry": self.tau,
            "risk_free_rate": self.rate,
            "dividend_yield": self.dividend,
            "option_type": "CALL" if self.is_call else "PUT",
            "reference_volatility": self.reference_volatility,
            "has_local_volatility": self.local_volatility is not None,
            "heston": self.heston.to_dict() if self.heston is not None else None,
            "grid": (self.grid or GridSpec()).to_dict(),
            "paths": self.paths,
            "seed": self.seed,
        }


@dataclass(frozen=True, slots=True)
class ModelValue:
    """What one model said, or why it could not say anything."""

    model: PricingModelKind
    model_version: str
    value: float | None
    method: str
    #: The inputs this particular model consumed, so a reader can see that the
    #: PDE used a local volatility surface and BSM used a single number.
    inputs_used: dict = field(default_factory=dict)
    diagnostics: dict = field(default_factory=dict)
    warnings: tuple[str, ...] = field(default=())
    unavailable_reason: str | None = None

    @property
    def available(self) -> bool:
        return self.value is not None

    def to_dict(self) -> dict:
        return {
            "model": str(self.model),
            "model_version": self.model_version,
            "value": self.value,
            "method": self.method,
            "inputs_used": dict(self.inputs_used),
            "diagnostics": dict(self.diagnostics),
            "warnings": list(self.warnings),
            "unavailable_reason": self.unavailable_reason,
        }


@dataclass(frozen=True, slots=True)
class ConfidenceContribution:
    """One named, weighted input to the confidence score."""

    name: str
    score: float
    weight: float
    basis: str

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "score": self.score,
            "weight": self.weight,
            "basis": self.basis,
        }


@dataclass(frozen=True, slots=True)
class Confidence:
    """A score that can always be taken apart.

    A weighted geometric mean, so one bad dimension pulls the whole score down
    rather than being averaged away by three healthy ones — the same aggregation
    the data-quality engine and the margin model use, for the same reason.
    """

    score: float
    contributions: tuple[ConfidenceContribution, ...]

    @property
    def weakest(self) -> ConfidenceContribution | None:
        candidates = [c for c in self.contributions if c.weight > 0]
        return min(candidates, key=lambda c: c.score) if candidates else None

    def to_dict(self) -> dict:
        weakest = self.weakest
        return {
            "score": self.score,
            "method": "weighted geometric mean of the contributions below",
            "contributions": [c.to_dict() for c in self.contributions],
            "weakest_contribution": weakest.name if weakest else None,
        }


@dataclass(frozen=True, slots=True)
class ConsensusResult:
    """Several models' values, their spread, and no verdict."""

    inputs: ConsensusInputs
    values: tuple[ModelValue, ...]
    reference_value: float | None
    reference_range: tuple[float, float] | None
    model_median: float | None
    dispersion_absolute: float | None
    dispersion_relative: float | None
    standard_deviation: float | None
    confidence: Confidence
    market_price: float | None = None
    market_deviation: float | None = None
    market_deviation_relative: float | None = None
    warnings: tuple[AnalyticalWarning, ...] = field(default=())
    model_version: str = CONSENSUS_MODEL_VERSION

    @property
    def models_requested(self) -> int:
        return len(self.values)

    @property
    def models_available(self) -> int:
        return sum(1 for value in self.values if value.available)

    def to_dict(self) -> dict:
        return {
            "model_version": self.model_version,
            "inputs": self.inputs.to_dict(),
            "counts": {
                "models_requested": self.models_requested,
                "models_available": self.models_available,
            },
            "reference_value": self.reference_value,
            "reference_range": (
                list(self.reference_range) if self.reference_range is not None else None
            ),
            "model_median": self.model_median,
            "model_dispersion": {
                "absolute": self.dispersion_absolute,
                "relative": self.dispersion_relative,
                "standard_deviation": self.standard_deviation,
            },
            "market_price": self.market_price,
            "market_deviation": self.market_deviation,
            "market_deviation_relative": self.market_deviation_relative,
            "confidence": self.confidence.to_dict(),
            "values": [value.to_dict() for value in self.values],
            "warnings": [warning.to_dict() for warning in self.warnings],
            "interpretation": (
                "The reference value is the median of the models below, not a "
                "price the contract is worth. The models "
                "rest on different and mutually inconsistent assumptions, so "
                "their spread is a statement about model risk rather than about "
                "the market. Where the spread is wide, no single number in this "
                "payload is more trustworthy than the range that contains them."
            ),
        }


# --------------------------------------------------------------------- models
class PricingModel(ABC):
    """One way of pricing the contract, and its own account of itself."""

    kind: PricingModelKind
    version: str

    @abstractmethod
    def price(self, inputs: ConsensusInputs) -> ModelValue:
        """Never raises for a modelling reason: an inability is a result."""

    def _unavailable(self, reason: str, method: str) -> ModelValue:
        return ModelValue(
            model=self.kind,
            model_version=self.version,
            value=None,
            method=method,
            unavailable_reason=reason,
        )


class BlackScholesMertonModel(PricingModel):
    kind = PricingModelKind.BLACK_SCHOLES_MERTON
    version = "bsm@1.0.0"
    method = "closed form at the surface's reference implied volatility"

    def price(self, inputs: ConsensusInputs) -> ModelValue:
        sigma = inputs.reference_volatility
        if sigma is None or not math.isfinite(sigma) or sigma <= 0:
            return self._unavailable(
                "the fitted surface produced no reference implied volatility for "
                "this contract, and Black-Scholes-Merton needs exactly one",
                self.method,
            )
        value = float(
            bsm_price(
                inputs.spot,
                inputs.strike,
                inputs.tau,
                inputs.rate,
                inputs.dividend,
                sigma,
                inputs.is_call,
            )
        )
        return ModelValue(
            model=self.kind,
            model_version=self.version,
            value=value,
            method=self.method,
            inputs_used={"implied_volatility": sigma},
            warnings=(
                "A single volatility cannot reproduce the observed smile: this "
                "value is consistent with the surface at this strike only.",
            ),
        )


class LocalVolPDEModel(PricingModel):
    kind = PricingModelKind.LOCAL_VOL_PDE
    version = "cn-rannacher@1.0.0"
    method = "Crank-Nicolson with Rannacher start-up on the Dupire local volatility"

    def price(self, inputs: ConsensusInputs) -> ModelValue:
        if inputs.local_volatility is None:
            return self._unavailable(
                "no Dupire local volatility surface was available, which is the "
                "only thing this model can be run against",
                self.method,
            )
        try:
            result = solve_local_vol_pde(
                spot=inputs.spot,
                strike=inputs.strike,
                tau=inputs.tau,
                rate=inputs.rate,
                dividend=inputs.dividend,
                local_volatility=inputs.local_volatility,
                is_call=inputs.is_call,
                spec=inputs.grid,
                reference_volatility=inputs.reference_volatility,
            )
        except PDEError as exc:
            return self._unavailable(f"the PDE solver refused: {exc}", self.method)

        warnings = list(result.warnings)
        fallback = getattr(inputs.local_volatility, "fallback_fraction", None)
        diagnostics = {
            "delta": result.delta,
            "gamma": result.gamma,
            "grid": result.spec.to_dict(),
            "rannacher_steps": result.rannacher_steps,
            "boundary": str(result.boundary),
        }
        if callable(fallback):
            share = float(fallback(np.array([inputs.spot]), inputs.tau))
            diagnostics["local_vol_fallback_fraction"] = share
            if share > 0.0:
                warnings.append(
                    f"{share:.0%} of the local volatility evaluations at the spot "
                    "fell in a region where Dupire's denominator is unusable and "
                    "the surface's own implied volatility was used instead"
                )

        return ModelValue(
            model=self.kind,
            model_version=self.version,
            value=float(result.price),
            method=self.method,
            inputs_used={"local_volatility_surface": True},
            diagnostics=diagnostics,
            warnings=tuple(warnings),
        )


class HestonModel(PricingModel):
    kind = PricingModelKind.HESTON
    version = "heston-cf@1.0.0"
    method = "characteristic function with the Albrecher little-trap branch"

    def price(self, inputs: ConsensusInputs) -> ModelValue:
        if inputs.heston is None:
            return self._unavailable(
                "no Heston calibration was supplied; the parameters are not "
                "defaulted, because a plausible-looking (v0, kappa, theta, xi, "
                "rho) would produce a confident number about nothing",
                self.method,
            )
        try:
            value = heston_price(
                spot=inputs.spot,
                strike=inputs.strike,
                tau=inputs.tau,
                rate=inputs.rate,
                dividend=inputs.dividend,
                parameters=inputs.heston,
                is_call=inputs.is_call,
            )
        except HestonError as exc:
            return self._unavailable(f"the Heston pricer refused: {exc}", self.method)

        return ModelValue(
            model=self.kind,
            model_version=self.version,
            value=float(value),
            method=self.method,
            inputs_used=inputs.heston.to_dict(),
            diagnostics={"feller": inputs.heston.feller},
            warnings=inputs.heston.warnings(),
        )


class MonteCarloModel(PricingModel):
    kind = PricingModelKind.MONTE_CARLO
    version = "gbm-mc@1.0.0"
    method = "seeded geometric Brownian motion, antithetic with a control variate"

    def price(self, inputs: ConsensusInputs) -> ModelValue:
        sigma = inputs.reference_volatility
        if sigma is None or not math.isfinite(sigma) or sigma <= 0:
            return self._unavailable(
                "no reference implied volatility to simulate under",
                self.method,
            )
        try:
            result = monte_carlo_price(
                spot=inputs.spot,
                strike=inputs.strike,
                tau=inputs.tau,
                rate=inputs.rate,
                dividend=inputs.dividend,
                sigma=sigma,
                is_call=inputs.is_call,
                paths=inputs.paths,
                seed=inputs.seed,
            )
        except MonteCarloError as exc:
            return self._unavailable(f"the simulation refused: {exc}", self.method)

        low, high = result.confidence_interval
        return ModelValue(
            model=self.kind,
            model_version=self.version,
            value=result.price,
            method=self.method,
            inputs_used={"implied_volatility": sigma, "paths": result.paths, "seed": result.seed},
            diagnostics={
                "standard_error": result.standard_error,
                "confidence_interval_95": [low, high],
                "variance_reduction": result.variance_reduction,
                "antithetic": result.antithetic,
                "control_variate": result.control_variate,
            },
            warnings=(
                "This value carries a sampling error: it is reproducible from "
                "its seed but is not the same number a different seed would give.",
            ),
        )


#: Every model the consensus can be asked for, by name. Ordering is fixed so
#: that the payload's model list is stable across runs.
PRICING_MODELS: dict[PricingModelKind, type[PricingModel]] = {
    PricingModelKind.BLACK_SCHOLES_MERTON: BlackScholesMertonModel,
    PricingModelKind.LOCAL_VOL_PDE: LocalVolPDEModel,
    PricingModelKind.HESTON: HestonModel,
    PricingModelKind.MONTE_CARLO: MonteCarloModel,
}

DEFAULT_MODELS: tuple[PricingModelKind, ...] = tuple(PRICING_MODELS)


class ModelConsensusService:
    """Pure computation. No session, no I/O, no recommendation."""

    def price(
        self,
        inputs: ConsensusInputs,
        models: Sequence[PricingModelKind] = DEFAULT_MODELS,
        market_price: float | None = None,
        external_contributions: Sequence[ConfidenceContribution] = (),
    ) -> ConsensusResult:
        """Run every requested model and describe how far apart they landed.

        ``external_contributions`` lets the caller fold in what it knows and
        this module cannot see — the data quality behind the surface, the
        liquidity of the contract, the age of the quotes. They enter the same
        weighted geometric mean and are listed by name alongside the internal
        ones, so a low confidence is always attributable.
        """
        requested = tuple(dict.fromkeys(models))
        values = tuple(PRICING_MODELS[kind]().price(inputs) for kind in requested)
        warnings: list[AnalyticalWarning] = []

        for value in values:
            if not value.available:
                warnings.append(
                    AnalyticalWarning.info(
                        ConsensusWarningCode.MODEL_UNAVAILABLE,
                        f"{value.model} produced no value: {value.unavailable_reason}. "
                        "The consensus continues over the remaining models and the "
                        "reduced count is reflected in the confidence score.",
                        model=str(value.model),
                    )
                )

        priced = sorted(value.value for value in values if value.value is not None)
        count = len(priced)

        if count == 0:
            warnings.append(
                AnalyticalWarning.error(
                    ConsensusWarningCode.NO_MODEL_PRODUCED_A_VALUE,
                    "No requested model could price this contract, so there is no "
                    "reference value. This is an absence, not a value of zero.",
                )
            )
            return ConsensusResult(
                inputs=inputs,
                values=values,
                reference_value=None,
                reference_range=None,
                model_median=None,
                dispersion_absolute=None,
                dispersion_relative=None,
                standard_deviation=None,
                confidence=Confidence(0.0, ()),
                market_price=market_price,
                warnings=tuple(warnings),
            )

        median = float(np.median(priced))
        low, high = float(priced[0]), float(priced[-1])
        spread = high - low
        deviation = float(np.std(priced, ddof=1)) if count > 1 else 0.0

        scale = max(abs(median), 0.0)
        relative: float | None = None
        if scale > MIN_PRICE_FOR_RELATIVE:
            relative = spread / scale
        else:
            warnings.append(
                AnalyticalWarning.info(
                    ConsensusWarningCode.NEGLIGIBLE_PRICE,
                    "The models agree that this contract is worth almost nothing. "
                    "A relative dispersion on such a price would be arithmetic "
                    "noise, so only the absolute spread is reported.",
                    median=median,
                )
            )

        if count == 1:
            warnings.append(
                AnalyticalWarning.warn(
                    ConsensusWarningCode.SINGLE_MODEL,
                    "Only one model produced a value, so the dispersion below is "
                    "zero because there is nothing to disagree with — not because "
                    "the models agree. Agreement is not scored.",
                )
            )
        elif relative is not None and relative > AGREEMENT_REFERENCE_DISPERSION:
            warnings.append(
                AnalyticalWarning.warn(
                    ConsensusWarningCode.WIDE_DISPERSION,
                    f"The models span {relative:.1%} of the median "
                    f"({low:.6g} to {high:.6g}). The width of that range is a "
                    "better description of what is known about this contract "
                    "than any single number inside it.",
                    relative_dispersion=relative,
                    reference_range=[low, high],
                )
            )

        confidence = self._confidence(
            values=values,
            requested=len(requested),
            available=count,
            relative_dispersion=relative,
            external=external_contributions,
        )

        market_deviation = None
        market_relative = None
        if market_price is not None:
            market_deviation = market_price - median
            if abs(median) > MIN_PRICE_FOR_RELATIVE:
                market_relative = market_deviation / abs(median)

        return ConsensusResult(
            inputs=inputs,
            values=values,
            reference_value=median,
            reference_range=(low, high),
            model_median=median,
            dispersion_absolute=spread,
            dispersion_relative=relative,
            standard_deviation=deviation,
            confidence=confidence,
            market_price=market_price,
            market_deviation=market_deviation,
            market_deviation_relative=market_relative,
            warnings=tuple(warnings),
        )

    # ---------------------------------------------------------- confidence
    @staticmethod
    def _confidence(
        values: tuple[ModelValue, ...],
        requested: int,
        available: int,
        relative_dispersion: float | None,
        external: Sequence[ConfidenceContribution],
    ) -> Confidence:
        contributions: list[ConfidenceContribution] = [
            ConfidenceContribution(
                name="model_count",
                score=available / requested if requested else 0.0,
                weight=1.0,
                basis=(
                    f"{available} of {requested} requested models produced a value. "
                    "A model that could not run lowers this score rather than "
                    "disappearing from the comparison."
                ),
            )
        ]

        if available >= 2 and relative_dispersion is not None:
            contributions.append(
                ConfidenceContribution(
                    name="model_agreement",
                    score=ratio_penalty_score(relative_dispersion, AGREEMENT_REFERENCE_DISPERSION),
                    weight=2.0,
                    basis=(
                        f"the models span {relative_dispersion:.2%} of the median, "
                        f"scored against a {AGREEMENT_REFERENCE_DISPERSION:.0%} "
                        "reference at which this contribution is one half. "
                        "Weighted twice, because disagreement between models is "
                        "the thing this comparison exists to measure."
                    ),
                )
            )

        simulation = next(
            (v for v in values if v.model is PricingModelKind.MONTE_CARLO and v.available), None
        )
        if simulation is not None and simulation.value is not None:
            error = float(simulation.diagnostics.get("standard_error", 0.0))
            width = 1.96 * error
            scale = max(abs(simulation.value), MIN_PRICE_FOR_RELATIVE)
            contributions.append(
                ConfidenceContribution(
                    name="simulation_precision",
                    score=max(0.0, 1.0 - min(1.0, width / scale)),
                    weight=0.5,
                    basis=(
                        f"the simulation's 95% interval is {width:.6g} wide against "
                        f"a value of {simulation.value:.6g}. Weighted lightly: it "
                        "measures the path count, not the model."
                    ),
                )
            )

        contributions.extend(external)

        usable = [c for c in contributions if c.weight > 0]
        if not usable:
            return Confidence(0.0, tuple(contributions))
        score = weighted_geometric_mean(
            [max(0.0, min(1.0, c.score)) for c in usable], [c.weight for c in usable]
        )
        return Confidence(float(score), tuple(contributions))
