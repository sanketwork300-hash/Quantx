"""Position valuation and portfolio aggregation.

Pure computation over a ``MarketState`` and, for options, a fitted surface.
No I/O and no session, so it is unit-testable without a database and the whole
portfolio is valued against **one** timestamp-consistent snapshot — which is the
point of ``MarketState`` and the reason a delta and a vega in the same report
cannot come from different minutes.

Three rules, all visible in the code:

* ``market_price`` and ``model_price`` are separate fields and neither is ever
  written from the other. ``price_used`` says which one was taken and
  ``valuation_method`` says why.
* A position Greek is ``signed quantity x multiplier x unit Greek``, scaled
  exactly once, here.
* Nothing is valued silently at a fallback. A last-trade price, a stale quote
  and a model price are three different ``valuation_method`` values, and a
  position that could not be valued says so rather than contributing zero.
"""

from __future__ import annotations

import math
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from domains.derivatives.surface import VolatilitySurface
from domains.instruments.enums import OptionType
from domains.instruments.models import Instrument
from domains.market_data.market_state import MarketState
from domains.portfolio.enums import (
    AggregationDimension,
    GreekSource,
    ValuationMethod,
)
from domains.portfolio.models import (
    UNRESOLVED_ASSET_CLASS,
    AggregateBucket,
    PortfolioValuation,
    Position,
    PositionGreeks,
    PositionValuation,
)
from quant.daycount import DEFAULT_DAY_COUNT, DayCount, year_fraction
from quant.pricing.black_scholes import bsm_greeks
from quant.volatility.implied import implied_vol_black76

VALUATION_MODEL_VERSION = "portfolio-valuation@1.0.0"

#: A quote older than this at the valuation time is used but marked
#: ``STALE_MARKET``. Not rejected: refusing to value a book because one leg is
#: an hour old is worse than valuing it with a visible label.
STALE_QUOTE_SECONDS = 900.0


class ValuationWarning:
    NO_PRICE = "POSITION_NO_PRICE"
    NO_UNDERLYING = "POSITION_NO_UNDERLYING_PRICE"
    NO_SURFACE = "POSITION_NO_SURFACE"
    EXPIRED = "POSITION_EXPIRED"
    STALE_QUOTE = "POSITION_STALE_QUOTE"
    LAST_PRICE_FALLBACK = "POSITION_LAST_PRICE_FALLBACK"
    NO_FX_RATE = "POSITION_NO_FX_RATE"
    NO_GREEKS = "POSITION_NO_GREEKS"
    MULTIPLIER_ASSUMED = "POSITION_MULTIPLIER_ASSUMED"


@dataclass(frozen=True, slots=True)
class ValuationContext:
    """Everything the valuation needs, frozen at one instant."""

    market_state: MarketState
    base_currency: str
    #: Per underlying, the surface used to value its options. Absent means
    #: options on that underlying are valued from their own market price only.
    surfaces: dict[uuid.UUID, VolatilitySurface]
    risk_free_rate: float = 0.0
    dividend_yield: float = 0.0
    day_count: DayCount = DEFAULT_DAY_COUNT
    #: Settlement instant on an expiry date. Without it, time to expiry is
    #: undefined and option Greeks are not produced.
    settlement_time_utc: object | None = None

    @property
    def as_of(self) -> datetime:
        return self.market_state.as_of

    def to_provenance(self) -> dict:
        return {
            "market_state_id": self.market_state.state_id,
            "base_currency": self.base_currency,
            "risk_free_rate": self.risk_free_rate,
            "dividend_yield": self.dividend_yield,
            "day_count": str(self.day_count),
            "settlement_time_utc": (
                self.settlement_time_utc.isoformat() if self.settlement_time_utc else None
            ),
            "surfaces": {
                str(underlying): surface.surface_id for underlying, surface in self.surfaces.items()
            },
            "stale_quote_seconds": STALE_QUOTE_SECONDS,
        }


