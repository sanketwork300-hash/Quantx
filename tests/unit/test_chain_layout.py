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
    read_filename,
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
            ("NIFTY_15SEP2026.csv", date(2026, 9, 15)),
            ("nifty-15-Sept-2026.csv", date(2026, 9, 15)),
            ("nifty 15 September 2026.csv", date(2026, 9, 15)),
            ("BANKNIFTY 28-10-2026.csv", date(2026, 10, 28)),
            ("NIFTY26OCT24000CE.csv", None),
            ("nifty.csv", None),
            (None, None),
        ],
    )
    def test_an_expiry_is_read_from_the_filename_when_one_is_there(self, filename, expected):
        assert filename_hints(filename)[0] == expected

    def test_two_dates_do_not_say_which_is_the_expiry(self):
        """The first used to be taken, and it was the snapshot date."""
        hint = read_filename("NIFTY-2026-09-30-snapshot-exp-2026-10-29.csv")
        assert hint.expiry is None
        assert "more than one date (2026-09-30, 2026-10-29)" in hint.note

    def test_a_numeric_date_that_reads_two_ways_suggests_nothing(self):
        hint = read_filename("BANKNIFTY 05-10-2026.csv")
        assert hint.expiry is None
        assert "day-month or month-day" in hint.note

    def test_why_nothing_was_suggested_is_part_of_the_evidence(self):
        detection = detect(nse_bytes(), filename="NIFTY-2026-09-30-exp-2026-10-29.csv")
        assert detection.suggested_expiry is None
        assert any("more than one date" in line for line in detection.evidence)

    def test_the_symbol_next_to_the_date_is_offered_too(self):
        assert filename_hints("option-chain-ED-NIFTY-15-Sep-2026.csv")[1] == "NIFTY"

    def test_a_detected_layout_carries_no_expiry_of_its_own(self):
        """The suggestion is offered beside the layout, never folded into it."""
        detection = detect(nse_bytes(), filename="option-chain-ED-NIFTY-15-Sep-2026.csv")
        assert detection.suggested_expiry == EXPIRY
        assert detection.suggestion_source == "filename"
        assert detection.two_sided.expiry is None


class TestReadingAFileTheCallerDidNotDescribe:
    """The commit path when the user uploads a download and says nothing.

    Working the reading out is a fallback, not a policy: it runs only when the
    caller named no layout and supplied no mapping at all. A caller who states a
    mapping knows something about their file that a header scan does not, and is
    never overridden by a guess -- an incomplete instruction is answered with the
    field it is missing, not with a different reading of the file.
    """

    @staticmethod
    def pipeline():
        from domains.market_data.ingestion.pipeline import OptionChainIngestionPipeline

        # Reading a file touches neither of these; resolution is pure.
        return OptionChainIngestionPipeline(instrument_service=None, repository=None)

    def resolve(self, data: bytes, mapping: dict, filename: str | None = None, layout=None):
        from domains.market_data.ingestion.column_mapping import ColumnMapping

        return self.pipeline()._resolve_reading(
            data, ColumnMapping(mapping=mapping), layout, filename
        )

    def test_an_undescribed_two_sided_file_is_read_rather_than_rejected(self):
        plan = self.resolve(nse_bytes(), {}, "option-chain-NIFTY-15-Sep-2026.csv")
        assert plan.layout is not None
        assert plan.layout.strike_column == 11
        assert plan.detection.layout is ChainLayout.TWO_SIDED
        assert plan.auto_detected

    def test_the_expiry_the_filename_carries_is_applied_and_attributed(self):
        plan = self.resolve(nse_bytes(), {}, "option-chain-NIFTY-15-Sep-2026.csv")
        assert plan.layout.expiry == EXPIRY
        assert plan.detection.suggestion_source == "filename"

    def test_an_expiry_that_is_nowhere_is_left_unknown(self):
        """Nothing plausible is substituted; the caller has to supply it."""
        plan = self.resolve(nse_bytes(), {}, "option-chain.csv")
        assert plan.layout.expiry is None

    def test_a_caller_who_gave_a_workable_mapping_is_not_second_guessed(self):
        mapping = {"strike": "STRIKE", "option_type": "CE_PE", "expiry": "EXPIRY_DT"}
        plan = self.resolve(nse_bytes(), mapping, "chain-15-Sep-2026.csv")
        assert plan.layout is None
        assert plan.detection is None
        assert plan.mapping.to_dict() == mapping

    def test_a_partial_mapping_is_an_instruction_too(self):
        """Answered with the field it is missing, not with a different reading."""
        plan = self.resolve(nse_bytes(), {"strike": "STRIKE"}, "chain-15-Sep-2026.csv")
        assert plan.layout is None
        assert plan.detection is None
        assert set(plan.mapping.missing_required(OPTION_CHAIN_FIELDS)) == {
            "option_type",
            "expiry",
        }

    def test_a_layout_the_caller_named_is_used_verbatim(self):
        named = confirmed()
        plan = self.resolve(nse_bytes(), {}, "chain-01-Jan-2027.csv", layout=named)
        assert plan.layout is named

    def test_a_long_form_file_is_not_turned_into_a_two_sided_one(self):
        plan = self.resolve(LONG_FORM.encode(), {}, "chain-15-Sep-2026.csv")
        assert plan.layout is None
        assert plan.detection.layout is ChainLayout.LONG

    def test_a_long_form_file_gets_its_columns_matched_by_name(self):
        """The same inference the preview shows, applied when nothing was said."""
        plan = self.resolve(LONG_FORM.encode(), {}, "chain-15-Sep-2026.csv")
        assert plan.mapping_inferred
        assert plan.mapping.to_dict()["strike"] == "STRIKE_PRICE"
        assert plan.mapping.to_dict()["option_type"] == "CE_PE"

    def test_a_file_whose_columns_mean_nothing_is_left_unread(self):
        """Inference that cannot find a required field reports it, not a guess."""
        plan = self.resolve(b"a,b,c\n1,2,3\n", {}, "chain-15-Sep-2026.csv")
        assert not plan.mapping_inferred
        assert plan.mapping.missing_required(OPTION_CHAIN_FIELDS)


