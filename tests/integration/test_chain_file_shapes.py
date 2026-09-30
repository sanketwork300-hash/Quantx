"""Chains as files actually arrive, uploaded with nothing said about them.

Each of these was a file the pipeline either refused although it was perfectly
readable -- a title above the header, semicolons between the cells, an exchange
bhavcopy's own column names, headers that say ``Call LTP`` -- or read into
numbers the file does not hold: a date taken day-first in one row and
month-first in the next, a price a hundred times too large, the wrong one of
two columns with the same name.

The first kind is now read, and says how. The second is refused, and says what
would settle it.
"""

from __future__ import annotations

from tests.integration.test_option_chain_ingestion import ingest, upload, wait_for_job


async def ingested(client, auth_header, data: bytes, filename: str = "chain.csv", **overrides):
    record = await upload(client, auth_header, data, filename)
    accepted = await ingest(client, auth_header, record["id"], column_mapping={}, **overrides)
    job = await wait_for_job(client, auth_header, accepted["job_id"])
    assert job["status"] == "COMPLETED", job
    result = await client.get(
        f"/jobs/{job['job_id']}/result", headers={"Authorization": auth_header}
    )
    return result.json()["result"]


async def refused(client, auth_header, data: bytes, **payload):
    record = await upload(client, auth_header, data)
    response = await client.post(
        f"/uploads/{record['id']}/ingest",
        headers={"Authorization": auth_header},
        json={
            "underlying": {"symbol": "NIFTY", "exchange": "NSE"},
            "as_of_timestamp": "2026-09-24T09:20:00Z",
            **payload,
        },
    )
    assert response.status_code == 422, response.text
    return record, response.json()


def warning(body: dict, code: str) -> dict:
    found = [item for item in body["warnings"] if item["code"] == code]
    assert found, [item["code"] for item in body["warnings"]]
    return found[0]


class TestATitleAboveTheHeader:
    FILE = (
        b"NIFTY option chain as on 24-Sep-2026\n"
        b"strike,option_type,expiry,bid,ask\n"
        b"24000,CE,2026-10-29,150,151\n"
        b"24000,PE,2026-10-29,20,21\n"
    )

    async def test_the_chain_is_read_from_the_real_header(self, client, auth_header):
        body = await ingested(client, auth_header, self.FILE)
        assert body["results"]["counts"]["input"] == 2
        assert body["results"]["counts"]["rejected"] == 0
        assert warning(body, "INGESTION_HEADER_ROW_DETECTED")["context"]["header_row"] == 1
        assert body["provenance"]["parameters"]["header_row"] == 1
        assert body["provenance"]["parameters"]["headers"][0] == "strike"


class TestADelimiterThatIsNotAComma:
    async def test_a_semicolon_separated_chain_is_read(self, client, auth_header):
        data = (
            b"strike;option_type;expiry;bid;ask\n"
            b"24000;CE;2026-10-29;150.5;151\n"
            b"24000;PE;2026-10-29;20;21\n"
        )
        body = await ingested(client, auth_header, data)
        assert body["results"]["counts"]["input"] == 2
        assert warning(body, "INGESTION_DELIMITER_DETECTED")["context"]["delimiter"] == ";"
        assert body["provenance"]["parameters"]["delimiter"] == ";"

    async def test_a_tab_separated_chain_is_read(self, client, auth_header):
        data = b"strike\toption_type\texpiry\tbid\task\n24000\tCE\t2026-10-29\t150\t151\n"
        body = await ingested(client, auth_header, data)
        assert body["results"]["counts"]["input"] == 1

    async def test_a_decimal_comma_is_not_read_as_a_number_a_hundred_times_larger(
        self, client, auth_header
    ):
        """``150,5`` with its comma deleted is 1505. It is set aside instead."""
        data = b"strike;option_type;expiry;bid;ask\n24000;CE;2026-10-29;150,5;151\n"
        body = await ingested(client, auth_header, data)
        unreadable = warning(body, "INGESTION_OPTIONAL_VALUES_UNREADABLE")
        assert unreadable["context"]["columns"]["bid"]["example"] == "150,5"
        assert body["results"]["counts"]["rejected"] == 0


