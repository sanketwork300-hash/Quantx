"""Password hashing.

``bcrypt`` is used directly rather than through passlib: passlib's bcrypt
backend is broken against bcrypt >= 4.1 and the wrapper buys nothing here.

bcrypt silently truncates inputs at 72 bytes, so longer passwords are rejected
explicitly. Silent truncation would mean two different passwords authenticating
the same account.
"""

from __future__ import annotations

import bcrypt

BCRYPT_ROUNDS = 12
MAX_PASSWORD_BYTES = 72
MIN_PASSWORD_LENGTH = 10


class PasswordError(ValueError):
    pass


def validate_password_strength(password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise PasswordError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise PasswordError(
            f"password must be at most {MAX_PASSWORD_BYTES} bytes when UTF-8 encoded"
        )


def hash_password(password: str) -> str:
    validate_password_strength(password)
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=BCRYPT_ROUNDS)).decode()


def verify_password(password: str, password_hash: str) -> bool:
    encoded = password.encode("utf-8")
    if len(encoded) > MAX_PASSWORD_BYTES:
        return False
    try:
        return bcrypt.checkpw(encoded, password_hash.encode("utf-8"))
    except ValueError:
        # Malformed stored hash: fail closed rather than raising into the route.
        return False
