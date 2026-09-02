"""Phase 1 end to end: upload a chain, analyse it, read the smile.

Carries the Phase 1 acceptance criteria from docs/backlog.md.
"""

from __future__ import annotations

import math

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
#: The rate the synthetic market in tests/data was generated at.
SYNTHETIC_RATE = 0.065

__all__ = ["AS_OF", "MAPPING", "SYNTHETIC_RATE", "ingest_clean_chain"]


async def ingest_clean_chain(client, header, csv_bytes) -> str:
    upload = await client.post(
        "/uploads",
        headers={"Authorization": header},
        files={"file": ("chain.csv", csv_bytes, "text/csv")},
    )
    assert upload.status_code == 201, upload.text
    accepted = await client.post(
        f"/uploads/{upload.json()['id']}/ingest",
        headers={"Authorization": header},
        json={
            "underlying": {"symbol": "NIFTY", "exchange": "SYNTH", "currency": "INR"},
            "as_of_timestamp": AS_OF,
            "column_mapping": MAPPING,
            "risk_free_rate": SYNTHETIC_RATE,
            "dividend_yield": 0.0,
            "contract": {
                "multiplier": "75",
                "tick_size": "0.05",
                "lot_size": "75",
                "expiry_time_utc": "10:00:00",
            },
        },
    )
    assert accepted.status_code == 202, accepted.text
    job = await client.get(f"/jobs/{accepted.json()['job_id']}", headers={"Authorization": header})
    assert job.json()["status"] == "COMPLETED"
    result = await client.get(
        f"/jobs/{accepted.json()['job_id']}/result", headers={"Authorization": header}
    )
    return result.json()["result"]["results"]["snapshot_id"]


@pytest.fixture
async def analysis(client, auth_header, clean_chain_csv):
    snapshot_id = await ingest_clean_chain(client, auth_header, clean_chain_csv)
    accepted = await client.post(
        f"/derivatives/chains/{snapshot_id}/analyze",
        headers={"Authorization": auth_header},
        json={
            "risk_free_rate": SYNTHETIC_RATE,
            "dividend_yield": 0.0,
            "settlement_time_utc": "10:00:00",
        },
    )
    assert accepted.status_code == 202, accepted.text
    job_id = accepted.json()["job_id"]

    job = await client.get(f"/jobs/{job_id}", headers={"Authorization": auth_header})
    assert job.json()["status"] == "COMPLETED", job.json()
    payload = await client.get(f"/jobs/{job_id}/result", headers={"Authorization": auth_header})
    body = payload.json()["result"]
    return {"snapshot_id": snapshot_id, "job": body, "analysis_id": body["analysis_id"]}


