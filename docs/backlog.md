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
- [x] Two-sided chain layout: detect an exchange export (calls left of the
      strike, puts right of it, header names repeated per side), resolve it by
      column index before any name-based mapping, and confirm it in the preview
- [x] Auto-detection on the commit path: a file uploaded with no mapping and no
      layout is read the way the preview would read it -- layout first, then
      column mapping -- with what was worked out reported in the result and
      recorded in provenance
- [x] The reading is reported, not requested: `preview` returns which column
      every field was read from and whether that was detected or supplied, the
      first rows as they were read with the unreadable ones kept in, and a
      verdict on whether the reading worked
- [x] A file that could not be read is refused rather than ingested into a
      near-empty snapshot -- checked against the sample on the request and
      against every row in the worker, by one rule
- [x] Jobs: model, service, task, status/progress/result API
- [x] Test infrastructure: unit, integration, quant-validation, regression harness

**Acceptance**

| Criterion | How it is verified |
| --- | --- |
| A user can register, log in and call an authenticated route | `tests/integration/test_auth.py` |
| Every field says which column it was read from | `tests/integration/test_option_chain_ingestion.py::TestThePreviewReportsTheReading` |
| A corrected column is attributed to the user, not to the platform | `test_a_column_the_user_corrected_is_attributed_to_them`, `test_one_corrected_column_makes_that_field_the_callers` |
| The sample keeps the rows it could not read | `test_rows_that_could_not_be_read_appear_in_the_sample` |
| An empty far strike is not counted as a misreading | `test_an_empty_far_strike_is_not_counted_as_a_misreading`, `tests/unit/test_reading_report.py::TestWhichRejectionsCountAgainstTheReading` |
| A file read with the wrong columns is refused, and nothing is written | `TestAFileThatCouldNotBeReadIsRefused::test_a_column_that_does_not_hold_what_it_was_taken_for_is_refused`, `::test_nothing_was_written_by_the_refusal` |
| A file that only goes wrong past the sample is refused by the worker | `test_a_file_that_only_goes_wrong_past_the_sample_is_refused_by_the_worker` |
| The failure is a diagnosis rather than a stack trace | `test_the_failed_job_names_the_columns_it_read_from` |
| An auto-read chain feeds volatility and surface analysis | `TestAnAutoReadChainFeedsTheRestOfThePlatform` |
| An exchange export with no spot column still solves implied volatility | `TestTheExchangeExportSupportsTheRestOfThePlatform::test_implied_volatility_solves_without_a_spot_column` |
| An expired contract is flagged whether or not a spot accompanies it | `test_an_expired_quote_is_flagged_even_with_no_underlying_price` |
| An as-of past the chain's expiry is called out at ingest, not three screens later | `TestAnAsOfPastTheExpiryIsCalledOut` |
| A foreign portfolio/upload id returns 404, not 403 | `tests/integration/test_ownership.py` |
| The instrument master round-trips and is idempotent under re-import | `tests/unit/test_instrument_identity.py` |
| The same contract yields the same UUID across processes | deterministic uuid5 test |
| An option chain CSV can be uploaded, previewed, ingested and retrieved | `tests/integration/test_option_chain_ingestion.py` |
| An exchange two-sided chain export ingests, and **calls keep the call prices** | `tests/integration/test_two_sided_chain.py`, `tests/unit/test_chain_layout.py` |
| A long-form file is still read as long-form | `test_a_long_form_file_is_left_alone` |
| A chain export ingests with an empty request body, and still keeps the call prices | `TestTheFileIsReadWithoutBeingDescribed` |
| A long-form file ingests with an empty body, and the inference is reported | `TestIngestingWithNothingSaidAboutTheFile` |
| Detection never overrides a caller who supplied a mapping, even a partial one | `TestReadingAFileTheCallerDidNotDescribe` |
| A file whose expiry is in neither a column nor its filename is refused, not dated | `test_a_file_whose_expiry_is_nowhere_is_refused_not_guessed` |
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

## Groundwork — Broker credential vault  `[x]`

Not a numbered phase: a prerequisite for the real-time provider work, done on
its own because it changes how the platform holds a secret and that is worth
finishing and testing before anything depends on it.

The problem it removes is stated plainly in `docs/credentials.md` — a broker
access token pasted into `.env` and the process restarted every time the provider
expires it, with one identity shared across the whole installation and nothing
recording who connected what.

- [x] `infrastructure/security/crypto.py`: AES-256-GCM sealing with key rotation
      and associated data binding each ciphertext to its own row
- [x] `domains/broker_auth`: a per-user credential obtained through the
      provider's own authorization-code flow, renewed where the provider allows
      it and marked `NEEDS_REAUTHORIZATION` where it does not
- [x] `broker_connections` table holding ciphertext only — no column on it can
      hold a token in the clear
- [x] Signed, single-use `state` bound to caller, provider and nonce, with a
      `typ` claim separating it from an API access token
- [x] `/connections` endpoints; no response, log or audit entry carries token
      material
- [x] Six audit actions covering the whole life of a connection
- [x] UI: the connections page and the provider callback
- [x] `scripts/generate_credential_key.py` and a documented rotation procedure

**Acceptance — verified**