class TestAnExchangeBhavcopy:
    """The end-of-day file, under the exchange's own column names."""

    LEGACY = (
        b"INSTRUMENT,SYMBOL,EXPIRY_DT,STRIKE_PR,OPTION_TYP,OPEN,HIGH,LOW,CLOSE,SETTLE_PR,"
        b"CONTRACTS,VAL_INLAKH,OPEN_INT,CHG_IN_OI,TIMESTAMP\n"
        b"FUTIDX,NIFTY,29-Oct-2026,0,XX,24010,24100,23900,24050,24050,900,5,3000,10,24-SEP-2026\n"
        b"OPTIDX,NIFTY,29-Oct-2026,24000,CE,150,160,140,151,151,1000,5,2000,10,24-SEP-2026\n"
        b"OPTIDX,NIFTY,29-Oct-2026,24000,PE,20,22,19,21,21,800,5,1500,10,24-SEP-2026\n"
        b"OPTIDX,BANKNIFTY,29-Oct-2026,52000,CE,300,320,290,310,310,700,5,900,10,24-SEP-2026\n"
    )
    UDIFF = (
        b"TradDt,TckrSymb,XpryDt,StrkPric,OptnTp,ClsPric,OpnIntrst,UndrlygPric\n"
        b"2026-09-24,NIFTY,2026-10-29,24000,CE,151,2000,24050\n"
        b"2026-09-24,NIFTY,2026-10-29,24000,PE,21,1500,24050\n"
    )

    async def test_the_legacy_columns_are_matched(self, client, auth_header):
        body = await ingested(client, auth_header, self.LEGACY)
        mapping = body["provenance"]["parameters"]["column_mapping"]
        assert mapping["strike"] == "STRIKE_PR"
        assert mapping["option_type"] == "OPTION_TYP"
        assert mapping["open_interest"] == "OPEN_INT"

    async def test_a_future_is_set_aside_as_a_future(self, client, auth_header):
        """Not as an unreadable row, and not as a strike the reading got wrong."""
        body = await ingested(client, auth_header, self.LEGACY)
        results = body["results"]
        assert results["rejection_counts"] == {"NOT_AN_OPTION": 1, "SYMBOL_MISMATCH": 1}
        assert results["counts"]["input"] == 4
        assert results["counts"]["kept"] + results["counts"]["excluded"] == 2

    async def test_the_newer_columns_are_matched(self, client, auth_header):
        body = await ingested(client, auth_header, self.UDIFF)
        mapping = body["provenance"]["parameters"]["column_mapping"]
        assert mapping["strike"] == "StrkPric"
        assert mapping["expiry"] == "XpryDt"
        assert mapping["underlying_price"] == "UndrlygPric"
        assert body["results"]["counts"]["rejected"] == 0


class TestHeadersThatNameTheirOwnSide:
    FILE = (
        b"Call OI,Call LTP,Call Bid,Call Ask,Strike,Put Bid,Put Ask,Put LTP,Put OI\n"
        b"100,150,149,151,24000,20,21,20.5,120\n"
    )

    async def test_the_chain_is_read_as_two_sided(self, client, auth_header):
        body = await ingested(client, auth_header, self.FILE, "NIFTY-29-Oct-2026.csv")
        assert body["results"]["counts"]["input"] == 2
        assert body["results"]["expiries"] == ["2026-10-29"]
        layout = body["provenance"]["parameters"]["layout"]
        assert layout["call_columns"]["last_price"] == 1
        assert layout["put_columns"]["last_price"] == 7


class TestADateThatReadsTwoWays:
    """``05/10/2026`` is 5 October or 10 May. It is never read as both."""

    HEADER = b"strike,option_type,expiry,bid,ask\n"
    AMBIGUOUS = HEADER + b"24000,CE,05/10/2026,150,151\n24000,PE,05/10/2026,20,21\n"
    SETTLED = AMBIGUOUS + b"24100,CE,29/10/2026,120,121\n"

    async def test_a_column_that_does_not_say_is_refused(self, client, auth_header):
        _, body = await refused(client, auth_header, self.AMBIGUOUS)
        assert body["code"] == "AMBIGUOUS_DATE_ORDER"
        assert "'05/10/2026'" in body["detail"]

    async def test_the_preview_says_which_column_and_why(self, client, auth_header):
        record = await upload(client, auth_header, self.AMBIGUOUS)
        response = await client.post(
            f"/uploads/{record['id']}/preview",
            headers={"Authorization": auth_header},
            json={},
        )
        seen = response.json()
        assert seen["verdict"]["problem"] == "AMBIGUOUS_DATE_ORDER"
        assert seen["date_readings"] == [
            {
                "field": "expiry",
                "column": "expiry",
                "order": None,
                "stated": False,
                "example": "05/10/2026",
                "problem": "AMBIGUOUS",
            }
        ]

    async def test_it_is_read_once_the_order_is_stated(self, client, auth_header):
        body = await ingested(client, auth_header, self.AMBIGUOUS, date_order="DMY")
        assert body["results"]["expiries"] == ["2026-10-05"]
        stated = warning(body, "INGESTION_DATE_ORDER")
        assert stated["context"]["stated"] is True
        assert body["provenance"]["parameters"]["date_readings"][0]["order"] == "DMY"

    async def test_one_value_that_reads_one_way_settles_the_column(self, client, auth_header):
        body = await ingested(client, auth_header, self.SETTLED)
        assert body["results"]["expiries"] == ["2026-10-05", "2026-10-29"]
        settled = warning(body, "INGESTION_DATE_ORDER")
        assert settled["context"]["order"] == "DMY"
        assert settled["context"]["example"] == "29/10/2026"
        assert settled["context"]["stated"] is False


class TestAHeaderNameUsedTwice:
    async def test_a_field_read_from_it_is_refused(self, client, auth_header):
        data = b"strike,option_type,expiry,bid,ask,bid\n24000,CE,2026-10-29,150,151,999\n"
        _, body = await refused(client, auth_header, data)
        assert body["code"] == "AMBIGUOUS_COLUMN"
        assert "'bid'" in body["detail"]


class TestATimestampWithNoOffset:
    async def test_the_assumption_is_reported(self, client, auth_header):
        data = (
            b"strike,option_type,expiry,bid,ask,timestamp\n"
            b"24000,CE,2026-10-29,150,151,2026-09-24 09:19:00\n"
            b"24000,PE,2026-10-29,20,21,2026-09-24 09:19:00\n"
        )
        body = await ingested(client, auth_header, data)
        assumed = warning(body, "INGESTION_TIMESTAMP_TIMEZONE_ASSUMED")
        assert assumed["context"]["columns"] == {"timestamp": 2}
        assert assumed["severity"] == "WARNING"
