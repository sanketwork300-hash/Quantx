"""Quality flags and the quality result object.

Flag codes are a closed enum so the frontend can map each one to a grounded
explanation (build spec 80) without string matching, and so a new check cannot
be added without appearing in the vocabulary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum, StrEnum


class Severity(IntEnum):
    """Ordered so that ``max()`` over a flag list yields the worst severity."""

    INFO = 10
    WARNING = 20
    ERROR = 30

    def __str__(self) -> str:
        return self.name


class QualityCode(StrEnum):
    # ---------------------------------------------------------- completeness
    MISSING_BID = "MISSING_BID"
    MISSING_ASK = "MISSING_ASK"
    MISSING_BOTH_SIDES = "MISSING_BOTH_SIDES"
    MISSING_LAST = "MISSING_LAST"
    MISSING_VOLUME = "MISSING_VOLUME"
    MISSING_OPEN_INTEREST = "MISSING_OPEN_INTEREST"
    MISSING_DEPTH = "MISSING_DEPTH"
    MISSING_SEQUENCE = "MISSING_SEQUENCE"
    MISSING_UNDERLYING_PRICE = "MISSING_UNDERLYING_PRICE"

    # ------------------------------------------------------------ structural
    NEGATIVE_PRICE = "NEGATIVE_PRICE"
    ZERO_BID = "ZERO_BID"
    ZERO_ASK = "ZERO_ASK"
    NEGATIVE_SIZE = "NEGATIVE_SIZE"
    INVALID_STRIKE = "INVALID_STRIKE"

    # ----------------------------------------------------------- consistency
    CROSSED_MARKET = "CROSSED_MARKET"
    LOCKED_MARKET = "LOCKED_MARKET"
    INCONSISTENT_TIMESTAMPS = "INCONSISTENT_TIMESTAMPS"
    FUTURE_TIMESTAMP = "FUTURE_TIMESTAMP"
    DUPLICATE_OBSERVATION = "DUPLICATE_OBSERVATION"
    EXTREME_PRICE_JUMP = "EXTREME_PRICE_JUMP"
    PRICE_BELOW_INTRINSIC = "PRICE_BELOW_INTRINSIC"
    PRICE_ABOVE_BOUND = "PRICE_ABOVE_BOUND"

    # --------------------------------------------------------------- options
    OPTION_EXPIRED = "OPTION_EXPIRED"
    UNKNOWN_EXPIRY_TIME = "UNKNOWN_EXPIRY_TIME"

    # -------------------------------------------------------------- staleness
    STALE_QUOTE = "STALE_QUOTE"

    # -------------------------------------------------------------- liquidity
    WIDE_SPREAD = "WIDE_SPREAD"
    ILLIQUID_CONTRACT = "ILLIQUID_CONTRACT"
    NO_QUOTED_SIZE = "NO_QUOTED_SIZE"

    # ------------------------------------------------------------ assumptions
    MULTIPLIER_ASSUMED = "MULTIPLIER_ASSUMED"


@dataclass(frozen=True, slots=True)
class QualityFlag:
    code: QualityCode
    severity: Severity
    message: str
    context: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "code": str(self.code),
            "severity": str(self.severity),
            "message": self.message,
            "context": self.context,
        }

    @classmethod
    def from_dict(cls, payload: dict) -> QualityFlag:
        """Rehydrate a stored flag.

        Exact rather than lossy: a flag list read back with its codes dropped
        would present as "no flags", which is a claim the row does not make.
        """
        return cls(
            code=QualityCode(payload["code"]),
            severity=Severity[str(payload["severity"])],
            message=payload.get("message", ""),
            context=dict(payload.get("context") or {}),
        )


@dataclass(frozen=True, slots=True)
class MarketDataQuality:
    """Five sub-scores in ``[0, 1]`` (1 = good) plus the flags that produced them."""

    stale_score: float
    spread_score: float
    liquidity_score: float
    consistency_score: float
    completeness_score: float
    overall_score: float
    flags: tuple[QualityFlag, ...] = ()

    @property
    def worst_severity(self) -> Severity | None:
        if not self.flags:
            return None
        return max(flag.severity for flag in self.flags)

    def has(self, code: QualityCode) -> bool:
        return any(flag.code is code for flag in self.flags)

    def flags_at_or_above(self, severity: Severity) -> tuple[QualityFlag, ...]:
        return tuple(flag for flag in self.flags if flag.severity >= severity)

    def primary_flag(self, severity: Severity) -> QualityFlag | None:
        """The flag that best explains an exclusion.

        Highest severity wins; ties break on declaration order in
        :class:`QualityCode`, so the reason recorded for a given quote is
        deterministic and a regression test can pin it.
        """
        eligible = self.flags_at_or_above(severity)
        if not eligible:
            return None
        order = list(QualityCode)
        return min(eligible, key=lambda flag: (-int(flag.severity), order.index(flag.code)))

    def to_dict(self) -> dict:
        return {
            "stale_score": self.stale_score,
            "spread_score": self.spread_score,
            "liquidity_score": self.liquidity_score,
            "consistency_score": self.consistency_score,
            "completeness_score": self.completeness_score,
            "overall_score": self.overall_score,
            "flags": [flag.to_dict() for flag in self.flags],
        }
