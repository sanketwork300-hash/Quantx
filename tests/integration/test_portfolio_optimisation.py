"""Phase 5 end to end: history → optimiser → target portfolio → risk metrics.

The covariance comes from a real warehouse dataset loaded through the real Phase
3 path. What is being checked is not that the weights are good — the prices are
made up — but that the optimiser refuses what it should refuse, that the
constraints bind, and that the risk of the portfolio it produced is reported
beside the portfolio.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest

from domains.instruments.enums import AssetClass
from domains.instruments.models import make_instrument
from domains.instruments.service import InstrumentService
from tests.conftest import register_and_login

START = datetime(2026, 1, 1, tzinfo=UTC)
SYMBOLS = ("ALPHA", "BETA", "GAMMA")


def csv_bars(symbol: str, count: int, volatility: float, phase: float) -> bytes:
    """Three series with different volatilities and partly shared moves."""
    lines = ["timestamp,symbol,open,high,low,close,volume"]
    price = 100.0
    for day in range(count):
        shared = math.sin(day / 11.0)
        own = math.sin(day / 5.0 + phase)
        price = 100.0 * (1 + 0.0004 * day) + volatility * (0.7 * shared + 0.3 * own)
        moment = (START + timedelta(days=day)).isoformat()
        lines.append(
            f"{moment},{symbol},{price:.4f},{price * 1.005:.4f},"
            f"{price * 0.995:.4f},{price:.4f},100000"
        )
    return "\n".join(lines).encode()


@pytest.fixture
async def instruments(db_session):
    service = InstrumentService(db_session)
    rows = []
    for symbol in SYMBOLS:
        instrument = make_instrument(
            asset_class=AssetClass.EQUITY, exchange="NSE", symbol=symbol, currency="INR"
        )
        await service.upsert(instrument)
        rows.append(instrument)
    await db_session.commit()
    return rows


@pytest.fixture
async def loaded(client, instruments):
    """Three instruments with overlapping warehouse history."""
    _user, header = await register_and_login(client)

    for index, instrument in enumerate(instruments):
        upload = await client.post(
            "/uploads",
            headers={"Authorization": header},
            files={
                "file": (
                    f"{instrument.symbol}.csv",
                    csv_bars(instrument.symbol, 260, 4.0 + index * 3.0, index * 1.3),
                    "text/csv",
                )
            },
            data={"kind": "BARS"},
        )
        assert upload.status_code == 201, upload.text

        job = await client.post(
            "/warehouse/datasets",
            headers={"Authorization": header},
            json={
                "upload_id": upload.json()["id"],
                "name": instrument.symbol,
                "exchange": "NSE",
                "corporate_action_treatment": "UNADJUSTED",
            },
        )
        assert job.status_code == 202, job.text
        result = await client.get(
            f"/jobs/{job.json()['job_id']}/result", headers={"Authorization": header}
        )
        assert result.json()["result"]["results"]["status"] == "AVAILABLE"

    return header, instruments


async def optimise(client, header, instruments, **overrides) -> dict:
    body = {
        "instrument_ids": [str(item.id) for item in instruments],
        "objective": "MINIMUM_VARIANCE",
        "exchange": "NSE",
        **overrides,
    }
    response = await client.post(
        "/portfolio-optimisation/target", headers={"Authorization": header}, json=body
    )
    return response


class TestFromHistoryToTargetPortfolio:
    """The Phase 5 acceptance criterion."""

    async def test_a_minimum_variance_portfolio_comes_back(self, client, loaded):
        header, instruments = loaded
        response = await optimise(client, header, instruments)
        assert response.status_code == 200, response.text

        results = response.json()["results"]
        assert len(results["holdings"]) == 3
        assert sum(item["weight"] for item in results["holdings"]) == pytest.approx(1.0, abs=1e-6)
        assert all(item["weight"] >= -1e-9 for item in results["holdings"])

    async def test_the_risk_of_the_portfolio_is_reported_beside_it(self, client, loaded):
        """The acceptance criterion's last step, and the reason the endpoint
        returns risk rather than only weights."""
        header, instruments = loaded
        response = await optimise(client, header, instruments)
        risk = response.json()["results"]["risk"]

        assert risk["volatility"] > 0
        assert risk["tail"]["value_at_risk"] is not None
        assert risk["effective_assets"] > 1
        assert risk["observations"] > 100
        assert risk["largest_risk_contribution"] > 0

    async def test_risk_contributions_are_reported_per_holding(self, client, loaded):
        """A holding with 5% of the weight and 40% of the risk is the
        portfolio's real position, whatever the weights say."""
        header, instruments = loaded
        response = await optimise(client, header, instruments)
        holdings = response.json()["results"]["holdings"]
        assert sum(item["risk_contribution"] for item in holdings) == pytest.approx(1.0, abs=1e-6)

    async def test_risk_parity_equalises_the_contributions(self, client, loaded):
        header, instruments = loaded
        response = await optimise(client, header, instruments, objective="RISK_PARITY")
        holdings = response.json()["results"]["holdings"]
        contributions = [item["risk_contribution"] for item in holdings]
        assert max(contributions) - min(contributions) < 0.01

    async def test_the_covariance_estimate_travels_with_the_answer(self, client, loaded):
        header, instruments = loaded
        response = await optimise(client, header, instruments)
        covariance = response.json()["results"]["covariance"]
        assert covariance["estimator"] == "SAMPLE"
        assert covariance["observations"] > 100
        assert len(covariance["correlation"]) == 3

    async def test_shrinkage_is_asked_for_and_reported(self, client, loaded):
        header, instruments = loaded
        response = await optimise(client, header, instruments, covariance_estimator="LEDOIT_WOLF")
        covariance = response.json()["results"]["covariance"]
        assert covariance["estimator"] == "LEDOIT_WOLF"
        assert covariance["shrinkage_intensity"] is not None
        codes = [warning["code"] for warning in response.json()["warnings"]]
        assert "PORTFOLIO_SHRINKAGE_APPLIED" in codes


