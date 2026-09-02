"""The synthetic market must be internally arbitrage-free.

Phase 0 acceptance criterion. It matters because every later phase validates
its arbitrage detector, surface fitter and risk engine against this data: if the
generated chain already violated static no-arbitrage conditions, a passing
Phase 2 test would prove nothing.

Tolerances are stated in ticks, because the generator rounds quotes to the tick
grid exactly as a venue does, and that rounding is the only source of violation
these tests should ever see.
"""

from __future__ import annotations

import asyncio
import math
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from domains.instruments.enums import OptionType
from domains.market_data.providers.synthetic import (
    SyntheticMarketConfig,
    SyntheticMarketDataProvider,
)

AS_OF = datetime(2026, 9, 24, 9, 20, tzinfo=UTC)
SECONDS_PER_YEAR = 365.0 * 24.0 * 3600.0


@pytest.fixture(scope="module")
def market():
    config = SyntheticMarketConfig(as_of=AS_OF)
    provider = SyntheticMarketDataProvider(config)
    chain = asyncio.run(provider.get_option_chain(provider.underlying.id))
    return config, provider, chain


def tau_for(config, expiry) -> float:
    instant = datetime.combine(expiry, config.expiry_time_utc, tzinfo=UTC)
    return (instant - config.as_of).total_seconds() / SECONDS_PER_YEAR


def by_strike(chain, expiry) -> dict[Decimal, dict[OptionType, Decimal]]:
    table: dict[Decimal, dict[OptionType, Decimal]] = defaultdict(dict)
    for quote in chain.for_expiry(expiry):
        table[quote.strike][quote.option_type] = quote.mid_price
    return table


class TestDeterminism:
    def test_the_same_seed_produces_the_same_market(self):
        config = SyntheticMarketConfig(as_of=AS_OF)
        first = SyntheticMarketDataProvider(config)
        second = SyntheticMarketDataProvider(config)
        assert first.dataset_version == second.dataset_version

        chain_a = asyncio.run(first.get_option_chain(first.underlying.id))
        chain_b = asyncio.run(second.get_option_chain(second.underlying.id))
        assert [q.quote.bid_price for q in chain_a.quotes] == [
            q.quote.bid_price for q in chain_b.quotes
        ]

    def test_dataset_version_changes_with_the_parameters(self):
        base = SyntheticMarketDataProvider(SyntheticMarketConfig(as_of=AS_OF))
        shifted = SyntheticMarketDataProvider(
            SyntheticMarketConfig(as_of=AS_OF, spot=Decimal("25000"))
        )
        assert base.dataset_version != shifted.dataset_version


class TestStructure:
    def test_chain_covers_every_expiry_and_strike(self, market):
        config, _provider, chain = market
        expected = len(config.expiry_days) * (2 * config.strikes_each_side + 1) * 2
        assert len(chain) == expected
        assert len(chain.expiries) == len(config.expiry_days)

    def test_every_quote_is_two_sided_and_positive(self, market):
        _config, _provider, chain = market
        for quote in chain.quotes:
            assert quote.quote.bid_price > 0
            assert quote.quote.ask_price > quote.quote.bid_price
            assert quote.mid_price is not None

    def test_spreads_widen_away_from_the_money(self, market):
        config, _provider, chain = market
        expiry = chain.expiries[0]
        table = {q.strike: q for q in chain.for_expiry(expiry) if q.option_type is OptionType.CALL}
        atm = min(table, key=lambda k: abs(k - config.spot))
        wing = max(table)
        assert table[wing].quote.relative_spread > table[atm].quote.relative_spread

    def test_liquidity_thins_away_from_the_money(self, market):
        config, _provider, chain = market
        expiry = chain.expiries[0]
        table = {q.strike: q for q in chain.for_expiry(expiry) if q.option_type is OptionType.CALL}
        atm = min(table, key=lambda k: abs(k - config.spot))
        wing = max(table)
        assert table[wing].quote.open_interest < table[atm].quote.open_interest


