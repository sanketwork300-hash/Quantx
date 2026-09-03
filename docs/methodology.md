# Methodology

Formulas, conventions, assumptions, numerical methods and limitations. This is
the document a user should be able to read to know exactly what the platform did
to their numbers.

Sections marked _(Phase N)_ specify methodology that is committed but not yet
implemented; they are written now so that the implementation has a spec to meet
rather than a spec written to match whatever the implementation did.

---

## 1. Language policy

The platform is an analytics and research tool. It does not give advice and does
not claim certainty it does not have.

| Never say | Say instead |
| --- | --- |
| fair value, true value | reference value, reference range |
| underpriced / overpriced / cheap / expensive | surface deviation, reference-value deviation |
| arbitrage opportunity | model-inconsistent quote, potential relative-value anomaly |
| will be liquidated at X | estimated margin-shortfall region beyond approximately X% adverse move |
| guaranteed / optimal execution | counterfactual estimate, estimated slippage |
| broker margin | estimated margin under `<named model>` |
| BUY / SELL / signal | (no such output exists) |

Example of the required framing:

> Under the `SimpleRiskMarginModel` approximation with a +8 vol-point stress and
> the stated capital assumption, the portfolio enters an estimated
> margin-shortfall region after an approximately 8.4% adverse move in NIFTY.

## 2. Conventions

### 2.1 Time

- All timestamps are UTC internally. Display-time conversion is a UI concern.
- Time to expiry uses **ACT/365 Fixed** by default:
  `T = (expiry_datetime - as_of) / 365 days`, computed in seconds and divided,
  not in whole days.
- The expiry instant is the contract's settlement time in UTC when the data
  source provides it; otherwise the exchange close on the expiry date, and the
  assumption is recorded as `EXPIRY_TIME_ASSUMED` in provenance.
- Business-day / trading-time conventions (ACT/252-style) are available where a
  calendar is known, sourced from QuantLib, never from hand-written holiday
  lists. The convention used is recorded per calculation.
- `T <= 0` is not a number to clamp. It is a structured non-result
  (`OPTION_EXPIRED`).

### 2.2 Rates and discounting

- Rates are continuously compounded unless a curve explicitly declares otherwise.
- Discount factor `DF(T) = exp(-r T)` for a flat rate; from the curve otherwise.
- The curve used is referenced by id in provenance. "We used a flat 6%" is a
  legitimate configuration and is recorded as such, not hidden.

### 2.3 Sign and unit conventions for Greeks

Reported per **position**, with units always stated:

| Greek | Unit | Position formula |
| --- | --- | --- |
| Delta | currency change per 1 unit change in underlying | `q * M * dV/dS` |
| Gamma | delta change per 1 unit change in underlying | `q * M * d2V/dS2` |
| Vega | **currency change per +1 volatility point (+0.01)** | `q * M * dV/dsigma * 0.01` |
| Theta | **currency change per calendar day** | `q * M * dV/dT / 365` |
| Rho | currency change per +1 basis point | `q * M * dV/dr * 0.0001` |

Higher-order, added in Phase 9 and reported per contract rather than scaled by
position, because they are read as sensitivities of the hedge rather than as
currency amounts:

| Greek | Unit | Formula |
| --- | --- | --- |
| Vanna | delta change per +1 volatility point | `-e^{-q tau} phi(d1) d2 / sigma * 0.01` |
| Volga | vega change per +1 volatility point | `vega * d1 * d2 / sigma * 0.01^2` |
| Charm | delta change per calendar day | `dDelta/dt / 365` |

Vanna and volga are identical for a call and a put — both are second
derivatives of a price pair differing by a term linear in spot and constant in
volatility, so the difference vanishes. Charm is not, because put-call parity's
spot term carries the dividend, and the two differ by exactly
`q e^{-q tau}` per year. Both facts are asserted as tests rather than assumed.

`q` = signed quantity, `M` = contract multiplier. A raw `dV/dsigma` is never
displayed: an unlabelled vega is a bug report waiting to happen. The raw
partials are kept alongside the scaled readings so nothing downstream has to
back out a factor, and the payload carries its own `units` block.

### 2.4 Numerical precision

- **Decimal** for money, prices, quantities, multipliers, tick and lot sizes, and
  anything persisted as a financial fact.
- **float64** inside numerical models (pricing, IV, calibration, PDE, MC), where
  Decimal would be both slow and meaningless — there is no exact decimal answer
  to a Black-Scholes price.
- Conversion happens once, at the domain/`quant` boundary, and back once on the
  way out.
- Comparisons use declared tolerances (`tests/tolerances.py`), never `==`.

## 3. Market data quality scoring — Phase 0 (implemented)

Each sub-score maps a raw measurement to `[0, 1]` through a documented, bounded
transform. All parameters live in `MarketDataQualityConfig` and are recorded in
provenance; there are no unnamed constants in the code.

### 3.1 Staleness

```
stale_score = 0.5 ** (age_seconds / half_life_seconds)
```

An exponential decay with an asset-class half-life (default 300s for equity
options, configurable). Chosen over a linear ramp because staleness damage is
multiplicative: a quote twice as old is roughly half as useful, and there is no
natural "age at which a quote becomes worthless" to place a linear zero at.

`age_seconds = max(0, as_of - exchange_timestamp)`. A quote from the future
(`exchange_timestamp > as_of`) is a consistency violation, not a fresh quote.

### 3.2 Spread

```
spread_score = 1 / (1 + (relative_spread / reference_relative_spread) ** 2)
```

with `relative_spread = (ask - bid) / mid`. Quadratic in the ratio so that
spreads near the reference are barely penalised and spreads several times the
reference collapse quickly. Missing bid or ask gives `spread_score = 0` and a
`MISSING_SIDE` flag, not a score computed from `last_price`.

### 3.3 Liquidity

```
liquidity_score = w_v * sat(volume / ref_volume)
                + w_o * sat(open_interest / ref_oi)
                + w_s * sat(min(bid_size, ask_size) / ref_size)
sat(x) = x / (1 + x)
```

Saturating rather than linear: the difference between 10x and 100x the reference
volume does not matter for whether a quote can be trusted, but the difference
between 0.1x and 1x does. Weights default to `(0.4, 0.4, 0.2)` and are config.

### 3.4 Consistency

Starts at 1.0; each violated consistency check multiplies by its penalty factor:
crossed market, locked market, price outside no-arbitrage bounds, sub-intrinsic
price, extreme jump versus the last accepted observation, `receive < exchange`
timestamp. Multiplicative rather than subtractive so two independent violations
compound instead of cancelling into an arbitrary floor.

### 3.5 Completeness

`completeness_score = present_expected_fields / expected_fields`, where the
expected set depends on asset class and on what the provider declares it can
supply. A provider that never publishes open interest is not penalised for the
absence of open interest; a provider that usually does, is.

### 3.6 Overall

```
overall_score = (prod_i score_i ** w_i) ** (1 / sum_i w_i)
```

A **weighted geometric mean**. This is the key design choice: a zero on any
single dimension drives the overall score to zero. An arithmetic mean would let a
crossed market (consistency 0) score 0.8 overall on the strength of four healthy
dimensions, which is exactly the failure the engine exists to prevent.

### 3.7 Option no-arbitrage bounds, and what is *not* checked

Bound checks come in two tiers, because they are not equally free of assumption.

**Assumption-free, always applied.** `C <= S e^{-qT} <= S` and
`P <= K e^{-rT} <= K` hold for every `r, q >= 0`, so the undiscounted forms are
valid upper bounds with no curve at all. Likewise `price >= 0`.

**Carry-dependent, applied only when the caller supplies both `r` and `q`.**
The lower bounds `C >= max(S e^{-qT} - K e^{-rT}, 0)` and
`P >= max(K e^{-rT} - S e^{-qT}, 0)` depend on the discount factor. Phase 0 has
no yield curve, and inventing `r = 0` tightens the put bound to `K - S`, which a
deep in-the-money European put legitimately trades below whenever rates are
positive. Applying that invented bound would flag a large fraction of a real
chain as sub-intrinsic — a confident wrong answer of exactly the kind build spec
1.1 forbids.

