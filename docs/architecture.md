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

### One edge the graph above does not draw

`domains/portfolio/valuation.py` imports `domains.derivatives.surface`, and the
graph should be read with that in mind. The import is of a **frozen value
object** — `VolatilitySurface` performs no I/O and holds no session; its
`reference()` is a pure function of persisted parameters. Valuation needs to
price an unquoted option off a fitted surface, and the alternatives were worse:
copying the SVI evaluation into the portfolio domain would create a second
implementation to keep in step, and passing the parameters as loose floats would
lose the flags and the extrapolation checks the value object carries.

What portfolio does *not* do is load a surface. Every database-level fan-out
goes through `domains/reports/composition.py::ValuationContextComposer`, which
section 4 designates as the only domain permitted to reach across engines. The
layering check enforces the part that matters — no domain may import another
domain's `orm` or `repository` — and this dependency is on neither.

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
`MarketDataQuality.stale_score`, and never silently repaired. The `quality` map
carries the measurement made at ingestion, rehydrated with its flags intact — a
consumer that needs to know how good a quote is reads the measurement that was
made rather than making a second, divergent one.

`/order-analysis` (§13a of the API document) is the contract's strongest test:
five engines answer five different questions about one proposed order, and all
five read the same `state_id`. The current-to-proposed differences a user reads
are then attributable to the order, which is the only thing that makes them
worth showing.

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
| raw uploads, L2 depth and event history **(P10)**, tick trades, historical option chains, MC paths, simulation output | object store, Parquet | unbounded volume; column-pruned analytical access |
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

## 18. What Phase 4 adds

- `domains/portfolio/models.py`: `Position` with a signed quantity whose sign and
  `side` must agree, and `PositionValuation` with `market_price` and
  `model_price` as separate fields.
- `domains/portfolio/importer.py`: column-mapping inference over position files,
  instrument resolution into `resolved / ambiguous / invalid`, and the refusal to
  auto-resolve.
- `domains/portfolio/valuation.py`: the price waterfall, per-position Greeks,
  currency conversion at the snapshot's own rate, and aggregation over five
  dimensions.
- `domains/reports/composition.py`: `ValuationContextComposer`, one `MarketState`
  plus the fitted surfaces for every underlying a portfolio touches.
- Persistence: `portfolios`, `positions`, `portfolio_valuations`,
  `position_valuations`.
- API: portfolio and position CRUD, the two-step import, valuation as a job, and
  Greeks by group.
- Web: portfolio list, import wizard showing the three buckets, and a valuation
  dashboard.

### The separation is enforced in three places, not one

The rule that a model estimate must never overwrite an observation is easy to
state and easy to erode. Phase 4 holds it in the schema (`market_price` and
`model_price` are distinct columns, with no column that could hold either), in
the type (`PositionValuation` mirrors the same split, and `valuation_method`
names which one was used), and in the tests (both a unit test on the service and
an integration test over the serialised response). The database `CHECK` closes
the remaining gap: a row with no value must carry `UNAVAILABLE`, so an unpriced
position cannot be recorded as worth zero.

### Aggregation reuses the position numbers rather than recomputing

Every aggregate dimension sums the *same* per-position `base_market_value` and
the same per-position Greeks. This is why each grouping totals to the portfolio
total, and why a property test can assert it over arbitrary mixes of long and
short legs. A dimension that recomputed from raw quotes would drift from the
position rows the moment either changed, and the drift would look like a rounding
problem rather than the double implementation it actually is.

## 19. What Phase 5 adds

- `quant/statistics/var.py`: sample and parametric tail risk, a normal quantile
  accurate to machine precision in both tails, and a bootstrap interval.
- `quant/statistics/covariance.py`: the sample estimator, with its noise and
  rank conditions reported rather than regularised away.
- `quant/simulation/paths.py`: a seeded, antithetic, explicitly-parameterised
  factor simulation.
- `domains/scenarios/`: shocks, scenarios, the shipped hypothetical templates,
  and derivation of a genuinely historical scenario from recorded data.
