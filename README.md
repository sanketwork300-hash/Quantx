# Quant Intelligence Platform

An open-source analytics platform for derivatives valuation, portfolio risk and
execution intelligence — built for retail traders, independent quants,
researchers and small trading teams who do not have institutional risk and TCA
infrastructure.

It exists to answer one question, in three parts:

> **What is this position or trade approximately worth, what risk does it add,
> and what will it probably cost me to execute?**

```
                          MARKET
                             |
                     Market Data Layer
                             |
                        MarketState              <- one consistent snapshot
                             |
         +-------------------+-------------------+
         v                   v                   v
     VALUATION             RISK              EXECUTION
         |                   |                   |
         +-------------------+-------------------+
                             v
                      DECISION CONTEXT
```

---

## What this is not

This is an analytics and research tool. It is deliberately incapable of telling
you what to trade.

| It never says | It says instead |
| --- | --- |
| fair value | reference value, reference range |
| underpriced / cheap / expensive | surface deviation, reference-value deviation |
| arbitrage opportunity | model-inconsistent quote, potential relative-value anomaly |
| you will be liquidated at X | estimated margin-shortfall region beyond ~X% adverse move |
| optimal execution | counterfactual estimate, estimated slippage |
| your broker's margin | estimated margin under a named, stated model |
| BUY / SELL | *(no such output exists; a test asserts the response schema has none)* |

Every material number carries the data it came from, the model version that
produced it, the assumptions that were made, and what the platform could not do.

---

## Status: Phases 0 through 8 complete

Three phases remain (9 through 11); `docs/backlog.md` has the plan and the
acceptance criteria each must meet.

**Phase 0 — foundation.**
Repository skeleton with layering rules **enforced in CI**; Docker Compose
stack; settings, structured logging with correlation ids, async SQLAlchemy,
reversible Alembic migrations (optionally TimescaleDB), Redis cache, object
store, Celery with an inline "eager" mode. Users with bcrypt, JWT, ownership
checks and an audit log. Instruments with canonical keys, deterministic `uuid5`
identity and a resolver where `AMBIGUOUS` is a first-class outcome. Market data:
the provider interface, CSV and seeded-synthetic providers, canonical quote
schemas, the **data-quality engine**, and the **option-chain ingestion
pipeline**. Asynchronous jobs.

**Phase 1 — options MVP.**
Day-count conventions and an explicit time-to-expiry policy. Content-addressed
yield curves. Three forward estimators (spot-carry, futures-derived, put-call
parity) reported side by side. Black-76 and Black-Scholes-Merton with analytic
Greeks whose units are named in the payload. An **implied-volatility engine**
that reports not just convergence but *conditioning*. Raw smiles in `(k, w)`
with the bid/ask IV envelope. Chain analysis as a job, with its own tables. A
smile chart in the UI.

**Phase 2 — volatility surface.**
Raw SVI calibrated per expiry by constrained SLSQP with deterministic
multi-start, with the no-arbitrage conditions — non-negative minimum variance,
Lee's wing bound and Durrleman's `g(k) >= 0` — **in the optimizer's feasible
set** rather than checked afterwards. An arbitrage validator that reports
observed-market and fitted-surface violations separately, with magnitudes and
the tolerance each was judged against. Content-addressed surfaces whose
reference values are a pure function of the five persisted parameters.
`MarketState`, the timestamp-consistency guarantee everything after this
depends on.

**Phase 3 — anomaly analytics.**
Surface characteristics recorded at standard tenors, so surfaces stay comparable
as expiries roll, with percentiles against an underlying's own history that
always carry their observation count. A scanner that compares observed implied
volatilities against the fitted reference and scores the difference against
everything that could explain it — the bid/ask width in volatility terms, the
slice's calibration error, and the numerical resolution of the inversion. Every
flagged quote explains itself from named measurements. No output field carries a
direction, a rating or a target, and a test asserts the words *buy*, *sell*,
*cheap*, *expensive*, *underpriced* and *arbitrage* appear nowhere in the
response.

