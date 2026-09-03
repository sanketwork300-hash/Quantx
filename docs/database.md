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

### Phase 4 tables (implemented)

```
portfolios                          positions
  id (PK), user_id (FK)               id (PK), portfolio_id (FK)
  name, base_currency                 instrument_id (FK)
  description                        quantity   <- signed; negative is short
       |                             side       <- as supplied, for audit
       |                             average_price, source, strategy_tag
       |                             position_metadata
       |                             CHECK(quantity <> 0)
       v
portfolio_valuations                position_valuations
  id (PK), portfolio_id (FK)          id (PK), valuation_id (FK)
  user_id (FK), as_of_timestamp       position_id, instrument_id (FK)
  base_currency                       canonical_key, asset_class
  market_state_id  <- the snapshot    expiry, strike, option_type
  positions, valued                   quantity, multiplier, currency
  base_market_value                   market_price  <- observed
  unrealized_pnl                      model_price   <- estimated
  gross_exposure, net_exposure        price_used, valuation_method
  delta, gamma                        market_value, base_market_value
  vega_per_vol_point                  fx_rate  <- from the same snapshot
  theta_per_day, rho_per_bp           unrealized_pnl
  valuation_methods  <- counts        delta, gamma, vega_per_vol_point
  aggregates         <- by dimension  theta_per_day, rho_per_bp
  provenance                          greek_source, implied_volatility
  CHECK(valued <= positions)          time_to_expiry, quote_age_seconds
                                      warnings
                                      CHECK(base_market_value IS NOT NULL
                                            OR valuation_method='UNAVAILABLE')
```

`market_price` and `model_price` are separate columns and neither is ever
written from the other. There is deliberately no single `price` column that
could hold either: a schema that permits the substitution is a schema where it
eventually happens. The `CHECK` on `position_valuations` enforces the other half
of the rule — a row without a value must say `UNAVAILABLE`, so an unpriced
position can never be silently recorded as worth nothing.

Valuations are append-only. Revaluing a portfolio inserts a new row rather than
updating the last one, because a risk number from yesterday is evidence about
yesterday and overwriting it destroys the only record of what was reported.

### Phase 5 tables (implemented)

```
stress_scenarios                    risk_snapshots
  id (PK), user_id (FK)               id (PK), user_id (FK)
  name, description                   portfolio_id (FK)
  source  <- HYPOTHETICAL |            valuation_id (FK) -> portfolio_valuations
             USER_DEFINED |            as_of_timestamp, base_currency
             DERIVED_FROM_HISTORY      market_state_id
  shocks       <- [{kind, type,        positions, excluded_positions
                    value, target}]    excluded  <- each with its reason
  derivation   <- series, date         base_value      <- model, at the anchors
                  range, event date    reported_value  <- what it is marked at
  scenario_metadata                    delta, gamma, vega_per_vol_point
  UQ(user_id, name)                    theta_per_day, rho_per_bp
  CHECK(source <> 'DERIVED_FROM_       provenance
        HISTORY' OR derivation              |
        IS NOT NULL)                        |
       |                                    +-------------------+
       |                                    |                   |
       v                                    v                   v
  the constraint is the rule:        var_results          stress_results
  a scenario that claims to            id (PK)              id (PK)
  come from data must carry            user_id (FK)         user_id (FK)
  the data it came from                portfolio_id (FK)    portfolio_id (FK)
                                       snapshot_id (FK)     snapshot_id (FK)
                                       method               scenario_id (FK, null
                                       horizon_days           for a template)
                                       scenarios            scenario_name/_source
                                       base_value           shocks <- as resolved
                                       seed  <- MC only     base_value
                                       tail_risk            shocked_value
                                       estimate_intervals   pnl  <- full repricing
                                       assumptions          greek_estimate <- the
                                       factor_panel                 linear estimate,
                                       warnings                     stored beside it
                                       provenance           time_decay_days
                                       CHECK(scenarios>=0)  floored_volatilities
                                       CHECK(method <>      contributions, positions
                                         'MONTE_CARLO'      warnings, provenance
                                         OR seed IS NOT
                                         NULL)
```

Three constraints carry rules that would otherwise live only in prose.

