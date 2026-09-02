"""Forward estimation."""

from __future__ import annotations

import math

import numpy as np
import pytest

from domains.derivatives.forward import (
    MIN_PARITY_PAIRS,
    ForwardEstimator,
    ForwardFailure,
    ForwardMethod,
)
from quant.pricing.black76 import black76_price

TAU, RATE, SIGMA = 0.25, 0.065, 0.18
FORWARD = 24392.0
DISCOUNT = math.exp(-RATE * TAU)
STRIKES = np.arange(22000.0, 27000.0, 200.0)


def parity_inputs(noise: float = 0.0, seed: int = 7):
    calls = black76_price(FORWARD, STRIKES, TAU, SIGMA, True, DISCOUNT)
    puts = black76_price(FORWARD, STRIKES, TAU, SIGMA, False, DISCOUNT)
    if noise:
        rng = np.random.default_rng(seed)
        calls = calls + rng.normal(0.0, noise, size=calls.shape)
        puts = puts + rng.normal(0.0, noise, size=puts.shape)
    return STRIKES, calls, puts


class TestPutCallParity:
    def test_recovers_forward_and_discount_factor_exactly(self):
        """No rate or dividend assumption enters: both come out of the option
        market itself. That is why this estimator outranks spot-carry."""
        strikes, calls, puts = parity_inputs()
        estimate = ForwardEstimator.from_put_call_parity(strikes, calls, puts)
        assert estimate.value == pytest.approx(FORWARD, rel=1e-10)
        assert estimate.discount_factor == pytest.approx(DISCOUNT, rel=1e-10)
        assert estimate.method is ForwardMethod.PUT_CALL_PARITY
        assert estimate.observations == len(strikes)
        assert estimate.residual_error < 1e-8

    def test_confidence_falls_as_quotes_get_noisier(self):
        clean = ForwardEstimator.from_put_call_parity(*parity_inputs(), price_scale=1.0)
        noisy = ForwardEstimator.from_put_call_parity(*parity_inputs(noise=5.0), price_scale=1.0)
        assert clean.confidence > noisy.confidence
        assert noisy.residual_error > clean.residual_error

    def test_residual_is_judged_against_the_spread_scale(self):
        """A fit inside the bid/ask noise is a good fit."""
        inputs = parity_inputs(noise=2.0)
        tight = ForwardEstimator.from_put_call_parity(*inputs, price_scale=0.1)
        wide = ForwardEstimator.from_put_call_parity(*inputs, price_scale=10.0)
        assert wide.confidence > tight.confidence

    def test_weights_are_honoured(self):
        strikes, calls, puts = parity_inputs()
        corrupted = np.array(calls, dtype=float)
        corrupted[0] += 500.0
        weights = np.ones_like(strikes)
        weights[0] = 0.0

        weighted = ForwardEstimator.from_put_call_parity(strikes, corrupted, puts, weights)
        unweighted = ForwardEstimator.from_put_call_parity(strikes, corrupted, puts)
        assert abs(weighted.value - FORWARD) < abs(unweighted.value - FORWARD)
        assert weighted.observations == len(strikes) - 1

    def test_too_few_pairs(self):
        estimate = ForwardEstimator.from_put_call_parity([100.0, 110.0], [5.0, 2.0], [2.0, 5.0])
        assert estimate.value is None
        assert estimate.error is ForwardFailure.INSUFFICIENT_PAIRS
        assert estimate.observations < MIN_PARITY_PAIRS

    def test_a_degenerate_regression_is_refused(self):
        """A recovered discount factor outside a plausible range is not a
        discount factor, and returning the forward it implies would be worse
        than returning nothing."""
        strikes = np.array([100.0, 110.0, 120.0, 130.0])
        estimate = ForwardEstimator.from_put_call_parity(strikes, np.full(4, 5.0), np.full(4, 5.0))
        assert estimate.value is None
        assert estimate.error is ForwardFailure.DEGENERATE_REGRESSION

    def test_non_finite_rows_are_dropped_not_propagated(self):
        strikes, calls, puts = parity_inputs()
        calls = np.array(calls, dtype=float)
        calls[3] = np.nan
        estimate = ForwardEstimator.from_put_call_parity(strikes, calls, puts)
        assert estimate.value == pytest.approx(FORWARD, rel=1e-9)
        assert estimate.observations == len(strikes) - 1


