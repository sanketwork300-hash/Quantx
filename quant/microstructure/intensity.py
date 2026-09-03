"""Event-arrival intensity: a Poisson baseline and a Hawkes alternative.

The reason both are here is the gate. A self-exciting model will always fit an
order-flow tape better *in sample* than a constant rate — it has two more
parameters and order flow does cluster — and reporting that improvement as
evidence would be reporting the extra parameters. So the two models are fitted
on the same training window, scored on the same held-out window, and the
Hawkes result is only ever presented when it wins the held-out comparison. That
is what :func:`compare_held_out` is for, and it is the whole reason this module
returns a comparison rather than a fit.

And "wins" is not "scores one nat more in total". On genuinely Poisson data the
self-exciting model wins the raw held-out total about as often as it loses it,
by margins of a hundredth of a nat — noise with a sign. So the comparison
decomposes the held-out likelihood into one predictive contribution per event
and asks whether the *mean* contribution is positive by more than its own
sampling error, using a one-sided Diebold-Mariano test with a Newey-West
variance (Diebold & Mariano 1995; Newey & West 1987). The HAC variance is not
optional decoration: consecutive contributions from a clustered point process
are serially correlated, and an i.i.d. standard error would understate their
spread and adopt the richer model on noise, which is the exact failure this
gate exists to prevent.

**Model.** Univariate Hawkes with an exponential kernel (Hawkes 1971):

    lambda(t) = mu + sum_{t_i < t} alpha * exp(-beta * (t - t_i))

with background rate ``mu > 0``, jump ``alpha > 0`` and decay ``beta > 0``. The
branching ratio ``n = alpha / beta`` is the expected number of children per
event; the process is stationary only for ``n < 1``, and its long-run rate is
``mu / (1 - n)``.

**Stationarity is structural, not checked afterwards.** The optimiser works in
``(log mu, logit n, log beta)`` and reconstructs ``alpha = n * beta``, so every
point it can evaluate — including every point it might stop at — already
satisfies ``0 < n < 1``. This follows the same rule as the Phase 2 SVI
calibration: a constraint that matters belongs in the feasible set rather than
in a post-hoc rejection, because a rejected optimum leaves nothing to report.

**Log-likelihood** on ``[0, T]`` for events ``t_1 < ... < t_N``:

    l = sum_i log(lambda(t_i)) - mu*T - (alpha/beta) * sum_i (1 - exp(-beta*(T - t_i)))

evaluated with Ogata's recursion ``R_i = exp(-beta*(t_i - t_{i-1})) * (1 + R_{i-1})``,
which makes it linear rather than quadratic in the event count.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum

import numpy as np
from scipy.optimize import minimize

__all__ = [
    "DIEBOLD_MARIANO_CRITICAL_VALUE",
    "HawkesParameters",
    "HeldOutComparison",
    "IntensityFit",
    "IntensityRefusal",
    "PoissonParameters",
    "PredictiveComparison",
    "compare_held_out",
    "fit_hawkes",
    "fit_poisson",
    "hawkes_log_likelihood",
    "held_out_predictive_gains",
    "poisson_log_likelihood",
    "predictive_comparison",
    "simulate_hawkes",
    "time_rescaling_residuals",
]

MODEL_VERSION = "intensity@1.0.0"

#: Below this many events in the training window there is nothing to fit: three
#: parameters cannot be identified from a handful of arrivals, and a Hawkes fit
#: on ten points will happily report a branching ratio of 0.9 that means only
#: that two of them happened to be close together.
MIN_TRAINING_EVENTS = 50

#: And below this many in the held-out window the comparison itself is noise.
MIN_HELD_OUT_EVENTS = 20


class IntensityRefusal(StrEnum):
    """Why an intensity model could not be fitted or compared."""

    TOO_FEW_TRAINING_EVENTS = "TOO_FEW_TRAINING_EVENTS"
    TOO_FEW_HELD_OUT_EVENTS = "TOO_FEW_HELD_OUT_EVENTS"
    NON_POSITIVE_WINDOW = "NON_POSITIVE_WINDOW"
    OPTIMISER_DID_NOT_CONVERGE = "OPTIMISER_DID_NOT_CONVERGE"
    EVENTS_OUTSIDE_WINDOW = "EVENTS_OUTSIDE_WINDOW"


class IntensityUnavailable(Exception):
    """Raised for a request the data cannot support. Carries a closed reason."""

    def __init__(self, reason: IntensityRefusal, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class PoissonParameters:
    """A constant rate. One parameter, and the thing Hawkes has to beat."""

    rate: float

    def to_dict(self) -> dict:
        return {"model": "POISSON", "rate": self.rate}


@dataclass(frozen=True, slots=True)
class HawkesParameters:
    mu: float
    alpha: float
    beta: float

    @property
    def branching_ratio(self) -> float:
        """``n = alpha / beta``: the expected number of children per event."""
        return self.alpha / self.beta

    @property
    def is_stationary(self) -> bool:
        return self.branching_ratio < 1.0

    @property
    def stationary_rate(self) -> float | None:
        """``mu / (1 - n)``, the long-run average intensity. ``None`` if explosive."""
        if not self.is_stationary:
            return None
        return self.mu / (1.0 - self.branching_ratio)

    @property
    def half_life_seconds(self) -> float:
        """How long half of one event's excitation survives: ``ln 2 / beta``."""
        return math.log(2.0) / self.beta

    def to_dict(self) -> dict:
        return {
            "model": "HAWKES_EXPONENTIAL",
            "mu": self.mu,
            "alpha": self.alpha,
            "beta": self.beta,
            "branching_ratio": self.branching_ratio,
            "stationary_rate": self.stationary_rate,
            "excitation_half_life_seconds": self.half_life_seconds,
        }


