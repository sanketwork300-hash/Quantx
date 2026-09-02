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

## Phase 4 — Portfolio  `[ ]`

- [ ] Portfolio + position CRUD; CSV import with column mapping and preview
- [ ] Instrument resolution with `resolved / ambiguous / invalid` buckets
- [ ] Position valuation, per-position Greeks, currency conversion
- [ ] Aggregation by underlying / expiry / asset class / currency

**Acceptance**: sum of position values equals portfolio value within tolerance
(property test); ambiguous rows are never auto-resolved; every valuation records
`valuation_method`.

## Phase 5 — Risk  `[ ]`

- [ ] Historical VaR with full repricing for nonlinear books
- [ ] Parametric VaR (assumptions stated in the response)
- [ ] Monte Carlo VaR (job) with seed reproducibility
- [ ] Expected shortfall
- [ ] Scenario engine + shock types; stress with full revaluation
- [ ] Risk contribution by instrument / underlying / expiry / asset class
- [ ] UI: portfolio dashboard, stress lab

**Acceptance**: VaR recovers the analytic quantile on synthetic distributions;
MC is seed-reproducible; stress on an option book reprices rather than
extrapolating Greeks, and a test proves the two differ for a large shock.

## Phase 6 — Margin  `[ ]`

- [ ] `MarginModel` ABC + `SimpleRiskMarginModel`
- [ ] Margin utilisation, shock-grid evaluation, margin-buffer curve
- [ ] Estimated margin-shortfall region

**Acceptance**: every margin result carries method, assumptions, confidence and
warnings; no output names a broker or claims broker equivalence; the shortfall
output is a region with assumptions, never a single guaranteed price.

## Phase 7 — Execution TCA  `[ ]`

- [ ] Trade-log upload, parent/child grouping
- [ ] Benchmarks: arrival, decision, prevailing mid, VWAP, TWAP, close
- [ ] Implementation shortfall (currency / bps / %)
- [ ] Model-based cost decomposition + data-coverage reporting
- [ ] UI: execution dashboard

**Acceptance**: deterministic synthetic price paths produce hand-checkable IS
values; every benchmark reports its window, source and method; low data coverage
degrades to `PARTIAL`, never to a confident wrong number.

## Phase 8 — Execution simulation  `[ ]`

- [ ] `ExecutionStrategy` ABC; TWAP, VWAP, POV, liquidity-adaptive
- [ ] `MarketImpactModel` ABC; square-root and linear baselines
- [ ] Counterfactual simulator; strategy comparison

**Acceptance**: every simulated result is labelled a counterfactual estimate;
schedules sum to the parent quantity; impact models are unit-tested against
closed-form expectations.

## Phase 9 — Advanced derivatives  `[ ]`

- [ ] SSVI / arbitrage-aware global surface
- [ ] Dupire local volatility from a smooth surface, with invalid regions
- [ ] Crank-Nicolson PDE pricer (+ optional MC)
- [ ] Heston (characteristic function or QuantLib), constrained calibration
- [ ] Model consensus + confidence; risk-neutral density
- [ ] Higher-order Greeks: vanna, volga, charm

**Acceptance**: with constant local vol the PDE converges to Black-Scholes
(order-of-convergence test); Heston cross-checks against QuantLib; consensus
output exposes dispersion and never a single "true" price.

## Phase 10 — Microstructure  `[ ]`

Only with adequate event data: L2 Parquet storage, imbalance, microprice, depth,
book slope, trade/cancel intensity, queue model, Hawkes. Each gated on a data
availability check, and Hawkes must beat a Poisson baseline on held-out data
before it ships.

## Phase 11 — Unified order analysis  `[ ]`

Compose valuation + surface + risk + margin + execution over one `MarketState`.

**Acceptance**: one `market_state_id` in the provenance of all five branches;
branch failure degrades to `PARTIAL`; the response schema contains no
recommendation field, asserted by test.
