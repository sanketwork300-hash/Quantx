# Live options intelligence

## 1. What real-time Phase 2 delivers

```
User selects NIFTY
      │
      ▼
POST /live/options/analyse           one job, one captured moment
      │
      ├─ capture       live cache → option_chain_snapshots (stored)
      ├─ analyse       the existing IV solver, on the stored snapshot
      ├─ calibrate     the existing SVI fit, characteristics, arbitrage reports
      └─ delta skew    25Δ / 10Δ risk reversal and butterfly off the fit
      │
      ▼
GET /derivatives/surfaces/{id}/delta-skew      shape, quoted the market's way
GET /market/open-interest/{underlying}         OI, volume, ratios
GET /market/open-interest/{underlying}/change  the move between two snapshots
```

Almost none of the quantitative machinery is new. The IV solver, the SVI
calibration, the arbitrage diagnostics and the surface characteristics were all
built in earlier phases and are used here unchanged. What this phase adds is the
*path* — a live chain reaching them — plus two genuinely new pieces of
arithmetic: delta-quoted skew and open-interest analytics.

## 2. The decision the whole phase rests on

**A live chain is written into the same `option_chain_snapshots` row a CSV
upload produces, and then the existing pipeline runs on it unchanged.**

Computing implied volatilities and surfaces straight off the live cache would
have been less code and much worse:

* a surface calibrated from memory cannot be refitted six months later to check
  what it said;
* the arbitrage reports and surface characteristics would need a second home;
* there would be two paths through the IV solver that could disagree.

Persisting first means a live analysis is reproducible on exactly the terms a
historical one is. That is the platform's central promise, and it is worth the
extra write.

## 3. What a capture reports about itself

Conservation holds as it does everywhere else:

```
contracts_considered == quotes_kept + quotes_excluded + contracts_without_quotes
```

A contract the feed had no price for is **rejected with that reason**, not
quietly left out of the chain. On an illiquid wing that is ordinary; the count
says how ordinary, and `LIVE_CHAIN_CONTRACTS_WITHOUT_QUOTES` carries it into the
warnings.

Two fields deserve their own mention.

**`timestamp_spread_seconds`** is the widest gap between the exchange timestamps
in the capture. Every calibration downstream treats these quotes as simultaneous;
this number is how much of an instant the "snapshot" really is. Above 30 seconds
the capture warns.

**`underlying_price`** is the live mid where there is a two-sided market and the
published level otherwise. The substitution is confined to this derived value —
the stored quote is untouched and `Quote.mid_price` still returns `None`. Where
the feed holds no price for the underlying at all, the snapshot carries none and
`LIVE_CHAIN_NO_UNDERLYING_PRICE` says so; nothing downstream invents one.

Quality is scored **again** at capture, not carried over from the feed. The feed
scored each quote as a standalone observation; an option in a chain is
additionally checked against its own no-arbitrage bounds and its underlying, and
that check needs the chain around it.

## 4. Delta-quoted skew

`dsigma/dk` at the money is already recorded in the surface characteristics. It
is the right coordinate for arbitrage and the wrong one for comparison: nobody
quotes a smile as a derivative. So the same shape is also recorded as a risk
reversal and a butterfly at 25 and 10 delta.

See `methodology.md` §8a-bis for the definitions. The three properties that
matter in use:

| Property | Consequence |
| --- | --- |
| The convention is **forward delta**, and it is on every result | Spot and premium-adjusted delta give different strikes for the same nominal delta. A number whose convention is unstated cannot be reconciled with a broker's runs. |
| A strike outside the fitted range is `EXTRAPOLATED`, not hidden | A 10-delta strike is often beyond anything quoted. The value is still useful; presenting it as though it were fitted is not. |
| A wing that could not be found gives `null`, not zero | A skew of zero and a skew that could not be measured are different facts, and a chart that cannot tell them apart is worse than a gap in the line. |

Nothing is stored. It is a pure function of five SVI numbers per slice, so it is
recomputed on read — which means it cannot disagree with a stored copy that has
drifted, and adding a delta level needs no migration.

## 4a. Greeks across the chain

`GET /derivatives/analyses/{id}/greeks` returns delta, gamma, vega, theta and
rho for every solved contract, read off the stored analysis.

Three choices, each a place a Greek quietly becomes the wrong number:

* **The volatility is the one solved from that contract's own quote**, not the
  surface's fitted value at its strike. These are the Greeks of the option you
  can actually trade, at the price it is actually shown.
* **A contract whose volatility did not solve has no Greeks at all** — it is
  listed with a reason (`NO_IMPLIED_VOL`, `EXPIRED`, `NO_UNDERLYING_PRICE`)
  rather than given zeros. A row of zeros reads as an option carrying no risk,
  and it plots and sums perfectly.
* **The carry assumption travels with the answer.** The rate and yield are read
  from the analysis's own provenance, so the Greeks are measured against the
  same carry the implied volatilities were solved with, and
  `dividend_yield_assumed` says whether the yield was supplied or defaulted.

Computed on read, like the delta skew: a deterministic function of the persisted
volatilities and the carry, so a stored copy could only drift.

## 5. Open interest

Arithmetic, reported as arithmetic. Open interest and volume by strike and
expiry, put-call ratios on each, turnover against open positions, and the change
between two snapshots.

**The platform reports these and does not interpret them.** A put-call ratio is
widely read as a sentiment indicator, usually contrarian, on evidence that is
thin and regime-dependent. No field, label or message in this surface says what
a ratio means, and an integration test asserts that the response body contains
none of the words that would.

Three rules, each guarding a number that would otherwise plot perfectly well and
be wrong: a missing figure is not a zero; a zero denominator gives `null` rather
than infinity; the open-interest unit is the venue's and is labelled rather than
normalised. `methodology.md` §8a-ter has the reasoning.

Change is always reported with `window_seconds`. Without it a figure measured
over eleven minutes reads exactly like one measured over a session.

**"Max pain" is deliberately absent.** It is a well-defined function of open
interest, and it is almost always presented as a prediction of where the
underlying will settle. The platform has no basis for that claim and will not
imply one by shipping the quantity under its usual name. `most_open_interest`
reports where positions sit, and says only that.

## 6. Running it

```bash
# The feed must be delivering; see live-market-data.md
curl -XPOST /api/v1/live/subscriptions -d '{"instrument_ids": [...]}'   # the chain
curl -XPOST /api/v1/live/options/analyse -d '{
  "underlying_id": "...",
  "risk_free_rate": 0.065,
  "settlement_time_utc": "10:00:00"
}'
```

Then poll `GET /jobs/{id}/result`. The result carries the snapshot, analysis and
surface identifiers, so every number traces back to the quotes it came from.

`settlement_time_utc` is worth supplying. Without it, time to expiry is known
only to the day, which on a weekly option is a large fraction of its life.

## 7. What is deliberately not here

**A second surface model fitted to live data.** SSVI, Heston, local volatility
and the risk-neutral density all already run on a stored analysis, and a live
capture produces exactly that. They need no live-specific path and did not get
one.

**Streaming surface updates.** A surface is refitted per capture, not per tick.
Refitting an SVI slice on every quote would spend seconds of optimiser time to
move the fifth decimal place, and would make "the surface at 09:31:04" a
question with no answer.

**Greeks against the fitted surface as well as the quotes.** The chain Greeks
are measured against each contract's own solved volatility, which describes the
market as quoted. Surface Greeks — the same derivatives taken against the fitted
smile — are a different and also useful quantity, and the platform will grow
them when something needs them. Producing both now, undistinguished, would give
a table where neighbouring strikes were measured against different things.
