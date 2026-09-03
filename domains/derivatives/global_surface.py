"""The global (SSVI) surface: one fit for every expiry at once.

Phase 2's surface is a list of independent per-expiry SVI fits. It reproduces
each smile well and has no structural reason for two neighbouring expiries to be
consistent, so calendar arbitrage is something it *detects* and reports.

This module fits SSVI instead: three shape parameters shared across the whole
surface plus one at-the-money total variance per expiry. Requiring that variance
term structure to be non-decreasing **is** the no-calendar-arbitrage condition,
so an admissible SSVI surface cannot contain the violation the SVI surface could
only name. The two live side by side rather than one replacing the other — the
per-expiry fit is still the better description of any single smile, and the
difference between them is itself informative.

Reference values from this surface carry the same :class:`ReferencePoint` shape
the Phase 2 surface produces, with the same flags, so nothing downstream has to
know which surface answered.
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

import numpy as np

from domains.derivatives.models import ChainAnalysis
from domains.derivatives.surface import ReferenceFlag, ReferenceMethod, ReferencePoint
from domains.instruments.enums import OptionType
from domains.reports.warnings import AnalyticalWarning
from quant.pricing.black76 import black76_price
from quant.volatility.local_vol import SurfaceLocalVol, local_volatility_surface
from quant.volatility.ssvi import SSVIParameters, SSVISurface, ThetaTermStructure
from quant.volatility.ssvi_calibration import (
    SSVICalibrationResult,
    SSVISliceDiagnostics,
    SSVISliceObservations,
    calibrate_ssvi,
)
from quant.volatility.svi_calibration import CalibrationStatus

GLOBAL_SURFACE_MODEL = "SSVI"
GLOBAL_SURFACE_MODEL_VERSION = "ssvi@1.0.0"
LOCAL_VOL_MODEL_VERSION = "dupire-ssvi@1.0.0"
DEFAULT_SEED = 20_260_924

#: Fewer quotes than this on an expiry and the slice contributes a theta knot
#: that is essentially one quote's opinion. It is still used — a thin expiry is
#: real information about the term structure — but it is reported.
THIN_SLICE_QUOTES = 5

#: Fit error above which the global surface is reported as fitting this market
#: worse than a per-expiry fit would. Three volatility points is not a failure;
#: it is the point at which the reader should look at the SVI surface too.
POOR_GLOBAL_FIT_VOL_POINTS = 3.0


class GlobalSurfaceWarningCode:
    NOT_CALIBRATED = "GLOBAL_SURFACE_NOT_CALIBRATED"
    DEGRADED = "GLOBAL_SURFACE_DEGRADED"
    POOR_FIT = "GLOBAL_SURFACE_POOR_FIT"
    THIN_SLICE = "GLOBAL_SURFACE_THIN_SLICE"
    SLICE_UNUSABLE = "GLOBAL_SURFACE_SLICE_UNUSABLE"
    BUTTERFLY_BOUNDS_NOT_MET = "GLOBAL_SURFACE_BUTTERFLY_BOUNDS_NOT_MET"
    NON_MONOTONE_OBSERVED_VARIANCE = "GLOBAL_SURFACE_NON_MONOTONE_OBSERVED_VARIANCE"
    SINGLE_EXPIRY = "GLOBAL_SURFACE_SINGLE_EXPIRY"


@dataclass(frozen=True, slots=True)
class GlobalSurfaceCalibrationRequest:
    seed: int = DEFAULT_SEED
    use_weights: bool = True
    max_iterations: int = 400
    enforce_butterfly_bounds: bool = True

    def to_provenance(self) -> dict:
        return {
            "seed": self.seed,
            "use_weights": self.use_weights,
            "max_iterations": self.max_iterations,
            "enforce_butterfly_bounds": self.enforce_butterfly_bounds,
            "model": GLOBAL_SURFACE_MODEL,
        }


@dataclass(frozen=True, slots=True)
class GlobalSurfaceSlice:
    """One expiry's contribution: a theta knot and the market context for it."""

    expiry: date
    time_to_expiry: float
    forward: float
    discount_factor: float
    theta: float
    diagnostics: SSVISliceDiagnostics | None = None
    forward_method: str | None = None
    forward_confidence: float = 0.0

    def to_dict(self) -> dict:
        return {
            "expiry": self.expiry.isoformat(),
            "time_to_expiry": self.time_to_expiry,
            "forward": self.forward,
            "discount_factor": self.discount_factor,
            "theta": self.theta,
            "atm_volatility": math.sqrt(self.theta / self.time_to_expiry)
            if self.time_to_expiry > 0
            else None,
            "forward_method": self.forward_method,
            "forward_confidence": self.forward_confidence,
            "diagnostics": self.diagnostics.to_dict() if self.diagnostics else None,
        }


