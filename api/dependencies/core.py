"""Shared FastAPI dependencies."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from api.errors import Unauthorized
from domains.broker_auth.service import BrokerAuthService
from domains.derivatives.advanced import AdvancedDerivativesService
from domains.derivatives.application import DerivativesService
from domains.execution.application import ExecutionApplicationService
from domains.execution.oms.service import OrderManagementService
from domains.instruments.service import InstrumentService
from domains.jobs.service import JobService
from domains.market_data.live import LiveMarketDataService
from domains.market_data.service import MarketDataService
from domains.market_data.streaming.live_state import LiveMarketStore
from domains.microstructure.application import MicrostructureApplicationService
from domains.portfolio.application import PortfolioApplicationService
from domains.portfolio.optimisation import PortfolioOptimisationService
from domains.portfolio.service import PortfolioService
from domains.reports.composition import (
    ExecutionWindowComposer,
    FactorHistoryComposer,
    ValuationContextComposer,
)
from domains.reports.order_analysis import OrderAnalysisService
from domains.research.service import ResearchService
from domains.risk.application import RiskApplicationService
from domains.scenarios.service import ScenarioService
from domains.users.models import User
from domains.users.service import UserService
from domains.warehouse.service import WarehouseService
from infrastructure.cache.client import Cache, get_cache
from infrastructure.database.session import get_sessionmaker
from infrastructure.security.tokens import TokenError, decode_token
from infrastructure.settings import Settings, get_settings
from infrastructure.storage.base import ObjectStore
from infrastructure.storage.factory import get_object_store


async def db_session() -> AsyncIterator[AsyncSession]:
    maker = get_sessionmaker()
    async with maker() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


SessionDep = Annotated[AsyncSession, Depends(db_session)]


def settings_dep() -> Settings:
    return get_settings()


SettingsDep = Annotated[Settings, Depends(settings_dep)]


def object_store_dep() -> ObjectStore:
    return get_object_store()


ObjectStoreDep = Annotated[ObjectStore, Depends(object_store_dep)]


def cache_dep() -> Cache:
    return get_cache()


CacheDep = Annotated[Cache, Depends(cache_dep)]


def _bearer_token(request: Request) -> str:
    header = request.headers.get("Authorization")
    if not header or not header.lower().startswith("bearer "):
        raise Unauthorized("Provide a bearer token in the Authorization header.")
    token = header.split(" ", 1)[1].strip()
    if not token:
        raise Unauthorized("Bearer token is empty.")
    return token


async def current_user(request: Request, session: SessionDep, settings: SettingsDep) -> User:
    token = _bearer_token(request)
    try:
        claims = decode_token(token, settings.secret_key)
    except TokenError as exc:
        raise Unauthorized(str(exc)) from exc

    try:
        user_id = uuid.UUID(claims["sub"])
    except (KeyError, ValueError) as exc:
        raise Unauthorized("Token subject is not a valid user id.") from exc

    user = await UserService(session).get(user_id)
    if user is None or not user.is_active:
        raise Unauthorized("The account for this token is not available.")
    return user


CurrentUser = Annotated[User, Depends(current_user)]


def user_service(session: SessionDep) -> UserService:
    return UserService(session)


def broker_auth_service(session: SessionDep, settings: SettingsDep) -> BrokerAuthService:
    """Provider credentials. The real HTTP OAuth client; tests override it."""
    return BrokerAuthService(session, settings)


def instrument_service(session: SessionDep) -> InstrumentService:
    return InstrumentService(session)


def job_service(session: SessionDep) -> JobService:
    return JobService(session)


def market_data_service(
    session: SessionDep, settings: SettingsDep, store: ObjectStoreDep
) -> MarketDataService:
    return MarketDataService(session, settings, store)


def live_market_service(
    session: SessionDep, settings: SettingsDep, cache: CacheDep
) -> LiveMarketDataService:
    """Live market data. Reads the store the feed worker writes, never a feed."""
    return LiveMarketDataService(
        InstrumentService(session),
        LiveMarketStore(cache, settings.live_quote_ttl_seconds),
        source=settings.market_data_provider,
        subscription_ttl_seconds=settings.live_subscription_ttl_seconds,
    )


def trading_service(
    session: SessionDep,
    settings: SettingsDep,
    live: LiveMarketServiceDep,
) -> OrderManagementService:
    """The OMS. Takes the live market service because a paper fill is decided
    against an observed quote, and takes the live-trading flag because whether
    an order may reach a real broker is a deployment decision, not a per-request
    one."""
    return OrderManagementService(
        session,
        InstrumentService(session),
        live,
        settings,
        # The vault, so a live account's token is fetched and renewed per
        # request rather than read from the environment. A paper account never
        # touches it.
        broker_auth=BrokerAuthService(session, settings),
    )


def microstructure_service(
    session: SessionDep, settings: SettingsDep, store: ObjectStoreDep
) -> MicrostructureApplicationService:
    """L2 datasets. Takes the object store because the bulk data lives there."""
    return MicrostructureApplicationService(session, settings, store)


def derivatives_service(session: SessionDep, settings: SettingsDep) -> DerivativesService:
    return DerivativesService(session, settings)


def advanced_derivatives_service(
    session: SessionDep, settings: SettingsDep
) -> AdvancedDerivativesService:
    return AdvancedDerivativesService(session, settings)


def portfolio_service(session: SessionDep) -> PortfolioService:
    return PortfolioService(session)


def portfolio_application(
    session: SessionDep, settings: SettingsDep
) -> PortfolioApplicationService:
    return PortfolioApplicationService(session, settings)


def execution_service(session: SessionDep, settings: SettingsDep) -> ExecutionApplicationService:
    return ExecutionApplicationService(session, settings)


def execution_window_composer(
    session: SessionDep, settings: SettingsDep, store: ObjectStoreDep
) -> ExecutionWindowComposer:
    """Market observations for a benchmark window, from market data alone."""
    return ExecutionWindowComposer(MarketDataService(session, settings, store))


def risk_service(session: SessionDep, settings: SettingsDep) -> RiskApplicationService:
    return RiskApplicationService(session, settings)


def order_analysis_service(
    session: SessionDep, settings: SettingsDep, store: ObjectStoreDep
) -> OrderAnalysisService:
    """The full cross-engine fan-out: all five engines over one snapshot."""
    return OrderAnalysisService(session, settings, store)


def portfolio_optimisation(
    session: SessionDep, settings: SettingsDep, store: ObjectStoreDep
) -> PortfolioOptimisationService:
    """Portfolio construction. Reads warehouse history for the covariance."""
    return PortfolioOptimisationService(session, settings, store)


def research_service(
    session: SessionDep, settings: SettingsDep, store: ObjectStoreDep
) -> ResearchService:
    """Backtests and the experiment record. Reads the warehouse; writes curves
    and fills to the object store because they grow with the run's length."""
    return ResearchService(session, settings, store)


