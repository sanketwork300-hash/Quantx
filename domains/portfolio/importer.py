"""Portfolio CSV import.

The rule that shapes this module: **no ambiguous row is ever auto-resolved.**
Picking the most likely contract is how a portfolio silently ends up holding the
wrong expiry while every downstream number looks fine and nothing raises. So the
import returns three buckets — resolved, ambiguous, invalid — and the commit
refuses to run while any row is ambiguous.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

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
from domains.portfolio.enums import PositionSide

IMPORT_MODEL_VERSION = "portfolio-import@1.0.0"

POSITION_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec(
        "symbol",
        FieldType.STRING,
        True,
        ("symbol", "ticker", "instrument", "name", "tradingsymbol", "scrip"),
        "Contract root or ticker.",
    ),
    FieldSpec(
        "quantity",
        FieldType.DECIMAL,
        True,
        ("quantity", "qty", "netqty", "position", "netquantity", "units", "lots"),
        "Signed quantity; negative is short.",
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
        "side",
        FieldType.STRING,
        False,
        ("side", "buysell", "direction", "longshort"),
        "LONG or SHORT. Must agree with the sign of the quantity.",
    ),
    FieldSpec(
        "average_price",
        FieldType.DECIMAL,
        False,
        ("averageprice", "avgprice", "avg", "costprice", "buyprice", "price"),
        "Average entry price, for unrealised P&L.",
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
    FieldSpec(
        "strategy_tag",
        FieldType.STRING,
        False,
        ("strategy", "tag", "book", "strategytag"),
        "Free-text grouping label.",
    ),
)


class ImportRejection(StrEnum):
    MISSING_SYMBOL = "MISSING_SYMBOL"
    MISSING_QUANTITY = "MISSING_QUANTITY"
    ZERO_QUANTITY = "ZERO_QUANTITY"
    SIDE_DISAGREES_WITH_QUANTITY = "SIDE_DISAGREES_WITH_QUANTITY"
    UNKNOWN_ASSET_CLASS = "UNKNOWN_ASSET_CLASS"
    INCOMPLETE_OPTION = "INCOMPLETE_OPTION"
    INCOMPLETE_FUTURE = "INCOMPLETE_FUTURE"
    INVALID_INSTRUMENT = "INVALID_INSTRUMENT"
    UNPARSEABLE_ROW = "UNPARSEABLE_ROW"
    INSTRUMENT_UNRESOLVED = "INSTRUMENT_UNRESOLVED"


@dataclass(frozen=True, slots=True)
class ResolvedRow:
    row_number: int
    instrument: Instrument
    quantity: Decimal
    side: PositionSide
    average_price: Decimal | None
    strategy_tag: str | None
    resolution_method: str
    #: True when the contract is not yet in the instrument master and would be
    #: created by the commit. Shown in the preview so it is a decision, not a
    #: side effect.
    creates_instrument: bool = False
    #: The underlying this row's contract would also create. An option whose
    #: underlying is not persisted is a dangling reference, so the commit has to
    #: know about both.
    creates_underlying: Instrument | None = None

    def to_dict(self) -> dict:
        return {
            "row_number": self.row_number,
            "instrument_id": str(self.instrument.id),
            "canonical_key": self.instrument.canonical_key,
            "symbol": self.instrument.symbol,
            "asset_class": str(self.instrument.asset_class),
            # The contract terms are shown because a preview the reviewer cannot
            # read the expiry and strike off is not a review.
            "expiry": (self.instrument.expiry.isoformat() if self.instrument.expiry else None),
            "strike": (
                format(self.instrument.strike, "f") if self.instrument.strike is not None else None
            ),
            "option_type": (
                str(self.instrument.option_type) if self.instrument.option_type else None
            ),
            "quantity": format(self.quantity, "f"),
            "side": str(self.side),
            "average_price": (
                format(self.average_price, "f") if self.average_price is not None else None
            ),
            "multiplier": format(self.instrument.multiplier, "f"),
            "currency": self.instrument.currency,
            "strategy_tag": self.strategy_tag,
            "resolution_method": self.resolution_method,
            "creates_instrument": self.creates_instrument,
            "creates_underlying": (
                self.creates_underlying.canonical_key if self.creates_underlying else None
            ),
            "multiplier_is_assumed": self.instrument.multiplier_is_assumed,
        }


@dataclass(frozen=True, slots=True)
class AmbiguousRow:
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
class InvalidRow:
    row_number: int
    reason: ImportRejection
    message: str
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "row_number": self.row_number,
            "reason": str(self.reason),
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class ImportPreview:
    resolved: tuple[ResolvedRow, ...]
    ambiguous: tuple[AmbiguousRow, ...]
    invalid: tuple[InvalidRow, ...]
    rows_in: int

    @property
    def committable(self) -> bool:
        """A commit is refused while any row is ambiguous.

        Not a warning: an ambiguous row committed under a guess is exactly the
        failure this whole module exists to prevent.
        """
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
class ImportDefaults:
    """Contract terms the file may not carry."""

    currency: str = "INR"
    exchange: str | None = None
    asset_class: AssetClass | None = None
    #: ``None`` records the multiplier as an assumption rather than guessing.
    multiplier: Decimal | None = None
    tick_size: Decimal = Decimal("0.05")
    lot_size: Decimal = Decimal(1)
    exercise_style: ExerciseStyle = ExerciseStyle.EUROPEAN
    settlement_type: SettlementType = SettlementType.CASH
    #: Create contracts the master has never seen. A separate, explicit decision.
    create_missing_instruments: bool = True

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
        }


class PositionImporter:
    def __init__(self, instruments: InstrumentService, max_rows: int = 100_000) -> None:
        self._instruments = instruments
        self._parser = TabularParser(POSITION_FIELDS, max_rows=max_rows)

    async def preview(
        self,
        data: bytes,
        mapping: ColumnMapping,
        defaults: ImportDefaults,
        limit: int | None = None,
    ) -> ImportPreview:
        parsed = self._parser.parse(data, mapping, limit=limit)

        invalid: list[InvalidRow] = [
            InvalidRow(
                row_number=error.row_number,
                reason=ImportRejection.UNPARSEABLE_ROW,
                message=error.message,
                raw=error.raw,
            )
            for error in parsed.errors
        ]
        resolved: list[ResolvedRow] = []
        ambiguous: list[AmbiguousRow] = []

        for row in parsed.rows:
            outcome = await self._row(row, defaults)
            if isinstance(outcome, InvalidRow):
                invalid.append(outcome)
            elif isinstance(outcome, AmbiguousRow):
                ambiguous.append(outcome)
            else:
                resolved.append(outcome)

        return ImportPreview(
            resolved=tuple(resolved),
            ambiguous=tuple(ambiguous),
            invalid=tuple(invalid),
            rows_in=len(parsed.rows) + len(parsed.errors),
        )

    async def _row(
        self, row: ParsedRow, defaults: ImportDefaults
    ) -> ResolvedRow | AmbiguousRow | InvalidRow:
        values = row.values

        symbol = (values.get("symbol") or "").strip()
        if not symbol:
            return InvalidRow(
                row.row_number, ImportRejection.MISSING_SYMBOL, "Symbol is empty.", row.raw
            )

        quantity = values.get("quantity")
        if quantity is None:
            return InvalidRow(
                row.row_number, ImportRejection.MISSING_QUANTITY, "Quantity is empty.", row.raw
            )
        if quantity == 0:
            return InvalidRow(
                row.row_number,
                ImportRejection.ZERO_QUANTITY,
                "A position with zero quantity is not a position.",
                row.raw,
            )

        side = PositionSide.for_quantity(quantity)
        supplied_side = values.get("side")
        if supplied_side:
            try:
                stated = PositionSide.parse(supplied_side)
            except ValueError as exc:
                return InvalidRow(
                    row.row_number,
                    ImportRejection.SIDE_DISAGREES_WITH_QUANTITY,
                    str(exc),
                    row.raw,
                )
            if stated is not side:
                # A file that says SHORT with a positive quantity is telling you
                # something is wrong upstream. Reconciling it silently would
                # flip the sign of every risk number for this position.
                return InvalidRow(
                    row.row_number,
                    ImportRejection.SIDE_DISAGREES_WITH_QUANTITY,
                    f"Row says {stated} but the quantity {quantity} is "
                    f"{side}. Fix the file rather than have the sign guessed.",
                    row.raw,
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
            return InvalidRow(
                row.row_number,
                ImportRejection.INCOMPLETE_OPTION,
                f"An option row needs {', '.join(missing)}.",
                row.raw,
            )
        if asset_class is AssetClass.FUTURE and expiry is None:
            return InvalidRow(
                row.row_number,
                ImportRejection.INCOMPLETE_FUTURE,
                "A future row needs an expiry.",
                row.raw,
            )

        exchange = (values.get("exchange") or defaults.exchange or "").strip()
        if not exchange:
            return InvalidRow(
                row.row_number,
                ImportRejection.INSTRUMENT_UNRESOLVED,
                "No exchange in the row and no default supplied.",
                row.raw,
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
            return AmbiguousRow(
                row_number=row.row_number,
                candidates=resolution.candidates,
                raw=row.raw,
            )

        instrument, creates, new_underlying, error = await self._instrument_for(
            row, resolution, symbol, exchange, asset_class, values, defaults
        )
        if error is not None:
            return error

        return ResolvedRow(
            row_number=row.row_number,
            instrument=instrument,
            quantity=quantity,
            side=side,
            average_price=values.get("average_price"),
            strategy_tag=values.get("strategy_tag"),
            resolution_method=(str(resolution.method) if resolution.method else "CREATED_FROM_ROW"),
            creates_instrument=creates,
            creates_underlying=new_underlying,
        )

    @staticmethod
    def _asset_class(
        row: ParsedRow, values: dict, defaults: ImportDefaults
    ) -> tuple[AssetClass | None, InvalidRow | None]:
        token = values.get("asset_class")
        if not token:
            if defaults.asset_class is not None:
                return defaults.asset_class, None
            # Infer only from fields that are structurally decisive.
            if values.get("strike") is not None and values.get("option_type") is not None:
                return AssetClass.OPTION, None
            if values.get("expiry") is not None:
                return AssetClass.FUTURE, None
            return AssetClass.EQUITY, None
        try:
            return AssetClass(token.strip().upper()), None
        except ValueError:
            return None, InvalidRow(
                row.row_number,
                ImportRejection.UNKNOWN_ASSET_CLASS,
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
        defaults: ImportDefaults,
    ) -> tuple[Instrument | None, bool, Instrument | None, InvalidRow | None]:
        if resolution.is_resolved:
            return resolution.instrument, False, None, None

        if not defaults.create_missing_instruments:
            return (
                None,
                False,
                None,
                InvalidRow(
                    row.row_number,
                    ImportRejection.INSTRUMENT_UNRESOLVED,
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

        metadata = {"created_by": "portfolio_import"}
        # Where the multiplier came from is recorded, because a multiplier
        # rescales every value and Greek for this contract. Only a number that
        # was in the file is a fact; a default is an assumption either way.
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
                InvalidRow(row.row_number, ImportRejection.INVALID_INSTRUMENT, str(exc), row.raw),
            )
        return instrument, True, new_underlying, None

    async def _underlying(
        self, symbol: str, exchange: str, defaults: ImportDefaults
    ) -> tuple[Instrument, bool]:
        underlying = make_instrument(
            asset_class=AssetClass.INDEX,
            exchange=exchange,
            symbol=symbol,
            currency=defaults.currency,
            metadata={"created_by": "portfolio_import"},
        )
        existing = await self._instruments.get(underlying.id)
        return (existing, False) if existing is not None else (underlying, True)


def commit_instruments(preview: ImportPreview) -> list[Instrument]:
    """Contracts the commit would create, underlyings first.

    Underlyings first because a contract's ``underlying_id`` points at one; a
    commit that inserted the option alone would leave a dangling reference.
    """
    seen: dict[uuid.UUID, Instrument] = {}
    for row in preview.resolved:
        if row.creates_underlying is not None:
            seen[row.creates_underlying.id] = row.creates_underlying
        if row.creates_instrument:
            seen[row.instrument.id] = row.instrument
    return sorted(seen.values(), key=lambda i: i.underlying_id is not None)
