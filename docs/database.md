# Database Design — ERD and storage conventions

Status: schema for Phase 0 is **implemented** (`migrations/`). Tables marked
_(planned)_ are designed here so foreign keys and identity choices are settled
before the phases that need them.

---

## 1. Conventions

- Primary keys are `UUID`. Instrument ids are **deterministic UUIDv5** derived
  from the canonical instrument key, so the same contract gets the same id in
  every environment and every import — essential for reproducibility (§1.4).
- All timestamps are `TIMESTAMPTZ`, stored UTC. Columns are named
  `*_timestamp` for event time and `*_at` for row lifecycle time.
- Money, prices, quantities and multipliers are `NUMERIC(28, 10)` and map to
  Python `Decimal`. Model outputs (IV, Greeks, RMSE) are `DOUBLE PRECISION` —
  see `docs/methodology.md` §"Numerical precision" for why the split.
- Observation tables are **append-only**. Corrections insert a new row.
- Analytical result tables carry `provenance JSONB NOT NULL`.
- Every user-owned table carries `user_id` and is queried through an
  ownership-scoped repository.

## 2. Entity relationship diagram

```
                         +----------------+
                         |     users      |
                         +----------------+
                         | id (PK)        |
                         | email (UQ)     |
                         | password_hash  |
                         | is_active      |
                         | created_at     |
                         +----------------+
                            |   |   |   |
        +-------------------+   |   |   +--------------------+
        |                       |   |                        |
        v                       v   v                        v
+----------------+   +----------------+            +----------------+
|   portfolios   |   |      jobs      |            |   audit_logs   |
+----------------+   +----------------+            +----------------+
| id (PK)        |   | id (PK)        |            | id (PK)        |
| user_id (FK)   |   | user_id (FK)   |            | user_id (FK)   |
| name           |   | job_type       |            | action         |
| base_currency  |   | status         |            | resource_type  |
| created_at     |   | progress       |            | resource_id    |
| updated_at     |   | input_reference|            | ip_address     |
+----------------+   | result_reference           | metadata JSONB |
        |            | error JSONB    |            | created_at     |
        |            | created_at     |            +----------------+
        |            | started_at     |
        |            | completed_at   |
        |            +----------------+
        v
+------------------+
|    positions     |
+------------------+
| id (PK)          |
| portfolio_id (FK)|
| instrument_id(FK)|
| quantity NUMERIC |
| average_price    |
| side             |
| source           |
| metadata JSONB   |
+------------------+
        |
        v
+----------------------------------------------------------+
|                      instruments                          |
+----------------------------------------------------------+
| id (PK, UUIDv5 of canonical_key)                          |
| canonical_key (UQ)   e.g. NSE:OPT:NIFTY:2026-09-24:24000:C|
| asset_class          EQUITY|INDEX|FUTURE|OPTION|FX|       |
|                      CRYPTO_SPOT|CRYPTO_PERPETUAL         |
| exchange, venue, symbol                                   |
| underlying_id (FK -> instruments.id, nullable)            |
| currency, multiplier, tick_size, lot_size                 |
| expiry (date, null), strike (numeric, null)               |
| option_type (C|P|null), exercise_style, settlement_type   |
| status, metadata JSONB, created_at, updated_at            |
+----------------------------------------------------------+
        ^                    ^                    ^
        |                    |                    |
+------------------+  +-----------------+  +------------------+
|instrument_aliases|  |  market_quotes  |  |  option_quotes   |
+------------------+  +-----------------+  +------------------+
| id (PK)          |  | (hypertable)    |  | (hypertable)     |
| instrument_id FK |  | instrument_id FK|  | instrument_id FK |
| source           |  | exchange_ts     |  | chain_snapshot FK|
| alias_symbol     |  | receive_ts      |  | exchange_ts      |
| UQ(source,alias) |  | bid_price/size  |  | bid/ask/last     |
+------------------+  | ask_price/size  |  | volume, oi       |
                      | last_price/size |  | underlying_price |
                      | volume, oi      |  | quality JSONB    |
                      | source, seq_no  |  | excluded bool    |
                      +-----------------+  | exclusion_reason |
                                           +------------------+
                                                    ^
                                                    |
                                        +-------------------------+
                                        | option_chain_snapshots  |
                                        +-------------------------+
                                        | id (PK)                 |
                                        | user_id (FK)            |
                                        | underlying_id (FK)      |
                                        | as_of_timestamp         |
                                        | source, provider        |
                                        | dataset_digest (sha256) |
                                        | upload_id (FK, null)    |
                                        | n_rows_in/kept/excluded |
                                        | quality_summary JSONB   |
                                        | created_at              |
                                        +-------------------------+
                                                    ^
                                                    |
                                        +-------------------------+
                                        |        uploads          |
                                        +-------------------------+
                                        | id (PK)                 |
                                        | user_id (FK)            |
                                        | kind  OPTION_CHAIN|     |
                                        |       POSITIONS|TRADES  |
                                        | original_filename       |
                                        | stored_key (objstore)   |
                                        | content_type, byte_size |
                                        | sha256                  |
                                        | status, error JSONB     |
                                        | created_at              |
                                        +-------------------------+

+-------------------------+   +--------------------------+
| data_quality_reports    |   |     model_versions       |
+-------------------------+   +--------------------------+
| id (PK)                 |   | id (PK)                  |
| scope_type              |   | model_name, version (UQ) |
| scope_id                |   | model_type               |
| stale_score             |   | parameters JSONB         |
| spread_score            |   | training_period_start/end|
| liquidity_score         |   | calibration_timestamp    |
| consistency_score       |   | code_commit              |
| completeness_score      |   | metrics JSONB            |
| overall_score           |   | created_at               |
| flags JSONB             |   +--------------------------+
| provenance JSONB        |
| created_at              |
+-------------------------+
```

