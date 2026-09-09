"""The provider side of an authorization-code exchange.

Everything here is RFC 6749 — the authorization request of section 4.1.1, the
token request of section 4.1.3 and the refresh request of section 6 — because
the standard is the only thing that can be relied on across providers. Anything
a particular broker does beyond the RFC is read if it is present and ignored if
it is not; nothing is assumed on a provider's behalf.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from domains.broker_auth.enums import BrokerProvider, ExpirySource
from domains.broker_auth.models import TokenGrant

#: Keys some providers use to name the account the grant belongs to. Read when
#: present so the UI can show *which* broker account is connected; its absence
#: is not an error and is never filled in with a substitute.
ACCOUNT_ID_FIELDS = ("user_id", "account_id", "sub")

#: Seconds to allow a token endpoint. A provider that is slow here should fail
#: the connect attempt rather than hold an API worker.
DEFAULT_TIMEOUT_SECONDS = 20.0


class OAuthError(Exception):
    """The provider's authorization server could not be used."""


class OAuthExchangeFailed(OAuthError):
    """The provider refused the code or the refresh token.

    ``error`` carries the provider's own error identifier when it sent one
    (RFC 6749 section 5.2), because "invalid_grant" and "invalid_client" call
    for very different responses from the operator.
    """

    def __init__(self, provider: BrokerProvider, status_code: int, error: str | None) -> None:
        super().__init__(
            f"{provider} token endpoint returned {status_code}" + (f" ({error})" if error else "")
        )
        self.provider = provider
        self.status_code = status_code
        self.error = error


@dataclass(frozen=True, slots=True)
class OAuthProviderConfig:
    """One provider's app registration. Long-lived; not a per-session token."""

    provider: BrokerProvider
    client_id: str
    client_secret: str
    redirect_uri: str
    authorize_url: str
    token_url: str
    scopes: tuple[str, ...] = ()

    def authorization_url(self, state: str) -> str:
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "state": state,
        }
        if self.scopes:
            params["scope"] = " ".join(self.scopes)
        separator = "&" if "?" in self.authorize_url else "?"
        return f"{self.authorize_url}{separator}{urlencode(params)}"


def parse_token_response(
    payload: Mapping[str, Any], provider: BrokerProvider, now: datetime | None = None
) -> TokenGrant:
    """Normalise a token response without inventing anything it omitted.

    The only field required is ``access_token``. An expiry is recorded only when
    the provider sent ``expires_in``; otherwise the grant is marked
    :data:`~domains.broker_auth.enums.ExpirySource.UNDECLARED` and the platform
    learns the token is dead from the provider rejecting it. Guessing a lifetime
    would either retire good credentials early or hide that they had lapsed.
    """
    now = now or datetime.now(UTC)

    access_token = payload.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise OAuthError(f"{provider} token response carried no access_token")

    expires_at: datetime | None = None
    expiry_source = ExpirySource.UNDECLARED
    raw_expires_in = payload.get("expires_in")
    if raw_expires_in is not None:
        try:
            seconds = int(raw_expires_in)
        except (TypeError, ValueError):
            seconds = -1
        if seconds > 0:
            expires_at = now + timedelta(seconds=seconds)
            expiry_source = ExpirySource.PROVIDER_DECLARED

    refresh_token = payload.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        refresh_token = None

    raw_scope = payload.get("scope")
    if isinstance(raw_scope, str):
        scopes = tuple(part for part in raw_scope.split() if part)
    elif isinstance(raw_scope, list):
        scopes = tuple(str(part) for part in raw_scope if str(part))
    else:
        scopes = ()

    account_id: str | None = None
    for key in ACCOUNT_ID_FIELDS:
        value = payload.get(key)
        if isinstance(value, str | int) and str(value):
            account_id = str(value)
            break

    return TokenGrant(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=expires_at,
        expiry_source=expiry_source,
        scopes=scopes,
        provider_account_id=account_id,
        response_fields=tuple(sorted(str(key) for key in payload)),
    )


class OAuthClient(ABC):
    """The seam that keeps the provider's network off the test suite."""

    @abstractmethod
    async def exchange_code(self, config: OAuthProviderConfig, code: str) -> TokenGrant: ...

    @abstractmethod
    async def refresh(self, config: OAuthProviderConfig, refresh_token: str) -> TokenGrant: ...


class HttpOAuthClient(OAuthClient):
    """Talks to a real authorization server over HTTPS."""

    def __init__(self, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self._timeout = timeout_seconds

    async def exchange_code(self, config: OAuthProviderConfig, code: str) -> TokenGrant:
        return await self._post(
            config,
            {
                "grant_type": "authorization_code",
                "code": code,
                "client_id": config.client_id,
                "client_secret": config.client_secret,
                "redirect_uri": config.redirect_uri,
            },
        )

    async def refresh(self, config: OAuthProviderConfig, refresh_token: str) -> TokenGrant:
        return await self._post(
            config,
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": config.client_id,
                "client_secret": config.client_secret,
            },
        )

    async def _post(self, config: OAuthProviderConfig, form: dict[str, str]) -> TokenGrant:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    config.token_url,
                    data=form,
                    headers={"Accept": "application/json"},
                )
        except httpx.HTTPError as exc:
            raise OAuthError(f"{config.provider} token endpoint unreachable: {exc}") from exc

        try:
            payload = response.json()
        except ValueError:
            payload = {}

        if response.status_code >= 400 or not isinstance(payload, dict):
            error = payload.get("error") if isinstance(payload, dict) else None
            raise OAuthExchangeFailed(
                config.provider, response.status_code, str(error) if error else None
            )
        return parse_token_response(payload, config.provider)
