"""Full revaluation under a shock, and the Greek approximation it replaces.

Both are here on purpose. The approximation is genuinely useful — it is fast
enough to move a slider against — and it is genuinely wrong for large moves. The
platform offers it, labels it, and can show you the two side by side, which is
more useful than an argument about which one is right.

    full:  V(S(1+s), sigma + dv, r + dr, tau - dt) - V(S, sigma, r, tau)
    greek: delta*dS + 0.5*gamma*dS^2 + vega*dv + rho*dr + theta*dt

The gap between them is second order in the shock and higher, so it is small for
a 1% move and large for a 10% move on a short-gamma book. A test asserts the
divergence, because if the two agreed the full revaluation would not be earning
its cost.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field

import numpy as np

from domains.instruments.enums import OptionType
from domains.risk.exposure import MIN_VOLATILITY, ExposureSet, PositionExposure
from domains.scenarios.models import RiskFactorKind, Scenario, ShockType
from quant.pricing.black_scholes import bsm_price

#: Trading days per year, used only to convert a stress horizon into the time
#: decay applied to an option's remaining life. A stated convention.
TRADING_DAYS_PER_YEAR = 252.0
CALENDAR_DAYS_PER_YEAR = 365.0


@dataclass(frozen=True, slots=True)
class FactorShock:
    """One underlying's resolved move, in the units the pricer wants."""

    spot_return: float = 0.0
    spot_absolute: float = 0.0
    vol_points: float = 0.0
    vol_relative: float = 0.0
    rate_shift: float = 0.0
    dividend_shift: float = 0.0

    @property
    def is_null(self) -> bool:
        return (
            self.spot_return == 0.0
            and self.spot_absolute == 0.0
            and self.vol_points == 0.0
            and self.vol_relative == 0.0
            and self.rate_shift == 0.0
            and self.dividend_shift == 0.0
        )

    def shocked_spot(self, spot: float) -> float:
        return spot * (1.0 + self.spot_return) + self.spot_absolute

    def shocked_volatility(self, volatility: float) -> float:
        return volatility * (1.0 + self.vol_relative) + self.vol_points

    def to_dict(self) -> dict:
        return {
            "spot_return": self.spot_return,
            "spot_absolute": self.spot_absolute,
            "vol_points": self.vol_points,
            "vol_relative": self.vol_relative,
            "rate_shift": self.rate_shift,
            "dividend_shift": self.dividend_shift,
        }


def resolve_scenario(scenario: Scenario, underlying_keys: Sequence[str]) -> dict[str, FactorShock]:
    """Turn a scenario's shocks into one resolved move per underlying.

    Shocks of the same kind compose additively in their own units rather than
    the last one winning, so "market -5%" plus "NIFTY -3%" is -8% on NIFTY and
    -5% elsewhere. Silently dropping one of them would be the alternative.
    """
    resolved: dict[str, FactorShock] = {}
    for key in underlying_keys:
        spot_return = spot_absolute = 0.0
        vol_points = vol_relative = 0.0
        rate_shift = dividend_shift = 0.0

        for shock in scenario.shocks_for(RiskFactorKind.UNDERLYING_PRICE, key):
            if shock.shock_type is ShockType.PERCENTAGE:
                spot_return += shock.value
            else:
                spot_absolute += shock.value

        for shock in scenario.shocks_for(RiskFactorKind.VOLATILITY, key):
            if shock.shock_type is ShockType.VOL_POINTS:
                vol_points += shock.value
            else:
                vol_relative += shock.value

        for shock in scenario.shocks_for(RiskFactorKind.RISK_FREE_RATE, key):
            rate_shift += (
                shock.value / 10_000.0
                if shock.shock_type is ShockType.BASIS_POINTS
                else shock.value
            )

        for shock in scenario.shocks_for(RiskFactorKind.DIVIDEND_YIELD, key):
            dividend_shift += (
                shock.value / 10_000.0
                if shock.shock_type is ShockType.BASIS_POINTS
                else shock.value
            )

        resolved[key] = FactorShock(
            spot_return=spot_return,
            spot_absolute=spot_absolute,
            vol_points=vol_points,
            vol_relative=vol_relative,
            rate_shift=rate_shift,
            dividend_shift=dividend_shift,
        )
    return resolved


def _rate_value(shock) -> float:
    """Basis points into a decimal rate; an absolute shock is already one."""
    return shock.value / 10_000.0 if shock.shock_type is ShockType.BASIS_POINTS else shock.value


