"""Phase 6 end to end: estimate margin, scan the buffer, read the region.

Carries the Phase 6 acceptance criteria from docs/backlog.md over the wire.
"""

from __future__ import annotations

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

CONTEXT = {"risk_free_rate": SYNTHETIC_RATE, "settlement_time_utc": "10:00:00"}


@pytest.fixture
async def book(client, auth_header, clean_chain_csv):
    """A calibrated market and an imported book. Margin needs no history."""
    snapshot_id = await ingest_clean_chain(client, auth_header, clean_chain_csv)
    analysis_id = await analyse(client, auth_header, snapshot_id)
    await calibrate(client, auth_header, analysis_id)

    portfolio_id = await create_portfolio(client, auth_header, "Margin book")
    upload_id = await upload_positions(client, auth_header)
    accepted = await client.post(
        f"/portfolios/{portfolio_id}/import",
        headers={"Authorization": auth_header},
        json={"upload_id": upload_id, "column_mapping": MAPPING, "defaults": DEFAULTS},
    )
    await run_job(client, auth_header, accepted.json()["job_id"])
    return portfolio_id


async def run_margin(client, header, portfolio_id, **overrides) -> dict:
    accepted = await client.post(
        f"/portfolios/{portfolio_id}/margin",
        headers={"Authorization": header},
        json={**CONTEXT, **overrides},
    )
    assert accepted.status_code == 202, accepted.text
    return await run_job(client, header, accepted.json()["job_id"])


class TestModelCatalogue:
    async def test_the_models_are_listed_and_none_claims_broker_equivalence(
        self, client, auth_header
    ):
        response = await client.get("/margin/models", headers={"Authorization": auth_header})
        assert response.status_code == 200
        body = response.json()
        assert len(body) >= 1
        for model in body:
            assert model["is_broker_equivalent"] is False
            assert model["version"]
            assert model["description"]

    async def test_an_unknown_model_is_refused_with_the_available_ones(
        self, client, auth_header, book
    ):
        response = await client.post(
            f"/portfolios/{book}/margin",
            headers={"Authorization": auth_header},
            json={**CONTEXT, "margin_model": "SPAN"},
        )
        assert response.status_code == 422
        assert response.json()["code"] == "UNKNOWN_MARGIN_MODEL"
        assert "SimpleRiskMarginModel" in response.json()["available"]


class TestEstimate:
    async def test_the_result_carries_method_assumptions_confidence_and_warnings(
        self, client, auth_header, book
    ):
        """Phase 6 acceptance, over the wire."""
        result = await run_margin(client, auth_header, book)
        assert result["status"] in {"OK", "PARTIAL"}, result
        results = result["results"]

        assert results["margin"]["method"] == "SimpleRiskMarginModel@1.0.0"
        assert len(results["margin"]["assumptions"]) >= 3
        assert 0.0 <= results["margin"]["confidence"] <= 1.0
        assert isinstance(results["margin"]["warnings"], list)
        assert results["estimated_margin"] > 0.0

    async def test_the_disclaimer_travels_as_a_warning_on_every_run(
        self, client, auth_header, book
    ):
        result = await run_margin(client, auth_header, book)
        codes = {warning["code"] for warning in result["warnings"]}
        assert "MARGIN_IS_A_MODEL_ESTIMATE" in codes
        disclaimer = next(
            w for w in result["warnings"] if w["code"] == "MARGIN_IS_A_MODEL_ESTIMATE"
        )
        assert "not your broker" in disclaimer["message"].lower()

    async def test_the_grid_it_measured_over_is_in_the_payload(self, client, auth_header, book):
        result = await run_margin(client, auth_header, book)
        grid = result["results"]["margin"]["parameters"]["grid"]
        assert 0.0 in grid["spot_returns"]
        assert grid["points"] == len(grid["spot_returns"]) * len(grid["vol_points"])

    async def test_a_custom_grid_is_honoured_and_recorded(self, client, auth_header, book):
        result = await run_margin(
            client,
            auth_header,
            book,
            grid={"spot_returns": [-0.3, -0.1, 0.0, 0.1, 0.3], "vol_points": [0.0, 0.1]},
        )
        grid = result["results"]["margin"]["parameters"]["grid"]
        assert grid["spot_returns"] == [-0.3, -0.1, 0.0, 0.1, 0.3]
        assert grid["points"] == 10

    async def test_a_grid_without_an_unshocked_point_is_refused(self, client, auth_header, book):
        response = await client.post(
            f"/portfolios/{book}/margin",
            headers={"Authorization": auth_header},
            json={**CONTEXT, "grid": {"spot_returns": [-0.2, -0.1]}},
        )
        assert response.status_code == 422

    async def test_a_wider_grid_never_estimates_less(self, client, auth_header, book):
        narrow = await run_margin(
            client, auth_header, book, grid={"spot_returns": [-0.05, 0.0, 0.05]}
        )
        wide = await run_margin(
            client, auth_header, book, grid={"spot_returns": [-0.3, -0.05, 0.0, 0.05, 0.3]}
        )
        assert wide["results"]["estimated_margin"] >= narrow["results"]["estimated_margin"]

    async def test_the_zero_defaults_declare_what_they_leave_out(self, client, auth_header, book):
        result = await run_margin(client, auth_header, book)
        codes = {warning["code"] for warning in result["warnings"]}
        assert "MARGIN_NO_SHORT_OPTION_MINIMUM" in codes
        message = next(
            w["message"]
            for w in result["warnings"]
            if w["code"] == "MARGIN_NO_SHORT_OPTION_MINIMUM"
        )
        assert "inventing a rule" in message

    async def test_a_short_option_minimum_can_be_opted_into(self, client, auth_header, book):
        bare = await run_margin(
            client, auth_header, book, grid={"spot_returns": [-0.01, 0.0, 0.01]}
        )
        floored = await run_margin(
            client,
            auth_header,
            book,
            grid={"spot_returns": [-0.01, 0.0, 0.01]},
            short_option_minimum_rate=0.05,
        )
        assert floored["results"]["estimated_margin"] > bare["results"]["estimated_margin"]
        codes = {w["code"] for w in floored["warnings"]}
        assert "MARGIN_NO_SHORT_OPTION_MINIMUM" not in codes

    async def test_the_components_are_broken_out_with_their_bases(self, client, auth_header, book):
        result = await run_margin(client, auth_header, book, concentration_add_on_rate=0.02)
        components = {c["name"]: c for c in result["results"]["margin"]["components"]}
        assert set(components) == {
            "scan_loss",
            "short_option_minimum",
            "concentration_add_on",
        }
        for component in components.values():
            assert len(component["basis"]) > 20