**Phase 4 — portfolio.**
Portfolios and positions with a signed quantity whose sign and stated side must
agree. Position import that resolves every row against the instrument master
into `resolved / ambiguous / invalid` and **refuses to commit while any row is
ambiguous** — picking the most likely contract is how a book silently acquires
the wrong expiry. Valuation against one `MarketState` covering every underlying,
with the observed price and the surface's reference price in separate columns
and a `valuation_method` naming which one was used. Greeks scaled once, by
signed quantity times multiplier times the FX rate from that same snapshot, and
aggregated by underlying, expiry, asset class, strategy tag and currency — every
dimension summing to the same portfolio total.

**Phase 5 — risk.**
Value at Risk and Expected Shortfall by three methods, two of which **fully
reprice** the book under every scenario rather than scaling today's Greeks — and
the third of which says in its own response that it did not. A scenario engine
with four shock types, where applying a scenario reprices every position from
anchors chosen so that a *null* scenario returns exactly zero. The second-order
Greek estimate of the same move is returned beside the answer and labelled as an
approximation; on a short-gamma book the two differ by over 5% on a 10% move and
by under 1% on a 0.1% move. Loss decomposition by underlying, expiry, asset
class and strategy tag, validated against the costlier hold-one-group-flat
construction. No shipped scenario is named after a real market event.

**Phase 6 — margin.**
A named, versioned margin model — the worst loss the book takes across a
**declared** shock grid, because a margin figure is the worst loss over the
moves someone chose to look at and a reader who cannot see those moves cannot
judge the figure. Utilisation and a buffer ladder that reruns the model at every
rung, in both directions, because a short-call book is short the upside. The
output where it matters is a **region**, interpolated between the two rungs that
bracket it and reported with them, never a liquidation price.

**Phase 7 — execution.**
Transaction cost analysis on your own trade log. Six benchmarks, each of which
reports the window it covered, where its observations came from and how they
were combined — and each of which can answer *"the data you hold cannot support
this, and here is why"* instead of a price. Implementation shortfall in
currency, basis points and percent, with the side convention applied in exactly
one place so a buy above the benchmark and a sell below it read the same way. A
cost decomposition where only fees are labelled `MEASURED`, market impact is
labelled `NOT_MODELLED`, and the residual says in words what it is carrying.

**Phase 8 — execution simulation.**
TWAP, VWAP, POV and liquidity-adaptive schedules whose slices sum to the parent
quantity *exactly*, priced against a path the market already printed under a
named impact model. **No impact coefficient ships calibrated**: the default is
the identity, which makes the output the shape of the model rather than a
magnitude, and every result computed that way says so. Every simulated number is
labelled a counterfactual estimate — on the object, in the payload, first in the
warnings, and by a database CHECK that makes an unlabelled row unstorable.

**Phase 9 — advanced derivatives.**
An SSVI global surface whose at-the-money variance term structure is
non-decreasing by construction, which for SSVI *is* the no-calendar-arbitrage
condition — so an admissible fit cannot contain the violation the per-expiry SVI
surface could only report, and a database CHECK refuses to store a converged row
that does. Dupire local volatility taken analytically on that surface, with the
regions where the denominator vanishes kept as **holes carrying their reasons**
rather than interpolated over; the round trip that says it works is that the
resulting PDE reprices the surface it came from, to under 0.2%. A
Crank-Nicolson solver validated on its **order of convergence**, not its error —
which is what caught gamma converging at first order while the price looked
fine. Heston from the characteristic function, cross-checked against QuantLib to
1.5e-11 across maturities from a week to ten years, with Feller reported and only
optionally enforced because real surfaces violate it. And a model consensus that
returns a median, the range the models actually spanned, and their dispersion —
with **no `best_model` field and no field that could hold one**, because
choosing between sets of wrong assumptions is a judgement the platform is not in
a position to make.