@dataclass(frozen=True, slots=True)
class GlobalSurface:
    """A calibrated SSVI surface, addressed by a content-derived id."""

    underlying_id: uuid.UUID
    as_of: datetime
    parameters: SSVIParameters | None
    term_structure: ThetaTermStructure | None
    slices: tuple[GlobalSurfaceSlice, ...]
    underlying_price: float | None = None
    curve_id: str | None = None
    analysis_id: uuid.UUID | None = None
    model: str = GLOBAL_SURFACE_MODEL
    model_version: str = GLOBAL_SURFACE_MODEL_VERSION

    @property
    def usable(self) -> bool:
        return self.parameters is not None and self.term_structure is not None and self.slices != ()

    @property
    def ssvi(self) -> SSVISurface | None:
        if self.parameters is None or self.term_structure is None:
            return None
        return SSVISurface(parameters=self.parameters, term_structure=self.term_structure)

    @property
    def surface_id(self) -> str:
        """Two surfaces with this id were fitted from the same numbers."""
        payload = {
            "underlying_id": str(self.underlying_id),
            "as_of": self.as_of.isoformat(),
            "model": self.model,
            "model_version": self.model_version,
            "curve_id": self.curve_id,
            "parameters": self.parameters.to_dict() if self.parameters else None,
            "slices": [
                {
                    "expiry": slice_.expiry.isoformat(),
                    "tau": slice_.time_to_expiry,
                    "forward": slice_.forward,
                    "discount_factor": slice_.discount_factor,
                    "theta": slice_.theta,
                }
                for slice_ in self.slices
            ],
        }
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode())
        return f"global-surface:{digest.hexdigest()[:16]}"

    @property
    def carry(self) -> float:
        """The deterministic carry ``r - q`` that best matches the forwards.

        Least squares through the origin on ``log(F_i / S) = carry * tau_i``.
        A single rate is an approximation of a forward curve, and it is stated
        as one: it is used only to place the local-volatility grid on the right
        log-moneyness coordinate at intermediate times, where the alternative is
        the strictly worse assumption that carry is zero.
        """
        if self.underlying_price is None or self.underlying_price <= 0 or not self.slices:
            return 0.0
        taus = np.array([s.time_to_expiry for s in self.slices])
        logs = np.log(np.array([s.forward for s in self.slices]) / self.underlying_price)
        denominator = float(np.sum(taus * taus))
        if denominator <= 0:
            return 0.0
        return float(np.sum(taus * logs) / denominator)

    # ------------------------------------------------------------- lookups
    def slice_for(self, expiry: date) -> GlobalSurfaceSlice | None:
        for slice_ in self.slices:
            if slice_.expiry == expiry:
                return slice_
        return None

    def time_to_expiry(self, expiry: date) -> float:
        """Maturity for an arbitrary date, on the slices' own time scale."""
        exact = self.slice_for(expiry)
        if exact is not None:
            return exact.time_to_expiry
        ordered = sorted(self.slices, key=lambda s: s.expiry)
        target = expiry.toordinal()
        if len(ordered) == 1:
            only = ordered[0]
            origin = only.expiry.toordinal() - max(round(only.time_to_expiry * 365), 1)
            scale = only.time_to_expiry / max(only.expiry.toordinal() - origin, 1)
            return max(scale * (target - origin), 1e-9)
        first, last = ordered[0], ordered[-1]
        days = last.expiry.toordinal() - first.expiry.toordinal()
        slope = (last.time_to_expiry - first.time_to_expiry) / max(days, 1)
        return max(first.time_to_expiry + slope * (target - first.expiry.toordinal()), 1e-9)

    def forward_for(self, expiry: date) -> tuple[float, float, bool]:
        """``(forward, discount factor, extrapolated)`` for any expiry.

        Interpolated linearly in log-forward across maturity between fitted
        expiries; held at the nearest fitted forward outside them, because
        continuing the slope past the observed range would invent a carry the
        market never quoted.
        """
        ordered = sorted(self.slices, key=lambda s: s.time_to_expiry)
        tau = self.time_to_expiry(expiry)
        if len(ordered) == 1:
            return ordered[0].forward, ordered[0].discount_factor, expiry != ordered[0].expiry
        if tau <= ordered[0].time_to_expiry:
            return ordered[0].forward, ordered[0].discount_factor, tau < ordered[0].time_to_expiry
        if tau >= ordered[-1].time_to_expiry:
            return (
                ordered[-1].forward,
                ordered[-1].discount_factor,
                tau > ordered[-1].time_to_expiry,
            )

        index = int(np.searchsorted([s.time_to_expiry for s in ordered], tau))
        left, right = ordered[max(index - 1, 0)], ordered[min(index, len(ordered) - 1)]
        span = right.time_to_expiry - left.time_to_expiry
        weight = 0.0 if span <= 0 else (tau - left.time_to_expiry) / span
        forward = math.exp(
            math.log(left.forward) + weight * (math.log(right.forward) - math.log(left.forward))
        )
        discount = left.discount_factor + weight * (right.discount_factor - left.discount_factor)
        return forward, discount, False

    def reference(
        self,
        strike: Decimal,
        expiry: date,
        option_type: OptionType | None = None,
    ) -> ReferencePoint:
        surface = self.ssvi
        if surface is None or not self.slices:
            return ReferencePoint(
                strike=strike,
                expiry=expiry,
                option_type=option_type,
                method=ReferenceMethod.UNAVAILABLE,
                error="the global surface was not calibrated",
            )

        tau = self.time_to_expiry(expiry)
        forward, discount, forward_extrapolated = self.forward_for(expiry)
        k = math.log(float(strike) / forward)
        w = float(np.asarray(surface.total_variance(k, tau)))
        iv = math.sqrt(max(w, 0.0) / tau)

        exact = self.slice_for(expiry)
        assert self.term_structure is not None
        outside_maturity = self.term_structure.is_extrapolated(tau)
        method = (
            ReferenceMethod.EXACT_SLICE
            if exact is not None
            else ReferenceMethod.EXTRAPOLATED_MATURITY
            if outside_maturity or forward_extrapolated
            else ReferenceMethod.INTERPOLATED_MATURITY
        )

        flags: list[ReferenceFlag] = []
        if outside_maturity or forward_extrapolated:
            flags.append(ReferenceFlag.EXTRAPOLATED_MATURITY)
        anchor = exact or min(self.slices, key=lambda s: abs(s.time_to_expiry - tau))
        if anchor.diagnostics is not None and not (
            anchor.diagnostics.k_min <= k <= anchor.diagnostics.k_max
        ):
            flags.append(ReferenceFlag.EXTRAPOLATED_STRIKE)
        if anchor.forward_confidence < 0.5:
            flags.append(ReferenceFlag.LOW_CONFIDENCE_FORWARD)

        price = None
        if option_type is not None:
            price = float(
                black76_price(
                    forward, float(strike), tau, iv, option_type is OptionType.CALL, discount
                )
            )

        return ReferencePoint(
            strike=strike,
            expiry=expiry,
            option_type=option_type,
            method=method,
            time_to_expiry=tau,
            forward=forward,
            discount_factor=discount,
            log_moneyness=k,
            reference_iv=iv,
            total_variance=w,
            reference_price=price,
            calibration_rmse_vol_points=(
                anchor.diagnostics.rmse_vol_points if anchor.diagnostics else None
            ),
            flags=tuple(flags),
        )

    def local_volatility(self, fallback: float) -> SurfaceLocalVol | None:
        """The Dupire coefficient this surface implies, as the PDE consumes it."""
        surface = self.ssvi
        if surface is None or self.underlying_price is None or self.underlying_price <= 0:
            return None
        return SurfaceLocalVol(
            surface=surface, spot=float(self.underlying_price), carry=self.carry, fallback=fallback
        )

    def local_volatility_grid(self, k_range: float = 0.5, nodes: int = 41, maturities=None):
        """A Dupire grid over the fitted maturity range, holes and all."""
        surface = self.ssvi
        if surface is None or not self.slices:
            return None
        if maturities is None:
            fitted = [s.time_to_expiry for s in self.slices]
            maturities = list(np.linspace(min(fitted), max(fitted), max(len(fitted) * 3, 6)))
        grid = np.linspace(-k_range, k_range, nodes)
        return local_volatility_surface(surface, grid, maturities)

    def to_dict(self, include_slices: bool = True) -> dict:
        payload = {
            "surface_id": self.surface_id,
            "underlying_id": str(self.underlying_id),
            "as_of_timestamp": self.as_of.isoformat(),
            "model": self.model,
            "model_version": self.model_version,
            "curve_id": self.curve_id,
            "analysis_id": str(self.analysis_id) if self.analysis_id else None,
            "underlying_price": self.underlying_price,
            "carry": self.carry,
            "parameters": self.parameters.to_dict() if self.parameters else None,
            "term_structure": (
                self.term_structure.to_dict() if self.term_structure is not None else None
            ),
            "counts": {"slices": len(self.slices)},
        }
        if include_slices:
            payload["slices"] = [slice_.to_dict() for slice_ in self.slices]
        return payload


