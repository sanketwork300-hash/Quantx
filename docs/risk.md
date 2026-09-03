# Risk Domain

Status: **implemented** (Phase 5). Formulas in `docs/methodology.md` §§12-13.

---

## 1. Snapshot

```
risk_snapshots
  portfolio_id, valuation_id -> portfolio_valuations
  as_of_timestamp, market_state_id, base_currency
  positions, excluded_positions, excluded[]
  base_value      <- model value at the unshocked anchors
  reported_value  <- what the portfolio is marked at
  delta, gamma, vega_per_vol_point, theta_per_day, rho_per_bp
  provenance
```

Every risk run starts by valuing the portfolio through the same code path the
valuation endpoint uses, stores that valuation, and points the snapshot at it.
That is what makes a VaR number auditable: the chain from it back to the
individual quotes is a sequence of foreign keys, not an assertion.

`margin_estimate` and `margin_buffer` are absent rather than nullable. Margin
arrives in Phase 6, and a column that is always null is a schema claiming a
capability the code does not have.

### The base value is a model value, and it is labelled as one

Under a shock nobody quoted anything, so every stressed price must come from a
model — which makes the *base* price a modelling choice too. If the base were
the observed market price while the stressed price came from a model, the
difference would mix the shock with the model's disagreement with the market,
and a null scenario would show a P&L.

So both sides are priced by the same function from the same anchors, and the
anchor volatility is the one implied by each position's own observed price
wherever there was one. A null scenario then reprices to the base value exactly,
and a test asserts it. The snapshot stores `base_value` and `reported_value`
separately; when they differ — because a position was marked to model with a
reference volatility that does not reprice its own mark — the gap is reported as
a warning rather than absorbed into the P&L.

## 2. VaR

Three methods, all available, each labelled with its assumptions:

| Method | Suitable for | Key parameters |
| --- | --- | --- |
| Historical simulation | anything, incl. nonlinear books | lookback, horizon, confidence, alignment policy |
| Parametric | approximately linear exposures only | covariance estimator, window |
| Monte Carlo | nonlinear books, custom factor models | factor model, paths, **seed** |

For nonlinear portfolios, historical and Monte Carlo VaR **fully reprice** under
each scenario. Scaling today's Greeks across a large shock is a linearisation
that fails precisely where risk numbers matter, and a test asserts the two
approaches diverge materially for a large shock on an option book.

### Where the factor history comes from

The lookback is the history the platform actually holds:

| Factor | Source | Accumulates |
| --- | --- | --- |
| Underlying return | `option_chain_snapshots.underlying_price` | one point per ingested chain |
| At-the-money volatility change | `surface_characteristics.atm_volatility` at the 30-day tenor | one point per calibrated surface |

Both come from work the platform already did for other reasons. Thirty days is
the market's own convention for a headline volatility number and is a
convention, not a derivation. When no volatility history exists the factor is
dropped, volatility is held constant in every scenario, and the response says
so — for an option book that understates risk, and saying so is the point.

Two chains ingested on the same day are two views of one day, not two days of
returns, so timestamps are collapsed to dates keeping the last observation of
each.

### Missing data

Dates absent from any series are **dropped, not forward-filled**, and the count
that was dropped is reported. Carrying a price across a gap manufactures a zero
return, which is not a day on which the market did not move — it is a day the
platform did not see. Zero returns pull a volatility estimate down and a VaR
with it.

Below ten aligned observations no number is produced at all: the response is
`FAILED` with `RISK_INSUFFICIENT_HISTORY` and the observation count, because a
sample that thin is not a thin estimate, it is an absent one.

### The horizon

Multi-day horizons take overlapping windows out of the actual return series
rather than multiplying a one-day number by the square root of time.
Square-root scaling assumes independent increments with no drift, which is
exactly what a stressed market stops having. Overlapping windows reuse
observations, so the response warns that the effective sample is smaller than
the count.

## 3. Expected shortfall

Always reported with VaR, with the distinction stated in the response payload so
it can be surfaced verbatim in the UI: VaR is a threshold loss, ES is the average
loss conditional on exceeding it.

## 4. Scenarios and stress

