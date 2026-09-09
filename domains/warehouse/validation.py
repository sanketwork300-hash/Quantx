"""What is wrong with a historical series, said out loud.

The rule this module exists to serve is the platform's oldest one: **suspicious
data is flagged and kept, never silently deleted.** A validator that removed bad
ticks would hand the research engine a clean-looking series and an unexplainable
backtest.

So the output is a trichotomy, the same one the option-chain ingestion pipeline
uses, and it conserves:

```
rows_in == rows_written + rows_excluded + rows_rejected
```

* **rejected** — the row is not a row of this kind at all. A bar whose high is
  below its low is not a bar. It reaches no partition, and it reports its source
  position and reason.
* **excluded** — the row is well-formed but must not be served *as a second
  observation of the same instant*. In practice this is only exact duplicates,
  where keeping both would make every downstream count wrong.
* **written** — everything else, including everything merely suspicious, which
  arrives in the partition with its flags attached.

Two of the checks deserve their reasoning spelled out, and both are in the
docstrings below: the outlier threshold, and the split-like jump detector that
notices a corporate action without ever pretending to correct one.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from domains.warehouse.enums import (
    CorporateActionTreatment,
    DatasetKind,
    ValidationCode,
    ValidationSeverity,
)

#: Modified z-score threshold for a return to be called an outlier.
#:
#: Deliberately far out. Financial returns have fat tails, so a threshold that
#: caught every three-sigma day would flag a third of every crisis and train
#: everyone to ignore the flag. Ten robust deviations is a move that is either
#: real and enormous or a data error, and either way is worth a look.
OUTLIER_Z_THRESHOLD = 10.0

#: Consistency factor making the median absolute deviation an estimator of the
#: standard deviation for normally distributed data (Iglewicz & Hoaglin, 1993).
MAD_TO_SIGMA = 0.6745

#: The same authors' fallback constant, for the mean absolute deviation about
#: the median. Needed because a series that barely moves has a MAD of exactly
#: zero, and a single spike in it would then be invisible to a MAD-based score —
#: which is precisely the bad tick worth catching.
MEAN_AD_TO_SIGMA = 0.7979

#: Ratios a split or a bonus issue produces. A jump landing near one of these,
#: on a series nobody has declared adjusted, is worth naming.
SPLIT_RATIOS: tuple[float, ...] = (2.0, 2.5, 3.0, 4.0, 5.0, 10.0, 1.5, 1.25, 20.0)

#: How close to a split ratio a jump has to be. Wide enough to survive a day's
#: ordinary move and a dividend on top of the split, narrow enough that a 1.9x
#: move does not get called a 2:1.
SPLIT_RATIO_TOLERANCE = 0.03

#: Below this many observations a robust dispersion estimate is noise, and
#: outlier detection is not attempted rather than being attempted badly.
MIN_OBSERVATIONS_FOR_OUTLIERS = 20


@dataclass(frozen=True, slots=True)
class ValidationFinding:
    """One thing the validator noticed."""

    code: ValidationCode
    severity: ValidationSeverity
    message: str
    row_number: int | None = None
    instrument_id: uuid.UUID | None = None
    #: The numbers the finding was made on, so it can be argued with.
    evidence: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "code": str(self.code),
            "severity": str(self.severity),
            "message": self.message,
            "row_number": self.row_number,
            "instrument_id": str(self.instrument_id) if self.instrument_id else None,
            "evidence": self.evidence,
        }


@dataclass(frozen=True, slots=True)
class ValidatedRow:
    """One row that will reach a partition, with whatever is odd about it."""

    row_number: int
    instrument_id: uuid.UUID
    exchange_timestamp: datetime
    values: dict
    flags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """What happened to a batch, and whether the count adds up."""

    kind: DatasetKind
    rows_in: int
    rows: tuple[ValidatedRow, ...] = ()
    excluded: tuple[ValidationFinding, ...] = ()
    rejected: tuple[ValidationFinding, ...] = ()
    findings: tuple[ValidationFinding, ...] = ()

    @property
    def rows_written(self) -> int:
        return len(self.rows)

    @property
    def conserved(self) -> bool:
        return self.rows_in == self.rows_written + len(self.excluded) + len(self.rejected)

    @property
    def worst_severity(self) -> ValidationSeverity | None:
        order = {
            ValidationSeverity.INFO: 0,
            ValidationSeverity.WARNING: 1,
            ValidationSeverity.ERROR: 2,
        }
        everything = [*self.findings, *self.excluded, *self.rejected]
        if not everything:
            return None
        return max((item.severity for item in everything), key=lambda s: order[s])

    def counts_by_code(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in (*self.findings, *self.excluded, *self.rejected):
            counts[str(item.code)] = counts.get(str(item.code), 0) + 1
        return dict(sorted(counts.items()))

    def to_dict(self, max_findings: int = 200) -> dict:
        return {
            "kind": str(self.kind),
            "rows_in": self.rows_in,
            "rows_written": self.rows_written,
            "rows_excluded": len(self.excluded),
            "rows_rejected": len(self.rejected),
            "conserved": self.conserved,
            "worst_severity": str(self.worst_severity) if self.worst_severity else None,
            "counts_by_code": self.counts_by_code(),
            "findings": [item.to_dict() for item in self.findings[:max_findings]],
            "excluded": [item.to_dict() for item in self.excluded[:max_findings]],
            "rejected": [item.to_dict() for item in self.rejected[:max_findings]],
        }


# ---------------------------------------------------------------- primitives
def modified_z_scores(values: Sequence[float]) -> list[float]:
    """Robust z-scores about the median, using the median absolute deviation.

    Robust rather than mean-and-standard-deviation because the thing being
    looked for is exactly what would inflate a standard deviation: one bad tick
    raises sigma enough to hide itself.

    A series that barely moves has a median absolute deviation of exactly zero,
    and a single spike in it would then divide by zero — or, worse, be scored as
    unremarkable. So the scale falls back to the mean absolute deviation about
    the median, which a lone spike does move. Only a genuinely constant sample
    returns zeros, and there a spike does not exist to be found.
    """
    if not values:
        return []
    ordered = sorted(values)
    median = _median(ordered)
    deviations = [abs(value - median) for value in values]
    mad = _median(sorted(deviations))

    if mad > 0.0:
        return [MAD_TO_SIGMA * (value - median) / mad for value in values]

    mean_ad = sum(deviations) / len(deviations)
    if mean_ad <= 0.0:
        return [0.0] * len(values)
    return [MEAN_AD_TO_SIGMA * (value - median) / mean_ad for value in values]


def _median(ordered: Sequence[float]) -> float:
    count = len(ordered)
    if count == 0:
        return 0.0
    middle = count // 2
    if count % 2:
        return ordered[middle]
    return 0.5 * (ordered[middle - 1] + ordered[middle])


def looks_like_a_split(ratio: float) -> float | None:
    """The split ratio a price jump resembles, or ``None``.

    Checks the jump both ways round, so a 5:1 split (price falls to a fifth) and
    a 1:5 reverse split (price rises fivefold) are both caught.

    This **detects**; it never corrects. The platform holds no corporate-action
    feed, so it cannot adjust a series and will not pretend to. What it can do
    is stop a 1:5 split being read as an -80% return by a backtest that had no
    way of knowing.
    """
    if ratio <= 0.0 or not math.isfinite(ratio):
        return None
    for candidate in SPLIT_RATIOS:
        for observed in (ratio, 1.0 / ratio):
            if abs(observed - candidate) <= SPLIT_RATIO_TOLERANCE * candidate:
                return candidate
    return None


# ----------------------------------------------------------------- the rows
def _reject(code: ValidationCode, message: str, row_number: int, **evidence) -> ValidationFinding:
    return ValidationFinding(
        code=code,
        severity=ValidationSeverity.ERROR,
        message=message,
        row_number=row_number,
        evidence=evidence,
    )


def validate_bars(
    rows: Sequence[dict],
    treatment: CorporateActionTreatment = CorporateActionTreatment.UNKNOWN,
) -> ValidationReport:
    """Validate OHLCV rows.

    ``rows`` are plain dicts so this runs against anything — a CSV reader, a
    provider response, a Parquet scan — without the caller first having to build
    domain objects out of data that may not survive validation.
    """
    accepted: list[ValidatedRow] = []
    rejected: list[ValidationFinding] = []
    excluded: list[ValidationFinding] = []
    findings: list[ValidationFinding] = []

    seen: dict[tuple[uuid.UUID, datetime], int] = {}

    for index, raw in enumerate(rows, start=1):
        instrument_id = raw.get("instrument_id")
        moment = raw.get("exchange_timestamp")

        if not isinstance(instrument_id, uuid.UUID):
            rejected.append(
                _reject(
                    ValidationCode.SCHEMA_INVALID,
                    "the row carries no usable instrument id",
                    index,
                )
            )
            continue
        if not isinstance(moment, datetime):
            rejected.append(
                _reject(
                    ValidationCode.SCHEMA_INVALID,
                    "the row carries no usable timestamp",
                    index,
                )
            )
            continue
        if moment.tzinfo is None:
            rejected.append(
                _reject(
                    ValidationCode.TIMESTAMP_NOT_TIMEZONE_AWARE,
                    "a timestamp with no offset does not name a moment; reading an "
                    "exchange's local time as UTC would shift the whole series",
                    index,
                )
            )
            continue

        prices = {name: raw.get(name) for name in ("open", "high", "low", "close")}
        if any(not isinstance(value, Decimal) for value in prices.values()):
            rejected.append(
                _reject(ValidationCode.SCHEMA_INVALID, "a price is missing or unreadable", index)
            )
            continue
        if any(value <= 0 for value in prices.values()):
            rejected.append(
                _reject(
                    ValidationCode.NON_POSITIVE_PRICE,
                    "a bar with a non-positive price is not a bar",
                    index,
                    **{name: format(value, "f") for name, value in prices.items()},
                )
            )
            continue

        volume = raw.get("volume")
        if not isinstance(volume, Decimal) or volume < 0:
            rejected.append(
                _reject(
                    ValidationCode.NEGATIVE_SIZE,
                    "a bar with a missing or negative volume is not a bar",
                    index,
                    volume=str(volume),
                )
            )
            continue

        if prices["high"] < prices["low"] or not (
            prices["low"] <= prices["open"] <= prices["high"]
            and prices["low"] <= prices["close"] <= prices["high"]
        ):
            rejected.append(
                _reject(
                    ValidationCode.BAR_RANGE_INCONSISTENT,
                    "the bar's open or close sits outside its own range, which is "
                    "structurally impossible whatever the numbers say",
                    index,
                    **{name: format(value, "f") for name, value in prices.items()},
                )
            )
            continue

        key = (instrument_id, moment)
        if key in seen:
            excluded.append(
                ValidationFinding(
                    code=ValidationCode.DUPLICATE_TIMESTAMP,
                    severity=ValidationSeverity.WARNING,
                    message=(
                        f"a bar for this instrument at {moment.isoformat()} was already "
                        f"read at row {seen[key]}; serving both would make every count "
                        "downstream disagree with the venue's"
                    ),
                    row_number=index,
                    instrument_id=instrument_id,
                    evidence={"first_seen_row": seen[key]},
                )
            )
            continue
        seen[key] = index

        accepted.append(
            ValidatedRow(
                row_number=index,
                instrument_id=instrument_id,
                exchange_timestamp=moment,
                values=dict(raw),
            )
        )

    accepted, order_findings = _apply_ordering(accepted)
    findings.extend(order_findings)

    accepted, series_findings = _flag_series(accepted, treatment)
    findings.extend(series_findings)

    findings.extend(_gap_findings(accepted))
    if treatment is CorporateActionTreatment.UNKNOWN:
        findings.append(
            ValidationFinding(
                code=ValidationCode.CORPORATE_ACTION_TREATMENT_UNKNOWN,
                severity=ValidationSeverity.WARNING,
                message=(
                    "nobody has said whether this series is adjusted for corporate "
                    "actions, so it cannot safely be joined to one that is, and a "
                    "return computed across a split in it would be wrong"
                ),
            )
        )

    return ValidationReport(
        kind=DatasetKind.BARS,
        rows_in=len(rows),
        rows=tuple(accepted),
        excluded=tuple(excluded),
        rejected=tuple(rejected),
        findings=tuple(findings),
    )


def _apply_ordering(
    rows: Sequence[ValidatedRow],
) -> tuple[list[ValidatedRow], list[ValidationFinding]]:
    """Sort by instrument then time, reporting that the input was not sorted.

    Sorting is not dropping, so this is a finding rather than an exclusion. It
    is still worth reporting: a source that emits out of order is a source whose
    other guarantees are worth checking.
    """
    findings: list[ValidationFinding] = []
    by_instrument: dict[uuid.UUID, list[ValidatedRow]] = {}
    for row in rows:
        by_instrument.setdefault(row.instrument_id, []).append(row)

    ordered: list[ValidatedRow] = []
    for instrument_id, series in by_instrument.items():
        timestamps = [row.exchange_timestamp for row in series]
        if timestamps != sorted(timestamps):
            findings.append(
                ValidationFinding(
                    code=ValidationCode.OUT_OF_ORDER,
                    severity=ValidationSeverity.INFO,
                    message="rows arrived out of time order and were sorted before writing",
                    instrument_id=instrument_id,
                    evidence={"rows": len(series)},
                )
            )
        ordered.extend(sorted(series, key=lambda row: row.exchange_timestamp))
    return ordered, findings


def _flag_series(
    rows: Sequence[ValidatedRow], treatment: CorporateActionTreatment
) -> tuple[list[ValidatedRow], list[ValidationFinding]]:
    """Flag outlier returns and split-like jumps, per instrument.

    Both are *flags on rows that are kept*. An outlier is often the most
    important observation in a sample, and a split-like jump is a fact about the
    series that the reader has to decide about — neither is the validator's to
    remove.
    """
    findings: list[ValidationFinding] = []
    by_instrument: dict[uuid.UUID, list[ValidatedRow]] = {}
    for row in rows:
        by_instrument.setdefault(row.instrument_id, []).append(row)

    flagged: dict[int, list[str]] = {}

    for instrument_id, series in by_instrument.items():
        if len(series) < MIN_OBSERVATIONS_FOR_OUTLIERS + 1:
            continue

        closes = [float(row.values["close"]) for row in series]
        returns = [
            math.log(closes[index] / closes[index - 1]) if closes[index - 1] > 0 else 0.0
            for index in range(1, len(closes))
        ]
        scores = modified_z_scores(returns)

        for offset, score in enumerate(scores):
            row = series[offset + 1]
            if abs(score) < OUTLIER_Z_THRESHOLD:
                continue

            flagged.setdefault(row.row_number, []).append(str(ValidationCode.OUTLIER_RETURN))
            ratio = closes[offset + 1] / closes[offset]
            findings.append(
                ValidationFinding(
                    code=ValidationCode.OUTLIER_RETURN,
                    severity=ValidationSeverity.WARNING,
                    message=(
                        f"a {ratio:.3f}x move against a robust dispersion of the series' "
                        f"own returns (modified z {score:.1f})"
                    ),
                    row_number=row.row_number,
                    instrument_id=instrument_id,
                    evidence={"ratio": ratio, "modified_z": score},
                )
            )

            split = looks_like_a_split(ratio)
            if split is not None and treatment is not CorporateActionTreatment.ADJUSTED_BY_SOURCE:
                flagged.setdefault(row.row_number, []).append(str(ValidationCode.SPLIT_LIKE_JUMP))
                findings.append(
                    ValidationFinding(
                        code=ValidationCode.SPLIT_LIKE_JUMP,
                        severity=ValidationSeverity.ERROR,
                        message=(
                            f"a {ratio:.3f}x jump, close to {split:g}, on a series declared "
                            f"{treatment}. This platform holds no corporate-action feed and "
                            "will not adjust the series; a return computed across this date "
                            "is probably wrong."
                        ),
                        row_number=row.row_number,
                        instrument_id=instrument_id,
                        evidence={"ratio": ratio, "resembles": split, "treatment": str(treatment)},
                    )
                )

    if not flagged:
        return list(rows), findings

    return [
        ValidatedRow(
            row_number=row.row_number,
            instrument_id=row.instrument_id,
            exchange_timestamp=row.exchange_timestamp,
            values=row.values,
            flags=tuple(flagged.get(row.row_number, ())),
        )
        for row in rows
    ], findings


def _gap_findings(rows: Sequence[ValidatedRow]) -> list[ValidationFinding]:
    """Dates with no observation, split by whether every instrument lost them.

    The platform holds no trading calendar — exchange calendars come from
    QuantLib, which is a test oracle here and not a runtime dependency — so it
    cannot say which absent dates are holidays. What it can say is *which
    absences are shared*: a date missing for every instrument in the dataset
    looks like a market closure, and a date missing for one instrument while its
    neighbours have data looks like missing data. That distinction is derived
    from the data itself and is the one a reader actually needs.
    """
    if not rows:
        return []

    by_instrument: dict[uuid.UUID, set[date]] = {}
    for row in rows:
        by_instrument.setdefault(row.instrument_id, set()).add(row.exchange_timestamp.date())

    all_days = sorted({day for days in by_instrument.values() for day in days})
    if len(all_days) < 2 or len(by_instrument) == 0:
        return []

    span = {all_days[0] + _days(offset) for offset in range((all_days[-1] - all_days[0]).days + 1)}
    findings: list[ValidationFinding] = []

    missing_everywhere = sorted(span - set(all_days))
    if missing_everywhere:
        findings.append(
            ValidationFinding(
                code=ValidationCode.GAP_ALL_INSTRUMENTS,
                severity=ValidationSeverity.INFO,
                message=(
                    f"{len(missing_everywhere)} date(s) in the range carry no observation for "
                    "any instrument, which is what a market closure looks like. The platform "
                    "holds no trading calendar and does not claim these are holidays."
                ),
                evidence={"dates": [day.isoformat() for day in missing_everywhere[:50]]},
            )
        )

    if len(by_instrument) > 1:
        present = set(all_days)
        for instrument_id, days in by_instrument.items():
            missing = sorted((present - days) & present)
            if missing:
                findings.append(
                    ValidationFinding(
                        code=ValidationCode.GAP_SINGLE_INSTRUMENT,
                        severity=ValidationSeverity.WARNING,
                        message=(
                            f"{len(missing)} date(s) carry data for other instruments in this "
                            "dataset but not for this one, which is what missing data looks "
                            "like rather than a closure"
                        ),
                        instrument_id=instrument_id,
                        evidence={"dates": [day.isoformat() for day in missing[:50]]},
                    )
                )

    return findings


def _days(count: int):
    from datetime import timedelta

    return timedelta(days=count)
