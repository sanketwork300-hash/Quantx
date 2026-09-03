# Implementation Backlog and Acceptance Criteria

One phase at a time. A phase is not started until the previous phase's
acceptance criteria pass in CI. Each phase is a **vertical slice**: data model,
service, API, tests, docs, UI where applicable.

Legend: `[x]` shipped · `[ ]` planned

---

## Phase 0 — Foundation  `[x]`

- [x] Repository skeleton, layering rules + CI layering check
- [x] Docker Compose: Postgres, Redis, MinIO, API, worker, scheduler, web
- [x] Settings (`pydantic-settings`), structured logging, correlation ids
- [x] Async SQLAlchemy session management, Alembic baseline migration
- [x] Redis cache client; object store abstraction (local FS + S3 interface)
- [x] Celery app + eager execution mode for tests/dev
- [x] Users: register, login, bcrypt, JWT, `/auth/me`, ownership dependency
- [x] Audit log
- [x] Instruments: canonical key, uuid5 identity, invariants, aliases, resolver
- [x] Market data: provider ABC, `CSVMarketDataProvider`, `SyntheticMarketDataProvider`
- [x] Canonical `Quote` / `OptionQuote` / `Bar` / `OrderBookSnapshot` / `Trade`
- [x] Data-quality engine (checks, five sub-scores, flags, exclusion policy)
- [x] Option-chain ingestion pipeline: upload -> validate -> normalize -> quality -> persist -> retrieve
- [x] Jobs: model, service, task, status/progress/result API
- [x] Test infrastructure: unit, integration, quant-validation, regression harness

**Acceptance**

| Criterion | How it is verified |
| --- | --- |
| A user can register, log in and call an authenticated route | `tests/integration/test_auth.py` |
| A foreign portfolio/upload id returns 404, not 403 | `tests/integration/test_ownership.py` |
| The instrument master round-trips and is idempotent under re-import | `tests/unit/test_instrument_identity.py` |
| The same contract yields the same UUID across processes | deterministic uuid5 test |
| An option chain CSV can be uploaded, previewed, ingested and retrieved | `tests/integration/test_option_chain_ingestion.py` |
| **Every excluded quote has a non-null reason** | asserted over the bad-quote fixture |
| A job runs asynchronously and reports terminal status | `tests/integration/test_jobs.py` |
| Synthetic provider produces an arbitrage-clean chain | `tests/quant_validation/test_synthetic_provider.py` |
| `quant/` imports nothing from `domains/` or `infrastructure/` | `scripts/check_layering.py` |

## Phase 1 — Options MVP  `[x]`

- [x] Day-count conventions (ACT/365F, ACT/360, ACT/365.25, 30/360) and an
      explicit time-to-expiry policy
- [x] `YieldCurve`: flat and piecewise-linear-in-zero-rate, content-addressed id
- [x] `ForwardEstimator`: spot-carry, futures-derived, put-call-parity regression
- [x] Black-76 (price, vega, bounds) and Black-Scholes-Merton (price, Greeks)
- [x] Analytic Greeks with explicit units; raw partials retained alongside
- [x] IV engine: vectorized safeguarded Newton with a bracketed Brent fallback,
      structured non-results, and a **reported conditioning** (vega and the
      volatility uncertainty implied by one price ulp)
- [x] Raw smile construction in `(k, w)` with ATM level, skew and curvature
- [x] Bid/ask implied-volatility envelope
- [x] Chain analysis as a job; `chain_analyses`, `forward_estimates`,
      `option_implied_vols`, `yield_curves`
- [x] API: `/derivatives/iv`, `/derivatives/greeks`, `/derivatives/forward`,
      `/derivatives/chains/{id}/analyze`, `/derivatives/chains/{id}/smile`,
      `/derivatives/analyses`
- [x] UI: smile chart with the IV envelope, per-expiry forward panel, and a
      conditioning inspector
- [ ] `MarketState` builder and `/market/state` — deferred to Phase 2, where the
      surface is the first consumer that genuinely needs a frozen snapshot
      rather than a single chain

**Acceptance — verified**

| Criterion | Where |
| --- | --- |
| price -> solve IV -> sigma recovered to 1e-6 over a wide grid | `tests/quant_validation/test_implied_vol.py::TestRoundTrip` (700 cases; ill-conditioned quotes bounded by their reported uncertainty instead, which is the honest form of the criterion) |
| Agreement with `vollib` and QuantLib | `test_black_scholes.py::TestAgainstReferenceLibraries`, `test_implied_vol.py::TestAgainstVollib` |
| Every Greek matches central finite differences | `test_black_scholes.py::TestGreeksAgainstFiniteDifferences` |
| Put-call parity holds within tolerance | `test_black_scholes.py::TestIdentities` |
| Every quote has an IV or a structured reason | `test_chain_analysis.py::TestCompleteness` |
| Forward estimates carry method, confidence, observations, residual | `tests/unit/test_forward_estimator.py` |
| The generating surface is recovered end to end from tick-rounded quotes | `test_chain_analysis.py::TestVolatilityRecovery` |

## Phase 2 — Volatility surface  `[x]`

- [x] Raw SVI calibration: constrained SLSQP, deterministic multi-start
- [x] No-arbitrage conditions as **optimizer constraints**, not post-hoc checks:
      non-negative minimum variance, Lee's wing bound, and Durrleman's
      `g(k) >= 0` on a grid
