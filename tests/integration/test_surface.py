"""Phase 2 end to end: calibrate a surface, read it back, price off it.

Carries the Phase 2 acceptance criteria from docs/backlog.md.
"""

from __future__ import annotations

import pytest

from tests.integration.test_derivatives import SYNTHETIC_RATE, ingest_clean_chain


async def analyse(client, header, snapshot_id) -> str:
    accepted = await client.post(
        f"/derivatives/chains/{snapshot_id}/analyze",
        headers={"Authorization": header},
        json={
            "risk_free_rate": SYNTHETIC_RATE,
            "dividend_yield": 0.0,
            "settlement_time_utc": "10:00:00",
        },
    )
    assert accepted.status_code == 202, accepted.text
    result = await client.get(
        f"/jobs/{accepted.json()['job_id']}/result", headers={"Authorization": header}
    )
    return result.json()["result"]["analysis_id"]


async def calibrate(client, header, analysis_id) -> dict:
    accepted = await client.post(
        f"/derivatives/analyses/{analysis_id}/calibrate",
        headers={"Authorization": header},
        json={"seed": 20260924, "use_weights": True},
    )
    assert accepted.status_code == 202, accepted.text
    job_id = accepted.json()["job_id"]

    job = await client.get(f"/jobs/{job_id}", headers={"Authorization": header})
    assert job.json()["status"] == "COMPLETED", job.json()
    payload = await client.get(f"/jobs/{job_id}/result", headers={"Authorization": header})
    return payload.json()["result"]


@pytest.fixture
async def calibrated(client, auth_header, clean_chain_csv):
    snapshot_id = await ingest_clean_chain(client, auth_header, clean_chain_csv)
    analysis_id = await analyse(client, auth_header, snapshot_id)
    body = await calibrate(client, auth_header, analysis_id)
    return {
        "snapshot_id": snapshot_id,
        "analysis_id": analysis_id,
        "body": body,
        "surface_row_id": body["results"]["surface_row_id"],
    }


class TestCalibration:
    async def test_every_expiry_is_fitted(self, calibrated):
        results = calibrated["body"]["results"]
        assert results["counts"]["slices"] == 2
        assert results["counts"]["fitted"] == 2
        assert results["model"] == "RAW_SVI"

    async def test_calibration_metrics_are_recorded(self, calibrated):
        for slice_ in calibrated["body"]["results"]["slices"]:
            metrics = slice_["calibration"]
            assert metrics["status"] == "CONVERGED"
            assert metrics["n_observations"] >= 5
            assert metrics["rmse_vol_points"] < 0.5
            assert metrics["optimizer"] == "SLSQP"
            assert metrics["starts_feasible"] >= 1
            assert metrics["starts_attempted"] >= metrics["starts_feasible"]

    async def test_fitted_parameters_satisfy_the_no_arbitrage_constraints(self, calibrated):
        """The Phase 2 acceptance criterion."""
        for slice_ in calibrated["body"]["results"]["slices"]:
            params = slice_["parameters"]
            metrics = slice_["calibration"]

            assert params["b"] >= 0
            assert abs(params["rho"]) < 1
            assert params["sigma"] > 0
            minimum = params["a"] + params["b"] * params["sigma"] * (1 - params["rho"] ** 2) ** 0.5
            assert minimum >= -1e-12, "minimum total variance must be non-negative"

            assert metrics["wing_slope"] <= 2.0 + 1e-9, "Lee's moment formula bound"
            assert metrics["min_durrleman_g"] > 0, "no negative implied density"
            assert metrics["constraints_satisfied"] is True

    async def test_the_fitted_range_is_recorded(self, calibrated):
        for slice_ in calibrated["body"]["results"]["slices"]:
            assert slice_["k_min"] < slice_["k_max"]

    async def test_a_narrow_chain_warns_about_identifiability(self, calibrated):
        """The committed fixture spans about 0.1 in log-moneyness, where SVI's
        five parameters are not identifiable."""
        codes = {warning["code"] for warning in calibrated["body"]["warnings"]}
        assert "SURFACE_NARROW_STRIKE_RANGE" in codes

    async def test_provenance_names_the_model_and_the_seed(self, calibrated):
        provenance = calibrated["body"]["provenance"]
        assert provenance["model_versions"]["surface"].startswith("svi-raw@")
        assert provenance["model_versions"]["arbitrage"].startswith("arbitrage-validator@")
        assert provenance["surface_id"].startswith("surface:")
        assert provenance["parameters"]["calibration"]["seed"] == 20260924
        assert provenance["parameters"]["calibration"]["use_weights"] is True

    async def test_calibration_is_deterministic(self, client, auth_header, calibrated):
        """The same analysis and seed must refit identically, or a stored
        surface could not be reproduced."""
        second = await calibrate(client, auth_header, calibrated["analysis_id"])
        assert second["results"]["surface_id"] == calibrated["body"]["results"]["surface_id"]


