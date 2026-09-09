# Sequence Diagrams

ASCII sequence diagrams for the flows that define the system. All are
**implemented**; each names the phase that built it.

---

## 1. Option-chain ingestion  **[implemented, Phase 0]**

```
User      API              ObjectStore   JobSvc   Worker   Parser  Validator  QualityEngine  Resolver   DB
 |         |                    |           |        |        |        |            |            |       |
 |-upload->|                    |           |        |        |        |            |            |       |
 |         |--size/MIME/ext---->|           |        |        |        |            |            |       |
 |         |   checks (stream)  |           |        |        |        |            |            |       |
 |         |--put(sha256 key)-->|           |        |        |        |            |            |       |
 |         |--insert uploads row------------------------------------------------------------------------>|
 |<-201 upload_id---------------|           |        |        |        |            |            |       |
 |         |                    |           |        |        |        |            |            |       |
 |-preview>|                    |           |        |        |        |            |            |       |
 |         |--get(head N rows)->|           |        |        |        |            |            |       |
 |         |--parse with candidate mapping------------------->|        |            |            |       |
 |<-mapped sample + inferred types + per-column warnings------|        |            |            |       |
 |         |            (nothing persisted)                            |            |            |       |
 |         |                                                           |            |            |       |
 |-ingest->|                                                           |            |            |       |
 |         |--create job(QUEUED)-->|        |                          |            |            |       |
 |<-202 job_id------------|       |         |                          |            |            |       |
 |         |              |--enqueue------->|                          |            |            |       |
 |         |                               |--RUNNING, progress=0------------------------------------->|
 |         |                               |--get object-->|           |            |            |       |
 |         |                               |--parse------->|           |            |            |       |
 |         |                               |   rows + row_index preserved           |            |       |
 |         |                               |--validate---------------->|            |            |       |
 |         |                               |   structural + domain rules            |            |       |
 |         |                               |   -> ValidatedRow(ok | rejected+reason)|            |       |
 |         |                               |--resolve instruments------------------------------->|       |
 |         |                               |   RESOLVED / AMBIGUOUS / UNRESOLVED                 |       |
 |         |                               |   (create_missing_instruments -> uuid5 upsert)      |       |
 |         |                               |--normalize (UTC, Decimal, canonical OptionQuote)    |       |
 |         |                               |--score------------------------------->|            |       |
 |         |                               |   per-quote scores + flags            |            |       |
 |         |                               |   chain-level consistency checks       |            |       |
 |         |                               |--apply exclusion policy (severity threshold)        |       |
 |         |                               |   every excluded row gets exactly one primary reason|       |
 |         |                               |--persist snapshot + kept + excluded + quality report------->|
 |         |                               |--write result ref, COMPLETED------------------------------>|
 |-poll--->|--read job--------------------------------------------------------------------------------->|
 |<-COMPLETED + snapshot_id-----|          |                                                            |
 |-GET /market/chains/{id}----->|----------------------------------------------------------------------->|
 |<-kept + excluded + reasons + quality-----|                                                            |
```

Key properties: the file lands in object storage **before** parsing; parsing runs
in the worker, never in the request thread; no row is dropped without a reason
row; the whole run is one job with a queryable status.

## 2. Volatility engine (Phase 1 -> Phase 2)

