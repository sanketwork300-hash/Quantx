"""Broker and market-data provider connections.

The endpoint set exists so that a provider credential is something a signed-in
user grants through the provider's own login, not something an operator pastes
into an environment file and redeploys. Nothing here ever returns a token.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from api.dependencies.core import BrokerAuthServiceDep, CurrentUser, SessionDep
from api.errors import BadRequest, NotFound, UnprocessableEntity
from api.schemas.connections import (
    AuthorizationCallbackRequest,
    AuthorizationHandoffOut,
    BrokerConnectionOut,
    ConnectionListOut,
    ProviderListOut,
    ProviderOut,
)
from domains.broker_auth.enums import BrokerProvider
from domains.broker_auth.errors import (
    ConnectionNotFound,
    CredentialEncryptionUnavailable,
    InvalidAuthorizationState,
    ProviderNotConfigured,
)
from domains.broker_auth.oauth import OAuthError, OAuthExchangeFailed

router = APIRouter(prefix="/connections", tags=["connections"])


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.get("/providers", response_model=ProviderListOut)
async def list_providers(_user: CurrentUser, broker_auth: BrokerAuthServiceDep) -> ProviderListOut:
    """What this deployment could connect to, and what is stopping it."""
    items: list[ProviderOut] = []
    for provider in BrokerProvider:
        try:
            broker_auth.provider_config(provider)
        except ProviderNotConfigured as exc:
            items.append(
                ProviderOut(provider=provider, configured=False, missing_settings=exc.missing)
            )
        else:
            items.append(ProviderOut(provider=provider, configured=True))

    storage_ready, storage_detail = broker_auth.credential_storage_status()

    return ProviderListOut(
        items=items,
        credential_storage_ready=storage_ready,
        credential_storage_detail=storage_detail,
    )


@router.get("", response_model=ConnectionListOut)
async def list_connections(
    user: CurrentUser, broker_auth: BrokerAuthServiceDep
) -> ConnectionListOut:
    connections = await broker_auth.list_connections(user.id)
    return ConnectionListOut(
        items=[BrokerConnectionOut.model_validate(item) for item in connections]
    )


@router.get("/{provider}", response_model=BrokerConnectionOut)
async def get_connection(
    provider: BrokerProvider, user: CurrentUser, broker_auth: BrokerAuthServiceDep
) -> BrokerConnectionOut:
    connection = await broker_auth.get_connection(user.id, provider)
    if connection is None:
        raise NotFound("Broker connection")
    return BrokerConnectionOut.model_validate(connection)


@router.post("/{provider}/authorize", response_model=AuthorizationHandoffOut)
async def start_authorization(
    provider: BrokerProvider,
    user: CurrentUser,
    broker_auth: BrokerAuthServiceDep,
    session: SessionDep,
) -> AuthorizationHandoffOut:
    """Start the provider's login and return where to send the browser."""
    try:
        handoff = await broker_auth.start_authorization(user.id, provider)
    except ProviderNotConfigured as exc:
        raise UnprocessableEntity(
            "PROVIDER_NOT_CONFIGURED", str(exc), missing_settings=list(exc.missing)
        ) from exc
    except CredentialEncryptionUnavailable as exc:
        raise UnprocessableEntity("CREDENTIAL_STORAGE_UNAVAILABLE", str(exc)) from exc

    await session.commit()
    return AuthorizationHandoffOut(
        provider=handoff.provider,
        authorization_url=handoff.authorization_url,
        state=handoff.state,
        expires_in=handoff.expires_in,
    )


@router.post("/{provider}/callback", response_model=BrokerConnectionOut)
async def complete_authorization(
    provider: BrokerProvider,
    payload: AuthorizationCallbackRequest,
    request: Request,
    user: CurrentUser,
    broker_auth: BrokerAuthServiceDep,
    session: SessionDep,
) -> BrokerConnectionOut:
    """Exchange the provider's authorization code for a stored credential."""
    try:
        connection = await broker_auth.complete_authorization(
            user.id, provider, payload.code, payload.state, ip_address=_client_ip(request)
        )
    except InvalidAuthorizationState as exc:
        # One code for every way the state can fail, so probing the endpoint
        # tells the caller nothing about which check they tripped.
        raise BadRequest("AUTHORIZATION_STATE_INVALID", str(exc)) from exc
    except ProviderNotConfigured as exc:
        raise UnprocessableEntity(
            "PROVIDER_NOT_CONFIGURED", str(exc), missing_settings=list(exc.missing)
        ) from exc
    except CredentialEncryptionUnavailable as exc:
        raise UnprocessableEntity("CREDENTIAL_STORAGE_UNAVAILABLE", str(exc)) from exc
    except OAuthExchangeFailed as exc:
        # The rejection and its audit entry are both durable.
        await session.commit()
        raise UnprocessableEntity(
            "PROVIDER_REJECTED_AUTHORIZATION",
            str(exc),
            provider_error=exc.error,
            provider_status=exc.status_code,
        ) from exc
    except OAuthError as exc:
        await session.commit()
        raise UnprocessableEntity("PROVIDER_UNAVAILABLE", str(exc)) from exc

    await session.commit()
    return BrokerConnectionOut.model_validate(connection)


@router.delete("/{provider}", response_model=BrokerConnectionOut)
async def revoke_connection(
    provider: BrokerProvider,
    request: Request,
    user: CurrentUser,
    broker_auth: BrokerAuthServiceDep,
    session: SessionDep,
) -> BrokerConnectionOut:
    """Erase the stored credential.

    Returns the connection rather than 204 so the caller can see the recorded
    outcome: the local copy is gone, and the row says so.
    """
    try:
        connection = await broker_auth.revoke(user.id, provider, ip_address=_client_ip(request))
    except ConnectionNotFound as exc:
        raise NotFound("Broker connection") from exc

    await session.commit()
    return BrokerConnectionOut.model_validate(connection)
