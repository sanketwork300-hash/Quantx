"""The rule that decides whether a file was read at all.

The distinction under test is between a row that could not be *read* -- its
column does not hold what it was taken to hold -- and a row that is simply
*empty*, which every exchange chain carries at its far strikes. Confusing the
two either refuses legitimate downloads or accepts misread ones.
"""

from __future__ import annotations

import pytest

from domains.market_data.ingestion.column_mapping import (
    OPTION_CHAIN_FIELDS,
    ColumnMapping,
)
from domains.market_data.ingestion.layout import TwoSidedLayout
from domains.market_data.ingestion.parser import TabularParser
from domains.market_data.ingestion.reading import (
    STRUCTURAL_REJECTIONS,
    ReadingProblem,
    ReadingRefused,
    ReadingSource,
    SampleRow,
    assess_rejections,
    assess_sample,
    describe,
    obstacle,
    sample,
)
from domains.market_data.ingestion.validator import RejectedRow, RejectionReason

HEADERS = ["EXPIRY_DT", "STRIKE_PRICE", "CE_PE", "BID", "ASK", "LTP"]
MAPPING = ColumnMapping(
    mapping={
        "expiry": "EXPIRY_DT",
        "strike": "STRIKE_PRICE",
        "option_type": "CE_PE",
        "bid_price": "BID",
        "ask_price": "ASK",
        "last_price": "LTP",
    }
)


def rows(*specs: tuple[bool, bool]) -> list[SampleRow]:
    """Build a sample from (read, structural) pairs."""
    return [
        SampleRow(
            row_number=index + 1,
            read=read,
            values={},
            problem=None if read else "no",
            reason=None if read else "MISSING_EXPIRY" if structural else "NO_PRICE_FIELDS",
            structural=structural,
        )
        for index, (read, structural) in enumerate(specs)
    ]


class TestTheVerdict:
    def test_a_file_that_reads_is_readable(self):
        verdict = assess_sample(rows((True, False), (True, False)))
        assert verdict.readable is True
        assert verdict.problem is None

    def test_rows_are_conserved(self):
        verdict = assess_sample(rows((True, False), (False, True), (False, False)))
        assert (
            verdict.rows_examined
            == verdict.rows_read + verdict.rows_unreadable + verdict.rows_empty
        )

    def test_an_empty_far_strike_is_not_a_misreading(self):
        """Blank prices on one side are what an exchange chain looks like."""
        verdict = assess_sample(rows(*[(False, False)] * 8, *[(True, False)] * 2))
        assert verdict.readable is True
        assert verdict.rows_empty == 8
        assert verdict.rows_unreadable == 0

    def test_a_sample_of_nothing_but_quiet_strikes_is_not_a_failed_reading(self):
        """An exchange chain opens on far strikes where neither side is quoted.

        The defect this covers: the sample was held to "nothing became a quote",
        so a file whose first strikes were unquoted was refused in the preview
        and at submission, although the whole-file rule read it without
        complaint. A sample's rows being empty says where the file starts.
        """
        verdict = assess_sample(rows(*[(False, False)] * 50))
        assert verdict.readable is True
        assert verdict.problem is None
        assert verdict.rows_read == 0
        assert verdict.rows_empty == 50
        assert "whole file" in verdict.message

    def test_a_sample_where_nothing_was_read_for_structural_reasons_is_refused(self):
        verdict = assess_sample(rows(*[(False, True)] * 6, *[(False, False)] * 4))
        assert verdict.readable is False
        assert verdict.problem is ReadingProblem.NO_ROW_COULD_BE_READ

    def test_a_whole_file_where_nothing_became_a_quote_is_refused(self):
        """Only the whole file can say nothing was there, and it still does."""
        empty = [
            RejectedRow(index, RejectionReason.NO_PRICE_FIELDS, "no price", {})
            for index in range(1, 5)
        ]
        verdict = assess_rejections(0, empty)
        assert verdict.readable is False
        assert verdict.problem is ReadingProblem.NO_ROW_COULD_BE_READ

    def test_a_sample_is_sized_in_lines_of_the_file_not_in_quotes(self):
        """A two-sided export yields two quotes per line; the user counts lines."""
        call_and_put = [
            SampleRow(row_number=line, read=True) for line in (1, 2, 3) for _ in ("CALL", "PUT")
        ]
        verdict = assess_sample(call_and_put)
        assert verdict.rows_examined == 6
        assert verdict.source_rows == 3

    def test_a_file_that_mostly_failed_structurally_is_refused(self):
        verdict = assess_sample(rows(*[(False, True)] * 6, *[(True, False)] * 4))
        assert verdict.readable is False
        assert verdict.problem is ReadingProblem.MOST_ROWS_COULD_NOT_BE_READ

    def test_a_structural_minority_is_dirty_data_rather_than_a_wrong_column(self):
        verdict = assess_sample(rows(*[(False, True)] * 4, *[(True, False)] * 6))
        assert verdict.readable is True

    def test_an_empty_file_is_refused(self):
        verdict = assess_sample([])
        assert verdict.problem is ReadingProblem.FILE_HAS_NO_DATA_ROWS

    def test_a_required_field_with_no_column_is_named(self):
        verdict = assess_sample(rows((True, False)), missing_required=["expiry"])
        assert verdict.problem is ReadingProblem.REQUIRED_FIELD_NOT_FOUND
        assert verdict.missing_required == ("expiry",)

    def test_the_message_states_the_rule_it_applied(self):
        verdict = assess_sample(rows(*[(False, True)] * 6, *[(True, False)] * 4))
        assert "more than half" in verdict.message

    def test_the_reasons_are_counted_so_the_user_can_repair_the_file(self):
        verdict = assess_sample(rows((False, True), (False, True), (False, False)))
        assert verdict.reasons == {"MISSING_EXPIRY": 2, "NO_PRICE_FIELDS": 1}


