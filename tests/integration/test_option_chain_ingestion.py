"""The Phase 0 flagship path: upload -> preview -> ingest -> retrieve.

These tests carry the Phase 0 acceptance criteria from docs/backlog.md.
"""

from __future__ import annotations

import pytest

AS_OF = "2026-09-24T09:20:00Z"
MAPPING = {
    "strike": "STRIKE_PRICE",
    "option_type": "CE_PE",
    "expiry": "EXPIRY_DT",
    "bid_price": "BID",
    "ask_price": "ASK",
    "last_price": "LTP",
    "bid_size": "BIDQTY",
    "ask_size": "ASKQTY",
    "volume": "VOL",
    "open_interest": "OI",
    "underlying_price": "UNDERLYING_VALUE",
}


async def upload(client, header, data: bytes, filename="chain.csv"):
    response = await client.post(
        "/uploads",
        headers={"Authorization": header},
        files={"file": (filename, data, "text/csv")},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def ingest(client, header, upload_id, **overrides):
    payload = {
        "underlying": {"symbol": "NIFTY", "exchange": "SYNTH", "currency": "INR"},
        "as_of_timestamp": AS_OF,
        "column_mapping": MAPPING,
        "contract": {
            "multiplier": "75",
            "tick_size": "0.05",
            "lot_size": "75",
            "expiry_time_utc": "10:00:00",
        },
        # The synthetic market was generated at this carry; supplying it enables
        # the carry-dependent bound checks (see docs/methodology.md).
        "risk_free_rate": 0.065,
        "dividend_yield": 0.0,
    }
    payload.update(overrides)
    response = await client.post(
        f"/uploads/{upload_id}/ingest", headers={"Authorization": header}, json=payload
    )
    assert response.status_code == 202, response.text
    return response.json()


async def wait_for_job(client, header, job_id):
    """Eager mode completes inline, so one read is enough."""
    response = await client.get(f"/jobs/{job_id}", headers={"Authorization": header})
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
async def ingested_clean(client, auth_header, clean_chain_csv):
    record = await upload(client, auth_header, clean_chain_csv)
    accepted = await ingest(client, auth_header, record["id"])
    job = await wait_for_job(client, auth_header, accepted["job_id"])
    assert job["status"] == "COMPLETED", job
    result = await client.get(
        f"/jobs/{job['job_id']}/result", headers={"Authorization": auth_header}
    )
    return result.json()["result"]


class TestUploadAndPreview:
    async def test_upload_records_size_and_digest(self, client, auth_header, clean_chain_csv):
        record = await upload(client, auth_header, clean_chain_csv)
        assert record["byte_size"] == len(clean_chain_csv)
        assert len(record["sha256"]) == 64
        assert record["status"] == "RECEIVED"

    async def test_filename_is_sanitised_for_display(self, client, auth_header, clean_chain_csv):
        record = await upload(client, auth_header, clean_chain_csv, "../../evil.csv")
        assert record["original_filename"] == "evil.csv"

    async def test_preview_infers_the_mapping_and_persists_nothing(
        self, client, auth_header, clean_chain_csv
    ):
        record = await upload(client, auth_header, clean_chain_csv)
        response = await client.post(
            f"/uploads/{record['id']}/preview",
            headers={"Authorization": auth_header},
            json={"limit": 5},
        )
        assert response.status_code == 200
        preview = response.json()
        assert preview["inferred_mapping"]["strike"] == "STRIKE_PRICE"
        assert preview["inferred_mapping"]["option_type"] == "CE_PE"
        assert preview["inferred_mapping"]["expiry"] == "EXPIRY_DT"
        assert preview["missing_required"] == []
        assert len(preview["sample"]) == 5
        assert preview["verdict"]["readable"] is True

        # Nothing was committed by previewing.
        chains = await client.get("/market/chains", headers={"Authorization": auth_header})
        assert chains.json() == []

    async def test_preview_reports_an_incomplete_mapping(
        self, client, auth_header, clean_chain_csv
    ):
        record = await upload(client, auth_header, clean_chain_csv)
        response = await client.post(
            f"/uploads/{record['id']}/preview",
            headers={"Authorization": auth_header},
            json={"column_mapping": {"strike": "STRIKE_PRICE"}},
        )
        assert set(response.json()["missing_required"]) == {"option_type", "expiry"}

    async def test_ingestion_refuses_an_incomplete_mapping(
        self, client, auth_header, clean_chain_csv
    ):
        record = await upload(client, auth_header, clean_chain_csv)
        response = await client.post(
            f"/uploads/{record['id']}/ingest",
            headers={"Authorization": auth_header},
            json={
                "underlying": {"symbol": "NIFTY", "exchange": "SYNTH"},
                "as_of_timestamp": AS_OF,
                "column_mapping": {"strike": "STRIKE_PRICE"},
            },
        )
        assert response.status_code == 422
        assert response.json()["code"] == "COLUMN_MAPPING_INCOMPLETE"


class TestThePreviewReportsTheReading:
    """The preview is a report of how the file was read, not a form to fill in.

    The user who downloaded a chain from an exchange has nothing to say about
    its columns. What they need is to see what was worked out, in the file's own
    words, and a way to correct the one column that is wrong.
    """

    @pytest.fixture
    async def previewed(self, client, auth_header, clean_chain_csv):
        record = await upload(client, auth_header, clean_chain_csv)
        response = await client.post(
            f"/uploads/{record['id']}/preview",
            headers={"Authorization": auth_header},
            json={"limit": 6},
        )
        assert response.status_code == 200, response.text
        return record, response.json()

    async def test_every_required_field_names_the_column_it_was_read_from(self, previewed):
        _, seen = previewed
        columns = {
            item["field"]: item["columns"][0]["header"]
            for item in seen["reading"]
            if item["columns"]
        }
        assert columns["strike"] == "STRIKE_PRICE"
        assert columns["option_type"] == "CE_PE"
        assert columns["expiry"] == "EXPIRY_DT"

    async def test_a_reading_nobody_asked_for_says_it_was_detected(self, previewed):
        _, seen = previewed
        detected = {
            item["field"] for item in seen["reading"] if item["source"] == "DETECTED_COLUMN"
        }
        assert {"strike", "option_type", "expiry"} <= detected

    async def test_a_field_the_file_does_not_carry_says_so_rather_than_vanishing(self, previewed):
        """Nothing is dropped without a reason, including a field with no column."""
        _, seen = previewed
        absent = {item["field"] for item in seen["reading"] if item["source"] == "NOT_IN_FILE"}
        assert "sequence_number" in absent
        missing = next(item for item in seen["reading"] if item["field"] == "sequence_number")
        assert missing["detail"]

    async def test_a_column_the_user_corrected_is_attributed_to_them(
        self, client, auth_header, clean_chain_csv
    ):
        """A correction has to be visible as a correction.

        Otherwise the report cannot be trusted as evidence: a user checking it
        later cannot tell what the platform worked out from what they told it.
        """
        record = await upload(client, auth_header, clean_chain_csv)
        corrected = dict(MAPPING)
        corrected["last_price"] = "BID"
        response = await client.post(
            f"/uploads/{record['id']}/preview",
            headers={"Authorization": auth_header},
            json={"column_mapping": corrected, "limit": 3},
        )
        reading = {item["field"]: item for item in response.json()["reading"]}
        assert reading["last_price"]["source"] == "SUPPLIED_COLUMN"
        assert reading["last_price"]["columns"][0]["header"] == "BID"

    async def test_the_sample_carries_the_source_row_number(self, previewed):
        _, seen = previewed
        assert [row["row_number"] for row in seen["sample"]] == [1, 2, 3, 4, 5, 6]

    async def test_the_sample_shows_only_fields_that_were_mapped(self, previewed):
        _, seen = previewed
        mapped = {item["field"] for item in seen["reading"] if item["source"] != "NOT_IN_FILE"}
        for row in seen["sample"]:
            assert set(row["values"]) <= mapped

    async def test_rows_that_could_not_be_read_appear_in_the_sample(
        self, client, auth_header, bad_chain_csv
    ):
        """The old sample showed the successes only, so it looked perfect always."""
        record = await upload(client, auth_header, bad_chain_csv, "bad.csv")
        response = await client.post(
            f"/uploads/{record['id']}/preview",
            headers={"Authorization": auth_header},
            json={"limit": 50},
        )
        sample = response.json()["sample"]
        unread = [row for row in sample if not row["read"]]
        assert unread, "a file with unreadable rows must not preview as clean"
        assert all(row["reason"] and row["problem"] for row in unread)
        assert [row["row_number"] for row in sample] == sorted(
            row["row_number"] for row in sample
        ), "the sample is the file's own order, not the successes followed by the failures"

    async def test_an_empty_far_strike_is_not_counted_as_a_misreading(
        self, client, auth_header, bad_chain_csv
    ):
        """A row with no prices is an empty row, not evidence of a wrong column.

        Every exchange chain carries strikes with nothing quoted on one side.
        Counting those against the reading would refuse legitimate files.
        """
        record = await upload(client, auth_header, bad_chain_csv, "bad.csv")
        response = await client.post(
            f"/uploads/{record['id']}/preview",
            headers={"Authorization": auth_header},
            json={"limit": 50},
        )
        verdict = response.json()["verdict"]
        assert verdict["readable"] is True
        assert verdict["rows_empty"] >= 1
        assert (
            verdict["rows_examined"]
            == verdict["rows_read"] + verdict["rows_unreadable"] + verdict["rows_empty"]
        )


class TestAFileThatCouldNotBeReadIsRefused:
    """A reading that did not work is an error, not a nearly empty snapshot.

    An empty snapshot is indistinguishable downstream from a market with
    nothing in it: every later analysis takes it at face value and reports a
    quiet chain rather than a failed import.
    """

    async def test_a_column_that_does_not_hold_what_it_was_taken_for_is_refused(
        self, client, auth_header, clean_chain_csv
    ):
        record = await upload(client, auth_header, clean_chain_csv)
        wrong = dict(MAPPING)
        wrong["expiry"] = "LTP"  # prices, not dates
        response = await client.post(
            f"/uploads/{record['id']}/ingest",
            headers={"Authorization": auth_header},
            json={
                "underlying": {"symbol": "NIFTY", "exchange": "SYNTH"},
                "as_of_timestamp": AS_OF,
                "column_mapping": wrong,
            },
        )
        assert response.status_code == 422, response.text
        body = response.json()
        assert body["code"] == "NO_ROW_COULD_BE_READ"
        assert body["rows_read"] == 0

    async def test_nothing_was_written_by_the_refusal(self, client, auth_header, clean_chain_csv):
        record = await upload(client, auth_header, clean_chain_csv)
        wrong = dict(MAPPING)
        wrong["expiry"] = "LTP"
        await client.post(
            f"/uploads/{record['id']}/ingest",
            headers={"Authorization": auth_header},
            json={
                "underlying": {"symbol": "NIFTY", "exchange": "SYNTH"},
                "as_of_timestamp": AS_OF,
                "column_mapping": wrong,
            },
        )
        chains = await client.get("/market/chains", headers={"Authorization": auth_header})
        assert chains.json() == []

    async def test_a_file_with_a_header_and_no_rows_is_refused(self, client, auth_header):
        record = await upload(client, auth_header, b"EXPIRY_DT,STRIKE_PRICE,CE_PE,BID,ASK,LTP\n")
        response = await client.post(
            f"/uploads/{record['id']}/ingest",
            headers={"Authorization": auth_header},
            json={
                "underlying": {"symbol": "NIFTY", "exchange": "SYNTH"},
                "as_of_timestamp": AS_OF,
            },
        )
        assert response.status_code == 422, response.text
        assert response.json()["code"] == "FILE_HAS_NO_DATA_ROWS"

    async def test_the_refusal_names_the_rows_and_the_reasons(
        self, client, auth_header, clean_chain_csv
    ):
        record = await upload(client, auth_header, clean_chain_csv)
        wrong = dict(MAPPING)
        wrong["expiry"] = "LTP"
        response = await client.post(
            f"/uploads/{record['id']}/ingest",
            headers={"Authorization": auth_header},
            json={
                "underlying": {"symbol": "NIFTY", "exchange": "SYNTH"},
                "as_of_timestamp": AS_OF,
                "column_mapping": wrong,
            },
        )
        body = response.json()
        assert body["reasons"]["UNPARSEABLE_ROW"] > 0
        assert "UNPARSEABLE_ROW" in body["detail"]

    async def test_a_file_that_only_goes_wrong_past_the_sample_is_refused_by_the_worker(
        self, client, auth_header
    ):
        """The route sees a sample; the worker sees the file. Both apply the rule.

        A file whose first rows read cleanly is accepted, and the whole-file
        reading is then assessed where it can be: in the job. It fails with the
        diagnosis rather than completing with a snapshot of the first few rows.
        """
        header = b"EXPIRY_DT,STRIKE_PRICE,CE_PE,BID,ASK,LTP\n"
        good = b"".join(
            f"2026-10-29,{22000 + index * 50},CE,10.00,11.00,10.50\n".encode()
            for index in range(60)
        )
        broken = b"".join(
            f"not-a-date,{22000 + index * 50},CE,10.00,11.00,10.50\n".encode()
            for index in range(400)
        )
        record = await upload(client, auth_header, header + good + broken, "late.csv")
        accepted = await ingest(
            client,
            auth_header,
            record["id"],
            column_mapping={
                "strike": "STRIKE_PRICE",
                "option_type": "CE_PE",
                "expiry": "EXPIRY_DT",
                "bid_price": "BID",
                "ask_price": "ASK",
                "last_price": "LTP",
            },
        )
        job = await wait_for_job(client, auth_header, accepted["job_id"])
        assert job["status"] == "FAILED", job
        assert job["error"]["details"]["code"] == "MOST_ROWS_COULD_NOT_BE_READ"
        assert job["error"]["details"]["verdict"]["rows_unreadable"] == 400

    async def test_the_failed_job_names_the_columns_it_read_from(self, client, auth_header):
        """The diagnosis has to be repairable: which column was taken for what."""
        header = b"EXPIRY_DT,STRIKE_PRICE,CE_PE,BID,ASK,LTP\n"
        good = b"".join(
            f"2026-10-29,{22000 + index * 50},CE,10.00,11.00,10.50\n".encode()
            for index in range(60)
        )
        broken = b"".join(
            f"not-a-date,{22000 + index * 50},CE,10.00,11.00,10.50\n".encode()
            for index in range(400)
        )
        record = await upload(client, auth_header, header + good + broken, "late.csv")
        accepted = await ingest(
            client,
            auth_header,
            record["id"],
            column_mapping={
                "strike": "STRIKE_PRICE",
                "option_type": "CE_PE",
                "expiry": "EXPIRY_DT",
                "bid_price": "BID",
                "ask_price": "ASK",
                "last_price": "LTP",
            },
        )
        job = await wait_for_job(client, auth_header, accepted["job_id"])
        reading = {item["field"]: item for item in job["error"]["details"]["reading"]}
        assert reading["expiry"]["columns"][0]["header"] == "EXPIRY_DT"


class TestIngestingWithNothingSaidAboutTheFile:
    """A long-form file uploaded with an empty request body.

    The commit path applies the same header inference the preview shows, so a
    user who uploads a file and says nothing gets their chain rather than a
    rejected row per quote. It is reported, because a column taken for the wrong
    field produces a plausible chain and no error.
    """

    @pytest.fixture
    async def ingested_blind(self, client, auth_header, clean_chain_csv):
        record = await upload(client, auth_header, clean_chain_csv)
        accepted = await ingest(
            client,
            auth_header,
            record["id"],
            column_mapping={},
        )
        job = await wait_for_job(client, auth_header, accepted["job_id"])
        assert job["status"] == "COMPLETED", job
        result = await client.get(
            f"/jobs/{job['job_id']}/result", headers={"Authorization": auth_header}
        )
        return result.json()["result"]

    async def test_the_chain_loads(self, ingested_blind):
        counts = ingested_blind["results"]["counts"]
        assert counts["kept"] > 0
        assert counts["input"] == counts["kept"] + counts["excluded"] + counts["rejected"]

    async def test_the_inference_is_reported_rather_than_done_quietly(self, ingested_blind):
        reported = [
            w for w in ingested_blind["warnings"] if w["code"] == "INGESTION_MAPPING_INFERRED"
        ]
        assert reported, [w["code"] for w in ingested_blind["warnings"]]
        assert reported[0]["context"]["column_mapping"]["option_type"] == "CE_PE"

    async def test_the_provenance_records_the_mapping_actually_used(self, ingested_blind):
        mapping = ingested_blind["provenance"]["parameters"]["column_mapping"]
        assert mapping["strike"] == "STRIKE_PRICE"
        assert mapping["expiry"] == "EXPIRY_DT"

    async def test_a_file_whose_headers_mean_nothing_is_still_refused(self, client, auth_header):
        record = await upload(client, auth_header, b"a,b,c\n1,2,3\n")
        response = await client.post(
            f"/uploads/{record['id']}/ingest",
            headers={"Authorization": auth_header},
            json={
                "underlying": {"symbol": "NIFTY", "exchange": "SYNTH"},
                "as_of_timestamp": AS_OF,
            },
        )
        assert response.status_code == 422
        assert response.json()["code"] == "COLUMN_MAPPING_INCOMPLETE"


class TestAnAsOfPastTheExpiryIsCalledOut:
    """An expired contract stores fine and then supports nothing.

    The implied-volatility solver refuses it, so there is no smile, no surface
    slice and nothing for the scanner. The user meets that as an empty chart
    three screens later unless ingestion says so at the point the timestamp was
    supplied — and the timestamp is the usual culprit, because it is typed by
    the caller rather than read from the file.

    A chain that had *partly* expired is stored, with the expired quotes set
    aside and named. One that had *wholly* expired is refused: it would store as
    a snapshot with no usable quote in it, which then becomes the underlying's
    latest chain.
    """

    async def _refused_at(self, client, auth_header, data, as_of, **overrides):
        record = await upload(client, auth_header, data)
        accepted = await ingest(
            client, auth_header, record["id"], as_of_timestamp=as_of, **overrides
        )
        job = await wait_for_job(client, auth_header, accepted["job_id"])
        assert job["status"] == "FAILED", job
        return job["error"]

    async def _ingest_at(self, client, auth_header, clean_chain_csv, as_of, **overrides):
        record = await upload(client, auth_header, clean_chain_csv)
        accepted = await ingest(
            client, auth_header, record["id"], as_of_timestamp=as_of, **overrides
        )
        job = await wait_for_job(client, auth_header, accepted["job_id"])
        assert job["status"] == "COMPLETED", job
        result = await client.get(
            f"/jobs/{job['job_id']}/result", headers={"Authorization": auth_header}
        )
        return result.json()["result"]

    async def test_a_chain_still_live_says_nothing(self, client, auth_header, clean_chain_csv):
        body = await self._ingest_at(client, auth_header, clean_chain_csv, AS_OF)
        codes = {warning["code"] for warning in body["warnings"]}
        assert "INGESTION_CONTRACTS_ALREADY_EXPIRED" not in codes

    async def test_a_partly_expired_chain_names_the_expiry(
        self, client, auth_header, clean_chain_csv
    ):
        body = await self._ingest_at(client, auth_header, clean_chain_csv, "2026-11-01T09:20:00Z")
        warning = next(
            w for w in body["warnings"] if w["code"] == "INGESTION_CONTRACTS_ALREADY_EXPIRED"
        )
        assert warning["severity"] == "WARNING"
        assert warning["context"]["expiries"] == ["2026-10-29"]

    async def test_a_wholly_expired_chain_is_refused(self, client, auth_header, clean_chain_csv):
        """Nothing downstream can be computed at all, so nothing is stored.

        The defect this covers: the job completed with every quote excluded, and
        the empty snapshot became the underlying's latest chain.
        """
        error = await self._refused_at(client, auth_header, clean_chain_csv, "2027-01-01T09:20:00Z")
        details = error["details"]
        assert details["code"] == "ALL_CONTRACTS_EXPIRED"
        assert details["expiries"] == ["2026-10-29", "2026-12-24"]
        assert details["quotes"] == 60
        assert details["expiry_time_utc"] == "10:00:00"

    async def test_it_points_at_the_as_of_timestamp_rather_than_the_file(
        self, client, auth_header, clean_chain_csv
    ):
        error = await self._refused_at(client, auth_header, clean_chain_csv, "2027-01-01T09:20:00Z")
        assert "as-of timestamp" in error["message"]
        assert "not read from the file" in error["message"]

    async def test_nothing_was_written_by_the_refusal(self, client, auth_header, clean_chain_csv):
        await self._refused_at(client, auth_header, clean_chain_csv, "2027-01-01T09:20:00Z")
        chains = await client.get("/market/chains", headers={"Authorization": auth_header})
        assert chains.json() == []

    async def test_with_no_settlement_time_only_a_past_date_is_expired(
        self, client, auth_header, clean_chain_csv
    ):
        """The expiry instant is unknown, so the last expiry day is not yet past."""
        no_time = {"multiplier": "75", "tick_size": "0.05", "lot_size": "75"}
        on_the_day = await self._ingest_at(
            client, auth_header, clean_chain_csv, "2026-12-24T23:00:00Z", contract=no_time
        )
        assert on_the_day["results"]["counts"]["input"] == 60
        error = await self._refused_at(
            client, auth_header, clean_chain_csv, "2026-12-25T00:30:00Z", contract=no_time
        )
        assert error["details"]["code"] == "ALL_CONTRACTS_EXPIRED"
        assert error["details"]["expiry_time_utc"] is None

    async def test_the_expired_part_of_a_chain_is_stored_with_its_reason(
        self, client, auth_header, clean_chain_csv
    ):
        """A partly expired chain is a legitimate record. The expired quotes are
        set aside, with the reason on every one, rather than thrown away."""
        body = await self._ingest_at(client, auth_header, clean_chain_csv, "2026-11-01T09:20:00Z")
        counts = body["results"]["counts"]
        assert counts["kept"] > 0
        assert counts["input"] == counts["kept"] + counts["excluded"] + counts["rejected"]
        assert body["results"]["exclusion_counts"]["OPTION_EXPIRED"] > 0

    async def test_an_expired_quote_is_flagged_even_with_no_underlying_price(
        self, client, auth_header
    ):
        """The defect this covers: expiry was decided inside the bounds check.

        The bounds check returns early without an underlying price, so a chain
        with no spot column -- every two-sided exchange export -- stored expired
        contracts at full quality scores and then supported nothing downstream.
        """
        chain = (
            b"EXPIRY_DT,STRIKE_PRICE,CE_PE,BID,ASK,LTP\n"
            b"2026-10-29,24000,CE,10.00,11.00,10.50\n"
            b"2026-12-24,24000,CE,10.00,11.00,10.50\n"
        )
        record = await upload(client, auth_header, chain, "no-spot.csv")
        accepted = await ingest(
            client,
            auth_header,
            record["id"],
            as_of_timestamp="2026-11-01T09:20:00Z",
            column_mapping={
                "strike": "STRIKE_PRICE",
                "option_type": "CE_PE",
                "expiry": "EXPIRY_DT",
                "bid_price": "BID",
                "ask_price": "ASK",
                "last_price": "LTP",
            },
        )
        job = await wait_for_job(client, auth_header, accepted["job_id"])
        result = await client.get(
            f"/jobs/{job['job_id']}/result", headers={"Authorization": auth_header}
        )
        results = result.json()["result"]["results"]
        assert "MISSING_UNDERLYING_PRICE" in results["flag_counts"], "no spot in this file"
        assert results["exclusion_counts"]["OPTION_EXPIRED"] == 1

    async def test_the_expiry_day_itself_is_judged_by_the_settlement_time(
        self, client, auth_header, clean_chain_csv
    ):
        """10:01 on expiry day is past a 10:00 settlement; 09:20 is not."""
        before = await self._ingest_at(client, auth_header, clean_chain_csv, "2026-10-29T09:20:00Z")
        after = await self._ingest_at(client, auth_header, clean_chain_csv, "2026-10-29T10:01:00Z")
        code = "INGESTION_CONTRACTS_ALREADY_EXPIRED"
        assert code not in {w["code"] for w in before["warnings"]}
        assert code in {w["code"] for w in after["warnings"]}


class TestAnOptionalColumnThatCannotBeReadCostsTheCellNotTheFile:
    """A row with a strike, an expiry, a side and a price is a quote.

    The defect this covers: a ``time`` column holding ``15:30:00`` is matched to
    the venue timestamp by its header, fails to parse as one, and rejected
    every row -- so a perfectly readable chain was refused outright.
    """

    CHAIN = (
        b"expiry,strike,option_type,bid,ask,time,volume\n"
        b"2026-10-29,24000,CE,150.00,151.00,15:30:00,1.2K\n"
        b"2026-10-29,24000,PE,20.00,21.00,15:30:00,300\n"
    )

    @pytest.fixture
    async def body(self, client, auth_header):
        record = await upload(client, auth_header, self.CHAIN, "with-time.csv")
        accepted = await ingest(client, auth_header, record["id"], column_mapping={})
        job = await wait_for_job(client, auth_header, accepted["job_id"])
        assert job["status"] == "COMPLETED", job
        result = await client.get(
            f"/jobs/{job['job_id']}/result", headers={"Authorization": auth_header}
        )
        return result.json()["result"]

    async def test_the_rows_are_kept(self, body):
        counts = body["results"]["counts"]
        assert counts["input"] == 2
        assert counts["rejected"] == 0

    async def test_the_cells_set_aside_are_named(self, body):
        warning = next(
            w for w in body["warnings"] if w["code"] == "INGESTION_OPTIONAL_VALUES_UNREADABLE"
        )
        columns = warning["context"]["columns"]
        assert columns["time"] == {
            "field": "exchange_timestamp",
            "count": 2,
            "example": "15:30:00",
            "first_row": 1,
        }
        assert columns["volume"]["count"] == 1
        assert "'1.2K'" in warning["message"]


class TestAnAutoReadChainFeedsTheRestOfThePlatform:
    """A file nobody described still reaches volatility and surface analysis.

    Ingestion is not the product; it is the door. A chain read automatically has
    to be the same object as one whose columns were named by hand, or the
    automatic path would be a second-class import that quietly supports less.
    """

    @pytest.fixture
    async def snapshot_id(self, client, auth_header, clean_chain_csv):
        record = await upload(client, auth_header, clean_chain_csv)
        accepted = await ingest(client, auth_header, record["id"], column_mapping={})
        job = await wait_for_job(client, auth_header, accepted["job_id"])
        assert job["status"] == "COMPLETED", job
        result = await client.get(
            f"/jobs/{job['job_id']}/result", headers={"Authorization": auth_header}
        )
        return result.json()["result"]["results"]["snapshot_id"]

    async def test_implied_volatility_is_solved_from_it(self, client, auth_header, snapshot_id):
        accepted = await client.post(
            f"/derivatives/chains/{snapshot_id}/analyze",
            headers={"Authorization": auth_header},
            json={
                "risk_free_rate": 0.065,
                "dividend_yield": 0.0,
                "settlement_time_utc": "10:00:00",
            },
        )
        assert accepted.status_code == 202, accepted.text
        result = await client.get(
            f"/jobs/{accepted.json()['job_id']}/result",
            headers={"Authorization": auth_header},
        )
        counts = result.json()["result"]["results"]["counts"]
        assert counts["solved"] > 0, counts

    async def test_a_surface_can_be_fitted_to_it(self, client, auth_header, snapshot_id):
        accepted = await client.post(
            f"/derivatives/chains/{snapshot_id}/analyze",
            headers={"Authorization": auth_header},
            json={
                "risk_free_rate": 0.065,
                "dividend_yield": 0.0,
                "settlement_time_utc": "10:00:00",
            },
        )
        analysis_id = (
            await client.get(
                f"/jobs/{accepted.json()['job_id']}/result",
                headers={"Authorization": auth_header},
            )
        ).json()["result"]["analysis_id"]

        fitted = await client.post(
            f"/derivatives/analyses/{analysis_id}/calibrate",
            headers={"Authorization": auth_header},
            json={"seed": 20260924, "use_weights": True},
        )
        assert fitted.status_code == 202, fitted.text
        body = (
            await client.get(
                f"/jobs/{fitted.json()['job_id']}/result",
                headers={"Authorization": auth_header},
            )
        ).json()["result"]
        assert body["results"]["surface_row_id"]


class TestCleanChainIngestion:
    async def test_job_completes_and_returns_a_summary(self, ingested_clean):
        assert ingested_clean["status"] in {"OK", "PARTIAL"}
        results = ingested_clean["results"]
        assert results["counts"]["input"] == 60
        assert results["counts"]["kept"] == 60
        assert results["counts"]["excluded"] == 0
        assert results["counts"]["rejected"] == 0

    async def test_row_conservation(self, ingested_clean):
        counts = ingested_clean["results"]["counts"]
        assert counts["input"] == counts["kept"] + counts["excluded"] + counts["rejected"]

    async def test_aggregate_quality_is_reported(self, ingested_clean):
        quality = ingested_clean["results"]["aggregate_quality"]
        assert 0.0 <= quality["overall_score"] <= 1.0
        assert quality["consistency_score"] == pytest.approx(1.0)

    async def test_provenance_is_complete(self, ingested_clean):
        provenance = ingested_clean["provenance"]
        assert provenance["market_state_timestamp"] == "2026-09-24T09:20:00+00:00"
        assert provenance["model_versions"]["ingestion"].startswith("option-chain-ingestion@")
        assert provenance["model_versions"]["quality"].startswith("market-data-quality@")
        assert provenance["code_commit"] == "test-commit"
        assert provenance["parameters"]["column_mapping"]["strike"] == "STRIKE_PRICE"
        # The quality parameters that produced every score are recorded.
        assert "weight_consistency" in provenance["parameters"]["quality_config"]

    async def test_chain_is_retrievable_with_quality_per_quote(
        self, client, auth_header, ingested_clean
    ):
        snapshot_id = ingested_clean["results"]["snapshot_id"]
        response = await client.get(
            f"/market/chains/{snapshot_id}", headers={"Authorization": auth_header}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "OK"

        results = body["results"]
        assert len(results["quotes"]) == 60
        assert len(results["expiries"]) == 2
        quote = results["quotes"][0]
        assert quote["mid_price"] is not None
        assert quote["quality"]["overall_score"] > 0.0
        assert quote["source_row_number"] is not None

    async def test_latest_chain_by_underlying(self, client, auth_header, ingested_clean):
        underlying_id = ingested_clean["results"]["underlying_id"]
        response = await client.get(
            f"/market/options/{underlying_id}", headers={"Authorization": auth_header}
        )
        assert response.status_code == 200
        assert response.json()["results"]["counts"]["kept"] == 60

    async def test_expiry_filter(self, client, auth_header, ingested_clean):
        snapshot_id = ingested_clean["results"]["snapshot_id"]
        expiry = ingested_clean["results"]["expiries"][0]
        response = await client.get(
            f"/market/chains/{snapshot_id}?expiry={expiry}",
            headers={"Authorization": auth_header},
        )
        quotes = response.json()["results"]["quotes"]
        assert quotes
        assert {quote["expiry"] for quote in quotes} == {expiry}

    async def test_instruments_were_created_for_every_contract(
        self, client, auth_header, ingested_clean
    ):
        underlying_id = ingested_clean["results"]["underlying_id"]
        response = await client.get(
            f"/instruments?underlying_id={underlying_id}&asset_class=OPTION&limit=1000",
            headers={"Authorization": auth_header},
        )
        items = response.json()["items"]
        assert len(items) == 60
        assert all(item["canonical_key"].startswith("SYNTH:OPTION:NIFTY:") for item in items)
        assert all(item["multiplier"] == "75" for item in items)

    async def test_reingestion_is_idempotent_for_instruments(
        self, client, auth_header, clean_chain_csv, ingested_clean
    ):
        """Deterministic ids mean a second import updates rather than duplicates."""
        underlying_id = ingested_clean["results"]["underlying_id"]
        record = await upload(client, auth_header, clean_chain_csv)
        accepted = await ingest(client, auth_header, record["id"])
        job = await wait_for_job(client, auth_header, accepted["job_id"])
        assert job["status"] == "COMPLETED"

        response = await client.get(
            f"/instruments?underlying_id={underlying_id}&asset_class=OPTION&limit=1000",
            headers={"Authorization": auth_header},
        )
        assert len(response.json()["items"]) == 60

        chains = await client.get("/market/chains", headers={"Authorization": auth_header})
        assert len(chains.json()) == 2, "each ingestion is its own observation snapshot"


class TestBadChainIngestion:
    @pytest.fixture
    async def ingested_bad(self, client, auth_header, bad_chain_csv):
        record = await upload(client, auth_header, bad_chain_csv, "bad.csv")
        accepted = await ingest(client, auth_header, record["id"])
        job = await wait_for_job(client, auth_header, accepted["job_id"])
        assert job["status"] == "COMPLETED", job
        result = await client.get(
            f"/jobs/{job['job_id']}/result", headers={"Authorization": auth_header}
        )
        return result.json()["result"]

    async def test_row_conservation_holds_with_bad_data(self, ingested_bad):
        counts = ingested_bad["results"]["counts"]
        assert counts["input"] == counts["kept"] + counts["excluded"] + counts["rejected"]
        assert counts["excluded"] > 0
        assert counts["rejected"] > 0
        assert counts["kept"] > 0, "bad rows must not poison the usable ones"

    async def test_every_excluded_quote_has_a_reason(self, client, auth_header, ingested_bad):
        """Phase 0/1 acceptance criterion, checked against data that triggers it."""
        snapshot_id = ingested_bad["results"]["snapshot_id"]
        response = await client.get(
            f"/market/chains/{snapshot_id}?include_excluded=true",
            headers={"Authorization": auth_header},
        )
        quotes = response.json()["results"]["quotes"]
        excluded = [quote for quote in quotes if quote["excluded"]]
        assert excluded
        for quote in excluded:
            assert quote["exclusion_reason"], quote
            assert quote["quality"]["flags"], quote
            assert any(flag["severity"] == "ERROR" for flag in quote["quality"]["flags"]), quote

    async def test_kept_quotes_have_no_reason(self, client, auth_header, ingested_bad):
        snapshot_id = ingested_bad["results"]["snapshot_id"]
        response = await client.get(
            f"/market/chains/{snapshot_id}", headers={"Authorization": auth_header}
        )
        kept = [q for q in response.json()["results"]["quotes"] if not q["excluded"]]
        assert kept
        assert all(quote["exclusion_reason"] is None for quote in kept)

    @pytest.mark.parametrize(
        "reason",
        [
            "CROSSED_MARKET",
            "ZERO_ASK",
            "MISSING_BOTH_SIDES",
            "NEGATIVE_PRICE",
            "PRICE_BELOW_INTRINSIC",
            "DUPLICATE_OBSERVATION",
        ],
    )
    async def test_each_seeded_corruption_is_caught(self, ingested_bad, reason):
        assert reason in ingested_bad["results"]["exclusion_counts"], ingested_bad["results"][
            "exclusion_counts"
        ]

    @pytest.mark.parametrize(
        "reason",
        [
            "NON_POSITIVE_STRIKE",
            "UNPARSEABLE_ROW",
            "MISSING_EXPIRY",
            "NO_PRICE_FIELDS",
            "MISSING_OPTION_TYPE",
        ],
    )
    async def test_each_unusable_row_is_rejected_with_a_reason(self, ingested_bad, reason):
        assert reason in ingested_bad["results"]["rejection_counts"], ingested_bad["results"][
            "rejection_counts"
        ]

    async def test_rejected_rows_name_their_source_row_number(self, ingested_bad):
        rejected = ingested_bad["results"]["rejected_rows"]
        assert rejected
        for row in rejected:
            assert row["row_number"] >= 1
            assert row["reason"]
            assert row["message"]

    async def test_wide_spread_and_illiquidity_are_flagged_but_kept(
        self, client, auth_header, ingested_bad
    ):
        snapshot_id = ingested_bad["results"]["snapshot_id"]
        response = await client.get(
            f"/market/chains/{snapshot_id}", headers={"Authorization": auth_header}
        )
        flags = {
            flag["code"]
            for quote in response.json()["results"]["quotes"]
            if not quote["excluded"]
            for flag in quote["quality"]["flags"]
        }
        assert "WIDE_SPREAD" in flags
        assert "ILLIQUID_CONTRACT" in flags

    async def test_warnings_explain_what_happened(self, ingested_bad):
        codes = {warning["code"] for warning in ingested_bad["warnings"]}
        assert "INGESTION_ROWS_REJECTED" in codes
        assert "INGESTION_CARRY_ASSUMPTION_USED" in codes


class TestExclusionPolicy:
    async def test_a_stricter_threshold_excludes_more(self, client, auth_header, bad_chain_csv):
        """The threshold is a request parameter and is recorded in provenance."""
        results = {}
        for threshold in ("ERROR", "WARNING"):
            record = await upload(client, auth_header, bad_chain_csv, "bad.csv")
            accepted = await ingest(
                client,
                auth_header,
                record["id"],
                options={"exclusion_severity_threshold": threshold},
            )
            job = await wait_for_job(client, auth_header, accepted["job_id"])
            payload = await client.get(
                f"/jobs/{job['job_id']}/result", headers={"Authorization": auth_header}
            )
            body = payload.json()["result"]
            results[threshold] = body

        strict = results["WARNING"]["results"]["counts"]
        lenient = results["ERROR"]["results"]["counts"]
        assert strict["excluded"] > lenient["excluded"]
        assert (
            results["WARNING"]["provenance"]["parameters"]["exclusion_severity_threshold"]
            == "WARNING"
        )