@dataclass(frozen=True, slots=True)
class PositionRevaluation:
    position_id: uuid.UUID
    canonical_key: str
    underlying_key: str
    strategy_tag: str | None
    expiry_key: str | None
    asset_class: str
    base_value: float
    shocked_value: float
    shocked_spot: float
    shocked_volatility: float | None
    volatility_was_floored: bool

    @property
    def pnl(self) -> float:
        return self.shocked_value - self.base_value

    def to_dict(self) -> dict:
        return {
            "position_id": str(self.position_id),
            "canonical_key": self.canonical_key,
            "underlying_id": self.underlying_key,
            "strategy_tag": self.strategy_tag,
            "expiry": self.expiry_key,
            "asset_class": self.asset_class,
            "base_value": self.base_value,
            "shocked_value": self.shocked_value,
            "pnl": self.pnl,
            "shocked_spot": self.shocked_spot,
            "shocked_volatility": self.shocked_volatility,
            "volatility_was_floored": self.volatility_was_floored,
        }


@dataclass(frozen=True, slots=True)
class RevaluationResult:
    """A full repricing, with the linear estimate beside it for comparison."""

    positions: tuple[PositionRevaluation, ...]
    base_value: float
    shocked_value: float
    greek_estimate: float
    time_decay_days: float
    shocks: dict[str, FactorShock] = field(default_factory=dict)
    floored_volatilities: int = 0

    @property
    def pnl(self) -> float:
        return self.shocked_value - self.base_value

    @property
    def approximation_error(self) -> float:
        """Full minus linear. Grows with the square of the shock."""
        return self.pnl - self.greek_estimate

    def to_dict(self, include_positions: bool = True) -> dict:
        payload = {
            "base_value": self.base_value,
            "shocked_value": self.shocked_value,
            "pnl": self.pnl,
            "greek_approximation": {
                "pnl": self.greek_estimate,
                "difference_from_full_revaluation": self.approximation_error,
                "method": "delta + 0.5 gamma dS^2 + vega dsigma + rho dr + theta dt",
                "caveat": (
                    "An approximation, offered for interactive use. The reported "
                    "P&L above is the full repricing; this number is not, and the "
                    "two diverge as the shock grows."
                ),
            },
            "time_decay_days": self.time_decay_days,
            "floored_volatilities": self.floored_volatilities,
            "shocks": {key: shock.to_dict() for key, shock in sorted(self.shocks.items())},
        }
        if include_positions:
            payload["positions"] = [position.to_dict() for position in self.positions]
        return payload


def revalue(
    exposures: ExposureSet,
    shocks: dict[str, FactorShock],
    time_decay_days: float = 0.0,
    include: Callable[[PositionExposure], bool] | None = None,
) -> RevaluationResult:
    """Reprice every exposure under its underlying's resolved shock.

    ``include`` selects which positions are *shocked*; the rest are held at
    their base anchors. That is how risk contribution is computed — by rerunning
    the same scenario with one group standing still — rather than by allocating
    the total linearly, which would attribute a nonlinear loss as if it were not.
    """
    revaluations: list[PositionRevaluation] = []
    greek_estimate = 0.0
    floored = 0
    decay_years = time_decay_days / CALENDAR_DAYS_PER_YEAR

    for exposure in exposures.exposures:
        shocked = include is None or include(exposure)
        shock = shocks.get(exposure.underlying_key, FactorShock()) if shocked else FactorShock()

        new_spot = shock.shocked_spot(exposure.spot)
        new_rate = exposure.rate + shock.rate_shift
        new_dividend = exposure.dividend_yield + shock.dividend_shift
        new_vol = None
        was_floored = False

        if exposure.is_option:
            raw_vol = shock.shocked_volatility(exposure.implied_volatility or 0.0)
            new_vol = max(raw_vol, MIN_VOLATILITY)
            was_floored = raw_vol < MIN_VOLATILITY
            if was_floored:
                floored += 1
            new_tau = max((exposure.time_to_expiry or 0.0) - decay_years, 0.0)
        else:
            new_tau = exposure.time_to_expiry

        shocked_value = exposure.value_at(
            spot=new_spot,
            volatility=new_vol,
            rate=new_rate,
            dividend_yield=new_dividend,
            time_to_expiry=new_tau,
        )
        revaluations.append(
            PositionRevaluation(
                position_id=exposure.position_id,
                canonical_key=exposure.canonical_key,
                underlying_key=exposure.underlying_key,
                strategy_tag=exposure.strategy_tag,
                expiry_key=_expiry_key(exposure),
                asset_class=str(exposure.asset_class),
                base_value=exposure.base_value,
                shocked_value=shocked_value,
                shocked_spot=new_spot,
                shocked_volatility=new_vol,
                volatility_was_floored=was_floored,
            )
        )
        greek_estimate += _greek_estimate(exposure, shock, time_decay_days)

    return RevaluationResult(
        positions=tuple(revaluations),
        base_value=sum(item.base_value for item in revaluations),
        shocked_value=sum(item.shocked_value for item in revaluations),
        greek_estimate=greek_estimate,
        time_decay_days=time_decay_days,
        shocks=dict(shocks),
        floored_volatilities=floored,
    )