`ck_scenario_historical_claim_has_derivation` makes it impossible to store a
scenario labelled `DERIVED_FROM_HISTORY` without the series, date range and
event date behind it. Naming a row "COVID crash" and putting a round -35% in it
is exactly the fabrication the platform exists not to do, and the schema refuses
it rather than trusting the service layer to.

`ck_var_monte_carlo_records_its_seed` makes an unreproducible Monte Carlo result
unstorable. A simulated risk number whose seed was not written down cannot be
recomputed, and a number that cannot be recomputed is not evidence.

`base_value` and `reported_value` are separate columns on `risk_snapshots` for
the same reason `market_price` and `model_price` are separate on
`position_valuations`: the gap between the model at today's anchors and what the
book is marked at is a fact about the book, not an error to reconcile away.

Every risk row points at the `portfolio_valuations` row it measured, so the
chain from a VaR number back to the individual quotes is a sequence of foreign
keys rather than an assertion. Results are append-only; a rerun inserts.

### Phase 6 tables (implemented)

```
margin_results
  id (PK), user_id (FK), portfolio_id (FK)
  snapshot_id (FK) -> risk_snapshots
  method, model_version   <- which model, at which version, produced this
  currency, estimated_margin, confidence
  eligible_capital  <- user-supplied; null means unknown
  buffer            <- null unless capital is known
  utilisation       <- null unless capital is known
  in_shortfall_at_rest, vol_co_shock
  worst_spot_return, worst_vol_points, worst_loss, worst_at_grid_edge
  positions, excluded_positions
  summary        <- the sentence the user was shown, stored verbatim
  components, assumptions, parameters, shortfall_region, ladder
  warnings, provenance
  CHECK(estimated_margin >= 0)
  CHECK(confidence BETWEEN 0 AND 1)
  CHECK(eligible_capital IS NOT NULL
        OR (buffer IS NULL AND utilisation IS NULL))
```

Note the columns that are **not** here: nothing called `required_margin`,
nothing naming a broker or a venue, and no liquidation price. The schema is the
first place a claim about broker equivalence could creep in, so it is the first
place it is refused.

`ck_margin_buffer_requires_capital` is the same idea as
`ck_position_valuation_has_value_or_reason` one phase earlier: a derived number
cannot be stored without the input it was derived from. Defaulting capital to
portfolio value would produce a confident buffer about a quantity nobody
supplied.

`summary` is stored rather than regenerated because it is what the user was
actually told. Newer code producing a different sentence from the same row would
make the record of that unrecoverable.

### Phase 7 tables (implemented)

```
executions                          execution_reports
  id (PK), user_id (FK)               id (PK), user_id (FK)
  instrument_id (FK)                  instrument_id (FK)
  upload_id (FK, nullable)            parent_order_key
  side       <- direction lives here  grouping_method      <- EXPLICIT or
  quantity   <- always positive       grouping_is_inferred    INFERRED_BY_TIME
  execution_price                     side, canonical_key
  exchange_timestamp                  currency, multiplier
  receive_timestamp                   fills, filled_quantity
  order_id                            order_quantity  <- null = not stated
  parent_order_key  <- null means     average_price, fees
                       the grouping   window_start, window_end
                       was inferred   primary_benchmark
  order_type, limit_price             primary_benchmark_price
  order_quantity                      shortfall_currency / _bps / _percent
  submit_timestamp                    observations
  decision_timestamp                  coverage_span_ratio
  broker, venue, fees                 coverage_is_sufficient
  source, execution_metadata          benchmarks, shortfalls
  CHECK(quantity > 0)                 unavailable_shortfalls
  CHECK(execution_price >= 0)         decomposition, market_window
  CHECK(fees >= 0)                    warnings, provenance
  CHECK(submit_timestamp IS NULL      CHECK(filled_quantity > 0)
        OR submit_timestamp <=        CHECK(window_end >= window_start)
           exchange_timestamp)        CHECK((primary_benchmark_price IS NULL)
                                            = (shortfall_currency IS NULL))
```

`executions` is **append-only**. There is no update method on the repository and
no correction column: a corrected fill is a new row and both stay. A trade log
that can be quietly rewritten cannot support a cost analysis anyone should act
on.

Quantity is positive here and signed on `positions`, deliberately. A position's
sign says which way you are exposed; a fill's side says what you did. Merging the
two conventions would create a second place for a sign to drift.

