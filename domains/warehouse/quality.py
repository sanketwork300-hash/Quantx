"""How good is a dataset, and on what evidence.

Five dimensions and an overall, as the build spec asks for. What matters more
than the arithmetic is that each one is **defined in terms of something actually
measured**, and that a dimension the platform cannot measure returns ``None``
rather than a plausible number.

That last rule does real work here. "Freshness" is meaningless for a historical
dataset — a 2015 tape is not stale, it is history — so freshness is scored only
for a dataset that declares itself intended to stay current, and is ``None``
otherwise. A zero there would rank every archive as broken.

The overall score is a **weighted geometric mean**, the same aggregation the
per-quote quality engine uses, and for the same reason: one catastrophic
dimension has to drive the overall to zero rather than be averaged away by four
healthy ones.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from domains.warehouse.enums import (
    CorporateActionTreatment,
    ValidationCode,
    ValidationSeverity,
)
from domains.warehouse.validation import ValidationReport
from quant.numerical.tolerances import clamp
from quant.statistics.scoring import weighted_geometric_mean

QUALITY_MODEL_VERSION = "warehouse-quality@1.0.0"

#: Freshness half-life for a dataset that declares itself continuous: after this
#: many days without an update the score has halved. A daily-bar feed that has
#: not moved in a week is a feed that has stopped.
DEFAULT_FRESHNESS_HALF_LIFE_DAYS = 3.0

WEIGHTS = {
    "completeness": 1.0,
    "consistency": 1.5,
    "outlier": 1.0,
    "source": 0.5,
    "freshness": 1.0,
}

#: Validation codes that mean a row was structurally wrong rather than merely
#: unusual. These drive the consistency dimension.
_CONSISTENCY_CODES = frozenset(
    {
        ValidationCode.SCHEMA_INVALID,
        ValidationCode.TIMESTAMP_NOT_TIMEZONE_AWARE,
        ValidationCode.DUPLICATE_TIMESTAMP,
        ValidationCode.BAR_RANGE_INCONSISTENT,
        ValidationCode.NON_POSITIVE_PRICE,
        ValidationCode.NEGATIVE_SIZE,
        ValidationCode.OUT_OF_ORDER,
    }
)


@dataclass(frozen=True, slots=True)
class DatasetQuality:
    """Five dimensions in ``[0, 1]`` (1 = good), and what they were measured on.

    ``None`` on a dimension means *not measurable*, which is a different
    statement from zero and is never collapsed into one.
    """

    #: Fraction of supplied rows that survived validation. Deliberately not
    #: "fraction of the data that should exist", because that needs a trading
    #: calendar the platform does not hold; the gap findings carry that instead.
    completeness_score: float
    #: Fraction of rows with no structural defect.
    consistency_score: float
    #: Fraction of rows carrying no outlier or split-like flag.
    outlier_score: float
    #: How completely the dataset's own provenance is declared — not a judgement
    #: about the vendor. A dataset that names its source, states its
    #: corporate-action treatment and carries a content digest scores 1.
    source_score: float
    #: Only for a dataset that says it is meant to stay current.
    freshness_score: float | None
    overall_score: float
    #: The counts behind the numbers, so a score can be argued with.
    evidence: dict
    model_version: str = QUALITY_MODEL_VERSION

    def to_dict(self) -> dict:
        return {
            "completeness_score": self.completeness_score,
            "consistency_score": self.consistency_score,
            "outlier_score": self.outlier_score,
            "source_score": self.source_score,
            "freshness_score": self.freshness_score,
            "overall_score": self.overall_score,
            "model_version": self.model_version,
            "evidence": self.evidence,
        }


def _freshness(
    last_observation: datetime | None,
    as_of: datetime,
    half_life_days: float,
) -> float | None:
    if last_observation is None:
        return None
    age_days = max((as_of - last_observation).total_seconds() / 86_400.0, 0.0)
    return float(0.5 ** (age_days / max(half_life_days, 1e-9)))


def _source_score(
    source: str | None,
    treatment: CorporateActionTreatment,
    dataset_digest: str | None,
) -> tuple[float, dict]:
    """Provenance completeness: three declared facts, each worth a third.

    Named ``source_score`` to match the build spec, but it measures how well the
    dataset describes itself rather than how good its vendor is. The platform
    has no basis for the second and will not invent a ranking of data vendors.
    """
    named = bool(source and source.strip())
    declared = treatment is not CorporateActionTreatment.UNKNOWN
    digested = bool(dataset_digest and dataset_digest.strip())
    score = (int(named) + int(declared) + int(digested)) / 3.0
    return score, {
        "source_named": named,
        "corporate_action_treatment_declared": declared,
        "content_digest_present": digested,
    }


def score_dataset(
    report: ValidationReport,
    source: str | None,
    treatment: CorporateActionTreatment,
    dataset_digest: str | None = None,
    last_observation: datetime | None = None,
    continuous: bool = False,
    as_of: datetime | None = None,
    freshness_half_life_days: float = DEFAULT_FRESHNESS_HALF_LIFE_DAYS,
) -> DatasetQuality:
    """Score a dataset from its validation report and its declared provenance."""
    moment = as_of or datetime.now(UTC)
    rows_in = max(report.rows_in, 1)

    completeness = report.rows_written / rows_in

    structural = sum(
        1
        for item in (*report.rejected, *report.excluded, *report.findings)
        if item.code in _CONSISTENCY_CODES
    )
    consistency = clamp(1.0 - structural / rows_in, 0.0, 1.0)

    flagged = sum(1 for row in report.rows if row.flags)
    outlier = clamp(1.0 - flagged / rows_in, 0.0, 1.0)

    source_value, source_evidence = _source_score(source, treatment, dataset_digest)
    freshness = (
        _freshness(last_observation, moment, freshness_half_life_days) if continuous else None
    )

    scores = [completeness, consistency, outlier, source_value]
    weights = [
        WEIGHTS["completeness"],
        WEIGHTS["consistency"],
        WEIGHTS["outlier"],
        WEIGHTS["source"],
    ]
    if freshness is not None:
        scores.append(freshness)
        weights.append(WEIGHTS["freshness"])

    overall = weighted_geometric_mean([clamp(value, 0.0, 1.0) for value in scores], weights)

    blocking = [
        item
        for item in (*report.findings, *report.rejected)
        if item.severity is ValidationSeverity.ERROR
    ]

    return DatasetQuality(
        completeness_score=clamp(completeness, 0.0, 1.0),
        consistency_score=consistency,
        outlier_score=outlier,
        source_score=source_value,
        freshness_score=freshness,
        overall_score=overall,
        evidence={
            "rows_in": report.rows_in,
            "rows_written": report.rows_written,
            "rows_excluded": len(report.excluded),
            "rows_rejected": len(report.rejected),
            "rows_flagged": flagged,
            "structural_findings": structural,
            "error_findings": len(blocking),
            "counts_by_code": report.counts_by_code(),
            "continuous": continuous,
            "last_observation": last_observation.isoformat() if last_observation else None,
            **source_evidence,
        },
    )