So when carry is unknown the sub-intrinsic check is **skipped**, and the
ingestion result says so with an `INGESTION_CARRY_ASSUMPTION_UNAVAILABLE`
warning. When it is supplied, the values are recorded in provenance and echoed
into the context of every flag they influenced. The full arbitrage suite arrives
in Phase 2 with a real curve and an estimated forward.

**Severity by magnitude.** A violation is `INFO` when it is smaller than
`max(1 tick, 1 x quoted spread)`, `WARNING` up to three spreads, and `ERROR`
beyond that. On a discrete strike grid with wide markets, sub-spread violations
are ubiquitous and not exploitable; treating them as errors would exclude most
of a real illiquid chain, and a boolean "violated" would erase the distinction
between noise and a genuinely broken quote.

### 3.8 Exclusion

The quality engine classifies; the ingestion pipeline decides. Default policy:
exclude a quote when any flag has severity `ERROR`. The threshold is a request
parameter and is recorded in provenance. Every excluded quote stores exactly one
**primary** `exclusion_reason` (the highest-severity, earliest-declared flag) plus
the complete flag list. Nothing is deleted.

## 4. Forward estimation — Phase 1 (implemented)

Three estimators, each returning `value, method, confidence, observations,
residual_error`:

**Spot-carry:** `F = S * exp((r - q) * T)`. Confidence degrades with the quality
of `S` and the uncertainty of the dividend/borrow assumption `q`.

**Futures-derived:** the forward is read from a listed future of matching expiry,
adjusted for the basis when expiries differ. Highest confidence when a liquid
matched-expiry future exists.

**Put-call parity regression:** for each strike with both a call and a put,
`C - P = DF * (F - K)`. Regressing `C - P` on `K` gives slope `-DF` and intercept
`DF * F`, so both the discount factor and the forward are recovered from option
prices alone. Confidence comes from the number of usable strike pairs, the
regression R-squared and the residual dispersion; strikes are weighted by
liquidity and spread.

The estimators are reported side by side and the highest-confidence one is
selected. Disagreement between them is information — it usually means bad data
or an unstated carry — so it is surfaced as a number rather than averaged away.

Put-call parity outranks spot-carry in practice because it needs **no rate or
dividend assumption at all**: both the forward and the discount factor come out
of the option market itself. A wrong assumed curve therefore degrades only the
spot-carry estimate and shows up as disagreement, which a test asserts.

Confidence for the parity regression is the weighted geometric mean of an
observation-count factor (`sat(pairs / 6)`) and a fit factor
(`1 / (1 + (rms / half_spread)^2)`) — the same saturating and ratio-penalty
shapes the data-quality engine uses, so "confidence" means the same thing
across the platform. A recovered discount factor outside `[0.5, 1.02]` is a
degenerate regression and returns no estimate rather than the forward it
implies.

## 5. Implied volatility — Phase 1 (implemented)

Model: Black-76 on the forward,
`C = DF * [F * N(d1) - K * N(d2)]`,
`d1 = (ln(F/K) + 0.5 sigma^2 T) / (sigma sqrt(T))`, `d2 = d1 - sigma sqrt(T)`.

Inversion: monotonic in sigma, so bracket and solve. Default bracket
`[1e-6, 5.0]`, expanded once if the target price lies outside; Brent's method;
absolute tolerance 1e-8 on price, capped iterations.

Pre-checks that produce a structured non-result instead of a wrong number:

- price below intrinsic (`PRICE_BELOW_INTRINSIC`)
- price above the no-arbitrage upper bound (`PRICE_ABOVE_BOUND`)
- `T <= 0` (`OPTION_EXPIRED`)
- zero time value (`NO_TIME_VALUE`)

### Solver

A **vectorized safeguarded Newton** over a maintained bracket solves a whole
chain at once; any element that fails falls back to scalar bracketed Brent.
Newton alone steps off the flat wings of the price curve where vega underflows;
keeping a bracket and bisecting whenever the Newton step escapes it gives
Newton's speed with bisection's guarantee.

Termination is on the **bracket width**, not the price residual. That
distinction is load-bearing: for a deep out-of-the-money option a residual of
1e-10 is reached while the volatility is still wrong in the sixth decimal, so
converging the argument is what makes the answer accurate where it is least well
determined.

### Reported conditioning

The result object always carries `converged`, `iterations`, `lower_bound`,
`upper_bound`, `solver` and `error`, plus two fields that matter more than they
look:

* `vega` — the price sensitivity at the solution.
* `uncertainty` — approximately `price_ulp / vega`, the volatility moved by one
  representable change in the price.

For a deep in-the-money option the price is nearly flat in volatility. Its
implied volatility can reproduce the observed price *bit for bit* and still be
uncertain in the fifth decimal, because many volatilities produce that same
float64 price. No algorithm can do better; the honest response is to say so.
Phase 3 confidence scoring consumes `uncertainty`, the API returns it, and the
UI truncates the displayed precision to match. Nothing in the platform prints an
implied volatility to more digits than its conditioning supports.

### Validation

Round-trip recovery over 700 parameter combinations: 1e-6 wherever the problem
is well conditioned, and within a few multiples of the reported `uncertainty`
where it is not. Cross-checked against `vollib` (see the finding in
`docs/references.md` — its Black entry point is unusable in this environment)
and against the end-to-end recovery of a known synthetic surface.

## 6a. Reference values from a surface — Phase 2 (implemented)

A fitted surface answers `reference_iv(strike, expiry)` and, given an option
type, `reference_price`. Three things about that answer:

- **It is a pure function of the persisted parameters.** `(a, b, rho, m, sigma)`
  plus the forward, the discount factor and the maturity are all it depends on.
  A stored surface is never re-fitted on read, so an old analysis reproduces
  exactly rather than approximately.
- **It says how it was obtained.** `EXACT_SLICE` when the expiry was fitted;
  `INTERPOLATED_MATURITY` when it lies between two, linear in **total variance**
  at fixed `k` (variance is what is additive in time, so interpolating between
  two calendar-consistent slices stays calendar-consistent);
  `EXTRAPOLATED_MATURITY` beyond the range, by scaling the nearest slice's total
  variance with maturity.
- **It carries flags.** `EXTRAPOLATED_STRIKE` outside the fitted `k` range,
  `EXTRAPOLATED_MATURITY`, `SLICE_DEGRADED` when the slice fitted but is not
  admissible, `LOW_CONFIDENCE_FORWARD` when the forward it sits on was itself a
  weak estimate. Flagged, never withheld: the user decides what to do with a
  wing value, and hiding it would be its own kind of dishonesty.

The output is called a *reference* value. It is not a fair value, it never
overwrites an observed market IV, and it lives in its own tables.

## 6. Smile representation — Phase 1 (implemented)

```
k = ln(K / F)              log-moneyness
w(k, T) = sigma^2(k, T) T  total implied variance
```

Total variance is the working coordinate because the no-arbitrage conditions are
natural in it (butterfly is convexity of `w` in `k`; calendar is monotonicity of
`w` in `T`), and because it removes the `1/sqrt(T)` distortion that makes short
expiries look artificially violent in IV space.

Raw observations and fitted values are stored in separate tables and are never
merged into one series.

**Bid/ask envelope.** Implied volatility is solved at the bid and at the ask as
well as the mid. The envelope width is how much of any apparent deviation from a
reference is simply the width of the market, and it is plotted as a band rather
than a footnote.

**Selection.** One quote per strike carries the smile. Out-of-the-money wins
(`K > F` calls, `K < F` puts): OTM options carry the time value that determines
the smile, while an ITM quote is dominated by intrinsic value and its implied
volatility is numerically fragile. Inside `|k| <= 0.02` both sides are
informative, so the tie-break is the better-conditioned quote, and an exact tie
goes to the call. Which side wins matters less than the rule being
deterministic: reproducibility requires that the same input always selects the
same quote.

**Summary statistics.** ATM volatility interpolates *total variance* to `k = 0`
and converts back (variance is what is additive in time), and refuses to
extrapolate when the observed strikes do not span the money. Skew and curvature
are local least-squares fits over a window around the money.

