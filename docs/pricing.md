# Pricing — engine design

Status: **implemented**. Black-76 (price, vega, bounds) and
Black-Scholes-Merton (price and all first- and second-order Greeks) landed in
Phase 1; the `PricingModel` abstraction, the local-volatility PDE, Heston, the
Monte Carlo engine, the higher-order Greeks and the model consensus landed in
Phase 9. Formulas and conventions are in `docs/methodology.md`; this document
covers engine design.

---

## 1. Interface

As built (`domains/derivatives/consensus.py`):

```python
class PricingModel(ABC):
    kind: PricingModelKind
    version: str

    def price(self, inputs: ConsensusInputs) -> ModelValue: ...
```

`ModelValue` carries `value`, `model`, `model_version`, `method`, `inputs_used`,
`diagnostics`, `warnings` and `unavailable_reason` — never a bare float. The
bare float is what makes model risk invisible.

Two deviations from the shape originally planned here, both deliberate:

* **`price` never raises for a modelling reason.** An inability is a result: a
  model that cannot run returns `value=None` with a named
  `unavailable_reason`, and the consensus continues over the rest. An exception
  would have made a missing calibration indistinguishable from a bug.
* **`ConsensusInputs` rather than `(Contract, MarketState)`.** All four models
  receive the *same* spot, rate, dividend, maturity and reference volatility,
  which is the point of the comparison — given their own inputs the dispersion
  would measure the inputs rather than the models. Resolving a `MarketState`
  per model would have made that impossible to guarantee.

Greeks are not on this interface. First-order Greeks come from the Phase 1
engine and the higher-order ones from `quant/pricing/higher_order.py`, both at
the surface's reference volatility, so every Greek in the platform comes from
one place under one convention.

## 2. Planned implementations

| Pricer | Phase | Notes |
| --- | --- | --- |
| `bsm_price` / `bsm_greeks` | 1 `[x]` | spot-based with continuous carry; the canonical **Greeks** engine |
| `black76_price` / `black76_vega` | 1 `[x]` | forward-based; the canonical **pricing** parameterization |
| `LocalVolPDEPricer` | 9 `[x]` | Crank-Nicolson + Rannacher start-up on `x = ln S`, second order in price, delta and gamma |
| `HestonPricer` | 9 `[x]` | characteristic function, little-trap branch, cross-checked against QuantLib to 1.5e-11 |
| `MonteCarloPricer` | 9 `[x]` | seeded, antithetic + a control variate on the discounted terminal price |
| `BinomialPricer` | later | American exercise (CRR with Richardson extrapolation). Not built: every instrument the platform ingests today is European, and an early-exercise pricer with nothing to price would be untested surface |

## 3. Why Black-76 is the default

Listed option markets quote against a forward that already embeds carry,
dividends and borrow. Using Black-76 on an estimated forward keeps those effects
in **one** explicitly estimated quantity with its own confidence
(`ForwardEstimator`), instead of scattering a guessed dividend yield through
every price and Greek. It also makes the smile coordinate `k = ln(K/F)` fall out
directly.

## 4. Greeks

Analytic wherever a closed form exists. Where it does not, central finite
differences with documented bump sizes; the method used is recorded per Greek so
a consumer knows whether a number is exact or bumped.

Greeks are taken in the **spot** parameterization even though pricing is done on
the forward. A forward-space theta is ambiguous — it depends on whether the
forward and the discount factor are held fixed as time passes — and an ambiguous
theta is worse than none. Black-76 and Black-Scholes-Merton agreeing on price
for the same inputs is asserted as a test, which is what makes the split safe.

Units are always attached (`docs/methodology.md` section 2.3): vega per +1 vol
point, theta per calendar day, rho per basis point. The **raw partials are kept
alongside** the scaled values (`vega_raw_per_unit_vol`, `theta_raw_per_year`),
so nothing downstream has to back out a scaling factor, and `/derivatives/greeks`
returns a `units` block naming each convention in plain language.

Degenerate inputs (`tau <= 0`, `sigma = 0`) collapse to their analytic limits —
delta becomes an indicator, every second-order Greek becomes zero — rather than
producing `nan`.

## 5. Heston: implemented, not wrapped

`docs/references.md` records the decision. The table above originally said
"QuantLib-wrapped"; the implementation deviates from that and prices Heston
directly from the characteristic function, with QuantLib as the *test oracle*.
That is the standing rule for core numerics — two independent implementations
that agree is a stronger guarantee than one implementation called twice — and
QuantLib is a test dependency rather than a runtime one, so wrapping it would
also have put it on the critical path of every deployment.

Two details carry the correctness:

* **The branch.** The naive form of the characteristic function crosses the
  principal branch cut of the complex logarithm at long maturities and produces
  prices wrong by percent, silently. The Albrecher et al. (2007) "little trap"
  form — the minus root with `c = 1/g` — does not. The ten-year case in
  `tests/quant_validation/test_heston.py` is the one that would catch a
  regression to the naive form.
* **The truncation is adaptive.** The integrand decays like
  `exp(-u^2 v tau / 2)`, so how far the integral must be carried depends on the
  maturity. A fixed limit is an unstated assumption that the tail has already
  died, and for a one-week option it has not: truncating at 200 is wrong in the
  seventh significant figure, which is small enough to read as rounding and
  large enough to fail a cross-check. `integration_limit` scales the cut with
  `1 / sqrt(v tau)`, floored at the fixed limit and rounded to a quantum so the
  quadrature cache still works.

