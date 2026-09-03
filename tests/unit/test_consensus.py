"""Model consensus, higher-order Greeks and the implied density."""

from __future__ import annotations

import json
import math

import numpy as np
import pytest

from domains.derivatives.consensus import (
    AGREEMENT_REFERENCE_DISPERSION,
    ConfidenceContribution,
    ConsensusInputs,
    ConsensusWarningCode,
    ModelConsensusService,
    PricingModelKind,
)
from quant.numerical.pde import GridSpec
from quant.pricing.black_scholes import bsm_greeks, bsm_price
from quant.pricing.heston import HestonParameters
from quant.pricing.higher_order import HIGHER_ORDER_UNITS, bsm_higher_order_greeks
from quant.pricing.monte_carlo import MonteCarloError, monte_carlo_price
from quant.volatility.density import DensityFlag, risk_neutral_density
from quant.volatility.local_vol import SurfaceLocalVol
from quant.volatility.ssvi import SSVIParameters, SSVISurface, ThetaTermStructure

SPOT, STRIKE, TAU, RATE, DIVIDEND = 100.0, 100.0, 1.0, 0.03, 0.01
SURFACE = SSVISurface(
    parameters=SSVIParameters(rho=-0.6, eta=1.0, gamma=0.45),
    term_structure=ThetaTermStructure(
        maturities=(0.1, 0.5, 1.0, 2.0), thetas=(0.004, 0.02, 0.04, 0.08)
    ),
)
FORWARD = SPOT * math.exp((RATE - DIVIDEND) * TAU)
SIGMA = float(SURFACE.implied_volatility(math.log(STRIKE / FORWARD), TAU))


def _inputs(**overrides) -> ConsensusInputs:
    base = {
        "spot": SPOT,
        "strike": STRIKE,
        "tau": TAU,
        "rate": RATE,
        "dividend": DIVIDEND,
        "is_call": True,
        "reference_volatility": SIGMA,
        "local_volatility": SurfaceLocalVol(
            surface=SURFACE, spot=SPOT, carry=RATE - DIVIDEND, fallback=SIGMA
        ),
        "heston": HestonParameters(v0=0.04, kappa=1.5, theta=0.04, xi=0.3, rho=-0.6),
        "grid": GridSpec(nodes=201, steps=100),
        "paths": 20_000,
    }
    return ConsensusInputs(**{**base, **overrides})


@pytest.fixture(scope="module")
def full():
    return ModelConsensusService().price(_inputs(), market_price=8.9)


class TestNoSinglePrice:
    def test_the_median_is_bracketed_by_the_range(self, full):
        low, high = full.reference_range
        assert low <= full.reference_value <= high
        assert full.dispersion_absolute == pytest.approx(high - low)

    def test_every_model_is_listed_with_its_own_value(self, full):
        assert full.models_requested == 4
        assert {value.model for value in full.values} == set(PricingModelKind)
        for value in full.values:
            assert value.model_version
            assert value.method

    def test_there_is_no_field_that_could_hold_a_verdict(self, full):
        payload = json.dumps(full.to_dict())
        for banned in ("best_model", "true_price", "fair_value", "recommendation"):
            assert banned not in payload

    def test_dispersion_is_reported_both_ways(self, full):
        assert full.dispersion_absolute > 0
        assert full.dispersion_relative == pytest.approx(
            full.dispersion_absolute / abs(full.reference_value)
        )
        assert full.standard_deviation > 0

    def test_the_three_models_sharing_a_surface_agree(self, full):
        """Black-Scholes at the surface's volatility, the Dupire PDE built from
        that surface and a simulation under it are three routes to the same
        number. A wide gap between them is a bug, not model risk."""
        by_model = {value.model: value.value for value in full.values}
        reference = by_model[PricingModelKind.BLACK_SCHOLES_MERTON]
        for kind in (PricingModelKind.LOCAL_VOL_PDE, PricingModelKind.MONTE_CARLO):
            assert abs(by_model[kind] - reference) / reference < 0.02

    def test_the_market_price_is_kept_apart_from_every_model_value(self, full):
        assert full.market_price == 8.9
        assert full.market_deviation == pytest.approx(8.9 - full.reference_value)
        assert full.market_deviation_relative == pytest.approx(
            full.market_deviation / abs(full.reference_value)
        )


