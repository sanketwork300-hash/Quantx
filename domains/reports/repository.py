"""Persistence for assembled multi-domain reports."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domains.reports.orm import OrderAnalysisORM


class ReportsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_order_analysis(self, **kwargs) -> OrderAnalysisORM:
        row = OrderAnalysisORM(**kwargs)
        self._session.add(row)
        await self._session.flush()
        return row

    async def get_order_analysis(
        self, analysis_id: uuid.UUID, user_id: uuid.UUID
    ) -> OrderAnalysisORM | None:
        """Scoped to the owner in the query, never filtered afterwards."""
        stmt = select(OrderAnalysisORM).where(
            OrderAnalysisORM.id == analysis_id, OrderAnalysisORM.user_id == user_id
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_order_analyses(
        self,
        user_id: uuid.UUID,
        portfolio_id: uuid.UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> Sequence[OrderAnalysisORM]:
        stmt = select(OrderAnalysisORM).where(OrderAnalysisORM.user_id == user_id)
        if portfolio_id is not None:
            stmt = stmt.where(OrderAnalysisORM.portfolio_id == portfolio_id)
        stmt = stmt.order_by(OrderAnalysisORM.created_at.desc()).limit(limit).offset(offset)
        return (await self._session.execute(stmt)).scalars().all()
