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


class CovarianceWarning(StrEnum):
    FEW_OBSERVATIONS = "COVARIANCE_FEW_OBSERVATIONS"
    RANK_DEFICIENT = "COVARIANCE_RANK_DEFICIENT"
    ZERO_VARIANCE_FACTOR = "COVARIANCE_ZERO_VARIANCE_FACTOR"


@dataclass(frozen=True, slots=True)
class CovarianceEstimate:
    factors: tuple[str, ...]
    mean: np.ndarray
    covariance: np.ndarray
    observations: int
    estimator: CovarianceEstimator
    warnings: tuple[str, ...] = ()

    @property
    def volatilities(self) -> np.ndarray:
        return np.sqrt(np.clip(np.diag(self.covariance), 0.0, None))

    def to_dict(self) -> dict:
        return {
            "factors": list(self.factors),
            "observations": self.observations,
            "estimator": str(self.estimator),
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
