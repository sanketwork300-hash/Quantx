"""Instrument persistence operations.

Upserts are keyed on the canonical key, so re-importing the same chain updates
in place rather than creating duplicate contracts.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domains.instruments.enums import (
    AssetClass,
    ExerciseStyle,
    InstrumentStatus,
    OptionType,
    SettlementType,
)
from domains.instruments.models import Instrument
from domains.instruments.orm import InstrumentAliasORM, InstrumentORM


def to_domain(row: InstrumentORM) -> Instrument:
    return Instrument(
        id=row.id,
        canonical_key=row.canonical_key,
        asset_class=AssetClass(row.asset_class),
        exchange=row.exchange,
        venue=row.venue,
        symbol=row.symbol,
        underlying_id=row.underlying_id,
        currency=row.currency,
        multiplier=row.multiplier,
        tick_size=row.tick_size,
        lot_size=row.lot_size,
        expiry=row.expiry,
        strike=row.strike,
        option_type=OptionType(row.option_type) if row.option_type else None,
        exercise_style=ExerciseStyle(row.exercise_style) if row.exercise_style else None,
        settlement_type=SettlementType(row.settlement_type) if row.settlement_type else None,
        status=InstrumentStatus(row.status),
        metadata=dict(row.instrument_metadata or {}),
    )


def _apply(row: InstrumentORM, instrument: Instrument) -> InstrumentORM:
    row.canonical_key = instrument.canonical_key
    row.asset_class = str(instrument.asset_class)
    row.exchange = instrument.exchange
    row.venue = instrument.venue
    row.symbol = instrument.symbol
    row.underlying_id = instrument.underlying_id
    row.currency = instrument.currency
    row.multiplier = instrument.multiplier
    row.tick_size = instrument.tick_size
    row.lot_size = instrument.lot_size
    row.expiry = instrument.expiry
    row.strike = instrument.strike
    row.option_type = str(instrument.option_type) if instrument.option_type else None
    row.exercise_style = str(instrument.exercise_style) if instrument.exercise_style else None
    row.settlement_type = str(instrument.settlement_type) if instrument.settlement_type else None
    row.status = str(instrument.status)
    row.instrument_metadata = dict(instrument.metadata)
    return row


class InstrumentRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, instrument_id: uuid.UUID) -> Instrument | None:
        row = await self._session.get(InstrumentORM, instrument_id)
        return to_domain(row) if row else None

    async def get_by_canonical_key(self, canonical_key: str) -> Instrument | None:
        stmt = select(InstrumentORM).where(InstrumentORM.canonical_key == canonical_key)
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return to_domain(row) if row else None

    async def upsert(self, instrument: Instrument) -> Instrument:
        row = await self._session.get(InstrumentORM, instrument.id)
        if row is None:
            row = InstrumentORM(id=instrument.id)
            self._session.add(_apply(row, instrument))
        else:
            _apply(row, instrument)
        await self._session.flush()
        return to_domain(row)

    async def upsert_many(self, instruments: Sequence[Instrument]) -> list[Instrument]:
        # Underlyings must exist before the contracts that reference them, or
        # the self-referential FK fails on the first option row.
        ordered = sorted(instruments, key=lambda i: i.underlying_id is not None)
        return [await self.upsert(instrument) for instrument in ordered]

    async def search(
        self,
        *,
        asset_class: AssetClass | None = None,
        exchange: str | None = None,
        symbol: str | None = None,
        underlying_id: uuid.UUID | None = None,
        expiry: date | None = None,
        strike: Decimal | None = None,
        option_type: OptionType | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[Instrument]:
        stmt = select(InstrumentORM)
        if asset_class is not None:
            stmt = stmt.where(InstrumentORM.asset_class == str(asset_class))
        if exchange is not None:
            stmt = stmt.where(InstrumentORM.exchange == exchange.strip().upper())
        if symbol is not None:
            stmt = stmt.where(InstrumentORM.symbol == symbol.strip().upper())
        if underlying_id is not None:
            stmt = stmt.where(InstrumentORM.underlying_id == underlying_id)
        if expiry is not None:
            stmt = stmt.where(InstrumentORM.expiry == expiry)
        if strike is not None:
            stmt = stmt.where(InstrumentORM.strike == strike)
        if option_type is not None:
            stmt = stmt.where(InstrumentORM.option_type == str(option_type))
        stmt = stmt.order_by(InstrumentORM.canonical_key).limit(limit).offset(offset)
        rows = (await self._session.execute(stmt)).scalars().all()
        return [to_domain(row) for row in rows]

    # ------------------------------------------------------------- aliases
    async def add_alias(self, instrument_id: uuid.UUID, source: str, alias_symbol: str) -> None:
        stmt = select(InstrumentAliasORM).where(
            InstrumentAliasORM.source == source,
            InstrumentAliasORM.alias_symbol == alias_symbol,
        )
        existing = (await self._session.execute(stmt)).scalar_one_or_none()
        if existing is not None:
            existing.instrument_id = instrument_id
            return
        self._session.add(
            InstrumentAliasORM(
                instrument_id=instrument_id, source=source, alias_symbol=alias_symbol
            )
        )
        await self._session.flush()

    async def find_by_alias(self, source: str, alias_symbol: str) -> Instrument | None:
        stmt = (
            select(InstrumentORM)
            .join(InstrumentAliasORM, InstrumentAliasORM.instrument_id == InstrumentORM.id)
            .where(
                InstrumentAliasORM.source == source,
                InstrumentAliasORM.alias_symbol == alias_symbol,
            )
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        return to_domain(row) if row else None

    async def list_aliases(self, instrument_id: uuid.UUID) -> list[tuple[str, str]]:
        stmt = select(InstrumentAliasORM).where(InstrumentAliasORM.instrument_id == instrument_id)
        rows = (await self._session.execute(stmt)).scalars().all()
        return [(row.source, row.alias_symbol) for row in rows]
