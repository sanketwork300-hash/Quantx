"""The fitted volatility surface.

A surface is a *model output*, and everything about this module is arranged so
that it can never be confused with an observation. Its values are called
``reference_iv`` and ``reference_price``, never fair value; they live in their
own tables; and every lookup carries flags saying whether the answer came from
a fitted slice, an interpolation between two, or an extrapolation past the data.

**Reference values are a pure function of the persisted parameters.** Given
``(a, b, rho, m, sigma)``, the forward, the discount factor and the day count,
a stored surface reproduces its reference IVs exactly, with no re-fitting on
read. That is what makes an old analysis reproducible rather than merely
repeatable, and a test asserts it bit-for-bit.
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

import numpy as np

from domains.instruments.enums import OptionType
from quant.pricing.black76 import black76_price
from quant.volatility.svi import SVIParameters, raw_svi_total_variance
from quant.volatility.svi_calibration import CalibrationStatus, SVICalibrationResult

SURFACE_MODEL = "RAW_SVI"
SURFACE_MODEL_VERSION = "svi-raw@1.0.0"


class ReferenceMethod(StrEnum):
    #: The requested expiry is a fitted slice.
    EXACT_SLICE = "EXACT_SLICE"
    #: Total variance interpolated linearly in maturity at fixed log-moneyness.
    INTERPOLATED_MATURITY = "INTERPOLATED_MATURITY"
    #: Outside the fitted maturity range.
    EXTRAPOLATED_MATURITY = "EXTRAPOLATED_MATURITY"
    UNAVAILABLE = "UNAVAILABLE"


class ReferenceFlag(StrEnum):
    #: The strike lies outside the log-moneyness range that was actually fitted.
    EXTRAPOLATED_STRIKE = "EXTRAPOLATED_STRIKE"
    EXTRAPOLATED_MATURITY = "EXTRAPOLATED_MATURITY"
    #: The slice fitted, but with a butterfly violation or an unconverged
    #: optimizer. The number is usable with care and says so.
    SLICE_DEGRADED = "SLICE_DEGRADED"
    #: The forward for this slice was itself a low-confidence estimate.
    LOW_CONFIDENCE_FORWARD = "LOW_CONFIDENCE_FORWARD"


@dataclass(frozen=True, slots=True)
class SurfaceSliceFit:
    """One expiry's fitted slice, plus the market context it was fitted in."""

    expiry: date
    time_to_expiry: float
    forward: float
    discount_factor: float
    parameters: SVIParameters | None
    calibration: SVICalibrationResult
    #: Log-moneyness range of the quotes that were actually used. A lookup
    #: outside it is extrapolation and is flagged as such.
    k_min: float | None = None
    k_max: float | None = None
    forward_method: str | None = None
    forward_confidence: float = 0.0

    @property
    def usable(self) -> bool:
        return self.parameters is not None and self.time_to_expiry > 0

    @property
    def degraded(self) -> bool:
        return self.calibration.status is CalibrationStatus.DEGRADED

    def total_variance(self, k: float | np.ndarray) -> np.ndarray:
        if self.parameters is None:
            raise ValueError(f"slice {self.expiry} has no fitted parameters")
        return raw_svi_total_variance(k, self.parameters)

    def implied_vol(self, k: float | np.ndarray) -> np.ndarray:
        w = np.maximum(self.total_variance(k), 0.0)
        return np.sqrt(w / self.time_to_expiry)

    def to_dict(self) -> dict:
        return {
            "expiry": self.expiry.isoformat(),
            "time_to_expiry": self.time_to_expiry,
            "forward": self.forward,
            "discount_factor": self.discount_factor,
            "forward_method": self.forward_method,
            "forward_confidence": self.forward_confidence,
            "k_min": self.k_min,
            "k_max": self.k_max,
            "parameters": (self.parameters.to_dict() if self.parameters is not None else None),
            "calibration": self.calibration.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class ReferencePoint:
    """A model value, with the provenance of *how* it was produced."""

    strike: Decimal
    expiry: date
    option_type: OptionType | None
    method: ReferenceMethod
    time_to_expiry: float | None = None
    forward: float | None = None
    discount_factor: float | None = None
    log_moneyness: float | None = None
    reference_iv: float | None = None
    total_variance: float | None = None
    reference_price: float | None = None
    calibration_rmse_vol_points: float | None = None
    flags: tuple[ReferenceFlag, ...] = field(default=())
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.reference_iv is not None

    def to_dict(self) -> dict:
        return {
            "strike": format(self.strike, "f"),
            "expiry": self.expiry.isoformat(),
            "option_type": str(self.option_type) if self.option_type else None,
            "method": str(self.method),
            "time_to_expiry": self.time_to_expiry,
            "forward": self.forward,
            "discount_factor": self.discount_factor,
            "log_moneyness": self.log_moneyness,
            "reference_iv": self.reference_iv,
            "total_variance": self.total_variance,
            "reference_price": self.reference_price,
            "calibration_rmse_vol_points": self.calibration_rmse_vol_points,
            "flags": [str(flag) for flag in self.flags],
            "error": self.error,
        }


@dataclass(frozen=True, slots=True)
class VolatilitySurface:
    """A calibrated surface, addressed by a content-derived id."""

    underlying_id: uuid.UUID
    as_of: datetime
    slices: tuple[SurfaceSliceFit, ...]
    curve_id: str | None = None
    analysis_id: uuid.UUID | None = None
    model: str = SURFACE_MODEL
    model_version: str = SURFACE_MODEL_VERSION

    @property
    def fitted_slices(self) -> tuple[SurfaceSliceFit, ...]:
        return tuple(slice_ for slice_ in self.slices if slice_.usable)

    @property
    def surface_id(self) -> str:
        """Two surfaces with this id were fitted from the same numbers."""
        payload = {
            "underlying_id": str(self.underlying_id),
            "as_of": self.as_of.isoformat(),
            "model": self.model,
            "model_version": self.model_version,
            "curve_id": self.curve_id,
            "slices": [
                {
                    "expiry": slice_.expiry.isoformat(),
                    "tau": slice_.time_to_expiry,
                    "forward": slice_.forward,
                    "discount_factor": slice_.discount_factor,
                    "parameters": (slice_.parameters.to_dict() if slice_.parameters else None),
                }
                for slice_ in self.slices
            ],
        }
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode())
        return f"surface:{digest.hexdigest()[:16]}"

    # ------------------------------------------------------------- lookups
    def slice_for(self, expiry: date) -> SurfaceSliceFit | None:
        for slice_ in self.slices:
            if slice_.expiry == expiry:
                return slice_
        return None

    def reference(
        self,
        strike: Decimal,
        expiry: date,
        option_type: OptionType | None = None,
    ) -> ReferencePoint:
        """Reference implied volatility, and price when an option type is given."""
        fitted = self.fitted_slices
        if not fitted:
            return ReferencePoint(
                strike=strike,
                expiry=expiry,
                option_type=option_type,
                method=ReferenceMethod.UNAVAILABLE,
                error="the surface has no usable slices",
            )

        exact = self.slice_for(expiry)
        if exact is not None and exact.usable:
            return self._from_slice(exact, strike, expiry, option_type)
        return self._interpolate(fitted, strike, expiry, option_type)

    def _from_slice(
        self,
        slice_: SurfaceSliceFit,
        strike: Decimal,
        expiry: date,
        option_type: OptionType | None,
    ) -> ReferencePoint:
        k = math.log(float(strike) / slice_.forward)
        w = float(slice_.total_variance(k))
        iv = math.sqrt(max(w, 0.0) / slice_.time_to_expiry)

        flags: list[ReferenceFlag] = []
        outside_fitted_strikes = (
            slice_.k_min is not None
            and slice_.k_max is not None
            and not (slice_.k_min <= k <= slice_.k_max)
        )
        if outside_fitted_strikes:
            flags.append(ReferenceFlag.EXTRAPOLATED_STRIKE)
        if slice_.degraded:
            flags.append(ReferenceFlag.SLICE_DEGRADED)
        if slice_.forward_confidence < 0.5:
            flags.append(ReferenceFlag.LOW_CONFIDENCE_FORWARD)

        price = None
        if option_type is not None:
            price = float(
                black76_price(
                    slice_.forward,
                    float(strike),
                    slice_.time_to_expiry,
                    iv,
                    option_type is OptionType.CALL,
                    slice_.discount_factor,
                )
            )

        return ReferencePoint(
            strike=strike,
            expiry=expiry,
            option_type=option_type,
            method=ReferenceMethod.EXACT_SLICE,
            time_to_expiry=slice_.time_to_expiry,
            forward=slice_.forward,
            discount_factor=slice_.discount_factor,
            log_moneyness=k,
            reference_iv=iv,
            total_variance=w,
            reference_price=price,
            calibration_rmse_vol_points=slice_.calibration.rmse_vol_points,
            flags=tuple(flags),
        )

    def _interpolate(
        self,
        fitted: tuple[SurfaceSliceFit, ...],
        strike: Decimal,
        expiry: date,
        option_type: OptionType | None,
    ) -> ReferencePoint:
        """Linear in **total variance** across maturity, at fixed log-moneyness.

        Interpolating variance rather than volatility is the consistent choice:
        variance is what is additive in time, and it is the coordinate the
        calendar no-arbitrage condition is written in, so a linear interpolation
        between two calendar-consistent slices stays calendar-consistent.

        Beyond the fitted range the nearest slice's total variance is scaled by
        the maturity ratio — the flat-forward-variance assumption — and the
        result is flagged as extrapolation. It is never silently presented as a
        fitted value.
        """
        ordered = sorted(fitted, key=lambda s: s.expiry)
        target_day = expiry.toordinal()
        days = [slice_.expiry.toordinal() for slice_ in ordered]

        # Map calendar days onto the slices' own time-to-expiry scale so the
        # interpolation stays in the day count the slices were fitted under.
        def tau_for(target: int) -> float:
            if len(ordered) == 1:
                only = ordered[0]
                scale = only.time_to_expiry / max(only.expiry.toordinal() - _origin(only), 1)
                return max(scale * (target - _origin(only)), 1e-9)
            first, last = ordered[0], ordered[-1]
            slope = (last.time_to_expiry - first.time_to_expiry) / max(days[-1] - days[0], 1)
            return max(first.time_to_expiry + slope * (target - days[0]), 1e-9)

        def _origin(slice_: SurfaceSliceFit) -> int:
            # The as-of date, recovered from the slice's own maturity.
            return slice_.expiry.toordinal() - max(round(slice_.time_to_expiry * 365), 1)

        tau = tau_for(target_day)
        flags: list[ReferenceFlag] = []

        if target_day < days[0] or target_day > days[-1]:
            anchor = ordered[0] if target_day < days[0] else ordered[-1]
            forward = anchor.forward
            k = math.log(float(strike) / forward)
            w_anchor = float(anchor.total_variance(k))
            w = w_anchor * (tau / anchor.time_to_expiry)
            discount = anchor.discount_factor
            method = ReferenceMethod.EXTRAPOLATED_MATURITY
            flags.append(ReferenceFlag.EXTRAPOLATED_MATURITY)
            rmse = anchor.calibration.rmse_vol_points
            reference_slice = anchor
        else:
            index = int(np.searchsorted(days, target_day))
            left, right = ordered[max(index - 1, 0)], ordered[min(index, len(ordered) - 1)]
            if left is right:
                return self._from_slice(left, strike, left.expiry, option_type)

            span = right.time_to_expiry - left.time_to_expiry
            weight = 0.0 if span <= 0 else (tau - left.time_to_expiry) / span
            weight = float(np.clip(weight, 0.0, 1.0))

            forward = left.forward + weight * (right.forward - left.forward)
            k = math.log(float(strike) / forward)
            w = float(left.total_variance(k)) + weight * (
                float(right.total_variance(k)) - float(left.total_variance(k))
            )
            discount = left.discount_factor + weight * (
                right.discount_factor - left.discount_factor
            )
            method = ReferenceMethod.INTERPOLATED_MATURITY
            rmse = max(
                left.calibration.rmse_vol_points or 0.0,
                right.calibration.rmse_vol_points or 0.0,
            )
            reference_slice = right if weight > 0.5 else left

        outside_fitted_strikes = (
            reference_slice.k_min is not None
            and reference_slice.k_max is not None
            and not (reference_slice.k_min <= k <= reference_slice.k_max)
        )
        if outside_fitted_strikes:
            flags.append(ReferenceFlag.EXTRAPOLATED_STRIKE)
        if reference_slice.degraded:
            flags.append(ReferenceFlag.SLICE_DEGRADED)

        iv = math.sqrt(max(w, 0.0) / tau)
        price = None
        if option_type is not None:
            price = float(
                black76_price(
                    forward,
                    float(strike),
                    tau,
                    iv,
                    option_type is OptionType.CALL,
                    discount,
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
            calibration_rmse_vol_points=rmse,
            flags=tuple(flags),
        )

    def to_dict(self, include_slices: bool = True) -> dict:
        payload = {
            "surface_id": self.surface_id,
            "underlying_id": str(self.underlying_id),
            "as_of_timestamp": self.as_of.isoformat(),
            "model": self.model,
            "model_version": self.model_version,
            "curve_id": self.curve_id,
            "analysis_id": str(self.analysis_id) if self.analysis_id else None,
            "counts": {
                "slices": len(self.slices),
                "fitted": len(self.fitted_slices),
            },
        }
        if include_slices:
            payload["slices"] = [slice_.to_dict() for slice_ in self.slices]
        return payload
