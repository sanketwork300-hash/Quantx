"""Live market data: connection, normalisation, distribution and live state.

The shape of this package follows one rule from the architecture: a quant engine
never talks to a feed. A frame arrives, is decoded, is normalised into the same
``Quote`` the CSV loader produces, is scored by the same quality engine the
ingestion pipeline uses, and is written into a live store from which a
``MarketState`` is assembled. Everything downstream sees a snapshot, exactly as
it does for historical data.

The transport is deliberately swappable. A polling transport built on the REST
provider and a WebSocket transport built on a pluggable frame decoder are two
implementations of one interface, so which one a deployment can use is a
configuration question rather than an architectural one.
"""
