# Test Strategy

A quantitative platform can be 100% green on software tests and still produce
numbers that are wrong. So the suite has two independent axes: **does the code
work** and **are the numbers right**.

---

## 1. Layout

```
tests/
├── unit/              pure logic, no I/O            (fast, run on every save)
├── integration/       API + DB + object store       (SQLite/aiosqlite or Postgres)
├── quant_validation/  numerical correctness         (analytic + reference libraries)
├── regression/        golden files, tolerance-gated
├── performance/       benchmarks, opt-in marker
└── data/              committed fixtures
```

`quant_validation` never touches the database. That is possible only because
`quant/` imports nothing from `domains/` or `infrastructure/`, which
`scripts/check_layering.py` enforces.

## 2. Software tests

- **Unit**: instrument invariants, canonical key/uuid5 determinism, quote
  derived fields, quality scoring, CSV parsing and column mapping, exclusion
  policy, job state machine, JWT/password primitives.
- **Integration**: full HTTP round trips through the real app with a real
  database — register, login, upload, preview, ingest, poll job, read chain.
  Ownership isolation is tested explicitly: user B must get 404 on user A's ids.
- **End-to-end** _(planned)_: Playwright against the compose stack for the
  upload -> chain -> smile -> surface path.

## 3. Quantitative validation

The mandatory checks from build spec §87, phased in with the features:

| Area | Test | Phase |
| --- | --- | --- |
| Synthetic market | generated chain is internally arbitrage-free (bounds, parity, butterfly, calendar) | 0 |
| Quality engine | crossed/locked/stale/sub-intrinsic fixtures produce the expected flags and exclusions | 0 |
| Black-Scholes | prices and Greeks vs `vollib` and QuantLib | 1 `[x]` |
| Put-call parity | `C - P == DF*(F - K)` within tolerance over a grid | 1 `[x]` |
| Black-76 vs BSM | the two parameterizations must price identically | 1 `[x]` |
| Implied volatility | `price(sigma) -> solve -> sigma` recovered to 1e-6 where well conditioned; within a few multiples of the reported `uncertainty` where it is not | 1 `[x]` |
| Greeks | analytic vs central finite differences, every Greek | 1 `[x]` |
| Greek units | vega per vol point, theta per day, rho per bp, each against a re-priced bump | 1 `[x]` |
| Forward estimation | parity regression recovers `F` and `DF` exactly from prices alone | 1 `[x]` |
| Chain analysis | the known generating surface is recovered end to end from tick-rounded quotes | 1 `[x]` |
| SVI parameterization | analytic derivatives vs finite differences; Durrleman `g` catches a steep slice; Lee's bound is the asymptotic slope | 2 `[x]` |
| SVI calibration | known parameters recovered from a wide slice to 1e-4; fitted slices always admissible; deterministic across runs | 2 `[x]` |
| SVI identifiability | a narrow window fits to 0.005 vol points while missing the parameters by 0.05 — asserted, not papered over | 2 `[x]` |
| Arbitrage conditions | clean chain produces none; each seeded corruption is caught by the right detector with the right magnitude | 2 `[x]` |
| Surface pipeline | the generating surface is recovered in sample from tick-rounded quotes; a corrupted market dirties the raw report but not the fit | 2 `[x]` |
| Reference values | reproduced bit-for-bit from persisted parameters, with no re-fitting on read | 2 `[x]` |
| Local vol | constant local vol -> PDE price converges to Black-Scholes; measured order of convergence | 9 |
| Heston | cross-check against QuantLib | 9 |
| Surface characteristics | analytic skew matches a finite difference of the fitted curve; interpolated tenors lie between their neighbours | 3 `[x]` |
| Historical percentiles | thin samples are reported and marked unreliable; a constant sample yields no z-score | 3 `[x]` |
| Anomaly detection | quiet on a market that agrees with its fit; finds a quote nudged off the surface; a wider market or worse fit lowers the score | 3 `[x]` |
| Anomaly language | no advisory word appears anywhere in the serialised output, at domain and HTTP level | 3 `[x]` |
| VaR | recovers analytic quantile of a known synthetic distribution | 5 |
| Monte Carlo | convergence with `1/sqrt(N)`; identical results for identical seeds | 5 |
| Execution | deterministic synthetic price path -> hand-computed IS/VWAP | 7 |

### A tolerance that adapts to conditioning

The implied-volatility round trip is the clearest case where a single fixed
tolerance would have been dishonest. For a deep in-the-money option the price is
nearly flat in volatility, so a float64 price simply does not determine the
volatility to 1e-6 — and asserting that it does would have meant either a failing
test or a tolerance loosened until it passed everywhere, which hides the real
result at the money.