| Criterion | Where |
| --- | --- |
| **No broker token is ever configured, returned, logged or stored in the clear** | `tests/integration/test_broker_connections.py::test_no_response_ever_carries_the_token` scans raw response bodies rather than named fields; `test_the_token_is_not_readable_from_the_row_that_holds_it` scans the stored ciphertext; `test_no_audit_entry_carries_the_credential` scans the audit metadata |
| A credential is per user | `test_a_second_account_cannot_see_the_first_ones_connection` — the second account lists nothing and gets 404 on the first's connection |
| The handoff cannot be replayed, borrowed or forged | `TestTheAuthorizationHandoffIsNotForgeable` — a reused state, another account's state, an invented state and an API access token presented as a state are all refused with one code, and the provider is never called |
| **An expiry the provider did not state is not invented** | `test_an_undeclared_expiry_is_not_treated_as_an_expiry` — no refresh is attempted and the token is used; `test_a_rejection_retires_a_credential_that_declared_no_expiry` — the provider's refusal, not a schedule, is what retires it; `tests/unit/test_credential_vault.py::test_a_response_with_no_lifetime_is_not_given_one` pins the parser |
| A declared expiry is renewed without the user | `test_an_expiring_credential_is_renewed_without_the_user` — inside the refresh skew the credential is renewed and the connection stays `CONNECTED` |
| A renewal that omits a new refresh token does not kill the connection | `test_a_renewal_that_returns_no_new_refresh_token_keeps_the_old_one` — two consecutive renewals both present the original refresh token (RFC 6749 §6 makes the new one optional) |
| What cannot be renewed asks the user rather than failing quietly | `test_a_declared_expiry_with_no_refresh_token_asks_the_user_back` and `test_a_failed_renewal_leaves_a_connection_that_says_what_happened` — status and `last_error` both say why |
| Rotating the encryption key does not disconnect anyone | `test_a_credential_sealed_under_a_retired_key_is_still_usable`; `tests/unit/test_credential_vault.py::test_a_retired_key_still_reads_the_rows_it_wrote` |
| A rotation can actually be finished | `test_using_a_credential_after_a_rotation_migrates_the_row` — using a credential re-seals **both** halves under the active key, so the retired key stops being referenced and can be removed |
| Rotation and renewal do not break each other | `test_renewing_a_credential_after_a_key_rotation_keeps_it_alive` — a renewal that carries the existing refresh token across must read it under the key it was written with, not the one just recorded; getting this wrong disconnects every user at their first renewal after a rotation and only then |
| A ciphertext is useless in another row | `test_a_ciphertext_moved_to_another_row_does_not_open` |
| No key means no storage, not plaintext storage | `test_without_an_encryption_key_the_flow_stops_before_the_broker` — refused before the user is sent to the broker, and `/connections/providers` says so up front |
| An unregistered provider names the settings to fill in | `test_an_unregistered_provider_names_what_is_missing` |
| A failed reconnect does not break a working connection | `test_a_failed_reconnect_does_not_break_a_working_connection` — the held credential is untouched, the status stays `CONNECTED`, and the failure is recorded in `last_error` |
| Disconnecting erases the secret and keeps the record | `TestDisconnecting` — ciphertext gone, row and broker account id retained |

**What is deliberately not here**: provider-side revocation on disconnect, and
any assumed token lifetime. The platform cannot make a broker forget a token —
only the broker can — so the audit entry says the local copy was destroyed and
claims nothing more. And a lifetime nobody published is a guess that fails in
both directions: too short interrupts users holding working credentials, too
long reports dead ones as live and turns every provider call into a silent gap.

## Real-time Phase 1 — Live market data  `[x]`

The first slice of the real-time platform: a live provider, an instrument
master, a feed, and a `MarketState` assembled from what the feed delivered.

- [x] `UpstoxMarketDataProvider` behind the existing `MarketDataProvider`
      interface — quotes, depth, bars and an option chain assembled from the
      platform's own instrument master
- [x] `NormalisationSpec`: the payload→schema mapping as versioned data, with a
      per-read report of fields read, fields missing and payload keys claimed by
      nothing
- [x] `UpstoxInstrumentMaster`: provider file → canonical instruments plus the
      alias rows joining platform ids to provider keys, with row conservation
- [x] `read_expiry`: a midnight expiry instant resolved against the contract's
      own name, or by a stated convention recorded on the instrument
- [x] `domains/market_data/streaming`: subscriptions, backoff, event bus, live
      store, decoders, two transports, and a manager that owns the connection
- [x] Redis live state with a TTL, and cross-process subscription interest
- [x] `LiveMarketDataService.live_market_state`: the join to every quant engine
- [x] `/live/*` endpoints and the market stream worker `apps/stream/main.py`
- [x] UI: the live market page, every price with its age

**Acceptance — verified**

The phase's own criterion — *user selects NIFTY → live price appears → bid/ask/
OI/volume update → MarketState updates* — is
`tests/integration/test_live_market_data.py::TestSelectingNiftyAndSeeingALivePrice`,
end to end through the real provider normalisation, the real quality engine and
the real store, with only the socket replaced.

| Criterion | Where |
| --- | --- |
| A live price appears with bid, ask, volume and open interest | `test_a_live_price_appears_with_bid_ask_volume_and_open_interest` |
| The price updates as the feed delivers | `test_the_price_updates_as_the_feed_delivers` |
| **Every price carries its own age** | `test_every_live_price_carries_its_own_age` — a price with no visible age gets treated as current whatever it is |
| **A MarketState is assembled from live prices** | `TestTheLiveMarketStateIsTheJoinToEverythingElse` — content-addressed, and `test_one_moment_and_one_set_of_prices_is_always_the_same_id` pins that one moment and one set of prices is always one id |
| A quote from after the snapshot is not in it | `test_a_quote_stamped_after_the_snapshot_is_not_in_it` |
| The reading report costs nothing on a healthy feed | `test_a_clean_reading_names_its_spec_and_nothing_more` and `test_a_reading_that_was_not_clean_carries_the_whole_report` — the full report rides on a quote only when something was missing or unclaimed |
| **A renamed provider field is visible immediately** | `tests/unit/test_live_normalisation.py::test_a_renamed_field_shows_up_as_missing_and_unmapped_together` — the failure that otherwise runs for days as quotes that quietly become empty |
| A quote whose age cannot be known is refused | `test_a_quote_whose_age_cannot_be_known_is_refused` — dating it to the read would make every stale price look fresh |
| A response that cannot be matched to the request is refused | `test_an_unmatchable_response_is_refused_not_guessed` and `test_several_unidentifiable_entries_match_nothing` — there is no third fallback, because that is where one instrument's price gets attached to another's id |
| **The instrument master conserves rows** | `test_every_row_is_accounted_for`, `test_a_filtered_row_still_closes_the_sum` |
| Nothing in the master is coerced | `test_an_exchange_with_no_recorded_currency_is_refused`, `test_an_option_on_a_venue_with_no_recorded_exercise_style_is_refused`, `test_a_contract_whose_underlying_was_filtered_out_is_refused` |
| The multiplier says where it came from | `test_the_multiplier_says_it_came_from_the_lot_size`, `test_an_index_with_no_lot_size_declares_its_multiplier_assumed` |
| **A midnight expiry is resolved or declared** | `TestExpiryIsNotGuessed` — the contract name settles it where it can, a stated convention otherwise, and both candidates are recorded on the instrument |
| An older observation never overwrites a newer one | `tests/unit/test_market_stream.py::test_an_older_observation_never_overwrites_a_newer_one` |
| A silent connection is reported STALE | `test_a_connection_delivering_nothing_reports_stale` |
| An unreadable frame is counted, not fatal | `test_an_unreadable_entry_is_counted_and_does_not_stop_the_feed` |
| Backoff resets only on a connection that delivered | `test_the_counter_resets_only_when_something_arrived` — resetting on connect makes our own client a denial-of-service attack on the provider |
| Every reconnect resends the whole subscription | `test_every_reconnect_resends_the_whole_subscription` |
| A slow consumer loses the oldest events and is told | `test_a_slow_consumer_loses_the_oldest_events_and_is_told` |
| **Synthetic prices are never a fallback** | `test_a_synthetic_deployment_says_its_prices_are_not_real`; `tests/unit/test_stream_worker.py::TestChoosingAProvider` — the synthetic market is refused at construction in a production-like environment, and a live provider that cannot be built raises rather than substituting one. Deliberately not a startup check: a deployment that only analyses uploaded chains never builds a provider, and `test_a_file_only_production_deployment_still_starts` pins that it can still start |
| An instrument with no live price is named, not omitted | `test_an_instrument_with_no_live_price_is_named_not_omitted` |
| A sampling transport does not claim to deliver every tick | `test_a_polling_transport_does_not_claim_to_deliver_every_tick` |
| **The worker refuses to serve a generated market** | `tests/unit/test_stream_worker.py::test_it_refuses_to_run_against_the_synthetic_market` — a feed worker publishing the synthetic market into the live store would put invented prices behind every live endpoint |
| A websocket with no frame decoder refuses to start | `test_a_websocket_without_a_frame_decoder_refuses_to_start` — a socket that stays up delivering zero quotes looks exactly like a quiet market |
| The market-data account is named, never defaulted | `test_it_refuses_to_pick_a_market_data_account_for_you` — one entitlement serves the deployment, and whose it is has licensing consequences |
| A poll with nothing subscribed is not sent | `test_it_does_not_call_the_provider_with_nothing_subscribed` |
| The provider declares only what it serves | `TestWhatTheProviderDeclaresItCanDo` — no `INSTRUMENTS` (that is the master loader) and no `BOOK_EVENTS` (a snapshot feed cannot support a queue model), so a caller cannot plan around a capability and fail halfway through |

