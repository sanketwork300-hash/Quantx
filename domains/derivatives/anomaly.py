"""Surface anomaly detection.

A deviation between an observed implied volatility and a fitted reference is
**not** a trading signal, and this module is built so it cannot be mistaken for
one. There is no price target, no direction, no rating. What it produces is a
measured difference, the scale of the things that could explain it, a confidence
grounded in named measurements, and a written explanation of both.

The detection rule is not a fixed threshold on the volatility difference.
A fixed threshold flags every illiquid wing quote in the market and nothing
else. A deviation is only interesting when the things that could account for it
— the width of the market, the error in the fit, and the numerical resolution of
the inversion — do not account for it. So the difference is standardised by

    explained_scale = sqrt(half_envelope^2 + calibration_rmse^2 + iv_uncertainty^2)

and it is those three quantities, all measured elsewhere in the platform for
their own reasons, that decide what counts as unusual.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum

import numpy as np

from domains.derivatives.models import ChainAnalysis, ImpliedVolPoint
from domains.derivatives.surface import (
    ReferenceFlag,
    ReferenceMethod,
    ReferencePoint,
    VolatilitySurface,
)
from domains.instruments.enums import OptionType
from quant.statistics import percentile_rank, summarise, weighted_geometric_mean, z_score

ANOMALY_MODEL_VERSION = "surface-anomaly@1.0.0"

#: Floors on the components of the explained scale, so a zero spread or a
#: perfect fit cannot make the denominator vanish and turn rounding into a
#: hundred-sigma event.
MIN_ENVELOPE_HALF = 1e-4  # one hundredth of a volatility point
MIN_CALIBRATION_RMSE = 1e-4  # in volatility units (0.01 vol points)

#: Reference scales for the confidence factors. Each is a stated convention,
#: recorded in provenance, not a tuned constant.
FIT_REFERENCE_VOL_POINTS = 0.5
UNCERTAINTY_REFERENCE = 1e-3
OBSERVATIONS_REFERENCE = 10.0
EXTRAPOLATION_PENALTY = 0.3
DEGRADED_SLICE_PENALTY = 0.5


class EnvelopePosition(StrEnum):
    """Where the reference sits relative to the market's own two-sided quote."""

    #: Inside the bid/ask implied-volatility envelope. The market's own width
    #: accounts for the difference entirely, so there is nothing to explain.
    INSIDE = "INSIDE"
    ABOVE_ASK = "ABOVE_ASK"
    BELOW_BID = "BELOW_BID"
    UNKNOWN = "UNKNOWN"


class ExplanationEffect(StrEnum):
    SUPPORTS = "SUPPORTS"
    REDUCES = "REDUCES"
    NEUTRAL = "NEUTRAL"


@dataclass(frozen=True, slots=True)
class Explanation:
    """One grounded reason, with the measurement behind it.

    Never an opaque narrative: every entry names the quantity it is talking
    about and the value that was measured, so a reader can check it.
    """

    factor: str
    effect: ExplanationEffect
    detail: str
    value: float | None = None

    def to_dict(self) -> dict:
        return {
            "factor": self.factor,
            "effect": str(self.effect),
            "detail": self.detail,
            "value": self.value,
        }


@dataclass(frozen=True, slots=True)
class SurfaceAnomaly:
    instrument_id: uuid.UUID
    expiry: date
    strike: Decimal
    option_type: OptionType

    market_iv: float
    reference_iv: float
    #: Observed minus reference, in volatility units. Positive means the market
    #: is implying more volatility than the fitted surface does.
    iv_difference: float
    relative_deviation: float

    market_iv_bid: float | None = None
    market_iv_ask: float | None = None
    envelope_position: EnvelopePosition = EnvelopePosition.UNKNOWN
    #: How far outside the bid/ask envelope, in volatility units. Zero when the
    #: reference lies inside it.
    excess_over_envelope: float = 0.0

    #: The combined size of everything that could account for the difference.
    explained_scale: float = 0.0
    z_score: float = 0.0
    #: Against this contract's own history of deviations, when there is one.
    historical_z_score: float | None = None
    historical_observations: int = 0

    liquidity_score: float = 1.0
    data_quality_score: float = 1.0
    calibration_rmse_vol_points: float | None = None
    iv_uncertainty: float | None = None
    reference_method: ReferenceMethod = ReferenceMethod.EXACT_SLICE
    reference_flags: tuple[ReferenceFlag, ...] = field(default=())

    confidence: float = 0.0
    flagged: bool = False
    explanation: tuple[Explanation, ...] = field(default=())

    @property
    def iv_difference_vol_points(self) -> float:
        return self.iv_difference * 100.0

    def to_dict(self) -> dict:
        return {
            "instrument_id": str(self.instrument_id),
            "expiry": self.expiry.isoformat(),
            "strike": format(self.strike, "f"),
            "option_type": str(self.option_type),
            "market_iv": self.market_iv,
            "reference_iv": self.reference_iv,
            "iv_difference": self.iv_difference,
            "iv_difference_vol_points": self.iv_difference_vol_points,
            "relative_deviation": self.relative_deviation,
            "market_iv_bid": self.market_iv_bid,
            "market_iv_ask": self.market_iv_ask,
            "envelope_position": str(self.envelope_position),
            "excess_over_envelope": self.excess_over_envelope,
            "explained_scale": self.explained_scale,
            "z_score": self.z_score,
            "historical_z_score": self.historical_z_score,
            "historical_observations": self.historical_observations,
            "liquidity_score": self.liquidity_score,
            "data_quality_score": self.data_quality_score,
            "calibration_rmse_vol_points": self.calibration_rmse_vol_points,
            "iv_uncertainty": self.iv_uncertainty,
            "reference_method": str(self.reference_method),
            "reference_flags": [str(flag) for flag in self.reference_flags],
            "confidence": self.confidence,
            "flagged": self.flagged,
            "explanation": [entry.to_dict() for entry in self.explanation],
        }


