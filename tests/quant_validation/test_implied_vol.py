"""Implied-volatility inversion.

The headline acceptance criterion is the round trip: price a known volatility,
solve it back, recover it. But a blanket tolerance would be dishonest, because
for a deep in-the-money option the price is nearly flat in volatility and *no*
algorithm can recover it to 1e-6 from a float64 price. So the criterion is
conditioning-aware: 1e-6 where the problem is well posed, and within a few
multiples of the solver's own reported uncertainty where it is not.

That the solver reports its conditioning at all is the point. An implied
volatility that reproduces its price exactly and is still uncertain in the
fifth decimal is a real thing, and the platform has to know which kind it has.
"""

from __future__ import annotations

import itertools
import math

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from quant.pricing.black76 import black76_price
from quant.pricing.black_scholes import bsm_price
from quant.volatility.implied import (
    MAX_VOL,
    MIN_VOL,
    ImpliedVolResult,
    IVFailure,
    implied_vol_black76,
    implied_vol_black76_batch,
    implied_vol_bsm,
)
from tests.tolerances import IV_ROUNDTRIP_ABS

FORWARDS = [100.0, 24000.0]
TAUS = [1.0 / 365.0, 7.0 / 365.0, 0.25, 1.0, 3.0]
SIGMAS = [0.05, 0.15, 0.40, 1.00, 2.50]
MONEYNESS = [0.6, 0.85, 0.95, 1.0, 1.05, 1.15, 1.5]

#: How many multiples of the solver's own reported uncertainty an ill-posed
#: recovery may miss by. The bound is a property of float64, not of the solver.
CONDITIONING_SLACK = 8.0


def roundtrip_cases():
    for forward, tau, sigma, ratio, is_call in itertools.product(
        FORWARDS, TAUS, SIGMAS, MONEYNESS, (True, False)
    ):
        yield forward, forward * ratio, tau, sigma, is_call


class TestRoundTrip:
    """price(sigma) -> solve -> sigma recovered."""

    @pytest.mark.parametrize(
        "forward,strike,tau,sigma,is_call",
        list(roundtrip_cases()),
        ids=lambda v: f"{v}",
    )
    def test_recovers_the_generating_volatility(self, forward, strike, tau, sigma, is_call):
        price = float(black76_price(forward, strike, tau, sigma, is_call))
        result = implied_vol_black76(price, forward, strike, tau, is_call)

        if result.implied_volatility is None:
            # Only ever for a structurally invertible-free quote, never a
            # silent failure on a healthy one.
            assert result.error in {
                IVFailure.NO_TIME_VALUE,
                IVFailure.NON_POSITIVE_PRICE,
            }
            return

        error = abs(result.implied_volatility - sigma)
        allowed = max(IV_ROUNDTRIP_ABS, CONDITIONING_SLACK * (result.uncertainty or 0.0))
        assert error <= allowed, (
            f"error {error:.3e} exceeds {allowed:.3e} "
            f"(uncertainty {result.uncertainty:.3e}, vega {result.vega:.3e})"
        )

    def test_well_conditioned_quotes_recover_to_the_headline_tolerance(self):
        """The acceptance criterion, on the quotes it is meant to describe."""
        checked = 0
        for forward, strike, tau, sigma, is_call in roundtrip_cases():
            price = float(black76_price(forward, strike, tau, sigma, is_call))
            result = implied_vol_black76(price, forward, strike, tau, is_call)
            if result.implied_volatility is None or not result.is_well_conditioned:
                continue
            checked += 1
            assert abs(result.implied_volatility - sigma) <= IV_ROUNDTRIP_ABS
        assert checked > 200, f"only {checked} well-conditioned cases exercised"

    @given(
        forward=st.floats(min_value=1.0, max_value=1e5),
        ratio=st.floats(min_value=0.5, max_value=2.0),
        tau=st.floats(min_value=1e-3, max_value=5.0),
        sigma=st.floats(min_value=0.02, max_value=2.0),
        is_call=st.booleans(),
    )
    @settings(max_examples=250, deadline=None)
    def test_round_trip_property(self, forward, ratio, tau, sigma, is_call):
        strike = forward * ratio
        price = float(black76_price(forward, strike, tau, sigma, is_call))
        result = implied_vol_black76(price, forward, strike, tau, is_call)
        if result.implied_volatility is None:
            return
        allowed = max(IV_ROUNDTRIP_ABS, CONDITIONING_SLACK * (result.uncertainty or 0.0))
        assert abs(result.implied_volatility - sigma) <= allowed


