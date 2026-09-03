"""Unit tests for the microstructure layer.

The theme running through all of them is the phase's one rule: a measurement
the data cannot support is an absence with a reason, never a number. Most of
these tests are therefore about what the code *refuses* to produce.
"""

from __future__ import annotations

import math
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from domains.market_data.enums import BookEventType, BookSide
from domains.market_data.models import BookEvent, OrderBookLevel, OrderBookSnapshot
from domains.microstructure.analytics import BookAnalyticsParams, analyse_snapshots, to_book
from domains.microstructure.availability import (
    MAX_TIED_EVENT_FRACTION,
    MIN_DEPTH_LEVELS,
    AvailabilityRefusal,
    CapabilityRefused,
    MicrostructureCapability,
    assess,
)
from domains.microstructure.importer import (
    EventImporter,
    SnapshotImporter,
    detect_level_columns,
)
from domains.microstructure.models import (
    EventRejection,
    SnapshotRejection,
    profile_dataset,
    sequencing_report,
)
from domains.microstructure.queue import QueueParams
from domains.microstructure.queue import estimate as estimate_queue
from domains.microstructure.storage import (
    events_from_parquet,
    events_to_parquet,
    snapshots_from_parquet,
    snapshots_to_parquet,
)
from quant.microstructure.book import (
    Book,
    Unavailable,
    analyse_book,
    book_slope,
    cost_to_trade,
    depth_concentration,
    imbalance,
    microprice,
    weighted_imbalance,
)
from quant.microstructure.queue import (
    CancellationPriority,
    QueueRefusal,
    QueueUnavailable,
    estimate_queue_outlook,
)

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
SNAPSHOT_CSV = (DATA_DIR / "orderbook_snapshots.csv").read_bytes()
EVENT_CSV = (DATA_DIR / "orderbook_events.csv").read_bytes()
BOOK_PARQUET = (DATA_DIR / "orderbook.parquet").read_bytes()

INSTRUMENT = uuid.UUID("11111111-2222-3333-4444-555555555555")
START = datetime(2026, 9, 24, 9, 20, tzinfo=UTC)


def book(bids, asks) -> Book:
    return Book.of(bids, asks)


