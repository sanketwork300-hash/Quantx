# Research and backtesting

## 1. What real-time Phase 4 delivers

```
Warehouse dataset
      │
      ▼
POST /research/backtests            one job: features → strategy → fills → report
      │
      ├─ features    point-in-time by construction
      ├─ strategy    a target weight, never an instruction
      ├─ engine      decide on bar t, fill on bar t+1
      ├─ metrics     with every convention they depended on stated
      └─ record      everything needed to run it again
      │
      ▼
GET /research/experiments/{id}      the claim, and what supports it
```

A backtest is not a number, it is a **claim**. A claim without the dataset, the
window, the parameters, the cost schedule and the code version that produced it
cannot be checked by anyone — including whoever ran it, a month later. So the
experiment record is not bookkeeping around the engine; it is half of what the
engine is for.

## 2. Look-ahead: prevented, not avoided

Look-ahead bias makes a backtest look brilliant and be worthless, and it is
almost never introduced deliberately. It arrives through a rolling mean that
includes the bar the decision was taken on, a z-score standardised over the whole
sample, a fill priced at the same bar the signal came from.

Two structural guarantees:

**Features are a function of the past alone.** The value of any feature at index
`i` is computed from bars `0..i`, from an explicit backward slice rather than a
vectorised shift — a shift is faster and is exactly where an off-by-one hides.
The guarantee is a *property test*: computing a feature over a truncated series
must give the same value at its last bar as computing it over the whole series.
`tests/unit/test_features.py` asserts that for every feature shipped.

**A decision taken on bar `t` is filled on bar `t+1`.** At its open or its close,
both causal. There is deliberately **no "same bar's close" option**: a decision
that used bar `t`'s close cannot also be filled at it, and making it a setting
would turn the most common backtest error into a configuration choice.

And the last bar is never traded on. A signal there has no next bar to fill
against, so it is recorded and not executed — filling it at the same bar would be
a free trade at a price the decision already saw.

## 3. Costs: supplied, never invented

Indian trading carries brokerage, exchange transaction charges, SEBI turnover
fees, STT or CTT, stamp duty and GST. Every one is set by a broker, an exchange
or a regulator; several differ by segment and by side; all of them change.

**This platform does not know them and will not invent them.** Build spec 1.1
puts fee schedules alongside contract multipliers: an unknown one is declared,
not defaulted to a plausible number. A backtest run with fabricated tax rates
would produce a net return that is wrong in a way nobody could detect.

So a schedule is supplied component by component, each naming its own basis:

| Basis | Charged on |
| --- | --- |
| `TURNOVER` | a fraction of price × quantity |
| `PER_UNIT` | a fixed amount per unit traded |
| `PER_ORDER` | a fixed amount per order |
| `ON_OTHER_COMPONENTS` | a fraction of named other components — how GST on brokerage is levied |

with an optional cap, floor, and a side (`BOTH`, `BUY`, `SELL`) so that a levy
charged on the sell alone can be expressed rather than approximated.

**A run with no schedule is not a run with zero costs — it is a `gross` run**, and
it says so on the result, on the metrics, on the experiment row and in a warning.
Silently assuming free trading is the single most common way a backtest reports
returns that do not exist.

Slippage is the same shape: a stated number of adverse basis points, zero being a
legitimate choice that is labelled rather than hidden. Fills at the reference
price are not free; they are optimistic by an unmeasured amount.

## 4. Metrics that state their own conventions

A Sharpe ratio is an excess return over *some* risk-free rate, annualised by
*some* factor, from *some* number of observations. A Sharpe of 2.1 from eleven
weekly bars is not the same object as one from six years of daily bars, however
identically they print.

* **The annualisation factor is measured, not assumed.** From the median gap
  between bars — median rather than mean, because a market closure is a long
  interval that would drag a mean and change every annualised number. Assuming
  252 on a weekly series would be wrong by a factor of seven.
* **The risk-free rate is supplied or it is zero and says so.**
* **A metric that needs more data than it has returns `None`.** No CAGR from a
  six-week window: annualising it produces a number about a year nobody observed,
  and it is invariably the largest figure in the report. No Sortino where nothing
  fell. No profit factor where nothing lost — an infinite one is not a number
  worth printing beside finite ones.
* **The observation count travels with the answer.** Below thirty returns the
  ratios are reported *and* marked unreliable, the same convention the surface
  characteristics use.

## 5. Attribution that has to close

```
equity change = realised P&L + unrealised P&L − costs
```

Every term is measured; none is a residual absorbing the others; the sum is
checked, and `reconciles` is on every report. An attribution that does not add up
is a bug, and saying so is more useful than three plausible numbers.

**Slippage is reported but is not a term in that identity**, and getting this
wrong is easy — it was wrong here first. A fill is booked at the price it
actually paid, slippage included, so the price P&L already contains it.
Subtracting it again double-counts, and the residual silently absorbs the
difference. What the slippage figure answers is a different question: how much of
the price P&L was given up against the reference price, which is worth knowing
precisely because it is an assumption rather than a fee.

**Greek attribution is absent rather than zero.** Splitting an option book's P&L
into delta, gamma, theta and vega needs a repriced surface at every step, which
a bar-series backtest does not produce. A zero theta reads as "no time decay",
not as "not applicable".

## 6. Strategies are benchmarks, not recommendations

Buy-and-hold, moving-average crossover, momentum and mean reversion — the four
build spec §44 asks for. They are here because they are **known**: a buy-and-hold
backtest whose return does not match the instrument's own return over the window
has an accounting bug, and no amount of staring at a Sharpe ratio would find it.
That test is `test_its_return_matches_the_instrument_over_the_held_window`, and
it is the most valuable test in the phase.

A strategy produces a **target weight**, and the type is `TargetPosition` with
`LONG`/`SHORT`/`FLAT` — a description of a simulated book's state, not a
`BUY`/`SELL` instruction. That is not squeamishness: the platform's language
policy forbids emitting a trading signal, and the difference between "this
simulated book was 40% long here" and "buy this" is the difference between a
research result and advice.

Which is also why **nothing in this phase evaluates a strategy against today's
market.** There is no endpoint that returns a current signal. That would be the
point at which a research tool became a recommendation engine.

## 7. What is deliberately not here

**Multi-instrument portfolios.** The engine runs one instrument. Portfolio
construction across a universe — weights, constraints, an optimiser — is Phase 5
in the build plan, and doing it badly here would have to be undone there.

**Parameter sweeps and optimisation.** Running a hundred parameter sets and
reporting the best is how a backtest becomes a story about noise. When it arrives
it needs the multiple-testing correction alongside it, not after it.

**Walk-forward and purged cross-validation.** Named in the build spec for the ML
phase, and they belong with the thing they are validating.

**Live or paper execution.** Phases 6 and 7. The order interface a paper broker
would need is deliberately not stubbed here, because a stub that nothing uses is
a guess about an interface.
