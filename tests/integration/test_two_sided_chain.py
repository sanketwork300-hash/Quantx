"""Ingesting an exchange chain export end to end: upload -> preview -> ingest.

The file under test is the shape NSE hands a user who clicks "download" on the
option chain page: a merged banner above the header, calls left of ``STRIKE``
and puts right of it, every header name repeated once per side, thousands
separators inside quoted cells, ``-`` for absent values, and an expiry that
appears only in the filename.

The interesting assertion is not that it loads. It is that the calls keep the
call prices. A reader keyed by header name would load this file without a
single error and give every call the put's bid and ask, which is a complete,
plausible, wrong chain -- exactly the failure the preview step exists to make
impossible.
"""

from __future__ import annotations

import pytest

AS_OF = "2026-09-24T09:20:00Z"
FILENAME = "option-chain-ED-NIFTY-15-Sep-2026 (1).csv"

_HEADER = (
    "CALLS,,PUTS\r\n"
    ",OI,CHNG IN OI,VOLUME,IV,LTP,CHNG,BID QTY,BID,ASK,ASK QTY,STRIKE,"
    "BID QTY,BID,ASK,ASK QTY,CHNG,LTP,IV,VOLUME,CHNG IN OI,OI,\r\n"
)

#: Strikes around a spot near 23,980 so both wings are present, with the deep
#: in-the-money call quoted and its put nearly worthless, as a real chain is.
_ROWS = (
    ',-,-,-,-,-,-,65,"1,877.00","1,925.30",65,"22,100.00",'
    '"2,925",3.20,3.25,"5,330",-4.60,3.20,20.93,"24,072",-,"17,189",\r\n'
    ',434,13,86,10.69,"1,011.00",68.30,65,"1,012.00","1,016.25",65,"23,000.00",'
    '"1,950",6.55,6.60,"1,430",-3.15,6.55,13.31,"27,994","7,431","19,368",\r\n'
    ',24,14,41,11.20,722.40,55.50,65,719.65,722.30,65,"23,300.00",'
    '"1,820",12.80,12.90,715,-7.70,12.80,11.36,"19,182","2,237","7,265",\r\n'
    ',92,-3,31,17.04,1.00,-0.10,260,1.00,1.15,"3,185","25,950.00",'
    '65,"1,917.90","2,189.80","1,690",-,-,-,-,-,-,\r\n'
)

CHAIN = (_HEADER + _ROWS).encode()
SOURCE_ROWS = 4