**What is deliberately not here**: a `/ws/market` push socket, order-book event
capability on either transport, and any decoder for a provider's binary wire
format written from observation. The first would be a second copy of the live
state with its own consistency question and no better update rate than a
browser can render. The second would let a queue model be built on periodic
snapshots, which is a model of a book nobody saw. The third is the worst failure
a market-data system has — plausible numbers from a format nobody checked — so
the protobuf decoder loads a module generated from the provider's own `.proto`
and refuses to start without one.

## Real-time Phase 2 — Live options intelligence  `[x]`

A live option chain reaching the volatility machinery that was already built,
plus the two analytics that were not.

The phase deliberately adds no second IV solver, no second SVI fit and no
live-specific surface path. It adds the route in — a live chain captured as a
stored snapshot — and then the existing Phase 1, 2, 3 and 9 machinery runs on it
unchanged.

- [x] `LiveChainCaptureService`: live cache → `option_chain_snapshots`, with
      conservation and quality scored against the chain rather than carried
      over from the feed
- [x] `MarketDataService.capture_live_chain`, so no other domain reaches into
      market data's repository to write a snapshot
- [x] `ANALYSE_LIVE_CHAIN`: capture → implied volatilities → SVI → delta skew as
      one job against one captured moment
- [x] `quant/volatility/delta_skew.py`: forward-delta strike solving, risk
      reversal and butterfly
- [x] `domains/derivatives/delta_skew.py`: the same across a stored surface,
      recomputed on read rather than stored
- [x] `domains/derivatives/chain_greeks.py`: delta, gamma, vega, theta and rho
      for every solved contract, against its own implied volatility
- [x] `domains/market_data/open_interest.py`: open interest, volume, put-call
      ratios, turnover, and change between two snapshots
- [x] `/live/options/analyse`, `/derivatives/surfaces/{id}/delta-skew`,
      `/market/open-interest/{underlying}` and `/change`
- [x] UI: the live options page — capture summary, skew term structure, open
      interest by expiry

**Acceptance — verified**

The phase's own criterion — *user selects NIFTY → live option chain → IV →
volatility surface → surface analytics* — is
`tests/integration/test_live_options.py::TestFromLiveChainToSurface`, run end to
end through the real capture, the real IV solver, the real SVI calibration and
the real skew, with the live quotes supplied by the seeded synthetic market so
the surface fitted back out can be checked against the one that went in.

| Criterion | Where |
| --- | --- |
| A live chain becomes a stored snapshot | `test_the_capture_becomes_a_stored_snapshot` |
| **Every contract is kept, excluded or rejected** | `test_every_contract_is_kept_excluded_or_rejected`; `test_a_contract_with_no_live_price_is_rejected_not_omitted` — an untraded wing is rejected *with that reason*, not dropped |
| How much of an instant the snapshot is gets reported | `test_how_much_of_an_instant_the_snapshot_is_gets_reported` — every calibration downstream treats these quotes as simultaneous |
| Implied volatilities are solved from the live chain | `test_implied_volatilities_are_solved_from_the_live_chain` |
| A surface is calibrated and stored | `test_a_surface_is_calibrated_and_stored` |
| **The recovered surface matches the market it came from** | `test_the_skew_has_the_sign_the_generated_market_was_given` — the synthetic market is built with a negative SVI rho, so the fitted 25Δ risk reversal must come back negative; a sign error here would be invisible in every other test |
| Every stage names the identifier the next one used | `test_every_stage_names_the_identifier_the_next_one_used` — which is what makes a surface traceable back to its quotes |
| **The delta convention is on every result** | `tests/unit/test_delta_skew.py::test_every_result_names_the_convention_it_used` — spot and premium-adjusted delta give different strikes for the same nominal delta |
| The solved strike really has that delta | `test_the_solved_strike_really_has_that_delta` |
| A strike outside the fitted range is flagged, not hidden | `test_a_strike_outside_the_fitted_range_is_flagged_not_hidden` |
| **A delta that occurs nowhere is refused, not searched harder for** | `test_a_delta_that_occurs_nowhere_is_reported_not_widened_into` and `test_a_smile_whose_delta_turns_back_on_itself_is_refused` — widening would put a strike far outside the traded market into a skew number |
| An unmeasurable wing gives null, not zero | `test_an_unmeasurable_wing_gives_null_rather_than_zero`; `test_an_unmeasurable_wing_is_listed_rather_than_dropped` |
| A slice with no fit is listed, not dropped | `test_a_slice_with_no_fit_is_listed_as_unmeasured_not_dropped` — a term structure with a silent hole reads as a smooth curve |
| Delta skew recomputes identically | `test_recomputing_it_gives_the_same_answer` — which is why it is computed on read rather than stored beside the surface |
| **An unsolved contract gets no Greeks, not zeros** | `tests/integration/test_live_options.py::test_an_unsolved_contract_is_listed_rather_than_given_zeros` — a zero delta reads as an option carrying no risk, and it plots and sums perfectly |
| Greeks have the signs their contracts require | `test_a_call_has_positive_delta_and_a_put_negative`, `test_gamma_and_vega_are_never_negative_for_a_long_option` |
| Greek units are named on the payload | `test_the_units_are_named_on_the_payload` — an unlabelled vega could be per 1.00 of volatility or per volatility point |
| The carry assumption travels with the Greeks | `test_the_carry_assumption_travels_with_the_answer` — a wrong carry moves every delta |
| **A missing open-interest figure is not a zero** | `tests/unit/test_open_interest.py::test_a_missing_figure_is_not_counted_as_zero`, with `coverage` saying how much of the chain carried one |
| A zero denominator gives null, not infinity | `test_a_zero_denominator_gives_none_rather_than_infinity` |
| The open-interest unit is labelled, not assumed | `test_the_unit_of_an_absolute_total_is_labelled_not_assumed` |
| Excluded quotes stay out of the sums and are counted | `test_excluded_quotes_stay_out_of_the_sums_and_are_counted` |
| **A change carries the window it happened over** | `test_the_window_travels_with_the_change`; `test_a_change_needs_two_snapshots_and_says_so` |
| Contracts are matched on identity, not on strike and date | `test_contracts_are_matched_on_identity_not_on_strike_and_date` |
| Contracts in only one snapshot are counted, not dropped | `test_contracts_in_only_one_snapshot_are_counted_not_dropped` |
| **No open-interest response interprets itself** | `test_the_ratio_is_reported_without_being_interpreted` — the whole response body is scanned for the words a reading would use |