- [x] Liquidity/spread weighting carried through from the quality engine
- [x] `ArbitrageValidator`: bounds, parity, vertical, butterfly, calendar
- [x] Raw-market and fitted-surface violations reported and stored separately
- [x] Surface persistence (`volatility_surfaces` / `surface_slices` /
      `surface_parameters` / `arbitrage_reports` / `arbitrage_violations`)
- [x] Reference IV and reference price lookup, with EXACT / INTERPOLATED /
      EXTRAPOLATED methods and per-point flags
- [x] `MarketState` and `GET /market/state` (deferred here from Phase 1, where
      it had no consumer)
- [x] Economic **price resolution** in the IV solver: half a spread, or a tick
      for a locked market, instead of a float64 ulp
- [x] UI: observed-vs-fitted overlay, total variance, per-slice admissibility,
      and the two arbitrage scopes side by side

**Acceptance — verified**

| Criterion | Where |
| --- | --- |
| Fitted parameters satisfy the documented no-arbitrage constraints | `test_surface.py::TestCalibration::test_fitted_parameters_satisfy_the_no_arbitrage_constraints`, `test_svi_calibration.py::TestConstraints` |
| Calibration metrics stored and displayed | `test_surface.py::TestCalibration::test_calibration_metrics_are_recorded` |
| A stored surface reproduces its reference IVs from persisted parameters | `test_surface.py::TestSurfaceRetrieval`, `tests/unit/test_surface.py::TestReproducibility` |
| Violations visible and separated by scope | `test_surface.py::TestArbitrageReporting`, `test_surface_pipeline.py::TestArbitrageOnACorruptedMarket` |
| Each condition fires on a seeded violation and stays quiet on a clean chain | `tests/quant_validation/test_arbitrage_conditions.py` |
| The fitted surface reproduces the generating one in sample | `test_surface_pipeline.py::TestCalibrationQuality` |

**Findings worth carrying forward**

- SVI's five parameters are **not identifiable** from a narrow strike window.
  On a realistic retail chain spanning ~0.1 in log-moneyness the fitted curve is
  right to 0.005 volatility points in sample while the parameters miss the truth
  by 0.05, and the wings are essentially free. Surfaced as
  `SURFACE_NARROW_STRIKE_RANGE`.
- A deep out-of-the-money weekly is worth less than a tick, so venues quote it
  locked at the floor. Inverting that price is numerically clean and
  economically meaningless — and a dozen such quotes moved a fit by **104
  volatility points**. Fixed by making the solver's `uncertainty` measure price
  resolution rather than float precision, and dropping quotes above a threshold
  as `ILL_CONDITIONED`.
- One badly mispriced quote is enough to bend an unconstrained least-squares fit
  into a negative implied density. Durrleman's condition is therefore in the
  optimizer's feasible set, not checked afterwards.

## Phase 3 — Anomaly analytics  `[x]`

- [x] Analytic surface characteristics (ATM level, skew, curvature, total
      variance) from the fitted parameters
- [x] Characteristics recorded at **standard tenors** so surfaces stay
      comparable as expiries roll
- [x] Historical percentile and z-score analytics, with the observation count
      travelling with every answer
- [x] Deviation model: absolute, relative, and bid/ask-envelope aware
- [x] Confidence from data quality, liquidity, calibration error, measurement
      resolution, slice breadth and extrapolation
- [x] Grounded explanations — every line names the measurement behind it
- [x] Surface scanner as a job, with the detection policy recorded in provenance
- [x] Time-series z-score against a contract's own past deviations, once a
      second scan exists
- [x] UI: scanner table with an explanation panel, and a history panel

**Acceptance — verified**

| Criterion | Where |
| --- | --- |
| Every anomaly answers what deviated, by how much, relative to what, with what liquidity and what confidence | `test_anomalies.py::TestScanning::test_a_flagged_quote_answers_every_required_question` |
| Explanations are grounded in measurements, not narrative | `test_anomaly.py::TestExplanation`, `test_anomalies.py::test_the_explanation_is_grounded_in_measurements` |
| No output uses buy, sell, cheap, expensive, underpriced or arbitrage | `test_anomaly.py::TestLanguagePolicy`, `test_anomalies.py::TestLanguagePolicy` — asserted over the whole serialised response |
| The detector is quiet on a market that agrees with its own fit | `test_anomalies.py::test_a_clean_chain_flags_nothing` |
| The detector finds a quote nudged off the surface | `test_anomalies.py::test_the_perturbed_quote_is_found` |
| Percentiles carry their observation count and are marked unreliable when thin | `test_characteristics_and_history.py::TestTenorHistory` |

**Design notes**

- **The threshold is not on the volatility difference.** A fixed threshold in
  volatility points flags every illiquid wing quote and nothing else. A
  deviation is standardised by the combined size of the things that could
  account for it — the bid/ask width in volatility terms, the slice's
  calibration RMSE, and the numerical resolution of the inversion — all measured
  elsewhere in the platform for their own reasons.
- **A reference inside the quoted range is not an anomaly.** If the market's own
  two-sided quote spans the model value, the width of the market accounts for
  the whole difference and there is nothing to explain.
- **Every scored quote is stored, not only the flagged ones.** The rest is the
  evidence the threshold was doing something, and it is the history a later scan
  measures against.

**Deferred with a data gate: PCA on surface changes (build spec section 30).**
Factor loadings must be computed empirically and only described as level, skew
and curvature when the loadings support it. Running PCA on surfaces generated by
our own synthetic provider would produce loadings that describe the generator,
not a market — precisely the claim `docs/risks.md` R1 says synthetic data must
never be used to support. It ships when real historical surfaces exist.

