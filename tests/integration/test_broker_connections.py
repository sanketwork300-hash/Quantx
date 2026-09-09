"""Connecting a broker without a token ever touching the environment.

The arrangement being replaced: an operator logs in to the broker by hand, copies
an access token into ``.env``, restarts the process, and repeats the next time
the broker expires it. Everyone on the installation then shares one identity, the
token sits in shell history and process listings, and the platform finds out it
has gone stale by failing a request.

These tests pin the replacement end to end: the user authorizes at the provider,
the code is exchanged server-side, the token is sealed before it is stored, it is
renewed without anyone being asked, and when it cannot be renewed the platform
says so instead of guessing.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from api.dependencies.core import SessionDep, broker_auth_service, settings_dep
from domains.broker_auth.enums import BrokerProvider, ConnectionStatus, ExpirySource
from domains.broker_auth.errors import ReauthorizationRequired
from domains.broker_auth.models import TokenGrant
from domains.broker_auth.oauth import OAuthClient, OAuthExchangeFailed, OAuthProviderConfig
from domains.broker_auth.orm import BrokerConnectionORM
from domains.broker_auth.service import BrokerAuthService
from infrastructure.security.crypto import generate_key_spec
from tests.conftest import register_and_login

PROVIDER = BrokerProvider.UPSTOX
ACCESS_TOKEN = "provider-access-token-9f2c"
REFRESH_TOKEN = "provider-refresh-token-1a7d"


class FakeOAuthClient(OAuthClient):
    """Stands in for the broker's authorization server.

    Never reaches the network: a test that can be made to hit a real broker is a
    test that will one day place a real request with a real credential.
    """

    def __init__(self, *responses: TokenGrant | Exception) -> None:
        self._responses = list(responses)
        self.exchanges: list[tuple[OAuthProviderConfig, str]] = []
        self.refreshes: list[tuple[OAuthProviderConfig, str]] = []

    def queue(self, response: TokenGrant | Exception) -> None:
        self._responses.append(response)

    def _next(self) -> TokenGrant:
        if not self._responses:
            raise AssertionError("the provider was called more times than the test expected")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async def exchange_code(self, config: OAuthProviderConfig, code: str) -> TokenGrant:
        self.exchanges.append((config, code))
        return self._next()

    async def refresh(self, config: OAuthProviderConfig, refresh_token: str) -> TokenGrant:
        self.refreshes.append((config, refresh_token))
        return self._next()


def grant(
    access: str = ACCESS_TOKEN,
    refresh: str | None = REFRESH_TOKEN,
    expires_in_seconds: int | None = 3600,
    account: str | None = "UP-4471",
) -> TokenGrant:
    expires_at = (
        datetime.now(UTC) + timedelta(seconds=expires_in_seconds)
        if expires_in_seconds is not None
        else None
    )
    return TokenGrant(
        access_token=access,
        refresh_token=refresh,
        expires_at=expires_at,
        expiry_source=(
            ExpirySource.PROVIDER_DECLARED
            if expires_in_seconds is not None
            else ExpirySource.UNDECLARED
        ),
        scopes=("market_data",),
        provider_account_id=account,
        response_fields=("access_token", "expires_in"),
    )


BROKER_ENV = {
    "QIP_UPSTOX_CLIENT_ID": "test-app-id",
    "QIP_UPSTOX_CLIENT_SECRET": "test-app-secret",
    "QIP_UPSTOX_REDIRECT_URI": "http://localhost:3000/connections/callback",
    "QIP_UPSTOX_AUTHORIZE_URL": "https://broker.test/authorize",
    "QIP_UPSTOX_TOKEN_URL": "https://broker.test/token",
}


@pytest.fixture
def broker_settings(app_environment):
    """Settings with an app registration and an encryption key configured."""
    from infrastructure.settings import get_settings, reset_settings_cache

    previous = {key: os.environ.get(key) for key in (*BROKER_ENV, "QIP_CREDENTIAL_ENCRYPTION_KEYS")}
    os.environ.update(BROKER_ENV)
    os.environ["QIP_CREDENTIAL_ENCRYPTION_KEYS"] = generate_key_spec("v1")
    reset_settings_cache()

    yield get_settings()

    for key, value in previous.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    reset_settings_cache()


@pytest.fixture
def oauth() -> FakeOAuthClient:
    return FakeOAuthClient()


@pytest.fixture
async def broker_client(broker_settings, oauth) -> AsyncIterator:
    """An API client whose OAuth exchanges are answered by ``oauth``."""
    import httpx

    from apps.api.main import create_app

    app = create_app(broker_settings)

    def _broker_auth(session: SessionDep) -> BrokerAuthService:
        return BrokerAuthService(session, broker_settings, oauth_client=oauth)

    app.dependency_overrides[broker_auth_service] = _broker_auth
    app.dependency_overrides[settings_dep] = lambda: broker_settings

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver/api/v1"
    ) as http_client:
        yield http_client


async def connect(client, header: str, oauth: FakeOAuthClient, response: TokenGrant | None = None):
    """Run the whole handoff and return the resulting connection payload."""
    oauth.queue(response if response is not None else grant())
    started = await client.post(
        f"/connections/{PROVIDER}/authorize", headers={"Authorization": header}
    )
    assert started.status_code == 200, started.text
    state = started.json()["state"]

    completed = await client.post(
        f"/connections/{PROVIDER}/callback",
        headers={"Authorization": header},
        json={"code": "provider-authorization-code", "state": state},
    )
    return started.json(), completed


class TestConnectingABroker:
    async def test_the_user_is_sent_to_the_providers_own_login(self, broker_client, oauth):
        _user, header = await register_and_login(broker_client)
        response = await broker_client.post(
            f"/connections/{PROVIDER}/authorize", headers={"Authorization": header}
        )
        assert response.status_code == 200, response.text
        body = response.json()

        assert body["authorization_url"].startswith("https://broker.test/authorize?")
        assert "client_id=test-app-id" in body["authorization_url"]
        assert f"state={body['state']}" in body["authorization_url"]
        # The password the platform holds for the broker never leaves the server.
        assert "test-app-secret" not in body["authorization_url"]

    async def test_the_code_is_exchanged_and_the_connection_becomes_usable(
        self, broker_client, oauth
    ):
        _user, header = await register_and_login(broker_client)
        _started, completed = await connect(broker_client, header, oauth)

        assert completed.status_code == 200, completed.text
        body = completed.json()
        assert body["status"] == ConnectionStatus.CONNECTED
        assert body["provider_account_id"] == "UP-4471"
        assert body["expiry_source"] == ExpirySource.PROVIDER_DECLARED
        assert body["has_refresh_token"] is True
        assert oauth.exchanges[0][1] == "provider-authorization-code"

    async def test_no_response_ever_carries_the_token(self, broker_client, oauth):
        """The credential is the one thing the API must never hand back.

        Checked against the raw body rather than named fields, so a field added
        later that happens to contain it still fails this test.
        """
        _user, header = await register_and_login(broker_client)
        _started, completed = await connect(broker_client, header, oauth)
        assert ACCESS_TOKEN not in completed.text
        assert REFRESH_TOKEN not in completed.text

        listed = await broker_client.get("/connections", headers={"Authorization": header})
        assert ACCESS_TOKEN not in listed.text
        assert REFRESH_TOKEN not in listed.text

        single = await broker_client.get(
            f"/connections/{PROVIDER}", headers={"Authorization": header}
        )
        assert ACCESS_TOKEN not in single.text

    async def test_the_token_is_not_readable_from_the_row_that_holds_it(
        self, broker_client, oauth, db_session
    ):
        _user, header = await register_and_login(broker_client)
        await connect(broker_client, header, oauth)

        row = (await db_session.execute(select(BrokerConnectionORM))).scalar_one()
        stored = b"".join(
            value
            for value in (row.access_token_ciphertext, row.refresh_token_ciphertext)
            if value is not None
        )
        assert ACCESS_TOKEN.encode() not in stored
        assert REFRESH_TOKEN.encode() not in stored
        assert row.encryption_key_id == "v1"

    async def test_a_second_account_cannot_see_the_first_ones_connection(
        self, broker_client, oauth
    ):
        _first, first_header = await register_and_login(broker_client)
        await connect(broker_client, first_header, oauth)

        _second, second_header = await register_and_login(broker_client)
        listed = await broker_client.get("/connections", headers={"Authorization": second_header})
        assert listed.json()["items"] == []

        single = await broker_client.get(
            f"/connections/{PROVIDER}", headers={"Authorization": second_header}
        )
        assert single.status_code == 404


class TestTheAuthorizationHandoffIsNotForgeable:
    async def test_a_state_can_only_be_used_once(self, broker_client, oauth):
        """An authorization code replayed with the same state must not produce a
        second exchange: the provider treats a code as single-use and so does
        this endpoint."""
        _user, header = await register_and_login(broker_client)
        started, completed = await connect(broker_client, header, oauth)
        assert completed.status_code == 200

        replay = await broker_client.post(
            f"/connections/{PROVIDER}/callback",
            headers={"Authorization": header},
            json={"code": "provider-authorization-code", "state": started["state"]},
        )
        assert replay.status_code == 400
        assert replay.json()["code"] == "AUTHORIZATION_STATE_INVALID"
        assert len(oauth.exchanges) == 1

    async def test_one_users_state_does_not_work_for_another(self, broker_client, oauth):
        _first, first_header = await register_and_login(broker_client)
        started = await broker_client.post(
            f"/connections/{PROVIDER}/authorize", headers={"Authorization": first_header}
        )

        _second, second_header = await register_and_login(broker_client)
        stolen = await broker_client.post(
            f"/connections/{PROVIDER}/callback",
            headers={"Authorization": second_header},
            json={"code": "code", "state": started.json()["state"]},
        )
        assert stolen.status_code == 400
        assert stolen.json()["code"] == "AUTHORIZATION_STATE_INVALID"
        assert oauth.exchanges == []

    async def test_an_invented_state_is_refused(self, broker_client, oauth):
        _user, header = await register_and_login(broker_client)
        await broker_client.post(
            f"/connections/{PROVIDER}/authorize", headers={"Authorization": header}
        )
        response = await broker_client.post(
            f"/connections/{PROVIDER}/callback",
            headers={"Authorization": header},
            json={"code": "code", "state": "not-a-real-state"},
        )
        assert response.status_code == 400
        assert oauth.exchanges == []

    async def test_an_api_access_token_is_not_accepted_as_a_state(self, broker_client, oauth):
        """Both are signed with the same key; only the purpose claim separates
        them, so that separation is what is tested."""
        _user, header = await register_and_login(broker_client)
        await broker_client.post(
            f"/connections/{PROVIDER}/authorize", headers={"Authorization": header}
        )
        response = await broker_client.post(
            f"/connections/{PROVIDER}/callback",
            headers={"Authorization": header},
            json={"code": "code", "state": header.removeprefix("Bearer ")},
        )
        assert response.status_code == 400
        assert oauth.exchanges == []

    async def test_the_callback_requires_a_signed_in_caller(self, broker_client, oauth):
        response = await broker_client.post(
            f"/connections/{PROVIDER}/callback", json={"code": "code", "state": "state"}
        )
        assert response.status_code == 401


class TestWhenTheProviderOrTheDeploymentSaysNo:
    async def test_a_refused_code_is_reported_and_the_connection_marked(self, broker_client, oauth):
        _user, header = await register_and_login(broker_client)
        oauth.queue(OAuthExchangeFailed(PROVIDER, 400, "invalid_grant"))
        started = await broker_client.post(
            f"/connections/{PROVIDER}/authorize", headers={"Authorization": header}
        )
        response = await broker_client.post(
            f"/connections/{PROVIDER}/callback",
            headers={"Authorization": header},
            json={"code": "stale-code", "state": started.json()["state"]},
        )

        assert response.status_code == 422
        body = response.json()
        assert body["code"] == "PROVIDER_REJECTED_AUTHORIZATION"
        # The provider's own error identifier survives: invalid_grant and
        # invalid_client need different things done about them.
        assert body["provider_error"] == "invalid_grant"

        current = await broker_client.get(
            f"/connections/{PROVIDER}", headers={"Authorization": header}
        )
        assert current.json()["status"] == ConnectionStatus.NEEDS_REAUTHORIZATION
        assert "invalid_grant" in current.json()["last_error"]

    async def test_a_failed_reconnect_does_not_break_a_working_connection(
        self, db_session, broker_settings, oauth, account
    ):
        """Reconnecting is a normal thing to do — to widen permissions, or after
        changing something at the broker. If that attempt fails, the credential
        already held is untouched and still works: a fumbled reconnect must not
        take out a connection that was fine."""
        service = BrokerAuthService(db_session, broker_settings, oauth_client=oauth)
        await connect_directly(service, account, oauth, grant(expires_in_seconds=3600))

        oauth.queue(OAuthExchangeFailed(PROVIDER, 400, "invalid_grant"))
        handoff = await service.start_authorization(account, PROVIDER)
        with pytest.raises(OAuthExchangeFailed):
            await service.complete_authorization(account, PROVIDER, "bad-code", handoff.state)

        connection = await service.get_connection(account, PROVIDER)
        assert connection.status is ConnectionStatus.CONNECTED
        assert "invalid_grant" in connection.last_error
        assert (await service.access_token(account, PROVIDER)).token == ACCESS_TOKEN

    async def test_an_unregistered_provider_names_what_is_missing(self, app_environment, oauth):
        """The operator is told which settings to fill in, not that something
        went wrong."""
        import httpx

        from apps.api.main import create_app

        app = create_app(app_environment["settings"])
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver/api/v1"
        ) as client:
            _user, header = await register_and_login(client)
            response = await client.post(
                f"/connections/{PROVIDER}/authorize", headers={"Authorization": header}
            )
            assert response.status_code == 422
            body = response.json()
            assert body["code"] == "PROVIDER_NOT_CONFIGURED"
            assert "QIP_UPSTOX_CLIENT_ID" in body["missing_settings"]

    async def test_without_an_encryption_key_the_flow_stops_before_the_broker(
        self, app_environment, oauth
    ):
        """Refusing up front beats walking a user through a broker login and
        then having nowhere safe to put the result."""
        import httpx

        from apps.api.main import create_app
        from infrastructure.settings import get_settings, reset_settings_cache

        previous = os.environ.get("QIP_CREDENTIAL_ENCRYPTION_KEYS")
        os.environ.update(BROKER_ENV)
        os.environ["QIP_CREDENTIAL_ENCRYPTION_KEYS"] = ""
        reset_settings_cache()
        try:
            app = create_app(get_settings())
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver/api/v1"
            ) as client:
                _user, header = await register_and_login(client)
                response = await client.post(
                    f"/connections/{PROVIDER}/authorize", headers={"Authorization": header}
                )
                assert response.status_code == 422
                assert response.json()["code"] == "CREDENTIAL_STORAGE_UNAVAILABLE"

                providers = await client.get(
                    "/connections/providers", headers={"Authorization": header}
                )
                assert providers.json()["credential_storage_ready"] is False
        finally:
            for key in BROKER_ENV:
                os.environ.pop(key, None)
            if previous is None:
                os.environ.pop("QIP_CREDENTIAL_ENCRYPTION_KEYS", None)
            else:
                os.environ["QIP_CREDENTIAL_ENCRYPTION_KEYS"] = previous
            reset_settings_cache()

    async def test_the_providers_list_says_what_is_ready(self, broker_client, oauth):
        _user, header = await register_and_login(broker_client)
        response = await broker_client.get(
            "/connections/providers", headers={"Authorization": header}
        )
        body = response.json()
        assert body["credential_storage_ready"] is True
        upstox = next(item for item in body["items"] if item["provider"] == PROVIDER)
        assert upstox["configured"] is True
        assert upstox["missing_settings"] == []


class TestDisconnecting:
    async def test_the_stored_credential_is_erased(self, broker_client, oauth, db_session):
        _user, header = await register_and_login(broker_client)
        await connect(broker_client, header, oauth)

        response = await broker_client.delete(
            f"/connections/{PROVIDER}", headers={"Authorization": header}
        )
        assert response.status_code == 200
        assert response.json()["status"] == ConnectionStatus.REVOKED
        assert response.json()["has_refresh_token"] is False

        row = (await db_session.execute(select(BrokerConnectionORM))).scalar_one()
        assert row.access_token_ciphertext is None
        assert row.refresh_token_ciphertext is None

    async def test_the_record_that_a_connection_existed_survives(
        self, broker_client, oauth, db_session
    ):
        """The secret goes; the history does not. Which accounts were ever
        connected to which broker is exactly the question an incident asks."""
        _user, header = await register_and_login(broker_client)
        await connect(broker_client, header, oauth)
        await broker_client.delete(f"/connections/{PROVIDER}", headers={"Authorization": header})

        rows = (await db_session.execute(select(BrokerConnectionORM))).scalars().all()
        assert len(rows) == 1
        assert rows[0].provider_account_id == "UP-4471"

    async def test_disconnecting_something_never_connected_is_a_404(self, broker_client, oauth):
        _user, header = await register_and_login(broker_client)
        response = await broker_client.delete(
            f"/connections/{PROVIDER}", headers={"Authorization": header}
        )
        assert response.status_code == 404


@pytest.fixture
async def account(db_session) -> uuid.UUID:
    from domains.users.orm import UserORM

    user = UserORM(
        email=f"vault-{uuid.uuid4().hex[:8]}@example.com",
        password_hash="not-used-by-these-tests",
    )
    db_session.add(user)
    await db_session.flush()
    return user.id


async def connect_directly(
    service: BrokerAuthService, user_id: uuid.UUID, oauth: FakeOAuthClient, response: TokenGrant
) -> None:
    """The handoff, without going through HTTP, so renewal can be exercised."""
    oauth.queue(response)
    handoff = await service.start_authorization(user_id, PROVIDER)
    await service.complete_authorization(user_id, PROVIDER, "code", handoff.state)


class TestKeepingACredentialAlive:
    """What removes the daily paste: the platform renews what it can renew, and
    says plainly when it cannot, without ever inventing a lifetime."""

    async def test_an_expiring_credential_is_renewed_without_the_user(
        self, db_session, broker_settings, oauth, account
    ):
        service = BrokerAuthService(db_session, broker_settings, oauth_client=oauth)
        await connect_directly(service, account, oauth, grant(expires_in_seconds=30))

        oauth.queue(grant(access="renewed-token", refresh="renewed-refresh"))
        # 30 seconds left is inside the 120-second refresh skew.
        token = await service.access_token(account, PROVIDER)

        assert token.token == "renewed-token"
        assert oauth.refreshes[0][1] == REFRESH_TOKEN

        connection = await service.get_connection(account, PROVIDER)
        assert connection.status is ConnectionStatus.CONNECTED
        assert connection.last_refreshed_at is not None

    async def test_a_renewal_that_returns_no_new_refresh_token_keeps_the_old_one(
        self, db_session, broker_settings, oauth, account
    ):
        """RFC 6749 section 6 makes a new refresh token optional. Discarding the
        existing one when the provider omits it turns a connection that renews
        indefinitely into one that dies at the first renewal."""
        service = BrokerAuthService(db_session, broker_settings, oauth_client=oauth)
        await connect_directly(service, account, oauth, grant(expires_in_seconds=30))

        oauth.queue(grant(access="renewed-1", refresh=None, expires_in_seconds=30))
        await service.access_token(account, PROVIDER)

        oauth.queue(grant(access="renewed-2", refresh=None, expires_in_seconds=3600))
        second = await service.access_token(account, PROVIDER)

        assert second.token == "renewed-2"
        # Both renewals presented the refresh token issued at connect time.
        assert [call[1] for call in oauth.refreshes] == [REFRESH_TOKEN, REFRESH_TOKEN]

    async def test_a_declared_expiry_with_no_refresh_token_asks_the_user_back(
        self, db_session, broker_settings, oauth, account
    ):
        service = BrokerAuthService(db_session, broker_settings, oauth_client=oauth)
        await connect_directly(service, account, oauth, grant(refresh=None, expires_in_seconds=30))

        with pytest.raises(ReauthorizationRequired, match="no refresh token"):
            await service.access_token(account, PROVIDER)

        connection = await service.get_connection(account, PROVIDER)
        assert connection.status is ConnectionStatus.NEEDS_REAUTHORIZATION
        assert oauth.refreshes == []

    async def test_an_undeclared_expiry_is_not_treated_as_an_expiry(
        self, db_session, broker_settings, oauth, account
    ):
        """A provider that states no lifetime must not have one assumed for it,
        in either direction: the credential is used, not pre-emptively retired
        and not renewed on a schedule nobody published."""
        service = BrokerAuthService(db_session, broker_settings, oauth_client=oauth)
        await connect_directly(
            service, account, oauth, grant(refresh=None, expires_in_seconds=None)
        )

        token = await service.access_token(account, PROVIDER)
        assert token.token == ACCESS_TOKEN
        assert token.expires_at is None
        assert token.expiry_source is ExpirySource.UNDECLARED
        assert oauth.refreshes == []

    async def test_a_rejection_retires_a_credential_that_declared_no_expiry(
        self, db_session, broker_settings, oauth, account
    ):
        """The other half of the same rule. With no declared lifetime the only
        authority on whether the token still works is the provider, so the
        adapter that got the refusal is what marks it."""
        service = BrokerAuthService(db_session, broker_settings, oauth_client=oauth)
        await connect_directly(
            service, account, oauth, grant(refresh=None, expires_in_seconds=None)
        )

        await service.report_rejection(account, PROVIDER, "provider returned 401 UDAPI100050")

        connection = await service.get_connection(account, PROVIDER)
        assert connection.status is ConnectionStatus.NEEDS_REAUTHORIZATION
        assert "UDAPI100050" in connection.last_error
        with pytest.raises(ReauthorizationRequired):
            await service.access_token(account, PROVIDER)

    async def test_a_failed_renewal_leaves_a_connection_that_says_what_happened(
        self, db_session, broker_settings, oauth, account
    ):
        service = BrokerAuthService(db_session, broker_settings, oauth_client=oauth)
        await connect_directly(service, account, oauth, grant(expires_in_seconds=30))

        oauth.queue(OAuthExchangeFailed(PROVIDER, 400, "invalid_grant"))
        with pytest.raises(ReauthorizationRequired):
            await service.access_token(account, PROVIDER)

        connection = await service.get_connection(account, PROVIDER)
        assert connection.status is ConnectionStatus.NEEDS_REAUTHORIZATION
        assert "invalid_grant" in connection.last_error

    async def test_a_disconnected_connection_hands_out_nothing(
        self, db_session, broker_settings, oauth, account
    ):
        service = BrokerAuthService(db_session, broker_settings, oauth_client=oauth)
        await connect_directly(service, account, oauth, grant())
        await service.revoke(account, PROVIDER)

        with pytest.raises(ReauthorizationRequired, match="disconnected"):
            await service.access_token(account, PROVIDER)

    async def test_a_credential_sealed_under_a_retired_key_is_still_usable(
        self, db_session, broker_settings, oauth, account
    ):
        """Rotating the encryption key must not disconnect every user."""
        service = BrokerAuthService(db_session, broker_settings, oauth_client=oauth)
        await connect_directly(service, account, oauth, grant(expires_in_seconds=3600))

        rotated = broker_settings.model_copy(
            update={
                "credential_encryption_keys": (
                    f"{generate_key_spec('v2')},{broker_settings.credential_encryption_keys}"
                )
            }
        )
        after_rotation = BrokerAuthService(db_session, rotated, oauth_client=oauth)
        token = await after_rotation.access_token(account, PROVIDER)
        assert token.token == ACCESS_TOKEN


class TestTheAuditTrail:
    async def test_every_step_of_a_connections_life_is_recorded(
        self, db_session, broker_settings, oauth, account
    ):
        """A stored broker token is the most sensitive thing the platform holds,
        so who connected it, when it was renewed and when it stopped working must
        all be reconstructable afterwards."""
        from domains.users.orm import AuditLogORM

        service = BrokerAuthService(db_session, broker_settings, oauth_client=oauth)
        await connect_directly(service, account, oauth, grant(expires_in_seconds=30))
        oauth.queue(grant(access="renewed"))
        await service.access_token(account, PROVIDER)
        await service.revoke(account, PROVIDER)

        rows = (
            (await db_session.execute(select(AuditLogORM).order_by(AuditLogORM.created_at)))
            .scalars()
            .all()
        )
        actions = [row.action for row in rows]
        assert "BROKER_AUTHORIZATION_STARTED" in actions
        assert "BROKER_CONNECTION_AUTHORIZED" in actions
        assert "BROKER_CONNECTION_REFRESHED" in actions
        assert "BROKER_CONNECTION_REVOKED" in actions

    async def test_no_audit_entry_carries_the_credential(
        self, db_session, broker_settings, oauth, account
    ):
        from domains.users.orm import AuditLogORM

        service = BrokerAuthService(db_session, broker_settings, oauth_client=oauth)
        await connect_directly(service, account, oauth, grant())

        rows = (await db_session.execute(select(AuditLogORM))).scalars().all()
        written = str([row.audit_metadata for row in rows])
        assert ACCESS_TOKEN not in written
        assert REFRESH_TOKEN not in written


class TestRotationAndRenewalTogether:
    async def test_renewing_a_credential_after_a_key_rotation_keeps_it_alive(
        self, db_session, broker_settings, oauth, account
    ):
        """The two mechanisms meet in one place and must not break each other.

        After a rotation the row's tokens are still sealed under the retired key.
        A renewal writes the access token under the *new* key and, when the
        provider returns no new refresh token, has to carry the existing one
        across — reading it under the key it was actually written with, not the
        one just recorded on the row. Getting that order wrong disconnects every
        user at their first renewal after a rotation, and only then.
        """
        service = BrokerAuthService(db_session, broker_settings, oauth_client=oauth)
        await connect_directly(service, account, oauth, grant(expires_in_seconds=30))

        rotated = broker_settings.model_copy(
            update={
                "credential_encryption_keys": (
                    f"{generate_key_spec('v2')},{broker_settings.credential_encryption_keys}"
                )
            }
        )
        after_rotation = BrokerAuthService(db_session, rotated, oauth_client=oauth)

        oauth.queue(grant(access="renewed-after-rotation", refresh=None, expires_in_seconds=30))
        first = await after_rotation.access_token(account, PROVIDER)
        assert first.token == "renewed-after-rotation"

        # The carried-over refresh token must still open on the next renewal.
        oauth.queue(grant(access="renewed-again", refresh=None, expires_in_seconds=3600))
        second = await after_rotation.access_token(account, PROVIDER)
        assert second.token == "renewed-again"
        assert [call[1] for call in oauth.refreshes] == [REFRESH_TOKEN, REFRESH_TOKEN]

    async def test_using_a_credential_after_a_rotation_migrates_the_row(
        self, db_session, broker_settings, oauth, account
    ):
        """A rotation has to be able to finish. Rows re-seal under the active key
        as they are used, so the retired key eventually stops being referenced
        and can be removed — without a bulk re-encryption step that has to
        succeed all at once."""
        service = BrokerAuthService(db_session, broker_settings, oauth_client=oauth)
        await connect_directly(service, account, oauth, grant(expires_in_seconds=3600))

        rotated = broker_settings.model_copy(
            update={
                "credential_encryption_keys": (
                    f"{generate_key_spec('v2')},{broker_settings.credential_encryption_keys}"
                )
            }
        )
        after_rotation = BrokerAuthService(db_session, rotated, oauth_client=oauth)
        assert (await after_rotation.access_token(account, PROVIDER)).token == ACCESS_TOKEN

        row = (await db_session.execute(select(BrokerConnectionORM))).scalar_one()
        assert row.encryption_key_id == "v2"

        # Both halves must have moved: leaving the refresh token under v1 would
        # strand it the moment v1 is removed.
        only_v2 = BrokerAuthService(
            db_session,
            broker_settings.model_copy(
                update={
                    "credential_encryption_keys": rotated.credential_encryption_keys.split(",")[0]
                }
            ),
            oauth_client=oauth,
        )
        oauth.queue(grant(access="renewed-under-v2", refresh=None, expires_in_seconds=3600))
        row.expires_at = datetime.now(UTC) + timedelta(seconds=10)
        await db_session.flush()
        assert (await only_v2.access_token(account, PROVIDER)).token == "renewed-under-v2"
        assert oauth.refreshes[-1][1] == REFRESH_TOKEN