**What is deliberately not here**: "max pain", a second surface model fitted to
live data, streaming per-tick surface updates, and Greeks taken against the
fitted surface alongside the quoted ones. Max pain
is a well-defined function of open interest that is almost always presented as a
prediction of where the underlying will settle; the platform has no basis for
that claim and will not imply one by shipping the quantity under its usual name.
SSVI, Heston, local volatility and the risk-neutral density already run on a
stored analysis, which is exactly what a live capture produces — they needed no
live-specific path. Refitting SVI per tick would spend seconds of optimiser time
to move the fifth decimal place and would make "the surface at 09:31:04" a
question with no answer. And surface Greeks are a genuinely different quantity
from quoted-vol Greeks: shipping both undistinguished would give a table where
neighbouring strikes were measured against different things.

## Real-time Phase 3 — Historical warehouse  `[x]`

Partitioned Parquet in the object store, a dataset registry in PostgreSQL, a
validator that reports and never repairs, and DuckDB over the top.

- [x] `domains/warehouse/partitioning.py`: Hive-style
      `exchange/year/month/day/instrument`, round-tripping through the path so a
      file found on its own is still identifiable
- [x] `schemas.py`: Arrow schemas with exact decimal prices, UTC microsecond
      timestamps, and a `flags` column carrying the validator's judgement
- [x] `validation.py`: schema, timestamps, duplicates, ordering, robust
      outliers, split-like jumps, and gaps split by whether they are shared
- [x] `quality.py`: five dimensions and a weighted geometric mean, with an
      unmeasurable dimension returning `None` rather than zero
- [x] `storage.py` and `query.py`: whole-partition writes, and two read paths
      that say which one ran
- [x] `warehouse_datasets` / `warehouse_partitions` with a conservation CHECK
- [x] `readers.py`: CSV and Parquet through one coercion path, symbols resolved
      against the instrument master
- [x] `INGEST_HISTORICAL_DATASET`, `/warehouse/*`, and the datasets UI

**Acceptance — verified**

The phase's own criterion — *historical dataset → validated → queryable → usable
by the research engine* — is `tests/integration/test_warehouse.py`, end to end
from an uploaded file to a time-ordered series read back out of the partitions.

| Criterion | Where |
| --- | --- |
| A historical file becomes a registered dataset | `test_a_csv_becomes_a_registered_dataset` |
| CSV and Parquet read the same way | `test_a_parquet_file_reads_the_same_way` — one coercion path, so the two formats cannot come to disagree about what a column means |
| **Every row in the file is accounted for** | `test_every_row_in_the_file_is_accounted_for` — the reader's accounting and the warehouse's, and between them every row is written, refused, unparseable or unresolvable |
| **Suspicious rows are flagged and kept** | `tests/unit/test_warehouse_validation.py::test_a_bad_tick_is_flagged_and_kept`; `tests/unit/test_warehouse_storage.py::test_flags_travel_into_the_partition`; `test_flagged_rows_are_returned_unless_the_caller_excludes_them` |
| **A naive timestamp is refused, not read as UTC** | `test_a_file_with_no_offset_is_refused_rather_than_read_as_utc` — a year of NSE bars read as UTC is a year shifted by five and a half hours; `test_an_offset_is_honoured_rather_than_overwritten` pins the other side |
| A duplicate is excluded, a malformed row rejected | `test_a_duplicate_is_excluded_rather_than_rejected`, `test_a_rejection_names_its_row_and_its_reason` |
| **A split-like jump is detected and never repaired** | `test_a_five_for_one_split_is_recognised`, `test_the_split_is_reported_and_never_repaired` — the post-split rows still hold the unadjusted prices the file gave |
| A source declaring itself adjusted is not second-guessed | `test_a_source_declaring_itself_adjusted_is_not_second_guessed` |
| An undeclared treatment is itself a warning | `test_an_undeclared_treatment_is_a_warning_in_itself`, `test_an_undeclared_corporate_action_treatment_is_warned_about` |
| **A quarantined dataset is not served** | `test_a_quarantined_dataset_is_not_served`, and `test_the_partitions_are_still_written_and_listed` — quarantine refuses to serve, it does not destroy |
| A gap shared by every instrument is not called a holiday | `test_a_date_missing_for_every_instrument_looks_like_a_closure` — the platform holds no trading calendar and says so |
| A gap in one instrument alone looks like missing data | `test_a_date_missing_for_one_instrument_looks_like_missing_data` |
| **A robust score is not fooled by the point it is judging** | `test_the_robust_score_is_not_fooled_by_the_point_it_is_judging`, and `test_a_spike_in_a_barely_moving_series_is_still_found` for the zero-MAD case |
| Freshness is not measurable for an archive | `test_freshness_is_not_measurable_for_an_archive` — a zero would rank every archive as broken |
| One ruined dimension drives the overall down | `test_one_ruined_dimension_drives_the_overall_down` |
| **A partition key round-trips through its path** | `test_a_key_round_trips_through_its_path`; `test_the_day_is_utc_whatever_zone_the_timestamp_carried` |
| A date range reads only the days it needs | `test_a_date_range_reads_only_the_days_it_needs` — `partitions_read` counts files that contributed rows, so a broken prune fails a test rather than merely being slow |
| Re-ingesting a day replaces rather than appends | `test_rewriting_a_day_replaces_rather_than_appends` |
| Prices survive as exact decimals | `test_prices_survive_as_decimals`, `test_prices_come_back_exact_rather_than_through_a_float` |
| A truncated answer says so | `test_a_truncated_answer_says_so` |
| A symbol resolving to nothing is reported, not guessed | `test_a_symbol_that_resolves_to_nothing_is_reported_not_guessed` — a bar filed under the wrong instrument is a series that looks reasonable and is somebody else's |
| A file missing a column fails as a read | `test_a_file_missing_a_required_column_fails_as_a_read` — rather than registering an empty dataset, which would look like a file with no rows |
| The full finding list is retrievable | `test_the_findings_are_retrievable_in_full` |
| **A research caller gets a usable series** | `test_the_warehouse_is_readable_by_a_research_caller` — one instrument, a window, chosen columns, in time order |