@dataclass(frozen=True, slots=True)
class IntensityFit:
    """A fitted model with the window it was fitted on and how it went."""

    parameters: PoissonParameters | HawkesParameters
    log_likelihood: float
    events: int
    window_seconds: float
    converged: bool
    iterations: int
    #: Kolmogorov-Smirnov statistic of the time-rescaled inter-event times
    #: against Exp(1). A diagnostic, reported rather than acted on: it says how
    #: far the fitted model is from describing its own training data.
    ks_statistic: float | None = None
    message: str = ""

    @property
    def parameter_count(self) -> int:
        return 1 if isinstance(self.parameters, PoissonParameters) else 3

    @property
    def log_likelihood_per_event(self) -> float | None:
        return self.log_likelihood / self.events if self.events else None

    def to_dict(self) -> dict:
        return {
            "parameters": self.parameters.to_dict(),
            "log_likelihood": self.log_likelihood,
            "log_likelihood_per_event": self.log_likelihood_per_event,
            "events": self.events,
            "window_seconds": self.window_seconds,
            "converged": self.converged,
            "iterations": self.iterations,
            "parameter_count": self.parameter_count,
            "ks_statistic": self.ks_statistic,
            "message": self.message,
        }


# ------------------------------------------------------------- likelihoods
def _as_sorted_array(times) -> np.ndarray:
    array = np.asarray(list(times), dtype=float)
    if array.size and not np.all(np.diff(array) >= 0.0):
        array = np.sort(array)
    return array


#: ``exp`` overflows above roughly 709 in float64. Chunking the excitation sum
#: so no intermediate exponent exceeds this keeps the vectorised recursion exact
#: where it matters: the terms the chunking drops are the ones already smaller
#: than float64 can represent relative to the sum they would join.
_EXPONENT_LIMIT = 400.0