class TestCapitalAndBuffer:
    async def test_unknown_capital_leaves_utilisation_and_buffer_null(
        self, client, auth_header, book
    ):
        results = (await run_margin(client, auth_header, book))["results"]
        assert results["eligible_capital"] is None
        assert results["buffer"] is None
        assert results["utilisation"] is None
        assert results["shortfall_region"] == {"downside": None, "upside": None}
        assert "stands on its own" in results["summary"]

    async def test_utilisation_is_the_ratio_it_claims_to_be(self, client, auth_header, book):
        capital = 20_000_000.0
        results = (await run_margin(client, auth_header, book, eligible_capital=capital))["results"]
        assert results["utilisation"] == pytest.approx(results["estimated_margin"] / capital)
        assert results["buffer"] == pytest.approx(capital - results["estimated_margin"])

    async def test_a_shortfall_region_is_a_region_with_brackets_not_a_price(
        self, client, auth_header, book
    ):
        """Phase 6 acceptance: never a single guaranteed price."""
        results = (await run_margin(client, auth_header, book, eligible_capital=1_000_000.0))[
            "results"
        ]
        region = results["shortfall_region"]
        found = region["downside"] or region["upside"]
        if found is not None:
            assert "approximate_entry" in found
            assert len(found["bracketed_by"]) == 2
            low, high = sorted(found["bracketed_by"])
            assert low <= found["approximate_entry"] <= high
        assert "estimate" in results["summary"]

    async def test_more_capital_moves_the_region_further_away(self, client, auth_header, book):
        near = (await run_margin(client, auth_header, book, eligible_capital=1_000_000.0))[
            "results"
        ]
        far = (await run_margin(client, auth_header, book, eligible_capital=50_000_000.0))[
            "results"
        ]
        near_entry = (near["shortfall_region"]["downside"] or {}).get("approximate_entry")
        far_region = far["shortfall_region"]["downside"]
        assert far_region is None or (
            near_entry is not None and far_region["approximate_entry"] < near_entry
        )

    async def test_the_ladder_reprices_both_sides_at_every_rung(self, client, auth_header, book):
        results = (await run_margin(client, auth_header, book, eligible_capital=20_000_000.0))[
            "results"
        ]
        ladder = results["ladder"]
        assert len(ladder) > 10
        by_return = {point["spot_return"]: point for point in ladder}
        down, flat = by_return[-0.10], by_return[0.0]
        assert down["portfolio_value"] != flat["portfolio_value"]
        assert down["estimated_margin"] != flat["estimated_margin"]
        assert all(point["buffer"] is not None for point in ladder)

    async def test_a_custom_ladder_is_honoured(self, client, auth_header, book):
        results = (
            await run_margin(
                client,
                auth_header,
                book,
                eligible_capital=20_000_000.0,
                ladder=[-0.1, -0.05, 0.0, 0.05, 0.1],
            )
        )["results"]
        assert [point["spot_return"] for point in results["ladder"]] == [
            -0.1,
            -0.05,
            0.0,
            0.05,
            0.1,
        ]

    async def test_a_volatility_co_shock_is_applied_and_stated(self, client, auth_header, book):
        results = (
            await run_margin(
                client, auth_header, book, eligible_capital=20_000_000.0, vol_co_shock=0.05
            )
        )["results"]
        assert results["vol_co_shock"] == 0.05
        assert all(point["vol_points"] == 0.05 for point in results["ladder"])
        assert "+5 vol-point co-shock" in results["summary"]