Instead the solver reports `uncertainty` (roughly one price ulp divided by
vega), and the test asserts 1e-6 where the problem is well conditioned and a few
multiples of `uncertainty` where it is not. That is a *stronger* assertion than a
blanket bound: it checks the solver against the best any algorithm could do on
that input, and it fails if the reported conditioning is itself wrong.

### Reference libraries as oracles, not dependencies

`vollib` and `QuantLib` are installed under the `validation` extra and used in
tests only. Production code paths do not import them unless a specific,
documented decision says so (see `docs/references.md`). This gives independent
implementations that can disagree — which is the point of a cross-check. Tests
that need them are marked `requires_reference_libs` and skip cleanly when absent,
so the core suite runs anywhere.

Phase 1 justified that posture concretely: `py_vollib.black.implied_volatility`
returns `inf` or raises on essentially every input in this environment,
including a plain at-the-money quote. A platform that had wrapped it would be
shipping `inf`. The suite uses the working `black_scholes` entry point as the
oracle, skips the cases where vollib itself fails, and asserts the gap.

## 4. Property-based tests (Hypothesis)

Invariants worth stating as properties:

- Call price is non-decreasing in spot and non-increasing in strike.
- Put price is non-decreasing in strike.
- Option value is non-negative and within its no-arbitrage bounds.
- Every quality sub-score lies in `[0, 1]`, and `overall_score <= max(sub-scores)`.
- Canonical key parsing round-trips: `parse(render(instrument)) == key`.
- Ingestion conserves rows: `input == kept + excluded`, and every excluded row
  has exactly one primary reason.
- Total variance from an admissible SVI slice is non-negative everywhere.
- A fitted slice's `g(k)` is non-negative across the checked grid.
- An anomaly's `explained_scale` grows when the market widens or the fit worsens.
- Confidence stays within `[0, 1]` for every combination of inputs.
- Portfolio aggregation: the sum over positions equals the portfolio total, for
  value and for every Greek, over arbitrary mixes of long and short, quoted and
  unquoted legs (`test_portfolio_valuation.py::TestSumProperty`). The value is
  compared exactly because it is `Decimal` throughout; the Greeks are float sums
  and are compared to float tolerance.
- Every position carries a `valuation_method`, and `base_market_value is None`
  exactly when that method is `UNAVAILABLE` — so an unpriced position can never
  be counted as worth zero.
- Position import conserves rows: `input == resolved + ambiguous + invalid`.
- A margin estimate is never negative and its confidence is always in `[0, 1]`,
  for any mix of long and short legs (`test_margin.py`).
- A schedule's slices sum to the parent quantity exactly, for arbitrary weights,
  totals and lot sizes (`test_execution_simulation.py`). Exactly, not to a
  tolerance: `Decimal` throughout, and `Schedule.__post_init__` raises otherwise.
- Every simulated result carries the counterfactual label, for any latency.
- A parent order's average price always lies between its best and worst fill,
  and a shortfall measured against that same average is always exactly zero
  (`test_tca.py`). The second is the check that the thing being measured cannot
  be its own benchmark.
- A Monte Carlo run is reproducible from its seed, for every path count and seed
  (`test_var.py::TestSimulation::test_every_run_is_reproducible`).
- The vectorised repricing agrees with the scalar one for any spot and
  volatility shock (`test_revaluation.py::TestVectorisedAgreesWithScalar`).
- Execution schedules sum to the parent quantity _(Phase 8)_.
- SSVI total variance at the money equals `theta` exactly, and the analytic
  strike derivatives match a Richardson-extrapolated finite difference
  (`test_ssvi.py`). The reference is extrapolated rather than differenced at one
  step because the second difference is squeezed from both sides — cancellation
  at a small step, a sharply peaked curvature at a large one.
- A fitted SSVI term structure is non-decreasing, for any observed input,
  including an inverted one.
- A local-volatility grid conserves its points: `total = valid + flagged`, and
  every invalid point carries at least one flag.
- Every model in a consensus carries exactly one of a value and an
  unavailability reason.

### Testing an order of convergence, not an error

Phase 9's PDE criterion is the *rate* at which the error falls under refinement,
not its size. A coarse grid can be close by luck and a wrong scheme can be close
on one contract; only the order separates a correct second-order scheme from an
incorrect one. `tests/quant_validation/test_pde.py` refines the grid four times,
fits a slope in log-log, and requires at least 1.8 for price, delta and gamma on
both a uniform and a concentrated grid.