```
Scenario: id, name, description, shocks[]
Shock:    risk_factor, shock_type (ABSOLUTE|PERCENTAGE|VOL_POINTS|BASIS_POINTS), shock_value
```

Applying a scenario shocks each position's own anchors and reprices it; a
volatility shock moves the implied volatility the option is priced at, so an
option is repriced under the shocked volatility rather than bumped by vega. A
volatility driven below `1e-4` is clipped and the clipping is counted on the
result: a scenario that drives implied volatility to zero has left the region
where the pricing model means anything.

Shocks of the same kind **compose additively in their own units** rather than
the last one winning, so "market -5%" plus "NIFTY -3%" is -8% on NIFTY and -5%
elsewhere. A shock type that makes no sense for its factor — a percentage move
in a rate, relative to what? — is refused rather than guessed at.

Result: `pnl` (the full repricing), `shocked_value`, `greek_approximation`
(labelled, never the answer), `shocks` as resolved, per-position revaluations,
and the contribution breakdowns.

### The Greek approximation is shipped, and labelled

    full:  V(S(1+s), sigma + dv, r + dr, tau - dt) - V(S, sigma, r, tau)
    greek: delta*dS + 0.5*gamma*dS^2 + vega*dv + rho*dr + theta*dt

Both are returned. The approximation is genuinely useful — fast enough to move a
slider against — and genuinely wrong for large moves. On the test book it is
within 1% of the full repricing for a 0.1% move and more than 5% out for a 10%
move, and it matches exactly on a purely linear book. Offering it, labelling it,
and showing the two side by side is more useful than an argument about which one
is right.

### Where a scenario's numbers come from

Every scenario carries a `source`, and it is the most important field on it:

| Source | Meaning |
| --- | --- |
| `HYPOTHETICAL` | A shipped template. Round numbers chosen for illustration, saying so in its own description. |
| `USER_DEFINED` | Shocks the user entered. Recorded as theirs, whatever they represent. |
| `DERIVED_FROM_HISTORY` | Computed from a series the platform holds, carrying that series, its date range and the date of the move. |

**No shipped template is named after a real market event, and a test asserts
it.** Calling a template "COVID crash" and putting a round -35% in it would
assert a fact about March 2020 that nobody here measured, and a user would
reasonably read the number as history rather than illustration. The templates
are named for what they are: `Underlying -10%`, `Volatility +5 points`,
`Sell-off with volatility spike`.

A genuinely historical scenario is produced by `POST /scenarios/derive`, which
finds the worst move (or a chosen quantile) in the underlying's own recorded
series and records where it came from. There is deliberately no way to *declare*
a scenario historical: the model refuses a `DERIVED_FROM_HISTORY` scenario with
no derivation, and a database CHECK refuses the row.

When a volatility series covers the same dates, the volatility move **observed
on that same date** is added as a second shock. It is not modelled, assumed, or
scaled from the price move: it is what that series did on that day, or the shock
is simply omitted.

## 5. Risk contribution

Loss decomposition by instrument, underlying, expiry and asset class, so a stress
number is actionable:

```
Stress loss = 380,000
  NIFTY short puts    54%
  BANKNIFTY futures   24%
  RELIANCE calls      12%
  Other               10%
```

Each group's contribution is its own **fully repriced** P&L — not its share of a
delta-allocated total. That distinction is the one that matters: a short put's
contribution to a 10% sell-off is what the option is actually worth afterwards,
which is not what its delta predicted.

The decomposition is exact, and the reason is worth stating because it will stop
being true. Portfolio value is a sum over positions, and each position is
repriced from its own anchors, so the group P&Ls add up to the total with
nothing left over. The equivalent-but-costlier construction — rerun the whole
scenario with one group held flat and difference the totals — gives the same
numbers, and a test asserts that it does. A portfolio-level quantity that is not
a sum over positions, such as the netted margin arriving in Phase 6, will not
decompose this cleanly; the `residual` reported on every breakdown is the check
that will notice when that day comes.

A position that carries no key for a dimension — an index leg has no expiry — is
counted as `ungrouped` rather than filed under a fabricated key, and its P&L is
reported separately so the table's omission is visible.