class PortfolioValuationService:
    """Values a portfolio against one snapshot."""

    def value(
        self,
        portfolio_id: uuid.UUID,
        positions: list[Position],
        instruments: dict[uuid.UUID, Instrument],
        context: ValuationContext,
    ) -> PortfolioValuation:
        valuations = [
            self._value_position(position, instruments.get(position.instrument_id), context)
            for position in positions
        ]
        return PortfolioValuation(
            portfolio_id=portfolio_id,
            base_currency=context.base_currency,
            as_of=context.as_of,
            market_state_id=context.market_state.state_id,
            valuations=tuple(valuations),
            aggregates=tuple(
                aggregate(
                    valuations,
                    instruments,
                    {p.id: p.strategy_tag for p in positions if p.strategy_tag},
                )
            ),
        )

    # ------------------------------------------------------------- one position
    def _value_position(
        self,
        position: Position,
        instrument: Instrument | None,
        context: ValuationContext,
    ) -> PositionValuation:
        if instrument is None:
            return PositionValuation(
                position_id=position.id,
                instrument_id=position.instrument_id,
                canonical_key="<unresolved>",
                asset_class=UNRESOLVED_ASSET_CLASS,
                quantity=position.quantity,
                # Nothing is priced, so this multiplier scales nothing; it is
                # here because the column is not nullable, not as a claim.
                multiplier=Decimal(1),
                currency=context.base_currency,
                warnings=("POSITION_INSTRUMENT_MISSING",),
            )

        warnings: list[str] = []
        if instrument.multiplier_is_assumed:
            warnings.append(ValuationWarning.MULTIPLIER_ASSUMED)

        market_price, method, age = self._market_price(instrument, context, warnings)
        model_price, implied_vol, greek_source, tau, unit_greeks = self._model(
            instrument, context, market_price, warnings
        )

        price_used, method = self._choose_price(market_price, model_price, method, warnings)

        scale = position.quantity * instrument.multiplier
        market_value = None if price_used is None else price_used * scale

        fx_rate, base_value = self._convert(market_value, instrument.currency, context, warnings)
        unrealized = None
        if market_value is not None and position.average_price is not None and fx_rate is not None:
            cost = position.average_price * scale
            unrealized = (market_value - cost) * fx_rate

        greeks = PositionGreeks()
        if unit_greeks is not None and fx_rate is not None:
            greeks = unit_greeks.scaled(float(scale) * float(fx_rate))
        elif instrument.is_option:
            warnings.append(ValuationWarning.NO_GREEKS)

        return PositionValuation(
            position_id=position.id,
            instrument_id=instrument.id,
            canonical_key=instrument.canonical_key,
            asset_class=instrument.asset_class,
            quantity=position.quantity,
            multiplier=instrument.multiplier,
            currency=instrument.currency,
            market_price=market_price,
            model_price=model_price,
            price_used=price_used,
            valuation_method=method,
            market_value=market_value,
            base_market_value=base_value,
            fx_rate=fx_rate,
            unrealized_pnl=unrealized,
            greeks=greeks,
            greek_source=greek_source,
            implied_volatility=implied_vol,
            time_to_expiry=tau,
            underlying_id=instrument.underlying_id,
            expiry=instrument.expiry,
            strike=instrument.strike,
            option_type=instrument.option_type,
            quote_age_seconds=age,
            warnings=tuple(warnings),
        )

    def _market_price(
        self, instrument: Instrument, context: ValuationContext, warnings: list[str]
    ) -> tuple[Decimal | None, ValuationMethod, float | None]:
        quote = context.market_state.quotes.get(instrument.id)
        if quote is None:
            spot = context.market_state.spot_prices.get(instrument.id)
            if spot is not None:
                return spot, ValuationMethod.MARKET_MID, None
            return None, ValuationMethod.UNAVAILABLE, None

        age = quote.age_seconds(context.as_of)
        mid = quote.mid_price
        if mid is not None:
            method = (
                ValuationMethod.STALE_MARKET
                if age > STALE_QUOTE_SECONDS
                else ValuationMethod.MARKET_MID
            )
            if method is ValuationMethod.STALE_MARKET:
                warnings.append(ValuationWarning.STALE_QUOTE)
            return mid, method, age

        if quote.last_price is not None and quote.last_price > 0:
            # An explicit, recorded substitution: a print is not a market.
            warnings.append(ValuationWarning.LAST_PRICE_FALLBACK)
            return quote.last_price, ValuationMethod.MARKET_LAST, age

        return None, ValuationMethod.UNAVAILABLE, age

    def _model(
        self,
        instrument: Instrument,
        context: ValuationContext,
        market_price: Decimal | None,
        warnings: list[str],
    ) -> tuple[Decimal | None, float | None, GreekSource, float | None, PositionGreeks | None]:
        """Model price, implied volatility and per-unit Greeks.

        Linear instruments have a delta of one and nothing else; options need a
        volatility, a spot and a time to expiry, and say which they lacked.
        """
        if not instrument.is_option:
            # Linear in its own underlying: delta one per unit, and no second
            # order sensitivity to report. Nothing here is a model estimate.
            return None, None, GreekSource.NOT_APPLICABLE, None, PositionGreeks(delta=1.0)

        underlying_id = instrument.underlying_id
        spot = context.market_state.spot_prices.get(underlying_id)
        if spot is None:
            quote = context.market_state.quotes.get(underlying_id)
            spot = None if quote is None else (quote.mid_price or quote.last_price)
        if spot is None:
            warnings.append(ValuationWarning.NO_UNDERLYING)
            return None, None, GreekSource.UNAVAILABLE, None, None

        tau = self._time_to_expiry(instrument, context)
        if tau is None or tau <= 0:
            warnings.append(ValuationWarning.EXPIRED)
            return None, None, GreekSource.UNAVAILABLE, tau, None

        surface = context.surfaces.get(underlying_id)
        model_price = None
        reference_iv = None
        if surface is not None:
            reference = surface.reference(
                instrument.strike, instrument.expiry, instrument.option_type
            )
            if reference.ok:
                reference_iv = reference.reference_iv
                if reference.reference_price is not None:
                    model_price = Decimal(str(reference.reference_price))
        else:
            warnings.append(ValuationWarning.NO_SURFACE)

        # Prefer the volatility implied by this contract's own observed price:
        # it reprices the observation exactly. Fall back to the surface, and say
        # which was used.
        implied_vol, source = self._implied_vol(
            instrument, context, market_price, float(spot), tau, reference_iv
        )
        if implied_vol is None:
            return model_price, None, GreekSource.UNAVAILABLE, tau, None

        greeks = bsm_greeks(
            float(spot),
            float(instrument.strike),
            tau,
            context.risk_free_rate,
            context.dividend_yield,
            implied_vol,
            instrument.option_type is OptionType.CALL,
        )
        return (
            model_price,
            implied_vol,
            source,
            tau,
            PositionGreeks(
                delta=float(greeks.delta),
                gamma=float(greeks.gamma),
                vega_per_vol_point=float(greeks.vega_per_vol_point),
                theta_per_day=float(greeks.theta_per_day),
                rho_per_bp=float(greeks.rho_per_bp),
            ),
        )

    def _implied_vol(
        self,
        instrument: Instrument,
        context: ValuationContext,
        market_price: Decimal | None,
        spot: float,
        tau: float,
        reference_iv: float | None,
    ) -> tuple[float | None, GreekSource]:
        if market_price is not None and market_price > 0:
            forward = spot * math.exp((context.risk_free_rate - context.dividend_yield) * tau)
            discount = math.exp(-context.risk_free_rate * tau)
            result = implied_vol_black76(
                float(market_price),
                forward,
                float(instrument.strike),
                tau,
                instrument.option_type is OptionType.CALL,
                discount,
            )
            if result.implied_volatility is not None:
                return result.implied_volatility, GreekSource.MARKET_IV

        if reference_iv is not None:
            return reference_iv, GreekSource.REFERENCE_IV
        return None, GreekSource.UNAVAILABLE

    @staticmethod
    def _time_to_expiry(instrument: Instrument, context: ValuationContext) -> float | None:
        if instrument.expiry is None or context.settlement_time_utc is None:
            return None
        instant = datetime.combine(
            instrument.expiry, context.settlement_time_utc, tzinfo=context.as_of.tzinfo
        )
        return year_fraction(context.as_of, instant, context.day_count)

    @staticmethod
    def _choose_price(
        market_price: Decimal | None,
        model_price: Decimal | None,
        method: ValuationMethod,
        warnings: list[str],
    ) -> tuple[Decimal | None, ValuationMethod]:
        if market_price is not None:
            return market_price, method
        if model_price is not None:
            return model_price, ValuationMethod.MODEL_REFERENCE
        warnings.append(ValuationWarning.NO_PRICE)
        return None, ValuationMethod.UNAVAILABLE

    @staticmethod
    def _convert(
        value: Decimal | None,
        currency: str,
        context: ValuationContext,
        warnings: list[str],
    ) -> tuple[Decimal | None, Decimal | None]:
        """Convert to the base currency at the rate in the snapshot.

        The rate comes from the same ``MarketState`` as the prices, so a
        portfolio is never converted at a rate from a different moment.
        """
        if currency == context.base_currency:
            return Decimal(1), value
        pair = f"{currency}{context.base_currency}"
        rate = context.market_state.fx_rates.get(pair)
        if rate is None:
            warnings.append(ValuationWarning.NO_FX_RATE)
            return None, None
        return rate, None if value is None else value * rate


