"""Book analytics over a dataset: measure every snapshot, then summarise.

The summary is the part worth being careful about. A mean spread over a session
is a number anyone will read as "the spread", so what it was computed over has
to travel with it: how many snapshots contributed, how many had no such
measurement, and why they did not. The alternative — averaging over whatever
happened to be computable and reporting the count as the dataset size — is how
a statistic ends up describing a subset nobody chose.

Percentiles rather than a standard deviation, because a session of book
snapshots is not remotely normal: a handful of instants around an auction or a
news print dominate a variance and say nothing about a typical moment.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from domains.market_data.models import OrderBookSnapshot
from quant.microstructure.book import (
    Book,
    BookAnalytics,
    BookSide,
    analyse_book,
    cost_to_trade,
)

ANALYTICS_MODEL_VERSION = "book-analytics@1.0.0"

#: Percentiles reported for every measure. The tails are there because the
#: interesting moments in a book are the ones a mean hides.
PERCENTILES: tuple[float, ...] = (5.0, 25.0, 50.0, 75.0, 95.0)

#: Measures summarised across the session, in the order the UI reads them.
MEASURES: tuple[str, ...] = (
    "spread",
    "relative_spread",
    "mid",
    "microprice",
    "microprice_tilt",
    "bid_depth",
    "ask_depth",
    "imbalance",
    "weighted_imbalance",
    "bid_slope_value",
    "ask_slope_value",
    "bid_concentration",
    "ask_concentration",
)


@dataclass(frozen=True, slots=True)
class BookAnalyticsParams:
    levels: int = 5
    weighted_decay: float = 0.5
    #: Sizes to walk the book for. Empty by default: a cost to trade is only
    #: meaningful at a size the caller actually cares about.
    trade_sizes: tuple[float, ...] = ()
    #: Points kept in the row's preview series. The full per-snapshot series is
    #: written to the object store; this is what the UI charts without a second
    #: fetch.
    preview_points: int = 500

    def to_dict(self) -> dict:
        return {
            "levels": self.levels,
            "weighted_decay": self.weighted_decay,
            "trade_sizes": list(self.trade_sizes),
            "preview_points": self.preview_points,
        }


@dataclass(frozen=True, slots=True)
class MeasureSummary:
    name: str
    observations: int
    #: Snapshots where this measure did not exist, by the reason it did not.
    missing: int
    missing_reasons: dict[str, int]
    mean: float | None
    percentiles: dict[str, float]
    minimum: float | None
    maximum: float | None

    def to_dict(self) -> dict:
        return {
            "measure": self.name,
            "observations": self.observations,
            "missing": self.missing,
            "missing_reasons": dict(self.missing_reasons),
            "mean": self.mean,
            "percentiles": dict(self.percentiles),
            "minimum": self.minimum,
            "maximum": self.maximum,
        }


@dataclass(frozen=True, slots=True)
class TradeCostSummary:
    """What walking the book would have cost, at one size, across the session."""

    quantity: float
    is_buy: bool
    #: Snapshots whose displayed depth could absorb the size at all.
    absorbed: int
    refused: int
    median_slippage_bps: float | None
    p95_slippage_bps: float | None
    median_levels_consumed: float | None

    def to_dict(self) -> dict:
        return {
            "quantity": self.quantity,
            "side": "BUY" if self.is_buy else "SELL",
            "snapshots_that_could_absorb_it": self.absorbed,
            "snapshots_that_could_not": self.refused,
            "median_slippage_bps": self.median_slippage_bps,
            "p95_slippage_bps": self.p95_slippage_bps,
            "median_levels_consumed": self.median_levels_consumed,
            "note": (
                "Displayed depth at an instant, taken all at once with nothing "
                "moving. Not an impact model and not a forecast; a size the "
                "displayed book cannot absorb is refused rather than "
                "extrapolated past the last level."
            ),
        }


@dataclass(frozen=True, slots=True)
class BookAnalyticsResult:
    params: BookAnalyticsParams
    snapshots_analysed: int
    first_timestamp: datetime | None
    last_timestamp: datetime | None
    summaries: tuple[MeasureSummary, ...]
    trade_costs: tuple[TradeCostSummary, ...]
    #: One entry per snapshot, in time order. Written to parquet; the row keeps
    #: a downsampled preview of it.
    series: list[dict] = field(default_factory=list)
    crossed_snapshots: int = 0
    locked_snapshots: int = 0

    def summary(self, name: str) -> MeasureSummary | None:
        for item in self.summaries:
            if item.name == name:
                return item
        return None

    def preview_series(self) -> list[dict]:
        """Evenly-spaced subsample of the series, endpoints kept.

        Evenly spaced rather than the first N, because the first N of a session
        is the opening and would make every chart a picture of the auction.
        """
        limit = self.params.preview_points
        if len(self.series) <= limit:
            return list(self.series)
        step = len(self.series) / limit
        indices = sorted({int(index * step) for index in range(limit)} | {len(self.series) - 1})
        return [self.series[index] for index in indices]

    def to_dict(self) -> dict:
        return {
            "model_version": ANALYTICS_MODEL_VERSION,
            "parameters": self.params.to_dict(),
            "snapshots_analysed": self.snapshots_analysed,
            "window": {
                "start": self.first_timestamp.isoformat() if self.first_timestamp else None,
                "end": self.last_timestamp.isoformat() if self.last_timestamp else None,
            },
            "crossed_snapshots": self.crossed_snapshots,
            "locked_snapshots": self.locked_snapshots,
            "measures": [item.to_dict() for item in self.summaries],
            "trade_costs": [item.to_dict() for item in self.trade_costs],
        }


def to_book(snapshot: OrderBookSnapshot, levels: int | None = None) -> Book:
    """Canonical snapshot to the numerical layer's book, in floats."""
    bids = snapshot.bids[:levels] if levels else snapshot.bids
    asks = snapshot.asks[:levels] if levels else snapshot.asks
    return Book(
        bids=BookSide(
            prices=tuple(float(level.price) for level in bids),
            sizes=tuple(float(level.quantity) for level in bids),
            is_bid=True,
        ),
        asks=BookSide(
            prices=tuple(float(level.price) for level in asks),
            sizes=tuple(float(level.quantity) for level in asks),
            is_bid=False,
        ),
    )


