"""Market-data persistence operations."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from domains.market_data.orm import (
    DataQualityReportORM,
    MarketQuoteORM,
    OptionChainSnapshotORM,
    OptionQuoteORM,
    UploadORM,
)
from domains.market_data.quality.flags import MarketDataQuality


@dataclass(frozen=True, slots=True)
class PersistableOptionQuote:
    instrument_id: uuid.UUID
    underlying_id: uuid.UUID
    source_row_number: int | None
    expiry: object
    strike: Decimal
    option_type: str
    exchange_timestamp: datetime
    receive_timestamp: datetime
    bid_price: Decimal | None
    bid_size: Decimal | None
    ask_price: Decimal | None
    ask_size: Decimal | None
    last_price: Decimal | None
    volume: Decimal | None
    open_interest: Decimal | None
    sequence_number: int | None
    underlying_price: Decimal | None
    quality: MarketDataQuality
    excluded: bool
    exclusion_reason: str | None


class MarketDataRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # -------------------------------------------------------------- uploads
    async def create_upload(self, **kwargs) -> UploadORM:
        row = UploadORM(**kwargs)
        self._session.add(row)
        await self._session.flush()
        return row

    async def get_upload(
        self, upload_id: uuid.UUID, user_id: uuid.UUID | None = None
    ) -> UploadORM | None:
        row = await self._session.get(UploadORM, upload_id)
        if row is None:
            return None
        # A UUID is not an authorization token: ownership is verified here so
        # no route can reach an upload by id alone.
        if user_id is not None and row.user_id != user_id:
            return None
        return row

    async def list_uploads(
        self, user_id: uuid.UUID, limit: int = 50, offset: int = 0
    ) -> list[UploadORM]:
        stmt = (
            select(UploadORM)
            .where(UploadORM.user_id == user_id)
            .order_by(UploadORM.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    # ------------------------------------------------------- chain snapshots
    async def create_chain_snapshot(self, **kwargs) -> OptionChainSnapshotORM:
        row = OptionChainSnapshotORM(**kwargs)
        self._session.add(row)
        await self._session.flush()
        return row

    async def get_chain_snapshot(
        self, snapshot_id: uuid.UUID, user_id: uuid.UUID | None = None
    ) -> OptionChainSnapshotORM | None:
        row = await self._session.get(OptionChainSnapshotORM, snapshot_id)
        if row is None:
            return None
        if user_id is not None and row.user_id != user_id:
            return None
        return row

    async def latest_chain_snapshot(
        self, user_id: uuid.UUID, underlying_id: uuid.UUID
    ) -> OptionChainSnapshotORM | None:
        stmt = (
            select(OptionChainSnapshotORM)
            .where(
                OptionChainSnapshotORM.user_id == user_id,
                OptionChainSnapshotORM.underlying_id == underlying_id,
            )
            .order_by(OptionChainSnapshotORM.as_of_timestamp.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_chain_snapshots(
        self,
        user_id: uuid.UUID,
        underlying_id: uuid.UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[OptionChainSnapshotORM]:
        stmt = select(OptionChainSnapshotORM).where(OptionChainSnapshotORM.user_id == user_id)
        if underlying_id is not None:
            stmt = stmt.where(OptionChainSnapshotORM.underlying_id == underlying_id)
        stmt = (
            stmt.order_by(OptionChainSnapshotORM.as_of_timestamp.desc()).limit(limit).offset(offset)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def add_option_quotes(
        self, snapshot_id: uuid.UUID, quotes: Sequence[PersistableOptionQuote]
    ) -> int:
        for quote in quotes:
            self._session.add(
                OptionQuoteORM(
                    chain_snapshot_id=snapshot_id,
                    instrument_id=quote.instrument_id,
                    underlying_id=quote.underlying_id,
                    source_row_number=quote.source_row_number,
                    expiry=quote.expiry,
                    strike=quote.strike,
                    option_type=quote.option_type,
                    exchange_timestamp=quote.exchange_timestamp,
                    receive_timestamp=quote.receive_timestamp,
                    bid_price=quote.bid_price,
                    bid_size=quote.bid_size,
                    ask_price=quote.ask_price,
                    ask_size=quote.ask_size,
                    last_price=quote.last_price,
                    volume=quote.volume,
                    open_interest=quote.open_interest,
                    sequence_number=quote.sequence_number,
                    underlying_price=quote.underlying_price,
                    overall_score=quote.quality.overall_score,
                    stale_score=quote.quality.stale_score,
                    spread_score=quote.quality.spread_score,
                    liquidity_score=quote.quality.liquidity_score,
                    consistency_score=quote.quality.consistency_score,
                    completeness_score=quote.quality.completeness_score,
                    quality_flags=[flag.to_dict() for flag in quote.quality.flags],
                    excluded=quote.excluded,
                    exclusion_reason=quote.exclusion_reason,
                )
            )
        await self._session.flush()
        return len(quotes)

    async def get_option_quotes(
        self,
        snapshot_id: uuid.UUID,
        expiry: object | None = None,
        include_excluded: bool = True,
        limit: int = 5000,
        offset: int = 0,
    ) -> list[OptionQuoteORM]:
        stmt = select(OptionQuoteORM).where(OptionQuoteORM.chain_snapshot_id == snapshot_id)
        if expiry is not None:
            stmt = stmt.where(OptionQuoteORM.expiry == expiry)
        if not include_excluded:
            stmt = stmt.where(OptionQuoteORM.excluded.is_(False))
        stmt = (
            stmt.order_by(OptionQuoteORM.expiry, OptionQuoteORM.strike, OptionQuoteORM.option_type)
            .limit(limit)
            .offset(offset)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def count_option_quotes(self, snapshot_id: uuid.UUID) -> int:
        stmt = (
            select(func.count())
            .select_from(OptionQuoteORM)
            .where(OptionQuoteORM.chain_snapshot_id == snapshot_id)
        )
        return int((await self._session.execute(stmt)).scalar_one())

    # ----------------------------------------------------------- plain quotes
    async def add_market_quote(self, **kwargs) -> MarketQuoteORM:
        row = MarketQuoteORM(**kwargs)
        self._session.add(row)
        await self._session.flush()
        return row

    async def latest_market_quote(self, instrument_id: uuid.UUID) -> MarketQuoteORM | None:
        stmt = (
            select(MarketQuoteORM)
            .where(MarketQuoteORM.instrument_id == instrument_id)
            .order_by(MarketQuoteORM.exchange_timestamp.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    # -------------------------------------------------------- quality reports
    async def add_quality_report(self, **kwargs) -> DataQualityReportORM:
        row = DataQualityReportORM(**kwargs)
        self._session.add(row)
        await self._session.flush()
        return row

    async def get_quality_report(
        self, scope_type: str, scope_id: uuid.UUID
    ) -> DataQualityReportORM | None:
        stmt = (
            select(DataQualityReportORM)
            .where(
                DataQualityReportORM.scope_type == scope_type,
                DataQualityReportORM.scope_id == scope_id,
            )
            .order_by(DataQualityReportORM.created_at.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()
