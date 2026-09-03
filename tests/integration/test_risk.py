"""Phase 5 end to end: build a history, measure risk on it, stress the book.

Carries the Phase 5 acceptance criteria from docs/backlog.md over the wire.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import numpy as np
import pytest

from tests.conftest import register_and_login
from tests.integration.test_derivatives import SYNTHETIC_RATE, ingest_clean_chain
from tests.integration.test_portfolio import (
    DEFAULTS,
    MAPPING,
    create_portfolio,
    run_job,
    upload_positions,
)
from tests.integration.test_surface import analyse, calibrate

#: Enough observations that the historical estimator is past its own
#: reliability threshold, so the tests exercise the answer rather than the
#: insufficient-history refusal (which has its own test).
HISTORY_DAYS = 260
BASE_AS_OF = datetime(2026, 9, 24, 9, 20, tzinfo=UTC)


async def seed_price_history(db_session, user_id, underlying_id, days: int = HISTORY_DAYS):
    """Backdated chain snapshots, which is where the price history lives.

    Written directly rather than by ingesting a chain a few hundred times: the
    rows are the same rows the pipeline writes, and the point of the test is the
    risk engine, not the ingestion path that has its own tests.
    """
    from domains.market_data.repository import MarketDataRepository

    repository = MarketDataRepository(db_session)
    rng = np.random.default_rng(20_260_924)
    level = 24_000.0
    for offset in range(days, 0, -1):
        level *= float(np.exp(rng.normal(0.0, 0.011)))
        await repository.create_chain_snapshot(
            user_id=user_id,
            underlying_id=underlying_id,
            as_of_timestamp=BASE_AS_OF - timedelta(days=offset),
            source="test-history",
            provider="test",
            dataset_digest=f"history-{offset}",
            underlying_price=Decimal(str(round(level, 2))),
            rows_input=0,
            rows_kept=0,
            rows_excluded=0,
            rows_rejected=0,
            quality_summary={},
            provenance={"seeded": "risk history fixture"},
        )
    await db_session.commit()


async def underlying_id_for(client, header) -> uuid.UUID:
    response = await client.get(
        "/instruments",
        headers={"Authorization": header},
        params={"asset_class": "INDEX", "limit": 1},
    )
    return uuid.UUID(response.json()["items"][0]["id"])


@pytest.fixture
async def book(client, auth_header, clean_chain_csv, db_session):
    """A calibrated market, a portfolio of options, and a price history."""
    snapshot_id = await ingest_clean_chain(client, auth_header, clean_chain_csv)
    analysis_id = await analyse(client, auth_header, snapshot_id)
    await calibrate(client, auth_header, analysis_id)

    portfolio_id = await create_portfolio(client, auth_header, "Risk book")
    upload_id = await upload_positions(client, auth_header)
    accepted = await client.post(
        f"/portfolios/{portfolio_id}/import",
        headers={"Authorization": auth_header},
        json={"upload_id": upload_id, "column_mapping": MAPPING, "defaults": DEFAULTS},
    )
    await run_job(client, auth_header, accepted.json()["job_id"])

    me = await client.get("/auth/me", headers={"Authorization": auth_header})
    underlying_id = await underlying_id_for(client, auth_header)
    await seed_price_history(db_session, uuid.UUID(me.json()["id"]), underlying_id)
    return {"portfolio_id": portfolio_id, "underlying_id": underlying_id}


async def run_var(client, header, portfolio_id, **overrides) -> dict:
    payload = {
        "method": "HISTORICAL",
        "risk_free_rate": SYNTHETIC_RATE,
        "settlement_time_utc": "10:00:00",
        **overrides,
    }
    accepted = await client.post(
        f"/portfolios/{portfolio_id}/var",
        headers={"Authorization": header},
        json=payload,
    )
    assert accepted.status_code == 202, accepted.text
    return await run_job(client, header, accepted.json()["job_id"])


async def run_stress(client, header, portfolio_id, scenario: str, **overrides) -> dict:
    accepted = await client.post(
        f"/portfolios/{portfolio_id}/stress",
        headers={"Authorization": header},
        json={
            "scenario": scenario,
            "risk_free_rate": SYNTHETIC_RATE,
            "settlement_time_utc": "10:00:00",
            **overrides,
        },
    )
    assert accepted.status_code == 202, accepted.text
    return await run_job(client, header, accepted.json()["job_id"])


class TestScenarioLibrary:
    async def test_the_templates_are_all_labelled_hypothetical(self, client, auth_header):
        response = await client.get("/scenarios", headers={"Authorization": auth_header})
        assert response.status_code == 200
        body = response.json()
        assert len(body) >= 8
        for scenario in body:
            assert scenario["source"] == "HYPOTHETICAL"
            assert scenario["derivation"] is None
            assert "not a historical event" in scenario["description"]

    async def test_a_user_defined_scenario_cannot_claim_to_be_historical(self, client, auth_header):
        """There is no field for it, and the stored source says what it is."""
        response = await client.post(
            "/scenarios",
            headers={"Authorization": auth_header},
            json={
                "name": "My crash",
                "source": "DERIVED_FROM_HISTORY",
                "shocks": [
                    {
                        "kind": "UNDERLYING_PRICE",
                        "shock_type": "PERCENTAGE",
                        "value": -0.35,
                    }
                ],
            },
        )
        assert response.status_code == 201
        assert response.json()["source"] == "USER_DEFINED"
        assert response.json()["derivation"] is None

    async def test_a_nonsensical_shock_is_refused(self, client, auth_header):
        response = await client.post(
            "/scenarios",
            headers={"Authorization": auth_header},
            json={
                "name": "Rates by percent",
                "shocks": [{"kind": "RISK_FREE_RATE", "shock_type": "PERCENTAGE", "value": 0.1}],
            },
        )
        assert response.status_code == 422

    async def test_a_duplicate_name_is_refused(self, client, auth_header):
        body = {
            "name": "Twice",
            "shocks": [{"kind": "UNDERLYING_PRICE", "shock_type": "PERCENTAGE", "value": -0.1}],
        }
        first = await client.post("/scenarios", headers={"Authorization": auth_header}, json=body)
        assert first.status_code == 201
        second = await client.post("/scenarios", headers={"Authorization": auth_header}, json=body)
        assert second.status_code == 422

    async def test_another_user_cannot_see_or_delete_it(self, client, auth_header):
        created = await client.post(
            "/scenarios",
            headers={"Authorization": auth_header},
            json={
                "name": "Private",
                "shocks": [{"kind": "UNDERLYING_PRICE", "shock_type": "PERCENTAGE", "value": -0.1}],
            },
        )
        scenario_id = created.json()["id"]
        _other, other_header = await register_and_login(client)
        assert (
            await client.get(f"/scenarios/{scenario_id}", headers={"Authorization": other_header})
        ).status_code == 404
        assert (
            await client.delete(
                f"/scenarios/{scenario_id}", headers={"Authorization": other_header}
            )
        ).status_code == 404


class TestDerivedScenario:
    async def test_it_carries_the_series_it_came_from(self, client, auth_header, book):
        response = await client.post(
            "/scenarios/derive",
            headers={"Authorization": auth_header},
            json={"name": "Worst recorded day", "underlying_id": str(book["underlying_id"])},
        )
        assert response.status_code == 201, response.text
        body = response.json()

        assert body["source"] == "DERIVED_FROM_HISTORY"
        derivation = body["derivation"]
        assert derivation is not None
        assert derivation["observations"] >= HISTORY_DAYS
        assert derivation["event_date"] >= derivation["start_date"]
        assert derivation["event_date"] <= derivation["end_date"]
        assert derivation["method"].startswith("worst")
        assert body["shocks"][0]["value"] < 0.0

    async def test_it_can_be_stressed_like_any_other(self, client, auth_header, book):
        await client.post(
            "/scenarios/derive",
            headers={"Authorization": auth_header},
            json={"name": "Worst recorded day", "underlying_id": str(book["underlying_id"])},
        )
        result = await run_stress(client, auth_header, book["portfolio_id"], "Worst recorded day")
        assert result["status"] in {"OK", "PARTIAL"}
        assert result["results"]["scenario"]["source"] == "DERIVED_FROM_HISTORY"

    async def test_an_underlying_with_one_observation_is_refused_not_invented(
        self, client, auth_header, clean_chain_csv
    ):
        """One ingested chain is one price, and one price has no move in it."""
        await ingest_clean_chain(client, auth_header, clean_chain_csv)
        underlying_id = await underlying_id_for(client, auth_header)

        response = await client.post(
            "/scenarios/derive",
            headers={"Authorization": auth_header},
            json={"name": "Nothing to derive from", "underlying_id": str(underlying_id)},
        )
        assert response.status_code == 422
        assert response.json()["code"] == "INSUFFICIENT_HISTORY"
        assert response.json()["observations"] == 1


class TestValueAtRisk:
    async def test_the_historical_method_reprices_and_says_so(self, client, auth_header, book):
        result = await run_var(client, auth_header, book["portfolio_id"])
        assert result["status"] in {"OK", "PARTIAL"}, result
        results = result["results"]

        assert results["method"] == "HISTORICAL"
        assert results["assumptions"]["repricing"] == "full"
        assert results["scenarios"] >= HISTORY_DAYS - 5
        for tail in results["tail_risk"]:
            assert tail["value_at_risk"] > 0.0
            assert tail["expected_shortfall"] >= tail["value_at_risk"]

    async def test_the_two_measures_are_explained_in_the_payload(self, client, auth_header, book):
        result = await run_var(client, auth_header, book["portfolio_id"])
        interpretation = result["results"]["tail_risk"][0]["interpretation"]
        assert "threshold" in interpretation["value_at_risk"]
        assert "average" in interpretation["expected_shortfall"]

    async def test_a_higher_confidence_is_a_larger_loss(self, client, auth_header, book):
        result = await run_var(
            client, auth_header, book["portfolio_id"], confidences=[0.9, 0.95, 0.99]
        )
        values = [tail["value_at_risk"] for tail in result["results"]["tail_risk"]]
        assert values == sorted(values)

    async def test_monte_carlo_records_its_seed_and_repeats_exactly(
        self, client, auth_header, book
    ):
        """Phase 5 acceptance: a Monte Carlo number can be recomputed."""
        first = await run_var(
            client, auth_header, book["portfolio_id"], method="MONTE_CARLO", paths=4000, seed=77
        )
        second = await run_var(
            client, auth_header, book["portfolio_id"], method="MONTE_CARLO", paths=4000, seed=77
        )
        assert [t["value_at_risk"] for t in first["results"]["tail_risk"]] == [
            t["value_at_risk"] for t in second["results"]["tail_risk"]
        ]
        assert "Seed 77" in first["results"]["assumptions"]["reproducibility"]
        assert first["provenance"]["parameters"]["run"]["seed"] == 77

    async def test_the_parametric_method_declares_that_it_did_not_reprice(
        self, client, auth_header, book
    ):
        result = await run_var(client, auth_header, book["portfolio_id"], method="PARAMETRIC")
        results = result["results"]
        assert results["assumptions"]["repricing"].startswith("none")
        assert "RISK_PARAMETRIC_ON_NONLINEAR_BOOK" in results["warnings"]
        assert any(
            warning["code"] == "RISK_PARAMETRIC_ON_NONLINEAR_BOOK" for warning in result["warnings"]
        )

    async def test_the_factor_panel_and_its_policy_travel_with_the_answer(
        self, client, auth_header, book
    ):
        result = await run_var(client, auth_header, book["portfolio_id"])
        panel = result["results"]["factor_panel"]
        assert panel["policy"] == "INTERSECT_DATES"
        assert "forward-filled" in panel["missing_data_policy"]
        assert panel["observations"] == result["results"]["scenarios"]
        assert panel["factors"][0]["source"] == "CHAIN_SNAPSHOTS"

    async def test_a_portfolio_with_no_history_refuses_rather_than_answering(
        self, client, auth_header, clean_chain_csv
    ):
        snapshot_id = await ingest_clean_chain(client, auth_header, clean_chain_csv)
        analysis_id = await analyse(client, auth_header, snapshot_id)
        await calibrate(client, auth_header, analysis_id)
        portfolio_id = await create_portfolio(client, auth_header, "No history")
        upload_id = await upload_positions(client, auth_header)
        accepted = await client.post(
            f"/portfolios/{portfolio_id}/import",
            headers={"Authorization": auth_header},
            json={"upload_id": upload_id, "column_mapping": MAPPING, "defaults": DEFAULTS},
        )
        await run_job(client, auth_header, accepted.json()["job_id"])

        result = await run_var(client, auth_header, portfolio_id)
        assert result["status"] == "FAILED"
        assert result["results"] is None
        codes = {warning["code"] for warning in result["warnings"]}
        assert codes == {"RISK_INSUFFICIENT_HISTORY"}

    async def test_a_run_is_listed_and_anchored_to_a_snapshot(self, client, auth_header, book):
        await run_var(client, auth_header, book["portfolio_id"])
        listed = await client.get(
            f"/portfolios/{book['portfolio_id']}/var",
            headers={"Authorization": auth_header},
        )
        assert listed.status_code == 200
        assert len(listed.json()) >= 1
        row = listed.json()[0]
        assert row["method"] == "HISTORICAL"
        assert row["snapshot_id"]

        snapshot = await client.get(
            f"/portfolios/{book['portfolio_id']}/risk-snapshot",
            headers={"Authorization": auth_header},
        )
        assert snapshot.status_code == 200
        assert snapshot.json()["valuation_id"]
        assert snapshot.json()["positions"] == 7

    async def test_another_user_cannot_run_or_read_it(self, client, auth_header, book):
        _other, other_header = await register_and_login(client)
        assert (
            await client.post(
                f"/portfolios/{book['portfolio_id']}/var",
                headers={"Authorization": other_header},
                json={"method": "HISTORICAL"},
            )
        ).status_code == 404
        assert (
            await client.get(
                f"/portfolios/{book['portfolio_id']}/var",
                headers={"Authorization": other_header},
            )
        ).status_code == 404


class TestStress:
    async def test_a_sell_off_reprices_rather_than_extrapolating(self, client, auth_header, book):
        """Phase 5 acceptance, over the wire: the two answers differ."""
        result = await run_stress(client, auth_header, book["portfolio_id"], "Underlying -10%")
        results = result["results"]
        full = results["pnl"]
        greek = results["greek_approximation"]["pnl"]

        assert full != greek
        assert abs(results["greek_approximation"]["difference_from_full_revaluation"]) > 0.0
        assert abs(full - greek) / abs(full) > 0.01
        assert "approximation" in results["greek_approximation"]["caveat"].lower()

    async def test_a_small_shock_makes_the_two_nearly_agree(self, client, auth_header, book):
        await client.post(
            "/scenarios",
            headers={"Authorization": auth_header},
            json={
                "name": "Tiny move",
                "shocks": [
                    {"kind": "UNDERLYING_PRICE", "shock_type": "PERCENTAGE", "value": -0.001}
                ],
            },
        )
        results = (await run_stress(client, auth_header, book["portfolio_id"], "Tiny move"))[
            "results"
        ]
        assert (
            abs(results["pnl"] - results["greek_approximation"]["pnl"]) / abs(results["pnl"]) < 0.02
        )

    async def test_the_shocks_actually_applied_are_reported(self, client, auth_header, book):
        results = (
            await run_stress(
                client, auth_header, book["portfolio_id"], "Sell-off with volatility spike"
            )
        )["results"]
        shock = next(iter(results["shocks"].values()))
        assert shock["spot_return"] == pytest.approx(-0.08)
        assert shock["vol_points"] == pytest.approx(0.08)

    async def test_contributions_decompose_the_loss(self, client, auth_header, book):
        results = (await run_stress(client, auth_header, book["portfolio_id"], "Underlying -10%"))[
            "results"
        ]
        dimensions = {item["dimension"] for item in results["contributions"]}
        assert {"UNDERLYING", "EXPIRY", "ASSET_CLASS", "STRATEGY_TAG"} <= dimensions

        for breakdown in results["contributions"]:
            assert breakdown["residual"] == pytest.approx(0.0, abs=1e-6)
            grouped = sum(item["contribution"] for item in breakdown["contributions"])
            assert grouped + breakdown["ungrouped_pnl"] == pytest.approx(
                breakdown["total_pnl"], rel=1e-9
            )

    async def test_the_strategy_tags_from_the_import_group_the_loss(
        self, client, auth_header, book
    ):
        results = (await run_stress(client, auth_header, book["portfolio_id"], "Underlying -10%"))[
            "results"
        ]
        tags = next(
            item for item in results["contributions"] if item["dimension"] == "STRATEGY_TAG"
        )
        assert {item["key"] for item in tags["contributions"]} == {
            "carry",
            "atm",
            "wings",
            "hedge",
        }

    async def test_time_decay_can_be_applied_alongside_the_shock(self, client, auth_header, book):
        instant = (await run_stress(client, auth_header, book["portfolio_id"], "Underlying -5%"))[
            "results"
        ]
        decayed = (
            await run_stress(
                client,
                auth_header,
                book["portfolio_id"],
                "Underlying -5%",
                time_decay_days=10.0,
            )
        )["results"]
        assert decayed["time_decay_days"] == 10.0
        assert decayed["pnl"] != instant["pnl"]

    async def test_an_unknown_scenario_is_a_404(self, client, auth_header, book):
        response = await client.post(
            f"/portfolios/{book['portfolio_id']}/stress",
            headers={"Authorization": auth_header},
            json={"scenario": "No such scenario"},
        )
        assert response.status_code == 404

    async def test_the_run_is_reproducible_from_its_provenance(self, client, auth_header, book):
        result = await run_stress(client, auth_header, book["portfolio_id"], "Underlying -10%")
        provenance = result["provenance"]
        assert provenance["market_state_id"].startswith("state:")
        assert provenance["model_versions"]["stress"]
        assert provenance["model_versions"]["pricing"]
        assert provenance["parameters"]["scenario"]["name"] == "Underlying -10%"
        assert provenance["parameters"]["run"]["risk_free_rate"] == SYNTHETIC_RATE

    async def test_a_stress_run_is_listed(self, client, auth_header, book):
        await run_stress(client, auth_header, book["portfolio_id"], "Underlying -5%")
        listed = await client.get(
            f"/portfolios/{book['portfolio_id']}/stress",
            headers={"Authorization": auth_header},
        )
        assert listed.status_code == 200
        assert listed.json()[0]["scenario_name"] == "Underlying -5%"


class TestLanguage:
    async def test_no_risk_response_recommends_a_trade(self, client, auth_header, book):
        """Product language policy, over the whole serialised response."""
        forbidden = (
            "fair value",
            "underpriced",
            "overpriced",
            "arbitrage opportunity",
            "optimal execution",
            "recommendation",
            "buy signal",
            "sell signal",
            "will be liquidated",
            "guaranteed",
        )
        payloads = [
            str(await run_var(client, auth_header, book["portfolio_id"])),
            str(await run_stress(client, auth_header, book["portfolio_id"], "Underlying -10%")),
            (await client.get("/scenarios", headers={"Authorization": auth_header})).text,
        ]
        for payload in payloads:
            lowered = payload.lower()
            for phrase in forbidden:
                assert phrase not in lowered
