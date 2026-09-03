# Execution Intelligence

Status: **transaction cost analysis and simulation implemented** (Phases 7-8).
Microstructure is Phase 10, and is implemented; see §7 below and
`docs/methodology.md` §18a. Methodology for this document's own subject is in
`docs/methodology.md` §§15-16.

---

## 1. Domain

```
Order        intent: instrument, side, quantity, order_type, limit_price, timestamps
ParentOrder  a trading decision
ChildOrder   a slice of a parent
Execution    a fill: id, user_id, instrument_id, order_id, parent_order_id,
             side, quantity, execution_price, exchange_timestamp,
             receive_timestamp, order_type, broker, fees, metadata
```

Executions are append-only. A correction is a new row. There is no update path
in the repository and no correction column: a trade log that can be quietly
rewritten cannot support a cost analysis anyone should act on.

Quantity is **always positive**; direction lives in `side`. That is the opposite
convention to `positions`, deliberately: a position's sign says which way you
are exposed, while a fill's side says what you did. Collapsing the two would
make `Side.sign` — applied in exactly one place, so that paying above the
benchmark on a buy and receiving below it on a sell read the same way — into a
second sign convention that could drift from the first.

## 2. Import

Minimum viable fields: `timestamp, symbol, side, quantity, price`. Optional and
valuable: `order_id, parent_order, order_type, limit_price, submit_timestamp,
broker, fees`.

Instrument resolution runs with the same `resolved / ambiguous / invalid` buckets
as portfolio import, and the same refusal: **no ambiguous row is ever
auto-resolved**. Here the consequence is worse than a mis-stated holding — a fill
attributed to the wrong contract lands in the wrong parent order and drags a
benchmark window with it, so every cost computed afterwards is wrong with nothing
to show for it.

Child fills group into parents by `parent_order` when supplied. Otherwise they
group by `(instrument, side, contiguous time window)` with the inference
explicitly flagged and the gap that produced it recorded, because a different gap
produces a different set of parents, different windows and different benchmarks.
Two fills on opposite sides never join one parent: that is two decisions, and
benchmarking them as one would be meaningless.

A fill timestamped **before its own submission** is rejected rather than
reconciled. One of the two timestamps is wrong, guessing which would corrupt
every benchmark for that order, and a database CHECK refuses the row as well.

## 3. Benchmarks

Each benchmark records its **time window**, **market-data source** and
**calculation method**. A VWAP without its window is not a benchmark, it is a
number.

| Benchmark | Method | Needs |
| --- | --- | --- |
| `ARRIVAL` | prevailing mid at the submit timestamp | a submit timestamp and an observation at or before it |
| `DECISION` | prevailing mid at the decision timestamp, or a price the caller supplies | a decision timestamp, which only the trade log can carry |
| `PREVAILING_MID` | quantity-weighted mid across the fills | an observation at or before each fill |
| `INTERVAL_TWAP` | piecewise-constant in time: each observation holds until the next | enough observations, spanning enough of the window |
| `INTERVAL_VWAP` | volume-weighted across the window | **interval** volume, which is not the same as session volume |
| `CLOSE` | the last observation at or after the window end | an observation after the last fill |

The window a parent order is measured over runs from **submission**, not from
the first fill: the delay before trading started is a real part of the cost.

### The arrival proxy

With no submit timestamp there is nothing to look up, so the first fill's own
price stands in and the result carries `ARRIVAL_PROXY_USED`. That flag is load
bearing — a shortfall measured against the first fill is systematically smaller
than one measured against the price before trading started, and a test asserts
the inequality.

### Data coverage

Checked before computing, and reported with every window:

| Threshold | Value | Why |
| --- | --- | --- |
| `MIN_INTERVAL_OBSERVATIONS` | 4 | below this a "mean over the interval" is a handful of moments inside one |
| `MIN_SPAN_RATIO` | 0.60 | four observations clustered in one corner describe the corner, not the interval |

Below either, the interval benchmarks report themselves **unavailable with a
reason** and no shortfall is computed against them. That is different from a
shortfall of zero, and the two never render the same way: a missing benchmark
produces a missing shortfall, and a database CHECK
(`ck_report_shortfall_needs_benchmark`) refuses to store one without the other.

