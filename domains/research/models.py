"""What a backtest is made of: signals, fills, positions and a book.

One naming decision is deliberate and worth stating. A strategy here produces a
**target position** — LONG, SHORT or FLAT, with a target weight — and not a BUY
or SELL instruction. That is not squeamishness about words: the platform's
language policy forbids emitting a trading signal to a user, and the difference
between "this simulated book was 40% long here" and "buy this" is the difference
between a research result and advice. The engine converts target weights into
orders because a simulation has to; nothing in this phase evaluates a strategy on
*today's* market and hands the answer to anyone.

Money is ``Decimal`` throughout. A year of daily rebalances accumulates float
error into the equity curve, and an equity curve is the one number the whole
phase exists to produce.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from domains.research.costs import TradeCost


class TargetPosition(StrEnum):
    """The position a strategy wants held, not an instruction to trade.

    Describes a state of the simulated book. The engine works out what orders
    reach it from where the book is now.
    """

    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"


class ExecutionTiming(StrEnum):
    """When a decision taken on bar ``t`` is filled.

    Both options are causal. There is deliberately no "same bar's close" option:
    a decision that used bar ``t``'s close cannot also be filled at it, and
    offering the choice would make the most common backtest error a
    configuration setting.
    """

    NEXT_OPEN = "NEXT_OPEN"
    NEXT_CLOSE = "NEXT_CLOSE"


@dataclass(frozen=True, slots=True)
class Signal:
    """One strategy's view at one instant.

    ``reason`` is required rather than optional. A signal nobody can explain is
    a signal nobody can debug, and the reason is what appears beside a trade in
    the run's own record.
    """

    instrument_id: uuid.UUID
    timestamp: datetime
    position: TargetPosition
    #: Fraction of equity to hold. Signed: negative is short. The engine clamps
    #: it against the configured gross and per-instrument limits and reports
    #: when it did.
    target_weight: Decimal
    reason: str
    #: The strategy's own confidence in ``[0, 1]``, where it has one. ``None``
    #: means the strategy does not produce one — which is different from zero
    #: confidence and is never collapsed into it.
    confidence: float | None = None
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "instrument_id": str(self.instrument_id),
            "timestamp": self.timestamp.isoformat(),
            "position": str(self.position),
            "target_weight": format(self.target_weight, "f"),
            "reason": self.reason,
            "confidence": self.confidence,
            "metadata": self.metadata,
        }


@dataclass(frozen=True, slots=True)
class Fill:
    """One simulated execution."""

    instrument_id: uuid.UUID
    timestamp: datetime
    quantity: Decimal
    #: The price the fill is booked at, after slippage.
    price: Decimal
    #: The price before slippage, kept so the two are never confused and the
    #: slippage charge is recomputable from the record.
    reference_price: Decimal
    cost: TradeCost
    reason: str = ""

    @property
    def is_buy(self) -> bool:
        return self.quantity > 0

    @property
    def notional(self) -> Decimal:
        return self.price * self.quantity

    @property
    def slippage_cost(self) -> Decimal:
        """Adverse price movement paid, always non-negative."""
        return abs(self.price - self.reference_price) * abs(self.quantity)

    def to_dict(self) -> dict:
        return {
            "instrument_id": str(self.instrument_id),
            "timestamp": self.timestamp.isoformat(),
            "quantity": format(self.quantity, "f"),
            "price": format(self.price, "f"),
            "reference_price": format(self.reference_price, "f"),
            "notional": format(self.notional, "f"),
            "slippage_cost": format(self.slippage_cost, "f"),
            "cost": self.cost.to_dict(),
            "reason": self.reason,
        }


@dataclass(slots=True)
class Position:
    """A holding, with the average price it was built at."""

    instrument_id: uuid.UUID
    quantity: Decimal = Decimal(0)
    average_price: Decimal = Decimal(0)
    #: Profit taken out of closed quantity. Kept separate from unrealised so a
    #: report never has to guess which half a number came from.
    realised_pnl: Decimal = Decimal(0)

    def market_value(self, price: Decimal) -> Decimal:
        return self.quantity * price

    def unrealised_pnl(self, price: Decimal) -> Decimal:
        return (price - self.average_price) * self.quantity

    def apply(self, quantity: Decimal, price: Decimal) -> Decimal:
        """Book a fill. Returns the realised profit it produced.

        Increasing a position moves the average price; reducing one realises
        against it. A position that crosses through zero realises the whole of
        the old side and opens the new one at the fill price — anything else
        would leave an average price mixing a long and a short.
        """
        if quantity == 0:
            return Decimal(0)

        realised = Decimal(0)
        if self.quantity == 0 or (self.quantity > 0) == (quantity > 0):
            total = self.quantity + quantity
            self.average_price = (
                (self.average_price * self.quantity + price * quantity) / total
                if total != 0
                else Decimal(0)
            )
            self.quantity = total
            return realised

        closing = min(abs(quantity), abs(self.quantity))
        direction = Decimal(1) if self.quantity > 0 else Decimal(-1)
        realised = (price - self.average_price) * closing * direction
        self.realised_pnl += realised

        remaining = self.quantity + quantity
        if remaining == 0 or (remaining > 0) == (self.quantity > 0):
            self.quantity = remaining
            if remaining == 0:
                self.average_price = Decimal(0)
        else:
            # Crossed through zero: the old side is fully realised above, and
            # what remains is a new position opened at this fill's price.
            self.quantity = remaining
            self.average_price = price
        return realised

    def to_dict(self, price: Decimal | None = None) -> dict:
        payload = {
            "instrument_id": str(self.instrument_id),
            "quantity": format(self.quantity, "f"),
            "average_price": format(self.average_price, "f"),
            "realised_pnl": format(self.realised_pnl, "f"),
        }
        if price is not None:
            payload["market_value"] = format(self.market_value(price), "f")
            payload["unrealised_pnl"] = format(self.unrealised_pnl(price), "f")
        return payload


@dataclass(slots=True)
class Book:
    """Cash and positions. The simulated portfolio.

    ``cash`` is allowed to go negative: a strategy that leverages beyond its
    equity is running on borrowed money, and the book says so rather than
    silently refusing the trade. Whether that is permitted is a risk limit, and
    risk limits live in the engine where they can be reported when they bind.
    """

    cash: Decimal
    positions: dict[uuid.UUID, Position] = field(default_factory=dict)

    def position(self, instrument_id: uuid.UUID) -> Position:
        if instrument_id not in self.positions:
            self.positions[instrument_id] = Position(instrument_id=instrument_id)
        return self.positions[instrument_id]

    def apply(self, fill: Fill) -> Decimal:
        """Book a fill: cash moves, the position moves, costs come out of cash."""
        position = self.position(fill.instrument_id)
        realised = position.apply(fill.quantity, fill.price)
        self.cash -= fill.notional
        self.cash -= fill.cost.total
        return realised

    def market_value(self, prices: dict[uuid.UUID, Decimal]) -> Decimal:
        return sum(
            (
                position.market_value(prices[instrument_id])
                for instrument_id, position in self.positions.items()
                if instrument_id in prices
            ),
            Decimal(0),
        )

    def equity(self, prices: dict[uuid.UUID, Decimal]) -> Decimal:
        return self.cash + self.market_value(prices)

    def gross_exposure(self, prices: dict[uuid.UUID, Decimal]) -> Decimal:
        return sum(
            (
                abs(position.market_value(prices[instrument_id]))
                for instrument_id, position in self.positions.items()
                if instrument_id in prices
            ),
            Decimal(0),
        )

    def to_dict(self, prices: dict[uuid.UUID, Decimal] | None = None) -> dict:
        prices = prices or {}
        return {
            "cash": format(self.cash, "f"),
            "equity": format(self.equity(prices), "f") if prices else None,
            "positions": [
                position.to_dict(prices.get(instrument_id))
                for instrument_id, position in sorted(
                    self.positions.items(), key=lambda item: str(item[0])
                )
                if position.quantity != 0
            ],
        }
