# Live market data

## 1. What Phase 1 delivers

```
User selects NIFTY
      │
      ▼
POST /live/subscriptions          interest, registered in Redis with a TTL
      │
      ▼
Market stream worker              a separate process, one connection
      │
      ├─ transport                polling (REST) or websocket
      ├─ decoder                  frame → provider entries
      ├─ normalisation            provider entries → canonical Quote
      ├─ quality engine           the same one the ingestion pipeline uses
      ▼
Live store (Redis, TTL)           qip:v1:quote:{instrument_id}
      │
      ▼
GET /live/quotes   GET /live/state
      │                  │
      ▼                  ▼
Frontend            MarketState → every quant engine
```

The last arrow is the point of the whole phase. A live price does not reach a
pricing model as a live price; it reaches it as a `MarketState`, exactly as a
chain uploaded from a CSV does. Two calculations that report the same
`state_id` provably saw the same inputs whether that state came from a feed or
a file.

## 2. The rules this had to obey

**No quant engine touches a feed.** The stream worker writes a store; the API
reads it. Nothing in a request path opens a provider connection, so two readers
cannot get two different answers about the same instant.

**Nothing is invented.** The mapping from a provider payload to a canonical
quote is data (`NormalisationSpec`), and every read reports which fields it
found, which mapped paths were absent, and which payload keys nothing claims. A
field the provider did not send stays `None` — never zero, never the previous
value, never a similar-looking neighbour.

**Synthetic data is never a fallback.** A deployment configured for a live
provider that cannot be built raises `ProviderNotAvailable` rather than
substituting one, and `build_provider` refuses to construct the synthetic market
at all in a production-like environment. The check sits at construction rather
than at startup because a deployment that only analyses uploaded chains never
builds a provider, and refusing to start that would be refusing something
legitimate; what must never happen is a generated market being *served* as real.

**An absence is reported as an absence.** `/live/quotes` returns the instruments
it has *and* the ones it does not, because a short answer that looks complete is
worse than no answer.

## 3. What the provider layer does with an unfamiliar payload

`domains/market_data/providers/normalisation.py` reads a payload through a
named, versioned spec and returns an account of what happened:

| Report | Meaning |
| --- | --- |
| `fields_read` | canonical field → the path it came from |
| `fields_missing` | mapped, required, and not present in this payload |
| `fields_unmapped` | present in the payload, claimed by no mapping |

Either list alone is ordinary: providers send extras, and optional fields are
optional (`open_interest` on an equity, for instance, is declared optional and
its absence is not reported). **Both together is the signature of a renamed
field**, which is exactly the failure that otherwise runs for days: the reader
finds nothing where the price used to be, every quote carries a null last price,
and downstream that is indistinguishable from an instrument nobody is trading.

The report travels on `Quote.metadata` and therefore into provenance — but only
when the reading was *not* clean. On a healthy feed it is identical for every
quote from the same spec, and carrying it per tick would put a kilobyte of
unchanging text into every stored quote and every cache write; a clean read
records the spec name alone. The moment anything is missing or unclaimed, the
whole report is on the quote. An
operator whose provider has changed a response can repoint one field with
`with_overrides` — driven from configuration — without waiting for a release.

## 4. Matching a response to the request

Upstox keys its quote responses by a display symbol, not by the instrument key
the request used. The entry is therefore matched on the `instrument_token` the
payload itself carries. Failing that, a single entry answering a
single-instrument request is unambiguous and is accepted.

There is deliberately no third fallback. The third fallback is where one
instrument's price gets attached to another instrument's id, which produces a
chain that prices, calibrates and reports beautifully and is wrong.

## 5. The instrument master

`UpstoxInstrumentMaster` turns the published instrument file into canonical
instruments plus the alias rows that join platform ids to provider keys.

* **Rows are conserved.** `input == accepted + rejected + filtered_out`, and
  every rejection names the row and the reason. Filtered rows are counted rather
  than listed, because the complete file runs to hundreds of thousands of rows.
* **Nothing is coerced.** An unknown instrument type, an exchange with no
  recorded settlement currency, an option on a venue with no recorded exercise
  style, a contract whose underlying was not loaded — each is rejected with its
  own reason rather than mapped to the nearest plausible instrument.
* **The multiplier says where it came from.** Taken from the provider's lot
  size, with `metadata["multiplier_source"] = "provider_lot_size"`. Where the
  provider gives none it falls back to 1 and records `"platform_default"`, which
  `Instrument.multiplier_is_assumed` then reports and the quality engine flags.

### The expiry that could be a day out

The provider publishes expiry as an epoch; the platform stores a calendar date,
and the date is part of the contract's identity. An instant at any time *during*
an exchange's day names that day and there is nothing to decide. An instant that
falls exactly on **local midnight** sits on a boundary: it could mean the start
of that day or the end of the previous one, and the two produce different
canonical keys, different instrument ids and different times to expiry.

`read_expiry` handles this explicitly:

1. Not on midnight → the date is read directly, source `provider_epoch`.
2. On midnight → the contract's own printed name is checked against renderings
   of **the two candidate dates only**. A name that matches one settles it,
   source `contract_name`. Because the name can only ever select between two
   dates already in hand, a contract-name format this code cannot read degrades
   to "could not resolve" rather than to some third date.
3. Otherwise → a stated `MidnightExpiryConvention` decides, source
   `convention`, and both candidates are recorded on the instrument's metadata.

An assumption that shaped an instrument's identity travels with that instrument.

## 6. Transports

