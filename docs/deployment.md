# Deployment

MVP target: a single host running Docker Compose. No Kubernetes.

---

## 1. Services

| Service | Image / build | Purpose |
| --- | --- | --- |
| `proxy` | Caddy | TLS termination, routing `/api` -> api, `/` -> web |
| `api` | `docker/api.Dockerfile` | FastAPI via uvicorn |
| `worker` | same image, different command | Celery worker |
| `scheduler` | same image, different command | Celery beat |
| `web` | `docker/web.Dockerfile` | Next.js |
| `postgres` | `timescale/timescaledb:latest-pg16` | app state + time series |
| `redis` | `redis:7-alpine` | cache, broker, rate limits, locks |
| `minio` | `minio/minio` | S3-compatible object store for local/self-host |

API, worker and scheduler share one image. They run the same code with different
entrypoints, so a task cannot drift from the service that enqueues it.

## 2. First run

```bash
cp .env.example .env
# generate a real secret
python -c "import secrets; print(secrets.token_hex(32))"   # -> QIP_SECRET_KEY

# Bake the commit into the image so provenance can name the code that produced
# a number. The .git directory is deliberately not copied into the image.
export GIT_COMMIT=$(git rev-parse --short HEAD)

docker compose up -d --build
```

The `migrate` service runs `alembic upgrade head` and the API, worker and
scheduler wait for it to complete successfully before starting.

Then:

- API docs: `http://localhost:8000/docs`
- Web: `http://localhost:3000`
- MinIO console: `http://localhost:9001`

Without Docker (development):

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev,validation]"
export QIP_DATABASE_URL=sqlite+aiosqlite:///./qip.db
export QIP_JOB_EXECUTION_MODE=eager
export QIP_OBJECT_STORE_BACKEND=local
.venv/bin/uvicorn apps.api.main:app --reload
```

`eager` mode runs job functions inline, so the full upload -> ingest -> retrieve
path works with no Redis and no worker. The code path is identical to production;
only the dispatcher differs.

## 3. Migrations

Alembic, autogenerate reviewed by hand — never applied unreviewed, because
autogenerate does not understand hypertables, partial indexes or data migrations.

```bash
alembic revision --autogenerate -m "add option_quotes"
alembic upgrade head
alembic downgrade -1
```

CI runs `upgrade head` then `downgrade base` on a scratch database, so every
migration is reversible.

## 4. Configuration

All configuration is environment variables with the `QIP_` prefix, loaded by
`pydantic-settings` into a single typed `Settings` object. No module reads
`os.environ` directly. Startup fails loudly on a missing or malformed required
setting rather than defaulting to something plausible.

In non-development environments, startup refuses to proceed if `QIP_SECRET_KEY`
is unset or still the example value.

Broker access tokens are the exception to "configuration is environment
variables": they are not configuration at all. Each user grants their own
through the provider's login and it is stored encrypted in the database — see
[`credentials.md`](credentials.md). What the environment holds is the app
registration (`QIP_UPSTOX_CLIENT_ID`, `QIP_UPSTOX_CLIENT_SECRET`,
`QIP_UPSTOX_REDIRECT_URI`) and the encryption key
(`QIP_CREDENTIAL_ENCRYPTION_KEYS`), none of which rotate on the provider's
schedule. A malformed key list fails startup rather than the first connect
attempt.

## 4a. The live feed

The market stream worker is a separate service and is **not started by
default**, because it needs a connected broker account and a live provider and
it exits rather than serving generated prices:

```bash
docker compose --profile live up -d stream    # or: make stream
```

One process holds one provider connection and writes the Redis live store; every
API route reads that store. Running two stream workers against one Redis is not
an error but is not useful either — they would write the same keys from two
connections, doubling the provider load for one set of prices.

Before it will start:

- `QIP_MARKET_DATA_PROVIDER=upstox`
- `QIP_MARKET_DATA_ACCOUNT_EMAIL` naming an account whose broker connection is
  live (connect it at `/connections`)
- the instrument master loaded, narrowed with
  `QIP_UPSTOX_INSTRUMENT_SEGMENTS` / `QIP_UPSTOX_INSTRUMENT_UNDERLYINGS`

`GET /live/status` is the operational view: feed state, messages received,
reconnects, last error, and a named reason when it is not delivering. A
`STALE` status means the connection is up and nothing is arriving, which is the
one condition that otherwise looks exactly like a quiet market. See
[`live-market-data.md`](live-market-data.md).

## 5. Health, backup, resources

- `/api/v1/health` — liveness, no dependencies touched.
- `/api/v1/health/ready` — readiness; checks database, Redis and object store,
  and reports which dependency failed.
- Backups: nightly `pg_dump` plus object-store replication. Raw uploads are kept
  so any ingestion can be replayed against a newer pipeline version — which is
  what makes historical analyses reproducible after a code change.
- Baseline sizing for a small deployment: 4 vCPU / 8 GB. The worker is the
  memory-hungry component (Monte Carlo, historical repricing) and gets its own
  concurrency and memory limits so a heavy job cannot starve the API.

## 6. Production hardening checklist

- [ ] `QIP_SECRET_KEY` from a secret manager, not `.env`
- [ ] `QIP_CREDENTIAL_ENCRYPTION_KEYS` from a secret manager, backed up
      separately from the database — the database alone cannot open the rows,
      which is the point, and losing the key means every user reconnects their
      broker
- [ ] No broker access token anywhere in the environment, the image or the
      compose file. If one is there, something is still reading it.
- [ ] `QIP_MARKET_DATA_PROVIDER` is not `synthetic` on any deployment serving
      users; the synthetic market is refused at construction in a
      production-like environment, but a deployment left on the default is one
      that has not been pointed at real data
- [ ] Exactly one market stream worker per Redis, and alerts on its feed status:
      `STALE` and a rising `reconnects` are the two signals that arrive before
      anyone notices prices are wrong
- [ ] The market-data account's licensing terms checked against what the
      deployment shows to whom. A free API does not make its data
      redistributable.
- [ ] TLS at the proxy; HSTS
- [ ] Postgres and Redis not published on host ports
- [ ] Object-store credentials scoped to the single bucket
- [ ] Rate limits enabled on auth and upload routes
- [ ] Log shipping with the correlation id preserved
- [ ] Alerts on: worker queue depth, job failure rate, **IV solver failure rate,
      surface calibration failure rate, quote staleness** — the quantitative
      metrics are the ones that catch wrong numbers while everything technical
      is green
- [ ] Object-store lifecycle policy for `microstructure/`: L2 parquet grows with
      market activity rather than user activity, so it is the one prefix that
      will outgrow its bucket without one
