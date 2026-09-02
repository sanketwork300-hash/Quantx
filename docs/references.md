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
| Heston pricing and calibration | `WRAP` (Phase 9) | Characteristic-function integration is numerically delicate; QuantLib's engine is mature. Wrapped behind `PricingModel` so it stays swappable and so our own implementation can be compared against it. |
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

**Feature:** Local volatility
**Academic reference:** B. Dupire, *Pricing with a Smile* (1994); Gatheral (2006) for the total-variance formulation
**Reference implementation:** QuantLib local-vol surfaces (oracle)
**Implemented independently:** derivatives taken on the smooth fitted surface in total-variance coordinates, with stability diagnostics and explicit invalid regions
**Known limitations:** wings and very short expiries are extrapolation; those regions are flagged invalid rather than clipped to a plausible number

---

**Feature:** Risk-neutral density
**Academic reference:** Breeden & Litzenberger (1978)
**Implemented independently:** second strike-derivative of the fitted call surface
**Known limitations:** an interpretability and arbitrage diagnostic only; it is a risk-neutral density and is never described as a forecast of physical probability

---

**Feature:** Heston
**Academic reference:** S. Heston (1993); Albrecher et al. (2007) on the correct branch of the characteristic function
**Reference implementation:** QuantLib
**Decision:** `WRAP` + `VALIDATE AGAINST`
**Known limitations:** calibration is non-convex; local minima are real. Optimizer status and multi-start results are recorded, and Heston is presented as one model among several, never as the true model

---

**Feature:** Historical / parametric / Monte Carlo VaR and Expected Shortfall
**Academic reference:** standard risk-management literature; Acerbi & Tasche (2002) for ES coherence
**Reference implementation:** Riskfolio-Lib (oracle)
**Implemented independently:** full historical repricing for nonlinear books; parametric VaR with assumptions stated in the response payload
**Known limitations:** parametric VaR is invalid for option books and is never returned as the sole measure

---

**Feature:** Margin estimation
**Academic reference:** none — exchange methodologies are proprietary
**Decision:** `IMPLEMENT INDEPENDENTLY` as an explicitly labelled approximation
**Known limitations:** **not** broker-equivalent. Output always carries method, assumptions, confidence and warnings, and describes a margin-shortfall *region*, never a liquidation price

---

**Feature:** Optimal execution scheduling
**Academic reference:** Almgren & Chriss, *Optimal Execution of Portfolio Transactions* (2000)
**Implemented independently:** TWAP, VWAP, POV, liquidity-adaptive; Almgren-Chriss later
**Known limitations:** all schedules are counterfactual — executing them would itself have moved the market

---

**Feature:** Market impact
**Academic reference:** Almgren et al. (2005); Gatheral, *No-dynamic-arbitrage and market impact* (2010); Bouchaud et al. on propagator models
**Implemented independently:** square-root and linear baselines; ML only after sufficient labelled executions exist
**Known limitations:** impact coefficients are regime- and venue-dependent; defaults are stated, not tuned to a market we have not measured

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
