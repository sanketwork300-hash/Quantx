"""Persistence for the dataset registry."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from domains.warehouse.orm import WarehouseDatasetORM, WarehousePartitionORM


class WarehouseRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # -------------------------------------------------------------- datasets
    async def create_dataset(self, **kwargs) -> WarehouseDatasetORM:
        row = WarehouseDatasetORM(**kwargs)
        self._session.add(row)
        await self._session.flush()
        return row

    async def get_dataset(
        self, dataset_id: uuid.UUID, user_id: uuid.UUID | None = None
    ) -> WarehouseDatasetORM | None:
        stmt = select(WarehouseDatasetORM).where(WarehouseDatasetORM.id == dataset_id)
        if user_id is not None:
            # 404 rather than 403 on a foreign dataset is the API's job; the
            # repository just refuses to find it.
            stmt = stmt.where(WarehouseDatasetORM.user_id == user_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_datasets(
        self,
        user_id: uuid.UUID,
        layer: str | None = None,
        kind: str | None = None,
        exchange: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[WarehouseDatasetORM]:
        stmt = select(WarehouseDatasetORM).where(WarehouseDatasetORM.user_id == user_id)
        if layer is not None:
            stmt = stmt.where(WarehouseDatasetORM.layer == layer)
        if kind is not None:
            stmt = stmt.where(WarehouseDatasetORM.kind == kind)
        if exchange is not None:
            stmt = stmt.where(WarehouseDatasetORM.exchange == exchange)
        stmt = stmt.order_by(WarehouseDatasetORM.created_at.desc()).limit(limit).offset(offset)
        return list((await self._session.execute(stmt)).scalars().all())

    # ------------------------------------------------------------ partitions
    async def replace_partitions(self, dataset_id: uuid.UUID, partitions: Sequence[dict]) -> int:
        """Write the partition rows for a dataset, replacing any it already had.

        A partition write is a whole-file replacement, so the registry follows:
        re-ingesting a day leaves one row for it, not two.
        """
        await self._session.execute(
            delete(WarehousePartitionORM).where(WarehousePartitionORM.dataset_id == dataset_id)
        )
        for payload in partitions:
            self._session.add(WarehousePartitionORM(dataset_id=dataset_id, **payload))
        await self._session.flush()
        return len(partitions)

    async def list_partitions(
        self, dataset_id: uuid.UUID, limit: int = 2000
    ) -> list[WarehousePartitionORM]:
        stmt = (
            select(WarehousePartitionORM)
            .where(WarehousePartitionORM.dataset_id == dataset_id)
            .order_by(WarehousePartitionORM.day, WarehousePartitionORM.instrument_id)
            .limit(limit)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def partitions_for_instruments(
        self,
        user_id: uuid.UUID,
        instrument_ids: Sequence[uuid.UUID],
        layer: str,
        kind: str,
    ) -> list[WarehousePartitionORM]:
        """Every partition of a kind covering these instruments, for this user."""
        stmt = (
            select(WarehousePartitionORM)
            .join(
                WarehouseDatasetORM,
                WarehouseDatasetORM.id == WarehousePartitionORM.dataset_id,
            )
            .where(
                WarehouseDatasetORM.user_id == user_id,
                WarehouseDatasetORM.layer == layer,
                WarehouseDatasetORM.kind == kind,
                WarehousePartitionORM.instrument_id.in_(instrument_ids),
            )
            .order_by(WarehousePartitionORM.day)
        )
        return list((await self._session.execute(stmt)).scalars().all())
