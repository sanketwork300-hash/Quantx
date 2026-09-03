# API Contract — `/api/v1`

Status: routes marked **[P0]** are implemented and covered by
`tests/integration/`. Routes marked _(planned)_ are contract commitments for
later phases and are listed so clients and the frontend can be designed against
a stable shape.

---

## 1. Conventions

- Base path `/api/v1`. Breaking changes require `/api/v2`.
- Auth: `Authorization: Bearer <jwt>` on everything except `/health`,
  `/auth/register`, `/auth/login`.
- All timestamps ISO-8601 with explicit `Z`.
- Monetary/price values are JSON **strings** carrying decimals (`"168.25"`) to
  avoid float round-tripping. Model outputs (IV, Greeks, scores) are JSON
  numbers.
- Errors use RFC-7807-style bodies:

```json
{"type": "https://qip.dev/errors/instrument-not-resolved",
 "title": "Instrument could not be resolved",
 "status": 422,
 "code": "INSTRUMENT_UNRESOLVED",
 "detail": "No instrument matches NSE:OPTION:NIFTY:2026-09-24:99000:C",
 "correlation_id": "01J..."}
```

- **Analytical** endpoints never use HTTP status to express quantitative
  failure. They return 200 with the envelope from §2.

### 1.1 Ownership

Every route that names a user-owned resource resolves it through an
ownership-scoped dependency. A resource owned by another user returns **404**,
not 403, so ids are not enumerable. A valid UUID confers nothing.

## 2. Analytical result envelope

```json
{
  "status": "OK",
  "results": {},
  "warnings": [
    {"code": "QUOTE_STALE", "severity": "WARNING",
     "message": "Underlying quote is 412s old at the requested as_of.",
     "context": {"instrument_id": "...", "age_seconds": 412}}
  ],
  "provenance": {
    "market_state_id": "...",
    "market_state_timestamp": "...",
    "market_data_sources": ["..."],
    "model_versions": {},
    "code_commit": "...",
    "computed_at": "..."
  }
}
```

`status`: `OK` | `PARTIAL` | `FAILED`. On `PARTIAL`, absent quantities are
present as `null` and a warning names each one.

## 3. Health and meta

| Method | Path | Status | Notes |
| --- | --- | --- | --- |
| GET | `/health` | **[P0]** | liveness; no auth, no DB |
| GET | `/health/ready` | **[P0]** | readiness: DB + cache + object store |
| GET | `/meta/version` | **[P0]** | app version, code commit, model registry digest |

## 4. Auth

| Method | Path | Status |
| --- | --- | --- |
| POST | `/auth/register` | **[P0]** |
| POST | `/auth/login` | **[P0]** |
| GET | `/auth/me` | **[P0]** |

```http
POST /api/v1/auth/login
{"email": "a@b.com", "password": "..."}

200
{"access_token": "...", "token_type": "bearer", "expires_in": 3600}
```

Rate limited per IP and per email. Failed logins are audit-logged; the response
does not distinguish "unknown user" from "wrong password".

## 5. Instruments **[P0]**

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/instruments` | filter by `asset_class`, `exchange`, `symbol`, `underlying_id`, `expiry`; paginated |
| GET | `/instruments/{id}` | canonical instrument |
| POST | `/instruments` | create/upsert by canonical key (idempotent) |
| POST | `/instruments/resolve` | resolution service, returns `RESOLVED`/`AMBIGUOUS`/`UNRESOLVED` |
| GET | `/instruments/{id}/aliases` | |
| POST | `/instruments/{id}/aliases` | |

```http
POST /api/v1/instruments/resolve
{"requests": [
  {"symbol": "NIFTY", "exchange": "NSE", "asset_class": "OPTION",
   "expiry": "2026-09-24", "strike": "24000", "option_type": "CALL"},
  {"symbol": "NIFTY26SEP24000CE", "source": "broker-x"}
]}

200
{"results": [
  {"status": "RESOLVED", "instrument_id": "...", "method": "STRUCTURED_MATCH", "confidence": 1.0},
  {"status": "UNRESOLVED", "reason": "NO_ALIAS_FOR_SOURCE", "request": {...}}
]}
```

## 6. Market data

| Method | Path | Status | Notes |
| --- | --- | --- | --- |
| GET | `/market/quotes/{instrument_id}` | **[P0]** | latest admitted quote + quality |
| GET | `/market/options/{underlying_id}` | **[P0]** | latest chain snapshot; `?expiry=`, `?include_excluded=true` |
| GET | `/market/chains/{snapshot_id}` | **[P0]** | a specific historical chain snapshot |
| GET | `/market/chains` | **[P0]** | list snapshots for an underlying |
| GET | `/market/bars/{instrument_id}` | _(planned P1)_ | |
| GET | `/market/orderbook/{instrument_id}` | _(planned P10)_ | |
| GET | `/market/state` | **[P2]** | timestamp-consistent snapshot; `?underlying_id=`, `?risk_free_rate=`, `?include_quotes=` |

Chain response (abridged):

```json
{
  "status": "OK",
  "results": {
    "snapshot_id": "...",
    "underlying": {"instrument_id": "...", "canonical_key": "NSE:INDEX:NIFTY"},
    "as_of_timestamp": "2026-09-24T09:20:00Z",
    "underlying_price": "24012.35",
    "counts": {"input": 480, "kept": 431, "excluded": 49},
    "quality_summary": {"overall_score": 0.81, "flag_counts": {"WIDE_SPREAD": 22}},
    "quotes": [
      {"instrument_id": "...", "strike": "24000", "option_type": "CALL",
       "expiry": "2026-10-29",
       "bid_price": "412.10", "ask_price": "415.60", "mid_price": "413.85",
       "relative_spread": 0.00846,
       "volume": "18400", "open_interest": "221500",
       "excluded": false, "exclusion_reason": null,
       "quality": {"overall_score": 0.93, "stale_score": 1.0, "spread_score": 0.88,
                   "liquidity_score": 0.97, "consistency_score": 1.0,
                   "completeness_score": 1.0,
                   "flags": []}}
    ]
  },
  "warnings": [],
  "provenance": {"...": "..."}
}
```

Note what is **absent**: no `implied_volatility`, no `reference_value`, no
`signal`. Phase 0 ships observation and quality only.

## 7. Uploads **[P0]**

| Method | Path | Notes |
| --- | --- | --- |
| POST | `/uploads` | multipart; `kind=OPTION_CHAIN\|POSITIONS\|TRADES`; stores to object store, returns `upload_id` |
| GET | `/uploads/{id}` | status + validation summary |
| POST | `/uploads/{id}/preview` | parse first N rows with a candidate column mapping; **no persistence** |
| POST | `/uploads/{id}/ingest` | run the ingestion pipeline; returns a `job_id` |

Preview-before-import is mandatory in the UI: users must see how their columns
were interpreted before any row is committed.

```http
POST /api/v1/uploads/{id}/ingest
{
  "kind": "OPTION_CHAIN",
  "underlying": {"symbol": "NIFTY", "exchange": "NSE", "asset_class": "INDEX"},
  "as_of_timestamp": "2026-09-24T09:20:00Z",
  "column_mapping": {"strike": "STRIKE_PRICE", "option_type": "CE_PE",
                     "expiry": "EXPIRY_DT", "bid_price": "BID",
                     "ask_price": "ASK", "volume": "VOL", "open_interest": "OI"},
  "risk_free_rate": 0.065,
  "dividend_yield": 0.0,
  "contract": {"multiplier": "75", "tick_size": "0.05", "lot_size": "75",
               "exercise_style": "EUROPEAN", "settlement_type": "CASH",
               "expiry_time_utc": "10:00:00"},
  "options": {"exclusion_severity_threshold": "ERROR",
              "create_missing_instruments": true}
}

