"""Seeded factor simulation for Monte Carlo risk.

Two properties matter more than speed here. The draws are **reproducible** from
a seed, so a risk number can be recomputed months later and compared. And the
factor model is **explicit**: a multivariate normal with a stated mean vector
and covariance, not an unnamed process buried in a loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import numpy as np

from quant.statistics.covariance import nearest_positive_semidefinite


class Distribution(StrEnum):
    NORMAL = "NORMAL"
    #: Heavier tails than the normal, with the degrees of freedom stated. Kept
    #: separate rather than made the default: a t with badly chosen degrees of
    #: freedom is not more honest than a normal, only more confident.
    STUDENT_T = "STUDENT_T"


@dataclass(frozen=True, slots=True)
class FactorModel:
    """What is simulated, stated in full so a run can be reproduced."""

    factors: tuple[str, ...]
    #: Per-period mean of each factor's return. Defaults to zero at the caller,
    #: and a non-zero drift is always visible in the recorded model.
    mean: np.ndarray
    covariance: np.ndarray
    distribution: Distribution = Distribution.NORMAL
    #: Only used by STUDENT_T. Below 2 the variance does not exist.
    degrees_of_freedom: float = 5.0

    def to_dict(self) -> dict:
        return {
            "factors": list(self.factors),
            "distribution": str(self.distribution),
            "mean": [float(x) for x in self.mean],
            "volatility": [float(x) for x in np.sqrt(np.clip(np.diag(self.covariance), 0, None))],
            "degrees_of_freedom": (
                self.degrees_of_freedom if self.distribution is Distribution.STUDENT_T else None
            ),
        }


@dataclass(frozen=True, slots=True)
class SimulationResult:
    draws: np.ndarray
    seed: int
    paths: int
    antithetic: bool
    covariance_repaired: bool

    def to_dict(self) -> dict:
        return {
            "paths": self.paths,
            "seed": self.seed,
            "antithetic": self.antithetic,
            "covariance_repaired": self.covariance_repaired,
        }


def simulate_factor_returns(
    model: FactorModel, paths: int, seed: int, antithetic: bool = True
) -> SimulationResult:
    """Draw ``paths`` correlated factor returns.

    Antithetic variates are on by default: for every draw ``z`` the draw ``-z``
    is also used, which removes the odd part of the sampling error exactly and
    makes a given seed's answer noticeably steadier. It requires an even number
    of paths, so an odd request is rounded up rather than silently halved.
    """
    if paths < 2:
        raise ValueError("a Monte Carlo run needs at least two paths")

    factors = len(model.factors)
    covariance, repaired = nearest_positive_semidefinite(np.asarray(model.covariance, float))

    # Cholesky of a semidefinite matrix can fail on a zero eigenvalue, so the
    # eigen-decomposition is used throughout rather than only as a fallback.
    eigenvalues, eigenvectors = np.linalg.eigh(covariance)
    factor_loadings = eigenvectors @ np.diag(np.sqrt(np.clip(eigenvalues, 0.0, None)))

    total = paths + (paths % 2 if antithetic else 0)
    rng = np.random.default_rng(seed)

    if antithetic:
        half = total // 2
        base = rng.standard_normal(size=(half, factors))
        normals = np.vstack([base, -base])
    else:
        normals = rng.standard_normal(size=(total, factors))

    if model.distribution is Distribution.STUDENT_T:
        if model.degrees_of_freedom <= 2.0:
            raise ValueError("Student-t needs more than two degrees of freedom for a variance")
        nu = model.degrees_of_freedom
        # Scaled so the marginal variance matches the covariance supplied,
        # rather than being inflated by nu / (nu - 2) without saying so.
        chi = rng.chisquare(nu, size=(normals.shape[0], 1))
        normals = normals * np.sqrt(nu / chi) / np.sqrt(nu / (nu - 2.0))

    draws = np.asarray(model.mean, float) + normals @ factor_loadings.T
    return SimulationResult(
        draws=draws,
        seed=seed,
        paths=int(draws.shape[0]),
        antithetic=antithetic,
        covariance_repaired=repaired,
    )