```
Client    API      SurfaceSvc   ChainRepo  Cleaner  ForwardEst  IVEngine  SVICalib  ArbValidator  DB/Cache
  |        |            |           |         |         |          |         |           |            |
  |-POST /derivatives/surfaces/calibrate----->|         |          |         |           |            |
  |<-202 job_id--------|            |         |         |          |         |           |            |
  |                    |--load chain snapshot->|        |          |         |           |            |
  |                    |--clean--------------->|------->|          |         |           |            |
  |                    |   drop/flag: crossed, zero, stale, sub-intrinsic,   |           |            |
  |                    |   above-bound, illiquid, wide -> kept + excluded[]  |           |            |
  |                    |--estimate forward per expiry----------->|           |            |            |
  |                    |   (a) spot-carry  (b) futures  (c) put-call parity regression   |            |
  |                    |   -> value, method, confidence, residual_error                  |            |
  |                    |--solve IV per quote----------------------------->|               |            |
  |                    |   Black-76 on forward; Brent bracketed [1e-6, 5.0]              |            |
  |                    |   -> iv, converged, iterations, bounds, solver                   |            |
  |                    |--raw smile: k = ln(K/F), w = iv^2 * T                            |            |
  |                    |--arbitrage diagnostics on RAW quotes---------------->|           |            |
  |                    |   bounds / parity / vertical / butterfly / calendar             |            |
  |                    |   -> raw_market_violations[]                                     |            |
  |                    |--calibrate SVI per expiry-------------------------->|            |            |
  |                    |   weights from spread + liquidity; constrained SLSQP            |            |
  |                    |   -> a,b,rho,m,sigma, rmse, weighted_rmse, status               |            |
  |                    |--arbitrage diagnostics on FITTED surface------------->|          |            |
  |                    |   butterfly (g(k) >= 0), calendar (dw/dT >= 0)                   |            |
  |                    |   -> fitted_surface_violations[]                                 |            |
  |                    |--persist surface + slices + params + metrics + provenance------------------->|
  |                    |--cache surface:{underlying}:{as_of}:{market_state_id}----------------------->|
  |-GET /derivatives/surfaces/{underlying}--->|                                                        |
  |<-raw obs + fitted params + reference IVs + violations + calibration metrics + confidence           |
```

Ordering is deliberate: arbitrage is checked on the **raw** market first, so a
bad fit is never blamed on the market and a bad market is never hidden by a
smooth fit. Both violation sets are stored and reported separately.

## 3. Portfolio risk (Phase 4 -> Phase 5 -> Phase 6)

```
Client   API    RiskSvc  PortfolioSvc  MarketStateBuilder  SurfaceSvc  Pricer  ScenarioEngine  MarginSvc  DB
  |       |        |          |               |                 |         |          |            |       |
  |-GET /risk/{pid}/summary-->|               |                 |         |          |            |       |
  |       |--ownership check (404 if not owner)                 |         |          |            |       |
  |       |        |--load portfolio + positions-->|            |         |          |            |       |
  |       |        |--resolve instruments--------->|            |         |          |            |       |
  |       |        |--build MarketState(as_of, universe)------->|         |          |            |       |
  |       |        |   quotes, spots, futures, curves, FX, surfaces; content-hash -> state_id     |       |
  |       |        |   per-instrument quality attached                                            |       |
  |       |        |--fetch/build reference surfaces----------->|         |          |            |       |
  |       |        |--value each position + Greeks------------------------>|         |            |       |
  |       |        |   position Greek = quantity * multiplier * unit Greek           |            |       |
  |       |        |   units stated explicitly (vega per +1 vol point, theta per day)|            |       |
  |       |        |--aggregate by portfolio / underlying / expiry / asset class / currency       |       |
  |       |        |--VaR                                                                          |       |
  |       |        |   historical: aligned factor returns -> full reprice per scenario             |       |
  |       |        |   parametric: linear-exposure covariance (labelled, not sole measure)         |       |
  |       |        |   MC: factor model -> paths -> reprice -> P&L distribution     (job)          |       |
  |       |        |--ES = mean loss | loss > VaR                                                  |       |
  |       |        |--stress: for each scenario--------------------------->|            |          |       |
  |       |        |   shock MarketState (spot %, vol pts, rate bp, FX %) -> FULL revaluation      |       |
  |       |        |   -> pnl, new greeks, contributions                                           |       |
  |       |        |--margin------------------------------------------------------------>|         |       |
  |       |        |   method + assumptions + estimate + confidence + warnings           |         |       |
  |       |        |--persist risk_snapshot + provenance----------------------------------------->|       |
  |<-summary: value, pnl, greeks, VaR/ES, worst stress, margin utilisation, warnings, provenance   |       |
```

Large portfolios or Monte Carlo return a `job_id` instead of blocking. The
snapshot is timestamped and reproducible from its `market_state_id`.

## 4. Transaction cost analysis (Phase 7)

