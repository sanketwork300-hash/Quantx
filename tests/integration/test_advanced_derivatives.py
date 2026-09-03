"""Phase 9 end to end: global surface, local volatility, density, consensus.

Carries the Phase 9 acceptance criteria from docs/backlog.md. The two numerical
criteria — PDE order of convergence and Heston against QuantLib — live in
``tests/quant_validation``; the third, that the consensus exposes dispersion and
never a single "true" price, is asserted here, on the payload the API actually
returns.
"""

from __future__ import annotations

import json

import pytest

from tests.conftest import register_and_login
from tests.integration.test_derivatives import SYNTHETIC_RATE, ingest_clean_chain
from tests.integration.test_surface import analyse

#: Phrases the product must never emit. Checked against the whole serialised
#: payload, not against a field list, because the failure this guards against is
#: a phrase reaching the user through a field nobody thought to check.
BANNED = (
    "fair value",
    "underpriced",
    "overpriced",
    "arbitrage opportunity",
    "will be liquidated",
    "optimal execution",
    "broker margin",
    "buy signal",
    "sell signal",
)

#: Field *keys* that must not exist anywhere in the payload. A consensus with a
#: ``best_model`` would be the platform choosing between sets of wrong
#: assumptions on the user's behalf.
BANNED_KEYS = ("best_model", "true_price", "fair_value", "recommendation", "signal")


def _keys(payload) -> set[str]:
    found: set[str] = set()
    if isinstance(payload, dict):
        for key, value in payload.items():
            found.add(key)
            found |= _keys(value)
    elif isinstance(payload, list):
        for item in payload:
            found |= _keys(item)
    return found


