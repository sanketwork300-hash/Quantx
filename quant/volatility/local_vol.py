"""Dupire local volatility, in total-variance form, from a smooth surface.

The raw price-derivative form of Dupire divides second differences of option
prices by each other, which on real quotes amplifies noise into a local
volatility surface that is mostly artefact. Gatheral's total-variance form

    sigma_loc^2 = (dw/dT) / D,
    D = 1 - (k/w)(dw/dk) + (1/4)(-1/4 - 1/w + k^2/w^2)(dw/dk)^2 + (1/2) d2w/dk2

is far better behaved, and it takes its derivatives from a **fitted** surface
where they are analytic rather than from quotes where they are differences.

Two rules govern what comes out. A denominator near zero produces `INVALID`, not
a clipped value: the formula has genuinely said nothing there, and a plausible
number in its place is worse than a gap. And a negative numerator or a negative
result is reported as such rather than square-rooted into a NaN or floored at
some small positive volatility — both would hide residual arbitrage in the
surface, which is precisely what this diagnostic is for.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import numpy as np

from quant.volatility.ssvi import SSVISurface

#: A denominator smaller than this is treated as zero. Dupire's denominator is
#: dimensionless and O(1) on an admissible surface, so this is genuinely small.
MIN_DENOMINATOR = 1e-8

#: Local variance below this is not a volatility, it is a rounding artefact.
MIN_LOCAL_VARIANCE = 1e-12

#: Above this the result is reported but flagged: a local volatility of 500% is
#: arithmetic, not a market.
IMPLAUSIBLE_LOCAL_VOL = 5.0


class LocalVolFlag(StrEnum):
    DENOMINATOR_NEAR_ZERO = "LOCAL_VOL_DENOMINATOR_NEAR_ZERO"
    NEGATIVE_VARIANCE = "LOCAL_VOL_NEGATIVE_VARIANCE"
    NEGATIVE_TIME_DERIVATIVE = "LOCAL_VOL_NEGATIVE_TIME_DERIVATIVE"
    EXTRAPOLATED = "LOCAL_VOL_EXTRAPOLATED_REGION"
    IMPLAUSIBLE_MAGNITUDE = "LOCAL_VOL_IMPLAUSIBLE_MAGNITUDE"
    DEGENERATE_TOTAL_VARIANCE = "LOCAL_VOL_DEGENERATE_TOTAL_VARIANCE"


@dataclass(frozen=True, slots=True)
class LocalVolPoint:
    """One point of the local volatility surface, or a stated absence of one."""

    log_moneyness: float
    maturity: float
    #: ``None`` when the formula did not produce a usable number here.
    local_volatility: float | None
    total_variance: float
    numerator: float
    denominator: float
    confidence: float
    flags: tuple[str, ...] = ()

    @property
    def is_valid(self) -> bool:
        return self.local_volatility is not None

    def to_dict(self) -> dict:
        return {
            "log_moneyness": self.log_moneyness,
            "maturity": self.maturity,
            "local_volatility": self.local_volatility,
            "is_valid": self.is_valid,
            "total_variance": self.total_variance,
            "numerator": self.numerator,
            "denominator": self.denominator,
            "confidence": self.confidence,
            "flags": list(self.flags),
        }


@dataclass(frozen=True, slots=True)
class LocalVolSurface:
    """A grid of local volatilities, with the invalid regions kept as holes."""

    points: tuple[LocalVolPoint, ...]
    log_moneyness: tuple[float, ...]
    maturities: tuple[float, ...]

    @property
    def valid(self) -> tuple[LocalVolPoint, ...]:
        return tuple(point for point in self.points if point.is_valid)

    @property
    def coverage(self) -> float:
        return len(self.valid) / len(self.points) if self.points else 0.0

    def grid(self) -> np.ndarray:
        """``(maturities, log_moneyness)`` with ``nan`` where nothing is valid.

        ``nan`` rather than a filled value, so a plotting layer that ignores it
        leaves a hole rather than drawing a line through a region where the
        formula said nothing.
        """
        lookup = {(p.maturity, p.log_moneyness): p for p in self.points}
        return np.array(
            [
                [
                    (lookup[(t, k)].local_volatility if lookup[(t, k)].is_valid else np.nan)
                    for k in self.log_moneyness
                ]
                for t in self.maturities
            ],
            dtype=float,
        )

    def flag_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for point in self.points:
            for flag in point.flags:
                counts[flag] = counts.get(flag, 0) + 1
        return dict(sorted(counts.items()))

    def to_dict(self, include_points: bool = False) -> dict:
        payload = {
            "log_moneyness": list(self.log_moneyness),
            "maturities": list(self.maturities),
            "points": len(self.points),
            "valid_points": len(self.valid),
            "coverage": self.coverage,
            "flag_counts": self.flag_counts(),
            "policy": (
                "Derivatives are taken on the fitted arbitrage-aware surface, "
                "never on raw quotes. Where Dupire's denominator approaches zero "
                "the point is reported invalid rather than clipped: the formula "
                "has said nothing there, and a plausible number in its place "
                "would be worse than a gap."
            ),
        }
        if include_points:
            payload["point_detail"] = [point.to_dict() for point in self.points]
        return payload


def dupire_denominator(
    k: np.ndarray, w: np.ndarray, dw_dk: np.ndarray, d2w_dk2: np.ndarray
) -> np.ndarray:
    """Gatheral's denominator. Exactly 1 for a flat surface, by construction."""
    w = np.maximum(w, MIN_LOCAL_VARIANCE)
    return (
        1.0
        - (k / w) * dw_dk
        + 0.25 * (-0.25 - 1.0 / w + (k * k) / (w * w)) * dw_dk * dw_dk
        + 0.5 * d2w_dk2
    )