def _excitation(times: np.ndarray, beta: float) -> np.ndarray:
    """``R_i = sum_{j < i} exp(-beta * (t_i - t_j))`` for every event.

    Ogata's recursion ``R_i = exp(-beta*dt_i) * (1 + R_{i-1})`` is sequential,
    and a Python loop over it dominates the runtime of a fit — the optimiser
    evaluates the likelihood hundreds of times per start. The identity
    ``sum_{j<i} exp(-beta*(t_i - t_j)) = exp(-beta*t_i) * cumsum(exp(beta*t_j))``
    vectorises it, at the cost of an exponential that overflows over a long
    window; so the series is walked in blocks short enough for the exponent to
    stay finite, carrying the previous block's tail forward exactly.

    ``tests/unit/test_microstructure.py`` checks this against the plain
    recursion, because a fast path that disagrees with the definition is worse
    than a slow one.
    """
    count = times.size
    excitation = np.zeros(count, dtype=float)
    if count < 2 or beta <= 0.0:
        return excitation

    span = _EXPONENT_LIMIT / beta
    start_index = 0
    carry = 0.0  # sum over every event up to the previous block's last event
    while start_index < count:
        stop_index = int(np.searchsorted(times, times[start_index] + span, side="right"))
        stop_index = max(stop_index, start_index + 1)
        block = times[start_index:stop_index]
        scaled = np.exp(beta * (block - block[0]))
        prefix = np.concatenate(([0.0], np.cumsum(scaled)[:-1]))
        inner = prefix / scaled
        if start_index == 0:
            excitation[start_index:stop_index] = inner
        else:
            tail = carry * np.exp(-beta * (block - times[start_index - 1]))
            excitation[start_index:stop_index] = inner + tail
        carry = excitation[stop_index - 1] + 1.0
        start_index = stop_index
    return excitation


def poisson_log_likelihood(times, start: float, end: float, rate: float) -> float:
    """``N log(rate) - rate * (end - start)`` for events inside the window."""
    window = end - start
    if window <= 0.0:
        raise IntensityUnavailable(
            IntensityRefusal.NON_POSITIVE_WINDOW,
            f"a window of {window} seconds contains no time for events to arrive",
        )
    array = _as_sorted_array(times)
    count = int(np.count_nonzero((array > start) & (array <= end)))
    if rate <= 0.0:
        # A zero rate assigns probability zero to any event at all. That is a
        # legitimate answer only for an empty window.
        return 0.0 if count == 0 else -math.inf
    return count * math.log(rate) - rate * window


def hawkes_log_likelihood(
    times,
    start: float,
    end: float,
    parameters: HawkesParameters,
) -> float:
    """Exact log-likelihood on ``(start, end]``, conditioned on earlier events.

    Events at or before ``start`` are *history*: they contribute to the
    intensity at later points and to the compensator over the window, but their
    own arrivals are not scored. That is what makes an honest held-out
    evaluation possible — the alternative, restarting the process at the split
    point, would throw away exactly the excitation the model claims exists and
    would score the Hawkes model on a window where it has been crippled.
    """
    window = end - start
    if window <= 0.0:
        raise IntensityUnavailable(
            IntensityRefusal.NON_POSITIVE_WINDOW,
            f"a window of {window} seconds contains no time for events to arrive",
        )
    mu, alpha, beta = parameters.mu, parameters.alpha, parameters.beta
    if mu <= 0.0 or alpha < 0.0 or beta <= 0.0:
        return -math.inf

    array = _as_sorted_array(times)
    array = array[array <= end]
    if array.size == 0:
        return -mu * window

    # Compensator over (start, end]: every event before `end` contributes the
    # part of its own decaying excitation that falls inside the window.
    reference = np.maximum(array, start)
    compensator = mu * window + (alpha / beta) * float(
        np.sum(np.exp(-beta * (reference - array)) - np.exp(-beta * (end - array)))
    )

    excitation = _excitation(array, beta)
    scored = array > start
    if not np.any(scored):
        return -compensator
    intensities = mu + alpha * excitation[scored]
    if np.any(intensities <= 0.0):
        return -math.inf
    return float(np.sum(np.log(intensities))) - compensator


def time_rescaling_residuals(times, start: float, end: float, parameters) -> np.ndarray:
    """Compensator increments between consecutive events.

    By the time-rescaling theorem these are i.i.d. Exp(1) if the model is
    right, which is what makes the KS statistic on them a real diagnostic
    rather than a restatement of the likelihood.
    """
    array = _as_sorted_array(times)
    array = array[(array > start) & (array <= end)]
    if array.size < 2:
        return np.empty(0, dtype=float)

    gaps = np.diff(array, prepend=start)
    if isinstance(parameters, PoissonParameters):
        return parameters.rate * gaps

    mu, alpha, beta = parameters.mu, parameters.alpha, parameters.beta
    # The excitation carried into each gap is the one standing just after the
    # previous event, which is that event's own contribution of 1 plus what it
    # inherited. Events before ``start`` are not carried: these residuals are a
    # diagnostic of the window they are computed on.
    inherited = np.concatenate(([0.0], _excitation(array, beta)[:-1] + 1.0))
    return mu * gaps + (alpha / beta) * inherited * (1.0 - np.exp(-beta * gaps))