class TestChainAnalysis:
    async def test_every_kept_quote_is_solved(self, analysis):
        counts = analysis["job"]["results"]["counts"]
        assert counts["quotes"] == 60
        assert counts["solved"] == 60, "a clean synthetic chain must invert completely"
        assert counts["expiries"] == 2

    async def test_provenance_records_the_model_and_the_assumptions(self, analysis):
        provenance = analysis["job"]["provenance"]
        assert provenance["model_versions"]["implied_volatility"].startswith("implied-vol-black76@")
        assert provenance["model_versions"]["forward"].startswith("forward-estimator@")
        assert provenance["yield_curve_id"].startswith("curve:")
        parameters = provenance["parameters"]
        assert parameters["risk_free_rate"] == SYNTHETIC_RATE
        assert parameters["dividend_yield_assumed"] is True
        assert parameters["expiry_policy"]["day_count"] == "ACT/365F"
        assert provenance["numerical_tolerances"]["iv_bracket"] == [1e-8, 5.0]

    async def test_warnings_name_the_assumptions(self, analysis):
        codes = {warning["code"] for warning in analysis["job"]["warnings"]}
        assert "DERIVATIVES_CURVE_ASSUMED" in codes
        assert "DERIVATIVES_SETTLEMENT_TIME_ASSUMED" in codes

    async def test_forward_is_recovered_from_option_prices_alone(self, analysis):
        """Put-call parity gives F and DF with no rate assumption at all, which
        is why it outranks the spot-carry estimate."""
        for slice_ in analysis["job"]["results"]["slices"]:
            selected = slice_["forward"]["selected"]
            assert selected["method"] == "PUT_CALL_PARITY"
            assert selected["confidence"] > 0.8
            assert selected["observations"] >= 10
            assert selected["residual_error"] < 0.1

            tau = slice_["time_to_expiry"]
            expected = 24000.0 * math.exp(SYNTHETIC_RATE * tau)
            assert selected["value"] == pytest.approx(expected, rel=1e-5)
            assert selected["discount_factor"] == pytest.approx(
                math.exp(-SYNTHETIC_RATE * tau), rel=1e-5
            )

    async def test_every_estimate_is_reported_not_only_the_winner(self, analysis):
        for slice_ in analysis["job"]["results"]["slices"]:
            methods = {e["method"] for e in slice_["forward"]["estimates"]}
            assert {"PUT_CALL_PARITY", "SPOT_CARRY"} <= methods
            assert slice_["forward"]["disagreement"] is not None

    async def test_smile_statistics_are_computed(self, analysis):
        for slice_ in analysis["job"]["results"]["slices"]:
            assert 0.0 < slice_["atm_volatility"] < 2.0
            assert slice_["skew"] < 0, "the generated surface has a put-side skew"
            assert slice_["curvature"] is not None
            assert slice_["counts"]["used_for_smile"] > 0

    async def test_total_variance_increases_with_maturity(self, analysis):
        """Calendar consistency of the observed ATM level, recovered end to end."""
        slices = sorted(analysis["job"]["results"]["slices"], key=lambda s: s["time_to_expiry"])
        variances = [s["atm_volatility"] ** 2 * s["time_to_expiry"] for s in slices]
        assert all(
            later > earlier for earlier, later in zip(variances, variances[1:], strict=False)
        ), variances