202
{"job_id": "...", "status": "QUEUED"}
```

Three fields are optional on purpose, and their absence is recorded rather than
guessed:

| Field | If omitted |
| --- | --- |
| `contract.multiplier` | recorded as `1` with a `MULTIPLIER_ASSUMED` flag and a warning. Greeks and margin scale with it, so it is never inferred from a symbol. |
| `contract.expiry_time_utc` | the expiry *instant* stays unknown, so time to expiry is undefined and carry-dependent checks are skipped. |
| `risk_free_rate` / `dividend_yield` | only the assumption-free option bounds run (`C <= S`, `P <= K`, `price >= 0`). Sub-intrinsic pricing is **not** checked, because without a discount curve a deep in-the-money European put legitimately trades below `K - S`. |

## 8. Jobs **[P0]**

| Method | Path |
| --- | --- |
| GET | `/jobs` |
| GET | `/jobs/{job_id}` |
| GET | `/jobs/{job_id}/result` |
| POST | `/jobs/{job_id}/cancel` |

```json
{"job_id": "...", "job_type": "INGEST_OPTION_CHAIN", "status": "RUNNING",
 "progress": 0.42, "created_at": "...", "started_at": "...",
 "completed_at": null, "error": null}
```

`status`: `QUEUED | RUNNING | COMPLETED | FAILED | CANCELLED`.

## 9. Derivatives

| Method | Path | Status |
| --- | --- | --- |
| POST | `/derivatives/iv` | **[P1]** invert one option price |
| POST | `/derivatives/greeks` | **[P1]** price and Greeks, with units named |
| POST | `/derivatives/forward` | **[P1]** every applicable estimator |
| POST | `/derivatives/chains/{snapshot_id}/analyze` | **[P1]** job |
| GET | `/derivatives/chains/{snapshot_id}/smile` | **[P1]** latest analysis as a smile |
| GET | `/derivatives/analyses` | **[P1]** |
| GET | `/derivatives/analyses/{analysis_id}` | **[P1]** |
| POST | `/derivatives/analyses/{analysis_id}/calibrate` | **[P2]** job |
| GET | `/derivatives/surfaces` | **[P2]** |
| GET | `/derivatives/surfaces/latest?underlying_id=` | **[P2]** |
| GET | `/derivatives/surfaces/{surface_row_id}` | **[P2]** |
| POST | `/derivatives/surfaces/{surface_row_id}/reference` | **[P2]** reference IV and price |
| GET | `/derivatives/arbitrage/{analysis_id}` | **[P2]** both scopes |
| POST | `/derivatives/surfaces/{surface_row_id}/anomalies` | **[P3]** job |
| GET | `/derivatives/anomalies/{underlying_id}` | **[P3]** latest scan |
| GET | `/derivatives/scans/{scan_id}` | **[P3]** |
| GET | `/derivatives/history/{underlying_id}` | **[P3]** percentiles by tenor |
| POST | `/derivatives/analyses/{analysis_id}/global-surface` | **[P9]** job: SSVI, local vol, densities, Heston |
| GET | `/derivatives/global-surfaces` | **[P9]** |
| GET | `/derivatives/global-surfaces/latest?underlying_id=` | **[P9]** |
| GET | `/derivatives/global-surfaces/{id}` | **[P9]** |
| GET | `/derivatives/global-surfaces/{id}/local-volatility` | **[P9]** the Dupire grid, holes and all |
| GET | `/derivatives/global-surfaces/{id}/densities` | **[P9]** one per fitted expiry |
| GET | `/derivatives/underlyings/{underlying_id}/heston` | **[P9]** latest calibration |
| POST | `/derivatives/consensus` | **[P9]** job |
| GET | `/derivatives/consensus` | **[P9]** previous runs |
| GET | `/derivatives/consensus/{id}` | **[P9]** |

### 9.1 Invert one price

```http
POST /api/v1/derivatives/iv
{"price": 8.916, "spot": 100, "strike": 100, "time_to_expiry": 0.25,
 "is_call": true, "rate": 0.05}

