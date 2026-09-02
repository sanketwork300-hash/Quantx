"""The stateless derivatives calculators."""

from __future__ import annotations

import math

import pytest

from quant.pricing.black_scholes import bsm_price


def auth(header):
    return {"Authorization": header}


class TestImpliedVolatilityEndpoint:
    async def test_round_trip_on_a_spot_quote(self, client, auth_header):
        price = float(bsm_price(100.0, 100.0, 0.25, 0.05, 0.0, 0.2, True))
        response = await client.post(
            "/derivatives/iv",
            headers=auth(auth_header),
            json={
                "price": price,
                "spot": 100.0,
                "strike": 100.0,
                "time_to_expiry": 0.25,
                "is_call": True,
                "rate": 0.05,
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "OK"
        assert body["results"]["implied_volatility"] == pytest.approx(0.2, abs=1e-8)
        assert body["results"]["parameterization"] == "BLACK_SCHOLES_MERTON"

    async def test_forward_and_spot_forms_agree(self, client, auth_header):
        tau, rate = 0.25, 0.05
        price = float(bsm_price(100.0, 105.0, tau, rate, 0.0, 0.3, True))
        common = {
            "price": price,
            "strike": 105.0,
            "time_to_expiry": tau,
            "is_call": True,
            "rate": rate,
        }
        spot_body = (
            await client.post(
                "/derivatives/iv", headers=auth(auth_header), json={**common, "spot": 100.0}
            )
        ).json()
        forward_body = (
            await client.post(
                "/derivatives/iv",
                headers=auth(auth_header),
                json={**common, "forward": 100.0 * math.exp(rate * tau)},
            )
        ).json()
        assert spot_body["results"]["implied_volatility"] == pytest.approx(
            forward_body["results"]["implied_volatility"], abs=1e-12
        )

    async def test_reports_the_solver_and_its_conditioning(self, client, auth_header):
        price = float(bsm_price(100.0, 100.0, 0.25, 0.0, 0.0, 0.2, True))
        results = (
            await client.post(
                "/derivatives/iv",
                headers=auth(auth_header),
                json={
                    "price": price,
                    "spot": 100.0,
                    "strike": 100.0,
                    "time_to_expiry": 0.25,
                    "is_call": True,
                },
            )
        ).json()["results"]
        assert results["converged"] is True
        assert results["solver"] in {"safeguarded-newton", "brent"}
        assert results["lower_bound"] > 0
        assert results["vega"] > 0
        assert results["well_conditioned"] is True

    async def test_a_sub_intrinsic_price_fails_with_a_named_reason(self, client, auth_header):
        """FAILED, not a 500, and not a null with no explanation."""
        response = await client.post(
            "/derivatives/iv",
            headers=auth(auth_header),
            json={
                "price": 1.0,
                "forward": 100.0,
                "strike": 80.0,
                "time_to_expiry": 0.5,
                "is_call": True,
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "FAILED"
        assert body["results"]["implied_volatility"] is None
        assert body["results"]["error"] == "PRICE_BELOW_INTRINSIC"
        assert body["warnings"][0]["code"] == "PRICE_BELOW_INTRINSIC"

    async def test_neither_spot_nor_forward_is_a_validation_error(self, client, auth_header):
        response = await client.post(
            "/derivatives/iv",
            headers=auth(auth_header),
            json={
                "price": 5.0,
                "strike": 100.0,
                "time_to_expiry": 0.25,
                "is_call": True,
            },
        )
        assert response.status_code == 422

    async def test_requires_authentication(self, client):
        response = await client.post(
            "/derivatives/iv",
            json={
                "price": 5.0,
                "spot": 100.0,
                "strike": 100.0,
                "time_to_expiry": 0.25,
                "is_call": True,
            },
        )
        assert response.status_code == 401


class TestGreeksEndpoint:
    async def test_returns_greeks_with_their_units(self, client, auth_header):
        response = await client.post(
            "/derivatives/greeks",
            headers=auth(auth_header),
            json={
                "spot": 100.0,
                "strike": 100.0,
                "time_to_expiry": 1.0,
                "sigma": 0.2,
                "is_call": True,
                "rate": 0.05,
                "dividend_yield": 0.02,
            },
        )
        assert response.status_code == 200
        results = response.json()["results"]
        assert results["delta"] == pytest.approx(0.5868511461, abs=1e-8)
        assert results["gamma"] == pytest.approx(0.0189505788, abs=1e-8)
        assert results["vega_per_vol_point"] == pytest.approx(0.3790115751, abs=1e-8)
        assert results["theta_per_day"] == pytest.approx(-0.0139433395, abs=1e-8)
        assert results["rho_per_bp"] == pytest.approx(0.0049458109, abs=1e-8)

    async def test_units_are_named_in_the_payload(self, client, auth_header):
        """An unlabelled vega is a bug report waiting to happen."""
        results = (
            await client.post(
                "/derivatives/greeks",
                headers=auth(auth_header),
                json={
                    "spot": 100.0,
                    "strike": 100.0,
                    "time_to_expiry": 1.0,
                    "sigma": 0.2,
                    "is_call": True,
                },
            )
        ).json()["results"]
        units = results["units"]
        assert "volatility point" in units["vega_per_vol_point"]
        assert "calendar day" in units["theta_per_day"]
        assert "basis point" in units["rho_per_bp"]

    async def test_forward_form_recovers_the_implied_spot(self, client, auth_header):
        tau, rate = 0.5, 0.06
        forward = 100.0 * math.exp(rate * tau)
        results = (
            await client.post(
                "/derivatives/greeks",
                headers=auth(auth_header),
                json={
                    "forward": forward,
                    "strike": 100.0,
                    "time_to_expiry": tau,
                    "sigma": 0.2,
                    "is_call": True,
                    "rate": rate,
                },
            )
        ).json()["results"]
        assert results["inputs"]["spot"] == pytest.approx(100.0, rel=1e-12)

    async def test_provenance_names_the_pricing_model(self, client, auth_header):
        body = (
            await client.post(
                "/derivatives/greeks",
                headers=auth(auth_header),
                json={
                    "spot": 100.0,
                    "strike": 100.0,
                    "time_to_expiry": 1.0,
                    "sigma": 0.2,
                    "is_call": True,
                },
            )
        ).json()
        assert body["provenance"]["model_versions"]["pricing"].startswith("black-scholes-merton@")


class TestForwardEndpoint:
    async def test_put_call_parity_regression(self, client, auth_header):
        from quant.pricing.black76 import black76_price

        tau, rate, sigma = 0.25, 0.065, 0.18
        forward = 24392.0
        discount = math.exp(-rate * tau)
        strikes = [22000.0 + 200.0 * i for i in range(25)]
        calls = [float(black76_price(forward, k, tau, sigma, True, discount)) for k in strikes]
        puts = [float(black76_price(forward, k, tau, sigma, False, discount)) for k in strikes]

        body = (
            await client.post(
                "/derivatives/forward",
                headers=auth(auth_header),
                json={
                    "time_to_expiry": tau,
                    "strikes": strikes,
                    "call_prices": calls,
                    "put_prices": puts,
                },
            )
        ).json()
        selected = body["results"]["selected"]
        assert selected["method"] == "PUT_CALL_PARITY"
        assert selected["value"] == pytest.approx(forward, rel=1e-9)
        assert selected["discount_factor"] == pytest.approx(discount, rel=1e-9)
        assert selected["observations"] == 25

    async def test_spot_carry_records_its_assumptions(self, client, auth_header):
        body = (
            await client.post(
                "/derivatives/forward",
                headers=auth(auth_header),
                json={
                    "time_to_expiry": 0.25,
                    "spot": 24000.0,
                    "rate": 0.065,
                    "dividend_yield": 0.0,
                },
            )
        ).json()
        selected = body["results"]["selected"]
        assert selected["method"] == "SPOT_CARRY"
        assert "dividend_yield_assumed" in selected["assumptions"]
        assert selected["confidence"] < 0.6, "an assumed carry cannot be high confidence"

    async def test_all_methods_are_returned_with_their_disagreement(self, client, auth_header):
        body = (
            await client.post(
                "/derivatives/forward",
                headers=auth(auth_header),
                json={
                    "time_to_expiry": 0.25,
                    "spot": 24000.0,
                    "rate": 0.065,
                    "future_price": 24400.0,
                },
            )
        ).json()
        methods = {e["method"] for e in body["results"]["estimates"]}
        assert methods == {"SPOT_CARRY", "FUTURE"}
        assert body["results"]["selected"]["method"] == "FUTURE"
        assert body["results"]["disagreement"] > 0

    async def test_too_few_pairs_fails_with_a_reason(self, client, auth_header):
        body = (
            await client.post(
                "/derivatives/forward",
                headers=auth(auth_header),
                json={
                    "time_to_expiry": 0.25,
                    "strikes": [100.0, 110.0],
                    "call_prices": [5.0, 2.0],
                    "put_prices": [2.0, 5.0],
                },
            )
        ).json()
        assert body["status"] == "FAILED"
        assert body["results"]["estimates"][0]["error"] == "INSUFFICIENT_PAIRS"

    async def test_no_inputs_is_a_validation_error(self, client, auth_header):
        response = await client.post(
            "/derivatives/forward",
            headers=auth(auth_header),
            json={"time_to_expiry": 0.25},
        )
        assert response.status_code == 422

    async def test_mismatched_array_lengths_are_rejected(self, client, auth_header):
        response = await client.post(
            "/derivatives/forward",
            headers=auth(auth_header),
            json={
                "time_to_expiry": 0.25,
                "strikes": [100.0, 110.0, 120.0],
                "call_prices": [5.0, 2.0],
                "put_prices": [2.0, 5.0],
            },
        )
        assert response.status_code == 422
