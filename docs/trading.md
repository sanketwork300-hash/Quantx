# Paper trading and order management

Build spec §23, §24 and §25. The acceptance path is *signal → risk check →
paper order → fill → portfolio → P&L*, and this document is about the three
places where that path could quietly become dishonest, and what was done instead.

---

## 1. The shape

```
OrderRequest
     |
     v
+----------------+     refused: an order row with a reason on it,
|   risk gate    |---> not an error with no record
+----------------+
     | allowed
     v
+----------------+     capability check: an instruction the adapter
| BrokerAdapter  |---> cannot carry out is refused, not translated
+----------------+
     | BrokerOrderUpdate
     v
+----------------+
|   the book     |  domains/research/models.Book — the same average-cost
+----------------+  accounting a backtest uses
     |
     v
  positions, cash, realised P&L, audit trail
```

`PaperBroker` and the Phase 7 `UpstoxBroker` implement one interface. That is
build spec §23's requirement, and the only way to know it holds is to have
written the second adapter against it — which is why Phase 7 is a substitution
rather than a rewrite.

---

## 2. What a fill is allowed to rest on

A fill is the platform asserting that a trade could have happened at a price.
The evidence for that assertion is the quote. Where the quote does not support
it, the answer is no fill and a reason.

| Situation | What happens |
| --- | --- |
| Two-sided quote | Buy fills at the ask, sell at the bid. Never the mid: nobody trades at the mid. |
| No offer on the traded side | **No fill.** `NO_TWO_SIDED_MARKET`. This is `Quote.mid_price` returning `None`, applied to execution. |
| Only a last traded price | No fill under the default `QUOTE_ONLY` policy. `ALLOW_LAST_TRADE` has to be chosen on the account, and every such fill carries `LAST_TRADE_NOT_A_QUOTE`. |
| Quote older than the account's tolerance | No fill, `QUOTE_STALE`, with the age in the message. Stricter than valuation on purpose: a stale *mark* is still the best observation of a position that exists, whereas a stale *fill* asserts liquidity nobody has published since. |
| Quote reports no size on the touched side | Fills in full, flagged `DEPTH_NOT_REPORTED`. The flag is what stops a full fill being read as evidence the depth was there. |
| Size smaller than the order | Fills to the size, flagged `LIMITED_BY_DEPTH`, remainder rests. |
| Marketable limit order | Fills at the **touch**, not at its own limit. A buy limit at 260 against an ask of 250 pays 250; booking it at 260 would invent ten rupees of cost the market never charged. |
| Unmarketable day limit | Rests. Not a rejection — it is doing what it was sent to do. |
| Unmarketable IOC | Cancelled. Neither a fill nor a rejection, and `FillDecision.cancel_remainder` says so rather than leaving the caller to infer it from two absences. |

Every paper fill carries `PAPER_FILL_COUNTERFACTUAL` and the
`exchange_timestamp` of the quote it was decided against, for its whole
lifetime. There is no query that can turn a paper fill into something the
market did.

---

## 3. What the gate does, and what it refuses to do

**It refuses; it never adjusts.** An order that would breach a position limit is
rejected whole, with the limit and the number that breached it. It is not cut
down to the largest size that would have passed — that would leave the account
holding a position nobody decided on.

**Every limit is the user's.** There is no default maximum position, no assumed
price band, no inferred loss limit. Exchange price bands and broker exposure
rules are real numbers the platform does not hold, and build spec 1.1 forbids
inventing them. `max_price_deviation` is a fat-finger guard *the user declares*,
measured against the quote mid, and it says so in its own `observed` payload.

**It reports the checks it did not run.** A check with no limit behind it is
returned with `not_configured: true` rather than as a pass. The difference
between "this was checked and was fine" and "there was nothing to check against"
is the whole value of the display.

**Nothing short-circuits.** An order breaching three limits reports three
failures. The `rejection` is the first, so the branchable reason is stable, but
the full list travels beside it — a gate that stopped at the first failure would
make the user fix their limits one round-trip at a time.

The cash check, when enabled, compares the order's notional against cash and
**says in its own message that it is not a margin check**. A margin requirement
is a broker's formula, the platform holds none, and a number produced here that
looked like margin would be precisely the fabrication the rules exist to prevent.

An account with no limits at all can trade on paper and is refused on live. That
asymmetry lives in the gate, keyed on the venue: an unlimited live account is not
something anybody decides on purpose.

---

## 4. Costs