## Phase 4 — Portfolio  `[x]`

- [x] Portfolio + position CRUD, ownership-scoped on every route
- [x] CSV import with column-mapping inference and a mandatory preview
- [x] Instrument resolution with `resolved / ambiguous / invalid` buckets
- [x] Position valuation with `market_price` and `model_price` in separate
      columns, and `valuation_method` naming which one was used
- [x] Per-position Greeks scaled once, by signed quantity times multiplier
- [x] Currency conversion at the rate in the same `MarketState` as the prices
- [x] Aggregation by underlying / expiry / asset class / strategy tag / currency
- [x] `VALUE_PORTFOLIO` and `IMPORT_POSITIONS` job handlers
- [x] UI: portfolio list, import wizard with the three buckets, valuation
      dashboard with Greeks by group

**Acceptance — verified**

| Criterion | Where |
| --- | --- |
| The sum over positions equals the portfolio total, for value and for every Greek | `test_portfolio_valuation.py::TestSumProperty` (hypothesis, arbitrary long/short mixes), `TestTotalsReconcile`, `test_portfolio.py::test_the_sum_over_positions_equals_the_portfolio_total` |
| Every aggregate dimension sums to the same portfolio total | `test_portfolio_valuation.py::test_every_aggregate_dimension_sums_to_the_portfolio_total`, `test_portfolio.py::test_each_grouping_sums_to_the_portfolio_total` |
| An ambiguous row is never auto-resolved | `test_position_import.py::TestAmbiguity` — no resolution, no instrument created, and the preview is not committable |
| A commit is refused while any row is ambiguous | `domains/portfolio/application.py::ImportRefused` |
| Every valuation records `valuation_method` | `test_portfolio_valuation.py::TestMethodIsAlwaysRecorded` over all five methods; `base_market_value is None` exactly when the method is `UNAVAILABLE` |
| Observations and model estimates stay in separate fields | `test_portfolio_valuation.py::TestObservationAndEstimateAreSeparate`, `test_portfolio.py::test_position_detail_keeps_observation_and_estimate_apart` |
| Nothing is dropped without a reason | `test_position_import.py::test_nothing_is_dropped_without_a_reason` — `input == resolved + ambiguous + invalid` |
| Every rejected row names its source row number and reason | `test_position_import.py::TestRejections`, `test_portfolio.py::test_every_rejected_row_names_its_row_number_and_reason` |
| One snapshot prices the whole portfolio | `test_portfolio.py::test_one_snapshot_priced_the_whole_portfolio` — the provenance and the result carry the same `market_state_id` |
| A portfolio route never serves another user's portfolio | `test_portfolio.py::TestPortfolioCrud` — 404, never 403 |
| No portfolio response contains advisory language | `test_portfolio.py::TestLanguage` — asserted over the whole serialised response |

**What is deliberately not here**: an ambiguity-resolution UI that lets a user
pick a candidate per row. The current behaviour is to refuse and say why, which
is correct but blunt; per-row selection is a Phase 5 refinement, not a gap in
the guarantee.

## Phase 5 — Risk  `[x]`

- [x] Historical VaR with full repricing for nonlinear books
- [x] Parametric VaR, with its invalidity for option books stated in the response
- [x] Monte Carlo VaR (job) with seed reproducibility and a bootstrap interval
- [x] Expected shortfall, always beside VaR, with the distinction spelled out
- [x] Scenario engine with four shock types; stress with full revaluation
- [x] The Greek approximation of the same move, reported beside it and labelled
- [x] Risk contribution by underlying / expiry / asset class / strategy tag
- [x] Factor histories assembled from ingested chains and calibrated surfaces,
      with an explicit alignment and missing-data policy
- [x] `RUN_VAR` and `RUN_STRESS` job handlers
- [x] UI: risk dashboard and stress lab, scenario library

**Acceptance — verified**

| Criterion | Where |
| --- | --- |
| Historical VaR recovers the analytic quantile on synthetic distributions | `test_var.py::TestHistoricalRecoversTheAnalyticQuantile` — normal at four confidences to within three sampling standard errors, uniform to 1e-9, plus a shift-and-scale equivariance check |
| Expected shortfall recovers its closed form | `test_var.py::TestHistoricalRecoversTheAnalyticQuantile::test_expected_shortfall_recovers_its_analytic_value`, `TestParametricMatchesTheClosedForm` |
| Monte Carlo is seed-reproducible | `test_var.py::TestSimulation` (including a hypothesis property over paths and seeds), `test_revaluation.py::test_monte_carlo_is_reproducible_from_its_seed`, `test_risk.py::test_monte_carlo_records_its_seed_and_repeats_exactly` |
| Stress reprices rather than extrapolating Greeks, and the two differ for a large shock | `test_revaluation.py::TestFullRevaluationVersusGreeks` — >5% divergence on a 10% move, <1% on a 0.1% move, error monotone in shock size, exact agreement on a linear book; `test_risk.py::test_a_sell_off_reprices_rather_than_extrapolating` |
| A null scenario reprices to exactly the base value | `test_revaluation.py::TestTheNullScenario` — the property that makes every P&L below meaningful |
| The vectorised repricing agrees with the scalar one | `test_revaluation.py::TestVectorisedAgreesWithScalar`, including a hypothesis property |
| Contributions decompose the loss exactly | `test_revaluation.py::test_the_cheap_decomposition_matches_holding_each_group_flat` — checked against the costly hold-one-group-flat construction |
| No scenario claims to be historical without the data behind it | `test_scenarios.py::TestNoInventedHistory` — templates are all `HYPOTHETICAL`, none is named after a real event, and a historical claim without a derivation is refused by the model *and* by a database CHECK |
| Too little history is a refusal, not a number | `test_risk.py::test_a_portfolio_with_no_history_refuses_rather_than_answering`, `test_an_underlying_with_one_observation_is_refused_not_invented` |
| Nothing is forward-filled across a gap | `test_revaluation.py::TestFactorPanel::test_nothing_is_forward_filled_across_a_gap` |
| An unpriceable position is reported, never treated as riskless | `test_revaluation.py::TestExclusions` |
| No risk response contains advisory language | `test_risk.py::TestLanguage` — asserted over the whole serialised response |

