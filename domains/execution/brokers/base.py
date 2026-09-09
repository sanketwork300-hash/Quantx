"""The broker interface, and what a broker is allowed to be asked.

Build spec §24 is explicit that Quantx must not be coupled to one broker, and
§23 that paper trading uses the same interface as live trading. Both are easy to
claim and hard to keep, so the interface is deliberately narrow: six methods,
each returning a platform type rather than a broker's payload, and a declared
capability set so the OMS can refuse an instruction before sending it rather
than discover mid-flight that this broker has no modify endpoint.

Nothing here reaches for a broker's numbers and calls them the platform's. A
figure a broker reports is stored as a broker-reported figure, with its name and
the time it was reported, and is never blended into one the platform computed.
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from domains.execution.oms.models import (
    FillFlag,
    OrderRequest,
    OrderStatus,
    OrderType,
    RejectionReason,
    TimeInForce,
)


class BrokerError(Exception):
    """The broker could not be reached, or answered with something unusable."""


class BrokerRejected(BrokerError):
    """The broker took the order and refused it. Not a transport failure."""

    def __init__(self, detail: str, code: str | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.code = code


class BrokerCapability(StrEnum):
    """What an adapter can actually do.

    The same pattern as ``ProviderCapability`` on the market-data side, and for
    the same reason: a caller that has to try an operation to find out whether
    it exists writes exception handling where a check belongs.
    """

    MARKET_ORDERS = "MARKET_ORDERS"
    LIMIT_ORDERS = "LIMIT_ORDERS"
    MODIFY = "MODIFY"
    CANCEL = "CANCEL"
    POSITIONS = "POSITIONS"
    ORDERS = "ORDERS"
    ACCOUNT = "ACCOUNT"
    IMMEDIATE_OR_CANCEL = "IMMEDIATE_OR_CANCEL"


#: Which capability each order type and time-in-force needs, so the check is a
#: lookup rather than a chain of conditionals repeated at every call site.
ORDER_TYPE_CAPABILITY: dict[OrderType, BrokerCapability] = {
    OrderType.MARKET: BrokerCapability.MARKET_ORDERS,
    OrderType.LIMIT: BrokerCapability.LIMIT_ORDERS,
}

TIME_IN_FORCE_CAPABILITY: dict[TimeInForce, BrokerCapability | None] = {
    TimeInForce.DAY: None,
    TimeInForce.IMMEDIATE_OR_CANCEL: BrokerCapability.IMMEDIATE_OR_CANCEL,
}


@dataclass(frozen=True, slots=True)
class BrokerFill:
    """One execution as the broker reports it, before the platform books it."""

    quantity: Decimal
    price: Decimal
    filled_at: datetime
    price_basis: str
    reference_price: Decimal | None = None
    reference_basis: str | None = None
    quote_exchange_timestamp: datetime | None = None
    broker_trade_id: str | None = None
    flags: tuple[FillFlag, ...] = ()


@dataclass(frozen=True, slots=True)
class BrokerOrderUpdate:
    """What a broker says about an order, in the platform's vocabulary.

    ``raw`` keeps the broker's own payload. It is not decoration: when a mapping
    turns out to be wrong months later, the only way to establish what the
    broker actually said is to have kept it.
    """

    status: OrderStatus
    broker_order_id: str | None = None
    fills: tuple[BrokerFill, ...] = ()
    rejection_reason: RejectionReason | None = None
    rejection_detail: str | None = None
    #: Fields present in the broker's payload that this adapter does not map.
    #: A silently ignored field is how a schema change becomes a silent bug.
    unmapped_fields: tuple[str, ...] = ()
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BrokerPosition:
    """A position as the broker holds it.

    Kept separate from the platform's own position so the two can be compared.
    A reconciliation that overwrote one with the other would destroy the only
    evidence that they had ever disagreed.
    """

    instrument_key: str
    quantity: Decimal
    average_price: Decimal | None
    instrument_id: uuid.UUID | None = None
    reported_at: datetime | None = None
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class BrokerAccount:
    """Balances as the broker reports them.

    Every field is prefixed with how it got here. The platform computes no
    margin figure of its own — build spec 1.1 forbids inventing a broker's
    formula — so what appears here is a passed-through observation, attributed
    and timestamped, and callers can see that is what it is.
    """

    broker: str
    reported_at: datetime
    reported_available_cash: Decimal | None = None
    reported_available_margin: Decimal | None = None
    reported_used_margin: Decimal | None = None
    currency: str = "INR"
    unmapped_fields: tuple[str, ...] = ()
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "broker": self.broker,
            "reported_at": self.reported_at.isoformat(),
            "reported_available_cash": (
                format(self.reported_available_cash, "f")
                if self.reported_available_cash is not None
                else None
            ),
            "reported_available_margin": (
                format(self.reported_available_margin, "f")
                if self.reported_available_margin is not None
                else None
            ),
            "reported_used_margin": (
                format(self.reported_used_margin, "f")
                if self.reported_used_margin is not None
                else None
            ),
            "currency": self.currency,
            "unmapped_fields": list(self.unmapped_fields),
        }


class BrokerAdapter(ABC):
    """The six operations of build spec §24.

    An adapter translates. It does not decide: no risk check, no sizing, no
    retry policy that could turn one instruction into two orders. Those belong
    to the OMS, where they can be recorded.
    """

    #: Stable identifier written onto every order this adapter places.
    name: str = "unnamed"
    capabilities: frozenset[BrokerCapability] = frozenset()

    def supports(self, capability: BrokerCapability) -> bool:
        return capability in self.capabilities

    def missing_capability(self, request: OrderRequest) -> BrokerCapability | None:
        """The capability this request needs and this adapter lacks, if any."""
        needed = [ORDER_TYPE_CAPABILITY[request.order_type]]
        tif = TIME_IN_FORCE_CAPABILITY[request.time_in_force]
        if tif is not None:
            needed.append(tif)
        for capability in needed:
            if not self.supports(capability):
                return capability
        return None

    @abstractmethod
    async def place_order(self, request: OrderRequest) -> BrokerOrderUpdate: ...

    @abstractmethod
    async def cancel_order(self, broker_order_id: str) -> BrokerOrderUpdate: ...

    @abstractmethod
    async def modify_order(
        self,
        broker_order_id: str,
        quantity: Decimal | None = None,
        limit_price: Decimal | None = None,
    ) -> BrokerOrderUpdate: ...

    @abstractmethod
    async def get_positions(self) -> list[BrokerPosition]: ...

    @abstractmethod
    async def get_orders(self) -> list[BrokerOrderUpdate]: ...

    @abstractmethod
    async def get_account(self) -> BrokerAccount: ...