def _ks_against_unit_exponential(residuals: np.ndarray) -> float | None:
    if residuals.size < 2:
        return None
    ordered = np.sort(residuals)
    empirical = np.arange(1, ordered.size + 1) / ordered.size
    theoretical = 1.0 - np.exp(-ordered)
    below = np.abs(empirical - theoretical)
    above = np.abs(theoretical - (np.arange(ordered.size) / ordered.size))
    return float(max(below.max(), above.max()))


# ------------------------------------------------------------------- fitting
def fit_poisson(times, start: float, end: float) -> IntensityFit:
    """The maximum-likelihood constant rate, which is just ``N / T``.

    Closed form, so there is no optimiser and nothing to converge. It is here
    to be the baseline: any model with more parameters has to beat this one
    out of sample before it is worth reporting.
    """
    window = end - start
    if window <= 0.0:
        raise IntensityUnavailable(
            IntensityRefusal.NON_POSITIVE_WINDOW,
            f"a window of {window} seconds contains no time for events to arrive",
        )
    array = _as_sorted_array(times)
    count = int(np.count_nonzero((array > start) & (array <= end)))
    rate = count / window
    parameters = PoissonParameters(rate=rate)
    return IntensityFit(
        parameters=parameters,
        log_likelihood=poisson_log_likelihood(array, start, end, rate),
        events=count,
        window_seconds=window,
        converged=True,
        iterations=0,
        ks_statistic=_ks_against_unit_exponential(
            time_rescaling_residuals(array, start, end, parameters)
        ),
        message="closed-form maximum likelihood",
    )


#: Deterministic multi-start. Branching ratio and decay are the two directions
#: the surface is genuinely multi-modal in, so the starts span both rather than
#: perturbing one point. Fixed, so a refit of the same data is the same fit.
_STARTS: tuple[tuple[float, float], ...] = (
    (0.30, 1.0),
    (0.30, 0.1),
    (0.60, 5.0),
    (0.60, 0.5),
    (0.85, 20.0),
    (0.10, 2.0),
)


def _unpack(vector: np.ndarray) -> HawkesParameters:
    """``(log mu, logit n, log beta)`` -> parameters, with ``alpha = n * beta``.

    The logit is what makes ``0 < n < 1`` structural: there is no vector the
    optimiser can propose that describes an explosive process, so stationarity
    never has to be enforced by rejecting a converged answer.
    """
    mu = math.exp(float(np.clip(vector[0], -30.0, 30.0)))
    ratio = 1.0 / (1.0 + math.exp(-float(np.clip(vector[1], -30.0, 30.0))))
    beta = math.exp(float(np.clip(vector[2], -30.0, 30.0)))
    return HawkesParameters(mu=mu, alpha=ratio * beta, beta=beta)