| | `polling` | `websocket` |
| --- | --- | --- |
| Mechanism | REST quote endpoint on a timer | provider feed held open |
| `delivers_every_update` | **False** | True |
| Needs a wire-format decoder | no | yes |
| Verified end to end in this repository | yes | connection lifecycle only |

`delivers_every_update` is reported by `/live/status` rather than assumed. A
sampling transport shows the latest state at its sample rate, which is all a UI
can render anyway — but a queue or arrival-intensity model built on samples
would be a model of a book nobody observed, and the flag is what stops that
happening by accident.

The websocket transport will not start without a decoder for the provider's
frames. This platform does **not** ship a reimplementation of a provider's
binary wire format: a decoder that produces plausible numbers from a format
nobody checked is the single worst failure a market-data system has. Generate
the module from the provider's own published `.proto`:

```bash
protoc --python_out=. MarketDataFeedV3.proto
export QIP_UPSTOX_FEED_PROTO_MODULE=MarketDataFeedV3_pb2
export QIP_MARKET_STREAM_TRANSPORT=websocket
```

`ProtobufFeedDecoder` then decodes with `preserving_proto_field_name=True`, so
one normalisation spec describes both the REST and the feed shape and the two
paths cannot disagree about what a field means.

## 7. What the stream manager guarantees

Four behaviours, each of them a way live data goes wrong without an error:

| Behaviour | Why |
| --- | --- |
| An older observation never overwrites a newer one | Feeds reorder and reconnections replay. Writing a stale price over a fresh one makes the market appear to move backwards. |
| A repeat is recognised as a repeat | The same exchange timestamp and prices is the same observation again, not new activity. It goes to the quality engine flagged as a duplicate. |
| A silent connection is reported `STALE` | A live socket delivering nothing looks exactly like a calm session unless something says so. |
| An unreadable frame is counted, not fatal | One bad frame must not drop a working connection, and a rising count is the only signal that a decoder is wrong. |
| A connection that opened and delivered nothing is counted separately | A socket that opens and closes empty is a different problem from one that dropped mid-stream — usually a subscription the provider is not honouring — and only the count distinguishes them. |

Reconnection uses capped, jittered exponential backoff, and the attempt counter
resets only when a connection has actually **delivered** something. Resetting on
connect would mean a provider that accepts and immediately drops gets retried at
the floor delay forever — a denial-of-service attack on the provider carried out
by our own client.

On every reconnection the whole subscription set is resent rather than the delta
since the drop, because the delta since a drop is precisely what was lost.

## 8. Running it

```bash
# 1. Connect the market-data account's broker at /connections (see credentials.md)
export QIP_MARKET_DATA_PROVIDER=upstox
export QIP_MARKET_DATA_ACCOUNT_EMAIL=you@example.com

# 2. Load the instrument master (also available as POST /live/instruments/refresh)
#    Narrow it: the complete file covers every listed instrument on every segment.
export QIP_UPSTOX_INSTRUMENT_SEGMENTS=NSE_INDEX,NSE_FO
export QIP_UPSTOX_INSTRUMENT_UNDERLYINGS=NIFTY,BANKNIFTY

# 3. Run the feed worker, in its own process
python -m apps.stream.main
```

The worker exits non-zero with the reason when it is configured for something it
cannot do — no credential for the named account, a websocket transport with no
decoder. It does not fall back to generated prices.

## 9. Where the code lives, and one deviation from the plan

The build plan sketched a top-level `services/market_stream/` package. The
decomposition it asked for is exactly what was built — manager, websocket
client, normalizer, event bus, subscriptions, reconnect — but it lives in
`domains/market_data/streaming/`, because a new top-level package would have
needed its own entry in the layering rules and would have put market-data logic
outside the market-data domain. The runnable process is `apps/stream/main.py`,
which is where every other runnable process in this repository lives.

| Plan | Here |
| --- | --- |
| `services/market_stream/manager.py` | `domains/market_data/streaming/manager.py` |
| `services/market_stream/websocket_client.py` | `streaming/feed.py` (`WebSocketFeedTransport`, `UpstoxWebSocketConnector`) |
| `services/market_stream/normalizer.py` | `providers/normalisation.py`, shared with the REST path |
| `services/market_stream/event_bus.py` | `streaming/bus.py` |
| `services/market_stream/subscriptions.py` | `streaming/subscriptions.py` |
| `services/market_stream/reconnect.py` | `streaming/reconnect.py` |

The normaliser is shared with the request/response path on purpose. Two readings
of one payload is two chances to disagree about what a field means, and a quote
that arrives over a socket must not be able to differ from the same quote
fetched over HTTP.

Redis keys follow the plan's layout with a version segment added —
`qip:v1:quote:{instrument_id}` — so a change to the stored shape cannot be read
by the previous code as though it were the old shape.

## 10. Deliberately not here

**A `/ws/market` push socket.** The frontend polls `/live/quotes`. A websocket
from the API would be a second copy of the live state with its own consistency
question, and its update rate would still be bounded by what a browser can
render. Polling the store is honest about being a sample; a socket that
delivered the same sample rate while implying tick-by-tick would not be.

**Order-book events from a polling transport.** `ProviderCapability.BOOK_EVENTS`
is not declared for either transport. Periodic depth snapshots support book
analytics and cannot support a queue model, and the capability declaration is
what keeps Phase 10's microstructure gate honest about which one it has.

**Redistribution of provider data.** The market-data account's entitlement
governs what may be shown to whom. That an API is free does not make its data
redistributable, and nothing in this phase exposes raw provider payloads to
anyone but the account holder.
