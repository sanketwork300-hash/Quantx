"""Benchmarks. Excluded from the default run (``-m performance`` to include).

Targets are tracked over time rather than asserted as hard pass/fail, per
docs/testing.md: a benchmark that fails on a busy CI box teaches nothing and
gets muted. What *is* asserted is that the work completes and produces the right
shape, so a benchmark cannot silently measure nothing.
"""

from __future__ import annotations

import time

import numpy as np
import pytest

from quant.pricing.black76 import black76_price

pytestmark = pytest.mark.performance


def _report(label: str, count: int, seconds: float) -> None:
    rate = count / seconds if seconds > 0 else float("inf")
    print(f"\n{label}: {count:,} in {seconds * 1000:.1f} ms ({rate:,.0f}/s)")


@pytest.mark.parametrize("count", [1, 1_000, 100_000])
def test_black76_pricing_throughput(count):
    forwards = np.full(count, 24000.0)
    strikes = np.linspace(18000.0, 30000.0, count)
    vols = np.full(count, 0.18)

    started = time.perf_counter()
    prices = black76_price(forwards, strikes, 0.25, vols, True)
    elapsed = time.perf_counter() - started

    _report(f"black76 x{count}", count, elapsed)
    assert prices.shape == (count,)
    assert np.all(np.isfinite(prices))


@pytest.mark.parametrize("strikes_each_side", [10, 50, 200])
def test_synthetic_chain_generation(strikes_each_side):
    import asyncio
    from datetime import UTC, datetime

    from domains.market_data.providers.synthetic import (
        SyntheticMarketConfig,
        SyntheticMarketDataProvider,
    )

    config = SyntheticMarketConfig(
        as_of=datetime(2026, 9, 24, 9, 20, tzinfo=UTC),
        strikes_each_side=strikes_each_side,
    )
    started = time.perf_counter()
    provider = SyntheticMarketDataProvider(config)
    chain = asyncio.run(provider.get_option_chain(provider.underlying.id))
    elapsed = time.perf_counter() - started

    _report(f"synthetic chain ({len(chain)} quotes)", len(chain), elapsed)
    assert len(chain) == len(config.expiry_days) * (2 * strikes_each_side + 1) * 2


@pytest.mark.parametrize("rows", [1_000, 10_000])
def test_quality_scoring_throughput(rows, clean_chain_csv):
    import uuid
    from datetime import UTC, datetime
    from decimal import Decimal

    from domains.instruments.enums import AssetClass, OptionType
    from domains.market_data.models import OptionQuote, Quote
    from domains.market_data.quality.engine import MarketDataQualityEngine, QuoteContext

    now = datetime(2026, 9, 24, 9, 20, tzinfo=UTC)
    instrument_id, underlying_id = uuid.uuid4(), uuid.uuid4()
    quotes = [
        OptionQuote(
            quote=Quote(
                instrument_id=instrument_id,
                exchange_timestamp=now,
                receive_timestamp=now,
                source="bench",
                bid_price=Decimal("412.10"),
                ask_price=Decimal("415.60"),
                bid_size=Decimal("75"),
                ask_size=Decimal("75"),
                volume=Decimal("1000"),
                open_interest=Decimal("5000"),
            ),
            underlying_id=underlying_id,
            expiry=now.date(),
            strike=Decimal(24000 + index),
            option_type=OptionType.CALL,
            expiry_timestamp=datetime(2026, 10, 29, 10, 0, tzinfo=UTC),
            underlying_price=Decimal("24012.35"),
        )
        for index in range(rows)
    ]
    engine = MarketDataQualityEngine()
    context = QuoteContext(asset_class=AssetClass.OPTION, as_of=now)

    started = time.perf_counter()
    scored = [engine.score_option_quote(quote, context) for quote in quotes]
    elapsed = time.perf_counter() - started

    _report(f"quality scoring x{rows}", rows, elapsed)
    assert len(scored) == rows