- `domains/risk/exposure.py`: the anchors both the base and the shocked price
  are computed from.
- `domains/risk/revaluation.py`: full repricing, one scenario at a time and
  vectorised across many, plus the Greek approximation of the same move.
- `domains/risk/stress.py`, `var.py`, `factors.py`, `application.py`, `jobs.py`.
- `domains/reports/composition.py`: `FactorHistoryComposer`, the second
  cross-engine fan-out.
- Persistence: `stress_scenarios`, `risk_snapshots`, `var_results`,
  `stress_results`.
- API: the scenario library, derivation, VaR and stress as jobs.
- Web: a risk dashboard, a stress lab, and the scenario library.

### The base price had to become a model price

Under a shock nobody quoted anything, so a stressed price can only come from a
model. That forces a decision about the *base* price, and the obvious choice —
compare the model's shocked price against the market's observed price — is
wrong: the difference would then contain the model's disagreement with the
market as well as the shock, and a scenario with no shocks in it would report a
P&L.

So both sides are priced by one function from one set of anchors, and the anchor
volatility is the one implied by each position's own observed price. That makes
the null scenario exactly zero, which is the property every stress number rests
on, and it is asserted by a test. What it costs is that the model value and the
marked value can differ; that gap is stored in its own column and reported as a
warning rather than quietly absorbed.

### Phase 5 reuses Phase 3's history rather than building its own

Historical VaR needs a factor history, and the platform had never stored one on
purpose. It turned out to have two: `option_chain_snapshots.underlying_price`,
written once per ingestion since Phase 0, and `surface_characteristics
.atm_volatility` at standard tenors, written once per calibration since Phase 3
— and recorded at *fixed* tenors precisely so the series would outlive any one
expiry.

That is why no price-history ingestion pipeline was added. The lookback a user
gets is exactly as long as their own ingestion record, which is a real
constraint; it is reported as an observation count on every answer, and below
ten aligned observations the response is a refusal rather than a number.

### Extraction boundaries after Phase 5

`domains/risk` imports `domains.portfolio.application` to value a book before
repricing it, and `domains.scenarios.models` for the shock vocabulary. Both are
service-level and value-object imports, not persistence, so the layering rule
holds. The database-level fan-out — one `MarketState` for valuation, two factor
histories for risk — stays inside `domains/reports/composition.py`.

## 20. What Phase 6 adds

- `domains/risk/margin.py`: the `MarginModel` interface, `ShockGrid`,
  `MarginParameters`, `MarginResult` and `SimpleRiskMarginModel`.
- `domains/risk/vulnerability.py`: the buffer ladder and the estimated
  margin-shortfall region.
- `domains/risk/exposure.py`: `shifted()`, which produces the book as it would
  stand in a moved market — the piece that lets a margin model be run *again* at
  every rung rather than only once at today's prices.
- Persistence: `margin_results`.
- API: the model catalogue, margin as a job, and stored results.
- Web: a margin page with the buffer curve, and no liquidation marker on it.

### Margin is in `domains/risk`, not a domain of its own

Section 4's domain table has no margin domain, and Phase 6 did not add one.
Margin is a measurement of a portfolio's risk taken with the same exposures,
the same repricing and the same shock machinery as VaR and stress; splitting it
out would have meant either duplicating `ExposureSet` or making a new domain
import another domain's internals. The `MarginModel` ABC provides the extension
point that a separate package would have been reaching for.

### The phase's real work was deciding what not to compute

Almost every design decision here was about a number *not* produced. There is no
short-option minimum rate, no concentration rate, no liquidation price, no
`required_margin` column, and no model claiming broker equivalence — and each
absence is enforced somewhere a future change would have to notice: a zero
default with a warning that says what it leaves out, a database CHECK, a test
that scans the serialised payload for venue names, and a test that permits the
word "liquidation" only when it is preceded by "not a broker".

