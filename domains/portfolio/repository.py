"""Portfolio persistence operations.

Every read is ownership-scoped. A UUID is not an authorization token, and a
portfolio is the most sensitive object in the platform.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from domains.portfolio.enums import PositionSide, PositionSource
from domains.portfolio.models import Portfolio, Position
from domains.portfolio.orm import (
    PortfolioORM,
    PortfolioValuationORM,
    PositionORM,
    PositionValuationORM,
)


def to_portfolio(row: PortfolioORM) -> Portfolio:
    return Portfolio(
        id=row.id,
        user_id=row.user_id,
        name=row.name,
        base_currency=row.base_currency,
        description=row.description,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def to_position(row: PositionORM) -> Position:
    return Position(
        id=row.id,
        portfolio_id=row.portfolio_id,
        instrument_id=row.instrument_id,
        quantity=row.quantity,
        side=PositionSide(row.side),
        average_price=row.average_price,
        source=PositionSource(row.source),
        strategy_tag=row.strategy_tag,
        metadata=dict(row.position_metadata or {}),
    )


class PortfolioRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------ portfolios
    async def create_portfolio(self, **kwargs) -> PortfolioORM:
        row = PortfolioORM(**kwargs)
        self._session.add(row)
        await self._session.flush()
        return row

    async def get_portfolio(
        self, portfolio_id: uuid.UUID, user_id: uuid.UUID | None = None
    ) -> PortfolioORM | None:
        row = await self._session.get(PortfolioORM, portfolio_id)
        if row is None:
            return None
        if user_id is not None and row.user_id != user_id:
            return None
        return row

    async def list_portfolios(
        self, user_id: uuid.UUID, limit: int = 100, offset: int = 0
    ) -> list[PortfolioORM]:
        stmt = (
            select(PortfolioORM)
            .where(PortfolioORM.user_id == user_id)
            .order_by(PortfolioORM.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def delete_portfolio(self, portfolio_id: uuid.UUID) -> None:
        await self._session.execute(delete(PortfolioORM).where(PortfolioORM.id == portfolio_id))
        await self._session.flush()

    # -------------------------------------------------------------- positions
    async def add_position(self, **kwargs) -> PositionORM:
        row = PositionORM(**kwargs)
        self._session.add(row)
        await self._session.flush()
        return row

    async def add_positions(self, rows: Sequence[dict]) -> list[PositionORM]:
        created = [PositionORM(**row) for row in rows]
        self._session.add_all(created)
        await self._session.flush()
        return created

    async def get_position(
        self, position_id: uuid.UUID, portfolio_id: uuid.UUID
    ) -> PositionORM | None:
        row = await self._session.get(PositionORM, position_id)
        if row is None or row.portfolio_id != portfolio_id:
            return None
        return row

    async def list_positions(self, portfolio_id: uuid.UUID) -> list[PositionORM]:
        stmt = (
            select(PositionORM)
            .where(PositionORM.portfolio_id == portfolio_id)
            .order_by(PositionORM.created_at)
        )
        return list((await self._session.execute(stmt)).scalars().all())

    async def find_position_for_instrument(
        self, portfolio_id: uuid.UUID, instrument_id: uuid.UUID
    ) -> PositionORM | None:
        stmt = select(PositionORM).where(
            PositionORM.portfolio_id == portfolio_id,
            PositionORM.instrument_id == instrument_id,
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def delete_position(self, position_id: uuid.UUID) -> None:
        await self._session.execute(delete(PositionORM).where(PositionORM.id == position_id))
        await self._session.flush()

    # ------------------------------------------------------------ valuations
    async def create_valuation(self, **kwargs) -> PortfolioValuationORM:
        row = PortfolioValuationORM(**kwargs)
        self._session.add(row)
        await self._session.flush()
        return row

    async def add_position_valuations(self, valuation_id: uuid.UUID, valuations) -> None:
        for valuation in valuations:
            self._session.add(
                PositionValuationORM(
                    valuation_id=valuation_id,
                    position_id=valuation.position_id,
                    instrument_id=valuation.instrument_id,
                    canonical_key=valuation.canonical_key,
                    asset_class=str(valuation.asset_class),
                    underlying_id=valuation.underlying_id,
                    expiry=valuation.expiry,
                    strike=valuation.strike,
                    option_type=(str(valuation.option_type) if valuation.option_type else None),
                    quantity=valuation.quantity,
                    multiplier=valuation.multiplier,
                    currency=valuation.currency,
                    market_price=valuation.market_price,
                    model_price=valuation.model_price,
                    price_used=valuation.price_used,
                    valuation_method=str(valuation.valuation_method),
                    market_value=valuation.market_value,
                    base_market_value=valuation.base_market_value,
                    fx_rate=valuation.fx_rate,
                    unrealized_pnl=valuation.unrealized_pnl,
                    delta=valuation.greeks.delta,
                    gamma=valuation.greeks.gamma,
                    vega_per_vol_point=valuation.greeks.vega_per_vol_point,
                    theta_per_day=valuation.greeks.theta_per_day,
                    rho_per_bp=valuation.greeks.rho_per_bp,
                    greek_source=str(valuation.greek_source),
                    implied_volatility=valuation.implied_volatility,
                    time_to_expiry=valuation.time_to_expiry,
                    quote_age_seconds=valuation.quote_age_seconds,
                    warnings=list(valuation.warnings),
                )
            )
        await self._session.flush()

    async def get_valuation(
        self, valuation_id: uuid.UUID, user_id: uuid.UUID | None = None
    ) -> PortfolioValuationORM | None:
        row = await self._session.get(PortfolioValuationORM, valuation_id)
        if row is None:
            return None
        if user_id is not None and row.user_id != user_id:
            return None
        return row

    async def latest_valuation(
        self, portfolio_id: uuid.UUID, user_id: uuid.UUID
    ) -> PortfolioValuationORM | None:
        stmt = (
            select(PortfolioValuationORM)
            .where(
                PortfolioValuationORM.portfolio_id == portfolio_id,
                PortfolioValuationORM.user_id == user_id,
            )
            .order_by(PortfolioValuationORM.created_at.desc())
            .limit(1)
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    async def get_position_valuations(
        self, valuation_id: uuid.UUID, limit: int = 20000
    ) -> list[PositionValuationORM]:
        stmt = (
            select(PositionValuationORM)
            .where(PositionValuationORM.valuation_id == valuation_id)
            .order_by(PositionValuationORM.canonical_key)
            .limit(limit)
        )
        return list((await self._session.execute(stmt)).scalars().all())


def decimal_or_none(value) -> Decimal | None:
    return None if value is None else Decimal(str(value))
