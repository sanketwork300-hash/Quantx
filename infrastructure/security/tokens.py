"""JWT access tokens."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

ALGORITHM = "HS256"


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
        "typ": "access",
    }
    if extra_claims:
        payload.update(extra_claims)
    return jwt.encode(payload, secret_key, algorithm=ALGORITHM), int(ttl_minutes * 60)


def decode_token(token: str, secret_key: str) -> dict[str, Any]:
    try:
        return jwt.decode(
            token,
            secret_key,
            algorithms=[ALGORITHM],
            options={"require": ["exp", "iat", "sub"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise TokenError("token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenError("invalid token") from exc
