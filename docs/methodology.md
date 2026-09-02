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

`q` = signed quantity, `M` = contract multiplier. A raw `dV/dsigma` is never
displayed: an unlabelled vega is a bug report waiting to happen.

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

## 9. Risk-neutral density — _(Phase 9)_

Breeden-Litzenberger: `f(K) = e^{rT} d2C/dK2`, evaluated on the smooth fitted
surface, never on raw quotes. Negative regions are flagged as evidence of
residual arbitrage or over-fitting. Presented as a diagnostic and an
interpretability aid — it is a risk-neutral density and is never described as a
forecast of what the market will do.

## 10. Local volatility — _(Phase 9)_

Dupire in total-variance form (Gatheral), which is numerically far better behaved
than the raw price-derivative form:

```
sigma_loc^2(k, T) = (dw/dT) / [ 1 - (k/w)(dw/dk) + 0.25(-0.25 - 1/w + k^2/w^2)(dw/dk)^2 + 0.5 d2w/dk2 ]
```

Rules: derivatives are taken on the fitted arbitrage-aware surface only;
denominators near zero produce `INVALID`, not a clipped value; extrapolated
regions are flagged; each `LocalVolPoint` carries `confidence` and `flags`.

## 11. Local-vol PDE — _(Phase 9)_

Crank-Nicolson on `x = ln S` with Rannacher start-up (two fully-implicit
half-steps) to damp the oscillations Crank-Nicolson produces against a
non-smooth payoff. Non-uniform grid concentrated near the strike; Dirichlet
boundaries from asymptotic payoff behaviour.

Mandatory validation: with `sigma_loc(S, t) = sigma` constant, the PDE price must
converge to the Black-Scholes price, and the test asserts the empirical order of
convergence, not merely that the final error is small.

## 12. VaR and Expected Shortfall — _(Phase 5)_

**Historical simulation.** Aligned factor-return series over a stated lookback;
for nonlinear books, **full repricing** under each historical scenario, not a
Greek extrapolation. The response reports lookback, horizon, confidence,
observation count and the alignment/missing-data policy applied.

**Parametric.** Covariance-based, valid only for approximately linear exposures.
The response states the assumptions inline and it is never returned as the sole
measure for an option book.

**Monte Carlo.** Explicit factor model, seeded and reproducible, full repricing,
convergence reported with the standard error. Fat tails, GARCH filtering and
copulas are later options, each requiring its own validation before it ships.

**Expected shortfall.** `ES_alpha = E[L | L > VaR_alpha]`, always reported
alongside VaR, with the distinction spelled out in the response: VaR is a
threshold loss, ES is the average loss given that the threshold is exceeded.

## 13. Stress testing — _(Phase 5)_

Shocks are applied to a `MarketState`, producing a new immutable shocked state;
the portfolio is then **fully revalued**. Shock types: `ABSOLUTE`, `PERCENTAGE`,
`VOL_POINTS`, `BASIS_POINTS`. Volatility shocks act on the surface, so a shocked
option is repriced under a shocked surface rather than bumped by vega.

Greek-based approximation is available for interactive previews and is **labelled
as an approximation** in the response. For large shocks the two genuinely differ,
and a test asserts that they do — if they did not, the full revaluation would not
be earning its cost.

## 14. Margin — _(Phase 6)_

`SimpleRiskMarginModel`: margin is the worst loss over a declared shock grid
(underlying % moves x volatility point moves), plus a short-option minimum, plus
a concentration add-on. Every one of those components is a stated assumption, not
an exchange rule.

Utilisation `= margin_required / eligible_capital` when capital is known.

Liquidation vulnerability is a **shock-grid scan**: for each underlying shock,
compute equity, margin requirement and buffer; report the region where
`buffer <= 0` as an *estimated margin-shortfall region* with its assumptions and
confidence. No single guaranteed liquidation price is ever produced, because
producing one would require broker rules we do not have.

## 15. Transaction cost analysis — _(Phase 7)_

Implementation shortfall for a buy: `IS = (P_exec - P_arrival) * Q`; sign
reversed for a sell, so a positive IS is always a cost. Reported in currency,
basis points and percent.

Benchmarks each declare window, market-data source and method. Arrival price is
the prevailing mid at the submit timestamp; when no submit timestamp exists, the
first-fill price is used as a proxy and flagged `ARRIVAL_PROXY_USED`, because
silently substituting one benchmark for another is how TCA becomes fiction.

Cost decomposition is **model-based** and labelled as such:

```
spread cost = 0.5 * quoted spread at fill
impact      = MarketImpactModel estimate
fees        = observed
timing      = IS - spread - impact - fees        (residual, by definition)
opportunity = unfilled quantity * (benchmark - decision price)
```

Spread, impact and timing are not separately observable. The residual definition
of timing is stated in the response so no one mistakes the decomposition for a
measurement.

## 16. Market impact — _(Phase 8)_

Square-root baseline: `impact = eta * sigma * sqrt(Q / ADV)`, with `eta`, the
volatility estimator and the ADV window all declared parameters recorded in the
model registry. Linear baseline for comparison. ML models only after enough
labelled executions exist, evaluated out-of-time, and only shipped if they beat
the square-root baseline on held-out data bucketed by order size.

## 17. Model consensus — _(Phase 9)_

Multiple pricers produce a set of values. The platform reports
`reference_value` (median), `reference_range`, `model_dispersion` and
`market_deviation` — never a single "true" price. Confidence aggregates model
disagreement, calibration error, bid/ask width, liquidity, extrapolation
distance, data quality, arbitrage violations, quote age and observation count,
and every confidence output can enumerate the specific contributions that made it
what it is (build spec §80).

## 18. Known limitations

1. European exercise only until an American engine lands; American options on
   dividend-paying underlyings are mispriced by the European formulas and are
   rejected rather than approximated.
2. Dividends/borrow enter as a continuous yield `q`. Discrete dividends are a
   later feature.
3. Per-expiry SVI can exhibit calendar arbitrage; detected, reported, not
   prevented until SSVI.
4. Margin models are approximations and are not broker-equivalent.
5. All execution simulations are counterfactual: executing the simulated schedule
   would have changed the market it was simulated against.
6. Anomaly z-scores require historical surface depth; with a short history they
   are reported with low confidence rather than suppressed or overstated.
7. Synthetic data is internally consistent by construction and must never be used
   to validate a claim about real markets — only to validate that code is correct.
