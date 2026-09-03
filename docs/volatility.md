# Volatility — IV engine, smile, surface

Status: **implemented** through Phase 9 — implied volatility, forward
estimation, the raw smile, SVI calibration, the reference surface, the arbitrage
validator, surface characteristics, historical percentiles, the anomaly scanner,
the SSVI global surface, Dupire local volatility and the Breeden-Litzenberger
implied density. PCA on surface changes is gated on real history. Formulas in
`docs/methodology.md` sections 5-8; this document covers structure and data
flow.

---

## 1. Pipeline

```
Option chain snapshot (Phase 0 output, quality-scored)          [implemented]
        |
   Cleaning + selection        -> kept / excluded, every exclusion with a reason
        |
   Time to expiry              -> stated day count + settlement-time policy
        |
   Forward estimation          -> per expiry, three methods, all reported
        |
   IV solve                    -> per quote, with convergence AND conditioning
        |
   Raw smile  (k, w)           -> stored as observations, one side per strike
        |
   Arbitrage diagnostics (RAW) -> before fitting, so a bad fit is never blamed
        |                         on the market
        |
   SVI calibration per expiry  -> constrained SLSQP, deterministic multi-start
        |
   Arbitrage diagnostics (FIT) -> a violation here is a model defect
        |
   Arbitrage diagnostics (RAW) -> raw_market_violations
        |
   SVI calibration per expiry  -> parameters + metrics + optimizer status
        |
   Arbitrage diagnostics (FIT) -> fitted_surface_violations
        |
   Reference surface           -> reference IV / reference value lookups
```

Raw and fitted live in separate tables and separate API fields, always.

## 2. Quote selection for calibration

- Prefer OTM quotes: `K > F` for calls, `K < F` for puts. OTM options carry the
  time value that determines the smile; ITM quotes are dominated by intrinsic
  value and their IVs are numerically fragile.
- Near ATM (`|k| < k_atm`, default 0.02), the rule is deterministic and
  documented: use the side with the tighter relative spread; on an exact tie,
  the call. A deterministic tie-break matters more than which side wins, because
  reproducibility requires that the same input always selects the same quote.
- Minimum quotes per slice (default 5) to attempt a 5-parameter SVI fit. Below
  that the slice is reported as `INSUFFICIENT_OBSERVATIONS`, not fitted with a
  degenerate parameter set.

## 3. Weighting

Calibration weights combine spread and liquidity:

```
weight = spread_score * liquidity_score
```

both already computed by the Phase 0 quality engine. This is a concrete payoff
from scoring quality at ingestion: the surface fitter does not need its own,
inevitably divergent, notion of which quotes are good. Phase 1 computes and
stores the weight on every implied-volatility row so the Phase 2 fit has it
without recomputation, and the same weight already drives the put-call-parity
regression.

## 2a. Which quotes carry the smile

Two filters, in order.

**Informative.** A quote's implied volatility is only as well determined as its
price. The solver reports `uncertainty` — half a spread (or one tick for a
locked market) divided by vega — and a quote above `MAX_SMILE_UNCERTAINTY`
(1e-2, i.e. one volatility point) is dropped as `ILL_CONDITIONED`.

This is not a theoretical nicety. A deep out-of-the-money weekly is worth less
than a tick, so a venue quotes it locked at the floor; inverting that price is
numerically flawless and yields, say, 50% against a true 12%. On a wide chain a
dozen such quotes moved the fitted slice by **104 volatility points**. With the
filter the same chain fits to under one. The fitted log-moneyness range
therefore contracts on its own to where the market carries information — narrow
for a weekly, wide for a quarterly — which is the correct behaviour and the
reason accuracy is judged over `[k_min, k_max]` rather than over the listed
strikes.

**Out-of-the-money preferred.** Of what remains, one quote per strike carries
the smile, chosen as described above.

## 3a. What Phase 1 and 2 store

`yield_curves` (content-addressed) -> `chain_analyses` (one run: curve, day
count, settlement time, provenance) -> `forward_estimates` (**every** method
attempted, not only the winner) and `option_implied_vols` (per quote:
`market_iv`, the bid and ask IVs, solver, iterations, vega, uncertainty,
`log_moneyness`, `total_variance`, weight, and whether it was selected for the
smile — with a reason when it was not).

Storing the rejected forward estimates is what makes a later "why was this
slice's log-moneyness wrong?" answerable at all.

Phase 2 adds `volatility_surfaces` (content-addressed header) ->
`surface_slices` (per expiry: forward, discount factor, fitted `k` range, and
every calibration metric) -> `surface_parameters` (the five SVI numbers, alone).

