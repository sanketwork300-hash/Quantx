# Instruments — canonical identity and resolution

Status: **implemented** in Phase 0 (`domains/instruments/`).

---

## 1. Why identity is the first hard problem

The same NIFTY 24000 call expiring 2026-09-24 arrives as `NIFTY26SEP24000CE`
from one broker, `NIFTY 24SEP2026 24000 CE` from a second, and
`{"root":"NIFTY","exp":"2026-09-24","k":24000,"cp":"C"}` from a third. If those
become three instruments, portfolio aggregation, risk netting and TCA are all
silently wrong, and nothing throws.

So identity is defined **once**, canonically, and every provider adapter is
responsible for mapping into it.

## 2. Canonical key

A deterministic string built from the fields that define the contract:

```
{EXCHANGE}:{ASSET_CLASS}:{SYMBOL}[:{EXPIRY}][:{STRIKE}][:{OPTION_TYPE}]
```

Examples:

```
NSE:INDEX:NIFTY
NSE:FUTURE:NIFTY:2026-09-24
NSE:OPTION:NIFTY:2026-09-24:24000:C
BINANCE:CRYPTO_PERPETUAL:BTCUSDT
NASDAQ:EQUITY:AAPL
```

Rules that make it stable:

- `EXCHANGE` and `SYMBOL` are upper-cased and stripped.
- `EXPIRY` is the ISO date of the contract's expiry in the **exchange's**
  calendar date, not a local-time conversion.
- `STRIKE` is a normalized `Decimal` rendered without trailing zeros
  (`24000`, `24000.5`), so `24000.00` and `24000` never diverge.
- `OPTION_TYPE` is `C` or `P`.

## 3. Deterministic ids

```python
instrument_id = uuid5(QIP_INSTRUMENT_NAMESPACE, canonical_key)
```

Consequences that matter:

- The same contract has the same UUID in dev, CI, prod and in a colleague's
  reproduction of a six-month-old analysis. Provenance records that reference an
  instrument id remain meaningful across databases.
- Import is idempotent: re-uploading the same chain updates, never duplicates.
- No central id-issuing service is needed, and no import has to round-trip to
  the database to discover an id.

The namespace UUID is a fixed constant in `domains/instruments/identity.py`.
Changing it is a breaking data migration and is called out as such.

## 4. Model

```
Instrument
  id                UUID (uuid5 of canonical_key)
  canonical_key     str, unique
  asset_class       EQUITY | INDEX | FUTURE | OPTION | FX
                    | CRYPTO_SPOT | CRYPTO_PERPETUAL
  exchange          str          e.g. NSE
  venue             str | None   execution venue if different from listing
  symbol            str          contract root, e.g. NIFTY
  underlying_id     UUID | None  -> instruments.id
  currency          ISO 4217 str
  multiplier        Decimal      contract multiplier / lot value factor
  tick_size         Decimal
  lot_size          Decimal
  expiry            date | None
  strike            Decimal | None
  option_type       CALL | PUT | None
  exercise_style    EUROPEAN | AMERICAN | BERMUDAN | None
  settlement_type   CASH | PHYSICAL | None
  status            ACTIVE | EXPIRED | DELISTED | UNKNOWN
  metadata          dict
```

### Invariants (enforced in the domain model, tested)

- `OPTION` requires `expiry`, `strike`, `option_type`, `exercise_style`,
  `underlying_id`.
- `FUTURE` requires `expiry` and `underlying_id`; must not have `strike` or
  `option_type`.
- `EQUITY`, `INDEX`, `CRYPTO_SPOT`, `FX` must not have `expiry`, `strike` or
  `option_type`.
- `CRYPTO_PERPETUAL` must not have `expiry` (that is the point of a perpetual).
- `multiplier`, `tick_size`, `lot_size` are strictly positive.
- `strike` is strictly positive when present.

Violations raise `InvalidInstrument` at construction. An invalid instrument
cannot exist in memory, so no downstream code has to defend against one.

### Multipliers are data, not guesses

`multiplier`, `tick_size` and `lot_size` are **never** inferred from a symbol
pattern. They come from the provider, from an instrument-master import, or from
an explicit user-supplied default that is recorded as such in `metadata`
(`"multiplier_source": "user_default"`). Build spec §1.1 forbids fabricating
contract multipliers, and a wrong multiplier silently scales every Greek and
every margin number in the platform.

When a multiplier is unknown, the instrument is created with the value the user
or file supplied and a `MULTIPLIER_ASSUMED` flag that propagates into the
quality report — not with a plausible-looking constant.

## 5. Aliases and resolution

```
InstrumentAlias
  instrument_id  UUID
  source         str    provider / broker / upload id
  alias_symbol   str
  unique(source, alias_symbol)
```

`InstrumentResolver.resolve(request)` tries, in order:

1. exact `instrument_id`
2. exact `canonical_key`
3. `(source, alias_symbol)` alias lookup
4. structured match on `(exchange, asset_class, symbol, expiry, strike, option_type)`
5. symbol-root match narrowed by asset class

and returns one of:

```
Resolved   (instrument, method, confidence=1.0)
Ambiguous  (candidates[], reason)      -> caller must disambiguate
Unresolved (reason, request echo)      -> caller must fix or create
```

**`Ambiguous` is never silently collapsed to the first candidate.** Portfolio
and execution import surface ambiguous rows to the user for an explicit choice
(build spec §32). This is the single most common source of quietly wrong
portfolio risk, so the resolver has no "best effort" mode.

`resolve_or_create` exists for ingestion paths where the file legitimately
defines new contracts (an option chain upload for an expiry we have never seen),
and it is a separate, explicitly-requested code path with its own audit entry.

## 6. Expiry calendars and trading hours

Not fabricated. Phase 0 stores `expiry` as supplied by the data source. Day-count
conventions and business-day calendars for time-to-expiry are introduced in
Phase 1 with an explicit, documented convention (see `docs/methodology.md`), and
QuantLib's calendar implementations are used where a real exchange calendar is
required rather than hand-rolling holiday rules.
