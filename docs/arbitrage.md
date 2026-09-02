# Arbitrage Diagnostics

Status: **implemented** (Phase 2). Conditions in `docs/methodology.md` section 8.

---

## 1. Purpose

Two distinct questions, deliberately kept apart:

1. **Is the market data internally consistent?** Violations here are a data
   quality signal — bad quotes, stale legs, mismatched timestamps — and only
   very rarely a real, executable opportunity.
2. **Is our fitted surface admissible?** Violations here are a *model* defect and
   must never be blamed on the market.

`ArbitrageValidator` runs the same tests over both and returns them in separate
collections: `raw_market_violations` and `fitted_surface_violations`.

## 2. Tests

| Test | Applies to | Units of the reported magnitude |
| --- | --- | --- |
| Price bounds (call and put, upper and lower) | raw | currency |
| Put-call parity | raw | currency |
| Vertical monotonicity and the `DF·dK` slope bound | raw | currency |
| Butterfly convexity on adjacent strike triples | raw | currency |
| Durrleman `g(k) >= 0` | fitted | dimensionless (density) |
| Lee wing bound `b(1+|rho|) <= 2` | fitted | slope |
| Calendar: total variance non-decreasing in `T` at fixed `k` | raw, fitted | total variance |

The butterfly test uses the general unequally-spaced form,

```
[(K3-K2) C1 - (K3-K1) C2 + (K2-K1) C3] / (K3-K1) >= 0
```

which reduces to `(C1 - 2 C2 + C3) / 2` on an even grid. The calendar test
compares at fixed **log-moneyness**, not fixed strike: comparing at fixed strike
would manufacture violations whenever the forward moves between expiries, and
only over the range both expiries actually observed, because extrapolating one
slice into the other's wings would manufacture them out of a missing quote.

On the fitted surface, butterfly freedom is tested by Durrleman's `g` rather
than a discrete second difference. `g` is proportional to the implied density,
so a negative region is literally a negative probability — exact, independent of
the strike grid, and interpretable.

## 3. Report

```
ArbitrageReport
  scope: RAW_MARKET | FITTED_SURFACE
  violations[]: {type, expiry, strike, option_type, magnitude, tolerance,
                 severity, detail, affected_instruments[]}
  summary: counts by type and by severity
  severity: worst severity present
  checks_run[]: which conditions actually ran
  observations: how much data they ran over
```

Both scopes are persisted, keyed `UNIQUE(surface_id, scope)`. An analysis can be
recalibrated, and the earlier reports stay, so it is visible whether a refit
fixed a violation or moved it.

**Magnitude is mandatory.** A boolean "butterfly violated" is nearly useless: on
a discrete strike grid with wide spreads, tiny convexity violations are
ubiquitous and meaningless, while a large one on liquid strikes is a genuine data
problem. Severity is derived from magnitude relative to the local bid/ask width,
so a violation smaller than the spread is `INFO` rather than `ERROR` — it is not
exploitable and it is not evidence of bad data. A parity residual inside the
cost of crossing both legs is not reported at all; reporting every strike would
bury the real ones.

Every violation also carries the `tolerance` it was judged against, in the same
units, so a reader can see *why* it was graded as it was rather than trusting
the grade.

## 3a. Why a violation almost never means free money

A no-arbitrage condition breached in observed quotes is, overwhelmingly, a
statement about the data: the two legs were not sampled at the same instant, one
side is stale, a multiplier is wrong, or the underlying reference is mismatched.
The platform therefore says so in the warning text itself, and the UI repeats it
next to the table. Presenting these as opportunities would be the single most
irresponsible output this platform could produce, and it is a short step from a
correct calculation to that presentation.

## 3b. Constraints versus checks

On the **fitted** side the conditions are not only checked, they are imposed.
Non-negative minimum variance, Lee's wing bound and Durrleman's `g(k) >= 0` on a
grid are all in the calibrator's feasible set.

This was not a theoretical precaution. Measured: a single mispriced quote —
one call bumped by 120 points in a 24,000 index chain — was enough to bend an
unconstrained least-squares fit into a butterfly violation with `min g = -0.19`.
With the condition in the feasible set, the fit absorbs the bad quote as error
instead, the surface stays admissible, and the raw-market report still names the
quote. That is exactly the division of labour the two scopes exist for.

## 4. Language

The report never says "arbitrage opportunity". It says a **violation of a
no-arbitrage condition in the observed data**, which is almost always a data
artefact: stale legs, non-simultaneous quotes, a wrong multiplier, or a mismatched
underlying reference. Presenting these as opportunities would be the single most
irresponsible output this platform could produce.