### Why the interval VWAP is usually unavailable

It needs to know how much traded *between* one observation and the next. The
platform's option quotes carry cumulative session volume, and attributing that
to the moment of the snapshot would weight a whole day onto one instant. So the
benchmark reports itself unavailable and says why, rather than quietly becoming
a time-weighted average wearing a volume-weighted name.

Where the observations come from: the option quotes stored with each ingested
chain, falling back to the underlying level recorded on the snapshot. The first
carries a two-sided market, so a spread travels with each observation and a
spread charge is attributable; the second does not, and the decomposition says
so instead of assuming one.

## 4. Cost decomposition

Model-based and labelled as such, component by component:

| Component | Status | What it is |
| --- | --- | --- |
| `spread` | `MODELLED` | half the quoted spread at each fill, weighted by that fill's quantity, with the number of covered fills stated |
| `fees` | `MEASURED` | as recorded on the fills — the only observation in the table |
| `impact` | `NOT_MODELLED` | Phase 8. It is not zero; it is inside the residual, and a number here would be an invention |
| `timing_residual` | `RESIDUAL` | what the total leaves after spread and fees, carrying impact, adverse selection and genuine drift together |
| `opportunity` | `MODELLED` or `UNAVAILABLE` | unfilled quantity valued at the later move, and unavailable when the log did not state the order's intended quantity |

Spread, impact and timing are not separately observable, and the response says
so in its own `caveat` field. The unfilled quantity is **never inferred** from
the fills: assuming an order filled completely because the log only shows fills
is how an unfilled order silently reports no opportunity cost at all.

## 5. Execution strategies

```python
class ExecutionStrategy(ABC):
    def generate_schedule(self, quantity, side, context) -> Schedule: ...
```

| Strategy | Needs from the caller |
| --- | --- |
| `TWAP` | nothing but the interval count |
| `VWAP` | an expected-volume profile that actually varies |
| `POV` | an expected-volume profile, a rate, and enough capacity in the window |
| `LiquidityAdaptive` | an expected-volume profile plus a per-interval spread or volatility that varies |

Almgren-Chriss and Hawkes-adaptive remain later work.

**The platform supplies none of those inputs.** It holds no intraday volume
profile of its own, so a strategy that needs one and does not get it raises
`StrategyUnavailable` with a reason. That refusal is the same rule as Phase 7's
unavailable benchmark, applied one layer up:

- VWAP on a *flat* profile is TWAP. Returning it under the VWAP name would make
  any comparison between the two meaningless, so it refuses.
- `LiquidityAdaptive` with flat spread and flat volatility is VWAP, and running
  it under this name would credit inputs that did nothing.
- POV at a rate the window cannot absorb refuses rather than truncating the
  order or quietly raising the rate.

When the whole order fits inside the window, POV's allocation *is* VWAP's — both
are proportional to expected volume — so the two schedules coincide, and the POV
schedule says so in its own assumptions rather than leaving a reader to wonder
why two rows are identical. They diverge in practice only through the
re-planning against realised volume that this ex-ante schedule does not
simulate.

### Slices sum to the parent quantity, exactly

Cumulative floors give each slice its share in whole lots; the lots left over go
one at a time to the largest fractional parts. Dumping the remainder on the last
slice would also sum correctly and would distort that slice; both matter, so
neither is traded away. `Schedule.__post_init__` raises if the sum is ever
wrong, and a hypothesis property covers arbitrary weights, totals and lot sizes.

## 5a. Market impact

```
square root:  permanent = eta   * sigma * sqrt(Q / ADV)
              temporary = gamma * sigma * sqrt(participation)
linear:       permanent = eta   * sigma * (Q / ADV)
              temporary = gamma * sigma * participation
zero:         nothing at all, for isolating the schedule from the model
```

The functional forms are from the literature and are uncontroversial. **The
coefficients are not**, and this platform does not ship one: they are regime-,
venue- and period-dependent, and the published estimates come from markets
nobody here has observed.

So `eta` and `gamma` default to `1.0` — not because one is right, but because it
is the identity, and it makes the output read as the *shape* of the model in
units of `sigma * sqrt(Q/ADV)` rather than as a magnitude the platform is
claiming. Every result computed that way carries
`IMPACT_COEFFICIENT_NOT_CALIBRATED`, and supplying coefficients fitted to your
own executions clears it.