The one place a number *had* to be chosen is the shock grid, and it is handled
by making the grid a declared parameter that travels on every result. A margin
figure is the worst loss over the moves someone chose to look at; a reader who
cannot see those moves cannot judge the figure, so they are always in the
payload.

### `shifted()` earns its place beyond margin

The ladder needs a book that can be *remeasured* in a hypothetical market, not
just repriced once. `PositionExposure.shifted()` moves the anchors and
recomputes the base price from them, and sets the reported value equal to the
model value — because a hypothetical state has no mark, and pretending otherwise
would make the repricing gap meaningless at every rung but the first. The same
operation is what a multi-step or path-dependent scenario will need later.

## 21. What Phase 7 adds

- `domains/execution/models.py`: `Execution`, `ParentOrder`, and the grouping
  that decides what a benchmark window even is.
- `domains/execution/benchmarks.py`: `MarketWindow`, `DataCoverage` and six
  benchmarks, each returning either a price with its window, source and method,
  or an explicit unavailability with a reason.
- `domains/execution/tca.py`: implementation shortfall in three units, and the
  model-based decomposition.
- `domains/execution/importer.py`, `orm.py`, `repository.py`, `application.py`,
  `jobs.py`.
- `domains/reports/composition.py`: `ExecutionWindowComposer`, the third
  cross-engine fan-out.
- `domains/market_data/service.py`: `instrument_quote_history` and
  `underlying_level_history`.
- Persistence: `executions` (append-only), `execution_reports`.
- API: trade-log preview and import, analysis as a job, reports.
- Web: the execution dashboard.

### An unavailable benchmark is a first-class result

The obvious shape for a benchmark function is "return a price". That shape has
no room for the answer this platform most often has to give, which is *"the data
you hold cannot support this benchmark, and here is why"* — so every benchmark
returns a `Benchmark` whose `price` may be `None` alongside an
`unavailable_reason` that is always populated when it is.

The consequence propagates: a benchmark with no price produces no shortfall
rather than a zero; the analysis carries an `unavailable_shortfalls` list beside
its `shortfalls` list; and the database refuses to store a shortfall without the
benchmark price it was measured against. Three layers, one rule, because "no
benchmark was available" and "the cost was zero" must never render the same way.

### The window comes from work done for other reasons, again

Like Phase 5's factor history, Phase 7's market window is assembled from what
the platform already stores: the option quotes written with each ingested chain,
falling back to the underlying level on the snapshot itself. No new ingestion
path was added.

For most users that data is sparse, and the honest consequence is that the
interval benchmarks usually refuse. The thresholds are stated constants (four
observations, sixty percent span), the coverage figures travel on every window,
and the refusal names the numbers that caused it. A platform that instead
averaged three ticks into an "interval VWAP" would produce a benchmark no market
ever traded at, and every cost measured against it would inherit the error
invisibly.

### Two sign conventions, kept apart on purpose

`positions.quantity` is signed; `executions.quantity` is always positive with
direction in `side`. That looks like an inconsistency and is not: a position's
sign says which way you are exposed, a fill's side says what you did, and the
cost convention that makes a buy above the benchmark and a sell below it read
the same way lives in exactly one place (`Side.sign`). Sharing one convention
between the two would give a sign two homes and let them drift.

## 22. What Phase 8 adds

- `domains/execution/impact.py`: the `MarketImpactModel` interface with
  square-root, linear and zero implementations, and the coefficient the platform
  refuses to invent.
- `domains/execution/strategies.py`: the `ExecutionStrategy` interface, TWAP,
  VWAP, POV and liquidity-adaptive, and the exact allocation that makes a
  schedule sum to its parent.
- `domains/execution/simulation.py`: the counterfactual simulator and strategy
  comparison.
- Persistence: `execution_simulations`.
- API: the strategy and impact-model catalogues, simulation as a job, and stored
  runs.
- Web: the simulation page.

### The identity is the only honest default for a coefficient

Phase 6 defaulted the margin model's optional rates to **zero**, because a
plausible-looking 2% would have been an invented venue rule and zero is visibly
an absence. That answer does not transfer here: an impact model whose
coefficient is zero is not a model with a missing part, it is a model that says
trading is free.

