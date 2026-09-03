"""Portfolio CRUD and position management."""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from domains.instruments.service import InstrumentService
from domains.portfolio.enums import PositionSide, PositionSource
from domains.portfolio.models import Portfolio, PortfolioError, Position
from domains.portfolio.repository import PortfolioRepository, to_portfolio, to_position


class PortfolioNotFound(Exception):
    pass


class PortfolioService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self.repository = PortfolioRepository(session)
        self.instruments = InstrumentService(session)

    # ------------------------------------------------------------ portfolios
    async def create(
        self,
        user_id: uuid.UUID,
        name: str,
        base_currency: str,
        description: str | None = None,
    ) -> Portfolio:
        if len(base_currency) != 3:
            raise PortfolioError(
                f"base_currency must be a 3-letter ISO code, got {base_currency!r}"
            )
        row = await self.repository.create_portfolio(
            user_id=user_id,
            name=name.strip(),
            base_currency=base_currency.upper(),
            description=description,
        )
        return to_portfolio(row)

    async def get(self, portfolio_id: uuid.UUID, user_id: uuid.UUID) -> Portfolio | None:
        row = await self.repository.get_portfolio(portfolio_id, user_id)
        return None if row is None else to_portfolio(row)

    async def list(self, user_id: uuid.UUID, limit: int = 100, offset: int = 0) -> list[Portfolio]:
        rows = await self.repository.list_portfolios(user_id, limit=limit, offset=offset)
        return [to_portfolio(row) for row in rows]

    async def update(
        self,
        portfolio_id: uuid.UUID,
        user_id: uuid.UUID,
        name: str | None = None,
        description: str | None = None,
    ) -> Portfolio:
        row = await self.repository.get_portfolio(portfolio_id, user_id)
        if row is None:
            raise PortfolioNotFound(str(portfolio_id))
        if name is not None:
            if not name.strip():
                raise PortfolioError("a portfolio needs a name")
            row.name = name.strip()
        if description is not None:
            row.description = description
        await self._session.flush()
        return to_portfolio(row)

    async def delete(self, portfolio_id: uuid.UUID, user_id: uuid.UUID) -> None:
        row = await self.repository.get_portfolio(portfolio_id, user_id)
        if row is None:
            raise PortfolioNotFound(str(portfolio_id))
        await self.repository.delete_portfolio(portfolio_id)

    # -------------------------------------------------------------- positions
    async def add_position(
        self,
        portfolio_id: uuid.UUID,
        user_id: uuid.UUID,
        instrument_id: uuid.UUID,
        quantity: Decimal,
        average_price: Decimal | None = None,
        side: PositionSide | None = None,
        source: PositionSource = PositionSource.MANUAL,
        strategy_tag: str | None = None,
        metadata: dict | None = None,
    ) -> Position:
        portfolio = await self.repository.get_portfolio(portfolio_id, user_id)
        if portfolio is None:
            raise PortfolioNotFound(str(portfolio_id))
        if await self.instruments.get(instrument_id) is None:
            raise PortfolioError(f"instrument {instrument_id} is not in the master")

        if quantity == 0:
            raise PortfolioError("a position with zero quantity is not a position")
        implied = PositionSide.for_quantity(quantity)
        if side is not None and side is not implied:
            raise PortfolioError(
                f"side {side} disagrees with quantity {quantity}; fix the input "
                "rather than have the sign guessed"
            )

        row = await self.repository.add_position(
            portfolio_id=portfolio_id,
            instrument_id=instrument_id,
            quantity=quantity,
            side=str(implied),
            average_price=average_price,
            source=str(source),
            strategy_tag=strategy_tag,
            position_metadata=metadata or {},
        )
        return to_position(row)

    async def update_position(
        self,
        portfolio_id: uuid.UUID,
        user_id: uuid.UUID,
        position_id: uuid.UUID,
        quantity: Decimal | None = None,
        average_price: Decimal | None = None,
        strategy_tag: str | None = None,
    ) -> Position:
        if await self.repository.get_portfolio(portfolio_id, user_id) is None:
            raise PortfolioNotFound(str(portfolio_id))
        row = await self.repository.get_position(position_id, portfolio_id)
        if row is None:
            raise PortfolioNotFound(str(position_id))

        if quantity is not None:
            if quantity == 0:
                raise PortfolioError(
                    "a position with zero quantity is not a position; delete it instead"
                )
            row.quantity = quantity
            row.side = str(PositionSide.for_quantity(quantity))
        if average_price is not None:
            row.average_price = average_price
        if strategy_tag is not None:
            row.strategy_tag = strategy_tag
        await self._session.flush()
        return to_position(row)

    async def delete_position(
        self, portfolio_id: uuid.UUID, user_id: uuid.UUID, position_id: uuid.UUID
    ) -> None:
        if await self.repository.get_portfolio(portfolio_id, user_id) is None:
            raise PortfolioNotFound(str(portfolio_id))
        row = await self.repository.get_position(position_id, portfolio_id)
        if row is None:
            raise PortfolioNotFound(str(position_id))
        await self.repository.delete_position(position_id)

    async def positions(self, portfolio_id: uuid.UUID) -> list[Position]:
        rows = await self.repository.list_positions(portfolio_id)
        return [to_position(row) for row in rows]