class TestHeadersThatNameTheirOwnSide:
    """``Call LTP`` and ``Put LTP`` say which side they are; position need not."""

    FILE = (
        b"Call OI,Call LTP,Call Bid,Call Ask,Strike,Put Bid,Put Ask,Put LTP,Put OI\n"
        b"100,150,149,151,24000,20,21,20.5,120\n"
    )

    def test_the_file_is_read_as_two_sided(self):
        detection = detect(self.FILE)
        assert detection.layout is ChainLayout.TWO_SIDED
        assert detection.two_sided.strike_column == 4
        assert detection.two_sided.call_columns == {
            "open_interest": 0,
            "last_price": 1,
            "bid_price": 2,
            "ask_price": 3,
        }
        assert detection.two_sided.put_columns == {
            "bid_price": 5,
            "ask_price": 6,
            "last_price": 7,
            "open_interest": 8,
        }

    def test_the_name_is_believed_over_the_position(self):
        """Puts written left of the strike are still the puts."""
        data = b"PE LTP,PE OI,Strike Price,CE LTP,CE OI\n20.5,120,24000,150,100\n"
        layout = detect(data).two_sided
        assert layout.put_columns == {"last_price": 0, "open_interest": 1}
        assert layout.call_columns == {"last_price": 3, "open_interest": 4}

    @pytest.mark.parametrize(
        "header",
        ["C_LTP,C_OI,STRIKE,P_LTP,P_OI", "LTP Call,OI Call,Strike,LTP Put,OI Put"],
    )
    def test_other_ways_of_naming_a_side(self, header):
        layout = detect(f"{header}\n150,100,24000,20.5,120\n".encode()).two_sided
        assert layout.call_columns == {"last_price": 0, "open_interest": 1}
        assert layout.put_columns == {"last_price": 3, "open_interest": 4}

    def test_the_evidence_says_the_sides_were_named(self):
        blob = " ".join(detect(self.FILE).evidence)
        assert "name their own side" in blob
        assert "'Call OI'" in blob

    def test_each_side_keeps_its_own_prices(self):
        layout = replace(detect(self.FILE).two_sided, expiry=EXPIRY)
        records, headers = split(self.FILE, layout)
        parser = TabularParser(OPTION_CHAIN_FIELDS, max_rows=10)
        call, put = parser.parse_records(records, headers, layout.identity_mapping()).rows
        assert call.values["option_type"] is OptionType.CALL
        assert call.values["last_price"] == Decimal("150")
        assert put.values["last_price"] == Decimal("20.5")

    def test_a_word_that_merely_starts_with_a_side_letter_is_not_a_side(self):
        """``Price`` is not a put, and ``Close`` is not a call."""
        assert detect(b"Close,Price,Strike\n1,2,24000\n").layout is ChainLayout.LONG


class TestALongFormFileWithLinesAboveItsHeader:
    FILE = (
        b"NIFTY option chain as on 30-Sep-2026\n"
        b"\n"
        b"strike,option_type,expiry,bid,ask\n"
        b"24000,CE,2026-10-29,150,151\n"
    )

    def test_the_header_is_found(self):
        detection = detect(self.FILE)
        assert detection.layout is ChainLayout.LONG
        assert detection.header_row == 2
        assert detection.headers == ("strike", "option_type", "expiry", "bid", "ask")
        assert any("Row 3 is the header" in line for line in detection.evidence)

    def test_a_semicolon_separated_chain_is_split_into_its_columns(self):
        data = b"strike;option_type;expiry;bid;ask\n24000;CE;2026-10-29;150;151\n"
        assert detect(data).headers == ("strike", "option_type", "expiry", "bid", "ask")
