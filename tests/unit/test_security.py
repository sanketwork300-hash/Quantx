"""Password and token primitives."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from infrastructure.security.passwords import (
    MAX_PASSWORD_BYTES,
    PasswordError,
    hash_password,
    verify_password,
)
from infrastructure.security.tokens import TokenError, create_access_token, decode_token

# At least 32 bytes: RFC 7518 section 3.2 for HS256.
SECRET = "unit-test-secret-key-with-enough-entropy-0123456789"


class TestPasswords:
    def test_hash_and_verify(self):
        hashed = hash_password("correct-horse-battery-staple")
        assert verify_password("correct-horse-battery-staple", hashed)
        assert not verify_password("wrong-horse-battery-staple", hashed)

    def test_hashes_are_salted(self):
        assert hash_password("same-password-here") != hash_password("same-password-here")

    def test_short_passwords_are_rejected(self):
        with pytest.raises(PasswordError):
            hash_password("short")

    def test_over_long_passwords_are_rejected_not_truncated(self):
        """bcrypt truncates at 72 bytes. Accepting a longer password would let
        two different passwords authenticate the same account."""
        with pytest.raises(PasswordError):
            hash_password("a" * (MAX_PASSWORD_BYTES + 1))

    def test_verify_fails_closed_on_a_malformed_hash(self):
        assert verify_password("anything-at-all", "not-a-bcrypt-hash") is False

    def test_multibyte_passwords_are_measured_in_bytes(self):
        # 30 emoji is 120 UTF-8 bytes even though it is 30 characters.
        with pytest.raises(PasswordError):
            hash_password("\N{ROCKET}" * 30)


class TestTokens:
    def test_round_trip(self):
        token, expires_in = create_access_token("user-123", SECRET, ttl_minutes=5)
        claims = decode_token(token, SECRET)
        assert claims["sub"] == "user-123"
        assert claims["typ"] == "access"
        assert expires_in == 300

    def test_tokens_carry_a_unique_id_for_revocation(self):
        first, _ = create_access_token("user-123", SECRET)
        time.sleep(0.001)
        second, _ = create_access_token("user-123", SECRET)
        assert decode_token(first, SECRET)["jti"] != decode_token(second, SECRET)["jti"]

    def test_a_different_secret_is_rejected(self):
        token, _ = create_access_token("user-123", SECRET)
        with pytest.raises(TokenError):
            decode_token(token, "another-secret-key-of-sufficient-length-0123456789")

    def test_an_expired_token_is_rejected(self):
        token, _ = create_access_token("user-123", SECRET, ttl_minutes=-1)
        with pytest.raises(TokenError):
            decode_token(token, SECRET)

    def test_a_token_without_an_expiry_is_rejected(self):
        forged = jwt.encode({"sub": "user-123"}, SECRET, algorithm="HS256")
        with pytest.raises(TokenError):
            decode_token(forged, SECRET)

    def test_the_none_algorithm_is_rejected(self):
        forged = jwt.encode(
            {
                "sub": "user-123",
                "exp": datetime.now(UTC) + timedelta(hours=1),
                "iat": datetime.now(UTC),
            },
            key="",
            algorithm="none",
        )
        with pytest.raises(TokenError):
            decode_token(forged, SECRET)
