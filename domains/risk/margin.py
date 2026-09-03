"""Margin estimation, and the one thing it is not.

**The platform does not know your broker's margin.** Exchange and broker
methodologies are proprietary, versioned, and change without notice. Every
number here is a model estimate produced by a model this repository defines, and
there is deliberately no field on any result that could be read as "your broker
will require X".

What the model *is*: the worst loss the book takes over a declared grid of
market moves, floored by an optional short-option minimum and increased by an
optional concentration add-on. Every component is a stated assumption travelling
with the answer, and the two optional components default to **zero** — because
the alternative is a plausible-looking rate nobody measured, which is exactly
the failure this module exists to avoid. A zero is visible in the assumptions
and carries a warning explaining what it leaves out; an invented 2% would not.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum

import numpy as np

from domains.risk.exposure import ExposureSet
from domains.risk.revaluation import revalue_many

#: The share of gross notional in one underlying beyond which the concentration
#: add-on starts to apply. A stated convention of this model, not a rule from
#: any venue.
DEFAULT_CONCENTRATION_THRESHOLD = 0.5

SIMPLE_RISK_MODEL_VERSION = "1.0.0"


class MarginWarning(StrEnum):
    NO_SHORT_OPTION_MINIMUM = "MARGIN_NO_SHORT_OPTION_MINIMUM"
    NO_CONCENTRATION_ADD_ON = "MARGIN_NO_CONCENTRATION_ADD_ON"
    WORST_AT_GRID_EDGE = "MARGIN_WORST_LOSS_AT_GRID_EDGE"
    POSITIONS_EXCLUDED = "MARGIN_POSITIONS_EXCLUDED"
    VOLATILITY_HELD_FLAT = "MARGIN_VOLATILITY_HELD_FLAT"
    NO_ELIGIBLE_CAPITAL = "MARGIN_NO_ELIGIBLE_CAPITAL"
    EMPTY_BOOK = "MARGIN_EMPTY_BOOK"


@dataclass(frozen=True, slots=True)
class ShockGrid:
    """The declared grid a scan-based model measures its worst loss over.

    Declared, because the grid *is* the model: a margin number is only ever the
    worst loss over the moves someone chose to look at, and a reader who cannot
    see those moves cannot judge the number.
    """

    spot_returns: tuple[float, ...] = (
        -0.20,
        -0.15,
        -0.10,
        -0.07,
        -0.05,
        -0.03,
        -0.01,
        0.0,
        0.01,
        0.03,
        0.05,
        0.07,
        0.10,
        0.15,
        0.20,
    )
    vol_points: tuple[float, ...] = (-0.05, 0.0, 0.05)

    def __post_init__(self) -> None:
        if not self.spot_returns or not self.vol_points:
            raise ValueError("a shock grid needs at least one point on each axis")
        if 0.0 not in self.spot_returns:
            raise ValueError(
                "a shock grid without an unshocked point cannot show that the "
                "book is flat where the market is"
            )

    @property
    def points(self) -> int:
        return len(self.spot_returns) * len(self.vol_points)

    def is_edge(self, spot_return: float, vol_point: float) -> bool:
        """Whether the worst point sat on a boundary the grid could be widened past.

        An axis with a single point is *not* an edge: there is no wider setting
        of it to explore, and a flat volatility axis is already reported by its
        own warning. Counting it here as well would say the grid was too narrow
        when what happened is that a dimension was deliberately switched off.
        """
        return self._on_edge(spot_return, self.spot_returns) or self._on_edge(
            vol_point, self.vol_points
        )

    @staticmethod
    def _on_edge(value: float, axis: tuple[float, ...]) -> bool:
        return len(axis) > 1 and value in (min(axis), max(axis))

    def to_dict(self) -> dict:
        return {
            "spot_returns": list(self.spot_returns),
            "vol_points": list(self.vol_points),
            "points": self.points,
        }


@dataclass(frozen=True, slots=True)
class MarginParameters:
    """Every knob, with a default that invents nothing.

    ``short_option_minimum_rate`` and ``concentration_add_on_rate`` default to
    zero. A short option far out of the money shows almost no loss on a scan
    grid while carrying unbounded tail risk, and a real margin system floors it
    for that reason — but the *rate* at which it does is a venue's rule, and
    picking a number here would be inventing one. Zero, plus a warning saying
    what it leaves out, is the honest default.
    """

    grid: ShockGrid = field(default_factory=ShockGrid)
    #: Fraction of contract notional (strike x multiplier) charged per short
    #: option, as a floor on the scan loss.
    short_option_minimum_rate: float = 0.0
    #: Fraction of the excess concentration charged as an add-on.
    concentration_add_on_rate: float = 0.0
    concentration_threshold: float = DEFAULT_CONCENTRATION_THRESHOLD

    def __post_init__(self) -> None:
        if self.short_option_minimum_rate < 0.0:
            raise ValueError("the short-option minimum rate cannot be negative")
        if self.concentration_add_on_rate < 0.0:
            raise ValueError("the concentration add-on rate cannot be negative")
        if not 0.0 < self.concentration_threshold <= 1.0:
            raise ValueError("the concentration threshold must be in (0, 1]")

    def to_dict(self) -> dict:
        return {
            "grid": self.grid.to_dict(),
            "short_option_minimum_rate": self.short_option_minimum_rate,
            "concentration_add_on_rate": self.concentration_add_on_rate,
            "concentration_threshold": self.concentration_threshold,
        }


@dataclass(frozen=True, slots=True)
class MarginComponent:
    name: str
    amount: float
    #: What this component is, in a sentence a reader can argue with.
    basis: str

    def to_dict(self) -> dict:
        return {"name": self.name, "amount": self.amount, "basis": self.basis}


@dataclass(frozen=True, slots=True)
class MarginResult:
    """An estimate, its method, and everything that went into it.

    There is no `broker_margin`, no `required_margin`, and no field naming a
    venue. `estimated_margin` is the only number, and its name is the claim.
    """

    method: str
    model_version: str
    estimated_margin: float
    currency: str
    components: tuple[MarginComponent, ...]
    assumptions: tuple[str, ...]
    confidence: float
    parameters: dict
    #: The grid point at which the worst loss occurred, and that loss.
    worst_spot_return: float
    worst_vol_points: float
    worst_loss: float
    worst_at_grid_edge: bool
    positions: int
    excluded_positions: int
    warnings: tuple[str, ...] = ()

    @property
    def disclaimer(self) -> str:
        return (
            "An estimate from the named model under the stated assumptions. It is "
            "not your broker's or your exchange's margin requirement, which this "
            "platform does not have and does not model."
        )

    def to_dict(self) -> dict:
        return {
            "method": self.method,
            "model_version": self.model_version,
            "estimated_margin": self.estimated_margin,
            "currency": self.currency,
            "components": [item.to_dict() for item in self.components],
            "assumptions": list(self.assumptions),
            "confidence": self.confidence,
            "parameters": self.parameters,
            "worst_case": {
                "spot_return": self.worst_spot_return,
                "vol_points": self.worst_vol_points,
                "loss": self.worst_loss,
                "at_grid_edge": self.worst_at_grid_edge,
            },
            "positions": self.positions,
            "excluded_positions": self.excluded_positions,
            "warnings": list(self.warnings),
            "disclaimer": self.disclaimer,
        }


class MarginModel(ABC):
    """A named, versioned way of estimating margin.

    Subclasses exist so that a second methodology can be added without the rest
    of the platform learning about it, and so that every stored result names
    which one produced it. An implementation is only allowed to describe itself
    as a broker's or an exchange's methodology if it implements a *published*
    one; otherwise it is an approximation and says so.
    """

    name: str
    version: str

    @abstractmethod
    def calculate(self, exposures: ExposureSet) -> MarginResult:
        """Estimate margin for a repriceable book."""

    @property
    def identifier(self) -> str:
        return f"{self.name}@{self.version}"


class SimpleRiskMarginModel(MarginModel):
    """Worst loss over a declared grid, floored and topped up by stated rules.

        scan_loss     = max(0, -min P&L over the grid)
        floor         = rate x sum(notional of short options)
        concentration = rate x max(0, largest_underlying_gross - threshold x gross)
        margin        = max(scan_loss, floor) + concentration

    The floor is a *floor* rather than an addition because that is what it is
    for: a book whose scan loss is small only because every short option is far
    out of the money is not a book with no risk. The concentration charge is an
    addition because it is a different statement — about how little the grid's
    single-underlying shocks say when everything is one name.
    """

    name = "SimpleRiskMarginModel"
    version = SIMPLE_RISK_MODEL_VERSION

    def __init__(self, parameters: MarginParameters | None = None) -> None:
        self.parameters = parameters or MarginParameters()

    def calculate(self, exposures: ExposureSet) -> MarginResult:
        grid = self.parameters.grid
        warnings: list[str] = []
        assumptions: list[str] = [
            "Margin is estimated as the worst loss this book takes across a "
            "declared grid of simultaneous moves in every underlying, which is a "
            "model of risk and not any venue's rule.",
            f"The grid spans {min(grid.spot_returns):.0%} to "
            f"{max(grid.spot_returns):.0%} in the underlying and "
            f"{min(grid.vol_points) * 100:+.0f} to {max(grid.vol_points) * 100:+.0f} "
            f"volatility points, {grid.points} points in all.",
            "Every underlying is shocked together, so the estimate assumes "
            "perfect correlation across names and gives no diversification "
            "credit between them.",
        ]

        if not exposures.exposures:
            warnings.append(MarginWarning.EMPTY_BOOK)
            return self._empty(exposures, assumptions, warnings)

        worst_return, worst_vol, worst_loss = self._scan(exposures, grid)
        at_edge = grid.is_edge(worst_return, worst_vol)
        if at_edge and worst_loss > 0.0:
            warnings.append(MarginWarning.WORST_AT_GRID_EDGE)
            assumptions.append(
                "The worst loss occurred at the edge of the grid, so the true "
                "worst case over a wider range of moves is larger than this."
            )

        floor, floor_basis = self._short_option_floor(exposures)
        concentration, concentration_basis = self._concentration(exposures)

        if self.parameters.short_option_minimum_rate == 0.0:
            warnings.append(MarginWarning.NO_SHORT_OPTION_MINIMUM)
            assumptions.append(
                "No short-option minimum was applied. A short option far out of "
                "the money shows almost no loss on this grid while its tail risk "
                "is unbounded, so this estimate understates such a book. The rate "
                "is left at zero because choosing one would be inventing a rule."
            )
        if self.parameters.concentration_add_on_rate == 0.0:
            warnings.append(MarginWarning.NO_CONCENTRATION_ADD_ON)
        if len(grid.vol_points) == 1 and grid.vol_points[0] == 0.0:
            warnings.append(MarginWarning.VOLATILITY_HELD_FLAT)
            assumptions.append(
                "Volatility was held flat across the grid, so a book whose risk "
                "is mostly vega is not measured by this estimate."
            )
        if exposures.excluded:
            warnings.append(MarginWarning.POSITIONS_EXCLUDED)
            assumptions.append(
                f"{len(exposures.excluded)} position(s) could not be repriced and "
                "contribute nothing to this estimate."
            )

        scan = max(0.0, worst_loss)
        components = (
            MarginComponent("scan_loss", scan, f"Worst loss over the {grid.points}-point grid."),
            MarginComponent("short_option_minimum", floor, floor_basis),
            MarginComponent("concentration_add_on", concentration, concentration_basis),
        )
        estimated = max(scan, floor) + concentration

        return MarginResult(
            method=self.identifier,
            model_version=self.version,
            estimated_margin=estimated,
            currency=exposures.base_currency,
            components=components,
            assumptions=tuple(assumptions),
            confidence=self._confidence(exposures, at_edge, scan),
            parameters=self.parameters.to_dict(),
            worst_spot_return=worst_return,
            worst_vol_points=worst_vol,
            worst_loss=worst_loss,
            worst_at_grid_edge=at_edge,
            positions=len(exposures.exposures),
            excluded_positions=len(exposures.excluded),
            warnings=tuple(dict.fromkeys(warnings)),
        )

    # ------------------------------------------------------------- internals
    @staticmethod
    def _scan(exposures: ExposureSet, grid: ShockGrid) -> tuple[float, float, float]:
        """Reprice the book at every grid point and take the worst.

        Every point is a genuine repricing, vectorised across the grid — the
        same machinery Phase 5's Monte Carlo uses, for the same reason.
        """
        returns, vols = np.meshgrid(
            np.asarray(grid.spot_returns, dtype=float),
            np.asarray(grid.vol_points, dtype=float),
            indexing="ij",
        )
        flat_returns, flat_vols = returns.ravel(), vols.ravel()
        keys = exposures.underlying_keys()
        pnl = revalue_many(
            exposures,
            {key: flat_returns for key in keys},
            {key: flat_vols for key in keys},
        )
        index = int(np.argmin(pnl))
        return float(flat_returns[index]), float(flat_vols[index]), float(-pnl[index])

    def _short_option_floor(self, exposures: ExposureSet) -> tuple[float, str]:
        rate = self.parameters.short_option_minimum_rate
        notional = sum(
            exposure.notional
            for exposure in exposures.exposures
            if exposure.is_option and exposure.is_short
        )
        return (
            rate * notional,
            (
                f"{rate:.2%} of {notional:,.0f} of short-option contract notional "
                "(strike x multiplier), applied as a floor on the scan loss. The "
                "rate is a parameter of this model, not a venue's rule."
            ),
        )

    def _concentration(self, exposures: ExposureSet) -> tuple[float, str]:
        rate = self.parameters.concentration_add_on_rate
        threshold = self.parameters.concentration_threshold
        totals = exposures.gross_notional_by_underlying()
        gross = sum(totals.values())
        if gross <= 0.0 or not totals:
            return 0.0, "No gross notional to concentrate."

        largest_key = max(totals, key=lambda key: totals[key])
        largest = totals[largest_key]
        excess = max(0.0, largest - threshold * gross)
        share = largest / gross
        return (
            rate * excess,
            (
                f"{rate:.2%} of the {excess:,.0f} by which the largest underlying's "
                f"gross notional exceeds {threshold:.0%} of the book "
                f"(it is {share:.1%}). Both the rate and the threshold are "
                "parameters of this model."
            ),
        )

    @staticmethod
    def _confidence(exposures: ExposureSet, at_edge: bool, scan: float) -> float:
        """How much this estimate deserves to be relied on, and why.

        A weighted geometric mean, so one bad dimension pulls the whole score
        down rather than being averaged away — the same aggregation the data
        quality engine uses, for the same reason.
        """
        total = len(exposures.exposures) + len(exposures.excluded)
        coverage = len(exposures.exposures) / total if total else 0.0
        containment = 0.5 if at_edge else 1.0

        scale = max(abs(exposures.reported_value), 1.0)
        consistency = max(0.0, 1.0 - min(1.0, abs(exposures.repricing_gap) / scale))

        scores = (coverage, containment, consistency)
        if any(score <= 0.0 for score in scores):
            return 0.0
        return float(np.exp(np.mean(np.log(scores))))

    def _empty(
        self, exposures: ExposureSet, assumptions: list[str], warnings: list[str]
    ) -> MarginResult:
        return MarginResult(
            method=self.identifier,
            model_version=self.version,
            estimated_margin=0.0,
            currency=exposures.base_currency,
            components=(),
            assumptions=(
                *assumptions,
                "No position in this book could be repriced, so there is nothing "
                "to estimate margin on. The zero below is an absence, not a "
                "finding that the book is riskless.",
            ),
            confidence=0.0,
            parameters=self.parameters.to_dict(),
            worst_spot_return=0.0,
            worst_vol_points=0.0,
            worst_loss=0.0,
            worst_at_grid_edge=False,
            positions=0,
            excluded_positions=len(exposures.excluded),
            warnings=tuple(dict.fromkeys(warnings)),
        )


#: Every model the platform can be asked for, by name.
MARGIN_MODELS: dict[str, type[MarginModel]] = {
    SimpleRiskMarginModel.name: SimpleRiskMarginModel,
}


def build_model(name: str, parameters: MarginParameters | None = None) -> MarginModel:
    try:
        factory = MARGIN_MODELS[name]
    except KeyError:
        raise ValueError(
            f"unknown margin model {name!r}; available: {', '.join(sorted(MARGIN_MODELS))}"
        ) from None
    return factory(parameters)