```
Client   API    ExecSvc  Upload/Parser  Resolver  BenchmarkSvc  MarketDataSvc  ImpactModel  DB
  |       |        |          |             |           |             |             |        |
  |-POST /executions/upload-->|             |           |             |             |        |
  |       |--store file, create job-------->|           |             |             |        |
  |       |        |--parse trade log------>|           |             |             |        |
  |       |        |   required: timestamp, symbol, side, quantity, price           |        |
  |       |        |   optional: order_id, parent_order, order_type, limit_price,   |        |
  |       |        |             submit_timestamp, broker, fees                     |        |
  |       |        |--resolve instruments-------------->|             |             |        |
  |       |        |   AMBIGUOUS rows returned to user, never guessed                        |
  |       |        |--group child fills into parent orders                                   |
  |       |        |--persist orders + executions (append-only)---------------------------->|
  |                                                                                          |
  |-GET /executions/{parent_id}/tca------->|                                                  |
  |       |        |--fetch parent + fills                                                    |
  |       |        |--benchmark window resolution------->|             |                      |
  |       |        |   arrival = prevailing mid at submit_timestamp (or first fill if absent, |
  |       |        |             flagged ARRIVAL_PROXY_USED)                                  |
  |       |        |   VWAP/TWAP over [start, end] from bars/trades--->|                      |
  |       |        |   close price, decision price if supplied                                |
  |       |        |   each benchmark records window + data source + method                   |
  |       |        |--implementation shortfall: sign * (P_exec - P_arrival) * Q               |
  |       |        |   reported in currency, bps and %                                        |
  |       |        |--cost decomposition (MODEL-BASED, labelled):                             |
  |       |        |     spread cost   = 0.5 * quoted spread at fill                          |
  |       |        |     impact        = estimate------------------------->|                  |
  |       |        |     timing        = residual after spread + impact + fees                |
  |       |        |     fees          = observed                                             |
  |       |        |     opportunity   = unfilled qty * (benchmark - decision)                |
  |       |        |--data coverage check: bars/quotes available over the window?             |
  |       |        |   insufficient -> PARTIAL with TCA_DATA_COVERAGE_LOW                     |
  |       |        |--persist execution_report + provenance-------------------------------->|
  |<-IS, bps, benchmark comparison, decomposition, coverage, warnings                         |
```

The decomposition is explicitly labelled model-based: spread, impact and timing
are not separately observable, and the residual definition is stated in the
response rather than presented as measurement.

## 5. Advanced derivatives (Phase 9)

```
Client   API   AdvancedSvc  ChainAnalysis  SSVICalib  Dupire  Density  HestonCalib  DB
  |       |         |            |             |         |        |         |        |
  |-POST /derivatives/analyses/{id}/global-surface--------------------------------->|
  |       |--create job, 202----------------------------------------------------->  |
  |       |         |--rehydrate the analysis FROM THE DATABASE---->|               |
  |       |         |   (not carried over in memory: a surface refitted from stored |
  |       |         |    rows in six months must be the same surface)               |
  |       |         |--one SLSQP fit over EVERY expiry at once----->|               |
  |       |         |   free: rho, eta, gamma, theta_1..theta_n                     |
  |       |         |   constrained: theta non-decreasing  (= no calendar arbitrage)|
  |       |         |                Theorem 4.2 bounds    (sufficient only)        |
  |       |         |                Durrleman g >= 0      (the actual condition)   |
  |       |         |--persist global_surfaces + slices--------------------------->|
  |       |         |   CHECK refuses a CONVERGED row that is not arbitrage-free    |
  |       |         |--Dupire grid on the fitted surface----->|                     |
  |       |         |   analytic dw/dk, d2w/dk2, dw/dT; NO bumping                  |
  |       |         |   denominator ~ 0 -> the point is a HOLE with its reason      |
  |       |         |--persist local_volatility_surfaces------------------------->|
  |       |         |   CHECK total_points = valid_points + flagged_points          |
  |       |         |--Breeden-Litzenberger per expiry---------------->|            |
  |       |         |   quantiles ONLY if non-negative AND normalised               |
  |       |         |--persist risk_neutral_densities---------------------------->|
  |       |         |--Heston: vega-weighted SLSQP on the same quotes-->|          |
  |       |         |   Feller reported; enforced only on request                   |
  |       |         |--persist heston_calibrations------------------------------->|
  |<-surface, calibration, local vol, densities, Heston, warnings                    |
  |                                                                                 |
  |-POST /derivatives/consensus {instrument_id}------------------------------------>|
  |       |   422 here, not a failed job, if the instrument is not a vanilla option  |
  |       |         |--load the global surface from its stored parameters           |
  |       |         |--reference IV at (K, T) -> the ONE volatility every model sees |
  |       |         |--BSM | Dupire PDE | Heston | Monte Carlo (seeded)              |
  |       |         |   a model whose inputs are missing returns a REASON, not a gap |
  |       |         |--observed two-sided mid at or before the surface's own as-of   |
  |       |         |   later quote would report the market moving as model error    |
  |       |         |--median, range, dispersion, confidence contributions           |
  |       |         |--persist model_consensus_runs + model_values--------------->|
  |       |         |   CHECK value XOR unavailable_reason                          |
  |       |         |   CHECK median lies inside [reference_low, reference_high]     |
  |<-reference_value, reference_range, dispersion, per-model values, confidence      |
```