**What is deliberately not here**: corporate-action adjustment, a trading
calendar, automatic outlier removal, and partition compaction. Detection without
a corporate-action feed is honest; correction without one is invention. Gaps are
reported as shared or not, which is derivable from the data; naming holidays is
not. Outlier removal takes a judgement — whether a 30% day is a bad tick or the
most interesting row in the sample — that belongs to whoever is modelling.
Compaction is real work with real failure modes and needs a workload to be
designed against rather than guessed at.

**One change outside the phase.** `submit_job` in eager mode no longer re-raises
a handler's exception. In queue mode the exception reaches a worker and the
submitting request returned its 202 long before; eager mode has to behave the
same way, or a failing job turns a submission into a 500 and the client never
learns the job id it would use to read the failure. `run_job` records `FAILED`
with the traceback either way, and the job row is the authoritative record.

## Real-time Phase 4 — Research and backtesting  `[x]`

Features that cannot see the future, strategies that produce target weights
rather than instructions, an event-driven engine, metrics that state their own
conventions, an attribution that has to close, and a record of every run.

- [x] `domains/research/features.py`: point-in-time by construction, with the
      look-ahead guarantee expressed as a property test rather than a comment
- [x] `strategies.py`: the four benchmarks build spec §44 asks for, each
      declaring the features it needs
- [x] `costs.py`: a supplied fee schedule with four bases, caps, floors and
      sides — and `NO_COST_MODEL`, which is a labelled absence rather than zero
- [x] `engine.py`: decide on bar `t`, fill on bar `t+1`, with position limits,
      a rebalance threshold and whole-unit fills
- [x] `metrics.py`: the §19 set, with the annualisation factor measured from the
      data and every uncomputable metric returning `None`
- [x] `attribution.py`: an identity that closes, with the residual published
- [x] `research_experiments` and `/research/*`, plus the experiments UI

**Acceptance — verified**

The phase's own criterion — *dataset → strategy → backtest → performance report*
— is `tests/integration/test_research.py`, run against a real warehouse dataset
loaded through the real Phase 3 ingestion path.

| Criterion | Where |
| --- | --- |
| A strategy runs over a warehouse dataset | `test_a_strategy_runs_over_a_warehouse_dataset` |
| The report carries the metrics it promises | `test_the_report_carries_the_metrics_it_promises` |
| **The attribution reconciles** | `test_the_attribution_reconciles`; `tests/unit/test_backtest.py::test_the_identity_closes` — an attribution that does not add up is a bug, not an approximation |
| **Buy-and-hold matches the instrument** | `test_its_return_matches_the_instrument_over_the_held_window` — the accounting benchmark, checked against an independently computed final equity rather than by asking the engine twice |
| **No feature can see the future** | `tests/unit/test_features.py::test_truncating_the_series_does_not_change_past_values` — a property test, parameterised over every feature shipped |
| A decision is filled on the next bar | `test_a_decision_is_filled_on_the_next_bar`, `test_the_fill_price_is_never_the_bar_the_decision_used` |
| A signal on the final bar is not executed | `test_a_signal_on_the_final_bar_is_recorded_and_not_executed` — filling it would be a free trade at a price the decision already saw |
| A feature is `None` until it has its window | `test_a_feature_is_none_until_it_has_its_window` — a 20-day mean of three days is not a 20-day mean |
| **No cost schedule means gross, and says so** | `test_no_schedule_means_gross_and_says_so`, `test_a_run_with_no_cost_schedule_is_gross_and_says_so` — silently assuming free trading is the commonest way a backtest reports returns that do not exist |
| A supplied schedule is charged and reduces the return | `test_a_supplied_schedule_is_charged_and_leaves_the_book`, `test_costs_reduce_the_return` |
| A sell-only levy is not charged on a buy | `test_a_sell_only_component_is_not_charged_on_a_buy` |
| A cap binds; a derived component sees only what it applies to | `test_a_cap_binds`, `test_a_derived_component_sees_only_what_it_applies_to` |
| Slippage always moves against the trader | `test_slippage_always_moves_against_the_trader`, `test_a_buy_pays_more_than_the_reference_price` |
| **Slippage is reported, not subtracted twice** | `test_slippage_is_reported_but_not_subtracted_twice` — it is already inside the fill prices, and double-counting would be absorbed by the residual |
| Greeks are absent rather than zero | `test_greeks_are_absent_rather_than_zero` — a zero theta reads as "no time decay", not "not applicable" |
| **The annualisation factor comes from the timestamps** | `test_the_annualisation_factor_comes_from_the_timestamps` — assuming 252 on a weekly series would be wrong sevenfold |
| A short window gets no CAGR | `test_a_short_window_gets_no_cagr` — annualising six weeks describes a year nobody observed |
| A short sample is reported with its count, not withheld | `test_a_short_sample_is_reported_with_its_count_not_withheld` |
| A curve that only rose has no Sortino | `test_a_curve_that_only_rises_has_no_sortino` |
| Crossing through zero opens the new side at the fill price | `test_crossing_through_zero_opens_the_new_side_at_the_fill_price` — anything else leaves an average price mixing a long and a short |
| A clamped weight is reported | `test_a_weight_beyond_the_limit_is_clamped_and_reported` — a strategy whose weights are cut is not the strategy that was described |
| A flagged bar is marked but not traded on | `test_a_flagged_bar_is_marked_but_not_traded_on`, and including them is a recorded choice |
| **The record holds what the run assumed** | `test_the_experiment_record_holds_what_the_run_assumed` — the cost schedule verbatim, the strategy parameters, the features, the code commit and the fill timing |
| Bad parameters are refused before the job | `test_bad_strategy_parameters_are_refused_before_the_job` |
| A run with no bars fails rather than reporting nothing | `test_a_run_with_no_bars_fails_rather_than_reporting_nothing` |