**What is deliberately not here**: GARCH filtering, copulas, fat-tailed
calibration and factor models beyond spot and volatility. A Student-t simulation
exists and is validated, but nothing calibrates its degrees of freedom, so it is
a parameter the user sets rather than a claim the platform makes.

## Phase 6 — Margin  `[x]`

- [x] `MarginModel` ABC + `SimpleRiskMarginModel`
- [x] Margin utilisation, shock-grid evaluation, margin-buffer curve
- [x] Estimated margin-shortfall region, bracketed by the rungs that locate it
- [x] `RUN_MARGIN` job handler
- [x] UI: margin page with the buffer curve and no liquidation marker

**Acceptance — verified**

| Criterion | Where |
| --- | --- |
| Every margin result carries method, assumptions, confidence and warnings | `test_margin.py::TestResultCompleteness::test_every_result_carries_all_four`, `test_margin.py (integration)::test_the_result_carries_method_assumptions_confidence_and_warnings` |
| No output names a broker or claims broker equivalence | `test_margin.py::TestNoBrokerClaim` — the serialised payload is scanned for venue names and affirmative claims, `/margin/models` reports `is_broker_equivalent: false` for every model, and no result field could be read as a requirement |
| "Liquidation" appears only inside its own denial | `test_margin.py::test_liquidation_is_only_ever_mentioned_to_deny_it` — every occurrence must be preceded by "not a broker" |
| The shortfall output is a region with assumptions, never a guaranteed price | `test_margin.py::TestVulnerabilityIsARegion` — the crossing is interpolated and reported with the two rungs that bracket it, and the interpolated point must lie between them |
| Unknown capital yields no buffer and no utilisation | `test_margin.py::TestCapitalIsNeverAssumed`, enforced again by `ck_margin_buffer_requires_capital` in the schema |
| The optional components default to zero and say what that leaves out | `test_margin.py::TestWhatTheDefaultsLeaveOut` — the warning text must contain "inventing a rule" |
| A worst case at the grid boundary is flagged and lowers confidence | `test_margin.py::test_a_worst_case_at_the_edge_is_flagged_and_lowers_confidence`, and a contained worst case is not flagged |
| Both sides of the buffer are remeasured at every rung | `test_margin.py::test_both_sides_of_the_buffer_move_along_the_ladder` |
| An upside shortfall is found, not only a downside one | `test_margin.py::test_an_upside_short_is_found_too` |
| The estimate is never negative, for any book | `test_margin.py::test_the_estimate_is_never_negative` (hypothesis) |

**What is deliberately not here**: `SPANApproximation`, the crypto cross- and
isolated-margin models, and `BrokerApproximationModel`. Each of those requires a
*published* methodology to implement against, and shipping one without would be
the exact failure this phase is built to avoid. The `MarginModel` interface
exists so they can be added when a methodology is in hand.

## Phase 7 — Execution TCA  `[x]`

- [x] Trade-log upload, parent/child grouping, explicit or inferred and flagged
- [x] Benchmarks: arrival, decision, prevailing mid, interval VWAP, interval
      TWAP, close — each with its window, source and method
- [x] Implementation shortfall in currency, basis points and percent
- [x] Model-based cost decomposition with data-coverage reporting
- [x] `IMPORT_TRADES` and `ANALYZE_EXECUTIONS` job handlers
- [x] UI: execution dashboard

**Acceptance — verified**

| Criterion | Where |
| --- | --- |
| A deterministic synthetic price path produces hand-checkable IS values | `test_tca.py::TestHandCheckableShortfall` — a path rising 1.00 per minute, an average of exactly 104 against an arrival of 100, giving 1200 in currency, 400 bps and 4% by hand; the multiplier scales only the currency amount; the same fills sold into the same path give exactly the negative |
| Every benchmark reports its window, source and method | `test_tca.py::TestEveryBenchmarkDeclaresItself`, `test_execution.py::test_every_benchmark_reports_window_source_and_method` |
| Low data coverage degrades, never to a confident wrong number | `test_tca.py::TestDataCoverage` — two ticks refuse, four ticks clustered in a corner refuse, and the interval VWAP refuses for want of interval volume rather than silently becoming a TWAP under a volume-weighted name |
| A missing benchmark produces a missing shortfall, not a zero | `test_tca.py::test_a_missing_benchmark_produces_no_shortfall_rather_than_zero`, and `ck_report_shortfall_needs_benchmark` in the schema |
| An arrival proxy is flagged, and understates the cost | `test_tca.py::TestArrivalProxy` — the proxied shortfall is provably smaller than the properly benchmarked one |
| An inferred parent grouping is flagged, and the gap that produced it is recorded | `test_tca.py::TestGrouping`, `test_execution.py::test_the_gap_changes_the_grouping_and_is_recorded` |
| No ambiguous row is auto-resolved | `domains/execution/application.py::ImportRefused`, mirroring the portfolio import |
| Every rejected row names its source row number and reason | `test_execution.py::test_every_rejected_row_names_its_row_number_and_reason` — four distinct reasons in the committed fixture |
| The decomposition is labelled model-based, with impact explicitly not modelled | `test_tca.py::TestDecomposition`, `test_execution.py::TestDecomposition` — only fees are labelled `MEASURED` |
| The components reconcile to the measured total | `test_tca.py::test_the_components_reconcile_to_the_total`, asserted again over the wire |
| A fill cannot precede its own submission | `TradeRejection.SUBMIT_AFTER_FILL` and `ck_execution_submit_not_after_fill` |