def fit_hawkes(
    times,
    start: float,
    end: float,
    min_events: int = MIN_TRAINING_EVENTS,
) -> IntensityFit:
    """Maximum likelihood for the exponential-kernel Hawkes process.

    Deterministic multi-start L-BFGS-B over the reparametrised surface. The
    likelihood is multi-modal in the decay, and a single start lands on a
    background-only solution often enough that reporting it would be reporting
    the optimiser rather than the data.

    Refuses below ``min_events``: three parameters are not identifiable from a
    short burst, and the fit that comes back from one is a description of that
    burst's accidents.
    """
    window = end - start
    if window <= 0.0:
        raise IntensityUnavailable(
            IntensityRefusal.NON_POSITIVE_WINDOW,
            f"a window of {window} seconds contains no time for events to arrive",
        )
    array = _as_sorted_array(times)
    inside = array[(array > start) & (array <= end)]
    if inside.size < min_events:
        raise IntensityUnavailable(
            IntensityRefusal.TOO_FEW_TRAINING_EVENTS,
            f"{inside.size} events in the training window; {min_events} is the "
            "minimum at which a background rate, a jump and a decay are "
            "separately identifiable. Fewer than that fits the accidents of a "
            "short burst.",
        )

    base_rate = inside.size / window

    def objective(vector: np.ndarray) -> float:
        value = hawkes_log_likelihood(array, start, end, _unpack(vector))
        # L-BFGS-B cannot step out of an infinity, so an inadmissible point is
        # a large finite penalty rather than a wall.
        return 1e12 if not math.isfinite(value) else -value

    best: tuple[float, np.ndarray, int, bool, str] | None = None
    for ratio, beta in _STARTS:
        # Start the background at the share of the observed rate the branching
        # ratio leaves for it, so every start describes the same total rate.
        mu_start = max(base_rate * (1.0 - ratio), 1e-9)
        guess = np.array(
            [math.log(mu_start), math.log(ratio / (1.0 - ratio)), math.log(beta)],
            dtype=float,
        )
        result = minimize(objective, guess, method="L-BFGS-B")
        score = float(result.fun)
        if best is None or score < best[0]:
            best = (
                score,
                np.asarray(result.x, dtype=float),
                int(result.nit),
                bool(result.success),
                str(result.message),
            )

    assert best is not None
    score, vector, iterations, converged, message = best
    parameters = _unpack(vector)
    log_likelihood = -score if score < 1e11 else -math.inf
    return IntensityFit(
        parameters=parameters,
        log_likelihood=log_likelihood,
        events=int(inside.size),
        window_seconds=window,
        converged=converged and math.isfinite(log_likelihood),
        iterations=iterations,
        ks_statistic=_ks_against_unit_exponential(
            time_rescaling_residuals(array, start, end, parameters)
        ),
        message=message,
    )


# ---------------------------------------------- predictive decomposition
#: One-sided critical value at 5%. Stated as a constant because the number a
#: gate turns on should be visible rather than buried inside a call.
DIEBOLD_MARIANO_CRITICAL_VALUE = 1.645


def _newey_west_variance(sample: np.ndarray, lags: int) -> float:
    """Long-run variance of a sample mean, allowing for serial correlation.

    Bartlett kernel, with the usual ``floor(4 * (n/100)^(2/9))`` bandwidth rule
    supplied by the caller. Clustered arrivals make consecutive predictive
    contributions dependent, and the i.i.d. variance is too small by exactly
    the amount that dependence contributes.
    """
    count = sample.size
    if count < 2:
        return float("nan")
    centred = sample - sample.mean()
    lags = max(0, min(lags, count - 1))

    variance = float(np.dot(centred, centred) / count)
    for lag in range(1, lags + 1):
        covariance = float(np.dot(centred[lag:], centred[:-lag]) / count)
        variance += 2.0 * (1.0 - lag / (lags + 1.0)) * covariance
    if variance <= 0.0:
        # A Bartlett estimate can come out non-positive on a short, strongly
        # negatively autocorrelated sample. Falling back to the i.i.d. variance
        # is conservative: it is the smaller denominator only in the cases where
        # the correction was going to shrink the statistic anyway.
        variance = float(np.dot(centred, centred) / count)
    return variance


@dataclass(frozen=True, slots=True)
class PredictiveComparison:
    """One-sided Diebold-Mariano test on the per-event predictive gain."""

    #: Mean per-event held-out log-likelihood of Hawkes minus that of Poisson.
    mean_gain: float
    standard_error: float
    statistic: float
    critical_value: float
    events: int
    lags: int
    #: The part of the held-out likelihood difference falling after the last
    #: event. It belongs to the total but is not an observation to test.
    tail_gain: float

    @property
    def is_significant(self) -> bool:
        return math.isfinite(self.statistic) and self.statistic > self.critical_value

    def to_dict(self) -> dict:
        return {
            "test": "one-sided Diebold-Mariano on per-event predictive log-likelihood",
            "variance_estimator": "Newey-West (Bartlett kernel)",
            "mean_gain_per_event": self.mean_gain,
            "standard_error": self.standard_error,
            "statistic": self.statistic,
            "critical_value": self.critical_value,
            "significant": self.is_significant,
            "events": self.events,
            "newey_west_lags": self.lags,
            "tail_gain": self.tail_gain,
        }


