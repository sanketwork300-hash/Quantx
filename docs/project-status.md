# Project status and design notes

What each phase shipped, what was left out on purpose, and the ideas the design
rests on. The [README](../README.md) is the short version; this is the long one.

## What has shipped

All eleven analytical phases in `docs/backlog.md` have shipped, each as a
vertical slice — data model, service, API, tests, docs and UI — and each gated
on the previous one's acceptance criteria passing in CI. The backlog records
what every phase claimed and the test that carries the claim.

On top of those sit two further slices: a **broker credential vault**, so a
provider token is granted by a user rather than pasted into an environment file,
and **real-time Phase 1**, which brings a live feed in through the same
`MarketDataProvider` interface and out as the same `MarketState` every engine
already consumes.

**Phase 0 — foundation.**
Repository skeleton with layering rules **enforced in CI**; Docker Compose
stack; settings, structured logging with correlation ids, async SQLAlchemy,
reversible Alembic migrations (optionally TimescaleDB), Redis cache, object
store, Celery with an inline "eager" mode. Users with bcrypt, JWT, ownership
checks and an audit log. Instruments with canonical keys, deterministic `uuid5`
identity and a resolver where `AMBIGUOUS` is a first-class outcome. Market data:
the provider interface, CSV and seeded-synthetic providers, canonical quote
schemas, the **data-quality engine**, and the **option-chain ingestion
pipeline** -- which reads both a long-form file and the **two-sided layout**
every exchange chain export uses, where calls sit left of the strike and puts
right of it under the same repeated header names. A file uploaded with nothing
said about it is read as it is actually arranged, and what comes back is the
*reading*: which column every field was taken from, whether that was detected or
supplied, and the file's first rows with the ones it could not read kept in
rather than quietly omitted. Correcting a column is there for when the reading
is wrong, not as the price of entry. A file that could not be read is refused
outright, because a snapshot holding four quotes out of forty thousand is
indistinguishable downstream from a market with four quotes in it. Asynchronous
jobs.

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

**Phase 11 — unified order analysis.**
The point of the previous ten. One proposed order, five engines — what the
market shows and what the models say around it, whether the contract's implied
volatility is out of line with the surface, what it is estimated to cost to
execute, and what it does to the book's Greeks, VaR, stress loss and estimated
margin — all computed from **one `MarketState`**, whose id appears in all five
provenance blocks and is asserted by a test. That is the whole design: the
current-to-proposed differences a user reads are attributable to their order and
not to five calculations catching the market at five moments.

Branches degrade independently and are never dropped: one that cannot answer is
`FAILED` with its reason and the envelope is `PARTIAL`. An order that cannot be
repriced is **refused rather than reported as a difference of zero**, which is
the single worst sentence this endpoint could produce — it would read as an
order that adds no risk. With no average daily volume the impact half of the
cost estimate is absent rather than zero, and the spread half, measured off an
observed quote, is still reported. Whether a resting limit order fills is not
modelled at all; the order is only classified against the touch it would have to
cross.

**There is no recommendation field, and nowhere for one to go.** No action, no
signal, no rating, no score, no ranking of the execution schedules, and no
column in the stored table one could live in. Three tests enforce it: over every
key of a live response, over the published OpenAPI schema, and over the whole
serialised payload for forbidden phrasing.

**Not shipped, on purpose:** American exercise, jump-diffusion and rough
volatility, PCA on surface changes (gated on real history), Almgren-Chriss, and
any calibrated impact coefficient. Also not shipped in order analysis: a
fill-probability model for a passive limit order, and any aggregation of the
five branches into a single figure — the second is the recommendation field
under another name. Also not shipped in microstructure: book
reconstruction from an event tape, a multivariate Hawkes process, and an
adverse-selection term in the queue model. Also not shipped: any margin model claiming
to be a broker's or an exchange's, any short-option or concentration rate —
those are venue rules, and the platform does not have them — and any reading of
the implied density as a forecast of where the underlying will go.

3,355 tests: unit, integration, quantitative validation, golden-file regression,
plus opt-in benchmarks.

