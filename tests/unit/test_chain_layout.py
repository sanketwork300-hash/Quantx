"""Reading a two-sided option chain export.

Every retail chain download -- NSE's included -- puts calls to the left of the
strike and puts to the right, repeats each header name once per side, and names
the expiry only in the filename. The dangerous outcome here is not a parse
error. It is a chain that loads cleanly with every call carrying the put's bid,
ask and last price, because a name-keyed reader keeps one column per name and
throws the other away. These tests pin the separation that stops that.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from domains.instruments.enums import OptionType
from domains.market_data.ingestion.column_mapping import OPTION_CHAIN_FIELDS
from domains.market_data.ingestion.layout import (
    ChainLayout,
    LayoutError,
    TwoSidedLayout,
    detect,
    filename_hints,
    split,
)
from domains.market_data.ingestion.parser import TabularParser

EXPIRY = date(2026, 9, 15)

#: The shape NSE emits: a merged banner, a header whose names repeat on each
#: side of STRIKE, thousands separators inside quotes, "-" for an absent value,
#: a leading blank column and a trailing comma.
NSE = (
    "CALLS,,PUTS\r\n"
    ",OI,CHNG IN OI,VOLUME,IV,LTP,CHNG,BID QTY,BID,ASK,ASK QTY,STRIKE,"
    "BID QTY,BID,ASK,ASK QTY,CHNG,LTP,IV,VOLUME,CHNG IN OI,OI,\r\n"
    ',-,-,-,-,-,-,65,"1,877.00","1,925.30",65,"22,100.00",'
    '"2,925",3.20,3.25,"5,330",-4.60,3.20,20.93,"24,072",-,"17,189",\r\n'
    ',434,13,86,10.69,"1,011.00",68.30,65,"1,012.00","1,016.25",65,"23,000.00",'
    '"1,950",6.55,6.60,"1,430",-3.15,6.55,13.31,"27,994","7,431","19,368",\r\n'
)

LONG_FORM = (
    "EXPIRY_DT,STRIKE_PRICE,CE_PE,BID,ASK,LTP\n2026-09-15,23000,CE,1012.00,1016.25,1011.00\n"
)


def nse_bytes(body: str = NSE) -> bytes:
    return body.encode()


def confirmed(data: bytes = None) -> TwoSidedLayout:
    """The layout a user would confirm from the preview."""
    detection = detect(data or nse_bytes())
    assert detection.two_sided is not None
    return replace(detection.two_sided, expiry=EXPIRY)


class TestDetection:
    def test_a_banner_row_does_not_hide_the_header(self):
        """Row 1 is a merged 'CALLS | PUTS' cell; the header is row 2."""
        detection = detect(nse_bytes())
        assert detection.layout is ChainLayout.TWO_SIDED
        assert detection.two_sided.header_row == 1
        assert detection.headers[11] == "STRIKE"

    def test_the_strike_column_separates_the_two_sides(self):
        layout = detect(nse_bytes()).two_sided
        assert layout.strike_column == 11
        assert all(index < 11 for index in layout.call_columns.values())
        assert all(index > 11 for index in layout.put_columns.values())

    def test_each_side_resolves_its_own_prices_and_sizes(self):
        layout = detect(nse_bytes()).two_sided
        assert layout.call_columns == {
            "open_interest": 1,
            "volume": 3,
            "last_price": 5,
            "bid_size": 7,
            "bid_price": 8,
            "ask_price": 9,
            "ask_size": 10,
        }
        # The put block is mirrored, so the same fields arrive in a different
        # column order. Resolving by index is what makes that survivable.
        assert layout.put_columns == {
            "bid_size": 12,
            "bid_price": 13,
            "ask_price": 14,
            "ask_size": 15,
            "last_price": 17,
            "volume": 19,
            "open_interest": 21,
        }

    def test_the_reading_is_explained_in_the_users_terms(self):
        detection = detect(nse_bytes())
        blob = " ".join(detection.evidence)
        assert "header" in blob
        assert "STRIKE" in blob
        assert "banner" in blob
        # The user is told which names could not have been told apart.
        assert "BID" in blob and "ASK" in blob

    def test_columns_that_match_no_field_are_reported_not_silently_dropped(self):
        detection = detect(nse_bytes())
        # There is no market_iv field yet, so both IV columns are ignored --
        # and the user is told so rather than left to assume they were read.
        assert detection.unmapped_columns.count("IV") == 2
        assert "CHNG" in detection.unmapped_columns

    def test_a_long_form_file_is_left_alone(self):
        detection = detect(LONG_FORM.encode())
        assert detection.layout is ChainLayout.LONG
        assert detection.two_sided is None

    def test_an_option_type_column_settles_it_even_on_a_wide_file(self):
        """An explicit side column means the file says what it is."""
        wide = b"OPTION_TYPE,BID,ASK,STRIKE,BID,ASK\nCE,1.0,1.1,23000,2.0,2.1\n"
        assert detect(wide).layout is ChainLayout.LONG

    def test_two_strike_columns_are_not_guessed_between(self):
        ambiguous = b"BID,STRIKE,ASK,STRIKE,BID\n1,2,3,4,5\n"
        assert detect(ambiguous).layout is ChainLayout.LONG

    def test_a_side_with_no_price_is_not_a_side(self):
        """Open interest either side of a strike is not a two-sided chain."""
        not_a_chain = b"OI,STRIKE,OI\n1,23000,2\n"
        assert detect(not_a_chain).layout is ChainLayout.LONG

    def test_an_empty_file_is_not_a_layout(self):
        assert detect(b"").layout is ChainLayout.LONG


class TestSplitting:
    def test_one_strike_becomes_one_call_and_one_put(self):
        records, _ = split(nse_bytes(), confirmed())
        assert len(records) == 4
        assert [record["option_type"] for _, record in records] == [
            "CALL",
            "PUT",
            "CALL",
            "PUT",
        ]

    def test_the_call_keeps_the_call_price_and_the_put_keeps_the_put_price(self):
        """The failure this module exists to prevent, stated as an assertion."""
        records, _ = split(nse_bytes(), confirmed())
        call, put = records[0][1], records[1][1]
        assert call["bid_price"] == "1,877.00"
        assert call["ask_price"] == "1,925.30"
        assert put["bid_price"] == "3.20"
        assert put["ask_price"] == "3.25"
        assert call["bid_price"] != put["bid_price"]

    def test_both_sides_report_the_row_the_user_can_find(self):
        """Both quotes came from one line, so both name that line."""
        records, _ = split(nse_bytes(), confirmed())
        assert [number for number, _ in records] == [1, 1, 2, 2]

    def test_the_strike_and_expiry_are_shared_by_both_sides(self):
        records, _ = split(nse_bytes(), confirmed())
        for _, record in records[:2]:
            assert record["strike"] == "22,100.00"
            assert record["expiry"] == "2026-09-15"

    def test_an_unquoted_side_is_still_emitted_for_the_validator_to_judge(self):
        """Dropping it here would be a second, untested exclusion rule."""
        records, _ = split(nse_bytes(), confirmed())
        call = records[0][1]
        assert call["last_price"] == "-"
        assert len(records) == 4

    def test_a_blank_trailing_line_is_not_a_row(self):
        records, _ = split(nse_bytes(NSE + ",,,,,,,,,,,,,,,,,,,,,,\n"), confirmed())
        assert len(records) == 4

    def test_a_short_row_yields_absent_cells_rather_than_empty_ones(self):
        truncated = NSE.rsplit("\r\n", 2)[0] + "\r\n,-,-,-,-,-,-,65,1.00\r\n"
        records, _ = split(truncated.encode(), confirmed())
        _, put = records[-1]
        assert put["bid_price"] is None

    def test_a_header_row_past_the_end_of_the_file_is_refused(self):
        layout = replace(confirmed(), header_row=50)
        with pytest.raises(LayoutError):
            split(nse_bytes(), layout)


class TestTheLayoutIsARealDescription:
    def test_a_side_without_a_price_column_cannot_be_declared(self):
        with pytest.raises(LayoutError):
            TwoSidedLayout(
                header_row=0,
                strike_column=1,
                call_columns={"open_interest": 0},
                put_columns={"bid_price": 2},
                expiry=EXPIRY,
            )

    def test_it_survives_the_round_trip_through_a_job_payload(self):
        layout = confirmed()
        assert TwoSidedLayout.from_dict(layout.to_dict()) == layout

    def test_its_mapping_covers_every_required_field(self):
        mapping = confirmed().identity_mapping()
        assert mapping.missing_required(OPTION_CHAIN_FIELDS) == ()


class TestCoercionIsShared:
    """Split records run through the same parser as a long-form file."""

    def test_the_two_sided_path_coerces_exactly_like_the_flat_path(self):
        layout = confirmed()
        records, headers = split(nse_bytes(), layout)
        parser = TabularParser(OPTION_CHAIN_FIELDS, max_rows=1000)
        result = parser.parse_records(records, headers, layout.identity_mapping())

        assert result.errors == []
        call = result.rows[0].values
        assert call["strike"] == Decimal("22100.00")
        assert call["option_type"] is OptionType.CALL
        assert call["expiry"] == EXPIRY
        assert call["bid_price"] == Decimal("1877.00")
        # "-" is an absent value, not a zero.
        assert call["last_price"] is None
        # Thousands separators inside a quoted cell survive as one number.
        assert result.rows[1].values["open_interest"] == Decimal(17189)

    def test_the_put_side_coerces_to_the_put_numbers(self):
        layout = confirmed()
        records, headers = split(nse_bytes(), layout)
        parser = TabularParser(OPTION_CHAIN_FIELDS, max_rows=1000)
        put = parser.parse_records(records, headers, layout.identity_mapping()).rows[1].values
        assert put["option_type"] is OptionType.PUT
        assert put["bid_price"] == Decimal("3.20")
        assert put["volume"] == Decimal(24072)


class TestFilenameHints:
    """A suggestion for the preview to show. Never applied on its own."""

    @pytest.mark.parametrize(
        ("filename", "expected"),
        [
            ("option-chain-ED-NIFTY-15-Sep-2026.csv", date(2026, 9, 15)),
            ("option-chain-ED-NIFTY-15-Sep-2026 (1).csv", date(2026, 9, 15)),
            ("chain_BANKNIFTY_2026-09-15.csv", date(2026, 9, 15)),
            ("nifty.csv", None),
            (None, None),
        ],
    )
    def test_an_expiry_is_read_from_the_filename_when_one_is_there(self, filename, expected):
        assert filename_hints(filename)[0] == expected

    def test_the_symbol_next_to_the_date_is_offered_too(self):
        assert filename_hints("option-chain-ED-NIFTY-15-Sep-2026.csv")[1] == "NIFTY"

    def test_a_detected_layout_carries_no_expiry_of_its_own(self):
        """The suggestion is offered beside the layout, never folded into it."""
        detection = detect(nse_bytes(), filename="option-chain-ED-NIFTY-15-Sep-2026.csv")
        assert detection.suggested_expiry == EXPIRY
        assert detection.suggestion_source == "filename"
        assert detection.two_sided.expiry is None