def held_out_predictive_gains(
    times,
    split: float,
    end: float,
    hawkes: HawkesParameters,
    poisson_rate: float,
) -> tuple[np.ndarray, float]:
    """Per-event held-out log-likelihood gains, and the post-last-event tail.

    A point-process likelihood decomposes sequentially: each event contributes
    ``log lambda(t_k) - Lambda(t_{k-1}, t_k)``, the log density of waiting
    exactly that long and then arriving. Differencing that between the two
    models gives one predictive comparison per event, and those are what the
    test averages.

    The gains plus the tail sum exactly to the difference of the two window
    log-likelihoods, asserted in the test suite: a decomposition that does not
    add back up is not a decomposition of anything.
    """
    array = _as_sorted_array(times)
    array = array[array <= end]
    inside_mask = array > split
    inside = array[inside_mask]

    mu, alpha, beta = hawkes.mu, hawkes.alpha, hawkes.beta
    excitation = _excitation(array, beta)

    if inside.size:
        first = int(np.argmax(inside_mask))
        # Excitation standing at the left edge of each inter-event interval,
        # including that boundary event's own unit contribution. The first
        # interval starts at the split, so its carried excitation is the last
        # training event's, decayed to the split.
        if first == 0:
            carried = 0.0
        else:
            carried = (excitation[first - 1] + 1.0) * math.exp(
                -beta * (split - array[first - 1])
            )
        standing = np.concatenate(([carried], excitation[first:][:-1] + 1.0))
        gaps = np.diff(inside, prepend=split)
        hawkes_compensator = mu * gaps + (alpha / beta) * standing * (
            1.0 - np.exp(-beta * gaps)
        )
        hawkes_intensity = mu + alpha * excitation[inside_mask]
        with np.errstate(divide="ignore"):
            hawkes_terms = np.log(hawkes_intensity) - hawkes_compensator
            poisson_log_rate = math.log(poisson_rate) if poisson_rate > 0.0 else -math.inf
        poisson_terms = poisson_log_rate - poisson_rate * gaps
        gains = np.asarray(hawkes_terms - poisson_terms, dtype=float)
        last = float(inside[-1])
    else:
        gains = np.empty(0, dtype=float)
        last = split

    # The stretch after the final event, where neither model scores an arrival
    # and only the survival terms differ.
    tail_length = end - last
    if array.size:
        reference = np.maximum(array, last)
        hawkes_tail = mu * tail_length + (alpha / beta) * float(
            np.sum(np.exp(-beta * (reference - array)) - np.exp(-beta * (end - array)))
        )
    else:
        hawkes_tail = mu * tail_length
    return gains, float(-hawkes_tail + poisson_rate * tail_length)


def predictive_comparison(
    times,
    split: float,
    end: float,
    hawkes: HawkesParameters,
    poisson_rate: float,
    critical_value: float = DIEBOLD_MARIANO_CRITICAL_VALUE,
) -> PredictiveComparison:
    """Is the mean per-event predictive gain positive beyond its own noise?"""
    gains, tail = held_out_predictive_gains(times, split, end, hawkes, poisson_rate)
    count = gains.size
    if count < 2 or not bool(np.all(np.isfinite(gains))):
        return PredictiveComparison(
            mean_gain=float(gains.mean()) if count else float("nan"),
            standard_error=float("nan"),
            statistic=float("nan"),
            critical_value=critical_value,
            events=count,
            lags=0,
            tail_gain=tail,
        )
    lags = max(0, min(int(math.floor(4.0 * (count / 100.0) ** (2.0 / 9.0))), count - 1))
    variance = _newey_west_variance(gains, lags)
    standard_error = math.sqrt(variance / count) if variance > 0.0 else float("nan")
    mean_gain = float(gains.mean())
    statistic = (
        mean_gain / standard_error
        if math.isfinite(standard_error) and standard_error > 0.0
        else float("nan")
    )
    return PredictiveComparison(
        mean_gain=mean_gain,
        standard_error=standard_error,
        statistic=statistic,
        critical_value=critical_value,
        events=count,
        lags=lags,
        tail_gain=tail,
    )