class TestSurfaceRetrieval:
    async def test_surface_is_rebuilt_from_persisted_parameters(
        self, client, auth_header, calibrated
    ):
        response = await client.get(
            f"/derivatives/surfaces/{calibrated['surface_row_id']}",
            headers={"Authorization": auth_header},
        )
        assert response.status_code == 200
        results = response.json()["results"]
        assert results["surface_id"] == calibrated["body"]["results"]["surface_id"]

        stored = {s["expiry"]: s["parameters"] for s in results["slices"]}
        original = {s["expiry"]: s["parameters"] for s in calibrated["body"]["results"]["slices"]}
        assert stored == original, "parameters must survive the round trip exactly"

    async def test_latest_surface_by_underlying(self, client, auth_header, calibrated):
        chain = await client.get(
            f"/market/chains/{calibrated['snapshot_id']}",
            headers={"Authorization": auth_header},
        )
        underlying_id = chain.json()["results"]["underlying_id"]

        response = await client.get(
            f"/derivatives/surfaces/latest?underlying_id={underlying_id}",
            headers={"Authorization": auth_header},
        )
        assert response.status_code == 200
        assert (
            response.json()["results"]["surface_id"]
            == (calibrated["body"]["results"]["surface_id"])
        )

    async def test_surfaces_are_listed(self, client, auth_header, calibrated):
        response = await client.get("/derivatives/surfaces", headers={"Authorization": auth_header})
        items = response.json()
        assert len(items) == 1
        assert items[0]["slices_fitted"] == 2
        assert items[0]["model_version"].startswith("svi-raw@")