def aggregate(
    valuations: list[PositionValuation],
    instruments: dict[uuid.UUID, Instrument],
    strategy_tags: dict[uuid.UUID, str] | None = None,
) -> list[AggregateBucket]:
    """Group by underlying, expiry, asset class and currency.

    Every dimension sums the *same* per-position numbers, so each grouping
    totals to the portfolio total. A property test asserts it.
    """
    buckets: list[AggregateBucket] = []

    def group(dimension: AggregationDimension, key_of, label_of) -> None:
        grouped: dict[str, list[PositionValuation]] = defaultdict(list)
        for valuation in valuations:
            key = key_of(valuation)
            if key is not None:
                grouped[str(key)].append(valuation)
        for key, members in sorted(grouped.items()):
            priced = [m for m in members if m.base_market_value is not None]
            total = sum((m.base_market_value for m in priced), Decimal(0))
            greeks = PositionGreeks()
            for member in members:
                greeks = greeks + member.greeks
            buckets.append(
                AggregateBucket(
                    dimension=str(dimension),
                    key=key,
                    label=label_of(key, members),
                    base_market_value=total,
                    greeks=greeks,
                    positions=len(members),
                    valued=len(priced),
                    gross_exposure=sum((abs(m.base_market_value) for m in priced), Decimal(0)),
                    unrealized_pnl=sum(
                        (m.unrealized_pnl for m in members if m.unrealized_pnl is not None),
                        Decimal(0),
                    ),
                )
            )

    def underlying_label(key: str, members: list[PositionValuation]) -> str:
        instrument = instruments.get(uuid.UUID(key)) if _is_uuid(key) else None
        if instrument is not None:
            return instrument.symbol
        # Fall back to the contract's own root, which is the same symbol. A
        # position whose instrument is gone has no canonical key to read, and
        # is labelled by its id rather than given a name it does not have.
        parts = members[0].canonical_key.split(":") if members else []
        return parts[2] if len(parts) > 2 else key

    group(
        AggregationDimension.UNDERLYING,
        lambda v: v.underlying_id or v.instrument_id,
        underlying_label,
    )
    group(AggregationDimension.EXPIRY, lambda v: v.expiry, lambda key, _m: key)
    group(AggregationDimension.ASSET_CLASS, lambda v: v.asset_class, lambda key, _m: key)
    if strategy_tags:
        group(
            AggregationDimension.STRATEGY_TAG,
            lambda v: strategy_tags.get(v.position_id),
            lambda key, _m: key,
        )
    group(AggregationDimension.CURRENCY, lambda v: v.currency, lambda key, _m: key)
    return buckets


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True
