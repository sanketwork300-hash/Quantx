# Execution Intelligence

Status: _(Phase 7-8, microstructure Phase 10)_. Methodology in
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

Executions are append-only. A correction is a new row.

## 2. Import

Minimum viable fields: `timestamp, symbol, side, quantity, price`. Optional and
valuable: `order_id, parent_order, order_type, limit_price, submit_timestamp,
broker, fees`.

Instrument resolution runs with the same `resolved / ambiguous / invalid` buckets
as portfolio import. Child fills group into parents by `parent_order` when
supplied; otherwise by `(instrument, side, contiguous time window)` with the
inference explicitly flagged, because an inferred parent changes every benchmark
that follows.

## 3. Benchmarks

Arrival, decision (when supplied), prevailing mid, VWAP, TWAP, close. Each
benchmark records its **time window**, **market-data source** and **calculation
method** in the result. A VWAP without its window is not a benchmark, it is a
number.

Data coverage is checked before computing: if quotes or bars do not cover the
execution window, the result is `PARTIAL` with `TCA_DATA_COVERAGE_LOW` rather
than a benchmark computed from three ticks.

## 4. Cost decomposition

Model-based and labelled as such. `timing` is the residual after spread, impact
and fees, and the response says so, because spread, impact and timing are not
separately observable and presenting the split as measurement would be false
precision.

## 5. Execution strategies

```python
class ExecutionStrategy(ABC):
    def generate_schedule(self, order, market_context) -> Schedule: ...
```

TWAP (uniform), VWAP (historical intraday volume profile), POV (participation of
volume), liquidity-adaptive (participation modulated by spread, depth, volume and
volatility). Almgren-Chriss and Hawkes-adaptive later.

Invariant, property-tested: a schedule's slices sum to the parent quantity.

## 6. Counterfactual simulator

Inputs: historical market data, a parent order, a strategy, an impact model,
latency assumptions. Outputs: synthetic fills, average price, slippage,
implementation shortfall, completion rate, time to completion.

**Every simulated result is labelled a counterfactual estimate.** Executing the
simulated schedule would itself have changed the market it was simulated against;
the simulator cannot capture that, and says so in its output rather than in a
footnote.

## 7. Microstructure _(Phase 10, gated on data)_

Spread, depth, microprice, order-book imbalance
`OBI = (V_b - V_a) / (V_b + V_a)` and its weighted multi-level form, book slope,
depth concentration, trade and cancellation intensity.

Queue position and Hawkes intensity are gated: they require event-level
add/cancel/modify/execute data with reliable sequencing. Without it, queue
position is unknowable and the platform says so instead of estimating one. When
implemented, output is probabilistic (`expected_fill_probability`,
`estimated_wait_time`, `estimated_queue_position`) and Hawkes must beat a Poisson
baseline out-of-sample before it ships.
