"""Linear interpolation with an explicit extrapolation policy.

``numpy.interp`` extrapolates flat by default and offers no alternative, which
is fine for a yield curve and wrong for a volatility surface wing. Making the
policy a named argument means a caller has to state which one they meant, and
the choice ends up in provenance rather than in an implicit library default.
"""

from __future__ import annotations

from enum import StrEnum

import numpy as np


class Extrapolation(StrEnum):
    #: Hold the end value. Correct for a zero curve: a 40-year rate quoted from
    #: a 30-year curve is a guess, and a flat guess is the least misleading one.
    FLAT = "FLAT"
    #: Continue the end slope. Reasonable for total variance in maturity.
    LINEAR = "LINEAR"
    #: Refuse. Correct wherever extrapolation would be presented as an
    #: observation, which is most places.
    ERROR = "ERROR"


def linear_interpolate(
    x: float | np.ndarray,
    xs: np.ndarray,
    ys: np.ndarray,
    extrapolation: Extrapolation = Extrapolation.FLAT,
) -> np.ndarray:
    """Interpolate ``(xs, ys)`` at ``x``. ``xs`` must be sorted ascending."""
    xs = np.asarray(xs, dtype=float)
    ys = np.asarray(ys, dtype=float)
    if xs.size == 0:
        raise ValueError("cannot interpolate an empty series")
    if xs.size != ys.size:
        raise ValueError(f"length mismatch: {xs.size} abscissae, {ys.size} ordinates")
    if np.any(np.diff(xs) <= 0):
        raise ValueError("abscissae must be strictly increasing")

    target = np.asarray(x, dtype=float)
    if xs.size == 1:
        if extrapolation is Extrapolation.ERROR and np.any(target != xs[0]):
            raise ValueError("single-point series cannot be extrapolated")
        return np.full(target.shape, ys[0])

    outside = (target < xs[0]) | (target > xs[-1])
    if extrapolation is Extrapolation.ERROR and np.any(outside):
        raise ValueError(
            f"value outside the interpolation range [{xs[0]}, {xs[-1]}] and "
            "extrapolation is not permitted"
        )

    result = np.interp(target, xs, ys)  # flat outside by construction
    if extrapolation is Extrapolation.LINEAR and np.any(outside):
        left_slope = (ys[1] - ys[0]) / (xs[1] - xs[0])
        right_slope = (ys[-1] - ys[-2]) / (xs[-1] - xs[-2])
        result = np.where(target < xs[0], ys[0] + left_slope * (target - xs[0]), result)
        result = np.where(target > xs[-1], ys[-1] + right_slope * (target - xs[-1]), result)
    return np.asarray(result, dtype=float)
