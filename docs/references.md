# References — literature, reference implementations and reuse decisions

Two purposes: (1) give every algorithm an academic source so nothing is invented,
and (2) record an explicit reuse decision per feature so the project is neither a
pile of thin wrappers nor a pointless reimplementation of mature infrastructure.

> **Licence caveat.** Licence identifiers below are recorded as the project's
> current understanding and **must be re-verified against the repository at the
> pinned version before any code is copied or vendored**. Where a library is used
> as a *test oracle* only (imported in `tests/`, never in shipped code paths),
> the derivative-work question does not arise, which is the default posture for
> the core numerics.

---

## 1. Reuse decision vocabulary

| Decision | Meaning |
| --- | --- |
| `USE DIRECTLY` | call the library at runtime; it is a dependency |
| `WRAP` | call it at runtime behind our own interface, so it can be swapped |
| `VALIDATE AGAINST` | do not ship it; use it in tests as an independent oracle |
| `ADAPT CONCEPTS FROM` | read the design, write our own code, credit the source |
| `IMPLEMENT INDEPENDENTLY` | write from the academic specification |

## 2. Reference-project mapping

### QuantLib — `lballabio/QuantLib`
Licence: modified BSD (permissive). Bindings: `QuantLib` on PyPI.

| Area | Decision | Rationale |
| --- | --- | --- |
| Black-Scholes / Black-76 analytic prices and Greeks | `VALIDATE AGAINST` | The formulas are short and we need them differentiable, vectorized and free of QuantLib's object graph. An independent implementation cross-checked against QuantLib is a stronger guarantee than a wrapper. |
| Exchange calendars, day-count conventions, business-day rules | `USE DIRECTLY` | Holiday calendars are data, not mathematics. Hand-rolling them is exactly the fabrication §1.1 forbids. |
| Heston pricing and calibration | `IMPLEMENT INDEPENDENTLY`, `VALIDATE AGAINST` (Phase 9) | Originally planned as `WRAP`. Changed during implementation: the standing rule for core numerics is to implement from the specification and use the library as a test oracle, and wrapping would also have put QuantLib on the runtime critical path of every deployment rather than only in CI. The agreement is 1.5e-11 absolute across maturities from one week to ten years. |
| PDE / Crank-Nicolson engines | `ADAPT CONCEPTS FROM`, `VALIDATE AGAINST` | We need direct control of the local-vol grid, boundary treatment and convergence diagnostics. QuantLib is the correctness oracle. |
| Term structures / yield curves | `ADAPT CONCEPTS FROM` | We need curves that serialize into provenance and rehydrate deterministically. |
| Instrument / pricing-engine separation | `ADAPT CONCEPTS FROM` | The `Instrument x Engine` split is the right abstraction and is reflected in our `PricingModel` ABC. |

### vollib / py_vollib — `vollib/py_vollib`
Licence: MIT. Implied volatility follows Jäckel's "Let's Be Rational".

| Area | Decision |
| --- | --- |
| Black / Black-Scholes / BSM prices and analytic Greeks | `VALIDATE AGAINST` |
| Implied volatility | `VALIDATE AGAINST` — our engine is a safeguarded Newton with a bracketed Brent fallback and an explicit convergence report; vollib's LBR result is the oracle in `tests/quant_validation/` |

Why not use it directly: we need a solver that reports bounds, iterations,
convergence status and failure reasons as structured data feeding the quality and
confidence machinery. That reporting requirement, not the arithmetic, is the
reason for our own layer.

> **Finding, recorded during Phase 1.** In this environment
> `py_vollib.black.implied_volatility` returns `inf` or raises
> `ZeroDivisionError` on essentially every input, including a plain at-the-money
> three-month 30% quote; the underlying `py_lets_be_rational` divides by zero.
> `py_vollib.black_scholes.implied_volatility` works and is used as the oracle
> instead, with failing cases skipped and the gap asserted in
> `TestAgainstVollib::test_we_solve_cases_vollib_cannot`.
>
> This is the concrete justification for the whole VALIDATE AGAINST posture: a
> platform that had wrapped vollib for implied volatility would be shipping
> `inf` to users. An independent implementation cross-checked against a library
> catches the library's failures; a wrapper inherits them.

