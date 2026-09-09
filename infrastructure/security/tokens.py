"""JWT access tokens."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

ALGORITHM = "HS256"

#: ``typ`` claim values. A token minted for one purpose must not be accepted for
#: another: an OAuth handoff token is short-lived and travels through a browser
#: redirect, so it must never be usable as an API access token.
ACCESS_TYPE = "access"
OAUTH_STATE_TYPE = "oauth_state"


class TokenError(Exception):
    pass


def create_access_token(
    subject: str,
    secret_key: str,
    ttl_minutes: int = 60,
    extra_claims: dict[str, Any] | None = None,
) -> tuple[str, int]:
    """Return ``(token, expires_in_seconds)``."""
    now = datetime.now(UTC)
    expires = now + timedelta(minutes=ttl_minutes)
    payload: dict[str, Any] = {
        "sub": subject,
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int(expires.timestamp()),
        "jti": uuid.uuid4().hex,
        "typ": ACCESS_TYPE,
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, secret_key, algorithm=ALGORITHM), int(ttl_minutes * 60)


def create_state_token(
    subject: str,
    secret_key: str,
    ttl_minutes: int,
    extra_claims: dict[str, Any] | None = None,
) -> tuple[str, str, int]:
    """Mint an OAuth ``state`` value. Returns ``(token, nonce, expires_in)``.

    The nonce is returned separately so the caller can store it and refuse a
    replay: a signed state is otherwise valid for its whole lifetime, and an
    authorization code that is presented twice should be accepted once.
    """
    now = datetime.now(UTC)
    expires = now + timedelta(minutes=ttl_minutes)
    nonce = uuid.uuid4().hex
    payload: dict[str, Any] = {
        "sub": subject,
        "iat": int(now.timestamp()),
        "nbf": int(now.timestamp()),
        "exp": int(expires.timestamp()),
        "jti": nonce,
        "typ": OAUTH_STATE_TYPE,
    }
    if extra_claims:
        payload.update(extra_claims)
    payload["typ"] = OAUTH_STATE_TYPE
    return jwt.encode(payload, secret_key, algorithm=ALGORITHM), nonce, int(ttl_minutes * 60)


def decode_token(
    token: str, secret_key: str, expected_type: str | None = ACCESS_TYPE
) -> dict[str, Any]:
    try:
        claims = jwt.decode(
            token,
            secret_key,
            algorithms=[ALGORITHM],
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError("invalid token") from exc

    if expected_type is not None and claims.get("typ") != expected_type:
        raise TokenError(f"token is a {claims.get('typ')!r} token, not a {expected_type!r} token")
    return claims
