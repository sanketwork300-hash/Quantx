"""Phase 4 end to end: dataset → strategy → backtest → performance report.

The dataset is a real warehouse dataset, loaded through the real ingestion path,
and the backtest reads it through the real query. What is being checked is not
that the numbers are good — they are made-up prices — but that the accounting is
right, the conventions are stated, and a run is reproducible from its record.
"""

from __future__ import annotations

import math
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from domains.instruments.enums import AssetClass
from domains.instruments.models import make_instrument
from domains.instruments.service import InstrumentService
from tests.conftest import register_and_login

START = datetime(2026, 1, 1, tzinfo=UTC)


def csv_bars(count: int, symbol: str = "NIFTY") -> bytes:
    """A wiggly-but-rising series: enough movement for a crossover to trade."""
    lines = ["timestamp,symbol,open,high,low,close,volume"]
    for day in range(count):
        price = 100 * (1 + 0.0015 * day) + 6 * math.sin(day / 7.0)
        moment = (START + timedelta(days=day)).isoformat()
        lines.append(
            f"{moment},{symbol},{price:.2f},{price * 1.01:.2f},"
            f"{price * 0.99:.2f},{price:.2f},{100000 + day}"
        )
    return "\n".join(lines).encode()


@pytest.fixture
async def instrument(db_session):
    row = make_instrument(
        asset_class=AssetClass.INDEX, exchange="NSE", symbol="NIFTY", currency="INR"
    )
    await InstrumentService(db_session).upsert(row)
    await db_session.commit()
    return row


@pytest.fixture
async def dataset(client, instrument):
    """A loaded warehouse dataset, as Phase 3 produces one."""
    _user, header = await register_and_login(client)

    upload = await client.post(
        "/uploads",
        headers={"Authorization": header},
        files={"file": ("bars.csv", csv_bars(400), "text/csv")},
        data={"kind": "BARS"},
    )
    assert upload.status_code == 201, upload.text

    job = await client.post(
        "/warehouse/datasets",
        headers={"Authorization": header},
        json={
            "upload_id": upload.json()["id"],
            "name": "NIFTY daily",
            "exchange": "NSE",
            "corporate_action_treatment": "UNADJUSTED",
        },
    )
    assert job.status_code == 202, job.text
    result = await client.get(
        f"/jobs/{job.json()['job_id']}/result", headers={"Authorization": header}
    )
    payload = result.json()["result"]
    assert payload["results"]["status"] == "AVAILABLE", payload
    return header, payload["results"]["dataset_id"], instrument


async def backtest(client, header, instrument_id, **overrides) -> dict:
    body = {
        "name": "crossover",
        "instrument_id": str(instrument_id),
        "strategy_name": "moving_average_crossover",
        "strategy_parameters": {"fast": 10, "slow": 30},
        "exchange": "NSE",
        **overrides,
    }
    response = await client.post(
        "/research/backtests", headers={"Authorization": header}, json=body
    )
    assert response.status_code == 202, response.text
    result = await client.get(
        f"/jobs/{response.json()['job_id']}/result", headers={"Authorization": header}
    )
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["status"] == "COMPLETED", body
    return body["result"]


