"""Black-76 correctness.

Validated three ways: against closed-form identities we can state independently,
against ``vollib`` (an independent implementation following Jaeckel), and
against QuantLib. Two libraries that agree with us to 1e-12 is a far stronger
statement than either one alone.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from quant.pricing.black76 import black76_price, forward_d1_d2
from tests.tolerances import PARITY_ABS, PRICE_VS_REFERENCE_ABS

vollib_black = pytest.importorskip("py_vollib.black", reason="reference library not installed")
try:
    import QuantLib as ql
except ImportError:  # pragma: no cover - optional
    ql = None

GRID = [
    (100.0, 100.0, 1.00, 0.20),
    (100.0, 80.0, 0.25, 0.35),
    (100.0, 120.0, 0.25, 0.35),
    (24000.0, 24000.0, 0.0833, 0.14),
    (24000.0, 20000.0, 0.5000, 0.22),
    (24000.0, 30000.0, 0.5000, 0.30),
    (1.20, 1.25, 2.0, 0.09),
    (50000.0, 45000.0, 0.0027, 0.85),
]


class TestIdentities:
    @pytest.mark.parametrize("forward,strike,tau,sigma", GRID)
    def test_put_call_parity(self, forward, strike, tau, sigma):
        call = float(black76_price(forward, strike, tau, sigma, True))
        put = float(black76_price(forward, strike, tau, sigma, False))
        assert call - put == pytest.approx(forward - strike, abs=PARITY_ABS)

    @pytest.mark.parametrize("forward,strike,tau,sigma", GRID)
    def test_discounting_is_a_pure_scaling(self, forward, strike, tau, sigma):
        discount = math.exp(-0.05 * tau)
        undiscounted = float(black76_price(forward, strike, tau, sigma, True))
        discounted = float(
            black76_price(forward, strike, tau, sigma, True, discount_factor=discount)
        )
        assert discounted == pytest.approx(undiscounted * discount, rel=1e-14)

    @pytest.mark.parametrize("forward,strike,tau,sigma", GRID)
    def test_prices_respect_no_arbitrage_bounds(self, forward, strike, tau, sigma):
        call = float(black76_price(forward, strike, tau, sigma, True))
        put = float(black76_price(forward, strike, tau, sigma, False))
        assert max(forward - strike, 0.0) - 1e-12 <= call <= forward + 1e-12
        assert max(strike - forward, 0.0) - 1e-12 <= put <= strike + 1e-12

    def test_zero_time_collapses_to_intrinsic(self):
        assert float(black76_price(120.0, 100.0, 0.0, 0.2, True)) == pytest.approx(20.0)
        assert float(black76_price(80.0, 100.0, 0.0, 0.2, True)) == pytest.approx(0.0)
        assert float(black76_price(80.0, 100.0, 0.0, 0.2, False)) == pytest.approx(20.0)

    def test_zero_volatility_collapses_to_intrinsic(self):
        assert float(black76_price(120.0, 100.0, 1.0, 0.0, True)) == pytest.approx(20.0)
        assert float(black76_price(80.0, 100.0, 1.0, 0.0, True)) == pytest.approx(0.0)

    def test_degenerate_inputs_never_produce_nan(self):
        prices = black76_price(
            np.array([100.0, 100.0, 100.0]),
            np.array([100.0, 90.0, 110.0]),
            np.array([0.0, 0.0, 0.0]),
            np.array([0.0, 0.0, 0.0]),
            True,
        )
        assert np.all(np.isfinite(prices))

    def test_d1_d2_relationship(self):
        d1, d2 = forward_d1_d2(100.0, 95.0, 0.5, 0.25)
        assert float(d1 - d2) == pytest.approx(0.25 * math.sqrt(0.5), rel=1e-14)


class TestMonotonicity:
    @given(
        forward=st.floats(min_value=1.0, max_value=1e5),
        strike=st.floats(min_value=1.0, max_value=1e5),
        tau=st.floats(min_value=1e-4, max_value=5.0),
        sigma=st.floats(min_value=1e-3, max_value=2.0),
    )
    @settings(max_examples=200, deadline=None)
    def test_call_increases_with_forward_and_decreases_with_strike(
        self, forward, strike, tau, sigma
    ):
        base = float(black76_price(forward, strike, tau, sigma, True))
        higher_forward = float(black76_price(forward * 1.01, strike, tau, sigma, True))
        higher_strike = float(black76_price(forward, strike * 1.01, tau, sigma, True))
        assert higher_forward >= base - 1e-9
        assert higher_strike <= base + 1e-9

    @given(
        forward=st.floats(min_value=1.0, max_value=1e5),
        strike=st.floats(min_value=1.0, max_value=1e5),
        tau=st.floats(min_value=1e-4, max_value=5.0),
        sigma=st.floats(min_value=1e-3, max_value=2.0),
    )
    @settings(max_examples=200, deadline=None)
    def test_put_increases_with_strike(self, forward, strike, tau, sigma):
        base = float(black76_price(forward, strike, tau, sigma, False))
        higher = float(black76_price(forward, strike * 1.01, tau, sigma, False))
        assert higher >= base - 1e-9

    @given(
        forward=st.floats(min_value=1.0, max_value=1e5),
        strike=st.floats(min_value=1.0, max_value=1e5),
        tau=st.floats(min_value=1e-4, max_value=5.0),
        sigma=st.floats(min_value=1e-3, max_value=2.0),
    )
    @settings(max_examples=200, deadline=None)
    def test_value_is_never_negative(self, forward, strike, tau, sigma):
        assert float(black76_price(forward, strike, tau, sigma, True)) >= 0.0
        assert float(black76_price(forward, strike, tau, sigma, False)) >= 0.0


@pytest.mark.requires_reference_libs
class TestAgainstReferenceLibraries:
    @pytest.mark.parametrize("forward,strike,tau,sigma", GRID)
    @pytest.mark.parametrize("flag", ["c", "p"])
    def test_matches_vollib(self, forward, strike, tau, sigma, flag):
        ours = float(black76_price(forward, strike, tau, sigma, flag == "c"))
        theirs = vollib_black.black(flag, forward, strike, tau, 0.0, sigma)
        assert ours == pytest.approx(theirs, abs=PRICE_VS_REFERENCE_ABS, rel=1e-12)

    @pytest.mark.parametrize("forward,strike,tau,sigma", GRID)
    @pytest.mark.parametrize("is_call", [True, False])
    def test_matches_quantlib(self, forward, strike, tau, sigma, is_call):
        if ql is None:
            pytest.skip("QuantLib not installed")
        option_type = ql.Option.Call if is_call else ql.Option.Put
        theirs = ql.blackFormula(option_type, strike, forward, sigma * math.sqrt(tau), 1.0)
        ours = float(black76_price(forward, strike, tau, sigma, is_call))
        assert ours == pytest.approx(theirs, abs=PRICE_VS_REFERENCE_ABS, rel=1e-12)


class TestVectorisation:
    def test_broadcasts_over_a_chain(self):
        strikes = np.linspace(80.0, 120.0, 41)
        prices = black76_price(100.0, strikes, 0.25, 0.2, True)
        assert prices.shape == strikes.shape
        assert np.all(np.diff(prices) <= 1e-12), "call price must fall with strike"

    def test_mixed_call_and_put_mask(self):
        strikes = np.array([90.0, 100.0, 110.0])
        is_call = np.array([True, False, True])
        prices = black76_price(100.0, strikes, 0.25, 0.2, is_call)
        expected = [
            float(black76_price(100.0, k, 0.25, 0.2, c))
            for k, c in zip(strikes, is_call, strict=True)
        ]
        assert prices == pytest.approx(expected)

    def test_vectorised_matches_scalar(self):
        strikes = np.linspace(50.0, 150.0, 101)
        vector = black76_price(100.0, strikes, 0.75, 0.3, True)
        scalar = [float(black76_price(100.0, k, 0.75, 0.3, True)) for k in strikes]
        assert vector == pytest.approx(scalar, rel=1e-15)