class TestSpotCarry:
    def test_formula(self):
        estimate = ForwardEstimator.from_spot_carry(24000.0, TAU, RATE, 0.01)
        assert estimate.value == pytest.approx(24000.0 * math.exp((RATE - 0.01) * TAU))
        assert estimate.discount_factor == pytest.approx(DISCOUNT)

    def test_an_assumed_dividend_caps_confidence(self):
        assumed = ForwardEstimator.from_spot_carry(24000.0, TAU, RATE, 0.0, True)
        observed = ForwardEstimator.from_spot_carry(24000.0, TAU, RATE, 0.0, False)
        assert assumed.confidence < observed.confidence
        assert "dividend_yield_assumed" in assumed.assumptions
        assert "dividend_yield_assumed" not in observed.assumptions

    def test_assumptions_are_recorded_with_their_values(self):
        estimate = ForwardEstimator.from_spot_carry(24000.0, TAU, 0.065, 0.012)
        assert "risk_free_rate=0.065000" in estimate.assumptions
        assert "dividend_yield=0.012000" in estimate.assumptions

    def test_no_spot(self):
        estimate = ForwardEstimator.from_spot_carry(None, TAU, RATE, 0.0)
        assert estimate.error is ForwardFailure.NO_SPOT

    def test_non_positive_time(self):
        estimate = ForwardEstimator.from_spot_carry(24000.0, 0.0, RATE, 0.0)
        assert estimate.error is ForwardFailure.NON_POSITIVE_TIME


class TestFuture:
    def test_matching_expiry_is_high_confidence(self):
        estimate = ForwardEstimator.from_future(24390.0, TAU, TAU, DISCOUNT)
        assert estimate.value == 24390.0
        assert estimate.confidence > 0.9

    def test_a_mismatched_expiry_is_refused_rather_than_basis_adjusted(self):
        """An unmodelled basis is a silent error in every strike's
        log-moneyness."""
        estimate = ForwardEstimator.from_future(24390.0, TAU + 0.25, TAU, DISCOUNT)
        assert estimate.value is None
        assert estimate.error is ForwardFailure.NO_FUTURE
        assert "basis not modelled" in estimate.assumptions[0]

    def test_no_future(self):
        assert ForwardEstimator.from_future(None, TAU, TAU).error is ForwardFailure.NO_FUTURE


class TestSelection:
    def test_highest_confidence_wins(self):
        parity = ForwardEstimator.from_put_call_parity(*parity_inputs(), price_scale=1.0)
        carry = ForwardEstimator.from_spot_carry(24000.0, TAU, RATE, 0.0)
        future = ForwardEstimator.from_future(24390.0, TAU, TAU, DISCOUNT)
        selected = ForwardEstimator.select([parity, carry, future])
        assert selected.selected.method is ForwardMethod.FUTURE

    def test_every_estimate_is_retained(self):
        carry = ForwardEstimator.from_spot_carry(24000.0, TAU, RATE, 0.0)
        failed = ForwardEstimator.from_future(None, TAU, TAU)
        result = ForwardEstimator.select([carry, failed])
        assert len(result.estimates) == 2
        assert result.selected is carry

    def test_disagreement_is_surfaced_not_averaged(self):
        """Two estimators 3% apart mean an input is wrong; their average would
        destroy that signal."""
        carry = ForwardEstimator.from_spot_carry(24000.0, TAU, RATE, 0.0)
        future = ForwardEstimator.from_future(25000.0, TAU, TAU, DISCOUNT)
        result = ForwardEstimator.select([carry, future])
        assert result.disagreement == pytest.approx((25000.0 - carry.value) / carry.value, rel=1e-9)
        assert result.selected.value == 25000.0, "no averaging"

    def test_no_usable_estimate(self):
        failed = ForwardEstimator.from_future(None, TAU, TAU)
        result = ForwardEstimator.select([failed])
        assert result.selected is None
        assert result.disagreement is None

    def test_serialises_for_persistence(self):
        result = ForwardEstimator.select(
            [ForwardEstimator.from_spot_carry(24000.0, TAU, RATE, 0.0)]
        )
        payload = result.to_dict()
        assert payload["selected"]["method"] == "SPOT_CARRY"
        assert len(payload["estimates"]) == 1
