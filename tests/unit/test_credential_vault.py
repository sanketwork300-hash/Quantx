"""The two pure pieces of the credential vault: sealing, and reading a grant.

Both exist to make one behaviour testable in isolation: the platform stores and
reports what the provider actually said, and nothing else.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import pytest

from domains.broker_auth.enums import BrokerProvider, ExpirySource
from domains.broker_auth.oauth import (
    OAuthError,
    OAuthProviderConfig,
    parse_token_response,
)
from infrastructure.security.crypto import (
    DecryptionFailed,
    KeyUnavailable,
    SecretBox,
    generate_key_spec,
    parse_keys,
)

PROVIDER = BrokerProvider.UPSTOX
NOW = datetime(2026, 9, 9, 9, 30, tzinfo=UTC)


class TestSealingACredential:
    def test_a_sealed_secret_comes_back_unchanged(self):
        box = SecretBox.from_spec(generate_key_spec())
        sealed = box.encrypt("live-token", context="user-a|upstox|access")
        assert box.decrypt(sealed, context="user-a|upstox|access") == "live-token"

    def test_the_ciphertext_does_not_contain_the_secret(self):
        box = SecretBox.from_spec(generate_key_spec())
        sealed = box.encrypt("live-token", context="user-a|upstox|access")
        assert b"live-token" not in sealed.payload

    def test_a_ciphertext_moved_to_another_row_does_not_open(self):
        """The whole point of binding context: a stolen row is not a credential.

        Copying user A's ciphertext into user B's row must fail loudly rather
        than hand B a working broker token.
        """
        box = SecretBox.from_spec(generate_key_spec())
        sealed = box.encrypt("live-token", context="user-a|upstox|access")
        with pytest.raises(DecryptionFailed):
            box.decrypt(sealed, context="user-b|upstox|access")

    def test_the_same_secret_seals_differently_every_time(self):
        box = SecretBox.from_spec(generate_key_spec())
        first = box.encrypt("live-token", context="c")
        second = box.encrypt("live-token", context="c")
        assert first.payload != second.payload
        assert first.nonce != second.nonce

    def test_a_retired_key_still_reads_the_rows_it_wrote(self):
        """Rotation must not require decrypting every row at the moment of it."""
        old = generate_key_spec("v1")
        old_box = SecretBox.from_spec(old)
        sealed = old_box.encrypt("live-token", context="c")

        rotated = SecretBox.from_spec(f"{generate_key_spec('v2')},{old}")
        assert rotated.active_key_id == "v2"
        assert rotated.decrypt(sealed, context="c") == "live-token"
        assert rotated.needs_rewrap(sealed) is True

    def test_dropping_the_key_a_row_was_written_with_is_reported_plainly(self):
        old_box = SecretBox.from_spec(generate_key_spec("v1"))
        sealed = old_box.encrypt("live-token", context="c")

        without_v1 = SecretBox.from_spec(generate_key_spec("v2"))
        with pytest.raises(DecryptionFailed, match="no longer"):
            without_v1.decrypt(sealed, context="c")

    def test_a_short_key_is_refused_rather_than_padded(self):
        short = base64.b64encode(b"too-short").decode()
        with pytest.raises(KeyUnavailable, match="AES-256"):
            parse_keys(f"v1:{short}")

    def test_the_first_key_is_the_one_new_rows_are_written_with(self):
        keys, active = parse_keys(f"{generate_key_spec('a')},{generate_key_spec('b')}")
        assert active == "a"
        assert set(keys) == {"a", "b"}


class TestReadingAProvidersTokenResponse:
    def test_a_declared_lifetime_is_recorded_as_declared(self):
        grant = parse_token_response({"access_token": "t", "expires_in": 3600}, PROVIDER, now=NOW)
        assert grant.expiry_source is ExpirySource.PROVIDER_DECLARED
        assert grant.expires_at == NOW + timedelta(seconds=3600)

    def test_a_response_with_no_lifetime_is_not_given_one(self):
        """The failure this pins: inventing "tokens last a day" for a provider
        that never said so, and then either dropping a good credential or
        reporting a dead one as live."""
        grant = parse_token_response({"access_token": "t"}, PROVIDER, now=NOW)
        assert grant.expiry_source is ExpirySource.UNDECLARED
        assert grant.expires_at is None

    def test_a_nonsense_lifetime_is_treated_as_no_lifetime(self):
        for value in ("soon", 0, -5, None):
            grant = parse_token_response(
                {"access_token": "t", "expires_in": value}, PROVIDER, now=NOW
            )
            assert grant.expires_at is None
            assert grant.expiry_source is ExpirySource.UNDECLARED

    def test_a_missing_access_token_is_an_error_not_an_empty_credential(self):
        with pytest.raises(OAuthError, match="no access_token"):
            parse_token_response({"expires_in": 3600}, PROVIDER, now=NOW)

    def test_the_account_the_grant_belongs_to_is_read_when_offered(self):
        grant = parse_token_response({"access_token": "t", "user_id": "UP12345"}, PROVIDER, now=NOW)
        assert grant.provider_account_id == "UP12345"

    def test_no_account_id_is_left_empty_rather_than_filled_in(self):
        grant = parse_token_response({"access_token": "t"}, PROVIDER, now=NOW)
        assert grant.provider_account_id is None

    def test_the_field_names_the_provider_sent_are_recorded_but_not_the_values(self):
        """Enough to notice a provider silently dropping ``refresh_token``,
        without writing any secret into the audit log."""
        grant = parse_token_response(
            {"access_token": "secret-value", "refresh_token": "also-secret"}, PROVIDER, now=NOW
        )
        assert grant.response_fields == ("access_token", "refresh_token")
        assert "secret-value" not in str(grant.response_fields)

    def test_scopes_are_split_on_whitespace_per_rfc_6749(self):
        grant = parse_token_response(
            {"access_token": "t", "scope": "market_data orders"}, PROVIDER, now=NOW
        )
        assert grant.scopes == ("market_data", "orders")


class TestTheAuthorizationUrl:
    def _config(self, authorize_url: str = "https://broker.example/authorize"):
        return OAuthProviderConfig(
            provider=PROVIDER,
            client_id="app-1",
            client_secret="shhh",
            redirect_uri="https://app.example/connections/callback",
            authorize_url=authorize_url,
            token_url="https://broker.example/token",
        )

    def test_it_carries_the_state_and_the_registered_redirect(self):
        url = self._config().authorization_url("state-abc")
        assert "response_type=code" in url
        assert "client_id=app-1" in url
        assert "state=state-abc" in url
        assert "connections%2Fcallback" in url

    def test_it_never_carries_the_client_secret(self):
        assert "shhh" not in self._config().authorization_url("state-abc")

    def test_a_url_that_already_has_a_query_string_is_appended_to(self):
        url = self._config("https://broker.example/authorize?v=2").authorization_url("s")
        assert "?v=2&" in url