def warehouse_service(
    session: SessionDep, settings: SettingsDep, store: ObjectStoreDep
) -> WarehouseService:
    """The historical warehouse. Takes the object store because the data is
    there and only the registry is in the database."""
    return WarehouseService(session, settings, store)


def scenario_service(session: SessionDep) -> ScenarioService:
    return ScenarioService(session)


def factor_history_composer(
    session: SessionDep, settings: SettingsDep, store: ObjectStoreDep
) -> FactorHistoryComposer:
    """The other cross-engine fan-out: price history plus volatility history."""
    return FactorHistoryComposer(
        MarketDataService(session, settings, store), DerivativesService(session, settings)
    )


def valuation_composer(
    session: SessionDep, settings: SettingsDep, store: ObjectStoreDep
) -> ValuationContextComposer:
    """The one cross-engine fan-out: market data plus derivatives surfaces."""
    return ValuationContextComposer(
        MarketDataService(session, settings, store), DerivativesService(session, settings)
    )


UserServiceDep = Annotated[UserService, Depends(user_service)]
BrokerAuthServiceDep = Annotated[BrokerAuthService, Depends(broker_auth_service)]
DerivativesServiceDep = Annotated[DerivativesService, Depends(derivatives_service)]
AdvancedDerivativesDep = Annotated[
    AdvancedDerivativesService, Depends(advanced_derivatives_service)
]
InstrumentServiceDep = Annotated[InstrumentService, Depends(instrument_service)]
JobServiceDep = Annotated[JobService, Depends(job_service)]
MarketDataServiceDep = Annotated[MarketDataService, Depends(market_data_service)]
LiveMarketServiceDep = Annotated[LiveMarketDataService, Depends(live_market_service)]
MicrostructureServiceDep = Annotated[
    MicrostructureApplicationService, Depends(microstructure_service)
]
PortfolioServiceDep = Annotated[PortfolioService, Depends(portfolio_service)]
PortfolioApplicationDep = Annotated[PortfolioApplicationService, Depends(portfolio_application)]
ValuationComposerDep = Annotated[ValuationContextComposer, Depends(valuation_composer)]
RiskServiceDep = Annotated[RiskApplicationService, Depends(risk_service)]
ExecutionServiceDep = Annotated[ExecutionApplicationService, Depends(execution_service)]
ExecutionWindowComposerDep = Annotated[ExecutionWindowComposer, Depends(execution_window_composer)]
ScenarioServiceDep = Annotated[ScenarioService, Depends(scenario_service)]
WarehouseServiceDep = Annotated[WarehouseService, Depends(warehouse_service)]
ResearchServiceDep = Annotated[ResearchService, Depends(research_service)]
PortfolioOptimisationDep = Annotated[PortfolioOptimisationService, Depends(portfolio_optimisation)]
FactorHistoryComposerDep = Annotated[FactorHistoryComposer, Depends(factor_history_composer)]
OrderAnalysisServiceDep = Annotated[OrderAnalysisService, Depends(order_analysis_service)]
TradingServiceDep = Annotated[OrderManagementService, Depends(trading_service)]