# ------------------------------------------------------------- the gate
@dataclass(frozen=True, slots=True)
class HeldOutComparison:
    """The comparison that decides whether a Hawkes fit may be reported.

    ``hawkes_is_adopted`` is the gate. It is not "the Hawkes model is true"; it
    is "on this dataset, fitted on the first part and scored on the second, the
    self-exciting model assigned more likelihood to data it had not seen than
    the constant rate did". When it is false the platform reports the Poisson
    baseline and says the Hawkes fit did not earn its parameters here.
    """

    split_timestamp: float
    train_start: float
    train_end: float
    test_end: float
    poisson_train: IntensityFit
    hawkes_train: IntensityFit
    poisson_held_out_log_likelihood: float
    hawkes_held_out_log_likelihood: float
    held_out_events: int
    predictive: PredictiveComparison
    hawkes_is_adopted: bool
    reason: str

    @property
    def log_likelihood_gain(self) -> float:
        """Held-out log-likelihood of Hawkes minus that of the Poisson baseline."""
        return self.hawkes_held_out_log_likelihood - self.poisson_held_out_log_likelihood

    @property
    def log_likelihood_gain_per_event(self) -> float | None:
        if not self.held_out_events:
            return None
        return self.log_likelihood_gain / self.held_out_events

    def to_dict(self) -> dict:
        return {
            "split_timestamp": self.split_timestamp,
            "train_window_seconds": self.train_end - self.train_start,
            "test_window_seconds": self.test_end - self.train_end,
            "held_out_events": self.held_out_events,
            "poisson": {
                "train": self.poisson_train.to_dict(),
                "held_out_log_likelihood": self.poisson_held_out_log_likelihood,
            },
            "hawkes": {
                "train": self.hawkes_train.to_dict(),
                "held_out_log_likelihood": self.hawkes_held_out_log_likelihood,
            },
            "log_likelihood_gain": self.log_likelihood_gain,
            "log_likelihood_gain_per_event": self.log_likelihood_gain_per_event,
            "predictive_test": self.predictive.to_dict(),
            "hawkes_is_adopted": self.hawkes_is_adopted,
            "reason": self.reason,
            "method": (
                "both models fitted on the training window only and scored on the "
                "held-out window, with the training events carried in as history "
                "so the Hawkes excitation is not reset at the split; adoption "
                "requires the mean per-event predictive gain to be positive by "
                "more than its own Newey-West standard error at the stated "
                "critical value, not merely to have a positive total"
            ),
        }


