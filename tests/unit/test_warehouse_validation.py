"""What the warehouse notices about a historical series, and what it refuses to fix.

Every check here has the same shape: the defect is *found and reported*, the row
is kept where keeping it is possible, and nothing is repaired. A validator that
quietly removed bad ticks would hand the research engine a clean-looking series
and an unexplainable backtest — which is the failure the whole module exists to
prevent, and the reason so many of these tests assert on what is still present.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from domains.warehouse.enums import (
    CorporateActionTreatment,
    ValidationCode,
    ValidationSeverity,
)
from domains.warehouse.quality import score_dataset
from domains.warehouse.validation import (
    MIN_OBSERVATIONS_FOR_OUTLIERS,
    looks_like_a_split,
    modified_z_scores,
    validate_bars,
)

INSTRUMENT = uuid.UUID(int=1)
OTHER = uuid.UUID(int=2)
START = datetime(2026, 1, 5, tzinfo=UTC)


def bar(
    day: int,
    close: str = "100",
    instrument: uuid.UUID = INSTRUMENT,
    volume: str = "1000",
    **overrides,
) -> dict:
    price = Decimal(close)
    row = {
        "instrument_id": instrument,
        "exchange_timestamp": START + timedelta(days=day),
        "interval": "1d",
        "open": price,
        "high": price + 1,
        "low": price - 1,
        "close": price,
        "volume": Decimal(volume),
    }
    row.update(overrides)
    return row


def steady(count: int, instrument: uuid.UUID = INSTRUMENT, start: float = 100.0) -> list[dict]:
    """A calm series, long enough for a robust dispersion to mean something."""
    return [
        bar(day, close=f"{start + day * 0.25:.2f}", instrument=instrument) for day in range(count)
    ]


class TestConservation:
    def test_every_row_is_written_excluded_or_rejected(self):
        rows = [
            *steady(3),
            bar(3, close="0"),  # non-positive price
            bar(0),  # duplicate of the first
        ]
        report = validate_bars(rows)

        assert report.rows_in == 5
        assert report.conserved is True
        assert report.rows_written + len(report.excluded) + len(report.rejected) == 5

    def test_a_rejection_names_its_row_and_its_reason(self):
        report = validate_bars([bar(0), bar(1, close="-5")])
        rejected = report.rejected[0]

        assert rejected.row_number == 2
        assert rejected.code is ValidationCode.NON_POSITIVE_PRICE
        assert rejected.severity is ValidationSeverity.ERROR
        assert "close" in rejected.evidence

    def test_a_duplicate_is_excluded_rather_than_rejected(self):
        """Excluded, because the row is well-formed — it just must not be served
        as a second observation of the same instant, which would make every
        count downstream disagree with the venue's."""
        report = validate_bars([bar(0), bar(0)])

        assert report.rows_written == 1
        assert len(report.excluded) == 1
        assert report.excluded[0].code is ValidationCode.DUPLICATE_TIMESTAMP
        assert report.excluded[0].evidence["first_seen_row"] == 1


class TestRowsThatAreNotRows:
    def test_a_naive_timestamp_is_refused_not_assumed_to_be_utc(self):
        """Reading an exchange's local time as UTC shifts a whole series, and
        nothing downstream would ever say so."""
        report = validate_bars([{**bar(0), "exchange_timestamp": datetime(2026, 1, 5)}])
        assert report.rejected[0].code is ValidationCode.TIMESTAMP_NOT_TIMEZONE_AWARE
        assert report.rows_written == 0

    def test_a_bar_whose_close_is_outside_its_range_is_refused(self):
        report = validate_bars(
            [{**bar(0), "high": Decimal("100"), "low": Decimal("99"), "close": Decimal("105")}]
        )
        assert report.rejected[0].code is ValidationCode.BAR_RANGE_INCONSISTENT

    def test_a_high_below_its_low_is_refused(self):
        report = validate_bars([{**bar(0), "high": Decimal("98"), "low": Decimal("102")}])
        assert report.rejected[0].code is ValidationCode.BAR_RANGE_INCONSISTENT

    def test_a_negative_volume_is_refused(self):
        report = validate_bars([bar(0, volume="-1")])
        assert report.rejected[0].code is ValidationCode.NEGATIVE_SIZE

    def test_a_missing_instrument_or_timestamp_is_refused_with_its_position(self):
        report = validate_bars([{"open": Decimal(1)}, {**bar(0), "exchange_timestamp": None}])
        assert [item.code for item in report.rejected] == [
            ValidationCode.SCHEMA_INVALID,
            ValidationCode.SCHEMA_INVALID,
        ]
        assert [item.row_number for item in report.rejected] == [1, 2]


