"""Provenance: what a number was computed from.

Build spec 1.4. A result without complete provenance is a bug, not a degraded
result: an analysis that cannot be reproduced six months later has no standing,
and the difference between "the model changed" and "the market changed" is only
answerable if both are recorded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class Provenance:
    computed_at: datetime
    code_commit: str
    #: Logical valuation timestamp of the inputs.
    market_state_timestamp: datetime | None = None
    market_state_id: str | None = None
    #: Provider names, e.g. ``["csv:user-upload:9f3a", "synthetic:v1"]``.
    market_data_sources: tuple[str, ...] = ()
    #: Provider/dataset -> content digest or etag.
    dataset_versions: dict[str, str] = field(default_factory=dict)
    yield_curve_id: str | None = None
    surface_id: str | None = None
    #: Component -> versioned model identifier, e.g. ``{"quality": "v1.0.0"}``.
    model_versions: dict[str, str] = field(default_factory=dict)
    calibration_timestamp: datetime | None = None
    #: Solver/grid tolerances that materially affect the result.
    numerical_tolerances: dict[str, Any] = field(default_factory=dict)
    #: Parameters chosen by the caller that changed the outcome.
    parameters: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def now(cls, code_commit: str, **kwargs) -> Provenance:
        return cls(computed_at=datetime.now(UTC), code_commit=code_commit, **kwargs)

    def to_dict(self) -> dict:
        return {
            "computed_at": self.computed_at.isoformat(),
            "code_commit": self.code_commit,
            "market_state_timestamp": (
                self.market_state_timestamp.isoformat() if self.market_state_timestamp else None
            ),
            "market_state_id": self.market_state_id,
            "market_data_sources": list(self.market_data_sources),
            "dataset_versions": dict(self.dataset_versions),
            "yield_curve_id": self.yield_curve_id,
            "surface_id": self.surface_id,
            "model_versions": dict(self.model_versions),
            "calibration_timestamp": (
                self.calibration_timestamp.isoformat() if self.calibration_timestamp else None
            ),
            "numerical_tolerances": dict(self.numerical_tolerances),
            "parameters": dict(self.parameters),
        }
