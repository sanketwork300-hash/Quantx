"""Phase 3 end to end: scan a calibrated surface and read the history.

Carries the Phase 3 acceptance criteria from docs/backlog.md.
"""

from __future__ import annotations

import json

import pytest

from tests.integration.test_derivatives import AS_OF, MAPPING, SYNTHETIC_RATE
from tests.integration.test_surface import analyse, calibrate


async def upload_chain(client, header, csv_bytes, mapping_overrides=None):
    upload = await client.post(
        "/uploads",
        headers={"Authorization": header},
        files={"file": ("chain.csv", csv_bytes, "text/csv")},
    )
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
    result = await client.get(
        f"/jobs/{accepted.json()['job_id']}/result", headers={"Authorization": header}
    )
    return result.json()["result"]["results"]["snapshot_id"]


def perturb(csv_bytes: bytes, strike: str, option_type: str, bump: float) -> bytes:
    """Move one quote off the surface, keeping its spread intact.

    A stale leg or a fat-fingered print looks exactly like this: a market that
    is internally well formed and simply in the wrong place.
    """
    lines = csv_bytes.decode().splitlines()
    header = lines[0].split(",")
    strike_i, type_i = header.index("STRIKE_PRICE"), header.index("CE_PE")
    bid_i, ask_i, ltp_i = header.index("BID"), header.index("ASK"), header.index("LTP")

    out = [lines[0]]
    for line in lines[1:]:
        cells = line.split(",")
        if cells[strike_i] == strike and cells[type_i] == option_type:
            for index in (bid_i, ask_i, ltp_i):
                cells[index] = f"{float(cells[index]) + bump:.2f}"
        out.append(",".join(cells))
    return ("\n".join(out) + "\n").encode()


async def scan(client, header, surface_row_id, **policy):
    accepted = await client.post(
        f"/derivatives/surfaces/{surface_row_id}/anomalies",
        headers={"Authorization": header},
        json=policy,
    )
    assert accepted.status_code == 202, accepted.text
    job_id = accepted.json()["job_id"]

    job = await client.get(f"/jobs/{job_id}", headers={"Authorization": header})
    assert job.json()["status"] == "COMPLETED", job.json()
    payload = await client.get(f"/jobs/{job_id}/result", headers={"Authorization": header})
    return payload.json()["result"]


@pytest.fixture
async def scanned(client, auth_header, clean_chain_csv):
    """A chain with one quote nudged 30 points off the surface."""
    corrupted = perturb(clean_chain_csv, "24000", "CE", 30.0)
    snapshot_id = await upload_chain(client, auth_header, corrupted)
    analysis_id = await analyse(client, auth_header, snapshot_id)
    surface = await calibrate(client, auth_header, analysis_id)
    surface_row_id = surface["results"]["surface_row_id"]
    body = await scan(client, auth_header, surface_row_id)
    return {
        "snapshot_id": snapshot_id,
        "analysis_id": analysis_id,
        "surface_row_id": surface_row_id,
        "body": body,
        "scan_id": body["results"]["scan_row_id"],
    }


class TestScanning:
    async def test_every_quote_is_examined_and_scored(self, scanned):
        counts = scanned["body"]["results"]["counts"]
        assert counts["examined"] == 60
        assert counts["scored"] == 60

    async def test_the_perturbed_quote_is_found(self, scanned):
        results = scanned["body"]["results"]
        assert results["counts"]["flagged"] >= 1
        strikes = {a["strike"] for a in results["anomalies"]}
        assert "24000" in strikes

    async def test_a_flagged_quote_answers_every_required_question(self, scanned):
        """Phase 3 acceptance: what deviated, by how much, relative to what,
        with what liquidity and what confidence."""
        anomaly = scanned["body"]["results"]["anomalies"][0]

        assert anomaly["instrument_id"] and anomaly["strike"] and anomaly["option_type"]
        assert anomaly["market_iv"] is not None  # what deviated
        assert anomaly["reference_iv"] is not None  # relative to what
        assert anomaly["iv_difference_vol_points"] != 0.0  # by how much
        assert anomaly["explained_scale"] > 0  # against what scale
        assert anomaly["z_score"] != 0.0
        assert anomaly["liquidity_score"] is not None  # with what liquidity
        assert 0.0 <= anomaly["confidence"] <= 1.0  # with what confidence
        assert anomaly["explanation"]  # and why

    async def test_the_explanation_is_grounded_in_measurements(self, scanned):
        anomaly = scanned["body"]["results"]["anomalies"][0]
        factors = {entry["factor"] for entry in anomaly["explanation"]}
        assert {
            "data quality",
            "liquidity",
            "surface fit",
            "measurement resolution",
            "bid/ask envelope",
            "standardised deviation",
        } <= factors
        for entry in anomaly["explanation"]:
            assert entry["detail"]
            assert entry["effect"] in {"SUPPORTS", "REDUCES", "NEUTRAL"}

    async def test_the_deviation_is_outside_the_quoted_market(self, scanned):
        anomaly = scanned["body"]["results"]["anomalies"][0]
        assert anomaly["envelope_position"] in {"ABOVE_ASK", "BELOW_BID"}
        assert anomaly["excess_over_envelope"] > 0

    async def test_the_policy_is_recorded_with_the_result(self, scanned):
        policy = scanned["body"]["results"]["policy"]
        assert policy["min_z_score"] == 2.0
        assert policy["require_outside_envelope"] is True
        # The formula that decides the answer is itself recorded.
        assert "bid/ask" in policy["explained_scale"]

    async def test_provenance_names_the_surface_and_the_model(self, scanned):
        provenance = scanned["body"]["provenance"]
        assert provenance["model_versions"]["anomaly"].startswith("surface-anomaly@")
        assert provenance["surface_id"].startswith("surface:")
        assert provenance["parameters"]["policy"]["min_z_score"] == 2.0

    async def test_a_first_scan_says_it_has_no_history(self, scanned):
        codes = {warning["code"] for warning in scanned["body"]["warnings"]}
        assert "ANOMALY_NO_HISTORY" in codes

    async def test_a_clean_chain_flags_nothing(self, client, auth_header, clean_chain_csv):
        """The detector must be quiet on a market that agrees with its own fit,
        or it is only measuring noise."""
        snapshot_id = await upload_chain(client, auth_header, clean_chain_csv)
        analysis_id = await analyse(client, auth_header, snapshot_id)
        surface = await calibrate(client, auth_header, analysis_id)
        body = await scan(client, auth_header, surface["results"]["surface_row_id"])
        assert body["results"]["counts"]["scored"] == 60
        assert body["results"]["counts"]["flagged"] == 0


