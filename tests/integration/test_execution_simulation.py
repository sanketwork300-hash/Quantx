"""Phase 8 end to end: schedule, simulate, compare — all counterfactual.

Carries the Phase 8 acceptance criteria from docs/backlog.md over the wire.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from tests.conftest import register_and_login
from tests.integration.test_derivatives import ingest_clean_chain

WINDOW = {"start": "2026-09-24T09:20:00Z", "end": "2026-09-24T15:20:00Z"}
#: A U-shaped intraday profile — the shape that makes VWAP differ from TWAP.
VOLUMES = [30000.0, 12000.0, 8000.0, 7000.0, 9000.0, 25000.0]


async def run_job(client, header, job_id) -> dict:
    job = await client.get(f"/jobs/{job_id}", headers={"Authorization": header})
    assert job.json()["status"] == "COMPLETED", job.json()
    result = await client.get(f"/jobs/{job_id}/result", headers={"Authorization": header})
    return result.json()["result"]


@pytest.fixture
async def instrument_id(client, auth_header, clean_chain_csv) -> str:
    """A contract with quotes behind it, so a window can be built at all."""
    await ingest_clean_chain(client, auth_header, clean_chain_csv)
    response = await client.get(
        "/instruments",
        headers={"Authorization": auth_header},
        params={"asset_class": "OPTION", "limit": 50},
    )
    options = response.json()["items"]
    chosen = next(
        (item for item in options if item["canonical_key"].endswith("24000:C")), options[0]
    )
    return chosen["id"]


async def simulate(client, header, instrument_id, **overrides) -> dict:
    payload = {
        "instrument_id": instrument_id,
        "side": "BUY",
        "quantity": "7500",
        **WINDOW,
        "intervals": 6,
        "strategies": ["TWAP"],
        "impact_model": "SquareRootImpactModel",
        "volatility": 0.18,
        "average_daily_volume": 500000.0,
        "lot_size": "75",
        # One chain is one observation, so the strict tolerance would leave
        # every slice unfilled. Widened deliberately, and recorded on the row.
        "max_price_age_seconds": 604800.0,
        **overrides,
    }
    accepted = await client.post(
        "/execution/simulate", headers={"Authorization": header}, json=payload
    )
    assert accepted.status_code == 202, accepted.text
    return await run_job(client, header, accepted.json()["job_id"])


class TestCatalogues:
    async def test_the_strategies_are_listed_with_what_each_one_needs(self, client, auth_header):
        response = await client.get("/execution/strategies", headers={"Authorization": auth_header})
        assert response.status_code == 200
        names = {item["name"]: item for item in response.json()}
        assert set(names) == {"TWAP", "VWAP", "POV", "LiquidityAdaptive"}
        assert names["TWAP"]["requires"] == []
        assert names["VWAP"]["requires"]
        assert names["LiquidityAdaptive"]["requires"]

    async def test_no_impact_model_ships_a_calibrated_coefficient(self, client, auth_header):
        response = await client.get(
            "/execution/impact-models", headers={"Authorization": auth_header}
        )
        assert response.status_code == 200
        body = response.json()
        assert {item["name"] for item in body} == {
            "SquareRootImpactModel",
            "LinearImpactModel",
            "ZeroImpactModel",
        }
        assert all(item["ships_calibrated_coefficients"] is False for item in body)

    async def test_an_unknown_strategy_is_refused_with_the_available_ones(
        self, client, auth_header, instrument_id
    ):
        response = await client.post(
            "/execution/simulate",
            headers={"Authorization": auth_header},
            json={
                "instrument_id": instrument_id,
                "side": "BUY",
                "quantity": "100",
                **WINDOW,
                "strategies": ["AlmgrenChriss"],
                "average_daily_volume": 500000.0,
            },
        )
        assert response.status_code == 422
        assert response.json()["code"] == "UNKNOWN_STRATEGY"
        assert "TWAP" in response.json()["available"]

    async def test_an_unknown_impact_model_is_refused(self, client, auth_header, instrument_id):
        response = await client.post(
            "/execution/simulate",
            headers={"Authorization": auth_header},
            json={
                "instrument_id": instrument_id,
                "side": "BUY",
                "quantity": "100",
                **WINDOW,
                "impact_model": "Propagator",
                "average_daily_volume": 500000.0,
            },
        )
        assert response.status_code == 422
        assert response.json()["code"] == "UNKNOWN_IMPACT_MODEL"


class TestEverythingIsCounterfactual:
    """Phase 8 acceptance, over the wire."""

    async def test_the_result_says_so_in_its_own_payload(self, client, auth_header, instrument_id):
        result = await simulate(client, auth_header, instrument_id)
        assert result["status"] in {"OK", "PARTIAL"}, result
        results = result["results"]

        assert results["counterfactual"] is True
        assert "never executed" in results["caveat"]
        for strategy in results["strategies"]:
            assert strategy["counterfactual"] is True
            assert "COUNTERFACTUAL_ESTIMATE" in strategy["warnings"]

    async def test_it_is_the_first_warning_on_the_envelope(
        self, client, auth_header, instrument_id
    ):
        result = await simulate(client, auth_header, instrument_id)
        codes = [warning["code"] for warning in result["warnings"]]
        assert codes[0] == "COUNTERFACTUAL_ESTIMATE"

    async def test_a_comparison_says_it_is_not_a_ranking(self, client, auth_header, instrument_id):
        result = await simulate(
            client,
            auth_header,
            instrument_id,
            strategies=["TWAP", "VWAP"],
            expected_volumes=VOLUMES,
        )
        caveat = result["results"]["comparison_caveat"]
        assert "not a ranking" in caveat
        assert "no strategy is recommended" in caveat

    async def test_the_stored_row_cannot_be_anything_but_counterfactual(
        self, client, auth_header, instrument_id
    ):
        await simulate(client, auth_header, instrument_id)
        listed = await client.get("/execution/simulations", headers={"Authorization": auth_header})
        assert listed.status_code == 200
        assert all(row["counterfactual"] is True for row in listed.json())


class TestSchedules:
    async def test_slices_sum_to_the_parent_quantity(self, client, auth_header, instrument_id):
        """Phase 8 acceptance, read back off the stored schedule."""
        result = await simulate(client, auth_header, instrument_id, quantity="7500")
        for strategy in result["results"]["strategies"]:
            detail = strategy["schedule"]
            assert Decimal(detail["parent_quantity"]) == Decimal(7500)

        listed = await client.get("/execution/simulations", headers={"Authorization": auth_header})
        simulation_id = listed.json()[0]["id"]
        stored = await client.get(
            f"/execution/simulations/{simulation_id}",
            headers={"Authorization": auth_header},
        )
        slices = stored.json()["results"]["schedule"]["slice_detail"]
        assert sum(Decimal(item["quantity"]) for item in slices) == Decimal(7500)

    async def test_vwap_follows_the_supplied_profile(self, client, auth_header, instrument_id):
        result = await simulate(
            client,
            auth_header,
            instrument_id,
            strategies=["VWAP"],
            expected_volumes=VOLUMES,
        )
        listed = await client.get("/execution/simulations", headers={"Authorization": auth_header})
        stored = await client.get(
            f"/execution/simulations/{listed.json()[0]['id']}",
            headers={"Authorization": auth_header},
        )
        slices = stored.json()["results"]["schedule"]["slice_detail"]
        quantities = [Decimal(item["quantity"]) for item in slices]
        assert quantities[0] > quantities[3] < quantities[-1]
        assert result["results"]["strategies"][0]["strategy"].startswith("VWAP@")

    async def test_a_strategy_whose_inputs_are_missing_is_reported_not_degraded(
        self, client, auth_header, instrument_id
    ):
        result = await simulate(
            client, auth_header, instrument_id, strategies=["TWAP", "VWAP", "POV"]
        )
        unavailable = {
            item["strategy"]: item["reason"] for item in result["results"]["unavailable"]
        }
        assert "VWAP" in unavailable
        assert "TWAP under the VWAP name" in unavailable["VWAP"]
        assert "POV" in unavailable
        assert [item["strategy"] for item in result["results"]["strategies"]][0].startswith("TWAP@")

    async def test_when_no_strategy_can_run_the_result_fails_with_reasons(
        self, client, auth_header, instrument_id
    ):
        result = await simulate(client, auth_header, instrument_id, strategies=["VWAP"])
        assert result["status"] == "FAILED"
        assert result["results"] is None
        assert {w["code"] for w in result["warnings"]} == {"SIMULATION_NO_STRATEGY_AVAILABLE"}

    async def test_a_per_interval_input_must_cover_every_interval(
        self, client, auth_header, instrument_id
    ):
        response = await client.post(
            "/execution/simulate",
            headers={"Authorization": auth_header},
            json={
                "instrument_id": instrument_id,
                "side": "BUY",
                "quantity": "7500",
                **WINDOW,
                "intervals": 6,
                "expected_volumes": [1.0, 2.0],
                "average_daily_volume": 500000.0,
            },
        )
        assert response.status_code == 422

    async def test_a_window_that_ends_before_it_starts_is_refused(
        self, client, auth_header, instrument_id
    ):
        response = await client.post(
            "/execution/simulate",
            headers={"Authorization": auth_header},
            json={
                "instrument_id": instrument_id,
                "side": "BUY",
                "quantity": "7500",
                "start": "2026-09-24T15:20:00Z",
                "end": "2026-09-24T09:20:00Z",
                "average_daily_volume": 500000.0,
            },
        )
        assert response.status_code == 422


class TestImpact:
    async def test_the_uncalibrated_default_is_flagged_on_the_result_and_the_row(
        self, client, auth_header, instrument_id
    ):
        result = await simulate(client, auth_header, instrument_id)
        codes = {warning["code"] for warning in result["warnings"]}
        assert "SIMULATION_IMPACT_NOT_CALIBRATED" in codes
        message = next(
            w["message"]
            for w in result["warnings"]
            if w["code"] == "SIMULATION_IMPACT_NOT_CALIBRATED"
        )
        assert "rather than a magnitude anyone measured" in message

        listed = await client.get("/execution/simulations", headers={"Authorization": auth_header})
        assert listed.json()[0]["impact_is_calibrated"] is False

    async def test_supplying_coefficients_clears_the_flag(self, client, auth_header, instrument_id):
        result = await simulate(
            client,
            auth_header,
            instrument_id,
            permanent_coefficient=0.31,
            temporary_coefficient=0.14,
        )
        codes = {warning["code"] for warning in result["warnings"]}
        assert "SIMULATION_IMPACT_NOT_CALIBRATED" not in codes

        listed = await client.get("/execution/simulations", headers={"Authorization": auth_header})
        assert listed.json()[0]["impact_is_calibrated"] is True

    async def test_impact_makes_a_buy_more_expensive_than_no_impact_at_all(
        self, client, auth_header, instrument_id
    ):
        free = await simulate(client, auth_header, instrument_id, impact_model="ZeroImpactModel")
        costed = await simulate(
            client,
            auth_header,
            instrument_id,
            permanent_coefficient=0.5,
            temporary_coefficient=0.3,
        )
        assert Decimal(costed["results"]["strategies"][0]["average_price"]) > Decimal(
            free["results"]["strategies"][0]["average_price"]
        )
        assert Decimal(free["results"]["strategies"][0]["modelled_impact_cost"]) == 0

    async def test_the_square_root_and_linear_models_disagree(
        self, client, auth_header, instrument_id
    ):
        square = await simulate(
            client,
            auth_header,
            instrument_id,
            impact_model="SquareRootImpactModel",
            permanent_coefficient=0.3,
            temporary_coefficient=0.2,
        )
        linear = await simulate(
            client,
            auth_header,
            instrument_id,
            impact_model="LinearImpactModel",
            permanent_coefficient=0.3,
            temporary_coefficient=0.2,
        )
        assert Decimal(square["results"]["strategies"][0]["modelled_impact_cost"]) != Decimal(
            linear["results"]["strategies"][0]["modelled_impact_cost"]
        )


class TestCoverage:
    async def test_a_strict_price_age_leaves_slices_unfilled_and_says_why(
        self, client, auth_header, instrument_id
    ):
        """One ingested chain is one observation; a strict tolerance refuses."""
        result = await simulate(client, auth_header, instrument_id, max_price_age_seconds=60.0)
        strategy = result["results"]["strategies"][0]
        assert strategy["completion_rate"] < 1.0
        assert strategy["unfilled"]
        assert all(item["reason"] for item in strategy["unfilled"])

        codes = {warning["code"] for warning in result["warnings"]}
        assert "SIMULATION_INCOMPLETE_SCHEDULE" in codes

    async def test_a_window_with_no_price_at_all_fails_with_a_reason(
        self, client, auth_header, instrument_id
    ):
        result = await simulate(
            client,
            auth_header,
            instrument_id,
            start="2020-01-01T09:20:00Z",
            end="2020-01-01T15:20:00Z",
        )
        assert result["status"] == "FAILED"
        assert result["results"] is None
        assert "a counterfactual needs a path" in str(result["warnings"]).lower()


class TestPersistenceAndOwnership:
    async def test_a_comparison_groups_its_rows(self, client, auth_header, instrument_id):
        result = await simulate(
            client,
            auth_header,
            instrument_id,
            strategies=["TWAP", "VWAP"],
            expected_volumes=VOLUMES,
        )
        comparison_id = result["results"]["comparison_id"]
        listed = await client.get(
            "/execution/simulations",
            headers={"Authorization": auth_header},
            params={"comparison_id": comparison_id},
        )
        assert len(listed.json()) == 2
        assert {row["strategy"].split("@")[0] for row in listed.json()} == {"TWAP", "VWAP"}

    async def test_a_stored_run_reads_back_whole(self, client, auth_header, instrument_id):
        await simulate(client, auth_header, instrument_id)
        listed = await client.get("/execution/simulations", headers={"Authorization": auth_header})
        detail = await client.get(
            f"/execution/simulations/{listed.json()[0]['id']}",
            headers={"Authorization": auth_header},
        )
        assert detail.status_code == 200, detail.text
        body = detail.json()["results"]
        assert body["counterfactual"] is True
        assert body["schedule"]["slice_detail"]
        assert body["context"]["interval_detail"]
        assert detail.json()["provenance"]["model_versions"]["simulation"]

    async def test_the_run_records_what_it_can_be_reproduced_from(
        self, client, auth_header, instrument_id
    ):
        result = await simulate(
            client, auth_header, instrument_id, permanent_coefficient=0.31, latency_seconds=2.0
        )
        parameters = result["provenance"]["parameters"]["simulation"]
        assert parameters["permanent_coefficient"] == 0.31
        assert parameters["latency_seconds"] == 2.0
        assert parameters["average_daily_volume"] == 500000.0
        assert parameters["strategies"] == ["TWAP"]

    async def test_another_user_sees_nothing(self, client, auth_header, instrument_id):
        await simulate(client, auth_header, instrument_id)
        listed = await client.get("/execution/simulations", headers={"Authorization": auth_header})
        simulation_id = listed.json()[0]["id"]

        _other, other_header = await register_and_login(client)
        assert (
            await client.get("/execution/simulations", headers={"Authorization": other_header})
        ).json() == []
        assert (
            await client.get(
                f"/execution/simulations/{simulation_id}",
                headers={"Authorization": other_header},
            )
        ).status_code == 404

    async def test_an_unknown_instrument_is_refused_before_a_job_is_created(
        self, client, auth_header
    ):
        response = await client.post(
            "/execution/simulate",
            headers={"Authorization": auth_header},
            json={
                "instrument_id": str(uuid.uuid4()),
                "side": "BUY",
                "quantity": "100",
                **WINDOW,
                "average_daily_volume": 500000.0,
            },
        )
        assert response.status_code == 404


class TestLanguage:
    async def test_no_simulation_response_recommends_a_strategy(
        self, client, auth_header, instrument_id
    ):
        payloads = [
            str(
                await simulate(
                    client,
                    auth_header,
                    instrument_id,
                    strategies=["TWAP", "VWAP"],
                    expected_volumes=VOLUMES,
                )
            ),
            (
                await client.get("/execution/strategies", headers={"Authorization": auth_header})
            ).text,
            (
                await client.get("/execution/impact-models", headers={"Authorization": auth_header})
            ).text,
        ]
        forbidden = (
            "optimal execution",
            "best strategy",
            "you should",
            "we recommend",
            "guaranteed",
            "fair value",
            "buy signal",
        )
        for payload in payloads:
            lowered = payload.lower()
            for phrase in forbidden:
                assert phrase not in lowered, phrase
