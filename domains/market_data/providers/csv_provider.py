"""CSV market-data provider (research / user-data mode).

Reads a directory of **canonical-schema** CSV files. This is the "research
mode" path from build spec section 8: point the platform at a folder of files
you already own and the whole system works with no external API.

Arbitrary user column layouts are handled by the upload pipeline
(``domains.market_data.ingestion``), which maps them into the canonical schema
first. Keeping the two apart means this provider has one fixed schema to trust
and the mapping problem is solved in exactly one place.

Expected files under ``root``::

    instruments.csv   asset_class, exchange, symbol, currency, multiplier,
                      tick_size, lot_size, [expiry, strike, option_type,
                      exercise_style, settlement_type, underlying_symbol,
                      underlying_asset_class, venue]
    quotes.csv        canonical_key, exchange_timestamp, [receive_timestamp,
                      bid_price, bid_size, ask_price, ask_size, last_price,
                      last_size, volume, open_interest, sequence_number]
    bars.csv          canonical_key, interval, start_timestamp, end_timestamp,
                      open, high, low, close, volume, [vwap, trade_count]

``quotes.csv`` holds every asset class; an option chain is assembled from the
option instruments that share an underlying, so there is no separate chain file
to fall out of sync with the quote file.
"""

from __future__ import annotations

import csv
import hashlib
import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from domains.instruments.enums import (
    AssetClass,
    ExerciseStyle,
    OptionType,
    SettlementType,
)
from domains.instruments.identity import build_canonical_key
from domains.instruments.models import Instrument, make_instrument
from domains.market_data.enums import BarInterval, ProviderCapability
from domains.market_data.ingestion.parser import (
    parse_date,
    parse_datetime,
    parse_decimal,
    parse_integer,
)
from domains.market_data.models import Bar, OptionChain, OptionQuote, Quote
from domains.market_data.providers.base import MarketDataProvider, ProviderError

INSTRUMENTS_FILE = "instruments.csv"
QUOTES_FILE = "quotes.csv"
BARS_FILE = "bars.csv"


def _cell(row: dict, name: str) -> str | None:
    value = row.get(name)
    if value is None:
        return None
    token = value.strip()
    return token or None


def _decimal(row: dict, name: str, default: Decimal | None = None) -> Decimal | None:
    token = _cell(row, name)
    return default if token is None else parse_decimal(token)