class TestPolicy:
    async def test_a_stricter_threshold_flags_fewer(self, client, auth_header, scanned):
        strict = await scan(client, auth_header, scanned["surface_row_id"], min_z_score=100.0)
        assert strict["results"]["counts"]["flagged"] == 0

    async def test_the_policy_is_the_only_gate(self, client, auth_header, scanned):
        """With every filter opened, every scored quote is flagged — so what the
        default policy excludes, it excludes on purpose rather than by accident.
        """
        loose = await scan(
            client,
            auth_header,
            scanned["surface_row_id"],
            min_z_score=0.0,
            require_outside_envelope=False,
            min_confidence=0.0,
            min_liquidity=0.0,
        )
        counts = loose["results"]["counts"]
        assert counts["flagged"] == counts["scored"] == 60
        assert counts["flagged"] > scanned["body"]["results"]["counts"]["flagged"]

    async def test_the_threshold_used_is_stored_on_the_scan(self, client, auth_header, scanned):
        body = await scan(client, auth_header, scanned["surface_row_id"], min_z_score=3.5)
        assert body["results"]["policy"]["min_z_score"] == 3.5


class TestRetrieval:
    async def test_the_scan_is_retrievable_by_id(self, client, auth_header, scanned):
        response = await client.get(
            f"/derivatives/scans/{scanned['scan_id']}",
            headers={"Authorization": auth_header},
        )
        assert response.status_code == 200
        assert response.json()["results"]["counts"]["scored"] == 60

    async def test_the_latest_scan_by_underlying(self, client, auth_header, scanned):
        chain = await client.get(
            f"/market/chains/{scanned['snapshot_id']}",
            headers={"Authorization": auth_header},
        )
        underlying_id = chain.json()["results"]["underlying_id"]
        response = await client.get(
            f"/derivatives/anomalies/{underlying_id}",
            headers={"Authorization": auth_header},
        )
        assert response.status_code == 200
        assert response.json()["results"]["scan_id"] == scanned["scan_id"]

    async def test_unflagged_quotes_are_stored_and_retrievable(self, client, auth_header, scanned):
        """The quotes that did not clear the threshold are the evidence the
        threshold was doing something."""
        response = await client.get(
            f"/derivatives/scans/{scanned['scan_id']}?flagged_only=false&limit=200",
            headers={"Authorization": auth_header},
        )
        assert len(response.json()["results"]["anomalies"]) == 60

    async def test_confidence_filtering(self, client, auth_header, scanned):
        response = await client.get(
            f"/derivatives/scans/{scanned['scan_id']}?flagged_only=false"
            "&min_confidence=0.99&limit=200",
            headers={"Authorization": auth_header},
        )
        for anomaly in response.json()["results"]["anomalies"]:
            assert anomaly["confidence"] >= 0.99


class TestHistoricalDeviation:
    async def test_a_second_scan_has_history_from_the_first(self, client, auth_header, scanned):
        second = await scan(client, auth_header, scanned["surface_row_id"])
        assert "ANOMALY_NO_HISTORY" not in {warning["code"] for warning in second["warnings"]}
        response = await client.get(
            f"/derivatives/scans/{second['results']['scan_row_id']}?flagged_only=false",
            headers={"Authorization": auth_header},
        )
        anomalies = response.json()["results"]["anomalies"]
        assert any(a["historical_observations"] > 0 for a in anomalies)