### py_vollib_vectorized — `marcdemers/py_vollib_vectorized`
Licence: MIT.

`ADAPT CONCEPTS FROM` — the vectorized chain-evaluation approach. Explicitly
**not** adopted: its monkey-patching of `py_vollib` at import time. Global
mutation of a third-party namespace is incompatible with a platform where model
version and provenance must be unambiguous.

### NautilusTrader — `nautechsystems/nautilus_trader`
Licence: LGPL-3.0. **Not a runtime dependency** (copyleft interaction with an
Apache-2.0 codebase is avoidable and we have no need to link it).

`ADAPT CONCEPTS FROM`:
- normalized, precision-explicit instrument model (our `Instrument` +
  `tick_size`/`lot_size`/`multiplier` as exact decimals);
- adapter architecture isolating venue quirks (our `MarketDataProvider`);
- separation of event time from receive time on every message;
- deterministic event-driven backtest design (our Phase 8 simulator);
- strict separation of research from execution.

Explicitly not reproduced: the live trading engine, actor model, message bus.
QIP is an analytics platform and does not place orders.

### ABIDES — `jpmorganchase/abides-jpmc-public` (and `abides-sim/abides`)
Licence: **verify before use.**

`ADAPT CONCEPTS FROM` (Phase 10, research only) — discrete-event exchange
simulation, agent populations, configurable latency. Deliberately **not** an MVP
runtime dependency: a full agent-based market simulator is an order of magnitude
more machinery than counterfactual TCA needs, and coupling the web platform to it
would make the common case pay for the rare one.

### Riskfolio-Lib — `dcajasn/Riskfolio-Lib`
Licence: BSD-3-Clause.

`VALIDATE AGAINST` — covariance estimators and portfolio risk measures. Not
wrapped: our risk service must reprice nonlinear books under scenarios and emit
provenance and warnings, which is a different shape of problem from
optimization-oriented risk models. Using it as a cross-check keeps our
transparency requirement without a hidden dependency.

### Infrastructure

| Project | Licence | Decision |
| --- | --- | --- |
| FastAPI `fastapi/fastapi` | MIT | `USE DIRECTLY` |
| TimescaleDB `timescale/timescaledb` | Apache-2.0 core (some features TSL) | `USE DIRECTLY`, and the schema degrades to plain Postgres if the extension is absent |
| DuckDB `duckdb/duckdb` | MIT | `USE DIRECTLY` for analytical reads over Parquet |
| redis-py `redis/redis-py` | MIT | `USE DIRECTLY` |
| Celery `celery/celery` | BSD-3-Clause | `USE DIRECTLY` |
| SQLAlchemy / Alembic | MIT | `USE DIRECTLY` |
| NumPy / SciPy / pandas / Polars / PyArrow | BSD-3 / MIT-family | `USE DIRECTLY` |

## 3. Feature -> reference table

Format per build spec §96.

---

**Feature:** Canonical instrument identity
**Academic reference:** none (engineering)
**Reference implementation:** NautilusTrader instrument model
**Reused conceptually:** precision-explicit contract fields, venue-agnostic identity
**Implemented independently:** canonical key grammar, deterministic uuid5 ids, invariants
**Known limitations:** exchange-specific corporate-action handling is out of scope in Phase 0

---

**Feature:** Implied volatility
**Academic reference:** P. Jäckel, *Let's Be Rational* (2015); *By Implication* (2006)
**Reference implementation:** `vollib/py_vollib` (oracle only; see the finding above)
**Reused conceptually:** the fact that a well-bracketed inversion is exactly solvable; test vectors
**Implemented independently:** vectorized safeguarded Newton over a maintained bracket, with a scalar Brent fallback; explicit bounds, iteration and convergence reporting; a reported conditioning (vega, and the volatility uncertainty implied by one price ulp)
**Known limitations:** near-zero time value and sub-intrinsic quotes have no solution — reported as a structured non-result, never as `NaN` or a clipped value. For a deep in-the-money option the price is nearly flat in volatility, so the implied volatility is genuinely undetermined beyond roughly `price_ulp / vega`; the solver reports that bound rather than implying a precision the data does not carry