def _greek_estimate(exposure: PositionExposure, shock: FactorShock, decay_days: float) -> float:
    """Second-order Taylor expansion in the position's own recorded Greeks.

    The units are the ones the Greeks were named for: ``vega_per_vol_point`` is
    per +0.01 of volatility and ``rho_per_bp`` is per basis point, so the shocks
    are converted into those units rather than the Greeks into the shocks'.
    """
    greeks = exposure.greeks
    d_spot = shock.shocked_spot(exposure.spot) - exposure.spot
    estimate = greeks.delta * d_spot + 0.5 * greeks.gamma * d_spot * d_spot

    if exposure.is_option:
        volatility = exposure.implied_volatility or 0.0
        d_vol = shock.shocked_volatility(volatility) - volatility
        estimate += greeks.vega_per_vol_point * (d_vol / 0.01)
        estimate += greeks.theta_per_day * decay_days

    estimate += greeks.rho_per_bp * (shock.rate_shift * 10_000.0)
    return estimate


def _expiry_key(exposure: PositionExposure) -> str | None:
    parts = exposure.canonical_key.split(":")
    # canonical key: EXCHANGE:ASSET_CLASS:SYMBOL[:EXPIRY[:STRIKE:TYPE]]
    return parts[3] if len(parts) > 3 else None


def revalue_many(
    exposures: ExposureSet,
    spot_returns: dict[str, np.ndarray],
    vol_changes: dict[str, np.ndarray] | None = None,
    rate_shifts: dict[str, np.ndarray] | None = None,
    time_decay_days: float = 0.0,
) -> np.ndarray:
    """Full repricing across many scenarios at once. Returns P&L per scenario.

    The loop is over positions and the vectorisation is over scenarios, which is
    the right way round: a book has tens of positions and a Monte Carlo run has
    tens of thousands of paths. Every path is a genuine repricing — this is a
    faster arrangement of the same arithmetic ``revalue`` does one scenario at a
    time, and a test asserts the two agree.
    """
    if not exposures.exposures:
        return np.zeros(0, dtype=float)

    lengths = {len(v) for v in spot_returns.values()}
    if len(lengths) != 1:
        raise ValueError("every factor must supply the same number of scenarios")
    scenarios = lengths.pop()

    vol_changes = vol_changes or {}
    rate_shifts = rate_shifts or {}
    zeros = np.zeros(scenarios, dtype=float)
    decay_years = time_decay_days / CALENDAR_DAYS_PER_YEAR

    pnl = np.zeros(scenarios, dtype=float)
    for exposure in exposures.exposures:
        key = exposure.underlying_key
        new_spot = exposure.spot * (1.0 + np.asarray(spot_returns.get(key, zeros), dtype=float))
        new_rate = exposure.rate + np.asarray(rate_shifts.get(key, zeros), dtype=float)

        if exposure.is_option:
            base_vol = exposure.implied_volatility or 0.0
            new_vol = np.maximum(
                base_vol + np.asarray(vol_changes.get(key, zeros), dtype=float),
                MIN_VOLATILITY,
            )
            new_tau = max((exposure.time_to_expiry or 0.0) - decay_years, 0.0)
            price = bsm_price(
                new_spot,
                exposure.strike,
                new_tau,
                new_rate,
                exposure.dividend_yield,
                new_vol,
                exposure.option_type is OptionType.CALL,
            )
        else:
            price = new_spot

        pnl += (np.asarray(price, dtype=float) - exposure.base_price) * (
            exposure.scale * exposure.fx_rate
        )
    return pnl
