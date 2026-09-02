"""The fitted surface: reference lookups, flags, and reproducibility."""

from __future__ import annotations

import math
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from domains.derivatives.surface import (
    ReferenceFlag,
    ReferenceMethod,
    SurfaceSliceFit,
    VolatilitySurface,
)
from domains.instruments.enums import OptionType
from quant.pricing.black76 import black76_price
from quant.volatility.svi import SVIParameters
from quant.volatility.svi_calibration import CalibrationStatus, SVICalibrationResult

AS_OF = datetime(2026, 9, 24, 9, 20, tzinfo=UTC)
UNDERLYING = uuid.uuid4()
PARAMS = SVIParameters(a=0.010, b=0.045, rho=-0.55, m=0.015, sigma=0.10)


def calibration(status=CalibrationStatus.CONVERGED, **overrides) -> SVICalibrationResult:
    kwargs = {
        "parameters": PARAMS,
        "status": status,
        "n_observations": 21,
        "rmse_vol_points": 0.05,
        "constraints_satisfied": status is CalibrationStatus.CONVERGED,
    }
    kwargs.update(overrides)
    return SVICalibrationResult(**kwargs)


def slice_at(expiry: date, tau: float, forward: float = 24000.0, **overrides) -> SurfaceSliceFit:
    kwargs = {
        "expiry": expiry,
        "time_to_expiry": tau,
        "forward": forward,
        "discount_factor": math.exp(-0.065 * tau),
        "parameters": PARAMS,
        "calibration": calibration(),
        "k_min": -0.20,
        "k_max": 0.20,
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
            slice_at(date(2026, 10, 29), 0.0960, 24150.0),
            slice_at(date(2026, 12, 24), 0.2494, 24392.0),
        ),
        curve_id="curve:abc",
    )


class TestIdentity:
    def test_surface_id_is_content_addressed(self, surface):
        same = VolatilitySurface(
            underlying_id=UNDERLYING,
            as_of=AS_OF,
            slices=surface.slices,
            curve_id="curve:abc",
        )
        assert surface.surface_id == same.surface_id
        assert surface.surface_id.startswith("surface:")

    def test_different_parameters_give_a_different_id(self, surface):
        moved = VolatilitySurface(
            underlying_id=UNDERLYING,
            as_of=AS_OF,
            slices=(
                slice_at(
                    date(2026, 10, 29),
                    0.0960,
                    24150.0,
                    parameters=SVIParameters(a=0.011, b=0.045, rho=-0.55, m=0.015, sigma=0.10),
                ),
                surface.slices[1],
            ),
            curve_id="curve:abc",
        )
        assert moved.surface_id != surface.surface_id

    def test_a_different_curve_gives_a_different_id(self, surface):
        other = VolatilitySurface(
            underlying_id=UNDERLYING,
            as_of=AS_OF,
            slices=surface.slices,
            curve_id="curve:xyz",
        )
        assert other.surface_id != surface.surface_id


class TestExactSlice:
    def test_reference_iv_matches_the_parameterization(self, surface):
        slice_ = surface.slices[0]
        strike = Decimal("24000")
        point = surface.reference(strike, slice_.expiry)

        assert point.method is ReferenceMethod.EXACT_SLICE
        k = math.log(24000.0 / slice_.forward)
        assert point.log_moneyness == pytest.approx(k)
        assert point.reference_iv == pytest.approx(float(slice_.implied_vol(k)))
        assert point.total_variance == pytest.approx(point.reference_iv**2 * slice_.time_to_expiry)

    def test_reference_price_is_black76_at_the_reference_vol(self, surface):
        slice_ = surface.slices[0]
        point = surface.reference(Decimal("24000"), slice_.expiry, OptionType.CALL)
        expected = float(
            black76_price(
                slice_.forward,
                24000.0,
                slice_.time_to_expiry,
                point.reference_iv,
                True,
                slice_.discount_factor,
            )
        )
        assert point.reference_price == pytest.approx(expected)

    def test_no_price_without_an_option_type(self, surface):
        point = surface.reference(Decimal("24000"), surface.slices[0].expiry)
        assert point.reference_iv is not None
        assert point.reference_price is None

    def test_put_and_call_reference_prices_satisfy_parity(self, surface):
        slice_ = surface.slices[1]
        strike = Decimal("25000")
        call = surface.reference(strike, slice_.expiry, OptionType.CALL)
        put = surface.reference(strike, slice_.expiry, OptionType.PUT)
        expected = slice_.discount_factor * (slice_.forward - 25000.0)
        assert call.reference_price - put.reference_price == pytest.approx(expected)