**What is deliberately not here**: a market impact model. Methodology §16 places
it in Phase 8, so the decomposition reports impact as `NOT_MODELLED` and states
that it is inside the timing residual rather than putting a number there. The
interval VWAP is unavailable on every window the platform can currently build,
because option quotes carry cumulative session volume rather than interval
volume — the benchmark exists, is tested, and reports why it cannot run.

## Phase 8 — Execution simulation  `[x]`

- [x] `ExecutionStrategy` ABC; TWAP, VWAP, POV, liquidity-adaptive
- [x] `MarketImpactModel` ABC; square-root and linear baselines, plus a zero
      model for isolating the schedule from the impact assumption
- [x] Counterfactual simulator; strategy comparison
- [x] `SIMULATE_EXECUTION` job handler
- [x] UI: simulation page with the schedules side by side

**Acceptance — verified**

| Criterion | Where |
| --- | --- |
| Every simulated result is labelled a counterfactual estimate | `test_execution_simulation.py::TestEverySimulationIsLabelled` — on the result, in the payload's own `caveat`, on the comparison, and asserted for arbitrary latencies by a hypothesis property; `ck_simulation_is_always_counterfactual` makes an unlabelled row unstorable |
| Schedules sum to the parent quantity | `TestSchedulesSumExactly` — a hypothesis property over arbitrary weights, totals and lot sizes, plus a second over TWAP schedules; `Schedule.__post_init__` raises if they ever do not |
| Impact models are unit-tested against closed-form expectations | `TestImpactAgainstClosedForms` — `eta*sigma*sqrt(Q/ADV)` and its linear counterpart checked at three parameter sets, quadrupling size doubling square-root impact, and the two models agreeing exactly at full participation |
| A comparison is not a ranking and recommends nothing | `test_there_is_no_best_or_recommended_field` (no such key anywhere in the payload) and `test_recommendation_is_only_ever_mentioned_to_deny_it` |
| No impact model ships a calibrated coefficient | `TestImpactRefusesToInvent`, `test_no_impact_model_ships_a_calibrated_coefficient`; the default is the identity and every result computed with it is flagged |
| A strategy whose inputs are missing refuses rather than degrading | `TestStrategiesRefuseRatherThanDegrade` — VWAP on a flat profile refuses because it would be TWAP, liquidity-adaptive on flat signals refuses because it would be VWAP, POV refuses when the window cannot absorb the order |
| A stale price leaves the slice unfilled, and the completion rate says so | `TestUnfilledSlices` — with the tolerance widened deliberately the same schedule completes, and the parameter is recorded on the row |
| Permanent impact accumulates and temporary impact does not | `test_permanent_impact_accumulates_across_slices`, `test_the_participation_rate_drives_the_temporary_term_only` |
| Simulated fills are scored by the same machinery as real ones | `test_the_simulated_fills_are_scored_by_the_phase_7_machinery` |

**What is deliberately not here**: Almgren-Chriss, Hawkes-adaptive scheduling,
and any calibrated impact coefficient. The first two are named as later work in
`docs/execution.md`; the third would require fitting to executions this platform
has not seen, and shipping someone else's published estimate as a default would
assert a measurement of a market nobody here observed.

## Phase 9 — Advanced derivatives  `[x]`

- [x] SSVI global surface, calendar-arbitrage-free by construction
- [x] Dupire local volatility from that surface, with invalid regions kept as
      holes that carry their reasons
- [x] Crank-Nicolson PDE with Rannacher start-up, plus a seeded Monte Carlo
- [x] Heston by characteristic function (little-trap branch), constrained
      calibration with Feller reported and optionally enforced
- [x] Model consensus with enumerable confidence; Breeden-Litzenberger density
- [x] Higher-order Greeks: vanna, volga, charm
- [x] `CALIBRATE_GLOBAL_SURFACE` and `PRICE_CONSENSUS` job handlers
- [x] UI: global surface page (term structure, local-vol grid, density, Heston)
      and a consensus page that draws the range before the median

**Acceptance — verified**

