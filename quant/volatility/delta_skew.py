"""Skew and smile quoted the way a market quotes them: by delta.

A slice's ``dsigma/dk`` at the money is a clean mathematical description of
skew, and it is what :mod:`domains.derivatives.characteristics` records. It is
not, however, what anyone trading options means by "the 25-delta skew". That is
a **risk reversal**: the difference between the volatility at the strike whose
call delta is +0.25 and the strike whose put delta is -0.25.

Two things make this less trivial than it looks, and both are handled explicitly
rather than assumed away.

**Delta depends on volatility, which depends on strike.** The strike whose delta
is 0.25 cannot be computed in closed form from a smile, because the volatility
used in the delta is itself a function of the strike being solved for. So it is
a root-find, on a function that is monotone in ``k`` for any admissible smile.

**"Delta" is not one quantity.** Spot delta and forward delta differ by the
discount factor; premium-adjusted delta differs again; and a convention chosen
silently is a number nobody can reconcile with their broker's. Everything here
is **forward delta** (Black-76, undiscounted), which is the convention the rest
of this platform's forward-based machinery uses, and the convention is carried
on every result rather than documented once and forgotten.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from quant.numerical.roots import brent

#: Below this the smile is a point, not a curve, and a delta has no strike.
MIN_TIME_TO_EXPIRY = 1e-8
MIN_VOLATILITY = 1e-8


class DeltaConvention(StrEnum):
    """Which delta the strike was solved for.

    Only one is implemented. The enum exists so that a result says which
    convention produced it, and so adding another later cannot silently change
    the meaning of numbers already stored.
    """

    #: ``N(d1)`` for a call, ``N(d1) - 1`` for a put. Undiscounted; Black-76.
    FORWARD = "FORWARD"


class DeltaSolveStatus(StrEnum):
    OK = "OK"
    #: The strike was found, but outside the log-moneyness range the smile was
    #: fitted over. The number is an extrapolation and says so.
    EXTRAPOLATED = "EXTRAPOLATED"
    #: No strike in the searched range produces this delta.
    NOT_BRACKETED = "NOT_BRACKETED"
    #: The root-find ran and did not converge.
    NOT_CONVERGED = "NOT_CONVERGED"
    #: The slice cannot support the calculation at all.
    UNUSABLE_SLICE = "UNUSABLE_SLICE"


def normal_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def forward_delta(k: float, volatility: float, time_to_expiry: float, is_call: bool) -> float:
    """Black-76 forward delta at log-moneyness ``k = ln(K / F)``.

    Undiscounted on purpose: the discount factor scales both sides of every
    comparison here and carrying it would only invite a convention mismatch.
    """
    if time_to_expiry <= MIN_TIME_TO_EXPIRY or volatility <= MIN_VOLATILITY:
        # At zero time or zero vol the option is its intrinsic value and delta
        # is a step function. Returning the step is correct, and honest.
        intrinsic = 1.0 if k < 0.0 else 0.0
        return intrinsic if is_call else intrinsic - 1.0

    sqrt_tau = math.sqrt(time_to_expiry)
    d1 = (-k + 0.5 * volatility * volatility * time_to_expiry) / (volatility * sqrt_tau)
    call_delta = normal_cdf(d1)
    return call_delta if is_call else call_delta - 1.0


@dataclass(frozen=True, slots=True)
class DeltaStrike:
    """The strike at a target delta, and how confident we are in it."""

    target_delta: float
    is_call: bool
    status: DeltaSolveStatus
    convention: DeltaConvention = DeltaConvention.FORWARD
    #: Log-moneyness of the solved strike. ``None`` unless the solve succeeded.
    log_moneyness: float | None = None
    strike: float | None = None
    volatility: float | None = None
    #: ``delta(solved strike) - target``. Small by construction when converged;
    #: reported so a caller need not take convergence on trust.
    residual: float | None = None

    @property
    def ok(self) -> bool:
        return self.status in {DeltaSolveStatus.OK, DeltaSolveStatus.EXTRAPOLATED}

    def to_dict(self) -> dict:
        return {
            "target_delta": self.target_delta,
            "option_type": "CALL" if self.is_call else "PUT",
            "status": str(self.status),
            "delta_convention": str(self.convention),
            "log_moneyness": self.log_moneyness,
            "strike": self.strike,
            "volatility": self.volatility,
            "residual": self.residual,
        }


def solve_delta_strike(
    target_delta: float,
    is_call: bool,
    volatility_at: Callable[[float], float],
    forward: float,
    time_to_expiry: float,
    k_lower: float = -3.0,
    k_upper: float = 3.0,
    fitted_range: tuple[float | None, float | None] = (None, None),
) -> DeltaStrike:
    """Find the strike whose forward delta equals ``target_delta``.

    ``volatility_at`` is the smile: log-moneyness in, volatility out. Passing it
    as a callable rather than a set of parameters keeps this function usable
    against any smile representation, and keeps the quant layer free of the
    domain's surface types.

    ``target_delta`` is positive for a call and negative for a put, matching the
    sign of the quantity being solved for. Anything else is refused rather than
    silently interpreted.
    """
    if time_to_expiry <= MIN_TIME_TO_EXPIRY:
        return DeltaStrike(target_delta, is_call, DeltaSolveStatus.UNUSABLE_SLICE)
    if is_call and not 0.0 < target_delta < 1.0:
        return DeltaStrike(target_delta, is_call, DeltaSolveStatus.UNUSABLE_SLICE)
    if not is_call and not -1.0 < target_delta < 0.0:
        return DeltaStrike(target_delta, is_call, DeltaSolveStatus.UNUSABLE_SLICE)

    def gap(k: float) -> float:
        return forward_delta(k, volatility_at(k), time_to_expiry, is_call) - target_delta

    try:
        low, high = gap(k_lower), gap(k_upper)
    except (ValueError, ZeroDivisionError, OverflowError):
        return DeltaStrike(target_delta, is_call, DeltaSolveStatus.UNUSABLE_SLICE)

    if not math.isfinite(low) or not math.isfinite(high):
        return DeltaStrike(target_delta, is_call, DeltaSolveStatus.UNUSABLE_SLICE)
    if low * high > 0.0:
        # No sign change across the searched range. For a smile whose wings obey
        # the no-arbitrage slope bound, delta is monotone in ``k`` and this means
        # the delta simply does not occur. For a smile violating that bound it
        # can also mean delta turned back on itself, and a root may exist inside.
        #
        # Both are reported the same way, and neither is resolved by widening the
        # range or by hunting for an interior root: the first would put a strike
        # far outside the quoted market into a skew number, and the second would
        # pick one of several strikes with the same delta without saying which.
        return DeltaStrike(target_delta, is_call, DeltaSolveStatus.NOT_BRACKETED)

    result = brent(gap, k_lower, k_upper)
    if not result.converged:
        return DeltaStrike(target_delta, is_call, DeltaSolveStatus.NOT_CONVERGED)

    k = float(result.root)
    volatility = float(volatility_at(k))
    k_min, k_max = fitted_range
    outside = (k_min is not None and k < k_min) or (k_max is not None and k > k_max)

    return DeltaStrike(
        target_delta=target_delta,
        is_call=is_call,
        status=DeltaSolveStatus.EXTRAPOLATED if outside else DeltaSolveStatus.OK,
        log_moneyness=k,
        strike=forward * math.exp(k),
        volatility=volatility,
        residual=float(result.residual),
    )


@dataclass(frozen=True, slots=True)
class DeltaSmile:
    """Risk reversal and butterfly at one delta level, for one expiry.

    Both are ``None`` unless the strikes they need were actually found. A skew
    of zero and a skew that could not be measured are different statements, and
    a chart that cannot tell them apart is worse than a gap in the line.
    """

    delta_level: float
    call: DeltaStrike
    put: DeltaStrike
    atm_volatility: float
    convention: DeltaConvention = DeltaConvention.FORWARD

    @property
    def risk_reversal(self) -> float | None:
        """``sigma(call) - sigma(put)``. Negative for the usual equity shape."""
        if not (self.call.ok and self.put.ok):
            return None
        return self.call.volatility - self.put.volatility

    @property
    def butterfly(self) -> float | None:
        """Mean wing volatility less the at-the-money level."""
        if not (self.call.ok and self.put.ok):
            return None
        return 0.5 * (self.call.volatility + self.put.volatility) - self.atm_volatility

    @property
    def measured(self) -> bool:
        return self.risk_reversal is not None

    def to_dict(self) -> dict:
        return {
            "delta_level": self.delta_level,
            "delta_convention": str(self.convention),
            "atm_volatility": self.atm_volatility,
            "risk_reversal": self.risk_reversal,
            "butterfly": self.butterfly,
            "call": self.call.to_dict(),
            "put": self.put.to_dict(),
        }


def delta_smile(
    delta_level: float,
    volatility_at: Callable[[float], float],
    forward: float,
    time_to_expiry: float,
    fitted_range: tuple[float | None, float | None] = (None, None),
) -> DeltaSmile:
    """Risk reversal and butterfly at ``delta_level`` (e.g. 0.25 for 25-delta).

    The at-the-money reference is the volatility at ``k = 0`` — forward
    at-the-money, the same reference the surface characteristics use, so a
    butterfly here and a level there are measured against the same thing.
    """
    call = solve_delta_strike(
        delta_level,
        True,
        volatility_at,
        forward,
        time_to_expiry,
        fitted_range=fitted_range,
    )
    put = solve_delta_strike(
        -delta_level,
        False,
        volatility_at,
        forward,
        time_to_expiry,
        fitted_range=fitted_range,
    )
    return DeltaSmile(
        delta_level=delta_level,
        call=call,
        put=put,
        atm_volatility=float(volatility_at(0.0)),
    )
