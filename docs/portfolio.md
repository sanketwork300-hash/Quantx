# Portfolio Domain

Status: **implemented** (Phase 4).

---

## 1. Model

```
Portfolio: id, user_id, name, base_currency, created_at, updated_at
Position:  id, portfolio_id, instrument_id, quantity (signed Decimal),
           average_price, side, source, metadata
```

Quantity is **signed**; `side` is retained as supplied by the source for audit
and reconciliation, and a mismatch between sign and side is a validation error,
not something to silently reconcile.

Supported asset classes follow instrument support: equities, futures, vanilla
options, crypto spot/perpetual, FX where data exists.

## 2. Import

Manual entry and CSV upload with flexible column mapping
(`symbol, exchange, asset_type, quantity, average_price, expiry, strike,
option_type`).

Mandatory preview before commit. The import returns three buckets:

```
resolved[]   -> ready to commit
ambiguous[]  -> multiple candidate instruments; the user must choose
invalid[]    -> failed validation, with the reason
```

**No ambiguous row is ever auto-resolved.** Picking the "most likely" contract is
how a portfolio silently ends up with the wrong expiry and every downstream
number becomes wrong without any error appearing.

## 3. Valuation

```
Portfolio -> Positions -> Instrument resolution -> MarketState
          -> Position valuation -> Currency conversion -> Aggregation
```

One `MarketState` covers every underlying the portfolio touches, assembled by
`domains/reports/composition.py::ValuationContextComposer`. Each underlying's
latest chain arrives with its own timestamp, so the common `as_of` is the
*latest* of them: taking the earliest would place some quotes after `as_of` and
the builder would reject them, while taking the latest keeps every quote and
lets the older ones be measured as stale. A delta and a vega in the same report
therefore cannot come from different minutes.

Prices are chosen in this order, and the choice is recorded, never inferred:

| Order | Source | `valuation_method` |
| --- | --- | --- |
| 1 | Observed two-sided mid, fresher than 15 minutes | `MARKET_MID` |
| 2 | Observed two-sided mid, older than that | `STALE_MARKET` |
| 3 | Last traded price, when there is no two-sided market | `MARKET_LAST` |
| 4 | Reference price from the fitted surface | `MODEL_REFERENCE` |
| 5 | Nothing usable | `UNAVAILABLE` |

A stale quote is used and flagged rather than discarded: an old price is
information, silence is not. An `UNAVAILABLE` position contributes nothing to
the totals and is listed with its reason rather than counted as zero.

Greeks come from Black-Scholes-Merton at the volatility implied by the
contract's own observed price where there is one (`greek_source: MARKET_IV`,
because that volatility reprices the observation exactly), and from the surface
otherwise (`REFERENCE_IV`). Per-unit Greeks are multiplied by signed quantity,
the contract multiplier and the FX rate in exactly one place, so the scaling
happens once. Without a settlement time, time to expiry is undefined and option
Greeks are omitted rather than computed against a guessed moment.

Per position, a stored snapshot records `market_price`, `model_price`,
`market_value`, `unrealized_pnl`, the Greeks, `valuation_method` and provenance.
`market_price` and `model_price` are separate columns and neither is ever
written from the other.

`valuation_method` values: `MARKET_MID`, `MARKET_LAST`, `MODEL_REFERENCE`,
`STALE_MARKET`, `UNAVAILABLE`. The user can always see which positions were
marked to market and which were marked to model.

## 4. Aggregation

By portfolio, underlying, expiry, asset class, strategy tag and currency. Greeks
aggregate as `q * M * unit_greek` with units stated (`docs/methodology.md` §2.3).

Every dimension sums the *same* per-position numbers, so each grouping totals to
the portfolio total. A position that does not carry a dimension is absent from
it rather than bucketed under a fabricated key: the index leg of a book has no
expiry, so the expiry grouping covers fewer positions than the portfolio and
says so through each bucket's `positions` and `valued` counts.

Cross-currency aggregation converts at the FX rates in the `MarketState`, and the
rate used is recorded on each position; it is not fetched separately at display
time. A position in a currency with no rate in the snapshot is left unvalued
with `POSITION_NO_FX_RATE` rather than converted at a rate from another moment.

Property test: the sum over positions equals the portfolio total within
tolerance, for value and for every Greek
(`tests/unit/test_portfolio_valuation.py::TestSumProperty`).

## 5. Provenance

Every stored valuation records the market-state id and timestamp, the data
sources and dataset digests behind it, the surface ids used, the model versions
of both the valuation service and the pricing engine, the risk-free rate,
dividend yield, day-count convention and staleness threshold it ran under. A
historical valuation can be recomputed from it; a valuation without it would be
a number with no way to check it.

## 6. What is deliberately absent

There is no field that can hold either an observation or an estimate, no
recommendation, and no ambiguity auto-resolution. A per-row ambiguity picker in
the UI is a planned refinement (see `docs/backlog.md` Phase 4); until it exists
the import refuses and names the rows, which is blunt but never wrong.