class TestReferenceValues:
    async def test_reference_iv_and_price_at_a_fitted_expiry(self, client, auth_header, calibrated):
        slice_ = calibrated["body"]["results"]["slices"][0]
        response = await client.post(
            f"/derivatives/surfaces/{calibrated['surface_row_id']}/reference",
            headers={"Authorization": auth_header},
            json={
                "requests": [
                    {
                        "strike": "24000",
                        "expiry": slice_["expiry"],
                        "option_type": "CALL",
                    }
                ]
            },
        )
        assert response.status_code == 200
        point = response.json()["results"]["points"][0]
        assert point["method"] == "EXACT_SLICE"
        assert 0.0 < point["reference_iv"] < 1.0
        assert point["reference_price"] > 0
        assert point["flags"] == []
        assert point["calibration_rmse_vol_points"] is not None

    async def test_reference_values_are_reproducible_from_the_stored_surface(
        self, client, auth_header, calibrated
    ):
        """Phase 2 acceptance: no re-fitting on read."""
        slice_ = calibrated["body"]["results"]["slices"][1]
        body = {
            "requests": [
                {"strike": "23000", "expiry": slice_["expiry"], "option_type": "PUT"},
                {"strike": "25000", "expiry": slice_["expiry"], "option_type": "CALL"},
            ]
        }
        first = await client.post(
            f"/derivatives/surfaces/{calibrated['surface_row_id']}/reference",
            headers={"Authorization": auth_header},
            json=body,
        )
        second = await client.post(
            f"/derivatives/surfaces/{calibrated['surface_row_id']}/reference",
            headers={"Authorization": auth_header},
            json=body,
        )
        assert first.json()["results"] == second.json()["results"]

    async def test_the_reference_tracks_the_market_iv_where_the_fit_is_good(
        self, client, auth_header, calibrated
    ):
        """The surface must actually describe the quotes it was fitted to."""
        analysis = await client.get(
            f"/derivatives/analyses/{calibrated['analysis_id']}",
            headers={"Authorization": auth_header},
        )
        slice_ = analysis.json()["results"]["slices"][0]
        used = [p for p in slice_["points"] if p["used_for_smile"]][:6]

        response = await client.post(
            f"/derivatives/surfaces/{calibrated['surface_row_id']}/reference",
            headers={"Authorization": auth_header},
            json={"requests": [{"strike": p["strike"], "expiry": p["expiry"]} for p in used]},
        )
        points = response.json()["results"]["points"]
        for market, reference in zip(used, points, strict=True):
            assert reference["reference_iv"] == pytest.approx(market["market_iv"], abs=5e-4)

    async def test_an_extrapolated_strike_is_flagged_and_warned_about(
        self, client, auth_header, calibrated
    ):
        slice_ = calibrated["body"]["results"]["slices"][0]
        response = await client.post(
            f"/derivatives/surfaces/{calibrated['surface_row_id']}/reference",
            headers={"Authorization": auth_header},
            json={
                "requests": [{"strike": "40000", "expiry": slice_["expiry"], "option_type": "CALL"}]
            },
        )
        body = response.json()
        point = body["results"]["points"][0]
        assert "EXTRAPOLATED_STRIKE" in point["flags"]
        assert point["reference_iv"] is not None, "flagged, not withheld"
        assert any(w["code"] == "REFERENCE_EXTRAPOLATED" for w in body["warnings"])

    async def test_an_unfitted_expiry_interpolates_and_says_so(
        self, client, auth_header, calibrated
    ):
        expiries = sorted(s["expiry"] for s in calibrated["body"]["results"]["slices"])
        between = "2026-11-20"
        assert expiries[0] < between < expiries[1]

        response = await client.post(
            f"/derivatives/surfaces/{calibrated['surface_row_id']}/reference",
            headers={"Authorization": auth_header},
            json={"requests": [{"strike": "24200", "expiry": between}]},
        )
        point = response.json()["results"]["points"][0]
        assert point["method"] == "INTERPOLATED_MATURITY"
        assert point["reference_iv"] is not None

    async def test_reference_output_is_never_called_a_fair_value(
        self, client, auth_header, calibrated
    ):
        """A contract guarantee: these are reference values, not fair values."""
        slice_ = calibrated["body"]["results"]["slices"][0]
        response = await client.post(
            f"/derivatives/surfaces/{calibrated['surface_row_id']}/reference",
            headers={"Authorization": auth_header},
            json={"requests": [{"strike": "24000", "expiry": slice_["expiry"]}]},
        )
        rendered = response.text.lower()
        for forbidden in ("fair_value", "fair value", "underpriced", "signal"):
            assert forbidden not in rendered


class TestArbitrageReporting:
    async def test_both_scopes_are_reported_separately(self, client, auth_header, calibrated):
        """Collapsing them would let a smooth fit hide a broken market."""
        response = await client.get(
            f"/derivatives/arbitrage/{calibrated['analysis_id']}",
            headers={"Authorization": auth_header},
        )
        assert response.status_code == 200
        results = response.json()["results"]
        assert results["raw_market"] is not None
        assert results["fitted_surface"] is not None
        assert results["raw_market"]["scope"] == "RAW_MARKET"
        assert results["fitted_surface"]["scope"] == "FITTED_SURFACE"

    async def test_the_checks_that_ran_are_named(self, client, auth_header, calibrated):
        results = (
            await client.get(
                f"/derivatives/arbitrage/{calibrated['analysis_id']}",
                headers={"Authorization": auth_header},
            )
        ).json()["results"]
        assert {"PRICE_BOUND", "PUT_CALL_PARITY", "BUTTERFLY", "CALENDAR"} <= set(
            results["raw_market"]["checks_run"]
        )
        assert {"DURRLEMAN", "WING_SLOPE", "CALENDAR"} <= set(
            results["fitted_surface"]["checks_run"]
        )

    async def test_a_clean_synthetic_chain_produces_no_violations(
        self, client, auth_header, calibrated
    ):
        results = (
            await client.get(
                f"/derivatives/arbitrage/{calibrated['analysis_id']}",
                headers={"Authorization": auth_header},
            )
        ).json()["results"]
        assert results["raw_market"]["violations_total"] == 0
        assert results["fitted_surface"]["violations_total"] == 0

    async def test_severity_filtering(self, client, auth_header, calibrated):
        response = await client.get(
            f"/derivatives/arbitrage/{calibrated['analysis_id']}?min_severity=ERROR",
            headers={"Authorization": auth_header},
        )
        assert response.status_code == 200