200
{"status": "OK",
 "results": {
   "implied_volatility": 0.2,
   "converged": true, "iterations": 2, "solver": "safeguarded-newton",
   "lower_bound": 1e-8, "upper_bound": 5.0,
   "vega": 19.62, "uncertainty": 7.1e-16, "well_conditioned": true,
   "error": null, "price_used": 8.916, "residual": 0.0,
   "parameterization": "BLACK_SCHOLES_MERTON"},
 "warnings": [], "provenance": {"...": "..."}}
```

`uncertainty` is roughly one price ulp divided by vega: the volatility moved by
one representable change in the price. **Do not display more precision than it
allows.** A deep in-the-money quote can reproduce its price exactly and still be
uncertain in the fifth decimal, and `well_conditioned` is `false` there.

A price with no invertible volatility returns `status: FAILED` inside a 200, a
`null` volatility, and a named `error` — `PRICE_BELOW_INTRINSIC`,
`PRICE_ABOVE_BOUND`, `NO_TIME_VALUE`, `OPTION_EXPIRED`, `NON_POSITIVE_PRICE`,
`NO_CONVERGENCE` or `INVALID_INPUT`.

### 9.2 Greeks

Returns display-scaled values (`vega_per_vol_point`, `theta_per_day`,
`rho_per_bp`), the raw partials alongside them, and a `units` block naming each
convention. Either `spot` or `forward` must be supplied.

### 9.3 Analyse a chain

```http
POST /api/v1/derivatives/chains/{snapshot_id}/analyze
{"risk_free_rate": 0.065, "dividend_yield": 0.0,
 "settlement_time_utc": "10:00:00", "day_count": "ACT/365F",
 "include_excluded_quotes": false}

202 {"job_id": "...", "status": "QUEUED"}
```

Two parameters change the answer and are therefore recorded in provenance rather
than defaulted quietly:

| Field | If omitted |
| --- | --- |
| `settlement_time_utc` | time to expiry is **undefined**, nothing is solved, and the result is `PARTIAL` with `DERIVATIVES_SETTLEMENT_TIME_UNKNOWN`. Defaulting to midnight would misprice every same-day expiry. |
| `risk_free_rate` | discounting assumes zero. Put-call parity recovers the true discount factor from the quotes anyway, so a wrong rate degrades only the spot-carry estimate and appears as forward disagreement. |

Quotes the quality engine excluded stay out by default: a quote set aside for a
reason should not silently enter a calibration.

The smile response returns, per expiry, the selected forward **and every
estimate that was attempted**, the solve-failure counts by reason, ATM level,
skew and curvature, and per-quote points carrying `market_iv`, the bid/ask IV
envelope, the solver report, the conditioning, and — when a quote is not used
for the smile — the reason.

### 9.4 Calibrate a surface

```http
POST /api/v1/derivatives/analyses/{analysis_id}/calibrate
{"seed": 20260924, "use_weights": true}

202 {"job_id": "...", "status": "QUEUED"}
```

Deterministic: the same analysis and seed refit to the same parameters, so a
stored surface can be reproduced rather than merely re-run. The result carries
the surface, both arbitrage reports, and warnings including
`SURFACE_NARROW_STRIKE_RANGE`, `SURFACE_SLICE_DEGRADED` and
`SURFACE_CALENDAR_NOT_PREVENTED`.

### 9.5 Reference values

```http
POST /api/v1/derivatives/surfaces/{surface_row_id}/reference
{"requests": [{"strike": "24000", "expiry": "2026-10-29", "option_type": "CALL"}]}

200
{"status": "OK",
 "results": {"surface_id": "surface:...",
   "points": [{
     "strike": "24000", "expiry": "2026-10-29", "option_type": "CALL",
     "method": "EXACT_SLICE",
     "reference_iv": 0.12329, "total_variance": 0.001459,
     "reference_price": 444.001011,
     "log_moneyness": -0.006238, "forward": 24150.18,
     "calibration_rmse_vol_points": 0.0021,
     "flags": [], "error": null}]},
 "warnings": [], "provenance": {"...": "..."}}
```

`method` is `EXACT_SLICE`, `INTERPOLATED_MATURITY`, `EXTRAPOLATED_MATURITY` or
`UNAVAILABLE`. `flags` may contain `EXTRAPOLATED_STRIKE`,
`EXTRAPOLATED_MATURITY`, `SLICE_DEGRADED` or `LOW_CONFIDENCE_FORWARD`.

These are **reference** values. There is no `fair_value` field, they never
overwrite an observed `market_iv`, and a test asserts the response text contains
no such word.

### 9.6 Scan for deviations

```http
POST /api/v1/derivatives/surfaces/{surface_row_id}/anomalies
{"min_z_score": 2.0, "require_outside_envelope": true,
 "min_confidence": 0.3, "min_liquidity": 0.05}

202 {"job_id": "...", "status": "QUEUED"}
```

```http
GET /api/v1/derivatives/anomalies/{underlying_id}?flagged_only=true