@dataclass(frozen=True, slots=True)
class AnomalyPolicy:
    """What counts as worth surfacing. Recorded in provenance, not hard-coded."""

    #: Minimum standardised deviation. Two means the difference is twice
    #: everything that could explain it.
    min_z_score: float = 2.0
    #: The market's own two-sided quote must not already account for it.
    require_outside_envelope: bool = True
    min_confidence: float = 0.3
    #: Quotes below this liquidity score are scored but never flagged: a wing
    #: quote nobody trades deviating from a fitted curve is not news.
    min_liquidity: float = 0.05

    def to_provenance(self) -> dict:
        return {
            "min_z_score": self.min_z_score,
            "require_outside_envelope": self.require_outside_envelope,
            "min_confidence": self.min_confidence,
            "min_liquidity": self.min_liquidity,
            "explained_scale": (
                "sqrt(half bid/ask IV envelope^2 + slice calibration RMSE^2 "
                "+ IV numerical uncertainty^2)"
            ),
        }


@dataclass(frozen=True, slots=True)
class AnomalyScan:
    surface_id: str
    underlying_id: uuid.UUID
    as_of: str
    anomalies: tuple[SurfaceAnomaly, ...]
    policy: AnomalyPolicy
    quotes_examined: int = 0
    quotes_scored: int = 0

    @property
    def flagged(self) -> tuple[SurfaceAnomaly, ...]:
        return tuple(a for a in self.anomalies if a.flagged)

    def to_dict(self, include_all: bool = False, limit: int = 500) -> dict:
        selected = self.anomalies if include_all else self.flagged
        ordered = sorted(selected, key=lambda a: -abs(a.z_score))[:limit]
        return {
            "surface_id": self.surface_id,
            "underlying_id": str(self.underlying_id),
            "as_of_timestamp": self.as_of,
            "counts": {
                "examined": self.quotes_examined,
                "scored": self.quotes_scored,
                "flagged": len(self.flagged),
                "returned": len(ordered),
            },
            "policy": self.policy.to_provenance(),
            "anomalies": [anomaly.to_dict() for anomaly in ordered],
        }


