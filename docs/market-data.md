# Market Data — canonical schemas, providers and quality

Status: provider interface, CSV + synthetic providers, canonical models and the
quality engine are **implemented** in Phase 0.

---

## 1. The rule

> Quant engines never call a market API, a broker API or a CSV parser.

Everything enters through `MarketDataProvider`, is normalized to the canonical
schemas below, is scored by the quality engine, and is assembled into a
`MarketState` before any model sees it.

```
NSE / Binance / IBKR / Yahoo / user CSV / Parquet / synthetic
                        |
                 Provider Adapter          <- provider-specific field names live here and nowhere else
                        |
                  Validation               <- structural + domain rules, per-row reasons
                        |
                 Normalization              <- canonical types, UTC, Decimal, instrument resolution
                        |
                 Quality Engine             <- scores + flags, never silent deletion
                        |
      +-----------------+-----------------+
      v                 v                 v
  PostgreSQL       Object store        Redis (latest)
      |
      v
  MarketState  (timestamp-consistent snapshot)
```

## 2. Provider interface

```python
class MarketDataProvider(ABC):
    name: str
    capabilities: frozenset[ProviderCapability]

    async def get_instrument(self, instrument_id) -> Instrument | None: ...
    async def get_quote(self, instrument_id) -> Quote | None: ...
    async def get_option_chain(self, underlying_id, expiry=None) -> OptionChain: ...
    async def get_order_book(self, instrument_id, depth=20) -> OrderBookSnapshot | None: ...
    async def get_trades(self, instrument_id, start, end) -> Sequence[Trade]: ...
    async def get_bars(self, instrument_id, interval, start, end) -> Sequence[Bar]: ...
```

`ProviderCapability.BOOK_EVENTS` is declared separately from `ORDER_BOOK`, and
the distinction matters more than it looks: a feed that publishes periodic depth
supports the Phase 10 book analytics and cannot support a queue or an arrival-
intensity model, because the changes between two snapshots are not the messages
that caused them. Collapsing the two into one "L2" capability would let a caller
plan for an analytic the feed can never provide.