---

### Groundwork — broker credentials without an environment variable

Not a numbered phase, and done before the real-time provider work depends on it.
A broker access token is a bearer credential for someone's brokerage account, so
it is no longer configuration: each user grants their own through the provider's
own sign-in, it is sealed with **AES-256-GCM** and bound to the row that holds
it, and it is renewed without anyone being asked wherever the provider allows
that. What stays in `.env` is the app registration and the encryption key —
neither of which rotates on the provider's schedule.

An expiry is recorded **only when the provider states one**. A credential with no
declared lifetime is used until the provider refuses it, and the refusal is what
retires it; assuming a lifetime nobody published either interrupts users holding
working credentials or reports dead ones as live, and the second failure shows up
as market data that is quietly missing. With no encryption key configured the
platform declines to store a credential at all rather than storing one it cannot
protect. See [`docs/credentials.md`](credentials.md).

### Real-time Phase 1 — live market data

A live provider behind the existing `MarketDataProvider` interface, an
instrument master that joins the provider's identifiers to canonical ones, a
feed worker in its own process, and a `MarketState` assembled from what the feed
delivered — so a live price reaches a pricing model the same way a chain
uploaded from a CSV does.

Three things it refuses to do. It never falls back to the **synthetic market**:
a provider that cannot be built raises rather than substituting one, and the
synthetic market will not be constructed at all in a production-like
environment. It never **invents a field**: the
payload-to-schema mapping is versioned data, and every read reports which fields
it found, which mapped paths were absent and which payload keys nothing claims —
so a provider renaming `last_price` is visible on the first response rather than
as quotes that quietly become empty. And it ships **no decoder for a provider's
binary wire format**; the protobuf decoder loads a module generated from the
provider's own `.proto` and refuses to start without one, because plausible
numbers from a format nobody checked is the worst failure a market-data system
has. See [`docs/live-market-data.md`](live-market-data.md).

### Real-time Phase 2 — live options intelligence

A live option chain reaching the volatility machinery that was already there. It
adds no second IV solver and no second SVI fit: the capture writes into the same
`option_chain_snapshots` row a CSV upload produces, and the Phase 1, 2, 3 and 9
engines run on it unchanged — which is what makes a live surface refittable six
months later on exactly the terms a historical one is.

Two genuinely new pieces of arithmetic. **Delta-quoted skew** — 25Δ and 10Δ risk
reversal and butterfly — because `dsigma/dk` is the right coordinate for
arbitrage and the wrong one for comparison with a broker's runs; the delta
convention rides on every result, a strike outside the fitted range is labelled
`EXTRAPOLATED`, and a wing that could not be found gives `null` rather than
zero. And **open interest**: sums, put-call ratios and change between snapshots,
reported as measurements with no reading of them attached — a missing figure is
never a zero, a zero denominator gives `null` rather than infinity, and the
venue's open-interest unit is labelled rather than normalised. "Max pain" is
deliberately absent. See [`docs/live-options.md`](live-options.md).

### Real-time Phase 3 — historical warehouse

Partitioned Parquet in the object store, a registry in PostgreSQL, DuckDB over
the top. The layout is Hive-style — `exchange=NSE/year=2026/month=03/day=02` —
so a query for one week reads that week's files and no others, and the response
reports how many partitions actually contributed rows so a broken prune fails a
test rather than merely being slow.

The validator's rule is the platform's oldest one: **found, reported, never
repaired.** Rows in equals rows written plus excluded plus rejected; a bad tick
is flagged and kept, with the flag as a column in the file; a naive timestamp is
refused rather than read as UTC, because a year of NSE bars read that way is a
year shifted by five and a half hours. A jump that looks like a 1:5 split is
detected and the dataset is **quarantined** — the platform holds no
corporate-action feed, so it will not adjust the series and will not pretend to,
and an unflagged split reads as an -80% return that a backtest has no way of
questioning. Gaps are reported as shared across every instrument, or not, because
that is derivable from the data; naming which absent dates are holidays is not.
See [`docs/warehouse.md`](warehouse.md).