## 7. SVI — Phase 2 (implemented)

Raw parameterization:

```
w(k) = a + b [ rho (k - m) + sqrt((k - m)^2 + sigma^2) ]
```

### Constraints, imposed during the fit

| Condition | Form | Source |
| --- | --- | --- |
| Parameter domain | `b >= 0`, `|rho| < 1`, `sigma > 0` | definition |
| Non-negative minimum variance | `a + b sigma sqrt(1 - rho^2) >= 0` | definition |
| Wing slope | `b (1 + |rho|) <= 2` | Lee's moment formula (2004) |
| No negative implied density | `g(k) >= 0` on a grid | Gatheral & Jacquier (2014) |

with Durrleman's function

```
g(k) = (1 - k w'/(2w))^2 - (w'^2 / 4)(1/w + 1/4) + w''/2
```

evaluated from **analytic** derivatives of `w`, because `g` divides by `w` and
squares `w'`, and differencing noise there turns an admissible slice into a
spurious violation.

All four are in the optimizer's feasible set, not checked afterwards. That
distinction is load-bearing: measured on a 24,000 index chain, a single call
mispriced by 120 points was enough to bend an unconstrained fit into `min g =
-0.19`. Constrained, the fit absorbs the bad quote as error and stays
admissible, while the raw-market arbitrage report still names the quote.

The constraint grid is coarser (61 points) than the check grid used afterwards
(801 points over a wider range), so a violation that slips between constraint
points is still caught and reported — the status is `DEGRADED`, never silently
`CONVERGED`.

### Objective and optimizer

Weighted least squares on total variance; weights are the spread and liquidity
scores from the Phase 0 quality engine, so a wide, thin quote does not drag the
slice and the fitter needs no separate notion of which quotes are good.

SLSQP from ten starting points: five fixed (one moment-based, from the smile
minimum and the difference in wing slopes) and five seeded perturbations. The
**seeding is deterministic** — a surface that refitted differently on each run
could not be reproduced, and reproducibility is the entire reason for storing
it.

Recorded per slice: parameters, RMSE in total variance, weighted RMSE, RMSE and
worst error in **volatility points** (the unit practitioners read), observation
count, optimizer, message, iterations, starts attempted and feasible, minimum
`g` and where it occurred, wing slope, constraint status, and the fitted
log-moneyness range.

### What a narrow chain can and cannot tell you

SVI has five parameters. Over a window of ~0.1 in log-moneyness — a realistic
retail chain — many parameter sets fit the observed arc equally well. Measured
on `k in [-0.07, 0.03]`: the fitted curve is right to 0.005 volatility points in
sample while the parameters miss the truth by 0.05, and the wings are
essentially free. Over `k in [-0.8, 0.8]` the parameters are recovered to 1e-5.

So the fitted *curve* is the meaningful object inside the fitted range, and the
*parameters* are meaningful only when the range is wide. The platform reports
`SURFACE_NARROW_STRIKE_RANGE`, records `k_min`/`k_max` per slice, and flags any
reference value outside them as `EXTRAPOLATED_STRIKE`.

### Which quotes are allowed to inform a slice

A quote's implied volatility is only as well determined as its price. The solver
reports `uncertainty` = price resolution / vega, where resolution is half the
spread or one tick for a locked market — **not** a float64 ulp. Quotes above one
volatility point of uncertainty are dropped as `ILL_CONDITIONED`.

This is the difference between a usable surface and a useless one on wide
chains. A deep out-of-the-money weekly is worth less than a tick and is quoted
locked at the floor; inverting it is numerically flawless and returns 50%
against a true 12%. Measured: a dozen such quotes moved a fitted slice by 104
volatility points; excluded, the same chain fits to under one.

### Calendar

Checked across slices as monotonicity of total variance in maturity at fixed
`k`. Per-expiry SVI fits each slice independently and cannot *prevent* calendar
arbitrage, so violations are detected and reported — with an explicit warning
saying exactly that — until an arbitrage-free global parameterization (SSVI)
lands in Phase 9.

## 8. Arbitrage diagnostics — Phase 2 (implemented)

Static bounds (European, with carry `q`):

```
max(S e^{-qT} - K e^{-rT}, 0) <= C <= S e^{-qT}
max(K e^{-rT} - S e^{-qT}, 0) <= P <= K e^{-rT}
```

Put-call parity: `C - P = S e^{-qT} - K e^{-rT}` (equivalently `DF (F - K)`).

Vertical: `C` non-increasing in `K`; `P` non-decreasing in `K`;
`|dC/dK| <= DF`.

Butterfly: `d2C/dK2 >= 0`, tested on adjacent strike triples and, on the fitted
surface, through `g(k) >= 0`.

Calendar: total variance non-decreasing in maturity at fixed log-moneyness.

Every violation reports type, expiry, strike, **magnitude**, the **tolerance it
was judged against**, severity, detail and the affected instruments. Magnitude
matters: a butterfly violation of 0.01 currency units on a wide-spread strike
triple is noise, one of 5.00 on liquid strikes is not, and a boolean would erase
that distinction.

Severity is magnitude relative to the local quoted spread: within one spread is
`INFO`, within three `WARNING`, beyond that `ERROR`. A parity residual inside the
cost of crossing both legs is not reported at all. Calendar breaches are judged
relative to the shorter slice's own total variance.

Raw-market and fitted-surface violations are reported and stored in separate
collections, and the raw checks run **before** the fit, so a bad fit is never
blamed on the market and a bad market is never hidden by a smooth fit.

## 8a. Surface characteristics and history — Phase 3 (implemented)

Level, skew and curvature are taken analytically from the fitted parameters:

```
sigma  = sqrt(w / tau)
sigma' = w' / (2 tau sigma)
sigma''= (w''/tau - 2 sigma'^2) / (2 sigma)
```

evaluated at `k = 0`. Recorded at **standard tenors** (7, 30, 60, 90, 180, 365
days) rather than at fitted expiries, because expiries roll and a time series of
"the October slice" ends in October. A tenor on a fitted expiry is exact;
between two it interpolates total variance and interpolates skew and curvature
on the same weight; outside the range it assumes flat forward variance and is
flagged `EXTRAPOLATED_MATURITY`.

Percentiles and z-scores are computed against the underlying's own history, and
**the observation count travels with every answer**. Below a stated minimum
(20 observations) a percentile is still reported but marked unreliable — a
percentile from eight surfaces is a different kind of statement from one from
six hundred, and presenting them identically is the dishonest part. A sample
with no variation returns no z-score rather than a large one from rounding
noise.

## 8b. Anomaly detection — Phase 3 (implemented)

### What is measured

```
iv_difference      = market_iv - reference_iv        (observed minus model)
relative_deviation = iv_difference / reference_iv
```

`market_iv` is implied by an observed price; `reference_iv` comes from the
fitted surface. They are separate fields in separate tables and neither is ever
written from the other.

### What counts as unusual

Not a fixed threshold in volatility points — that flags every illiquid wing
quote in the market and nothing else. The difference is standardised by the
combined size of everything that could account for it:

```
explained_scale = sqrt(half_bid_ask_iv_envelope^2
                       + slice_calibration_rmse^2
                       + iv_numerical_uncertainty^2)
z_score = iv_difference / explained_scale
```

Every term is measured elsewhere in the platform for its own reasons: the
envelope by solving implied volatility at both sides of the market (Phase 1),
the RMSE by the calibration (Phase 2), the resolution by the price-resolution
conditioning (Phase 2). The denominator is floored so a locked market and a
perfect fit cannot turn rounding into a hundred-sigma reading.

A second, time-series z-score is computed against a contract's **own** past
deviations once more than one scan exists. Until then it is `null` with an
observation count of zero, and the explanation says so — never a zero score,
which would read as "no deviation" rather than "no history".

### Flagging policy

A quote is flagged when all of:

- `|z_score| >= min_z_score` (default 2)
- the reference lies **outside** the quoted bid/ask implied-volatility envelope
  — if the market's own width spans the model value, it accounts for the whole
  difference