---

**Feature:** Option pricing (Black-76 / BS / BSM) and analytic Greeks
**Academic reference:** Black & Scholes (1973); Merton (1973); Black (1976)
**Reference implementation:** QuantLib, vollib
**Reused conceptually:** nothing — the formulas are the primary source
**Implemented independently:** vectorized NumPy implementations with explicit unit conventions
**Known limitations:** European exercise only in Phase 1; American handled later by a binomial/PDE engine

---

**Feature:** Forward estimation
**Academic reference:** put-call parity; standard cost-of-carry
**Reference implementation:** none adopted
**Implemented independently:** three estimators (spot-carry, futures-derived, parity regression) each returning value, method, confidence, observation count and residual error
**Known limitations:** parity regression degrades with few liquid pairs; confidence reflects that and the estimate is not used silently

---

**Feature:** Volatility smile / SVI
**Academic reference:** J. Gatheral, *The Volatility Surface* (2006); Gatheral & Jacquier, *Arbitrage-free SVI volatility surfaces* (2014), Lemma 2.2 for Durrleman's condition; R. Lee, *The moment formula for implied volatility at extreme strikes* (2004) for the wing bound
**Reference implementation:** none copied
**Reused conceptually:** raw SVI parameterization, Durrleman's `g(k) >= 0`, the wing-slope bound
**Implemented independently:** analytic derivatives of `w`; constrained SLSQP with the no-arbitrage conditions in the feasible set rather than checked afterwards; deterministic multi-start; liquidity/spread weighting taken from the quality engine; full optimizer and admissibility reporting
**Known limitations:** raw SVI is fitted per expiry, so calendar arbitrage across slices is *detected and reported*, not prevented, until SSVI lands in Phase 9. The five parameters are not identifiable from a narrow strike window: on `k in [-0.07, 0.03]` the fitted curve is right to 0.005 volatility points while the parameters miss by 0.05, so the fitted curve is the meaningful object in sample and the wings are weakly constrained. Both facts are surfaced (`SURFACE_NARROW_STRIKE_RANGE`, `EXTRAPOLATED_STRIKE`) rather than hidden

---

**Feature:** Arbitrage diagnostics
**Academic reference:** Gatheral & Jacquier (2014) for butterfly/calendar in total-variance form; standard static no-arbitrage bounds
**Implemented independently:** bounds, parity, vertical monotonicity and the `DF·dK` slope bound, butterfly convexity in its general unequally-spaced form, calendar consistency at fixed log-moneyness over the observed overlap; Durrleman's `g` and Lee's bound on fitted slices; raw-market and fitted-surface violations reported and persisted separately; severity graded by magnitude against the local quoted spread
**Known limitations:** discrete strike grids make butterfly tests sensitive to spacing; magnitude and the tolerance it was judged against are both reported so severity can be argued with rather than thresholded blindly. A violation in observed quotes is treated as a data-quality signal, never as an opportunity

---

**Feature:** Portfolio valuation and Greek aggregation
**Academic reference:** none — this is bookkeeping, not a model. The pricing and Greeks it aggregates are Black-Scholes-Merton (see the entry above)
**Reference implementation:** NautilusTrader's portfolio/position accounting was read for its treatment of signed quantity and multipliers; QuantLib has no portfolio layer of the shape needed
**Decision:** `IMPLEMENT INDEPENDENTLY`
**Reused conceptually:** signed quantity with the side retained for reconciliation rather than used to infer the sign
**Implemented independently:** the price waterfall with a recorded `valuation_method`; per-position Greeks scaled exactly once by signed quantity, contract multiplier and the snapshot's own FX rate; aggregation over five dimensions that all sum the same per-position numbers
**Known limitations:** unrealised P&L is measured against the average entry price supplied by the user or their broker export, so it inherits whatever cost convention that source used — the platform does not reconstruct a cost basis from trades, and does not claim to. Cross-currency conversion needs an FX rate inside the same `MarketState`; a position in a currency with no rate is left unvalued rather than converted at a rate from another moment

