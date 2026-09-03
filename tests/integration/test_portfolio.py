"""Phase 4 end to end: import a position file, value it, read the Greeks.

Carries the Phase 4 acceptance criteria from docs/backlog.md.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from tests.conftest import register_and_login
from tests.integration.test_derivatives import SYNTHETIC_RATE, ingest_clean_chain
from tests.integration.test_surface import analyse, calibrate

POSITIONS_CSV = (
    Path(__file__).resolve().parents[1] / "data" / "portfolio_options.csv"
).read_bytes()

MAPPING = {
    "symbol": "SYMBOL",
    "exchange": "EXCH",
    "asset_class": "TYPE",
    "quantity": "NETQTY",
    "average_price": "AVGPRICE",
    "expiry": "EXPIRY_DT",
    "strike": "STRIKE_PRICE",
    "option_type": "CE_PE",
    "side": "SIDE",
    "strategy_tag": "TAG",
}
DEFAULTS = {"currency": "INR", "exchange": "SYNTH", "multiplier": "75"}


async def create_portfolio(client, header, name="Book") -> str:
    response = await client.post(
        "/portfolios",
        headers={"Authorization": header},
        json={"name": name, "base_currency": "INR"},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def upload_positions(client, header, data: bytes = POSITIONS_CSV) -> str:
    response = await client.post(
        "/uploads",
        headers={"Authorization": header},
        files={"file": ("positions.csv", data, "text/csv")},
        data={"kind": "POSITIONS"},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def run_job(client, header, job_id) -> dict:
    job = await client.get(f"/jobs/{job_id}", headers={"Authorization": header})
    assert job.json()["status"] == "COMPLETED", job.json()
    result = await client.get(f"/jobs/{job_id}/result", headers={"Authorization": header})
    return result.json()["result"]


@pytest.fixture
async def chain(client, auth_header, clean_chain_csv):
    """The contracts in the instrument master, with quotes. No surface.

    Most of this module only needs the contracts to exist; calibration is
    minutes of SLSQP and is asked for only where a model price is the subject.
    """
    return await ingest_clean_chain(client, auth_header, clean_chain_csv)


@pytest.fixture
async def market(client, auth_header, clean_chain_csv):
    """A calibrated surface, so option legs can be marked to model as well."""
    snapshot_id = await ingest_clean_chain(client, auth_header, clean_chain_csv)
    analysis_id = await analyse(client, auth_header, snapshot_id)
    body = await calibrate(client, auth_header, analysis_id)
    return {"snapshot_id": snapshot_id, "surface_row_id": body["results"]["surface_row_id"]}


@pytest.fixture
async def imported(client, auth_header, market):
    portfolio_id = await create_portfolio(client, auth_header)
    upload_id = await upload_positions(client, auth_header)
    accepted = await client.post(
        f"/portfolios/{portfolio_id}/import",
        headers={"Authorization": auth_header},
        json={"upload_id": upload_id, "column_mapping": MAPPING, "defaults": DEFAULTS},
    )
    assert accepted.status_code == 202, accepted.text
    body = await run_job(client, auth_header, accepted.json()["job_id"])
    return {"portfolio_id": portfolio_id, "upload_id": upload_id, "body": body}


class TestPortfolioCrud:
    async def test_a_portfolio_round_trips(self, client, auth_header):
        portfolio_id = await create_portfolio(client, auth_header, "Vol book")
        response = await client.get(
            f"/portfolios/{portfolio_id}", headers={"Authorization": auth_header}
        )
        assert response.status_code == 200
        assert response.json()["name"] == "Vol book"
        assert response.json()["base_currency"] == "INR"

    async def test_another_user_cannot_see_it(self, client, auth_header):
        portfolio_id = await create_portfolio(client, auth_header)
        _other, other_header = await register_and_login(client)
        response = await client.get(
            f"/portfolios/{portfolio_id}", headers={"Authorization": other_header}
        )
        # 404, not 403: a 403 would confirm the id exists.
        assert response.status_code == 404

    async def test_another_user_cannot_value_it(self, client, auth_header):
        portfolio_id = await create_portfolio(client, auth_header)
        _other, other_header = await register_and_login(client)
        response = await client.post(
            f"/portfolios/{portfolio_id}/valuation",
            headers={"Authorization": other_header},
            json={"risk_free_rate": SYNTHETIC_RATE},
        )
        assert response.status_code == 404

    async def test_a_zero_quantity_position_is_refused(self, client, auth_header, chain):
        portfolio_id = await create_portfolio(client, auth_header)
        instruments = await client.get(
            "/instruments", headers={"Authorization": auth_header}, params={"limit": 1}
        )
        instrument_id = instruments.json()["items"][0]["id"]
        response = await client.post(
            f"/portfolios/{portfolio_id}/positions",
            headers={"Authorization": auth_header},
            json={"instrument_id": instrument_id, "quantity": "0"},
        )
        assert response.status_code == 422
        assert response.json()["code"] == "INVALID_POSITION"

    async def test_a_side_that_disagrees_with_the_sign_is_refused(self, client, auth_header, chain):
        portfolio_id = await create_portfolio(client, auth_header)
        instruments = await client.get(
            "/instruments", headers={"Authorization": auth_header}, params={"limit": 1}
        )
        instrument_id = instruments.json()["items"][0]["id"]
        response = await client.post(
            f"/portfolios/{portfolio_id}/positions",
            headers={"Authorization": auth_header},
            json={"instrument_id": instrument_id, "quantity": "5", "side": "SHORT"},
        )
        assert response.status_code == 422


class TestImport:
    async def test_the_preview_infers_the_mapping_and_splits_three_ways(
        self, client, auth_header, chain
    ):
        portfolio_id = await create_portfolio(client, auth_header)
        upload_id = await upload_positions(client, auth_header)
        response = await client.post(
            f"/portfolios/{portfolio_id}/import/preview",
            headers={"Authorization": auth_header},
            json={"upload_id": upload_id, "defaults": DEFAULTS},
        )
        assert response.status_code == 200, response.text
        body = response.json()

        assert body["inferred_mapping"]["quantity"] == "NETQTY"
        assert body["rows_in"] == len(body["resolved"]) + len(body["ambiguous"]) + len(
            body["invalid"]
        )
        assert len(body["resolved"]) == 7
        assert len(body["invalid"]) == 3
        assert body["committable"] is True

    async def test_every_rejected_row_names_its_row_number_and_reason(
        self, client, auth_header, chain
    ):
        portfolio_id = await create_portfolio(client, auth_header)
        upload_id = await upload_positions(client, auth_header)
        response = await client.post(
            f"/portfolios/{portfolio_id}/import/preview",
            headers={"Authorization": auth_header},
            json={"upload_id": upload_id, "defaults": DEFAULTS},
        )
        for invalid in response.json()["invalid"]:
            assert invalid["row_number"] > 0
            assert invalid["reason"]
            assert invalid["message"]

    async def test_the_preview_resolves_against_the_ingested_chain(
        self, client, auth_header, chain
    ):
        """The contracts are already in the master, so nothing is created."""
        portfolio_id = await create_portfolio(client, auth_header)
        upload_id = await upload_positions(client, auth_header)
        response = await client.post(
            f"/portfolios/{portfolio_id}/import/preview",
            headers={"Authorization": auth_header},
            json={"upload_id": upload_id, "defaults": DEFAULTS},
        )
        resolved = response.json()["resolved"]
        assert all(row["creates_instrument"] is False for row in resolved)

    async def test_a_commit_inserts_only_the_resolved_rows(self, client, auth_header, imported):
        positions = await client.get(
            f"/portfolios/{imported['portfolio_id']}/positions",
            headers={"Authorization": auth_header},
        )
        assert len(positions.json()) == 7
        assert imported["body"]["results"]["committed"] == 7

    async def test_a_commit_reports_the_rows_it_could_not_use(self, imported):
        codes = {w["code"] for w in imported["body"]["warnings"]}
        assert "PORTFOLIO_IMPORT_INVALID_ROWS" in codes

    async def test_the_import_records_the_file_it_came_from(self, imported):
        results = imported["body"]["results"]
        assert results["upload_id"] == imported["upload_id"]
        assert len(results["dataset_digest"]) == 64

    async def test_a_commit_without_a_mapping_is_refused(self, client, auth_header, chain):
        portfolio_id = await create_portfolio(client, auth_header)
        upload_id = await upload_positions(client, auth_header)
        response = await client.post(
            f"/portfolios/{portfolio_id}/import",
            headers={"Authorization": auth_header},
            json={"upload_id": upload_id, "column_mapping": {}, "defaults": DEFAULTS},
        )
        assert response.status_code == 400

    async def test_a_mapping_missing_a_required_field_is_refused(self, client, auth_header, chain):
        portfolio_id = await create_portfolio(client, auth_header)
        upload_id = await upload_positions(client, auth_header)
        response = await client.post(
            f"/portfolios/{portfolio_id}/import",
            headers={"Authorization": auth_header},
            json={
                "upload_id": upload_id,
                "column_mapping": {"symbol": "SYMBOL"},
                "defaults": DEFAULTS,
            },
        )
        assert response.status_code == 422
        assert response.json()["missing_required"] == ["quantity"]

    async def test_an_option_chain_upload_is_not_importable_as_positions(
        self, client, auth_header, clean_chain_csv
    ):
        portfolio_id = await create_portfolio(client, auth_header)
        upload = await client.post(
            "/uploads",
            headers={"Authorization": auth_header},
            files={"file": ("chain.csv", clean_chain_csv, "text/csv")},
        )
        response = await client.post(
            f"/portfolios/{portfolio_id}/import/preview",
            headers={"Authorization": auth_header},
            json={"upload_id": upload.json()["id"], "defaults": DEFAULTS},
        )
        assert response.status_code == 422
        assert response.json()["code"] == "WRONG_UPLOAD_KIND"

    async def test_a_positions_file_is_not_ingestable_as_a_chain(self, client, auth_header):
        upload_id = await upload_positions(client, auth_header)
        response = await client.post(
            f"/uploads/{upload_id}/ingest",
            headers={"Authorization": auth_header},
            json={
                "kind": "POSITIONS",
                "underlying": {"symbol": "NIFTY", "exchange": "SYNTH"},
                "as_of_timestamp": "2026-09-24T09:20:00Z",
                "column_mapping": MAPPING,
            },
        )
        assert response.status_code == 400
        assert response.json()["code"] == "WRONG_INGESTION_ROUTE"


@pytest.fixture
async def valued(client, auth_header, imported):
    accepted = await client.post(
        f"/portfolios/{imported['portfolio_id']}/valuation",
        headers={"Authorization": auth_header},
        json={
            "risk_free_rate": SYNTHETIC_RATE,
            "dividend_yield": 0.0,
            "settlement_time_utc": "10:00:00",
        },
    )
    assert accepted.status_code == 202, accepted.text
    body = await run_job(client, auth_header, accepted.json()["job_id"])
    return {"portfolio_id": imported["portfolio_id"], "body": body}


class TestValuation:
    async def test_every_position_is_valued_and_says_how(self, valued):
        results = valued["body"]["results"]
        assert results["counts"]["positions"] == 7
        assert results["counts"]["valued"] == 7
        assert sum(results["valuation_methods"].values()) == 7
        assert "UNAVAILABLE" not in results["valuation_methods"]

    async def test_the_totals_are_reported_with_their_units(self, valued):
        results = valued["body"]["results"]
        assert set(results["totals"]) == {
            "base_market_value",
            "unrealized_pnl",
            "gross_exposure",
            "net_exposure",
        }
        assert set(results["greek_units"]) == set(results["greeks"])

    async def test_one_snapshot_priced_the_whole_portfolio(self, valued):
        """A delta and a vega in one report cannot come from different minutes."""
        results = valued["body"]["results"]
        provenance = valued["body"]["provenance"]
        assert results["market_state_id"].startswith("state:")
        assert provenance["market_state_id"] == results["market_state_id"]

    async def test_the_run_records_what_it_can_be_reproduced_from(self, valued):
        provenance = valued["body"]["provenance"]
        assert provenance["market_state_timestamp"]
        assert provenance["market_data_sources"]
        assert provenance["model_versions"]["valuation"]
        assert provenance["model_versions"]["pricing"]
        assert provenance["parameters"]["context"]["risk_free_rate"] == SYNTHETIC_RATE

    async def test_position_detail_keeps_observation_and_estimate_apart(
        self, client, auth_header, valued
    ):
        detail = await client.get(
            f"/portfolios/{valued['portfolio_id']}/valuation/"
            f"{valued['body']['results']['valuation_id']}",
            headers={"Authorization": auth_header},
        )
        assert detail.status_code == 200, detail.text
        positions = detail.json()["results"]["positions_detail"]
        assert len(positions) == 7

        options = [p for p in positions if p["asset_class"] == "OPTION"]
        assert options
        for one in options:
            assert one["valuation_method"] in {"MARKET_MID", "MARKET_LAST", "MODEL_REFERENCE"}
            if one["valuation_method"] == "MARKET_MID":
                assert one["price_used"] == one["market_price"]
                # Recorded next to the observation, never in place of it.
                assert one["model_price"] is not None
            assert one["greek_source"] in {"MARKET_IV", "REFERENCE_IV"}

    async def test_the_sum_over_positions_equals_the_portfolio_total(
        self, client, auth_header, valued
    ):
        """Phase 4 acceptance, over the wire."""
        detail = await client.get(
            f"/portfolios/{valued['portfolio_id']}/valuation/"
            f"{valued['body']['results']['valuation_id']}",
            headers={"Authorization": auth_header},
        )
        body = detail.json()["results"]
        total = sum(
            Decimal(p["base_market_value"])
            for p in body["positions_detail"]
            if p["base_market_value"] is not None
        )
        assert Decimal(body["base_market_value"]) == total

    async def test_the_latest_valuation_is_readable_on_its_own(self, client, auth_header, valued):
        response = await client.get(
            f"/portfolios/{valued['portfolio_id']}/valuation",
            headers={"Authorization": auth_header},
        )
        assert response.status_code == 200
        assert response.json()["valuation_id"] == valued["body"]["results"]["valuation_id"]

    async def test_a_portfolio_with_no_market_data_fails_with_a_reason(self, client, auth_header):
        portfolio_id = await create_portfolio(client, auth_header, "Empty")
        accepted = await client.post(
            f"/portfolios/{portfolio_id}/valuation",
            headers={"Authorization": auth_header},
            json={"risk_free_rate": SYNTHETIC_RATE, "settlement_time_utc": "10:00:00"},
        )
        body = await run_job(client, auth_header, accepted.json()["job_id"])
        assert body["status"] == "FAILED"
        assert body["results"] is None
        assert {w["code"] for w in body["warnings"]} == {"PORTFOLIO_NO_MARKET_DATA"}

    async def test_valuing_without_a_settlement_time_warns_rather_than_guessing(
        self, client, auth_header, imported
    ):
        accepted = await client.post(
            f"/portfolios/{imported['portfolio_id']}/valuation",
            headers={"Authorization": auth_header},
            json={"risk_free_rate": SYNTHETIC_RATE},
        )
        body = await run_job(client, auth_header, accepted.json()["job_id"])
        assert "PORTFOLIO_NO_SETTLEMENT_TIME" in {w["code"] for w in body["warnings"]}


class TestGreeks:
    async def test_greeks_are_grouped_and_named(self, client, auth_header, valued):
        response = await client.get(
            f"/portfolios/{valued['portfolio_id']}/greeks",
            headers={"Authorization": auth_header},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert set(body["greeks"]["units"]) >= {"delta", "gamma", "vega_per_vol_point"}
        assert {b["dimension"] for b in body["aggregates"]} == {
            "UNDERLYING",
            "EXPIRY",
            "ASSET_CLASS",
            "STRATEGY_TAG",
            "CURRENCY",
        }

    async def test_one_dimension_can_be_requested(self, client, auth_header, valued):
        response = await client.get(
            f"/portfolios/{valued['portfolio_id']}/greeks",
            headers={"Authorization": auth_header},
            params={"dimension": "expiry"},
        )
        assert {b["dimension"] for b in response.json()["aggregates"]} == {"EXPIRY"}

    async def test_the_strategy_tags_from_the_file_survive_into_the_grouping(
        self, client, auth_header, valued
    ):
        response = await client.get(
            f"/portfolios/{valued['portfolio_id']}/greeks",
            headers={"Authorization": auth_header},
            params={"dimension": "STRATEGY_TAG"},
        )
        keys = {b["key"] for b in response.json()["aggregates"]}
        assert keys == {"carry", "atm", "wings", "hedge"}

    async def test_each_grouping_sums_to_the_portfolio_total(self, client, auth_header, valued):
        response = await client.get(
            f"/portfolios/{valued['portfolio_id']}/greeks",
            headers={"Authorization": auth_header},
        )
        body = response.json()
        for dimension in ("UNDERLYING", "ASSET_CLASS", "CURRENCY", "STRATEGY_TAG"):
            buckets = [b for b in body["aggregates"] if b["dimension"] == dimension]
            total = sum(Decimal(b["base_market_value"]) for b in buckets)
            assert total == Decimal(body["base_market_value"])
            assert sum(b["greeks"]["delta"] for b in buckets) == pytest.approx(
                body["greeks"]["delta"], rel=1e-9
            )


class TestLanguage:
    async def test_no_endpoint_recommends_a_trade(self, client, auth_header, valued):
        """Product language policy: no advice, no fair value, no signal."""
        forbidden = (
            "fair value",
            "underpriced",
            "overpriced",
            "arbitrage opportunity",
            "optimal execution",
            "recommendation",
            "buy signal",
            "sell signal",
        )
        for path in (
            f"/portfolios/{valued['portfolio_id']}/valuation",
            f"/portfolios/{valued['portfolio_id']}/greeks",
        ):
            body = (await client.get(path, headers={"Authorization": auth_header})).text.lower()
            for phrase in forbidden:
                assert phrase not in body
