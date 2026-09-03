"""Execution reads and writes, always scoped to an owner.

There is deliberately no update method for `executions`. The table is
append-only; a correction is a new row.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domains.execution.models import Execution, ExecutionSource, OrderType, Side
from domains.execution.orm import (
    ExecutionORM,
    ExecutionReportORM,
    ExecutionSimulationORM,
)


def to_execution(row: ExecutionORM) -> Execution:
    return Execution(
        id=row.id,
        user_id=row.user_id,
        instrument_id=row.instrument_id,
        side=Side(row.side),
        quantity=row.quantity,
        execution_price=row.execution_price,
        exchange_timestamp=row.exchange_timestamp,
        receive_timestamp=row.receive_timestamp,
        order_id=row.order_id,
        parent_order_key=row.parent_order_key,
        order_type=OrderType(row.order_type),
        limit_price=row.limit_price,
        order_quantity=row.order_quantity,
        submit_timestamp=row.submit_timestamp,
        decision_timestamp=row.decision_timestamp,
        broker=row.broker,
        fees=row.fees,
        venue=row.venue,
        source=ExecutionSource(row.source),
        metadata=dict(row.execution_metadata or {}),
    )


class ExecutionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------ executions
    async def add_executions(self, rows: Sequence[dict]) -> list[ExecutionORM]:
        created = [ExecutionORM(**row) for row in rows]
        self._session.add_all(created)
        await self._session.flush()
        return created

    async def list_executions(
        self,
        user_id: uuid.UUID,
        instrument_id: uuid.UUID | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        parent_order_key: str | None = None,
        limit: int = 5_000,
    ) -> list[ExecutionORM]:
        stmt = select(ExecutionORM).where(ExecutionORM.user_id == user_id)
        if instrument_id is not None:
            stmt = stmt.where(ExecutionORM.instrument_id == instrument_id)
        if start is not None:
            stmt = stmt.where(ExecutionORM.exchange_timestamp >= start)
        if end is not None:
            stmt = stmt.where(ExecutionORM.exchange_timestamp <= end)
        if parent_order_key is not None:
            stmt = stmt.where(ExecutionORM.parent_order_key == parent_order_key)
        stmt = stmt.order_by(ExecutionORM.exchange_timestamp).limit(limit)
        return list((await self._session.execute(stmt)).scalars().all())

    async def count_executions(self, user_id: uuid.UUID) -> int:
        rows = await self._session.execute(
            select(ExecutionORM.id).where(ExecutionORM.user_id == user_id)
        )
        return len(list(rows.scalars()))

    # --------------------------------------------------------------- reports
    async def create_report(self, **kwargs) -> ExecutionReportORM:
        row = ExecutionReportORM(**kwargs)
        self._session.add(row)
        await self._session.flush()
        return row

    async def get_report(
        self, report_id: uuid.UUID, user_id: uuid.UUID | None = None
    ) -> ExecutionReportORM | None:
        row = await self._session.get(ExecutionReportORM, report_id)
        if row is None or (user_id is not None and row.user_id != user_id):
            return None
        return row

    async def list_reports(
        self, user_id: uuid.UUID, limit: int = 100, offset: int = 0
    ) -> Sequence[ExecutionReportORM]:
        result = await self._session.execute(
            select(ExecutionReportORM)
            .where(ExecutionReportORM.user_id == user_id)
            .order_by(ExecutionReportORM.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars())

    # ----------------------------------------------------------- simulations
    async def add_simulations(self, rows: Sequence[dict]) -> list[ExecutionSimulationORM]:
        created = [ExecutionSimulationORM(**row) for row in rows]
        self._session.add_all(created)
        await self._session.flush()
        return created

    async def get_simulation(
        self, simulation_id: uuid.UUID, user_id: uuid.UUID | None = None
    ) -> ExecutionSimulationORM | None:
        row = await self._session.get(ExecutionSimulationORM, simulation_id)
        if row is None or (user_id is not None and row.user_id != user_id):
            return None
        return row

    async def list_simulations(
        self,
        user_id: uuid.UUID,
        comparison_id: uuid.UUID | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[ExecutionSimulationORM]:
        stmt = select(ExecutionSimulationORM).where(ExecutionSimulationORM.user_id == user_id)
        if comparison_id is not None:
            stmt = stmt.where(ExecutionSimulationORM.comparison_id == comparison_id)
        stmt = (
            stmt.order_by(ExecutionSimulationORM.created_at.desc(), ExecutionSimulationORM.strategy)
            .limit(limit)
            .offset(offset)
        )
        return list((await self._session.execute(stmt)).scalars().all())