### Phase 1 tables (implemented)

```
+------------------------+        +---------------------------+
|     yield_curves       |        |     chain_analyses        |
+------------------------+        +---------------------------+
| id (PK)                |        | id (PK)                   |
| curve_id (UQ)          |<-------| curve_id                  |
|   content-addressed:   |        | user_id (FK)              |
|   same numbers ->      |        | chain_snapshot_id (FK)    |
|   same id              |        | underlying_id (FK)        |
| as_of_timestamp        |        | as_of_timestamp           |
| currency, source       |        | day_count                 |
| day_count              |        | settlement_time_utc       |
| interpolation          |        | underlying_price          |
| points JSONB           |        | quotes_in / quotes_solved |
+------------------------+        | expiries                  |
                                  | summary JSONB             |
                                  | provenance JSONB          |
                                  +---------------------------+
                                       |                 |
                     +-----------------+                 +--------------+
                     v                                                  v
        +---------------------------+                  +-----------------------------+
        |    forward_estimates      |                  |    option_implied_vols      |
        +---------------------------+                  +-----------------------------+
        | id (PK)                   |                  | id (PK)                     |
        | analysis_id (FK)          |                  | analysis_id (FK)            |
        | expiry, method            |                  | instrument_id (FK)          |
        | selected  <- every method |                  | expiry, strike, option_type |
        |   attempted is stored,    |                  | price_used, price_source    |
        |   not only the winner     |                  | market_iv                   |
        | value, confidence         |                  |   <- implied by an OBSERVED |
        | observations              |                  |      price; the fitted      |
        | residual_error            |                  |      reference_iv is a      |
        | discount_factor           |                  |      Phase 2 column in a    |
        | time_to_expiry            |                  |      Phase 2 table          |
        | error, assumptions JSONB  |                  | market_iv_bid / _ask        |
        | UQ(analysis, expiry,      |                  | converged, iterations       |
        |    method)                |                  | solver, error               |
        +---------------------------+                  | vega, uncertainty           |
                                                       | log_moneyness               |
                                                       | total_variance, weight      |
                                                       | used_for_smile              |
                                                       | smile_exclusion             |
                                                       | CHECK(market_iv NOT NULL    |
                                                       |       OR error NOT NULL)    |
                                                       +-----------------------------+
```

Two things this schema is doing deliberately:

* **Implied volatility is not a column on `option_quotes`.** That table is an
  append-only record of what the market showed; this one holds what a *model*
  implied from it under a stated forward, curve and day count. Re-running with a
  different curve writes new rows here and touches nothing observed.
* **A CHECK constraint enforces "a value or a reason"**, so a silent hole in the
  surface is a database error rather than a plausible-looking gap.

### Phase 2 tables (implemented)

```
volatility_surfaces          surface_slices                surface_parameters
  id (PK)                      id (PK)                       id (PK)
  surface_id  <- content-      surface_id (FK)               slice_id (FK)
    addressed; two rows with   expiry, time_to_expiry        parameterization
    this id were fitted from   forward, discount_factor      a, b, rho, m, sigma
    the same numbers          forward_method/confidence      UQ(slice, param'n)
  user_id (FK)                 status, n_observations
  analysis_id (FK)             rmse_total_variance             ^ the five numbers
  underlying_id (FK)           weighted_rmse                     alone, because a
  as_of_timestamp              rmse_vol_points                   reference IV must
  model, model_version         max_error_vol_points              depend on these
  curve_id                     optimizer, message, iterations    plus the forward
  slices_total / _fitted       starts_attempted / _feasible      and maturity and
  calibration_timestamp        min_durrleman_g, min_..._k        nothing else
  summary, provenance          wing_slope
  CHECK(fitted <= total)       constraints_satisfied
                               k_min, k_max  <- fitted range
                               UQ(surface_id, expiry)

arbitrage_reports                      arbitrage_violations
  id (PK)                                id (PK)
  surface_id (FK), analysis_id (FK)      report_id (FK)
  user_id (FK)                           scope, violation_type, severity
  scope  RAW_MARKET|FITTED_SURFACE       magnitude   <- in the condition's
  severity, violations_total                            own units
  observations, checks_run               tolerance   <- what it was judged
  summary                                                against, same units
  UQ(surface_id, scope)                  expiry, strike, option_type
                                         detail, affected_instruments
```

