# Architecture — Quant Intelligence Platform (QIP)

Status: **design baseline for Phase 0**. Sections marked _(planned)_ describe a
design commitment, not shipped code. Every claim about shipped behaviour in this
document is testable against `tests/`.

---

## 1. Product framing

QIP answers one question in three parts:

> What is this position/trade approximately worth, what risk does it add, and
> what will it probably cost me to execute?

Three engines answer those parts. They are **not** three applications. They share
one instrument master, one market-data layer, one `MarketState` snapshot
abstraction, one job system, one provenance model and one reporting layer. The
integration is the product; the individual calculators are commodity.

```
                         MARKET
                            |
                    Market Data Layer
                            |
                       MarketState                 <-- single consistent snapshot
                            |
        +-------------------+-------------------+
        v                   v                   v
    VALUATION             RISK              EXECUTION
        |                   |                   |
        +-------------------+-------------------+
                            v
                     DECISION CONTEXT             <-- /order-analysis
```

## 2. Deployment topology (MVP)

A **modular monolith**, deployed as three images from one codebase, plus
infrastructure. No Kubernetes, no microservices, no service mesh in MVP.

```
                          CLIENTS
                             |
      +----------------------+----------------------+
      |                      |                      |
   Web UI (Next.js)      REST API             Python SDK (planned)
      |                      |                      |
      +----------------------+----------------------+
                             v
                   +---------------------+
                   |  reverse proxy      |   (Caddy/nginx, TLS termination)
                   +---------------------+
                             v
                   +---------------------+        +--------------------+
                   |  apps/api  (FastAPI)|<------->|  Redis             |
                   +---------------------+        |  cache + broker    |
                             |                    +--------------------+
                             | enqueue                     ^
                             v                             |
                   +---------------------+                 |
                   | apps/worker (Celery)|-----------------+
                   +---------------------+
                             |
      +----------------------+----------------------+
      v                      v                      v
+------------+     +--------------------+   +------------------+
| PostgreSQL |     | PostgreSQL +       |   | Object store     |
| app state  |     | TimescaleDB        |   | Parquet, L2,     |
|            |     | quotes, bars,      |   | chains, MC paths |
|            |     | surfaces, snapshots|   | (local FS / S3)  |
+------------+     +--------------------+   +------------------+
```

`apps/scheduler` (Celery beat) triggers periodic work: surface recalibration,
snapshot rollups, data-quality sweeps. It holds no business logic.

### Why a modular monolith

- The three engines share a large amount of state (`MarketState`, instrument
  master, yield curves, surfaces). Splitting them into services early would turn
  every valuation into a distributed transaction over a snapshot that must be
  timestamp-consistent (§13 of the build spec). That is the wrong first problem
  to solve.
- Domain boundaries are enforced in-process by the import rules in §4 below and
  checked by a lint rule, so extraction later is mechanical.

## 3. Layering and dependency rules

```
apps/            process entrypoints. Wiring only. No logic.
  |
api/             HTTP surface: routing, request/response schemas, authz.
  |              MUST NOT contain financial mathematics.
  v
domains/*/service.py     application + domain services. Orchestration,
  |                      persistence, provenance stamping, policy.
  v
quant/           pure numerical libraries. No I/O, no DB, no HTTP, no logging
  |              of business events, no knowledge of users or portfolios.
  v
infrastructure/  DB sessions, cache, queue, object store, logging, security
                 primitives. Knows nothing about finance.
```

Hard rules, enforced by `scripts/check_layering.py` in CI:

| Layer | May import |
| --- | --- |
| `apps/` | everything |
| `api/` | `domains/`, `infrastructure/`, `quant/` (types only) |
| `domains/` | other `domains/` **contracts only**, `quant/`, `infrastructure/` |
| `quant/` | stdlib, numpy, scipy, pandas/polars. **Nothing else in this repo.** |
| `infrastructure/` | stdlib + third-party only. **No `domains/`, no `quant/`.** |