The parameters live in their own row deliberately: a reference implied
volatility must be reproducible from *those five numbers plus the forward and
the maturity* and nothing else, and separating them makes that contract legible
in the schema rather than only in prose.

`arbitrage_reports` and `arbitrage_violations` store both scopes, keyed
`UNIQUE(surface_id, scope)` — one report per scope per surface, so a refitted
analysis keeps its earlier reports and you can see whether the refit fixed a
violation or merely moved it.

## 4. Storage

`volatility_surfaces` (header: underlying, as_of, model, model_version, forwards,
curve id, overall metrics, provenance) -> `surface_slices` (per expiry: n_obs,
rmse, weighted_rmse, optimizer, status) -> `surface_parameters` (per slice:
`a, b, rho, m, sigma`).

A stored surface must reproduce its reference IVs exactly from persisted
parameters — no re-fitting on read. A regression test asserts this.

## 5. Historical surface analytics — Phase 3 (implemented)

Every calibration records the surface's shape at **standard tenors** — 7, 30,
60, 90, 180 and 365 days. Fitted expiries roll, so a time series of "the October
slice" runs out in October; a time series of "the 30-day level" is what makes a
percentile mean anything.

Level, skew and curvature come from the fitted parameters analytically
(`sigma = sqrt(w/tau)`, `sigma' = w'/(2 tau sigma)`,
`sigma'' = (w''/tau - 2 sigma'^2)/(2 sigma)`), so a stored characteristic
reproduces exactly from the surface it came from. A tenor landing on a fitted
expiry is exact; between two it interpolates total variance; outside the range
it extrapolates at flat forward variance and is flagged.

Percentiles and z-scores are reported for each characteristic, **always with the
observation count**, and marked unreliable below a stated minimum. A percentile
from eight surfaces and one from six hundred are different kinds of statement,
and presenting them identically is the dishonest part.

PCA on surface changes is **deferred with a data gate**. Loadings must be
computed empirically and only labelled level/skew/curvature when they support
it; running it on our own synthetic surfaces would describe the generator rather
than a market.

## 6. Anomalies — Phase 3 (implemented)

A deviation between an observed implied volatility and the fitted reference is
scored, never acted on. The output is a measurement, the scale of what could
explain it, and a confidence — with no direction, rating or target anywhere in
the schema, asserted by a test over the whole serialised response.

**The threshold is not on the volatility difference.** A fixed vol-point
threshold flags every illiquid wing quote in the market and nothing else. The
difference is standardised by

```
explained_scale = sqrt(half bid/ask IV envelope^2
                       + slice calibration RMSE^2
                       + IV numerical uncertainty^2)
```

Each term is measured elsewhere in the platform for its own reasons: the
envelope by the Phase 1 solver at both sides of the market, the RMSE by the
Phase 2 calibration, the resolution by the price-resolution conditioning. A
deviation is interesting exactly when the things that could account for it do
not.

**A reference inside the quoted range is not an anomaly.** If the market's own
two-sided quote spans the model value, the width of the market accounts for the
whole difference.

Confidence is a weighted geometric mean — the same aggregation the quality
engine uses, so "confidence" means the same thing across the platform — over
data quality, liquidity, calibration error, measurement resolution, slice
breadth and extrapolation penalties. Every factor produces a line of explanation
naming its measured value, which is what makes "why is confidence 0.42?"
answerable from the response itself.




## 7. The global surface — Phase 9 (implemented)

### 7.1 Why a second surface rather than a replacement

Per-expiry SVI fits five parameters to each smile independently. It reproduces
any single smile better than three shared parameters can, and it has no
structural reason for two neighbouring expiries to be consistent with each
other, so calendar arbitrage is something it *detects*.

SSVI (Gatheral & Jacquier 2014) fits

```
w(k, theta) = (theta / 2) * { 1 + rho phi(theta) k
                              + sqrt[ (phi(theta) k + rho)^2 + (1 - rho^2) ] }
```

with three global parameters and one at-the-money total variance `theta` per
expiry. Requiring `theta` to be non-decreasing in maturity **is** the
no-calendar-arbitrage condition, so an admissible SSVI surface cannot contain
the violation SVI could only name.

Both are stored, in separate tables, and both are offered. Overloading one table
with a `model` column would have hidden the fact that the two carry different
guarantees. `ck_converged_global_surface_is_arbitrage_free` makes the SSVI
guarantee a database rule: a row marked `CONVERGED` while carrying a decreasing
term structure or a negative implied density cannot be stored.

### 7.2 Calibration

One SLSQP fit over `[rho, eta, gamma, theta_1 .. theta_n]`, deterministic
multi-start, with three families of constraint in the feasible set rather than
checked afterwards: monotone `theta`, the closed-form butterfly bounds of
Theorem 4.2, and Durrleman's `g(k) >= 0` on a grid per slice.