- `confidence >= min_confidence` (default 0.3)
- `liquidity_score >= min_liquidity` (default 0.05) — a wing quote nobody trades
  deviating from a fitted curve is not news

The policy is a request parameter and is recorded in provenance, including the
formula for `explained_scale`, because it decides the answer.

### Confidence, and how it explains itself

A weighted geometric mean — the same aggregation the data-quality engine uses,
so the word means the same thing across the platform — over:

| Factor | Source | Weight |
| --- | --- | --- |
| Data quality | the quote's Phase 0 overall score | 1.0 |
| Liquidity | volume, open interest, quoted size | 1.0 |
| Surface fit | the slice's calibration RMSE in vol points | 1.5 |
| Measurement resolution | the IV's price-resolution uncertainty | 1.0 |
| Slice breadth | how many quotes the slice was fitted on | 0.5 |
| Extrapolation | penalties for extrapolated strike or maturity, degraded slice | 1.5 |

Confidence is in *the measurement*, not in a trade. Each factor emits an
explanation naming its measured value and whether it supports or reduces
confidence, which is what makes "why is confidence 0.42?" answerable from the
response itself rather than from a narrative.

### Language

The scanner emits no direction, rating, target or recommendation, and the words
buy, sell, cheap, expensive, underpriced, overpriced and arbitrage appear
nowhere in its output. That is asserted by a test over the entire serialised
response, at both the domain and the HTTP layer, because it is a contract rather
than a style preference.

## 8c. Portfolio valuation — Phase 4 (implemented)

### Price selection

A position is priced from the first source that exists, and the source is
recorded rather than inferred:

| Order | Source | `valuation_method` |
| --- | --- | --- |
| 1 | Observed two-sided mid, fresher than `STALE_QUOTE_SECONDS` (900s) | `MARKET_MID` |
| 2 | Observed two-sided mid, older than that | `STALE_MARKET` |
| 3 | Last traded price, when there is no two-sided market | `MARKET_LAST` |
| 4 | Reference price from the fitted surface | `MODEL_REFERENCE` |
| 5 | Nothing usable | `UNAVAILABLE` |

`Quote.mid_price` returns `None` rather than falling back to the last trade, so
step 3 is an explicit, flagged substitution (`POSITION_LAST_PRICE_FALLBACK`) and
never a silent one: a print is not a market.

### Position value and Greeks

```
scale         = signed_quantity * contract_multiplier
market_value  = price_used * scale
base_value    = market_value * fx_rate
unrealised    = (market_value - average_price * scale) * fx_rate
greek         = unit_greek * scale * fx_rate
```

The scaling by quantity, multiplier and FX rate happens in exactly one place, so
a Greek can be neither double-scaled nor unscaled. Units are carried in the
field names (`vega_per_vol_point`, `theta_per_day`, `rho_per_bp`) and restated in
`GREEK_UNITS` on every response.

Unit Greeks come from Black-Scholes-Merton (§2.3) at

- the volatility implied by the contract's own observed price where there is
  one — `greek_source: MARKET_IV`, preferred because that volatility reprices
  the observation exactly, which the surface does not; otherwise
- the surface's reference volatility — `REFERENCE_IV`.

Linear instruments (equity, index, future, perpetual, spot, FX) have a delta of
one per unit and no second-order sensitivity to report. Nothing about them is a
model estimate.

### Time to expiry

`tau = year_fraction(as_of, combine(expiry, settlement_time_utc), day_count)`.
Without a settlement time, `tau` is undefined, and option Greeks are omitted
with `POSITION_NO_GREEKS` rather than computed against a guessed moment. The
observed price is still an observation; only the model half is absent.

### Currency

Conversion uses the FX rate in the same `MarketState` as the prices, and the
rate is recorded on the position. A position in a currency with no rate in the
snapshot is left unvalued with `POSITION_NO_FX_RATE` rather than converted at a
rate from a different moment.

### Aggregation

Buckets over underlying, expiry, asset class, strategy tag and currency each sum
the *same* per-position numbers, so every dimension totals to the portfolio
total. A position that does not carry a dimension is absent from that grouping
rather than assigned a fabricated key, and each bucket reports how many of its
members were actually priced.

## 8d. SSVI global surface — Phase 9 (implemented)

```
w(k, theta) = (theta / 2) * { 1 + rho phi(theta) k
                              + sqrt[ (phi(theta) k + rho)^2 + (1 - rho^2) ] }
phi(theta)  = eta / ( theta^gamma (1 + theta)^(1 - gamma) )
```

`theta(T)` is at-the-money total variance and `w(0, theta) = theta` exactly.
The `(1 + theta)` factor in the power law keeps `theta * phi` bounded as
maturity grows, which is what keeps the butterfly condition satisfiable at the
long end rather than only near the front.

Admissibility, all three imposed inside the optimizer rather than checked after
it (Gatheral & Jacquier 2014, §4):

| Condition | Form | Status |
| --- | --- | --- |
| Calendar | `theta` non-decreasing in `T` | necessary and sufficient for SSVI |
| Butterfly | `theta phi (1 + |rho|) < 4` and `theta phi^2 (1 + |rho|) <= 4` | sufficient only |
| Butterfly | Durrleman `g(k) >= 0` on a grid | the actual condition |

The closed-form bounds and Durrleman's function are both evaluated and both
stored. A surface can fail the bounds and still have a non-negative density
everywhere, and treating a sufficient condition as if it were necessary would
mean rejecting admissible surfaces on the strength of a theorem rather than of
the surface in front of us.

Interpolation of `theta` between fitted expiries is monotone piecewise-cubic, so
the calendar condition holds everywhere and not merely at the knots. Below the
first expiry `theta` is linear to `theta(0) = 0`, which is a boundary condition
rather than an extrapolation — a zero-length period has zero variance. Above the
last expiry it is flat, which is a refusal to extrapolate. Both are flagged.

## 9. Risk-neutral density — Phase 9 (implemented)

Breeden-Litzenberger: `f(K) = e^{rT} d2C/dK2`, evaluated on the smooth fitted
surface by a relative-bump second difference, never on raw quotes. Negative
regions are flagged as evidence of residual arbitrage or over-fitting.

Four diagnostics travel with every density: the trapezoidal mass, the implied
mean against the forward (the martingale property, and a measure of how much of
the tail the strike range truncated), the negative mass, and whether the strike
range is wide enough to contain the distribution.

A density is **admissible** when it is non-negative *and* normalised, and
quantiles are computed only for an admissible one. A quantile normalises by the
mass it found, so on a truncated window it would be a quantile of the window
rather than of the density, and would look entirely reasonable while being
wrong.

Presented as a diagnostic and an interpretability aid — it is a risk-neutral
density and is never described as a forecast of what the market will do.

## 10. Local volatility — Phase 9 (implemented)

Dupire in total-variance form (Gatheral), which is numerically far better behaved
than the raw price-derivative form:

```
sigma_loc^2(k, T) = (dw/dT) / [ 1 - (k/w)(dw/dk) + 0.25(-0.25 - 1/w + k^2/w^2)(dw/dk)^2 + 0.5 d2w/dk2 ]
```

Derivatives are analytic, not bumped: a finite-difference error in `d2w/dk2`
becomes a local volatility wrong by an amount nothing downstream can detect.

Rules: derivatives are taken on the fitted arbitrage-aware surface only;
denominators near zero produce `INVALID`, not a clipped value; extrapolated
regions are flagged; each `LocalVolPoint` carries `confidence` and `flags`; and
the stored grid conserves its points, `total = valid + flagged`.

Evaluating the surface at calendar time `t` uses the forward **to t**, not the
forward to the option's maturity: the surface is parameterised in
`k = log(K / F_T)`, and holding one forward fixed across the time grid shifts
every lookup along the smile by the carry. The forward curve is
`spot * exp(carry * t)` with `carry` fitted by least squares through the origin
on the observed forwards, stated as the deterministic-carry approximation it is.

The validation is a round trip: a local-volatility surface derived from an
implied surface must reprice that implied surface. It does, to under 0.2%.

## 11. Local-vol PDE and Monte Carlo — Phase 9 (implemented)