class TestUnavailability:
    def test_a_model_with_no_inputs_reports_a_reason_and_the_rest_continue(self):
        result = ModelConsensusService().price(_inputs(heston=None))
        heston = next(v for v in result.values if v.model is PricingModelKind.HESTON)
        assert heston.value is None
        assert "not defaulted" in heston.unavailable_reason
        assert result.models_available == 3
        assert result.reference_value is not None

    def test_the_reduced_model_count_shows_in_the_confidence(self):
        service = ModelConsensusService()
        complete = service.price(_inputs())
        reduced = service.price(_inputs(heston=None, local_volatility=None))
        counts = {
            c.name: c.score
            for result in (complete, reduced)
            for c in result.confidence.contributions
            if c.name == "model_count"
        }
        del counts
        by_name = {c.name: c for c in reduced.confidence.contributions}
        assert by_name["model_count"].score == pytest.approx(0.5)

    def test_no_model_at_all_is_an_absence_not_a_zero(self):
        result = ModelConsensusService().price(
            _inputs(reference_volatility=None, local_volatility=None, heston=None)
        )
        assert result.reference_value is None
        assert result.reference_range is None
        assert result.models_available == 0
        assert result.confidence.score == 0.0
        codes = {warning.code for warning in result.warnings}
        assert ConsensusWarningCode.NO_MODEL_PRODUCED_A_VALUE in codes

    def test_one_model_says_that_zero_dispersion_is_not_agreement(self):
        result = ModelConsensusService().price(
            _inputs(), models=(PricingModelKind.BLACK_SCHOLES_MERTON,)
        )
        assert result.dispersion_absolute == 0.0
        codes = {warning.code for warning in result.warnings}
        assert ConsensusWarningCode.SINGLE_MODEL in codes
        names = {c.name for c in result.confidence.contributions}
        assert "model_agreement" not in names, "agreement over one model is not measurable"


class TestConfidence:
    def test_a_score_always_comes_with_its_contributions(self, full):
        assert 0.0 <= full.confidence.score <= 1.0
        assert full.confidence.contributions
        for contribution in full.confidence.contributions:
            assert contribution.basis
            assert 0.0 <= contribution.score <= 1.0
            assert contribution.weight >= 0.0
        assert full.confidence.weakest is not None

    def test_agreement_saturates_rather_than_hitting_zero(self):
        """A linear ramp would score a 5.1% spread and a 50% spread identically
        at zero, and the geometric mean would collapse the whole confidence for
        both. The ratio penalty distinguishes them."""
        service = ModelConsensusService()
        close = service.price(_inputs(heston=None))
        wide = service.price(
            _inputs(heston=HestonParameters(v0=0.09, kappa=1.0, theta=0.09, xi=0.5, rho=-0.7))
        )
        wide_agreement = next(
            c for c in wide.confidence.contributions if c.name == "model_agreement"
        )
        close_agreement = next(
            c for c in close.confidence.contributions if c.name == "model_agreement"
        )
        assert wide.dispersion_relative > AGREEMENT_REFERENCE_DISPERSION
        assert 0.0 < wide_agreement.score < close_agreement.score

    def test_one_bad_dimension_pulls_the_whole_score_down(self):
        result = ModelConsensusService().price(
            _inputs(),
            external_contributions=(
                ConfidenceContribution(
                    name="market_data_quality", score=0.0, weight=1.0, basis="crossed market"
                ),
            ),
        )
        assert result.confidence.score == 0.0
        assert result.confidence.weakest.name == "market_data_quality"

    def test_external_contributions_are_listed_by_name(self):
        result = ModelConsensusService().price(
            _inputs(),
            external_contributions=(
                ConfidenceContribution(
                    name="quote_age", score=0.7, weight=1.0, basis="eleven minutes old"
                ),
            ),
        )
        names = {c.name for c in result.confidence.contributions}
        assert "quote_age" in names


