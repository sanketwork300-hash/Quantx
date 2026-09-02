from __future__ import annotations

from fastapi import APIRouter, Request, status

from api.dependencies.core import CurrentUser, SessionDep, SettingsDep, UserServiceDep
from api.errors import Conflict, Unauthorized, UnprocessableEntity
from api.schemas.auth import LoginRequest, RegisterRequest, TokenResponse, UserOut
from domains.users.service import AuthError, EmailAlreadyRegistered
from infrastructure.security.passwords import PasswordError
from infrastructure.security.tokens import create_access_token

router = APIRouter(prefix="/auth", tags=["auth"])


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.post("/register", response_model=UserOut, status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterRequest, users: UserServiceDep, session: SessionDep) -> UserOut:
    try:
        user = await users.register(
            payload.email, payload.password, display_name=payload.display_name
        )
    except EmailAlreadyRegistered as exc:
        raise Conflict(
            "EMAIL_ALREADY_REGISTERED", "An account with that email already exists."
        ) from exc
    except PasswordError as exc:
        raise UnprocessableEntity("WEAK_PASSWORD", str(exc)) from exc

    # The route owns the transaction; services never commit.
    await session.commit()
    return UserOut.model_validate(user)


@router.post("/login", response_model=TokenResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    users: UserServiceDep,
    session: SessionDep,
    settings: SettingsDep,
) -> TokenResponse:
    try:
        user = await users.authenticate(
            payload.email, payload.password, ip_address=_client_ip(request)
        )
    except AuthError as exc:
        # The failed-login audit entry must survive the rejection.
        await session.commit()
        raise Unauthorized("Invalid credentials.") from exc

    token, expires_in = create_access_token(
        subject=str(user.id),
        secret_key=settings.secret_key,
        ttl_minutes=settings.access_token_ttl_minutes,
    )
    await session.commit()
    return TokenResponse(access_token=token, expires_in=expires_in)


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUser) -> UserOut:
    return UserOut.model_validate(user)