There is no default cost schedule. Brokerage, STT, GST and exchange charges are
exchange and régime rules; a plausible-looking rate invented here would silently
change every P&L computed under it.

The schedule is `domains/research/costs.CostSchedule` — the same one backtests
use, reused rather than re-specified, so a strategy's paper costs and its
backtest costs are charged by one implementation. It supports the four bases a
real contract note needs, including `ON_OTHER_COMPONENTS` for GST levied on
brokerage, and per-order caps.

With no schedule the account uses `NO_COST_MODEL`: every figure is **gross**, the
P&L response carries `gross_of_costs: true`, a warning says so, and the fills
carry `COSTS_NOT_MODELLED`. That is not a claim that trading is free — it is a
refusal to guess what it costs.

---

## 5. The book

Positions, cash and realised P&L come from `domains.research.models.Book`, the
average-cost accounting written for the backtest engine. Reusing it is not
economy: it means a strategy's paper P&L and its backtest P&L are produced by the
same arithmetic, so a disagreement between them is a difference in the *fills*
and never a difference in the accounting.

The stored position rows are a cache of the fills — a live risk view cannot
replay a year of fills per request. That optimisation is only safe while the two
agree, so the agreement is asserted:
`test_replaying_the_fills_reproduces_the_stored_position` rebuilds the book from
the fill rows and compares quantity, average price and realised P&L.

`equity` is `None` when any held position has no mark, and the unpriced
instruments are named. A total that quietly omitted a position would look like an
answer. Likewise `unrealised_pnl` is `None` rather than zero for an unmarked
position: an unmarked position is not a worthless one.

---

## 6. Idempotency, and the one thing that is left indefinite

`client_order_id` is unique per account. A resubmission with the same id returns
the order that already exists, with `replayed: true` and a warning, and places
nothing. That is what makes a retried submission safe.

The exception is a broker that could not be reached. The order is left `NEW` with
the failure recorded and the request fails, because the platform does not know
whether the order arrived. Marking it `REJECTED` would claim a definite outcome
for an indefinite one, and that is how duplicate orders get sent.

---

## 7. The audit trail

`trading_order_events` is append-only and never updated. Every submission, gate
decision, broker request, broker response, fill and state transition lands there
with the payload that caused it. "Why did this order do that" is answerable from
the database, without depending on application logs having been retained.

A refused order records `SUBMITTED`, `GATE_DECISION` and the transition to
`REJECTED`, and **no** `BROKER_REQUEST` — which is itself the evidence that
nothing reached a broker.

---

## 8. `rebalance-preview` is arithmetic, not advice

`POST /trading/accounts/{id}/rebalance-preview` takes a target the caller
supplies — typically from the portfolio optimiser, which they ran with their own
objective and constraints — and subtracts the current book from it. The output is
`RequiredTrade`, named for what it is: the difference between where the book is
and where the caller said they want it.

There is no endpoint anywhere that produces a target, and no response field
recommends anything. Quantities round down to whole units and the discarded
fraction is reported per instrument as `rounding_residual` rather than absorbed,
so the resulting weight drift is visible instead of implicit.

---

## 9. What is deliberately absent

| Not built | Why |
| --- | --- |
| Stop and stop-limit orders | A stop's trigger is an exchange behaviour. A paper broker with one would be asserting when the exchange would have fired it. |
| Order modification on paper | An amendment's effect is a queue position, and there is no queue. `PaperBroker` does not declare `MODIFY`, so the OMS refuses the instruction with a reason instead of pretending it took effect. |
| Broker positions from the paper adapter | `get_positions()` returns empty. The paper account's positions are the platform's own; returning them here would make a reconciliation compare a number against itself and always agree. |
| Margin | A broker's formula. Build spec 1.1. |
| Partial-fill probability, queue simulation, latency jitter | Each would need a model of a book the platform is not observing. The existing execution simulator (Phase 8) is the counterfactual tool and says so; a paper account pretending to be one would blur the two. |


---

## 10. Live trading

Build spec §46 Phase 7: *only after paper trading is stable*, and with
`LIVE_TRADING_ENABLED=false` by default.

Almost none of this is new code, which is the point. The OMS, the gate, the kill
switch, the book and the audit trail are the same objects as on paper; `venue` is
a field. Writing the second adapter is what established that build spec §23's
"same order interface" was true rather than merely intended — no OMS method
changed to accommodate it.

### Three gates