Two constraints carry rules that would otherwise live only in prose.

`ck_execution_submit_not_after_fill` refuses a fill timestamped before its own
submission. The pair is the basis of every arrival benchmark, so an impossible
ordering is rejected at the boundary rather than producing a negative delay
downstream.

`ck_report_shortfall_needs_benchmark` makes a shortfall unstorable without the
benchmark price it was measured against, in either direction. A cost with no
benchmark is a number with no meaning, and "no benchmark was available" must not
be able to render as a cost of zero.

`grouping_is_inferred` is denormalised onto the summary row rather than left
inside the JSON, because it changes what every other number on the row means and
a reader scanning a list has to see it.

### Phase 8 tables (implemented)

```
execution_simulations
  id (PK), user_id (FK), instrument_id (FK)
  comparison_id      <- groups the rows of one strategy comparison
  counterfactual     <- always true, and CHECK(counterfactual) makes
                        anything else unstorable
  strategy, impact_model
  impact_is_calibrated  <- false while the coefficients are the identity
  side, ordered_quantity, filled_quantity, completion_rate
  average_price      <- null when nothing filled
  window_start, window_end
  latency_seconds, max_price_age_seconds
  modelled_impact_cost, modelled_spread_cost
  primary_benchmark, shortfall_currency, shortfall_bps
  schedule, context, fills, unfilled, benchmarks
  warnings, provenance
  CHECK(counterfactual)
  CHECK(ordered_quantity > 0)
  CHECK(filled_quantity BETWEEN 0 AND ordered_quantity)
  CHECK(completion_rate BETWEEN 0 AND 1)
  CHECK(window_end >= window_start)
  CHECK((primary_benchmark IS NULL) = (shortfall_currency IS NULL))
```

The `counterfactual` column exists **only to be constrained**. It is `True` on
every row and `ck_simulation_is_always_counterfactual` forbids anything else, so
a simulated result cannot be stored without the label that says it never
happened — not by a future refactor, not by a bulk insert, not by hand. It is
the same device as `ck_scenario_historical_claim_has_derivation` in Phase 5 and
`ck_margin_buffer_requires_capital` in Phase 6: a rule that matters gets an
enforcement point the code cannot walk past.

Simulated fills live in this row's `fills` JSON and are deliberately **not**
written to `executions`. That table is what happened.

`docs/database.md` originally planned to put the run detail in the object store.
It is stored inline instead, because the slice count is bounded by the request
(at most 200 intervals) so the payload is small, and keeping it in the row means
a stored counterfactual can be read back with one query rather than two systems
having to agree.

### Phase 9 tables (implemented)

