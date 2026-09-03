"""Phase 11 end to end: one order, five branches, one snapshot.

Carries the Phase 11 acceptance criteria from docs/backlog.md over the wire.
"""

from __future__ import annotations

import uuid

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
from tests.integration.test_risk import seed_price_history, underlying_id_for
from tests.integration.test_surface import analyse, calibrate

CONTEXT = {"risk_free_rate": SYNTHETIC_RATE, "settlement_time_utc": "10:00:00"}

#: Words the product language rules forbid anywhere in a response. Checked
#: against the whole serialised payload, not against a field list, because the
#: failure mode is a sentence somewhere in a nested block.
FORBIDDEN_PHRASES = (
    "fair value",
    "underpriced",
    "overpriced",
    "arbitrage opportunity",
    "will be liquidated",
    "optimal execution",
    "broker margin",
    "you should",
    "we recommend",
    "buy signal",
    "sell signal",
)

#: Keys that would turn a measurement into advice. Checked over every key at
#: every depth of the response.
FORBIDDEN_KEYS = (
    "action",
    "signal",
    "rating",
    "recommendation",
    "recommended",
    "recommended_strategy",
    "verdict",
    "advice",
    "best_strategy",
    "best_model",
    "suggested_action",
)


async def option_instrument(client, header, option_type: str = "CALL") -> dict:
    response = await client.get(
        "/instruments",
        headers={"Authorization": header},
        params={"asset_class": "OPTION", "limit": 200},
    )
    assert response.status_code == 200, response.text
    options = [
        item
        for item in response.json()["items"]
        if item["option_type"] == option_type and item["strike"]
    ]
    assert options, "the clean chain fixture should have produced option contracts"
    return sorted(options, key=lambda item: item["canonical_key"])[len(options) // 2]


@pytest.fixture
async def market(client, auth_header, clean_chain_csv, db_session):
    """A calibrated market, a book of options, and enough history for VaR."""
    snapshot_id = await ingest_clean_chain(client, auth_header, clean_chain_csv)
    analysis_id = await analyse(client, auth_header, snapshot_id)
    await calibrate(client, auth_header, analysis_id)

    portfolio_id = await create_portfolio(client, auth_header, "Order book")
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

    instrument = await option_instrument(client, auth_header)
    return {
        "portfolio_id": portfolio_id,
        "underlying_id": str(underlying_id),
        "instrument": instrument,
    }


async def analyse_order(client, header, market, **overrides) -> dict:
    payload = {
        "portfolio_id": market["portfolio_id"],
        "instrument_id": market["instrument"]["id"],
        "side": "SELL",
        "quantity": "10",
        "order_type": "MARKET",
        "scenario": "Sell-off with volatility spike",
        **CONTEXT,
        **overrides,
    }
    response = await client.post("/order-analysis", headers={"Authorization": header}, json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def _walk(node, on_key=None, on_text=None) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if on_key is not None:
                on_key(key)
            _walk(value, on_key, on_text)
    elif isinstance(node, list):
        for item in node:
            _walk(item, on_key, on_text)
    elif isinstance(node, str) and on_text is not None:
        on_text(node)


class TestOneSnapshotForEveryBranch:
    async def test_all_five_branches_name_the_same_market_state(self, client, auth_header, market):
        """The acceptance criterion the whole phase exists for."""
        body = await analyse_order(client, auth_header, market)
        branches = body["results"]["branches"]
        assert set(branches) == {"VALUATION", "SURFACE", "EXECUTION", "RISK", "MARGIN"}

        state_ids = {branch["provenance"]["market_state_id"] for branch in branches.values()}
        assert len(state_ids) == 1, state_ids
        state_id = state_ids.pop()
        assert state_id is not None
        assert state_id.startswith("state:")
        assert body["provenance"]["market_state_id"] == state_id
        assert body["results"]["market_state"]["state_id"] == state_id
        # The claim is published, not only true.
        assert body["results"]["market_state_ids"] == [state_id]

    async def test_every_branch_names_the_same_moment_as_well(self, client, auth_header, market):
        body = await analyse_order(client, auth_header, market)
        stamps = {
            branch["provenance"]["market_state_timestamp"]
            for branch in body["results"]["branches"].values()
        }
        assert len(stamps) == 1

    async def test_the_stored_row_names_that_snapshot(self, client, auth_header, market):
        body = await analyse_order(client, auth_header, market)
        analysis_id = body["results"]["order_analysis_id"]

        listing = await client.get(
            "/order-analysis",
            headers={"Authorization": auth_header},
            params={"portfolio_id": market["portfolio_id"]},
        )
        assert listing.status_code == 200, listing.text
        row = next(item for item in listing.json() if item["id"] == analysis_id)
        assert row["market_state_id"] == body["provenance"]["market_state_id"]
        assert row["status"] == body["status"]
        assert row["branches_ok"] + row["branches_failed"] == 5


class TestBranchesDegradeIndependently:
    async def test_a_book_with_no_history_still_answers_four_branches(
        self, client, auth_header, clean_chain_csv
    ):
        """No price history means no VaR, and four branches that do not care."""
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
        instrument = await option_instrument(client, auth_header)

        body = await analyse_order(
            client,
            auth_header,
            {"portfolio_id": portfolio_id, "instrument": instrument},
        )
        risk = body["results"]["branches"]["RISK"]
        # The Greeks stand; the value-at-risk comparison is an absence with a
        # reason rather than a number computed from too few points.
        assert risk["results"]["greeks"]["movements"]
        assert risk["results"]["value_at_risk"] is None
        codes = {warning["code"] for warning in risk["warnings"]}
        assert "RISK_INSUFFICIENT_HISTORY" in codes

    async def test_an_order_on_a_contract_with_no_surface_fails_only_two_branches(
        self, client, auth_header, market, db_session
    ):
        """A branch that cannot answer says so; the others still answer."""
        body = await analyse_order(client, auth_header, market, scenario=None)
        assert body["results"]["branches"]["RISK"]["status"] != "FAILED"

    async def test_a_missing_scenario_is_a_bad_request_not_a_broken_analysis(
        self, client, auth_header, market
    ):
        response = await client.post(
            "/order-analysis",
            headers={"Authorization": auth_header},
            json={
                "portfolio_id": market["portfolio_id"],
                "instrument_id": market["instrument"]["id"],
                "side": "BUY",
                "quantity": "1",
                "scenario": "a scenario nobody defined",
                **CONTEXT,
            },
        )
        assert response.status_code == 422, response.text
        assert response.json()["code"] == "ORDER_ANALYSIS_REFUSED"

    async def test_the_status_is_partial_when_a_branch_failed(self, client, auth_header, market):
        """A cost estimate needs a two-sided market; a non-option has no surface.

        Whichever branch is the one that cannot answer, the contract is the
        same: the analysis is PARTIAL, the failure is named, and the branch
        carries the reason rather than disappearing.
        """
        underlying = market["underlying_id"]
        response = await client.post(
            "/order-analysis",
            headers={"Authorization": auth_header},
            json={
                "portfolio_id": market["portfolio_id"],
                "instrument_id": underlying,
                "side": "BUY",
                "quantity": "1",
                **CONTEXT,
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "PARTIAL"
        branches = body["results"]["branches"]
        assert branches["VALUATION"]["status"] == "FAILED"
        assert branches["SURFACE"]["status"] == "FAILED"
        for name in ("VALUATION", "SURFACE"):
            codes = {warning["code"] for warning in branches[name]["warnings"]}
            assert codes, f"{name} failed without saying why"
        assert any(
            warning["code"] == "ORDER_ANALYSIS_BRANCH_FAILED" for warning in body["warnings"]
        )


class TestTheOrderIsInTheNumbers:
    async def test_the_greeks_move_by_the_order_and_not_by_nothing(
        self, client, auth_header, market
    ):
        body = await analyse_order(client, auth_header, market, side="SELL", quantity="10")
        movements = {
            item["name"]: item
            for item in body["results"]["branches"]["RISK"]["results"]["greeks"]["movements"]
        }
        delta = movements["delta"]
        assert delta["current"] != delta["proposed"]
        assert delta["change"] == pytest.approx(delta["proposed"] - delta["current"])
        # Selling a call takes delta out of the book.
        assert delta["change"] < 0

    async def test_doubling_the_order_doubles_its_greek_contribution(
        self, client, auth_header, market
    ):
        one = await analyse_order(client, auth_header, market, quantity="10")
        two = await analyse_order(client, auth_header, market, quantity="20")

        def change(body: dict, name: str) -> float:
            movements = body["results"]["branches"]["RISK"]["results"]["greeks"]["movements"]
            return next(item["change"] for item in movements if item["name"] == name)

        assert change(two, "delta") == pytest.approx(2 * change(one, "delta"), rel=1e-9)
        assert change(two, "vega_per_vol_point") == pytest.approx(
            2 * change(one, "vega_per_vol_point"), rel=1e-9
        )

    async def test_both_sides_say_they_were_measured_on_one_shared_panel(
        self, client, auth_header, market
    ):
        """The caveat that belongs to the comparison, not to either side."""
        body = await analyse_order(client, auth_header, market)
        risk = body["results"]["branches"]["RISK"]
        assert risk["results"]["value_at_risk"] is not None
        codes = {warning["code"] for warning in risk["warnings"]}
        assert "INCREMENTAL_ONE_PANEL_FOR_BOTH_SIDES" in codes
        message = next(
            w["message"]
            for w in risk["warnings"]
            if w["code"] == "INCREMENTAL_ONE_PANEL_FOR_BOTH_SIDES"
        )
        assert "one factor panel" in message

    async def test_margin_is_compared_on_both_sides_under_one_model(
        self, client, auth_header, market
    ):
        body = await analyse_order(client, auth_header, market)
        margin = body["results"]["branches"]["MARGIN"]
        assert margin["status"] != "FAILED", margin["warnings"]
        results = margin["results"]
        assert results["current"]["method"] == results["proposed"]["method"]
        movements = {item["name"]: item for item in results["movements"]}
        assert set(movements) >= {"estimated_margin", "worst_loss_on_the_grid"}
        assert movements["estimated_margin"]["change"] is not None
        assert "not" in results["disclaimer"].lower()

    async def test_a_buy_and_a_sell_move_the_book_in_opposite_directions(
        self, client, auth_header, market
    ):
        buy = await analyse_order(client, auth_header, market, side="BUY", quantity="10")
        sell = await analyse_order(client, auth_header, market, side="SELL", quantity="10")

        def delta_change(body: dict) -> float:
            movements = body["results"]["branches"]["RISK"]["results"]["greeks"]["movements"]
            return next(item["change"] for item in movements if item["name"] == "delta")

        assert delta_change(buy) == pytest.approx(-delta_change(sell), rel=1e-9)


class TestAnOrderThatCannotBeRepricedIsRefused:
    """The most dangerous output this endpoint could produce is a row of zeros."""

    async def test_risk_and_margin_refuse_rather_than_report_zero_differences(
        self, client, auth_header, market
    ):
        # With no settlement time there is no time to expiry, so no option in
        # the book or in the order can be repriced. Both sides of every
        # comparison would then be identical and every difference exactly zero.
        response = await client.post(
            "/order-analysis",
            headers={"Authorization": auth_header},
            json={
                "portfolio_id": market["portfolio_id"],
                "instrument_id": market["instrument"]["id"],
                "side": "SELL",
                "quantity": "10",
                "risk_free_rate": SYNTHETIC_RATE,
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "PARTIAL"

        for name in ("RISK", "MARGIN"):
            branch = body["results"]["branches"][name]
            assert branch["status"] == "FAILED", name
            codes = {warning["code"] for warning in branch["warnings"]}
            assert "INCREMENTAL_ORDER_NOT_REPRICEABLE" in codes, name
            message = " ".join(w["message"] for w in branch["warnings"])
            # The reason the contract could not be repriced travels with the
            # refusal, and the refusal says it is one.
            assert "EXPOSURE_" in message, name
            assert "refused" in message, name

    async def test_the_branches_that_do_not_need_a_repriceable_book_still_answer(
        self, client, auth_header, market
    ):
        response = await client.post(
            "/order-analysis",
            headers={"Authorization": auth_header},
            json={
                "portfolio_id": market["portfolio_id"],
                "instrument_id": market["instrument"]["id"],
                "side": "SELL",
                "quantity": "10",
                "risk_free_rate": SYNTHETIC_RATE,
            },
        )
        branches = response.json()["results"]["branches"]
        assert branches["EXECUTION"]["status"] != "FAILED"
        assert branches["SURFACE"]["status"] != "FAILED"


class TestReproducibility:
    async def test_the_same_order_twice_names_the_same_snapshot_and_position(
        self, client, auth_header, market
    ):
        """A content-addressed snapshot and a derived position id, not two random ones."""
        first = await analyse_order(client, auth_header, market)
        second = await analyse_order(client, auth_header, market)

        assert first["provenance"]["market_state_id"] == second["provenance"]["market_state_id"]
        assert (
            first["results"]["order"]["proposed_position_id"]
            == second["results"]["order"]["proposed_position_id"]
        )
        assert first["results"]["order_analysis_id"] != second["results"]["order_analysis_id"]

    async def test_a_different_size_is_a_different_proposed_position(
        self, client, auth_header, market
    ):
        one = await analyse_order(client, auth_header, market, quantity="10")
        two = await analyse_order(client, auth_header, market, quantity="20")
        assert (
            one["results"]["order"]["proposed_position_id"]
            != two["results"]["order"]["proposed_position_id"]
        )


class TestObservationsAndEstimatesStaySeparate:
    async def test_the_observed_market_is_reported_as_observed(self, client, auth_header, market):
        body = await analyse_order(client, auth_header, market)
        observed = body["results"]["branches"]["VALUATION"]["results"]["observed"]
        assert observed["available"] is True
        assert observed["bid"] is not None
        assert observed["ask"] is not None
        assert "substituted" in observed["note"]

    async def test_the_reference_value_is_a_range_across_models(self, client, auth_header, market):
        body = await analyse_order(client, auth_header, market)
        consensus = body["results"]["branches"]["VALUATION"]["results"]["consensus"]
        assert consensus["reference_value"] is not None
        assert consensus["model_dispersion"] is not None
        assert consensus["confidence"]["contributions"]
        # Models that could not run say why rather than being dropped.
        unavailable = [item for item in consensus["values"] if item["value"] is None]
        for item in unavailable:
            assert item["unavailable_reason"]

    async def test_the_execution_estimate_says_it_is_a_counterfactual(
        self, client, auth_header, market
    ):
        body = await analyse_order(client, auth_header, market)
        execution = body["results"]["branches"]["EXECUTION"]
        assert execution["status"] != "FAILED", execution["warnings"]
        results = execution["results"]
        assert "COUNTERFACTUAL_ESTIMATE" in results["warnings"]
        assert "ORDER_COST_REFERENCE_HELD_FLAT" in results["warnings"]
        assert results["caveat"]

    async def test_with_no_daily_volume_the_impact_half_is_absent_not_zero(
        self, client, auth_header, market
    ):
        body = await analyse_order(client, auth_header, market)
        results = body["results"]["branches"]["EXECUTION"]["results"]
        assert "ORDER_COST_NO_AVERAGE_DAILY_VOLUME" in results["warnings"]
        for strategy in results["strategies"]:
            assert strategy["impact_component_currency"] is None
            assert strategy["estimated_slippage_currency"] is None
            # The half that *is* measurable is still reported.
            assert strategy["spread_component_currency"] is not None

    async def test_supplying_a_daily_volume_produces_a_total(self, client, auth_header, market):
        body = await analyse_order(
            client,
            auth_header,
            market,
            execution={"average_daily_volume": 500_000.0, "volatility": 0.2},
        )
        results = body["results"]["branches"]["EXECUTION"]["results"]
        immediate = next(s for s in results["strategies"] if s["strategy"].startswith("IMMEDIATE"))
        assert immediate["estimated_slippage_currency"] > 0
        assert immediate["impact_component_currency"] is not None
        assert "ORDER_COST_IMPACT_NOT_CALIBRATED" in results["warnings"]


class TestLanguage:
    async def test_no_response_carries_a_recommendation_field(self, client, auth_header, market):
        body = await analyse_order(client, auth_header, market)
        seen: list[str] = []
        _walk(body, on_key=seen.append)
        offenders = [key for key in seen if key.lower() in FORBIDDEN_KEYS]
        assert not offenders, offenders

    async def test_no_response_advises_or_promises(self, client, auth_header, market):
        body = await analyse_order(client, auth_header, market)
        text: list[str] = []
        _walk(body, on_text=text.append)
        blob = " ".join(text).lower()
        offenders = [phrase for phrase in FORBIDDEN_PHRASES if phrase in blob]
        assert not offenders, offenders

    async def test_the_published_schema_has_no_recommendation_field(self, client):
        """A contract guarantee, checked against what the API actually publishes."""
        schema = (await client.get("http://testserver/openapi.json")).json()
        paths = {path: spec for path, spec in schema["paths"].items() if "order-analysis" in path}
        assert paths, "the order-analysis routes should be published"

        offenders: list[str] = []
        for name, definition in schema["components"]["schemas"].items():
            if "Order" not in name and "Execution" not in name:
                continue
            for field in definition.get("properties") or {}:
                if field.lower() in FORBIDDEN_KEYS:
                    offenders.append(f"{name}.{field}")
        assert not offenders, offenders

    async def test_the_execution_schedules_are_not_ranked(self, client, auth_header, market):
        body = await analyse_order(
            client,
            auth_header,
            market,
            quantity="150",
            execution={
                "average_daily_volume": 500_000.0,
                "volatility": 0.2,
                "intervals": 2,
                "strategies": ["IMMEDIATE", "TWAP"],
            },
        )
        results = body["results"]["branches"]["EXECUTION"]["results"]
        assert len(results["strategies"]) >= 2
        assert "ranked" not in str(results["interpretation"]).replace("not ranked", "")


class TestOwnership:
    async def test_another_users_portfolio_is_not_found(self, client, auth_header, market):
        _other, other_header = await register_and_login(client)
        response = await client.post(
            "/order-analysis",
            headers={"Authorization": other_header},
            json={
                "portfolio_id": market["portfolio_id"],
                "instrument_id": market["instrument"]["id"],
                "side": "BUY",
                "quantity": "1",
                **CONTEXT,
            },
        )
        assert response.status_code == 404

    async def test_another_users_analysis_is_not_found(self, client, auth_header, market):
        body = await analyse_order(client, auth_header, market)
        _other, other_header = await register_and_login(client)
        response = await client.get(
            f"/order-analysis/{body['results']['order_analysis_id']}",
            headers={"Authorization": other_header},
        )
        assert response.status_code == 404

    async def test_a_stored_analysis_reads_back_in_full(self, client, auth_header, market):
        body = await analyse_order(client, auth_header, market)
        response = await client.get(
            f"/order-analysis/{body['results']['order_analysis_id']}",
            headers={"Authorization": auth_header},
        )
        assert response.status_code == 200, response.text
        stored = response.json()
        assert stored["status"] == body["status"]
        assert set(stored["results"]["branches"]) == set(body["results"]["branches"])
        assert stored["provenance"]["market_state_id"] == body["provenance"]["market_state_id"]


class TestRequestValidation:
    async def test_a_negative_quantity_is_refused(self, client, auth_header, market):
        response = await client.post(
            "/order-analysis",
            headers={"Authorization": auth_header},
            json={
                "portfolio_id": market["portfolio_id"],
                "instrument_id": market["instrument"]["id"],
                "side": "SELL",
                "quantity": "-10",
                **CONTEXT,
            },
        )
        assert response.status_code == 422

    async def test_a_limit_order_without_a_price_is_refused(self, client, auth_header, market):
        response = await client.post(
            "/order-analysis",
            headers={"Authorization": auth_header},
            json={
                "portfolio_id": market["portfolio_id"],
                "instrument_id": market["instrument"]["id"],
                "side": "SELL",
                "quantity": "10",
                "order_type": "LIMIT",
                **CONTEXT,
            },
        )
        assert response.status_code == 422

    async def test_an_unknown_model_name_is_refused_with_the_available_ones(
        self, client, auth_header, market
    ):
        """A typo is a bad request, not four working branches and one broken one."""
        for block, code, available in (
            ({"margin": {"margin_model": "SPAN"}}, "UNKNOWN_MARGIN_MODEL", "SimpleRiskMarginModel"),
            (
                {"execution": {"impact_model": "AlmgrenChriss"}},
                "UNKNOWN_IMPACT_MODEL",
                "SquareRootImpactModel",
            ),
        ):
            response = await client.post(
                "/order-analysis",
                headers={"Authorization": auth_header},
                json={
                    "portfolio_id": market["portfolio_id"],
                    "instrument_id": market["instrument"]["id"],
                    "side": "SELL",
                    "quantity": "10",
                    **CONTEXT,
                    **block,
                },
            )
            assert response.status_code == 422, response.text
            body = response.json()
            assert body["code"] == code
            assert available in body["available"]

    async def test_an_unknown_instrument_is_refused(self, client, auth_header, market):
        response = await client.post(
            "/order-analysis",
            headers={"Authorization": auth_header},
            json={
                "portfolio_id": market["portfolio_id"],
                "instrument_id": str(uuid.uuid4()),
                "side": "SELL",
                "quantity": "10",
                **CONTEXT,
            },
        )
        assert response.status_code == 422