# --------------------------------------------------------------- book measures
class TestHandWorkedBookMeasures:
    """Every number below is checkable on paper, which is the point."""

    def test_the_microprice_leans_away_from_the_thick_side(self):
        # 100 bid for 30, 101 offered for 10. The bid is thicker, so the
        # microprice sits above the mid: (100*10 + 101*30) / 40 = 100.75.
        assert microprice(book([(100.0, 30.0)], [(101.0, 10.0)])) == pytest.approx(100.75)
        # Mirror it and it leans the other way, by exactly as much.
        assert microprice(book([(100.0, 10.0)], [(101.0, 30.0)])) == pytest.approx(100.25)
        # Balanced sizes give the plain mid.
        assert microprice(book([(100.0, 20.0)], [(101.0, 20.0)])) == pytest.approx(100.5)

    def test_the_imbalance_is_bounded_and_signed_the_stated_way(self):
        assert imbalance(book([(100.0, 30.0)], [(101.0, 10.0)])) == pytest.approx(0.5)
        assert imbalance(book([(100.0, 10.0)], [(101.0, 30.0)])) == pytest.approx(-0.5)
        assert imbalance(book([(100.0, 25.0)], [(101.0, 25.0)])) == pytest.approx(0.0)

    def test_zero_decay_reduces_the_weighted_imbalance_to_the_plain_one(self):
        deep = book(
            [(100.0, 10.0), (99.0, 20.0), (98.0, 30.0)],
            [(101.0, 5.0), (102.0, 15.0), (103.0, 25.0)],
        )
        assert weighted_imbalance(deep, levels=3, decay=0.0) == pytest.approx(
            imbalance(deep, levels=3)
        )

    def test_a_large_decay_reduces_it_to_the_top_of_book(self):
        deep = book(
            [(100.0, 10.0), (99.0, 900.0)],
            [(101.0, 30.0), (102.0, 900.0)],
        )
        assert weighted_imbalance(deep, levels=2, decay=50.0) == pytest.approx(
            imbalance(deep, levels=1), abs=1e-9
        )

    def test_the_slope_is_the_through_origin_least_squares_fit(self):
        # Mid 100. Bids at 99 and 98, so the relative distances are 0.01 and
        # 0.02 and the cumulative depths are 10 and 30. Through the origin,
        #   slope = (0.01*10 + 0.02*30) / (0.01^2 + 0.02^2) = 0.7 / 0.0005 = 1400
        # and the residuals are (10 - 14) and (30 - 28), so
        #   R^2 = 1 - (16 + 4) / (100 + 900) = 0.98.
        deep = book([(99.0, 10.0), (98.0, 20.0)], [(101.0, 10.0), (102.0, 20.0)])
        assert deep.mid == pytest.approx(100.0)
        estimate = book_slope(deep, is_bid=True)
        assert estimate.slope == pytest.approx(1_400.0)
        assert estimate.r_squared == pytest.approx(0.98)
        assert estimate.levels_used == 2

    def test_depth_concentration_reads_as_an_effective_level_count(self):
        # Two equal levels: H = 0.5, so the depth is spread across 2 levels.
        herfindahl, effective = depth_concentration(
            book([(100.0, 50.0), (99.0, 50.0)], [(101.0, 1.0)]).bids
        )
        assert herfindahl == pytest.approx(0.5)
        assert effective == pytest.approx(2.0)

    def test_the_cost_to_trade_is_the_walk_it_says_it_is(self):
        # Buy 15 against 5 at 101 and 10 at 102: average 101.6667, mid 100.5.
        cost = cost_to_trade(book([(100.0, 10.0)], [(101.0, 5.0), (102.0, 10.0)]), 15.0, True)
        assert cost.average_price == pytest.approx((5 * 101 + 10 * 102) / 15)
        assert cost.slippage_per_unit == pytest.approx(cost.average_price - 100.5)
        assert cost.levels_consumed == 2

    def test_selling_into_the_bid_is_a_cost_with_the_same_sign(self):
        two_sided = book([(100.0, 10.0), (99.0, 10.0)], [(101.0, 10.0), (102.0, 10.0)])
        buy = cost_to_trade(two_sided, 15.0, True)
        sell = cost_to_trade(two_sided, 15.0, False)
        assert buy.slippage_per_unit > 0
        assert sell.slippage_per_unit > 0
        assert buy.slippage_bps == pytest.approx(sell.slippage_bps)


class TestWhatABookCannotSupport:
    """The refusals. Each has a plausible-looking number that means nothing."""

    def test_a_one_sided_book_has_no_mid_and_says_so(self):
        one_sided = book([(100.0, 10.0)], [])
        assert one_sided.mid is None
        assert microprice(one_sided) == (None, Unavailable.ONE_SIDED_BOOK)
        assert book_slope(one_sided, is_bid=True) == (None, Unavailable.ONE_SIDED_BOOK)

    def test_a_book_with_no_resting_size_has_no_imbalance_rather_than_zero(self):
        """Zero would read as "balanced", which is a different claim."""
        empty = book([(100.0, 0.0)], [(101.0, 0.0)])
        assert imbalance(empty) == (None, Unavailable.NO_RESTING_SIZE)
        assert microprice(empty) == (None, Unavailable.NO_RESTING_SIZE)

    def test_a_single_level_side_has_no_slope(self):
        """``C_1 / d_1`` is an identity, not a fit, and would look like one."""
        assert book_slope(book([(100.0, 10.0)], [(101.0, 10.0)]), is_bid=True) == (
            None,
            Unavailable.SINGLE_LEVEL,
        )

    def test_a_single_level_side_has_no_concentration(self):
        result = depth_concentration(book([(100.0, 10.0)], [(101.0, 5.0)]).bids)
        assert result == (None, Unavailable.SINGLE_LEVEL)

    def test_a_size_the_book_cannot_absorb_is_refused_not_extrapolated(self):
        """The price beyond the last level is not in the book."""
        shallow = book([(100.0, 10.0)], [(101.0, 5.0)])
        assert cost_to_trade(shallow, 50.0, True) == (None, Unavailable.INSUFFICIENT_DEPTH)

    def test_every_refusal_is_recorded_on_the_roll_up(self):
        analytics = analyse_book(book([(100.0, 10.0)], [(101.0, 5.0)]), levels=5)
        assert analytics.microprice is not None
        assert analytics.bid_slope is None
        assert analytics.unavailable["bid_slope"] == str(Unavailable.SINGLE_LEVEL)
        assert analytics.unavailable["ask_concentration"] == str(Unavailable.SINGLE_LEVEL)

    def test_levels_must_be_ordered_best_first(self):
        with pytest.raises(ValueError, match="best-first"):
            Book.of([(99.0, 10.0), (100.0, 10.0)], [(101.0, 1.0)])

    @given(
        bid=st.floats(min_value=1.0, max_value=1_000.0),
        spread=st.floats(min_value=0.01, max_value=50.0),
        bid_size=st.floats(min_value=0.1, max_value=10_000.0),
        ask_size=st.floats(min_value=0.1, max_value=10_000.0),
    )
    @settings(max_examples=200, deadline=None)
    def test_the_microprice_always_lies_inside_the_quoted_market(
        self, bid, spread, bid_size, ask_size
    ):
        """A weighted average of the two quotes cannot escape them."""
        ask = bid + spread
        value = microprice(book([(bid, bid_size)], [(ask, ask_size)]))
        assert isinstance(value, float)
        assert bid - 1e-9 <= value <= ask + 1e-9

    @given(
        bid_size=st.floats(min_value=0.0, max_value=10_000.0),
        ask_size=st.floats(min_value=0.0, max_value=10_000.0),
    )
    @settings(max_examples=200, deadline=None)
    def test_the_imbalance_is_always_in_minus_one_to_one(self, bid_size, ask_size):
        outcome = imbalance(book([(100.0, bid_size)], [(101.0, ask_size)]))
        if isinstance(outcome, tuple):
            assert bid_size + ask_size == 0
        else:
            assert -1.0 <= outcome <= 1.0


