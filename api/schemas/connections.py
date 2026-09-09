from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import Field

from api.schemas.common import APIModel
from domains.broker_auth.enums import BrokerProvider, ConnectionStatus, ExpirySource


class ProviderOut(APIModel):
    """A provider the deployment is registered with and could connect to."""

    provider: BrokerProvider
    configured: bool
    #: Settings that must be filled in before this provider can be connected.
    #: Named rather than described so an operator can act on it directly.
    missing_settings: tuple[str, ...] = ()


class ProviderListOut(APIModel):
    items: list[ProviderOut]
    #: False when no encryption key is configured. Every connect attempt will be
    #: refused until it is, and the reason is stated once here rather than
    #: discovered per request.
    credential_storage_ready: bool
    credential_storage_detail: str | None = None


class BrokerConnectionOut(APIModel):
    """A held credential, described without any part of it being disclosed.

    There is no token field on this model and no code path that adds one.
    """

    id: uuid.UUID
    provider: BrokerProvider
    status: ConnectionStatus
    provider_account_id: str | None
    scopes: tuple[str, ...]
    #: Present only when the provider stated a lifetime; see ``expiry_source``.
    expires_at: datetime | None
    expiry_source: ExpirySource
    #: Whether the connection can renew itself without the user returning.
    has_refresh_token: bool
    connected_at: datetime | None
    last_refreshed_at: datetime | None
    last_used_at: datetime | None
    last_error: str | None


class ConnectionListOut(APIModel):
    items: list[BrokerConnectionOut]


class AuthorizationHandoffOut(APIModel):
    """Where to send the browser, and the state that must come back with it."""

    provider: BrokerProvider
    authorization_url: str
    state: str
    expires_in: int


class AuthorizationCallbackRequest(APIModel):
    """The authorization code the provider handed back, plus its state.

    Posted by the frontend after the provider redirects to it, so the code never
    reaches the API without the caller also proving who they are.
    """

    code: str = Field(min_length=1, max_length=4096)
    state: str = Field(min_length=1, max_length=4096)
