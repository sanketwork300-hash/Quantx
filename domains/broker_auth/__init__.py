"""Broker and market-data provider credentials.

The problem this package exists to remove: an access token pasted into an
environment variable and a process restarted every time the broker expires it.
That arrangement makes the credential a deployment artefact, shares one identity
across every user of the installation, leaves the token in shell history and
process listings, and gives the platform no way to notice that it has gone stale
other than a failed request.

Here, a credential is a per-user record. It is obtained through the provider's
own authorization flow, encrypted at rest, refreshed without a human when the
provider issues a refresh token, and marked as needing re-authorization when it
does not. Only the app registration — client id, client secret, redirect URI —
stays in configuration, because that genuinely is per-deployment and long-lived.
"""
