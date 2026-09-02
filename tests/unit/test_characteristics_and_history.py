"""Surface characteristics and historical percentiles."""

from __future__ import annotations

import math
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import pytest

from domains.derivatives.characteristics import (
    STANDARD_TENORS,
    CharacteristicKind,
    characteristics_at_tenor,
    slice_characteristics,
    surface_term_structure,
)
from domains.derivatives.history import CHARACTERISTIC_NAMES, build_tenor_history
from domains.derivatives.surface import (
    ReferenceFlag,
    ReferenceMethod,
    SurfaceSliceFit,
    VolatilitySurface,
)
from quant.statistics import MIN_RELIABLE_OBSERVATIONS
from quant.volatility.svi import SVIParameters, raw_svi_implied_vol
from quant.volatility.svi_calibration import CalibrationStatus, SVICalibrationResult

AS_OF = datetime(2026, 9, 24, 9, 20, tzinfo=UTC)
UNDERLYING = uuid.uuid4()
PARAMS = SVIParameters(a=0.010, b=0.045, rho=-0.55, m=0.015, sigma=0.10)


def calibration(status=CalibrationStatus.CONVERGED, **kw) -> SVICalibrationResult:
    base = {
        "parameters": PARAMS,
        "status": status,
        "n_observations": 21,
        "rmse_vol_points": 0.05,
        "constraints_satisfied": status is CalibrationStatus.CONVERGED,
    }
    base.update(kw)
    return SVICalibrationResult(**base)


def make_slice(expiry: date, tau: float, **overrides) -> SurfaceSliceFit:
    kwargs = {
        "expiry": expiry,
        "time_to_expiry": tau,
        "forward": 24000.0,
        "discount_factor": math.exp(-0.065 * tau),
        "parameters": PARAMS,
        "calibration": calibration(),
        "k_min": -0.2,
        "k_max": 0.2,
        "forward_method": "PUT_CALL_PARITY",
        "forward_confidence": 0.9,
    }
    kwargs.update(overrides)
    return SurfaceSliceFit(**kwargs)


@pytest.fixture
def surface() -> VolatilitySurface:
    return VolatilitySurface(
        underlying_id=UNDERLYING,
        as_of=AS_OF,
        slices=(
            make_slice(date(2026, 10, 24), 30 / 365),
            make_slice(date(2026, 12, 23), 90 / 365),
        ),
    )


class TestSliceCharacteristics:
    def test_atm_volatility_matches_the_parameterization(self, surface):
        slice_ = surface.slices[0]
        characteristic = slice_characteristics(slice_)
        expected = float(raw_svi_implied_vol(0.0, slice_.time_to_expiry, PARAMS))
        assert characteristic.atm_volatility == pytest.approx(expected)

    def test_skew_matches_a_finite_difference_of_the_fitted_curve(self, surface):
        slice_ = surface.slices[0]
        characteristic = slice_characteristics(slice_)
        h = 1e-5
        fd = (
            float(raw_svi_implied_vol(h, slice_.time_to_expiry, PARAMS))
            - float(raw_svi_implied_vol(-h, slice_.time_to_expiry, PARAMS))
        ) / (2 * h)
        assert characteristic.skew == pytest.approx(fd, rel=1e-6)

    def test_negative_skew_for_a_put_side_smile(self, surface):
        assert slice_characteristics(surface.slices[0]).skew < 0

    def test_total_variance_is_consistent_with_the_level(self, surface):
        characteristic = slice_characteristics(surface.slices[0])
        assert characteristic.atm_total_variance == pytest.approx(
            characteristic.atm_volatility**2 * characteristic.time_to_expiry
        )

    def test_an_unfitted_slice_yields_nothing(self):
        unfitted = make_slice(
            date(2026, 10, 24),
            30 / 365,
            parameters=None,
            calibration=calibration(CalibrationStatus.FAILED, parameters=None),
        )
        assert slice_characteristics(unfitted) is None

    def test_a_degraded_slice_is_flagged(self):
        degraded = make_slice(
            date(2026, 10, 24), 30 / 365, calibration=calibration(CalibrationStatus.DEGRADED)
        )
        assert ReferenceFlag.SLICE_DEGRADED in slice_characteristics(degraded).flags


