"""Phase 7 end to end: import a trade log, benchmark it, read the report.

Carries the Phase 7 acceptance criteria from docs/backlog.md over the wire.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.conftest import register_and_login
from tests.integration.test_derivatives import ingest_clean_chain

TRADES_CSV = (Path(__file__).resolve().parents[1] / "data" / "trades.csv").read_bytes()

MAPPING = {
    "timestamp": "TRADE_TIME",
    "symbol": "SYMBOL",
    "exchange": "EXCH",
    "asset_class": "TYPE",
    "expiry": "EXPIRY_DT",
    "strike": "STRIKE_PRICE",
    "option_type": "CE_PE",
    "side": "ACTION",
    "quantity": "QTY",
    "price": "FILL_PRICE",
    "parent_order": "PARENTORDER",
    "order_id": "ORDER_ID",
    "order_type": "ORDER_TYPE",
    "submit_timestamp": "SUBMITTIME",
    "order_quantity": "ORDERQTY",
    "fees": "COMMISSION",
    "broker": "BROKERNAME",
}
DEFAULTS = {"currency": "INR", "exchange": "SYNTH", "multiplier": "75"}


async def upload_trades(client, header, data: bytes = TRADES_CSV) -> str:
    response = await client.post(
        "/uploads",
        headers={"Authorization": header},
        files={"file": ("trades.csv", data, "text/csv")},
        data={"kind": "TRADES"},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def run_job(client, header, job_id) -> dict:
    job = await client.get(f"/jobs/{job_id}", headers={"Authorization": header})
    assert job.json()["status"] == "COMPLETED", job.json()
    result = await client.get(f"/jobs/{job_id}/result", headers={"Authorization": header})
    return result.json()["result"]


@pytest.fixture
async def market(client, auth_header, clean_chain_csv):
    """An ingested chain, so the fills resolve and a benchmark window exists."""
    return await ingest_clean_chain(client, auth_header, clean_chain_csv)


@pytest.fixture
async def imported(client, auth_header, market):
    upload_id = await upload_trades(client, auth_header)
    accepted = await client.post(
        "/execution/trades/import",
        headers={"Authorization": auth_header},
        json={"upload_id": upload_id, "column_mapping": MAPPING, "defaults": DEFAULTS},
    )
    assert accepted.status_code == 202, accepted.text
    body = await run_job(client, auth_header, accepted.json()["job_id"])
    return {"upload_id": upload_id, "body": body}


async def analyse(client, header, **overrides) -> dict:
    accepted = await client.post(
        "/execution/analyze", headers={"Authorization": header}, json=overrides
    )
    assert accepted.status_code == 202, accepted.text
    return await run_job(client, header, accepted.json()["job_id"])


class TestImport:
    async def test_the_preview_infers_the_mapping_and_splits_three_ways(
        self, client, auth_header, market
    ):
        upload_id = await upload_trades(client, auth_header)
        response = await client.post(
            "/execution/trades/preview",
            headers={"Authorization": auth_header},
            json={"upload_id": upload_id, "defaults": DEFAULTS},
        )
        assert response.status_code == 200, response.text
        body = response.json()

        assert body["inferred_mapping"]["timestamp"] == "TRADE_TIME"
        assert body["inferred_mapping"]["quantity"] == "QTY"
        assert body["rows_in"] == len(body["resolved"]) + len(body["ambiguous"]) + len(
            body["invalid"]
        )
        assert len(body["resolved"]) == 8
        assert len(body["invalid"]) == 4
        assert body["committable"] is True

    async def test_every_rejected_row_names_its_row_number_and_reason(
        self, client, auth_header, market
    ):
        upload_id = await upload_trades(client, auth_header)
        response = await client.post(
            "/execution/trades/preview",
            headers={"Authorization": auth_header},
            json={"upload_id": upload_id, "defaults": DEFAULTS},
        )
        reasons = {row["reason"] for row in response.json()["invalid"]}
        assert reasons == {
            "NON_POSITIVE_QUANTITY",
            "INCOMPLETE_OPTION",
            "SUBMIT_AFTER_FILL",
            "UNRECOGNISED_SIDE",
        }
        for row in response.json()["invalid"]:
            assert row["row_number"] > 0
            assert len(row["message"]) > 10

    async def test_a_fill_that_precedes_its_own_submission_is_refused(
        self, client, auth_header, market
    ):
        """One of the two timestamps is wrong, and guessing which corrupts the
        arrival benchmark for that whole order."""
        upload_id = await upload_trades(client, auth_header)
        response = await client.post(
            "/execution/trades/preview",
            headers={"Authorization": auth_header},
            json={"upload_id": upload_id, "defaults": DEFAULTS},
        )
        bad = next(
            row for row in response.json()["invalid"] if row["reason"] == "SUBMIT_AFTER_FILL"
        )
        assert "after the fill" in bad["message"]

    async def test_a_commit_inserts_only_the_resolved_rows(self, client, auth_header, imported):
        assert imported["body"]["results"]["committed"] == 8
        listed = await client.get("/execution/executions", headers={"Authorization": auth_header})
        assert len(listed.json()) == 8

    async def test_the_import_records_the_file_it_came_from(self, imported):
        results = imported["body"]["results"]
        assert results["upload_id"] == imported["upload_id"]
        assert len(results["dataset_digest"]) == 64

    async def test_a_commit_without_a_mapping_is_refused(self, client, auth_header, market):
        upload_id = await upload_trades(client, auth_header)
        response = await client.post(
            "/execution/trades/import",
            headers={"Authorization": auth_header},
            json={"upload_id": upload_id, "column_mapping": {}, "defaults": DEFAULTS},
        )
        assert response.status_code == 400

    async def test_a_mapping_missing_a_required_field_is_refused(self, client, auth_header, market):
        upload_id = await upload_trades(client, auth_header)
        response = await client.post(
            "/execution/trades/import",
            headers={"Authorization": auth_header},
            json={
                "upload_id": upload_id,
                "column_mapping": {"symbol": "SYMBOL"},
                "defaults": DEFAULTS,
            },
        )
        assert response.status_code == 422
        assert set(response.json()["missing_required"]) == {
            "timestamp",
            "side",
            "quantity",
            "price",
        }

    async def test_an_option_chain_upload_is_not_importable_as_trades(
        self, client, auth_header, clean_chain_csv
    ):
        upload = await client.post(
            "/uploads",
            headers={"Authorization": auth_header},
            files={"file": ("chain.csv", clean_chain_csv, "text/csv")},
        )
        response = await client.post(
            "/execution/trades/preview",
            headers={"Authorization": auth_header},
            json={"upload_id": upload.json()["id"], "defaults": DEFAULTS},
        )
        assert response.status_code == 422
        assert response.json()["code"] == "WRONG_UPLOAD_KIND"

    async def test_a_trade_log_is_not_ingestable_as_a_chain(self, client, auth_header):
        upload_id = await upload_trades(client, auth_header)
        response = await client.post(
            f"/uploads/{upload_id}/ingest",
            headers={"Authorization": auth_header},
            json={
                "kind": "TRADES",
                "underlying": {"symbol": "NIFTY", "exchange": "SYNTH"},
                "as_of_timestamp": "2026-09-24T09:20:00Z",
                "column_mapping": MAPPING,
            },
        )
        assert response.status_code == 400
        assert response.json()["code"] == "WRONG_INGESTION_ROUTE"


class TestGrouping:
    async def test_named_parents_are_kept_and_unnamed_ones_are_inferred(
        self, client, auth_header, imported
    ):
        results = (await analyse(client, auth_header))["results"]
        by_key = {report["parent_order"]["key"]: report for report in results["reports"]}

        assert "P1" in by_key and "P2" in by_key
        assert by_key["P1"]["parent_order"]["grouping_is_inferred"] is False
        assert by_key["P2"]["parent_order"]["grouping_method"] == "EXPLICIT"

        inferred = [
            report
            for report in results["reports"]
            if report["parent_order"]["grouping_is_inferred"]
        ]
        assert len(inferred) == 2

    async def test_the_gap_changes_the_grouping_and_is_recorded(
        self, client, auth_header, imported
    ):
        tight = await analyse(client, auth_header, parent_gap_seconds=300.0)
        loose = await analyse(client, auth_header, parent_gap_seconds=7200.0)

        assert tight["results"]["parent_orders"] > loose["results"]["parent_orders"]
        assert tight["provenance"]["parameters"]["analysis"]["parent_gap_seconds"] == 300.0
        assert loose["provenance"]["parameters"]["analysis"]["parent_gap_seconds"] == 7200.0

    async def test_an_inferred_grouping_is_reported_as_a_warning(
        self, client, auth_header, imported
    ):
        result = await analyse(client, auth_header)
        codes = {warning["code"] for warning in result["warnings"]}
        assert "TCA_INFERRED_PARENT_GROUPING" in codes
        message = next(
            w["message"] for w in result["warnings"] if w["code"] == "TCA_INFERRED_PARENT_GROUPING"
        )
        assert "different benchmarks" in message


class TestBenchmarksAndShortfall:
    async def test_the_average_price_is_the_hand_checkable_one(self, client, auth_header, imported):
        """P1 bought 75 at 442, 445 and 448, so the average is exactly 445."""
        results = (await analyse(client, auth_header))["results"]
        p1 = next(r for r in results["reports"] if r["parent_order"]["key"] == "P1")
        assert p1["parent_order"]["average_price"] == "445.00"
        assert p1["parent_order"]["filled_quantity"] == "225"

    async def test_every_benchmark_reports_window_source_and_method(
        self, client, auth_header, imported
    ):
        """Phase 7 acceptance, over the wire."""
        results = (await analyse(client, auth_header))["results"]
        for report in results["reports"]:
            assert len(report["benchmarks"]) == 6
            for benchmark in report["benchmarks"]:
                assert benchmark["method"]
                assert benchmark["kind"]
                if benchmark["available"]:
                    assert benchmark["window"]["start"] is not None
                    assert benchmark["window"]["end"] is not None
                    assert benchmark["source"]
                else:
                    assert benchmark["unavailable_reason"]

    async def test_the_shortfall_is_reported_in_three_units(self, client, auth_header, imported):
        results = (await analyse(client, auth_header))["results"]
        found = False
        for report in results["reports"]:
            for shortfall in report["shortfalls"]:
                found = True
                assert "currency_amount" in shortfall
                assert "basis_points" in shortfall
                assert "percent" in shortfall
                assert "Positive is a cost" in shortfall["convention"]
        assert found

    async def test_low_coverage_produces_an_unavailable_benchmark_not_a_number(
        self, client, auth_header, imported
    ):
        """Phase 7 acceptance: one ingested chain is one observation."""
        results = (await analyse(client, auth_header))["results"]
        unavailable = [
            benchmark
            for report in results["reports"]
            for benchmark in report["benchmarks"]
            if not benchmark["available"]
        ]
        assert unavailable
        for benchmark in unavailable:
            assert benchmark["price"] is None
            assert benchmark["unavailable_reason"]

    async def test_the_interval_vwap_refuses_for_want_of_interval_volume(
        self, client, auth_header, imported
    ):
        results = (await analyse(client, auth_header))["results"]
        vwaps = [
            benchmark
            for report in results["reports"]
            for benchmark in report["benchmarks"]
            if benchmark["kind"] == "INTERVAL_VWAP"
        ]
        assert vwaps
        assert all(item["available"] is False for item in vwaps)

    async def test_an_order_with_no_submit_timestamp_uses_a_flagged_proxy(
        self, client, auth_header, imported
    ):
        results = (await analyse(client, auth_header))["results"]
        proxied = [
            benchmark
            for report in results["reports"]
            for benchmark in report["benchmarks"]
            if benchmark["kind"] == "ARRIVAL" and benchmark["method"] == "FIRST_FILL_PROXY"
        ]
        assert proxied
        assert all("ARRIVAL_PROXY_USED" in item["flags"] for item in proxied)

        codes = {w["code"] for w in (await analyse(client, auth_header))["warnings"]}
        assert "ARRIVAL_PROXY_USED" in codes

    async def test_the_market_window_carries_its_coverage(self, client, auth_header, imported):
        results = (await analyse(client, auth_header))["results"]
        for report in results["reports"]:
            coverage = report["market_window"]["coverage"]
            assert "observations" in coverage
            assert "span_ratio" in coverage
            assert "not an interval price" in coverage["policy"]


class TestDecomposition:
    async def test_it_is_labelled_model_based(self, client, auth_header, imported):
        result = await analyse(client, auth_header)
        codes = {warning["code"] for warning in result["warnings"]}
        assert "TCA_DECOMPOSITION_IS_MODELLED" in codes

        decompositions = [
            report["decomposition"]
            for report in result["results"]["reports"]
            if report["decomposition"]
        ]
        assert decompositions
        for decomposition in decompositions:
            assert "not a measurement" in decomposition["caveat"]

    async def test_impact_is_not_modelled_and_says_so(self, client, auth_header, imported):
        results = (await analyse(client, auth_header))["results"]
        decomposition = next(
            report["decomposition"] for report in results["reports"] if report["decomposition"]
        )
        impact = next(c for c in decomposition["components"] if c["name"] == "impact")
        assert impact["amount"] is None
        assert impact["status"] == "NOT_MODELLED"

    async def test_the_components_reconcile_to_the_total(self, client, auth_header, imported):
        from decimal import Decimal

        results = (await analyse(client, auth_header))["results"]
        for report in results["reports"]:
            decomposition = report["decomposition"]
            if not decomposition:
                continue
            amounts = {
                item["name"]: Decimal(item["amount"])
                for item in decomposition["components"]
                if item["amount"] is not None
            }
            attributed = amounts.get("spread", Decimal(0)) + amounts["fees"]
            assert attributed + amounts["timing_residual"] == pytest.approx(
                Decimal(decomposition["total"])
            )


class TestPersistenceAndProvenance:
    async def test_reports_are_listed_and_read_back_whole(self, client, auth_header, imported):
        await analyse(client, auth_header)
        listed = await client.get("/execution/reports", headers={"Authorization": auth_header})
        assert listed.status_code == 200
        assert len(listed.json()) >= 4

        report_id = listed.json()[0]["id"]
        detail = await client.get(
            f"/execution/reports/{report_id}", headers={"Authorization": auth_header}
        )
        assert detail.status_code == 200, detail.text
        body = detail.json()["results"]
        assert body["benchmarks"]
        assert body["market_window"]
        assert detail.json()["provenance"]["model_versions"]["tca"]

    async def test_the_summary_row_carries_the_grouping_and_the_coverage(
        self, client, auth_header, imported
    ):
        await analyse(client, auth_header)
        listed = await client.get("/execution/reports", headers={"Authorization": auth_header})
        for row in listed.json():
            assert row["grouping_method"] in {"EXPLICIT", "INFERRED_BY_TIME"}
            assert isinstance(row["grouping_is_inferred"], bool)
            assert isinstance(row["coverage_is_sufficient"], bool)
            assert row["observations"] >= 0

    async def test_an_analysis_of_nothing_fails_with_a_reason(self, client):
        _user, header = await register_and_login(client)
        result = await analyse(client, header)
        assert result["status"] == "FAILED"
        assert result["results"] is None
        assert {w["code"] for w in result["warnings"]} == {"TCA_NO_EXECUTIONS_IN_RANGE"}

    async def test_another_user_sees_neither_the_fills_nor_the_reports(
        self, client, auth_header, imported
    ):
        await analyse(client, auth_header)
        listed = await client.get("/execution/reports", headers={"Authorization": auth_header})
        report_id = listed.json()[0]["id"]

        _other, other_header = await register_and_login(client)
        assert (
            await client.get("/execution/executions", headers={"Authorization": other_header})
        ).json() == []
        assert (
            await client.get("/execution/reports", headers={"Authorization": other_header})
        ).json() == []
        assert (
            await client.get(
                f"/execution/reports/{report_id}", headers={"Authorization": other_header}
            )
        ).status_code == 404


class TestLanguage:
    async def test_no_execution_response_recommends_or_promises(
        self, client, auth_header, imported
    ):
        payloads = [
            str(await analyse(client, auth_header)),
            (await client.get("/execution/reports", headers={"Authorization": auth_header})).text,
        ]
        forbidden = (
            "optimal execution",
            "best execution guaranteed",
            "recommendation",
            "you should have",
            "fair value",
            "guaranteed",
            "buy signal",
            "sell signal",
        )
        for payload in payloads:
            lowered = payload.lower()
            for phrase in forbidden:
                assert phrase not in lowered, phrase