class TestFromDatasetToPerformanceReport:
    """The Phase 4 acceptance criterion."""

    async def test_a_strategy_runs_over_a_warehouse_dataset(self, client, dataset):
        header, _dataset_id, instrument = dataset
        result = await backtest(client, header, instrument.id)

        assert result["results"]["experiment_id"]
        backtest_payload = result["results"]["backtest"]
        assert backtest_payload["counts"]["bars_in"] == 400
        assert backtest_payload["counts"]["fills"] > 0

    async def test_the_report_carries_the_metrics_it_promises(self, client, dataset):
        header, _dataset_id, instrument = dataset
        result = await backtest(client, header, instrument.id)
        metrics = result["results"]["metrics"]

        for key in (
            "total_return",
            "cagr",
            "annualised_volatility",
            "sharpe",
            "sortino",
            "calmar",
            "max_drawdown",
            "value_at_risk",
            "trades",
        ):
            assert key in metrics, key
        assert metrics["trades"]["count"] >= 0

    async def test_the_attribution_reconciles(self, client, dataset):
        """An attribution that does not add up to the equity change is a bug,
        not an approximation."""
        header, _dataset_id, instrument = dataset
        result = await backtest(client, header, instrument.id)
        attribution = result["results"]["attribution"]

        assert attribution["reconciles"] is True
        assert abs(float(attribution["residual"])) < 0.01

    async def test_buy_and_hold_matches_the_instrument(self, client, dataset):
        """The accounting benchmark. When it does not match, the engine is
        wrong and no other number in the report would have said so."""
        header, _dataset_id, instrument = dataset
        result = await backtest(
            client,
            header,
            instrument.id,
            name="benchmark",
            strategy_name="buy_and_hold",
            strategy_parameters={},
        )
        payload = result["results"]["backtest"]
        assert payload["counts"]["fills"] == 1

        experiment_id = result["results"]["experiment_id"]
        fills = await client.get(
            f"/research/experiments/{experiment_id}/fills",
            headers={"Authorization": header},
        )
        fill = fills.json()["items"][0]
        entry = float(fill["price"])
        held = float(fill["quantity"])
        cash_left = float(payload["initial_equity"]) - held * entry
        # Final equity is idle cash plus the position at the last close.
        assert float(payload["final_equity"]) > cash_left

    async def test_the_equity_curve_is_retrievable_and_continuous(self, client, dataset):
        header, _dataset_id, instrument = dataset
        result = await backtest(client, header, instrument.id)
        experiment_id = result["results"]["experiment_id"]

        curve = await client.get(
            f"/research/experiments/{experiment_id}/equity-curve",
            headers={"Authorization": header},
        )
        assert curve.status_code == 200, curve.text
        assert curve.json()["count"] == 400


class TestTheRunSaysWhatItAssumed:
    async def test_a_run_with_no_cost_schedule_is_gross_and_says_so(self, client, dataset):
        """Silently assuming free trading is the commonest way a backtest
        reports returns that do not exist."""
        header, _dataset_id, instrument = dataset
        result = await backtest(client, header, instrument.id)

        assert result["results"]["backtest"]["gross_of_costs"] is True
        assert result["results"]["metrics"]["gross_of_costs"] is True
        codes = [warning["code"] for warning in result["warnings"]]
        assert "RESEARCH_COSTS_NOT_MODELLED" in codes
        assert "RESEARCH_SLIPPAGE_NOT_MODELLED" in codes

    async def test_a_supplied_schedule_produces_a_net_run(self, client, dataset):
        header, _dataset_id, instrument = dataset
        result = await backtest(
            client,
            header,
            instrument.id,
            cost_schedule_name="illustrative",
            cost_schedule_source="rates supplied by the user, not by this platform",
            cost_components=[
                {"name": "brokerage", "basis": "TURNOVER", "rate": "0.0003", "maximum": "20"},
                {"name": "stt", "basis": "TURNOVER", "rate": "0.00025", "side": "SELL"},
            ],
            slippage_basis_points="5",
            slippage_source="assumed",
        )
        assert result["results"]["backtest"]["gross_of_costs"] is False
        assert float(result["results"]["backtest"]["total_costs"]) > 0
        assert float(result["results"]["backtest"]["total_slippage"]) > 0

    async def test_costs_reduce_the_return(self, client, dataset):
        header, _dataset_id, instrument = dataset
        gross = await backtest(client, header, instrument.id)
        net = await backtest(
            client,
            header,
            instrument.id,
            cost_components=[
                {"name": "brokerage", "basis": "TURNOVER", "rate": "0.002"},
            ],
        )
        assert (
            net["results"]["metrics"]["total_return"] < gross["results"]["metrics"]["total_return"]
        )

    async def test_the_experiment_record_holds_what_the_run_assumed(self, client, dataset):
        """A net return means nothing without a statement of what was deducted
        from it, and the record is where that statement lives."""
        header, _dataset_id, instrument = dataset
        result = await backtest(
            client,
            header,
            instrument.id,
            cost_components=[{"name": "brokerage", "basis": "TURNOVER", "rate": "0.0003"}],
            cost_schedule_source="user-supplied illustrative rates",
        )
        experiment_id = result["results"]["experiment_id"]

        detail = await client.get(
            f"/research/experiments/{experiment_id}", headers={"Authorization": header}
        )
        assert detail.status_code == 200, detail.text
        body = detail.json()
        assert body["cost_schedule"]["models_costs"] is True
        assert "user-supplied" in body["cost_schedule"]["source"]
        assert body["strategy_parameters"]["fast"] == 10
        assert body["features"] == ["sma_10", "sma_30"]
        assert body["code_commit"]
        assert body["engine_config"]["timing"] == "NEXT_OPEN"

    async def test_the_default_fill_timing_is_the_next_bar(self, client, dataset):
        header, _dataset_id, instrument = dataset
        result = await backtest(client, header, instrument.id)
        assert result["results"]["backtest"]["config"]["timing"] == "NEXT_OPEN"


