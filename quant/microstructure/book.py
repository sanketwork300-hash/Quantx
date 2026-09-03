"""Order-book snapshot analytics.

Every quantity here is a *measurement of a book*, not a model of one. The rule
that shapes the module is that a measurement the levels cannot support is
returned as an absence with a reason, never as a number computed from one point
or from a side that is not there. A slope fitted through a single level, an
imbalance over an empty side and a cost to trade a size the book cannot absorb
are all reported as refusals, because each of them has a plausible-looking
numerical answer that means nothing.

Definitions follow the standard microstructure literature; the ones with more
than one convention in circulation are stated explicitly here and repeated in
``docs/methodology.md`` so a reader never has to guess which was used:

* **microprice** is size-weighted toward the *thin* side:
  ``(b*Qa + a*Qb) / (Qb + Qa)``. A large resting bid pulls it toward the ask.
* **imbalance** is ``(Qb - Qa) / (Qb + Qa)`` over the top ``levels`` of each
  side, so it is +1 for an all-bid book and -1 for an all-ask book.
* **book slope** is the through-the-origin least-squares slope of cumulative
  depth against *relative* distance from the mid. Through the origin because a
  book holds no depth at zero distance from the mid by construction.
* **depth concentration** is the Herfindahl index of the level sizes, whose
  reciprocal is the effective number of levels.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = [
    "Book",
    "BookAnalytics",
    "BookSide",
    "SlopeEstimate",
    "TradeCost",
    "Unavailable",
    "analyse_book",
    "book_slope",
    "cost_to_trade",
    "depth",
    "depth_concentration",
    "imbalance",
    "microprice",
    "weighted_imbalance",
]


class Unavailable(StrEnum):
    """Why a measurement could not be made. Closed vocabulary, as everywhere."""

    EMPTY_SIDE = "EMPTY_SIDE"
    ONE_SIDED_BOOK = "ONE_SIDED_BOOK"
    NO_RESTING_SIZE = "NO_RESTING_SIZE"
    SINGLE_LEVEL = "SINGLE_LEVEL"
    NON_POSITIVE_MID = "NON_POSITIVE_MID"
    DEGENERATE_PRICES = "DEGENERATE_PRICES"
    INSUFFICIENT_DEPTH = "INSUFFICIENT_DEPTH"


@dataclass(frozen=True, slots=True)
class BookSide:
    """One side of a book, best-first.

    Best-first is an invariant rather than a convention: level 0 is the top of
    book everywhere downstream, and a side sorted the other way would produce a
    negative slope and a nonsense cost-to-trade with nothing raising.
    """

    prices: tuple[float, ...]
    sizes: tuple[float, ...]
    is_bid: bool

    def __post_init__(self) -> None:
        if len(self.prices) != len(self.sizes):
            raise ValueError(
                f"{len(self.prices)} prices against {len(self.sizes)} sizes; a level "
                "without a size is not a level"
            )
        if any(size < 0 for size in self.sizes):
            raise ValueError("a negative resting size is not a book level")
        ordered = sorted(self.prices, reverse=self.is_bid)
        if list(self.prices) != ordered:
            raise ValueError(
                f"{'bids' if self.is_bid else 'asks'} are not ordered best-first"
            )

    def __len__(self) -> int:
        return len(self.prices)

    @property
    def best_price(self) -> float | None:
        return self.prices[0] if self.prices else None

    @property
    def best_size(self) -> float | None:
        return self.sizes[0] if self.sizes else None

    def depth(self, levels: int | None = None) -> float:
        """Total resting size over the top ``levels`` (all of them by default)."""
        return float(sum(self.sizes[:levels] if levels is not None else self.sizes))

    def cumulative(self, levels: int | None = None) -> tuple[float, ...]:
        running = 0.0
        out: list[float] = []
        for size in self.sizes[:levels] if levels is not None else self.sizes:
            running += size
            out.append(running)
        return tuple(out)


@dataclass(frozen=True, slots=True)
class Book:
    """A two-sided depth snapshot. Either side may be empty."""

    bids: BookSide
    asks: BookSide

    @classmethod
    def of(
        cls,
        bids: list[tuple[float, float]] | tuple[tuple[float, float], ...],
        asks: list[tuple[float, float]] | tuple[tuple[float, float], ...],
    ) -> Book:
        """Build from ``(price, size)`` pairs, which is how tests read best."""
        return cls(
            bids=BookSide(
                prices=tuple(price for price, _ in bids),
                sizes=tuple(size for _, size in bids),
                is_bid=True,
            ),
            asks=BookSide(
                prices=tuple(price for price, _ in asks),
                sizes=tuple(size for _, size in asks),
                is_bid=False,
            ),
        )

    @property
    def is_two_sided(self) -> bool:
        return bool(self.bids.prices) and bool(self.asks.prices)

    @property
    def best_bid(self) -> float | None:
        return self.bids.best_price

    @property
    def best_ask(self) -> float | None:
        return self.asks.best_price

    @property
    def spread(self) -> float | None:
        if not self.is_two_sided:
            return None
        return self.asks.prices[0] - self.bids.prices[0]

    @property
    def mid(self) -> float | None:
        if not self.is_two_sided:
            return None
        return (self.bids.prices[0] + self.asks.prices[0]) / 2.0

    @property
    def relative_spread(self) -> float | None:
        mid, spread = self.mid, self.spread
        if mid is None or spread is None or mid <= 0.0:
            return None
        return spread / mid

    @property
    def is_crossed(self) -> bool:
        return self.is_two_sided and self.bids.prices[0] > self.asks.prices[0]

    @property
    def is_locked(self) -> bool:
        return self.is_two_sided and self.bids.prices[0] == self.asks.prices[0]

    def side(self, is_bid: bool) -> BookSide:
        return self.bids if is_bid else self.asks


# ------------------------------------------------------------------ measures
def depth(book: Book, levels: int = 1) -> tuple[float, float]:
    """``(bid depth, ask depth)`` over the top ``levels`` of each side."""
    return book.bids.depth(levels), book.asks.depth(levels)


def microprice(book: Book) -> float | tuple[None, Unavailable]:
    """Size-weighted mid, weighted by the *opposite* side's size.

    Stoikov's microprice in its one-level form. Returns the reason instead of a
    number when a side is missing or neither side rests any size: a "weighted"
    average with no weights is the plain mid wearing another name, and
    returning it under this one would misdescribe what was measured.
    """
    if not book.is_two_sided:
        return None, Unavailable.ONE_SIDED_BOOK
    bid_size, ask_size = book.bids.sizes[0], book.asks.sizes[0]
    total = bid_size + ask_size
    if total <= 0.0:
        return None, Unavailable.NO_RESTING_SIZE
    return (book.bids.prices[0] * ask_size + book.asks.prices[0] * bid_size) / total


def imbalance(book: Book, levels: int = 1) -> float | tuple[None, Unavailable]:
    """``(Qb - Qa) / (Qb + Qa)`` over the top ``levels`` of each side.

    Bounded in ``[-1, 1]``: +1 is an all-bid book, -1 an all-ask book, 0 a
    balanced one. A book with no resting size on either side has no imbalance,
    which is not the same as a balanced one, so it is refused rather than
    reported as zero.
    """
    bid_depth, ask_depth = depth(book, levels)
    total = bid_depth + ask_depth
    if total <= 0.0:
        return None, Unavailable.NO_RESTING_SIZE
    return (bid_depth - ask_depth) / total


def weighted_imbalance(
    book: Book, levels: int = 5, decay: float = 0.5
) -> float | tuple[None, Unavailable]:
    """Multi-level imbalance with geometrically decaying level weights.

    ``w_i = exp(-decay * i)`` for zero-based level ``i``, so ``decay = 0``
    reduces exactly to :func:`imbalance` over the same levels and a large
    ``decay`` reduces to the top of book. The decay is a *parameter of the
    measurement* and travels with it into provenance; it is not tuned, because
    there is nothing here to tune it against.
    """
    if decay < 0.0:
        raise ValueError(f"a negative decay amplifies deep levels: {decay}")
    import math

    def weighted(side: BookSide) -> float:
        return float(
            sum(
                size * math.exp(-decay * index)
                for index, size in enumerate(side.sizes[:levels])
            )
        )

    bid_weight, ask_weight = weighted(book.bids), weighted(book.asks)
    total = bid_weight + ask_weight
    if total <= 0.0:
        return None, Unavailable.NO_RESTING_SIZE
    return (bid_weight - ask_weight) / total


@dataclass(frozen=True, slots=True)
class SlopeEstimate:
    """A through-the-origin fit of cumulative depth against distance from mid."""

    #: Quantity per unit *relative* distance from the mid. A slope of 10,000
    #: means roughly 100 units of depth accumulate by 1% away from the mid.
    slope: float
    #: Uncentred R-squared, which is the right one for a no-intercept model:
    #: the centred form can be negative for a fit that is perfectly reasonable.
    r_squared: float
    levels_used: int

    def to_dict(self) -> dict:
        return {
            "slope": self.slope,
            "r_squared": self.r_squared,
            "levels_used": self.levels_used,
            "units": "quantity per unit relative distance from mid",
        }


def book_slope(
    book: Book, is_bid: bool, levels: int = 5
) -> SlopeEstimate | tuple[None, Unavailable]:
    """How fast depth accumulates as you move away from the mid.

    Fitted through the origin, because a book holds no depth at zero distance
    from the mid by construction and an intercept would absorb exactly the
    quantity being measured. Two levels are the minimum: a line through one
    point is not a measurement, and returning ``C_1 / d_1`` under the name
    "slope" would present an arithmetic identity as a fit.

    The reported R-squared is the uncentred one, ``1 - SS_res / sum(C_i^2)``,
    which is the appropriate goodness-of-fit for a no-intercept regression. A
    low value means the book is far from linear in distance, which is itself
    worth seeing rather than hiding behind a slope.
    """
    mid = book.mid
    if mid is None:
        return None, Unavailable.ONE_SIDED_BOOK
    if mid <= 0.0:
        return None, Unavailable.NON_POSITIVE_MID

    side = book.side(is_bid)
    prices = side.prices[:levels]
    if len(prices) < 2:
        return None, Unavailable.SINGLE_LEVEL

    distances = tuple(abs(price - mid) / mid for price in prices)
    cumulative = side.cumulative(levels)

    denominator = sum(distance * distance for distance in distances)
    if denominator <= 0.0:
        # Every level sits at the mid, so there is no distance axis to regress
        # against. That is a degenerate book, not a vertical slope.
        return None, Unavailable.DEGENERATE_PRICES

    slope = sum(d * c for d, c in zip(distances, cumulative, strict=True)) / denominator
    residual = sum(
        (c - slope * d) ** 2 for d, c in zip(distances, cumulative, strict=True)
    )
    total = sum(c * c for c in cumulative)
    r_squared = 1.0 - residual / total if total > 0.0 else 0.0
    return SlopeEstimate(slope=slope, r_squared=r_squared, levels_used=len(prices))


def depth_concentration(
    side: BookSide, levels: int = 5
) -> tuple[float, float] | tuple[None, Unavailable]:
    """``(Herfindahl index, effective number of levels)`` for one side.

    ``H = sum((q_i / Q)^2)`` over the top levels. ``H = 1`` means all the depth
    sits at one level; ``1 / H`` is the effective number of levels the depth is
    spread across, which is the form worth reading. A single-level side is
    refused rather than reported as maximally concentrated, because a book that
    only *shows* one level and a book that genuinely holds all its size there
    are different facts and the snapshot cannot tell them apart.
    """
    sizes = side.sizes[:levels]
    if len(sizes) < 2:
        return None, Unavailable.SINGLE_LEVEL
    total = float(sum(sizes))
    if total <= 0.0:
        return None, Unavailable.NO_RESTING_SIZE
    herfindahl = float(sum((size / total) ** 2 for size in sizes))
    return herfindahl, 1.0 / herfindahl


@dataclass(frozen=True, slots=True)
class TradeCost:
    """What walking the book would cost, at the sizes the book actually shows."""

    quantity: float
    average_price: float
    #: Signed so that a cost is positive whichever way the order goes: paying
    #: above the mid to buy and receiving below it to sell are the same thing.
    slippage_per_unit: float
    slippage_bps: float
    levels_consumed: int

    def to_dict(self) -> dict:
        return {
            "quantity": self.quantity,
            "average_price": self.average_price,
            "slippage_per_unit": self.slippage_per_unit,
            "slippage_bps": self.slippage_bps,
            "levels_consumed": self.levels_consumed,
        }


def cost_to_trade(
    book: Book, quantity: float, is_buy: bool
) -> TradeCost | tuple[None, Unavailable]:
    """Walk the book for ``quantity`` and report the cost against the mid.

    This is a measurement of the displayed book at one instant, not a
    prediction: it says what the resting size would have cost if it had all
    been taken at once and nothing had moved. It is not an impact model, it
    does not include hidden liquidity or replenishment, and an order larger
    than the displayed depth is refused rather than extrapolated past the last
    level — the price beyond the book is not in the book.
    """
    if quantity <= 0.0:
        raise ValueError(f"a non-positive quantity has no cost to trade: {quantity}")
    mid = book.mid
    if mid is None:
        return None, Unavailable.ONE_SIDED_BOOK
    if mid <= 0.0:
        return None, Unavailable.NON_POSITIVE_MID

    side = book.asks if is_buy else book.bids
    if side.depth() < quantity:
        return None, Unavailable.INSUFFICIENT_DEPTH

    remaining = quantity
    notional = 0.0
    consumed = 0
    for price, size in zip(side.prices, side.sizes, strict=True):
        if remaining <= 0.0:
            break
        taken = min(size, remaining)
        if taken <= 0.0:
            continue
        notional += taken * price
        remaining -= taken
        consumed += 1

    average = notional / quantity
    slippage = (average - mid) if is_buy else (mid - average)
    return TradeCost(
        quantity=quantity,
        average_price=average,
        slippage_per_unit=slippage,
        slippage_bps=10_000.0 * slippage / mid,
        levels_consumed=consumed,
    )


# ----------------------------------------------------------------- roll-up
@dataclass(frozen=True, slots=True)
class BookAnalytics:
    """Everything measurable about one snapshot, plus what was not measurable.

    ``unavailable`` is not decoration. A caller that averages ``imbalance``
    across a session has to know which snapshots contributed and which had no
    imbalance to contribute, and a null that carries no reason turns into a
    zero somewhere downstream.
    """

    levels: int
    weighted_decay: float
    best_bid: float | None
    best_ask: float | None
    bid_size: float | None
    ask_size: float | None
    mid: float | None
    spread: float | None
    relative_spread: float | None
    microprice: float | None
    #: ``microprice - mid``. The sign is the direction the resting size leans.
    microprice_tilt: float | None
    bid_depth: float
    ask_depth: float
    imbalance: float | None
    weighted_imbalance: float | None
    bid_slope: SlopeEstimate | None
    ask_slope: SlopeEstimate | None
    bid_concentration: float | None
    ask_concentration: float | None
    bid_levels: int
    ask_levels: int
    is_crossed: bool
    is_locked: bool
    unavailable: dict[str, str]

    def to_dict(self) -> dict:
        return {
            "levels": self.levels,
            "weighted_decay": self.weighted_decay,
            "best_bid": self.best_bid,
            "best_ask": self.best_ask,
            "bid_size": self.bid_size,
            "ask_size": self.ask_size,
            "mid": self.mid,
            "spread": self.spread,
            "relative_spread": self.relative_spread,
            "microprice": self.microprice,
            "microprice_tilt": self.microprice_tilt,
            "bid_depth": self.bid_depth,
            "ask_depth": self.ask_depth,
            "imbalance": self.imbalance,
            "weighted_imbalance": self.weighted_imbalance,
            "bid_slope": self.bid_slope.to_dict() if self.bid_slope else None,
            "ask_slope": self.ask_slope.to_dict() if self.ask_slope else None,
            "bid_concentration": self.bid_concentration,
            "ask_concentration": self.ask_concentration,
            "bid_levels": self.bid_levels,
            "ask_levels": self.ask_levels,
            "is_crossed": self.is_crossed,
            "is_locked": self.is_locked,
            "unavailable": dict(self.unavailable),
        }


def _value(outcome, unavailable: dict[str, str], name: str):
    """Unpack ``value | (None, reason)`` and record the reason if there is one."""
    if isinstance(outcome, tuple) and len(outcome) == 2 and outcome[0] is None:
        unavailable[name] = str(outcome[1])
        return None
    return outcome


def analyse_book(book: Book, levels: int = 5, weighted_decay: float = 0.5) -> BookAnalytics:
    """Measure one snapshot, recording every measurement it could not support."""
    unavailable: dict[str, str] = {}

    micro = _value(microprice(book), unavailable, "microprice")
    mid = book.mid
    bid_depth, ask_depth = depth(book, levels)

    concentration: dict[str, float | None] = {}
    for name, side in (("bid_concentration", book.bids), ("ask_concentration", book.asks)):
        outcome = depth_concentration(side, levels)
        if outcome[0] is None:
            unavailable[name] = str(outcome[1])
            concentration[name] = None
        else:
            concentration[name] = outcome[0]

    return BookAnalytics(
        levels=levels,
        weighted_decay=weighted_decay,
        best_bid=book.best_bid,
        best_ask=book.best_ask,
        bid_size=book.bids.best_size,
        ask_size=book.asks.best_size,
        mid=mid,
        spread=book.spread,
        relative_spread=book.relative_spread,
        microprice=micro,
        microprice_tilt=None if (micro is None or mid is None) else micro - mid,
        bid_depth=bid_depth,
        ask_depth=ask_depth,
        imbalance=_value(imbalance(book, levels), unavailable, "imbalance"),
        weighted_imbalance=_value(
            weighted_imbalance(book, levels, weighted_decay), unavailable, "weighted_imbalance"
        ),
        bid_slope=_value(book_slope(book, True, levels), unavailable, "bid_slope"),
        ask_slope=_value(book_slope(book, False, levels), unavailable, "ask_slope"),
        bid_concentration=concentration["bid_concentration"],
        ask_concentration=concentration["ask_concentration"],
        bid_levels=len(book.bids),
        ask_levels=len(book.asks),
        is_crossed=book.is_crossed,
        is_locked=book.is_locked,
        unavailable=unavailable,
    )