Two schema decisions doing real work:

* **`UNIQUE(surface_id, scope)`, not `(analysis_id, scope)`.** An analysis can
  legitimately be recalibrated — a different seed, a later code version — and
  keeping the earlier reports is how you see whether a refit fixed a violation
  or merely moved it. The first version of this constraint made recalibration
  impossible, which a test caught.
* **Parameters in their own table.** The contract "a stored surface reproduces
  its reference IVs from five numbers" is legible in the schema, not only in
  prose, and the reproduction test reads the same way the code does.

### Phase 3 tables (implemented)

```
surface_characteristics              anomaly_scans
  id (PK)                              id (PK)
  surface_id (FK), user_id (FK)        user_id, surface_id, analysis_id (FK)
  underlying_id, as_of_timestamp       underlying_id, as_of_timestamp
  tenor_days   <- 7/30/60/90/180/365   quotes_examined / _scored / flagged
  time_to_expiry, forward              policy   <- the detection policy,
  atm_volatility, skew, curvature                  recorded because it
  atm_total_variance                               decides the answer
  method, flags                        provenance
  UQ(surface_id, tenor_days)           CHECK(flagged <= quotes_scored)
       |                                     |
       |  recorded at fixed tenors           v
       |  because expiries roll        surface_anomalies
       v                                 id (PK), scan_id (FK)
  a time series of "the 30-day          instrument_id (FK)
  level" outlives any one expiry        expiry, strike, option_type
                                        market_iv     <- observed
                                        reference_iv  <- model output
                                        iv_difference, relative_deviation
                                        market_iv_bid / _ask
                                        envelope_position, excess_over_envelope
                                        explained_scale, z_score
                                        historical_z_score, historical_observations
                                        liquidity_score, data_quality_score
                                        calibration_rmse_vol_points, iv_uncertainty
                                        reference_method, reference_flags
                                        confidence, flagged
                                        explanation  <- grounded reasons, each
                                                        naming its measurement
                                        CHECK(flagged = false OR confidence > 0)
```

Note what `surface_anomalies` has no column for: action, direction, rating,
target. The absence is the point, and a test asserts it.

**Every scored quote is stored, not only the flagged ones.** The unflagged rows
are the evidence the threshold was doing something, and they are the history a
later scan's time-series z-score is measured against.

### Planned tables (phases 4-11)

| Table | Phase | Notes |
| --- | --- | --- |
| `market_bars` | 1 | hypertable, `(instrument_id, interval, exchange_timestamp)` — deferred with the bars endpoint |
| `pricing_results` | 9 | one row per (contract, model, market_state) |
| `risk_snapshots` | 5 | portfolio-level, timestamped |
| `var_results` | 5 | method, confidence, horizon, lookback, n_obs |
| `stress_scenarios`, `stress_results` | 5 | scenario definition reusable across portfolios |
| `margin_results` | 6 | method, assumptions, estimate, confidence, warnings |
| `orders`, `executions` | 7 | executions append-only, parent/child linkage |
| `execution_reports` | 7 | TCA output per parent order |
| `execution_simulations` | 8 | counterfactual runs, result in object store |

## 3. Indexing

| Table | Index | Purpose |
| --- | --- | --- |
| `instruments` | `UQ(canonical_key)` | identity |
| `instruments` | `(underlying_id, expiry, strike, option_type)` | chain assembly |
| `instrument_aliases` | `UQ(source, alias_symbol)` | resolution |
| `market_quotes` | hypertable on `exchange_timestamp`, index `(instrument_id, exchange_timestamp DESC)` | latest + range |
| `option_quotes` | `(chain_snapshot_id, strike, option_type)` | chain read |
| `option_chain_snapshots` | `(underlying_id, as_of_timestamp DESC)` | history |
| `jobs` | `(user_id, status, created_at DESC)` | polling |
| `audit_logs` | `(user_id, created_at DESC)` | review |

## 4. TimescaleDB usage

`market_quotes`, `option_quotes` and (later) `market_bars` and `risk_snapshots`
are hypertables partitioned by time with a 7-day chunk interval, plus compression
after 30 days. The migration degrades gracefully: if the `timescaledb` extension
is unavailable (e.g. plain Postgres in CI), the tables are created as ordinary
tables with the same indexes and every query still works. Nothing in the
application depends on Timescale-specific SQL.

## 5. What does *not* go in Postgres

Per build spec §67: no L2 order-book histories, no tick tapes, no Monte Carlo
paths, no raw uploaded files. Those live in the object store as Parquet with a
metadata row in Postgres holding the key, schema version, row count and digest.

The rule of thumb used here: if a dataset grows with market activity rather than
with user activity, it belongs in the object store.