class TestSmileRetrieval:
    async def test_smile_endpoint_returns_points(self, client, auth_header, analysis):
        response = await client.get(
            f"/derivatives/chains/{analysis['snapshot_id']}/smile",
            headers={"Authorization": auth_header},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "OK"

        slices = body["results"]["slices"]
        assert slices
        points = slices[0]["points"]
        assert points
        assert all(point["used_for_smile"] for point in points)
        for point in points:
            assert point["market_iv"] is not None
            assert point["log_moneyness"] is not None
            assert point["total_variance"] == pytest.approx(
                point["market_iv"] ** 2 * point["time_to_expiry"], rel=1e-9
            )

    async def test_points_carry_their_solver_report(self, client, auth_header, analysis):
        response = await client.get(
            f"/derivatives/analyses/{analysis['analysis_id']}",
            headers={"Authorization": auth_header},
        )
        points = response.json()["results"]["slices"][0]["points"]
        for point in points:
            assert point["converged"] is True
            assert point["solver"] in {"safeguarded-newton", "brent"}
            assert point["iterations"] >= 0
            assert point["vega"] is not None
            assert point["uncertainty"] is not None

    async def test_bid_ask_iv_envelope_is_available(self, client, auth_header, analysis):
        """How much of an apparent deviation is just the spread."""
        response = await client.get(
            f"/derivatives/analyses/{analysis['analysis_id']}",
            headers={"Authorization": auth_header},
        )
        points = response.json()["results"]["slices"][0]["points"]
        with_envelope = [p for p in points if p["iv_envelope_width"] is not None]
        assert with_envelope
        for point in with_envelope:
            assert point["market_iv_bid"] < point["market_iv_ask"]
            assert point["iv_envelope_width"] > 0
            assert point["market_iv_bid"] <= point["market_iv"] <= point["market_iv_ask"]

    async def test_expiry_filter(self, client, auth_header, analysis):
        expiries = [s["expiry"] for s in analysis["job"]["results"]["slices"]]
        response = await client.get(
            f"/derivatives/analyses/{analysis['analysis_id']}?expiry={expiries[0]}",
            headers={"Authorization": auth_header},
        )
        slices = response.json()["results"]["slices"]
        assert len(slices) == 1
        assert slices[0]["expiry"] == expiries[0]

    async def test_one_side_per_strike_carries_the_smile(self, client, auth_header, analysis):
        """Out-of-the-money preferred, deterministic near the money."""
        response = await client.get(
            f"/derivatives/analyses/{analysis['analysis_id']}",
            headers={"Authorization": auth_header},
        )
        for slice_ in response.json()["results"]["slices"]:
            used = [p for p in slice_["points"] if p["used_for_smile"]]
            strikes = [p["strike"] for p in used]
            assert len(strikes) == len(set(strikes)), "one quote per strike"
            for point in used:
                if point["log_moneyness"] > 0.02:
                    assert point["option_type"] == "CALL"
                elif point["log_moneyness"] < -0.02:
                    assert point["option_type"] == "PUT"

    async def test_unused_points_say_why(self, client, auth_header, analysis):
        response = await client.get(
            f"/derivatives/analyses/{analysis['analysis_id']}?used_for_smile_only=false",
            headers={"Authorization": auth_header},
        )
        for slice_ in response.json()["results"]["slices"]:
            for point in slice_["points"]:
                if not point["used_for_smile"]:
                    assert point["smile_exclusion"], point


class TestOwnership:
    async def test_another_users_analysis_is_not_found(self, client, analysis):
        from tests.conftest import register_and_login

        _other, other_header = await register_and_login(client, "d-other@example.com")
        for path in (
            f"/derivatives/analyses/{analysis['analysis_id']}",
            f"/derivatives/chains/{analysis['snapshot_id']}/smile",
        ):
            response = await client.get(path, headers={"Authorization": other_header})
            assert response.status_code == 404

    async def test_cannot_analyse_another_users_chain(self, client, analysis):
        from tests.conftest import register_and_login

        _other, other_header = await register_and_login(client, "d-other2@example.com")
        response = await client.post(
            f"/derivatives/chains/{analysis['snapshot_id']}/analyze",
            headers={"Authorization": other_header},
            json={"settlement_time_utc": "10:00:00"},
        )
        assert response.status_code == 404


class TestDegradedInputs:
    async def test_without_a_settlement_time_nothing_is_solved_and_it_says_so(
        self, client, auth_header, clean_chain_csv
    ):
        """Time to expiry is undefined, so an implied volatility would be a
        fabrication. The result is PARTIAL with a named reason."""
        snapshot_id = await ingest_clean_chain(client, auth_header, clean_chain_csv)
        accepted = await client.post(
            f"/derivatives/chains/{snapshot_id}/analyze",
            headers={"Authorization": auth_header},
            json={"risk_free_rate": SYNTHETIC_RATE},
        )
        job_id = accepted.json()["job_id"]
        body = (
            await client.get(f"/jobs/{job_id}/result", headers={"Authorization": auth_header})
        ).json()["result"]

        assert body["results"]["counts"]["solved"] == 0
        codes = {warning["code"] for warning in body["warnings"]}
        assert "DERIVATIVES_SETTLEMENT_TIME_UNKNOWN" in codes
        assert body["status"] == "PARTIAL"

    async def test_analysis_of_an_unknown_snapshot_is_not_found(self, client, auth_header):
        import uuid

        response = await client.post(
            f"/derivatives/chains/{uuid.uuid4()}/analyze",
            headers={"Authorization": auth_header},
            json={"settlement_time_utc": "10:00:00"},
        )
        assert response.status_code == 404
