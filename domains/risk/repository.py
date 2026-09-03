"""Risk reads and writes, always scoped to an owner."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domains.risk.orm import (
    MarginResultORM,
    RiskSnapshotORM,
    StressResultORM,
    VaRResultORM,
)


class RiskRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------- snapshots
    async def create_snapshot(self, **kwargs) -> RiskSnapshotORM:
        row = RiskSnapshotORM(**kwargs)
        self._session.add(row)
        await self._session.flush()
        return row

    async def get_snapshot(
        self, snapshot_id: uuid.UUID, user_id: uuid.UUID | None = None
    ) -> RiskSnapshotORM | None:
        row = await self._session.get(RiskSnapshotORM, snapshot_id)
        if row is None or (user_id is not None and row.user_id != user_id):
            return None
        return row

    async def latest_snapshot(
        self, portfolio_id: uuid.UUID, user_id: uuid.UUID
    ) -> RiskSnapshotORM | None:
        result = await self._session.execute(
            select(RiskSnapshotORM)
            .where(
                RiskSnapshotORM.portfolio_id == portfolio_id,
                RiskSnapshotORM.user_id == user_id,
            )
            .order_by(RiskSnapshotORM.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    # ------------------------------------------------------------------- VaR
    async def create_var(self, **kwargs) -> VaRResultORM:
        row = VaRResultORM(**kwargs)
        self._session.add(row)
        await self._session.flush()
        return row

    async def get_var(
        self, var_id: uuid.UUID, user_id: uuid.UUID | None = None
    ) -> VaRResultORM | None:
        row = await self._session.get(VaRResultORM, var_id)
        if row is None or (user_id is not None and row.user_id != user_id):
            return None
        return row

    async def list_var(
        self, portfolio_id: uuid.UUID, user_id: uuid.UUID, limit: int = 50
    ) -> Sequence[VaRResultORM]:
        result = await self._session.execute(
            select(VaRResultORM)
            .where(
                VaRResultORM.portfolio_id == portfolio_id,
                VaRResultORM.user_id == user_id,
            )
            .order_by(VaRResultORM.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars())

    async def var_for_snapshot(self, snapshot_id: uuid.UUID) -> Sequence[VaRResultORM]:
        result = await self._session.execute(
            select(VaRResultORM)
            .where(VaRResultORM.snapshot_id == snapshot_id)
            .order_by(VaRResultORM.created_at.desc())
        )
        return list(result.scalars())

    # ---------------------------------------------------------------- stress
    async def create_stress(self, **kwargs) -> StressResultORM:
        row = StressResultORM(**kwargs)
        self._session.add(row)
        await self._session.flush()
        return row

    async def get_stress(
        self, stress_id: uuid.UUID, user_id: uuid.UUID | None = None
    ) -> StressResultORM | None:
        row = await self._session.get(StressResultORM, stress_id)
        if row is None or (user_id is not None and row.user_id != user_id):
            return None
        return row

    async def list_stress(
        self, portfolio_id: uuid.UUID, user_id: uuid.UUID, limit: int = 50
    ) -> Sequence[StressResultORM]:
        result = await self._session.execute(
            select(StressResultORM)
            .where(
                StressResultORM.portfolio_id == portfolio_id,
                StressResultORM.user_id == user_id,
            )
            .order_by(StressResultORM.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars())

    # ---------------------------------------------------------------- margin
    async def create_margin(self, **kwargs) -> MarginResultORM:
        row = MarginResultORM(**kwargs)
        self._session.add(row)
        await self._session.flush()
        return row

    async def get_margin(
        self, margin_id: uuid.UUID, user_id: uuid.UUID | None = None
    ) -> MarginResultORM | None:
        row = await self._session.get(MarginResultORM, margin_id)
        if row is None or (user_id is not None and row.user_id != user_id):
            return None
        return row

    async def latest_margin(
        self, portfolio_id: uuid.UUID, user_id: uuid.UUID
    ) -> MarginResultORM | None:
        result = await self._session.execute(
            select(MarginResultORM)
            .where(
                MarginResultORM.portfolio_id == portfolio_id,
                MarginResultORM.user_id == user_id,
            )
            .order_by(MarginResultORM.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def list_margin(
        self, portfolio_id: uuid.UUID, user_id: uuid.UUID, limit: int = 50
    ) -> Sequence[MarginResultORM]:
        result = await self._session.execute(
            select(MarginResultORM)
            .where(
                MarginResultORM.portfolio_id == portfolio_id,
                MarginResultORM.user_id == user_id,
            )
            .order_by(MarginResultORM.created_at.desc())
            .limit(limit)
        )
        return list(result.scalars())
