from __future__ import annotations

from domains.broker_auth.enums import BrokerProvider


class BrokerAuthError(Exception):
    """Base class for credential-management failures."""


class ProviderNotConfigured(BrokerAuthError):
    """The deployment has no app registration for this provider."""

    def __init__(self, provider: BrokerProvider, missing: tuple[str, ...]) -> None:
        super().__init__(
            f"provider {provider} is not configured; missing settings: {', '.join(missing)}"
        )
        self.provider = provider
        self.missing = missing


class CredentialEncryptionUnavailable(BrokerAuthError):
    """No encryption key, so no credential may be stored.

    Deliberately fatal rather than degrading to plaintext storage: a broker
    token in the clear is worse than a broker connection that cannot be made.
    """


class ConnectionNotFound(BrokerAuthError):
    def __init__(self, provider: BrokerProvider) -> None:
        super().__init__(f"no {provider} connection for this account")
        self.provider = provider


class ReauthorizationRequired(BrokerAuthError):
    """A credential exists but cannot be used, and cannot be renewed silently."""

    def __init__(self, provider: BrokerProvider, reason: str) -> None:
        super().__init__(f"{provider} credential needs re-authorization: {reason}")
        self.provider = provider
        self.reason = reason


class InvalidAuthorizationState(BrokerAuthError):
    """The ``state`` returned with the authorization code did not check out.

    Covers a forged, expired, replayed or cross-account state. The message is
    intentionally uniform so that a caller probing the endpoint cannot learn
    which of those it hit.
    """

    def __init__(self, detail: str = "authorization state is not valid") -> None:
        super().__init__(detail)