class TestSurfaceHistory:
    async def test_characteristics_are_recorded_at_standard_tenors(
        self, client, auth_header, scanned
    ):
        chain = await client.get(
            f"/market/chains/{scanned['snapshot_id']}",
            headers={"Authorization": auth_header},
        )
        underlying_id = chain.json()["results"]["underlying_id"]

        response = await client.get(
            f"/derivatives/history/{underlying_id}",
            headers={"Authorization": auth_header},
        )
        assert response.status_code == 200
        tenors = response.json()["results"]["tenors"]
        assert {t["tenor_days"] for t in tenors} == {7, 30, 60, 90, 180, 365}

    async def test_a_single_surface_is_reported_but_marked_unreliable(
        self, client, auth_header, scanned
    ):
        chain = await client.get(
            f"/market/chains/{scanned['snapshot_id']}",
            headers={"Authorization": auth_header},
        )
        underlying_id = chain.json()["results"]["underlying_id"]
        body = (
            await client.get(
                f"/derivatives/history/{underlying_id}?tenor_days=30",
                headers={"Authorization": auth_header},
            )
        ).json()

        tenor = body["results"]["tenors"][0]
        assert tenor["observations"] == 1
        assert tenor["is_reliable"] is False
        assert any(w["code"] == "HISTORY_INSUFFICIENT_OBSERVATIONS" for w in body["warnings"])
        for percentile in tenor["percentiles"]:
            assert percentile["percentile"] is not None
            assert percentile["is_reliable"] is False

    async def test_every_characteristic_is_ranked(self, client, auth_header, scanned):
        chain = await client.get(
            f"/market/chains/{scanned['snapshot_id']}",
            headers={"Authorization": auth_header},
        )
        underlying_id = chain.json()["results"]["underlying_id"]
        tenor = (
            await client.get(
                f"/derivatives/history/{underlying_id}?tenor_days=30",
                headers={"Authorization": auth_header},
            )
        ).json()["results"]["tenors"][0]
        assert {p["name"] for p in tenor["percentiles"]} == {
            "atm_volatility",
            "skew",
            "curvature",
            "atm_total_variance",
        }

    async def test_extrapolated_tenors_are_flagged(self, client, auth_header, scanned):
        chain = await client.get(
            f"/market/chains/{scanned['snapshot_id']}",
            headers={"Authorization": auth_header},
        )
        underlying_id = chain.json()["results"]["underlying_id"]
        tenor = (
            await client.get(
                f"/derivatives/history/{underlying_id}?tenor_days=365",
                headers={"Authorization": auth_header},
            )
        ).json()["results"]["tenors"][0]
        assert tenor["series"][0]["method"] == "EXTRAPOLATED_MATURITY"

    async def test_history_for_an_unknown_underlying_is_not_found(self, client, auth_header):
        import uuid

        response = await client.get(
            f"/derivatives/history/{uuid.uuid4()}",
            headers={"Authorization": auth_header},
        )
        assert response.status_code == 404


class TestLanguagePolicy:
    """A contract guarantee: analysis, never advice."""

    FORBIDDEN = (
        "buy",
        "sell",
        "cheap",
        "expensive",
        "underpriced",
        "overpriced",
        "arbitrage",
        "fair value",
        "opportunity",
        "recommend",
    )

    async def test_the_scan_response_contains_no_advisory_language(
        self, client, auth_header, scanned
    ):
        response = await client.get(
            f"/derivatives/scans/{scanned['scan_id']}?flagged_only=false&limit=200",
            headers={"Authorization": auth_header},
        )
        rendered = response.text.lower()
        for word in self.FORBIDDEN:
            assert word not in rendered, f"anomaly output must not contain {word!r}"

    async def test_no_anomaly_carries_a_direction_or_a_rating(self, scanned):
        for anomaly in scanned["body"]["results"]["anomalies"]:
            for key in ("action", "direction", "rating", "recommendation", "target"):
                assert key not in anomaly

    async def test_the_job_result_contains_no_advisory_language(self, scanned):
        rendered = json.dumps(scanned["body"]).lower()
        for word in self.FORBIDDEN:
            assert word not in rendered


class TestOwnership:
    async def test_another_users_scan_is_not_found(self, client, scanned):
        from tests.conftest import register_and_login

        _other, other_header = await register_and_login(client, "a-other@example.com")
        response = await client.get(
            f"/derivatives/scans/{scanned['scan_id']}",
            headers={"Authorization": other_header},
        )
        assert response.status_code == 404

    async def test_cannot_scan_another_users_surface(self, client, scanned):
        from tests.conftest import register_and_login

        _other, other_header = await register_and_login(client, "a-other2@example.com")
        response = await client.post(
            f"/derivatives/surfaces/{scanned['surface_row_id']}/anomalies",
            headers={"Authorization": other_header},
            json={},
        )
        assert response.status_code == 404
