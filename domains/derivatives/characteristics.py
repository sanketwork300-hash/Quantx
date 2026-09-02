"""Surface characteristics: the numbers that make surfaces comparable over time.

A surface's expiries move every day, so a time series of "the 2026-10-29 slice"
runs out after one expiry. Characteristics are therefore recorded at **standard
tenors** — 7, 30, 60, 90, 180 and 365 days — which is what makes "today's
30-day at-the-money level is at the 82nd percentile of the last 60 observations"
a sentence that means something.

Level, skew and curvature come from the fitted parameters analytically, so a
stored characteristic reproduces exactly from the surface it came from, like
every other model output on the platform.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum

import numpy as np

from domains.derivatives.surface import (
    ReferenceFlag,
    ReferenceMethod,
    SurfaceSliceFit,
    VolatilitySurface,
)
from quant.volatility.svi import raw_svi_vol_derivatives

#: Tenors at which a surface's shape is recorded for history. Chosen to span the
#: listed maturities a retail chain actually carries; a tenor with no nearby
#: slice is recorded as extrapolation, not silently omitted.
STANDARD_TENORS: tuple[int, ...] = (7, 30, 60, 90, 180, 365)

CHARACTERISTICS_MODEL_VERSION = "surface-characteristics@1.0.0"

DAYS_PER_YEAR = 365.0


class CharacteristicKind(StrEnum):
    #: Taken directly from a fitted slice; exact.
    SLICE = "SLICE"
    #: Interpolated or extrapolated onto a standard tenor for comparability.
    STANDARD_TENOR = "STANDARD_TENOR"


@dataclass(frozen=True, slots=True)
class SurfaceCharacteristics:
    """The shape of one slice, or of the surface at one standard tenor."""

    kind: CharacteristicKind
    time_to_expiry: float
    forward: float
    #: Volatility at ``k = 0``.
    atm_volatility: float
    #: ``d sigma / dk`` at the money. Negative for the usual equity-index shape.
    skew: float
    #: ``d2 sigma / dk2`` at the money.
    curvature: float
    #: ``sigma^2 tau`` at the money; the coordinate calendar consistency lives in.
    atm_total_variance: float
    expiry: date | None = None
    tenor_days: int | None = None
    method: ReferenceMethod = ReferenceMethod.EXACT_SLICE
    flags: tuple[ReferenceFlag, ...] = field(default=())

    def to_dict(self) -> dict:
        return {
            "kind": str(self.kind),
            "expiry": self.expiry.isoformat() if self.expiry else None,
            "tenor_days": self.tenor_days,
            "time_to_expiry": self.time_to_expiry,
            "forward": self.forward,
            "atm_volatility": self.atm_volatility,
            "skew": self.skew,
            "curvature": self.curvature,
            "atm_total_variance": self.atm_total_variance,
            "method": str(self.method),
            "flags": [str(flag) for flag in self.flags],
        }


def slice_characteristics(slice_: SurfaceSliceFit) -> SurfaceCharacteristics | None:
    """Exact characteristics of a fitted slice, from its five parameters."""
    if slice_.parameters is None or slice_.time_to_expiry <= 0:
        return None

    sigma, dsigma, d2sigma = raw_svi_vol_derivatives(0.0, slice_.time_to_expiry, slice_.parameters)
    flags: list[ReferenceFlag] = []
    if slice_.degraded:
        flags.append(ReferenceFlag.SLICE_DEGRADED)
    if slice_.forward_confidence < 0.5:
        flags.append(ReferenceFlag.LOW_CONFIDENCE_FORWARD)
    # k = 0 is inside every reasonable chain, but say so if it is not.
    atm_outside_fit = (
        slice_.k_min is not None
        and slice_.k_max is not None
        and not (slice_.k_min <= 0.0 <= slice_.k_max)
    )
    if atm_outside_fit:
        flags.append(ReferenceFlag.EXTRAPOLATED_STRIKE)

    return SurfaceCharacteristics(
        kind=CharacteristicKind.SLICE,
        expiry=slice_.expiry,
        time_to_expiry=slice_.time_to_expiry,
        forward=slice_.forward,
        atm_volatility=float(sigma),
        skew=float(dsigma),
        curvature=float(d2sigma),
        atm_total_variance=float(sigma) ** 2 * slice_.time_to_expiry,
        method=ReferenceMethod.EXACT_SLICE,
        flags=tuple(flags),
    )


def characteristics_at_tenor(
    surface: VolatilitySurface, tenor_days: int
) -> SurfaceCharacteristics | None:
    """The surface's shape at a standard tenor.

    At-the-money **total variance** is interpolated linearly in maturity, then
    converted back to a volatility — variance is what is additive in time, and
    interpolating it keeps a calendar-consistent pair calendar-consistent.

    Skew and curvature are interpolated in their own units on the same weight.
    That is an interpolation of derived quantities rather than of the model, and
    it is recorded as such: a tenor that lands on a fitted expiry is
    ``EXACT_SLICE``, one between two is ``INTERPOLATED_MATURITY``, and one
    outside the fitted range is ``EXTRAPOLATED_MATURITY`` and flagged.
    """
    fitted = [
        characteristic
        for characteristic in (slice_characteristics(s) for s in surface.fitted_slices)
        if characteristic is not None
    ]
    if not fitted:
        return None

    ordered = sorted(fitted, key=lambda c: c.time_to_expiry)
    target_tau = tenor_days / DAYS_PER_YEAR
    taus = np.array([c.time_to_expiry for c in ordered])

    if target_tau <= taus[0] or target_tau >= taus[-1] or len(ordered) == 1:
        anchor = ordered[0] if target_tau <= taus[0] else ordered[-1]
        if abs(anchor.time_to_expiry - target_tau) < 1e-9:
            return _relabel(anchor, tenor_days, ReferenceMethod.EXACT_SLICE, ())
        # Flat forward variance: the only assumption that adds no shape.
        total_variance = anchor.atm_total_variance * (target_tau / anchor.time_to_expiry)
        return SurfaceCharacteristics(
            kind=CharacteristicKind.STANDARD_TENOR,
            tenor_days=tenor_days,
            time_to_expiry=target_tau,
            forward=anchor.forward,
            atm_volatility=float(np.sqrt(max(total_variance, 0.0) / target_tau)),
            skew=anchor.skew,
            curvature=anchor.curvature,
            atm_total_variance=float(total_variance),
            method=ReferenceMethod.EXTRAPOLATED_MATURITY,
            flags=(ReferenceFlag.EXTRAPOLATED_MATURITY, *anchor.flags),
        )

    index = int(np.searchsorted(taus, target_tau))
    left, right = ordered[index - 1], ordered[index]
    if abs(left.time_to_expiry - target_tau) < 1e-9:
        return _relabel(left, tenor_days, ReferenceMethod.EXACT_SLICE, ())
    if abs(right.time_to_expiry - target_tau) < 1e-9:
        return _relabel(right, tenor_days, ReferenceMethod.EXACT_SLICE, ())

    span = right.time_to_expiry - left.time_to_expiry
    weight = 0.0 if span <= 0 else (target_tau - left.time_to_expiry) / span
    total_variance = left.atm_total_variance + weight * (
        right.atm_total_variance - left.atm_total_variance
    )
    return SurfaceCharacteristics(
        kind=CharacteristicKind.STANDARD_TENOR,
        tenor_days=tenor_days,
        time_to_expiry=target_tau,
        forward=left.forward + weight * (right.forward - left.forward),
        atm_volatility=float(np.sqrt(max(total_variance, 0.0) / target_tau)),
        skew=left.skew + weight * (right.skew - left.skew),
        curvature=left.curvature + weight * (right.curvature - left.curvature),
        atm_total_variance=float(total_variance),
        method=ReferenceMethod.INTERPOLATED_MATURITY,
        flags=tuple({*left.flags, *right.flags}),
    )


def _relabel(
    source: SurfaceCharacteristics,
    tenor_days: int,
    method: ReferenceMethod,
    extra_flags: tuple[ReferenceFlag, ...],
) -> SurfaceCharacteristics:
    return SurfaceCharacteristics(
        kind=CharacteristicKind.STANDARD_TENOR,
        tenor_days=tenor_days,
        expiry=source.expiry,
        time_to_expiry=source.time_to_expiry,
        forward=source.forward,
        atm_volatility=source.atm_volatility,
        skew=source.skew,
        curvature=source.curvature,
        atm_total_variance=source.atm_total_variance,
        method=method,
        flags=tuple({*source.flags, *extra_flags}),
    )


def surface_term_structure(
    surface: VolatilitySurface, tenors: tuple[int, ...] = STANDARD_TENORS
) -> list[SurfaceCharacteristics]:
    """Characteristics at every standard tenor, for storage and history."""
    result = []
    for tenor in tenors:
        characteristic = characteristics_at_tenor(surface, tenor)
        if characteristic is not None:
            result.append(characteristic)
    return result