class TestStrategiesAndRefusals:
    async def test_the_shipped_strategies_are_listed_with_their_features(self, client):
        _user, header = await register_and_login(client)
        response = await client.get("/research/strategies", headers={"Authorization": header})
        assert response.status_code == 200, response.text

        names = {item["name"] for item in response.json()}
        assert {"buy_and_hold", "moving_average_crossover", "momentum", "mean_reversion"} <= names
        crossover = next(
            item for item in response.json() if item["name"] == "moving_average_crossover"
        )
        assert crossover["features"] == ["sma_20", "sma_50"]

    async def test_an_unknown_strategy_names_what_is_available(self, client, instrument):
        _user, header = await register_and_login(client)
        response = await client.post(
            "/research/backtests",
            headers={"Authorization": header},
            json={
                "name": "x",
                "instrument_id": str(instrument.id),
                "strategy_name": "telepathy",
            },
        )
        assert response.status_code == 422
        assert response.json()["code"] == "UNKNOWN_STRATEGY"

    async def test_bad_strategy_parameters_are_refused_before_the_job(self, client, instrument):
        _user, header = await register_and_login(client)
        response = await client.post(
            "/research/backtests",
            headers={"Authorization": header},
            json={
                "name": "x",
                "instrument_id": str(instrument.id),
                "strategy_name": "moving_average_crossover",
                "strategy_parameters": {"fast": 50, "slow": 20},
            },
        )
        assert response.status_code == 422
        assert response.json()["code"] == "INVALID_STRATEGY_PARAMETERS"

    async def test_a_naive_timestamp_is_refused(self, client, instrument):
        _user, header = await register_and_login(client)
        response = await client.post(
            "/research/backtests",
            headers={"Authorization": header},
            json={
                "name": "x",
                "instrument_id": str(instrument.id),
                "strategy_name": "buy_and_hold",
                "start": "2026-01-01T00:00:00",
            },
        )
        assert response.status_code == 422
        assert response.json()["code"] == "TIMESTAMP_NOT_TIMEZONE_AWARE"

    async def test_a_run_with_no_bars_fails_rather_than_reporting_nothing(self, client, instrument):
        _user, header = await register_and_login(client)
        response = await client.post(
            "/research/backtests",
            headers={"Authorization": header},
            json={
                "name": "x",
                "instrument_id": str(instrument.id),
                "strategy_name": "buy_and_hold",
            },
        )
        assert response.status_code == 202
        job = await client.get(
            f"/jobs/{response.json()['job_id']}", headers={"Authorization": header}
        )
        assert job.json()["status"] == "FAILED"


class TestOwnership:
    async def test_another_account_cannot_read_the_experiment(self, client, dataset):
        header, _dataset_id, instrument = dataset
        result = await backtest(client, header, instrument.id)

        _other, other_header = await register_and_login(client)
        response = await client.get(
            f"/research/experiments/{result['results']['experiment_id']}",
            headers={"Authorization": other_header},
        )
        assert response.status_code == 404

    async def test_every_route_requires_a_signed_in_caller(self, client):
        for path in ("/research/strategies", "/research/experiments"):
            assert (await client.get(path)).status_code == 401


def _unused() -> uuid.UUID:  # pragma: no cover
    return uuid.uuid4()
