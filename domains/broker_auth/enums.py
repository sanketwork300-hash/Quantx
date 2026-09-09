from __future__ import annotations

from enum import StrEnum


class BrokerProvider(StrEnum):
    """Providers whose credentials the platform can hold.

    A member exists once the OAuth configuration for that provider is
    understood, independently of whether a market-data or broker adapter has
    been written against it.
    """

    UPSTOX = "upstox"


class ConnectionStatus(StrEnum):
    #: Holding a credential believed to be usable.
    CONNECTED = "CONNECTED"
    #: The credential is gone or was rejected; the user must authorize again.
    #: Reached either because a declared expiry passed with no refresh token,
    #: or because the provider rejected the token in use.
    NEEDS_REAUTHORIZATION = "NEEDS_REAUTHORIZATION"
    #: The user disconnected. The secrets are erased; the row remains so the
    #: history of the connection is not silently lost.
    REVOKED = "REVOKED"


class ExpirySource(StrEnum):
    """Where a credential's expiry came from — never from an assumption.

    Brokers differ: some return ``expires_in`` with the grant, some document a
    fixed daily cutoff, some say nothing. The platform records only what the
    provider actually stated. ``UNDECLARED`` means the response carried no
    expiry, so the credential is used until the provider rejects it rather than
    retired on a guessed schedule.
    """

    PROVIDER_DECLARED = "PROVIDER_DECLARED"
    UNDECLARED = "UNDECLARED"