```
global_surfaces
  id (PK), surface_id, user_id (FK), analysis_id (FK), underlying_id (FK)
  as_of_timestamp, model, model_version, curve_id, status
  rho, eta, gamma          <- three parameters for the WHOLE surface
  n_observations, n_slices
  rmse_total_variance, weighted_rmse, rmse_vol_points, max_error_vol_points
  optimizer, optimizer_message, iterations
  starts_attempted, starts_feasible
  min_durrleman_g          <- the actual butterfly condition, numerically
  max_butterfly_quantity, butterfly_bounds_satisfied
                           <- Theorem 4.2's *sufficient* condition
  calendar_arbitrage_free  <- structural for SSVI, not a diagnostic that passed
  error, calibration_timestamp, provenance
  CHECK(status <> 'CONVERGED' OR (rho, eta, gamma all present))
  CHECK(status <> 'CONVERGED'
        OR (calendar_arbitrage_free AND min_durrleman_g >= -1e-9))

global_surface_slices
  id (PK), global_surface_id (FK)
  expiry, time_to_expiry, forward, discount_factor
  forward_method, forward_confidence
                           <- the Phase 1 estimate's own provenance; a surface
                              that forgot it would flag every reference value
                              it produced as LOW_CONFIDENCE_FORWARD
  theta, atm_volatility    <- this expiry's knot in the variance term structure
  n_observations, rmse_vol_points, max_error_vol_points, k_min, k_max
  butterfly_first, butterfly_second, butterfly_bounds_satisfied
  min_durrleman_g
  UNIQUE(global_surface_id, expiry)
  CHECK(theta > 0)

local_volatility_surfaces
  id (PK), user_id (FK), global_surface_id (FK), underlying_id (FK)
  as_of_timestamp, model_version, spot, carry
  total_points, valid_points, flagged_points, coverage
  flag_counts              <- flag name -> count, so a hole is attributable
  grid                     <- axes, per-point detail, and a matrix with nulls
  provenance
  CHECK(total_points = valid_points + flagged_points)
  CHECK(coverage BETWEEN 0 AND 1)

risk_neutral_densities
  id (PK), user_id (FK), global_surface_id (FK), underlying_id (FK)
  expiry, time_to_expiry, forward, discount_factor
  total_mass, implied_mean, negative_mass, mean_error, is_admissible, flags
  percentile_5, percentile_25, percentile_50, percentile_75, percentile_95
  strikes, density, provenance
  UNIQUE(global_surface_id, expiry)
  CHECK(is_admissible OR every percentile IS NULL)

heston_calibrations
  id (PK), user_id (FK), analysis_id (FK), underlying_id (FK)
  as_of_timestamp, model_version, status
  v0, kappa, theta, xi, rho
  n_observations, n_maturities
  rmse_price, rmse_vol_points, max_error_vol_points
  optimizer, optimizer_message, iterations, starts_attempted, starts_feasible
  feller, satisfies_feller, feller_enforced
  warnings, error, provenance
  CHECK(status <> 'CONVERGED' OR (all five parameters present))
  CHECK(NOT feller_enforced OR feller >= -1e-9)

model_consensus_runs
  id (PK), user_id (FK), global_surface_id (FK), heston_calibration_id (FK)
  instrument_id (FK), underlying_id (FK), as_of_timestamp, model_version
  expiry, strike, option_type
  spot, time_to_expiry, risk_free_rate, dividend_yield, reference_volatility
  models_requested, models_available
  reference_value          <- the median, bracketed below
  reference_low, reference_high
  dispersion_absolute, dispersion_relative, standard_deviation
  market_price             <- an observation, never written from a model
  market_deviation, market_deviation_relative
  confidence, confidence_contributions
  vanna, volga, charm_per_day
  seed, paths, grid, warnings, provenance
  CHECK((reference_value IS NULL) = (models_available = 0))
  CHECK(reference_value IS NULL
        OR reference_value BETWEEN reference_low AND reference_high)
  CHECK(models_available <= models_requested)

model_values
  id (PK), consensus_id (FK)
  model, model_version, value, method
  inputs_used, diagnostics, warnings, unavailable_reason
  UNIQUE(consensus_id, model)
  CHECK((value IS NOT NULL AND unavailable_reason IS NULL)
        OR (value IS NULL AND unavailable_reason IS NOT NULL))
```

Four of these constraints are the phase's rules made unenforceable-around:

* `ck_converged_global_surface_is_arbitrage_free` — SSVI's entire claim over
  per-expiry SVI is that an admissible fit is free of both arbitrages by
  construction. A row calling itself `CONVERGED` while carrying a negative
  density or a decreasing variance term structure would be that claim quietly
  withdrawn.
* `ck_model_value_has_value_or_reason` — a model that could not run is a row
  with a reason, not a missing row. A consensus over three models because the
  fourth failed and a consensus over three because only three were asked for are
  different results, and only stored unavailability tells them apart.
* `ck_consensus_reference_is_bracketed` — the median cannot float free of the
  evidence it was drawn from.
* `ck_density_quantiles_require_admissibility` — a quantile normalises by the
  mass it found, so on a truncated or negative density it would be a plausible
  number with no meaning.

`global_surfaces` is a separate table from `volatility_surfaces` rather than a
new `model` value on it. The two have different shapes and different guarantees,
and overloading one table would have made the second guarantee invisible.
`heston_calibrations` is separate for the same reason: SSVI and Heston are two
descriptions of the same market fitted to the same quotes, and the point of the
consensus is that they disagree — storing one inside the other's row would
suggest a hierarchy the platform does not assert.

### Planned tables (phases 10-11)

| Table | Phase | Notes |
| --- | --- | --- |
| `market_bars` | 1 | hypertable, `(instrument_id, interval, exchange_timestamp)` — deferred with the bars endpoint |

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