---

**Feature:** SSVI global surface
**Academic reference:** J. Gatheral and A. Jacquier, *Arbitrage-free SVI volatility surfaces*, Quantitative Finance 14(1), 2014, §4
**Reference implementation:** none used; the paper's Theorem 4.2 is the specification
**Decision:** `IMPLEMENT INDEPENDENTLY`
**Implemented independently:** the power-law `phi`, analytic first and second strike derivatives and the maturity derivative through a monotone `theta(T)`, one constrained SLSQP fit over every expiry at once with deterministic multi-start, and both admissibility conditions evaluated and stored
**Known limitations:** three shared parameters cannot bend to each smile the way five per expiry can, so the per-expiry SVI surface fits any single smile better and both are kept. `gamma` is not identified by a single expiry, and a one-expiry fit says so. The closed-form butterfly bounds are sufficient and not necessary, so they are reported alongside Durrleman's condition rather than instead of it

---

**Feature:** Local volatility
**Academic reference:** B. Dupire, *Pricing with a Smile* (1994); Gatheral (2006) for the total-variance formulation
**Reference implementation:** QuantLib local-vol surfaces (oracle)
**Decision:** `IMPLEMENT INDEPENDENTLY`, `VALIDATE AGAINST` a repricing round trip
**Implemented independently:** derivatives taken analytically on the smooth fitted surface in total-variance coordinates, with stability diagnostics and explicit invalid regions; the PDE coefficient evaluated against the forward to each calendar time rather than a single fixed forward
**Known limitations:** wings and very short expiries are extrapolation; those regions are flagged invalid rather than clipped to a plausible number. Past the last fitted expiry the variance term structure is deliberately flat, so `dw/dT` is zero and local volatility is undefined; the PDE substitutes the surface's implied volatility there and counts the substitutions

---

**Feature:** Crank-Nicolson local-volatility PDE
**Academic reference:** R. Rannacher, *Finite element solution of diffusion problems with irregular data* (1984), for the implicit start-up; standard finite-difference literature for the non-uniform operator
**Reference implementation:** QuantLib FD engines were read for grid construction; nothing was copied
**Decision:** `ADAPT CONCEPTS FROM`, `VALIDATE AGAINST` Black-Scholes
**Implemented independently:** the sinh-concentrated log grid, second-order differences on it, the Thomas solve, the Rannacher schedule, and a quartic solution interpolation
**Known limitations:** European exercise only. The validation is an order-of-convergence test rather than an error tolerance, because a coarse grid can be close by luck; that test reported gamma at first order until the interpolation was widened from three nodes to five

---

**Feature:** Monte Carlo option pricing
**Academic reference:** standard variance-reduction literature; the control variate is the discounted terminal price, whose expectation is exact under the pricing measure
**Decision:** `IMPLEMENT INDEPENDENTLY`
**Implemented independently:** exact GBM terminals, antithetic pairs, control variate with the regression coefficient estimated from the sample
**Known limitations:** European vanillas under geometric Brownian motion only. A simulated price is never reported without its standard error, and the seed and path count travel with it

---

**Feature:** Risk-neutral density
**Academic reference:** Breeden & Litzenberger (1978)
**Decision:** `IMPLEMENT INDEPENDENTLY`
**Implemented independently:** second strike-derivative of the fitted call surface by a relative-bump difference, with mass, implied mean against the forward, negative mass and strike-range diagnostics
**Known limitations:** an interpretability and arbitrage diagnostic only; it is a risk-neutral density and is never described as a forecast of physical probability. Quantiles are computed only for a density that is both non-negative and normalised, because a quantile normalises by whatever mass it found and would otherwise be a quantile of the strike window

