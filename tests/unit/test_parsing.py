"""Column mapping and tabular parsing."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from domains.instruments.enums import OptionType
from domains.market_data.ingestion.column_mapping import (
    OPTION_CHAIN_FIELDS,
    ColumnMapping,
    infer_mapping,
)
from domains.market_data.ingestion.parser import (
    NOT_AN_OPTION,
    DateOrder,
    DateProblem,
    RowParseError,
    TabularParser,
    detect_delimiter,
    parse_decimal,
    parse_integer,
)
from domains.market_data.ingestion.validator import (
    NON_OPTION_TOKENS,
    OptionChainRowValidator,
    RejectedRow,
    RejectionReason,
)

HEADER = "EXPIRY_DT,STRIKE_PRICE,CE_PE,BID,ASK,LTP,VOL,OI,UNDERLYING_VALUE"


def csv_bytes(*rows: str) -> bytes:
    return ("\n".join([HEADER, *rows]) + "\n").encode()


def parser() -> TabularParser:
    return TabularParser(OPTION_CHAIN_FIELDS, max_rows=1000)


def mapping() -> ColumnMapping:
    return infer_mapping(HEADER.split(","), OPTION_CHAIN_FIELDS)


class TestInference:
    def test_infers_messy_real_world_headers(self):
        resolved = mapping().to_dict()
        assert resolved["strike"] == "STRIKE_PRICE"
        assert resolved["option_type"] == "CE_PE"
        assert resolved["expiry"] == "EXPIRY_DT"
        assert resolved["bid_price"] == "BID"
        assert resolved["ask_price"] == "ASK"
        assert resolved["last_price"] == "LTP"
        assert resolved["volume"] == "VOL"
        assert resolved["open_interest"] == "OI"
        assert resolved["underlying_price"] == "UNDERLYING_VALUE"

    def test_reports_unmapped_required_fields(self):
        partial = infer_mapping(["BID", "ASK"], OPTION_CHAIN_FIELDS)
        assert set(partial.missing_required(OPTION_CHAIN_FIELDS)) == {
            "strike",
            "option_type",
            "expiry",
        }

    def test_reports_columns_it_did_not_use(self):
        headers = [*HEADER.split(","), "SOME_BROKER_FIELD"]
        inferred = infer_mapping(headers, OPTION_CHAIN_FIELDS)
        assert inferred.unmapped_columns(headers) == ("SOME_BROKER_FIELD",)


class TestParsing:
    def test_parses_a_clean_row(self):
        result = parser().parse(
            csv_bytes("2026-10-29,24000,CE,412.10,415.60,414.00,1000,5000,24012.35"),
            mapping(),
        )
        assert result.errors == []
        values = result.rows[0].values
        assert values["expiry"] == date(2026, 10, 29)
        assert values["strike"] == Decimal("24000")
        assert values["option_type"] is OptionType.CALL
        assert values["bid_price"] == Decimal("412.10")

    def test_a_bad_row_does_not_abort_the_file(self):
        """39,997 good rows and three reported problems beats a stack trace."""
        result = parser().parse(
            csv_bytes(
                "2026-10-29,24000,CE,412.10,415.60,414.00,1000,5000,24012.35",
                "2026-10-29,oops,CE,412.10,415.60,414.00,1000,5000,24012.35",
                "2026-10-29,24100,PE,1.10,1.60,1.20,1000,5000,24012.35",
            ),
            mapping(),
        )
        assert len(result.rows) == 2
        assert len(result.errors) == 1
        assert result.errors[0].row_number == 2
        assert result.errors[0].column == "STRIKE_PRICE"

    def test_row_numbers_are_one_based_excluding_the_header(self):
        result = parser().parse(
            csv_bytes(
                "2026-10-29,24000,CE,1,2,1.5,1,1,24000",
                "2026-10-29,24100,CE,1,2,1.5,1,1,24000",
            ),
            mapping(),
        )
        assert [row.row_number for row in result.rows] == [1, 2]

    @pytest.mark.parametrize("token", ["", "-", "NA", "n/a", "null", "--"])
    def test_null_tokens_become_none(self, token):
        result = parser().parse(
            csv_bytes(f"2026-10-29,24000,CE,{token},415.60,414.00,1000,5000,24012.35"),
            mapping(),
        )
        assert result.rows[0].values["bid_price"] is None

    def test_thousands_separators_are_accepted(self):
        result = parser().parse(
            csv_bytes('2026-10-29,24000,CE,412.10,415.60,414.00,"1,000","5,000",24012.35'),
            mapping(),
        )
        assert result.rows[0].values["volume"] == Decimal("1000")

    @pytest.mark.parametrize(
        "token,expected",
        [
            ("2026-10-29", date(2026, 10, 29)),
            ("29-10-2026", date(2026, 10, 29)),
            ("29/10/2026", date(2026, 10, 29)),
            ("29-Oct-2026", date(2026, 10, 29)),
        ],
    )
    def test_date_formats(self, token, expected):
        result = parser().parse(csv_bytes(f"{token},24000,CE,1,2,1.5,1,1,24000"), mapping())
        assert result.rows[0].values["expiry"] == expected

    def test_a_formula_cell_is_data_not_a_formula(self):
        """No spreadsheet evaluation, ever. The cell is text that fails to parse."""
        result = parser().parse(csv_bytes("2026-10-29,=1+1,CE,1,2,1.5,1,1,24000"), mapping())
        assert result.rows == []
        assert len(result.errors) == 1

    def test_an_unreadable_optional_cell_rejects_the_row_by_default(self):
        result = parser().parse(csv_bytes("2026-10-29,24000,CE,1,2,1.5,1.2K,1,24000"), mapping())
        assert result.rows == []
        assert result.errors[0].column == "VOL"

    def test_a_lenient_parser_keeps_the_row_and_reports_the_cell(self):
        """The row has a strike, an expiry, a side and a price: it is a quote.

        The defect this covers: a volume of ``1.2K``, or a ``time`` column
        holding ``15:30:00``, rejected every row and so refused the whole file.
        """
        lenient = TabularParser(OPTION_CHAIN_FIELDS, max_rows=1000, lenient_optional=True)
        result = lenient.parse(csv_bytes("2026-10-29,24000,CE,1,2,1.5,1.2K,1,24000"), mapping())
        assert result.errors == []
        assert result.rows[0].values["volume"] is None
        assert result.rows[0].values["bid_price"] == Decimal("1")
        issue = result.cell_issues[0]
        assert (issue.row_number, issue.field, issue.column, issue.value) == (
            1,
            "volume",
            "VOL",
            "1.2K",
        )

    def test_a_lenient_parser_still_rejects_an_unreadable_required_cell(self):
        lenient = TabularParser(OPTION_CHAIN_FIELDS, max_rows=1000, lenient_optional=True)
        result = lenient.parse(csv_bytes("not-a-date,24000,CE,1,2,1.5,1,1,24000"), mapping())
        assert result.rows == []
        assert len(result.errors) == 1
        assert result.cell_issues == []

    def test_respects_a_preview_limit_without_calling_it_truncation(self):
        rows = [f"2026-10-29,{24000 + i},CE,1,2,1.5,1,1,24000" for i in range(10)]
        result = parser().parse(csv_bytes(*rows), mapping(), limit=3)
        assert len(result.rows) == 3
        assert result.truncated is False

    def test_reports_truncation_at_the_configured_cap(self):
        small = TabularParser(OPTION_CHAIN_FIELDS, max_rows=2)
        rows = [f"2026-10-29,{24000 + i},CE,1,2,1.5,1,1,24000" for i in range(5)]
        result = small.parse(csv_bytes(*rows), mapping())
        assert result.truncated is True

    def test_read_headers_without_parsing(self):
        assert TabularParser.read_headers(csv_bytes()) == HEADER.split(",")


class TestValidation:
    def _row(self, line: str):
        result = parser().parse(csv_bytes(line), mapping())
        if result.errors:
            return RejectedRow(
                row_number=1,
                reason=RejectionReason.UNPARSEABLE_ROW,
                message=result.errors[0].message,
                raw={},
            )
        return OptionChainRowValidator("NIFTY").validate(result.rows[0])

    def test_accepts_a_usable_row(self):
        outcome = self._row("2026-10-29,24000,CE,412.10,415.60,414.00,1000,5000,24012.35")
        assert not isinstance(outcome, RejectedRow)

    def test_rejects_a_non_positive_strike(self):
        outcome = self._row("2026-10-29,0,CE,1,2,1.5,1,1,24000")
        assert isinstance(outcome, RejectedRow)
        assert outcome.reason is RejectionReason.NON_POSITIVE_STRIKE

    def test_rejects_a_row_with_no_prices(self):
        outcome = self._row("2026-10-29,24000,CE,,,,1,1,24000")
        assert isinstance(outcome, RejectedRow)
        assert outcome.reason is RejectionReason.NO_PRICE_FIELDS

    def test_rejects_a_missing_expiry(self):
        outcome = self._row(",24000,CE,1,2,1.5,1,1,24000")
        assert isinstance(outcome, RejectedRow)

    def test_rejects_a_missing_option_type(self):
        outcome = self._row("2026-10-29,24000,,1,2,1.5,1,1,24000")
        assert isinstance(outcome, RejectedRow)


class TestNumbersAreNotRepaired:
    """A number the file does not hold is never manufactured from one it does."""

    @pytest.mark.parametrize(
        ("token", "expected"),
        [
            ("1,877.00", Decimal("1877.00")),
            ("24,072", Decimal("24072")),
            ("1,234,567.5", Decimal("1234567.5")),
            ("12,34,567", Decimal("1234567")),  # grouped the Indian way, as NSE writes it
            ("-4.60", Decimal("-4.60")),
        ],
    )
    def test_a_grouped_number_is_read(self, token, expected):
        assert parse_decimal(token) == expected

    @pytest.mark.parametrize("token", ["24000,50", "1.234,56", "1,5", "12,3456"])
    def test_a_comma_that_is_not_a_group_separator_is_refused(self, token):
        """``24000,50`` with its comma deleted is 2,400,050: a hundred times too large."""
        with pytest.raises(RowParseError, match="thousands separator"):
            parse_decimal(token)

    def test_a_whole_number_is_an_integer(self):
        assert parse_integer("1,234") == 1234
        assert parse_integer("7.0") == 7

    def test_a_fraction_is_not_truncated_into_an_integer(self):
        with pytest.raises(RowParseError, match="not an integer"):
            parse_integer("1.9")


class TestTheShapeOfTheFileIsReadOffTheFile:
    ROWS = (
        ("2026-10-29", "24000", "CE", "412.10", "415.60", "414.00", "1000", "5000", "24012.35"),
        ("2026-10-29", "24000", "PE", "20.10", "21.60", "21.00", "900", "4000", "24012.35"),
    )

    def _file(self, delimiter: str, above: str = "") -> bytes:
        lines = [delimiter.join(HEADER.split(",")), *(delimiter.join(row) for row in self.ROWS)]
        return (above + "\n".join(lines) + "\n").encode()

    @pytest.mark.parametrize("delimiter", [",", ";", "\t"])
    def test_the_delimiter_is_detected(self, delimiter):
        data = self._file(delimiter)
        assert detect_delimiter(data.decode()) == delimiter
        result = parser().parse(data, mapping())
        assert result.delimiter == delimiter
        assert len(result.rows) == 2
        assert result.rows[1].values["bid_price"] == Decimal("20.10")

    def test_a_quoted_thousands_comma_does_not_make_a_semicolon_file_a_comma_file(self):
        data = b'STRIKE;BID;ASK\n"24,000";"1,877.00";"1,925.30"\n"24,050";"1,830.00";"1,880.30"\n'
        assert detect_delimiter(data.decode()) == ";"

    def test_read_headers_uses_the_detected_delimiter(self):
        assert TabularParser.read_headers(self._file(";")) == HEADER.split(",")

    def test_a_title_line_above_the_header_is_not_the_header(self):
        data = self._file(",", above="NIFTY option chain as on 30-Sep-2026\n\n")
        result = parser().parse(data, mapping())
        assert result.header_row == 2
        assert result.headers == HEADER.split(",")
        assert [row.row_number for row in result.rows] == [1, 2]
        assert TabularParser.read_headers(data, OPTION_CHAIN_FIELDS) == HEADER.split(",")

    def test_the_first_line_stays_the_header_when_nothing_beats_it(self):
        assert parser().parse(b"a,b,c\n1,2,3\n", ColumnMapping()).header_row == 0

    def test_a_mapped_header_name_that_repeats_is_reported(self):
        data = (HEADER + ",BID\n" + ",".join(self.ROWS[0]) + ",999\n").encode()
        assert parser().parse(data, mapping()).duplicate_headers == ("BID",)

    def test_a_timestamp_with_no_offset_is_counted(self):
        data = (
            HEADER
            + ",TIMESTAMP\n"
            + ",".join(self.ROWS[0])
            + ",2026-09-30 15:30:00\n"
            + ",".join(self.ROWS[1])
            + ",2026-09-30T15:30:00+05:30\n"
        ).encode()
        headers = [*HEADER.split(","), "TIMESTAMP"]
        result = parser().parse(data, infer_mapping(headers, OPTION_CHAIN_FIELDS))
        assert result.naive_timestamps == {"TIMESTAMP": 1}


class TestTheOrderOfANumericDateIsSettledPerColumn:
    """``03/04/2026`` is 3 April or 4 March, and the cell does not say which.

    Read cell by cell, one column came out day-first in some rows and
    month-first in others. The order is settled once, from a value that reads
    only one way or from the caller, and otherwise not at all.
    """

    def _parse(self, *dates: str, **options):
        strict = TabularParser(OPTION_CHAIN_FIELDS, max_rows=1000, resolve_date_order=True)
        rows = [f"{token},{24000 + i},CE,1,2,1.5,1,1,24000" for i, token in enumerate(dates)]
        return strict.parse(csv_bytes(*rows), mapping(), **options)

    def test_one_unambiguous_value_settles_the_whole_column(self):
        result = self._parse("03/04/2026", "29/10/2026")
        assert [row.values["expiry"] for row in result.rows] == [
            date(2026, 4, 3),
            date(2026, 10, 29),
        ]
        reading = result.date_readings[0]
        assert (reading.order, reading.example, reading.stated) == (
            DateOrder.DAY_FIRST,
            "29/10/2026",
            False,
        )

    def test_a_month_first_column_is_read_month_first_throughout(self):
        result = self._parse("03/04/2026", "10/29/2026")
        assert [row.values["expiry"] for row in result.rows] == [
            date(2026, 3, 4),
            date(2026, 10, 29),
        ]
        assert result.date_readings[0].order is DateOrder.MONTH_FIRST

    def test_the_sample_reads_the_column_the_way_the_file_does(self):
        """The value that settles it is past the preview limit."""
        result = self._parse("03/04/2026", "05/06/2026", "29/10/2026", limit=1)
        assert result.rows[0].values["expiry"] == date(2026, 4, 3)

    def test_a_column_that_never_says_is_not_read(self):
        result = self._parse("03/04/2026", "05/06/2026")
        assert result.rows == []
        assert result.date_readings[0].problem is DateProblem.AMBIGUOUS
        assert "ambiguous date" in result.errors[0].message

    def test_a_column_that_says_both_is_not_read(self):
        result = self._parse("13/01/2026", "01/13/2026")
        assert result.date_readings[0].problem is DateProblem.CONFLICTING

    def test_the_caller_can_say(self):
        result = self._parse("03/04/2026", date_order=DateOrder.MONTH_FIRST)
        assert result.rows[0].values["expiry"] == date(2026, 3, 4)
        assert result.date_readings[0].stated is True

    def test_a_value_that_contradicts_the_stated_order_is_refused_by_row(self):
        result = self._parse("03/04/2026", "29/10/2026", date_order=DateOrder.MONTH_FIRST)
        assert len(result.rows) == 1
        assert "month first" in result.errors[0].message

    def test_a_date_that_reads_one_way_needs_no_order(self):
        result = self._parse("2026-10-29", "29-Oct-2026", "05/05/2026")
        assert len(result.rows) == 3
        assert result.date_readings == ()


class TestARowThatIsNotAnOption:
    """A bhavcopy lists futures beside the options and marks them ``XX``."""

    def _row(self, row: str):
        chain = TabularParser(
            OPTION_CHAIN_FIELDS, max_rows=1000, non_option_tokens=NON_OPTION_TOKENS
        )
        result = chain.parse(csv_bytes(row), mapping())
        assert result.errors == []
        return result.rows[0]

    def test_it_is_read_rather_than_failing_to_parse(self):
        assert self._row("2026-10-29,0,XX,1,2,1.5,1,1,24000").values["option_type"] is NOT_AN_OPTION

    def test_it_is_rejected_for_what_it_is_not_for_its_strike(self):
        outcome = OptionChainRowValidator("NIFTY").validate(
            self._row("2026-10-29,0,XX,1,2,1.5,1,1,24000")
        )
        assert isinstance(outcome, RejectedRow)
        assert outcome.reason is RejectionReason.NOT_AN_OPTION