# ------------------------------------------------------------------- importer
class TestWideColumnDetection:
    def test_it_recognises_both_orderings_a_capture_tool_writes(self):
        detected = detect_level_columns(
            ["TIMESTAMP", "SEQ", "BID_PX_1", "BID_SZ_1", "ASK1PRICE", "ASK1QTY"]
        )
        assert detected.timestamp == "TIMESTAMP"
        assert detected.sequence == "SEQ"
        assert detected.levels["bid"][1] == {"price": "BID_PX_1", "size": "BID_SZ_1"}
        assert detected.levels["ask"][1] == {"price": "ASK1PRICE", "size": "ASK1QTY"}
        assert detected.depth == 1

    def test_a_column_it_cannot_place_is_reported_not_guessed(self):
        detected = detect_level_columns(["TIMESTAMP", "BID_PX_1", "BID_SZ_1", "SOME_NOTE"])
        assert detected.unrecognised == ("SOME_NOTE",)

    def test_a_level_with_a_price_but_no_size_is_not_counted_as_depth(self):
        detected = detect_level_columns(["TIMESTAMP", "BID_PX_1", "ASK_PX_1", "ASK_SZ_1"])
        assert detected.depth == 0

    def test_the_detected_mapping_travels_into_the_preview(self):
        batch = SnapshotImporter(INSTRUMENT, "fixture").parse(SNAPSHOT_CSV, limit=5)
        assert batch.detected_columns["depth"] == 5
        assert batch.detected_columns["timestamp"] == "TIMESTAMP"


