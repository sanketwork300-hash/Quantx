"""Portfolio valuation: which price was used, and totals that reconcile.

Carries two Phase 4 acceptance criteria from docs/backlog.md: every valuation
records its ``valuation_method``, and the sum over positions equals the
portfolio total, for value and for every Greek.
"""

from __future__ import annotations

import math
import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from domains.derivatives.surface import SurfaceSliceFit, VolatilitySurface
from domains.instruments.enums import (
    AssetClass,
    ExerciseStyle,
    OptionType,
    SettlementType,
)
from domains.instruments.models import make_instrument
from domains.market_data.market_state import MarketStateBuilder
from domains.market_data.models import Quote
from domains.portfolio.enums import (
    GreekSource,
    PositionSide,
    ValuationMethod,
)
from domains.portfolio.models import Position, PositionGreeks
from domains.portfolio.valuation import (
    STALE_QUOTE_SECONDS,
    PortfolioValuationService,
    ValuationContext,
    ValuationWarning,
)
from quant.volatility.svi import SVIParameters
from quant.volatility.svi_calibration import CalibrationStatus, SVICalibrationResult

AS_OF = datetime(2026, 9, 24, 9, 20, tzinfo=UTC)
EXPIRY = date(2026, 10, 29)
SETTLEMENT = time(10, 0)
SPOT = Decimal("24000")
RATE = 0.065
PARAMS = SVIParameters(a=0.010, b=0.045, rho=-0.55, m=0.015, sigma=0.10)
PORTFOLIO = uuid.uuid4()

UNDERLYING = make_instrument(
    asset_class=AssetClass.INDEX, exchange="SYNTH", symbol="NIFTY", currency="INR"
)


def option(strike: str, option_type: OptionType, multiplier: str = "75", currency: str = "INR"):
    return make_instrument(
        asset_class=AssetClass.OPTION,
        exchange="SYNTH",
        symbol="NIFTY",
        currency=currency,
        multiplier=Decimal(multiplier),
        expiry=EXPIRY,
        strike=Decimal(strike),
        option_type=option_type,
        exercise_style=ExerciseStyle.EUROPEAN,
        settlement_type=SettlementType.CASH,
        underlying_id=UNDERLYING.id,
    )


def position(instrument, quantity: str, average_price: str | None = None, tag: str | None = None):
    return Position(
        id=uuid.uuid4(),
        portfolio_id=PORTFOLIO,
        instrument_id=instrument.id,
        quantity=Decimal(quantity),
        side=PositionSide.for_quantity(Decimal(quantity)),
        average_price=None if average_price is None else Decimal(average_price),
        strategy_tag=tag,
    )


def quote(instrument, bid: str, ask: str, age_seconds: float = 0.0, last: str | None = None):
    stamp = AS_OF - timedelta(seconds=age_seconds)
    return Quote(
        instrument_id=instrument.id,
        exchange_timestamp=stamp,
        receive_timestamp=stamp,
        source="test",
        bid_price=None if bid is None else Decimal(bid),
        ask_price=None if ask is None else Decimal(ask),
        last_price=None if last is None else Decimal(last),
    )


def surface(tau: float = 0.0960) -> VolatilitySurface:
    return VolatilitySurface(
        underlying_id=UNDERLYING.id,
        as_of=AS_OF,
        slices=(
            SurfaceSliceFit(
                expiry=EXPIRY,
                time_to_expiry=tau,
                forward=float(SPOT) * math.exp(RATE * tau),
                discount_factor=math.exp(-RATE * tau),
                parameters=PARAMS,
                calibration=SVICalibrationResult(
                    parameters=PARAMS,
                    status=CalibrationStatus.CONVERGED,
                    n_observations=21,
                    rmse_vol_points=0.05,
                    constraints_satisfied=True,
                ),
                k_min=-0.20,
                k_max=0.20,
            ),
        ),
        curve_id="curve:test",
    )


