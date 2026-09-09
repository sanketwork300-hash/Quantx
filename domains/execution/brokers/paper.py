"""The paper broker.

Build spec §23 requires paper trading to use the same order interface as live
trading, so this is a :class:`BrokerAdapter` like any other rather than a branch
inside the OMS. That constraint is what makes the Phase 7 live adapter a
substitution instead of a rewrite.

The adapter is thin on purpose. It holds a quote source and a fill policy, asks
:func:`decide_fill` what happens, and returns the answer in the platform's
vocabulary. Every judgement is in the pure engine next door; everything here is
plumbing, and there is no state, so two paper accounts cannot interfere.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal

from domains.execution.brokers.base import (
    BrokerAccount,
    BrokerAdapter,
    BrokerCapability,
    BrokerFill,
    BrokerOrderUpdate,
    BrokerPosition,
)
from domains.execution.brokers.paper_fills import (
    FillContext,
    PaperFillPolicy,
    decide_fill,
)
from domains.execution.oms.models import (
    FillFlag,
    OrderRequest,
    OrderStatus,
    OrderType,
    RejectionReason,
    TimeInForce,
)
from domains.market_data.models import Quote

PAPER = "paper"

#: A quote source: instrument id in, the newest quote held for it out.
QuoteSource = Callable[[uuid.UUID], Awaitable[Quote | None]]


class PaperBroker(BrokerAdapter):
    """Fills orders against observed quotes, and charges nothing it was not told to.

    Notably **without** ``MODIFY``. A paper modify would have to invent a queue
    position to say what happens to the order's place in the book, and there is
    no book. The OMS reads the capability set and refuses the instruction with
    a reason rather than pretending the amendment took effect.
    """

    name = PAPER
    capabilities = frozenset(
        {
            BrokerCapability.MARKET_ORDERS,
            BrokerCapability.LIMIT_ORDERS,
            BrokerCapability.CANCEL,
            BrokerCapability.POSITIONS,
            BrokerCapability.ORDERS,
            BrokerCapability.IMMEDIATE_OR_CANCEL,
        }
    )

    def __init__(
        self,
        quotes: QuoteSource,
        *,
        policy: PaperFillPolicy = PaperFillPolicy.QUOTE_ONLY,
        max_quote_age_seconds: float = 30.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._quotes = quotes
        self._policy = policy
        self._max_quote_age_seconds = max_quote_age_seconds
        self._clock = clock or (lambda: datetime.now(UTC))

    # ------------------------------------------------------------ placement
    async def place_order(self, request: OrderRequest) -> BrokerOrderUpdate:
        return await self.offer(
            instrument_id=request.instrument_id,
            side=request.side,
            remaining_quantity=request.quantity,
            order_type=request.order_type,
            time_in_force=request.time_in_force,
            limit_price=request.limit_price,
        )

    async def offer(
        self,
        *,
        instrument_id: uuid.UUID,
        side,
        remaining_quantity: Decimal,
        order_type: OrderType,
        time_in_force: TimeInForce,
        limit_price: Decimal | None,
    ) -> BrokerOrderUpdate:
        """Offer the current market to an order, new or already resting.

        The same call serves both, which is the point: a resting order that
        becomes marketable later must be filled by exactly the code that would
        have filled it on arrival, or the two paths drift.
        """
        as_of = self._clock()
        quote = await self._quotes(instrument_id)
        decision = decide_fill(
            FillContext(
                side=side,
                order_type=order_type,
                remaining_quantity=remaining_quantity,
                time_in_force=time_in_force,
                limit_price=limit_price,
            ),
            quote,
            as_of,
            policy=self._policy,
            max_quote_age_seconds=self._max_quote_age_seconds,
        )

        if decision.rejection is not None:
            return BrokerOrderUpdate(
                status=OrderStatus.REJECTED,
                broker_order_id=_paper_order_id(),
                rejection_reason=decision.rejection,
                rejection_detail=decision.detail,
                raw={"policy": str(self._policy), "decided_at": as_of.isoformat()},
            )

        if decision.cancel_remainder:
            return BrokerOrderUpdate(
                status=OrderStatus.CANCELLED,
                broker_order_id=_paper_order_id(),
                rejection_detail=decision.detail,
                raw={"policy": str(self._policy), "decided_at": as_of.isoformat()},
            )

        if not decision.fills:
            return BrokerOrderUpdate(
                status=OrderStatus.ACKNOWLEDGED,
                broker_order_id=_paper_order_id(),
                rejection_detail=decision.detail,
                raw={"policy": str(self._policy), "decided_at": as_of.isoformat()},
            )

        signed = decision.quantity * side.sign
        fill = BrokerFill(
            quantity=signed,
            price=decision.price,
            filled_at=as_of,
            price_basis=str(decision.price_basis),
            reference_price=decision.reference_price,
            reference_basis=decision.reference_basis,
            quote_exchange_timestamp=quote.exchange_timestamp if quote else None,
            flags=decision.flags,
        )
        partial = decision.quantity < remaining_quantity
        if partial and not decision.rests:
            # Immediate-or-cancel that filled part: what traded is a fill and
            # what did not is withdrawn. Reported as CANCELLED with the fill
            # attached, because the order is finished either way.
            status = OrderStatus.CANCELLED
        elif partial:
            status = OrderStatus.PARTIALLY_FILLED
        else:
            status = OrderStatus.FILLED
        return BrokerOrderUpdate(
            status=status,
            broker_order_id=_paper_order_id(),
            fills=(fill,),
            rejection_detail=decision.detail,
            raw={"policy": str(self._policy), "decided_at": as_of.isoformat()},
        )

    async def cancel_order(self, broker_order_id: str) -> BrokerOrderUpdate:
        """Always succeeds. There is no queue to lose a place in.

        A live broker can refuse a cancel because the order filled while the
        request was in flight. That race does not exist here, and simulating it
        would be inventing a behaviour rather than modelling one.
        """
        return BrokerOrderUpdate(status=OrderStatus.CANCELLED, broker_order_id=broker_order_id)

    async def modify_order(
        self,
        broker_order_id: str,
        quantity: Decimal | None = None,
        limit_price: Decimal | None = None,
    ) -> BrokerOrderUpdate:
        raise NotImplementedError(
            "the paper broker does not modify orders. An amendment's effect is a "
            "queue position, and there is no queue here to have one. Cancel and "
            "replace instead, which is what the amendment would have cost anyway"
        )

    async def get_positions(self) -> list[BrokerPosition]:
        """Empty, and honestly so.

        The paper account's positions are the platform's own records; there is
        no counterparty holding a second copy. Returning the platform's view
        here would make a reconciliation compare a number against itself and
        always agree.
        """
        return []

    async def get_orders(self) -> list[BrokerOrderUpdate]:
        return []

    async def get_account(self) -> BrokerAccount:
        """No balances. The cash is the platform's own and is not a broker's report."""
        return BrokerAccount(
            broker=PAPER,
            reported_at=self._clock(),
            raw={
                "note": (
                    "a paper account has no broker holding a balance. Cash and "
                    "positions come from the platform's own records"
                )
            },
        )


def _paper_order_id() -> str:
    return f"paper-{uuid.uuid4().hex[:16]}"


#: Flags a caller may want to treat as "this fill rests on less than a full
#: two-sided market". Exported so the API and the frontend agree on what makes a
#: fill worth qualifying, rather than each keeping its own list.
QUALIFIED_FILL_FLAGS = frozenset(
    {
        FillFlag.DEPTH_NOT_REPORTED,
        FillFlag.LAST_TRADE_NOT_A_QUOTE,
        FillFlag.LIMITED_BY_DEPTH,
    }
)


__all__ = [
    "PAPER",
    "QUALIFIED_FILL_FLAGS",
    "PaperBroker",
    "PaperFillPolicy",
    "QuoteSource",
    "RejectionReason",
]
