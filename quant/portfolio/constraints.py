"""What a portfolio is allowed to be.

Constraints are declared as data rather than assembled inline, for two reasons.
A solver needs them as functions and a human needs them as a description, and
generating both from one object means the description cannot drift from what was
actually imposed. And when a problem is **infeasible**, the answer has to name
which constraints could not be met together — "no solution" is a useless message
when six constraints are in play.

Nothing here is defaulted to a plausible limit. A portfolio with no stated gross
exposure limit has no gross exposure limit; inventing 1.0 because it is the
common case would silently change what a user asked for.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import numpy as np

#: Weights closer than this to a bound are reported as binding. Not a tolerance
#: on the solve — a threshold for saying "this constraint shaped the answer",
#: which is what a user needs to know when a result looks odd.
BINDING_TOLERANCE = 1e-6


class InfeasibleProblem(ValueError):
    """The constraints cannot all be satisfied at once.

    Carries the diagnosis rather than only the fact. A solver that returns
    "failed" leaves the user to guess which of their limits is the impossible
    one, and the guess is usually wrong.
    """

    def __init__(self, reasons: Sequence[str]) -> None:
        super().__init__("the constraints cannot all be satisfied: " + "; ".join(reasons))
        self.reasons = tuple(reasons)


@dataclass(frozen=True, slots=True)
class GroupLimit:
    """A bound on the summed weight of a named set of assets.

    Sector limits are the usual case, but the group is any index list — the
    optimiser does not know what a sector is and does not need to.
    """

    name: str
    indices: tuple[int, ...]
    minimum: float | None = None
    maximum: float | None = None

    def exposure(self, weights: np.ndarray) -> float:
        return float(np.sum(weights[list(self.indices)]))

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "assets": len(self.indices),
            "minimum": self.minimum,
            "maximum": self.maximum,
        }


@dataclass(frozen=True, slots=True)
class Constraints:
    """The feasible set, described once and used by both solver and report."""

    #: Number of assets. Carried so bounds can be validated before a solve.
    size: int
    #: Sum of weights. ``None`` means unconstrained — a long/short book that is
    #: not required to be fully invested.
    budget: float | None = 1.0
    long_only: bool = False
    #: Per-asset bounds. A scalar applies to every asset; a sequence must match
    #: ``size``. ``None`` means unbounded on that side.
    minimum_weight: float | Sequence[float] | None = None
    maximum_weight: float | Sequence[float] | None = None
    #: Sum of absolute weights.
    maximum_gross_exposure: float | None = None
    #: Sum of signed weights, where that is not already fixed by ``budget``.
    minimum_net_exposure: float | None = None
    maximum_net_exposure: float | None = None
    groups: tuple[GroupLimit, ...] = ()
    #: Sum of absolute weight changes from ``current_weights``. Both are needed
    #: together: a turnover limit without a starting point is meaningless.
    maximum_turnover: float | None = None
    current_weights: tuple[float, ...] | None = None

    def __post_init__(self) -> None:
        if self.size <= 0:
            raise ValueError("a portfolio needs at least one asset")
        for name in ("minimum_weight", "maximum_weight"):
            value = getattr(self, name)
            if isinstance(value, Sequence) and len(value) != self.size:
                raise ValueError(f"{name} has {len(value)} entries for {self.size} assets")
        if self.maximum_turnover is not None and self.current_weights is None:
            raise ValueError(
                "a turnover limit needs current_weights; without a starting point "
                "there is nothing to measure turnover against"
            )
        if self.current_weights is not None and len(self.current_weights) != self.size:
            raise ValueError("current_weights does not match the number of assets")

    # ------------------------------------------------------------- bounds
    def bounds(self) -> list[tuple[float | None, float | None]]:
        """Per-asset ``(low, high)`` pairs for the solver."""
        lower = self._per_asset(self.minimum_weight)
        upper = self._per_asset(self.maximum_weight)
        if self.long_only:
            lower = [0.0 if value is None else max(value, 0.0) for value in lower]
        return list(zip(lower, upper, strict=True))

    def _per_asset(self, value) -> list[float | None]:
        if value is None:
            return [None] * self.size
        if isinstance(value, Sequence):
            return [float(item) for item in value]
        return [float(value)] * self.size

    # -------------------------------------------------------- feasibility
    def check_feasible(self) -> None:
        """Catch the contradictions that can be seen without solving.

        Not every infeasibility is visible here — that is what the solver is
        for — but the ones that are should be named immediately rather than
        after thirty seconds of a search that could never succeed.
        """
        reasons: list[str] = []
        lower, upper = zip(*self.bounds(), strict=True)

        for index, (low, high) in enumerate(zip(lower, upper, strict=True)):
            if low is not None and high is not None and low > high:
                reasons.append(
                    f"asset {index} has a minimum weight of {low} above its maximum of {high}"
                )

        if self.budget is not None:
            low_sum = sum(value for value in lower if value is not None)
            if any(value is None for value in lower):
                low_sum = float("-inf")
            high_sum = sum(value for value in upper if value is not None)
            if any(value is None for value in upper):
                high_sum = float("inf")
            if low_sum > self.budget + BINDING_TOLERANCE:
                reasons.append(
                    f"the minimum weights sum to {low_sum:.4f}, above the budget of {self.budget}"
                )
            if high_sum < self.budget - BINDING_TOLERANCE:
                reasons.append(
                    f"the maximum weights sum to {high_sum:.4f}, below the budget of {self.budget}"
                )

        if (
            self.budget is not None
            and self.maximum_gross_exposure is not None
            and abs(self.budget) > self.maximum_gross_exposure + BINDING_TOLERANCE
        ):
            reasons.append(
                f"a budget of {self.budget} cannot fit inside a gross exposure limit "
                f"of {self.maximum_gross_exposure}"
            )

        if (
            self.minimum_net_exposure is not None
            and self.maximum_net_exposure is not None
            and self.minimum_net_exposure > self.maximum_net_exposure
        ):
            reasons.append("the minimum net exposure is above the maximum")

        for group in self.groups:
            if (
                group.minimum is not None
                and group.maximum is not None
                and group.minimum > group.maximum
            ):
                reasons.append(f"group {group.name!r} has a minimum above its maximum")

        if reasons:
            raise InfeasibleProblem(reasons)

    # ----------------------------------------------------------- reporting
    def binding(self, weights: np.ndarray) -> list[str]:
        """Which constraints the solution is sitting on.

        A user looking at an odd-looking portfolio usually wants to know what
        was preventing something else, and this is that answer.
        """
        touching: list[str] = []
        for index, (low, high) in enumerate(self.bounds()):
            if low is not None and abs(weights[index] - low) <= BINDING_TOLERANCE:
                touching.append(f"asset {index} at its minimum weight")
            if high is not None and abs(weights[index] - high) <= BINDING_TOLERANCE:
                touching.append(f"asset {index} at its maximum weight")

        gross = float(np.sum(np.abs(weights)))
        if (
            self.maximum_gross_exposure is not None
            and abs(gross - self.maximum_gross_exposure) <= 1e-4
        ):
            touching.append("gross exposure at its limit")

        net = float(np.sum(weights))
        for bound, label in (
            (self.minimum_net_exposure, "minimum"),
            (self.maximum_net_exposure, "maximum"),
        ):
            if bound is not None and abs(net - bound) <= 1e-4:
                touching.append(f"net exposure at its {label}")

        for group in self.groups:
            exposure = group.exposure(weights)
            if group.maximum is not None and abs(exposure - group.maximum) <= 1e-4:
                touching.append(f"group {group.name!r} at its maximum")
            if group.minimum is not None and abs(exposure - group.minimum) <= 1e-4:
                touching.append(f"group {group.name!r} at its minimum")

        if self.maximum_turnover is not None and self.current_weights is not None:
            turnover = float(np.sum(np.abs(weights - np.asarray(self.current_weights))))
            if abs(turnover - self.maximum_turnover) <= 1e-4:
                touching.append("turnover at its limit")

        return touching

    def scipy_constraints(self) -> list[dict]:
        """The constraint list SLSQP takes.

        Inequalities are written ``g(w) >= 0``, which is scipy's convention and
        the source of a whole family of sign errors when it is not written down.
        """
        constraints: list[dict] = []

        if self.budget is not None:
            budget = float(self.budget)
            constraints.append({"type": "eq", "fun": lambda w: float(np.sum(w)) - budget})

        if self.maximum_gross_exposure is not None:
            limit = float(self.maximum_gross_exposure)
            constraints.append({"type": "ineq", "fun": lambda w: limit - float(np.sum(np.abs(w)))})

        if self.minimum_net_exposure is not None:
            floor = float(self.minimum_net_exposure)
            constraints.append({"type": "ineq", "fun": lambda w: float(np.sum(w)) - floor})

        if self.maximum_net_exposure is not None:
            ceiling = float(self.maximum_net_exposure)
            constraints.append({"type": "ineq", "fun": lambda w: ceiling - float(np.sum(w))})

        for group in self.groups:
            indices = list(group.indices)
            if group.maximum is not None:
                maximum = float(group.maximum)
                constraints.append(
                    {
                        "type": "ineq",
                        "fun": lambda w, i=indices, m=maximum: m - float(np.sum(w[i])),
                    }
                )
            if group.minimum is not None:
                minimum = float(group.minimum)
                constraints.append(
                    {
                        "type": "ineq",
                        "fun": lambda w, i=indices, m=minimum: float(np.sum(w[i])) - m,
                    }
                )

        if self.maximum_turnover is not None and self.current_weights is not None:
            limit = float(self.maximum_turnover)
            current = np.asarray(self.current_weights, dtype=float)
            constraints.append(
                {
                    "type": "ineq",
                    "fun": lambda w: limit - float(np.sum(np.abs(w - current))),
                }
            )

        return constraints

    def to_dict(self) -> dict:
        return {
            "size": self.size,
            "budget": self.budget,
            "long_only": self.long_only,
            "minimum_weight": _describe(self.minimum_weight),
            "maximum_weight": _describe(self.maximum_weight),
            "maximum_gross_exposure": self.maximum_gross_exposure,
            "minimum_net_exposure": self.minimum_net_exposure,
            "maximum_net_exposure": self.maximum_net_exposure,
            "groups": [group.to_dict() for group in self.groups],
            "maximum_turnover": self.maximum_turnover,
        }


def _describe(value) -> object:
    if value is None or isinstance(value, int | float):
        return value
    return [float(item) for item in value]


def from_mapping(size: int, payload: Mapping) -> Constraints:
    """Build constraints from a request payload."""
    groups = tuple(
        GroupLimit(
            name=str(item["name"]),
            indices=tuple(int(index) for index in item["indices"]),
            minimum=_optional_float(item.get("minimum")),
            maximum=_optional_float(item.get("maximum")),
        )
        for item in payload.get("groups") or ()
    )
    current = payload.get("current_weights")
    return Constraints(
        size=size,
        budget=_optional_float(payload.get("budget", 1.0)),
        long_only=bool(payload.get("long_only", False)),
        minimum_weight=payload.get("minimum_weight"),
        maximum_weight=payload.get("maximum_weight"),
        maximum_gross_exposure=_optional_float(payload.get("maximum_gross_exposure")),
        minimum_net_exposure=_optional_float(payload.get("minimum_net_exposure")),
        maximum_net_exposure=_optional_float(payload.get("maximum_net_exposure")),
        groups=groups,
        maximum_turnover=_optional_float(payload.get("maximum_turnover")),
        current_weights=tuple(float(value) for value in current) if current else None,
    )


def _optional_float(value) -> float | None:
    return None if value is None else float(value)