Calibration is weighted least squares on **vega-weighted price error**, which is
volatility error to first order at a fraction of the cost of inverting every
model price on every iteration; the reported RMSE is then recomputed in exact
volatility points, so the approximation never reaches the reported number.
Feller (`2 kappa theta > xi^2`) is a diagnostic by default and a constraint on
request: real index surfaces routinely calibrate to parameter sets that violate
it, and refusing those fits would mean refusing to describe the market. When it
*is* enforced the fit lands on the constraint boundary and costs about half a
volatility point, which is the trade-off made visible rather than argued about.

`kappa` and `theta` are not separately identified by a short term structure —
only their product is — so a fit over fewer than three expiries carries
`HESTON_MEAN_REVERSION_NOT_IDENTIFIED`, and the consensus repeats it as a caveat
on the price it produced. The fit is still used: the surface is reproduced well,
and the surface is what the price depends on. What is refused is reading the
parameters individually as though they were measurements.

There is one documented limit. The characteristic function carries `1 / xi^2`,
so a vanishing vol-of-vol is ill-conditioned: measured against Black-Scholes at
the same variance, the price converges as `xi^2` down to about `xi = 1e-4` and
then worsens again. The deterministic-variance limit is Black-Scholes and should
be priced with it.

## 6. Model consensus

`ModelConsensusService` prices one contract with several models over one
`MarketState` and returns `reference_value` (median), `reference_range`,
`model_median`, `model_dispersion`, `market_deviation` and `confidence`, plus the
individual model values. It never returns a single price. If models disagree by
5%, the user must see that they disagree by 5% — that disagreement is the most
honest thing the platform can tell them.

Four models run: Black-Scholes-Merton at the surface's reference volatility, the
Crank-Nicolson PDE on the Dupire local volatility derived from the same surface,
Heston at its own calibration, and a seeded simulation. The first, second and
fourth are three routes to the same number and agree to well under a percent —
a test asserts it, because a gap between them would be a bug rather than model
risk. Heston is the one that genuinely disagrees, and that disagreement is the
output.

**There is no `best_model` field and there never will be.** Choosing between
models is a judgement about which set of wrong assumptions is least wrong for
one contract on one day, and the platform is not in a position to make it. The
API schema has no such field, a test scans every key of the serialised payload
for one, and the stored row has no column that could hold it.

Confidence is a weighted geometric mean over named contributions — model count,
model agreement, simulation precision, surface admissibility, extrapolation,
surface fit, plus anything the caller folds in — so a low score always comes
with the dimension that caused it. Agreement uses the quadratic ratio penalty
rather than a linear ramp: there is no dispersion at which model agreement
becomes exactly worthless, and a ramp would score a 5.1% spread and a 50% spread
identically at zero, which through a geometric mean would collapse the whole
confidence for both.

One property is worth stating because it looks wrong and is not: dropping a
disagreeing model can *raise* the confidence, because agreement improves more
than the model count falls. That is the intended reading. A model that disagrees
is information, and what it tells you is that there is model risk here.

## 6. Failure modes

`OPTION_EXPIRED`, `MISSING_UNDERLYING_PRICE`, `MISSING_CURVE`,
`MISSING_SURFACE`, `EXTRAPOLATED_STRIKE`, `EXTRAPOLATED_MATURITY`,
`CALIBRATION_UNAVAILABLE`, `NUMERICAL_NONCONVERGENCE`. Each degrades one model to
`null` with a warning; the consensus continues with the remaining models and
reports the reduced model count in its confidence.

A model that could not run is stored as a row with a reason, not as a missing
row. The difference matters: a consensus over three models because the fourth
failed and a consensus over three models because only three were asked for are
different results, and only stored unavailability can tell them apart.
`ck_model_value_has_value_or_reason` makes the exclusive-or a database rule.

Nothing is defaulted to make a model runnable. Heston with no calibration
reports itself unavailable rather than being priced on a plausible-looking
`(v0, kappa, theta, xi, rho)`, because a confident number about nothing is worse
than an absence.

## 7. Numerical engines

**PDE.** Crank-Nicolson on `x = ln S` over a sinh-concentrated grid, with the
first two steps split into four implicit halves (Rannacher). Without the
start-up the payoff kink at the strike sets off oscillations that Crank-Nicolson
damps only slowly, and gamma is where they show. Second-order differences on a
non-uniform grid, Thomas-algorithm solve, asymptotic Dirichlet boundaries. The
solution is read at the spot by a quartic fit through five nodes: a quadratic
through three gives a second derivative accurate only to `O(h)`, and the
order-of-convergence test reported gamma at order 1 until it was widened.

**Monte Carlo.** Exact geometric Brownian motion terminals, antithetic pairs,
and a control variate on the discounted terminal price — whose expectation
`S e^{-q tau}` is known exactly under the pricing measure. Measured variance
reduction is about 78%. Every result carries its standard error, its seed and
its path count, and states in its own payload that the same seed reproduces it
and a different one will not.