class TestSpotAndForwardAgree:
    @pytest.mark.parametrize("tau", [0.05, 0.25, 1.0])
    @pytest.mark.parametrize("rate", [0.0, 0.065])
    @pytest.mark.parametrize("dividend", [0.0, 0.02])
    @pytest.mark.parametrize("is_call", [True, False])
    def test_bsm_and_black76_imply_the_same_volatility(self, tau, rate, dividend, is_call):
        spot, strike, sigma = 100.0, 105.0, 0.25
        price = float(bsm_price(spot, strike, tau, rate, dividend, sigma, is_call))
        spot_result = implied_vol_bsm(price, spot, strike, tau, rate, dividend, is_call)

        forward = spot * math.exp((rate - dividend) * tau)
        discount = math.exp(-rate * tau)
        fwd_result = implied_vol_black76(price, forward, strike, tau, is_call, discount)

        assert spot_result.implied_volatility == pytest.approx(
            fwd_result.implied_volatility, abs=1e-12
        )
        assert spot_result.implied_volatility == pytest.approx(sigma, abs=IV_ROUNDTRIP_ABS)


class TestStructuredNonResults:
    """No answer is reported as a named reason, never as nan or a clipped value."""

    def test_expired_option(self):
        result = implied_vol_black76(5.0, 100.0, 100.0, 0.0, True)
        assert result.implied_volatility is None
        assert result.error is IVFailure.OPTION_EXPIRED

    def test_price_below_intrinsic(self):
        result = implied_vol_black76(1.0, 100.0, 80.0, 0.5, True)
        assert result.implied_volatility is None
        assert result.error is IVFailure.PRICE_BELOW_INTRINSIC

    def test_price_above_upper_bound(self):
        result = implied_vol_black76(150.0, 100.0, 100.0, 0.5, True)
        assert result.implied_volatility is None
        assert result.error is IVFailure.PRICE_ABOVE_BOUND

    def test_put_upper_bound_is_the_strike(self):
        result = implied_vol_black76(90.0, 100.0, 80.0, 0.5, False)
        assert result.implied_volatility is None
        assert result.error is IVFailure.PRICE_ABOVE_BOUND

    def test_price_at_intrinsic_has_no_time_value(self):
        result = implied_vol_black76(20.0, 100.0, 80.0, 0.5, True)
        assert result.implied_volatility is None
        assert result.error is IVFailure.NO_TIME_VALUE

    def test_non_positive_price(self):
        result = implied_vol_black76(0.0, 100.0, 120.0, 0.5, True)
        assert result.implied_volatility is None
        assert result.error is IVFailure.NON_POSITIVE_PRICE

    def test_invalid_inputs(self):
        assert implied_vol_black76(5.0, -100.0, 100.0, 0.5, True).error is (IVFailure.INVALID_INPUT)
        assert implied_vol_black76(5.0, 100.0, 0.0, 0.5, True).error is (IVFailure.INVALID_INPUT)

    def test_a_non_result_never_returns_nan(self):
        for result in (
            implied_vol_black76(5.0, 100.0, 100.0, 0.0, True),
            implied_vol_black76(1.0, 100.0, 80.0, 0.5, True),
        ):
            assert result.implied_volatility is None
            assert not result.converged


