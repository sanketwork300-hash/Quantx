"""Order-of-convergence validation for the Crank-Nicolson PDE.

The Phase 9 acceptance criterion is not "the PDE is close to Black-Scholes" —
a coarse grid can be close by luck, and a wrong scheme can be close on one
contract. It is that the error falls at the *rate the scheme claims*: halving
the grid spacing must quarter the error. A first-order result would mean the
Rannacher start-up or the non-uniform second-difference operator is wrong even
though the price looks plausible.

Gamma is checked as well as price, because it is the quantity that exposes a
bad interpolation: the payoff kink at the strike makes the second derivative
the first thing a scheme gets wrong.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from quant.numerical.pde import (
    GridSpec,
    PDEError,
    convergence_order,
    solve_local_vol_pde,
)
from quant.pricing.black_scholes import bsm_greeks, bsm_price
from quant.volatility.local_vol import constant_local_volatility

SPOT = 100.0
RATE = 0.04
DIVIDEND = 0.015
SIGMA = 0.25

#: Successive refinements. Each doubles the nodes and the steps, so a
#: second-order scheme quarters the error at every step.
REFINEMENTS = [1, 2, 4, 8]
BASE_NODES = 101
BASE_STEPS = 40

#: Empirically the scheme lands at 2.00; anything at or above 1.8 is
#: second order in practice, and anything near 1.0 is a broken scheme.
MIN_ORDER = 1.8


def _errors(strike: float, tau: float, is_call: bool, concentration: float):
    exact = float(bsm_price(SPOT, strike, tau, RATE, DIVIDEND, SIGMA, is_call))
    reference = bsm_greeks(SPOT, strike, tau, RATE, DIVIDEND, SIGMA, is_call)
    local_vol = constant_local_volatility(SIGMA)

    price_errors, delta_errors, gamma_errors = [], [], []
    for factor in REFINEMENTS:
        spec = GridSpec(
            nodes=BASE_NODES * factor,
            steps=BASE_STEPS * factor,
            concentration=concentration,
        )
        result = solve_local_vol_pde(
            SPOT, strike, tau, RATE, DIVIDEND, local_vol, is_call, spec, SIGMA
        )
        price_errors.append(abs(result.price - exact))
        delta_errors.append(abs(result.delta - float(reference.delta)))
        gamma_errors.append(abs(result.gamma - float(reference.gamma)))
    return price_errors, delta_errors, gamma_errors


class TestOrderOfConvergence:
    @pytest.mark.parametrize("concentration", [0.0, 5.0], ids=["uniform", "concentrated"])
    def test_price_converges_at_second_order(self, concentration):
        errors, _, _ = _errors(100.0, 1.0, True, concentration)
        assert errors == sorted(errors, reverse=True), "refinement must reduce the error"
        assert convergence_order(errors, REFINEMENTS) >= MIN_ORDER

    @pytest.mark.parametrize("concentration", [0.0, 5.0], ids=["uniform", "concentrated"])
    def test_delta_converges_at_second_order(self, concentration):
        _, errors, _ = _errors(100.0, 1.0, True, concentration)
        assert convergence_order(errors, REFINEMENTS) >= MIN_ORDER

    @pytest.mark.parametrize("concentration", [0.0, 5.0], ids=["uniform", "concentrated"])
    def test_gamma_converges_at_second_order(self, concentration):
        """The strictest of the three: a quadratic interpolation through the
        solution would give a second derivative accurate only to O(h) and this
        test would report order one."""
        _, _, errors = _errors(100.0, 1.0, True, concentration)
        assert convergence_order(errors, REFINEMENTS) >= MIN_ORDER


class TestAgainstBlackScholes:
    @pytest.mark.parametrize("strike", [80.0, 100.0, 125.0])
    @pytest.mark.parametrize("tau", [0.1, 1.0])
    @pytest.mark.parametrize("is_call", [True, False])
    def test_constant_local_volatility_reproduces_the_closed_form(self, strike, tau, is_call):
        exact = float(bsm_price(SPOT, strike, tau, RATE, DIVIDEND, SIGMA, is_call))
        result = solve_local_vol_pde(
            SPOT,
            strike,
            tau,
            RATE,
            DIVIDEND,
            constant_local_volatility(SIGMA),
            is_call,
            GridSpec(nodes=801, steps=400),
            SIGMA,
        )
        assert result.price == pytest.approx(exact, abs=2e-3, rel=2e-4)

    def test_put_call_parity_holds_on_the_grid(self):
        """Parity is a property of the *scheme*, not of the model: both sides
        are solved on the same grid with the same boundaries, so a violation
        would be a discretisation error the price alone would not reveal."""
        strike, tau = 105.0, 0.75
        spec = GridSpec(nodes=601, steps=300)
        local_vol = constant_local_volatility(SIGMA)
        call = solve_local_vol_pde(
            SPOT, strike, tau, RATE, DIVIDEND, local_vol, True, spec, SIGMA
        ).price
        put = solve_local_vol_pde(
            SPOT, strike, tau, RATE, DIVIDEND, local_vol, False, spec, SIGMA
        ).price
        parity = SPOT * math.exp(-DIVIDEND * tau) - strike * math.exp(-RATE * tau)
        assert (call - put) == pytest.approx(parity, abs=1e-4)


class TestRefusals:
    @pytest.mark.parametrize(
        "spot,strike,tau",
        [(0.0, 100.0, 1.0), (100.0, 0.0, 1.0), (100.0, 100.0, 0.0), (100.0, 100.0, -1.0)],
    )
    def test_a_degenerate_contract_is_refused_rather_than_priced(self, spot, strike, tau):
        with pytest.raises(PDEError):
            solve_local_vol_pde(spot, strike, tau, RATE, DIVIDEND, constant_local_volatility(SIGMA))

    def test_a_non_positive_local_volatility_at_the_spot_is_refused(self):
        with pytest.raises(PDEError):
            solve_local_vol_pde(
                SPOT,
                100.0,
                1.0,
                RATE,
                DIVIDEND,
                lambda spot, tau: np.zeros_like(np.asarray(spot, dtype=float)),
            )

    def test_a_coarse_grid_says_so(self):
        result = solve_local_vol_pde(
            SPOT,
            100.0,
            1.0,
            RATE,
            DIVIDEND,
            constant_local_volatility(SIGMA),
            spec=GridSpec(nodes=41, steps=10),
            reference_volatility=SIGMA,
        )
        assert "PDE_COARSE_GRID" in result.warnings


class TestDupireConsistency:
    """A local-volatility surface derived from an implied surface must reprice
    that implied surface. This is the round trip the whole Dupire construction
    rests on, and it is what caught two real errors: a fixed maturity forward
    used at every time step, and a variance term structure clamped flat below
    the first expiry rather than run down to zero at the origin."""

    @pytest.mark.parametrize("strike", [85.0, 100.0, 115.0])
    @pytest.mark.parametrize("tau", [0.25, 1.0])
    def test_the_pde_reprices_the_surface_it_was_derived_from(self, strike, tau):
        from quant.volatility.local_vol import SurfaceLocalVol
        from quant.volatility.ssvi import SSVIParameters, SSVISurface, ThetaTermStructure

        surface = SSVISurface(
            parameters=SSVIParameters(rho=-0.6, eta=1.0, gamma=0.45),
            term_structure=ThetaTermStructure(
                maturities=(0.1, 0.5, 1.0, 2.0), thetas=(0.004, 0.02, 0.04, 0.08)
            ),
        )
        forward = SPOT * math.exp((RATE - DIVIDEND) * tau)
        sigma = float(surface.implied_volatility(math.log(strike / forward), tau))
        expected = float(bsm_price(SPOT, strike, tau, RATE, DIVIDEND, sigma, True))

        result = solve_local_vol_pde(
            SPOT,
            strike,
            tau,
            RATE,
            DIVIDEND,
            SurfaceLocalVol(surface=surface, spot=SPOT, carry=RATE - DIVIDEND, fallback=sigma),
            True,
            GridSpec(nodes=801, steps=400),
            sigma,
        )
        assert result.price == pytest.approx(expected, rel=2e-3)