Permanent and temporary impact are separate fields because the simulator needs
them separately: permanent impact moves the reference price for every later
slice, temporary impact is paid on the slice and does not persist. Reporting
only the total would make the two indistinguishable and the simulation wrong in
a way nothing would reveal. The temporary term uses the **participation rate**
rather than total size, because that is the quantity a trader controls by
choosing a schedule — trading the same order more slowly lowers it, and the
permanent term does not move.

## 6. Counterfactual simulator

Inputs: a market window from the same source Phase 7 benchmarks against, a
parent order, one or more strategies, an impact model, a latency and a maximum
price age. Outputs: simulated fills, average price, implementation shortfall,
completion rate and time to completion.

Each slice is priced as

    fill = observed_mid + accumulated_permanent
           + side_sign * (temporary_impact + half_spread)

with permanent impact accumulating into the reference for every later slice.

**Every simulated result is labelled a counterfactual estimate.** Executing the
simulated schedule would itself have changed the market it was simulated
against: the observed path already contains whatever the real order did, and it
does not contain what this hypothetical one would have done. That label is on
the result, in the payload's own `caveat`, on the envelope's first warning, and
in a database CHECK — `ck_simulation_is_always_counterfactual` — so an
unlabelled row cannot be stored by a refactor, a bulk insert or by hand.

### A stale price does not fill a hypothetical slice

A slice whose nearest observation is older than `max_price_age_seconds` is left
**unfilled**, and the completion rate says so. That is the opposite of what a
portfolio valuation does with a stale quote, deliberately: a stale mark is still
the best observation of a position that exists, whereas filling a hypothetical
slice against a price from hours ago asserts liquidity nobody saw. The tolerance
is a declared parameter, recorded on every stored run.

### The simulated fills are scored by the same ruler as real ones

They are turned into `Execution` objects, grouped into a `ParentOrder`, and run
through the Phase 7 benchmark set and shortfall calculation unchanged. A
counterfactual and the execution it is compared against are never measured by
two different pieces of code.

They are **not** written to `executions`. The trade log is what happened.

### A comparison is not a ranking

Several schedules on one path under one impact model, side by side. There is no
`best_strategy` field, no ranking, and no recommendation — a test asserts no such
key exists anywhere in the payload, and that the word "recommended" appears only
inside the sentence that refuses to make one. The comparison also states that
the differences between strategies are smaller than the uncertainty in an
uncalibrated impact coefficient.

## 7. Microstructure _(Phase 10 — implemented, gated on data)_

Spread, depth, microprice, order-book imbalance
`OBI = (V_b - V_a) / (V_b + V_a)` and its weighted multi-level form, book slope,
depth concentration, cost to trade the displayed book, and trade and
cancellation intensity. Definitions are in `docs/methodology.md` §18a; the
conventions that have more than one form in circulation are written out there
rather than implied.

The gating is real and it is checked once, at import, rather than being a note
in the documentation. A dataset gets six capabilities, each granted or refused
with a closed-vocabulary reason and the evidence the verdict was taken on, and a
refused capability has no endpoint that will answer anyway. A snapshot-only
export supports the book analytics above and cannot support anything that needs
the messages between the snapshots — the changes a snapshot series implies are
not the messages that caused them, and this platform does not reconstruct a book
from a tape.

Queue position ships as a **bracket**, not a number. Its two ends are the two
cancellation-priority assumptions a public feed cannot distinguish, and the
response says in its own words that it is not a claim about where any exchange
has placed an order. It requires priced, sided events with a complete monotone
sequence: a tape with a hole in it describes a different book, and nothing in a
queue number would say so.

Hawkes had to beat a Poisson baseline out of sample before it shipped, and it
still has to on every dataset. Both models are fitted on a training window and
scored on a held-out one, and the self-exciting fit is reported only when the
*mean* per-event predictive gain clears its own Newey-West standard error. On
genuinely Poisson arrivals the raw held-out total favours it about half the
time by hundredths of a nat, which is why the test is on the mean rather than
the total.