class TestSolverReporting:
    def test_reports_its_bracket_and_solver(self):
        price = float(black76_price(100.0, 100.0, 0.25, 0.2, True))
        result = implied_vol_black76(price, 100.0, 100.0, 0.25, True)
        assert result.lower_bound == MIN_VOL
        assert result.upper_bound == MAX_VOL
        assert result.solver in {"safeguarded-newton", "brent"}
        assert result.iterations >= 0
        assert result.converged

    def test_reports_vega_and_conditioning(self):
        price = float(black76_price(100.0, 100.0, 0.25, 0.2, True))
        result = implied_vol_black76(price, 100.0, 100.0, 0.25, True)
        assert result.vega > 0
        assert result.uncertainty is not None
        assert result.is_well_conditioned

    def test_a_deep_in_the_money_quote_is_flagged_ill_conditioned(self):
        """The price carries almost no volatility information here, and the
        solver says so rather than implying six good decimals."""
        price = float(bsm_price(100.0, 60.0, 1.0, 0.05, 0.0, 0.08, True))
        result = implied_vol_bsm(price, 100.0, 60.0, 1.0, 0.05, 0.0, True)
        assert result.implied_volatility is not None
        assert not result.is_well_conditioned
        assert result.uncertainty > 1e-6
        assert abs(result.implied_volatility - 0.08) <= CONDITIONING_SLACK * result.uncertainty

    def test_result_serialises_for_persistence(self):
        price = float(black76_price(100.0, 100.0, 0.25, 0.2, True))
        payload = implied_vol_black76(price, 100.0, 100.0, 0.25, True).to_dict()
        for key in ("implied_volatility", "converged", "iterations", "solver", "vega"):
            assert key in payload


class TestBatchSolver:
    def test_a_chain_solves_in_one_call(self):
        forward, tau = 24000.0, 0.25
        strikes = np.linspace(16000.0, 34000.0, 37)
        sigmas = 0.18 + 0.05 * np.abs(np.log(strikes / forward))
        prices = black76_price(forward, strikes, tau, sigmas, True)

        batch = implied_vol_black76_batch(
            prices,
            np.full(37, forward),
            strikes,
            np.full(37, tau),
            np.full(37, True),
        )
        assert len(batch) == 37
        assert batch.success_rate == 1.0
        recovered = batch.implied_volatility
        allowed = np.maximum(IV_ROUNDTRIP_ABS, CONDITIONING_SLACK * batch.uncertainty)
        assert np.all(np.abs(recovered - sigmas) <= allowed)

    def test_batch_matches_the_scalar_path(self):
        forward, tau = 100.0, 0.5
        strikes = np.linspace(70.0, 140.0, 15)
        prices = black76_price(forward, strikes, tau, 0.3, True)
        batch = implied_vol_black76_batch(
            prices, np.full(15, forward), strikes, np.full(15, tau), np.full(15, True)
        )
        for index, strike in enumerate(strikes):
            scalar = implied_vol_black76(float(prices[index]), forward, float(strike), tau, True)
            assert batch.implied_volatility[index] == pytest.approx(
                scalar.implied_volatility, abs=1e-14
            )

    def test_bad_quotes_do_not_poison_good_ones(self):
        """One unusable quote in a chain must not cost the other 999 their IV."""
        forward, tau = 100.0, 0.25
        strikes = np.array([90.0, 100.0, 110.0, 120.0])
        prices = np.array(
            [
                float(black76_price(forward, 90.0, tau, 0.2, True)),
                -1.0,  # non-positive
                float(black76_price(forward, 110.0, tau, 0.2, True)),
                500.0,  # above the upper bound
            ]
        )
        batch = implied_vol_black76_batch(
            prices, np.full(4, forward), strikes, np.full(4, tau), np.full(4, True)
        )
        assert batch.converged.tolist() == [True, False, True, False]
        assert batch.errors[1] is IVFailure.NON_POSITIVE_PRICE
        assert batch.errors[3] is IVFailure.PRICE_ABOVE_BOUND
        assert batch.implied_volatility[0] == pytest.approx(0.2, abs=IV_ROUNDTRIP_ABS)
        assert batch.implied_volatility[2] == pytest.approx(0.2, abs=IV_ROUNDTRIP_ABS)

    def test_mixed_calls_and_puts(self):
        forward, tau, sigma = 100.0, 0.25, 0.3
        strikes = np.array([80.0, 100.0, 120.0, 100.0])
        is_call = np.array([True, True, False, False])
        prices = black76_price(forward, strikes, tau, sigma, is_call)
        batch = implied_vol_black76_batch(
            prices, np.full(4, forward), strikes, np.full(4, tau), is_call
        )
        assert np.all(batch.converged)
        assert batch.implied_volatility == pytest.approx(sigma, abs=IV_ROUNDTRIP_ABS)

    def test_an_empty_chain_is_not_an_error(self):
        empty = np.array([])
        batch = implied_vol_black76_batch(empty, empty, empty, empty, np.array([], dtype=bool))
        assert len(batch) == 0
        assert batch.success_rate == 1.0

    def test_at(self):
        forward, tau = 100.0, 0.25
        prices = black76_price(forward, np.array([95.0, 105.0]), tau, 0.25, True)
        batch = implied_vol_black76_batch(
            prices, np.full(2, forward), np.array([95.0, 105.0]), np.full(2, tau), True
        )
        result = batch.at(1)
        assert isinstance(result, ImpliedVolResult)
        assert result.implied_volatility == pytest.approx(0.25, abs=IV_ROUNDTRIP_ABS)