class TestFlags:
    def test_a_strike_inside_the_fitted_range_is_unflagged(self, surface):
        slice_ = surface.slices[0]
        inside = Decimal(str(round(slice_.forward)))
        assert surface.reference(inside, slice_.expiry).flags == ()

    def test_a_strike_outside_the_fitted_range_is_flagged(self, surface):
        """SVI's wings are weakly constrained by a narrow window, so a lookup
        past the data must say so."""
        slice_ = surface.slices[0]
        far = Decimal(str(round(slice_.forward * math.exp(0.5))))
        point = surface.reference(far, slice_.expiry)
        assert ReferenceFlag.EXTRAPOLATED_STRIKE in point.flags
        assert point.reference_iv is not None, "flagged, not withheld"

    def test_a_degraded_slice_is_flagged(self):
        degraded = VolatilitySurface(
            underlying_id=UNDERLYING,
            as_of=AS_OF,
            slices=(
                slice_at(
                    date(2026, 10, 29),
                    0.0960,
                    calibration=calibration(CalibrationStatus.DEGRADED),
                ),
            ),
        )
        point = degraded.reference(Decimal("24000"), date(2026, 10, 29))
        assert ReferenceFlag.SLICE_DEGRADED in point.flags

    def test_a_low_confidence_forward_is_flagged(self):
        weak = VolatilitySurface(
            underlying_id=UNDERLYING,
            as_of=AS_OF,
            slices=(slice_at(date(2026, 10, 29), 0.0960, forward_confidence=0.3),),
        )
        point = weak.reference(Decimal("24000"), date(2026, 10, 29))
        assert ReferenceFlag.LOW_CONFIDENCE_FORWARD in point.flags


class TestMaturityInterpolation:
    def test_between_two_slices(self, surface):
        point = surface.reference(Decimal("24200"), date(2026, 11, 20))
        assert point.method is ReferenceMethod.INTERPOLATED_MATURITY
        assert point.reference_iv is not None
        short, long = surface.slices
        assert short.time_to_expiry < point.time_to_expiry < long.time_to_expiry

    def test_interpolated_total_variance_lies_between_the_neighbours(self, surface):
        """Interpolating variance rather than volatility keeps a calendar-
        consistent pair calendar-consistent."""
        strike = Decimal("24200")
        mid = surface.reference(strike, date(2026, 11, 20))
        short = surface.reference(strike, surface.slices[0].expiry)
        long = surface.reference(strike, surface.slices[1].expiry)
        assert short.total_variance <= mid.total_variance <= long.total_variance

    def test_beyond_the_last_expiry_is_extrapolation_and_says_so(self, surface):
        point = surface.reference(Decimal("24400"), date(2027, 6, 30))
        assert point.method is ReferenceMethod.EXTRAPOLATED_MATURITY
        assert ReferenceFlag.EXTRAPOLATED_MATURITY in point.flags
        assert point.reference_iv is not None

    def test_before_the_first_expiry_is_extrapolation(self, surface):
        point = surface.reference(Decimal("24100"), date(2026, 9, 30))
        assert point.method is ReferenceMethod.EXTRAPOLATED_MATURITY
        assert ReferenceFlag.EXTRAPOLATED_MATURITY in point.flags


class TestUnusableSurface:
    def test_a_surface_with_no_fitted_slices_returns_a_reason(self):
        empty = VolatilitySurface(
            underlying_id=UNDERLYING,
            as_of=AS_OF,
            slices=(
                slice_at(
                    date(2026, 10, 29),
                    0.0960,
                    parameters=None,
                    calibration=calibration(CalibrationStatus.FAILED, parameters=None),
                ),
            ),
        )
        point = empty.reference(Decimal("24000"), date(2026, 10, 29))
        assert point.method is ReferenceMethod.UNAVAILABLE
        assert point.reference_iv is None
        assert point.error


class TestReproducibility:
    def test_reference_values_depend_only_on_the_persisted_parameters(self, surface):
        """The Phase 2 acceptance criterion: a stored surface reproduces its
        reference IVs from ``(a, b, rho, m, sigma)``, the forward and the
        maturity, with no re-fitting on read."""
        original = surface.slices[0]
        rebuilt = VolatilitySurface(
            underlying_id=UNDERLYING,
            as_of=AS_OF,
            slices=(
                SurfaceSliceFit(
                    expiry=original.expiry,
                    time_to_expiry=original.time_to_expiry,
                    forward=original.forward,
                    discount_factor=original.discount_factor,
                    parameters=SVIParameters(**original.parameters.to_dict()),
                    calibration=calibration(),
                    k_min=original.k_min,
                    k_max=original.k_max,
                ),
            ),
            curve_id=surface.curve_id,
        )
        for strike in ("22000", "24000", "26000"):
            first = surface.reference(Decimal(strike), original.expiry, OptionType.CALL)
            second = rebuilt.reference(Decimal(strike), original.expiry, OptionType.CALL)
            assert first.reference_iv == second.reference_iv
            assert first.reference_price == second.reference_price

    def test_lookups_are_pure(self, surface):
        strike, expiry = Decimal("24000"), surface.slices[0].expiry
        results = [surface.reference(strike, expiry).reference_iv for _ in range(5)]
        assert len(set(results)) == 1


class TestSerialisation:
    def test_surface_serialises_with_and_without_slices(self, surface):
        with_slices = surface.to_dict()
        assert with_slices["counts"]["fitted"] == 2
        assert len(with_slices["slices"]) == 2
        assert surface.to_dict(include_slices=False).get("slices") is None

    def test_reference_point_serialises(self, surface):
        payload = surface.reference(
            Decimal("24000"), surface.slices[0].expiry, OptionType.CALL
        ).to_dict()
        for key in ("reference_iv", "reference_price", "method", "flags"):
            assert key in payload
        # It must not be mistakable for an observation.
        assert "market_iv" not in payload
        assert "fair_value" not in payload