class SurfaceAnomalyScanner:
    """Compares observed implied volatilities against a fitted reference."""

    def scan(
        self,
        analysis: ChainAnalysis,
        surface: VolatilitySurface,
        policy: AnomalyPolicy | None = None,
        history: dict[uuid.UUID, list[float]] | None = None,
    ) -> AnomalyScan:
        policy = policy or AnomalyPolicy()
        history = history or {}

        anomalies: list[SurfaceAnomaly] = []
        examined = 0

        for smile in analysis.slices:
            slice_fit = surface.slice_for(smile.expiry)
            rmse = (
                slice_fit.calibration.rmse_vol_points / 100.0
                if slice_fit is not None and slice_fit.calibration.rmse_vol_points is not None
                else None
            )
            observations = slice_fit.calibration.n_observations if slice_fit is not None else 0

            for point in smile.points:
                examined += 1
                if point.market_iv is None or not point.converged:
                    continue
                reference = surface.reference(point.strike, smile.expiry)
                if not reference.ok:
                    continue
                anomalies.append(
                    self.score_point(point, reference, rmse, observations, policy, history)
                )

        return AnomalyScan(
            surface_id=surface.surface_id,
            underlying_id=analysis.underlying_id,
            as_of=analysis.as_of,
            anomalies=tuple(anomalies),
            policy=policy,
            quotes_examined=examined,
            quotes_scored=len(anomalies),
        )

    # ------------------------------------------------------------------ score
    def score_point(
        self,
        point: ImpliedVolPoint,
        reference: ReferencePoint,
        calibration_rmse: float | None,
        observations: int,
        policy: AnomalyPolicy,
        history: dict[uuid.UUID, list[float]],
    ) -> SurfaceAnomaly:
        """Score one quote against the surface.

        Public because Phase 11 scores a single contract inside a unified order
        analysis and must do it with *this* code rather than a second copy: two
        implementations of the explained scale would disagree about what counts
        as a deviation, and only one of them would be tested.
        """
        market_iv = float(point.market_iv)
        reference_iv = float(reference.reference_iv)
        difference = market_iv - reference_iv

        position, excess, half_envelope = self._envelope(point, reference_iv)
        rmse = max(calibration_rmse or 0.0, MIN_CALIBRATION_RMSE)
        uncertainty = point.uncertainty or 0.0
        explained = float(
            np.sqrt(max(half_envelope, MIN_ENVELOPE_HALF) ** 2 + rmse**2 + uncertainty**2)
        )
        z = difference / explained if explained > 0 else 0.0

        past = history.get(point.instrument_id, [])
        historical_z = z_score(difference, past)

        confidence, explanation = self._confidence(
            point, reference, calibration_rmse, observations, position, z, historical_z
        )

        flagged = (
            abs(z) >= policy.min_z_score
            and confidence >= policy.min_confidence
            and point.liquidity_score >= policy.min_liquidity
            and (not policy.require_outside_envelope or position is not EnvelopePosition.INSIDE)
        )

        return SurfaceAnomaly(
            instrument_id=point.instrument_id,
            expiry=point.expiry,
            strike=point.strike,
            option_type=point.option_type,
            market_iv=market_iv,
            reference_iv=reference_iv,
            iv_difference=difference,
            relative_deviation=difference / reference_iv if reference_iv > 0 else 0.0,
            market_iv_bid=point.market_iv_bid,
            market_iv_ask=point.market_iv_ask,
            envelope_position=position,
            excess_over_envelope=excess,
            explained_scale=explained,
            z_score=z,
            historical_z_score=historical_z,
            historical_observations=len(past),
            liquidity_score=point.liquidity_score,
            data_quality_score=point.data_quality_score,
            calibration_rmse_vol_points=(
                calibration_rmse * 100.0 if calibration_rmse is not None else None
            ),
            iv_uncertainty=point.uncertainty,
            reference_method=reference.method,
            reference_flags=reference.flags,
            confidence=confidence,
            flagged=flagged,
            explanation=explanation,
        )

    @staticmethod
    def _envelope(
        point: ImpliedVolPoint, reference_iv: float
    ) -> tuple[EnvelopePosition, float, float]:
        bid, ask = point.market_iv_bid, point.market_iv_ask
        if bid is None or ask is None or ask < bid:
            return EnvelopePosition.UNKNOWN, 0.0, 0.0

        half = (ask - bid) / 2.0
        if reference_iv > ask:
            return EnvelopePosition.ABOVE_ASK, reference_iv - ask, half
        if reference_iv < bid:
            return EnvelopePosition.BELOW_BID, bid - reference_iv, half
        return EnvelopePosition.INSIDE, 0.0, half

    def _confidence(
        self,
        point: ImpliedVolPoint,
        reference: ReferencePoint,
        calibration_rmse: float | None,
        observations: int,
        position: EnvelopePosition,
        z: float,
        historical_z: float | None,
    ) -> tuple[float, tuple[Explanation, ...]]:
        """Confidence that the *deviation is real*, not that a trade is good.

        Every factor is a measurement made elsewhere in the platform for its own
        reasons, and every one produces a line of explanation naming its value.
        """
        explanation: list[Explanation] = []

        quality = point.data_quality_score
        explanation.append(
            Explanation(
                "data quality",
                ExplanationEffect.SUPPORTS if quality >= 0.7 else ExplanationEffect.REDUCES,
                f"The quote's overall data-quality score is {quality:.2f}.",
                quality,
            )
        )

        liquidity = point.liquidity_score
        explanation.append(
            Explanation(
                "liquidity",
                ExplanationEffect.SUPPORTS if liquidity >= 0.5 else ExplanationEffect.REDUCES,
                f"Liquidity score {liquidity:.2f} from volume, open interest and quoted size.",
                liquidity,
            )
        )

        rmse_points = (calibration_rmse or 0.0) * 100.0
        fit = 1.0 / (1.0 + (rmse_points / FIT_REFERENCE_VOL_POINTS) ** 2)
        explanation.append(
            Explanation(
                "surface fit",
                ExplanationEffect.SUPPORTS if rmse_points <= 0.25 else ExplanationEffect.REDUCES,
                f"This expiry's slice fits its quotes to {rmse_points:.3f} "
                "volatility points, which is the model error the deviation is "
                "measured against.",
                rmse_points,
            )
        )

        uncertainty = point.uncertainty or 0.0
        conditioning = 1.0 / (1.0 + (uncertainty / UNCERTAINTY_REFERENCE) ** 2)
        explanation.append(
            Explanation(
                "measurement resolution",
                ExplanationEffect.SUPPORTS
                if uncertainty <= UNCERTAINTY_REFERENCE
                else ExplanationEffect.REDUCES,
                f"The quoted price pins this implied volatility down to "
                f"{uncertainty * 100:.4f} volatility points.",
                uncertainty,
            )
        )

        breadth = observations / (OBSERVATIONS_REFERENCE + observations)
        explanation.append(
            Explanation(
                "slice breadth",
                ExplanationEffect.SUPPORTS if observations >= 10 else ExplanationEffect.REDUCES,
                f"The reference came from a slice fitted on {observations} quotes.",
                float(observations),
            )
        )

        extrapolation = 1.0
        if ReferenceFlag.EXTRAPOLATED_STRIKE in reference.flags:
            extrapolation *= EXTRAPOLATION_PENALTY
            explanation.append(
                Explanation(
                    "extrapolation",
                    ExplanationEffect.REDUCES,
                    "This strike lies outside the range the slice was fitted on, "
                    "where SVI's wings are weakly constrained.",
                )
            )
        if reference.method is ReferenceMethod.EXTRAPOLATED_MATURITY:
            extrapolation *= EXTRAPOLATION_PENALTY
            explanation.append(
                Explanation(
                    "extrapolation",
                    ExplanationEffect.REDUCES,
                    "This expiry lies outside the fitted maturity range.",
                )
            )
        if ReferenceFlag.SLICE_DEGRADED in reference.flags:
            extrapolation *= DEGRADED_SLICE_PENALTY
            explanation.append(
                Explanation(
                    "slice admissibility",
                    ExplanationEffect.REDUCES,
                    "The slice fitted but is not fully admissible, so the reference "
                    "itself is suspect.",
                )
            )

        if position is EnvelopePosition.INSIDE:
            explanation.append(
                Explanation(
                    "bid/ask envelope",
                    ExplanationEffect.REDUCES,
                    "The reference lies inside the quoted bid/ask implied-volatility "
                    "range, so the width of the market accounts for the whole "
                    "difference.",
                )
            )
        elif position is not EnvelopePosition.UNKNOWN:
            explanation.append(
                Explanation(
                    "bid/ask envelope",
                    ExplanationEffect.SUPPORTS,
                    f"The reference lies {position.value.replace('_', ' ').lower()}, "
                    "so the market's own quoted width does not account for the "
                    "difference.",
                )
            )

        if historical_z is not None:
            explanation.append(
                Explanation(
                    "historical deviation",
                    ExplanationEffect.SUPPORTS
                    if abs(historical_z) >= 2.0
                    else ExplanationEffect.NEUTRAL,
                    f"Against this contract's own past deviations this is "
                    f"{historical_z:+.1f} standard deviations.",
                    historical_z,
                )
            )
        else:
            explanation.append(
                Explanation(
                    "historical deviation",
                    ExplanationEffect.NEUTRAL,
                    "No usable history for this contract, so the deviation is "
                    "measured only against what today's market and fit can explain.",
                )
            )

        explanation.append(
            Explanation(
                "standardised deviation",
                ExplanationEffect.NEUTRAL,
                f"The difference is {z:+.1f} times the combined size of the "
                "bid/ask width, the calibration error and the measurement "
                "resolution.",
                z,
            )
        )

        confidence = weighted_geometric_mean(
            [quality, liquidity, fit, conditioning, breadth, extrapolation],
            [1.0, 1.0, 1.5, 1.0, 0.5, 1.5],
        )
        return float(confidence), tuple(explanation)


def deviation_percentile(value: float, sample: list[float]) -> float | None:
    """Where a deviation sits in a historical sample. Exposed for the API."""
    return percentile_rank(value, sample)


def deviation_summary(sample: list[float]):
    return summarise(sample)