Providers declare `capabilities` rather than raising `NotImplementedError` at
call time, so a caller can plan (e.g. "this provider has no L2, skip
microstructure analytics") instead of failing mid-pipeline. Unsupported calls
raise `CapabilityNotSupported`, which the service layer converts into a
`PARTIAL` result with a named warning, never a 500.

### Shipped implementations

| Provider | Purpose | Capabilities |
| --- | --- | --- |
| `CSVMarketDataProvider` | user-uploaded / research CSV directories with an explicit column mapping | quotes, option chains, bars |
| `SyntheticMarketDataProvider` | deterministic, seeded, arbitrage-clean synthetic market for tests, demos and CI | instruments, quotes, option chains, bars |

`SyntheticMarketDataProvider` matters more than it looks: it is what keeps the
whole platform usable and testable when no market data is available at all
(build spec §8), and it gives quantitative tests a market whose true parameters
are known.

Planned: `ParquetMarketDataProvider`, `CryptoMarketDataProvider`,
`IBKRMarketDataProvider`, `NSECompatibleProvider`. Adding one must require
touching exactly one directory.

## 3. Canonical schemas

### 3.1 `Instrument`

See `docs/instruments.md`.

### 3.2 `Quote`

Observed:

| Field | Type | Notes |
| --- | --- | --- |
| `instrument_id` | UUID | resolved, never a raw symbol |
| `exchange_timestamp` | datetime (UTC) | event time as reported by the venue |
| `receive_timestamp` | datetime (UTC) | when we obtained it |
| `bid_price` / `bid_size` | Decimal / Decimal | nullable |
| `ask_price` / `ask_size` | Decimal / Decimal | nullable |
| `last_price` / `last_size` | Decimal / Decimal | nullable |
| `volume` | Decimal | session volume, nullable |
| `open_interest` | Decimal | nullable |
| `source` | str | provider name + dataset id |
| `sequence_number` | int | nullable |

Derived (computed properties, never stored as truth):

| Field | Definition |
| --- | --- |
| `mid_price` | `(bid + ask) / 2` when both sides are present and positive, else `None` |
| `spread` | `ask - bid` |
| `relative_spread` | `spread / mid` |
| `microprice` | `(bid*ask_size + ask*bid_size) / (bid_size + ask_size)` — weighted by the *opposite* side's size, so a large resting bid pulls it toward the ask |
| `quote_age(now)` | `now - exchange_timestamp` |

`mid_price` returns `None` rather than falling back to `last_price`. A silent
fallback is exactly the kind of substitution of estimate for observation that
§1.2 forbids; callers that want a fallback must ask for it explicitly and the
choice is recorded as a flag.

### 3.3 `OptionQuote`

`Quote` plus the option context needed for downstream work without a second
lookup: `underlying_id`, `expiry`, `strike`, `option_type`, `underlying_price`,
`underlying_source`. Derived: `time_to_expiry` (year fraction under a stated day
count), `log_moneyness` once a forward exists, `intrinsic_value`.

### 3.4 `Bar`

`instrument_id`, `interval`, `start_timestamp`, `end_timestamp`, `open`, `high`,
`low`, `close`, `volume`, `vwap` (nullable — stored only if the venue publishes
it; we do not silently reconstruct a VWAP and call it observed), `trade_count`.

### 3.5 `OrderBookSnapshot`

`instrument_id`, `exchange_timestamp`, `receive_timestamp`, `bids[]`, `asks[]`,
`sequence_number`, `source`; each level is `(price, quantity, order_count?)`.
Levels are ordered best-first and validated for monotonicity on ingest.

Storage **(implemented in Phase 10)**: parquet in the object store, one row per
snapshot with the levels as list columns rather than a fixed
`bid_px_1 ... bid_px_20` width — depth genuinely varies snapshot to snapshot,
and a padded level is indistinguishable from a level quoted at zero. Prices and
quantities are `decimal128(38, 12)`, so a venue's ticks are not re-rounded on
the way to disk. One PostgreSQL row per book update is explicitly rejected as a
design.

### 3.6 `BookEvent`

`instrument_id`, `exchange_timestamp`, `event_type` (ADD / CANCEL / MODIFY /
TRADE), `side`, `price`, `quantity`, `sequence_number`, `order_id`, `source`.

Note what is *not* on it: no queue position, no inferred aggressor, no
reconstructed book state. Each of those is a derivation, and derivations live
beside the observation rather than inside it.

`event_type` is never inferred. A feed that does not label its messages is
reported as unlabelled rather than classified by a heuristic on price and size —
a cancellation counted as a trade drains a queue that never drained.
`MODIFY` is kept distinct from an ADD/CANCEL pair because a venue that publishes
it usually preserves queue priority for a size reduction and loses it for an
increase, and collapsing the two would silently assert one of those.

`sequence_number` is nullable because plenty of exported tapes drop it, and its
absence is a fact the Phase 10 availability gate reads: without sequencing there
is no way to know whether the tape is complete, and a queue model computed on a
tape with a hole in it is a queue model of a different book.

### 3.7 `Trade`

`instrument_id`, `exchange_timestamp`, `price`, `quantity`, `aggressor_side`
(nullable — many feeds do not publish it and inferring it is a model, not an
observation), `trade_id`, `source`.

## 4. Data quality engine

The quality engine answers "how much should a model trust this?" and is
available to every downstream consumer through `MarketState.quality`.

### Checks

Structural: missing bid, missing ask, zero/negative bid, zero/negative ask,
missing last, missing timestamp, inconsistent timestamp (receive < exchange),
duplicate message, missing sequence number.

Market-consistency: crossed market (`bid > ask`), locked market (`bid == ask`),
abnormal spread (relative spread beyond a per-asset-class threshold), extreme
price jump versus the previous accepted observation, stale quote.

Option-specific: non-positive time to expiry, non-positive strike, price below
intrinsic, price above the no-arbitrage upper bound, put/call bound violation,
zero bid on a deep-ITM contract, unreliable open interest, illiquid contract
(volume and OI both below floor), missing depth.

### Scores

Five sub-scores in `[0, 1]`, 1 = good:

| Score | Driven by |
| --- | --- |
| `stale_score` | quote age relative to an asset-class half-life |
| `spread_score` | relative spread against an asset-class reference spread |
| `liquidity_score` | volume, open interest, quoted size |
| `consistency_score` | crossed/locked/bounds/jump violations |
| `completeness_score` | fraction of expected fields present |

`overall_score` is a **weighted minimum-biased blend**: the geometric mean of the
sub-scores, so one catastrophic dimension cannot be averaged away by four good
ones. Weights are configuration, recorded in provenance, not magic constants
buried in code.

### Flags and exclusion

Every check that fires produces a `QualityFlag { code, severity, message,
context }`. Severity is `INFO | WARNING | ERROR`.

Exclusion policy, per build spec §12 and §15:

- The engine **never deletes** an observation. It marks it.
- The ingestion pipeline decides `excluded = True` with a **single primary
  `exclusion_reason`** plus the full flag list.
- Both kept and excluded rows are persisted and both are returned by the API.
- Only `ERROR`-severity flags cause exclusion by default; the threshold is a
  parameter of the ingestion request and is recorded in provenance.

This is why the Phase 1 acceptance criterion "every excluded quote has a reason"
is checkable mechanically: the reason is a NOT NULL column whenever
`excluded` is true, and a test asserts it.

## 5. MarketState assembly

`MarketStateBuilder` collects quotes, spots, futures, curves, FX and surfaces for
a requested instrument universe at a requested `as_of`, hashes the normalized
inputs into a content-addressed `state_id`, and freezes the result.

Rules:

- A quote is admitted to a snapshot only if `exchange_timestamp <= as_of`.
- The builder records, per instrument, the age of the admitted quote. It does
  not reject stale data; it labels it, because refusing to value a portfolio
  because one leg is stale is worse than valuing it with a visible warning.
- The same `as_of` and the same inputs always produce the same `state_id`.

## 6. Provider-specific fields

Provider payload fields that do not map to the canonical schema are preserved in
`metadata` on the canonical object and are **never** promoted into domain models
or read by `quant/`. If a provider field turns out to be genuinely needed by a
model, the correct response is to extend the canonical schema and every adapter,
not to reach into `metadata` from a pricing function.
