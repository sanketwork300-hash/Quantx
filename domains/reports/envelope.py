"""The analytical result envelope.

Every analytical endpoint returns this shape. HTTP status expresses whether the
*request* was valid; ``ResultStatus`` expresses whether the *calculation*
succeeded. Conflating the two is how a partially-computable risk report turns
into a 500 and the user learns nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from domains.reports.provenance import Provenance
from domains.reports.warnings import AnalyticalWarning, WarningSeverity


class ResultStatus(StrEnum):
    #: Every requested quantity was computed within tolerance.
    OK = "OK"
    #: Some quantities are present; absent ones are null and named by a warning.
    PARTIAL = "PARTIAL"
    #: Nothing usable was produced.
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class AnalyticalResult[T]:
    status: ResultStatus
    results: T | None
    provenance: Provenance
    warnings: tuple[AnalyticalWarning, ...] = field(default=())

    @classmethod
    def ok(
        cls,
        results: T,
        provenance: Provenance,
        warnings: tuple[AnalyticalWarning, ...] = (),
    ) -> AnalyticalResult[T]:
        # A result carrying error-severity warnings is partial by definition,
        # whatever the caller intended to label it.
        status = (
            ResultStatus.PARTIAL
            if any(w.severity is WarningSeverity.ERROR for w in warnings)
            else ResultStatus.OK
        )
        return cls(status=status, results=results, provenance=provenance, warnings=warnings)

    @classmethod
    def partial(
        cls, results: T, provenance: Provenance, warnings: tuple[AnalyticalWarning, ...]
    ) -> AnalyticalResult[T]:
        return cls(
            status=ResultStatus.PARTIAL,
            results=results,
            provenance=provenance,
            warnings=warnings,
        )

    @classmethod
    def failed(
        cls, provenance: Provenance, warnings: tuple[AnalyticalWarning, ...]
    ) -> AnalyticalResult[T]:
        return cls(
            status=ResultStatus.FAILED, results=None, provenance=provenance, warnings=warnings
        )

    def with_warnings(self, *extra: AnalyticalWarning) -> AnalyticalResult[T]:
        return replace(self, warnings=(*self.warnings, *extra))

    def to_dict(self, serializer=None) -> dict[str, Any]:
        payload = self.results
        if serializer is not None and payload is not None:
            payload = serializer(payload)
        return {
            "status": str(self.status),
            "results": payload,
            "warnings": [warning.to_dict() for warning in self.warnings],
            "provenance": self.provenance.to_dict(),
        }