async def upload(client, header, data: bytes = CHAIN, filename: str = FILENAME):
    response = await client.post(
        "/uploads",
        headers={"Authorization": header},
        files={"file": (filename, data, "text/csv")},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def preview(client, header, upload_id, **payload):
    response = await client.post(
        f"/uploads/{upload_id}/preview",
        headers={"Authorization": header},
        json={"limit": 50, **payload},
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
async def previewed(client, auth_header):
    record = await upload(client, auth_header)
    return record, await preview(client, auth_header, record["id"])


@pytest.fixture
async def ingested(client, auth_header, previewed):
    record, seen = previewed
    layout = dict(seen["detected_layout"]["two_sided"])
    layout["expiry"] = seen["detected_layout"]["suggested_expiry"]
    response = await client.post(
        f"/uploads/{record['id']}/ingest",
        headers={"Authorization": auth_header},
        json={
            "underlying": {"symbol": "NIFTY", "exchange": "NSE", "currency": "INR"},
            "as_of_timestamp": AS_OF,
            "layout": layout,
            "underlying_price": "23980",
            "contract": {
                "multiplier": "75",
                "tick_size": "0.05",
                "lot_size": "75",
                "expiry_time_utc": "10:00:00",
            },
        },
    )
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    job = await client.get(f"/jobs/{job_id}", headers={"Authorization": auth_header})
    assert job.json()["status"] == "COMPLETED", job.text
    result = await client.get(f"/jobs/{job_id}/result", headers={"Authorization": auth_header})
    return result.json()["result"]


class TestThePreviewExplainsTheFile:
    async def test_the_layout_is_detected_rather_than_the_banner_read_as_a_header(self, previewed):
        _, seen = previewed
        detected = seen["detected_layout"]
        assert detected["layout"] == "TWO_SIDED"
        assert detected["two_sided"]["header_row"] == 1
        assert detected["two_sided"]["strike_column"] == 11

    async def test_the_user_is_told_why_it_was_read_that_way(self, previewed):
        _, seen = previewed
        blob = " ".join(seen["detected_layout"]["evidence"])
        assert "banner" in blob
        assert "STRIKE" in blob

    async def test_the_expiry_comes_from_the_filename_and_says_so(self, previewed):
        """A chain export names its expiry in no column, only in its filename.

        The preview applies the suggestion so the sample rows are legible, and
        states where it came from. It is not a fact from the data, so the
        ingest request has to carry the expiry explicitly -- see
        ``test_a_layout_without_an_expiry_is_refused``.
        """
        _, seen = previewed
        detected = seen["detected_layout"]
        assert detected["suggested_expiry"] == "2026-09-15"
        assert detected["suggestion_source"] == "filename"
        assert detected["two_sided"]["expiry"] == "2026-09-15"

    async def test_a_layout_without_an_expiry_is_refused(self, client, auth_header, previewed):
        """A guessed expiry moves every contract along the term structure."""
        record, seen = previewed
        layout = dict(seen["detected_layout"]["two_sided"])
        layout.pop("expiry")
        response = await client.post(
            f"/uploads/{record['id']}/ingest",
            headers={"Authorization": auth_header},
            json={
                "underlying": {"symbol": "NIFTY", "exchange": "NSE", "currency": "INR"},
                "as_of_timestamp": AS_OF,
                "layout": layout,
            },
        )
        assert response.status_code == 422, response.text

    async def test_nothing_required_is_missing_once_the_layout_is_applied(self, previewed):
        _, seen = previewed
        assert seen["missing_required"] == []

    async def test_the_sample_shows_both_sides_of_a_strike(self, previewed):
        _, seen = previewed
        sides = [row["option_type"] for row in seen["sample_rows"]]
        assert sides[:2] == ["CALL", "PUT"]

    async def test_columns_that_were_ignored_are_named(self, previewed):
        _, seen = previewed
        # There is no market_iv field yet, so IV is ignored -- and said to be.
        assert "IV" in seen["unmapped_columns"]

    async def test_previewing_commits_nothing(self, client, auth_header, previewed):
        record, _ = previewed
        response = await client.get(
            f"/uploads/{record['id']}", headers={"Authorization": auth_header}
        )
        assert response.json()["status"] == "RECEIVED"


class TestTheChainLoads:
    async def test_every_source_row_became_two_quotes(self, ingested):
        counts = ingested["results"]["counts"]
        assert counts["input"] == SOURCE_ROWS * 2

    async def test_rows_are_conserved(self, ingested):
        counts = ingested["results"]["counts"]
        assert counts["input"] == counts["kept"] + counts["excluded"] + counts["rejected"]

    async def test_the_split_is_reported_rather_than_done_quietly(self, ingested):
        codes = {warning["code"] for warning in ingested["warnings"]}
        assert "INGESTION_TWO_SIDED_LAYOUT" in codes

    async def test_the_provenance_names_the_ingestion_rules(self, ingested):
        versions = ingested["provenance"]["model_versions"]
        assert versions["ingestion"].startswith("option-chain-ingestion@")


class TestTheCallsKeptTheCallPrices:
    """The whole point. A name-keyed reader gets this wrong and says nothing."""

    @pytest.fixture
    async def quotes(self, client, auth_header, ingested):
        snapshot_id = ingested["results"]["snapshot_id"]
        response = await client.get(
            f"/market/chains/{snapshot_id}?include_excluded=true",
            headers={"Authorization": auth_header},
        )
        assert response.status_code == 200, response.text
        return response.json()["results"]["quotes"]

    async def test_the_deep_in_the_money_call_carries_its_own_bid(self, quotes):
        call = next(
            q for q in quotes if q["option_type"] == "CALL" and float(q["strike"]) == 22100.0
        )
        assert float(call["bid_price"]) == 1877.00
        assert float(call["ask_price"]) == 1925.30

    async def test_the_matching_put_carries_the_put_bid_not_the_call_bid(self, quotes):
        put = next(q for q in quotes if q["option_type"] == "PUT" and float(q["strike"]) == 22100.0)
        assert float(put["bid_price"]) == 3.20
        assert float(put["ask_price"]) == 3.25

    async def test_no_strike_has_the_same_quote_on_both_sides(self, quotes):
        by_strike: dict[str, dict] = {}
        for quote in quotes:
            by_strike.setdefault(quote["strike"], {})[quote["option_type"]] = quote
        assert by_strike
        for strike, sides in by_strike.items():
            if len(sides) != 2:
                continue
            assert sides["CALL"]["bid_price"] != sides["PUT"]["bid_price"], strike

    async def test_a_dash_is_an_absent_value_not_a_zero(self, quotes):
        """The 22,100 call was never traded, so it has no last price at all."""
        call = next(
            q for q in quotes if q["option_type"] == "CALL" and float(q["strike"]) == 22100.0
        )
        assert call["last_price"] is None

    async def test_a_quoted_thousands_separator_is_one_number(self, quotes):
        put = next(q for q in quotes if q["option_type"] == "PUT" and float(q["strike"]) == 22100.0)
        assert float(put["open_interest"]) == 17189.0

    async def test_both_sides_point_at_the_source_line_the_user_can_open(self, quotes):
        rows = {(q["option_type"], float(q["strike"])): q["source_row_number"] for q in quotes}
        assert rows[("CALL", 22100.0)] == rows[("PUT", 22100.0)] == 1
        assert rows[("CALL", 23000.0)] == 2