**What is deliberately not here**: multi-instrument portfolios, parameter sweeps,
walk-forward validation, and anything that evaluates a strategy against *today's*
market. The first is Phase 5 and doing it badly here would have to be undone
there. The second is how a backtest becomes a story about noise, and when it
arrives it needs a multiple-testing correction alongside it rather than after it.
The last is the point at which a research tool would become a recommendation
engine, and the platform's language policy forbids that: a strategy here produces
a **target weight for a simulated book**, and the type is `LONG`/`SHORT`/`FLAT`
describing a state rather than `BUY`/`SELL` instructing anybody.

## Real-time Phase 5 — Portfolio construction  `[x]`

Five objectives, Black-Litterman, CVaR, and a constraint set that names its own
contradictions.

- [x] `quant/portfolio/constraints.py`: budget, bounds, gross, net, group and
      turnover limits, with a feasibility pre-check that reports *which*
      constraints contradict
- [x] `quant/portfolio/optimisation.py`: minimum variance, maximum Sharpe,
      mean-variance, risk parity — multi-start SLSQP, with the starts attempted
      and converged both reported
- [x] `quant/portfolio/black_litterman.py`: equilibrium returns from a supplied
      prior, blended with views carrying their own uncertainty
- [x] `quant/portfolio/cvar.py`: the Rockafellar-Uryasev linear program, with
      gross and turnover limits carried as auxiliary variables
- [x] Ledoit-Wolf shrinkage in `quant/statistics/covariance.py`, requested by
      name and reporting the intensity it chose
- [x] `domains/portfolio/optimisation.py` and `/portfolio-optimisation/target`,
      plus the construction UI

**Acceptance — verified**

The phase's own criterion — *signals → optimiser → target portfolio → risk
metrics* — is `tests/integration/test_portfolio_optimisation.py`, with the
covariance estimated from a real warehouse dataset loaded through the Phase 3
path.

| Criterion | Where |
| --- | --- |
| A target portfolio comes back, respecting budget and sign | `test_a_minimum_variance_portfolio_comes_back` |
| **The risk of that portfolio is reported beside it** | `test_the_risk_of_the_portfolio_is_reported_beside_it` — volatility, historical VaR and expected shortfall, effective assets, and the observation count |
| Risk contributions are reported per holding | `test_risk_contributions_are_reported_per_holding` — a holding with 5% of the weight and 40% of the risk is the portfolio's real position |
| Minimum variance beats every single asset | `tests/unit/test_portfolio_optimisation.py::test_it_beats_every_single_asset` — the claim of diversification, and a check the objective is minimised rather than merely evaluated |
| Risk parity equalises the contributions | `test_every_asset_contributes_the_same_risk`, `test_risk_parity_equalises_the_contributions` |
| **A return-seeking objective with no forecast is refused** | `test_a_return_seeking_objective_with_no_forecast_is_refused` — mean-variance maximises the error in its expected returns, so quietly substituting sample means would build a confident portfolio on the worst input |
| Historical means can be asked for and are warned about | `test_historical_means_can_be_asked_for_and_are_warned_about`, `test_historical_means_are_flagged_wherever_they_are_used` |
| A supplied forecast is used and named | `test_a_supplied_forecast_is_used_and_named` |
| **Mean-variance without a risk aversion is refused** | `test_mean_variance_without_a_risk_aversion_is_refused` — it is a statement about a person's tolerance, not a property of the market |
| Black-Litterman without `tau` is refused | `test_black_litterman_without_tau_is_refused` — no consensus value exists, so the platform will not pick one |
| A prior with no views gives equilibrium returns | `test_a_prior_with_no_views_gives_equilibrium_returns`, `test_with_no_views_the_posterior_is_the_prior` |
| A view moves the asset it names, and its neighbours | `test_a_view_moves_the_asset_it_names`, `test_a_view_moves_correlated_assets_too` |
| A more confident view moves the answer further | `test_a_more_confident_view_moves_the_answer_further` |
| A view held with certainty is refused | `test_a_view_held_with_certainty_is_refused` — that is a constraint, not a view |
| The posterior covariance exceeds the prior | `test_the_posterior_covariance_exceeds_the_prior` — estimation uncertainty in the mean adds to the covariance of returns |
| **Impossible constraints name the contradiction** | `test_impossible_constraints_name_the_contradiction`, `test_a_budget_outside_the_gross_limit_is_named`, `test_a_minimum_above_a_maximum_is_named_per_asset` — "no solution" is useless when six limits are in play |
| Weight, group and turnover limits bind and are reported | `TestConstraintsBind`, `test_a_maximum_weight_binds_and_is_reported` |
| A turnover limit without a starting point is refused | `test_a_turnover_limit_without_a_starting_point_is_refused` — turnover from nowhere is not a quantity |
| **CVaR avoids the risk variance cannot see** | `test_it_avoids_the_asset_variance_cannot_see` — a fat left tail that a covariance rates as ordinary |
| CVaR is never below VaR | `test_cvar_is_never_below_var` |
| A thin tail is reported, not averaged over anyway | `test_a_thin_tail_is_reported_rather_than_averaged_over_anyway` — a CVaR from five points is a number, not an estimate |
| Shrinkage is asked for by name and reported | `test_shrinkage_is_asked_for_by_name`, `test_shrinkage_is_asked_for_and_reported` |
| Shrinkage improves the conditioning | `test_shrinkage_improves_the_conditioning` — the noise in a sample covariance lands where an optimiser looks for its cleverest trades |
| Effective assets counts the spread, not the holdings | `test_effective_assets_counts_the_spread_not_the_holdings` |
| Instruments with no history are reported, not dropped | `test_instruments_with_no_history_are_reported_not_silently_dropped` — a weight of zero and an absent asset are different things |

**What is deliberately not here**: a default risk aversion, market-cap weights,
and margin or liquidity constraints. The first would be choosing a portfolio on
the user's behalf. The second is data the platform does not hold. The third and
fourth are named in the build spec and are genuinely wanted — but margin needs a
broker's formula, which build spec 1.1 forbids inventing, and liquidity needs
volume joined to a participation assumption. Each would be wrong if guessed, so
neither is offered rather than being offered badly.