class TestWhichRejectionsCountAgainstTheReading:
    """Each refusal is classified by what it says about the *columns*.

    A column that holds prices where dates belong is a wrong reading. A row
    with no prices on one side is an ordinary far strike. The two must not be
    counted together, or every real chain would be refused.
    """

    @pytest.mark.parametrize(
        ("row", "reason", "structural"),
        [
            (b"1562.85,22600,CE,1.00,2.00,1.50\n", "UNPARSEABLE_ROW", True),
            (b"2026-10-29,,CE,1.00,2.00,1.50\n", "MISSING_STRIKE", True),
            (b"2026-10-29,0,CE,1.00,2.00,1.50\n", "NON_POSITIVE_STRIKE", True),
            (b",22600,CE,1.00,2.00,1.50\n", "MISSING_EXPIRY", True),
            (b"2026-10-29,22600,,1.00,2.00,1.50\n", "MISSING_OPTION_TYPE", True),
            (b"2026-10-29,22600,CE,,,\n", "NO_PRICE_FIELDS", False),
        ],
    )
    def test_each_reason_is_classified(self, row, reason, structural):
        parsed = TabularParser(OPTION_CHAIN_FIELDS, max_rows=10).parse(
            b"EXPIRY_DT,STRIKE_PRICE,CE_PE,BID,ASK,LTP\n" + row, MAPPING
        )
        seen = sample(parsed)[0]
        assert seen.read is False
        assert seen.reason == reason
        assert seen.structural is structural

    def test_every_rejection_reason_has_been_considered(self):
        """A new reason must be classified deliberately, not default to safe."""
        classified = STRUCTURAL_REJECTIONS | {
            RejectionReason.NO_PRICE_FIELDS,
            RejectionReason.SYMBOL_MISMATCH,
            RejectionReason.NOT_AN_OPTION,
            RejectionReason.INSTRUMENT_UNRESOLVED,
            RejectionReason.INSTRUMENT_AMBIGUOUS,
        }
        assert set(RejectionReason) == classified


