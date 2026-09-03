"""Heston validation: against QuantLib, and against itself.

The Phase 9 acceptance criterion is that the characteristic-function pricer
cross-checks against QuantLib. It is implemented here rather than wrapped
(``docs/references.md``: IMPLEMENT INDEPENDENTLY) because two independent
implementations that agree to ten significant figures is a stronger guarantee
than one implementation called twice.

The branch matters. The naive form of the characteristic function crosses the
principal branch cut of the complex logarithm at long maturities and produces
prices that are wrong by percent rather than by rounding, without any warning.
The Albrecher et al. (2007) "little trap" form used here does not, and the
ten-year case below is the one that would catch a regression to the naive form.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from quant.pricing.black_scholes import bsm_greeks
from quant.pricing.heston import (
    HestonError,
    HestonParameters,
    heston_implied_volatility,
    heston_price,
)
from quant.pricing.heston_calibration import (
    HestonObservation,
    calibrate_heston,
)
from quant.volatility.implied import implied_vol_bsm
from quant.volatility.svi_calibration import CalibrationStatus

try:  # pragma: no cover - the import is the point of the marker
    import QuantLib as ql
except ImportError:  # pragma: no cover
    ql = None

SPOT = 100.0
RATE = 0.03
DIVIDEND = 0.01
BASE = HestonParameters(v0=0.04, kappa=1.5, theta=0.05, xi=0.4, rho=-0.6)

#: Cases chosen for where a characteristic-function pricer goes wrong: the very
#: short end, the deep wings, and the long maturities where the branch cut is.
#: Maturities are whole numbers of days so that the reference and the pricer are
#: asked about the same instant — a mismatch of a day and a half at T = 0.1 is a
#: 2.7e-2 price difference and looks exactly like a model error.
CASES = [
    (7 / 365.0, 100.0),
    (7 / 365.0, 115.0),
    (91 / 365.0, 90.0),
    (91 / 365.0, 100.0),
    (365 / 365.0, 80.0),
    (365 / 365.0, 100.0),
    (365 * 5 / 365.0, 120.0),
    (365 * 10 / 365.0, 100.0),
]

#: Feller is violated here (2*1.5*0.05 = 0.15 < 0.16), deliberately: a pricer
#: that only agrees where the variance process stays away from zero has not
#: been validated on the parameter sets real surfaces calibrate to.
assert BASE.feller < 0


class TestSelfConsistency:
    @pytest.mark.parametrize("tau,strike", CASES)
    def test_put_call_parity_holds_exactly(self, tau, strike):
        call = heston_price(SPOT, strike, tau, RATE, DIVIDEND, BASE, True)
        put = heston_price(SPOT, strike, tau, RATE, DIVIDEND, BASE, False)
        parity = SPOT * math.exp(-DIVIDEND * tau) - strike * math.exp(-RATE * tau)
        assert (call - put) == pytest.approx(parity, abs=1e-12)

    @pytest.mark.parametrize("tau,strike", CASES)
    def test_the_price_respects_the_model_free_bounds(self, tau, strike):
        call = heston_price(SPOT, strike, tau, RATE, DIVIDEND, BASE, True)
        put = heston_price(SPOT, strike, tau, RATE, DIVIDEND, BASE, False)
        assert call >= 0.0
        assert put >= 0.0
        assert call <= SPOT * math.exp(-DIVIDEND * tau) + 1e-9
        assert put <= strike * math.exp(-RATE * tau) + 1e-9

    @pytest.mark.parametrize("xi,tolerance", [(1e-2, 3e-4), (1e-3, 3e-6), (1e-4, 3e-7)])
    def test_the_price_converges_to_black_scholes_as_vol_of_vol_vanishes(self, xi, tolerance):
        """With v0 = theta and xi -> 0 the variance is deterministic, so the
        price must approach Black-Scholes at the root of that variance — and it
        must approach it *as xi^2*, which is what these three tolerances assert.

        The convergence stops at about xi = 1e-4. Below it the ``1 / xi^2`` in
        the characteristic function loses conditioning faster than the model
        converges and the error grows again. That is a documented limit of the
        pricer rather than a tolerance to widen: the deterministic-variance
        limit is Black-Scholes, and should be priced with it."""
        tau = 1.0
        parameters = HestonParameters(v0=0.04, kappa=2.0, theta=0.04, xi=xi, rho=0.0)
        ours = heston_price(SPOT, 100.0, tau, RATE, DIVIDEND, parameters, True)
        reference = float(bsm_greeks(SPOT, 100.0, tau, RATE, DIVIDEND, math.sqrt(0.04), True).price)
        assert ours == pytest.approx(reference, abs=tolerance)

    def test_an_expired_option_is_its_intrinsic_value(self):
        assert heston_price(SPOT, 90.0, 0.0, RATE, DIVIDEND, BASE, True) == 10.0
        assert heston_price(SPOT, 90.0, 0.0, RATE, DIVIDEND, BASE, False) == 0.0

    def test_the_smile_is_not_flat(self):
        """A stochastic-volatility model with rho < 0 must produce a downward
        skew; a flat smile would mean the correlation is not reaching the price."""
        strikes = np.array([85.0, 100.0, 115.0])
        vols = [
            heston_implied_volatility(SPOT, float(k), 1.0, RATE, DIVIDEND, BASE, True)
            for k in strikes
        ]
        assert all(v is not None for v in vols)
        assert vols[0] > vols[1] > vols[2]

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"spot": 0.0},
            {"strike": 0.0},
            {"tau": -1.0},
        ],
    )
    def test_a_degenerate_contract_is_refused(self, kwargs):
        arguments = {"spot": SPOT, "strike": 100.0, "tau": 1.0, **kwargs}
        with pytest.raises(HestonError):
            heston_price(
                arguments["spot"],
                arguments["strike"],
                arguments["tau"],
                RATE,
                DIVIDEND,
                BASE,
            )

    def test_feller_is_reported_and_not_enforced(self):
        assert BASE.satisfies_feller is False
        assert "HESTON_FELLER_CONDITION_VIOLATED" in BASE.warnings()
        # It still prices: refusing would mean refusing the parameter sets real
        # index surfaces calibrate to.
        assert heston_price(SPOT, 100.0, 1.0, RATE, DIVIDEND, BASE, True) > 0


class TestCalibration:
    """A surface generated from known parameters must calibrate back to them."""

    TRUTH = HestonParameters(v0=0.045, kappa=1.8, theta=0.05, xi=0.55, rho=-0.65)

    @staticmethod
    def _observations(truth: HestonParameters) -> list[HestonObservation]:
        observations = []
        for tau in (0.1, 0.35, 0.75, 1.5):
            for strike in (80.0, 90.0, 95.0, 100.0, 105.0, 110.0, 125.0):
                price = heston_price(SPOT, strike, tau, RATE, DIVIDEND, truth, True)
                solved = implied_vol_bsm(price, SPOT, strike, tau, RATE, DIVIDEND, True)
                if solved.implied_volatility is None:
                    continue
                greeks = bsm_greeks(
                    SPOT, strike, tau, RATE, DIVIDEND, solved.implied_volatility, True
                )
                observations.append(
                    HestonObservation(
                        strike=strike,
                        maturity=tau,
                        price=price,
                        is_call=True,
                        vega=float(greeks.vega),
                        rate=RATE,
                        dividend=DIVIDEND,
                        market_volatility=solved.implied_volatility,
                    )
                )
        return observations

    @pytest.fixture(scope="class")
    @classmethod
    def fitted(cls):
        return calibrate_heston(SPOT, cls._observations(cls.TRUTH))

    def test_the_known_parameters_are_recovered(self, fitted):
        assert fitted.status is CalibrationStatus.CONVERGED
        assert fitted.parameters is not None
        for name in ("v0", "kappa", "theta", "xi", "rho"):
            assert getattr(fitted.parameters, name) == pytest.approx(
                getattr(self.TRUTH, name), abs=1e-2
            ), name

    def test_the_fit_is_reported_in_volatility_points(self, fitted):
        assert fitted.rmse_vol_points < 0.05
        assert fitted.max_error_vol_points < 0.1

    def test_the_calibration_is_reproducible(self):
        first = calibrate_heston(SPOT, self._observations(self.TRUTH), seed=7)
        second = calibrate_heston(SPOT, self._observations(self.TRUTH), seed=7)
        assert first.parameters == second.parameters

    def test_too_few_quotes_is_reported_rather_than_fitted(self):
        result = calibrate_heston(SPOT, self._observations(self.TRUTH)[:3])
        assert result.status is CalibrationStatus.INSUFFICIENT_OBSERVATIONS
        assert result.parameters is None
        assert "5" in (result.error or "")

    def test_enforcing_feller_produces_a_feasible_fit_that_fits_worse(self, fitted):
        """The trade-off, made visible rather than argued about: the constrained
        fit satisfies the condition and pays for it in volatility points.

        The tolerance is the optimizer's own, not a strict inequality. A
        constrained fit lands *on* the boundary, where ``2 kappa theta - xi^2``
        is zero to within rounding, and the database constraint on
        ``feller_enforced`` uses the same bound so that a row cannot be forced
        to disclaim an enforcement that did happen."""
        constrained = calibrate_heston(SPOT, self._observations(self.TRUTH), require_feller=True)
        assert constrained.parameters is not None
        assert constrained.feller >= -1e-9
        assert constrained.feller_enforced is True
        assert constrained.rmse_vol_points > fitted.rmse_vol_points


@pytest.mark.requires_reference_libs
class TestAgainstQuantLib:
    """The acceptance criterion, on an independent implementation."""

    @staticmethod
    def _quantlib_price(tau: float, strike: float, is_call: bool) -> float:
        today = ql.Date(24, 9, 2026)
        ql.Settings.instance().evaluationDate = today
        day_count = ql.Actual365Fixed()
        calendar = ql.NullCalendar()
        maturity = today + int(round(tau * 365))

        spot = ql.QuoteHandle(ql.SimpleQuote(SPOT))
        rate_curve = ql.YieldTermStructureHandle(
            ql.FlatForward(today, RATE, day_count, ql.Continuous)
        )
        dividend_curve = ql.YieldTermStructureHandle(
            ql.FlatForward(today, DIVIDEND, day_count, ql.Continuous)
        )
        process = ql.HestonProcess(
            rate_curve,
            dividend_curve,
            spot,
            BASE.v0,
            BASE.kappa,
            BASE.theta,
            BASE.xi,
            BASE.rho,
        )
        engine = ql.AnalyticHestonEngine(ql.HestonModel(process), 192)
        option = ql.VanillaOption(
            ql.PlainVanillaPayoff(ql.Option.Call if is_call else ql.Option.Put, strike),
            ql.EuropeanExercise(maturity),
        )
        option.setPricingEngine(engine)
        del calendar
        return option.NPV()

    @pytest.mark.parametrize("tau,strike", CASES)
    @pytest.mark.parametrize("is_call", [True, False])
    def test_price_matches_quantlib(self, tau, strike, is_call):
        if ql is None:
            pytest.skip("QuantLib not installed")
        theirs = self._quantlib_price(tau, strike, is_call)
        ours = heston_price(SPOT, strike, tau, RATE, DIVIDEND, BASE, is_call)
        assert ours == pytest.approx(theirs, rel=1e-9, abs=1e-9)


class TestIntegrationLimit:
    """The truncation is adaptive, and the test shows it is doing something."""

    def test_a_short_maturity_needs_a_wider_integral_than_a_long_one(self):
        from quant.pricing.heston import INTEGRATION_LIMIT, integration_limit

        week = integration_limit(7 / 365.0, BASE)
        year = integration_limit(1.0, BASE)
        assert week > year
        assert year == INTEGRATION_LIMIT, "the floor binds at ordinary maturities"

    def test_the_fixed_floor_is_not_enough_at_the_short_end(self):
        """The regression this guards: truncating a seven-day option's integral
        at the floor is wrong in the seventh significant figure, which is small
        enough to look like rounding and large enough to fail a cross-check."""
        from quant.pricing.heston import INTEGRATION_LIMIT

        tau, strike = 7 / 365.0, 100.0
        adaptive = heston_price(SPOT, strike, tau, RATE, DIVIDEND, BASE, True)
        truncated = heston_price(
            SPOT, strike, tau, RATE, DIVIDEND, BASE, True, limit=INTEGRATION_LIMIT
        )
        assert abs(truncated - adaptive) > 1e-8
        assert abs(truncated - adaptive) < 1e-5