def compare_held_out(
    times,
    start: float,
    end: float,
    train_fraction: float = 0.7,
    min_training_events: int = MIN_TRAINING_EVENTS,
    min_held_out_events: int = MIN_HELD_OUT_EVENTS,
    critical_value: float = DIEBOLD_MARIANO_CRITICAL_VALUE,
) -> HeldOutComparison:
    """Fit both models on the first part of the window and score the rest.

    The split is by *time*, not by event count, so the held-out window is a
    stretch of market the models genuinely did not see. Splitting by count
    would put the busiest period on whichever side happened to have more
    events and make the comparison a statement about that.

    Raises :class:`IntensityUnavailable` when either side of the split is too
    thin to carry the comparison. A gate that answers on any input is not a
    gate.
    """
    if not 0.0 < train_fraction < 1.0:
        raise ValueError(f"train_fraction must lie strictly inside (0, 1): {train_fraction}")
    window = end - start
    if window <= 0.0:
        raise IntensityUnavailable(
            IntensityRefusal.NON_POSITIVE_WINDOW,
            f"a window of {window} seconds contains no time for events to arrive",
        )

    array = _as_sorted_array(times)
    split = start + train_fraction * window
    held_out = int(np.count_nonzero((array > split) & (array <= end)))
    if held_out < min_held_out_events:
        raise IntensityUnavailable(
            IntensityRefusal.TOO_FEW_HELD_OUT_EVENTS,
            f"{held_out} events in the held-out window; {min_held_out_events} is "
            "the minimum at which the comparison says anything. Below it, which "
            "model wins is decided by one or two arrivals.",
        )

    poisson_train = fit_poisson(array, start, split)
    hawkes_train = fit_hawkes(array, start, split, min_events=min_training_events)

    poisson_rate = poisson_train.parameters.rate  # type: ignore[union-attr]
    hawkes_parameters = hawkes_train.parameters  # type: ignore[assignment]
    poisson_test = poisson_log_likelihood(array, split, end, poisson_rate)
    hawkes_test = hawkes_log_likelihood(array, split, end, hawkes_parameters)  # type: ignore[arg-type]
    predictive = predictive_comparison(
        array, split, end, hawkes_parameters, poisson_rate, critical_value  # type: ignore[arg-type]
    )

    adopted = bool(
        hawkes_train.converged
        and math.isfinite(hawkes_test)
        and predictive.is_significant
    )
    if not hawkes_train.converged:
        reason = (
            "The Hawkes fit did not converge on the training window, so it is "
            "not reported and the constant-rate baseline stands."
        )
    elif not math.isfinite(hawkes_test):
        reason = (
            "The fitted Hawkes model assigns zero density to the held-out "
            "window, so it is not reported and the constant-rate baseline stands."
        )
    elif adopted:
        reason = (
            f"On {predictive.events} held-out events the Hawkes model assigned "
            f"{predictive.mean_gain:.4f} more log-likelihood per event than the "
            f"constant rate, which is {predictive.statistic:.2f} standard errors "
            f"above zero against a threshold of {predictive.critical_value:.3f}. "
            "The clustering it describes is present in data it was not fitted on."
        )
    elif not math.isfinite(predictive.statistic):
        reason = (
            "The held-out window does not support the comparison: the per-event "
            "predictive gains have no usable variance, so nothing distinguishes "
            "the two models and the constant-rate baseline is what is reported."
        )
    else:
        reason = (
            f"On {predictive.events} held-out events the Hawkes model's mean "
            f"predictive gain over the constant rate was {predictive.mean_gain:.4f} "
            f"per event, {predictive.statistic:.2f} standard errors from zero "
            f"against a threshold of {predictive.critical_value:.3f}. That is "
            "within the noise of the comparison, so its extra parameters have not "
            "been shown to describe anything the constant rate does not, and the "
            "baseline is what is reported."
        )

    return HeldOutComparison(
        split_timestamp=split,
        train_start=start,
        train_end=split,
        test_end=end,
        poisson_train=poisson_train,
        hawkes_train=hawkes_train,
        poisson_held_out_log_likelihood=poisson_test,
        hawkes_held_out_log_likelihood=hawkes_test,
        held_out_events=held_out,
        predictive=predictive,
        hawkes_is_adopted=adopted,
        reason=reason,
    )


# --------------------------------------------------------------- simulation
def simulate_hawkes(
    parameters: HawkesParameters,
    horizon: float,
    rng: np.random.Generator,
    max_events: int = 1_000_000,
) -> np.ndarray:
    """Ogata's thinning algorithm for the exponential-kernel process.

    Present so the estimator can be checked against a process whose parameters
    are known, which is the only way to test a maximum-likelihood fit without
    an oracle implementation. It is a validation instrument: nothing it
    produces describes a market, and no result derived from it may be presented
    as a measurement of one.
    """
    if horizon <= 0.0:
        raise ValueError(f"a non-positive horizon simulates nothing: {horizon}")
    if not parameters.is_stationary:
        raise ValueError(
            f"branching ratio {parameters.branching_ratio:.3f} is not below one; "
            "the process is explosive and has no stationary simulation"
        )

    mu, alpha, beta = parameters.mu, parameters.alpha, parameters.beta
    events: list[float] = []
    moment = 0.0
    excitation = 0.0

    while len(events) < max_events:
        # The intensity just after the current point bounds it everywhere after,
        # because the exponential kernel only decays between events.
        bound = mu + excitation
        if bound <= 0.0:
            break
        step = float(rng.exponential(1.0 / bound))
        moment += step
        if moment > horizon:
            break
        excitation *= math.exp(-beta * step)
        if rng.random() <= (mu + excitation) / bound:
            events.append(moment)
            excitation += alpha

    return np.asarray(events, dtype=float)
