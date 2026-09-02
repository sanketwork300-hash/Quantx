"""Black-Scholes-Merton prices and Greeks.

Three independent checks: closed-form identities, agreement with ``vollib`` and
QuantLib, and every Greek against central finite differences of our own price
function. The last one catches an error the first two cannot: a Greek that is
internally inconsistent with the price it claims to differentiate.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from quant.pricing.black76 import black76_price
from quant.pricing.black_scholes import bsm_greeks, bsm_price, forward_price
from quant.pricing.greeks import BASIS_POINT, DAYS_PER_YEAR, VOL_POINT
from tests.tolerances import GREEK_VS_FD_REL, PARITY_ABS, PRICE_VS_REFERENCE_ABS

vollib_bsm = pytest.importorskip(
    "py_vollib.black_scholes_merton", reason="reference library not installed"
)
vollib_greeks = pytest.importorskip(
    "py_vollib.black_scholes_merton.greeks.analytical",
    reason="reference library not installed",
)
try:
    import QuantLib as ql
except ImportError:  # pragma: no cover - optional
    ql = None

# (spot, strike, tau, rate, dividend, sigma)
GRID = [
    (100.0, 100.0, 1.00, 0.05, 0.02, 0.20),
    (100.0, 80.0, 0.25, 0.03, 0.00, 0.35),
    (100.0, 130.0, 0.25, 0.03, 0.06, 0.35),
    (24000.0, 24000.0, 0.0833, 0.065, 0.0, 0.14),
    (24000.0, 20000.0, 0.50, 0.065, 0.01, 0.22),
    (24000.0, 30000.0, 0.50, 0.065, 0.01, 0.30),
    (1.20, 1.25, 2.00, 0.01, 0.03, 0.09),
    (50.0, 55.0, 0.0027, 0.02, 0.0, 0.85),
]


class TestIdentities:
    @pytest.mark.parametrize("spot,strike,tau,rate,dividend,sigma", GRID)
    def test_put_call_parity(self, spot, strike, tau, rate, dividend, sigma):
        call = float(bsm_price(spot, strike, tau, rate, dividend, sigma, True))
        put = float(bsm_price(spot, strike, tau, rate, dividend, sigma, False))
        expected = spot * math.exp(-dividend * tau) - strike * math.exp(-rate * tau)
        assert call - put == pytest.approx(expected, abs=PARITY_ABS)

    @pytest.mark.parametrize("spot,strike,tau,rate,dividend,sigma", GRID)
    def test_agrees_with_black76_on_the_forward(self, spot, strike, tau, rate, dividend, sigma):
        """The two parameterizations are the same model, so they must agree.

        This is what justifies pricing on the forward and taking Greeks in spot
        space.
        """
        forward = float(forward_price(spot, tau, rate, dividend))
        discount = math.exp(-rate * tau)
        for is_call in (True, False):
            spot_price = float(bsm_price(spot, strike, tau, rate, dividend, sigma, is_call))
            fwd_price = float(black76_price(forward, strike, tau, sigma, is_call, discount))
            assert spot_price == pytest.approx(fwd_price, rel=1e-13, abs=1e-13)

    @pytest.mark.parametrize("spot,strike,tau,rate,dividend,sigma", GRID)
    def test_prices_respect_no_arbitrage_bounds(self, spot, strike, tau, rate, dividend, sigma):
        carry = spot * math.exp(-dividend * tau)
        discounted = strike * math.exp(-rate * tau)
        call = float(bsm_price(spot, strike, tau, rate, dividend, sigma, True))
        put = float(bsm_price(spot, strike, tau, rate, dividend, sigma, False))
        assert max(carry - discounted, 0.0) - 1e-12 <= call <= carry + 1e-12
        assert max(discounted - carry, 0.0) - 1e-12 <= put <= discounted + 1e-12

    def test_expiry_collapses_to_intrinsic(self):
        assert float(bsm_price(120.0, 100.0, 0.0, 0.05, 0.0, 0.2, True)) == pytest.approx(20.0)
        assert float(bsm_price(80.0, 100.0, 0.0, 0.05, 0.0, 0.2, True)) == pytest.approx(0.0)


class TestGreeksAgainstFiniteDifferences:
    """Every Greek must differentiate the price function it belongs to."""

    @staticmethod
    def _central(f, x, h):
        return (f(x + h) - f(x - h)) / (2.0 * h)

    @pytest.mark.parametrize("spot,strike,tau,rate,dividend,sigma", GRID)
    @pytest.mark.parametrize("is_call", [True, False])
    def test_delta(self, spot, strike, tau, rate, dividend, sigma, is_call):
        greeks = bsm_greeks(spot, strike, tau, rate, dividend, sigma, is_call)
        fd = self._central(
            lambda s: float(bsm_price(s, strike, tau, rate, dividend, sigma, is_call)),
            spot,
            spot * 1e-5,
        )
        assert float(greeks.delta) == pytest.approx(fd, rel=GREEK_VS_FD_REL, abs=1e-9)

    @pytest.mark.parametrize("spot,strike,tau,rate,dividend,sigma", GRID)
    @pytest.mark.parametrize("is_call", [True, False])
    def test_gamma(self, spot, strike, tau, rate, dividend, sigma, is_call):
        greeks = bsm_greeks(spot, strike, tau, rate, dividend, sigma, is_call)
        h = spot * 1e-4
        fd = (
            float(bsm_price(spot + h, strike, tau, rate, dividend, sigma, is_call))
            - 2.0 * float(bsm_price(spot, strike, tau, rate, dividend, sigma, is_call))
            + float(bsm_price(spot - h, strike, tau, rate, dividend, sigma, is_call))
        ) / (h * h)
        assert float(greeks.gamma) == pytest.approx(fd, rel=1e-3, abs=1e-9)

    @pytest.mark.parametrize("spot,strike,tau,rate,dividend,sigma", GRID)
    @pytest.mark.parametrize("is_call", [True, False])
    def test_vega(self, spot, strike, tau, rate, dividend, sigma, is_call):
        greeks = bsm_greeks(spot, strike, tau, rate, dividend, sigma, is_call)
        fd = self._central(
            lambda v: float(bsm_price(spot, strike, tau, rate, dividend, v, is_call)),
            sigma,
            1e-6,
        )
        assert float(greeks.vega) == pytest.approx(fd, rel=GREEK_VS_FD_REL, abs=1e-9)

    @pytest.mark.parametrize("spot,strike,tau,rate,dividend,sigma", GRID)
    @pytest.mark.parametrize("is_call", [True, False])
    def test_theta(self, spot, strike, tau, rate, dividend, sigma, is_call):
        """Theta is -dV/dtau: value decays as time to expiry shrinks."""
        greeks = bsm_greeks(spot, strike, tau, rate, dividend, sigma, is_call)
        h = min(tau * 1e-4, 1e-6)
        d_by_dtau = self._central(
            lambda t: float(bsm_price(spot, strike, t, rate, dividend, sigma, is_call)),
            tau,
            h,
        )
        assert float(greeks.theta_per_year) == pytest.approx(-d_by_dtau, rel=1e-3, abs=1e-7)

    @pytest.mark.parametrize("spot,strike,tau,rate,dividend,sigma", GRID)
    @pytest.mark.parametrize("is_call", [True, False])
    def test_rho(self, spot, strike, tau, rate, dividend, sigma, is_call):
        greeks = bsm_greeks(spot, strike, tau, rate, dividend, sigma, is_call)
        fd = self._central(
            lambda r: float(bsm_price(spot, strike, tau, r, dividend, sigma, is_call)),
            rate,
            1e-7,
        )
        assert float(greeks.rho) == pytest.approx(fd, rel=1e-4, abs=1e-7)


class TestGreekUnits:
    """Units are the part people actually get wrong."""

    def test_vega_is_per_volatility_point(self):
        spot, strike, tau, rate, dividend, sigma = 100.0, 100.0, 1.0, 0.05, 0.0, 0.2
        greeks = bsm_greeks(spot, strike, tau, rate, dividend, sigma)
        moved = float(bsm_price(spot, strike, tau, rate, dividend, sigma + VOL_POINT))
        base = float(bsm_price(spot, strike, tau, rate, dividend, sigma))
        assert float(greeks.vega_per_vol_point) == pytest.approx(moved - base, rel=2e-3)

    def test_theta_is_per_calendar_day(self):
        greeks = bsm_greeks(100.0, 100.0, 1.0, 0.05, 0.0, 0.2)
        assert float(greeks.theta_per_day) == pytest.approx(
            float(greeks.theta_per_year) / DAYS_PER_YEAR
        )

    def test_rho_is_per_basis_point(self):
        spot, strike, tau, rate, dividend, sigma = 100.0, 100.0, 1.0, 0.05, 0.0, 0.2
        greeks = bsm_greeks(spot, strike, tau, rate, dividend, sigma)
        moved = float(bsm_price(spot, strike, tau, rate + BASIS_POINT, dividend, sigma))
        base = float(bsm_price(spot, strike, tau, rate, dividend, sigma))
        assert float(greeks.rho_per_bp) == pytest.approx(moved - base, rel=1e-3)

    def test_display_dict_names_its_units(self):
        payload = bsm_greeks(100.0, 100.0, 1.0, 0.05, 0.0, 0.2).to_dict()
        assert "vega_per_vol_point" in payload
        assert "theta_per_day" in payload
        assert "rho_per_bp" in payload
        # The raw partials are kept too, so nothing has to be back-derived.
        assert "vega_raw_per_unit_vol" in payload


class TestDegenerateInputs:
    def test_expired_option_greeks_are_limits_not_nan(self):
        greeks = bsm_greeks(120.0, 100.0, 0.0, 0.05, 0.0, 0.2, True)
        assert np.isfinite(greeks.delta)
        assert float(greeks.gamma) == 0.0
        assert float(greeks.vega) == 0.0

    def test_zero_volatility_greeks_are_finite(self):
        greeks = bsm_greeks(120.0, 100.0, 1.0, 0.05, 0.0, 0.0, True)
        for value in (greeks.price, greeks.delta, greeks.gamma, greeks.vega):
            assert np.isfinite(value)

    def test_expired_out_of_the_money_delta_is_zero(self):
        assert float(bsm_greeks(80.0, 100.0, 0.0, 0.05, 0.0, 0.2, True).delta) == 0.0


@pytest.mark.requires_reference_libs
class TestAgainstReferenceLibraries:
    @pytest.mark.parametrize("spot,strike,tau,rate,dividend,sigma", GRID)
    @pytest.mark.parametrize("flag", ["c", "p"])
    def test_price_matches_vollib(self, spot, strike, tau, rate, dividend, sigma, flag):
        ours = float(bsm_price(spot, strike, tau, rate, dividend, sigma, flag == "c"))
        theirs = vollib_bsm.black_scholes_merton(flag, spot, strike, tau, rate, sigma, dividend)
        assert ours == pytest.approx(theirs, abs=PRICE_VS_REFERENCE_ABS, rel=1e-12)

    @pytest.mark.parametrize("spot,strike,tau,rate,dividend,sigma", GRID)
    @pytest.mark.parametrize("flag", ["c", "p"])
    def test_greeks_match_vollib(self, spot, strike, tau, rate, dividend, sigma, flag):
        """vollib reports display units: vega per point, theta per day, rho per
        1%. Comparing against our scaled fields is also a check that our unit
        conventions are the conventional ones."""
        greeks = bsm_greeks(spot, strike, tau, rate, dividend, sigma, flag == "c")
        args = (flag, spot, strike, tau, rate, sigma, dividend)
        assert float(greeks.delta) == pytest.approx(vollib_greeks.delta(*args), abs=1e-10)
        assert float(greeks.gamma) == pytest.approx(vollib_greeks.gamma(*args), abs=1e-10)
        assert float(greeks.vega_per_vol_point) == pytest.approx(
            vollib_greeks.vega(*args), abs=1e-9
        )
        assert float(greeks.theta_per_day) == pytest.approx(vollib_greeks.theta(*args), abs=1e-9)
        # vollib's rho is per 1%, ours per basis point: a factor of 100.
        assert float(greeks.rho_per_bp) * 100.0 == pytest.approx(vollib_greeks.rho(*args), abs=1e-9)

    @pytest.mark.parametrize("spot,strike,tau,rate,dividend,sigma", GRID)
    @pytest.mark.parametrize("is_call", [True, False])
    def test_price_matches_quantlib(self, spot, strike, tau, rate, dividend, sigma, is_call):
        if ql is None:
            pytest.skip("QuantLib not installed")
        forward = spot * math.exp((rate - dividend) * tau)
        discount = math.exp(-rate * tau)
        option_type = ql.Option.Call if is_call else ql.Option.Put
        theirs = ql.blackFormula(option_type, strike, forward, sigma * math.sqrt(tau), discount)
        ours = float(bsm_price(spot, strike, tau, rate, dividend, sigma, is_call))
        assert ours == pytest.approx(theirs, abs=PRICE_VS_REFERENCE_ABS, rel=1e-12)


class TestVectorisation:
    def test_greeks_broadcast_over_a_chain(self):
        strikes = np.linspace(80.0, 120.0, 41)
        greeks = bsm_greeks(100.0, strikes, 0.25, 0.05, 0.0, 0.2, True)
        assert greeks.delta.shape == strikes.shape
        assert np.all(np.diff(greeks.delta) <= 1e-12), "call delta falls with strike"
        assert np.all(greeks.vega >= 0.0)

    def test_vectorised_matches_scalar(self):
        strikes = np.linspace(60.0, 160.0, 51)
        batch = bsm_greeks(100.0, strikes, 0.5, 0.03, 0.01, 0.25, True)
        scalar = [float(bsm_greeks(100.0, k, 0.5, 0.03, 0.01, 0.25, True).delta) for k in strikes]
        assert batch.delta == pytest.approx(scalar, rel=1e-15)


class TestMonotonicity:
    @given(
        spot=st.floats(min_value=1.0, max_value=1e4),
        strike=st.floats(min_value=1.0, max_value=1e4),
        tau=st.floats(min_value=1e-3, max_value=5.0),
        sigma=st.floats(min_value=1e-2, max_value=2.0),
    )
    @settings(max_examples=150, deadline=None)
    def test_call_delta_is_between_zero_and_the_carry_factor(self, spot, strike, tau, sigma):
        greeks = bsm_greeks(spot, strike, tau, 0.03, 0.01, sigma, True)
        assert 0.0 <= float(greeks.delta) <= math.exp(-0.01 * tau) + 1e-12

    @given(
        spot=st.floats(min_value=1.0, max_value=1e4),
        strike=st.floats(min_value=1.0, max_value=1e4),
        tau=st.floats(min_value=1e-3, max_value=5.0),
        sigma=st.floats(min_value=1e-2, max_value=2.0),
    )
    @settings(max_examples=150, deadline=None)
    def test_gamma_and_vega_are_non_negative(self, spot, strike, tau, sigma):
        greeks = bsm_greeks(spot, strike, tau, 0.03, 0.01, sigma, True)
        assert float(greeks.gamma) >= -1e-15
        assert float(greeks.vega) >= -1e-12
