from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime

from domains.broker_auth.enums import BrokerProvider, ConnectionStatus, ExpirySource


@dataclass(frozen=True, slots=True)
class BrokerConnection:
    """What the platform will say about a credential it holds.

    Note what is absent: the access token, the refresh token and the client
    secret. This object is what the API returns and what the logs may carry, so
    it holds no bearer material at all. ``has_refresh_token`` answers the only
    question a caller legitimately has about the refresh token — whether the
    connection can renew itself without the user.
    """

    id: uuid.UUID
    user_id: uuid.UUID
    provider: BrokerProvider
    status: ConnectionStatus
    provider_account_id: str | None
    scopes: tuple[str, ...]
    expires_at: datetime | None
    expiry_source: ExpirySource
    has_refresh_token: bool
    connected_at: datetime | None
    last_refreshed_at: datetime | None
    last_used_at: datetime | None
    last_error: str | None

    def is_expired(self, now: datetime) -> bool:
        """True only when the provider declared an expiry and it has passed.

        An undeclared expiry is not treated as "expires now" nor as "never
        expires"; it is unknown, and the answer comes from the provider when the
        credential is next used.
        """
        return self.expires_at is not None and now >= self.expires_at


@dataclass(frozen=True, slots=True)
class AuthorizationHandoff:
    """Everything the browser needs to start the provider's own login."""

    provider: BrokerProvider
    authorization_url: str
    state: str
    expires_in: int


@dataclass(frozen=True, slots=True)
class TokenGrant:
    """A provider's token response, normalised but not embellished.

    ``expires_at`` is populated only if the provider stated a lifetime. Fields
    the provider did not send stay ``None``; nothing here is defaulted to a
    plausible value.
    """

    access_token: str
    refresh_token: str | None = None
    expires_at: datetime | None = None
    expiry_source: ExpirySource = ExpirySource.UNDECLARED
    scopes: tuple[str, ...] = ()
    provider_account_id: str | None = None
    #: Names — never values — of the fields the provider sent. Recorded so that
    #: a provider quietly starting or stopping to send ``refresh_token`` is
    #: visible in the audit log instead of being inferred from behaviour.
    response_fields: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class AccessToken:
    """A usable credential handed to a provider adapter.

    Carries its expiry knowledge with it so the adapter can report a rejection
    against the right expectation, rather than assuming the token was fine.
    """

    provider: BrokerProvider
    token: str
    expires_at: datetime | None
    expiry_source: ExpirySource