There is no step in which a model is chosen. The output is the set of values and
the width of the interval containing them; picking one would be a judgement
about which set of wrong assumptions is least wrong today, and the platform does
not make it.

## 6. Microstructure (Phase 10)

The shape of this flow is different from every other one in this document, and
deliberately so: the judgement about what the data can support is made **once,
at import**, and every later call reads it rather than re-deriving it.

```
Client   API   MicroSvc  Importer  Gate  ObjectStore  DB   quant/microstructure
  |       |        |         |       |        |        |            |
  |-POST /uploads (kind=BOOK_SNAPSHOTS) ---------------->|           |
  |-POST /uploads (kind=BOOK_EVENTS) ------------------->|           |
  |
  |-POST /microstructure/datasets/preview -->|
  |       |        |--parse both halves----->|            |          |
  |       |        |   wide-CSV level detection, or canonical parquet|
  |       |        |   every bad row kept with its row number + reason
  |       |        |--profile + assess------------->|     |          |
  |<--detected columns, counts, and what this WOULD support (writes nothing)
  |
  |-POST /microstructure/datasets (confirmed mapping) --->|
  |       |--create job, 202--------------------------------------->|
  |       |        |--parse, then write parquet--------->|           |
  |       |        |   snapshots.parquet, events.parquet, rejections.json
  |       |        |--assess ONCE------------------>|     |          |
  |       |        |--store dataset row + the full availability report->|
  |
  |-POST /microstructure/datasets/{id}/analyze ---------->|
  |       |        |--require(TOP_OF_BOOK) from the STORED report--->|
  |       |        |   refused -> 422 with reason + evidence, and stop
  |       |        |--read snapshots parquet----------->|            |
  |       |        |--measure every snapshot------------------------>|
  |       |        |   each measure carries its observation count and
  |       |        |   the reasons the rest had no such measurement
  |
  |-POST /microstructure/datasets/{id}/intensity -------->|
  |       |        |--require(EVENT_INTENSITY [, CANCELLATION_INTENSITY])
  |       |        |--fit Poisson AND Hawkes on the training window->|
  |       |        |--score both on the held-out window------------->|
  |       |        |--Diebold-Mariano on the per-event gain--------->|
  |       |        |--store BOTH, with the verdict between them----->|
  |
  |-POST /microstructure/datasets/{id}/queue  (inline, not a job)
  |       |        |--require(QUEUE_POSITION)
  |       |        |--read the level, count departures at that price->|
  |<--a bracket: two ends, two assumptions, and no single number
```

Three things this diagram is drawing attention to.

**The gate is consulted, not recomputed.** `require_capability` reads the report
stored at import. Deciding at call time would let four endpoints drift into four
different opinions about what the data supports.

**Both intensity models are stored whatever the verdict.** "It was tried and did
not earn its parameters here" is the evidence that the comparison ran, and a
database CHECK stops a row claiming the self-exciting model without the held-out
statistic that earned it.

**The queue answer is inline and is a range.** Inline because a user moving a
price around should not poll for each answer; a range because its two ends are
the two cancellation-priority assumptions a public feed cannot distinguish.

## 7. Unified order analysis (Phase 11)

