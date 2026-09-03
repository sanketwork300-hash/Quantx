"""Canonical market-data schemas.

The central design rule (build spec 1.2): **observations are stored, derived
quantities are computed**. There is no field on these types that can hold
either. ``mid_price`` is a property, not a column; ``market_iv`` (Phase 1) will
be a stored observation and ``reference_iv`` a separate stored estimate, and
neither will ever be written from the other.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from domains.instruments.enums import OptionType
from domains.market_data.enums import (
    AggressorSide,
    BarInterval,
    BookEventType,
    BookSide,
)

SECONDS_PER_YEAR_ACT365 = Decimal(365 * 24 * 3600)


def _positive(value: Decimal | None) -> bool:
    return value is not None and value > 0


@dataclass(frozen=True, slots=True)
class Quote:
    """A single market observation for one instrument."""

    instrument_id: uuid.UUID
    exchange_timestamp: datetime
    receive_timestamp: datetime
    source: str
    bid_price: Decimal | None = None
    bid_size: Decimal | None = None
    ask_price: Decimal | None = None
    ask_size: Decimal | None = None
    last_price: Decimal | None = None
    last_size: Decimal | None = None
    volume: Decimal | None = None
    open_interest: Decimal | None = None
    sequence_number: int | None = None
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("exchange_timestamp", "receive_timestamp"):
            value = getattr(self, name)
            if value.tzinfo is None:
                raise ValueError(f"{name} must be timezone-aware")

    # ------------------------------------------------------- derived values
    @property
    def has_two_sided_market(self) -> bool:
        return _positive(self.bid_price) and _positive(self.ask_price)

    @property
    def mid_price(self) -> Decimal | None:
        """Mid of a valid two-sided market, else ``None``.

        Deliberately does **not** fall back to ``last_price``. Substituting a
        trade print for a mid is exactly the kind of silent replacement of an
        observation by an estimate that build spec 1.2 forbids; a caller that
        wants that fallback must ask for it and have the substitution recorded.
        """
        if not self.has_two_sided_market:
            return None
        return (self.bid_price + self.ask_price) / Decimal(2)

    @property
    def spread(self) -> Decimal | None:
        if not (self.bid_price is not None and self.ask_price is not None):
            return None
        return self.ask_price - self.bid_price

    @property
    def relative_spread(self) -> Decimal | None:
        mid = self.mid_price
        spread = self.spread
        if mid is None or spread is None or mid <= 0:
            return None
        return spread / mid

    @property
    def microprice(self) -> Decimal | None:
        """Size-weighted mid: ``(bid*ask_size + ask*bid_size) / (bid_size + ask_size)``.

        Weighted by the *opposite* side's size, so a large resting bid pulls the
        microprice toward the ask.
        """
        if not self.has_two_sided_market:
            return None
        if not (_positive(self.bid_size) and _positive(self.ask_size)):
            return None
        total = self.bid_size + self.ask_size
        return (self.bid_price * self.ask_size + self.ask_price * self.bid_size) / total

    @property
    def is_crossed(self) -> bool:
        return (
            self.bid_price is not None
            and self.ask_price is not None
            and self.bid_price > self.ask_price
        )

    @property
    def is_locked(self) -> bool:
        return (
            self.bid_price is not None
            and self.ask_price is not None
            and self.bid_price == self.ask_price
        )

    def age_seconds(self, as_of: datetime) -> float:
        return (as_of - self.exchange_timestamp).total_seconds()


@dataclass(frozen=True, slots=True)
class OptionQuote:
    """A quote for an option contract, carrying its own contract context.

    The strike/expiry/type are duplicated from the instrument on purpose: the
    cleaning, quality and calibration pipelines process tens of thousands of
    these and must not need a lookup per row to know what they are looking at.
    """

    quote: Quote
    underlying_id: uuid.UUID
    expiry: date
    strike: Decimal
    option_type: OptionType
    expiry_timestamp: datetime | None = None
    underlying_price: Decimal | None = None
    underlying_source: str | None = None

    @property
    def instrument_id(self) -> uuid.UUID:
        return self.quote.instrument_id

    @property
    def mid_price(self) -> Decimal | None:
        return self.quote.mid_price

    def time_to_expiry_years(self, as_of: datetime) -> Decimal | None:
        """ACT/365 Fixed year fraction to the expiry instant.

        Returns ``None`` when the expiry instant is unknown; returns a
        non-positive value when the option has expired, which callers must treat
        as a structured non-result rather than clamping to a small positive
        number.
        """
        if self.expiry_timestamp is None:
            return None
        seconds = Decimal((self.expiry_timestamp - as_of).total_seconds())
        return seconds / SECONDS_PER_YEAR_ACT365

    def intrinsic_value(self) -> Decimal | None:
        """Undiscounted intrinsic value against the observed underlying price."""
        if self.underlying_price is None:
            return None
        if self.option_type is OptionType.CALL:
            return max(self.underlying_price - self.strike, Decimal(0))
        return max(self.strike - self.underlying_price, Decimal(0))


@dataclass(frozen=True, slots=True)
class OptionChain:
    """A set of option quotes observed together for one underlying."""

    underlying_id: uuid.UUID
    as_of: datetime
    quotes: tuple[OptionQuote, ...]
    source: str
    underlying_price: Decimal | None = None
    metadata: dict = field(default_factory=dict)

    @property
    def expiries(self) -> tuple[date, ...]:
        return tuple(sorted({quote.expiry for quote in self.quotes}))

    def for_expiry(self, expiry: date) -> tuple[OptionQuote, ...]:
        return tuple(quote for quote in self.quotes if quote.expiry == expiry)

    def __len__(self) -> int:
        return len(self.quotes)


@dataclass(frozen=True, slots=True)
class Bar:
    instrument_id: uuid.UUID
    interval: BarInterval
    start_timestamp: datetime
    end_timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    source: str
    #: Only populated when the venue publishes it. A VWAP we reconstructed is a
    #: derived estimate and does not belong in an observation field.
    vwap: Decimal | None = None
    trade_count: int | None = None

    def __post_init__(self) -> None:
        if self.high < self.low:
            raise ValueError(f"bar high {self.high} below low {self.low}")
        for name in ("open", "close"):
            value = getattr(self, name)
            if not (self.low <= value <= self.high):
                raise ValueError(f"bar {name} {value} outside [low, high]")


@dataclass(frozen=True, slots=True)
class OrderBookLevel:
    price: Decimal
    quantity: Decimal
    order_count: int | None = None


@dataclass(frozen=True, slots=True)
class OrderBookSnapshot:
    instrument_id: uuid.UUID
    exchange_timestamp: datetime
    receive_timestamp: datetime
    bids: tuple[OrderBookLevel, ...]
    asks: tuple[OrderBookLevel, ...]
    source: str
    sequence_number: int | None = None

    def __post_init__(self) -> None:
        # Best-first ordering is an invariant, not a convention: every depth
        # calculation downstream indexes level 0 as the top of book.
        for side, levels, descending in (("bids", self.bids, True), ("asks", self.asks, False)):
            prices = [level.price for level in levels]
            expected = sorted(prices, reverse=descending)
            if prices != expected:
                raise ValueError(f"{side} are not ordered best-first")

    @property
    def best_bid(self) -> OrderBookLevel | None:
        return self.bids[0] if self.bids else None

    @property
    def best_ask(self) -> OrderBookLevel | None:
        return self.asks[0] if self.asks else None

    def imbalance(self, levels: int = 1) -> float | None:
        """Order-book imbalance ``(Vb - Va) / (Vb + Va)`` over the top levels."""
        bid_volume = sum((level.quantity for level in self.bids[:levels]), Decimal(0))
        ask_volume = sum((level.quantity for level in self.asks[:levels]), Decimal(0))
        total = bid_volume + ask_volume
        if total == 0:
            return None
        return float((bid_volume - ask_volume) / total)


@dataclass(frozen=True, slots=True)
class BookEvent:
    """One order-book message.

    The canonical form of an event-level feed, and the only input from which a
    queue or an arrival-intensity model can be built. Note what is *not* here:
    no queue position, no inferred aggressor, no reconstructed book state. Each
    of those is a derivation, and derivations live beside the observation
    rather than inside it.

    ``sequence_number`` is nullable because plenty of exported tapes drop it,
    and its absence is a fact the availability gate reads: without sequencing
    there is no way to know whether the tape is complete, and a queue model
    computed on a tape with a hole in it is a queue model of a different book.
    """

    instrument_id: uuid.UUID
    exchange_timestamp: datetime
    event_type: BookEventType
    source: str
    side: BookSide | None = None
    price: Decimal | None = None
    quantity: Decimal | None = None
    sequence_number: int | None = None
    order_id: str | None = None
    receive_timestamp: datetime | None = None

    def __post_init__(self) -> None:
        if self.exchange_timestamp.tzinfo is None:
            raise ValueError("exchange_timestamp must be timezone-aware")
        if self.quantity is not None and self.quantity < 0:
            raise ValueError(
                f"a negative event quantity is not a quantity: {self.quantity}; "
                "a removal is carried by the event type, not by a sign"
            )

    @property
    def is_departure(self) -> bool:
        """Whether this event removed size from its price level.

        A ``MODIFY`` is not counted: the feed does not say whether the size went
        up or down, and guessing would put an invented departure into a queue
        estimate.
        """
        return self.event_type in {BookEventType.CANCEL, BookEventType.TRADE}


@dataclass(frozen=True, slots=True)
class Trade:
    instrument_id: uuid.UUID
    exchange_timestamp: datetime
    price: Decimal
    quantity: Decimal
    source: str
    #: Nullable because most feeds do not publish it. Inferring the aggressor
    #: from a tick rule is a model, not an observation, and does not go here.
    aggressor_side: AggressorSide | None = None
    trade_id: str | None = None