---

**Feature:** Heston
**Academic reference:** S. Heston, *A Closed-Form Solution for Options with Stochastic Volatility*, RFS 6(2), 1993; H. Albrecher et al., *The little Heston trap*, Wilmott, 2007, on the correct branch of the characteristic function
**Reference implementation:** QuantLib `AnalyticHestonEngine` (test oracle only)
**Decision:** `IMPLEMENT INDEPENDENTLY`, `VALIDATE AGAINST` — **changed from the `WRAP` recorded in the plan.** The standing rule for core numerics is to implement from the specification and validate against the library in tests, because two independent implementations that agree is a stronger guarantee than a wrapper; wrapping would also have made QuantLib a runtime dependency rather than a CI one. The deviation is recorded in `docs/pricing.md` §5 and `docs/architecture.md` §23
**Implemented independently:** the little-trap characteristic function, puts by put-call parity rather than a second quadrature, an integration limit that scales with `1 / sqrt(v tau)`, and a vega-weighted constrained calibration
**Known limitations:** calibration is non-convex; local minima are real. Optimizer status and multi-start results are recorded, and Heston is presented as one model among several, never as the true model. `kappa` and `theta` trade off against each other on a single surface — their product is what the data pins — so neither should be read alone. The characteristic function carries `1 / xi^2` and is ill-conditioned as the vol-of-vol vanishes: the deterministic-variance limit is Black-Scholes and should be priced with it

---

**Feature:** Model consensus
**Academic reference:** none — this is a product decision, not a method from the literature
**Decision:** `IMPLEMENT INDEPENDENTLY`
**Implemented independently:** the median as `reference_value`, the range the models actually spanned, absolute and relative dispersion, and a confidence that is a weighted geometric mean over named contributions each carrying its basis
**Known limitations:** the models are not independent. Black-Scholes at the surface's volatility, the Dupire PDE built from that surface and a simulation under it are three routes to the same number and agree to well under a percent, so the dispersion is dominated by whether Heston is present. That is a real property of the model set rather than a defect in the aggregation, and it is why the individual values are shown alongside the spread. Dropping a disagreeing model can raise the confidence, because agreement improves more than the model count falls; that is the intended reading, since a model that disagrees is telling you there is model risk

---

**Feature:** Historical / parametric / Monte Carlo VaR and Expected Shortfall
**Academic reference:** standard risk-management literature; Acerbi & Tasche (2002) for ES coherence
**Reference implementation:** Riskfolio-Lib (oracle)
**Implemented independently:** full historical repricing for nonlinear books; parametric VaR with assumptions stated in the response payload
**Known limitations:** parametric VaR is invalid for option books and is never returned as the sole measure

---

**Feature:** Value at Risk and Expected Shortfall
**Academic reference:** Acerbi & Tasche, *On the coherence of expected shortfall* (2002); Hyndman & Fan, *Sample quantiles in statistical packages* (1996) for the quantile convention; Efron & Tibshirani for the bootstrap interval
**Reference implementation:** Riskfolio-Lib was read for its estimator vocabulary; nothing was copied. `scipy.stats` is used as a test oracle for the normal quantile only
**Decision:** `IMPLEMENT INDEPENDENTLY`, `VALIDATE AGAINST` closed forms
**Implemented independently:** sample quantile and conditional tail mean with the observation counts travelling with the answer; parametric VaR/ES from the closed form; full repricing for both the historical and Monte Carlo paths; a bootstrap interval for the estimate rather than an asymptotic standard error
**Known limitations:** the lookback is the platform's own ingestion record, so it is short by the standards of the literature and every answer reports its observation count. Parametric VaR is invalid for an option book and is never returned as the sole measure. No GARCH filtering, no copulas, and no calibration of the Student-t degrees of freedom — it is a parameter the user sets, not a claim the platform makes

