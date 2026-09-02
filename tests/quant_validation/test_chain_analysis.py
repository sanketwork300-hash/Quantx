"""End-to-end numerical validation of the Phase 1 pipeline.

The synthetic market is generated from a known raw-SVI slice at a known
forward, so this asks the strongest question available: given only tick-rounded
bid/ask quotes, does the pipeline recover the surface that produced them?

That is a much better test than any single-step check, because it exercises the
interaction of the parts — a forward that is off by a basis point shows up as a
skewed smile, and a day-count error shows up as a level shift.
"""

from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime, time

import pytest

from domains.derivatives.forward import ForwardMethod
from domains.derivatives.service import (
    ChainAnalysisRequest,
    ChainAnalysisService,
    QuoteInput,
)
from domains.derivatives.timeconv import ExpiryPolicy
from domains.market_data.curves import YieldCurve
from domains.market_data.providers.synthetic import (
    SyntheticMarketConfig,
    SyntheticMarketDataProvider,
)

AS_OF = datetime(2026, 9, 24, 9, 20, tzinfo=UTC)


@pytest.fixture(scope="module")
def analysed():
    config = SyntheticMarketConfig(as_of=AS_OF)
    provider = SyntheticMarketDataProvider(config)
    chain = asyncio.run(provider.get_option_chain(provider.underlying.id))

    quotes = [
        QuoteInput(
            instrument_id=quote.instrument_id,
            expiry=quote.expiry,
            strike=quote.strike,
            option_type=quote.option_type,
            bid_price=quote.quote.bid_price,
            ask_price=quote.quote.ask_price,
            last_price=quote.quote.last_price,
        )
        for quote in chain.quotes
    ]
    request = ChainAnalysisRequest(
        as_of=AS_OF,
        expiry_policy=ExpiryPolicy(settlement_time_utc=time(10, 0)),
        curve=YieldCurve.flat(config.risk_free_rate, AS_OF, "INR", source="assumption"),
        underlying_price=chain.underlying_price,
    )
    analysis, warnings = ChainAnalysisService().analyze(
        provider.underlying.id, provider.underlying.id, quotes, request
    )
    return config, provider, analysis, warnings


class TestCompleteness:
    def test_every_quote_inverts(self, analysed):
        _config, _provider, analysis, _warnings = analysed
        assert analysis.total_quotes == 150
        assert analysis.total_solved == 150

    def test_one_slice_per_expiry(self, analysed):
        config, _provider, analysis, _warnings = analysed
        assert len(analysis.slices) == len(config.expiry_days)

    def test_every_point_has_a_volatility_or_a_reason(self, analysed):
        """The Phase 1 acceptance criterion."""
        _config, _provider, analysis, _warnings = analysed
        for slice_ in analysis.slices:
            for point in slice_.points:
                assert (point.market_iv is not None) != (point.error is not None), point


class TestForwardRecovery:
    def test_parity_recovers_the_generating_forward(self, analysed):
        config, _provider, analysis, _warnings = analysed
        for slice_ in analysis.slices:
            expected = float(config.spot) * math.exp(config.risk_free_rate * slice_.time_to_expiry)
            selected = slice_.forward.selected
            assert selected.method is ForwardMethod.PUT_CALL_PARITY
            # Tick rounding of the quotes is the only error source.
            assert selected.value == pytest.approx(expected, rel=1e-6)

    def test_parity_recovers_the_discount_factor(self, analysed):
        config, _provider, analysis, _warnings = analysed
        for slice_ in analysis.slices:
            expected = math.exp(-config.risk_free_rate * slice_.time_to_expiry)
            assert slice_.forward.selected.discount_factor == pytest.approx(expected, rel=1e-5)

    def test_estimators_agree_to_within_a_few_basis_points(self, analysed):
        _config, _provider, analysis, _warnings = analysed
        for slice_ in analysis.slices:
            assert slice_.forward.disagreement < 1e-4


