# Sequence Diagrams

ASCII sequence diagrams for the five flows that define the system. Diagram 1 is
**implemented** in Phase 0; 2-5 are design commitments whose seams already exist
in the Phase 0 code (provider interface, `MarketState`, job system, envelope).

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

## 5. Unified order analysis (Phase 11)

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

Branches degrade independently. If surface calibration failed, the response is
`PARTIAL` with the surface block `null` and a warning; valuation, execution,
risk and margin still return.