---

**Feature:** Inverse normal CDF
**Academic reference:** P. Acklam's rational approximation (2003); Halley's method for the refinement
**Reference implementation:** `scipy.stats.norm.ppf` (oracle only)
**Decision:** `IMPLEMENT INDEPENDENTLY`
**Implemented independently:** the rational approximation with a single Halley step whose residual is computed from the complement above the median, which keeps full precision in the far upper tail where `F(x) - p` cancels catastrophically
**Known limitations:** none measured — it agrees with the oracle to 2.2e-16 relative across 1e-12 to 1 - 1e-12. It exists so `scipy` stays a test dependency rather than a runtime one

---

**Feature:** Scenario stress testing
**Academic reference:** none — this is repricing under stated inputs, not a model
**Reference implementation:** none adopted
**Decision:** `IMPLEMENT INDEPENDENTLY`
**Implemented independently:** four shock types with composition rules; full revaluation from anchors that make the null scenario exactly zero; the second-order Greek estimate reported beside the answer and labelled; exact loss decomposition validated against the hold-one-group-flat construction
**Known limitations:** a volatility shock moves each position's own implied volatility rather than refitting the surface, so it preserves the smile's shape exactly and cannot represent a change in skew. Shocking the surface parameters instead would let a stress express a skew move, and is the natural extension. Historical scenarios are only ever derived from data the platform holds; no historical event ships with the product, because a round number under a real event's name is a fabricated measurement

---

**Feature:** Margin estimation
**Academic reference:** none — exchange and broker methodologies are proprietary, versioned and unpublished. The *shape* of a scan-grid model (worst loss over a declared grid of underlying and volatility moves, floored for short options) is a widely described pattern rather than a citable formula
**Reference implementation:** none adopted. No published methodology was available to implement against, so none was approximated
**Decision:** `IMPLEMENT INDEPENDENTLY` as an explicitly labelled approximation
**Implemented independently:** `SimpleRiskMarginModel` — full repricing at every grid point, an optional short-option floor, an optional concentration add-on, and a confidence score combining coverage, grid containment and mark consistency; a two-directional buffer ladder that reruns the model at every rung; a shortfall *region* interpolated between the two rungs that bracket it and reported with them
**Known limitations:** **not** broker-equivalent, and every output says so. The short-option minimum and concentration rates default to zero because choosing a value would be inventing a venue's rule, which means the estimate understates a book of far out-of-the-money short options until a user supplies their own rate. All underlyings are shocked together, so no diversification credit is given between names. An unbounded-loss book's worst case is never inside a finite grid, so its estimate is permanently a lower bound and its confidence is capped accordingly. No liquidation price is produced, at all, ever

---

**Feature:** Transaction cost analysis
**Academic reference:** A. Perold, *The implementation shortfall: paper versus reality* (1988) for the measure itself; standard TCA practice for the benchmark set
**Reference implementation:** none adopted; no open-source TCA engine was copied or wrapped
**Decision:** `IMPLEMENT INDEPENDENTLY`
**Reused conceptually:** the shortfall definition and the conventional benchmark set (arrival, decision, prevailing mid, interval VWAP/TWAP, close)
**Implemented independently:** benchmarks that each carry their window, source and method and can return an explicit unavailability instead of a price; a coverage model with stated thresholds that refuses an interval statistic the data cannot support; the shortfall in currency, basis points and percent with the side convention applied in one place; a component-labelled decomposition in which only fees are `MEASURED`
**Known limitations:** the market window is assembled from ingested option chains, which are sparse, so the interval benchmarks frequently report themselves unavailable — that is the honest outcome, not a gap to be filled with an average of three ticks. The interval VWAP is unavailable on every window the platform can currently build, because the stored volume is cumulative for the session rather than per interval. Market impact is not modelled in this phase, so the timing residual carries it; the decomposition says so rather than putting a number there. Opportunity cost requires the order's intended quantity, which most trade logs do not carry, and is reported unavailable rather than assumed to be zero