So the default is `1.0` — the identity — which makes the output the *shape* of
the model in units of `sigma * sqrt(Q/ADV)` rather than a magnitude, and every
result computed that way is flagged `IMPACT_COEFFICIENT_NOT_CALIBRATED`. The
principle is the same in both phases: never state a number nobody measured. The
form the refusal takes depends on what a missing number would mean.

### Three layers of "this never happened"

The counterfactual label is not a string in a docstring. It is on the result
object, in the serialised payload's own `caveat`, first in the envelope's
warnings, and enforced by `CHECK(counterfactual)` on the table. A hypothesis
property asserts it survives every path through the simulator.

That redundancy is deliberate. A simulated average price and a real one look
identical in a table, and the moment one is copied into a report without its
label it becomes a claim about what happened.

### Refusal propagates one layer up from Phase 7

Phase 7 established that a benchmark the data cannot support returns an explicit
unavailability rather than a number. Phase 8 applies the same rule to
strategies: VWAP on a flat profile refuses because it *is* TWAP, and
liquidity-adaptive on flat signals refuses because it *is* VWAP. Returning
either under the wrong name would make the comparison between them meaningless,
which is the only thing the comparison is for.

### The simulator borrows Phase 7's ruler rather than building its own

Simulated fills become `Execution` objects, group into a `ParentOrder`, and run
through the same benchmark set and shortfall calculation as real fills. Writing
a second scoring path would have been easier and would have guaranteed that a
counterfactual and the execution it is compared against eventually diverged for
reasons having nothing to do with the schedules.

They are not written to `executions`, which stays what happened.

## 23. What Phase 9 adds

- `quant/volatility/ssvi.py` and `ssvi_calibration.py`: the SSVI surface, its
  admissibility conditions in closed form and numerically, and one constrained
  SLSQP fit over every expiry at once.
- `quant/volatility/local_vol.py`: Dupire in total-variance form, with the
  invalid regions kept as holes.
- `quant/volatility/density.py`: Breeden-Litzenberger, with four diagnostics and
  quantiles withheld from an inadmissible density.
- `quant/numerical/pde.py`: Crank-Nicolson with Rannacher start-up on a
  concentrated log grid, and an order-of-convergence helper.
- `quant/pricing/heston.py` and `heston_calibration.py`: the little-trap
  characteristic function with an adaptive integration limit, and a vega-weighted
  constrained calibration.
- `quant/pricing/monte_carlo.py`: seeded, antithetic, control-variate.
- `quant/pricing/higher_order.py`: vanna, volga and charm, analytic and scaled.
- `domains/derivatives/global_surface.py`: the domain surface, its reference
  lookups, and the local-volatility coefficient it hands the PDE.
- `domains/derivatives/consensus.py`: the `PricingModel` interface, four
  implementations, and the consensus that never returns one price.
- `domains/derivatives/advanced.py` and `advanced_jobs.py`: persistence and the
  two job handlers.
- Persistence: `global_surfaces`, `global_surface_slices`,
  `local_volatility_surfaces`, `risk_neutral_densities`, `heston_calibrations`,
  `model_consensus_runs`, `model_values`.
- API: global-surface calibration and retrieval, the local-volatility grid, the
  densities, the Heston fit, and consensus pricing as a job.
- Web: the global-surface page and the consensus page.

### Two surfaces, not one replaced by the other

Per-expiry SVI describes any single smile better than three shared parameters
can. SSVI cannot contain calendar arbitrage. Those are different virtues, so
both are kept, in separate tables, and the difference between them on the same
analysis is itself a measurement. Adding a `model` column to
`volatility_surfaces` would have been less code and would have hidden the fact
that only one of the two carries the structural guarantee.

### The guarantee is a database constraint, not a docstring