```
Client  API   OrderAnalysisSvc  MarketStateBuilder  ValuationSvc  SurfaceSvc  ExecSvc  RiskSvc  MarginSvc
  |      |            |                 |                |            |          |        |         |
  |-POST /order-analysis--------------->|                |            |          |        |         |
  |      |--ownership check on portfolio_id              |            |          |        |         |
  |      |            |--build ONE MarketState---------->|            |          |        |         |
  |      |            |   every branch below uses this same state_id  |          |        |         |
  |      |            |                                               |          |        |         |
  |      |            |--valuation------------------------>|          |          |        |         |
  |      |            |   market mid (observed) + reference range across models  |        |         |
  |      |            |   model dispersion -> confidence                         |        |         |
  |      |            |--surface analysis---------------------------->|          |        |         |
  |      |            |   market IV vs reference IV, z-score vs history, liquidity|        |         |
  |      |            |--execution cost------------------------------------------>|       |         |
  |      |            |   market-order slippage / TWAP / VWAP / POV estimates     |       |         |
  |      |            |   labelled counterfactual                                 |       |         |
  |      |            |--incremental risk------------------------------------------------>|         |
  |      |            |   hypothetical portfolio = current + proposed position            |         |
  |      |            |   greeks, VaR, ES, stress: current -> proposed                    |         |
  |      |            |--incremental margin--------------------------------------------------------->|
  |      |            |   margin, utilisation, buffer: current -> proposed                          |
  |      |            |--assemble, merge warnings, single provenance block                          |
  |<-analysis (no recommendation field)                                                              |
```

All five branches read the **same** `MarketState`. That is the entire point of
the snapshot abstraction: the delta shown to the user is attributable to the
order, not to the market moving between five independent calculations.

Branches degrade independently. If surface calibration failed, that branch is
`FAILED` with its reason and the envelope is `PARTIAL`; execution, risk and
margin still return.

Three things the shipped implementation does that the sketch above does not say.

**The proposed position is valued by the code that values a stored one.** It is
built as a `Position` with a derived id, never written, and passed through
`PortfolioValuationService` against the context the book was valued in. That is
what lets `build_exposures` treat it identically, and what makes an order that
cannot be repriced fail for the same reason and in the same vocabulary a stored
position would.

**A zero difference has to mean a zero difference.** An order that could not be
repriced never enters the combined book, so both sides are identical and every
delta is exactly zero. `CombinedBook.order_is_repriceable` records that, and the
risk and margin branches refuse rather than report the zeros.

**One factor panel, not two.** It is built over the *combined* book and used for
both sides, because building one per side would let a new underlying change the
sample the current book is measured on, and the difference would then contain
that change as well as the order.


---

## 8. Live market data  **[implemented, real-time Phase 1]**

Two processes and a store between them. The API never opens a provider
connection, which is what stops two readers getting different answers about the
same instant.

```
User    Web        API           Redis        StreamWorker   Provider   QualityEngine
 |       |          |              |               |             |            |
 |-pick->|          |              |               |             |            |
 |       |--POST /live/subscriptions------------->|              |            |
 |       |          |--interest+TTL->|             |             |            |
 |       |<-200 subscribed---------|               |             |            |
 |       |          |              |               |             |            |
 |       |          |              |<--read interest (5s loop)---|            |
 |       |          |              |               |--resolve instrument+key->|
 |       |          |              |               |             |            |
 |       |          |              |               |--quotes---->|            |
 |       |          |              |               |<--payload---|            |
 |       |          |              |               |--normalise (spec)------->|
 |       |          |              |               |   report: read/missing/unmapped
 |       |          |              |               |--score quote------------>|
 |       |          |              |               |<-MarketDataQuality-------|
 |       |          |              |               |                          |
 |       |          |              |  ordering + duplicate checks             |
 |       |          |              |<--put quote+quality (TTL)--|             |
 |       |          |              |<--put feed health----------|             |
 |       |          |              |               |             |            |
 |       |--GET /live/quotes------>|              |              |            |
 |       |          |--read------->|              |              |            |
 |       |<-prices + age + quality-|              |              |            |
 |       |   + the ids it has none for            |              |            |
 |       |          |              |              |              |            |
 |       |--GET /live/state------->|              |              |            |
 |       |          |--read------->|              |              |            |
 |       |          |--MarketStateBuilder (refuses quotes after as_of)         |
 |       |<-content-addressed state_id------------|              |            |
```