---

**Feature:** Optimal execution scheduling
**Academic reference:** Almgren & Chriss, *Optimal Execution of Portfolio Transactions* (2000) for the framing; TWAP/VWAP/POV are industry constructions rather than results
**Reference implementation:** none adopted
**Decision:** `IMPLEMENT INDEPENDENTLY`
**Implemented independently:** TWAP, VWAP, POV and liquidity-adaptive schedules; an allocation that distributes whole lots by largest fractional part so slices sum to the parent quantity exactly; a `StrategyUnavailable` outcome so a strategy whose inputs are missing refuses rather than silently becoming another strategy
**Known limitations:** all schedules are counterfactual — executing them would itself have moved the market. Almgren-Chriss is not implemented: it optimises a trade-off between impact and volatility risk whose parameters this platform has not measured, so it would be an optimiser over invented coefficients. VWAP and POV need a volume forecast the platform does not hold and must be given one. POV is the ex-ante schedule a forecast implies, not a simulation of tracking realised volume as the day unfolds

---

**Feature:** Market impact
**Academic reference:** Almgren et al. (2005); Gatheral, *No-dynamic-arbitrage and market impact* (2010); Bouchaud et al. on propagator models
**Reference implementation:** none adopted
**Decision:** `IMPLEMENT INDEPENDENTLY`
**Reused conceptually:** the square-root dependence on relative size, and the permanent/temporary split
**Implemented independently:** square-root and linear models with permanent and temporary terms returned separately, the temporary term driven by participation rate rather than total size, and a zero model for isolating a schedule from the impact assumption
**Known limitations:** impact coefficients are regime- and venue-dependent and **none is shipped**. They default to the identity, which makes the output the shape of the model rather than a magnitude, and every result computed that way is flagged `IMPACT_COEFFICIENT_NOT_CALIBRATED`. Adopting a published estimate as a default would assert a measurement of a market nobody here observed. No propagator or transient-impact model, no cross-impact, and no decay: temporary impact is paid on the slice and gone

---

**Feature:** Counterfactual execution simulation
**Academic reference:** none — this is repricing a schedule against a recorded path, not a model of the market's response
**Reference implementation:** none adopted; no backtesting framework was wrapped
**Decision:** `IMPLEMENT INDEPENDENTLY`
**Implemented independently:** a slice walker that accumulates permanent impact into the reference price, pays temporary impact and half the quoted spread per slice, leaves a slice unfilled when the nearest observation is beyond a declared age, and scores the simulated fills through the Phase 7 benchmark machinery unchanged
**Known limitations:** **the simulation cannot capture its own effect on the market.** The observed path already contains what the real order did and does not contain what the hypothetical one would have done, so every result is labelled a counterfactual estimate on the object, in the payload and by a database CHECK. There is no queue model, no partial-fill model within a slice, and no adverse-selection model; latency shifts the price lookup and nothing else

---

**Feature:** Order-book analytics
**Academic reference:** standard microstructure literature; Stoikov (2018) on microprice
**Implemented independently:** spread, depth, microprice, single- and multi-level imbalance, book slope
**Known limitations:** requires genuine L2; snapshot-only feeds cannot support queue or intensity models

---

**Feature:** Queue position and Hawkes intensity
**Academic reference:** Hawkes (1971); Bacry, Mastromatteo & Muzy (2015) survey
**Decision:** gated — not implemented until event-level add/cancel/modify/execute data with sequencing exists, and only if it beats a Poisson baseline out-of-sample
**Known limitations:** without exchange-level queue information, position can only ever be probabilistic, and will be reported as such

---

## 4. Compliance procedure before copying any code

1. Read the licence file at the exact pinned version.
2. Record repository, commit/tag and licence in this document.
3. Confirm compatibility with Apache-2.0 distribution.
4. Preserve copyright and licence notices in the copied file.
5. Prefer implementing from the academic specification and validating against the
   library instead, which is the default choice throughout this project.