def context(*quotes, with_surface: bool = True, settlement=SETTLEMENT, fx=None):
    builder = MarketStateBuilder(AS_OF).add_spot(UNDERLYING.id, SPOT)
    for one in quotes:
        builder.add_quote(one)
    for pair, rate in (fx or {}).items():
        builder.add_fx_rate(pair, Decimal(rate))
    return ValuationContext(
        market_state=builder.build(),
        base_currency="INR",
        surfaces={UNDERLYING.id: surface()} if with_surface else {},
        risk_free_rate=RATE,
        dividend_yield=0.0,
        settlement_time_utc=settlement,
    )


def value(positions, instruments, ctx):
    return PortfolioValuationService().value(PORTFOLIO, positions, instruments, ctx)


class TestObservationAndEstimateAreSeparate:
    """The non-negotiable rule: neither field is ever written from the other."""

    def test_a_quoted_option_is_marked_to_market_and_still_carries_a_model_price(self):
        call = option("24000", OptionType.CALL)
        result = value(
            [position(call, "2")], {call.id: call}, context(quote(call, "420.00", "421.00"))
        )
        one = result.valuations[0]

        assert one.valuation_method is ValuationMethod.MARKET_MID
        assert one.market_price == Decimal("420.5")
        assert one.price_used == one.market_price
        # The model price is recorded next to it, never in place of it.
        assert one.model_price is not None
        assert one.model_price != one.market_price

    def test_an_unquoted_option_is_marked_to_model_and_says_so(self):
        call = option("24000", OptionType.CALL)
        result = value([position(call, "2")], {call.id: call}, context())
        one = result.valuations[0]

        assert one.valuation_method is ValuationMethod.MODEL_REFERENCE
        assert one.market_price is None
        assert one.price_used == one.model_price
        assert one.greek_source is GreekSource.REFERENCE_IV

    def test_with_neither_a_quote_nor_a_surface_nothing_is_invented(self):
        call = option("24000", OptionType.CALL)
        result = value([position(call, "2")], {call.id: call}, context(with_surface=False))
        one = result.valuations[0]

        assert one.valuation_method is ValuationMethod.UNAVAILABLE
        assert one.price_used is None
        assert one.base_market_value is None
        assert ValuationWarning.NO_PRICE in one.warnings
        assert ValuationWarning.NO_SURFACE in one.warnings

    def test_an_implied_vol_from_the_contracts_own_price_is_preferred(self):
        """It reprices the observation exactly; the surface does not."""
        call = option("24000", OptionType.CALL)
        result = value(
            [position(call, "1")], {call.id: call}, context(quote(call, "420.00", "421.00"))
        )
        assert result.valuations[0].greek_source is GreekSource.MARKET_IV


