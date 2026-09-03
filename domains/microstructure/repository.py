"""Microstructure reads and writes, always scoped to an owner.

Every query filters on ``user_id`` in the same statement that filters on the
id, so a foreign dataset is a 404 rather than a 403 — the platform's standing
rule, which keeps the existence of another user's data unobservable.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domains.microstructure.orm import (
    BookAnalyticsReportORM,
    IntensityModelORM,
    MicrostructureDatasetORM,
    QueueEstimateORM,
)


class MicrostructureRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # -------------------------------------------------------------- datasets
    async def add_dataset(self, **values) -> MicrostructureDatasetORM:
        row = MicrostructureDatasetORM(**values)
        self._session.add(row)
        await self._session.flush()
        return row

    async def get_dataset(
        self, dataset_id: uuid.UUID, user_id: uuid.UUID
    ) -> MicrostructureDatasetORM | None:
        statement = select(MicrostructureDatasetORM).where(
            MicrostructureDatasetORM.id == dataset_id,
            MicrostructureDatasetORM.user_id == user_id,
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def list_datasets(
        self,
        user_id: uuid.UUID,
        instrument_id: uuid.UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[MicrostructureDatasetORM]:
        statement = select(MicrostructureDatasetORM).where(
            MicrostructureDatasetORM.user_id == user_id
        )
        if instrument_id is not None:
            statement = statement.where(MicrostructureDatasetORM.instrument_id == instrument_id)
        statement = (
            statement.order_by(MicrostructureDatasetORM.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return (await self._session.execute(statement)).scalars().all()

    # --------------------------------------------------------------- reports
    async def add_report(self, **values) -> BookAnalyticsReportORM:
        row = BookAnalyticsReportORM(**values)
        self._session.add(row)
        await self._session.flush()
        return row

    async def get_report(
        self, report_id: uuid.UUID, user_id: uuid.UUID
    ) -> BookAnalyticsReportORM | None:
        statement = select(BookAnalyticsReportORM).where(
            BookAnalyticsReportORM.id == report_id,
            BookAnalyticsReportORM.user_id == user_id,
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def list_reports(
        self,
        user_id: uuid.UUID,
        dataset_id: uuid.UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[BookAnalyticsReportORM]:
        statement = select(BookAnalyticsReportORM).where(BookAnalyticsReportORM.user_id == user_id)
        if dataset_id is not None:
            statement = statement.where(BookAnalyticsReportORM.dataset_id == dataset_id)
        statement = (
            statement.order_by(BookAnalyticsReportORM.created_at.desc()).limit(limit).offset(offset)
        )
        return (await self._session.execute(statement)).scalars().all()

    # ------------------------------------------------------------- intensity
    async def add_intensity(self, **values) -> IntensityModelORM:
        row = IntensityModelORM(**values)
        self._session.add(row)
        await self._session.flush()
        return row

    async def get_intensity(
        self, model_id: uuid.UUID, user_id: uuid.UUID
    ) -> IntensityModelORM | None:
        statement = select(IntensityModelORM).where(
            IntensityModelORM.id == model_id, IntensityModelORM.user_id == user_id
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def list_intensities(
        self,
        user_id: uuid.UUID,
        dataset_id: uuid.UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[IntensityModelORM]:
        statement = select(IntensityModelORM).where(IntensityModelORM.user_id == user_id)
        if dataset_id is not None:
            statement = statement.where(IntensityModelORM.dataset_id == dataset_id)
        statement = (
            statement.order_by(IntensityModelORM.created_at.desc()).limit(limit).offset(offset)
        )
        return (await self._session.execute(statement)).scalars().all()

    # ----------------------------------------------------------------- queue
    async def add_queue_estimate(self, **values) -> QueueEstimateORM:
        row = QueueEstimateORM(**values)
        self._session.add(row)
        await self._session.flush()
        return row

    async def list_queue_estimates(
        self,
        user_id: uuid.UUID,
        dataset_id: uuid.UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[QueueEstimateORM]:
        statement = select(QueueEstimateORM).where(QueueEstimateORM.user_id == user_id)
        if dataset_id is not None:
            statement = statement.where(QueueEstimateORM.dataset_id == dataset_id)
        statement = (
            statement.order_by(QueueEstimateORM.created_at.desc()).limit(limit).offset(offset)
        )
        return (await self._session.execute(statement)).scalars().all()