class TestPersistenceAndProvenance:
    async def test_a_run_is_listed_and_anchored_to_a_snapshot(self, client, auth_header, book):
        await run_margin(client, auth_header, book, eligible_capital=20_000_000.0)
        listed = await client.get(
            f"/portfolios/{book}/margin", headers={"Authorization": auth_header}
        )
        assert listed.status_code == 200
        row = listed.json()[0]
        assert row["method"] == "SimpleRiskMarginModel@1.0.0"
        assert row["snapshot_id"]
        assert row["eligible_capital"] == 20_000_000.0

    async def test_a_stored_result_reads_back_whole(self, client, auth_header, book):
        result = await run_margin(client, auth_header, book, eligible_capital=20_000_000.0)
        margin_id = result["results"]["margin_id"]

        detail = await client.get(
            f"/portfolios/{book}/margin/{margin_id}", headers={"Authorization": auth_header}
        )
        assert detail.status_code == 200, detail.text
        body = detail.json()["results"]
        assert body["ladder"]
        assert body["assumptions"]
        assert body["summary"] == result["results"]["summary"]
        assert detail.json()["provenance"]["model_versions"]["margin"]

    async def test_the_run_records_what_it_can_be_reproduced_from(self, client, auth_header, book):
        result = await run_margin(
            client, auth_header, book, eligible_capital=20_000_000.0, vol_co_shock=0.05
        )
        provenance = result["provenance"]
        assert provenance["market_state_id"].startswith("state:")
        assert provenance["model_versions"]["margin"] == "SimpleRiskMarginModel@1.0.0"
        assert provenance["model_versions"]["pricing"]
        margin = provenance["parameters"]["margin"]
        assert margin["eligible_capital"] == 20_000_000.0
        assert margin["vol_co_shock"] == 0.05
        assert margin["grid"]["points"] > 0

    async def test_another_user_cannot_run_or_read_it(self, client, auth_header, book):
        result = await run_margin(client, auth_header, book)
        margin_id = result["results"]["margin_id"]
        _other, other_header = await register_and_login(client)

        assert (
            await client.post(
                f"/portfolios/{book}/margin",
                headers={"Authorization": other_header},
                json=CONTEXT,
            )
        ).status_code == 404
        assert (
            await client.get(f"/portfolios/{book}/margin", headers={"Authorization": other_header})
        ).status_code == 404
        assert (
            await client.get(
                f"/portfolios/{book}/margin/{margin_id}",
                headers={"Authorization": other_header},
            )
        ).status_code == 404


class TestLanguage:
    async def test_no_margin_response_promises_a_broker_level(self, client, auth_header, book):
        """Phase 6 acceptance, over the whole serialised response."""
        payloads = [
            str(await run_margin(client, auth_header, book, eligible_capital=1_000_000.0)),
            (await client.get("/margin/models", headers={"Authorization": auth_header})).text,
        ]
        forbidden = (
            "will be liquidated",
            "liquidation price",
            "your broker requires",
            "broker margin",
            "guaranteed",
            "fair value",
            "recommendation",
            "span margin",
        )
        for payload in payloads:
            lowered = payload.lower()
            for phrase in forbidden:
                assert phrase not in lowered, phrase

    async def test_the_region_is_described_as_estimated(self, client, auth_header, book):
        results = (await run_margin(client, auth_header, book, eligible_capital=1_000_000.0))[
            "results"
        ]
        summary = results["summary"].lower()
        assert "estimated margin-shortfall region" in summary or "estimate" in summary
