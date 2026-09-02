"""Deterministic synthetic market.

This provider matters more than it looks. Market-data availability is the
platform's largest operational constraint (docs/risks.md R1), and a seeded
synthetic market means the entire system — demos, CI, tutorials, quantitative
validation — works with zero external data.

It also gives tests a market whose *true* parameters are known: the surface is
generated from an admissible raw-SVI slice with total variance increasing in
maturity, so the resulting chain is internally arbitrage-free by construction
(put-call parity exactly, static bounds and butterfly/calendar consistency up to
tick rounding). A test asserting those properties is therefore testing the
platform's checks, not the data.

Nothing generated here describes a real market. It must never be used to
validate a claim about real markets — only to validate that code is correct.
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal

import numpy as np

from domains.instruments.enums import (
    AssetClass,
    ExerciseStyle,
    OptionType,
    SettlementType,
)
from domains.instruments.models import Instrument, make_instrument
from domains.market_data.enums import BarInterval, ProviderCapability
from domains.market_data.models import Bar, OptionChain, OptionQuote, Quote
from domains.market_data.providers.base import MarketDataProvider
from quant.pricing.black76 import black76_price
from quant.volatility.svi import SVIParameters, raw_svi_total_variance

SECONDS_PER_YEAR = 365.0 * 24.0 * 3600.0


@dataclass(frozen=True, slots=True)
class SyntheticMarketConfig:
    as_of: datetime
    underlying_symbol: str = "NIFTY"
    exchange: str = "SYNTH"
    currency: str = "INR"
    spot: Decimal = Decimal("24000")
    multiplier: Decimal = Decimal("75")
    tick_size: Decimal = Decimal("0.05")
    lot_size: Decimal = Decimal("75")
    risk_free_rate: float = 0.065
    dividend_yield: float = 0.0
    expiry_days: tuple[int, ...] = (7, 35, 91)
    #: Settlement instant for generated contracts. A parameter of this
    #: synthetic venue, not a claim about any real exchange's calendar.
    expiry_time_utc: time = time(10, 0)
    strike_step: Decimal = Decimal("100")
    strikes_each_side: int = 12
    #: Raw SVI slice defining total variance at one year. Total variance at
    #: maturity ``tau`` is ``w1(k) * tau * (1 + term_slope * tau)``, which is
    #: increasing in ``tau`` and therefore calendar-arbitrage free.
    svi: SVIParameters = field(
        default_factory=lambda: SVIParameters(a=0.010, b=0.045, rho=-0.55, m=0.015, sigma=0.10)
    )
    term_slope: float = 0.05
    atm_relative_spread: float = 0.006
    wing_spread_multiple: float = 6.0
    atm_volume: Decimal = Decimal("25000")
    atm_open_interest: Decimal = Decimal("300000")
    liquidity_decay_k: float = 0.06
    quote_size: Decimal = Decimal("750")
    seed: int = 20_260_924

    def digest(self) -> str:
        payload = {
            "as_of": self.as_of.isoformat(),
            "symbol": self.underlying_symbol,
            "exchange": self.exchange,
            "spot": format(self.spot, "f"),
            "rate": self.risk_free_rate,
            "dividend": self.dividend_yield,
            "expiry_days": list(self.expiry_days),
            "strike_step": format(self.strike_step, "f"),
            "strikes_each_side": self.strikes_each_side,
            "svi": self.svi.to_dict(),
            "term_slope": self.term_slope,
            "seed": self.seed,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()[:16]


class SyntheticMarketDataProvider(MarketDataProvider):
    name = "synthetic"
    capabilities = frozenset(
        {
            ProviderCapability.INSTRUMENTS,
            ProviderCapability.QUOTES,
            ProviderCapability.OPTION_CHAINS,
            ProviderCapability.BARS,
        }
    )

    def __init__(self, config: SyntheticMarketConfig) -> None:
        self.config = config
        self._rng = np.random.default_rng(config.seed)
        self._instruments: dict[uuid.UUID, Instrument] = {}
        self._quotes: dict[uuid.UUID, Quote] = {}
        self._underlying: Instrument | None = None
        self._build()

    @property
    def dataset_version(self) -> str:
        return f"synthetic:{self.config.digest()}"

    # ------------------------------------------------------------- geometry
    def _expiry_dates(self) -> list[date]:
        return [
            (self.config.as_of + timedelta(days=days)).date() for days in self.config.expiry_days
        ]

    def _expiry_instant(self, expiry: date) -> datetime:
        return datetime.combine(expiry, self.config.expiry_time_utc, tzinfo=UTC)

    def _tau(self, expiry: date) -> float:
        seconds = (self._expiry_instant(expiry) - self.config.as_of).total_seconds()
        return seconds / SECONDS_PER_YEAR

    def _forward(self, tau: float) -> float:
        config = self.config
        return float(config.spot) * math.exp((config.risk_free_rate - config.dividend_yield) * tau)

    def implied_vol(self, log_moneyness: float, tau: float) -> float:
        """Generating volatility at ``k = ln(K/F)`` and maturity ``tau``.

        Public because it is the ground truth a calibration test compares
        against: the surface Phase 2 recovers should be this one.
        """
        w1 = float(raw_svi_total_variance(log_moneyness, self.config.svi))
        total_variance = w1 * tau * (1.0 + self.config.term_slope * tau)
        return math.sqrt(total_variance / tau)

    def _round_to_tick(self, value: float) -> Decimal:
        tick = self.config.tick_size
        return (Decimal(str(value)) / tick).quantize(Decimal(1), rounding=ROUND_HALF_UP) * tick

    # -------------------------------------------------------------- building
    def _build(self) -> None:
        config = self.config
        underlying = make_instrument(
            asset_class=AssetClass.INDEX,
            exchange=config.exchange,
            symbol=config.underlying_symbol,
            currency=config.currency,
            multiplier=Decimal(1),
            tick_size=Decimal("0.05"),
            lot_size=Decimal(1),
            metadata={"synthetic": True},
        )
        self._underlying = underlying
        self._register(underlying)
        self._quotes[underlying.id] = self._index_quote(underlying)

        for expiry in self._expiry_dates():
            tau = self._tau(expiry)
            forward = self._forward(tau)

            future = make_instrument(
                asset_class=AssetClass.FUTURE,
                exchange=config.exchange,
                symbol=config.underlying_symbol,
                currency=config.currency,
                multiplier=config.multiplier,
                tick_size=config.tick_size,
                lot_size=config.lot_size,
                expiry=expiry,
                underlying_id=underlying.id,
                settlement_type=SettlementType.CASH,
                metadata={"synthetic": True},
            )
            self._register(future)
            self._quotes[future.id] = self._two_sided_quote(
                future.id,
                forward,
                config.atm_relative_spread,
                config.atm_volume,
                config.atm_open_interest,
            )

            for strike in self._strikes():
                log_moneyness = math.log(float(strike) / forward)
                vol = self.implied_vol(log_moneyness, tau)
                discount = math.exp(-config.risk_free_rate * tau)
                for option_type in (OptionType.CALL, OptionType.PUT):
                    option = make_instrument(
                        asset_class=AssetClass.OPTION,
                        exchange=config.exchange,
                        symbol=config.underlying_symbol,
                        currency=config.currency,
                        multiplier=config.multiplier,
                        tick_size=config.tick_size,
                        lot_size=config.lot_size,
                        expiry=expiry,
                        strike=strike,
                        option_type=option_type,
                        exercise_style=ExerciseStyle.EUROPEAN,
                        settlement_type=SettlementType.CASH,
                        underlying_id=underlying.id,
                        metadata={"synthetic": True},
                    )
                    self._register(option)
                    mid = float(
                        black76_price(
                            forward,
                            float(strike),
                            tau,
                            vol,
                            option_type is OptionType.CALL,
                            discount,
                        )
                    )
                    self._quotes[option.id] = self._option_quote(option.id, mid, log_moneyness)

    def _register(self, instrument: Instrument) -> None:
        self._instruments[instrument.id] = instrument

    def _strikes(self) -> list[Decimal]:
        config = self.config
        atm = (config.spot / config.strike_step).quantize(
            Decimal(1), rounding=ROUND_HALF_UP
        ) * config.strike_step
        offsets = range(-config.strikes_each_side, config.strikes_each_side + 1)
        return [atm + config.strike_step * offset for offset in offsets]

    def _index_quote(self, underlying: Instrument) -> Quote:
        spot = float(self.config.spot)
        return self._two_sided_quote(
            underlying.id,
            spot,
            relative_spread=0.0001,
            volume=Decimal(0),
            open_interest=Decimal(0),
            last_only=True,
        )

    def _two_sided_quote(
        self,
        instrument_id: uuid.UUID,
        mid: float,
        relative_spread: float,
        volume: Decimal,
        open_interest: Decimal,
        last_only: bool = False,
    ) -> Quote:
        as_of = self.config.as_of
        half = max(mid * relative_spread / 2.0, float(self.config.tick_size))
        bid = self._round_to_tick(max(mid - half, float(self.config.tick_size)))
        ask = self._round_to_tick(mid + half)
        last = self._round_to_tick(mid)
        return Quote(
            instrument_id=instrument_id,
            exchange_timestamp=as_of,
            receive_timestamp=as_of,
            source=self.dataset_version,
            bid_price=None if last_only else bid,
            ask_price=None if last_only else ask,
            bid_size=None if last_only else self.config.quote_size,
            ask_size=None if last_only else self.config.quote_size,
            last_price=last,
            volume=volume,
            open_interest=open_interest,
            sequence_number=int(self._rng.integers(1, 1_000_000)),
        )

    def _option_quote(self, instrument_id: uuid.UUID, mid: float, log_moneyness: float) -> Quote:
        config = self.config
        # Spreads widen and liquidity thins away from the money, which is what
        # makes the quality and (later) calibration weighting meaningful.
        moneyness_penalty = min(
            abs(log_moneyness) / config.liquidity_decay_k, config.wing_spread_multiple
        )
        relative_spread = config.atm_relative_spread * (1.0 + moneyness_penalty)
        decay = math.exp(-0.5 * (log_moneyness / config.liquidity_decay_k) ** 2)
        volume = (config.atm_volume * Decimal(str(decay))).quantize(Decimal(1))
        open_interest = (config.atm_open_interest * Decimal(str(decay))).quantize(Decimal(1))
        size = (config.quote_size * Decimal(str(max(decay, 0.05)))).quantize(Decimal(1))

        quote = self._two_sided_quote(instrument_id, mid, relative_spread, volume, open_interest)
        return replace(quote, bid_size=size, ask_size=size)

    # ------------------------------------------------------------- interface
    async def get_instrument(self, instrument_id: uuid.UUID) -> Instrument | None:
        return self._instruments.get(instrument_id)

    async def list_instruments(self) -> Sequence[Instrument]:
        return tuple(self._instruments.values())

    @property
    def underlying(self) -> Instrument:
        assert self._underlying is not None
        return self._underlying

    async def get_quote(self, instrument_id: uuid.UUID) -> Quote | None:
        return self._quotes.get(instrument_id)

    async def get_option_chain(
        self, underlying_id: uuid.UUID, expiry: date | None = None
    ) -> OptionChain:
        config = self.config
        spot_quote = self._quotes[self.underlying.id]
        quotes: list[OptionQuote] = []
        for instrument in self._instruments.values():
            if instrument.asset_class is not AssetClass.OPTION:
                continue
            if instrument.underlying_id != underlying_id:
                continue
            if expiry is not None and instrument.expiry != expiry:
                continue
            quotes.append(
                OptionQuote(
                    quote=self._quotes[instrument.id],
                    underlying_id=underlying_id,
                    expiry=instrument.expiry,
                    strike=instrument.strike,
                    option_type=instrument.option_type,
                    expiry_timestamp=self._expiry_instant(instrument.expiry),
                    underlying_price=spot_quote.last_price,
                    underlying_source=self.dataset_version,
                )
            )
        quotes.sort(key=lambda q: (q.expiry, q.strike, q.option_type))
        return OptionChain(
            underlying_id=underlying_id,
            as_of=config.as_of,
            quotes=tuple(quotes),
            source=self.dataset_version,
            underlying_price=spot_quote.last_price,
            metadata={
                "generator": "SyntheticMarketDataProvider",
                "svi": config.svi.to_dict(),
                "risk_free_rate": config.risk_free_rate,
                "dividend_yield": config.dividend_yield,
                "term_slope": config.term_slope,
                "seed": config.seed,
            },
        )

    async def get_bars(
        self,
        instrument_id: uuid.UUID,
        interval: BarInterval,
        start: datetime,
        end: datetime,
    ) -> Sequence[Bar]:
        """Seeded geometric random walk around the instrument's quoted level."""
        self.require(ProviderCapability.BARS)
        quote = self._quotes.get(instrument_id)
        if quote is None or quote.last_price is None:
            return ()

        rng = np.random.default_rng(self.config.seed ^ instrument_id.int % (2**32))
        step = timedelta(seconds=interval.seconds)
        bars: list[Bar] = []
        level = float(quote.last_price)
        cursor = start
        # Daily volatility spread across the bar frequency.
        per_bar_vol = 0.15 * math.sqrt(interval.seconds / SECONDS_PER_YEAR)
        while cursor < end:
            shocks = rng.normal(0.0, per_bar_vol, size=4)
            path = level * np.exp(np.cumsum(shocks))
            open_, close = float(path[0]), float(path[-1])
            high, low = float(path.max()), float(path.min())
            bars.append(
                Bar(
                    instrument_id=instrument_id,
                    interval=interval,
                    start_timestamp=cursor,
                    end_timestamp=cursor + step,
                    open=self._round_to_tick(open_),
                    high=self._round_to_tick(max(high, open_, close)),
                    low=self._round_to_tick(min(low, open_, close)),
                    close=self._round_to_tick(close),
                    volume=Decimal(int(rng.integers(1_000, 50_000))),
                    source=self.dataset_version,
                )
            )
            level = close
            cursor += step
        return tuple(bars)
