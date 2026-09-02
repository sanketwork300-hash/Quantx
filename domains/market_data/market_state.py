"""``MarketState``: one timestamp-consistent snapshot of everything a
calculation is allowed to see.

This is the single most important architectural constraint in the platform.
Every material calculation takes a ``MarketState``, never a live provider
handle, so two calculations run against the same ``state_id`` are guaranteed to
have seen the same inputs. Risk aggregation cannot mix an 09:15 delta with an
09:47 vega, and the delta a user sees between "current" and "proposed" is
attributable to their order rather than to the market moving underneath.

The id is **content-addressed**: identical inputs produce an identical id, so
rehydrating a stored state and recomputing gives the same answer. That is what
makes an analysis reproducible rather than merely repeatable.

Phase 2 populates quotes, spot prices, the curve, surfaces and per-instrument
quality. The future/FX maps are declared but empty until the phases that own
them arrive — declared rather than omitted so consumers can be written against
the final shape now.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from types import MappingProxyType

from domains.market_data.curves import YieldCurve
from domains.market_data.models import Quote
from domains.market_data.quality.flags import MarketDataQuality

#: ``(underlying_id, model)`` identifies a surface within a state.
SurfaceKey = tuple[uuid.UUID, str]


@dataclass(frozen=True, slots=True)
class MarketState:
    """Frozen, content-addressed, and safe to pass anywhere."""

    #: The logical valuation timestamp. Quotes carry their own event times; this
    #: is the *decision* time, and staleness is measured against it.
    as_of: datetime
    quotes: Mapping[uuid.UUID, Quote] = field(default_factory=dict)
    spot_prices: Mapping[uuid.UUID, Decimal] = field(default_factory=dict)
    future_prices: Mapping[uuid.UUID, Decimal] = field(default_factory=dict)
    yield_curves: Mapping[str, YieldCurve] = field(default_factory=dict)
    fx_rates: Mapping[str, Decimal] = field(default_factory=dict)
    #: Surface identifiers rather than surface objects: the derivatives domain
    #: owns surfaces, and a market-data value object holding one would invert
    #: the dependency between the two.
    volatility_surfaces: Mapping[SurfaceKey, str] = field(default_factory=dict)
    data_versions: Mapping[str, str] = field(default_factory=dict)
    quality: Mapping[uuid.UUID, MarketDataQuality] = field(default_factory=dict)
    sources: tuple[str, ...] = field(default=())

    @property
    def state_id(self) -> str:
        payload = {
            "as_of": self.as_of.isoformat(),
            "quotes": sorted(
                (
                    str(instrument_id),
                    quote.exchange_timestamp.isoformat(),
                    str(quote.bid_price),
                    str(quote.ask_price),
                    str(quote.last_price),
                    quote.source,
                )
                for instrument_id, quote in self.quotes.items()
            ),
            "spot_prices": sorted(
                (str(key), format(value, "f")) for key, value in self.spot_prices.items()
            ),
            "future_prices": sorted(
                (str(key), format(value, "f")) for key, value in self.future_prices.items()
            ),
            "yield_curves": sorted(self.yield_curves),
            "fx_rates": sorted((key, format(value, "f")) for key, value in self.fx_rates.items()),
            "surfaces": sorted(
                (str(key[0]), key[1], value) for key, value in self.volatility_surfaces.items()
            ),
            "data_versions": sorted(self.data_versions.items()),
        }
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode())
        return f"state:{digest.hexdigest()[:16]}"

    def quote_age_seconds(self, instrument_id: uuid.UUID) -> float | None:
        quote = self.quotes.get(instrument_id)
        return None if quote is None else quote.age_seconds(self.as_of)

    def curve(self, curve_id: str | None) -> YieldCurve | None:
        if curve_id is None:
            return next(iter(self.yield_curves.values()), None)
        return self.yield_curves.get(curve_id)

    def to_provenance(self) -> dict:
        return {
            "market_state_id": self.state_id,
            "market_state_timestamp": self.as_of.isoformat(),
            "market_data_sources": list(self.sources),
            "dataset_versions": dict(self.data_versions),
            "instruments": len(self.quotes),
            "yield_curves": list(self.yield_curves),
            "volatility_surfaces": [
                {"underlying_id": str(key[0]), "model": key[1], "surface_id": value}
                for key, value in self.volatility_surfaces.items()
            ],
        }

    def to_dict(self, include_quotes: bool = False) -> dict:
        payload = {
            "state_id": self.state_id,
            "as_of_timestamp": self.as_of.isoformat(),
            "counts": {
                "quotes": len(self.quotes),
                "spot_prices": len(self.spot_prices),
                "yield_curves": len(self.yield_curves),
                "volatility_surfaces": len(self.volatility_surfaces),
            },
            "sources": list(self.sources),
            "dataset_versions": dict(self.data_versions),
            "yield_curves": {
                curve_id: curve.to_provenance() for curve_id, curve in self.yield_curves.items()
            },
            "volatility_surfaces": [
                {"underlying_id": str(key[0]), "model": key[1], "surface_id": value}
                for key, value in self.volatility_surfaces.items()
            ],
            "spot_prices": {
                str(key): format(value, "f") for key, value in self.spot_prices.items()
            },
        }
        if include_quotes:
            payload["quotes"] = {
                str(instrument_id): {
                    "exchange_timestamp": quote.exchange_timestamp.isoformat(),
                    "bid_price": (
                        format(quote.bid_price, "f") if quote.bid_price is not None else None
                    ),
                    "ask_price": (
                        format(quote.ask_price, "f") if quote.ask_price is not None else None
                    ),
                    "mid_price": (
                        format(quote.mid_price, "f") if quote.mid_price is not None else None
                    ),
                    "age_seconds": quote.age_seconds(self.as_of),
                    "source": quote.source,
                }
                for instrument_id, quote in self.quotes.items()
            }
        return payload


class MarketStateBuilder:
    """Assembles a snapshot, admitting nothing that post-dates ``as_of``.

    Stale data is **labelled, not rejected**: refusing to value a portfolio
    because one leg is an hour old is worse than valuing it with a visible
    warning, so the builder records every admitted quote's age and leaves the
    judgement to the quality engine and the caller.
    """

    def __init__(self, as_of: datetime) -> None:
        if as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        self._as_of = as_of
        self._quotes: dict[uuid.UUID, Quote] = {}
        self._spots: dict[uuid.UUID, Decimal] = {}
        self._futures: dict[uuid.UUID, Decimal] = {}
        self._curves: dict[str, YieldCurve] = {}
        self._fx: dict[str, Decimal] = {}
        self._surfaces: dict[SurfaceKey, str] = {}
        self._versions: dict[str, str] = {}
        self._quality: dict[uuid.UUID, MarketDataQuality] = {}
        self._sources: list[str] = []
        self._rejected: list[tuple[uuid.UUID, str]] = []

    @property
    def rejected(self) -> tuple[tuple[uuid.UUID, str], ...]:
        """Observations excluded from the snapshot, with the reason."""
        return tuple(self._rejected)

    def add_quote(
        self, quote: Quote, quality: MarketDataQuality | None = None
    ) -> MarketStateBuilder:
        if quote.exchange_timestamp > self._as_of:
            # A quote from after the decision time cannot belong to it.
            self._rejected.append((quote.instrument_id, "AFTER_AS_OF"))
            return self
        existing = self._quotes.get(quote.instrument_id)
        if existing is not None and existing.exchange_timestamp >= quote.exchange_timestamp:
            return self
        self._quotes[quote.instrument_id] = quote
        if quality is not None:
            self._quality[quote.instrument_id] = quality
        return self

    def add_spot(self, instrument_id: uuid.UUID, price: Decimal) -> MarketStateBuilder:
        self._spots[instrument_id] = price
        return self

    def add_future(self, instrument_id: uuid.UUID, price: Decimal) -> MarketStateBuilder:
        self._futures[instrument_id] = price
        return self

    def add_curve(self, curve: YieldCurve) -> MarketStateBuilder:
        self._curves[curve.curve_id] = curve
        return self

    def add_fx_rate(self, pair: str, rate: Decimal) -> MarketStateBuilder:
        self._fx[pair.upper()] = rate
        return self

    def add_surface(
        self, underlying_id: uuid.UUID, model: str, surface_id: str
    ) -> MarketStateBuilder:
        self._surfaces[(underlying_id, model)] = surface_id
        return self

    def add_source(self, source: str, version: str | None = None) -> MarketStateBuilder:
        if source not in self._sources:
            self._sources.append(source)
        if version:
            self._versions[source] = version
        return self

    def build(self) -> MarketState:
        return MarketState(
            as_of=self._as_of,
            quotes=MappingProxyType(dict(self._quotes)),
            spot_prices=MappingProxyType(dict(self._spots)),
            future_prices=MappingProxyType(dict(self._futures)),
            yield_curves=MappingProxyType(dict(self._curves)),
            fx_rates=MappingProxyType(dict(self._fx)),
            volatility_surfaces=MappingProxyType(dict(self._surfaces)),
            data_versions=MappingProxyType(dict(self._versions)),
            quality=MappingProxyType(dict(self._quality)),
            sources=tuple(self._sources),
        )
