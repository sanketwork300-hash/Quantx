"""Shared FastAPI dependencies."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from api.errors import Unauthorized
from domains.derivatives.application import DerivativesService
from domains.instruments.service import InstrumentService
from domains.jobs.service import JobService
from domains.market_data.service import MarketDataService
from domains.users.models import User
from domains.users.service import UserService
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


def instrument_service(session: SessionDep) -> InstrumentService:
    return InstrumentService(session)


def job_service(session: SessionDep) -> JobService:
    return JobService(session)


def market_data_service(
    session: SessionDep, settings: SettingsDep, store: ObjectStoreDep
) -> MarketDataService:
    return MarketDataService(session, settings, store)


def derivatives_service(session: SessionDep, settings: SettingsDep) -> DerivativesService:
    return DerivativesService(session, settings)


UserServiceDep = Annotated[UserService, Depends(user_service)]
DerivativesServiceDep = Annotated[DerivativesService, Depends(derivatives_service)]
InstrumentServiceDep = Annotated[InstrumentService, Depends(instrument_service)]
JobServiceDep = Annotated[JobService, Depends(job_service)]
MarketDataServiceDep = Annotated[MarketDataService, Depends(market_data_service)]