class TestOrdering:
    def test_rows_are_sorted_and_the_disorder_is_reported(self):
        """Sorting is not dropping, so this is a finding rather than an
        exclusion. It is still worth saying: a source that emits out of order is
        a source whose other guarantees are worth checking."""
        report = validate_bars([bar(2), bar(0), bar(1)])

        assert [row.exchange_timestamp for row in report.rows] == sorted(
            row.exchange_timestamp for row in report.rows
        )
        assert any(item.code is ValidationCode.OUT_OF_ORDER for item in report.findings)
        assert report.rows_written == 3


class TestOutliers:
    def test_a_calm_series_is_flagged_nowhere(self):
        report = validate_bars(steady(60))
        assert not any(row.flags for row in report.rows)
        assert report.counts_by_code().get(str(ValidationCode.OUTLIER_RETURN)) is None

    def test_a_bad_tick_is_flagged_and_kept(self):
        rows = steady(60)
        rows[30] = bar(30, close="4000")
        report = validate_bars(rows)

        flagged = [row for row in report.rows if str(ValidationCode.OUTLIER_RETURN) in row.flags]
        assert flagged, "the 40x tick should have been flagged"
        assert report.rows_written == 60, "and it should still be in the data"

    def test_a_short_series_is_not_judged_at_all(self):
        """Below a stated minimum a robust dispersion estimate is noise, so no
        attempt is made rather than a bad one."""
        rows = steady(MIN_OBSERVATIONS_FOR_OUTLIERS - 5)
        rows[-1] = bar(len(rows) - 1, close="9999")
        report = validate_bars(rows)
        assert not any(row.flags for row in report.rows)

    def test_the_robust_score_is_not_fooled_by_the_point_it_is_judging(self):
        """A mean-and-standard-deviation z-score would let one enormous value
        inflate sigma enough to hide itself."""
        values = [0.01 * (index % 5) for index in range(40)] + [50.0]
        scores = modified_z_scores(values)
        assert abs(scores[-1]) > 10

    def test_a_spike_in_a_barely_moving_series_is_still_found(self):
        """A series that hardly moves has a median absolute deviation of exactly
        zero, and a MAD-only score would rate a lone spike in it as
        unremarkable — which is precisely the bad tick worth catching."""
        values = [0.0] * 40 + [50.0]
        scores = modified_z_scores(values)
        assert abs(scores[-1]) > 10

    def test_a_genuinely_constant_sample_scores_zero_rather_than_infinity(self):
        assert modified_z_scores([3.0] * 10) == [0.0] * 10


class TestCorporateActions:
    def test_a_five_for_one_split_is_recognised(self):
        rows = steady(50)
        # The price falls to a fifth and stays there, as a split does.
        for day in range(50, 60):
            rows.append(bar(day, close=f"{(100 + 49 * 0.25) / 5:.2f}"))
        report = validate_bars(rows, CorporateActionTreatment.UNADJUSTED)

        splits = [item for item in report.findings if item.code is ValidationCode.SPLIT_LIKE_JUMP]
        assert splits, report.counts_by_code()
        assert splits[0].evidence["resembles"] == 5.0
        assert splits[0].severity is ValidationSeverity.ERROR

    def test_the_split_is_reported_and_never_repaired(self):
        """The platform holds no corporate-action feed. It can notice the jump;
        it cannot adjust the series, and will not pretend to."""
        rows = steady(50)
        for day in range(50, 60):
            rows.append(bar(day, close=f"{(100 + 49 * 0.25) / 5:.2f}"))
        report = validate_bars(rows, CorporateActionTreatment.UNADJUSTED)

        after = [row for row in report.rows if row.exchange_timestamp >= START + timedelta(days=50)]
        assert after, "the post-split rows must still be there"
        assert all(row.values["close"] < Decimal(30) for row in after), (
            "and must still hold the prices the file gave, unadjusted"
        )

    def test_a_source_declaring_itself_adjusted_is_not_second_guessed(self):
        rows = steady(50)
        for day in range(50, 60):
            rows.append(bar(day, close=f"{(100 + 49 * 0.25) / 5:.2f}"))
        report = validate_bars(rows, CorporateActionTreatment.ADJUSTED_BY_SOURCE)

        assert not any(item.code is ValidationCode.SPLIT_LIKE_JUMP for item in report.findings)

    def test_an_undeclared_treatment_is_a_warning_in_itself(self):
        report = validate_bars(steady(3))
        codes = {item.code for item in report.findings}
        assert ValidationCode.CORPORATE_ACTION_TREATMENT_UNKNOWN in codes

    @pytest.mark.parametrize(
        "ratio,expected",
        [(0.2, 5.0), (5.0, 5.0), (0.5, 2.0), (2.02, 2.0), (1.13, None), (0.97, None)],
    )
    def test_the_ratio_test_catches_splits_both_ways_round(self, ratio, expected):
        assert looks_like_a_split(ratio) == expected


