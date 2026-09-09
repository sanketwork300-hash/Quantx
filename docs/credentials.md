# Broker and provider credentials

## 1. The arrangement this replaces

The usual way to give a platform access to a broker's API is to log in by hand,
copy the access token into an environment variable, and restart the process.
Every provider that expires tokens then turns that into a recurring chore, and
the chore has to be done by whoever holds the deployment credentials, at the
time of day the provider chose.

The cost is not only the inconvenience:

| Problem | Consequence |
| --- | --- |
| One token for the whole installation | Every user of the deployment trades and reads data as one broker identity. Per-user attribution is impossible. |
| The token is in the environment | It is in the shell history, in `ps` output, in the container inspect output, and in any crash dump that captures the environment. |
| Renewal is manual | Between expiry and the next paste, every market-data call fails. Nobody finds out until something breaks. |
| Rotation is a redeploy | Revoking a compromised token means editing configuration and restarting, not clicking a button. |
| No record | Nothing says who connected what, when it was renewed, or when it stopped working. |

## 2. What is here instead

A credential is a row: one per user, per provider. It is obtained through the
provider's own authorization flow, sealed before it is stored, renewed on its
own where the provider allows it, and marked as needing the user's attention
where it does not.

```
User clicks Connect
      │
      ▼
POST /connections/{provider}/authorize   ── mints a signed, single-use state
      │
      ▼
Provider's own login page                ── the user's broker password is
      │                                     entered at the broker, never here
      ▼
Redirect to the frontend callback with ?code&state
      │
      ▼
POST /connections/{provider}/callback    ── code exchanged server-side
      │
      ▼
Token sealed with AES-256-GCM and stored ── the browser never sees it
      │
      ▼
BrokerAuthService.access_token(user, provider)
      │                                     the seam a provider adapter uses,
      ▼                                     in place of reading the environment
Renewed if it is about to expire, or the caller is told to ask the user again
```

Configuration keeps only what is genuinely long-lived and per-deployment: the
app registration (`client_id`, `client_secret`, `redirect_uri`), the provider's
endpoints, and the encryption key. None of those rotate daily.

## 3. Storage

`broker_connections` holds ciphertext and nothing else. There is no column on
that table capable of holding a token in the clear, so no code path can write
one there by accident.

Sealing uses AES-256-GCM. Two properties are load-bearing:

**The key id travels with the ciphertext.** Rotation means putting a new key
first in `QIP_CREDENTIAL_ENCRYPTION_KEYS` and leaving the old one after it. New
rows are sealed with the new key, and an existing row re-seals under it the next
time its credential is used — both halves together, so the refresh token is not
left stranded under a key that is about to be removed. There is no flag day and
no bulk re-encryption step that has to succeed atomically.

**The associated data names the row.** Each ciphertext is bound to
`broker_connection|{user_id}|{provider}|{field}`. A ciphertext lifted out of one
user's row and pasted into another's fails authentication rather than decrypting
into a credential the second user was never granted.

If no key is configured, the platform refuses to store credentials at all —
`CREDENTIAL_STORAGE_UNAVAILABLE`, raised before the user is sent to the broker.
Walking someone through a broker login and then discovering there is nowhere
safe to put the result is a worse failure than declining up front, and storing
the token in the clear is worse than either.

## 4. Expiry is not assumed

This is the rule that most shapes the code. Providers differ: some return
`expires_in` with the grant, some publish a fixed daily cutoff, some say
nothing at all. The platform records only what the provider actually sent.

* `expiry_source = PROVIDER_DECLARED` — `expires_at` is set. The credential is
  refreshed `QIP_CREDENTIAL_REFRESH_SKEW_SECONDS` before it, so it cannot lapse
  between the check and the call that uses it.
* `expiry_source = UNDECLARED` — `expires_at` is `null`. The credential is used
  until the provider refuses it, at which point the adapter that got the
  refusal calls `report_rejection` and the connection moves to
  `NEEDS_REAUTHORIZATION`.

A guessed lifetime fails in both directions: too short retires working
credentials and interrupts users for no reason; too long reports a dead
credential as live and turns every market-data call into a silent gap. Neither
failure announces itself, which is exactly why the guess is not made.

The endpoint URLs are configuration rather than constants for the same reason.
The defaults in `.env.example` are the published Upstox v2 endpoints, and they
carry an instruction to verify them against the provider's current documentation
before deploying — a provider changing them should be a deployment change, not a
code change and not a surprise.

## 5. The handoff cannot be forged

`state` is a JWT signed with the application secret, carrying the user id, the
provider and a nonce, and living for `QIP_OAUTH_STATE_TTL_MINUTES`. The nonce is
also written to the connection row and cleared the moment a callback consumes
it, which makes the state single-use: a replayed redirect finds the nonce gone.

Four things are checked, and all four failures return the same
`AUTHORIZATION_STATE_INVALID` so that probing the endpoint reveals nothing:

1. the signature and expiry;
2. that the token was minted as a state and not as an API access token — both
   are signed with the same key, and only the `typ` claim separates them;
3. that the subject is the caller;
4. that the nonce matches the one the row is waiting for.

The callback also requires the caller to be signed in, so an authorization code
alone is not enough to attach a broker to an account.

## 5a. Reconnecting

Connecting again over an existing connection is normal — to widen permissions,
or after something changed at the broker. If that attempt fails, the credential
already held is left exactly as it was and the connection stays `CONNECTED`; the
failure is recorded in `last_error` and in the audit log. A refused reconnect
says nothing about a credential that is still working, and taking one out
because of the other would make reconnecting a risk rather than a repair.

## 6. Disconnecting

`DELETE /connections/{provider}` erases the ciphertext and sets the status to
`REVOKED`. The row survives, because which accounts were ever connected to which
broker is precisely the question an incident asks.

The audit entry says what actually happened — the local copy was destroyed.
It does not claim the token was revoked at the provider, because only the
provider can do that, and asserting it would be exactly the kind of invented API
behaviour the rest of the platform avoids.

## 7. What a provider adapter does

An adapter never reads the environment for a token, and never holds one across
calls:

```python
credential = await broker_auth.access_token(user_id, BrokerProvider.UPSTOX)
response = await call_the_provider(credential.token)
if response.status_code == 401:
    await broker_auth.report_rejection(user_id, BrokerProvider.UPSTOX, response.text[:200])
```

`access_token` renews the credential if it is about to expire and raises
`ReauthorizationRequired` when it cannot — with the reason, so the user can be
told why they are being asked to sign in again rather than just that they are.

## 8. Operating it

```bash
python scripts/generate_credential_key.py        # first key
python scripts/generate_credential_key.py v2     # a rotation key
```

To rotate, put the new entry first and keep the old one:

```
QIP_CREDENTIAL_ENCRYPTION_KEYS=v2:<new>,v1:<old>
```

Rows migrate to `v2` as they are used, so the population referencing `v1`
drains on its own. Remove `v1` only once no row still names it — the query is
`SELECT count(*) FROM broker_connections WHERE encryption_key_id = 'v1'`.
Dropping a key that rows still reference makes those credentials unrecoverable
and every affected user has to reconnect; `DecryptionFailed` says exactly that
rather than presenting itself as a corrupt row.