`quant/` being import-clean is what makes the numerical layer testable in
isolation and reusable from a notebook, and is why quantitative validation tests
(`tests/quant_validation/`) need no database.

### Module dependency graph

```
                                   apps/api            apps/worker      apps/scheduler
                                       |                    |                |
                                       v                    v                v
                                   api/routes  <----------- domains/jobs ----+
                                       |                        ^
        +------------------------------+------------------------+
        |            |            |             |               |
        v            v            v             v               v
  domains/users  domains/    domains/       domains/        domains/
                 instruments market_data    derivatives     portfolio
                     ^            |             |               |
                     |            |             |               v
                     +------------+             |          domains/risk
                        resolves                |               |
                                                |               v
                                                |        domains/scenarios
                                                |               |
                                                +-------+-------+
                                                        |
                                                        v
                                                 domains/execution
                                                        |
        +-----------------------------------------------+
        v
   quant/  (pricing, volatility, interpolation, numerical, statistics,
            optimization, simulation)
        |
        v
   infrastructure/ (database, cache, queue, storage, observability, security)
```

Cross-domain reads go through a **service interface**, never through another
domain's SQLAlchemy models. `domains/risk` asks `domains/portfolio` for a
valued portfolio; it does not `SELECT` from `positions`.

## 4. Domain boundaries

| Domain | Owns | Does not own |
| --- | --- | --- |
| `users` | identity, credentials, sessions, ownership | anything financial |
| `instruments` | canonical instrument identity, aliases, resolution | prices |
| `market_data` | providers, quotes, bars, books, option chains, **quality**, `MarketState` | models fitted to that data |
| `derivatives` | IV, smiles, SVI/SSVI surfaces, arbitrage, local vol, model consensus, anomalies | portfolio aggregation |
| `portfolio` | portfolios, positions, import, valuation, Greeks aggregation | risk measures |
| `risk` | VaR, ES, stress results, risk contribution, **margin** | scenario definitions |
| `scenarios` | scenario/shock definitions, historical stress events, shock application | how a portfolio reprices |
| `execution` | orders, executions, benchmarks, TCA, impact models, simulators | portfolio risk |
| `reports` | assembled multi-domain read models (incl. `/order-analysis`) | any calculation of its own |
| `jobs` | async job lifecycle, progress, results, cancellation | what the job computes |

`reports` is the only domain permitted to fan out across the other engines, and
it may only *compose* their results.

## 5. The `MarketState` contract

Every material calculation takes a `MarketState`, never a live provider handle.
This is the single most important architectural constraint in the system.

```python
@dataclass(frozen=True)
class MarketState:
    state_id: UUID  # content-addressed; identical inputs -> identical id
    as_of: datetime  # the logical valuation timestamp (UTC)
    quotes: Mapping[InstrumentId, Quote]
    spot_prices: Mapping[InstrumentId, Decimal]
    future_prices: Mapping[InstrumentId, Decimal]
    yield_curves: Mapping[CurveId, YieldCurve]
    fx_rates: Mapping[CurrencyPair, Decimal]
    volatility_surfaces: Mapping[SurfaceKey, VolatilitySurface]
    data_versions: Mapping[str, str]  # provider -> dataset version/etag
    quality: Mapping[InstrumentId, MarketDataQuality]
```

Consequences:

- Two calculations run against the same `state_id` are guaranteed to have seen
  the same inputs. Risk aggregation cannot mix a 09:15 delta with a 09:47 vega.
- Re-running an analysis is a matter of rehydrating a persisted `MarketState`.
- `quant/` functions receive plain floats/arrays extracted from `MarketState` by
  the domain layer, keeping the numerical layer free of infrastructure types.

Quotes inside a snapshot carry their own `exchange_timestamp`; the snapshot's
`as_of` is the *decision* time. Staleness is measured per instrument, surfaced in
`MarketDataQuality.stale_score`, and never silently repaired.

## 6. Observation vs. estimate (build spec §1.2)

The type system enforces the split. There is no field that can hold either.