def _extract(analytics: BookAnalytics, measure: str) -> float | None:
    if measure == "bid_slope_value":
        return analytics.bid_slope.slope if analytics.bid_slope else None
    if measure == "ask_slope_value":
        return analytics.ask_slope.slope if analytics.ask_slope else None
    return getattr(analytics, measure, None)


def _missing_key(measure: str) -> str:
    """Which ``unavailable`` entry explains this measure's absence."""
    return {
        "bid_slope_value": "bid_slope",
        "ask_slope_value": "ask_slope",
        "microprice_tilt": "microprice",
    }.get(measure, measure)


def _summarise(
    measure: str, values: list[float], reasons: dict[str, int], missing: int
) -> MeasureSummary:
    if not values:
        return MeasureSummary(
            name=measure,
            observations=0,
            missing=missing,
            missing_reasons=reasons,
            mean=None,
            percentiles={},
            minimum=None,
            maximum=None,
        )
    array = np.asarray(values, dtype=float)
    return MeasureSummary(
        name=measure,
        observations=int(array.size),
        missing=missing,
        missing_reasons=reasons,
        mean=float(array.mean()),
        percentiles={
            f"p{int(percentile):02d}": float(np.percentile(array, percentile))
            for percentile in PERCENTILES
        },
        minimum=float(array.min()),
        maximum=float(array.max()),
    )


def analyse_snapshots(
    snapshots: tuple[OrderBookSnapshot, ...], params: BookAnalyticsParams
) -> BookAnalyticsResult:
    """Measure every snapshot and summarise what was measurable."""
    ordered = sorted(snapshots, key=lambda snapshot: snapshot.exchange_timestamp)

    collected: dict[str, list[float]] = {measure: [] for measure in MEASURES}
    reasons: dict[str, dict[str, int]] = {measure: {} for measure in MEASURES}
    missing: dict[str, int] = {measure: 0 for measure in MEASURES}
    series: list[dict] = []
    costs: dict[tuple[float, bool], list[tuple[float, int]]] = {
        (size, is_buy): [] for size in params.trade_sizes for is_buy in (True, False)
    }
    cost_refusals: dict[tuple[float, bool], int] = dict.fromkeys(costs, 0)
    crossed = locked = 0

    for snapshot in ordered:
        book = to_book(snapshot)
        analytics = analyse_book(book, params.levels, params.weighted_decay)
        crossed += int(analytics.is_crossed)
        locked += int(analytics.is_locked)

        for measure in MEASURES:
            value = _extract(analytics, measure)
            if value is None:
                missing[measure] += 1
                reason = analytics.unavailable.get(_missing_key(measure), "NOT_MEASURED")
                reasons[measure][reason] = reasons[measure].get(reason, 0) + 1
            else:
                collected[measure].append(value)

        row = {"timestamp": snapshot.exchange_timestamp}
        row.update(
            {measure: _extract(analytics, measure) for measure in MEASURES if measure != "mid"}
        )
        row["mid"] = analytics.mid
        row["bid_levels"] = analytics.bid_levels
        row["ask_levels"] = analytics.ask_levels
        series.append(row)

        for (size, is_buy), bucket in costs.items():
            outcome = cost_to_trade(book, size, is_buy)
            if isinstance(outcome, tuple):
                cost_refusals[(size, is_buy)] += 1
            else:
                bucket.append((outcome.slippage_bps, outcome.levels_consumed))

    summaries = tuple(
        _summarise(measure, collected[measure], reasons[measure], missing[measure])
        for measure in MEASURES
    )
    trade_costs = tuple(
        TradeCostSummary(
            quantity=size,
            is_buy=is_buy,
            absorbed=len(bucket),
            refused=cost_refusals[(size, is_buy)],
            median_slippage_bps=(
                float(np.median([item[0] for item in bucket])) if bucket else None
            ),
            p95_slippage_bps=(
                float(np.percentile([item[0] for item in bucket], 95.0)) if bucket else None
            ),
            median_levels_consumed=(
                float(np.median([item[1] for item in bucket])) if bucket else None
            ),
        )
        for (size, is_buy), bucket in sorted(
            costs.items(), key=lambda item: (item[0][0], not item[0][1])
        )
    )

    return BookAnalyticsResult(
        params=params,
        snapshots_analysed=len(ordered),
        first_timestamp=ordered[0].exchange_timestamp if ordered else None,
        last_timestamp=ordered[-1].exchange_timestamp if ordered else None,
        summaries=summaries,
        trade_costs=trade_costs,
        series=series,
        crossed_snapshots=crossed,
        locked_snapshots=locked,
    )
