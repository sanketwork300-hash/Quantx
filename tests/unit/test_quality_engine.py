"""The data-quality engine: what it flags, and what it refuses to hide."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from domains.instruments.enums import AssetClass, OptionType
from domains.market_data.models import OptionQuote, Quote
from domains.market_data.quality.config import MarketDataQualityConfig
from domains.market_data.quality.engine import MarketDataQualityEngine, QuoteContext
from domains.market_data.quality.flags import QualityCode, Severity
from tests.tolerances import SCORE_ABS

NOW = datetime(2026, 9, 24, 9, 20, tzinfo=UTC)
EXPIRY = datetime(2026, 10, 29, 10, 0, tzinfo=UTC)
INSTRUMENT = uuid.uuid4()
UNDERLYING = uuid.uuid4()


def make_quote(**overrides) -> Quote:
    kwargs = {
        "instrument_id": INSTRUMENT,
        "exchange_timestamp": NOW,
        "receive_timestamp": NOW,
        "source": "test",
        "bid_price": Decimal("612.00"),
        "ask_price": Decimal("615.00"),
        "bid_size": Decimal("75"),
        "ask_size": Decimal("75"),
        "last_price": Decimal("613.50"),
        "volume": Decimal("18400"),
        "open_interest": Decimal("221500"),
        "sequence_number": 1,
    }
    kwargs.update(overrides)
    return Quote(**kwargs)


def make_option(quote: Quote | None = None, **overrides) -> OptionQuote:
    kwargs = {
        "quote": quote or make_quote(),
        "underlying_id": UNDERLYING,
        "expiry": EXPIRY.date(),
        "strike": Decimal("24000"),
        "option_type": OptionType.CALL,
        "expiry_timestamp": EXPIRY,
        "underlying_price": Decimal("24500"),
    }
    kwargs.update(overrides)
    return OptionQuote(**kwargs)


def context(**overrides) -> QuoteContext:
    kwargs = {
        "asset_class": AssetClass.OPTION,
        "as_of": NOW,
        "tick_size": Decimal("0.05"),
    }
    kwargs.update(overrides)
    return QuoteContext(**kwargs)


@pytest.fixture
def engine() -> MarketDataQualityEngine:
    return MarketDataQualityEngine()


def codes(quality) -> set[QualityCode]:
    return {flag.code for flag in quality.flags}


class TestHealthyQuote:
    def test_a_good_quote_raises_no_flags(self, engine):
        quality = engine.score_option_quote(make_option(), context())
        assert quality.flags == ()
        assert quality.overall_score > 0.9

    def test_every_score_is_within_the_unit_interval(self, engine):
        quality = engine.score_option_quote(make_option(), context())
        for name in (
            "stale_score",
            "spread_score",
            "liquidity_score",
            "consistency_score",
            "completeness_score",
            "overall_score",
        ):
            assert 0.0 <= getattr(quality, name) <= 1.0


class TestConsistencyChecks:
    def test_crossed_market_is_an_error_and_zeroes_the_score(self, engine):
        quote = make_quote(bid_price=Decimal("620"), ask_price=Decimal("615"))
        quality = engine.score_option_quote(make_option(quote), context())
        assert QualityCode.CROSSED_MARKET in codes(quality)
        assert quality.consistency_score == pytest.approx(0.0, abs=SCORE_ABS)
        assert quality.overall_score == pytest.approx(0.0, abs=SCORE_ABS)

    def test_locked_market_is_a_warning_not_an_error(self, engine):
        quote = make_quote(bid_price=Decimal("615"), ask_price=Decimal("615"))
        quality = engine.score_option_quote(make_option(quote), context())
        assert QualityCode.LOCKED_MARKET in codes(quality)
        assert quality.worst_severity is Severity.WARNING

    def test_zero_ask_is_an_error(self, engine):
        quality = engine.score_option_quote(
            make_option(make_quote(ask_price=Decimal(0))), context()
        )
        assert QualityCode.ZERO_ASK in codes(quality)
        assert quality.worst_severity is Severity.ERROR

    def test_negative_price_is_an_error(self, engine):
        quality = engine.score_option_quote(
            make_option(make_quote(bid_price=Decimal("-1"))), context()
        )
        assert QualityCode.NEGATIVE_PRICE in codes(quality)

    def test_missing_both_sides_is_an_error(self, engine):
        quote = make_quote(bid_price=None, ask_price=None)
        quality = engine.score_option_quote(make_option(quote), context())
        assert QualityCode.MISSING_BOTH_SIDES in codes(quality)
        assert quality.worst_severity is Severity.ERROR

    def test_future_timestamp_cannot_belong_to_the_snapshot(self, engine):
        quote = make_quote(exchange_timestamp=NOW + timedelta(minutes=5))
        quality = engine.score_option_quote(make_option(quote), context())
        assert QualityCode.FUTURE_TIMESTAMP in codes(quality)
        assert quality.consistency_score == pytest.approx(0.0, abs=SCORE_ABS)

    def test_receive_before_exchange_is_inconsistent(self, engine):
        quote = make_quote(receive_timestamp=NOW - timedelta(seconds=10))
        quality = engine.score_option_quote(make_option(quote), context())
        assert QualityCode.INCONSISTENT_TIMESTAMPS in codes(quality)

    def test_duplicate_is_reported_by_the_caller_and_scored_here(self, engine):
        quality = engine.score_option_quote(make_option(), context(is_duplicate=True))
        assert QualityCode.DUPLICATE_OBSERVATION in codes(quality)
        assert quality.worst_severity is Severity.ERROR


class TestStaleness:
    def test_decays_with_a_half_life(self, engine):
        quality = engine.score_option_quote(
            make_option(make_quote(exchange_timestamp=NOW - timedelta(seconds=300))),
            context(),
        )
        assert quality.stale_score == pytest.approx(0.5, abs=SCORE_ABS)

    def test_moderately_stale_is_a_warning(self, engine):
        quality = engine.score_option_quote(
            make_option(make_quote(exchange_timestamp=NOW - timedelta(seconds=900))),
            context(),
        )
        assert QualityCode.STALE_QUOTE in codes(quality)
        assert quality.worst_severity is Severity.WARNING

    def test_very_stale_is_an_error(self, engine):
        quality = engine.score_option_quote(
            make_option(make_quote(exchange_timestamp=NOW - timedelta(hours=2))),
            context(),
        )
        assert quality.worst_severity is Severity.ERROR


class TestSpreadAndLiquidity:
    def test_wide_spread_is_flagged_but_kept(self, engine):
        quote = make_quote(bid_price=Decimal("500"), ask_price=Decimal("700"))
        quality = engine.score_option_quote(make_option(quote), context())
        assert QualityCode.WIDE_SPREAD in codes(quality)
        assert quality.worst_severity is Severity.WARNING
        assert quality.spread_score < 0.1

    def test_spread_score_is_a_half_at_the_reference(self, engine):
        # reference_relative_spread for options is 0.02; mid 100 -> spread 2
        quote = make_quote(bid_price=Decimal("99"), ask_price=Decimal("101"))
        quality = engine.score_option_quote(make_option(quote, underlying_price=None), context())
        assert quality.spread_score == pytest.approx(0.5, abs=1e-6)

    def test_dead_contract_is_flagged(self, engine):
        quote = make_quote(volume=Decimal(0), open_interest=Decimal(0))
        quality = engine.score_option_quote(make_option(quote), context())
        assert QualityCode.ILLIQUID_CONTRACT in codes(quality)
        assert quality.liquidity_score < 0.3


class TestOptionBounds:
    """Lower bounds require a stated carry assumption; upper bounds do not."""

    @pytest.fixture
    def carry_engine(self) -> MarketDataQualityEngine:
        return MarketDataQualityEngine(
            MarketDataQualityConfig(assumed_risk_free_rate=0.065, assumed_dividend_yield=0.0)
        )

    def test_sub_intrinsic_check_is_skipped_without_a_carry_assumption(self, engine):
        """Inventing r=0 would flag every legitimate deep ITM European put."""
        quote = make_quote(bid_price=Decimal("3"), ask_price=Decimal("4"))
        quality = engine.score_option_quote(make_option(quote), context())
        assert QualityCode.PRICE_BELOW_INTRINSIC not in codes(quality)

    def test_a_deep_itm_european_put_is_not_flagged_under_positive_rates(self, carry_engine):
        """P = K*exp(-rT) - S is legitimately below the undiscounted K - S."""
        # K = 26000, S = 24500, r = 6.5%, T ~ 0.096y -> intrinsic 1500, but the
        # discounted lower bound is about 1338.
        quote = make_quote(bid_price=Decimal("1380"), ask_price=Decimal("1390"))
        option = make_option(quote, strike=Decimal("26000"), option_type=OptionType.PUT)
        quality = carry_engine.score_option_quote(option, context())
        assert QualityCode.PRICE_BELOW_INTRINSIC not in codes(quality)

    def test_upper_bounds_hold_without_any_assumption(self, engine):
        """C <= S and P <= K are true for every r, q >= 0, so they always run."""
        quote = make_quote(bid_price=Decimal("30000"), ask_price=Decimal("30100"))
        quality = engine.score_option_quote(make_option(quote), context())
        assert QualityCode.PRICE_ABOVE_BOUND in codes(quality)

    def test_grossly_sub_intrinsic_price_is_an_error(self, carry_engine):
        # Intrinsic is 24500 - 24000 = 500; the market says 3.5.
        quote = make_quote(bid_price=Decimal("3"), ask_price=Decimal("4"))
        quality = carry_engine.score_option_quote(make_option(quote), context())
        assert QualityCode.PRICE_BELOW_INTRINSIC in codes(quality)
        assert quality.worst_severity is Severity.ERROR

    def test_a_violation_smaller_than_the_spread_is_only_informational(self, carry_engine):
        """On a discrete strike grid with wide markets, tiny bound violations
        are ubiquitous and not exploitable. Treating them as errors would
        exclude most of a real illiquid chain."""
        # Discounted lower bound at r=6.5%, T~0.096y is about 649.3; this mid
        # sits under it by less than the quoted spread.
        quote = make_quote(bid_price=Decimal("647.00"), ask_price=Decimal("650.00"))
        quality = carry_engine.score_option_quote(make_option(quote), context())
        flags = {flag.code: flag for flag in quality.flags}
        assert QualityCode.PRICE_BELOW_INTRINSIC in flags
        assert flags[QualityCode.PRICE_BELOW_INTRINSIC].severity is Severity.INFO

    def test_expired_option_is_an_error(self, engine):
        past = datetime(2026, 9, 1, 10, 0, tzinfo=UTC)
        option = make_option(expiry=past.date(), expiry_timestamp=past)
        quality = engine.score_option_quote(option, context())
        assert QualityCode.OPTION_EXPIRED in codes(quality)
        assert quality.consistency_score == pytest.approx(0.0, abs=SCORE_ABS)

    def test_bounds_are_skipped_without_an_underlying_price(self, engine):
        quote = make_quote(bid_price=Decimal("3"), ask_price=Decimal("4"))
        quality = engine.score_option_quote(make_option(quote, underlying_price=None), context())
        assert QualityCode.PRICE_BELOW_INTRINSIC not in codes(quality)
        assert QualityCode.MISSING_UNDERLYING_PRICE in codes(quality)

    def test_carry_assumption_is_recorded_on_the_violation(self, carry_engine):
        quote = make_quote(bid_price=Decimal("3"), ask_price=Decimal("4"))
        quality = carry_engine.score_option_quote(make_option(quote), context())
        flag = next(f for f in quality.flags if f.code is QualityCode.PRICE_BELOW_INTRINSIC)
        assert "assumed_risk_free_rate" in flag.context
        assert "assumed_dividend_yield" in flag.context


class TestAggregation:
    def test_one_zero_dimension_zeroes_the_overall_score(self, engine):
        """The point of a geometric mean: a crossed market cannot be averaged
        away by four healthy dimensions."""
        quote = make_quote(bid_price=Decimal("620"), ask_price=Decimal("615"))
        quality = engine.score_option_quote(make_option(quote), context())
        assert quality.liquidity_score > 0.5
        assert quality.completeness_score > 0.5
        assert quality.overall_score == pytest.approx(0.0, abs=SCORE_ABS)

    def test_overall_never_exceeds_the_best_dimension(self, engine):
        quality = engine.score_option_quote(make_option(), context())
        assert quality.overall_score <= max(
            quality.stale_score,
            quality.spread_score,
            quality.liquidity_score,
            quality.consistency_score,
            quality.completeness_score,
        )


class TestPrimaryReason:
    def test_highest_severity_wins(self, engine):
        quote = make_quote(
            bid_price=Decimal("620"),
            ask_price=Decimal("615"),
            exchange_timestamp=NOW - timedelta(seconds=900),
        )
        quality = engine.score_option_quote(make_option(quote), context())
        primary = quality.primary_flag(Severity.ERROR)
        assert primary is not None
        assert primary.severity is Severity.ERROR

    def test_no_primary_reason_when_nothing_meets_the_threshold(self, engine):
        quality = engine.score_option_quote(make_option(), context())
        assert quality.primary_flag(Severity.ERROR) is None

    def test_selection_is_deterministic(self, engine):
        quote = make_quote(bid_price=Decimal("620"), ask_price=Decimal(0))
        first = engine.score_option_quote(make_option(quote), context())
        second = engine.score_option_quote(make_option(quote), context())
        assert first.primary_flag(Severity.ERROR).code is second.primary_flag(Severity.ERROR).code


class TestConfigProvenance:
    def test_every_parameter_is_serialisable(self):
        import json

        payload = MarketDataQualityConfig().to_provenance()
        json.dumps(payload)
        assert "weight_consistency" in payload
        assert "OPTION" in payload["thresholds"]
