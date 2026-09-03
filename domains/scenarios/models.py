"""Scenario and shock definitions.

A scenario is a named set of shocks to named risk factors. Applying one produces
a shocked market, and the portfolio is then fully revalued (`docs/risk.md` §4).

The one rule that shapes this module: **a scenario that claims to be historical
must carry the data it was derived from.** A shock labelled "COVID crash" with a
round -35% in it is a plausible-looking invention, and this platform does not
ship those. Built-in templates are labelled `HYPOTHETICAL` and say in their own
description that they are round numbers chosen for illustration; a scenario is
only `DERIVED_FROM_HISTORY` when it was computed from a series the platform
actually holds, and it then records that series, its date range and the date of
the move it came from.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum


class RiskFactorKind(StrEnum):
    UNDERLYING_PRICE = "UNDERLYING_PRICE"
    VOLATILITY = "VOLATILITY"
    RISK_FREE_RATE = "RISK_FREE_RATE"
    DIVIDEND_YIELD = "DIVIDEND_YIELD"
    FX_RATE = "FX_RATE"


class ShockType(StrEnum):
    #: Added to the factor level in its own units.
    ABSOLUTE = "ABSOLUTE"
    #: Multiplicative: ``level * (1 + value)``. ``-0.10`` is a 10% fall.
    PERCENTAGE = "PERCENTAGE"
    #: Added to an implied volatility. ``0.05`` is five volatility points.
    VOL_POINTS = "VOL_POINTS"
    #: Added to a rate. ``25`` is 25 basis points, i.e. ``+0.0025``.
    BASIS_POINTS = "BASIS_POINTS"


class ScenarioSource(StrEnum):
    #: Round numbers chosen for illustration. Makes no claim about any market.
    HYPOTHETICAL = "HYPOTHETICAL"
    #: Computed from a price or volatility series the platform holds, and
    #: carrying that series' identity and date range.
    DERIVED_FROM_HISTORY = "DERIVED_FROM_HISTORY"
    USER_DEFINED = "USER_DEFINED"


#: Which shock types make sense for which factor. A percentage shock to a rate
#: is ambiguous (relative to what?), so it is rejected rather than guessed at.
VALID_SHOCK_TYPES: dict[RiskFactorKind, frozenset[ShockType]] = {
    RiskFactorKind.UNDERLYING_PRICE: frozenset({ShockType.ABSOLUTE, ShockType.PERCENTAGE}),
    RiskFactorKind.VOLATILITY: frozenset({ShockType.VOL_POINTS, ShockType.PERCENTAGE}),
    RiskFactorKind.RISK_FREE_RATE: frozenset({ShockType.BASIS_POINTS, ShockType.ABSOLUTE}),
    RiskFactorKind.DIVIDEND_YIELD: frozenset({ShockType.BASIS_POINTS, ShockType.ABSOLUTE}),
    RiskFactorKind.FX_RATE: frozenset({ShockType.ABSOLUTE, ShockType.PERCENTAGE}),
}

#: A volatility cannot be shocked below this. Clipping is recorded on the
#: result; a scenario that drives implied volatility to zero has left the region
#: where the pricing model means anything.
MIN_SHOCKED_VOLATILITY = 1e-4


class ScenarioError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Shock:
    """One factor moved one way.

    ``target`` scopes the shock: an underlying id for price and volatility, a
    currency pair for FX, and ``None`` for "every factor of this kind", which is
    how a market-wide move is written.
    """

    kind: RiskFactorKind
    shock_type: ShockType
    value: float
    target: str | None = None

    def __post_init__(self) -> None:
        allowed = VALID_SHOCK_TYPES[self.kind]
        if self.shock_type not in allowed:
            raise ScenarioError(
                f"{self.shock_type} is not a meaningful shock to {self.kind}; "
                f"use one of {', '.join(sorted(allowed))}"
            )
        if self.shock_type is ShockType.PERCENTAGE and self.value <= -1.0:
            raise ScenarioError(
                f"a percentage shock of {self.value} takes the factor to zero or "
                "below, which is not a market state this platform can price"
            )

    def applies_to(self, target: str | None) -> bool:
        return self.target is None or (target is not None and self.target == target)

    def apply(self, level: float) -> float:
        match self.shock_type:
            case ShockType.ABSOLUTE:
                return level + self.value
            case ShockType.PERCENTAGE:
                return level * (1.0 + self.value)
            case ShockType.VOL_POINTS:
                return level + self.value
            case ShockType.BASIS_POINTS:
                return level + self.value / 10_000.0
        raise ScenarioError(f"unhandled shock type {self.shock_type}")

    @property
    def label(self) -> str:
        match self.shock_type:
            case ShockType.PERCENTAGE:
                return f"{self.value:+.2%}"
            case ShockType.VOL_POINTS:
                return f"{self.value * 100:+.2f} vol pts"
            case ShockType.BASIS_POINTS:
                return f"{self.value:+.0f} bp"
        return f"{self.value:+g}"

    def to_dict(self) -> dict:
        return {
            "kind": str(self.kind),
            "shock_type": str(self.shock_type),
            "value": self.value,
            "target": self.target,
            "label": self.label,
        }


@dataclass(frozen=True, slots=True)
class HistoricalDerivation:
    """Where a historical scenario's numbers actually came from."""

    series: str
    observations: int
    start_date: date
    end_date: date
    #: The date whose move the scenario reproduces.
    event_date: date
    window_days: int
    method: str

    def to_dict(self) -> dict:
        return {
            "series": self.series,
            "observations": self.observations,
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
            "event_date": self.event_date.isoformat(),
            "window_days": self.window_days,
            "method": self.method,
        }


@dataclass(frozen=True, slots=True)
class Scenario:
    id: uuid.UUID
    name: str
    shocks: tuple[Shock, ...]
    source: ScenarioSource
    description: str | None = None
    derivation: HistoricalDerivation | None = None
    created_at: datetime | None = None
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ScenarioError("a scenario needs a name")
        if not self.shocks:
            raise ScenarioError("a scenario with no shocks is not a scenario")
        if self.source is ScenarioSource.DERIVED_FROM_HISTORY and self.derivation is None:
            raise ScenarioError(
                "a scenario claiming to be derived from history must carry the "
                "series, date range and event date it was derived from"
            )

    def shocks_for(self, kind: RiskFactorKind, target: str | None) -> tuple[Shock, ...]:
        return tuple(s for s in self.shocks if s.kind is kind and s.applies_to(target))

    @property
    def is_historical_claim(self) -> bool:
        return self.source is ScenarioSource.DERIVED_FROM_HISTORY

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "name": self.name,
            "description": self.description,
            "source": str(self.source),
            "shocks": [shock.to_dict() for shock in self.shocks],
            "derivation": self.derivation.to_dict() if self.derivation else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "metadata": dict(self.metadata),
        }