Also absent: automatic conversion of strategy signals into expected returns. A
signal says "hold 40% long"; it does not say what return is expected.
`views_from_signals` exists and **requires** a stated `return_scale` — what a
full-weight signal is worth — because a platform that picked one would be
inventing the view rather than translating it.

---

## Real-time Phase 6 — Paper trading  `[x]`

**Acceptance** (build spec §46): *Signal → Risk check → Paper order → Fill →
Portfolio → P&L*.

The slice is the order lifecycle, end to end, with a broker on the far side of
an interface rather than a special case in the middle of the service. What makes
it a phase and not a stub is that the paper broker is the *same interface* the
live broker implements in Phase 7 — build spec §23 is explicit that paper
trading must use the same order interface as live trading, and the only way to
know that is true is to have written the second one against it.

- [x] `BrokerAdapter` — `place_order` / `cancel_order` / `modify_order` /
      `positions` / `orders` / `account`, with a declared `capabilities` set so
      an adapter that cannot modify an order says so instead of failing at the
      call.
- [x] `PaperBroker` over a **pure** fill engine: `decide_fill(order, quote,
      policy, as_of)`. Deterministic and independently testable; the adapter
      only supplies the quote and persists the outcome.
- [x] Order lifecycle over exactly the six states build spec §25 names — `NEW`,
      `ACKNOWLEDGED`, `PARTIALLY_FILLED`, `FILLED`, `CANCELLED`, `REJECTED` —
      with the legal transitions declared as a table and an illegal one raising.
- [x] `client_order_id` as an idempotency key. A resubmitted id returns the
      order that already exists rather than placing a second one.
- [x] Pre-trade risk gate: every check named, every refusal recorded as a
      `REJECTED` order with its reason. An order is never silently resized.
- [x] Positions, cash and realised P&L through the **existing** `Book` from
      `domains/research/models.py`, so the paper account and a backtest of the
      same fills are computed by one implementation rather than two that agree
      until they do not.
- [x] Live risk on the paper account through the existing valuation and
      exposure engines.
- [x] `POST /trading/accounts/{id}/rebalance-preview` — the trades required to
      reach a **stated** target, which is arithmetic on the user's own target
      and not a recommendation.
- [x] Frontend: `web/app/trading/`.

**Deliberate refusals, decided before writing the code**

| Question | Answer |
| --- | --- |
| What does a paper market order fill at with no two-sided market? | It does not fill. `PaperFillPolicy.QUOTE_ONLY` is the default; filling at the last trade is a different policy that has to be asked for, because a trade print is not a quote — the same rule that makes `Quote.mid_price` return `None`. |
| What fills when the quote reports no depth? | The full quantity, flagged `DEPTH_NOT_REPORTED`. Asserting a complete fill without a size on the quote is asserting liquidity nobody saw, and the flag is what stops that reading as an observation. |
| What are the brokerage, STT and GST? | Whatever the user's `CostSchedule` says. There is no default schedule: statutory rates are exchange and régime rules, and build spec 1.1 forbids inventing them. With no schedule the P&L is **gross** and labelled so — `NO_COST_MODEL`, reused from Phase 4. |
| Is a fill an observation? | No. Every paper fill carries `PAPER_FILL_COUNTERFACTUAL` and the `exchange_timestamp` of the quote it was decided against, so it can never be read back as something the market did. |
| Does the platform decide what to trade? | No. `rebalance-preview` differences the current book against a target the user supplied. There is no endpoint that produces an order the user did not ask for. |


**Evidence**

| Claim | Test |
| --- | --- |
| The acceptance path runs end to end | `test_an_order_fills_against_the_live_quote_and_reaches_the_pnl` — an Upstox frame becomes a live quote, an order fills at the ask, and the position and cash reach the P&L |
| A fill needs a quote to rest on | `test_a_market_order_with_no_offer_does_not_fill` — the rule behind `Quote.mid_price` returning `None`, at the point of execution |
| Filling at a trade print has to be asked for | `test_the_permissive_policy_has_to_be_chosen_on_the_account`, `test_filling_at_the_last_trade_has_to_be_asked_for` |
| A stale quote does not fill | `test_a_stale_quote_does_not_fill` — stricter than valuation, deliberately |
| A full fill with no published depth is flagged | `test_a_full_fill_with_no_published_depth_is_flagged` — asserting a complete fill without a size asserts unseen liquidity |
| A marketable limit pays the touch | `test_a_marketable_limit_pays_the_touch_not_its_own_limit` — booking at the limit would invent cost the market never charged |
| A resting order is not a failed one | `test_a_resting_order_is_not_rejected_for_being_unfillable` |
| The gate refuses rather than resizes | `test_a_position_limit_refuses_rather_than_trims`, `test_an_order_over_the_limit_is_recorded_as_rejected` |
| Every breach is listed, not only the first | `test_every_breach_is_listed_not_only_the_first` |
| Checks that could not run say so | `test_every_check_is_reported_even_when_it_passes`, `test_the_band_is_not_applied_without_a_two_sided_quote` |
| An unmeasured loss limit is not a limit | `test_a_loss_limit_with_no_loss_supplied_refuses` |
| An unlimited live account is refused | `test_a_live_account_may_not` |
| The cash check says it is not a margin check | `test_the_cash_check_says_it_is_not_a_margin_check` |
| The kill switch cancels what is resting | `test_the_kill_switch_halts_the_account_and_cancels_what_rests` — a working order is exposure the switch was pulled to stop |
| A halt always carries a reason | `test_a_kill_switch_without_a_reason_is_refused`, plus `ck_kill_switch_has_a_reason` |
| A retried submission is safe | `test_a_repeated_client_order_id_does_not_place_a_second_order` |
| An unpriced position withholds equity | `test_an_unpriced_position_suppresses_the_equity_figure` |
| With no schedule the P&L says it is gross | `test_with_no_schedule_the_pnl_says_it_is_gross` |
| A supplied schedule is charged and recorded | `test_a_supplied_schedule_is_charged_and_travels_into_provenance` — including GST on capped brokerage |
| **The book is the fills** | `test_replaying_the_fills_reproduces_the_stored_position` — the stored position is a cache, and this is what keeps it honest |
| The audit trail records the refusal too | `test_a_rejection_records_why_before_anything_else_happens` — and records **no** `BROKER_REQUEST`, which is the evidence nothing was sent |
| An impossible state change fails loudly | `test_a_terminal_order_goes_nowhere`, `test_a_partially_filled_order_cannot_be_rejected` |
| Nothing recommends anything | `test_no_response_field_recommends_anything` |