**Phase 10 — microstructure.**
Order-book analytics, arrival intensity and a queue outlook, every one of them
behind a **data-availability gate**. A dataset is assessed once at import and
gets six capability verdicts, each granted or refused with a closed-vocabulary
reason and the evidence it was decided on; a refused capability has no endpoint
that will answer anyway and no parameter that overrides it. That is not caution
for its own sake — a volatility surface fitted to thin data is visibly
uncertain, but an order-book imbalance computed from a one-level feed is a
number between -1 and 1 that looks exactly like a real one.

The Hawkes arrival model ships only when it earns its parameters. Both it and a
constant-rate baseline are fitted on a training window and scored on a held-out
one, and the self-exciting fit is reported only when the *mean* per-event
predictive gain clears its own Newey-West standard error. The first version
compared held-out totals and adopted the richer model on seven of ten tapes with
no clustering whatsoever, by hundredths of a nat; the threshold now sits on the
difference standardised by the noise in it, and the same ten are all refused. A
database CHECK makes a row claiming the model without the statistic unstorable.

Queue position is a **bracket**, not a number — its two ends are the two
cancellation-priority assumptions a public feed cannot distinguish — and the
response says in its own words that it is not a claim about where any exchange
has placed an order. Depth snapshots and event tapes are parquet in the object
store with prices as decimals, because a stored observation is a fact and the
platform does not re-round a venue's ticks on the way to disk.

**Not shipped, on purpose:** American exercise, jump-diffusion and rough
volatility, PCA on surface changes (gated on real history), Almgren-Chriss, and
any calibrated impact coefficient. Also not shipped in microstructure: book
reconstruction from an event tape, a multivariate Hawkes process, and an
adverse-selection term in the queue model. Also not shipped: any margin model claiming
to be a broker's or an exchange's, any short-option or concentration rate —
those are venue rules, and the platform does not have them — and any reading of
the implied density as a forecast of where the underlying will go.

2,533 tests: unit, integration, quantitative validation, golden-file regression,
plus opt-in benchmarks.

---

## Five ideas the whole design rests on

**1. Observations are never overwritten by estimates.**
`market_bid` and `market_iv` are stored facts; `mid_price`, `reference_iv` and
`reference_value` are derived and versioned. There is no field that can hold
either. `Quote.mid_price` returns `None` rather than quietly falling back to the
last trade, because a silent substitution is how a risk report becomes fiction.

**2. Nothing is dropped without a reason.**
Ingestion conserves rows: `input == kept + excluded + rejected`, enforced by a
database CHECK constraint. Every excluded quote stores one primary reason plus
its full flag list; every rejected row reports its source row number and why. A
test runs this against a fixture that triggers each failure individually.

**3. A quantitative failure is a result, not a 500.**
Analytical endpoints return `{status: OK | PARTIAL | FAILED, results, warnings,
provenance}`. HTTP status describes the request; `status` describes the
calculation. A chain analysed without a settlement time returns `PARTIAL`,
solves nothing, and says exactly why — rather than inventing a midnight expiry
and returning a plausible surface.

**4. A number carries how well it is known.**
The implied-volatility solver reports its own conditioning: vega at the
solution, and the volatility moved by one unit of *price* resolution — half a
spread, or a tick for a locked market. This is not decoration. A deep
out-of-the-money weekly is worth less than a tick, so venues quote it locked at
the floor; inverting that price is numerically flawless and returns 50% against
a true 12%. A dozen such quotes moved a fitted slice by 104 volatility points
until the platform learned to say "this price pins down nothing" and drop them.

**5. A refusal is an answer.**
Historical VaR needs a factor history, and the only history this platform has is
the user's own ingestion record. Below ten aligned observations it returns
`FAILED` with the observation count rather than a number computed from four
points. Nothing is forward-filled across a gap either: a carried-forward price
is a zero return the market never had, and zero returns pull every volatility
estimate — and every VaR built on one — downward.

**6. The hardest number to produce honestly is margin, so it is the one most
hedged.** The platform does not know your broker's margin — exchange
methodologies are proprietary and change without notice — so it ships a model of
its own, names it, versions it, declares the grid it measured over, and reports
a shortfall *region* rather than a liquidation price. The short-option minimum
and concentration rates default to zero, because picking a plausible 2% would
manufacture exactly the kind of number this project exists not to produce. A
test permits the word "liquidation" in the output only when it is immediately
preceded by "not a broker".

