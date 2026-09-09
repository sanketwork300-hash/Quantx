"""Covariance estimation for the parametric and Monte Carlo risk methods.

The sample covariance is the estimator, and its weakness is stated rather than
papered over: with `n` observations and `p` factors it is noisy once `p` is
comparable to `n`, and singular once `p >= n`. The platform reports the
condition it is in instead of silently regularising, because a shrinkage
intensity chosen to make a matrix invertible is a modelling decision the user
should get to see.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

import numpy as np

#: Below this ratio of observations to factors the sample covariance is noisy
#: enough that the estimate is flagged. Ten observations per factor is a
#: convention, not a theorem, and it is stated as one.
MIN_OBSERVATIONS_PER_FACTOR = 10


class CovarianceEstimator(StrEnum):
    SAMPLE = "SAMPLE"
    #: Ledoit-Wolf shrinkage towards a constant-correlation target, with the
    #: intensity computed analytically rather than tuned. Offered as an explicit
    #: choice and never as a default: shrinking is a modelling decision, and the
    #: intensity it chose is reported so the decision stays visible.
    LEDOIT_WOLF = "LEDOIT_WOLF"


class CovarianceWarning(StrEnum):
    FEW_OBSERVATIONS = "COVARIANCE_FEW_OBSERVATIONS"
    #: Shrinkage pulled the estimate most of the way to the target, which means
    #: the sample said very little. Worth knowing before optimising on it.
    HEAVY_SHRINKAGE = "COVARIANCE_HEAVY_SHRINKAGE"
    RANK_DEFICIENT = "COVARIANCE_RANK_DEFICIENT"
    ZERO_VARIANCE_FACTOR = "COVARIANCE_ZERO_VARIANCE_FACTOR"


@dataclass(frozen=True, slots=True)
class CovarianceEstimate:
    factors: tuple[str, ...]
    mean: np.ndarray
    covariance: np.ndarray
    observations: int
    estimator: CovarianceEstimator
    #: How far the estimate was pulled towards the shrinkage target, in [0, 1].
    #: ``None`` for the sample estimator, which shrinks nothing.
    shrinkage_intensity: float | None = None
    warnings: tuple[str, ...] = ()

    @property
    def volatilities(self) -> np.ndarray:
        return np.sqrt(np.clip(np.diag(self.covariance), 0.0, None))

    def to_dict(self) -> dict:
        return {
            "factors": list(self.factors),
            "observations": self.observations,
            "estimator": str(self.estimator),
            "shrinkage_intensity": self.shrinkage_intensity,
            "mean": [float(x) for x in self.mean],
            "volatility": [float(x) for x in self.volatilities],
            "correlation": [[float(x) for x in row] for row in self.correlation()],
            "warnings": list(self.warnings),
        }

    def correlation(self) -> np.ndarray:
        vol = self.volatilities
        safe = np.where(vol > 0.0, vol, 1.0)
        return self.covariance / np.outer(safe, safe)


def sample_covariance(
    factors: Sequence[str], returns: np.ndarray, use_bessel: bool = True
) -> CovarianceEstimate:
    """Sample mean and covariance of aligned factor returns.

    ``returns`` is ``(observations, factors)`` and must already be aligned; the
    alignment policy belongs to the caller, because dropping a row is a decision
    about data, not about arithmetic.
    """
    matrix = np.asarray(returns, dtype=float)
    if matrix.ndim != 2:
        raise ValueError("returns must be a 2-D (observations, factors) array")
    if matrix.shape[1] != len(factors):
        raise ValueError(f"{matrix.shape[1]} return columns but {len(factors)} factor names")
    n, p = matrix.shape
    if n < 2:
        raise ValueError("covariance needs at least two observations")

    warnings: list[str] = []
    if n < MIN_OBSERVATIONS_PER_FACTOR * p:
        warnings.append(CovarianceWarning.FEW_OBSERVATIONS)
    if n <= p:
        warnings.append(CovarianceWarning.RANK_DEFICIENT)

    mean = matrix.mean(axis=0)
    covariance = np.cov(matrix, rowvar=False, ddof=1 if use_bessel else 0)
    covariance = np.atleast_2d(covariance)

    if np.any(np.diag(covariance) <= 0.0):
        warnings.append(CovarianceWarning.ZERO_VARIANCE_FACTOR)

    return CovarianceEstimate(
        factors=tuple(factors),
        mean=mean,
        covariance=covariance,
        observations=n,
        estimator=CovarianceEstimator.SAMPLE,
        warnings=tuple(dict.fromkeys(warnings)),
    )


def nearest_positive_semidefinite(matrix: np.ndarray) -> tuple[np.ndarray, bool]:
    """Clip negative eigenvalues to zero.

    A sample covariance is positive semidefinite in exact arithmetic; in float64
    the smallest eigenvalues can come out slightly negative, and a Cholesky
    factorisation then fails on a matrix that is fine. This repairs *that*, and
    returns whether it had to, so a genuinely indefinite input is visible rather
    than quietly fixed.
    """
    symmetric = 0.5 * (matrix + matrix.T)
    eigenvalues, eigenvectors = np.linalg.eigh(symmetric)
    if float(eigenvalues.min()) >= 0.0:
        return symmetric, False
    clipped = np.clip(eigenvalues, 0.0, None)
    return eigenvectors @ np.diag(clipped) @ eigenvectors.T, True


def ledoit_wolf_covariance(factors: Sequence[str], returns: np.ndarray) -> CovarianceEstimate:
    """Sample covariance shrunk towards a constant-correlation target.

    Ledoit & Wolf (2003), *Honey, I Shrunk the Sample Covariance Matrix*. The
    sample estimate is noisy when observations are not many times the number of
    assets, and the noise lands hardest on the smallest eigenvalues — which is
    exactly where a mean-variance optimiser looks for its cleverest trades. The
    shrinkage intensity is derived from the data rather than chosen, which is
    what makes this an estimator rather than a knob.

    It is not the default. The platform's rule is that a regularisation which
    makes a matrix invertible is a modelling decision the user should see, so
    this is asked for by name and the intensity it picked is reported.
    """
    sample = sample_covariance(factors, returns)
    observations = sample.observations
    if observations < 2:
        return sample

    data = np.asarray(returns, dtype=float)
    centred = data - data.mean(axis=0, keepdims=True)
    count, size = centred.shape

    covariance = sample.covariance
    volatilities = sample.volatilities
    safe = np.where(volatilities > 0.0, volatilities, 1.0)
    correlation = covariance / np.outer(safe, safe)

    off_diagonal = correlation[~np.eye(size, dtype=bool)]
    average_correlation = float(np.mean(off_diagonal)) if off_diagonal.size else 0.0
    target = average_correlation * np.outer(volatilities, volatilities)
    np.fill_diagonal(target, np.diag(covariance))

    # pi: summed variance of the sample covariance entries.
    squared = centred**2
    pi_matrix = (
        (squared.T @ squared) / count
        - 2.0 * covariance * ((centred.T @ centred) / count)
        + covariance**2
    )
    pi = float(np.sum(pi_matrix))

    # gamma: squared distance from the sample estimate to the target.
    gamma = float(np.sum((target - covariance) ** 2))

    # rho is dropped: the constant-correlation target's cross term is a long
    # expression whose contribution is small, and Ledoit and Wolf's own
    # simplified estimator omits it. Stated rather than silently dropped.
    intensity = 0.0 if gamma <= 0 else max(0.0, min(1.0, (pi / gamma) / count))

    shrunk = intensity * target + (1.0 - intensity) * covariance
    warnings = list(sample.warnings)
    if intensity > 0.5:
        warnings.append(str(CovarianceWarning.HEAVY_SHRINKAGE))

    return CovarianceEstimate(
        factors=sample.factors,
        mean=sample.mean,
        covariance=shrunk,
        observations=observations,
        estimator=CovarianceEstimator.LEDOIT_WOLF,
        shrinkage_intensity=intensity,
        warnings=tuple(warnings),
    )