### Real-time Phase 4 — research and backtesting

Point-in-time features, four benchmark strategies, an event-driven engine, the
metrics, an attribution that closes, and a record of every run.

Three refusals shape it. **No feature can see the future** — the guarantee is a
property test, not a comment: computing a feature over a truncated series must
give the same value at its last bar as over the whole series, asserted for every
feature shipped. **A decision on bar `t` is filled on bar `t+1`**, and there is no
"same bar's close" option, because making the most common backtest error a
setting means somebody will set it. **Trading costs are supplied, never
invented** — brokerage, STT and GST are set by brokers, exchanges and regulators,
and a net return computed from fabricated rates would be wrong in a way nobody
could detect; a run without a schedule is *gross* and says so on every figure.

The attribution has to close: `equity change = realised + unrealised − costs`,
with the residual published. Slippage is reported but not subtracted, because it
is already inside the fill prices — that one was wrong here first. And nothing in
the phase evaluates a strategy against today's market, which is the point at
which a research tool becomes a recommendation engine. See
[`docs/research.md`](research.md).

### Real-time Phase 6 — paper trading

An order lifecycle end to end, with the broker behind an interface rather than a
special case in the middle of the service. `PaperBroker` and the live adapter
implement the same six methods, which is build spec §23's requirement and the
only reason Phase 7 is a substitution rather than a rewrite.

The rule that shapes it is that a fill is an *assertion* that a trade could have
happened at a price, and the evidence for it is the quote. A market order with
no offer on its side does not fill; it is refused with `NO_TWO_SIDED_MARKET`,
because filling at the last trade would be `Quote.mid_price` falling back to the
last trade all over again — this time with a position to show for it. Filling
against a print is a policy that has to be chosen, and every such fill says so
for the rest of its life.

The gate refuses and never resizes: an order over a position limit is rejected
whole, with the limit and the number that breached it, because an order trimmed
to fit is one nobody sent. It reports the checks it *did not* run, since "there
was no limit to check against" and "this was checked and was fine" are different
answers. And the book is the existing backtest accounting, so a strategy's paper
P&L and its backtest P&L come from one implementation — asserted by replaying the
stored fills and comparing. See [`docs/trading.md`](trading.md).

### Real-time Phase 7 — live trading

Almost no new code, which is the point: the OMS, gate, kill switch, book and
audit trail are the same objects as on paper, and `venue` is a field. Writing the
second broker adapter is what established that "paper uses the same interface as
live" was true rather than merely intended — no OMS method changed to take it.

Three independent gates gate a live order: the deployment flag (`false` by
default), the account's arming, and the adapter's own
`verified_against_documentation`. The third is the one this phase added on its own
account. The endpoints and status vocabulary in the Upstox adapter were written
without reading the broker's published contract, and a wrong field name fails
loudly whereas a wrong *status* mapping does not — it tells the platform an order
filled when it did not, and the book is wrong with nothing reporting a problem.
So the adapter is complete, wired and tested against recorded payloads, and it
refuses to send a live order until somebody who has checked sets the flag. In the
same spirit, a broker status the map does not contain raises rather than being
rounded to a neighbour: "complete" and "cancelled" are both terminal, and
confusing them either loses a position or invents one. See
[`docs/trading.md`](trading.md).

## The ideas the whole design rests on

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

## On reusing other people's work

[`references.md`](references.md) records, for every algorithm, its academic source and an
explicit decision: `USE DIRECTLY`, `WRAP`, `VALIDATE AGAINST`,
`ADAPT CONCEPTS FROM` or `IMPLEMENT INDEPENDENTLY`, with the reasoning.

The default posture for core numerics is **implement from the specification,
validate against the library**. Black-76 here is our own vectorized
implementation, cross-checked against both `vollib` and QuantLib to 1e-12 in
`tests/quant_validation/`. Two independent implementations that agree is a much
stronger statement than one wrapper. Where a library is genuinely better placed
to be authoritative — exchange calendars, Heston characteristic-function
integration — it is used or wrapped, and that choice is recorded.
