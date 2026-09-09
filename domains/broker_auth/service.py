"""Obtaining, storing, renewing and retiring provider credentials.

The rule the whole module is arranged around: **a credential is never guessed
at.** It is used until the provider says otherwise, renewed only when the
provider gave us the means to renew it, and marked as needing the user's
attention the moment either of those stops being true. Nothing here decides on
its own that a token has probably expired.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from domains.broker_auth.enums import BrokerProvider, ConnectionStatus, ExpirySource
from domains.broker_auth.errors import (
    ConnectionNotFound,
    CredentialEncryptionUnavailable,
    InvalidAuthorizationState,
    ProviderNotConfigured,
    ReauthorizationRequired,
)
from domains.broker_auth.models import (
    AccessToken,
    AuthorizationHandoff,
    BrokerConnection,
    TokenGrant,
)
from domains.broker_auth.oauth import (
    HttpOAuthClient,
    OAuthClient,
    OAuthError,
    OAuthProviderConfig,
)
from domains.broker_auth.orm import BrokerConnectionORM
from domains.users.models import AuditAction
from domains.users.service import UserService
from infrastructure.security.crypto import Ciphertext, KeyUnavailable, SecretBox
from infrastructure.security.tokens import (
    OAUTH_STATE_TYPE,
    TokenError,
    create_state_token,
    decode_token,
)
from infrastructure.settings import Settings

#: Which settings carry each provider's app registration. Adding a broker is a
#: block of settings plus an entry here — not a change to any calling code.
PROVIDER_SETTINGS: dict[BrokerProvider, dict[str, str]] = {
    BrokerProvider.UPSTOX: {
        "client_id": "upstox_client_id",
        "client_secret": "upstox_client_secret",
        "redirect_uri": "upstox_redirect_uri",
        "authorize_url": "upstox_authorize_url",
        "token_url": "upstox_token_url",
    },
}


def _now() -> datetime:
    return datetime.now(UTC)


class BrokerAuthService:
    def __init__(
        self,
        session: AsyncSession,
        settings: Settings,
        oauth_client: OAuthClient | None = None,
    ) -> None:
        self._session = session
        self._settings = settings
        self._oauth = oauth_client or HttpOAuthClient()
        self._users = UserService(session)

    # ------------------------------------------------------- configuration
    def provider_config(self, provider: BrokerProvider) -> OAuthProviderConfig:
        fields = PROVIDER_SETTINGS.get(provider)
        if fields is None:  # pragma: no cover - unreachable while the enum is the registry
            raise ProviderNotConfigured(provider, ("registry entry",))

        values = {name: getattr(self._settings, attribute) for name, attribute in fields.items()}
        missing = tuple(
            f"QIP_{fields[name].upper()}" for name, value in sorted(values.items()) if not value
        )
        if missing:
            raise ProviderNotConfigured(provider, missing)
        return OAuthProviderConfig(provider=provider, **values)

    def _secret_box(self) -> SecretBox:
        spec = self._settings.credential_encryption_keys
        if not spec:
            raise CredentialEncryptionUnavailable(
                "QIP_CREDENTIAL_ENCRYPTION_KEYS is not set, so broker credentials cannot be "
                "stored. Generate a key with `python scripts/generate_credential_key.py`."
            )
        try:
            return SecretBox.from_spec(spec)
        except KeyUnavailable as exc:
            raise CredentialEncryptionUnavailable(str(exc)) from exc

    def credential_storage_status(self) -> tuple[bool, str | None]:
        """Whether credentials can be stored at all, and why not if they cannot.

        Reported once by the API rather than surfacing as a failure on the first
        connect attempt, because "there is nowhere safe to put this" is an
        operator problem and the operator should see it before a user does.
        """
        try:
            self._secret_box()
        except CredentialEncryptionUnavailable as exc:
            return False, str(exc)
        return True, None

    @staticmethod
    def _context(user_id: uuid.UUID, provider: BrokerProvider, field: str) -> str:
        """Associated data binding a ciphertext to its own row and column."""
        return f"broker_connection|{user_id}|{provider}|{field}"

    # ------------------------------------------------------------- reading
    async def list_connections(self, user_id: uuid.UUID) -> Sequence[BrokerConnection]:
        stmt = (
            select(BrokerConnectionORM)
            .where(BrokerConnectionORM.user_id == user_id)
            .order_by(BrokerConnectionORM.provider)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [_to_domain(row) for row in rows]

    async def get_connection(
        self, user_id: uuid.UUID, provider: BrokerProvider
    ) -> BrokerConnection | None:
        row = await self._row(user_id, provider)
        return _to_domain(row) if row is not None else None

    async def _row(
        self, user_id: uuid.UUID, provider: BrokerProvider
    ) -> BrokerConnectionORM | None:
        stmt = select(BrokerConnectionORM).where(
            BrokerConnectionORM.user_id == user_id,
            BrokerConnectionORM.provider == str(provider),
        )
        return (await self._session.execute(stmt)).scalar_one_or_none()

    # ------------------------------------------------------- authorization
    async def start_authorization(
        self, user_id: uuid.UUID, provider: BrokerProvider
    ) -> AuthorizationHandoff:
        """Begin the provider's own login. Fails now if it could not be finished.

        The encryption key is checked before the user is sent to the broker:
        walking someone through a login and then discovering there is nowhere
        safe to put the result is a worse failure than refusing up front.
        """
        config = self.provider_config(provider)
        self._secret_box()

        row = await self._row(user_id, provider)
        if row is None:
            row = BrokerConnectionORM(
                user_id=user_id,
                provider=str(provider),
                status=str(ConnectionStatus.NEEDS_REAUTHORIZATION),
                expiry_source=str(ExpirySource.UNDECLARED),
                scopes=[],
            )
            self._session.add(row)
            await self._session.flush()

        ttl_minutes = self._settings.oauth_state_ttl_minutes
        state, nonce, expires_in = create_state_token(
            subject=str(user_id),
            secret_key=self._settings.secret_key,
            ttl_minutes=ttl_minutes,
            extra_claims={"provider": str(provider), "cid": str(row.id)},
        )
        row.pending_state_nonce = nonce
        row.pending_state_expires_at = _now() + timedelta(minutes=ttl_minutes)
        await self._session.flush()

        await self._users.audit(
            AuditAction.BROKER_AUTHORIZATION_STARTED,
            user_id=user_id,
            resource_type="broker_connection",
            resource_id=str(row.id),
            provider=str(provider),
        )

        return AuthorizationHandoff(
            provider=provider,
            authorization_url=config.authorization_url(state),
            state=state,
            expires_in=expires_in,
        )

    async def complete_authorization(
        self,
        user_id: uuid.UUID,
        provider: BrokerProvider,
        code: str,
        state: str,
        ip_address: str | None = None,
    ) -> BrokerConnection:
        config = self.provider_config(provider)
        box = self._secret_box()

        try:
            claims = decode_token(state, self._settings.secret_key, expected_type=OAUTH_STATE_TYPE)
        except TokenError as exc:
            raise InvalidAuthorizationState() from exc

        if claims.get("sub") != str(user_id) or claims.get("provider") != str(provider):
            raise InvalidAuthorizationState()

        row = await self._row(user_id, provider)
        if row is None or not row.pending_state_nonce:
            raise InvalidAuthorizationState()
        if row.pending_state_nonce != claims.get("jti"):
            raise InvalidAuthorizationState()

        # Consumed whether or not the exchange succeeds: an authorization code
        # is single-use at the provider too, so a retry needs a fresh handoff.
        row.pending_state_nonce = None
        row.pending_state_expires_at = None
        await self._session.flush()

        try:
            grant = await self._oauth.exchange_code(config, code)
        except OAuthError as exc:
            # A failed *re*-connect says nothing about the credential already
            # held. Downgrading the status here would let a fumbled reconnect
            # break a connection that is still working perfectly.
            if row.access_token_ciphertext is None:
                row.status = str(ConnectionStatus.NEEDS_REAUTHORIZATION)
            row.last_error = str(exc)[:200]
            await self._session.flush()
            await self._users.audit(
                AuditAction.BROKER_AUTHORIZATION_FAILED,
                user_id=user_id,
                resource_type="broker_connection",
                resource_id=str(row.id),
                ip_address=ip_address,
                provider=str(provider),
                stage="exchange_code",
                reason=str(exc)[:200],
                credential_retained=row.access_token_ciphertext is not None,
            )
            raise

        self._store_grant(row, grant, box, user_id, provider)
        row.connected_at = _now()
        await self._session.flush()

        await self._users.audit(
            AuditAction.BROKER_CONNECTION_AUTHORIZED,
            user_id=user_id,
            resource_type="broker_connection",
            resource_id=str(row.id),
            ip_address=ip_address,
            provider=str(provider),
            expiry_source=str(grant.expiry_source),
            has_refresh_token=grant.refresh_token is not None,
            provider_response_fields=list(grant.response_fields),
        )
        return _to_domain(row)

    async def revoke(
        self, user_id: uuid.UUID, provider: BrokerProvider, ip_address: str | None = None
    ) -> BrokerConnection:
        """Disconnect: erase the secrets, keep the record that it existed.

        The platform cannot make the broker forget the token — only the broker
        can — so the audit entry says exactly what happened here: the local copy
        was destroyed.
        """
        row = await self._row(user_id, provider)
        if row is None:
            raise ConnectionNotFound(provider)

        _erase_secrets(row)
        row.status = str(ConnectionStatus.REVOKED)
        row.expires_at = None
        row.expiry_source = str(ExpirySource.UNDECLARED)
        row.pending_state_nonce = None
        row.pending_state_expires_at = None
        row.last_error = None
        await self._session.flush()

        await self._users.audit(
            AuditAction.BROKER_CONNECTION_REVOKED,
            user_id=user_id,
            resource_type="broker_connection",
            resource_id=str(row.id),
            ip_address=ip_address,
            provider=str(provider),
            note="local credential erased; provider-side revocation is not implied",
        )
        return _to_domain(row)

    # ---------------------------------------------------------------- use
    async def access_token(self, user_id: uuid.UUID, provider: BrokerProvider) -> AccessToken:
        """The credential a provider adapter should send, refreshed if it must be.

        This is the method that replaces reading a token out of the environment.
        """
        box = self._secret_box()
        row = await self._row(user_id, provider)
        if row is None:
            raise ConnectionNotFound(provider)
        if row.status == str(ConnectionStatus.REVOKED):
            raise ReauthorizationRequired(provider, "the connection was disconnected")
        if row.status == str(ConnectionStatus.NEEDS_REAUTHORIZATION):
            raise ReauthorizationRequired(
                provider, row.last_error or "the stored credential is no longer usable"
            )
        if row.access_token_ciphertext is None:
            raise ReauthorizationRequired(provider, "no credential is stored")

        now = _now()
        if row.expires_at is not None:
            skew = timedelta(seconds=self._settings.credential_refresh_skew_seconds)
            if now >= row.expires_at - skew:
                await self._refresh(row, box, user_id, provider)

        stored = Ciphertext(
            key_id=str(row.encryption_key_id),
            nonce=bytes(row.access_token_nonce or b""),
            payload=bytes(row.access_token_ciphertext or b""),
        )
        token = box.decrypt(stored, context=self._context(user_id, provider, "access"))
        if box.needs_rewrap(stored):
            self._rewrap(row, box, user_id, provider, token)

        row.last_used_at = now
        await self._session.flush()
        return AccessToken(
            provider=provider,
            token=token,
            expires_at=row.expires_at,
            expiry_source=ExpirySource(row.expiry_source),
        )

    async def report_rejection(
        self, user_id: uuid.UUID, provider: BrokerProvider, reason: str
    ) -> BrokerConnection:
        """Record that the provider refused the stored credential.

        This is how a connection with an undeclared expiry is retired: the
        adapter that got the 401 says so, rather than the platform inferring a
        lifetime the provider never published.
        """
        row = await self._row(user_id, provider)
        if row is None:
            raise ConnectionNotFound(provider)

        row.status = str(ConnectionStatus.NEEDS_REAUTHORIZATION)
        row.last_error = reason[:200]
        await self._session.flush()
        await self._users.audit(
            AuditAction.BROKER_CREDENTIAL_REJECTED,
            user_id=user_id,
            resource_type="broker_connection",
            resource_id=str(row.id),
            provider=str(provider),
            reason=reason[:200],
        )
        return _to_domain(row)

    # ------------------------------------------------------------ internals
    async def _refresh(
        self,
        row: BrokerConnectionORM,
        box: SecretBox,
        user_id: uuid.UUID,
        provider: BrokerProvider,
    ) -> None:
        if row.refresh_token_ciphertext is None:
            row.status = str(ConnectionStatus.NEEDS_REAUTHORIZATION)
            row.last_error = (
                "the provider declared an expiry and issued no refresh token, so the "
                "credential cannot be renewed without the user"
            )
            await self._session.flush()
            raise ReauthorizationRequired(provider, row.last_error)

        refresh_token = box.decrypt(
            Ciphertext(
                key_id=str(row.encryption_key_id),
                nonce=bytes(row.refresh_token_nonce or b""),
                payload=bytes(row.refresh_token_ciphertext),
            ),
            context=self._context(user_id, provider, "refresh"),
        )

        config = self.provider_config(provider)
        try:
            grant = await self._oauth.refresh(config, refresh_token)
        except OAuthError as exc:
            row.status = str(ConnectionStatus.NEEDS_REAUTHORIZATION)
            row.last_error = str(exc)[:200]
            await self._session.flush()
            await self._users.audit(
                AuditAction.BROKER_AUTHORIZATION_FAILED,
                user_id=user_id,
                resource_type="broker_connection",
                resource_id=str(row.id),
                provider=str(provider),
                stage="refresh",
                reason=str(exc)[:200],
            )
            raise ReauthorizationRequired(provider, str(exc)) from exc

        # RFC 6749 section 6 leaves a new refresh token optional. Keeping the
        # existing one when none is returned is the difference between a
        # connection that renews indefinitely and one that dies on first renewal.
        self._store_grant(
            row, grant, box, user_id, provider, keep_refresh_token=grant.refresh_token is None
        )
        row.last_refreshed_at = _now()
        await self._session.flush()
        await self._users.audit(
            AuditAction.BROKER_CONNECTION_REFRESHED,
            user_id=user_id,
            resource_type="broker_connection",
            resource_id=str(row.id),
            provider=str(provider),
            expiry_source=str(grant.expiry_source),
            rotated_refresh_token=grant.refresh_token is not None,
        )

    def _rewrap(
        self,
        row: BrokerConnectionORM,
        box: SecretBox,
        user_id: uuid.UUID,
        provider: BrokerProvider,
        access_plaintext: str,
    ) -> None:
        """Re-seal a row under the active key.

        This is what makes a key rotation finish on its own: rows migrate as
        they are used, so retiring the old key eventually becomes safe without a
        bulk re-encryption step that has to succeed all at once.
        """
        previous_key_id = str(row.encryption_key_id)

        refresh_plaintext: str | None = None
        if row.refresh_token_ciphertext is not None:
            refresh_plaintext = box.decrypt(
                Ciphertext(
                    key_id=previous_key_id,
                    nonce=bytes(row.refresh_token_nonce or b""),
                    payload=bytes(row.refresh_token_ciphertext),
                ),
                context=self._context(user_id, provider, "refresh"),
            )

        access = box.encrypt(access_plaintext, context=self._context(user_id, provider, "access"))
        row.encryption_key_id = access.key_id
        row.access_token_nonce = access.nonce
        row.access_token_ciphertext = access.payload

        if refresh_plaintext is not None:
            refresh = box.encrypt(
                refresh_plaintext, context=self._context(user_id, provider, "refresh")
            )
            row.refresh_token_nonce = refresh.nonce
            row.refresh_token_ciphertext = refresh.payload

    def _store_grant(
        self,
        row: BrokerConnectionORM,
        grant: TokenGrant,
        box: SecretBox,
        user_id: uuid.UUID,
        provider: BrokerProvider,
        keep_refresh_token: bool = False,
    ) -> None:
        # Read before the row's key id is overwritten: an existing refresh token
        # further down was sealed with the *previous* key, which after a rotation
        # is not the one about to be written here.
        previous_key_id = str(row.encryption_key_id)

        access = box.encrypt(grant.access_token, context=self._context(user_id, provider, "access"))
        row.encryption_key_id = access.key_id
        row.access_token_nonce = access.nonce
        row.access_token_ciphertext = access.payload

        if grant.refresh_token is not None:
            refresh = box.encrypt(
                grant.refresh_token, context=self._context(user_id, provider, "refresh")
            )
            row.refresh_token_nonce = refresh.nonce
            row.refresh_token_ciphertext = refresh.payload
        elif keep_refresh_token and row.refresh_token_ciphertext is not None:
            # Re-seal under the active key so the row keeps a single key id.
            existing = box.decrypt(
                Ciphertext(
                    key_id=previous_key_id,
                    nonce=bytes(row.refresh_token_nonce or b""),
                    payload=bytes(row.refresh_token_ciphertext),
                ),
                context=self._context(user_id, provider, "refresh"),
            )
            resealed = box.encrypt(existing, context=self._context(user_id, provider, "refresh"))
            row.refresh_token_nonce = resealed.nonce
            row.refresh_token_ciphertext = resealed.payload
        else:
            row.refresh_token_nonce = None
            row.refresh_token_ciphertext = None

        row.status = str(ConnectionStatus.CONNECTED)
        row.expires_at = grant.expires_at
        row.expiry_source = str(grant.expiry_source)
        row.scopes = list(grant.scopes)
        row.last_error = None
        if grant.provider_account_id is not None:
            row.provider_account_id = grant.provider_account_id


def _erase_secrets(row: BrokerConnectionORM) -> None:
    row.access_token_nonce = None
    row.access_token_ciphertext = None
    row.refresh_token_nonce = None
    row.refresh_token_ciphertext = None
    row.encryption_key_id = None


def _to_domain(row: BrokerConnectionORM) -> BrokerConnection:
    return BrokerConnection(
        id=row.id,
        user_id=row.user_id,
        provider=BrokerProvider(row.provider),
        status=ConnectionStatus(row.status),
        provider_account_id=row.provider_account_id,
        scopes=tuple(row.scopes or ()),
        expires_at=row.expires_at,
        expiry_source=ExpirySource(row.expiry_source),
        has_refresh_token=row.refresh_token_ciphertext is not None,
        connected_at=row.connected_at,
        last_refreshed_at=row.last_refreshed_at,
        last_used_at=row.last_used_at,
        last_error=row.last_error,
    )