class TestWhatTheOptimiserRefuses:
    async def test_a_return_seeking_objective_with_no_forecast_is_refused(self, client, loaded):
        """Rather than quietly substituting sample means, which the optimiser
        would then maximise the error of."""
        header, instruments = loaded
        response = await optimise(client, header, instruments, objective="MAXIMUM_SHARPE")

        assert response.status_code == 422
        assert response.json()["code"] == "OPTIMISATION_REFUSED"
        assert "will not estimate a forecast you did not ask for" in response.json()["detail"]

    async def test_historical_means_can_be_asked_for_and_are_warned_about(self, client, loaded):
        header, instruments = loaded
        response = await optimise(
            client,
            header,
            instruments,
            objective="MAXIMUM_SHARPE",
            use_historical_means=True,
        )
        assert response.status_code == 200, response.text
        assert response.json()["results"]["return_source"] == "HISTORICAL_MEAN"
        codes = [warning["code"] for warning in response.json()["warnings"]]
        assert "PORTFOLIO_HISTORICAL_MEANS_USED" in codes

    async def test_a_supplied_forecast_is_used_and_named(self, client, loaded):
        header, instruments = loaded
        response = await optimise(
            client,
            header,
            instruments,
            objective="MAXIMUM_SHARPE",
            expected_returns=[0.05, 0.08, 0.12],
        )
        assert response.status_code == 200, response.text
        assert response.json()["results"]["return_source"] == "SUPPLIED"
        codes = [warning["code"] for warning in response.json()["warnings"]]
        assert "PORTFOLIO_HISTORICAL_MEANS_USED" not in codes

    async def test_mean_variance_without_a_risk_aversion_is_refused(self, client, loaded):
        """It is a statement about a person's tolerance rather than a property
        of the market, and the platform will not choose one."""
        header, instruments = loaded
        response = await optimise(
            client,
            header,
            instruments,
            objective="MEAN_VARIANCE",
            expected_returns=[0.05, 0.08, 0.12],
        )
        assert response.status_code == 422
        assert "risk aversion" in response.json()["detail"]

    async def test_black_litterman_without_tau_is_refused(self, client, loaded):
        header, instruments = loaded
        response = await optimise(
            client,
            header,
            instruments,
            objective="MAXIMUM_SHARPE",
            prior_weights=[0.5, 0.3, 0.2],
            risk_aversion=2.5,
        )
        assert response.status_code == 422
        assert "tau" in response.json()["detail"]

    async def test_impossible_constraints_name_the_contradiction(self, client, loaded):
        header, instruments = loaded
        response = await optimise(
            client,
            header,
            instruments,
            constraints={"budget": 1.0, "long_only": True, "minimum_weight": 0.5},
        )
        assert response.status_code == 422
        assert "sum to" in response.json()["detail"]


