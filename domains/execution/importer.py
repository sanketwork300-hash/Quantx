"""Trade-log import: parse, resolve, and refuse to guess.

The same three buckets as the portfolio import — resolved, ambiguous, invalid —
and the same rule behind them: **no ambiguous row is ever auto-resolved.** A
fill attributed to the wrong contract does not merely mis-state one number; it
lands in the wrong parent order, takes a benchmark window with it, and every
cost computed downstream inherits the error silently.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum

from domains.execution.models import (
    DEFAULT_PARENT_GAP_SECONDS,
    Execution,
    ExecutionSource,
    OrderType,
    Side,
)
from domains.instruments.enums import (
    AssetClass,
    ExerciseStyle,
    SettlementType,
)
from domains.instruments.errors import InstrumentError
from domains.instruments.models import (
    MULTIPLIER_ASSUMED,
    Instrument,
    make_instrument,
)
from domains.instruments.resolver import (
    ResolutionRequest,
    ResolutionResult,
    ResolutionStatus,
)
from domains.instruments.service import InstrumentService
from domains.market_data.ingestion.column_mapping import ColumnMapping, FieldSpec, FieldType
from domains.market_data.ingestion.parser import ParsedRow, TabularParser

IMPORT_MODEL_VERSION = "trade-log-import@1.0.0"

TRADE_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec(
        "timestamp",
        FieldType.DATETIME,
        True,
        ("timestamp", "time", "executiontime", "filltime", "tradetime", "datetime", "tradedate"),
        "When the fill happened on the exchange.",
    ),
    FieldSpec(
        "symbol",
        FieldType.STRING,
        True,
        ("symbol", "ticker", "instrument", "name", "tradingsymbol", "scrip"),
        "Contract root or ticker.",
    ),
    FieldSpec(
        "side",
        FieldType.STRING,
        True,
        ("side", "buysell", "direction", "action", "transactiontype"),
        "BUY or SELL. Quantity stays positive; direction lives here.",
    ),
    FieldSpec(
        "quantity",
        FieldType.DECIMAL,
        True,
        ("quantity", "qty", "filledquantity", "fillqty", "size", "units", "executedqty"),
        "How much traded on this fill. Always positive.",
    ),
    FieldSpec(
        "price",
        FieldType.DECIMAL,
        True,
        ("price", "executionprice", "fillprice", "tradeprice", "avgprice", "rate"),
        "The price this fill traded at.",
    ),
    FieldSpec(
        "exchange",
        FieldType.STRING,
        False,
        ("exchange", "exch", "venue", "market"),
        "Listing exchange.",
    ),
    FieldSpec(
        "asset_class",
        FieldType.STRING,
        False,
        ("assetclass", "asset_type", "assettype", "type", "instrumenttype", "segment"),
        "EQUITY, INDEX, FUTURE, OPTION, FX, CRYPTO_SPOT or CRYPTO_PERPETUAL.",
    ),
    FieldSpec(
        "expiry",
        FieldType.DATE,
        False,
        ("expiry", "expirydate", "expiry_date", "expirydt", "expiration", "maturity"),
        "Contract expiry, for dated instruments.",
    ),
    FieldSpec(
        "strike",
        FieldType.DECIMAL,
        False,
        ("strike", "strikeprice", "strike_price", "k"),
        "Option strike.",
    ),
    FieldSpec(
        "option_type",
        FieldType.OPTION_TYPE,
        False,
        ("optiontype", "option_type", "cp", "cepe", "callput", "right"),
        "CALL/PUT, C/P or CE/PE.",
    ),
    FieldSpec(
        "order_id",
        FieldType.STRING,
        False,
        ("orderid", "order_id", "brokerorderid", "clientorderid"),
        "The broker's identifier for the child order.",
    ),
    FieldSpec(
        "parent_order",
        FieldType.STRING,
        False,
        ("parentorder", "parent_order", "parentorderid", "parentid", "basketid", "algoid"),
        "Groups fills into one decision. Without it, grouping is inferred and flagged.",
    ),
    FieldSpec(
        "order_type",
        FieldType.STRING,
        False,
        ("ordertype", "order_type", "type_of_order"),
        "MARKET, LIMIT, STOP or STOP_LIMIT.",
    ),
    FieldSpec(
        "limit_price",
        FieldType.DECIMAL,
        False,
        ("limitprice", "limit_price", "limit"),
        "The order's limit, when it had one.",
    ),
    FieldSpec(
        "submit_timestamp",
        FieldType.DATETIME,
        False,
        ("submittime", "submit_timestamp", "ordertime", "placedat", "entrytime"),
        "When the order reached the market. Without it, arrival falls back to a flagged proxy.",
    ),
    FieldSpec(
        "decision_timestamp",
        FieldType.DATETIME,
        False,
        ("decisiontime", "decision_timestamp", "signaltime", "decidedat"),
        "When the trading decision was made.",
    ),
    FieldSpec(
        "order_quantity",
        FieldType.DECIMAL,
        False,
        ("orderquantity", "order_quantity", "requestedqty", "totalquantity", "orderqty"),
        "What the parent order asked for. Without it, nothing is said about what went unfilled.",
    ),
    FieldSpec(
        "fees",
        FieldType.DECIMAL,
        False,
        ("fees", "commission", "brokerage", "charges", "cost"),
        "Fees charged on this fill.",
    ),
    FieldSpec(
        "broker",
        FieldType.STRING,
        False,
        ("broker", "brokername", "counterparty"),
        "Who executed it.",
    ),
    FieldSpec(
        "currency",
        FieldType.STRING,
        False,
        ("currency", "ccy", "curr"),
        "ISO currency of the contract.",
    ),
    FieldSpec(
        "multiplier",
        FieldType.DECIMAL,
        False,
        ("multiplier", "lotsize", "contractsize", "lot_size", "contractmultiplier"),
        "Contract multiplier. Recorded as an assumption when absent.",
    ),
)


class TradeRejection(StrEnum):
    MISSING_SYMBOL = "MISSING_SYMBOL"
    MISSING_TIMESTAMP = "MISSING_TIMESTAMP"
    MISSING_SIDE = "MISSING_SIDE"
    UNRECOGNISED_SIDE = "UNRECOGNISED_SIDE"
    MISSING_QUANTITY = "MISSING_QUANTITY"
    NON_POSITIVE_QUANTITY = "NON_POSITIVE_QUANTITY"
    MISSING_PRICE = "MISSING_PRICE"
    NEGATIVE_PRICE = "NEGATIVE_PRICE"
    NEGATIVE_FEES = "NEGATIVE_FEES"
    UNKNOWN_ASSET_CLASS = "UNKNOWN_ASSET_CLASS"
    INCOMPLETE_OPTION = "INCOMPLETE_OPTION"
    INCOMPLETE_FUTURE = "INCOMPLETE_FUTURE"
    INSTRUMENT_UNRESOLVED = "INSTRUMENT_UNRESOLVED"
    INVALID_INSTRUMENT = "INVALID_INSTRUMENT"
    UNPARSEABLE_ROW = "UNPARSEABLE_ROW"
    SUBMIT_AFTER_FILL = "SUBMIT_AFTER_FILL"


@dataclass(frozen=True, slots=True)
class ResolvedTrade:
    row_number: int
    instrument: Instrument
    execution: Execution
    resolution_method: str
    creates_instrument: bool = False
    creates_underlying: Instrument | None = None

    def to_dict(self) -> dict:
        return {
            "row_number": self.row_number,
            "instrument_id": str(self.instrument.id),
            "canonical_key": self.instrument.canonical_key,
            "symbol": self.instrument.symbol,
            "asset_class": str(self.instrument.asset_class),
            "expiry": (self.instrument.expiry.isoformat() if self.instrument.expiry else None),
            "strike": (
                format(self.instrument.strike, "f") if self.instrument.strike is not None else None
            ),
            "option_type": (
                str(self.instrument.option_type) if self.instrument.option_type else None
            ),
            "side": str(self.execution.side),
            "quantity": format(self.execution.quantity, "f"),
            "price": format(self.execution.execution_price, "f"),
            "timestamp": self.execution.exchange_timestamp.isoformat(),
            "parent_order_key": self.execution.parent_order_key,
            "fees": format(self.execution.fees, "f"),
            "resolution_method": self.resolution_method,
            "creates_instrument": self.creates_instrument,
            "creates_underlying": (
                self.creates_underlying.canonical_key if self.creates_underlying else None
            ),
            "multiplier_is_assumed": self.instrument.multiplier_is_assumed,
        }


@dataclass(frozen=True, slots=True)
class AmbiguousTrade:
    row_number: int
    candidates: tuple[Instrument, ...]
    raw: dict
    reason: str = "MULTIPLE_CANDIDATES"

    def to_dict(self) -> dict:
        return {
            "row_number": self.row_number,
            "reason": self.reason,
            "candidates": [
                {
                    "instrument_id": str(candidate.id),
                    "canonical_key": candidate.canonical_key,
                    "symbol": candidate.symbol,
                    "expiry": candidate.expiry.isoformat() if candidate.expiry else None,
                    "strike": (
                        format(candidate.strike, "f") if candidate.strike is not None else None
                    ),
                    "option_type": (str(candidate.option_type) if candidate.option_type else None),
                }
                for candidate in self.candidates
            ],
            "raw": self.raw,
        }


@dataclass(frozen=True, slots=True)
class InvalidTrade:
    row_number: int
    reason: TradeRejection
    message: str
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "row_number": self.row_number,
            "reason": str(self.reason),
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class TradeImportPreview:
    resolved: tuple[ResolvedTrade, ...]
    ambiguous: tuple[AmbiguousTrade, ...]
    invalid: tuple[InvalidTrade, ...]
    rows_in: int

    @property
    def committable(self) -> bool:
        """Refused while any row is ambiguous, for the same reason as always."""
        return not self.ambiguous and bool(self.resolved)

    def to_dict(self) -> dict:
        return {
            "counts": {
                "input": self.rows_in,
                "resolved": len(self.resolved),
                "ambiguous": len(self.ambiguous),
                "invalid": len(self.invalid),
            },
            "committable": self.committable,
            "resolved": [row.to_dict() for row in self.resolved],
            "ambiguous": [row.to_dict() for row in self.ambiguous],
            "invalid": [row.to_dict() for row in self.invalid],
        }


@dataclass(frozen=True, slots=True)
class TradeImportDefaults:
    """Contract terms and conventions the file may not carry."""

    currency: str = "INR"
    exchange: str | None = None
    asset_class: AssetClass | None = None
    multiplier: Decimal | None = None
    tick_size: Decimal = Decimal("0.05")
    lot_size: Decimal = Decimal(1)
    exercise_style: ExerciseStyle = ExerciseStyle.EUROPEAN
    settlement_type: SettlementType = SettlementType.CASH
    create_missing_instruments: bool = True
    broker: str | None = None
    #: How far apart two fills may be and still be one inferred parent order.
    parent_gap_seconds: float = DEFAULT_PARENT_GAP_SECONDS

    def to_provenance(self) -> dict:
        return {
            "currency": self.currency,
            "exchange": self.exchange,
            "asset_class": str(self.asset_class) if self.asset_class else None,
            "multiplier": (format(self.multiplier, "f") if self.multiplier is not None else None),
            "tick_size": format(self.tick_size, "f"),
            "lot_size": format(self.lot_size, "f"),
            "exercise_style": str(self.exercise_style),
            "settlement_type": str(self.settlement_type),
            "create_missing_instruments": self.create_missing_instruments,
            "broker": self.broker,
            "parent_gap_seconds": self.parent_gap_seconds,
        }


class TradeImporter:
    def __init__(self, instruments: InstrumentService, max_rows: int = 500_000) -> None:
        self._instruments = instruments
        self._parser = TabularParser(TRADE_FIELDS, max_rows=max_rows)

    async def preview(
        self,
        data: bytes,
        mapping: ColumnMapping,
        defaults: TradeImportDefaults,
        user_id: uuid.UUID,
        limit: int | None = None,
    ) -> TradeImportPreview:
        parsed = self._parser.parse(data, mapping, limit=limit)

        invalid: list[InvalidTrade] = [
            InvalidTrade(
                row_number=error.row_number,
                reason=TradeRejection.UNPARSEABLE_ROW,
                message=error.message,
                raw=error.raw,
            )
            for error in parsed.errors
        ]
        resolved: list[ResolvedTrade] = []
        ambiguous: list[AmbiguousTrade] = []

        for row in parsed.rows:
            outcome = await self._row(row, defaults, user_id)
            if isinstance(outcome, InvalidTrade):
                invalid.append(outcome)
            elif isinstance(outcome, AmbiguousTrade):
                ambiguous.append(outcome)
            else:
                resolved.append(outcome)

        return TradeImportPreview(
            resolved=tuple(resolved),
            ambiguous=tuple(ambiguous),
            invalid=tuple(invalid),
            rows_in=len(parsed.rows) + len(parsed.errors),
        )

    async def _row(
        self, row: ParsedRow, defaults: TradeImportDefaults, user_id: uuid.UUID
    ) -> ResolvedTrade | AmbiguousTrade | InvalidTrade:
        values = row.values

        def reject(reason: TradeRejection, message: str) -> InvalidTrade:
            return InvalidTrade(row.row_number, reason, message, row.raw)

        symbol = (values.get("symbol") or "").strip()
        if not symbol:
            return reject(TradeRejection.MISSING_SYMBOL, "Symbol is empty.")

        timestamp = values.get("timestamp")
        if timestamp is None:
            return reject(
                TradeRejection.MISSING_TIMESTAMP,
                "A fill with no timestamp cannot be placed in a benchmark window.",
            )

        raw_side = values.get("side")
        if not raw_side:
            return reject(TradeRejection.MISSING_SIDE, "Side is empty.")
        try:
            side = Side.parse(raw_side)
        except ValueError as exc:
            return reject(TradeRejection.UNRECOGNISED_SIDE, str(exc))

        quantity = values.get("quantity")
        if quantity is None:
            return reject(TradeRejection.MISSING_QUANTITY, "Quantity is empty.")
        if quantity <= 0:
            return reject(
                TradeRejection.NON_POSITIVE_QUANTITY,
                f"A fill's quantity must be positive, got {quantity}. Direction is "
                "carried by side, not by the sign.",
            )

        price = values.get("price")
        if price is None:
            return reject(TradeRejection.MISSING_PRICE, "Price is empty.")
        if price < 0:
            return reject(TradeRejection.NEGATIVE_PRICE, f"A negative fill price: {price}.")

        fees = values.get("fees") or Decimal(0)
        if fees < 0:
            return reject(
                TradeRejection.NEGATIVE_FEES,
                f"Negative fees of {fees} are a rebate, which needs its own column "
                "rather than a sign.",
            )

        submit = _aware(values.get("submit_timestamp"))
        stamp = _aware(timestamp)
        if submit is not None and submit > stamp:
            return reject(
                TradeRejection.SUBMIT_AFTER_FILL,
                f"The order was submitted at {submit.isoformat()}, after the fill "
                f"at {stamp.isoformat()}. One of the two timestamps is wrong, and "
                "guessing which would corrupt every benchmark for this order.",
            )

        asset_class, error = self._asset_class(row, values, defaults)
        if error is not None:
            return error

        expiry = values.get("expiry")
        strike = values.get("strike")
        option_type = values.get("option_type")

        if asset_class is AssetClass.OPTION and (
            expiry is None or strike is None or option_type is None
        ):
            missing = [
                name
                for name, value in (
                    ("expiry", expiry),
                    ("strike", strike),
                    ("option_type", option_type),
                )
                if value is None
            ]
            return reject(
                TradeRejection.INCOMPLETE_OPTION, f"An option row needs {', '.join(missing)}."
            )
        if asset_class is AssetClass.FUTURE and expiry is None:
            return reject(TradeRejection.INCOMPLETE_FUTURE, "A future row needs an expiry.")

        exchange = (values.get("exchange") or defaults.exchange or "").strip()
        if not exchange:
            return reject(
                TradeRejection.INSTRUMENT_UNRESOLVED,
                "No exchange in the row and no default supplied.",
            )

        resolution = await self._instruments.resolve(
            ResolutionRequest(
                symbol=symbol,
                exchange=exchange,
                asset_class=asset_class,
                expiry=expiry,
                strike=strike,
                option_type=option_type,
            )
        )
        if resolution.status is ResolutionStatus.AMBIGUOUS:
            return AmbiguousTrade(
                row_number=row.row_number, candidates=resolution.candidates, raw=row.raw
            )

        instrument, creates, new_underlying, error = await self._instrument_for(
            row, resolution, symbol, exchange, asset_class, values, defaults
        )
        if error is not None:
            return error

        execution = Execution(
            id=uuid.uuid4(),
            user_id=user_id,
            instrument_id=instrument.id,
            side=side,
            quantity=quantity,
            execution_price=price,
            exchange_timestamp=stamp,
            order_id=values.get("order_id"),
            parent_order_key=values.get("parent_order"),
            order_type=OrderType.parse(values.get("order_type")),
            limit_price=values.get("limit_price"),
            order_quantity=values.get("order_quantity"),
            submit_timestamp=submit,
            decision_timestamp=_aware(values.get("decision_timestamp")),
            broker=values.get("broker") or defaults.broker,
            fees=fees,
            venue=exchange,
            source=ExecutionSource.CSV_IMPORT,
            metadata={"source_row_number": row.row_number},
        )
        return ResolvedTrade(
            row_number=row.row_number,
            instrument=instrument,
            execution=execution,
            resolution_method=(str(resolution.method) if resolution.method else "CREATED_FROM_ROW"),
            creates_instrument=creates,
            creates_underlying=new_underlying,
        )

    @staticmethod
    def _asset_class(
        row: ParsedRow, values: dict, defaults: TradeImportDefaults
    ) -> tuple[AssetClass | None, InvalidTrade | None]:
        token = values.get("asset_class")
        if not token:
            if defaults.asset_class is not None:
                return defaults.asset_class, None
            if values.get("strike") is not None and values.get("option_type") is not None:
                return AssetClass.OPTION, None
            if values.get("expiry") is not None:
                return AssetClass.FUTURE, None
            return AssetClass.EQUITY, None
        try:
            return AssetClass(token.strip().upper()), None
        except ValueError:
            return None, InvalidTrade(
                row.row_number,
                TradeRejection.UNKNOWN_ASSET_CLASS,
                f"Unrecognised asset class {token!r}. Expected one of "
                f"{', '.join(a.value for a in AssetClass)}.",
                row.raw,
            )

    async def _instrument_for(
        self,
        row: ParsedRow,
        resolution: ResolutionResult,
        symbol: str,
        exchange: str,
        asset_class: AssetClass,
        values: dict,
        defaults: TradeImportDefaults,
    ) -> tuple[Instrument | None, bool, Instrument | None, InvalidTrade | None]:
        if resolution.is_resolved:
            return resolution.instrument, False, None, None

        if not defaults.create_missing_instruments:
            return (
                None,
                False,
                None,
                InvalidTrade(
                    row.row_number,
                    TradeRejection.INSTRUMENT_UNRESOLVED,
                    "This contract is not in the instrument master and instrument "
                    "creation was not requested.",
                    row.raw,
                ),
            )

        underlying_id = None
        new_underlying = None
        if asset_class in {AssetClass.OPTION, AssetClass.FUTURE}:
            underlying, is_new = await self._underlying(symbol, exchange, defaults)
            underlying_id = underlying.id
            new_underlying = underlying if is_new else None

        metadata = {"created_by": "trade_log_import"}
        multiplier = values.get("multiplier")
        if multiplier is None and defaults.multiplier is not None:
            multiplier = defaults.multiplier
            metadata[MULTIPLIER_ASSUMED] = "user_default"
        elif multiplier is None:
            multiplier = Decimal(1)
            metadata[MULTIPLIER_ASSUMED] = "platform_default"

        try:
            instrument = make_instrument(
                asset_class=asset_class,
                exchange=exchange,
                symbol=symbol,
                currency=(values.get("currency") or defaults.currency),
                multiplier=multiplier,
                tick_size=defaults.tick_size,
                lot_size=defaults.lot_size,
                expiry=values.get("expiry"),
                strike=values.get("strike"),
                option_type=values.get("option_type"),
                exercise_style=(
                    defaults.exercise_style if asset_class is AssetClass.OPTION else None
                ),
                settlement_type=(
                    defaults.settlement_type
                    if asset_class in {AssetClass.OPTION, AssetClass.FUTURE}
                    else None
                ),
                underlying_id=underlying_id,
                metadata=metadata,
            )
        except InstrumentError as exc:
            return (
                None,
                False,
                None,
                InvalidTrade(row.row_number, TradeRejection.INVALID_INSTRUMENT, str(exc), row.raw),
            )
        return instrument, True, new_underlying, None

    async def _underlying(
        self, symbol: str, exchange: str, defaults: TradeImportDefaults
    ) -> tuple[Instrument, bool]:
        underlying = make_instrument(
            asset_class=AssetClass.INDEX,
            exchange=exchange,
            symbol=symbol,
            currency=defaults.currency,
            metadata={"created_by": "trade_log_import"},
        )
        existing = await self._instruments.get(underlying.id)
        return (existing, False) if existing is not None else (underlying, True)


def commit_instruments(preview: TradeImportPreview) -> list[Instrument]:
    """Contracts the commit would create, underlyings first."""
    seen: dict[uuid.UUID, Instrument] = {}
    for row in preview.resolved:
        if row.creates_underlying is not None:
            seen[row.creates_underlying.id] = row.creates_underlying
        if row.creates_instrument:
            seen[row.instrument.id] = row.instrument
    return sorted(seen.values(), key=lambda item: item.underlying_id is not None)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)