class TestAFileThatReadsTwoWaysIsNotReadEitherWay:
    """Some files cannot be read one way, whatever their rows hold.

    A choice would have to be made for the user, the chain would look entirely
    plausible, and nothing downstream could tell it had been a choice.
    """

    def _parse(self, data: bytes, mapping: ColumnMapping = MAPPING, **options):
        parser = TabularParser(OPTION_CHAIN_FIELDS, max_rows=50, resolve_date_order=True)
        return parser.parse(data, mapping, **options)

    def test_a_date_column_that_never_says_which_number_is_the_day(self):
        parsed = self._parse(
            b"EXPIRY_DT,STRIKE_PRICE,CE_PE,BID,ASK,LTP\n03/04/2026,22600,CE,1.00,2.00,1.50\n"
        )
        problem, message = obstacle(parsed)
        assert problem is ReadingProblem.AMBIGUOUS_DATE_ORDER
        assert "'03/04/2026'" in message
        assert "DMY or MDY" in message
        verdict = assess_sample(sample(parsed), obstacle=obstacle(parsed))
        assert verdict.readable is False
        assert verdict.problem is ReadingProblem.AMBIGUOUS_DATE_ORDER

    def test_a_date_column_that_says_both(self):
        parsed = self._parse(
            b"EXPIRY_DT,STRIKE_PRICE,CE_PE,BID,ASK,LTP\n"
            b"13/01/2026,22600,CE,1.00,2.00,1.50\n"
            b"01/13/2026,22600,PE,1.00,2.00,1.50\n"
        )
        problem, message = obstacle(parsed)
        assert problem is ReadingProblem.AMBIGUOUS_DATE_ORDER
        assert "13/01/2026 and 01/13/2026" in message

    def test_a_field_read_from_a_header_name_that_occurs_twice(self):
        parsed = self._parse(
            b"EXPIRY_DT,STRIKE_PRICE,CE_PE,BID,ASK,LTP,BID\n"
            b"2026-10-29,22600,CE,1.00,2.00,1.50,999\n"
        )
        problem, message = obstacle(parsed)
        assert problem is ReadingProblem.AMBIGUOUS_COLUMN
        assert "'BID'" in message

    def test_a_repeated_header_nothing_is_read_from_is_not_an_obstacle(self):
        parsed = self._parse(
            b"EXPIRY_DT,STRIKE_PRICE,CE_PE,BID,ASK,LTP,CHNG,CHNG\n"
            b"2026-10-29,22600,CE,1.00,2.00,1.50,1,2\n"
        )
        assert obstacle(parsed) is None

    def test_an_obstacle_refuses_the_whole_file_too(self):
        parsed = self._parse(
            b"EXPIRY_DT,STRIKE_PRICE,CE_PE,BID,ASK,LTP\n03/04/2026,22600,CE,1.00,2.00,1.50\n"
        )
        verdict = assess_rejections(0, [], obstacle=obstacle(parsed))
        assert verdict.problem is ReadingProblem.AMBIGUOUS_DATE_ORDER


class TestTheSample:
    def _parse(self, data: bytes):
        return TabularParser(OPTION_CHAIN_FIELDS, max_rows=50).parse(data, MAPPING)

    def test_unreadable_rows_are_kept_in_file_order(self):
        parsed = self._parse(
            b"EXPIRY_DT,STRIKE_PRICE,CE_PE,BID,ASK,LTP\n"
            b"2026-10-29,22600,CE,1.00,2.00,1.50\n"
            b"not-a-date,22700,CE,1.00,2.00,1.50\n"
            b"2026-10-29,22800,PE,1.00,2.00,1.50\n"
        )
        seen = sample(parsed)
        assert [row.row_number for row in seen] == [1, 2, 3]
        assert [row.read for row in seen] == [True, False, True]

    def test_an_unreadable_row_carries_its_reason(self):
        parsed = self._parse(
            b"EXPIRY_DT,STRIKE_PRICE,CE_PE,BID,ASK,LTP\nnot-a-date,22700,CE,1.00,2.00,1.50\n"
        )
        row = sample(parsed)[0]
        assert row.reason == str(RejectionReason.UNPARSEABLE_ROW)
        assert row.structural is True
        assert "EXPIRY_DT" in row.problem

    def test_a_row_that_parses_but_cannot_become_a_quote_is_not_shown_as_read(self):
        """The old sample stopped at parsing, so these looked fine and then vanished."""
        parsed = self._parse(b"EXPIRY_DT,STRIKE_PRICE,CE_PE,BID,ASK,LTP\n2026-10-29,22700,CE,,,\n")
        row = sample(parsed)[0]
        assert row.read is False
        assert row.reason == str(RejectionReason.NO_PRICE_FIELDS)
        assert row.structural is False, "an empty row is not evidence of a wrong column"

    def test_values_are_rendered_without_losing_precision(self):
        parsed = self._parse(
            b"EXPIRY_DT,STRIKE_PRICE,CE_PE,BID,ASK,LTP\n2026-10-29,22600.50,CE,1.00,2.00,1.50\n"
        )
        assert sample(parsed)[0].values["strike"] == "22600.50"