200
{"status": "OK",
 "results": {
   "scan_id": "...", "counts": {"examined": 60, "scored": 60, "flagged": 1},
   "policy": {"min_z_score": 2.0, "explained_scale": "sqrt(half bid/ask IV envelope^2 + ...)"},
   "anomalies": [{
     "expiry": "2026-10-29", "strike": "24000", "option_type": "CALL",
     "market_iv": 0.13527, "reference_iv": 0.12328,
     "iv_difference_vol_points": 1.199,
     "market_iv_bid": 0.13476, "market_iv_ask": 0.13577,
     "envelope_position": "BELOW_BID", "excess_over_envelope": 0.01148,
     "explained_scale": 0.00072, "z_score": 16.64,
     "historical_z_score": null, "historical_observations": 0,
     "liquidity_score": 1.0, "data_quality_score": 1.0, "confidence": 0.941,
     "flagged": true,
     "explanation": [
       {"factor": "bid/ask envelope", "effect": "SUPPORTS",
        "detail": "The reference lies below bid, so the market's own quoted
                   width does not account for the difference."}]}]},
 "warnings": [], "provenance": {"...": "..."}}
```

**There is no direction, rating, target or recommendation field**, and the words
buy, sell, cheap, expensive, underpriced and arbitrage appear nowhere in the
response. A test asserts this over the entire serialised body.

`flagged_only=false` returns every scored quote, not only the flagged ones —
the rest is the evidence the threshold was doing something.

### 9.7 Surface history

```http
GET /api/v1/derivatives/history/{underlying_id}?tenor_days=30
```

Percentiles and z-scores for the at-the-money level, skew, curvature and total
variance at standard tenors, against the underlying's own past. Every answer
carries `observations` and an `is_reliable` flag; below
`minimum_reliable_observations` a percentile is reported but should not be read
as a distribution, and a `HISTORY_INSUFFICIENT_OBSERVATIONS` warning says so.

### 9.8 Arbitrage

```http
GET /api/v1/derivatives/arbitrage/{analysis_id}?min_severity=WARNING
```

Returns `raw_market` and `fitted_surface` as separate blocks — never merged, so
a smooth fit cannot hide a broken market and a broken fit is not reported as a
market anomaly. Each violation carries its `magnitude` and the `tolerance` it
was judged against, in that condition's own units.

### 9.9 Global surface (SSVI) **[P9]**

```http
POST /api/v1/derivatives/analyses/{analysis_id}/global-surface
{"seed": 20260924, "use_weights": true, "enforce_butterfly_bounds": true,
 "calibrate_heston": true, "require_feller": false,
 "build_local_volatility": true, "build_densities": true}

202 {"job_id": "...", "status": "QUEUED"}
```

One job produces the SSVI fit, the Dupire local-volatility grid, the implied
density per expiry and a constrained Heston fit. They read the same analysis;
running them separately would let four artefacts disagree about which quotes
they were built from.

```http
GET /api/v1/derivatives/global-surfaces                     -> summaries
GET /api/v1/derivatives/global-surfaces/latest?underlying_id=...
GET /api/v1/derivatives/global-surfaces/{id}                -> envelope
GET /api/v1/derivatives/global-surfaces/{id}/local-volatility
GET /api/v1/derivatives/global-surfaces/{id}/densities
GET /api/v1/derivatives/underlyings/{underlying_id}/heston
```

A stored surface is rebuilt from three parameters and one `theta` per expiry,
with no re-fitting on read.

`calendar_arbitrage_free` on the summary is a **structural** property of an
admissible SSVI fit, not a diagnostic that happened to pass. Both butterfly
conditions are reported: `butterfly_bounds_satisfied` is the closed-form
sufficient condition of Gatheral-Jacquier Theorem 4.2, and `min_durrleman_g` is
the actual one. A surface can fail the first and satisfy the second, and the
warning that says so states which is which.

The local-volatility response carries `values` as a matrix with `null` wherever
Dupire's denominator vanished, alongside `flag_counts` naming why, and
`total_points = valid_points + flagged_points` holds by database constraint. A
density's `percentiles` are `null` unless it is admissible.

### 9.10 Model consensus **[P9]**

```http
POST /api/v1/derivatives/consensus
{"instrument_id": "...", "risk_free_rate": 0.065, "dividend_yield": 0.0,
 "paths": 100000, "seed": 20260924, "grid_nodes": 401, "grid_steps": 200}

202 {"job_id": "...", "status": "QUEUED"}
```

`models` may name a subset; an unknown name is a 422 that lists what is
available, and a non-option instrument is a 422 before a job exists. The
contract must have a calibrated global surface for its underlying — the models
read their volatility from it rather than from an assumption.

```http
GET /api/v1/derivatives/consensus            -> previous runs
GET /api/v1/derivatives/consensus/{id}
```

The result carries `reference_value` (the median), `reference_range`,
`model_dispersion`, `market_deviation`, the per-model `values` and a
`confidence` whose `contributions` each carry the `basis` that produced them.

**There is no `best_model`, `fair_value`, `true_price` or `recommendation`
field, and no field that could hold one.** A test scans every key of the
serialised payload. Each entry in `values` carries either a `value` or an
`unavailable_reason` and never both or neither, which the database enforces.
The `interpretation` field states in the response itself that the median is not
a price the contract is worth and that the spread is a statement about model
risk rather than about the market.

## 10. Portfolio **[P4]**

Every route resolves the portfolio through an ownership-scoped read. A
`portfolio_id` is never trusted because it is a well-formed UUID; a foreign id
returns 404, which is also the correct answer to "does this exist?".

```
POST   /api/v1/portfolios
GET    /api/v1/portfolios
GET    /api/v1/portfolios/{portfolio_id}
PATCH  /api/v1/portfolios/{portfolio_id}
DELETE /api/v1/portfolios/{portfolio_id}
```

### 10.1 Positions

```
GET    /api/v1/portfolios/{portfolio_id}/positions
POST   /api/v1/portfolios/{portfolio_id}/positions
PATCH  /api/v1/portfolios/{portfolio_id}/positions/{position_id}
DELETE /api/v1/portfolios/{portfolio_id}/positions/{position_id}
```

Quantity is signed and negative is short. `side` is optional and is *checked
against* the sign rather than used to infer it: a body saying `SHORT` with a
positive quantity is `422 INVALID_POSITION`, because a silently reconciled sign
is a portfolio whose every risk number is wrong with no error anywhere. A zero
quantity is likewise refused.

### 10.2 Import

```
POST /api/v1/portfolios/{portfolio_id}/import/preview
POST /api/v1/portfolios/{portfolio_id}/import        -> 202 job
```

The file is uploaded through `POST /uploads` with `kind=POSITIONS`; an upload of
another kind is refused with `422 WRONG_UPLOAD_KIND`, and a positions file sent
to `POST /uploads/{id}/ingest` is refused with `400 WRONG_INGESTION_ROUTE`.

The preview writes nothing and returns three buckets:

```json
{
  "upload_id": "…", "headers": ["SYMBOL", "NETQTY", "…"],
  "inferred_mapping": {"symbol": "SYMBOL", "quantity": "NETQTY"},
  "applied_mapping":  {"symbol": "SYMBOL", "quantity": "NETQTY"},
  "rows_in": 10, "committable": true,
  "resolved":  [{"row_number": 1, "canonical_key": "…", "expiry": "2026-10-29",
                 "strike": "23000", "option_type": "CALL", "quantity": "2",
                 "side": "LONG", "resolution_method": "STRUCTURED_MATCH",
                 "creates_instrument": false, "multiplier_is_assumed": false}],
  "ambiguous": [{"row_number": 4, "reason": "MULTIPLE_CANDIDATES",
                 "candidates": [{"canonical_key": "…"}, {"canonical_key": "…"}]}],
  "invalid":   [{"row_number": 8, "reason": "SIDE_DISAGREES_WITH_QUANTITY",
                 "message": "…"}]
}
```

`rows_in == len(resolved) + len(ambiguous) + len(invalid)` always: nothing is
dropped without a reason, and every invalid row names its source row number.

**No ambiguous row is ever auto-resolved.** `committable` is false while any row
is ambiguous, and the commit job fails with `ImportRefused` rather than picking
the most likely contract. Picking one is how a portfolio silently acquires the
wrong expiry and every downstream number becomes wrong with no error appearing.

The commit is a job. `defaults` supplies what the file does not carry; a
multiplier is recorded as an assumption on any contract the import creates,
whether it came from the request or from the platform, because a multiplier
rescales every value and Greek for that contract.

### 10.3 Valuation

```
POST /api/v1/portfolios/{portfolio_id}/valuation             -> 202 job
GET  /api/v1/portfolios/{portfolio_id}/valuation             -> the latest
GET  /api/v1/portfolios/{portfolio_id}/valuation/{valuation_id}  -> envelope
GET  /api/v1/portfolios/{portfolio_id}/greeks?dimension=EXPIRY
```

```jsonc
{
  "risk_free_rate": 0.065,
  "dividend_yield": 0.0,
  "settlement_time_utc": "10:00:00",   // omit and option Greeks are omitted too
  "as_of": null                        // omit for the latest snapshot
}
```

The whole portfolio is valued against one `MarketState`, and the result and its
provenance carry the same `market_state_id`. Each position reports
`market_price` and `model_price` as separate fields — neither is ever written
from the other — plus `price_used` and a `valuation_method` of `MARKET_MID`,
`MARKET_LAST`, `STALE_MARKET`, `MODEL_REFERENCE` or `UNAVAILABLE`, and a
`greek_source` of `MARKET_IV`, `REFERENCE_IV`, `NOT_APPLICABLE` or
`UNAVAILABLE`.

An unvalued position contributes nothing to the totals and is listed with its
reason rather than counted as zero; the envelope is then `PARTIAL`. A portfolio
with no market data at all returns `FAILED` with `PORTFOLIO_NO_MARKET_DATA`,
never a total of zero.

`GET .../greeks` reads the stored valuation rather than recomputing, so the
Greeks and the values on the two responses came from one snapshot. Aggregates
cover `UNDERLYING`, `EXPIRY`, `ASSET_CLASS`, `STRATEGY_TAG` and `CURRENCY`;
every dimension sums the same per-position numbers and therefore totals to the
portfolio total.

No portfolio response contains a recommendation, a fair value, or a
buy/sell signal, and a test asserts it over the whole serialised response.

## 11. Risk **[P5, P6]**

### 11.1 Scenarios

```
GET    /api/v1/scenarios?include_templates=true
POST   /api/v1/scenarios
POST   /api/v1/scenarios/derive
GET    /api/v1/scenarios/{scenario_id_or_name}
DELETE /api/v1/scenarios/{scenario_id}
```

Every scenario carries a `source`, and it is the field that matters:

| Source | Meaning |
| --- | --- |
| `HYPOTHETICAL` | A shipped template. Round numbers for illustration, saying so in its own description. |
| `USER_DEFINED` | Shocks the caller entered. |
| `DERIVED_FROM_HISTORY` | Computed by `/scenarios/derive` from a series the platform holds. |

`POST /scenarios` always records `USER_DEFINED`, whatever the body says: there
is no way to *declare* a scenario historical. That label is earned only by
`/scenarios/derive`, and the model and a database CHECK both refuse a historical
claim with no derivation attached.

```jsonc
// POST /scenarios
{
  "name": "Gap down",
  "shocks": [
    {"kind": "UNDERLYING_PRICE", "shock_type": "PERCENTAGE",   "value": -0.08},
    {"kind": "VOLATILITY",       "shock_type": "VOL_POINTS",   "value":  0.06},
    {"kind": "RISK_FREE_RATE",   "shock_type": "BASIS_POINTS", "value": 25.0,
     "target": null}   // null = every factor of this kind
  ]
}
```

A shock type that makes no sense for its factor is `422 INVALID_SCENARIO` — a
percentage move in a rate is ambiguous, so it is refused rather than guessed at.
Shocks of the same kind compose additively; a market-wide -5% plus a name-level
-3% is -8% on that name and -5% elsewhere.

```jsonc
// POST /scenarios/derive
{"name": "Worst recorded day", "underlying_id": "…", "window_days": 1,
 "percentile": null}   // null = the worst move; a quantile otherwise
```

Returns a scenario whose `derivation` names the series, its observation count,
its date range, and the date of the move being reproduced. Fewer than two
recorded observations is `422 INSUFFICIENT_HISTORY` with the true count — the
platform will not derive a move from a series that has none in it.

### 11.2 Value at Risk

```
POST /api/v1/portfolios/{portfolio_id}/var    -> 202 job
GET  /api/v1/portfolios/{portfolio_id}/var    -> past runs
```

```jsonc
{
  "method": "HISTORICAL",          // or MONTE_CARLO, PARAMETRIC
  "risk_free_rate": 0.065,
  "settlement_time_utc": "10:00:00",
  "horizon_days": 1,
  "confidences": [0.95, 0.99],
  "paths": 10000,                  // MONTE_CARLO only
  "seed": 20260924,                // MONTE_CARLO only; recorded, and required
  "lookback": null,
  "include_volatility_factor": true
}
```

The result carries, for each confidence level, `value_at_risk` (a threshold
loss), `expected_shortfall` (the average loss given the threshold is exceeded),
the observation counts behind both, and an `interpretation` block stating that
distinction in words. Alongside them:

- `assumptions.repricing` — `"full"` for historical and Monte Carlo, and a
  string beginning `"none"` for parametric.
- `factor_panel` — the sources, date range, alignment policy, and the
  missing-data policy in words. Nothing is forward-filled.
- `estimate_intervals` — a 90% bootstrap interval for the estimate itself.
- `worst_scenario_dates` — for the historical method, the recorded dates that
  hurt most.

Too little history is `FAILED` with `RISK_INSUFFICIENT_HISTORY` and the
observation count, never a number computed from four observations. A parametric
run on a book containing options always carries
`RISK_PARAMETRIC_ON_NONLINEAR_BOOK`.

### 11.3 Stress

```
POST /api/v1/portfolios/{portfolio_id}/stress   -> 202 job
GET  /api/v1/portfolios/{portfolio_id}/stress   -> past runs
GET  /api/v1/portfolios/{portfolio_id}/risk-snapshot
```

```jsonc
{"scenario": "Underlying -10%", "time_decay_days": 0.0,
 "risk_free_rate": 0.065, "settlement_time_utc": "10:00:00"}
```

`scenario` accepts a template name, a stored scenario's name, or either one's
id. The result's `pnl` is the **full repricing**. Beside it,
`greek_approximation` carries the second-order estimate of the same move, its
difference from the full repricing, the formula used and a caveat saying it is
not the answer. On the reference book the two differ by more than 5% on a 10%
move and by under 1% on a 0.1% move.

`contributions` decomposes the loss by underlying, expiry, asset class and
strategy tag. Each breakdown reports a `residual` (zero while portfolio value is
a sum over independently repriced positions — it is the check on that, not a
plug) and an `ungrouped_positions` count for positions that carry no key for
that dimension.

`shocks` reports the moves as actually resolved, per underlying, in the pricer's
own units. `floored_volatilities` counts positions whose shocked volatility hit
the `1e-4` floor.

Every risk run stores a `risk_snapshot` pointing at the `portfolio_valuation` it
measured, so the chain from a VaR number back to the quotes behind it is a
sequence of foreign keys.

### 11.4 Margin **[P6]**

```
GET  /api/v1/margin/models
POST /api/v1/portfolios/{portfolio_id}/margin              -> 202 job
GET  /api/v1/portfolios/{portfolio_id}/margin              -> past runs
GET  /api/v1/portfolios/{portfolio_id}/margin/{margin_id}  -> envelope
```

**No endpoint here reports a broker's margin.** `GET /margin/models` lists what
is implemented, and every entry carries `is_broker_equivalent: false` — a field
that would only ever be true for a model implementing a *published*
methodology. An unknown model name is `422 UNKNOWN_MARGIN_MODEL` with the
available ones listed.

```jsonc
{
  "margin_model": "SimpleRiskMarginModel",
  "risk_free_rate": 0.065,
  "settlement_time_utc": "10:00:00",
  "grid": {                              // the grid IS the model; it is declared
    "spot_returns": [-0.2, -0.1, 0.0, 0.1, 0.2],
    "vol_points": [-0.05, 0.0, 0.05]
  },
  "short_option_minimum_rate": 0.0,      // zero on purpose; see below
  "concentration_add_on_rate": 0.0,
  "concentration_threshold": 0.5,
  "eligible_capital": null,              // null = unknown, not zero
  "ladder": null,                        // null = the default, both directions
  "vol_co_shock": 0.0
}
```

A `spot_returns` grid without `0.0` in it is refused: without an unshocked point
the grid cannot show that the book is flat where the market actually is.

The two rates default to **zero**, and that is a refusal rather than an
omission. A short option far out of the money shows almost no loss on a scan
grid while carrying unbounded tail risk, and a real margin system floors it for
that reason — but the rate at which it does is a venue's rule. Every response
with a zero rate carries `MARGIN_NO_SHORT_OPTION_MINIMUM` and says in words
what the zero leaves out.

The result carries:

- `estimated_margin` — the only number, and its name is the claim. There is no
  `required_margin` and no field naming a venue.
- `margin.components[]` — `scan_loss`, `short_option_minimum`,
  `concentration_add_on`, each with the `basis` it was computed on.
- `margin.assumptions[]` and `assumptions[]` — the model's and the ladder's, in
  plain language.
- `margin.confidence` — coverage, grid containment and mark consistency,
  combined as a geometric mean.
- `margin.worst_case` — the grid point that produced the estimate, and whether
  it sat on the grid's own boundary (`MARGIN_WORST_LOSS_AT_GRID_EDGE`).
- `buffer`, `utilisation` — `null` when `eligible_capital` was not supplied.
  They are never defaulted to portfolio value, and a database CHECK enforces it.
- `ladder[]` — every rung, with the portfolio fully repriced *and* the margin
  model rerun on that moved market.
- `shortfall_region.downside` / `.upside` — each an
  `{approximate_entry, bracketed_by, buffer_before, buffer_after}` **region**,
  never a price. The interpolated entry always lies between the two rungs that
  bracket it, and those rungs are reported so the coarseness is visible.
- `summary` — the sentence rendered verbatim by the UI, stored on the row so
  that what a user was told stays recoverable.

Every run also carries `MARGIN_IS_A_MODEL_ESTIMATE` as a warning, whose message
states that this is not the user's broker's or exchange's requirement.

## 12. Execution **[P7, P8]**

### 12.1 Trade-log import

```
POST /api/v1/execution/trades/preview
POST /api/v1/execution/trades/import   -> 202 job
```

The file is uploaded through `POST /uploads` with `kind=TRADES`; an upload of
another kind is `422 WRONG_UPLOAD_KIND`, and a trade log sent to
`POST /uploads/{id}/ingest` is `400 WRONG_INGESTION_ROUTE`.

Required fields: `timestamp, symbol, side, quantity, price`. Valuable and
optional: `order_id, parent_order, order_type, limit_price, submit_timestamp,
decision_timestamp, order_quantity, broker, fees`.

The preview returns the same three buckets as the portfolio import, and
`committable` is false while any row is ambiguous. **No ambiguous row is ever
auto-resolved**: a fill on the wrong contract lands in the wrong parent order and
drags a benchmark window with it.

Rejections carry their source row number and a reason, including
`SUBMIT_AFTER_FILL` — a fill timestamped before its own submission is refused
rather than reconciled, because one of the two stamps is wrong and guessing which
would corrupt every benchmark for that order.

`defaults.parent_gap_seconds` governs only fills the file did not assign a parent
to. It is recorded on every report, because a different gap gives different
parents, windows and benchmarks.

### 12.2 Analysis

```
POST /api/v1/execution/analyze          -> 202 job
GET  /api/v1/execution/executions
GET  /api/v1/execution/reports
GET  /api/v1/execution/reports/{report_id}
```

```jsonc
{
  "start": null, "end": null,             // whole history by default
  "instrument_id": null,
  "parent_order_key": null,
  "primary_benchmark": "ARRIVAL",
  "parent_gap_seconds": 300.0,
  "staleness_tolerance_seconds": 300.0,   // older than this is not "prevailing"
  "window_padding_seconds": 3600.0        // arrival looks back, close looks forward
}
```

Each parent order returns six benchmarks — `ARRIVAL`, `DECISION`,
`PREVAILING_MID`, `INTERVAL_TWAP`, `INTERVAL_VWAP`, `CLOSE` — and every one
carries its `window`, `source`, `method` and `observations`. A benchmark the
available data cannot support carries `available: false` and an
`unavailable_reason`, and produces **no shortfall** rather than a zero.

Shortfalls report `currency_amount`, `basis_points` and `percent`, with a
`convention` field stating that positive is always a cost.

`decomposition` splits the primary shortfall into `spread` (`MODELLED`), `fees`
(`MEASURED`), `impact` (`NOT_MODELLED`, Phase 8) and `timing_residual`
(`RESIDUAL`), with a `caveat` saying the split is not a measurement.

`market_window.coverage` reports the observation count, the span ratio, the
largest gap, whether the window is bracketed at each end, and the policy in
words. `grouping_is_inferred` is on the parent order and on the stored summary
row, because it changes what every number below it means.

A run with no stored fills in range returns `FAILED` with
`TCA_NO_EXECUTIONS_IN_RANGE`, never an empty analysis.

### 12.3 Simulation **[P8]**

```
GET  /api/v1/execution/strategies
GET  /api/v1/execution/impact-models
POST /api/v1/execution/simulate                     -> 202 job
GET  /api/v1/execution/simulations?comparison_id=…
GET  /api/v1/execution/simulations/{simulation_id}
```

`GET /execution/strategies` lists what each strategy needs from the caller;
`GET /execution/impact-models` reports `ships_calibrated_coefficients: false`
for every entry, because none is calibrated and none ever will be by default.

```jsonc
{
  "instrument_id": "…", "side": "BUY", "quantity": "7500",
  "start": "2026-09-24T09:20:00Z", "end": "2026-09-24T15:20:00Z",
  "intervals": 6,
  "strategies": ["TWAP", "VWAP"],
  "impact_model": "SquareRootImpactModel",
  "permanent_coefficient": 1.0,      // the identity, not a calibration
  "temporary_coefficient": 1.0,
  "volatility": 0.18,
  "average_daily_volume": 500000.0,
  "lot_size": "75",
  "expected_volumes": [30000, 12000, 8000, 7000, 9000, 25000],
  "spreads": null, "volatilities": null,
  "participation_rate": 0.10,
  "latency_seconds": 0.0,
  "max_price_age_seconds": null      // null = the window's own tolerance
}
```

A per-interval input must cover **every** interval or the request is `422`: a
partial profile would leave the rest of the schedule silently assuming
something. Unknown strategies and impact models are `422` with the available
ones listed, and an unknown instrument is `404` before a job is created.

**Every number in the result is a counterfactual estimate.** The result's
`counterfactual` flag is `true`, its `caveat` says the schedules were never
executed and that running one would itself have moved the path, and
`COUNTERFACTUAL_ESTIMATE` is the envelope's first warning. A database CHECK
makes an unlabelled row unstorable.

Per strategy the result carries the schedule (with every slice), the simulated
fills (observed price, price after accumulated permanent impact, fill price,
spread and impact per unit), `completion_rate`, `average_price`,
`modelled_impact_cost`, `modelled_spread_cost`, the Phase 7 benchmarks computed
on the simulated fills, and any `unfilled` slices with their reasons.

`unavailable` lists the strategies that could not run and why — a VWAP on a flat
profile refuses because it would be a TWAP, and reporting it under the VWAP name
would make the comparison meaningless.

`comparison_caveat` states that the result is **not a ranking**, that no strategy
is recommended, and that the differences between them are smaller than the
uncertainty in an uncalibrated impact coefficient. There is no `best_strategy`
field, and a test asserts no such key exists.

## 13. Microstructure **[P10]**

Every analytical route here consults the dataset's stored availability report
before it computes anything. A capability the data cannot support is a **422**
carrying `capability`, `reason` and the `evidence` the verdict was taken on —
never a number with a caveat. There is no `force` parameter, and a test scans
the published schema for one.

### What a dataset can be asked for

```http
GET /api/v1/microstructure/capabilities
```

Reference, and answerable before anything is uploaded: the six capabilities,
what each unlocks and what each needs. A snapshot-only export cannot support a
queue model however it is post-processed, and finding that out after the upload
is a worse experience than reading it here.

### Import

Depth snapshots and event tapes are uploaded separately —
`kind=BOOK_SNAPSHOTS` and `kind=BOOK_EVENTS` — and imported together as one
dataset, because the two halves are assessed together for what they can
support. Wide CSV and canonical parquet are both accepted; parquet is the one
binary format the upload endpoint admits, and it is checked at both ends for
the magic number rather than only at the head.

```http
POST /api/v1/microstructure/datasets/preview
{"instrument_id": "...", "snapshot_upload_id": "...", "event_upload_id": "..."}
```

Writes nothing. Returns `detected_snapshot_columns` — the `BID_PX_1`-style
level columns the importer recognised, level by level, plus everything it could
not place — the inferred event mapping, per-bucket counts with a sample of the
rejections, and the full availability report the dataset *would* get.

The preview is mandatory for a reason sharper than usual: a book whose price and
size columns were read the wrong way round parses cleanly and produces analytics
that are wrong in every number and look entirely ordinary. The commit sends the
detected mapping back, so what is imported is what was reviewed.

```http
POST /api/v1/microstructure/datasets
{"instrument_id": "...", "name": "NIFTY ATM call, 30 minutes",
 "snapshot_upload_id": "...", "event_upload_id": "...",
 "snapshot_columns": { ... as returned by the preview ... }}
202 {"job_id": "...", "status": "QUEUED"}
```

### Reading a dataset

```http
GET /api/v1/microstructure/datasets
GET /api/v1/microstructure/datasets/{id}
GET /api/v1/microstructure/datasets/{id}/capabilities
GET /api/v1/microstructure/datasets/{id}/rejections
```

The detail route is `PARTIAL` whenever any capability was refused: an import
that succeeded but cannot answer half of what the phase offers is not a
complete answer to "what can I do with this?". `rows` conserves —
`input == kept + rejected` on both halves — and `rejection_counts` accounts for
every rejected row by reason. `/rejections` returns the **complete** list, each
with its 1-based source row number and message, read from the object store
because it is unbounded and a column would have to truncate it.

`/capabilities` returns one verdict per capability with `available`, `reason`,
a sentence saying what was missing, and the `evidence` it was decided on, so a
refusal can be argued with rather than only obeyed.

### Book analytics

```http
POST /api/v1/microstructure/datasets/{id}/analyze
{"levels": 5, "weighted_decay": 0.5, "trade_sizes": [500]}
202 {"job_id": "...", "status": "QUEUED"}

GET /api/v1/microstructure/reports/{report_id}
```

Every snapshot is measured and the session is summarised by percentiles rather
than a standard deviation — a session of books is not normal and a handful of
auction instants own the variance. Each measure carries its `observations` and
its `missing` count broken down by the reason the snapshots that had no such
measurement did not, so `observations + missing` always equals
`snapshots_analysed` and an average is never quietly over a subset nobody chose.

`trade_costs` reports what walking the displayed book for a stated size would
have cost. A size the book cannot absorb is counted under
`snapshots_that_could_not` with a null slippage — the price beyond the last
level is not in the book. The `note` on every entry states that this is one
instant with nothing moving, and not an impact model.

`series` is a subsample of the per-snapshot series spaced evenly across the
window, so the chart is not a picture of the opening. The full series is parquet
in the object store.

### Arrival intensity

```http
POST /api/v1/microstructure/datasets/{id}/intensity
{"event_types": ["CANCEL"], "side": "BID", "train_fraction": 0.7}
202 {"job_id": "...", "status": "QUEUED"}

GET /api/v1/microstructure/intensity
GET /api/v1/microstructure/intensity/{model_id}
```

Both models are always returned. `hawkes_is_adopted` is the field to read
before rendering any Hawkes parameter: when it is false those parameters
describe a candidate the held-out test rejected, and `reason` says by how much
it failed.

`predictive_test` carries the decision — a one-sided Diebold-Mariano statistic
on the per-event held-out predictive gain, with a Newey-West variance, against
a stated `critical_value`. Raising `critical_value` makes the gate stricter;
there is no value that disables it. The result is `PARTIAL` when the constant
rate is what is reported, because the richer model was asked for and did not
earn its place.

### Queue outlook

```http
POST /api/v1/microstructure/datasets/{id}/queue
{"side": "BID", "horizon_seconds": 300, "price": "444.00"}
```

Answered inline rather than as a job: it reads one level from one snapshot and
counts the events at that price, and a user moving a price around should not
have to poll for each answer.

**The result is a bracket.** `estimated_fill_probability_range` and
`estimated_wait_seconds_range` are the answer; the two ends differ only in
whether cancellations at the level are assumed to remove size ahead of the
order or behind it, which public data does not say. There is no single
`fill_probability` at the top level of the response and no column that could
hold one — a probability exists only inside a labelled end of the bracket.

Every assumption travels in `assumptions`, and `interpretation` states in the
response itself that this is not a claim about where any exchange has actually
placed an order. A level at which nothing was observed to leave returns
`FAILED` with the reason, rather than a probability of zero that would read as
a statement about the order rather than about the data.

## 13a. Unified order analysis _(planned — P11)_

```http
POST /api/v1/order-analysis
{"portfolio_id": "...", "instrument_id": "...", "side": "SELL",
 "quantity": "5000", "order_type": "LIMIT", "limit_price": "168.00"}
```

Returns valuation, surface deviation, execution cost estimate, and
current -> proposed deltas for Greeks, VaR, stress loss and margin.

**This endpoint returns no recommendation field.** There is no `action`,
`signal`, `rating` or `score` that could be read as advice. That is a contract
guarantee, and a test asserts the response schema contains no such key.

## 14. Pagination, sorting, idempotency

- Cursor pagination: `?limit=`, `?cursor=`; responses carry `next_cursor`.
- Sorting: `?sort=field,-field`, whitelisted per route.
- Mutating POSTs accept `Idempotency-Key`; a repeat with the same key and body
  returns the original result rather than acting twice.