def local_volatility_point(surface: SSVISurface, k: float, maturity: float) -> LocalVolPoint:
    """One point, with every reason it might not exist reported."""
    flags: list[str] = []
    if surface.term_structure.is_extrapolated(maturity):
        flags.append(LocalVolFlag.EXTRAPOLATED)

    w, dw_dk, d2w_dk2, dw_dt = surface.derivatives(k, maturity)
    w = float(w)
    numerator = float(dw_dt)
    denominator = float(dupire_denominator(np.array(k), np.array(w), dw_dk, d2w_dk2))

    if w <= MIN_LOCAL_VARIANCE:
        flags.append(LocalVolFlag.DEGENERATE_TOTAL_VARIANCE)
        return LocalVolPoint(k, maturity, None, w, numerator, denominator, 0.0, tuple(flags))

    if abs(denominator) < MIN_DENOMINATOR:
        flags.append(LocalVolFlag.DENOMINATOR_NEAR_ZERO)
        return LocalVolPoint(k, maturity, None, w, numerator, denominator, 0.0, tuple(flags))

    if numerator < 0.0:
        # Total variance falling with maturity is calendar arbitrage in the
        # surface, and no local volatility exists there.
        flags.append(LocalVolFlag.NEGATIVE_TIME_DERIVATIVE)
        return LocalVolPoint(k, maturity, None, w, numerator, denominator, 0.0, tuple(flags))

    variance = numerator / denominator
    if variance < MIN_LOCAL_VARIANCE:
        flags.append(LocalVolFlag.NEGATIVE_VARIANCE)
        return LocalVolPoint(k, maturity, None, w, numerator, denominator, 0.0, tuple(flags))

    volatility = float(np.sqrt(variance))
    if volatility > IMPLAUSIBLE_LOCAL_VOL:
        flags.append(LocalVolFlag.IMPLAUSIBLE_MAGNITUDE)

    return LocalVolPoint(
        log_moneyness=k,
        maturity=maturity,
        local_volatility=volatility,
        total_variance=w,
        numerator=numerator,
        denominator=denominator,
        confidence=_confidence(denominator, flags),
        flags=tuple(flags),
    )


