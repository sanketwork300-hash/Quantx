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
| POST | `/derivatives/pricing` | _(planned P9)_ |
| POST | `/derivatives/pricing/compare` | _(planned P9 — model consensus)_ |
| POST | `/derivatives/surfaces/{surface_row_id}/anomalies` | **[P3]** job |
| GET | `/derivatives/anomalies/{underlying_id}` | **[P3]** latest scan |
| GET | `/derivatives/scans/{scan_id}` | **[P3]** |
| GET | `/derivatives/history/{underlying_id}` | **[P3]** percentiles by tenor |
| GET | `/derivatives/density/{surface_id}` | _(planned P9)_ |

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

## 10. Portfolio _(planned — P4)_

`POST /portfolios`, `GET /portfolios`, `GET /portfolios/{id}`,
`PATCH /portfolios/{id}`, `DELETE /portfolios/{id}`,
`POST /portfolios/{id}/positions`, `PATCH /portfolios/{id}/positions/{pid}`,
`POST /portfolios/{id}/upload`, `GET /portfolios/{id}/valuation`,
`GET /portfolios/{id}/greeks`.

## 11. Risk _(planned — P5/P6)_

`GET /risk/{portfolio_id}/summary`, `POST /risk/var` (job),
`POST /risk/stress` (job), `GET|POST /risk/scenarios`,
`GET /risk/{portfolio_id}/margin`,
`GET /risk/{portfolio_id}/liquidation-vulnerability`.

## 12. Execution _(planned — P7/P8)_

`POST /executions/upload`, `GET /executions`, `GET /executions/{id}/tca`,
`POST /execution/simulate` (job), `POST /execution/compare-strategies` (job).

## 13. Unified order analysis _(planned — P11)_

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