class TestReproducibility:
    def test_the_same_seed_gives_the_same_consensus(self):
        service = ModelConsensusService()
        first = service.price(_inputs(seed=7))
        second = service.price(_inputs(seed=7))
        assert first.reference_value == second.reference_value
        assert [v.value for v in first.values] == [v.value for v in second.values]

    def test_a_different_seed_moves_only_the_simulation(self):
        service = ModelConsensusService()
        first = {v.model: v.value for v in service.price(_inputs(seed=1)).values}
        second = {v.model: v.value for v in service.price(_inputs(seed=2)).values}
        assert first[PricingModelKind.MONTE_CARLO] != second[PricingModelKind.MONTE_CARLO]
        for kind in (PricingModelKind.BLACK_SCHOLES_MERTON, PricingModelKind.HESTON):
            assert first[kind] == second[kind]


class TestMonteCarlo:
    def test_the_control_variate_reduces_the_variance(self):
        plain = monte_carlo_price(
            SPOT, STRIKE, TAU, RATE, DIVIDEND, 0.2, paths=40_000, control_variate=False
        )
        controlled = monte_carlo_price(
            SPOT, STRIKE, TAU, RATE, DIVIDEND, 0.2, paths=40_000, control_variate=True
        )
        assert controlled.standard_error < plain.standard_error / 2
        assert controlled.variance_reduction > 0.5

    def test_the_estimate_brackets_the_closed_form(self):
        exact = float(bsm_price(SPOT, STRIKE, TAU, RATE, DIVIDEND, 0.2, True))
        result = monte_carlo_price(SPOT, STRIKE, TAU, RATE, DIVIDEND, 0.2, paths=200_000)
        low, high = result.confidence_interval
        assert low < exact < high

    @pytest.mark.parametrize("kwargs", [{"spot": 0.0}, {"tau": 0.0}, {"sigma": 0.0}, {"paths": 1}])
    def test_a_degenerate_request_is_refused(self, kwargs):
        arguments = {
            "spot": SPOT,
            "strike": STRIKE,
            "tau": TAU,
            "rate": RATE,
            "dividend": DIVIDEND,
            "sigma": 0.2,
            **kwargs,
        }
        with pytest.raises(MonteCarloError):
            monte_carlo_price(**arguments)


class TestHigherOrderGreeks:
    def test_they_match_finite_differences_of_the_first_order_greeks(self):
        sigma, h = 0.25, 1e-5
        up = bsm_greeks(SPOT, STRIKE, TAU, RATE, DIVIDEND, sigma + h, True)
        down = bsm_greeks(SPOT, STRIKE, TAU, RATE, DIVIDEND, sigma - h, True)
        analytic = bsm_higher_order_greeks(SPOT, STRIKE, TAU, RATE, DIVIDEND, sigma, True)

        assert float(analytic.vanna) == pytest.approx(
            float(up.delta - down.delta) / (2 * h), rel=1e-6
        )
        assert float(analytic.volga) == pytest.approx(
            float(up.vega - down.vega) / (2 * h), rel=1e-6
        )

    def test_vanna_and_volga_are_the_same_for_a_call_and_a_put(self):
        call = bsm_higher_order_greeks(SPOT, STRIKE, TAU, RATE, DIVIDEND, 0.25, True)
        put = bsm_higher_order_greeks(SPOT, STRIKE, TAU, RATE, DIVIDEND, 0.25, False)
        assert float(call.vanna) == pytest.approx(float(put.vanna))
        assert float(call.volga) == pytest.approx(float(put.volga))

    def test_charm_differs_between_a_call_and_a_put_by_the_dividend_term(self):
        call = bsm_higher_order_greeks(SPOT, STRIKE, TAU, RATE, DIVIDEND, 0.25, True)
        put = bsm_higher_order_greeks(SPOT, STRIKE, TAU, RATE, DIVIDEND, 0.25, False)
        difference = float(call.charm_per_year) - float(put.charm_per_year)
        assert difference == pytest.approx(DIVIDEND * math.exp(-DIVIDEND * TAU))

    def test_every_quantity_states_its_unit(self):
        greeks = bsm_higher_order_greeks(SPOT, STRIKE, TAU, RATE, DIVIDEND, 0.25, True)
        payload = greeks.to_dict()
        assert set(payload["units"]) == {
            "vanna_per_vol_point",
            "volga_per_vol_point",
            "charm_per_day",
        }
        assert float(greeks.vanna_per_vol_point) == pytest.approx(float(greeks.vanna) * 0.01)
        assert float(greeks.charm_per_day) == pytest.approx(float(greeks.charm_per_year) / 365.0)