class TestMethodIsAlwaysRecorded:
    @pytest.mark.parametrize(
        ("quotes", "with_surface", "expected"),
        [
            (("420.00", "421.00", 0.0, None), True, ValuationMethod.MARKET_MID),
            (
                ("420.00", "421.00", STALE_QUOTE_SECONDS + 60, None),
                True,
                ValuationMethod.STALE_MARKET,
            ),
            ((None, None, 0.0, "419.75"), True, ValuationMethod.MARKET_LAST),
            (None, True, ValuationMethod.MODEL_REFERENCE),
            (None, False, ValuationMethod.UNAVAILABLE),
        ],
    )
    def test_every_valuation_records_how_it_was_priced(self, quotes, with_surface, expected):
        """Phase 4 acceptance: no position is priced anonymously."""
        call = option("24000", OptionType.CALL)
        args = () if quotes is None else (quote(call, quotes[0], quotes[1], quotes[2], quotes[3]),)
        result = value(
            [position(call, "1")],
            {call.id: call},
            context(*args, with_surface=with_surface),
        )
        assert result.valuations[0].valuation_method is expected

    def test_a_stale_quote_is_used_but_flagged(self):
        """Kept, not dropped: an old price is information, silence is not."""
        call = option("24000", OptionType.CALL)
        result = value(
            [position(call, "1")],
            {call.id: call},
            context(quote(call, "420.00", "421.00", STALE_QUOTE_SECONDS + 60)),
        )
        one = result.valuations[0]
        assert one.valuation_method is ValuationMethod.STALE_MARKET
        assert one.price_used == Decimal("420.5")
        assert ValuationWarning.STALE_QUOTE in one.warnings
        assert one.quote_age_seconds > STALE_QUOTE_SECONDS

    def test_a_position_whose_instrument_is_gone_is_reported_not_guessed(self):
        call = option("24000", OptionType.CALL)
        result = value([position(call, "1")], {}, context())
        one = result.valuations[0]
        assert one.valuation_method is ValuationMethod.UNAVAILABLE
        assert one.asset_class == "UNRESOLVED"
        assert "POSITION_INSTRUMENT_MISSING" in one.warnings

    def test_an_assumed_multiplier_is_carried_into_the_valuation(self):
        call = make_instrument(
            asset_class=AssetClass.OPTION,
            exchange="SYNTH",
            symbol="NIFTY",
            currency="INR",
            expiry=EXPIRY,
            strike=Decimal("24000"),
            option_type=OptionType.CALL,
            exercise_style=ExerciseStyle.EUROPEAN,
            settlement_type=SettlementType.CASH,
            underlying_id=UNDERLYING.id,
            metadata={"multiplier_source": "platform_default"},
        )
        result = value([position(call, "1")], {call.id: call}, context())
        assert ValuationWarning.MULTIPLIER_ASSUMED in result.valuations[0].warnings


class TestTimeToExpiry:
    def test_without_a_settlement_time_no_greeks_are_produced(self):
        """Time to expiry is undefined, so it is left undefined."""
        call = option("24000", OptionType.CALL)
        result = value(
            [position(call, "1")],
            {call.id: call},
            context(quote(call, "420.00", "421.00"), settlement=None),
        )
        one = result.valuations[0]
        assert one.time_to_expiry is None
        assert one.greeks == PositionGreeks()
        assert ValuationWarning.NO_GREEKS in one.warnings
        # The observed price is still an observation; only the model half is absent.
        assert one.market_price == Decimal("420.5")


class TestCurrency:
    def test_a_position_in_another_currency_needs_a_rate_from_the_same_snapshot(self):
        call = option("24000", OptionType.CALL, currency="USD")
        result = value([position(call, "1")], {call.id: call}, context())
        one = result.valuations[0]
        assert one.base_market_value is None
        assert ValuationWarning.NO_FX_RATE in one.warnings

    def test_the_rate_used_is_recorded_on_the_position(self):
        call = option("24000", OptionType.CALL, currency="USD")
        result = value([position(call, "1")], {call.id: call}, context(fx={"USDINR": "83.25"}))
        one = result.valuations[0]
        assert one.fx_rate == Decimal("83.25")
        assert one.base_market_value == one.market_value * Decimal("83.25")