class TestNoArbitrage:
    def test_put_call_parity_holds_to_within_a_tick(self, market):
        config, _provider, chain = market
        tick = float(config.tick_size)
        for expiry in chain.expiries:
            tau = tau_for(config, expiry)
            forward = float(config.spot) * math.exp(config.risk_free_rate * tau)
            discount = math.exp(-config.risk_free_rate * tau)
            for strike, sides in by_strike(chain, expiry).items():
                residual = float(sides[OptionType.CALL] - sides[OptionType.PUT]) - discount * (
                    forward - float(strike)
                )
                assert abs(residual) <= tick, f"{expiry} {strike}: {residual}"

    def test_static_price_bounds_hold(self, market):
        config, _provider, chain = market
        tick = float(config.tick_size)
        for expiry in chain.expiries:
            tau = tau_for(config, expiry)
            forward = float(config.spot) * math.exp(config.risk_free_rate * tau)
            discount = math.exp(-config.risk_free_rate * tau)
            for strike, sides in by_strike(chain, expiry).items():
                call = float(sides[OptionType.CALL])
                put = float(sides[OptionType.PUT])
                k = float(strike)
                assert call >= max(discount * (forward - k), 0.0) - tick
                assert call <= discount * forward + tick
                assert put >= max(discount * (k - forward), 0.0) - tick
                assert put <= discount * k + tick

    def test_vertical_spreads_are_monotone(self, market):
        config, _provider, chain = market
        tick = float(config.tick_size)
        for expiry in chain.expiries:
            table = by_strike(chain, expiry)
            strikes = sorted(table)
            calls = [float(table[k][OptionType.CALL]) for k in strikes]
            puts = [float(table[k][OptionType.PUT]) for k in strikes]
            assert all(b - a <= tick for a, b in zip(calls, calls[1:], strict=False))
            assert all(a - b <= tick for a, b in zip(puts, puts[1:], strict=False))

    def test_butterflies_are_convex(self, market):
        """Second difference in strike must be non-negative: the discrete form
        of a non-negative risk-neutral density."""
        config, _provider, chain = market
        # Three tick-rounded prices enter each second difference.
        tolerance = 3 * float(config.tick_size)
        for expiry in chain.expiries:
            table = by_strike(chain, expiry)
            strikes = sorted(table)
            for option_type in (OptionType.CALL, OptionType.PUT):
                prices = [float(table[k][option_type]) for k in strikes]
                for i in range(1, len(strikes) - 1):
                    second_difference = prices[i - 1] - 2 * prices[i] + prices[i + 1]
                    assert second_difference >= -tolerance, (
                        f"{expiry} {option_type} at {strikes[i]}: {second_difference}"
                    )

    def test_total_variance_is_non_decreasing_in_maturity(self, market):
        """Calendar-arbitrage freedom, in the coordinate it is natural in."""
        config, provider, chain = market
        expiries = sorted(chain.expiries)
        for log_moneyness in (-0.2, -0.05, 0.0, 0.05, 0.2):
            variances = []
            for expiry in expiries:
                tau = tau_for(config, expiry)
                vol = provider.implied_vol(log_moneyness, tau)
                variances.append(vol * vol * tau)
            assert all(
                later >= earlier - 1e-12
                for earlier, later in zip(variances, variances[1:], strict=False)
            ), f"k={log_moneyness}: {variances}"


class TestBars:
    def test_bars_are_internally_consistent(self, market):
        _config, provider, _chain = market
        from domains.market_data.enums import BarInterval

        bars = asyncio.run(
            provider.get_bars(
                provider.underlying.id,
                BarInterval.M5,
                AS_OF,
                datetime(2026, 9, 24, 10, 20, tzinfo=UTC),
            )
        )
        assert len(bars) == 12
        for bar in bars:
            assert bar.low <= bar.open <= bar.high
            assert bar.low <= bar.close <= bar.high

    def test_bars_are_seed_reproducible(self, market):
        _config, provider, _chain = market
        from domains.market_data.enums import BarInterval

        end = datetime(2026, 9, 24, 10, 20, tzinfo=UTC)
        first = asyncio.run(provider.get_bars(provider.underlying.id, BarInterval.M5, AS_OF, end))
        second = asyncio.run(provider.get_bars(provider.underlying.id, BarInterval.M5, AS_OF, end))
        assert [b.close for b in first] == [b.close for b in second]
