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
        assert len(preview["sample_rows"]) == 5

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