Crank-Nicolson on `x = ln S` with Rannacher start-up (two fully-implicit
half-steps) to damp the oscillations Crank-Nicolson produces against a
non-smooth payoff. Non-uniform grid concentrated near the strike; Dirichlet
boundaries from asymptotic payoff behaviour.

The solution is read at the spot by a quartic fit through five nodes. A
quadratic through three nodes gives a second derivative accurate only to `O(h)`,
and the convergence test reported gamma at first order until it was widened —
which is the point of testing the order rather than the error.

Mandatory validation: with `sigma_loc(S, t) = sigma` constant, the PDE price must
converge to the Black-Scholes price, and the test asserts the empirical order of
convergence, not merely that the final error is small. Measured: 2.00 for price,
2.00-2.03 for delta and gamma, on a uniform and on a concentrated grid.

**Monte Carlo.** Exact geometric Brownian motion terminals, antithetic pairs,
and a control variate on the discounted terminal price, whose expectation
`S e^{-q tau}` is known exactly under the pricing measure. Measured variance
reduction about 78%. A simulated price is meaningless without its standard
error, so the two are never separated, and the seed and path count are stored
with the result: the same pair reproduces the number exactly and a different
seed does not.

**Heston.** Characteristic function in the Albrecher et al. (2007) little-trap
form, which keeps the complex logarithm on its principal branch at long
maturities where the naive form silently crosses the cut. Puts come from
put-call parity rather than a second quadrature, so a call and a put from the
same parameters cannot drift apart. The integration limit scales with
`1 / sqrt(v tau)`: the integrand decays like `exp(-u^2 v tau / 2)`, so a fixed
truncation is an unstated assumption that the tail has died, and for a one-week
option it has not. Cross-checked against QuantLib's `AnalyticHestonEngine` to
1.5e-11 absolute across maturities from one week to ten years.

## 12. VaR and Expected Shortfall — Phase 5 (implemented)

Everything works on a **loss** convention: a loss is a positive number, so
`loss = -pnl`. The sign is applied in exactly one place
(`quant/statistics/var.py::losses_from_pnl`), because mixing the two is the
classic sign error in risk code.

### Historical simulation