| Criterion | Where |
| --- | --- |
| With constant local vol the PDE converges to Black-Scholes at the order it claims | `tests/quant_validation/test_pde.py::TestOrderOfConvergence` — empirical order 2.00 for price, 2.00-2.03 for delta and gamma, on both a uniform and a concentrated grid; gamma is the strict one, and it reported order 1 until the solution interpolation was widened from a 3-point quadratic to a 5-point quartic |
| Heston cross-checks against QuantLib | `TestAgainstQuantLib::test_price_matches_quantlib` — 8 cases x calls and puts, worst absolute difference 1.5e-11 against `AnalyticHestonEngine`, including 7-day, deep-OTM, 5-year and 10-year with Feller violated |
| Consensus exposes dispersion and never a single "true" price | `tests/unit/test_consensus.py::TestNoSinglePrice` and `tests/integration/test_advanced_derivatives.py::TestModelConsensus` — the median is asserted to lie inside the range, the range and the dispersion are always present, and `test_there_is_no_field_that_could_hold_a_verdict` scans every key in the payload for `best_model`, `true_price`, `fair_value`, `recommendation` and `signal` |
| SSVI cannot contain calendar arbitrage | `TestGlobalSurface::test_calendar_arbitrage_is_structurally_impossible` and `TestCalibration::test_an_inverted_observed_term_structure_is_made_monotone`; `ck_converged_global_surface_is_arbitrage_free` makes a CONVERGED row with a decreasing term structure or a negative density unstorable |
| Butterfly freedom is checked two ways | `TestArbitrageConditions` — the Theorem 4.2 bounds are sufficient, not necessary, so Durrleman's `g >= 0` is evaluated numerically alongside them and both are stored |
| The Dupire surface reprices the surface it came from | `test_pde.py::TestDupireConsistency` — under 0.2% across three strikes and two maturities. It was 1.6-2.3% until two errors were fixed: one fixed forward used at every time step instead of the forward to each time, and a variance term structure clamped flat below the first expiry instead of running to zero at the origin |
| A local-volatility hole is a hole with a reason | `TestLocalVolatility::test_invalid_regions_are_holes_with_reasons`; `ck_local_vol_grid_conserves_points` enforces `total = valid + flagged` |
| A quantile is withheld from an inadmissible density | `TestDensity::test_quantiles_exist_only_for_an_admissible_density`; `ck_density_quantiles_require_admissibility` makes the row unstorable |
| Every model reports a value or a reason, never neither | `test_every_model_reports_a_value_or_a_reason`; `ck_model_value_has_value_or_reason` enforces the exclusive-or in the database |
| Confidence can always be taken apart | `TestConfidence` — every contribution carries its basis, one zero dimension drives the score to zero, and agreement saturates rather than ramping to zero so a 5.1% and a 50% spread are distinguishable |
| Calibrations are reproducible from their seeds | `test_the_calibration_is_reproducible` for SSVI and Heston, `TestReproducibility` for the consensus |
| A stored surface does not disclaim its own forwards | `test_a_stored_surface_keeps_the_provenance_of_its_forwards` and the `extrapolation` assertion in `test_confidence_can_always_be_taken_apart`. Found by the live walkthrough, not by the suite: the forward method and confidence were being dropped on write, so a surface read back flagged every value `LOW_CONFIDENCE_FORWARD` and took the consensus confidence from 0.998 to 0.904 for a reason that was not true |
| An unidentified parameter is caveated, not presented as a measurement | `test_a_short_term_structure_says_mean_reversion_is_not_identified` and `test_a_caveated_calibration_travels_with_the_price_it_produced` — two expiries pin `kappa * theta` and not the two separately, so the calibration says so and the consensus repeats it on the price |

**What is deliberately not here**: American exercise, a jump-diffusion or rough
volatility model, and any use of the implied density as a forecast. The first
two are named as later work in `docs/pricing.md`; the third is a category error
the density payload states in its own `interpretation` field.

## Phase 10 — Microstructure  `[x]`

- [x] L2 parquet storage in the object store: depth snapshots as list columns,
      event tapes as a message table, prices and quantities as decimals
- [x] Wide-CSV and canonical-parquet import, with level-column detection shown
      in a mandatory preview and confirmed on commit
- [x] The **data-availability gate**: six capabilities, each granted or refused
      with a closed-vocabulary reason and the evidence it was decided on
- [x] Book analytics: spread, microprice and its tilt, single- and multi-level
      imbalance, weighted imbalance, book slope with an uncentred R-squared,
      depth concentration, cost to trade the displayed book
- [x] Trade and cancellation intensity, by event type, side and price level
- [x] Hawkes against a Poisson baseline, adopted only on a held-out
      Diebold-Mariano test with a Newey-West variance
- [x] Queue outlook as a bracket over the cancellation-priority assumption
- [x] `IMPORT_BOOK_DATA`, `ANALYZE_MICROSTRUCTURE` and `FIT_INTENSITY` job
      handlers; the queue estimate answers inline
- [x] UI: dataset page with the capability verdicts, the session measures, the
      two intensity models side by side and the queue bracket

**Acceptance — verified**