class TestStandardTenors:
    def test_a_tenor_on_a_fitted_expiry_is_exact(self, surface):
        characteristic = characteristics_at_tenor(surface, 30)
        assert characteristic.method is ReferenceMethod.EXACT_SLICE
        assert characteristic.tenor_days == 30
        assert characteristic.kind is CharacteristicKind.STANDARD_TENOR

    def test_a_tenor_between_slices_is_interpolated(self, surface):
        characteristic = characteristics_at_tenor(surface, 60)
        assert characteristic.method is ReferenceMethod.INTERPOLATED_MATURITY
        assert ReferenceFlag.EXTRAPOLATED_MATURITY not in characteristic.flags

    def test_interpolated_total_variance_lies_between_its_neighbours(self, surface):
        short = characteristics_at_tenor(surface, 30)
        middle = characteristics_at_tenor(surface, 60)
        long = characteristics_at_tenor(surface, 90)
        assert short.atm_total_variance <= middle.atm_total_variance <= long.atm_total_variance

    def test_a_tenor_outside_the_range_is_extrapolated_and_flagged(self, surface):
        for tenor in (7, 365):
            characteristic = characteristics_at_tenor(surface, tenor)
            assert characteristic.method is ReferenceMethod.EXTRAPOLATED_MATURITY
            assert ReferenceFlag.EXTRAPOLATED_MATURITY in characteristic.flags

    def test_extrapolation_assumes_flat_forward_variance(self, surface):
        """The only assumption that adds no shape of its own."""
        anchor = characteristics_at_tenor(surface, 90)
        far = characteristics_at_tenor(surface, 365)
        assert far.atm_total_variance == pytest.approx(
            anchor.atm_total_variance * (far.time_to_expiry / anchor.time_to_expiry)
        )

    def test_the_term_structure_covers_every_standard_tenor(self, surface):
        assert [c.tenor_days for c in surface_term_structure(surface)] == list(STANDARD_TENORS)

    def test_a_surface_with_no_fitted_slices_has_no_characteristics(self):
        empty = VolatilitySurface(
            underlying_id=UNDERLYING,
            as_of=AS_OF,
            slices=(
                make_slice(
                    date(2026, 10, 24),
                    30 / 365,
                    parameters=None,
                    calibration=calibration(CalibrationStatus.FAILED, parameters=None),
                ),
            ),
        )
        assert surface_term_structure(empty) == []
        assert characteristics_at_tenor(empty, 30) is None


@dataclass
class Row:
    """Stands in for a persisted characteristic row."""

    as_of_timestamp: datetime
    time_to_expiry: float
    forward: float
    atm_volatility: float
    skew: float
    curvature: float
    atm_total_variance: float
    method: str = "EXACT_SLICE"


def rows(levels: list[float]) -> list[Row]:
    return [
        Row(
            as_of_timestamp=AS_OF + timedelta(days=index),
            time_to_expiry=30 / 365,
            forward=24000.0,
            atm_volatility=level,
            skew=-0.13,
            curvature=1.5,
            atm_total_variance=level**2 * 30 / 365,
        )
        for index, level in enumerate(levels)
    ]


class TestTenorHistory:
    def test_no_history_is_reported_as_none_not_as_a_default(self):
        history = build_tenor_history(30, [])
        assert history.observations == 0
        assert history.percentiles == ()
        assert history.as_of is None

    def test_the_most_recent_row_is_the_current_observation(self):
        history = build_tenor_history(30, rows([0.10, 0.12, 0.18]))
        level = next(p for p in history.percentiles if p.name == "atm_volatility")
        assert level.current == pytest.approx(0.18)
        assert level.percentile == pytest.approx(1.0), "the highest of three"

    def test_every_characteristic_is_ranked(self):
        history = build_tenor_history(30, rows([0.10, 0.12, 0.14, 0.13]))
        assert {p.name for p in history.percentiles} == set(CHARACTERISTIC_NAMES)

    def test_a_middling_level_ranks_in_the_middle(self):
        history = build_tenor_history(30, rows([0.10, 0.20, 0.30, 0.15]))
        level = next(p for p in history.percentiles if p.name == "atm_volatility")
        assert level.percentile == pytest.approx(0.5)

    def test_a_short_history_is_reported_but_marked_unreliable(self):
        """Eight surfaces and six hundred are different kinds of statement."""
        history = build_tenor_history(30, rows([0.10 + 0.01 * i for i in range(8)]))
        assert history.observations == 8
        assert not history.is_reliable
        assert all(not p.is_reliable for p in history.percentiles)
        assert all(p.percentile is not None for p in history.percentiles)

    def test_a_long_history_is_reliable(self):
        history = build_tenor_history(
            30, rows([0.10 + 0.001 * i for i in range(MIN_RELIABLE_OBSERVATIONS + 5)])
        )
        assert history.is_reliable

    def test_a_constant_history_yields_no_z_score(self):
        history = build_tenor_history(30, rows([0.15] * 6))
        level = next(p for p in history.percentiles if p.name == "atm_volatility")
        assert level.z_score is None, "no variation is not infinite significance"
        assert level.percentile == pytest.approx(1.0)

    def test_the_series_is_returned_for_plotting(self):
        history = build_tenor_history(30, rows([0.10, 0.12, 0.14]))
        assert len(history.series) == 3
        assert history.series[0]["atm_volatility"] == pytest.approx(0.10)

    def test_the_series_can_be_omitted(self):
        payload = build_tenor_history(30, rows([0.10, 0.12])).to_dict(include_series=False)
        assert "series" not in payload
        assert payload["observations"] == 2

    def test_the_reliability_threshold_travels_with_the_answer(self):
        payload = build_tenor_history(30, rows([0.10, 0.12])).to_dict()
        assert payload["minimum_reliable_observations"] == MIN_RELIABLE_OBSERVATIONS
        assert payload["is_reliable"] is False