Aligned factor returns over the recorded lookback, with **full repricing** under
each historical scenario. VaR is the sample quantile by linear interpolation
between order statistics (Hyndman & Fan type 7, NumPy's default), which converges
to the analytic quantile as the sample grows — validated against the normal and
uniform closed forms in `tests/quant_validation/test_var.py`.

Expected shortfall is the mean of the observations at or beyond that quantile,
and is reported with the number of observations in that tail. At 99% a 250-day
sample puts 2.5 observations in the tail, so a `tail_observations` of 2 is not a
statistic and the response says so rather than presenting it as one.

### Parametric

`VaR = mu + z_alpha * sigma` on the loss distribution, with

    ES = mu + sigma * phi(z_alpha) / (1 - alpha)

for the normal. The portfolio is linearised by delta and vega: the exposure to a
1.0 return of factor *i* is `sum(delta * spot)` over that underlying, and the
exposure to 1.0 of volatility is `sum(vega_per_vol_point / 0.01)`. Then
`sigma_pnl = sqrt(w' Sigma w)`.

The response states inline that this ignores convexity, and carries
`RISK_PARAMETRIC_ON_NONLINEAR_BOOK` whenever the book contains an option. It is
never returned as the sole measure for an option book.

`normal_quantile` is Acklam's rational approximation refined by one Halley step.
Above the median the Halley residual is computed from the *complement*
(`(1 - p) - upper_tail`) rather than `F(x) - p`, because the latter loses every
digit when both terms are within 1e-9 of one. That single change is the
difference between nine correct digits and sixteen at a 1e-9 tail; it agrees
with `scipy.stats.norm.ppf` to 2.2e-16 relative across 1e-12 to 1 - 1e-12.

### Monte Carlo

An explicit multivariate factor model — a stated mean vector and covariance,
normal or Student-t — simulated from a seed and **fully repriced**. The same
seed and factor panel reproduce the numbers exactly, and a test asserts it.

- **Drift is set to zero, not estimated.** A mean return from a few dozen
  observations is indistinguishable from noise, and letting it into a one-day
  risk number would put a trend nobody measured into the answer.
- **Antithetic variates** are on by default: for every draw `z` the draw `-z` is
  also used, which removes the odd part of the sampling error exactly. The
  sample mean of the draws is then zero to machine precision.
- The factorisation is by eigen-decomposition rather than Cholesky, so a
  singular covariance (one factor a copy of another) still simulates.
- **Student-t is scaled so the marginal variance matches the covariance
  supplied**, rather than being inflated by `nu / (nu - 2)` without saying so.
  Nothing calibrates `nu`; it is a parameter the user sets.

### Uncertainty in the estimate itself

Reported as a **bootstrap interval**, not an asymptotic standard error. The
asymptotic formula needs the density at the quantile, and estimating a density
from the same thin tail whose uncertainty is in question is circular. Resampling
makes no such assumption.

### Horizon scaling

`scale_to_horizon` implements square-root-of-time and is offered because it is
the market convention, but the VaR path does not use it: multi-day horizons come
from overlapping windows in the actual series. Square-root scaling is valid only
for independent, identically distributed increments with no drift, and an option
book's risk does not scale that way because its Greeks change as the underlying
moves.

### Covariance

The sample covariance, with its weakness stated rather than papered over: it is
noisy once the number of factors approaches the number of observations, and
singular beyond. The platform flags `COVARIANCE_FEW_OBSERVATIONS` below ten
observations per factor and `COVARIANCE_RANK_DEFICIENT` at or beyond parity,
rather than silently shrinking — a shrinkage intensity chosen to make a matrix
invertible is a modelling decision the user should get to see. Only float-noise
negative eigenvalues are repaired, and the repair reports that it happened.

## 13. Stress testing — Phase 5 (implemented)

Shocks are applied to each position's own pricing anchors and the position is
**fully revalued**. Shock types: `ABSOLUTE`, `PERCENTAGE`, `VOL_POINTS`,
`BASIS_POINTS`.

    new_spot = spot * (1 + pct) + abs
    new_vol  = max(vol * (1 + rel) + points, 1e-4)
    new_rate = rate + bp / 10000
    new_tau  = max(tau - decay_days / 365, 0)
    pnl      = (V(new) - V(base)) * quantity * multiplier * fx

The base price is the model price at the unshocked anchors, and the anchor
volatility is the one implied by the position's own observed price. A null
scenario therefore reprices to the base value **exactly** — the property that
makes every stress P&L a statement about the shock rather than about the model.

Greek-based approximation is available and is **labelled as an approximation**
in the response:

    dV ~ delta*dS + 0.5*gamma*dS^2 + vega_per_vol_point*(dsigma/0.01)
         + rho_per_bp*(dr*10000) + theta_per_day*dt

The units are the ones the Greeks were named for, so the shocks are converted
into those units rather than the Greeks into the shocks'. For large shocks the
two genuinely differ, and a test asserts that they do — if they did not, the
full revaluation would not be earning its cost.

## 14. Margin — Phase 6 (implemented)

**The platform does not know your broker's margin.** Exchange and broker
methodologies are proprietary, versioned, and change without notice. Everything
below is a model defined in this repository, and every result says so.

### `SimpleRiskMarginModel`

    scan_loss     = max(0, -min P&L over the declared grid)
    floor         = short_option_minimum_rate x sum(short-option notional)
    concentration = add_on_rate x max(0, largest_gross - threshold x total_gross)
    margin        = max(scan_loss, floor) + concentration

Short-option notional is strike x multiplier x |quantity| — what the contract
controls on exercise, not the premium it cost.

The floor is a floor rather than an addition: a book whose scan loss is small
only because every short option is far out of the money is not a book with no
risk. The concentration charge is an addition, because it is a different
statement about a different weakness of the grid.

Every grid point is a genuine repricing of every position. The grid is a
parameter and travels on every result, because a margin number *is* the worst
loss over the moves someone chose to look at.

### The two rates default to zero

Not to a plausible-looking 2%. A rate at which a venue floors a short option is
that venue's rule, and inventing one here would produce a confident number about
the quantity that can force a user out of a position. The response instead
carries `MARGIN_NO_SHORT_OPTION_MINIMUM` and says in words that the estimate
understates a book of far out-of-the-money shorts.

### Confidence

The weighted geometric mean of coverage (repriceable positions over all),
grid containment (0.5 when the worst point sat on a boundary that could be
widened past, 1.0 otherwise) and mark consistency (how closely the model at
today's anchors reproduces the marked value). A geometric mean so that one bad
dimension pulls the whole score down rather than being averaged away.

An unbounded-loss book can never contain its worst case in a finite grid, so its
containment score stays at 0.5 permanently. That is the honest reading: such an
estimate genuinely is a lower bound.

### Utilisation and buffer

`utilisation = estimated_margin / eligible_capital` and
`buffer = eligible_capital - estimated_margin`, both only when capital is
supplied. When it is not, both are null with a warning; they are never defaulted
to portfolio value, which is a different quantity. A database CHECK refuses a
stored row carrying a buffer without the capital it was measured against.

### Margin vulnerability

A ladder scan in **both** directions. At each rung the book is fully repriced
and the margin model is rerun on the moved market, because both the value and
the requirement change as the market moves. Available capital is the stated
capital plus the mark-to-model change, which assumes no cash movement and no
position being closed — all stated.

The reported crossing is interpolated linearly in the underlying's return
between the two rungs that bracket it, and **those two rungs are reported with
it**, so the coarseness of the estimate is visible rather than hidden behind a
decimal place.

No single guaranteed liquidation price is ever produced, because producing one
would require broker rules this platform does not have and cannot obtain.

## 15. Transaction cost analysis — Phase 7 (implemented)

Implementation shortfall for a buy: `IS = (P_exec - P_benchmark) * Q * M`; sign
reversed for a sell, so a **positive IS is always a cost**. The side's sign is
applied in exactly one place (`Side.sign`), because paying above the benchmark on
a buy and receiving below it on a sell are the same thing and reporting one as a
gain is a sign error with a very confident face.

Reported in three units:

    currency     = (P_exec - P_benchmark) * side_sign * Q * multiplier
    basis points = (P_exec - P_benchmark) * side_sign / P_benchmark * 10000
    percent      = the same fraction, times 100

The multiplier scales the currency amount and nothing else — a shortfall in
basis points is a property of the prices, not of the contract size.

### Benchmarks

Each declares window, market-data source and method. Arrival is the prevailing
mid at the submit timestamp; when no submit timestamp exists the first-fill price
is used as a proxy and flagged `ARRIVAL_PROXY_USED`, because silently
substituting one benchmark for another is how TCA becomes fiction — and because
the substitution is *biased*: measuring from the first fill hides everything that
moved before it, so the proxied shortfall is systematically smaller.

The order's window runs from **submission** to the last fill, not from the first
fill, since the delay before trading started is part of what it cost.

The interval TWAP is piecewise-constant in time: each observation holds until the
next arrives, which is what the platform actually knows. Interpolating between
observations would invent prices the market never showed.

The prevailing mid is weighted by **fill quantity** rather than by time, because
it answers "what was on screen while I was trading?" and a fill of 500 at a wide
moment costs more than a fill of 5.

### Coverage before computation

An interval statistic is computed only when the observations both number enough
(4) and span enough of the window (60%). Below either, the benchmark reports
itself unavailable with a reason and **no shortfall is computed against it**. A
missing benchmark is not a cost of zero, and the two never render the same way.

The interval VWAP additionally requires *interval* volume. A cumulative session
volume carried on a snapshot is not that, and treating it as though it were would
weight the whole day onto one instant, so the benchmark reports itself
unavailable rather than degrading into a time-weighted average under a
volume-weighted name.

### Cost decomposition

**Model-based, and labelled as such component by component:**

```
spread cost = 0.5 * quoted spread at fill, weighted by fill quantity  MODELLED
fees        = as recorded on the fills                                MEASURED
impact      = not modelled in this phase                              NOT_MODELLED
timing      = IS - spread - fees                                      RESIDUAL
opportunity = unfilled quantity * (reference - benchmark)             MODELLED
```

Only fees are an observation. Impact carries no number at all: it is Phase 8's
work, it is not zero, and it sits inside the residual — which the residual's own
`basis` says. Presenting the split as measurement would be false precision, so
the decomposition carries a `caveat` field stating that spread, impact and timing
are not separately observable.

Opportunity cost needs the order's intended quantity, which only the trade log
can supply. It is never inferred from the fills: assuming an order filled
completely because the log shows only fills is how an unfilled order silently
reports no opportunity cost.

## 16. Market impact and execution simulation — Phase 8 (implemented)

### Impact

```
square root:  permanent = eta   * sigma * sqrt(Q / ADV)
              temporary = gamma * sigma * sqrt(participation)
linear:       permanent = eta   * sigma * (Q / ADV)
              temporary = gamma * sigma * participation
```

The square-root dependence on relative size is the most robust empirical
regularity in the impact literature (Almgren et al. 2005; Gatheral 2010). The
linear model is kept as a comparison baseline precisely because it overstates
large orders — a conclusion that survives both is one that does not depend on
the choice.

**No coefficient ships calibrated.** `eta` and `gamma` default to `1.0`, which
is the identity rather than an estimate: at that setting the output is the shape
of the model in units of `sigma * sqrt(Q/ADV)`, and every result carries
`IMPACT_COEFFICIENT_NOT_CALIBRATED`. Coefficients are regime-, venue- and
period-dependent; adopting a published estimate as a default would assert a
measurement of a market nobody here observed. ML models remain gated on having
enough labelled executions, evaluated out-of-time and bucketed by order size.

Permanent and temporary impact are returned separately because a simulator needs
them so: permanent moves the reference for every later slice, temporary is paid
on the slice and does not persist. The temporary term is driven by the
**participation rate**, which is what a schedule actually controls.

### Schedule allocation

Slices must sum to the parent quantity exactly. Cumulative floors give each
slice its whole-lot share and the leftover lots go one at a time to the largest
fractional parts, which keeps the slices near their weights *and* makes the sum
exact. Any sub-lot dust joins the largest slice rather than disappearing.

### Simulation

    fill = observed_mid + accumulated_permanent
           + side_sign * (temporary_impact + half_spread)

Every result is a **counterfactual estimate**: the observed path contains what
the real market did, not what this hypothetical order would have done to it. A
slice whose nearest observation is older than the declared tolerance is left
unfilled rather than filled at a stale price — the reverse of the portfolio
convention, because a stale mark still describes a position that exists whereas
a stale hypothetical fill asserts liquidity nobody saw.

Simulated fills are scored by the Phase 7 benchmark and shortfall machinery
unchanged, so a counterfactual and a real execution are never measured
differently.

## 17. Model consensus — Phase 9 (implemented)

Multiple pricers produce a set of values. The platform reports
`reference_value` (median), `reference_range`, `model_dispersion` and
`market_deviation` — never a single "true" price, and with no field anywhere in
the payload or the stored row that could hold one. Confidence aggregates model
disagreement, calibration error, bid/ask width, liquidity, extrapolation
distance, data quality, arbitrage violations, quote age and observation count,
and every confidence output can enumerate the specific contributions that made it
what it is (build spec §80).

The aggregation is a weighted geometric mean, so one bad dimension pulls the
score down rather than being averaged away — the same aggregation the quality
engine and the margin model use, so "confidence" means the same thing across the
platform. Model agreement uses the quadratic ratio penalty
`1 / (1 + (d / d_ref)^2)` with `d_ref = 5%`: there is no dispersion at which
agreement becomes exactly worthless, and a linear ramp to zero would score a
5.1% spread and a 50% spread identically.

A model that cannot run contributes a named unavailability, not a missing entry,
and the reduced model count enters the confidence directly. Nothing is defaulted
to make a model runnable.

## 18a. Microstructure — Phase 10 (implemented)

Everything in this section sits behind a **data-availability gate**, and the
gate is the design rather than a precaution around it. Every other engine in
the platform computes what it can and degrades with a warning; microstructure
does not get that latitude, because its failure mode is different. A volatility
surface fitted to thin data is visibly uncertain — wide confidence, few
observations, a warning the reader sees. An order-book imbalance computed from a
one-level feed, or a queue position inferred from a tape with holes in it, looks
exactly like the real thing. There is nothing in the number that says otherwise.

So a dataset is assessed once, at import, and the verdicts are stored with it.
Each capability is `GRANTED` or `REFUSED`; a refusal carries a
closed-vocabulary reason, a sentence saying what was missing, and the evidence
it was decided on. There is no third state, no override parameter and no
endpoint that will answer anyway.

| Capability | What it unlocks | What it needs |
| --- | --- | --- |
| `TOP_OF_BOOK` | spread, mid, microprice, top-of-book imbalance | one snapshot with a price on both sides |
| `DEPTH_ANALYTICS` | multi-level depth, weighted imbalance, book slope, concentration, cost to trade | snapshots with at least 2 levels per side |
| `EVENT_INTENSITY` | arrival rate for a chosen event type | 70 events spanning at least 60 seconds |
| `CANCELLATION_INTENSITY` | a cancellation rate, separate from the trade rate | 20 events *labelled* as cancellations |
| `SELF_EXCITATION` | Hawkes branching ratio and excitation half-life | the above, with under 20% of consecutive events sharing a timestamp |
| `QUEUE_POSITION` | bracketed queue position, wait and fill probability | priced, sided events with a complete monotone sequence, plus snapshots |

The thresholds are stated, not tuned. Each is a point below which the
measurement is not the measurement it claims to be: two levels because a slope
through one point is an identity rather than a fit; a labelled cancellation
before a cancellation rate, because deriving one from a size decrease between
snapshots conflates a cancellation with a trade and those move a queue very
differently; a timestamp resolution finer than the clustering, because a
self-exciting model fitted to a one-second tape estimates the recording clock
and reports it as market behaviour.

### Definitions

More than one convention is in circulation for several of these, so the ones
used here are stated rather than implied.

- **Microprice** `(b*Qa + a*Qb) / (Qb + Qa)`, weighted by the *opposite* side's
  size, so a large resting bid pulls it toward the ask. Reported alongside a
  `microprice_tilt` of `microprice - mid`, whose sign is the direction the
  resting size leans.
- **Imbalance** `(Qb - Qa) / (Qb + Qa)` over the top *n* levels: `+1` for an
  all-bid book, `-1` for an all-ask book. A book with no resting size on either
  side has *no* imbalance, which is not the same as a balanced one, and is
  reported as absent rather than as zero.
- **Weighted imbalance** the same, with geometric level weights
  `w_i = exp(-decay * i)`. `decay = 0` reduces exactly to the plain imbalance
  over the same levels and a large decay reduces to the top of book, which is
  why the decay is a recorded parameter of the measurement rather than a
  constant chosen somewhere.
- **Book slope** the through-the-origin least-squares slope of cumulative depth
  against *relative* distance from the mid, in quantity per unit relative
  distance. Through the origin because a book holds no depth at zero distance
  from the mid by construction, and an intercept would absorb exactly the
  quantity being measured. Reported with the **uncentred** R-squared, which is
  the correct goodness-of-fit for a no-intercept model; the centred form can be
  negative for a perfectly reasonable fit.
- **Depth concentration** the Herfindahl index of the level sizes. Its
  reciprocal is the effective number of levels the depth is spread across,
  which is the form worth reading.
- **Cost to trade** the average price of walking the displayed book for a given
  size, against the mid, signed so a cost is positive whichever way the order
  goes. It is a measurement of one instant, not a prediction and not an impact
  model: it says what the resting size would have cost if it had all been taken
  at once and nothing had moved. **A size larger than the displayed depth is
  refused**, not extrapolated past the last level — the price beyond the book is
  not in the book.

Session summaries report percentiles rather than a standard deviation, because
a session of books is not remotely normal and a handful of instants around an
auction own the variance. Every measure carries its own observation count, and
the snapshots that had no such measurement are counted by the reason they had
none, so an average is never quietly an average over a subset nobody chose.

### Arrival intensity, and the gate that decides which model is reported

Two models are fitted to the same events. The baseline is a homogeneous Poisson
process with rate `N / T`. The alternative is a univariate Hawkes process with
an exponential kernel (Hawkes 1971):

    lambda(t) = mu + sum_{t_i < t} alpha * exp(-beta * (t - t_i))

whose branching ratio `n = alpha / beta` is the expected number of children per
event and whose long-run rate is `mu / (1 - n)`.

**Stationarity is structural.** The optimiser works in `(log mu, logit n, log
beta)` and reconstructs `alpha = n * beta`, so `0 < n < 1` holds at every point
it can evaluate — the same rule as the Phase 2 SVI calibration, where a
constraint that matters belongs in the feasible set rather than in a rejection
afterwards, because a rejected optimum leaves nothing to report. The likelihood
is evaluated with Ogata's recursion, vectorised by blocking so the exponent
stays finite over a window far longer than the decay; the fast path is checked
against the plain recursion in the test suite.

**A self-exciting model always fits better in sample.** It has two more
parameters and order flow does cluster, so reporting the in-sample improvement
would be reporting the parameters. Both models are therefore fitted on a
training window and scored on a held-out one, split **by time** rather than by
event count — splitting by count would put the busiest period on whichever side
had more events and make the comparison a statement about that. The training
events are carried into the held-out window as history, so the Hawkes excitation
is not reset at the split and the comparison is not rigged against it.

**And "wins" is not "scores one nat more".** On genuinely Poisson arrivals the
raw held-out total favours the richer model about as often as not, by hundredths
of a nat — noise with a sign. So the held-out likelihood is decomposed into one
predictive contribution per event, `log lambda(t_k) - Lambda(t_{k-1}, t_k)`, and
the *mean* contribution is tested against zero with a one-sided
Diebold-Mariano statistic using a Newey-West variance (Diebold & Mariano 1995;
Newey & West 1987). The HAC correction is not decoration: consecutive
contributions from a clustered point process are serially correlated, and an
i.i.d. standard error would understate their spread and adopt the richer model
on noise. The Hawkes fit is reported only when that statistic clears its stated
critical value; otherwise the constant rate is what is reported, together with
by how much the alternative failed to earn its parameters. Both models are
stored either way, because "it was tried and did not win here" is the evidence
that the gate ran. A database CHECK makes a row claiming the self-exciting model
without the statistic to back it unstorable.

The fitted model's time-rescaled inter-event times are reported as a
Kolmogorov-Smirnov statistic against `Exp(1)`. That is a diagnostic, reported
rather than acted on: it says how far the model is from describing its own
training data, which the likelihood alone does not.

### Queue outlook

The most gated calculation in the platform, and worth being precise about why.
An exchange knows the order of the queue at a price level. An observer of a
public feed does not: they see the level's total size and the messages that
change it. They cannot see priority, cannot see which specific orders
cancelled, and on most feeds cannot see hidden size at all.

So the output is a **bracket**, in the same spirit as the Phase 6 margin
shortfall region. A cancellation at the level either removes size ahead of the
order or behind it, and the feed does not say which:

- `CANCELS_AHEAD` — every cancellation removes size in front. The queue drains
  fastest; the optimistic end.
- `CANCELS_BEHIND` — only trades move the order forward. The pessimistic end.

Departures are modelled as a Poisson counting process consuming the queue in
units of the mean size of an observed departure. **That size unit is shared by
both ends**, and sharing it is what makes the pair a bracket: the optimistic
departure stream contains the pessimistic one, so its volume rate is larger by
construction, and with a common unit the wait can only be shorter and the fill
probability only higher. Giving each end its own mean size breaks that, and did
— a level where five small cancellations accompanied one large trade produced an
"optimistic" end *less* likely to fill, which is not a bracket but two unrelated
numbers. The invariant is asserted on the object and again by a database CHECK.

Every assumption travels with the estimate: FIFO priority, displayed size only,
Poisson departures at the observed rate, rates measured over the past rather
than forecast, and an order that joins at the back and is never modified. The
response says in its own `interpretation` field that it is not a claim about
where any exchange has actually placed an order. There is no single
`fill_probability` field anywhere in the payload or the stored row — a
probability exists only inside a labelled end of the bracket.

A level at which nothing was observed to leave is **refused**, not scored zero.
A probability of zero reads as "this will not fill" when the truth is "this feed
did not show us anything happening here", and those are different statements.

### Storage

Depth snapshots and event tapes are the two datasets that grow with market
activity rather than user activity, so they live in the object store as parquet
rather than in PostgreSQL — one session of depth for one liquid contract is
millions of rows, and the access pattern is analytical. Prices and quantities
are stored as `decimal128(38, 12)`: a stored observation is a fact, and the
platform does not quietly re-round the ticks a venue published. Levels are list
columns rather than a fixed `bid_px_1 ... bid_px_20` width, because depth varies
snapshot to snapshot and a padded level is indistinguishable from a level quoted
at zero, which is a real thing on some venues.

### Import

Wide CSV — one row per instant with the levels spread across it — is what a
capture tool writes, and the indexed-column layout defeats the platform's
ordinary one-column-per-field mapping, so the level columns are detected. As
everywhere else, detection is a *suggestion* shown in a mandatory preview and
confirmed on commit: a book whose price and size columns were read the wrong way
round parses cleanly and produces analytics that are wrong in every number and
look entirely ordinary. Canonical parquet is accepted directly.

Nothing is repaired. A book whose levels are not ordered best-first is refused
with a reason rather than sorted, because sorting would rescue exactly the
transposed file above and turn a detectable mistake into a plausible book. The
counts conserve, and every rejected row carries its 1-based source row number
and a closed-vocabulary reason — the complete list, in the object store, because
it is unbounded and a column would have to truncate it.

## 18b. Unified order analysis — Phase 11 (implemented)

### The composition, and why it is the method

There is no new estimator in this phase. Every number comes from an engine
documented above, and the entire methodological content is that they all read
one snapshot.

A user asking what happens if they sell a contract is asking five questions, and
the answers are only comparable if they were computed from the same market. The
service builds one `ValuationContext` — covering the book's underlyings *and*
the order's, so a contract on an underlying the portfolio does not hold yet is
still valued inside the same snapshot — values the proposed position in it, and
hands that context to every branch. The `market_state_id` appears in all five
provenance blocks. A difference between "current" and "proposed" is then the
order's contribution, which is the only thing that makes it worth showing.

### Valuing a position that does not exist

The proposed position is constructed as a `Position` with an id derived from the
contract and the size, is never written, and is valued by
`PortfolioValuationService` against the caller's context — the same function,
with the same anchors, that values a stored position. That is deliberate. It
means `build_exposures` treats it identically, and that an order which cannot be
repriced fails for the same reason, in the same closed vocabulary, as a stored
position would.

It carries no average price. An order that has not been placed has no cost, and
an unrealised P&L measured against the limit price would be a number about a
fill nobody got.

### Incremental risk: one panel, one seed, one grid

Both sides are measured by the same estimator, over a factor panel built once
from the **combined** book, with the same seed where the method draws anything
and the same shock grid for margin. Building a panel per side would let an order
on a new underlying change the aligned sample that the current book is measured
on, and the difference would then contain that change as well as the order. The
cost of the choice — that adding such an order can shorten the sample for both
sides — is reported as a warning on the branch rather than hidden.

### The refusal that matters

If the proposed contract has no volatility, no underlying level or no time to
expiry, it never enters the combined book. Both sides are then literally the
same object, every difference is exactly zero, and the analysis reads as an
order that adds no risk. `CombinedBook.order_is_repriceable` records that fact,
and the risk and margin branches return `FAILED` with the exclusion reason
rather than the zeros.

### The forward cost estimate

The Phase 8 simulator walks a schedule against prices the market printed. A
proposed order has no such path, so the reference price is held flat at the
snapshot mid for the whole horizon and the estimate contains only the movement
the order is modelled to cause. Everything else — permanent impact accumulating
into the reference for later slices, temporary impact and half the quoted spread
paid on the slice, the signed shortfall against the arrival reference — is the
same convention, and a unit test asserts the two agree slice for slice on a flat
path so there is one definition with two entry points.

Two properties of the result are easy to misread and are stated in the payload:

- **The two halves are known to different standards.** The spread half is
  measured off an observed two-sided quote; the impact half is model output at a
  coefficient that is almost certainly not this market's. Either can be absent,
  and the total is absent whenever one of them is — a slippage figure quietly
  omitting impact would read as a complete answer.
- **Splitting an order does not obviously reduce its cost in this model.**
  Permanent impact is evaluated per slice and accumulates, so it grows roughly
  as the square root of the slice count while the temporary term falls. Which
  dominates depends on coefficients nobody here has calibrated, and the output
  therefore carries no argument for or against working an order.

### What is measured about a limit price

Only whether it would cross: marketable, passive, or unknown when there is no
two-sided quote to decide against. A passive order's figures are explicitly
conditional on it filling in full, and whether a resting order fills is not
modelled. That needs the queue at the level, which is a gated microstructure
capability most feeds cannot support — and splicing a bracketed queue estimate
into a single cost figure would bury the gate that Phase 10 exists to enforce.

## 18. Known limitations

1. European exercise only; American options on dividend-paying underlyings are
   mispriced by the European formulas and are rejected rather than approximated.
   No binomial engine ships, because every instrument the platform ingests today
   is European and an early-exercise pricer with nothing to price would be
   untested surface.
2. Dividends/borrow enter as a continuous yield `q`. Discrete dividends are a
   later feature.
3. Per-expiry SVI can exhibit calendar arbitrage; it is detected and reported.
   The SSVI global surface cannot, by construction, and both are offered — SVI
   still describes any single smile better than three shared parameters can.
4. Margin models are approximations and are not broker-equivalent.
5. All execution simulations are counterfactual: executing the simulated schedule
   would have changed the market it was simulated against.
6. Anomaly z-scores require historical surface depth; with a short history they
   are reported with low confidence rather than suppressed or overstated.
7. Synthetic data is internally consistent by construction and must never be used
   to validate a claim about real markets — only to validate that code is correct.
8. Heston's characteristic function carries `1 / xi^2` and is ill-conditioned as
   the vol-of-vol vanishes: against Black-Scholes at the same variance the price
   converges as `xi^2` down to about `xi = 1e-4` and then worsens. The
   deterministic-variance limit is Black-Scholes and should be priced with it.
9. Local volatility is undefined past the last fitted expiry, where the variance
   term structure is deliberately flat and `dw/dT` is therefore zero. The PDE
   substitutes the surface's implied volatility there and counts the
   substitutions rather than extrapolating a term structure nobody quoted.
10. Microstructure analytics describe *displayed* liquidity. Hidden and iceberg
    quantity is invisible to a public feed, so a depth, a cost to trade and a
    queue position are all lower bounds on what is really resting there, and no
    feed says by how much.
11. The book is never reconstructed from an event tape. A dataset with events
    and no snapshots gets arrival intensities and nothing that needs a book at
    an instant, because a reconstruction would depend on a starting book, a
    complete tape and a venue-specific set of message semantics — three
    assumptions that would be invisible in the output.
12. The queue model has no adverse-selection term and no model of the order's
    own effect on the level it joins. Both ends of its bracket describe a level
    behaving as it did over the observation window.
13. The intensity models are univariate. Trades exciting cancellations, and
    either exciting the other side, are a multivariate Hawkes process that is
    not implemented; a scope covering several event types is fitted as one
    superposed process and labelled as such rather than being called "order
    flow".
14. A unified order analysis holds the reference price flat over the execution
    horizon, because a proposed order has no path. The estimate therefore
    contains the movement the order is modelled to cause and none of the
    movement a real window would have had anyway, which is the difference
    between a cost estimate and a forecast.
15. Nothing in an order analysis weighs its five branches against each other. A
    single figure combining reference value, estimated slippage and margin
    consumption would be a statement about someone's risk appetite, and the
    platform does not have one; the branches are reported side by side.