| Observed (never overwritten) | Derived (recomputable, versioned) |
| --- | --- |
| `market_bid`, `market_ask`, `market_last` | `mid_price`, `microprice`, `spread` |
| `market_iv` (from an observed price) | `reference_iv` (from a fitted surface) |
| `exchange_timestamp` | `quote_age` |
| `market_mid` | `reference_value`, `reference_range` |
| `volume`, `open_interest` | `liquidity_score` |

Persisted rows for observations are **append-only**. A correction is a new row
with a new `receive_timestamp` and a supersedes pointer, never an `UPDATE`.

## 7. Provenance

Every persisted analytical result carries a `provenance` JSONB column with a
fixed shape (`domains/reports/provenance.py`, planned as a shared value object):

```json
{
  "market_state_id": "...",
  "market_state_timestamp": "2026-09-24T09:20:00Z",
  "market_data_sources": ["csv:user-upload:9f3a", "synthetic:v1"],
  "dataset_versions": {"csv:user-upload:9f3a": "sha256:..."},
  "yield_curve_id": "...",
  "surface_id": "...",
  "model_versions": {"pricing": "black76@1.0.0", "surface": "svi-raw@0.3.1"},
  "calibration_timestamp": "2026-09-24T09:20:04Z",
  "numerical_tolerances": {"iv_abs": 1e-8, "iv_max_iter": 100},
  "code_commit": "abc1234",
  "computed_at": "2026-09-24T09:20:05Z"
}
```

A result without complete provenance is a bug, not a degraded result.

## 8. Failure handling (build spec §92)

A quantitative failure is a **result**, not an exception escaping to a 500.

Every analytical endpoint returns an envelope:

```json
{
  "status": "OK | PARTIAL | FAILED",
  "results": { "...": "..." },
  "warnings": [
    {"code": "SVI_CALIBRATION_DEGRADED",
     "severity": "WARNING",
     "message": "Expiry 2026-10-29 fitted on 7 quotes; RMSE 1.8 vol pts.",
     "context": {"expiry": "2026-10-29", "n_obs": 7}}
  ],
  "provenance": { "...": "..." }
}
```

- `OK` — every requested quantity computed within tolerance.
- `PARTIAL` — some quantities are present, some are `null` with a warning that
  names the missing quantity. The client renders what exists.
- `FAILED` — nothing usable. Still HTTP 200 with a structured body when the
  request itself was valid; HTTP 4xx only for malformed/unauthorized requests.

Warning codes are a closed enum in `domains/*/warnings.py` so the frontend can
map them to explanations (§80 explainability) without string matching.

Fail-fast is reserved for programming errors and infrastructure loss.

## 9. Asynchronous compute

Anything whose p95 exceeds ~1s runs as a job. Phase 0 ships the machinery; later
phases add job types.

```
POST /api/v1/<resource>          -> 202 {"job_id": ..., "status": "QUEUED"}
GET  /api/v1/jobs/{job_id}       -> status + progress
GET  /api/v1/jobs/{job_id}/result-> result payload or pointer to object store
```

Job results larger than ~1 MB are written to the object store; PostgreSQL keeps
the pointer (`result_reference`), never the blob.

`QIP_JOB_EXECUTION_MODE=eager` runs the same task function inline. Tests and
laptop development therefore need no Redis, and the code path under test is the
production code path.

## 10. Storage strategy

| Data | Store | Rationale |
| --- | --- | --- |
| users, portfolios, positions, instruments, jobs, model registry | PostgreSQL | transactional, relational, small |
| quotes, bars, risk snapshots, surface parameters | PostgreSQL + TimescaleDB hypertables | time-ordered, queried by range, moderate volume |
| raw uploads, L2 history, tick trades, historical option chains, MC paths, simulation output | object store, Parquet | unbounded volume; column-pruned analytical access |
| latest quote, latest surface, expensive results | Redis | read-through cache, versioned keys |

Partitioning for object-store datasets:
`{dataset}/exchange={x}/instrument={i}/date={yyyy-mm-dd}/hour={hh}/part-*.parquet`.