**7. "Unavailable" is a first-class result, not an error.**
A benchmark function that can only return a price has no room for the answer
this platform most often has to give. So every benchmark returns either a price
with its window, source and method, or an explicit unavailability with a reason
— and the consequence propagates: no price means no shortfall rather than a
zero, the analysis lists what it could not compute beside what it could, and a
database CHECK refuses to store a shortfall without the benchmark it was
measured against. "No benchmark was available" and "the cost was zero" must
never render the same way.

**8. A number that never happened must be unable to lose its label.**
A simulated average price and a real one look identical in a table, and the
moment one is copied into a report without its label it becomes a claim about
what happened. So the counterfactual label sits on the result object, in the
serialised payload's own caveat, first in the envelope's warnings, and in a
`CHECK` constraint that makes an unlabelled row unstorable — not by a refactor,
not by a bulk insert, not by hand.

**9. A deviation from a model is a statement about the model.**
The anomaly scanner produces a measured difference, the scale of everything that
could account for it, and a confidence grounded in named measurements. It
produces no direction, no rating and no target — there is no such field in the
schema — and a violated no-arbitrage condition in observed quotes is reported as
what it almost always is: stale legs, non-simultaneous quotes, a wrong
multiplier.

---

## Quick start

### Docker (full stack)

```bash
cp .env.example .env
python -c "import secrets; print(secrets.token_hex(32))"   # -> QIP_SECRET_KEY
docker compose up -d --build
```

- API and docs: <http://localhost:8000/docs>
- Web: <http://localhost:3000>
- Through the proxy: <http://localhost>

### Local (no Docker, no Redis, no Postgres)

```bash
make venv
make migrate
make run
```

`QIP_JOB_EXECUTION_MODE=eager` runs jobs inline through the *same* code path a
worker uses, so the full upload → ingest → retrieve flow works on a laptop with
no infrastructure. Production startup refuses that mode.

### Try it with no market data at all

Market-data availability is this project's largest operational constraint, so a
seeded synthetic market is a first-class provider. It generates an
arbitrage-free chain from an admissible SVI slice, and it is what the committed
test fixtures are made of:

```bash
make fixtures        # regenerates tests/data/*.csv deterministically
```

Upload `tests/data/options_chain_clean.csv` at <http://localhost:3000/data>, or
`options_chain_bad_quotes.csv` to watch the quality engine explain itself. Then
open the chain and follow **Implied volatility** to solve forwards and a smile
from it — the synthetic market was generated at a 6.5% rate with a 10:00 UTC
settlement, and put-call parity will recover both from the quotes alone — then
**fit a volatility surface**, then **scan for deviations**. On the clean chain
the scanner should find nothing, which is the point; edit one quote in the CSV
and it will find that.

Then create a portfolio at <http://localhost:3000/portfolios> and import
`tests/data/portfolio_options.csv` into it. Seven of its ten rows resolve
against the contracts you just ingested; the other three are shown with the
reason each was kept out. Value it and the dashboard will tell you which legs
were marked to market and which to the fitted surface. From there, **Risk and
stress** applies a scenario and fully reprices the book — compare the answer
with the Greek approximation shown beside it, then widen the shock and watch
them come apart. **Margin** estimates the requirement over a declared shock grid
and draws the buffer curve; give it some capital and it will tell you roughly
how far the market can move before the estimated buffer goes negative, and
refuse to tell you where you would be liquidated.

Finally, upload `tests/data/trades.csv` at <http://localhost:3000/execution>.
Eight of its twelve rows become four parent orders — two the file names, two the
platform has to infer and flags as inferred — and the other four are shown with
the reason each was kept out. Analyse them and most interval benchmarks will
report themselves unavailable, because one ingested chain is one observation;
that is the honest answer, and each one says so in its own words.

