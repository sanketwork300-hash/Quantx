"""Surface calibration: fit SVI per expiry and diagnose the result.

The order of operations is deliberate:

    raw quotes  ->  arbitrage diagnostics on the RAW market
                ->  SVI calibration per expiry
                ->  arbitrage diagnostics on the FITTED surface

Checking the raw market **first** means a bad fit is never blamed on the market
and a bad market is never hidden by a smooth fit. Both reports are produced and
stored separately, always.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from domains.derivatives.arbitrage import (
    ArbitrageReport,
    ArbitrageValidator,
    ViolationType,
)
from domains.derivatives.models import ChainAnalysis
from domains.derivatives.surface import (
    SURFACE_MODEL,
    SURFACE_MODEL_VERSION,
    SurfaceSliceFit,
    VolatilitySurface,
)
from domains.market_data.quality.flags import Severity
from domains.reports.warnings import AnalyticalWarning
from quant.volatility.svi_calibration import (
    MIN_OBSERVATIONS,
    CalibrationStatus,
    SVICalibrationResult,
    calibrate_svi,
)

CALIBRATION_MODEL_VERSION = SURFACE_MODEL_VERSION
DEFAULT_SEED = 20_260_924


class SurfaceWarningCode:
    NO_SLICES_FITTED = "SURFACE_NO_SLICES_FITTED"
    SLICE_INSUFFICIENT = "SURFACE_SLICE_INSUFFICIENT_OBSERVATIONS"
    SLICE_FAILED = "SURFACE_SLICE_CALIBRATION_FAILED"
    SLICE_DEGRADED = "SURFACE_SLICE_DEGRADED"
    POOR_FIT = "SURFACE_POOR_FIT"
    NARROW_STRIKE_RANGE = "SURFACE_NARROW_STRIKE_RANGE"
    RAW_ARBITRAGE = "SURFACE_RAW_MARKET_ARBITRAGE"
    FITTED_ARBITRAGE = "SURFACE_FITTED_ARBITRAGE"
    CALENDAR_NOT_PREVENTED = "SURFACE_CALENDAR_NOT_PREVENTED"


#: Log-moneyness width below which SVI's five parameters are not identifiable.
#: Inside a narrow window many parameter sets fit the observed arc equally well,
#: so the fitted curve is trustworthy in sample while the wings are essentially
#: unconstrained. Measured: on k in [-0.07, 0.03] the parameters miss the truth
#: by 0.05 while the in-sample curve is right to 0.005 volatility points.
NARROW_K_RANGE = 0.20

#: A slice fitting worse than this in volatility points is reported. It is not
#: an error — a wide, illiquid chain legitimately fits loosely — but a reference
#: value drawn from it inherits the error and the user should see it.
POOR_FIT_VOL_POINTS = 1.0


@dataclass(frozen=True, slots=True)
class SurfaceCalibrationRequest:
    seed: int = DEFAULT_SEED
    use_weights: bool = True
    max_iterations: int = 400

    def to_provenance(self) -> dict:
        return {
            "seed": self.seed,
            "use_weights": self.use_weights,
            "max_iterations": self.max_iterations,
            "min_observations": MIN_OBSERVATIONS,
            "model": SURFACE_MODEL,
        }


@dataclass(frozen=True, slots=True)
class SurfaceCalibrationResult:
    surface: VolatilitySurface
    raw_report: ArbitrageReport
    fitted_report: ArbitrageReport
    warnings: tuple[AnalyticalWarning, ...]

    @property
    def fitted_count(self) -> int:
        return len(self.surface.fitted_slices)


class SurfaceCalibrationService:
    """Pure computation over a `ChainAnalysis`. No I/O, no session."""

    def calibrate(
        self, analysis: ChainAnalysis, request: SurfaceCalibrationRequest
    ) -> SurfaceCalibrationResult:
        warnings: list[AnalyticalWarning] = []
        validator = ArbitrageValidator()

        # Raw first, so the market is judged on its own terms.
        raw_report = validator.validate_raw(list(analysis.slices))
        self._report_arbitrage(warnings, raw_report, SurfaceWarningCode.RAW_ARBITRAGE)

        fits = [self._fit_slice(slice_, request, warnings) for slice_ in analysis.slices]

        surface = VolatilitySurface(
            underlying_id=analysis.underlying_id,
            as_of=_parse_timestamp(analysis.as_of),
            slices=tuple(fits),
            curve_id=analysis.curve_id,
            analysis_id=analysis.snapshot_id,
            model=SURFACE_MODEL,
            model_version=SURFACE_MODEL_VERSION,
        )

        fitted_report = validator.validate_surface(surface)
        self._report_arbitrage(warnings, fitted_report, SurfaceWarningCode.FITTED_ARBITRAGE)

        if any(
            violation.violation_type is ViolationType.CALENDAR
            for violation in fitted_report.violations
        ):
            warnings.append(
                AnalyticalWarning.warn(
                    SurfaceWarningCode.CALENDAR_NOT_PREVENTED,
                    "The fitted surface violates calendar consistency. Per-expiry "
                    "SVI fits each slice independently and cannot prevent this; it "
                    "is detected and reported until an arbitrage-free global "
                    "parameterization (SSVI) lands in Phase 9.",
                )
            )

        if not surface.fitted_slices:
            warnings.append(
                AnalyticalWarning.error(
                    SurfaceWarningCode.NO_SLICES_FITTED,
                    "No expiry could be calibrated, so the surface has no usable "
                    "slices and produces no reference values.",
                )
            )

        return SurfaceCalibrationResult(
            surface=surface,
            raw_report=raw_report,
            fitted_report=fitted_report,
            warnings=tuple(warnings),
        )

    # ------------------------------------------------------------- per slice
    def _fit_slice(
        self,
        slice_,
        request: SurfaceCalibrationRequest,
        warnings: list[AnalyticalWarning],
    ) -> SurfaceSliceFit:
        selected = slice_.forward.selected
        forward = selected.value if selected else None
        discount = (selected.discount_factor if selected else None) or 1.0
        tau = slice_.time_to_expiry

        points = [
            point
            for point in slice_.points
            if point.used_for_smile
            and point.market_iv is not None
            and point.log_moneyness is not None
            and point.total_variance is not None
        ]

        if forward is None or tau is None or tau <= 0 or len(points) < MIN_OBSERVATIONS:
            reason = (
                "no usable forward"
                if forward is None
                else "non-positive time to expiry"
                if tau is None or tau <= 0
                else f"{len(points)} usable quotes"
            )
            status = (
                CalibrationStatus.INSUFFICIENT_OBSERVATIONS
                if forward is not None and tau and tau > 0
                else CalibrationStatus.FAILED
            )
            warnings.append(
                AnalyticalWarning.warn(
                    SurfaceWarningCode.SLICE_INSUFFICIENT
                    if status is CalibrationStatus.INSUFFICIENT_OBSERVATIONS
                    else SurfaceWarningCode.SLICE_FAILED,
                    f"Expiry {slice_.expiry} was not calibrated: {reason}.",
                    expiry=slice_.expiry.isoformat(),
                )
            )
            return SurfaceSliceFit(
                expiry=slice_.expiry,
                time_to_expiry=tau or 0.0,
                forward=forward or 0.0,
                discount_factor=discount,
                parameters=None,
                calibration=SVICalibrationResult(
                    parameters=None,
                    status=status,
                    n_observations=len(points),
                    error=reason,
                ),
                forward_method=str(selected.method) if selected else None,
                forward_confidence=selected.confidence if selected else 0.0,
            )

        k = np.array([point.log_moneyness for point in points])
        w = np.array([point.total_variance for point in points])
        weights = np.array([point.weight for point in points]) if request.use_weights else None

        calibration = calibrate_svi(
            k, w, tau, weights, seed=request.seed, max_iterations=request.max_iterations
        )

        if calibration.status is CalibrationStatus.FAILED:
            warnings.append(
                AnalyticalWarning.warn(
                    SurfaceWarningCode.SLICE_FAILED,
                    f"Expiry {slice_.expiry} did not calibrate: {calibration.error}.",
                    expiry=slice_.expiry.isoformat(),
                )
            )
        elif calibration.status is CalibrationStatus.DEGRADED:
            warnings.append(
                AnalyticalWarning.warn(
                    SurfaceWarningCode.SLICE_DEGRADED,
                    f"Expiry {slice_.expiry} calibrated but is not admissible: "
                    f"{calibration.error}. Reference values from this slice carry a "
                    "SLICE_DEGRADED flag.",
                    expiry=slice_.expiry.isoformat(),
                )
            )
        elif (calibration.rmse_vol_points or 0.0) > POOR_FIT_VOL_POINTS:
            warnings.append(
                AnalyticalWarning.info(
                    SurfaceWarningCode.POOR_FIT,
                    f"Expiry {slice_.expiry} fits to "
                    f"{calibration.rmse_vol_points:.2f} volatility points RMSE. "
                    "Reference values from this slice inherit that error.",
                    expiry=slice_.expiry.isoformat(),
                    rmse_vol_points=calibration.rmse_vol_points,
                )
            )

        k_width = float(np.max(k) - np.min(k))
        if k_width < NARROW_K_RANGE and calibration.parameters is not None:
            warnings.append(
                AnalyticalWarning.info(
                    SurfaceWarningCode.NARROW_STRIKE_RANGE,
                    f"Expiry {slice_.expiry} spans only {k_width:.3f} in "
                    "log-moneyness. SVI's five parameters are not identifiable over "
                    "so narrow a window: the fitted curve is reliable across the "
                    "observed strikes, but the wings are essentially unconstrained "
                    "and reference values outside the range carry an "
                    "EXTRAPOLATED_STRIKE flag.",
                    expiry=slice_.expiry.isoformat(),
                    k_range=[float(np.min(k)), float(np.max(k))],
                )
            )

        return SurfaceSliceFit(
            expiry=slice_.expiry,
            time_to_expiry=tau,
            forward=forward,
            discount_factor=discount,
            parameters=calibration.parameters,
            calibration=calibration,
            k_min=float(np.min(k)),
            k_max=float(np.max(k)),
            forward_method=str(selected.method),
            forward_confidence=selected.confidence,
        )

    @staticmethod
    def _report_arbitrage(
        warnings: list[AnalyticalWarning], report: ArbitrageReport, code: str
    ) -> None:
        serious = report.at_or_above(Severity.WARNING)
        if not serious:
            return
        scope = (
            "observed market data" if report.scope.value == "RAW_MARKET" else "the fitted surface"
        )
        note = (
            " A violation in observed quotes is almost always a data artefact -- "
            "stale legs, non-simultaneous quotes, a wrong multiplier -- not an "
            "executable opportunity."
            if report.scope.value == "RAW_MARKET"
            else ""
        )
        warnings.append(
            AnalyticalWarning.warn(
                code,
                f"{len(serious)} no-arbitrage condition(s) are violated in {scope}." + note,
                counts=report.summary,
            )
        )


def _parse_timestamp(value: str):
    from datetime import datetime

    return datetime.fromisoformat(value)