@pytest.mark.requires_reference_libs
class TestAgainstVollib:
    """Cross-check against ``vollib``'s Let's-Be-Rational implementation.

    Note the caveat recorded in docs/references.md: in this environment
    ``py_vollib.black.implied_volatility`` returns ``inf`` or raises
    ``ZeroDivisionError`` on essentially every input, so the Black-Scholes entry
    point is used as the oracle and cases where it fails are skipped. This is
    precisely why the reuse decision for implied volatility is VALIDATE AGAINST
    rather than USE DIRECTLY: a wrapper would have shipped those failures.
    """

    @pytest.mark.parametrize(
        "tau,sigma,strike,rate,flag",
        list(
            itertools.product(
                (0.25, 1.0, 3.0),
                (0.15, 0.35, 0.8),
                (80.0, 100.0, 125.0),
                (0.0, 0.05),
                ("c", "p"),
            )
        ),
    )
    def test_matches_vollib_where_vollib_works(self, tau, sigma, strike, rate, flag):
        bs = pytest.importorskip("py_vollib.black_scholes")
        iv_module = pytest.importorskip("py_vollib.black_scholes.implied_volatility")

        price = bs.black_scholes(flag, 100.0, strike, tau, rate, sigma)
        try:
            theirs = iv_module.implied_volatility(price, 100.0, strike, tau, rate, flag)
        except Exception:
            pytest.skip("vollib's LBR implementation failed on this input")
        if not math.isfinite(theirs):
            pytest.skip("vollib returned a non-finite implied volatility")

        result = implied_vol_bsm(price, 100.0, strike, tau, rate, 0.0, flag == "c")
        assert result.implied_volatility is not None
        allowed = max(1e-6, CONDITIONING_SLACK * (result.uncertainty or 0.0))
        assert abs(result.implied_volatility - theirs) <= allowed

    def test_we_solve_cases_vollib_cannot(self):
        """Documented, not incidental: our solver must cover vollib's gaps."""
        black = pytest.importorskip("py_vollib.black")
        black_iv = pytest.importorskip("py_vollib.black.implied_volatility")

        price = black.black("c", 100.0, 100.0, 0.25, 0.0, 0.3)
        vollib_failed = False
        try:
            theirs = black_iv.implied_volatility(price, 100.0, 100.0, 0.25, 0.0, "c")
            vollib_failed = not math.isfinite(theirs)
        except Exception:
            vollib_failed = True

        ours = implied_vol_black76(price, 100.0, 100.0, 0.25, True)
        assert ours.implied_volatility == pytest.approx(0.3, abs=IV_ROUNDTRIP_ABS)
        if not vollib_failed:
            pytest.skip("vollib's Black entry point works in this environment")