Three points the diagram is drawn to make.

**The normalisation report is on the hot path, not beside it.** Every read says
which fields it found, which mapped paths were absent and which payload keys
nothing claims. A provider renaming a field is visible on the first response
rather than as quotes that gradually become empty.

**Ordering and duplicate checks happen before the store, not after.** A replayed
or reordered quote is rejected there; once a stale price is in the store,
nothing downstream can tell it from a fresh one.

**The state is where the live path ends.** Everything after this diagram —
pricing, surfaces, risk, margin, execution — takes a `MarketState` and cannot
tell whether it came from a feed or from a file, which is the property that
makes a live analysis reproducible later.


---

## 9. Live chain to volatility surface  **[implemented, real-time Phase 2]**

One job, one captured moment, four stages — and almost every box below already
existed. What Phase 2 added is the first arrow.

```
User    API        JobSvc  Worker   LiveStore  Snapshot  IVSolver  SVI    DeltaSkew
 |       |            |       |         |          |         |       |        |
 |-pick->|            |       |         |          |         |       |        |
 |       |--create job|------>|         |          |         |       |        |
 |<-202 job_id--------|       |         |          |         |       |        |
 |       |            |       |         |          |         |       |        |
 |       |            |       |--read chain quotes>|         |       |        |
 |       |            |       |  score against the chain, not the feed         |
 |       |            |       |--write snapshot--------------->|      |        |
 |       |            |       |  input == kept + excluded + rejected           |
 |       |            |       |         |          |         |       |        |
 |       |            |       |--analyse(snapshot_id)------------------>|      |
 |       |            |       |<-implied vols, forwards, per expiry-----|      |
 |       |            |       |         |          |         |       |        |
 |       |            |       |--calibrate(analysis_id)------------------->|   |
 |       |            |       |  + characteristics, + arbitrage reports    |   |
 |       |            |       |<-surface_id-------------------------------|   |
 |       |            |       |         |          |         |       |        |
 |       |            |       |--delta skew off the stored surface------------>|
 |       |            |       |<-25D / 10D risk reversal, butterfly-----------|
 |       |            |       |         |          |         |       |        |
 |--GET /jobs/{id}/result---->|         |          |         |       |        |
 |<-snapshot_id, analysis_id, surface_id, stages, skew-------|       |        |
```

Three points the diagram exists to make.

**The snapshot is written before anything is computed.** Not for durability —
for reproducibility. A surface calibrated from memory cannot be refitted six
months later to check what it said, and there would be two paths through the IV
solver that could disagree.

**Quality is scored at capture, not carried over.** The feed scored each quote
as a standalone observation. An option in a chain is additionally checked
against its own no-arbitrage bounds and its underlying, and that check needs the
chain around it.

**Each stage names the identifier the next one used.** `snapshot_id` →
`analysis_id` → `surface_id`, all in the job result, which is what makes a
number on the surface traceable back to the individual quote it came from.


---

## 10. Historical dataset into the warehouse  **[implemented, real-time Phase 3]**

```
User    API      JobSvc  Worker   Reader  Validator  ObjectStore  Registry  DuckDB
 |       |          |       |        |        |           |          |        |
 |-upload>|         |       |        |        |           |          |        |
 |       |--create job----->|        |        |           |          |        |
 |<-202 job_id------|       |        |        |           |          |        |
 |       |          |       |        |        |           |          |        |
 |       |          |       |--read (CSV or Parquet)----->|          |        |
 |       |          |       |<-rows + parse errors--------|          |        |
 |       |          |       |  symbols resolved, or reported unresolved       |
 |       |          |       |        |        |           |          |        |
 |       |          |       |--validate------>|           |          |        |
 |       |          |       |<-written / excluded / rejected, and flags       |
 |       |          |       |   rows_in == written + excluded + rejected      |
 |       |          |       |        |        |           |          |        |
 |       |          |       |--write partitions (one file per instrument-day)>|
 |       |          |       |        |        |  exchange=NSE/year=/month=/day=|
 |       |          |       |        |        |           |          |        |
 |       |          |       |--register (counts, five quality scores)-------->|
 |       |          |       |   AVAILABLE, or QUARANTINED if validation errored|
 |       |          |       |        |        |           |          |        |
 |--GET /warehouse/query--->|        |        |           |          |        |
 |       |          quarantined? refuse       |           |          |        |
 |       |--------------------------------------------------------->|--glob->|
 |<-rows, read_path, partitions_read, truncated---------------------|         |
```