class TestSnapshotImport:
    @pytest.fixture(scope="class")
    @classmethod
    def batch(cls):
        return SnapshotImporter(INSTRUMENT, "fixture").parse(SNAPSHOT_CSV)

    def test_nothing_is_dropped_without_a_reason(self, batch):
        counts = batch.counts()
        assert counts["input"] == counts["kept"] + counts["rejected"]
        assert counts["rejected"] > 0

    def test_every_rejected_row_names_its_row_number_and_reason(self, batch):
        assert batch.rejected
        for row in batch.rejected:
            assert row.row_number >= 1
            assert row.reason in set(SnapshotRejection)
            assert row.message

    def test_the_fixture_seeds_every_snapshot_rejection(self, batch):
        seeded = {row.reason for row in batch.rejected}
        assert seeded == set(SnapshotRejection), sorted(
            str(item) for item in set(SnapshotRejection) - seeded
        )

    def test_a_transposed_book_is_refused_rather_than_sorted(self, batch):
        """Sorting would rescue a file whose price and size columns are swapped
        and turn a detectable mistake into a plausible book."""
        offenders = [
            row for row in batch.rejected if row.reason is SnapshotRejection.LEVELS_OUT_OF_ORDER
        ]
        assert offenders
        assert "not repaired by sorting" in offenders[0].message

    def test_the_kept_snapshots_are_ordered_best_first(self, batch):
        for snapshot in batch.snapshots[:50]:
            bids = [level.price for level in snapshot.bids]
            asks = [level.price for level in snapshot.asks]
            assert bids == sorted(bids, reverse=True)
            assert asks == sorted(asks)

    def test_a_parquet_upload_is_read_directly(self):
        batch = SnapshotImporter(INSTRUMENT, "fixture").parse(BOOK_PARQUET)
        assert batch.rejected == ()
        assert len(batch.snapshots) == 361
        assert batch.detected_columns == {"format": "parquet", "schema": "canonical"}


class TestEventImport:
    @pytest.fixture(scope="class")
    @classmethod
    def batch(cls):
        return EventImporter(INSTRUMENT, "fixture").parse(EVENT_CSV)

    def test_nothing_is_dropped_without_a_reason(self, batch):
        counts = batch.counts()
        assert counts["input"] == counts["kept"] + counts["rejected"]

    def test_the_fixture_seeds_every_event_rejection(self, batch):
        seeded = {row.reason for row in batch.rejected}
        assert seeded == set(EventRejection), sorted(
            str(item) for item in set(EventRejection) - seeded
        )

    def test_the_event_type_is_read_never_inferred(self, batch):
        """A cancellation counted as a trade drains a queue that never drained."""
        unlabelled = [
            row for row in batch.rejected if row.reason is EventRejection.MISSING_EVENT_TYPE
        ]
        assert unlabelled
        assert "not inferable" in unlabelled[0].message

    def test_the_kept_tape_carries_types_sides_prices_and_sequence(self, batch):
        assert len(batch.events) > 2_000
        for event in batch.events[:100]:
            assert event.side is not None
            assert event.price is not None
            assert event.sequence_number is not None


# --------------------------------------------------------------- the gate
def snapshot(offset_seconds: int, levels: int = 5, sequence: int | None = None):
    return OrderBookSnapshot(
        instrument_id=INSTRUMENT,
        exchange_timestamp=START + timedelta(seconds=offset_seconds),
        receive_timestamp=START + timedelta(seconds=offset_seconds),
        bids=tuple(
            OrderBookLevel(price=Decimal(100 - index), quantity=Decimal(10 + index))
            for index in range(levels)
        ),
        asks=tuple(
            OrderBookLevel(price=Decimal(101 + index), quantity=Decimal(10 + index))
            for index in range(levels)
        ),
        source="test",
        sequence_number=sequence,
    )


def event(
    offset_seconds: float,
    sequence: int | None,
    event_type: BookEventType = BookEventType.ADD,
    side: BookSide | None = BookSide.BID,
    price: Decimal | None = Decimal(100),
):
    return BookEvent(
        instrument_id=INSTRUMENT,
        exchange_timestamp=START + timedelta(seconds=offset_seconds),
        event_type=event_type,
        source="test",
        side=side,
        price=price,
        quantity=Decimal(10),
        sequence_number=sequence,
    )


def tape(count: int, span: float = 600.0, **kwargs):
    return tuple(event(index * span / count, index + 1, **kwargs) for index in range(count))