From there, **Simulation** prices TWAP and VWAP schedules against the same path.
Ask for a VWAP without giving it a volume profile and it will refuse rather than
hand you a TWAP under the wrong name — which is the only thing that makes the
comparison between the two mean anything.

---

## Common commands

```bash
make test              # unit + integration + quant validation + regression
make test-quant        # numerical correctness only (no database needed)
make bench             # benchmarks, printed not asserted
make lint layering     # style + architecture rules
make golden-diff       # has any committed result drifted?
make check             # everything CI runs
```

---

## Repository layout

```
apps/            process entrypoints (api, worker, scheduler) — wiring only
api/             HTTP: routing, schemas, authz. No financial mathematics.
domains/         instruments, market_data, derivatives, portfolio, risk,
                 scenarios, execution, jobs, users, reports
quant/           pure numerics: pricing, volatility, statistics, simulation,
                 numerical, interpolation, daycount
infrastructure/  database, cache, queue, storage, observability, security
web/             Next.js frontend
tests/           unit, integration, quant_validation, regression, performance
migrations/      Alembic
docs/            architecture, methodology, references, backlog, risks
scripts/         layering check, fixture generation, golden regeneration
```

Dependency flow is one-directional and machine-checked
(`scripts/check_layering.py`):

```
apps -> api -> domains -> quant -> (nothing from this repo)
                      \-> infrastructure -> (nothing from this repo)
```

`quant/` importing nothing from the rest of the repository is what lets
`tests/quant_validation` run with no database, and a test asserts it in a fresh
interpreter.

---

## Documentation

| Document | What it covers |
| --- | --- |
| [`docs/architecture.md`](docs/architecture.md) | HLD, module graph, layering, failure handling, security |
| [`docs/methodology.md`](docs/methodology.md) | Formulas, conventions, assumptions, limitations |
| [`docs/database.md`](docs/database.md) | ERD, indexing, what does *not* go in Postgres |
| [`docs/market-data.md`](docs/market-data.md) | Provider interface, canonical schemas, quality engine |
| [`docs/instruments.md`](docs/instruments.md) | Canonical identity and resolution |
| [`docs/api.md`](docs/api.md) | API contract, current and committed |
| [`docs/sequence-diagrams.md`](docs/sequence-diagrams.md) | The five flows that define the system |
| [`docs/backlog.md`](docs/backlog.md) | Phase plan with acceptance criteria |
| [`docs/testing.md`](docs/testing.md) | Test strategy, tolerances, golden files |
| [`docs/risks.md`](docs/risks.md) | Technical and data risks, ranked |
| [`docs/references.md`](docs/references.md) | Literature, and a reuse decision per feature |
| [`docs/deployment.md`](docs/deployment.md) | Compose, migrations, hardening checklist |
| Domain designs | [volatility](docs/volatility.md) · [pricing](docs/pricing.md) · [arbitrage](docs/arbitrage.md) · [portfolio](docs/portfolio.md) · [risk](docs/risk.md) · [margin](docs/margin.md) · [execution](docs/execution.md) |

---

## On reusing other people's work

`docs/references.md` records, for every algorithm, its academic source and an
explicit decision: `USE DIRECTLY`, `WRAP`, `VALIDATE AGAINST`,
`ADAPT CONCEPTS FROM` or `IMPLEMENT INDEPENDENTLY`, with the reasoning.

The default posture for core numerics is **implement from the specification,
validate against the library**. Black-76 here is our own vectorized
implementation, cross-checked against both `vollib` and QuantLib to 1e-12 in
`tests/quant_validation/`. Two independent implementations that agree is a much
stronger statement than one wrapper. Where a library is genuinely better placed
to be authoritative — exchange calendars, Heston characteristic-function
integration — it is used or wrapped, and that choice is recorded.

---

## Disclaimer

This software produces model estimates. Model estimates are wrong in ways that
depend on their assumptions, and this platform's job is to make those
assumptions visible rather than to hide them behind a confident number. Nothing
here is investment advice, a broker margin calculation, or a guarantee of
execution outcomes. Validate everything against your own data before relying on
it.

## Licence

Apache-2.0.