**A dialect bug found while building this.** `DecimalType` is NUMERIC on
Postgres and TEXT elsewhere, so a CHECK written as `filled_quantity <= quantity`
compares *strings* on SQLite — where `'4' <= '10'` is false and `'40' <= '10'` is
true. The constraint would have rejected the honest case and admitted the
impossible one. Every decimal comparison in the trading tables casts to NUMERIC,
which is a no-op on Postgres, and
`test_the_database_refuses_an_order_that_filled_more_than_it_asked` asserts the
constraint bites on the dialect where it would otherwise invert. Older tables
have simpler decimal CHECKs (`quantity <> 0`) that are correct by accident rather
than by construction; they are worth a sweep and are not one.

---

## Real-time Phase 7 — Live trading  `[x]`

**Build spec §46**: *only after paper trading is stable*. `UpstoxBroker`, live
OMS, execution algorithms, kill switch, risk limits, audit logs, and
`LIVE_TRADING_ENABLED=false` by default.

Most of this phase was already built, because Phase 6 was built as though this
one existed. The OMS, the gate, the kill switch and the audit trail are not
duplicated for live; they are the same code, and the venue is a field. What is
genuinely new is the second adapter — and writing it was the test of whether
build spec §23's "same order interface" claim was true. It was: no OMS method
changed to accommodate it.

- [x] `UpstoxBroker` implementing the same six methods as `PaperBroker`.
- [x] Live routing in the OMS, with the credential coming from the Phase-0
      vault per request rather than from the environment.
- [x] Arming: a per-account act, independent of the deployment flag.
- [x] Execution algorithms connected to order placement, reusing the existing
      TWAP/VWAP/POV/liquidity-adaptive schedulers rather than growing a second
      set.
- [x] Kill switch and mandatory risk limits — Phase 6, applying here by venue.
- [x] Audit log — Phase 6, and it now records arming attempts and their refusals.

**Three gates, and why there are three**

| Gate | Question it answers | Where |
| --- | --- | --- |
| `live_trading_enabled` | May this *installation* trade real money? | Deployment config, default `false` |
| `live_armed_at` | Is this *book* meant to be trading right now? | Per account, an explicit act, cleared by the kill switch |
| `verified_against_documentation` | Has anybody checked this adapter against the broker's published contract? | Per adapter, default `false` |

The first two are the build spec's requirement plus the observation that a
configuration flag alone is a single point of failure. The third is the one
this phase added on its own account, and it is the most important.

**Why an unverified adapter cannot place an order.** A wrong field name fails
loudly — the broker returns an error and somebody fixes it. A wrong *status*
mapping does not: it tells the platform an order filled when it did not, the
book is then wrong, and nothing anywhere reports a problem. The endpoints, the
request field names and the status vocabulary in `UpstoxBroker` were written
without reading the broker's published contract, and build spec 1.1 forbids
presenting that as verified. So the adapter is complete, wired and tested
against recorded payloads, and it refuses to send a live order until a
deployment that has done the checking sets the flag. A paper account is
unaffected.

**And an unrecognised status is an error.** `DEFAULT_STATUS_MAP` is deliberately
not exhaustive-by-guessing. A broker state absent from it raises
`UnknownBrokerStatus` rather than being rounded to the nearest plausible
neighbour — "complete" and "cancelled" are both terminal, and treating one as
the other either loses a position or invents one.

**Evidence**

| Claim | Test |
| --- | --- |
| An unverified mapping sends nothing | `test_an_unverified_adapter_will_not_place_a_live_order` — and asserts the transport recorded no call |
| The shipped default is unverified | `test_the_default_endpoint_set_is_unverified` |
| An unknown status stops rather than guesses | `test_an_unknown_status_is_an_error_not_a_guess`, `test_the_error_lists_the_states_it_does_know` |
| An acknowledgement is not read as working | `test_a_placement_acknowledgement_is_not_read_as_working` — an id and nothing else means acknowledged |
| A completed order with nothing filled is refused | `test_a_completed_order_with_nothing_filled_is_refused` — either the mapping or the payload is wrong |
| A fill's side comes from the broker's own field | `test_a_sell_is_booked_negative_from_the_brokers_own_field` — a fill on the wrong side inverts a position |
| The broker's average is named as an average | `test_a_completed_order_produces_a_fill_named_for_what_it_is` — `BROKER_REPORTED_AVERAGE`, not a trade price |
| Unmapped fields are reported | `test_fields_the_adapter_does_not_map_are_reported` |
| A 5xx is an unknown outcome | `test_a_server_error_is_an_unknown_outcome_not_a_rejection` |
| Margin stays attributed to the broker | `test_margin_figures_keep_their_attribution` — no field on the payload is a bare `margin` |
| Live accounts are created unarmed | `test_a_live_account_is_created_unarmed` |
| Arming lists every obstacle at once | `test_arming_reports_every_obstacle_at_once` |
| A paper account cannot be armed | `test_a_paper_account_cannot_be_armed` |
| An unarmed live order is refused and recorded | `test_an_unarmed_live_order_is_refused_and_recorded` |
| Nothing reaches a broker while disabled | `test_a_live_order_never_reaches_a_broker_while_disabled` — no `BROKER_REQUEST` in the trail |
| Disarming is not the kill switch | `test_disarming_is_not_the_kill_switch` — one pauses, the other cancels |
| Only the open slice is placed | `test_only_the_open_slice_is_placed` |
| A repeated call places each slice once | `test_calling_it_twice_does_not_place_the_slice_twice` |
| A closed slice is missed, not placed late | `test_a_closed_slice_is_reported_missed_not_placed_late` |
| A VWAP without a volume profile is refused | `test_a_vwap_without_a_volume_profile_is_refused` — falling back to TWAP answers a different question under this name |
| Nothing claims an optimal execution | `test_no_response_here_claims_an_optimal_execution` |

**Deliberately not built**

| Not built | Why |
| --- | --- |
| Order modification against the live broker | The modify contract is unconfirmed, and an amendment that silently becomes a no-op leaves an order working at terms nobody chose. `MODIFY` is absent from the capability set, so the OMS refuses the instruction rather than sending it. |
| Automatic reconciliation of broker positions against the platform's | `get_positions()` returns the broker's view, kept beside ours. Overwriting one with the other destroys the only evidence they ever disagreed, which is the entire reason for asking. |
| A resubmit-on-timeout retry | A broker that could not be reached leaves the order `NEW` with the failure recorded. Retrying an indefinite outcome is how duplicate orders get sent. |
| An intraday volume profile | POV and VWAP need one and the platform does not hold one. A forecast invented here would make both produce confident schedules on a number nobody supplied. |
