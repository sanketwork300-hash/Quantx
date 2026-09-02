"""User registration, authentication and audit logging."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domains.users.models import AuditAction, User
from domains.users.orm import AuditLogORM, UserORM
from infrastructure.security.passwords import (
    PasswordError,
    hash_password,
    verify_password,
)


class AuthError(Exception):
    """Authentication failed.

    Deliberately carries no detail distinguishing an unknown email from a wrong
    password: the caller must not be able to enumerate accounts.
    """


class EmailAlreadyRegistered(Exception):
    pass


def normalize_email(email: str) -> str:
    return email.strip().lower()


class UserService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def register(self, email: str, password: str, display_name: str | None = None) -> User:
        normalized = normalize_email(email)
        stmt = select(UserORM).where(UserORM.email == normalized)
        if (await self._session.execute(stmt)).scalar_one_or_none() is not None:
            raise EmailAlreadyRegistered(normalized)

        try:
            password_hash = hash_password(password)
        except PasswordError:
            raise

        row = UserORM(email=normalized, password_hash=password_hash, display_name=display_name)
        self._session.add(row)
        await self._session.flush()
        await self.audit(AuditAction.USER_REGISTERED, user_id=row.id)
        return _to_domain(row)

    async def authenticate(self, email: str, password: str, ip_address: str | None = None) -> User:
        normalized = normalize_email(email)
        stmt = select(UserORM).where(UserORM.email == normalized)
        row = (await self._session.execute(stmt)).scalar_one_or_none()

        if row is None:
            # Hash anyway so the response time does not reveal whether the
            # account exists.
            verify_password(password, "$2b$12$" + "x" * 53)
            await self.audit(
                AuditAction.LOGIN_FAILED, ip_address=ip_address, reason="UNKNOWN_EMAIL"
            )
            raise AuthError("invalid credentials")

        if not verify_password(password, row.password_hash):
            await self.audit(
                AuditAction.LOGIN_FAILED,
                user_id=row.id,
                ip_address=ip_address,
                reason="BAD_PASSWORD",
            )
            raise AuthError("invalid credentials")

        if not row.is_active:
            await self.audit(
                AuditAction.LOGIN_FAILED,
                user_id=row.id,
                ip_address=ip_address,
                reason="INACTIVE",
            )
            raise AuthError("invalid credentials")

        await self.audit(AuditAction.LOGIN_SUCCEEDED, user_id=row.id, ip_address=ip_address)
        return _to_domain(row)

    async def get(self, user_id: uuid.UUID) -> User | None:
        row = await self._session.get(UserORM, user_id)
        return _to_domain(row) if row else None

    async def audit(
        self,
        action: AuditAction,
        *,
        user_id: uuid.UUID | None = None,
        resource_type: str | None = None,
        resource_id: str | None = None,
        ip_address: str | None = None,
        **metadata,
    ) -> None:
        self._session.add(
            AuditLogORM(
                user_id=user_id,
                action=str(action),
                resource_type=resource_type,
                resource_id=resource_id,
                ip_address=ip_address,
                audit_metadata=metadata,
            )
        )
        await self._session.flush()


def _to_domain(row: UserORM) -> User:
    return User(id=row.id, email=row.email, is_active=row.is_active, created_at=row.created_at)