| Gate | Question | Default |
| --- | --- | --- |
| `live_trading_enabled` | May this *installation* trade real money? | `false` |
| `live_armed_at` | Is this *book* meant to be trading right now? | unset; an explicit act, cleared by the kill switch |
| `verified_against_documentation` | Has anybody checked this adapter against the broker's contract? | `false` |

The first two are the spec's requirement plus the observation that one flag is a
single point of failure. Arming is `POST /trading/accounts/{id}/arm`, and it
refuses unless the account is live, the deployment is enabled, the kill switch is
released, risk limits are set, a cost schedule is set, and a usable credential is
stored. It reports **every** obstacle at once: arming is done once, carefully,
and telling somebody about one obstacle at a time wastes the care.

Disarming (`DELETE .../arm`) is not the kill switch. Disarming says "not now" and
leaves resting orders alone; the kill switch says "stop" and cancels them.
Collapsing the two would mean every pause threw away the book.

### The third gate, and why it exists

The endpoints, request field names and status vocabulary in `UpstoxBroker` were
written **without reading the broker's published contract**. Build spec 1.1
forbids presenting that as verified, so the adapter says so and refuses to place
a live order until a deployment that has done the checking sets
`UpstoxOrderEndpoints.verified_against_documentation`.

The reason this gate is on the *status map* rather than only on the endpoints: a
wrong field name fails loudly — the broker returns an error and somebody fixes
it. A wrong status mapping does not. It tells the platform an order filled when
it did not, the book is then wrong, and nothing anywhere reports a problem.

Relatedly, `DEFAULT_STATUS_MAP` is not exhaustive-by-guessing. A broker state
absent from it raises `UnknownBrokerStatus` rather than being resolved to the
nearest plausible neighbour: "complete" and "cancelled" are both terminal, and
treating one as the other either loses a position or invents one. An order in an
unknown state is a position of unknown size, and stopping is the right response.

### What the adapter refuses to assume

| Payload | Read as |
| --- | --- |
| An order id and nothing else | `ACKNOWLEDGED`. Not working, not filled — the real state comes from the next poll, not from optimism. |
| `status: complete`, `filled_quantity: 0` | An error. Either the mapping or the payload is wrong and both need looking at. |
| `status: complete`, filled short of the quantity | `PARTIALLY_FILLED`. |
| `average_price` | A fill at `BROKER_REPORTED_AVERAGE` — the broker's average across however many executions it aggregated, not a price on a single trade. |
| Fill side | The broker's own `transaction_type` where present, falling back to the request only if absent. Ours is the instruction that created the order; theirs is the statement about what happened, and a fill on the wrong side inverts a position. |
| Fields the adapter does not map | Reported as `unmapped_fields`. A field that appears and is silently dropped is how a schema change becomes a silent bug. |
| HTTP 5xx | An **unknown** outcome. The order may or may not have arrived; it is left `NEW` to be reconciled, because retrying an indefinite outcome is how duplicates get sent. |
| HTTP 401/403 | A credential refusal, with "no order was placed" stated explicitly. |

Balances from `get_account()` keep their `reported_` prefix and their timestamp.
The platform computes no margin figure of its own and blends none into these:
what is there is a broker's observation, attributed, and callers can see that.

### Execution algorithms

Build spec §26 asks that the existing algorithms be connected to trading. They
are — `domains/execution/oms/algorithms.py` reuses the TWAP, VWAP, POV and
liquidity-adaptive schedulers already in `domains/execution/strategies.py`,
rather than growing a second set that would agree with the first until it did
not. What is added is only the part they never needed: turning a schedule into
orders that are placed, one slice at a time, as each slice's window arrives.

- Child order ids derive from the parent's (`parent-1-000`), so a repeated call
  across the working window places each slice exactly once through the OMS's
  existing idempotency check, with nothing new to get wrong.
- Child orders are immediate-or-cancel. One that rested past its own interval
  would still be working when the next slice arrived, and the schedule would
  deliver more than it planned.
- A slice whose window has **closed** is reported as missed, not placed late.
  Putting a whole missed interval into the market at once is the opposite of
  what a schedule is for, and the gap is the caller's to see.
- A VWAP or POV asked for without the inputs it needs is refused. Falling back to
  TWAP would answer a different question and label the answer with the name of
  the question.

None of this is called optimal execution, and the platform does not use the
phrase. A schedule is a plan for splitting a quantity the caller already decided
to trade. It does not say whether trading is a good idea, it does not choose the
size, and it is not a claim that this split beats another — that claim needs a
counterfactual, which is what the Phase 8 simulator is for and what it calls
itself.
