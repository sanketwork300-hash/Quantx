"""Instrument application service."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from domains.instruments.enums import AssetClass, OptionType
from domains.instruments.models import Instrument
from domains.instruments.repository import InstrumentRepository
from domains.instruments.resolver import (
    InstrumentResolver,
    ResolutionRequest,
    ResolutionResult,
)


class InstrumentService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self.repository = InstrumentRepository(session)
        self.resolver = InstrumentResolver(self.repository)

    async def get(self, instrument_id: uuid.UUID) -> Instrument | None:
        return await self.repository.get(instrument_id)

    async def get_by_canonical_key(self, canonical_key: str) -> Instrument | None:
        return await self.repository.get_by_canonical_key(canonical_key)

    async def upsert(self, instrument: Instrument) -> Instrument:
        return await self.repository.upsert(instrument)

    async def upsert_many(self, instruments: list[Instrument]) -> list[Instrument]:
        return await self.repository.upsert_many(instruments)

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
        return await self.repository.search(
            asset_class=asset_class,
            exchange=exchange,
            symbol=symbol,
            underlying_id=underlying_id,
            expiry=expiry,
            strike=strike,
            option_type=option_type,
            limit=limit,
            offset=offset,
        )

    async def resolve(self, request: ResolutionRequest) -> ResolutionResult:
        return await self.resolver.resolve(request)

    async def resolve_many(self, requests: list[ResolutionRequest]) -> list[ResolutionResult]:
        return await self.resolver.resolve_many(requests)

    async def add_alias(self, instrument_id: uuid.UUID, source: str, alias_symbol: str) -> None:
        await self.repository.add_alias(instrument_id, source, alias_symbol)

    async def list_aliases(self, instrument_id: uuid.UUID) -> list[tuple[str, str]]:
        return await self.repository.list_aliases(instrument_id)
