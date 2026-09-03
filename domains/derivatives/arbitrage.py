"""Arbitrage diagnostics.

Two questions, deliberately kept apart:

1. **Is the market data internally consistent?** Violations here are almost
   always a data-quality signal — stale legs, non-simultaneous quotes, a wrong
   multiplier, a mismatched underlying reference — and only very rarely a real
   executable opportunity.
2. **Is our fitted surface admissible?** Violations here are a *model* defect
   and must never be blamed on the market.

The same conditions run over both, and the results are returned in separate
reports with an explicit ``scope``. Collapsing them would let a smooth fit hide
a broken market, and let a broken fit be reported as a market anomaly.

Severity comes from **magnitude relative to the quoted spread**. A convexity
breach smaller than the spread is not exploitable and is ubiquitous on a
discrete strike grid; a large one on liquid strikes is a genuine problem. A
boolean "violated" would erase the difference.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum

import numpy as np

from domains.derivatives.models import SmileSlice
from domains.derivatives.surface import VolatilitySurface
from domains.instruments.enums import OptionType
from domains.market_data.quality.flags import Severity
from quant.interpolation import Extrapolation, linear_interpolate
from quant.volatility.arbitrage import (
    check_butterfly,
    check_calendar,
    check_price_bounds,
    check_put_call_parity,
    check_vertical_spreads,
)
from quant.volatility.svi import LEE_WING_BOUND, durrleman_g

ARBITRAGE_MODEL_VERSION = "arbitrage-validator@1.0.0"

#: A breach within one spread is INFO, within three is WARNING, beyond is ERROR.
SPREAD_MULTIPLE_WARNING = 1.0
SPREAD_MULTIPLE_ERROR = 3.0
#: Used when no spread is available for the strikes involved.
FALLBACK_SCALE_RELATIVE = 1e-4

#: Durrleman's g is our own model output, so any negative value is a defect.
#: Below this it is numerical noise in the grid search rather than a real region.
DURRLEMAN_NOISE = 1e-9
DURRLEMAN_ERROR = 1e-6
#: Calendar breaches judged against the shorter slice's own total variance.
CALENDAR_INFO_RELATIVE = 1e-3
CALENDAR_WARNING_RELATIVE = 1e-2

#: Grid on which fitted slices are compared for calendar consistency.
CALENDAR_GRID_POINTS = 81


class ArbitrageScope(StrEnum):
    RAW_MARKET = "RAW_MARKET"
    FITTED_SURFACE = "FITTED_SURFACE"


class ViolationType(StrEnum):
    PRICE_BOUND = "PRICE_BOUND"
    PUT_CALL_PARITY = "PUT_CALL_PARITY"
    VERTICAL_SPREAD = "VERTICAL_SPREAD"
    BUTTERFLY = "BUTTERFLY"
    CALENDAR = "CALENDAR"
    #: Durrleman's condition on a fitted slice: a negative implied density.
    DURRLEMAN = "DURRLEMAN"
    #: Lee's moment formula bound on the asymptotic wing slope.
    WING_SLOPE = "WING_SLOPE"


@dataclass(frozen=True, slots=True)
class ArbitrageViolation:
    scope: ArbitrageScope
    violation_type: ViolationType
    severity: Severity
    magnitude: float
    expiry: date | None = None
    strike: Decimal | None = None
    option_type: OptionType | None = None
    #: What the magnitude was judged against, in the same units.
    tolerance: float | None = None
    detail: dict = field(default_factory=dict)
    affected_instruments: tuple[uuid.UUID, ...] = field(default=())

    def to_dict(self) -> dict:
        return {
            "scope": str(self.scope),
            "type": str(self.violation_type),
            "severity": str(self.severity),
            "magnitude": self.magnitude,
            "tolerance": self.tolerance,
            "expiry": self.expiry.isoformat() if self.expiry else None,
            "strike": format(self.strike, "f") if self.strike is not None else None,
            "option_type": str(self.option_type) if self.option_type else None,
            "detail": self.detail,
            "affected_instruments": [str(i) for i in self.affected_instruments],
        }


@dataclass(frozen=True, slots=True)
class ArbitrageReport:
    scope: ArbitrageScope
    violations: tuple[ArbitrageViolation, ...] = field(default=())
    checks_run: tuple[str, ...] = field(default=())
    observations: int = 0

    @property
    def severity(self) -> Severity | None:
        if not self.violations:
            return None
        return max(violation.severity for violation in self.violations)

    @property
    def summary(self) -> dict:
        by_type: dict[str, int] = {}
        by_severity: dict[str, int] = {}
        for violation in self.violations:
            key = str(violation.violation_type)
            by_type[key] = by_type.get(key, 0) + 1
            level = str(violation.severity)
            by_severity[level] = by_severity.get(level, 0) + 1
        return {"by_type": by_type, "by_severity": by_severity}

    def at_or_above(self, severity: Severity) -> tuple[ArbitrageViolation, ...]:
        return tuple(v for v in self.violations if v.severity >= severity)

    def to_dict(self, max_violations: int = 500) -> dict:
        return {
            "scope": str(self.scope),
            "severity": str(self.severity) if self.severity else None,
            "counts": {"violations": len(self.violations), **self.summary},
            "checks_run": list(self.checks_run),
            "observations": self.observations,
            "violations": [v.to_dict() for v in self.violations[:max_violations]],
        }


def _severity_from_spread(magnitude: float, scale: float) -> tuple[Severity, float]:
    """Grade a price-units breach against the local spread."""
    tolerance = max(scale, 0.0)
    if magnitude <= tolerance * SPREAD_MULTIPLE_WARNING:
        return Severity.INFO, tolerance
    if magnitude <= tolerance * SPREAD_MULTIPLE_ERROR:
        return Severity.WARNING, tolerance
    return Severity.ERROR, tolerance


class ArbitrageValidator:
    """Runs the same conditions over observed quotes and over fitted slices."""

    # ------------------------------------------------------------ raw market
    def validate_raw(self, slices: list[SmileSlice]) -> ArbitrageReport:
        violations: list[ArbitrageViolation] = []
        observations = 0

        usable = [s for s in slices if s.forward.selected is not None and s.points]
        for slice_ in usable:
            observations += len(slice_.points)
            violations.extend(self._raw_slice(slice_))

        violations.extend(self._raw_calendar(usable))

        return ArbitrageReport(
            scope=ArbitrageScope.RAW_MARKET,
            violations=tuple(violations),
            checks_run=(
                "PRICE_BOUND",
                "PUT_CALL_PARITY",
                "VERTICAL_SPREAD",
                "BUTTERFLY",
                "CALENDAR",
            ),
            observations=observations,
        )

    def _raw_slice(self, slice_: SmileSlice) -> list[ArbitrageViolation]:
        forward = slice_.forward.selected.value
        discount = slice_.forward.selected.discount_factor or 1.0

        by_type: dict[OptionType, list] = {OptionType.CALL: [], OptionType.PUT: []}
        for point in slice_.points:
            if point.price_used is None:
                continue
            by_type[point.option_type].append(point)

        violations: list[ArbitrageViolation] = []
        for option_type, points in by_type.items():
            if len(points) < 2:
                continue
            points = sorted(points, key=lambda p: p.strike)
            strikes = np.array([float(p.strike) for p in points])
            prices = np.array([p.price_used for p in points])
            spreads = np.array([p.price_spread if p.price_spread else 0.0 for p in points])
            is_call = option_type is OptionType.CALL

            for raw in check_price_bounds(strikes, prices, forward, discount, is_call):
                index = raw.indices[0]
                severity, tolerance = _severity_from_spread(
                    raw.magnitude, self._scale(spreads[[index]], prices[index])
                )
                violations.append(
                    ArbitrageViolation(
                        ArbitrageScope.RAW_MARKET,
                        ViolationType.PRICE_BOUND,
                        severity,
                        raw.magnitude,
                        expiry=slice_.expiry,
                        strike=points[index].strike,
                        option_type=option_type,
                        tolerance=tolerance,
                        detail=raw.detail,
                        affected_instruments=(points[index].instrument_id,),
                    )
                )

            for raw in check_vertical_spreads(strikes, prices, discount, is_call):
                indices = list(raw.indices)
                severity, tolerance = _severity_from_spread(
                    raw.magnitude, self._scale(spreads[indices], prices[indices[0]])
                )
                violations.append(
                    ArbitrageViolation(
                        ArbitrageScope.RAW_MARKET,
                        ViolationType.VERTICAL_SPREAD,
                        severity,
                        raw.magnitude,
                        expiry=slice_.expiry,
                        strike=points[indices[0]].strike,
                        option_type=option_type,
                        tolerance=tolerance,
                        detail=raw.detail,
                        affected_instruments=tuple(points[i].instrument_id for i in indices),
                    )
                )

            for raw in check_butterfly(strikes, prices):
                indices = list(raw.indices)
                severity, tolerance = _severity_from_spread(
                    raw.magnitude, self._scale(spreads[indices], prices[indices[1]])
                )
                violations.append(
                    ArbitrageViolation(
                        ArbitrageScope.RAW_MARKET,
                        ViolationType.BUTTERFLY,
                        severity,
                        raw.magnitude,
                        expiry=slice_.expiry,
                        strike=points[indices[1]].strike,
                        option_type=option_type,
                        tolerance=tolerance,
                        detail=raw.detail,
                        affected_instruments=tuple(points[i].instrument_id for i in indices),
                    )
                )

        violations.extend(self._raw_parity(slice_, forward, discount))
        return violations

    def _raw_parity(
        self, slice_: SmileSlice, forward: float, discount: float
    ) -> list[ArbitrageViolation]:
        pairs: dict[Decimal, dict[OptionType, object]] = {}
        for point in slice_.points:
            if point.price_used is None:
                continue
            pairs.setdefault(point.strike, {})[point.option_type] = point

        strikes, calls, puts, scales, instruments = [], [], [], [], []
        for strike in sorted(pairs):
            sides = pairs[strike]
            call, put = sides.get(OptionType.CALL), sides.get(OptionType.PUT)
            if call is None or put is None:
                continue
            strikes.append(float(strike))
            calls.append(call.price_used)
            puts.append(put.price_used)
            # A parity residual is a two-legged trade: both spreads must be paid.
            scales.append((call.price_spread or 0.0) / 2.0 + (put.price_spread or 0.0) / 2.0)
            instruments.append((call.instrument_id, put.instrument_id))

        if not strikes:
            return []

        violations: list[ArbitrageViolation] = []
        for raw in check_put_call_parity(
            np.array(strikes), np.array(calls), np.array(puts), forward, discount
        ):
            index = raw.indices[0]
            scale = scales[index] or max(abs(calls[index]) * FALLBACK_SCALE_RELATIVE, 1e-9)
            severity, tolerance = _severity_from_spread(raw.magnitude, scale)
            if severity is Severity.INFO:
                # Parity holds within the cost of crossing both legs; reporting
                # every strike as a violation would bury the real ones.
                continue
            violations.append(
                ArbitrageViolation(
                    ArbitrageScope.RAW_MARKET,
                    ViolationType.PUT_CALL_PARITY,
                    severity,
                    raw.magnitude,
                    expiry=slice_.expiry,
                    strike=Decimal(str(strikes[index])),
                    tolerance=tolerance,
                    detail=raw.detail,
                    affected_instruments=instruments[index],
                )
            )
        return violations

    def _raw_calendar(self, slices: list[SmileSlice]) -> list[ArbitrageViolation]:
        """Observed total variance must not fall with maturity at fixed ``k``.

        Compared only over the log-moneyness range both expiries actually
        observed: extrapolating one slice to reach the other's wings would
        manufacture violations out of a missing quote.
        """
        usable = []
        for slice_ in sorted(slices, key=lambda s: s.expiry):
            points = [p for p in slice_.points if p.used_for_smile and p.total_variance is not None]
            if len(points) >= 2:
                points.sort(key=lambda p: p.log_moneyness)
                usable.append((slice_, points))

        violations: list[ArbitrageViolation] = []
        for (short_slice, short_points), (long_slice, long_points) in zip(
            usable, usable[1:], strict=False
        ):
            short_k = np.array([p.log_moneyness for p in short_points])
            long_k = np.array([p.log_moneyness for p in long_points])
            low = max(short_k.min(), long_k.min())
            high = min(short_k.max(), long_k.max())
            if high <= low:
                continue

            grid = np.linspace(low, high, CALENDAR_GRID_POINTS)
            short_w = linear_interpolate(
                grid,
                short_k,
                np.array([p.total_variance for p in short_points]),
                Extrapolation.ERROR,
            )
            long_w = linear_interpolate(
                grid,
                long_k,
                np.array([p.total_variance for p in long_points]),
                Extrapolation.ERROR,
            )

            for raw in check_calendar(grid, short_w, long_w):
                index = raw.indices[0]
                severity = self._calendar_severity(raw.magnitude, float(short_w[index]))
                violations.append(
                    ArbitrageViolation(
                        ArbitrageScope.RAW_MARKET,
                        ViolationType.CALENDAR,
                        severity,
                        raw.magnitude,
                        expiry=long_slice.expiry,
                        tolerance=float(short_w[index]) * CALENDAR_INFO_RELATIVE,
                        detail={
                            **raw.detail,
                            "short_expiry": short_slice.expiry.isoformat(),
                            "long_expiry": long_slice.expiry.isoformat(),
                        },
                    )
                )
        return violations

    # --------------------------------------------------------- fitted surface
    def validate_surface(self, surface: VolatilitySurface) -> ArbitrageReport:
        violations: list[ArbitrageViolation] = []
        fitted = surface.fitted_slices

        for slice_ in fitted:
            params = slice_.parameters
            assert params is not None

            slope = params.b * (1.0 + abs(params.rho))
            if slope > LEE_WING_BOUND + DURRLEMAN_NOISE:
                violations.append(
                    ArbitrageViolation(
                        ArbitrageScope.FITTED_SURFACE,
                        ViolationType.WING_SLOPE,
                        Severity.ERROR,
                        float(slope - LEE_WING_BOUND),
                        expiry=slice_.expiry,
                        tolerance=LEE_WING_BOUND,
                        detail={"wing_slope": float(slope), "bound": LEE_WING_BOUND},
                    )
                )

            low = (slice_.k_min if slice_.k_min is not None else -1.0) - 1.0
            high = (slice_.k_max if slice_.k_max is not None else 1.0) + 1.0
            grid = np.linspace(low, high, 401)
            g = durrleman_g(grid, params)
            worst = float(np.min(g))
            if worst < -DURRLEMAN_NOISE:
                index = int(np.argmin(g))
                violations.append(
                    ArbitrageViolation(
                        ArbitrageScope.FITTED_SURFACE,
                        ViolationType.DURRLEMAN,
                        Severity.ERROR if worst < -DURRLEMAN_ERROR else Severity.WARNING,
                        -worst,
                        expiry=slice_.expiry,
                        tolerance=DURRLEMAN_NOISE,
                        detail={
                            "min_g": worst,
                            "at_log_moneyness": float(grid[index]),
                            "meaning": ("a negative Durrleman g is a negative implied density"),
                        },
                    )
                )

        violations.extend(self._surface_calendar(fitted))

        return ArbitrageReport(
            scope=ArbitrageScope.FITTED_SURFACE,
            violations=tuple(violations),
            checks_run=("DURRLEMAN", "WING_SLOPE", "CALENDAR"),
            observations=len(fitted),
        )

    def _surface_calendar(self, fitted) -> list[ArbitrageViolation]:
        ordered = sorted(fitted, key=lambda s: s.time_to_expiry)
        violations: list[ArbitrageViolation] = []

        for short_slice, long_slice in zip(ordered, ordered[1:], strict=False):
            low = max(
                short_slice.k_min if short_slice.k_min is not None else -1.0,
                long_slice.k_min if long_slice.k_min is not None else -1.0,
            )
            high = min(
                short_slice.k_max if short_slice.k_max is not None else 1.0,
                long_slice.k_max if long_slice.k_max is not None else 1.0,
            )
            if high <= low:
                low, high = -0.3, 0.3

            grid = np.linspace(low, high, CALENDAR_GRID_POINTS)
            short_w = short_slice.total_variance(grid)
            long_w = long_slice.total_variance(grid)

            for raw in check_calendar(grid, short_w, long_w):
                index = raw.indices[0]
                severity = self._calendar_severity(raw.magnitude, float(short_w[index]))
                violations.append(
                    ArbitrageViolation(
                        ArbitrageScope.FITTED_SURFACE,
                        ViolationType.CALENDAR,
                        severity,
                        raw.magnitude,
                        expiry=long_slice.expiry,
                        tolerance=float(short_w[index]) * CALENDAR_INFO_RELATIVE,
                        detail={
                            **raw.detail,
                            "short_expiry": short_slice.expiry.isoformat(),
                            "long_expiry": long_slice.expiry.isoformat(),
                            "note": (
                                "per-expiry SVI cannot prevent calendar arbitrage, "
                                "only detect it; the SSVI global surface fitted "
                                "from the same analysis cannot contain it"
                            ),
                        },
                    )
                )
        return violations

    # -------------------------------------------------------------- helpers
    @staticmethod
    def _scale(spreads: np.ndarray, price: float) -> float:
        usable = [float(s) for s in np.atleast_1d(spreads) if s and s > 0]
        if usable:
            return float(np.mean(usable))
        return max(abs(price) * FALLBACK_SCALE_RELATIVE, 1e-9)

    @staticmethod
    def _calendar_severity(magnitude: float, reference: float) -> Severity:
        if reference <= 0:
            return Severity.ERROR
        relative = magnitude / reference
        if relative <= CALENDAR_INFO_RELATIVE:
            return Severity.INFO
        if relative <= CALENDAR_WARNING_RELATIVE:
            return Severity.WARNING
        return Severity.ERROR
