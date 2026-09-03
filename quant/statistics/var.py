"""Value at Risk and Expected Shortfall from a sample of profit and loss.

Everything here works on a **loss** convention: a loss is a positive number, so
``loss = -pnl``. Mixing the two is the classic sign error in risk code, and the
one place it is done is at the boundary of this module.

The estimators are deliberately plain. Each returns the convention it used and
how many observations sat in the tail, because a 99% VaR from 60 observations is
supported by fewer than one point and saying so is the honest part.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

import numpy as np

#: Below this many observations a sample quantile is reported but marked
#: unreliable. At the 99th percentile a 250-day sample puts 2.5 observations in
#: the tail; below 100 the tail is a single point and the estimate is that
#: point, not a statistic.
MIN_RELIABLE_OBSERVATIONS = 100

#: The tail must contain at least this many observations for the Expected
#: Shortfall average to mean anything at all.
MIN_TAIL_OBSERVATIONS = 2


class QuantileMethod(StrEnum):
    #: NumPy's default: linear interpolation between order statistics
    #: (Hyndman & Fan type 7). Converges to the analytic quantile as the sample
    #: grows, which is what the validation tests assert.
    LINEAR_INTERPOLATION = "LINEAR_INTERPOLATION"
    #: The closed-form normal quantile, used by the parametric method.
    NORMAL_ANALYTIC = "NORMAL_ANALYTIC"


class VaRWarning(StrEnum):
    THIN_SAMPLE = "VAR_THIN_SAMPLE"
    THIN_TAIL = "VAR_THIN_TAIL"
    EMPTY_TAIL = "VAR_EMPTY_TAIL"
    DEGENERATE_SAMPLE = "VAR_DEGENERATE_SAMPLE"


@dataclass(frozen=True, slots=True)
class TailRisk:
    """One confidence level's answer, with what supports it.

    ``value_at_risk`` is a *threshold* loss: the level exceeded with probability
    ``1 - confidence``. ``expected_shortfall`` is the *average* loss given that
    the threshold was exceeded. They are different questions and the difference
    is stated wherever this is rendered.
    """

    confidence: float
    value_at_risk: float
    expected_shortfall: float
    observations: int
    tail_observations: int
    quantile_method: QuantileMethod
    mean_loss: float
    worst_loss: float
    warnings: tuple[str, ...] = ()

    @property
    def is_reliable(self) -> bool:
        return (
            self.observations >= MIN_RELIABLE_OBSERVATIONS
            and self.tail_observations >= MIN_TAIL_OBSERVATIONS
        )

    def to_dict(self) -> dict:
        return {
            "confidence": self.confidence,
            "value_at_risk": self.value_at_risk,
            "expected_shortfall": self.expected_shortfall,
            "observations": self.observations,
            "tail_observations": self.tail_observations,
            "quantile_method": str(self.quantile_method),
            "mean_loss": self.mean_loss,
            "worst_loss": self.worst_loss,
            "is_reliable": self.is_reliable,
            "warnings": list(self.warnings),
            "interpretation": {
                "value_at_risk": (
                    "A threshold loss: the loss exceeded with probability "
                    f"{1 - self.confidence:.1%} over the stated horizon."
                ),
                "expected_shortfall": (
                    "The average loss in the cases where that threshold was exceeded, "
                    "which is why it is never smaller than the value at risk."
                ),
            },
        }


def losses_from_pnl(pnl: Sequence[float] | np.ndarray) -> np.ndarray:
    """The one place the sign convention is applied."""
    return -np.asarray(pnl, dtype=float)


def historical_tail_risk(losses: Sequence[float] | np.ndarray, confidence: float) -> TailRisk:
    """Sample quantile and conditional tail mean of a loss sample.

    No distribution is assumed, so this is the method that stays valid for an
    option book: the sample is whatever the full repricing produced.
    """
    _check_confidence(confidence)
    sample = np.asarray(losses, dtype=float)
    sample = sample[np.isfinite(sample)]
    n = int(sample.size)
    if n == 0:
        raise ValueError("a tail risk estimate needs at least one observation")

    warnings: list[str] = []
    if n < MIN_RELIABLE_OBSERVATIONS:
        warnings.append(VaRWarning.THIN_SAMPLE)

    var = float(np.quantile(sample, confidence, method="linear"))
    tail = sample[sample >= var]
    tail_n = int(tail.size)

    if tail_n == 0:
        # Only reachable when every observation is identical, so the quantile is
        # the whole sample; the shortfall is then that same number.
        warnings.append(VaRWarning.EMPTY_TAIL)
        shortfall = var
    else:
        shortfall = float(tail.mean())
        if tail_n < MIN_TAIL_OBSERVATIONS:
            warnings.append(VaRWarning.THIN_TAIL)

    if float(np.ptp(sample)) == 0.0:
        warnings.append(VaRWarning.DEGENERATE_SAMPLE)

    return TailRisk(
        confidence=confidence,
        value_at_risk=var,
        expected_shortfall=max(shortfall, var),
        observations=n,
        tail_observations=tail_n,
        quantile_method=QuantileMethod.LINEAR_INTERPOLATION,
        mean_loss=float(sample.mean()),
        worst_loss=float(sample.max()),
        warnings=tuple(dict.fromkeys(warnings)),
    )


def parametric_tail_risk(
    mean_loss: float, std_loss: float, confidence: float, observations: int
) -> TailRisk:
    """Normal-distribution VaR and ES.

    Valid only for approximately linear exposures. The caller is responsible for
    saying so in the response; this function will happily compute a number for a
    portfolio it does not fit, which is precisely why it is never returned as
    the only measure for an option book.
    """
    _check_confidence(confidence)
    if std_loss < 0:
        raise ValueError("standard deviation cannot be negative")

    z = normal_quantile(confidence)
    var = mean_loss + z * std_loss
    # E[L | L > VaR] for a normal: mu + sigma * phi(z) / (1 - alpha).
    density = math.exp(-0.5 * z * z) / math.sqrt(2.0 * math.pi)
    shortfall = mean_loss + std_loss * density / (1.0 - confidence)

    warnings: list[str] = []
    if observations < MIN_RELIABLE_OBSERVATIONS:
        warnings.append(VaRWarning.THIN_SAMPLE)
    if std_loss == 0.0:
        warnings.append(VaRWarning.DEGENERATE_SAMPLE)

    return TailRisk(
        confidence=confidence,
        value_at_risk=var,
        expected_shortfall=max(shortfall, var),
        observations=observations,
        # The normal tail is continuous, so no observation count applies; the
        # sample size is reported instead and the tail count is what the sample
        # *would* have put beyond the threshold.
        tail_observations=int(round(observations * (1.0 - confidence))),
        quantile_method=QuantileMethod.NORMAL_ANALYTIC,
        mean_loss=mean_loss,
        worst_loss=float("nan"),
        warnings=tuple(warnings),
    )


def normal_quantile(probability: float) -> float:
    """Inverse standard normal CDF.

    Acklam's rational approximation, refined by one Halley step against
    ``math.erfc``. Accurate to full double precision over the range that
    matters here, and it keeps ``scipy`` out of the runtime dependencies.
    """
    if not 0.0 < probability < 1.0:
        raise ValueError(f"probability must be in (0, 1), got {probability}")

    a = (
        -3.969683028665376e01,
        2.209460984245205e02,
        -2.759285104469687e02,
        1.383577518672690e02,
        -3.066479806614716e01,
        2.506628277459239e00,
    )
    b = (
        -5.447609879822406e01,
        1.615858368580409e02,
        -1.556989798598866e02,
        6.680131188771972e01,
        -1.328068155288572e01,
    )
    c = (
        -7.784894002430293e-03,
        -3.223964580411365e-01,
        -2.400758277161838e00,
        -2.549732539343734e00,
        4.374664141464968e00,
        2.938163982698783e00,
    )
    d = (
        7.784695709041462e-03,
        3.224671290700398e-01,
        2.445134137142996e00,
        3.754408661907416e00,
    )
    low, high = 0.02425, 1.0 - 0.02425

    if probability < low:
        q = math.sqrt(-2.0 * math.log(probability))
        x = (((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
        )
    elif probability <= high:
        q = probability - 0.5
        r = q * q
        x = (
            (((((a[0] * r + a[1]) * r + a[2]) * r + a[3]) * r + a[4]) * r + a[5])
            * q
            / (((((b[0] * r + b[1]) * r + b[2]) * r + b[3]) * r + b[4]) * r + 1.0)
        )
    else:
        q = math.sqrt(-2.0 * math.log(1.0 - probability))
        x = -(((((c[0] * q + c[1]) * q + c[2]) * q + c[3]) * q + c[4]) * q + c[5]) / (
            (((d[0] * q + d[1]) * q + d[2]) * q + d[3]) * q + 1.0
        )

    # One Halley refinement, which takes the approximation to machine precision.
    #
    # The residual is F(x) - p, but computing it that way loses every digit in
    # the far upper tail, where F(x) and p are both within 1e-9 of one. Above
    # the median it is computed from the complement instead: (1 - p) is exact
    # for p >= 0.5, and the upper tail 0.5*erfc(x/sqrt(2)) is small, so the two
    # small numbers subtract cleanly. That is the whole difference between
    # nine correct digits and sixteen at a 1e-9 tail.
    root_two = math.sqrt(2.0)
    if probability > 0.5:
        error = (1.0 - probability) - 0.5 * math.erfc(x / root_two)
    else:
        error = 0.5 * math.erfc(-x / root_two) - probability

    density = math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)
    if density > 0.0:
        u = error / density
        x -= u / (1.0 + 0.5 * x * u)
    return x


def scale_to_horizon(value: float, from_days: float, to_days: float) -> float:
    """Square-root-of-time scaling.

    Valid only for independent, identically distributed increments with no
    drift. It is offered because it is the market convention, and every response
    that uses it says so: an option book's risk does not scale by sqrt(t),
    because its Greeks change as the underlying moves.
    """
    if from_days <= 0 or to_days <= 0:
        raise ValueError("horizons must be positive")
    return value * math.sqrt(to_days / from_days)


def bootstrap_interval(
    losses: Sequence[float] | np.ndarray,
    confidence: float,
    seed: int,
    resamples: int = 500,
    interval: float = 0.90,
) -> tuple[float, float]:
    """A confidence interval for the VaR estimate itself.

    Reported instead of an asymptotic standard error because that formula needs
    the density at the quantile, and estimating a density from the same thin
    tail whose uncertainty is in question is circular. Resampling makes no such
    assumption.
    """
    sample = np.asarray(losses, dtype=float)
    n = sample.size
    if n == 0:
        raise ValueError("a bootstrap needs at least one observation")

    rng = np.random.default_rng(seed)
    draws = rng.integers(0, n, size=(resamples, n))
    estimates = np.quantile(sample[draws], confidence, axis=1, method="linear")
    tail = (1.0 - interval) / 2.0
    return (
        float(np.quantile(estimates, tail)),
        float(np.quantile(estimates, 1.0 - tail)),
    )


def _check_confidence(confidence: float) -> None:
    if not 0.0 < confidence < 1.0:
        raise ValueError(f"confidence must be in (0, 1), got {confidence}")
