"""Numerical tolerances and guarded arithmetic.

Floating-point comparisons in this codebase go through :func:`is_close` with an
explicit tolerance rather than ``==``. Division that can meet a zero
denominator goes through :func:`safe_divide`, which returns a sentinel instead
of producing ``inf`` or ``nan`` and letting it propagate silently into a risk
number.
"""

from __future__ import annotations

import math

DEFAULT_ABS_TOL = 1e-12
DEFAULT_REL_TOL = 1e-9

#: Below this, a denominator is treated as zero rather than as a small number.
ZERO_DENOMINATOR = 1e-15


def is_close(
    a: float, b: float, *, rel_tol: float = DEFAULT_REL_TOL, abs_tol: float = DEFAULT_ABS_TOL
) -> bool:
    return math.isclose(a, b, rel_tol=rel_tol, abs_tol=abs_tol)


def safe_divide(numerator: float, denominator: float, default: float | None = None) -> float | None:
    """Divide, returning ``default`` when the denominator is effectively zero."""
    if abs(denominator) < ZERO_DENOMINATOR:
        return default
    result = numerator / denominator
    if not math.isfinite(result):
        return default
    return result


def clamp(value: float, low: float, high: float) -> float:
    if low > high:
        raise ValueError(f"clamp bounds inverted: [{low}, {high}]")
    return max(low, min(high, value))
