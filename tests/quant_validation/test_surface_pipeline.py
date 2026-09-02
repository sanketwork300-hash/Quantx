"""End-to-end validation of the Phase 2 pipeline against a known surface.

The synthetic market is generated from a known raw-SVI slice, so this asks: from
tick-rounded bid/ask quotes alone, does the pipeline recover a surface that
reproduces the one that produced them — and do the arbitrage diagnostics fire
where they should and stay quiet where they should not?
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import replace
from datetime import UTC, datetime, time
from decimal import Decimal

import numpy as np
import pytest

from domains.derivatives.arbitrage import ArbitrageScope, ViolationType
from domains.derivatives.calibration import (
    SurfaceCalibrationRequest,
    SurfaceCalibrationService,
)
from domains.derivatives.service import (
    ChainAnalysisRequest,
    ChainAnalysisService,
    QuoteInput,
)
from domains.derivatives.surface import ReferenceFlag, ReferenceMethod
from domains.derivatives.timeconv import ExpiryPolicy
from domains.instruments.enums import OptionType
from domains.market_data.curves import YieldCurve
from domains.market_data.providers.synthetic import (
    SyntheticMarketConfig,
    SyntheticMarketDataProvider,
)
from domains.market_data.quality.flags import Severity
from quant.volatility.svi import durrleman_g

AS_OF = datetime(2026, 9, 24, 9, 20, tzinfo=UTC)


def build_quotes(chain, tick_size: Decimal = Decimal("0.05")) -> list[QuoteInput]:
    return [
        QuoteInput(
            instrument_id=quote.instrument_id,
            expiry=quote.expiry,
            strike=quote.strike,
            option_type=quote.option_type,
            bid_price=quote.quote.bid_price,
            ask_price=quote.quote.ask_price,
            last_price=quote.quote.last_price,
            tick_size=tick_size,
        )
        for quote in chain.quotes
    ]


def run_pipeline(config: SyntheticMarketConfig, quotes=None):
    provider = SyntheticMarketDataProvider(config)
    chain = asyncio.run(provider.get_option_chain(provider.underlying.id))
    quotes = quotes if quotes is not None else build_quotes(chain, config.tick_size)

    analysis, _ = ChainAnalysisService().analyze(
        provider.underlying.id,
        provider.underlying.id,
        quotes,
        ChainAnalysisRequest(
            as_of=config.as_of,
            expiry_policy=ExpiryPolicy(settlement_time_utc=time(10, 0)),
            curve=YieldCurve.flat(config.risk_free_rate, config.as_of, "INR"),
            underlying_price=chain.underlying_price,
        ),
    )
    result = SurfaceCalibrationService().calibrate(analysis, SurfaceCalibrationRequest())
    return provider, chain, analysis, result


@pytest.fixture(scope="module")
def pipeline():
    """A realistically narrow chain: 25 strikes over about 0.1 in k."""
    return run_pipeline(SyntheticMarketConfig(as_of=AS_OF))


@pytest.fixture(scope="module")
def wide_pipeline():
    """A wide chain, where SVI's parameters are actually identifiable.

    Note what the pipeline does with it: the deep wings of the short expiries
    are worth less than a tick, so their quotes rest on the tick floor and are
    dropped as ill-conditioned. The *fitted* range therefore contracts to where
    the market actually carries volatility information, which is the correct
    outcome and is why accuracy is asserted over ``[k_min, k_max]`` rather than
    over the strikes that happen to be listed.
    """
    return run_pipeline(
        SyntheticMarketConfig(as_of=AS_OF, strikes_each_side=30, strike_step=Decimal("400"))
    )


class TestCalibrationQuality:
    def test_every_slice_converges(self, pipeline):
        _provider, _chain, _analysis, result = pipeline
        assert result.fitted_count == len(result.surface.slices)
        for slice_ in result.surface.slices:
            assert slice_.calibration.status.value == "CONVERGED"

    def test_the_fit_is_tight_in_sample(self, pipeline):
        _provider, _chain, _analysis, result = pipeline
        for slice_ in result.surface.slices:
            assert slice_.calibration.rmse_vol_points < 0.05
            assert slice_.calibration.max_error_vol_points < 0.2

    def test_the_surface_reproduces_the_observed_market_ivs(self, pipeline):
        """Where there are quotes, the reference IV must match the market IV."""
        _provider, _chain, analysis, result = pipeline
        for smile in analysis.slices:
            for point in smile.points:
                if not point.used_for_smile or point.market_iv is None:
                    continue
                reference = result.surface.reference(point.strike, smile.expiry)
                assert reference.reference_iv == pytest.approx(point.market_iv, abs=2e-3)

    def test_the_surface_reproduces_the_generating_surface_in_sample(self, pipeline):
        provider, _chain, _analysis, result = pipeline
        for slice_ in result.surface.slices:
            grid = np.linspace(slice_.k_min, slice_.k_max, 21)
            errors = [
                abs(
                    float(slice_.implied_vol(k))
                    - provider.implied_vol(float(k), slice_.time_to_expiry)
                )
                for k in grid
            ]
            assert max(errors) * 100 < 0.1, f"{slice_.expiry}: {max(errors) * 100:.4f}"

    def test_a_wide_chain_recovers_the_surface_across_its_fitted_range(self, wide_pipeline):
        """With real strike coverage the fit tracks the generating surface far
        beyond the money."""
        provider, _chain, _analysis, result = wide_pipeline
        widest = max(result.surface.fitted_slices, key=lambda s: s.k_max - s.k_min)
        assert widest.k_max - widest.k_min > 0.4, "the fitted window should be wide"

        # The interior of the fitted window, away from the last few strikes
        # where tick rounding dominates the quoted price.
        width = widest.k_max - widest.k_min
        grid = np.linspace(widest.k_min + 0.15 * width, widest.k_max - 0.15 * width, 41)
        errors = [
            abs(
                float(widest.implied_vol(k)) - provider.implied_vol(float(k), widest.time_to_expiry)
            )
            for k in grid
        ]
        assert max(errors) * 100 < 0.6

    def test_accuracy_degrades_toward_the_edge_of_the_fitted_window(self, wide_pipeline):
        """An honest characteristic, not a defect.

        SVI's wings are straight lines in ``k``, and the outermost quotes are
        the ones most distorted by tick rounding, so a few noisy far strikes
        tilt the wing. Measured on this chain the error is under 0.1 volatility
        points near the money and around 3 at the very edge — which is why a
        reference value outside the fitted range carries an
        EXTRAPOLATED_STRIKE flag, and why the wings are the part of a surface to
        trust least.
        """
        provider, _chain, _analysis, result = wide_pipeline
        widest = max(result.surface.fitted_slices, key=lambda s: s.k_max - s.k_min)

        def error_at(k: float) -> float:
            return abs(
                float(widest.implied_vol(k)) - provider.implied_vol(k, widest.time_to_expiry)
            )

        centre = (widest.k_min + widest.k_max) / 2.0
        assert error_at(centre) < error_at(widest.k_max)

    def test_tick_floored_wing_quotes_are_dropped_as_ill_conditioned(self, wide_pipeline):
        """A deep out-of-the-money weekly is worth less than a tick, so the
        venue quotes it locked at the floor. Inverting that price is
        numerically clean and economically meaningless; the pipeline drops it
        with a reason rather than letting it bend the slice."""
        from domains.derivatives.models import SmileExclusion

        _provider, _chain, analysis, _result = wide_pipeline
        shortest = min(analysis.slices, key=lambda s: s.time_to_expiry)
        dropped = [
            point
            for point in shortest.points
            if point.smile_exclusion is SmileExclusion.ILL_CONDITIONED
        ]
        assert dropped, "the wings of a one-week chain must be dropped"
        for point in dropped:
            assert point.market_iv is not None, "solved, then judged uninformative"
            assert point.uncertainty > 1e-2

    def test_the_fitted_range_contracts_to_where_information_exists(self, wide_pipeline):
        """A one-week option carries volatility information only near the money;
        a three-month one carries it much further out."""
        _provider, _chain, _analysis, result = wide_pipeline
        ordered = sorted(result.surface.fitted_slices, key=lambda s: s.time_to_expiry)
        widths = [slice_.k_max - slice_.k_min for slice_ in ordered]
        assert widths[0] < widths[-1]


class TestAdmissibility:
    def test_every_fitted_slice_is_butterfly_free(self, pipeline):
        _provider, _chain, _analysis, result = pipeline
        for slice_ in result.surface.slices:
            grid = np.linspace(slice_.k_min - 1.0, slice_.k_max + 1.0, 401)
            assert np.all(durrleman_g(grid, slice_.parameters) > 0)

    def test_every_fitted_slice_satisfies_lee(self, pipeline):
        _provider, _chain, _analysis, result = pipeline
        for slice_ in result.surface.slices:
            assert slice_.calibration.wing_slope <= 2.0 + 1e-9

    def test_total_variance_is_non_decreasing_in_maturity(self, pipeline):
        """Calendar consistency, recovered end to end through quotes and a fit."""
        _provider, _chain, _analysis, result = pipeline
        ordered = sorted(result.surface.fitted_slices, key=lambda s: s.time_to_expiry)
        grid = np.linspace(-0.05, 0.03, 21)
        for short, long in zip(ordered, ordered[1:], strict=False):
            assert np.all(long.total_variance(grid) >= short.total_variance(grid) - 1e-12)


class TestArbitrageOnACleanMarket:
    def test_no_raw_violations(self, pipeline):
        _provider, _chain, _analysis, result = pipeline
        assert result.raw_report.violations == ()
        assert result.raw_report.scope is ArbitrageScope.RAW_MARKET
        assert result.raw_report.observations > 0

    def test_no_fitted_violations(self, pipeline):
        _provider, _chain, _analysis, result = pipeline
        assert result.fitted_report.violations == ()
        assert result.fitted_report.scope is ArbitrageScope.FITTED_SURFACE

    def test_both_reports_name_the_checks_they_ran(self, pipeline):
        _provider, _chain, _analysis, result = pipeline
        assert "BUTTERFLY" in result.raw_report.checks_run
        assert "DURRLEMAN" in result.fitted_report.checks_run


class TestArbitrageOnACorruptedMarket:
    """Scope separation, demonstrated on a market with a real defect."""

    @pytest.fixture(scope="class")
    @staticmethod
    def corrupted():
        config = SyntheticMarketConfig(as_of=AS_OF)
        provider = SyntheticMarketDataProvider(config)
        chain = asyncio.run(provider.get_option_chain(provider.underlying.id))
        quotes = build_quotes(chain)

        # Push one mid-chain call far out of line, keeping the spread intact so
        # the quality engine still admits it. This is what a stale leg or a
        # fat-fingered print looks like.
        target = next(
            index
            for index, quote in enumerate(quotes)
            if quote.option_type is OptionType.CALL
            and quote.expiry == chain.expiries[1]
            and quote.strike == Decimal("24000")
        )
        bad = quotes[target]
        bump = Decimal("120")
        quotes[target] = replace(
            bad, bid_price=bad.bid_price + bump, ask_price=bad.ask_price + bump
        )
        return run_pipeline(config, quotes)

    def test_the_raw_market_report_catches_it(self, corrupted):
        _provider, _chain, _analysis, result = corrupted
        serious = result.raw_report.at_or_above(Severity.WARNING)
        assert serious, "a 120-point mispriced call must be caught"
        types = {violation.violation_type for violation in serious}
        assert ViolationType.BUTTERFLY in types or ViolationType.PUT_CALL_PARITY in types

    def test_violations_carry_a_magnitude_and_a_tolerance(self, corrupted):
        _provider, _chain, _analysis, result = corrupted
        for violation in result.raw_report.at_or_above(Severity.WARNING):
            assert violation.magnitude > 0
            assert violation.tolerance is not None
            assert violation.magnitude > violation.tolerance

    def test_violations_name_the_instruments_involved(self, corrupted):
        _provider, _chain, _analysis, result = corrupted
        serious = result.raw_report.at_or_above(Severity.WARNING)
        assert any(violation.affected_instruments for violation in serious)

    def test_the_fitted_surface_stays_admissible(self, corrupted):
        """The bad quote must not be able to produce a negative implied density.

        Durrleman's condition is in the optimizer's feasible set, so the fit
        absorbs the bad print as error instead of bending around it. That is why
        the two scopes are reported separately: a clean fitted report next to a
        dirty raw report is the correct reading of a market with one bad print,
        and merging them would lose it.
        """
        _provider, _chain, _analysis, result = corrupted
        for slice_ in result.surface.fitted_slices:
            assert slice_.calibration.constraints_satisfied
            assert slice_.calibration.min_durrleman_g > 0

    def test_the_warning_says_a_raw_violation_is_probably_a_data_artefact(self, corrupted):
        _provider, _chain, _analysis, result = corrupted
        raw_warnings = [
            warning for warning in result.warnings if warning.code == "SURFACE_RAW_MARKET_ARBITRAGE"
        ]
        assert raw_warnings
        assert "data artefact" in raw_warnings[0].message

    def test_the_bad_quote_degrades_the_fit_it_is_part_of(self, corrupted, pipeline):
        _provider, _chain, _analysis, dirty = corrupted
        _p2, _c2, _a2, clean = pipeline
        affected = dirty.surface.slice_for(dirty.surface.slices[1].expiry)
        reference = clean.surface.slice_for(affected.expiry)
        assert affected.calibration.rmse_vol_points > reference.calibration.rmse_vol_points


class TestReferenceValues:
    def test_an_exact_expiry_is_not_flagged(self, pipeline):
        _provider, _chain, _analysis, result = pipeline
        slice_ = result.surface.slices[0]
        strike = Decimal(str(round(slice_.forward / 100) * 100))
        point = result.surface.reference(strike, slice_.expiry, OptionType.CALL)
        assert point.method is ReferenceMethod.EXACT_SLICE
        assert ReferenceFlag.EXTRAPOLATED_STRIKE not in point.flags

    def test_reference_prices_satisfy_put_call_parity(self, pipeline):
        """The surface prices one volatility, so parity is exact by construction."""
        _provider, _chain, _analysis, result = pipeline
        slice_ = result.surface.slices[1]
        strike = Decimal(str(round(slice_.forward / 100) * 100))
        call = result.surface.reference(strike, slice_.expiry, OptionType.CALL)
        put = result.surface.reference(strike, slice_.expiry, OptionType.PUT)
        expected = slice_.discount_factor * (slice_.forward - float(strike))
        assert call.reference_price - put.reference_price == pytest.approx(expected, abs=1e-9)

    def test_a_far_strike_is_flagged_as_extrapolation(self, pipeline):
        _provider, _chain, _analysis, result = pipeline
        slice_ = result.surface.slices[0]
        far = Decimal(str(round(slice_.forward * math.exp(1.0))))
        point = result.surface.reference(far, slice_.expiry)
        assert ReferenceFlag.EXTRAPOLATED_STRIKE in point.flags


class TestDeterminism:
    def test_the_same_quotes_produce_the_same_surface_id(self):
        config = SyntheticMarketConfig(as_of=AS_OF)
        _p1, _c1, _a1, first = run_pipeline(config)
        _p2, _c2, _a2, second = run_pipeline(config)
        assert first.surface.surface_id == second.surface.surface_id

    def test_parameters_are_bitwise_identical_across_runs(self):
        config = SyntheticMarketConfig(as_of=AS_OF)
        _p1, _c1, _a1, first = run_pipeline(config)
        _p2, _c2, _a2, second = run_pipeline(config)
        for a, b in zip(first.surface.slices, second.surface.slices, strict=True):
            assert a.parameters == b.parameters