class TestTotalsReconcile:
    @pytest.fixture
    def valued(self):
        call = option("24000", OptionType.CALL)
        put = option("23200", OptionType.PUT)
        far = option("25400", OptionType.CALL)
        instruments = {i.id: i for i in (call, put, far, UNDERLYING)}
        positions = [
            position(call, "2", "410.00", tag="atm"),
            position(put, "-3", "60.00", tag="carry"),
            position(far, "4", "88.00", tag="wings"),
            position(UNDERLYING, "150", "23980.00", tag="hedge"),
        ]
        ctx = context(
            quote(call, "420.00", "421.00"),
            quote(put, "58.00", "58.50"),
        )
        return value(positions, instruments, ctx)

    def test_position_values_sum_to_the_portfolio_value(self, valued):
        total = sum(
            (v.base_market_value for v in valued.valuations if v.base_market_value is not None),
            Decimal(0),
        )
        assert valued.base_market_value == total

    def test_every_aggregate_dimension_sums_to_the_portfolio_total(self, valued):
        """Every dimension sums the same per-position numbers."""
        dimensions = {bucket.dimension for bucket in valued.aggregates}
        assert dimensions == {
            "UNDERLYING",
            "EXPIRY",
            "ASSET_CLASS",
            "STRATEGY_TAG",
            "CURRENCY",
        }
        for dimension in dimensions - {"EXPIRY"}:
            buckets = [b for b in valued.aggregates if b.dimension == dimension]
            assert sum((b.base_market_value for b in buckets), Decimal(0)) == (
                valued.base_market_value
            )
            assert sum(b.positions for b in buckets) == len(valued.valuations)

    def test_the_expiry_dimension_covers_only_dated_positions(self, valued):
        """The index leg has no expiry, so it is absent rather than bucketed
        under a fabricated one."""
        buckets = [b for b in valued.aggregates if b.dimension == "EXPIRY"]
        assert sum(b.positions for b in buckets) == 3

    def test_greeks_sum_the_same_way(self, valued):
        for dimension in ("UNDERLYING", "ASSET_CLASS", "CURRENCY", "STRATEGY_TAG"):
            buckets = [b for b in valued.aggregates if b.dimension == dimension]
            for name in ("delta", "gamma", "vega_per_vol_point", "theta_per_day", "rho_per_bp"):
                total = sum(getattr(b.greeks, name) for b in buckets)
                assert total == pytest.approx(getattr(valued.greeks, name), rel=1e-12, abs=1e-9)

    def test_a_short_leg_reduces_net_but_adds_to_gross(self, valued):
        assert valued.gross_exposure > abs(valued.net_exposure)


@st.composite
def portfolios(draw):
    """Random mixes of long, short, quoted, unquoted and unresolvable legs."""
    strikes = draw(
        st.lists(
            st.sampled_from(["22600", "23200", "24000", "24800", "25400"]),
            min_size=1,
            max_size=5,
            unique=True,
        )
    )
    legs = []
    for strike in strikes:
        option_type = draw(st.sampled_from([OptionType.CALL, OptionType.PUT]))
        quantity = draw(st.integers(min_value=-20, max_value=20).filter(lambda q: q != 0))
        quoted = draw(st.booleans())
        legs.append((option(strike, option_type), quantity, quoted))
    return legs


class TestSumProperty:
    @given(legs=portfolios())
    @settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    def test_the_sum_over_positions_equals_the_portfolio_total(self, legs):
        """Phase 4 acceptance, over arbitrary mixes of long and short legs.

        Exact for the value because it is Decimal throughout; the Greeks are
        float sums and are compared to float tolerance.
        """
        instruments = {instrument.id: instrument for instrument, _q, _quoted in legs}
        positions = [position(instrument, str(q)) for instrument, q, _quoted in legs]
        quotes = [
            quote(instrument, "100.00", "101.00") for instrument, _q, quoted in legs if quoted
        ]
        result = value(positions, instruments, context(*quotes))

        assert result.base_market_value == sum(
            (v.base_market_value for v in result.valuations if v.base_market_value is not None),
            Decimal(0),
        )
        for name in ("delta", "gamma", "vega_per_vol_point", "theta_per_day", "rho_per_bp"):
            assert getattr(result.greeks, name) == pytest.approx(
                sum(getattr(v.greeks, name) for v in result.valuations), rel=1e-9, abs=1e-9
            )

    @given(legs=portfolios())
    @settings(max_examples=40, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    def test_every_position_carries_a_method_whatever_the_inputs(self, legs):
        instruments = {instrument.id: instrument for instrument, _q, _quoted in legs}
        positions = [position(instrument, str(q)) for instrument, q, _quoted in legs]
        result = value(positions, instruments, context())

        assert len(result.valuations) == len(positions)
        for valuation in result.valuations:
            assert valuation.valuation_method in set(ValuationMethod)
            assert (valuation.base_market_value is None) == (
                valuation.valuation_method is ValuationMethod.UNAVAILABLE
            )