class TestRiskNeutralDensity:
    def test_a_flat_smile_recovers_the_lognormal_density(self):
        strikes = np.linspace(50.0, 200.0, 401)
        density = risk_neutral_density(
            strikes, lambda k: np.full_like(np.asarray(k, dtype=float), 0.2), FORWARD, TAU
        )
        sigma_root = 0.2 * math.sqrt(TAU)
        expected = np.exp(
            -((np.log(strikes / FORWARD) + 0.5 * sigma_root**2) ** 2) / (2 * sigma_root**2)
        ) / (strikes * sigma_root * math.sqrt(2 * math.pi))
        assert float(np.max(np.abs(density.density - expected))) < 1e-6
        # The window spans +/- 3.5 standard deviations, so about 5e-4 of the
        # mass is outside it. The shortfall is truncation, not a fit error.
        assert density.total_mass == pytest.approx(1.0, abs=1e-3)
        # The mean is more sensitive to truncation than the mass is, because
        # the missing tail is where the large values are. ``mean_error`` is the
        # quantity that measures it, and here it is 1.4 basis points.
        assert density.implied_mean == pytest.approx(FORWARD, rel=5e-4)
        assert abs(density.mean_error) < 5e-4
        assert density.is_admissible

    def test_quantiles_are_withheld_from_an_inadmissible_density(self):
        """A quantile of a curve that does not integrate to one and dips
        negative is a number with no meaning, so it is absent rather than
        computed anyway."""
        strikes = np.linspace(95.0, 105.0, 21)
        density = risk_neutral_density(
            strikes, lambda k: np.full_like(np.asarray(k, dtype=float), 0.2), FORWARD, TAU
        )
        assert DensityFlag.NARROW_STRIKE_RANGE in density.flags
        assert not density.is_admissible
        assert density.percentile(0.5) is None

    def test_the_quantiles_of_an_admissible_density_are_ordered(self):
        strikes = np.linspace(50.0, 200.0, 401)
        density = risk_neutral_density(
            strikes, lambda k: np.full_like(np.asarray(k, dtype=float), 0.2), FORWARD, TAU
        )
        values = [density.percentile(p) for p in (0.05, 0.25, 0.5, 0.75, 0.95)]
        assert all(value is not None for value in values)
        assert values == sorted(values)

    def test_the_payload_says_it_is_not_a_forecast(self):
        strikes = np.linspace(50.0, 200.0, 201)
        density = risk_neutral_density(
            strikes, lambda k: np.full_like(np.asarray(k, dtype=float), 0.2), FORWARD, TAU
        )
        assert "not a forecast" in density.to_dict()["interpretation"].lower()

    @pytest.mark.parametrize("strikes", [np.array([100.0, 110.0]), np.array([100.0, 90.0, 110.0])])
    def test_a_degenerate_strike_grid_is_refused(self, strikes):
        with pytest.raises(ValueError):
            risk_neutral_density(
                strikes, lambda k: np.full_like(np.asarray(k, dtype=float), 0.2), FORWARD, TAU
            )


def test_units_are_documented_once():
    """Every higher-order quantity is published in a scaled unit with a stated
    meaning, so nobody downstream has to remember a convention."""
    assert set(HIGHER_ORDER_UNITS) == {
        "vanna_per_vol_point",
        "volga_per_vol_point",
        "charm_per_day",
    }
    for description in HIGHER_ORDER_UNITS.values():
        assert description
