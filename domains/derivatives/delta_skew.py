"""Delta-quoted skew across a calibrated surface.

:mod:`domains.derivatives.characteristics` already records the mathematical
shape of each slice — level, ``dsigma/dk``, curvature — at standard tenors.
This module records the same shape in the units a market quotes it in: the
25-delta and 10-delta risk reversal and butterfly.

The two are not redundant. ``dsigma/dk`` is a derivative at a point and is the
right coordinate for calendar and butterfly arbitrage; a risk reversal is a
difference between two traded strikes and is the number anyone comparing this
surface with a broker's runs will be holding. Keeping both means neither has to
be converted into the other by hand, which is where the convention mistakes
happen.

Nothing here re-fits anything. It reads a surface that has already been
calibrated and stored, so a skew number reproduces exactly from the surface it
came from.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

import numpy as np

from domains.derivatives.surface import SurfaceSliceFit, VolatilitySurface
from quant.volatility.delta_skew import (
    DeltaConvention,
    DeltaSmile,
    delta_smile,
)

#: Delta levels recorded for every surface. 25 is the market's standard skew
#: quote; 10 says how the far wing behaves, which is where a smile that has been
#: fitted rather than observed is most likely to be wrong.
DELTA_LEVELS: tuple[float, ...] = (0.25, 0.10)

DELTA_SKEW_MODEL_VERSION = "delta-skew@1.0.0"


@dataclass(frozen=True, slots=True)
class SliceDeltaSkew:
    """One expiry's delta-quoted smile."""

    expiry: date
    time_to_expiry: float
    forward: float
    atm_volatility: float
    smiles: tuple[DeltaSmile, ...]
    #: True when the slice's fit was degraded. The numbers are still computed —
    #: a degraded fit is not a missing one — but a caller must be able to see
    #: that the smile they are reading was not a clean fit.
    degraded: bool = False

    def at(self, level: float) -> DeltaSmile | None:
        return next((smile for smile in self.smiles if smile.delta_level == level), None)

    def to_dict(self) -> dict:
        return {
            "expiry": self.expiry.isoformat(),
            "time_to_expiry": self.time_to_expiry,
            "forward": self.forward,
            "atm_volatility": self.atm_volatility,
            "degraded": self.degraded,
            "smiles": [smile.to_dict() for smile in self.smiles],
        }


@dataclass(frozen=True, slots=True)
class UnmeasuredSlice:
    """An expiry whose delta skew could not be computed, and why."""

    expiry: date
    reason: str

    def to_dict(self) -> dict:
        return {"expiry": self.expiry.isoformat(), "reason": self.reason}


@dataclass(frozen=True, slots=True)
class SurfaceDeltaSkew:
    """Delta-quoted skew for every slice of one surface."""

    surface_id: str
    as_of: datetime
    levels: tuple[float, ...]
    slices: tuple[SliceDeltaSkew, ...]
    #: Expiries that produced no measurement. Listed rather than omitted: a
    #: term structure with a silent hole in it reads as a smooth curve.
    unmeasured: tuple[UnmeasuredSlice, ...] = ()
    convention: DeltaConvention = DeltaConvention.FORWARD
    model_version: str = DELTA_SKEW_MODEL_VERSION

    def term_structure(self, level: float) -> tuple[tuple[float, float | None, float | None], ...]:
        """``(time_to_expiry, risk_reversal, butterfly)`` per expiry.

        The two measurements are ``None`` where the strikes could not be found,
        so a caller plotting this cannot join across a gap without noticing.
        """
        rows: list[tuple[float, float | None, float | None]] = []
        for item in self.slices:
            smile = item.at(level)
            if smile is None:
                continue
            rows.append((item.time_to_expiry, smile.risk_reversal, smile.butterfly))
        return tuple(sorted(rows, key=lambda row: row[0]))

    def to_dict(self) -> dict:
        return {
            "surface_id": self.surface_id,
            "as_of": self.as_of.isoformat(),
            "delta_convention": str(self.convention),
            "model_version": self.model_version,
            "levels": list(self.levels),
            "slices": [item.to_dict() for item in self.slices],
            "unmeasured": [item.to_dict() for item in self.unmeasured],
        }


def slice_delta_skew(
    fit: SurfaceSliceFit, levels: tuple[float, ...] = DELTA_LEVELS
) -> SliceDeltaSkew:
    """Delta-quoted smile for one fitted slice."""

    def volatility_at(k: float) -> float:
        return float(np.atleast_1d(fit.implied_vol(k))[0])

    fitted_range = (fit.k_min, fit.k_max)
    return SliceDeltaSkew(
        expiry=fit.expiry,
        time_to_expiry=fit.time_to_expiry,
        forward=fit.forward,
        atm_volatility=volatility_at(0.0),
        smiles=tuple(
            delta_smile(
                level,
                volatility_at,
                forward=fit.forward,
                time_to_expiry=fit.time_to_expiry,
                fitted_range=fitted_range,
            )
            for level in levels
        ),
        degraded=fit.degraded,
    )


def surface_delta_skew(
    surface: VolatilitySurface, levels: tuple[float, ...] = DELTA_LEVELS
) -> SurfaceDeltaSkew:
    """Delta-quoted skew for every usable slice of a surface."""
    measured: list[SliceDeltaSkew] = []
    unmeasured: list[UnmeasuredSlice] = []

    for fit in surface.slices:
        if not fit.usable:
            unmeasured.append(
                UnmeasuredSlice(
                    expiry=fit.expiry,
                    reason=(
                        "the slice has no fitted parameters"
                        if fit.parameters is None
                        else "the slice has expired"
                    ),
                )
            )
            continue
        measured.append(slice_delta_skew(fit, levels))

    return SurfaceDeltaSkew(
        surface_id=surface.surface_id,
        as_of=surface.as_of,
        levels=levels,
        slices=tuple(measured),
        unmeasured=tuple(unmeasured),
    )