`ck_converged_global_surface_is_arbitrage_free` refuses a row that calls itself
`CONVERGED` while carrying a decreasing variance term structure or a negative
implied density. This is the same device as Phase 5's
`ck_scenario_historical_claim_has_derivation`, Phase 6's
`ck_margin_buffer_requires_capital` and Phase 8's
`ck_simulation_is_always_counterfactual`: when a rule is the reason a feature
exists, it gets an enforcement point a refactor cannot walk past.

### A model that cannot run is a row, not a gap

`ck_model_value_has_value_or_reason` makes the exclusive-or a database rule.
A consensus over three models because the fourth failed and a consensus over
three because only three were asked for are different results, and only stored
unavailability distinguishes them. Nothing is defaulted to make a model
runnable: Heston with no calibration reports itself unavailable rather than
being priced on a plausible-looking parameter set.

### Testing an order, not an error

The PDE's acceptance criterion is the *rate* at which its error falls, not the
size of the error. A coarse grid can be close by luck and a wrong scheme can be
close on one contract; only the order distinguishes a correct second-order
scheme from an incorrect one. It earned its place immediately: gamma converged
at first order until the solution interpolation was widened from a three-point
quadratic to a five-point quartic, and the price and delta looked fine
throughout.

### The round trip that found two real errors

A local-volatility surface derived from an implied surface must reprice that
implied surface. Ours was off by 1.6-2.3% and did not improve with refinement,
which ruled out discretisation. Two causes: the maturity forward was being used
at every time step instead of the forward to each time, shifting every lookup
along the smile by the carry; and the variance term structure was clamped flat
below the first expiry, making `dtheta/dT` zero across the whole front so Dupire
produced nothing there. Both were invisible in every other test. The error is
now under 0.2%.

### Two things only a live run found

The integration suite passed while a stored surface was silently dropping the
forward method and confidence its slices were fitted with. Nothing broke: every
reference value still came back, and every assertion still held. What changed
was that a surface read back from the database flagged all of its own values
`LOW_CONFIDENCE_FORWARD` and quietly took the consensus confidence from 0.998 to
0.904 for a reason that was not true. The two columns are now stored and
restored, and a test asserts the round trip rather than the values.

The same run showed a Heston fit with `kappa = 0.043` and `theta = 0.23` against
a `v0` of 0.015 — parameters that look wrong and are not. Two expiries pin
`kappa * theta` and not the two separately, and the fit reproduced the observed
surface to 0.009 volatility points. The calibration now emits
`HESTON_MEAN_REVERSION_NOT_IDENTIFIED` below three maturities and the consensus
repeats it as a caveat on the price, because the alternative — presenting an
unidentified parameter as a measurement — is the failure this platform exists to
avoid. It is a caveat and not a refusal: the surface is described well, and the
surface is what the price depends on.

### Deviating from a written plan, in writing

`docs/pricing.md` said Heston would be QuantLib-wrapped. It is implemented
directly instead, with QuantLib as the test oracle, following the standing rule
for core numerics in `docs/references.md`. The deviation is recorded in both
documents rather than left as a discrepancy between the plan and the code.

## 24. What Phase 10 adds

```
quant/microstructure/
  book.py            snapshot measures, each returning a value or a reason
  intensity.py       Poisson and Hawkes, and the held-out test between them
  queue.py           the bracketed queue outlook

domains/microstructure/
  models.py          dataset profile, rejection vocabularies, sequencing report
  availability.py    THE GATE — six capabilities, granted or refused with evidence
  importer.py        wide-CSV level detection and the canonical parquet path
  storage.py         parquet in the object store, decimals not floats
  analytics.py       measure every snapshot, summarise what was measurable
  intensity.py       select a scope, run the comparison, carry the verdict out
  queue.py           assemble the level and its departures, hand them to quant/
  orm.py             four tables, three of which encode the gate as CHECKs
  application.py     require the capability, then compute. Never the reverse
  jobs.py            IMPORT_BOOK_DATA, ANALYZE_MICROSTRUCTURE, FIT_INTENSITY

api/routes/microstructure.py, api/schemas/microstructure.py
web/app/microstructure/
```

### The gate is a stored judgement, not a runtime check