class TestDescribingALongFormReading:
    def test_a_column_nobody_named_is_reported_as_detected(self):
        reading = {item.field: item for item in describe(OPTION_CHAIN_FIELDS, MAPPING, HEADERS)}
        assert reading["strike"].source is ReadingSource.DETECTED_COLUMN
        assert reading["strike"].columns[0].header == "STRIKE_PRICE"
        assert reading["strike"].columns[0].index == 1

    def test_a_column_the_caller_named_is_attributed_to_them(self):
        reading = {
            item.field: item
            for item in describe(
                OPTION_CHAIN_FIELDS,
                MAPPING,
                HEADERS,
                supplied=ColumnMapping(mapping={"last_price": "LTP"}),
            )
        }
        assert reading["last_price"].source is ReadingSource.SUPPLIED_COLUMN
        assert reading["strike"].source is ReadingSource.DETECTED_COLUMN

    def test_a_field_with_no_column_says_so_rather_than_disappearing(self):
        reading = {item.field: item for item in describe(OPTION_CHAIN_FIELDS, MAPPING, HEADERS)}
        assert reading["open_interest"].source is ReadingSource.NOT_IN_FILE
        assert reading["open_interest"].detail

    def test_every_field_is_accounted_for(self):
        reading = describe(OPTION_CHAIN_FIELDS, MAPPING, HEADERS)
        assert {item.field for item in reading} == {spec.name for spec in OPTION_CHAIN_FIELDS}


class TestDescribingATwoSidedReading:
    LAYOUT = TwoSidedLayout(
        header_row=0,
        strike_column=3,
        call_columns={"bid_price": 1, "ask_price": 2},
        put_columns={"bid_price": 4, "ask_price": 5},
        expiry=None,
    )
    HEADERS = ["OI", "BID", "ASK", "STRIKE", "BID", "ASK"]

    def test_the_side_comes_from_position_not_from_a_column(self):
        reading = {
            item.field: item
            for item in describe(
                OPTION_CHAIN_FIELDS, ColumnMapping(), self.HEADERS, layout=self.LAYOUT
            )
        }
        assert reading["option_type"].source is ReadingSource.IMPLIED_BY_POSITION
        assert reading["option_type"].columns == ()

    def test_a_price_field_names_both_of_its_columns(self):
        reading = {
            item.field: item
            for item in describe(
                OPTION_CHAIN_FIELDS,
                ColumnMapping(),
                self.HEADERS,
                layout=self.LAYOUT,
                detected_layout=self.LAYOUT,
            )
        }
        sides = {column.side: column.index for column in reading["bid_price"].columns}
        assert sides == {"CALL": 1, "PUT": 4}
        assert reading["bid_price"].source is ReadingSource.DETECTED_COLUMN

    def test_one_corrected_column_makes_that_field_the_callers(self):
        corrected = TwoSidedLayout(
            header_row=0,
            strike_column=3,
            call_columns={"bid_price": 0, "ask_price": 2},
            put_columns={"bid_price": 4, "ask_price": 5},
            expiry=None,
        )
        reading = {
            item.field: item
            for item in describe(
                OPTION_CHAIN_FIELDS,
                ColumnMapping(),
                self.HEADERS,
                layout=corrected,
                detected_layout=self.LAYOUT,
            )
        }
        assert reading["bid_price"].source is ReadingSource.SUPPLIED_COLUMN
        assert reading["ask_price"].source is ReadingSource.DETECTED_COLUMN

    def test_an_expiry_that_is_in_no_column_says_where_it_came_from(self):
        from datetime import date

        dated = TwoSidedLayout(
            header_row=0,
            strike_column=3,
            call_columns={"bid_price": 1},
            put_columns={"bid_price": 4},
            expiry=date(2026, 9, 15),
        )
        reading = {
            item.field: item
            for item in describe(
                OPTION_CHAIN_FIELDS,
                ColumnMapping(),
                self.HEADERS,
                layout=dated,
                detected_layout=dated,
                expiry_source="filename",
            )
        }
        assert reading["expiry"].source is ReadingSource.STATED_SEPARATELY
        assert "filename" in reading["expiry"].detail
        assert "term structure" in reading["expiry"].detail


class TestTheRefusal:
    def test_it_carries_the_diagnosis_rather_than_only_a_message(self):
        verdict = assess_sample(rows((False, True), (False, True)))
        refusal = ReadingRefused(verdict, describe(OPTION_CHAIN_FIELDS, MAPPING, HEADERS))
        assert refusal.code == "NO_ROW_COULD_BE_READ"
        assert refusal.details["verdict"]["rows_read"] == 0
        fields = {item["field"] for item in refusal.details["reading"]}
        assert "strike" in fields

    def test_it_is_a_value_error_so_a_worker_records_it_as_a_failure(self):
        refusal = ReadingRefused(assess_sample([]))
        assert isinstance(refusal, ValueError)
        assert str(refusal)