| Criterion | Where |
| --- | --- |
| Every capability is gated on a data-availability check, with a reason and its evidence | `tests/unit/test_microstructure.py::TestTheAvailabilityGate` — a snapshot-only feed, an event-only feed, a top-of-book feed, a tape with no cancellations, a coarse clock, an unsequenced tape and a tape with a hole each get the specific refusal they earn; `test_microstructure.py (integration)::TestTheGate` carries the same over the wire as a 422 with `reason`, `capability` and `evidence` |
| There is no way past the gate from outside | `TestTheGate::test_there_is_no_parameter_that_overrides_a_refusal` — the published OpenAPI schema for every microstructure path is scanned for `force`, `override`, `skip_gate` and `ignore_availability` |
| **Hawkes must beat a Poisson baseline on held-out data before it ships** | `tests/quant_validation/test_intensity.py::TestTheGate` — adopted on five self-exciting tapes, refused on ten Poisson tapes, and `test_a_raw_positive_total_is_not_enough_on_its_own` proves at least one Poisson tape with a *positive* raw held-out gain is still refused, so the gate is not reading the sign of a difference |
| The stored row cannot claim a win it did not get | `ck_intensity_hawkes_needs_a_held_out_win` and `ck_intensity_adopted_model_matches_the_verdict`, exercised in `test_microstructure.py::test_the_stored_row_cannot_claim_a_win_it_did_not_get` against a below-threshold statistic, a non-converged fit and a drifted model name |
| The estimator recovers a process whose parameters are known | `TestParameterRecovery` — three parameter sets recovered to 15% from 20,000 seconds of simulated arrivals, and the fit scores at least as well as the truth on its own sample |
| The likelihood is the likelihood | `TestTheLikelihoodIsTheLikelihood` — the compensator against a quadrature of the intensity, the zero-jump case against the Poisson closed form, and `TestTheExcitationRecursion` checking the vectorised Ogata sum against the plain recursion at six decays including a window 10^4700 past overflow |
| Stationarity is structural, not checked afterwards | `test_stationarity_is_structural` — fitted to arrivals with no clustering at all, the branching ratio is still inside `(0, 1)` |
| Book measures are hand-checkable | `TestHandWorkedBookMeasures` — a microprice of 100.75 leaning away from the thick side, a slope of exactly 1400 with an R-squared of 0.98, an effective level count of 2, a walk of 15 across two levels |
| A measurement the data cannot support is an absence with a reason | `TestWhatABookCannotSupport` — no mid on a one-sided book, no imbalance (not zero) with no resting size, no slope through one level, no cost to trade a size the book cannot absorb; `analyse_book` records every one on `unavailable` |
| Every session measure reports what it was computed over | `TestSessionAnalytics::test_every_measure_reports_what_it_was_computed_over` — `observations + missing == snapshots_analysed` for all thirteen measures, and a measure that never existed carries the reason it did not |
| The queue outlook is a bracket, never a number | `TestTheQueueBracket` — the two ends are the two cancellation assumptions, the optimistic end can only be faster, there is no field that could hold a single probability, and `ck_queue_estimate_is_a_bracket` makes an inverted pair unstorable |
| A level nothing was seen to leave is refused, not scored zero | `test_a_level_nothing_ever_left_is_refused_not_scored_zero` |
| Nothing is dropped without a reason | `TestSnapshotImport` / `TestEventImport` — `input == kept + rejected`, and the fixtures seed *every* member of both rejection enums, asserted by set equality so a new reason cannot be added without a row that triggers it |
| Every rejected row names its source row number and reason | `test_every_rejected_row_names_its_row_number_and_reason`, over the complete list rather than a sample |
| A transposed book is refused rather than sorted | `test_a_transposed_book_is_refused_rather_than_sorted` |
| Stored observations are not re-rounded | `TestParquetRoundTrip::test_a_tick_price_is_not_re_rounded_by_the_store` — a 0.05 tick and an eight-decimal quantity survive exactly |
| No microstructure response advises or promises | `TestLanguage` — asserted over the whole serialised response, plus `test_a_queue_position_never_claims_to_be_the_exchange_queue` |

**Design notes**

- **The gate is the phase.** Every other engine degrades with a warning; this
  one refuses. An imbalance from a one-level feed and a queue position from a
  tape with holes look exactly like the real thing, and there is nothing in the
  number that says otherwise — so the judgement is made once, at import, stored
  with the dataset, and consulted before anything runs.
- **A raw held-out win is not evidence.** On genuinely Poisson arrivals the
  self-exciting model wins the raw held-out total about as often as it loses it,
  by hundredths of a nat. The first implementation adopted it seven times out of
  eight on data with no clustering whatsoever. The fix was to decompose the
  held-out likelihood into one predictive contribution per event and test the
  *mean* against its own Newey-West standard error; with that, five out of five
  self-exciting tapes are adopted and ten out of ten Poisson tapes are refused.
- **A bracket has to be monotone by construction.** The queue model originally
  gave each end its own mean departure size, and a level where five small
  cancellations accompanied one large trade produced an "optimistic" end *less*
  likely to fill than the pessimistic one. Found by the database CHECK, not by
  the suite. Both ends now share one size unit, so the optimistic departure
  stream containing the pessimistic one is enough to make the ordering hold.

**What is deliberately not here**: book reconstruction from an event tape, a
multivariate Hawkes process, and an adverse-selection term in the queue model.
The first would need a starting book, a complete tape and venue-specific message
semantics — three assumptions that would be invisible in the output. The second
is the honest model of trades exciting cancellations and each side exciting the
other, and it is named as later work rather than approximated by fitting one
univariate process to a superposition and calling it order flow.

## Phase 11 — Unified order analysis  `[x]`

Compose valuation + surface + risk + margin + execution over one `MarketState`.

- [x] `OrderAnalysisService` in `domains/reports`: the one place permitted to
      fan out across all five engines, and permitted only to compose them
- [x] One `ValuationContext` covering the book's underlyings **and** the order's,
      built once and handed to every branch
- [x] The proposed position, valued by the code that values a stored one,
      against that same context and never written
- [x] Valuation branch: the observed two-sided market, plus a reference range
      across the models that could run, their dispersion and a confidence whose
      contributions are listed
- [x] Surface branch: this contract scored by the Phase 3 anomaly scanner, made
      public as `score_point` rather than reimplemented