Analytical access to Parquet uses DuckDB/Polars, never a row-by-row ORM load.

Cache keys embed the data version so invalidation is implicit:
`surface:v1:NIFTY:2026-09-24:{market_state_id}`.

## 11. Security model

- Password hashing: bcrypt, cost 12, via `infrastructure/security/passwords.py`.
- Tokens: short-lived JWT (HS256 in MVP, asymmetric when multi-service), `sub` =
  user id, `jti` for revocation, explicit `exp`/`iat`/`nbf`.
- **Ownership is checked on every resource route.** A UUID is not an
  authorization token. `api/dependencies/authz.py` provides
  `owned_portfolio(portfolio_id, user)` style dependencies that 404 (not 403) on
  a foreign resource, so ids are not enumerable.
- Uploads: size cap, extension + sniffed-content check, row cap, schema
  validation, sanitized stored filename (server-generated UUID; the client name
  is metadata only), no spreadsheet formula evaluation, parsed in the worker
  after landing in the object store.
- Rate limiting: Redis token bucket per user and per IP, applied to auth and
  upload routes first.
- Audit log: append-only `audit_logs` for auth events, uploads, portfolio
  mutation, and every job submission.

## 12. Observability

Structured JSON logs (`structlog`) with a request-scoped `correlation_id`
propagated into Celery task headers, so one HTTP request and its downstream jobs
share an id.

Two metric families, deliberately separated:

- **Technical**: HTTP latency/error rate, queue depth, job duration, DB latency,
  cache hit rate.
- **Quantitative**: quote age distribution, IV solver failure rate, surface
  calibration failure rate, SVI RMSE, arbitrage-violation counts, invalid
  local-vol points, MC runtime, TCA data coverage.

The second family is the one that tells you the *numbers* are wrong while every
technical metric is green. It is a first-class requirement, not an add-on.

## 13. Model registry

Analytical models are versioned exactly like ML models. `model_versions` records
name, semantic version, type, parameters, training/calibration window, code
commit and metrics. Every result references the `model_version_id` it used.
Changing a formula requires a version bump; the regression suite pins outputs so
an unversioned change fails CI.

## 14. What Phase 0 actually ships

Implemented and tested in this repository today:

- Repository skeleton, layering rules, Docker Compose environment.
- Settings, structured logging, database session management, Redis cache,
  object store (local + S3-compatible interface), Celery wiring with eager mode.
- `users`: registration, login, bcrypt, JWT, ownership dependency, audit log.
- `instruments`: canonical identity, deterministic ids, aliases, resolver.
- `market_data`: provider interface, `CSVMarketDataProvider`,
  `SyntheticMarketDataProvider`, canonical `Quote`/`OptionQuote`/`Bar`/
  `OrderBookSnapshot`, the **data-quality engine**, and the option-chain
  ingestion pipeline (upload -> validate -> normalize -> quality -> persist ->
  retrieve).
- `jobs`: model, service, Celery task, status/progress/result API.

## 15. What Phase 1 adds

- `quant/daycount.py`: ACT/365F, ACT/360, ACT/365.25, 30/360, computed in
  seconds so intraday time is not rounded away.
- `quant/pricing`: Black-76 (price, vega, no-arbitrage bounds) and
  Black-Scholes-Merton (price and all first- and second-order Greeks), with a
  `Greeks` container whose fields name their units.
- `quant/numerical/roots.py`: bracketed Brent and a vectorized safeguarded
  Newton, both reporting convergence.
- `quant/volatility`: the implied-volatility engine (structured non-results, and
  a *reported conditioning* — vega and the volatility uncertainty implied by one
  price ulp) and raw smile construction in `(k, w)`.
- `quant/interpolation`: linear interpolation with an explicit extrapolation
  policy, so the choice lands in provenance instead of a library default.