The closed-form bounds are **sufficient, not necessary**. A market with a very
steep short-dated smile can be admissible — Durrleman non-negative everywhere —
and still fail them. So both are evaluated and both are reported, and
`enforce_butterfly_bounds` can be turned off to keep only the condition that
actually decides the sign of the density. Trusting a sufficient condition alone
would mean silently rejecting admissible surfaces and, worse, believing a proof
rather than the surface in front of us.

An inverted observed term structure — ATM variance that falls with maturity — is
calendar arbitrage in the raw market. The fit imposes monotonicity, so the
surface will not reproduce the inversion; a warning names it and the raw
arbitrage report on the same analysis names the quotes responsible.

### 7.3 The two ends of the term structure differ, on purpose

Between the fitted expiries `theta` is interpolated with a monotone
piecewise-cubic (PCHIP), so monotonicity holds everywhere and not only at the
knots.

**Before the first expiry** total variance is proportional to maturity, running
down to `theta(0) = 0`. That is not extrapolation of a fitted slope: a
zero-length period has zero variance, so the origin is a boundary condition, and
a straight line to it is the flat-implied-volatility reading of the front slice.
Clamping flat instead makes `dtheta/dT` zero across the whole front, and Dupire
divides into that — the local volatility over the first month came back as pure
fallback, and the PDE mispriced the surface it was derived from by 1.6%.

**After the last expiry** it is flat, and that *is* a refusal to extrapolate:
continuing a fitted slope past the last observed expiry invents a term
structure, whereas a flat one cannot introduce calendar arbitrage. Local
volatility past the end is consequently undefined and reported as such.

Both ends are flagged `SSVI_EXTRAPOLATED_MATURITY` regardless.

## 8. Local volatility — Phase 9 (implemented)

Dupire in Gatheral's total-variance form, evaluated on the **fitted** surface
with analytic first and second derivatives:

```
sigma_loc^2(k, T) = (dw/dT) / [ 1 - (k/w) dw/dk
                                + (1/4)(-1/4 - 1/w + k^2/w^2)(dw/dk)^2
                                + (1/2) d2w/dk2 ]
```

Analytic rather than bumped, because a finite-difference error in the second
derivative turns into a local volatility that is wrong by an amount nothing
downstream can detect.

**The grid keeps its holes.** Where the denominator approaches zero the point
has no value and carries the flag that says why; the same is true where
`dw/dT` is negative or the magnitude is implausible. A grid that interpolated
across those regions would look complete while being fiction exactly where it
matters. `ck_local_vol_grid_conserves_points` enforces
`total = valid + flagged`, the same conservation rule ingestion lives under.

The PDE cannot have a hole in its coefficient, so `SurfaceLocalVol` substitutes
the surface's own implied volatility there and **counts the substitutions**, and
the count travels with the price so a value computed mostly from fallbacks is
visible as one.

One subtlety worth stating because getting it wrong is invisible: the surface is
parameterised in `k = log(K / F_T)`, so evaluating it at calendar time `t`
requires the forward **to t**, not the forward to the option's maturity. Holding
one forward fixed across the time grid shifts every lookup along the smile by
the carry — a systematic error in the wings that no amount of grid refinement
removes. The forward curve used is `spot * exp(carry * t)` with `carry` fitted
by least squares through the origin on the observed forwards, and it is stated
as the deterministic-carry approximation it is.

The validation for all of this is a round trip: a local-volatility surface
derived from an implied surface must reprice that implied surface. It does, to
under 0.2% across strikes, maturities and carries. It was 1.6-2.3% until the
forward coordinate and the front of the term structure were fixed.

## 9. Risk-neutral density — Phase 9 (implemented)

Breeden-Litzenberger: the density is the second strike-derivative of the
discounted call surface, taken on the **fit**, never on raw quotes — second
differences of noise are exactly the failure the total-variance machinery exists
to avoid.

Every density is stored with what is wrong with it: negative regions, mass away
from one, a mean away from the forward, a strike range too narrow to contain the
distribution. **Quantiles are stored only for an admissible density**, meaning
non-negative *and* normalised, and
`ck_density_quantiles_require_admissibility` makes that a database rule. Both
conditions matter: a negative region means the surface implies a distribution
that cannot exist, and a mass away from one means the strike window does not
contain the distribution, so a quantile — which normalises by whatever mass it
found — would be a quantile of the *window*, and would look perfectly reasonable
while being wrong.

The payload states in its own `interpretation` field that this is the
distribution the option market is pricing under and not a forecast of where the
underlying will go. Those are different objects, and the difference is the whole
content of the risk premium.