class CSVMarketDataProvider(MarketDataProvider):
    name = "csv"
    capabilities = frozenset(
        {
            ProviderCapability.INSTRUMENTS,
            ProviderCapability.QUOTES,
            ProviderCapability.OPTION_CHAINS,
            ProviderCapability.BARS,
        }
    )

    def __init__(self, root: Path, source_label: str | None = None) -> None:
        self._root = Path(root)
        if not self._root.is_dir():
            raise ProviderError(f"CSV provider root is not a directory: {self._root}")
        self._label = source_label or self._root.name
        self._instruments: dict[uuid.UUID, Instrument] = {}
        self._by_key: dict[str, Instrument] = {}
        self._quotes: dict[uuid.UUID, Quote] = {}
        self._digest = self._compute_digest()
        self._load_instruments()
        self._load_quotes()

    # ---------------------------------------------------------------- setup
    @property
    def dataset_version(self) -> str:
        return f"csv:{self._label}:{self._digest}"

    def _compute_digest(self) -> str:
        """Content digest over the dataset, so provenance names the exact bytes."""
        hasher = hashlib.sha256()
        for filename in sorted((INSTRUMENTS_FILE, QUOTES_FILE, BARS_FILE)):
            path = self._root / filename
            if path.exists():
                hasher.update(filename.encode())
                hasher.update(path.read_bytes())
        return hasher.hexdigest()[:16]

    def _read(self, filename: str) -> list[dict]:
        path = self._root / filename
        if not path.exists():
            return []
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return [
                {(key or "").strip(): value for key, value in row.items()}
                for row in csv.DictReader(handle)
            ]

    def _load_instruments(self) -> None:
        rows = self._read(INSTRUMENTS_FILE)
        if not rows:
            raise ProviderError(f"{INSTRUMENTS_FILE} is missing or empty in {self._root}")

        # Two passes: underlyings must exist before the contracts that
        # reference them, and files are not required to be topologically sorted.
        pending: list[dict] = []
        for row in rows:
            asset_class = AssetClass(_cell(row, "asset_class").upper())
            if asset_class in {AssetClass.FUTURE, AssetClass.OPTION}:
                pending.append(row)
            else:
                self._add_instrument(row, asset_class, underlying_id=None)

        for row in pending:
            asset_class = AssetClass(_cell(row, "asset_class").upper())
            underlying_symbol = _cell(row, "underlying_symbol") or _cell(row, "symbol")
            underlying_class = AssetClass((_cell(row, "underlying_asset_class") or "INDEX").upper())
            underlying_key = build_canonical_key(
                exchange=_cell(row, "exchange"),
                asset_class=underlying_class,
                symbol=underlying_symbol,
            )
            underlying = self._by_key.get(underlying_key)
            if underlying is None:
                raise ProviderError(
                    f"{INSTRUMENTS_FILE}: underlying {underlying_key!r} referenced by "
                    f"{_cell(row, 'symbol')!r} is not defined in the file"
                )
            self._add_instrument(row, asset_class, underlying_id=underlying.id)

    def _add_instrument(
        self, row: dict, asset_class: AssetClass, underlying_id: uuid.UUID | None
    ) -> None:
        expiry_token = _cell(row, "expiry")
        option_type_token = _cell(row, "option_type")
        exercise_token = _cell(row, "exercise_style")
        settlement_token = _cell(row, "settlement_type")

        instrument = make_instrument(
            asset_class=asset_class,
            exchange=_cell(row, "exchange"),
            symbol=_cell(row, "symbol"),
            currency=_cell(row, "currency") or "USD",
            multiplier=_decimal(row, "multiplier", Decimal(1)),
            tick_size=_decimal(row, "tick_size", Decimal("0.01")),
            lot_size=_decimal(row, "lot_size", Decimal(1)),
            expiry=parse_date(expiry_token) if expiry_token else None,
            strike=_decimal(row, "strike"),
            option_type=OptionType.parse(option_type_token) if option_type_token else None,
            exercise_style=(
                ExerciseStyle(exercise_token.upper())
                if exercise_token
                else (ExerciseStyle.EUROPEAN if asset_class is AssetClass.OPTION else None)
            ),
            settlement_type=(
                SettlementType(settlement_token.upper()) if settlement_token else None
            ),
            underlying_id=underlying_id,
            venue=_cell(row, "venue"),
            metadata={"source_file": INSTRUMENTS_FILE},
        )
        self._instruments[instrument.id] = instrument
        self._by_key[instrument.canonical_key] = instrument

    def _load_quotes(self) -> None:
        for row in self._read(QUOTES_FILE):
            key = _cell(row, "canonical_key")
            instrument = self._by_key.get(key)
            if instrument is None:
                raise ProviderError(
                    f"{QUOTES_FILE}: canonical_key {key!r} is not defined in {INSTRUMENTS_FILE}"
                )
            exchange_timestamp = parse_datetime(_cell(row, "exchange_timestamp"))
            receive_token = _cell(row, "receive_timestamp")
            sequence_token = _cell(row, "sequence_number")
            self._quotes[instrument.id] = Quote(
                instrument_id=instrument.id,
                exchange_timestamp=exchange_timestamp,
                receive_timestamp=(
                    parse_datetime(receive_token) if receive_token else exchange_timestamp
                ),
                source=self.dataset_version,
                bid_price=_decimal(row, "bid_price"),
                bid_size=_decimal(row, "bid_size"),
                ask_price=_decimal(row, "ask_price"),
                ask_size=_decimal(row, "ask_size"),
                last_price=_decimal(row, "last_price"),
                last_size=_decimal(row, "last_size"),
                volume=_decimal(row, "volume"),
                open_interest=_decimal(row, "open_interest"),
                sequence_number=parse_integer(sequence_token) if sequence_token else None,
            )

    # ------------------------------------------------------------- interface
    async def get_instrument(self, instrument_id: uuid.UUID) -> Instrument | None:
        return self._instruments.get(instrument_id)

    async def list_instruments(self) -> Sequence[Instrument]:
        return tuple(self._instruments.values())

    def instrument_by_key(self, canonical_key: str) -> Instrument | None:
        return self._by_key.get(canonical_key)

    async def get_quote(self, instrument_id: uuid.UUID) -> Quote | None:
        return self._quotes.get(instrument_id)

    async def get_option_chain(
        self, underlying_id: uuid.UUID, expiry: date | None = None
    ) -> OptionChain:
        underlying = self._instruments.get(underlying_id)
        if underlying is None:
            raise ProviderError(f"unknown underlying {underlying_id}")

        underlying_quote = self._quotes.get(underlying_id)
        underlying_price = None
        if underlying_quote is not None:
            underlying_price = underlying_quote.mid_price or underlying_quote.last_price

        quotes: list[OptionQuote] = []
        as_of: datetime | None = None
        for instrument in self._instruments.values():
            if instrument.asset_class is not AssetClass.OPTION:
                continue
            if instrument.underlying_id != underlying_id:
                continue
            if expiry is not None and instrument.expiry != expiry:
                continue
            quote = self._quotes.get(instrument.id)
            if quote is None:
                continue
            as_of = (
                quote.exchange_timestamp if as_of is None else max(as_of, quote.exchange_timestamp)
            )
            quotes.append(
                OptionQuote(
                    quote=quote,
                    underlying_id=underlying_id,
                    expiry=instrument.expiry,
                    strike=instrument.strike,
                    option_type=instrument.option_type,
                    # Expiry instant is not in the canonical CSV schema; the
                    # ingestion caller supplies the convention, and until it
                    # does this stays None rather than being guessed.
                    expiry_timestamp=None,
                    underlying_price=underlying_price,
                    underlying_source=self.dataset_version,
                )
            )

        quotes.sort(key=lambda q: (q.expiry, q.strike, q.option_type))
        return OptionChain(
            underlying_id=underlying_id,
            as_of=as_of or datetime.now(UTC),
            quotes=tuple(quotes),
            source=self.dataset_version,
            underlying_price=underlying_price,
            metadata={"root": str(self._root), "digest": self._digest},
        )

    async def get_bars(
        self,
        instrument_id: uuid.UUID,
        interval: BarInterval,
        start: datetime,
        end: datetime,
    ) -> Sequence[Bar]:
        self.require(ProviderCapability.BARS)
        bars: list[Bar] = []
        for row in self._read(BARS_FILE):
            instrument = self._by_key.get(_cell(row, "canonical_key"))
            if instrument is None or instrument.id != instrument_id:
                continue
            if BarInterval(_cell(row, "interval")) is not interval:
                continue
            start_timestamp = parse_datetime(_cell(row, "start_timestamp"))
            if not (start <= start_timestamp < end):
                continue
            trade_count = _cell(row, "trade_count")
            bars.append(
                Bar(
                    instrument_id=instrument_id,
                    interval=interval,
                    start_timestamp=start_timestamp,
                    end_timestamp=parse_datetime(_cell(row, "end_timestamp")),
                    open=_decimal(row, "open"),
                    high=_decimal(row, "high"),
                    low=_decimal(row, "low"),
                    close=_decimal(row, "close"),
                    volume=_decimal(row, "volume", Decimal(0)),
                    vwap=_decimal(row, "vwap"),
                    trade_count=parse_integer(trade_count) if trade_count else None,
                    source=self.dataset_version,
                )
            )
        bars.sort(key=lambda bar: bar.start_timestamp)
        return tuple(bars)