Every other engine in the platform computes what it can and degrades with a
warning. This one refuses, and the refusal is decided **once, at import**, and
stored on the dataset row.

That ordering is the design. Deciding at call time would mean each endpoint
re-derived what the data supports, and the four endpoints would drift; deciding
at import means there is one judgement, recorded with its evidence and the
thresholds it was taken against, that every endpoint consults and none can
recompute more leniently. `MicrostructureApplicationService.require_capability`
is the only way through, and it reads the stored report rather than the data.

The reason microstructure gets this and the other phases do not is that its
failure mode is different. A surface fitted to thin data is *visibly* uncertain.
An order-book imbalance from a one-level feed is a number between -1 and 1 that
looks exactly like a real one.

### `BookEvent` went into market data, not into this domain

An order-book message is a canonical market observation, so it lives beside
`Quote`, `Bar`, `Trade` and `OrderBookSnapshot` in `domains/market_data/models.py`
rather than in the domain that happens to consume it first. Two things follow:
a provider can publish events without Phase 10 existing, and
`ProviderCapability.BOOK_EVENTS` is declarable separately from `ORDER_BOOK` —
which matters, because a feed that publishes periodic depth supports the book
analytics and cannot support a queue or an intensity model, and the difference
has to be visible before a caller plans around it.

### The storage rule finally bites

`docs/architecture.md` §10 has said since Phase 0 that anything growing with
market activity belongs in the object store. Phase 10 is the first dataset where
that is not a rounding error — one session of depth for one liquid contract is
millions of rows — and it is followed exactly: parquet under a server-generated
key, a metadata row in Postgres, and the complete rejection list in the object
store too, because it is unbounded and a column would have to truncate the very
thing that makes "nothing is dropped without a reason" checkable.

Two decisions inside the file are worth recording. Prices and quantities are
`decimal128(38, 12)` rather than floats, because a stored observation is a fact
and the platform does not re-round a venue's ticks on the way to disk. And
levels are list columns rather than a fixed `bid_px_1 ... bid_px_20` width,
because depth genuinely varies and a padded level is indistinguishable from a
level quoted at zero — which is a real thing on some venues.

### A held-out win had to become a held-out *test*

The first version of the intensity gate compared held-out log-likelihood totals
and adopted the self-exciting model when the difference was positive. On ten
tapes of genuinely Poisson arrivals it adopted the richer model seven times, by
margins around 0.05 nats over nine hundred events — noise with a sign.

The fix was to stop comparing totals. The held-out likelihood decomposes
sequentially into one predictive contribution per event, and the mean of those
is testable against zero with a one-sided Diebold-Mariano statistic and a
Newey-West variance. With the HAC correction — which is not optional, because
consecutive contributions from a clustered process are serially correlated — the
same ten Poisson tapes are all refused and five self-exciting tapes are all
adopted with statistics around 9.

The general lesson is one this codebase keeps relearning, first in Phase 3's
anomaly threshold: **the threshold does not belong on the raw difference.** It
belongs on the difference standardised by the size of the thing that could
account for it.

### A constraint found a bug the tests did not

`ck_queue_estimate_is_a_bracket` requires the optimistic end of a queue outlook
to be at least as likely to fill as the pessimistic one. It failed on the first
integration run.

The cause was real. Each end had been given its own mean departure size —
trades only for the pessimistic end, trades and cancellations for the optimistic
one — and on a level where five small cancellations accompanied one large trade,
the optimistic end had a *smaller* mean size, therefore needed *more* departure
events, and came out less likely to fill. The two ends were not a bracket at
all; they were two unrelated numbers.

Both ends now consume the queue in one shared size unit, so the optimistic
departure stream containing the pessimistic one is enough to make the ordering
hold by construction. The invariant is asserted on the object as well, in
`QueueOutlook.__post_init__`, following `Schedule.__post_init__` from Phase 8.

This is the third time a database CHECK written to express a documented rule has
caught a live defect rather than merely documenting one, and it is the argument
for writing them.