It earned its place on the first run: gamma came back at order 1.0 because the
solution was interpolated with a three-point quadratic, whose second derivative
is accurate only to `O(h)`. The price and the delta looked correct throughout.

The same file holds the Dupire round trip — a local-volatility surface must
reprice the implied surface it was derived from — which found two further errors
that no other test could see, both described in `docs/architecture.md` §23.

### Testing an absence

Phase 6's acceptance criteria are mostly about numbers the platform must *not*
produce, which needs a different kind of test. Three shapes are used:

- **Scan the serialised payload.** `test_margin.py` renders the whole result to
  a string and asserts that venue names and affirmative claims do not appear in
  it. This catches a phrase added to a docstring or an assumption three layers
  down, which an assertion on a named field would not.
- **Test the construction, not the word.** "Liquidation" is allowed to appear,
  because the sentence doing the most work is the one that denies it. The test
  walks every occurrence and requires "not a broker" immediately before it.
  Banning the substring outright would have forbidden the disclaimer.
- **Assert the absence of fields.** `required_margin`, `broker_margin` and
  `exchange_margin` must not be keys of the result payload, so a future addition
  has to delete a test to land. Phase 9 does the same for the consensus:
  `best_model`, `true_price`, `fair_value`, `recommendation` and `signal` are
  checked against every key at every depth of the serialised response.

### Two invariants that exist to catch a future change

A null scenario reprices to **exactly** the base value, and every scenario's
risk contributions sum to the total with a zero residual. Both hold by
construction today. They are asserted anyway, because both stop holding the
moment something portfolio-level and non-additive — netted margin, in Phase 6 —
enters the revaluation, and it is better for a test to say so than for a number
to quietly change.

## 5. Regression / golden files

Committed fixtures with committed expected outputs:

```
tests/data/options_chain_clean.csv        -> expected_ingestion_clean.json
tests/data/options_chain_bad_quotes.csv   -> expected_ingestion_bad.json
tests/data/portfolio_options.csv          -> expected_risk.json         (P4/P5)
tests/data/trades.csv                     -> expected_tca.json          (P7)
tests/data/orderbook.parquet              -> expected_microstructure.json (P10)
                                          -> expected_iv.json           (P1, planned)
                                          -> expected_svi.json          (P2)
```

A drift beyond the declared tolerance fails CI. Regenerating a golden file is a
deliberate act: `python scripts/regen_golden.py --accept <name>` rewrites it and
the diff must be reviewed and justified in the PR, alongside a model version
bump if a formula changed.

## 6. Tolerances

Declared centrally in `tests/tolerances.py`, never inline magic numbers:

| Quantity | Tolerance |
| --- | --- |
| Option price vs analytic | 1e-10 relative |
| Option price vs reference library | 1e-8 absolute |
| Implied volatility round-trip | 1e-6 absolute, **or** 8x the solver's reported `uncertainty` where the problem is ill conditioned |
| Greeks vs finite difference | 1e-5 relative |
| PDE vs closed form | 5e-3 relative (grid-dependent, asserted with convergence order) |
| Monte Carlo vs closed form | 3 standard errors |
| Quality scores | 1e-9 |
| SVI parameter recovery (wide slice) | 1e-4 absolute |
| Fitted surface vs generating surface, in sample | 0.1 volatility points |
| Reference IV vs the market IV it was fitted to | 2e-3 |

## 7. Performance benchmarks

Marked `performance`, excluded from the default run. Targets tracked over time
rather than asserted as pass/fail, per build spec §90: 1 / 1e3 / 1e5 option
pricing and IV solve, SVI calibration per expiry, 10k-position portfolio
valuation, Monte Carlo throughput, large trade import, L2 analytics.

Optimization only follows a profile. Numba is not introduced speculatively.

## 8. CI

1. `ruff check` + `ruff format --check`
2. `mypy` on `quant/` and `domains/`
3. `python scripts/check_layering.py`
4. `pytest -m "not performance and not requires_reference_libs"`
5. `pytest -m "requires_reference_libs"` (with the validation extra installed)
6. `pytest -m regression`
7. Alembic: `upgrade head` then `downgrade base` on a scratch database

## 9. What the tests deliberately do not assert

No test asserts that the platform's reference value is "correct" in the sense of
predicting a market price. The reference value is a model output; tests assert it
is *computed correctly, reproducibly, and with honest uncertainty*, which is the
only claim the product makes.