class TestConstraintsBind:
    async def test_a_maximum_weight_is_respected_and_reported(self, client, loaded):
        header, instruments = loaded
        response = await optimise(
            client,
            header,
            instruments,
            constraints={"budget": 1.0, "long_only": True, "maximum_weight": 0.4},
        )
        results = response.json()["results"]
        assert max(item["weight"] for item in results["holdings"]) <= 0.4 + 1e-6
        assert results["solver"]["binding_constraints"]

    async def test_a_group_limit_caps_a_pair(self, client, loaded):
        header, instruments = loaded
        response = await optimise(
            client,
            header,
            instruments,
            constraints={
                "budget": 1.0,
                "long_only": True,
                "groups": [{"name": "pair", "indices": [0, 1], "maximum": 0.5}],
            },
        )
        holdings = response.json()["results"]["holdings"]
        assert holdings[0]["weight"] + holdings[1]["weight"] <= 0.5 + 1e-6

    async def test_a_turnover_limit_holds_the_portfolio_near_where_it_was(self, client, loaded):
        header, instruments = loaded
        current = [1 / 3, 1 / 3, 1 / 3]
        response = await optimise(
            client,
            header,
            instruments,
            constraints={
                "budget": 1.0,
                "long_only": True,
                "maximum_turnover": 0.1,
                "current_weights": current,
            },
        )
        holdings = response.json()["results"]["holdings"]
        moved = sum(
            abs(item["weight"] - start) for item, start in zip(holdings, current, strict=True)
        )
        assert moved <= 0.1 + 1e-4


class TestBlackLittermanThroughTheApi:
    async def test_a_prior_with_no_views_gives_equilibrium_returns(self, client, loaded):
        header, instruments = loaded
        response = await optimise(
            client,
            header,
            instruments,
            objective="MAXIMUM_SHARPE",
            prior_weights=[0.5, 0.3, 0.2],
            risk_aversion=2.5,
            tau=0.05,
        )
        assert response.status_code == 200, response.text
        results = response.json()["results"]
        assert results["return_source"] == "EQUILIBRIUM"
        assert results["black_litterman"]["shift"] == pytest.approx([0.0, 0.0, 0.0], abs=1e-12)

    async def test_a_view_moves_the_portfolio_towards_it(self, client, loaded):
        header, instruments = loaded
        base = await optimise(
            client,
            header,
            instruments,
            objective="MAXIMUM_SHARPE",
            prior_weights=[1 / 3, 1 / 3, 1 / 3],
            risk_aversion=2.5,
            tau=0.05,
        )
        with_view = await optimise(
            client,
            header,
            instruments,
            objective="MAXIMUM_SHARPE",
            prior_weights=[1 / 3, 1 / 3, 1 / 3],
            risk_aversion=2.5,
            tau=0.05,
            views=[
                {
                    "weights": {str(instruments[2].id): 1.0},
                    "expected_return": 0.30,
                    "uncertainty": 0.02,
                    "description": "GAMMA will do well",
                }
            ],
        )
        assert with_view.status_code == 200, with_view.text
        assert with_view.json()["results"]["return_source"] == "BLACK_LITTERMAN"

        before = base.json()["results"]["holdings"][2]["weight"]
        after = with_view.json()["results"]["holdings"][2]["weight"]
        assert after > before


class TestOwnership:
    async def test_the_endpoint_requires_a_signed_in_caller(self, client):
        response = await client.post(
            "/portfolio-optimisation/target",
            json={"instrument_ids": [], "objective": "MINIMUM_VARIANCE"},
        )
        assert response.status_code == 401

    async def test_instruments_with_no_history_are_reported_not_silently_dropped(
        self, client, loaded, db_session
    ):
        """A weight of zero and an absent asset are different things."""
        header, instruments = loaded
        lonely = make_instrument(
            asset_class=AssetClass.EQUITY, exchange="NSE", symbol="NOHIST", currency="INR"
        )
        await InstrumentService(db_session).upsert(lonely)
        await db_session.commit()

        response = await optimise(client, header, [*instruments, lonely])
        assert response.status_code == 200, response.text
        assert len(response.json()["results"]["holdings"]) == 3
        codes = [warning["code"] for warning in response.json()["warnings"]]
        assert "PORTFOLIO_INSTRUMENTS_DROPPED" in codes

    async def test_a_group_limit_is_not_reindexed_around_a_dropped_instrument(
        self, client, loaded, db_session
    ):
        """Silently shifting an index would cap the wrong pair of instruments."""
        header, instruments = loaded
        lonely = make_instrument(
            asset_class=AssetClass.EQUITY, exchange="NSE", symbol="NOHIST2", currency="INR"
        )
        await InstrumentService(db_session).upsert(lonely)
        await db_session.commit()

        response = await optimise(
            client,
            header,
            [lonely, *instruments],
            constraints={
                "budget": 1.0,
                "long_only": True,
                "groups": [{"name": "pair", "indices": [1, 2], "maximum": 0.5}],
            },
        )
        assert response.status_code == 422, response.text
        body = response.json()
        assert body["code"] == "INVALID_CONSTRAINTS"
        assert "overlapping history" in body["detail"]