class TestMarketState:
    async def test_state_composes_quotes_curve_and_surface(self, client, auth_header, calibrated):
        chain = await client.get(
            f"/market/chains/{calibrated['snapshot_id']}",
            headers={"Authorization": auth_header},
        )
        underlying_id = chain.json()["results"]["underlying_id"]

        response = await client.get(
            f"/market/state?underlying_id={underlying_id}&risk_free_rate=0.065",
            headers={"Authorization": auth_header},
        )
        assert response.status_code == 200
        results = response.json()["results"]

        assert results["state_id"].startswith("state:")
        assert results["counts"]["quotes"] == 60
        assert results["counts"]["yield_curves"] == 1
        assert results["counts"]["volatility_surfaces"] == 1
        assert (
            results["volatility_surfaces"][0]["surface_id"]
            == (calibrated["body"]["results"]["surface_id"])
        )

    async def test_the_state_id_is_stable_across_calls(self, client, auth_header, calibrated):
        """Two calculations reporting the same id provably saw the same inputs."""
        chain = await client.get(
            f"/market/chains/{calibrated['snapshot_id']}",
            headers={"Authorization": auth_header},
        )
        underlying_id = chain.json()["results"]["underlying_id"]
        path = f"/market/state?underlying_id={underlying_id}&risk_free_rate=0.065"

        first = await client.get(path, headers={"Authorization": auth_header})
        second = await client.get(path, headers={"Authorization": auth_header})
        assert first.json()["results"]["state_id"] == second.json()["results"]["state_id"]

    async def test_a_missing_curve_is_warned_about(self, client, auth_header, calibrated):
        chain = await client.get(
            f"/market/chains/{calibrated['snapshot_id']}",
            headers={"Authorization": auth_header},
        )
        underlying_id = chain.json()["results"]["underlying_id"]
        body = (
            await client.get(
                f"/market/state?underlying_id={underlying_id}",
                headers={"Authorization": auth_header},
            )
        ).json()
        assert any(w["code"] == "MARKET_STATE_NO_CURVE" for w in body["warnings"])

    async def test_provenance_carries_the_state_id(self, client, auth_header, calibrated):
        chain = await client.get(
            f"/market/chains/{calibrated['snapshot_id']}",
            headers={"Authorization": auth_header},
        )
        underlying_id = chain.json()["results"]["underlying_id"]
        body = (
            await client.get(
                f"/market/state?underlying_id={underlying_id}&risk_free_rate=0.065",
                headers={"Authorization": auth_header},
            )
        ).json()
        assert body["provenance"]["market_state_id"] == body["results"]["state_id"]


class TestOwnership:
    async def test_another_users_surface_is_not_found(self, client, calibrated):
        from tests.conftest import register_and_login

        _other, other_header = await register_and_login(client, "s-other@example.com")
        for path in (
            f"/derivatives/surfaces/{calibrated['surface_row_id']}",
            f"/derivatives/arbitrage/{calibrated['analysis_id']}",
        ):
            response = await client.get(path, headers={"Authorization": other_header})
            assert response.status_code == 404

    async def test_cannot_calibrate_another_users_analysis(self, client, calibrated):
        from tests.conftest import register_and_login

        _other, other_header = await register_and_login(client, "s-other2@example.com")
        response = await client.post(
            f"/derivatives/analyses/{calibrated['analysis_id']}/calibrate",
            headers={"Authorization": other_header},
            json={},
        )
        assert response.status_code == 404