class TestVolatilityRecovery:
    def test_atm_level_matches_the_generating_surface(self, analysed):
        _config, provider, analysis, _warnings = analysed
        for slice_ in analysis.slices:
            expected = provider.implied_vol(0.0, slice_.time_to_expiry)
            assert slice_.atm_volatility == pytest.approx(expected, abs=5e-5)

    def test_every_solved_point_matches_the_generating_surface(self, analysed):
        """The whole slice, not just the money."""
        _config, provider, analysis, _warnings = analysed
        for slice_ in analysis.slices:
            forward = slice_.forward.selected.value
            errors = []
            for point in slice_.points:
                if point.market_iv is None:
                    continue
                k = math.log(float(point.strike) / forward)
                errors.append(abs(point.market_iv - provider.implied_vol(k, slice_.time_to_expiry)))
            # A 0.05 tick is a meaningful fraction of a one-week option's time
            # value, so the shortest expiry sets this bound, not the solver.
            assert max(errors) < 2e-3, f"{slice_.expiry}: max error {max(errors):.2e}"

    def test_longer_expiries_recover_more_precisely(self, analysed):
        """Tick rounding hurts least where an option has the most time value."""
        _config, provider, analysis, _warnings = analysed

        def slice_error(slice_):
            forward = slice_.forward.selected.value
            return max(
                abs(
                    point.market_iv
                    - provider.implied_vol(
                        math.log(float(point.strike) / forward), slice_.time_to_expiry
                    )
                )
                for point in slice_.points
                if point.market_iv is not None
            )

        ordered = sorted(analysis.slices, key=lambda s: s.time_to_expiry)
        assert slice_error(ordered[-1]) < slice_error(ordered[0])

    def test_skew_is_negative_as_generated(self, analysed):
        _config, _provider, analysis, _warnings = analysed
        for slice_ in analysis.slices:
            assert slice_.skew < 0

    def test_total_variance_increases_with_maturity(self, analysed):
        """Calendar-arbitrage freedom survives the round trip through quotes."""
        _config, _provider, analysis, _warnings = analysed
        ordered = sorted(analysis.slices, key=lambda s: s.time_to_expiry)
        variances = [s.atm_volatility**2 * s.time_to_expiry for s in ordered]
        assert all(
            later > earlier for earlier, later in zip(variances, variances[1:], strict=False)
        )


class TestSmileConstruction:
    def test_one_quote_per_strike_carries_the_smile(self, analysed):
        _config, _provider, analysis, _warnings = analysed
        for slice_ in analysis.slices:
            used = [point for point in slice_.points if point.used_for_smile]
            strikes = [point.strike for point in used]
            assert len(strikes) == len(set(strikes))

    def test_out_of_the_money_quotes_are_preferred(self, analysed):
        from domains.instruments.enums import OptionType

        _config, _provider, analysis, _warnings = analysed
        for slice_ in analysis.slices:
            for point in slice_.points:
                if not point.used_for_smile or abs(point.log_moneyness) <= 0.02:
                    continue
                expected = OptionType.CALL if point.log_moneyness > 0 else OptionType.PUT
                assert point.option_type is expected

    def test_selection_is_deterministic(self):
        """Reproducibility needs the same input to select the same quote."""
        config = SyntheticMarketConfig(as_of=AS_OF)
        provider = SyntheticMarketDataProvider(config)
        chain = asyncio.run(provider.get_option_chain(provider.underlying.id))
        quotes = [
            QuoteInput(
                instrument_id=q.instrument_id,
                expiry=q.expiry,
                strike=q.strike,
                option_type=q.option_type,
                bid_price=q.quote.bid_price,
                ask_price=q.quote.ask_price,
                last_price=q.quote.last_price,
            )
            for q in chain.quotes
        ]
        request = ChainAnalysisRequest(
            as_of=AS_OF,
            expiry_policy=ExpiryPolicy(settlement_time_utc=time(10, 0)),
            curve=YieldCurve.flat(config.risk_free_rate, AS_OF, "INR"),
            underlying_price=chain.underlying_price,
        )
        first, _ = ChainAnalysisService().analyze(
            provider.underlying.id, provider.underlying.id, quotes, request
        )
        second, _ = ChainAnalysisService().analyze(
            provider.underlying.id, provider.underlying.id, quotes, request
        )
        for slice_a, slice_b in zip(first.slices, second.slices, strict=True):
            assert [p.used_for_smile for p in slice_a.points] == [
                p.used_for_smile for p in slice_b.points
            ]
            assert [p.market_iv for p in slice_a.points] == [p.market_iv for p in slice_b.points]

    def test_bid_ask_envelope_brackets_the_mid_volatility(self, analysed):
        _config, _provider, analysis, _warnings = analysed
        checked = 0
        for slice_ in analysis.slices:
            for point in slice_.points:
                if point.market_iv_bid is None or point.market_iv_ask is None:
                    continue
                checked += 1
                assert point.market_iv_bid <= point.market_iv <= point.market_iv_ask
                assert point.iv_envelope_width > 0
        assert checked > 100


