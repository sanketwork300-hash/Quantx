"""The anomaly scanner: what it measures, and what it refuses to say."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from domains.derivatives.anomaly import (
    AnomalyPolicy,
    EnvelopePosition,
    ExplanationEffect,
    SurfaceAnomalyScanner,
)
from domains.derivatives.forward import (
    ForwardEstimate,
    ForwardEstimateSet,
    ForwardMethod,
)
from domains.derivatives.models import (
    ChainAnalysis,
    ImpliedVolPoint,
    PriceSource,
    SmileSlice,
)
from domains.derivatives.surface import SurfaceSliceFit, VolatilitySurface
from domains.instruments.enums import OptionType
from quant.volatility.svi import SVIParameters
from quant.volatility.svi_calibration import CalibrationStatus, SVICalibrationResult

AS_OF = datetime(2026, 9, 24, 9, 20, tzinfo=UTC)
EXPIRY = date(2026, 12, 24)
UNDERLYING = uuid.uuid4()
TAU = 0.25
FORWARD = 24000.0
#: A flat slice: total variance 0.25^2 * 0.25 at every strike, so the reference
#: volatility is exactly 25% everywhere and deviations are easy to reason about.
FLAT = SVIParameters(a=0.015625, b=0.0, rho=0.0, m=0.0, sigma=0.1)


def surface(**overrides) -> VolatilitySurface:
    kwargs = {
        "expiry": EXPIRY,
        "time_to_expiry": TAU,
        "forward": FORWARD,
        "discount_factor": 0.984,
        "parameters": FLAT,
        "calibration": SVICalibrationResult(
            parameters=FLAT,
            status=CalibrationStatus.CONVERGED,
            n_observations=25,
            rmse_vol_points=0.05,
            constraints_satisfied=True,
        ),
        "k_min": -0.2,
        "k_max": 0.2,
        "forward_method": "PUT_CALL_PARITY",
        "forward_confidence": 0.9,
    }
    kwargs.update(overrides)
    return VolatilitySurface(
        underlying_id=UNDERLYING, as_of=AS_OF, slices=(SurfaceSliceFit(**kwargs),)
    )


def point(market_iv: float, **overrides) -> ImpliedVolPoint:
    kwargs = {
        "instrument_id": uuid.uuid4(),
        "expiry": EXPIRY,
        "strike": Decimal("24000"),
        "option_type": OptionType.CALL,
        "price_used": 1000.0,
        "price_source": PriceSource.MID,
        "market_iv": market_iv,
        "market_iv_bid": market_iv - 0.001,
        "market_iv_ask": market_iv + 0.001,
        "converged": True,
        "solver": "safeguarded-newton",
        "vega": 1000.0,
        "uncertainty": 1e-5,
        "data_quality_score": 0.95,
        "liquidity_score": 0.9,
        "time_to_expiry": TAU,
        "log_moneyness": 0.0,
        "total_variance": market_iv**2 * TAU,
        "weight": 0.85,
        "used_for_smile": True,
    }
    kwargs.update(overrides)
    return ImpliedVolPoint(**kwargs)


def analysis(points) -> ChainAnalysis:
    return ChainAnalysis(
        snapshot_id=uuid.uuid4(),
        underlying_id=UNDERLYING,
        as_of=AS_OF.isoformat(),
        slices=(
            SmileSlice(
                expiry=EXPIRY,
                time_to_expiry=TAU,
                forward=ForwardEstimateSet(
                    estimates=(),
                    selected=ForwardEstimate(
                        value=FORWARD,
                        method=ForwardMethod.PUT_CALL_PARITY,
                        confidence=0.9,
                        observations=12,
                        discount_factor=0.984,
                    ),
                ),
                points=tuple(points),
            ),
        ),
    )


def scan(points, policy: AnomalyPolicy | None = None, history=None, **surface_kwargs):
    return SurfaceAnomalyScanner().scan(
        analysis(points), surface(**surface_kwargs), policy or AnomalyPolicy(), history
    )


class TestMeasurement:
    def test_a_quote_on_the_surface_has_no_deviation(self):
        result = scan([point(0.25)])
        anomaly = result.anomalies[0]
        assert anomaly.iv_difference == pytest.approx(0.0, abs=1e-12)
        assert anomaly.z_score == pytest.approx(0.0, abs=1e-9)
        assert not anomaly.flagged

    def test_the_difference_is_observed_minus_reference(self):
        anomaly = scan([point(0.28)]).anomalies[0]
        assert anomaly.market_iv == 0.28
        assert anomaly.reference_iv == pytest.approx(0.25)
        assert anomaly.iv_difference == pytest.approx(0.03)
        assert anomaly.iv_difference_vol_points == pytest.approx(3.0)
        assert anomaly.relative_deviation == pytest.approx(0.12)

    def test_a_negative_difference_means_the_market_implies_less(self):
        anomaly = scan([point(0.22)]).anomalies[0]
        assert anomaly.iv_difference < 0
        assert anomaly.z_score < 0

    def test_unconverged_and_unsolved_quotes_are_examined_but_not_scored(self):
        result = scan(
            [
                point(0.25),
                point(0.30, converged=False),
                ImpliedVolPoint(
                    instrument_id=uuid.uuid4(),
                    expiry=EXPIRY,
                    strike=Decimal("25000"),
                    option_type=OptionType.CALL,
                    price_used=None,
                    price_source=PriceSource.NONE,
                    market_iv=None,
                    error="NO_TIME_VALUE",
                ),
            ]
        )
        assert result.quotes_examined == 3
        assert result.quotes_scored == 1


class TestExplainedScale:
    def test_the_denominator_combines_spread_fit_and_resolution(self):
        """A deviation is only interesting when the things that could explain it
        do not explain it."""
        anomaly = scan([point(0.28)]).anomalies[0]
        # half envelope 0.001, calibration rmse 0.05 vol points = 5e-4,
        # uncertainty 1e-5  ->  sqrt(1e-6 + 2.5e-7 + 1e-10)
        assert anomaly.explained_scale == pytest.approx(0.0011180, rel=1e-3)
        assert anomaly.z_score == pytest.approx(0.03 / anomaly.explained_scale, rel=1e-9)

    def test_a_wider_market_explains_more_and_lowers_the_score(self):
        tight = scan([point(0.28, market_iv_bid=0.279, market_iv_ask=0.281)]).anomalies[0]
        wide = scan([point(0.28, market_iv_bid=0.25, market_iv_ask=0.31)]).anomalies[0]
        assert abs(wide.z_score) < abs(tight.z_score)
        assert wide.explained_scale > tight.explained_scale

    def test_a_worse_fit_explains_more(self):
        good = scan([point(0.28)])
        poor = scan(
            [point(0.28)],
            calibration=SVICalibrationResult(
                parameters=FLAT,
                status=CalibrationStatus.CONVERGED,
                n_observations=25,
                rmse_vol_points=2.0,
                constraints_satisfied=True,
            ),
        )
        assert abs(poor.anomalies[0].z_score) < abs(good.anomalies[0].z_score)

    def test_the_denominator_is_floored(self):
        """A perfect fit and a locked market must not turn rounding into a
        thousand-sigma event."""
        anomaly = scan(
            [point(0.2500001, market_iv_bid=0.2500001, market_iv_ask=0.2500001, uncertainty=0.0)],
            calibration=SVICalibrationResult(
                parameters=FLAT,
                status=CalibrationStatus.CONVERGED,
                n_observations=25,
                rmse_vol_points=0.0,
                constraints_satisfied=True,
            ),
        ).anomalies[0]
        assert abs(anomaly.z_score) < 1.0


class TestEnvelope:
    def test_a_reference_inside_the_quoted_range_is_explained_by_the_spread(self):
        anomaly = scan([point(0.2505, market_iv_bid=0.24, market_iv_ask=0.26)]).anomalies[0]
        assert anomaly.envelope_position is EnvelopePosition.INSIDE
        assert anomaly.excess_over_envelope == 0.0
        assert not anomaly.flagged, "the market's own width accounts for it"

    def test_a_reference_below_the_bid(self):
        anomaly = scan([point(0.30, market_iv_bid=0.299, market_iv_ask=0.301)]).anomalies[0]
        assert anomaly.envelope_position is EnvelopePosition.BELOW_BID
        assert anomaly.excess_over_envelope == pytest.approx(0.299 - 0.25)

    def test_a_reference_above_the_ask(self):
        anomaly = scan([point(0.20, market_iv_bid=0.199, market_iv_ask=0.201)]).anomalies[0]
        assert anomaly.envelope_position is EnvelopePosition.ABOVE_ASK
        assert anomaly.excess_over_envelope == pytest.approx(0.25 - 0.201)

    def test_a_one_sided_market_has_an_unknown_envelope(self):
        anomaly = scan([point(0.30, market_iv_bid=None)]).anomalies[0]
        assert anomaly.envelope_position is EnvelopePosition.UNKNOWN

    def test_the_envelope_requirement_can_be_relaxed(self):
        # Inside a wide quoted range, but far enough from the reference that the
        # standardised deviation alone would clear the threshold.
        inside = point(0.256, market_iv_bid=0.24, market_iv_ask=0.26)
        strict = scan([inside], AnomalyPolicy(min_z_score=0.1)).anomalies[0]
        assert strict.envelope_position is EnvelopePosition.INSIDE
        assert abs(strict.z_score) > 0.1
        relaxed = scan(
            [inside], AnomalyPolicy(min_z_score=0.1, require_outside_envelope=False)
        ).anomalies[0]
        assert not strict.flagged
        assert relaxed.flagged


class TestPolicy:
    def test_the_threshold_is_a_parameter_not_a_constant(self):
        quote = point(0.2515, market_iv_bid=0.2514, market_iv_ask=0.2516)
        assert not scan([quote], AnomalyPolicy(min_z_score=50.0)).anomalies[0].flagged
        assert scan([quote], AnomalyPolicy(min_z_score=0.5)).anomalies[0].flagged

    def test_illiquid_quotes_are_scored_but_never_flagged(self):
        """A wing quote nobody trades deviating from a fitted curve is not news."""
        anomaly = scan([point(0.35, liquidity_score=0.01)]).anomalies[0]
        assert anomaly.z_score > 2
        assert not anomaly.flagged

    def test_low_confidence_is_not_flagged(self):
        anomaly = scan(
            [point(0.35, data_quality_score=0.05, liquidity_score=0.2)],
            AnomalyPolicy(min_confidence=0.6),
        ).anomalies[0]
        assert not anomaly.flagged

    def test_the_policy_is_serialised_for_provenance(self):
        payload = AnomalyPolicy().to_provenance()
        assert payload["min_z_score"] == 2.0
        assert "explained_scale" in payload, "the formula itself is recorded"


class TestConfidence:
    def test_a_clean_liquid_quote_scores_high(self):
        anomaly = scan([point(0.30)]).anomalies[0]
        assert anomaly.confidence > 0.7

    def test_poor_data_quality_reduces_confidence(self):
        good = scan([point(0.30)]).anomalies[0]
        bad = scan([point(0.30, data_quality_score=0.2)]).anomalies[0]
        assert bad.confidence < good.confidence

    def test_a_poor_fit_reduces_confidence(self):
        poor = scan(
            [point(0.30)],
            calibration=SVICalibrationResult(
                parameters=FLAT,
                status=CalibrationStatus.CONVERGED,
                n_observations=25,
                rmse_vol_points=3.0,
                constraints_satisfied=True,
            ),
        ).anomalies[0]
        assert poor.confidence < scan([point(0.30)]).anomalies[0].confidence

    def test_extrapolation_reduces_confidence(self):
        inside = scan([point(0.30)]).anomalies[0]
        outside = scan([point(0.30, strike=Decimal("40000"))]).anomalies[0]
        assert outside.confidence < inside.confidence

    def test_a_degraded_slice_reduces_confidence(self):
        degraded = scan(
            [point(0.30)],
            calibration=SVICalibrationResult(
                parameters=FLAT,
                status=CalibrationStatus.DEGRADED,
                n_observations=25,
                rmse_vol_points=0.05,
                constraints_satisfied=False,
            ),
        ).anomalies[0]
        assert degraded.confidence < scan([point(0.30)]).anomalies[0].confidence

    def test_confidence_is_bounded(self):
        for quote in (point(0.25), point(0.5, liquidity_score=0.001)):
            assert 0.0 <= scan([quote]).anomalies[0].confidence <= 1.0


class TestExplanation:
    def test_every_factor_is_named_with_its_measurement(self):
        """Not an opaque narrative: a reader can check each line."""
        anomaly = scan([point(0.30)]).anomalies[0]
        factors = {entry.factor for entry in anomaly.explanation}
        assert {
            "data quality",
            "liquidity",
            "surface fit",
            "measurement resolution",
            "slice breadth",
            "bid/ask envelope",
            "historical deviation",
            "standardised deviation",
        } <= factors

    def test_effects_are_about_confidence_not_about_a_trade(self):
        anomaly = scan([point(0.30)]).anomalies[0]
        for entry in anomaly.explanation:
            assert entry.effect in set(ExplanationEffect)

    def test_the_measured_values_are_carried(self):
        anomaly = scan([point(0.30)]).anomalies[0]
        quality = next(e for e in anomaly.explanation if e.factor == "data quality")
        assert quality.value == pytest.approx(0.95)

    def test_extrapolation_is_explained_when_it_happens(self):
        anomaly = scan([point(0.30, strike=Decimal("40000"))]).anomalies[0]
        details = " ".join(e.detail for e in anomaly.explanation)
        assert "outside the range" in details

    def test_missing_history_is_stated_rather_than_implied(self):
        anomaly = scan([point(0.30)]).anomalies[0]
        entry = next(e for e in anomaly.explanation if e.factor == "historical deviation")
        assert "No usable history" in entry.detail
        assert anomaly.historical_z_score is None
        assert anomaly.historical_observations == 0


class TestHistoricalDeviation:
    def test_history_produces_a_time_series_z_score(self):
        quote = point(0.30)
        past = [0.001, 0.002, -0.001, 0.0015, 0.0005, -0.0005]
        result = scan([quote], history={quote.instrument_id: past})
        anomaly = result.anomalies[0]
        assert anomaly.historical_observations == len(past)
        assert anomaly.historical_z_score is not None
        assert anomaly.historical_z_score > 5, "0.05 against a sample around zero"

    def test_a_constant_history_yields_no_score_rather_than_infinity(self):
        quote = point(0.30)
        result = scan([quote], history={quote.instrument_id: [0.001] * 5})
        assert result.anomalies[0].historical_z_score is None


class TestLanguagePolicy:
    """A contract guarantee, not a style preference."""

    FORBIDDEN = (
        "buy",
        "sell",
        "cheap",
        "expensive",
        "underpriced",
        "overpriced",
        "arbitrage",
        "fair value",
        "signal",
        "opportunity",
        "recommend",
        "target",
    )

    def test_no_output_field_or_explanation_uses_advisory_language(self):
        import json

        result = scan(
            [
                point(0.30),
                point(0.20, strike=Decimal("23000")),
                point(0.35, strike=Decimal("40000")),
            ]
        )
        rendered = json.dumps(result.to_dict(include_all=True)).lower()
        for word in self.FORBIDDEN:
            assert word not in rendered, f"anomaly output must not contain {word!r}"

    def test_there_is_no_direction_or_rating_field(self):
        payload = scan([point(0.30)]).anomalies[0].to_dict()
        for key in ("action", "direction", "rating", "score_label", "recommendation"):
            assert key not in payload


class TestScanSummary:
    def test_counts_are_reported(self):
        result = scan([point(0.25), point(0.30, strike=Decimal("23800"))])
        payload = result.to_dict(include_all=True)
        assert payload["counts"]["examined"] == 2
        assert payload["counts"]["scored"] == 2
        assert payload["counts"]["returned"] == 2

    def test_flagged_only_by_default(self):
        result = scan([point(0.25), point(0.30, strike=Decimal("23800"))])
        assert result.to_dict()["counts"]["returned"] == len(result.flagged)

    def test_results_are_ordered_by_absolute_deviation(self):
        result = scan(
            [
                point(0.28, strike=Decimal("23800")),
                point(0.40, strike=Decimal("23900")),
                point(0.32, strike=Decimal("24100")),
            ]
        )
        scores = [abs(a["z_score"]) for a in result.to_dict(include_all=True)["anomalies"]]
        assert scores == sorted(scores, reverse=True)
