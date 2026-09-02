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
from domains.market_data.ingestion.parser import TabularParser
from domains.market_data.ingestion.validator import (
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
