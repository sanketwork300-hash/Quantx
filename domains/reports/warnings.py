"""Structured warnings.

A quantitative failure is a *result*, not an exception escaping to a 500. Every
analytical service reports what it could not do as warnings with closed-vocabulary
codes, so the frontend can map a code to a grounded explanation (build spec 80)
without string matching, and so a new failure mode cannot be introduced without
appearing in a domain's code enum.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class WarningSeverity(StrEnum):
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


@dataclass(frozen=True, slots=True)
class AnalyticalWarning:
    code: str
    severity: WarningSeverity
    message: str
    context: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "severity": str(self.severity),
            "message": self.message,
            "context": self.context,
        }

    @classmethod
    def info(cls, code: str, message: str, **context) -> AnalyticalWarning:
        return cls(code, WarningSeverity.INFO, message, context)

    @classmethod
    def warn(cls, code: str, message: str, **context) -> AnalyticalWarning:
        return cls(code, WarningSeverity.WARNING, message, context)

    @classmethod
    def error(cls, code: str, message: str, **context) -> AnalyticalWarning:
        return cls(code, WarningSeverity.ERROR, message, context)