class TestGaps:
    def test_a_date_missing_for_every_instrument_looks_like_a_closure(self):
        rows = [bar(day) for day in (0, 1, 4, 5)] + [
            bar(day, instrument=OTHER) for day in (0, 1, 4, 5)
        ]
        report = validate_bars(rows)

        closures = [
            item for item in report.findings if item.code is ValidationCode.GAP_ALL_INSTRUMENTS
        ]
        assert closures
        assert closures[0].severity is ValidationSeverity.INFO
        assert "does not claim these are holidays" in closures[0].message

    def test_a_date_missing_for_one_instrument_looks_like_missing_data(self):
        rows = [bar(day) for day in (0, 1, 2)] + [bar(day, instrument=OTHER) for day in (0, 2)]
        report = validate_bars(rows)

        holes = [
            item for item in report.findings if item.code is ValidationCode.GAP_SINGLE_INSTRUMENT
        ]
        assert holes
        assert holes[0].instrument_id == OTHER
        assert holes[0].severity is ValidationSeverity.WARNING

    def test_a_complete_series_reports_no_gaps(self):
        report = validate_bars([bar(day) for day in range(5)])
        assert not any(
            item.code in {ValidationCode.GAP_ALL_INSTRUMENTS, ValidationCode.GAP_SINGLE_INSTRUMENT}
            for item in report.findings
        )


class TestDatasetQuality:
    def test_a_clean_well_described_dataset_scores_near_one(self):
        report = validate_bars(steady(60), CorporateActionTreatment.UNADJUSTED)
        quality = score_dataset(
            report,
            source="vendor",
            treatment=CorporateActionTreatment.UNADJUSTED,
            dataset_digest="abc123",
        )
        assert quality.completeness_score == 1.0
        assert quality.consistency_score == 1.0
        assert quality.source_score == 1.0
        assert quality.overall_score > 0.95

    def test_freshness_is_not_measurable_for_an_archive(self):
        """A 2015 tape is not stale, it is history. A zero here would rank every
        archive as broken."""
        report = validate_bars(steady(30), CorporateActionTreatment.UNADJUSTED)
        quality = score_dataset(
            report,
            source="vendor",
            treatment=CorporateActionTreatment.UNADJUSTED,
            last_observation=datetime(2015, 1, 1, tzinfo=UTC),
            continuous=False,
        )
        assert quality.freshness_score is None

    def test_a_continuous_feed_that_has_stopped_scores_badly_on_freshness(self):
        report = validate_bars(steady(30), CorporateActionTreatment.UNADJUSTED)
        quality = score_dataset(
            report,
            source="vendor",
            treatment=CorporateActionTreatment.UNADJUSTED,
            last_observation=datetime.now(UTC) - timedelta(days=30),
            continuous=True,
        )
        assert quality.freshness_score < 0.01

    def test_an_undeclared_provenance_costs_the_source_score(self):
        report = validate_bars(steady(30))
        quality = score_dataset(report, source=None, treatment=CorporateActionTreatment.UNKNOWN)
        assert quality.source_score == 0.0
        assert quality.evidence["corporate_action_treatment_declared"] is False

    def test_one_ruined_dimension_drives_the_overall_down(self):
        """A geometric mean, so a catastrophic dimension cannot be averaged away
        by four healthy ones."""
        report = validate_bars([bar(day, close="0") for day in range(10)])
        quality = score_dataset(
            report,
            source="vendor",
            treatment=CorporateActionTreatment.UNADJUSTED,
            dataset_digest="abc",
        )
        assert quality.completeness_score == 0.0
        assert quality.overall_score == 0.0

    def test_the_evidence_carries_the_counts_the_scores_came_from(self):
        report = validate_bars(steady(30), CorporateActionTreatment.UNADJUSTED)
        quality = score_dataset(
            report, source="vendor", treatment=CorporateActionTreatment.UNADJUSTED
        )
        assert quality.evidence["rows_in"] == 30
        assert quality.evidence["rows_written"] == 30
        assert "counts_by_code" in quality.evidence