- `domains/market_data/curves.py`: content-addressed `YieldCurve`.
- `domains/derivatives`: the time-to-expiry policy, the three forward
  estimators, the chain-analysis pipeline, its job type, and persistence
  (`yield_curves`, `chain_analyses`, `forward_estimates`,
  `option_implied_vols`).
- API: `/derivatives/iv`, `/greeks`, `/forward`, `/chains/{id}/analyze`,
  `/chains/{id}/smile`, `/analyses`.
- Web: the smile chart with the bid/ask IV envelope, a per-expiry forward panel
  showing every estimate, and a conditioning inspector.

Deferred from Phase 1 by design: the `MarketState` builder and `/market/state`.
Phase 1 works from a single chain snapshot, which is already timestamp-coherent;
the surface in Phase 2 is the first consumer that genuinely needs a frozen
multi-instrument snapshot, and building the abstraction before it has a real
consumer would have fixed its shape around a guess.

## 16. What Phase 2 adds

- `quant/volatility/svi.py`: analytic derivatives of `w`, Durrleman's `g`, Lee's
  wing bound.
- `quant/volatility/svi_calibration.py`: constrained SLSQP with deterministic
  multi-start, with the no-arbitrage conditions **in the feasible set**.
- `quant/volatility/arbitrage.py`: the static conditions as pure array checks,
  returning signed magnitudes rather than booleans.
- `domains/derivatives/surface.py`: the fitted surface, content-addressed, whose
  reference values are a pure function of the persisted parameters.
- `domains/derivatives/arbitrage.py`: scope, severity-by-magnitude, reporting.
- `domains/derivatives/calibration.py`: raw diagnostics, then fit, then fitted
  diagnostics — in that order.
- `domains/market_data/market_state.py`: the snapshot abstraction and its
  builder, and `domains/reports/composition.py` to assemble one across the
  market-data and derivatives domains without either importing the other.
- Persistence: `volatility_surfaces`, `surface_slices`, `surface_parameters`,
  `arbitrage_reports`, `arbitrage_violations`.
- API: calibration as a job, surface retrieval, reference values, arbitrage by
  scope, `GET /market/state`.
- Web: observed-versus-fitted overlay with the extrapolated region dashed,
  per-slice admissibility, and the two arbitrage scopes side by side.

## 17. What Phase 3 adds

- `quant/volatility/svi.py`: analytic volatility derivatives, so a slice's level,
  skew and curvature are exact functions of its five parameters.
- `quant/statistics/distribution.py`: percentile ranks, z-scores and sample
  summaries, with explicit guards for thin and degenerate samples.
- `domains/derivatives/characteristics.py`: surface shape at standard tenors.
- `domains/derivatives/history.py`: percentiles against an underlying's own past.
- `domains/derivatives/anomaly.py`: the deviation model, the confidence model
  and the explanations.
- Persistence: `surface_characteristics`, `anomaly_scans`, `surface_anomalies`.
- API: scanning as a job, scan retrieval, and surface history.
- Web: the scanner table with an explanation panel and a history panel.

### The pieces built for other reasons are what make this work

The anomaly scanner invents almost nothing. Its denominator is assembled from
three quantities the platform already had for unrelated reasons: the bid/ask
implied-volatility envelope (Phase 1, to show how much of a deviation is just
the spread), the slice calibration RMSE (Phase 2, a fit metric), and the
price-resolution uncertainty (Phase 2, added to stop tick-floored quotes
corrupting a fit). Its confidence reuses the Phase 0 quality scores and the same
weighted geometric mean the quality engine aggregates with.

That is the integration the product is supposed to be. A scanner that had to
invent its own notion of a trustworthy quote would disagree with the ingestion
pipeline about which quotes are good, and both would be right in their own
terms.

### `domains/reports` earns its description

The composition of a `MarketState` is the first case where a single object needs
material from two engines. Market data owns quotes and curves; derivatives owns
surfaces. Rather than have either import the other, `MarketStateComposer` in
`domains/reports` assembles both halves — which is exactly the role section 4
assigns that domain, and the layering check enforces it.