@dataclass(frozen=True, slots=True)
class GlobalSurfaceCalibrationResult:
    surface: GlobalSurface
    calibration: SSVICalibrationResult
    warnings: tuple[AnalyticalWarning, ...] = field(default=())

    def to_dict(self, include_slices: bool = True) -> dict:
        return {
            "surface": self.surface.to_dict(include_slices),
            "calibration": self.calibration.to_dict(),
        }


class GlobalSurfaceCalibrationService:
    """Pure computation over a `ChainAnalysis`. No I/O, no session."""

    def calibrate(
        self, analysis: ChainAnalysis, request: GlobalSurfaceCalibrationRequest
    ) -> GlobalSurfaceCalibrationResult:
        warnings: list[AnalyticalWarning] = []
        observations: list[SSVISliceObservations] = []
        context: dict[float, tuple] = {}

        for slice_ in analysis.slices:
            selected = slice_.forward.selected
            forward = selected.value if selected else None
            tau = slice_.time_to_expiry
            points = [
                point
                for point in slice_.points
                if point.used_for_smile
                and point.log_moneyness is not None
                and point.total_variance is not None
                and point.total_variance > 0
            ]
            if forward is None or tau is None or tau <= 0 or not points:
                warnings.append(
                    AnalyticalWarning.info(
                        GlobalSurfaceWarningCode.SLICE_UNUSABLE,
                        f"Expiry {slice_.expiry} contributes no knot to the "
                        "at-the-money variance term structure: "
                        + (
                            "no usable forward"
                            if forward is None
                            else "non-positive time to expiry"
                            if tau is None or tau <= 0
                            else "no quote survived smile selection"
                        )
                        + ".",
                        expiry=slice_.expiry.isoformat(),
                    )
                )
                continue

            if len(points) < THIN_SLICE_QUOTES:
                warnings.append(
                    AnalyticalWarning.info(
                        GlobalSurfaceWarningCode.THIN_SLICE,
                        f"Expiry {slice_.expiry} contributes a term-structure knot "
                        f"from only {len(points)} quotes. The knot is used — a thin "
                        "expiry is still information about the term structure — but "
                        "it is pinned by very little.",
                        expiry=slice_.expiry.isoformat(),
                        quotes=len(points),
                    )
                )

            observations.append(
                SSVISliceObservations(
                    maturity=tau,
                    log_moneyness=np.array([p.log_moneyness for p in points], dtype=float),
                    total_variance=np.array([p.total_variance for p in points], dtype=float),
                    weights=(
                        np.array([p.weight for p in points], dtype=float)
                        if request.use_weights
                        else None
                    ),
                    label=slice_.expiry.isoformat(),
                )
            )
            context[tau] = (
                slice_.expiry,
                float(forward),
                float(selected.discount_factor or 1.0),
                str(selected.method),
                float(selected.confidence),
            )

        as_of = datetime.fromisoformat(analysis.as_of)
        underlying_price = (
            float(analysis.underlying_price) if analysis.underlying_price is not None else None
        )

        if not observations:
            warnings.append(
                AnalyticalWarning.error(
                    GlobalSurfaceWarningCode.NOT_CALIBRATED,
                    "No expiry contributed a usable slice, so no global surface "
                    "could be fitted and it produces no reference values.",
                )
            )
            return GlobalSurfaceCalibrationResult(
                surface=GlobalSurface(
                    underlying_id=analysis.underlying_id,
                    as_of=as_of,
                    parameters=None,
                    term_structure=None,
                    slices=(),
                    underlying_price=underlying_price,
                    curve_id=analysis.curve_id,
                    analysis_id=analysis.snapshot_id,
                ),
                calibration=SSVICalibrationResult(
                    parameters=None,
                    term_structure=None,
                    status=CalibrationStatus.INSUFFICIENT_OBSERVATIONS,
                    n_observations=0,
                    n_slices=0,
                    error="no expiry produced usable quotes",
                ),
                warnings=tuple(warnings),
            )

        if len(observations) == 1:
            warnings.append(
                AnalyticalWarning.warn(
                    GlobalSurfaceWarningCode.SINGLE_EXPIRY,
                    "Only one expiry was usable. SSVI's advantage over a per-expiry "
                    "fit is that its variance term structure cannot contain calendar "
                    "arbitrage, and a single expiry has no term structure to be "
                    "consistent with. The maturity decay parameter gamma is not "
                    "identified by one slice, and total variance away from that "
                    "expiry is taken proportional to maturity.",
                )
            )

        observed = [
            (
                item.maturity,
                float(np.interp(0.0, *_sorted(item.log_moneyness, item.total_variance))),
            )
            for item in sorted(observations, key=lambda i: i.maturity)
        ]
        pairs = zip(observed, observed[1:], strict=False)
        if any(later[1] < earlier[1] for earlier, later in pairs):
            warnings.append(
                AnalyticalWarning.warn(
                    GlobalSurfaceWarningCode.NON_MONOTONE_OBSERVED_VARIANCE,
                    "The observed at-the-money total variance decreases somewhere "
                    "across maturity, which is calendar arbitrage in the raw market. "
                    "The fit imposes a non-decreasing term structure, so the surface "
                    "will not reproduce that inversion; the raw arbitrage report on "
                    "the same analysis names the quotes responsible.",
                    observed=[[tau, value] for tau, value in observed],
                )
            )

        calibration = calibrate_ssvi(
            observations,
            seed=request.seed,
            max_iterations=request.max_iterations,
            enforce_butterfly_bounds=request.enforce_butterfly_bounds,
        )

        slices: list[GlobalSurfaceSlice] = []
        if calibration.term_structure is not None:
            for index, tau in enumerate(calibration.term_structure.maturities):
                expiry, forward, discount, method, confidence = context[tau]
                slices.append(
                    GlobalSurfaceSlice(
                        expiry=expiry,
                        time_to_expiry=tau,
                        forward=forward,
                        discount_factor=discount,
                        theta=calibration.term_structure.thetas[index],
                        diagnostics=(
                            calibration.slices[index] if index < len(calibration.slices) else None
                        ),
                        forward_method=method,
                        forward_confidence=confidence,
                    )
                )

        surface = GlobalSurface(
            underlying_id=analysis.underlying_id,
            as_of=as_of,
            parameters=calibration.parameters,
            term_structure=calibration.term_structure,
            slices=tuple(slices),
            underlying_price=underlying_price,
            curve_id=analysis.curve_id,
            analysis_id=analysis.snapshot_id,
        )

        if calibration.status is CalibrationStatus.FAILED:
            warnings.append(
                AnalyticalWarning.error(
                    GlobalSurfaceWarningCode.NOT_CALIBRATED,
                    f"The global surface did not calibrate: {calibration.error}.",
                )
            )
        elif calibration.status is CalibrationStatus.DEGRADED:
            warnings.append(
                AnalyticalWarning.warn(
                    GlobalSurfaceWarningCode.DEGRADED,
                    f"The global surface fitted but is not admissible: "
                    f"{calibration.error}. Reference values from it are usable with "
                    "care and the condition it fails is reported alongside them.",
                )
            )

        if (calibration.rmse_vol_points or 0.0) > POOR_GLOBAL_FIT_VOL_POINTS:
            warnings.append(
                AnalyticalWarning.info(
                    GlobalSurfaceWarningCode.POOR_FIT,
                    f"The global surface fits to "
                    f"{calibration.rmse_vol_points:.2f} volatility points RMSE across "
                    "every expiry at once. Three parameters cannot bend to each smile "
                    "the way five per expiry can; compare the per-expiry SVI surface "
                    "on the same analysis before reading its wings.",
                    rmse_vol_points=calibration.rmse_vol_points,
                )
            )

        if calibration.parameters is not None and not calibration.butterfly_bounds_satisfied:
            warnings.append(
                AnalyticalWarning.info(
                    GlobalSurfaceWarningCode.BUTTERFLY_BOUNDS_NOT_MET,
                    "The surface fails the closed-form butterfly bounds of "
                    "Gatheral-Jacquier Theorem 4.2 while Durrleman's condition — the "
                    "one that actually decides whether the implied density is "
                    "non-negative — holds everywhere checked. The bounds are "
                    "sufficient, not necessary, so this is a surface the theorem "
                    "cannot certify rather than a surface with an arbitrage in it.",
                    max_butterfly_quantity=calibration.max_butterfly_quantity,
                    min_durrleman_g=calibration.min_durrleman_g,
                )
            )

        return GlobalSurfaceCalibrationResult(
            surface=surface, calibration=calibration, warnings=tuple(warnings)
        )


def _sorted(k: np.ndarray, w: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    order = np.argsort(k)
    return k[order], w[order]
