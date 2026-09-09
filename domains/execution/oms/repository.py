"""Reads and writes for the trading tables.

Plain data access. No decisions: the repository never sets a status, never
decides whether a fill is legal and never charges a cost, because a rule that
lives in a query is a rule nobody finds when it turns out to be wrong.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from domains.execution.oms.orm import (
    OrderEventORM,
    OrderFillORM,
    OrderORM,
    TradingAccountORM,
    TradingPositionORM,
)


class TradingRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------- accounts
    async def create_account(self, **values) -> TradingAccountORM:
        row = TradingAccountORM(**values)
        self._session.add(row)
        await self._session.flush()
        return row

    async def get_account(
        self, account_id: uuid.UUID, user_id: uuid.UUID
    ) -> TradingAccountORM | None:
        stmt = select(TradingAccountORM).where(
            TradingAccountORM.id == account_id, TradingAccountORM.user_id == user_id
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_accounts(self, user_id: uuid.UUID) -> list[TradingAccountORM]:
        stmt = (
            select(TradingAccountORM)
            .where(TradingAccountORM.user_id == user_id)
            .order_by(TradingAccountORM.created_at.desc())
        )
        return list((await self._session.execute(stmt)).scalars())

    # ------------------------------------------------------------ positions
    async def positions(self, account_id: uuid.UUID) -> list[TradingPositionORM]:
        stmt = select(TradingPositionORM).where(TradingPositionORM.account_id == account_id)
        return list((await self._session.execute(stmt)).scalars())

    async def position(
        self, account_id: uuid.UUID, instrument_id: uuid.UUID
    ) -> TradingPositionORM | None:
        stmt = select(TradingPositionORM).where(
            TradingPositionORM.account_id == account_id,
            TradingPositionORM.instrument_id == instrument_id,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def upsert_position(
        self,
        account_id: uuid.UUID,
        instrument_id: uuid.UUID,
        quantity: Decimal,
        average_price: Decimal,
        realised_pnl: Decimal,
        fees_paid: Decimal,
        strategy_tag: str | None = None,
    ) -> TradingPositionORM:
        row = await self.position(account_id, instrument_id)
        if row is None:
            row = TradingPositionORM(
                account_id=account_id,
                instrument_id=instrument_id,
                quantity=quantity,
                average_price=average_price,
                realised_pnl=realised_pnl,
                fees_paid=fees_paid,
                strategy_tag=strategy_tag,
            )
            self._session.add(row)
        else:
            row.quantity = quantity
            row.average_price = average_price
            row.realised_pnl = realised_pnl
            row.fees_paid = fees_paid
            if strategy_tag is not None:
                row.strategy_tag = strategy_tag
        await self._session.flush()
        return row

    # --------------------------------------------------------------- orders
    async def create_order(self, **values) -> OrderORM:
        row = OrderORM(**values)
        self._session.add(row)
        await self._session.flush()
        return row

    async def get_order(self, order_id: uuid.UUID, user_id: uuid.UUID) -> OrderORM | None:
        stmt = select(OrderORM).where(OrderORM.id == order_id, OrderORM.user_id == user_id)
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def by_client_order_id(
        self, account_id: uuid.UUID, client_order_id: str
    ) -> OrderORM | None:
        stmt = select(OrderORM).where(
            OrderORM.account_id == account_id, OrderORM.client_order_id == client_order_id
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def list_orders(
        self,
        account_id: uuid.UUID,
        statuses: list[str] | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[OrderORM]:
        stmt = select(OrderORM).where(OrderORM.account_id == account_id)
        if statuses:
            stmt = stmt.where(OrderORM.status.in_(statuses))
        stmt = stmt.order_by(OrderORM.created_at.desc()).limit(limit).offset(offset)
        return list((await self._session.execute(stmt)).scalars())

    async def open_orders(self, account_id: uuid.UUID) -> list[OrderORM]:
        """Orders still able to trade, oldest first.

        Oldest first because a resting book is worked in the order it was
        submitted; taking them newest-first would let a later order consume
        depth an earlier one had been waiting for.
        """
        stmt = (
            select(OrderORM)
            .where(
                OrderORM.account_id == account_id,
                OrderORM.status.in_(("NEW", "ACKNOWLEDGED", "PARTIALLY_FILLED")),
            )
            .order_by(OrderORM.created_at.asc())
        )
        return list((await self._session.execute(stmt)).scalars())

    async def accounts_with_open_orders(self, venue: str | None = None) -> list[uuid.UUID]:
        stmt = select(OrderORM.account_id).where(
            OrderORM.status.in_(("NEW", "ACKNOWLEDGED", "PARTIALLY_FILLED"))
        )
        if venue is not None:
            stmt = stmt.where(OrderORM.venue == venue)
        return list((await self._session.execute(stmt.distinct())).scalars())

    async def orders_since(self, account_id: uuid.UUID, since: datetime) -> int:
        stmt = select(func.count(OrderORM.id)).where(
            OrderORM.account_id == account_id, OrderORM.created_at >= since
        )
        return int((await self._session.execute(stmt)).scalar_one())

    async def orders_in_last_minute(self, account_id: uuid.UUID, now: datetime) -> int:
        return await self.orders_since(account_id, now - timedelta(minutes=1))

    # ---------------------------------------------------------------- fills
    async def add_fill(self, **values) -> OrderFillORM:
        row = OrderFillORM(**values)
        self._session.add(row)
        await self._session.flush()
        return row

    async def fills_for_order(self, order_id: uuid.UUID) -> list[OrderFillORM]:
        stmt = (
            select(OrderFillORM)
            .where(OrderFillORM.order_id == order_id)
            .order_by(OrderFillORM.sequence.asc())
        )
        return list((await self._session.execute(stmt)).scalars())

    async def fills_for_account(
        self, account_id: uuid.UUID, since: datetime | None = None
    ) -> list[OrderFillORM]:
        stmt = select(OrderFillORM).where(OrderFillORM.account_id == account_id)
        if since is not None:
            stmt = stmt.where(OrderFillORM.filled_at >= since)
        stmt = stmt.order_by(OrderFillORM.filled_at.asc(), OrderFillORM.sequence.asc())
        return list((await self._session.execute(stmt)).scalars())

    # ---------------------------------------------------------------- audit
    async def add_event(self, **values) -> OrderEventORM:
        row = OrderEventORM(**values)
        self._session.add(row)
        await self._session.flush()
        return row

    async def events(
        self,
        account_id: uuid.UUID,
        order_id: uuid.UUID | None = None,
        limit: int = 500,
    ) -> list[OrderEventORM]:
        stmt = select(OrderEventORM).where(OrderEventORM.account_id == account_id)
        if order_id is not None:
            stmt = stmt.where(OrderEventORM.order_id == order_id)
        stmt = stmt.order_by(OrderEventORM.occurred_at.desc()).limit(limit)
        return list((await self._session.execute(stmt)).scalars())