class TestDegradedInputs:
    def _analyse(self, request_overrides: dict):
        config = SyntheticMarketConfig(as_of=AS_OF)
        provider = SyntheticMarketDataProvider(config)
        chain = asyncio.run(provider.get_option_chain(provider.underlying.id))
        quotes = [
            QuoteInput(
                instrument_id=q.instrument_id,
                expiry=q.expiry,
                strike=q.strike,
                option_type=q.option_type,
                bid_price=q.quote.bid_price,
                ask_price=q.quote.ask_price,
                last_price=q.quote.last_price,
            )
            for q in chain.quotes
        ]
        base = {
            "as_of": AS_OF,
            "expiry_policy": ExpiryPolicy(settlement_time_utc=time(10, 0)),
            "curve": YieldCurve.flat(config.risk_free_rate, AS_OF, "INR"),
            "underlying_price": chain.underlying_price,
        }
        base.update(request_overrides)
        return ChainAnalysisService().analyze(
            provider.underlying.id,
            provider.underlying.id,
            quotes,
            ChainAnalysisRequest(**base),
        )

    def test_no_settlement_time_solves_nothing_and_says_why(self):
        analysis, warnings = self._analyse({"expiry_policy": ExpiryPolicy()})
        assert analysis.total_solved == 0
        assert any(w.code == "DERIVATIVES_SETTLEMENT_TIME_UNKNOWN" for w in warnings)
        for slice_ in analysis.slices:
            assert slice_.reason == "SETTLEMENT_TIME_UNKNOWN"

    def test_no_underlying_price_still_works_via_parity(self):
        """Put-call parity needs no spot at all."""
        analysis, warnings = self._analyse({"underlying_price": None})
        assert analysis.total_solved == 150
        assert any(w.code == "DERIVATIVES_NO_UNDERLYING_PRICE" for w in warnings)
        for slice_ in analysis.slices:
            assert slice_.forward.selected.method is ForwardMethod.PUT_CALL_PARITY

    def test_an_expired_chain_reports_rather_than_solving(self):
        analysis, warnings = self._analyse({"as_of": datetime(2027, 6, 1, tzinfo=UTC)})
        assert analysis.total_solved == 0
        assert any(w.code == "DERIVATIVES_EXPIRY_IN_THE_PAST" for w in warnings)
        for slice_ in analysis.slices:
            assert slice_.reason == "OPTION_EXPIRED"

    def test_a_wrong_rate_assumption_does_not_break_the_smile(self):
        """Parity recovers the true discount factor from the quotes, so a bad
        curve degrades the spot-carry estimate and nothing else."""
        analysis, _warnings = self._analyse({"curve": YieldCurve.flat(0.20, AS_OF, "INR")})
        assert analysis.total_solved == 150
        for slice_ in analysis.slices:
            assert slice_.forward.selected.method is ForwardMethod.PUT_CALL_PARITY
            assert slice_.forward.disagreement > 1e-3, "the bad carry should disagree"


class TestNoUnrealisticClaims:
    def test_no_output_field_suggests_a_trade(self, analysed):
        """A contract guarantee: this domain produces analysis, not advice."""
        _config, _provider, analysis, _warnings = analysed
        payload = analysis.to_dict()
        rendered = str(payload).lower()
        for forbidden in ("fair_value", "underpriced", "overpriced", "signal", "recommend"):
            assert forbidden not in rendered

    def test_market_iv_is_never_labelled_a_reference(self, analysed):
        """Phase 1 produces market-implied volatility only. The fitted
        reference IV is a Phase 2 field and must not exist yet."""
        _config, _provider, analysis, _warnings = analysed
        point = analysis.slices[0].points[0].to_dict()
        assert "market_iv" in point
        assert "reference_iv" not in point
