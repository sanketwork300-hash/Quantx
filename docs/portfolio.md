# Portfolio Domain

Status: _(Phase 4)_.

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

Cross-currency aggregation converts at the FX rates in the `MarketState`, and the
rate used is recorded; it is not fetched separately at display time.

Property test: the sum over positions equals the portfolio total within
tolerance, for value and for every Greek.