Three points.

**The trichotomy conserves, and the flags are a column.** A suspicious row
reaches its partition carrying what was suspicious about it, so a reader can
exclude it, weight it down or look at it. A loader that quietly removed bad ticks
would hand the research engine a clean-looking series and an unexplainable
backtest.

**Quarantine refuses to serve, it does not destroy.** The partitions are written
and the findings are stored in full. What is prevented is the one thing that
matters: serving a series with an unadjusted split in it and hoping the warning
is read.

**The partition layout is the query plan.** `exchange=`/`year=`/`month=`/`day=`
are columns to DuckDB, so a week's query reads a week's files — and
`partitions_read` on the response counts the files that actually contributed
rows, so a broken prune is visible rather than merely slow.


---

## 11. Dataset to performance report  **[implemented, real-time Phase 4]**

```
User    API    JobSvc  Worker  Warehouse  Features  Strategy  Engine  Metrics  Record
 |       |        |       |        |          |         |        |       |        |
 |-run-->|        |       |        |          |         |        |       |        |
 |       |--create job--->|        |          |         |        |       |        |
 |<-202 job_id----|       |        |          |         |        |       |        |
 |       |        |       |        |          |         |        |       |        |
 |       |        |       |--query bars------>|         |        |       |        |
 |       |        |       |  refuses a quarantined dataset       |       |        |
 |       |        |       |<-rows + validator flags--|          |       |        |
 |       |        |       |        |          |         |        |       |        |
 |       |        |       |--compute (bars 0..i only)->|        |       |        |
 |       |        |       |        |          |         |        |       |        |
 |       |        |       |     for each bar t:        |        |       |        |
 |       |        |       |       features at t ------>|        |       |        |
 |       |        |       |       <- target weight ----|        |       |        |
 |       |        |       |       fill against bar t+1 --------->|      |        |
 |       |        |       |       book updated, equity marked    |      |        |
 |       |        |       |        |          |         |        |       |        |
 |       |        |       |--equity curve------------------------------>|        |
 |       |        |       |<-metrics, with the conventions attached-----|        |
 |       |        |       |--attribute (identity must close)            |        |
 |       |        |       |        |          |         |        |       |        |
 |       |        |       |--record strategy, params, features, costs, commit--->|
 |       |        |       |  curve and fills to the object store               |
 |       |        |       |        |          |         |        |       |        |
 |--GET /research/experiments/{id}--------------------------------------------->|
 |<-the claim, and everything that supports it---------------------------------|
```

Three points.

**The loop's shape is the anti-look-ahead argument.** Features at bar `t` see
bars `0..t`; the fill is against bar `t+1`. A decision that used bar `t`'s close
cannot be filled at it, so the engine offers no option to.

**Costs enter as data, not as a default.** The schedule comes from the request
and is recorded verbatim. Without one the run is gross, and every figure it
produces says so.

**The record is the deliverable.** A backtest number without the dataset, the
parameters, the cost schedule and the code commit is an anecdote; the last arrow
is what makes it a claim somebody else could check.


---

## 12. History to target portfolio  **[implemented, real-time Phase 5]**

```
User    API   Optimiser  Warehouse  Covariance  Forecast  Solver  Risk
 |       |        |          |          |          |        |      |
 |-post->|        |          |          |          |        |      |
 |       |------->|          |          |          |        |      |
 |       |        |--bars per instrument-->|       |        |      |
 |       |        |<-aligned returns (inner join on timestamp)     |
 |       |        |   an instrument with no overlap is DROPPED and reported
 |       |        |          |          |          |        |      |
 |       |        |--estimate----------->|         |        |      |
 |       |        |<-covariance + shrinkage intensity if asked     |
 |       |        |          |          |          |        |      |
 |       |        |--what is the forecast?-------->|        |      |
 |       |        |   supplied / Black-Litterman / historical, or REFUSE
 |       |        |          |          |          |        |      |
 |       |        |--feasibility pre-check                  |      |
 |       |        |   contradictions named together, not one at a time
 |       |        |--solve (multi-start SLSQP, or LP for CVaR)---->|
 |       |        |<-weights, starts attempted and converged------|
 |       |        |          |          |          |        |      |
 |       |        |--risk of the portfolio that came back-------------->|
 |       |        |<-volatility, historical VaR/ES, effective assets----|
 |<-target portfolio + its risk + what shaped it--|         |      |
```