async def calibrate_global(client, header, analysis_id) -> dict:
    accepted = await client.post(
        f"/derivatives/analyses/{analysis_id}/global-surface",
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
async def global_surface(client, auth_header, clean_chain_csv):
    snapshot_id = await ingest_clean_chain(client, auth_header, clean_chain_csv)
    analysis_id = await analyse(client, auth_header, snapshot_id)
    body = await calibrate_global(client, auth_header, analysis_id)
    return {
        "snapshot_id": snapshot_id,
        "analysis_id": analysis_id,
        "body": body,
        "row_id": body["results"]["global_surface_row_id"],
    }


class TestGlobalSurface:
    async def test_one_parameter_set_covers_every_expiry(self, global_surface):
        results = global_surface["body"]["results"]
        parameters = results["parameters"]
        assert set(parameters) == {"rho", "eta", "gamma"}
        assert -1.0 < parameters["rho"] < 1.0
        assert parameters["eta"] > 0
        assert 0.0 < parameters["gamma"] < 1.0
        assert len(results["slices"]) == 2, "both expiries contribute a theta knot"

    async def test_calendar_arbitrage_is_structurally_impossible(self, global_surface):
        """SSVI's claim over per-expiry SVI, asserted rather than assumed."""
        calibration = global_surface["body"]["results"]["calibration"]
        assert calibration["calendar_arbitrage_free"] is True

        thetas = calibration["term_structure"]["thetas"]
        assert thetas == sorted(thetas), "the variance term structure is non-decreasing"
        assert calibration["term_structure"]["is_monotone"] is True

    async def test_butterfly_freedom_is_checked_two_ways(self, global_surface):
        """The closed-form bounds are sufficient, not necessary, so Durrleman's
        condition is evaluated too and both are reported."""
        calibration = global_surface["body"]["results"]["calibration"]
        assert calibration["min_durrleman_g"] >= -1e-9
        assert calibration["max_butterfly_quantity"] is not None
        assert isinstance(calibration["butterfly_bounds_satisfied"], bool)

    async def test_the_fit_is_reported_in_volatility_points(self, global_surface):
        calibration = global_surface["body"]["results"]["calibration"]
        assert calibration["rmse_vol_points"] < 2.0
        for slice_ in calibration["slices"]:
            assert slice_["n_observations"] >= 5
            assert slice_["theta"] > 0
            assert slice_["atm_volatility"] > 0

    async def test_a_stored_surface_reads_back_without_refitting(
        self, global_surface, client, auth_header
    ):
        response = await client.get(
            f"/derivatives/global-surfaces/{global_surface['row_id']}",
            headers={"Authorization": auth_header},
        )
        assert response.status_code == 200, response.text
        stored = response.json()["results"]
        assert stored["parameters"] == global_surface["body"]["results"]["parameters"]
        assert stored["surface_id"] == global_surface["body"]["results"]["surface_id"]

    async def test_a_stored_surface_keeps_the_provenance_of_its_forwards(
        self, global_surface, client, auth_header
    ):
        """A surface read back must not disclaim its own forwards.

        The forward method and confidence are Phase 1 measurements, and a
        reference value is flagged LOW_CONFIDENCE_FORWARD on the strength of
        the second. A stored surface that forgot them would flag every value it
        produced and would quietly penalise the consensus confidence for a
        reason that is not true — which is exactly what it did until these two
        columns were added.
        """
        response = await client.get(
            f"/derivatives/global-surfaces/{global_surface['row_id']}",
            headers={"Authorization": auth_header},
        )
        for slice_ in response.json()["results"]["slices"]:
            assert slice_["forward_method"] == "PUT_CALL_PARITY"
            assert slice_["forward_confidence"] > 0.5

    async def test_provenance_names_every_model_behind_the_result(self, global_surface):
        provenance = global_surface["body"]["provenance"]
        versions = provenance["model_versions"]
        assert versions["global_surface"].startswith("ssvi@")
        assert versions["local_volatility"].startswith("dupire-ssvi@")
        assert versions["density"].startswith("breeden-litzenberger@")
        assert versions["heston"].startswith("heston-slsqp@")
        assert provenance["surface_id"].startswith("global-surface:")
        assert provenance["calibration_timestamp"] is not None


class TestLocalVolatility:
    async def test_the_grid_conserves_its_points(self, global_surface, client, auth_header):
        response = await client.get(
            f"/derivatives/global-surfaces/{global_surface['row_id']}/local-volatility",
            headers={"Authorization": auth_header},
        )
        assert response.status_code == 200, response.text
        grid = response.json()
        assert grid["total_points"] == grid["valid_points"] + grid["flagged_points"]
        assert 0.0 <= grid["coverage"] <= 1.0
        assert len(grid["values"]) == len(grid["maturities"])
        assert len(grid["values"][0]) == len(grid["log_moneyness"])

    async def test_invalid_regions_are_holes_with_reasons(
        self, global_surface, client, auth_header
    ):
        response = await client.get(
            f"/derivatives/global-surfaces/{global_surface['row_id']}/local-volatility",
            headers={"Authorization": auth_header},
        )
        grid = response.json()
        holes = sum(1 for row in grid["values"] for value in row if value is None)
        assert holes == grid["flagged_points"]
        if holes:
            assert grid["flag_counts"], "a hole without a named reason is a silent drop"


class TestDensity:
    async def test_a_density_is_stored_per_expiry(self, global_surface, client, auth_header):
        response = await client.get(
            f"/derivatives/global-surfaces/{global_surface['row_id']}/densities",
            headers={"Authorization": auth_header},
        )
        assert response.status_code == 200, response.text
        densities = response.json()
        assert len(densities) == 2
        for density in densities:
            assert density["total_mass"] > 0
            assert len(density["strikes"]) == len(density["density"])

    async def test_quantiles_exist_only_for_an_admissible_density(
        self, global_surface, client, auth_header
    ):
        response = await client.get(
            f"/derivatives/global-surfaces/{global_surface['row_id']}/densities",
            headers={"Authorization": auth_header},
        )
        for density in response.json():
            values = [value for value in density["percentiles"].values() if value is not None]
            if density["is_admissible"]:
                assert len(values) == 5
                assert values == sorted(values)
            else:
                assert values == [], "a quantile of an inadmissible density has no meaning"


class TestHeston:
    async def test_feller_is_reported_whether_or_not_it_holds(
        self, global_surface, client, auth_header
    ):
        surface = await client.get(
            f"/derivatives/global-surfaces/{global_surface['row_id']}",
            headers={"Authorization": auth_header},
        )
        underlying_id = surface.json()["results"]["underlying_id"]
        response = await client.get(
            f"/derivatives/underlyings/{underlying_id}/heston",
            headers={"Authorization": auth_header},
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["feller"] is not None
        assert body["satisfies_feller"] == (body["feller"] > 0)
        assert body["feller_enforced"] is False

    async def test_a_short_term_structure_says_mean_reversion_is_not_identified(
        self, global_surface, client, auth_header
    ):
        """Two expiries pin `kappa * theta` and not the two separately. The fit
        describes the surface well and the parameters must not be read alone,
        so the result says which of those two things it is."""
        surface = await client.get(
            f"/derivatives/global-surfaces/{global_surface['row_id']}",
            headers={"Authorization": auth_header},
        )
        underlying_id = surface.json()["results"]["underlying_id"]
        body = (
            await client.get(
                f"/derivatives/underlyings/{underlying_id}/heston",
                headers={"Authorization": auth_header},
            )
        ).json()
        assert body["n_maturities"] == 2
        assert "HESTON_MEAN_REVERSION_NOT_IDENTIFIED" in body["warnings"]
        assert body["rmse_vol_points"] < 1.0, "the surface itself is still described well"


@pytest.fixture
async def consensus(client, auth_header, global_surface):
    """An at-the-money option from the chain, priced by every model."""
    chain = await client.get(
        f"/market/chains/{global_surface['snapshot_id']}",
        headers={"Authorization": auth_header},
    )
    assert chain.status_code == 200, chain.text
    results = chain.json()["results"]
    spot = float(results["underlying_price"])
    calls = [row for row in results["quotes"] if row["option_type"] == "CALL"]
    assert calls
    target = min(calls, key=lambda row: abs(float(row["strike"]) - spot))

    accepted = await client.post(
        "/derivatives/consensus",
        headers={"Authorization": auth_header},
        json={
            "instrument_id": target["instrument_id"],
            "risk_free_rate": SYNTHETIC_RATE,
            "dividend_yield": 0.0,
            "paths": 20000,
            "grid_nodes": 201,
            "grid_steps": 100,
        },
    )
    assert accepted.status_code == 202, accepted.text
    job_id = accepted.json()["job_id"]
    job = await client.get(f"/jobs/{job_id}", headers={"Authorization": auth_header})
    assert job.json()["status"] == "COMPLETED", job.json()
    payload = await client.get(f"/jobs/{job_id}/result", headers={"Authorization": auth_header})
    return payload.json()["result"]


class TestModelConsensus:
    async def test_there_is_no_single_price(self, consensus):
        results = consensus["results"]
        assert results["reference_value"] is not None
        low, high = results["reference_range"]
        assert low <= results["reference_value"] <= high
        assert results["model_dispersion"]["absolute"] == pytest.approx(high - low)
        assert len(results["values"]) == 4

    async def test_every_model_reports_a_value_or_a_reason(self, consensus):
        for value in consensus["results"]["values"]:
            has_value = value["value"] is not None
            has_reason = value["unavailable_reason"] is not None
            assert has_value != has_reason, value["model"]

    async def test_the_models_that_share_a_surface_agree_closely(self, consensus):
        """Black-Scholes at the surface's own volatility, the Dupire PDE built
        from that surface and a simulation under it are three routes to the same
        number, so a wide gap between them is a bug rather than model risk."""
        by_model = {v["model"]: v["value"] for v in consensus["results"]["values"]}
        bsm = by_model["BLACK_SCHOLES_MERTON"]
        assert bsm is not None
        for name in ("LOCAL_VOL_PDE", "MONTE_CARLO"):
            assert by_model[name] is not None
            assert abs(by_model[name] - bsm) / bsm < 0.02, name

    async def test_confidence_can_always_be_taken_apart(self, consensus):
        confidence = consensus["results"]["confidence"]
        assert 0.0 <= confidence["score"] <= 1.0
        names = {c["name"] for c in confidence["contributions"]}
        assert {"model_count", "surface_admissibility", "extrapolation"} <= names
        # An at-the-money contract on a fitted expiry is not an extrapolation of
        # anything, so this contribution must be full marks. It was 0.5 while a
        # stored surface was losing its forward confidence.
        extrapolation = next(c for c in confidence["contributions"] if c["name"] == "extrapolation")
        assert extrapolation["score"] == 1.0, extrapolation["basis"]
        for contribution in confidence["contributions"]:
            assert contribution["basis"], "a score without its basis is not explainable"
        assert confidence["weakest_contribution"] in names

    async def test_the_market_price_is_an_observation_kept_apart(self, consensus):
        results = consensus["results"]
        if results["market_price"] is None:
            codes = {w["code"] for w in consensus["warnings"]}
            assert "ADVANCED_NO_OBSERVED_PRICE" in codes
        else:
            assert results["market_deviation"] == pytest.approx(
                results["market_price"] - results["reference_value"]
            )

    async def test_a_caveated_calibration_travels_with_the_price_it_produced(self, consensus):
        """The Heston parameters here are fitted to two expiries, which pins
        `kappa * theta` and not the two separately. The consensus prices from
        them anyway — the surface is reproduced well and that is what the price
        depends on — and says so, rather than presenting the parameters as if
        they were identified."""
        codes = {warning["code"] for warning in consensus["warnings"]}
        assert "ADVANCED_HESTON_FITTED_WITH_CAVEATS" in codes
        caveat = next(
            w for w in consensus["warnings"] if w["code"] == "ADVANCED_HESTON_FITTED_WITH_CAVEATS"
        )
        assert "HESTON_MEAN_REVERSION_NOT_IDENTIFIED" in caveat["context"]["heston_warnings"]
        heston = next(v for v in consensus["results"]["values"] if v["model"] == "HESTON")
        assert heston["value"] is not None, "a caveat is not a refusal"

    async def test_higher_order_greeks_come_with_their_units(self, consensus):
        greeks = consensus["results"]["higher_order_greeks"]
        assert greeks is not None
        # The units block names the *scaled* readings, because those are the
        # ones a reader acts on; the raw partials sit beside them so nothing
        # downstream has to back out a factor.
        assert set(greeks["units"]) == {
            "vanna_per_vol_point",
            "volga_per_vol_point",
            "charm_per_day",
        }
        assert greeks["vanna"] is not None
        assert greeks["volga"] is not None
        assert greeks["vanna_per_vol_point"] == pytest.approx(greeks["vanna"] * 0.01)
        assert greeks["charm_per_day"] == pytest.approx(greeks["charm_per_year"] / 365.0)

    async def test_the_payload_carries_no_advisory_language(self, consensus):
        blob = json.dumps(consensus).lower()
        assert [phrase for phrase in BANNED if phrase in blob] == []

    async def test_there_is_no_field_that_could_hold_a_verdict(self, consensus):
        keys = {key.lower() for key in _keys(consensus)}
        assert [key for key in BANNED_KEYS if key in keys] == []

    async def test_a_stored_run_reads_back(self, consensus, client, auth_header):
        row_id = consensus["results"]["consensus_row_id"]
        response = await client.get(
            f"/derivatives/consensus/{row_id}", headers={"Authorization": auth_header}
        )
        assert response.status_code == 200, response.text
        stored = response.json()
        assert stored["reference_value"] == pytest.approx(consensus["results"]["reference_value"])
        assert stored["models_available"] == consensus["results"]["counts"]["models_available"]
        assert len(stored["values"]) == 4

    async def test_another_user_cannot_read_the_run(self, consensus, client):
        row_id = consensus["results"]["consensus_row_id"]
        _other, other_header = await register_and_login(client)
        response = await client.get(
            f"/derivatives/consensus/{row_id}", headers={"Authorization": other_header}
        )
        assert response.status_code == 404
        assert (
            await client.get("/derivatives/consensus", headers={"Authorization": other_header})
        ).json() == []


class TestRefusals:
    async def test_a_non_option_is_refused_before_a_job_exists(
        self, client, auth_header, global_surface
    ):
        surface = await client.get(
            f"/derivatives/global-surfaces/{global_surface['row_id']}",
            headers={"Authorization": auth_header},
        )
        underlying_id = surface.json()["results"]["underlying_id"]
        response = await client.post(
            "/derivatives/consensus",
            headers={"Authorization": auth_header},
            json={"instrument_id": underlying_id},
        )
        assert response.status_code == 422
        assert response.json()["code"] == "INSTRUMENT_NOT_AN_OPTION"

    async def test_an_unknown_model_is_named(self, client, auth_header, global_surface):
        chain = await client.get(
            f"/market/chains/{global_surface['snapshot_id']}",
            headers={"Authorization": auth_header},
        )
        instrument_id = chain.json()["results"]["quotes"][0]["instrument_id"]
        response = await client.post(
            "/derivatives/consensus",
            headers={"Authorization": auth_header},
            json={"instrument_id": instrument_id, "models": ["BINOMIAL_TREE"]},
        )
        assert response.status_code == 422
        assert response.json()["code"] == "UNKNOWN_PRICING_MODEL"
        assert "BINOMIAL_TREE" in response.json()["detail"]