class TestTheAvailabilityGate:
    def test_a_snapshot_only_dataset_gets_book_analytics_and_nothing_else(self):
        report = assess(profile_dataset(INSTRUMENT, tuple(snapshot(i * 5) for i in range(20)), ()))
        assert report.allows(MicrostructureCapability.TOP_OF_BOOK)
        assert report.allows(MicrostructureCapability.DEPTH_ANALYTICS)
        for refused in (
            MicrostructureCapability.EVENT_INTENSITY,
            MicrostructureCapability.CANCELLATION_INTENSITY,
            MicrostructureCapability.SELF_EXCITATION,
            MicrostructureCapability.QUEUE_POSITION,
        ):
            assessment = report[refused]
            assert not assessment.is_available
            assert assessment.reason is AvailabilityRefusal.NO_EVENTS

    def test_an_event_only_dataset_gets_intensity_but_not_a_queue(self):
        report = assess(profile_dataset(INSTRUMENT, (), tape(200)))
        assert report.allows(MicrostructureCapability.EVENT_INTENSITY)
        assert not report.allows(MicrostructureCapability.TOP_OF_BOOK)
        queue = report[MicrostructureCapability.QUEUE_POSITION]
        assert queue.reason is AvailabilityRefusal.NO_SNAPSHOTS

    def test_a_top_of_book_feed_is_refused_depth_analytics(self):
        report = assess(
            profile_dataset(INSTRUMENT, tuple(snapshot(i, levels=1) for i in range(30)), ())
        )
        assert report.allows(MicrostructureCapability.TOP_OF_BOOK)
        depth = report[MicrostructureCapability.DEPTH_ANALYTICS]
        assert depth.reason is AvailabilityRefusal.SINGLE_LEVEL_ONLY
        assert depth.evidence["required_levels"] == MIN_DEPTH_LEVELS

    def test_a_tape_with_no_cancellations_is_refused_a_cancellation_rate(self):
        """Deriving them from size decreases would conflate a cancellation with
        a trade, and those move a queue very differently."""
        report = assess(profile_dataset(INSTRUMENT, (), tape(200, event_type=BookEventType.ADD)))
        cancels = report[MicrostructureCapability.CANCELLATION_INTENSITY]
        assert cancels.reason is AvailabilityRefusal.NO_CANCEL_EVENTS

    def test_a_coarse_clock_is_refused_a_self_exciting_model(self):
        """Half the events share a timestamp, so an inter-arrival model would
        be measuring the recording resolution."""
        events = tuple(event(float(index // 2), index + 1) for index in range(300))
        report = assess(profile_dataset(INSTRUMENT, (), events))
        assert report.allows(MicrostructureCapability.EVENT_INTENSITY)
        excitation = report[MicrostructureCapability.SELF_EXCITATION]
        assert excitation.reason is AvailabilityRefusal.TIMESTAMP_RESOLUTION_TOO_COARSE
        assert excitation.evidence["tied_event_fraction"] > MAX_TIED_EVENT_FRACTION

    def test_a_tape_without_sequence_numbers_is_refused_a_queue(self):
        events = tuple(
            event(index * 3.0, None, event_type=BookEventType.TRADE) for index in range(200)
        )
        report = assess(
            profile_dataset(INSTRUMENT, tuple(snapshot(i * 5) for i in range(20)), events)
        )
        queue = report[MicrostructureCapability.QUEUE_POSITION]
        assert queue.reason is AvailabilityRefusal.NO_SEQUENCE_NUMBERS

    def test_a_tape_with_a_hole_in_it_is_refused_a_queue(self):
        """Each gap may have carried a departure at the level in question."""
        events = tuple(
            event(
                index * 3.0,
                index + 1 + (500 if index > 100 else 0),
                event_type=BookEventType.TRADE,
            )
            for index in range(200)
        )
        report = assess(
            profile_dataset(INSTRUMENT, tuple(snapshot(i * 5) for i in range(20)), events)
        )
        queue = report[MicrostructureCapability.QUEUE_POSITION]
        assert queue.reason is AvailabilityRefusal.SEQUENCE_HAS_GAPS
        assert queue.evidence["sequencing"]["missing_in_range"] == 500

    def test_a_short_window_is_refused_an_arrival_rate(self):
        events = tuple(event(index * 0.1, index + 1) for index in range(200))
        report = assess(profile_dataset(INSTRUMENT, (), events))
        intensity = report[MicrostructureCapability.EVENT_INTENSITY]
        assert intensity.reason is AvailabilityRefusal.WINDOW_TOO_SHORT

    def test_every_verdict_carries_the_evidence_it_was_taken_on(self):
        report = assess(profile_dataset(INSTRUMENT, tuple(snapshot(i * 5) for i in range(20)), ()))
        for assessment in report.assessments:
            assert assessment.message
            assert assessment.evidence
            assert (assessment.reason is None) == assessment.is_available

    def test_require_raises_with_the_reason_attached(self):
        report = assess(profile_dataset(INSTRUMENT, (), tape(200)))
        with pytest.raises(CapabilityRefused) as excinfo:
            report.require(MicrostructureCapability.TOP_OF_BOOK)
        assert excinfo.value.reason is AvailabilityRefusal.NO_SNAPSHOTS
        assert excinfo.value.capability is MicrostructureCapability.TOP_OF_BOOK

    def test_the_thresholds_travel_with_the_report(self):
        """A refusal has to be arguable, which means the bar has to be visible."""
        payload = assess(profile_dataset(INSTRUMENT, (), ())).to_dict()
        assert payload["thresholds"]["min_depth_levels"] == MIN_DEPTH_LEVELS
        assert "gate_version" in payload


class TestSequencing:
    def test_a_complete_contiguous_tape_has_no_gaps(self):
        report = sequencing_report([1, 2, 3, 4, 5])
        assert report.present and report.monotone
        assert report.missing_in_range == 0 and report.duplicates == 0

    def test_a_hole_is_counted(self):
        assert sequencing_report([1, 2, 5]).missing_in_range == 2

    def test_going_backwards_is_not_monotone(self):
        assert not sequencing_report([1, 5, 3]).monotone

    def test_an_absent_numbering_is_reported_as_absent(self):
        assert not sequencing_report([None, None]).present


# ---------------------------------------------------------------- analytics
class TestSessionAnalytics:
    @pytest.fixture(scope="class")
    @classmethod
    def result(cls):
        snapshots = SnapshotImporter(INSTRUMENT, "fixture").parse(SNAPSHOT_CSV).snapshots
        return analyse_snapshots(snapshots, BookAnalyticsParams(levels=5, trade_sizes=(500.0,)))

    def test_every_measure_reports_what_it_was_computed_over(self, result):
        for summary in result.summaries:
            assert summary.observations + summary.missing == result.snapshots_analysed

    def test_a_measure_that_never_existed_says_why(self):
        """One-level snapshots have no slope, and the summary has to carry the
        reason rather than an empty percentile block with no explanation."""
        result = analyse_snapshots(
            tuple(snapshot(index * 5, levels=1) for index in range(10)),
            BookAnalyticsParams(levels=5),
        )
        slope = result.summary("bid_slope_value")
        assert slope.observations == 0
        assert slope.missing == 10
        assert slope.missing_reasons == {str(Unavailable.SINGLE_LEVEL): 10}

    def test_the_preview_series_spans_the_window(self, result):
        preview = result.preview_series()
        assert len(preview) <= result.params.preview_points
        assert preview[0]["timestamp"] == result.first_timestamp
        assert preview[-1]["timestamp"] == result.last_timestamp

    def test_a_refused_trade_size_is_counted_not_extrapolated(self):
        result = analyse_snapshots(
            tuple(snapshot(index * 5) for index in range(10)),
            BookAnalyticsParams(levels=5, trade_sizes=(1e9,)),
        )
        assert result.trade_costs[0].absorbed == 0
        assert result.trade_costs[0].refused == 10
        assert result.trade_costs[0].median_slippage_bps is None

    def test_the_conversion_to_the_numerical_layer_keeps_the_order(self):
        converted = to_book(snapshot(0))
        assert converted.best_bid == 100.0
        assert converted.best_ask == 101.0
        assert converted.spread == pytest.approx(1.0)


# ------------------------------------------------------------------- storage
class TestParquetRoundTrip:
    def test_snapshots_survive_a_round_trip_exactly(self):
        original = tuple(snapshot(index * 5, sequence=index + 1) for index in range(20))
        restored = snapshots_from_parquet(
            snapshots_to_parquet(original, INSTRUMENT, "test", "commit"), INSTRUMENT, "test"
        )
        assert len(restored) == len(original)
        for before, after in zip(original, restored, strict=True):
            assert after.exchange_timestamp == before.exchange_timestamp
            assert after.sequence_number == before.sequence_number
            assert [level.price for level in after.bids] == [level.price for level in before.bids]
            assert [level.quantity for level in after.asks] == [
                level.quantity for level in before.asks
            ]

    def test_a_tick_price_is_not_re_rounded_by_the_store(self):
        """Decimals, not floats: a stored observation is a fact, and 0.05 has
        no exact float representation."""
        original = (
            OrderBookSnapshot(
                instrument_id=INSTRUMENT,
                exchange_timestamp=START,
                receive_timestamp=START,
                bids=(OrderBookLevel(price=Decimal("443.95"), quantity=Decimal("0.00000001")),),
                asks=(OrderBookLevel(price=Decimal("444.05"), quantity=Decimal("7.5")),),
                source="test",
            ),
        )
        restored = snapshots_from_parquet(
            snapshots_to_parquet(original, INSTRUMENT, "test", "commit"), INSTRUMENT, "test"
        )
        assert restored[0].bids[0].price == Decimal("443.95")
        assert restored[0].bids[0].quantity == Decimal("0.00000001")

    def test_events_survive_a_round_trip_exactly(self):
        original = tape(50)
        restored = events_from_parquet(
            events_to_parquet(original, INSTRUMENT, "test", "commit"), INSTRUMENT, "test"
        )
        assert [item.event_type for item in restored] == [item.event_type for item in original]
        assert [item.side for item in restored] == [item.side for item in original]
        assert [item.price for item in restored] == [item.price for item in original]
        assert [item.sequence_number for item in restored] == [
            item.sequence_number for item in original
        ]

    def test_a_book_of_varying_depth_keeps_its_depth(self):
        """List columns rather than a padded fixed width: a padded level is
        indistinguishable from a level quoted at zero."""
        varied = (snapshot(0, levels=1), snapshot(5, levels=4))
        restored = snapshots_from_parquet(
            snapshots_to_parquet(varied, INSTRUMENT, "test", "commit"), INSTRUMENT, "test"
        )
        assert [len(item.bids) for item in restored] == [1, 4]


# --------------------------------------------------------------------- queue
class TestTheQueueBracket:
    def test_the_two_ends_bracket_the_answer(self):
        outlook = estimate_queue_outlook(
            quantity_ahead=500.0,
            level_quantity=800.0,
            trades_observed=40,
            traded_quantity=4_000.0,
            cancels_observed=60,
            cancelled_quantity=3_000.0,
            observation_window_seconds=600.0,
            horizon_seconds=120.0,
        )
        low, high = outlook.fill_probability_range
        assert 0.0 <= low <= high <= 1.0
        fast, slow = outlook.wait_seconds_range
        assert fast <= slow
        assert outlook.optimistic.priority is CancellationPriority.CANCELS_AHEAD
        assert outlook.pessimistic.priority is CancellationPriority.CANCELS_BEHIND

    def test_cancellations_ahead_can_only_drain_the_queue_faster(self):
        outlook = estimate_queue_outlook(
            quantity_ahead=300.0,
            level_quantity=300.0,
            trades_observed=10,
            traded_quantity=500.0,
            cancels_observed=50,
            cancelled_quantity=2_500.0,
            observation_window_seconds=300.0,
            horizon_seconds=60.0,
        )
        assert outlook.optimistic.departure_rate > outlook.pessimistic.departure_rate
        assert outlook.optimistic.expected_wait_seconds <= outlook.pessimistic.expected_wait_seconds

    def test_a_level_with_nothing_ahead_fills_at_once(self):
        outlook = estimate_queue_outlook(
            quantity_ahead=0.0,
            level_quantity=100.0,
            trades_observed=5,
            traded_quantity=100.0,
            cancels_observed=5,
            cancelled_quantity=100.0,
            observation_window_seconds=100.0,
            horizon_seconds=10.0,
        )
        assert outlook.fill_probability_range == (1.0, 1.0)
        assert outlook.wait_seconds_range == (0.0, 0.0)

    def test_a_level_nothing_ever_left_is_refused_not_scored_zero(self):
        """Zero would read as a statement about the order rather than the data."""
        with pytest.raises(QueueUnavailable) as excinfo:
            estimate_queue_outlook(
                quantity_ahead=100.0,
                level_quantity=100.0,
                trades_observed=0,
                traded_quantity=0.0,
                cancels_observed=0,
                cancelled_quantity=0.0,
                observation_window_seconds=600.0,
                horizon_seconds=60.0,
            )
        assert excinfo.value.reason is QueueRefusal.NO_DEPARTURES_OBSERVED

    def test_a_tape_of_only_cancellations_leaves_the_pessimistic_end_hopeless(self):
        """With no trades at all, an order that only advances on trades never
        advances, and the bracket says so at one end and not the other."""
        outlook = estimate_queue_outlook(
            quantity_ahead=200.0,
            level_quantity=200.0,
            trades_observed=0,
            traded_quantity=0.0,
            cancels_observed=40,
            cancelled_quantity=2_000.0,
            observation_window_seconds=400.0,
            horizon_seconds=120.0,
        )
        assert outlook.pessimistic.fill_probability == 0.0
        assert math.isinf(outlook.pessimistic.expected_wait_seconds)
        assert outlook.optimistic.fill_probability > 0.0

    def test_every_assumption_is_stated_including_the_one_about_priority(self):
        outlook = estimate_queue_outlook(
            quantity_ahead=100.0,
            level_quantity=200.0,
            trades_observed=20,
            traded_quantity=1_000.0,
            cancels_observed=20,
            cancelled_quantity=1_000.0,
            observation_window_seconds=300.0,
            horizon_seconds=60.0,
        )
        joined = " ".join(outlook.assumptions).lower()
        assert "first-in-first-out" in joined
        assert "hidden" in joined
        assert "not a claim about where an exchange" in joined

    def test_a_wider_bracket_lowers_the_confidence(self):
        narrow = estimate_queue_outlook(
            quantity_ahead=10.0,
            level_quantity=100.0,
            trades_observed=60,
            traded_quantity=6_000.0,
            cancels_observed=60,
            cancelled_quantity=6_000.0,
            observation_window_seconds=600.0,
            horizon_seconds=600.0,
        )
        wide = estimate_queue_outlook(
            quantity_ahead=400.0,
            level_quantity=400.0,
            trades_observed=5,
            traded_quantity=200.0,
            cancels_observed=60,
            cancelled_quantity=6_000.0,
            observation_window_seconds=600.0,
            horizon_seconds=60.0,
        )
        narrow_low, narrow_high = narrow.fill_probability_range
        wide_low, wide_high = wide.fill_probability_range
        assert (wide_high - wide_low) > (narrow_high - narrow_low)
        assert wide.confidence < narrow.confidence

    def test_the_payload_has_no_field_that_could_hold_a_single_probability(self):
        payload = estimate_queue_outlook(
            quantity_ahead=100.0,
            level_quantity=200.0,
            trades_observed=20,
            traded_quantity=1_000.0,
            cancels_observed=20,
            cancelled_quantity=1_000.0,
            observation_window_seconds=300.0,
            horizon_seconds=60.0,
        ).to_dict()
        assert "estimated_fill_probability_range" in payload
        assert "fill_probability" not in payload
        assert "estimated_fill_probability" not in payload

    def test_the_domain_layer_defaults_to_the_whole_displayed_level(self):
        """An order joining now is behind all of it; assuming less would assume
        priority the venue never granted."""
        snapshots = (snapshot(0), snapshot(60))
        events = tuple(
            event(
                index * 3.0,
                index + 1,
                event_type=BookEventType.TRADE if index % 2 else BookEventType.CANCEL,
                price=Decimal(100),
            )
            for index in range(40)
        )
        result = estimate_queue(
            snapshots, events, QueueParams(side=BookSide.BID, horizon_seconds=60.0)
        )
        assert result.price == Decimal(100)
        assert result.outlook.quantity_ahead == float(snapshots[-1].bids[0].quantity)
        assert result.outlook.queue_position_fraction == pytest.approx(1.0)

    def test_events_at_another_price_are_not_counted_as_departures(self):
        """A cancellation one tick away drained a different queue."""
        snapshots = (snapshot(0),)
        elsewhere = tuple(
            event(index * 3.0, index + 1, event_type=BookEventType.TRADE, price=Decimal(99))
            for index in range(40)
        )
        with pytest.raises(QueueUnavailable):
            estimate_queue(
                snapshots, elsewhere, QueueParams(side=BookSide.BID, horizon_seconds=60.0)
            )