- [x] Execution branch: a forward cost estimate against a reference held flat,
      sharing the Phase 8 slice convention, split into a measured spread half
      and a modelled impact half
- [x] Risk and margin branches: the same estimators run on the book and on the
      book with the order in it, over one factor panel, one seed, one grid
- [x] `POST /order-analysis` inline, plus `GET` by id and by portfolio
- [x] `order_analyses` table with two CHECK constraints and no column a
      recommendation could go in
- [x] UI: the order-analysis page, five branches side by side

**Acceptance — verified**

| Criterion | Where |
| --- | --- |
| **One `market_state_id` in the provenance of all five branches** | `tests/integration/test_order_analysis.py::TestOneSnapshotForEveryBranch` — the set of state ids across the five branch provenance blocks has exactly one element, it matches the envelope's and the payload's, and `test_every_branch_names_the_same_moment_as_well` carries the same over the timestamp |
| Branch failure degrades to `PARTIAL` | `TestBranchesDegradeIndependently::test_the_status_is_partial_when_a_branch_failed` — an order on a non-option fails valuation and surface, names a reason on each, and execution, risk and margin still answer |
| A branch that needs history it does not have is an absence, not a number | `test_a_book_with_no_history_still_answers_four_branches` — the Greeks stand, `value_at_risk` is `null`, and `RISK_INSUFFICIENT_HISTORY` says why |
| **The response schema contains no recommendation field** | `TestLanguage::test_no_response_carries_a_recommendation_field` walks every key at every depth of a live response against a closed list; `test_the_published_schema_has_no_recommendation_field` walks the published OpenAPI components; `test_no_response_advises_or_promises` scans the whole serialised payload for forbidden phrasing |
| An order that cannot be repriced is refused, not scored zero | `TestAnOrderThatCannotBeRepricedIsRefused` — risk and margin both return `FAILED` with `INCREMENTAL_ORDER_NOT_REPRICEABLE` and the exclusion reason, while the branches that do not need a repriceable book still answer; `tests/unit/test_incremental_risk.py::test_an_order_that_cannot_be_repriced_is_reported_not_absorbed` pins the same at the domain level |
| The difference is the order's | `TestTheOrderIsInTheNumbers` — doubling the order doubles its Greek contribution, a buy and a sell move the book by equal and opposite amounts, and every `change` equals `proposed - current` |
| The two cost estimators are one convention | `tests/unit/test_order_cost.py::TestOneConvention` — the forward estimate and the Phase 8 simulator agree slice for slice on a flat path, on both sides, for fill price, spread, temporary and permanent impact |
| A cost that cannot be estimated is absent, not zero | `TestWhatCannotBeEstimated` — no daily volume leaves the impact half and the total `null` with the spread half still reported; no two-sided quote leaves the spread half `null`; a strategy that cannot be built is listed with its reason |
| Nothing claims that working an order is cheaper | `test_this_model_does_not_say_that_working_an_order_is_cheaper` — the permanent term rises with the slice count and the temporary term falls, pinned in both directions |
| A limit order's fill is classified, never predicted | `TestMarketability` — marketable, passive and unknown against the touch that would have to be crossed, and a passive order says its fill is not modelled |
| Observations are still observations | `TestObservationsAndEstimatesStaySeparate` — the observed block carries bid, ask and mid with the note that a mid is absent rather than substituted from a print, and the reference value is a range across models with every unavailable model naming its reason |
| The stored row cannot claim more than it has | `ck_order_analysis_status_matches_its_branches` and `ck_order_analysis_names_its_market_state`, with `test_the_stored_row_names_that_snapshot` reading the row back |
| The same order twice is the same analysis | `TestReproducibility` — one content-addressed snapshot and one derived proposed-position id, and a different size is a different position |
| Ownership is enforced in the query | `TestOwnership` — a foreign portfolio and a foreign analysis are both 404 |

**Design notes**

- **The snapshot is the deliverable.** Every branch here already existed. What
  Phase 11 adds is that they run against one `MarketState`, so the number a user
  actually reads — the difference between the book and the book with their order
  in it — is attributable. Building five analyses that each fetched their own
  market would have produced the same fields and none of the meaning.
- **A row of zeros is the dangerous output.** The first working version reported
  a proposed position that could not be repriced as deltas of exactly zero,
  which reads as an order that adds no risk and is the single worst sentence
  this endpoint could produce. `CombinedBook.order_is_repriceable` exists so the
  caller cannot read the numbers without reading that flag first.
- **One panel for both sides.** Building a factor panel per side let an order on
  a new underlying shorten the aligned sample the *current* book was measured
  on, and the difference then contained that as well as the order. One panel
  over the combined book is used for both, and the fact that this can shorten
  the sample for both is a warning on the branch rather than a hidden effect.
- **The impact model does not say what it looks like it says.** Permanent impact
  is evaluated per slice and accumulates, so splitting an order raises it as
  roughly the square root of the slice count while lowering the temporary term.
  Which dominates depends on coefficients nobody here has calibrated. The
  estimate therefore carries no argument for or against working an order, and a
  test pins both directions so such a claim cannot appear by accident.

**What is deliberately not here**: a fill-probability model for a passive limit
order, an idempotency key on the endpoint, and any aggregation of the five
branches into a single figure. The first needs the queue at the level, which is
a gated microstructure capability that most feeds cannot support, and splicing a
bracketed queue estimate into a cost figure would bury the gate. The third is
the recommendation field under another name: any weighting of reference value
against slippage against margin is a statement about someone's risk appetite,
and the platform does not have one.