Three points.

**The forecast is a decision, not a step.** A return-seeking objective with
nothing supplied is refused rather than given sample means. Mean-variance
maximises the error in its expected returns, so substituting a poor forecast
quietly produces a confident portfolio built on the worst available input.

**Alignment drops rather than fills.** An instrument whose history does not
overlap the others is reported, not forward-filled: a filled return is an
invented observation, and a covariance estimated from invented observations
understates every correlation it touches.

**The risk reported is the risk of the portfolio that came back**, not of the one
that was asked for — including the risk contributions, which routinely disagree
with the weights about where the portfolio's real bet is.


---

## 13. Signal to P&L  **[implemented, real-time Phase 6]**

```
User    API    Gate    Broker   FillEngine   Book     Audit
 |       |       |        |          |         |        |
 |-order>|       |        |          |         |        |
 |       |--is this allowed?         |         |        |
 |       |<-decision + EVERY check, passed or not------->|
 |       |   a refusal is an ORDER ROW with a reason on it, not a 4xx
 |       |       |        |          |         |        |
 |       |--can this adapter do this?|         |        |
 |       |   an instruction it cannot carry out is refused, not translated
 |       |       |        |          |         |        |
 |       |--place-------->|          |         |        |
 |       |       |        |--quote + order---->|        |
 |       |       |        |<-fill / rest / refuse, with a reason
 |       |       |        |          |         |        |
 |       |--book the fill------------------->|          |
 |       |   average cost, the SAME code a backtest uses
 |       |       |        |          |         |        |
 |       |--every step---------------------------------->|
 |<-order + gate + fills, each flagged with what it rested on
```

Three points.

**The gate comes back whether or not it refused.** A user whose order passed
still wants to know which limits were checked and which were *not configured*,
and an interface that shows its checks only on failure teaches people that
silence means safety.

**A refusal is a `201`.** The order row exists with its reason on it. Answering a
refusal with an error and no record leaves the user unable to see what the gate
objected to.

**The fill's evidence travels with it forever.** `price_basis` says which
observed field the price came from, `quote_exchange_timestamp` says which quote
it was decided against, and `PAPER_FILL_COUNTERFACTUAL` says nobody was on the
other side. There is no query that turns a paper fill into something the market
did.

---

## 14. A live order, and the three gates  **[implemented, real-time Phase 7]**

```
User    API   Deployment  Account  Adapter   Vault   Broker
 |       |        |          |        |        |       |
 |-order>|        |          |        |        |       |
 |       |--pre-trade gate (as above; limits are MANDATORY on live)
 |       |        |          |        |        |       |
 |       |--live_trading_enabled?     |        |       |
 |       |   "may this installation trade real money" — default NO
 |       |        |          |        |        |       |
 |       |--live_armed_at?--->        |        |       |
 |       |   "is this book meant to be trading now" — a separate act
 |       |        |          |        |        |       |
 |       |--verified_against_documentation?--->|       |
 |       |   "has anybody checked this mapping" — default NO
 |       |        |          |        |        |       |
 |       |--token------------------------------>       |
 |       |   fetched per request and renewed by the vault; never an env var
 |       |        |          |        |--place-------->|
 |       |        |          |        |<-payload-------|
 |       |   status not in the map -> RAISE, never round to a neighbour
 |<-order, or a rejection recorded with which gate refused it
```

The three gates are independent on purpose. A configuration flag alone is a
single point of failure; an armed account on a disabled deployment is a
mistake caught rather than an order sent.

The third gate is the one that is easy to leave out. A wrong field name in an
adapter fails loudly — the broker returns an error and somebody fixes it. A
wrong *status* mapping does not: it tells the platform an order filled when it
did not, the book is then wrong, and nothing anywhere reports a problem. So the
adapter refuses to place a live order until a deployment that has read the
broker's contract says the mapping has been checked.
