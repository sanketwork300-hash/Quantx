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

## Status: Phases 0 through 3 complete

Eight phases remain (4 through 11); `docs/backlog.md` has the plan and the
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

**Not shipped, on purpose:** SSVI, local volatility, PCA on surface changes
(gated on real history), portfolio, risk, margin and TCA.

1,740 tests: unit, integration, quantitative validation, golden-file regression,
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

**5. A deviation from a model is a statement about the model.**
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
domains/         instruments, market_data, derivatives, jobs, users, reports
quant/           pure numerics: pricing, volatility, statistics, numerical,
                 interpolation, daycount
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
