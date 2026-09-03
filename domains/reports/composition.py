"""Cross-engine composition.

``domains.reports`` is the only domain permitted to fan out across the engines,
and it may only *compose* their results (docs/architecture.md section 4). This
module assembles a ``MarketState`` from the market-data half and the derivatives
half without either domain importing the other.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime, time, timedelta

from domains.derivatives.application import DerivativesService
from domains.derivatives.surface import VolatilitySurface
from domains.execution.benchmarks import MarketObservation, MarketWindow
from domains.market_data.market_state import MarketState
from domains.market_data.service import MarketDataService
from domains.portfolio.valuation import ValuationContext
from domains.risk.factors import (
    FactorKind,
    FactorSeries,
    HistorySource,
    spot_factor_name,
    volatility_factor_name,
)


class MarketStateComposer:
    def __init__(self, market_data: MarketDataService, derivatives: DerivativesService) -> None:
        self._market_data = market_data
        self._derivatives = derivatives

    async def build(
        self,
        user_id: uuid.UUID,
        underlying_id: uuid.UUID,
        as_of: datetime | None = None,
        risk_free_rate: float | None = None,
        include_surface: bool = True,
    ) -> MarketState | None:
        builder = await self._market_data.build_market_state(
            user_id, underlying_id, as_of=as_of, risk_free_rate=risk_free_rate
        )
        if builder is None:
            return None

        if include_surface:
            loaded = await self._derivatives.latest_surface(user_id, underlying_id)
            if loaded is not None:
                row, surface = loaded
                builder.add_surface(underlying_id, surface.model, surface.surface_id)
                builder.add_source(f"surface:{row.model_version}", surface.surface_id)
        return builder.build()


class ValuationContextComposer:
    """Assembles what a portfolio valuation needs from two engines.

    Market data owns quotes, spot prices and FX; derivatives owns surfaces.
    Neither imports the other, so the composition lives here — ``domains.reports``
    is the only domain permitted to fan out (docs/architecture.md section 4).
    """

    def __init__(self, market_data: MarketDataService, derivatives: DerivativesService) -> None:
        self._market_data = market_data
        self._derivatives = derivatives

    async def build(
        self,
        user_id: uuid.UUID,
        underlying_ids: set[uuid.UUID],
        base_currency: str,
        as_of: datetime | None = None,
        risk_free_rate: float = 0.0,
        dividend_yield: float = 0.0,
        settlement_time_utc: time | None = None,
    ) -> ValuationContext | None:
        """One snapshot covering every underlying the portfolio touches.

        A single ``MarketState`` for the whole portfolio is the point: a delta
        and a vega in the same report then cannot come from different minutes.

        Each underlying's latest chain arrives with its own timestamp, so the
        common ``as_of`` is the *latest* of them. Taking the earliest instead
        would place some quotes after ``as_of`` and the builder would reject
        them; taking the latest keeps every quote and lets the older ones be
        measured as stale, which is the visible, recorded outcome rather than a
        silent loss of positions.
        """
        from domains.market_data.market_state import MarketStateBuilder

        parts: list[tuple[uuid.UUID, MarketState]] = []
        surfaces: dict[uuid.UUID, VolatilitySurface] = {}

        for underlying_id in sorted(underlying_ids, key=str):
            builder = await self._market_data.build_market_state(
                user_id, underlying_id, as_of=as_of, risk_free_rate=risk_free_rate
            )
            if builder is None:
                continue
            parts.append((underlying_id, builder.build()))

            loaded = await self._derivatives.latest_surface(user_id, underlying_id)
            if loaded is not None:
                _row, surface = loaded
                surfaces[underlying_id] = surface

        if not parts:
            return None

        moment = as_of or max(state.as_of for _, state in parts)
        merged = MarketStateBuilder(moment)
        for underlying_id, state in parts:
            for quote in state.quotes.values():
                merged.add_quote(quote)
            for instrument_id, price in state.spot_prices.items():
                merged.add_spot(instrument_id, price)
            for instrument_id, price in state.future_prices.items():
                merged.add_future(instrument_id, price)
            for curve in state.yield_curves.values():
                merged.add_curve(curve)
            for pair, rate in state.fx_rates.items():
                merged.add_fx_rate(pair, rate)
            for source in state.sources:
                merged.add_source(source, state.data_versions.get(source))

            surface = surfaces.get(underlying_id)
            if surface is not None:
                merged.add_surface(underlying_id, surface.model, surface.surface_id)
                merged.add_source(f"surface:{surface.model}", surface.surface_id)

        return ValuationContext(
            market_state=merged.build(),
            base_currency=base_currency,
            surfaces=surfaces,
            risk_free_rate=risk_free_rate,
            dividend_yield=dividend_yield,
            settlement_time_utc=settlement_time_utc,
        )


#: The tenor whose at-the-money level stands in for "the volatility" of an
#: underlying. Thirty days is the market's own convention for a headline
#: volatility number, and it is a convention, not a derivation.
VOLATILITY_FACTOR_TENOR_DAYS = 30


class FactorHistoryComposer:
    """Assembles the factor histories a risk run needs from two engines.

    The price history is one observation per ingested option chain; the
    volatility history is one per calibrated surface, read at a fixed tenor so
    the series survives expiries rolling off. Both come from work the platform
    already did for other reasons, which is why the lookback is exactly as long
    as the user's own record and is reported as such.
    """

    def __init__(self, market_data: MarketDataService, derivatives: DerivativesService) -> None:
        self._market_data = market_data
        self._derivatives = derivatives

    async def build(
        self,
        user_id: uuid.UUID,
        underlying_ids: Sequence[uuid.UUID],
        limit: int = 500,
        tenor_days: int = VOLATILITY_FACTOR_TENOR_DAYS,
        include_volatility: bool = True,
    ) -> list[FactorSeries]:
        series: list[FactorSeries] = []

        for underlying_id in sorted(set(underlying_ids), key=str):
            key = str(underlying_id)
            prices = await self._market_data.underlying_price_history(
                user_id, underlying_id, limit=limit
            )
            if len(prices) >= 2:
                dates, levels = _daily([(stamp, float(price)) for stamp, price in prices])
                series.append(
                    FactorSeries(
                        name=spot_factor_name(key),
                        kind=FactorKind.SPOT_RETURN,
                        target=key,
                        source=HistorySource.CHAIN_SNAPSHOTS,
                        dates=dates,
                        levels=levels,
                    )
                )

            if not include_volatility:
                continue
            history = await self._derivatives.tenor_history(
                user_id, underlying_id, tenor_days, limit=limit
            )
            points = [
                (
                    datetime.fromisoformat(point["as_of_timestamp"]),
                    float(point["atm_volatility"]),
                )
                for point in history.series
                if point.get("atm_volatility") is not None
            ]
            if len(points) >= 2:
                dates, levels = _daily(points)
                series.append(
                    FactorSeries(
                        name=volatility_factor_name(key),
                        kind=FactorKind.VOLATILITY_CHANGE,
                        target=key,
                        source=HistorySource.SURFACE_CHARACTERISTICS,
                        dates=dates,
                        levels=levels,
                    )
                )
        return series


def _daily(points: Sequence[tuple[datetime, float]]) -> tuple[tuple, tuple]:
    """Collapse timestamps to dates, keeping the last observation of each day.

    Two chains ingested on the same day are two views of one day, not two days
    of returns; treating them as two would put a spurious zero-ish return into
    every volatility estimate downstream.
    """
    by_day: dict = {}
    for stamp, level in sorted(points, key=lambda item: item[0]):
        by_day[stamp.date()] = level
    ordered = sorted(by_day.items())
    return tuple(day for day, _ in ordered), tuple(level for _, level in ordered)


class ExecutionWindowComposer:
    """Assembles the market observations a benchmark window can be built from.

    Two sources, and neither was created for this: the option quotes stored with
    each ingested chain, and the underlying level recorded on the snapshot
    itself. Both carry a two-sided market, so a spread is available at each
    observation — which is what makes a spread charge attributable at all.

    Neither carries *interval* volume. A snapshot's volume is cumulative for the
    session, and attributing it to the moment of the snapshot would weight a
    whole day onto one instant, so it is deliberately not passed through: the
    interval VWAP benchmark then reports itself unavailable rather than becoming
    a time-weighted average wearing a volume-weighted name.
    """

    def __init__(self, market_data: MarketDataService) -> None:
        self._market_data = market_data

    async def build(
        self,
        user_id: uuid.UUID,
        instrument_id: uuid.UUID,
        start: datetime,
        end: datetime,
        underlying_id: uuid.UUID | None = None,
        padding_seconds: float = 3600.0,
        staleness_tolerance_seconds: float = 300.0,
    ) -> MarketWindow:
        """Observations for one instrument over one window, plus padding.

        The padding matters: an arrival benchmark looks *backwards* from the
        submit time and a close benchmark looks forwards from the last fill, so
        a window holding only what falls between the fills could answer neither.
        """
        padding = timedelta(seconds=padding_seconds)
        observations = await self._market_data.instrument_quote_history(
            user_id, instrument_id, start - padding, end + padding
        )
        source = "option_quotes"

        if not observations and underlying_id is not None:
            observations = await self._market_data.underlying_level_history(
                user_id, underlying_id, start - padding, end + padding
            )
            source = "chain_snapshots"

        return MarketWindow(
            instrument_id=instrument_id,
            start=start,
            end=end,
            observations=tuple(
                MarketObservation(
                    timestamp=stamp, price=price, source=source, spread=spread, volume=None
                )
                for stamp, price, spread in observations
            ),
            source=source,
            staleness_tolerance_seconds=staleness_tolerance_seconds,
        )
