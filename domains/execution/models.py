"""Execution domain model.

Two rules shape it.

**Executions are append-only.** A fill happened; a correction is a new row, not
an edit. A trade log that can be rewritten cannot support a cost analysis that
anyone should act on.

**A parent order is either stated or inferred, and which one is recorded.**
Grouping child fills into a parent decides every benchmark that follows — the
arrival price, the window, the shortfall — so an inferred grouping is flagged as
inferred rather than presented as though the file had said so.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum


class ExecutionError(ValueError):
    pass


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        """+1 for a buy, -1 for a sell.

        Used to make a cost positive whichever way the trade went: paying more
        than the benchmark on a buy and receiving less than it on a sell are the
        same thing, and reporting one as a gain would be a sign error with a
        very confident face.
        """
        return 1 if self is Side.BUY else -1

    @classmethod
    def parse(cls, value: str) -> Side:
        token = str(value).strip().upper()
        if token in {"BUY", "B", "BOT", "BOUGHT", "LONG", "+"}:
            return cls.BUY
        if token in {"SELL", "S", "SLD", "SOLD", "SHORT", "-"}:
            return cls.SELL
        raise ValueError(f"unrecognised side: {value!r}")


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"
    STOP_LIMIT = "STOP_LIMIT"
    UNKNOWN = "UNKNOWN"

    @classmethod
    def parse(cls, value: str | None) -> OrderType:
        if not value:
            return cls.UNKNOWN
        token = str(value).strip().upper().replace(" ", "_").replace("-", "_")
        aliases = {
            "MKT": cls.MARKET,
            "MARKET": cls.MARKET,
            "LMT": cls.LIMIT,
            "LIMIT": cls.LIMIT,
            "SL": cls.STOP_LIMIT,
            "STOP_LOSS": cls.STOP,
            "STOP": cls.STOP,
            "STOP_LIMIT": cls.STOP_LIMIT,
        }
        return aliases.get(token, cls.UNKNOWN)


class GroupingMethod(StrEnum):
    #: The file named the parent. Nothing was guessed.
    EXPLICIT = "EXPLICIT"
    #: Fills were grouped by instrument, side and a contiguous time window.
    #: Flagged, because a different grouping produces different benchmarks.
    INFERRED_BY_TIME = "INFERRED_BY_TIME"


class ExecutionSource(StrEnum):
    CSV_IMPORT = "CSV_IMPORT"
    MANUAL = "MANUAL"
    BROKER_API = "BROKER_API"


#: Fills further apart than this are treated as separate parent orders when the
#: file does not name one. A stated convention, and a parameter of the import.
DEFAULT_PARENT_GAP_SECONDS = 300.0


@dataclass(frozen=True, slots=True)
class Execution:
    """One fill. Immutable, because it already happened."""

    id: uuid.UUID
    user_id: uuid.UUID
    instrument_id: uuid.UUID
    side: Side
    quantity: Decimal
    execution_price: Decimal
    exchange_timestamp: datetime
    receive_timestamp: datetime | None = None
    #: The broker's own identifiers, kept verbatim for reconciliation.
    order_id: str | None = None
    parent_order_key: str | None = None
    order_type: OrderType = OrderType.UNKNOWN
    limit_price: Decimal | None = None
    #: The quantity the parent order asked for, when the file says. Without it
    #: nothing can be said about what went unfilled, and the opportunity cost is
    #: reported as unavailable rather than assumed to be zero.
    order_quantity: Decimal | None = None
    #: When the decision or the order was made, if the file said. Without it the
    #: arrival benchmark falls back to a proxy and says so.
    submit_timestamp: datetime | None = None
    decision_timestamp: datetime | None = None
    broker: str | None = None
    fees: Decimal = Decimal(0)
    venue: str | None = None
    source: ExecutionSource = ExecutionSource.CSV_IMPORT
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.quantity <= 0:
            raise ExecutionError(
                f"a fill's quantity is how much traded and must be positive, got "
                f"{self.quantity}; direction is carried by side, not by the sign"
            )
        if self.execution_price < 0:
            raise ExecutionError(
                f"a negative execution price is not a fill: {self.execution_price}"
            )
        if self.fees < 0:
            raise ExecutionError(
                f"negative fees are a rebate and need their own field, not a sign: {self.fees}"
            )

    @property
    def signed_quantity(self) -> Decimal:
        return self.quantity if self.side is Side.BUY else -self.quantity

    @property
    def notional(self) -> Decimal:
        return self.quantity * self.execution_price

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "instrument_id": str(self.instrument_id),
            "side": str(self.side),
            "quantity": format(self.quantity, "f"),
            "execution_price": format(self.execution_price, "f"),
            "exchange_timestamp": self.exchange_timestamp.isoformat(),
            "receive_timestamp": (
                self.receive_timestamp.isoformat() if self.receive_timestamp else None
            ),
            "order_id": self.order_id,
            "parent_order_key": self.parent_order_key,
            "order_type": str(self.order_type),
            "limit_price": _fmt(self.limit_price),
            "order_quantity": _fmt(self.order_quantity),
            "submit_timestamp": (
                self.submit_timestamp.isoformat() if self.submit_timestamp else None
            ),
            "decision_timestamp": (
                self.decision_timestamp.isoformat() if self.decision_timestamp else None
            ),
            "broker": self.broker,
            "fees": format(self.fees, "f"),
            "venue": self.venue,
            "source": str(self.source),
        }


@dataclass(frozen=True, slots=True)
class ParentOrder:
    """A trading decision, and the fills that carried it out."""

    key: str
    instrument_id: uuid.UUID
    side: Side
    executions: tuple[Execution, ...]
    grouping_method: GroupingMethod
    canonical_key: str | None = None
    symbol: str | None = None
    #: Multiplier and currency of the instrument, so a cost in currency means
    #: something. One contract of 75 is not one unit.
    multiplier: Decimal = Decimal(1)
    currency: str = "INR"

    def __post_init__(self) -> None:
        if not self.executions:
            raise ExecutionError("a parent order with no fills is not a parent order")
        if any(execution.side is not self.side for execution in self.executions):
            raise ExecutionError(
                "a parent order groups fills on one side; a two-sided group is "
                "two decisions and benchmarking it as one would be meaningless"
            )

    @property
    def ordered(self) -> tuple[Execution, ...]:
        return tuple(sorted(self.executions, key=lambda item: item.exchange_timestamp))

    @property
    def filled_quantity(self) -> Decimal:
        return sum((execution.quantity for execution in self.executions), Decimal(0))

    @property
    def fees(self) -> Decimal:
        return sum((execution.fees for execution in self.executions), Decimal(0))

    @property
    def average_price(self) -> Decimal:
        """The execution's own volume-weighted average price.

        This is the number *being measured*, not a benchmark. Comparing it with
        itself would always show zero cost, which is why the benchmarks it is
        measured against come from market data and are named separately.
        """
        total = self.filled_quantity
        if total == 0:
            raise ExecutionError("a parent order with zero filled quantity has no average price")
        return (
            sum(
                (execution.quantity * execution.execution_price for execution in self.executions),
                Decimal(0),
            )
            / total
        )

    @property
    def first_fill(self) -> Execution:
        return self.ordered[0]

    @property
    def last_fill(self) -> Execution:
        return self.ordered[-1]

    @property
    def start(self) -> datetime:
        """When the order started, preferring the stated submit time.

        The distinction matters: the window from submission to last fill
        includes the delay before the first fill, and that delay is a real part
        of the cost.
        """
        submits = [e.submit_timestamp for e in self.executions if e.submit_timestamp]
        return min([*submits, self.first_fill.exchange_timestamp])

    @property
    def end(self) -> datetime:
        return self.last_fill.exchange_timestamp

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    @property
    def order_quantity(self) -> Decimal | None:
        """What the order asked for, if the file said. Never inferred.

        Assuming the order was fully filled because the log only shows fills is
        how an unfilled order silently reports zero opportunity cost.
        """
        stated = [e.order_quantity for e in self.executions if e.order_quantity is not None]
        return max(stated) if stated else None

    @property
    def unfilled_quantity(self) -> Decimal | None:
        stated = self.order_quantity
        if stated is None:
            return None
        return max(stated - self.filled_quantity, Decimal(0))

    @property
    def has_submit_timestamp(self) -> bool:
        return any(execution.submit_timestamp is not None for execution in self.executions)

    @property
    def decision_timestamp(self) -> datetime | None:
        stamps = [e.decision_timestamp for e in self.executions if e.decision_timestamp]
        return min(stamps) if stamps else None

    @property
    def is_inferred(self) -> bool:
        return self.grouping_method is GroupingMethod.INFERRED_BY_TIME

    def to_dict(self, include_executions: bool = False) -> dict:
        payload = {
            "key": self.key,
            "instrument_id": str(self.instrument_id),
            "canonical_key": self.canonical_key,
            "symbol": self.symbol,
            "side": str(self.side),
            "grouping_method": str(self.grouping_method),
            "grouping_is_inferred": self.is_inferred,
            "fills": len(self.executions),
            "filled_quantity": format(self.filled_quantity, "f"),
            "multiplier": format(self.multiplier, "f"),
            "currency": self.currency,
            "average_price": format(self.average_price, "f"),
            "order_quantity": _fmt(self.order_quantity),
            "unfilled_quantity": _fmt(self.unfilled_quantity),
            "fees": format(self.fees, "f"),
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "duration_seconds": self.duration.total_seconds(),
            "has_submit_timestamp": self.has_submit_timestamp,
            "decision_timestamp": (
                self.decision_timestamp.isoformat() if self.decision_timestamp else None
            ),
        }
        if include_executions:
            payload["executions"] = [execution.to_dict() for execution in self.ordered]
        return payload


def group_executions(
    executions: list[Execution],
    instruments: dict[uuid.UUID, object] | None = None,
    max_gap_seconds: float = DEFAULT_PARENT_GAP_SECONDS,
) -> list[ParentOrder]:
    """Group fills into parent orders, preferring what the file said.

    Fills carrying a ``parent_order_key`` are grouped by it and nothing is
    guessed. The rest are grouped by instrument and side into runs of fills no
    more than ``max_gap_seconds`` apart, and every such group is marked
    ``INFERRED_BY_TIME``. That flag is not decoration: a different gap produces
    a different set of parents, different windows and different benchmarks, and
    the reader has to be able to see which they are looking at.
    """
    instruments = instruments or {}
    explicit: dict[tuple[str, uuid.UUID, Side], list[Execution]] = {}
    loose: dict[tuple[uuid.UUID, Side], list[Execution]] = {}

    for execution in executions:
        if execution.parent_order_key:
            key = (execution.parent_order_key, execution.instrument_id, execution.side)
            explicit.setdefault(key, []).append(execution)
        else:
            loose.setdefault((execution.instrument_id, execution.side), []).append(execution)

    parents: list[ParentOrder] = []
    for (key, instrument_id, side), fills in explicit.items():
        parents.append(
            _parent(key, instrument_id, side, fills, GroupingMethod.EXPLICIT, instruments)
        )

    for (instrument_id, side), fills in loose.items():
        ordered = sorted(fills, key=lambda item: item.exchange_timestamp)
        run: list[Execution] = [ordered[0]]
        for execution in ordered[1:]:
            gap = (execution.exchange_timestamp - run[-1].exchange_timestamp).total_seconds()
            if gap > max_gap_seconds:
                parents.append(
                    _parent(
                        _inferred_key(instrument_id, side, run),
                        instrument_id,
                        side,
                        run,
                        GroupingMethod.INFERRED_BY_TIME,
                        instruments,
                    )
                )
                run = [execution]
            else:
                run.append(execution)
        parents.append(
            _parent(
                _inferred_key(instrument_id, side, run),
                instrument_id,
                side,
                run,
                GroupingMethod.INFERRED_BY_TIME,
                instruments,
            )
        )

    parents.sort(key=lambda parent: (parent.start, parent.key))
    return parents


def _inferred_key(instrument_id: uuid.UUID, side: Side, fills: list[Execution]) -> str:
    stamp = min(fill.exchange_timestamp for fill in fills)
    return f"inferred:{instrument_id}:{side}:{stamp.isoformat()}"


def _parent(
    key: str,
    instrument_id: uuid.UUID,
    side: Side,
    fills: list[Execution],
    method: GroupingMethod,
    instruments: dict,
) -> ParentOrder:
    instrument = instruments.get(instrument_id)
    return ParentOrder(
        key=key,
        instrument_id=instrument_id,
        side=side,
        executions=tuple(fills),
        grouping_method=method,
        canonical_key=getattr(instrument, "canonical_key", None),
        symbol=getattr(instrument, "symbol", None),
        multiplier=getattr(instrument, "multiplier", Decimal(1)),
        currency=getattr(instrument, "currency", "INR"),
    )


def _fmt(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")
