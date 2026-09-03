"""Assembling aligned factor histories, with the missing-data policy stated.

The lookback a user gets is the history the platform actually holds: one
observation per option chain they have ingested, plus one per calibrated
surface for the volatility factors. That is a real constraint and it is reported
as an observation count on every answer rather than hidden behind a default
window that quietly runs out of data.

**Nothing is forward-filled.** Carrying a price across a gap manufactures a zero
return, which is not a day on which the market did not move — it is a day the
platform did not see. Zero returns pull a volatility estimate down and a VaR
with it, so gaps are dropped and counted instead.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum

import numpy as np

#: Minimum aligned observations before a historical or Monte Carlo answer is
#: produced at all. Below this the sample is not thin, it is absent.
MIN_OBSERVATIONS = 10


class FactorKind(StrEnum):
    #: Simple return of an underlying's level.
    SPOT_RETURN = "SPOT_RETURN"
    #: Absolute change in at-the-money implied volatility, in volatility points.
    VOLATILITY_CHANGE = "VOLATILITY_CHANGE"


class AlignmentPolicy(StrEnum):
    #: Keep only the dates present in every series. The strictest option, and
    #: the only one that never invents an observation.
    INTERSECT_DATES = "INTERSECT_DATES"


class HistorySource(StrEnum):
    CHAIN_SNAPSHOTS = "CHAIN_SNAPSHOTS"
    SURFACE_CHARACTERISTICS = "SURFACE_CHARACTERISTICS"


class FactorWarning(StrEnum):
    INSUFFICIENT_HISTORY = "RISK_INSUFFICIENT_HISTORY"
    VOLATILITY_HELD_CONSTANT = "RISK_VOLATILITY_HELD_CONSTANT"
    OBSERVATIONS_DROPPED = "RISK_OBSERVATIONS_DROPPED_BY_ALIGNMENT"
    OVERLAPPING_WINDOWS = "RISK_OVERLAPPING_WINDOWS"
    NON_POSITIVE_LEVEL = "RISK_NON_POSITIVE_LEVEL"


@dataclass(frozen=True, slots=True)
class FactorSeries:
    """One factor's raw observations, before alignment."""

    name: str
    kind: FactorKind
    target: str
    source: HistorySource
    dates: tuple[date, ...]
    levels: tuple[float, ...]

    def __post_init__(self) -> None:
        if len(self.dates) != len(self.levels):
            raise ValueError(
                f"factor {self.name}: {len(self.dates)} dates, {len(self.levels)} levels"
            )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "kind": str(self.kind),
            "target": self.target,
            "source": str(self.source),
            "observations": len(self.dates),
            "start_date": self.dates[0].isoformat() if self.dates else None,
            "end_date": self.dates[-1].isoformat() if self.dates else None,
        }


@dataclass(frozen=True, slots=True)
class FactorPanel:
    """Aligned factor returns: ``(observations, factors)``, and how it got there."""

    factors: tuple[FactorSeries, ...]
    dates: tuple[date, ...]
    returns: np.ndarray
    window_days: int
    policy: AlignmentPolicy
    raw_observations: dict[str, int]
    aligned_levels: int
    warnings: tuple[str, ...] = ()

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(factor.name for factor in self.factors)

    @property
    def observations(self) -> int:
        return int(self.returns.shape[0]) if self.returns.size else 0

    @property
    def is_sufficient(self) -> bool:
        return self.observations >= MIN_OBSERVATIONS

    def column(self, name: str) -> np.ndarray:
        return self.returns[:, self.names.index(name)]

    def to_dict(self) -> dict:
        return {
            "policy": str(self.policy),
            "window_days": self.window_days,
            "observations": self.observations,
            "aligned_levels": self.aligned_levels,
            "date_range": (
                [self.dates[0].isoformat(), self.dates[-1].isoformat()] if self.dates else None
            ),
            "factors": [factor.to_dict() for factor in self.factors],
            "raw_observations": dict(self.raw_observations),
            "missing_data_policy": (
                "Dates absent from any series are dropped, not forward-filled. "
                "A carried-forward price is a zero return the market never had, "
                "and zero returns understate every risk number computed from them."
            ),
            "warnings": list(self.warnings),
        }


def build_panel(
    series: Sequence[FactorSeries],
    window_days: int = 1,
    policy: AlignmentPolicy = AlignmentPolicy.INTERSECT_DATES,
    lookback: int | None = None,
) -> FactorPanel:
    """Align the series by date and difference them over ``window_days``.

    Spot factors become simple returns; volatility factors become absolute
    changes in volatility points, because that is the unit a volatility shock is
    expressed in and converting twice is how sign errors get in.
    """
    if not series:
        raise ValueError("a factor panel needs at least one series")

    raw_counts = {factor.name: len(factor.dates) for factor in series}
    warnings: list[str] = []

    common = set(series[0].dates)
    for factor in series[1:]:
        common &= set(factor.dates)
    aligned_dates = sorted(common)

    if lookback is not None and len(aligned_dates) > lookback:
        aligned_dates = aligned_dates[-lookback:]

    dropped = any(len(factor.dates) > len(aligned_dates) for factor in series)
    if dropped:
        warnings.append(FactorWarning.OBSERVATIONS_DROPPED)

    step = max(1, int(window_days))
    if step > 1:
        # Overlapping windows reuse observations, so the count overstates how
        # much independent information is present.
        warnings.append(FactorWarning.OVERLAPPING_WINDOWS)

    columns: list[np.ndarray] = []
    for factor in series:
        lookup = dict(zip(factor.dates, factor.levels, strict=True))
        levels = np.array([lookup[day] for day in aligned_dates], dtype=float)
        if len(levels) <= step:
            columns.append(np.empty(0, dtype=float))
            continue
        if factor.kind is FactorKind.SPOT_RETURN:
            if np.any(levels <= 0.0):
                warnings.append(FactorWarning.NON_POSITIVE_LEVEL)
                columns.append(np.empty(0, dtype=float))
                continue
            columns.append(levels[step:] / levels[:-step] - 1.0)
        else:
            columns.append(levels[step:] - levels[:-step])

    usable = [column for column in columns if column.size > 0]
    if len(usable) != len(columns) or not usable:
        returns = np.empty((0, len(series)), dtype=float)
    else:
        returns = np.column_stack(usable)

    panel = FactorPanel(
        factors=tuple(series),
        dates=tuple(aligned_dates[step:]) if len(aligned_dates) > step else (),
        returns=returns,
        window_days=step,
        policy=policy,
        raw_observations=raw_counts,
        aligned_levels=len(aligned_dates),
        warnings=tuple(dict.fromkeys(warnings)),
    )
    if not panel.is_sufficient:
        panel = FactorPanel(
            **{
                **{f: getattr(panel, f) for f in panel.__slots__ if f != "warnings"},
                "warnings": tuple(
                    dict.fromkeys([*panel.warnings, FactorWarning.INSUFFICIENT_HISTORY])
                ),
            }
        )
    return panel


def spot_factor_name(underlying_key: str) -> str:
    return f"spot:{underlying_key}"


def volatility_factor_name(underlying_key: str) -> str:
    return f"vol:{underlying_key}"