def local_volatility_surface(
    surface: SSVISurface,
    log_moneyness: np.ndarray | list[float],
    maturities: np.ndarray | list[float],
) -> LocalVolSurface:
    grid_k = [float(value) for value in np.asarray(log_moneyness, dtype=float)]
    grid_t = [float(value) for value in np.asarray(maturities, dtype=float)]
    points = tuple(local_volatility_point(surface, k, t) for t in grid_t for k in grid_k)
    return LocalVolSurface(points=points, log_moneyness=tuple(grid_k), maturities=tuple(grid_t))


def _confidence(denominator: float, flags: list[str]) -> float:
    """How far the denominator is from the zero that would invalidate it.

    A denominator of one is the flat-surface case and earns full marks; as it
    approaches zero the point is increasingly sensitive to the fit and the score
    falls with it. Extrapolation and implausible magnitude each halve it.
    """
    score = min(1.0, abs(denominator))
    if LocalVolFlag.EXTRAPOLATED in flags:
        score *= 0.5
    if LocalVolFlag.IMPLAUSIBLE_MAGNITUDE in flags:
        score *= 0.5
    return float(max(0.0, min(1.0, score)))


def constant_local_volatility(sigma: float) -> ConstantLocalVol:
    return ConstantLocalVol(sigma)


@dataclass(frozen=True, slots=True)
class ConstantLocalVol:
    """A flat local volatility, for the PDE's convergence test.

    Not a market model. It exists so the PDE can be run against a case whose
    exact answer is known, which is the only way to measure an order of
    convergence rather than merely observe a small error.
    """

    sigma: float

    def __call__(self, spot: np.ndarray, time_to_expiry: float) -> np.ndarray:
        del time_to_expiry
        return np.full_like(np.asarray(spot, dtype=float), self.sigma, dtype=float)


@dataclass(frozen=True, slots=True)
class SurfaceLocalVol:
    """Local volatility read off an SSVI surface, as the PDE consumes it.

    The surface is parameterised in ``k = log(K / F_T)``, so evaluating it at
    calendar time ``t`` needs the forward **to t**, not the forward to the
    option's own maturity. Holding one forward fixed across the whole time
    grid shifts every lookup along the smile by the carry, which on a skewed
    surface is a systematic error in the wings that no amount of grid
    refinement removes. The forward curve here is ``spot * exp(carry * t)``,
    a deterministic carry — stated rather than assumed silently, and the same
    convention the forward estimator records.

    Where the surface produces no valid point the caller's ``fallback`` is used
    and the substitution is counted, because a PDE cannot have a hole in its
    coefficient — but the count travels with the result so a price computed
    mostly from fallbacks is visible as one.
    """

    surface: SSVISurface
    #: Today's spot, the base of the forward curve.
    spot: float
    #: ``r - q``: the continuously compounded cost of carry.
    carry: float
    fallback: float

    def __post_init__(self) -> None:
        if self.spot <= 0:
            raise ValueError("the spot must be positive")
        if self.fallback <= 0:
            raise ValueError("the fallback volatility must be positive")

    def forward(self, maturity: float) -> float:
        return float(self.spot * np.exp(self.carry * max(maturity, 0.0)))

    def __call__(self, spot: np.ndarray, time_to_expiry: float) -> np.ndarray:
        spot = np.asarray(spot, dtype=float)
        maturity = max(time_to_expiry, 1e-8)
        k = np.log(np.maximum(spot, 1e-300) / self.forward(maturity))

        w, dw_dk, d2w_dk2, dw_dt = self.surface.derivatives(k, maturity)
        denominator = dupire_denominator(k, w, dw_dk, d2w_dk2)
        variance = np.where(
            (np.abs(denominator) >= MIN_DENOMINATOR) & (dw_dt >= 0.0),
            dw_dt / np.where(np.abs(denominator) < MIN_DENOMINATOR, 1.0, denominator),
            np.nan,
        )
        usable = np.isfinite(variance) & (variance > MIN_LOCAL_VARIANCE)
        return np.where(usable, np.sqrt(np.where(usable, variance, 1.0)), self.fallback)

    def fallback_fraction(self, spot: np.ndarray, time_to_expiry: float) -> float:
        spot = np.asarray(spot, dtype=float)
        values = self(spot, time_to_expiry)
        return float(np.mean(np.isclose(values, self.fallback)))
