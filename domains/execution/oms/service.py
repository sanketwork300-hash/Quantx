"""The order management system.

Build spec §25's flow — signal, portfolio target, order generator, risk
validation, OMS, broker, execution report, portfolio — meets the database here.
The service owns four things and delegates everything else:

* the **lifecycle**, through the declared transition table;
* the **gate**, run before any broker is contacted;
* the **book**, through the accounting already written for backtests;
* the **audit trail**, written on every decision including the ones that
  refused.

What it does not own is worth stating. It does not decide what to trade, price
anything, or invent a cost. It never resizes an order to make it pass a limit.
And it never writes a status without going through :func:`check_transition`, so
an impossible sequence fails loudly instead of being recorded as fact.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from domains.execution.brokers.base import (
    BrokerAdapter,
    BrokerError,
    BrokerOrderUpdate,
    BrokerRejected,
)
from domains.execution.brokers.paper import PAPER, PaperBroker
from domains.execution.brokers.paper_fills import PaperFillPolicy
from domains.execution.brokers.upstox import (
    BROKER_NAME as UPSTOX,
)
from domains.execution.brokers.upstox import (
    HttpUpstoxOrderTransport,
    UpstoxBroker,
    UpstoxOrderEndpoints,
)
from domains.execution.oms.algorithms import ParentOrderPlan
from domains.execution.oms.models import (
    FillFlag,
    Order,
    OrderFill,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    OrderVenue,
    Rejection,
    RejectionReason,
    TimeInForce,
    check_transition,
)
from domains.execution.oms.orm import OrderFillORM, OrderORM, TradingAccountORM
from domains.execution.oms.repository import TradingRepository
from domains.execution.oms.risk_gate import (
    AccountSnapshot,
    GateDecision,
    RiskLimits,
    evaluate,
)
from domains.instruments.service import InstrumentService
from domains.market_data.models import Quote
from domains.reports.envelope import AnalyticalResult
from domains.reports.provenance import Provenance
from domains.reports.warnings import AnalyticalWarning
from domains.research.costs import (
    NO_COST_MODEL,
    ChargedComponent,
    CostSchedule,
    TradeCost,
)
from domains.research.costs import schedule_from_components as _schedule_from_components
from domains.research.models import Book, Fill
from infrastructure.settings import Settings

MODEL_VERSION = "1.0.0"


class TradingError(ValueError):
    """The request cannot be carried out as asked."""


class AccountNotFound(TradingError):
    pass


class OrderNotFound(TradingError):
    pass


class WarningCode:
    NO_COST_SCHEDULE = "TRADING_NO_COST_SCHEDULE"
    NO_RISK_LIMITS = "TRADING_NO_RISK_LIMITS"
    FILL_QUALIFIED = "TRADING_FILL_QUALIFIED"
    UNPRICED_POSITIONS = "TRADING_UNPRICED_POSITIONS"
    MULTIPLIER_ASSUMED = "TRADING_MULTIPLIER_ASSUMED"
    KILL_SWITCH = "TRADING_KILL_SWITCH_ENGAGED"
    IDEMPOTENT_REPLAY = "TRADING_IDEMPOTENT_REPLAY"
    SLICES_MISSED = "TRADING_SCHEDULE_SLICES_MISSED"
    NO_VOLUME_PROFILE = "TRADING_NO_VOLUME_PROFILE"


class EventType:
    """Audit event kinds. Strings rather than an enum on the row so a future
    event type does not require a migration to be recordable."""

    SUBMITTED = "SUBMITTED"
    GATE = "GATE_DECISION"
    BROKER_REQUEST = "BROKER_REQUEST"
    BROKER_RESPONSE = "BROKER_RESPONSE"
    TRANSITION = "TRANSITION"
    FILL = "FILL"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    KILL_SWITCH = "KILL_SWITCH"
    ARMED = "LIVE_ARMED"
    REPLAY = "IDEMPOTENT_REPLAY"


@dataclass(frozen=True, slots=True)
class AccountView:
    """An account and the state of its book."""

    id: uuid.UUID
    user_id: uuid.UUID
    name: str
    venue: OrderVenue
    broker: str
    base_currency: str
    cash: Decimal
    opening_cash: Decimal
    cost_schedule: CostSchedule
    fill_policy: PaperFillPolicy
    max_quote_age_seconds: int
    risk_limits: RiskLimits
    kill_switch_engaged_at: datetime | None
    kill_switch_reason: str | None
    live_armed_at: datetime | None
    created_at: datetime

    @property
    def kill_switch_engaged(self) -> bool:
        return self.kill_switch_engaged_at is not None

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "name": self.name,
            "venue": str(self.venue),
            "broker": self.broker,
            "base_currency": self.base_currency,
            "cash": format(self.cash, "f"),
            "opening_cash": format(self.opening_cash, "f"),
            "cost_schedule": self.cost_schedule.to_dict(),
            "fill_policy": str(self.fill_policy),
            "max_quote_age_seconds": self.max_quote_age_seconds,
            "risk_limits": self.risk_limits.to_dict(),
            "kill_switch_engaged": self.kill_switch_engaged,
            "kill_switch_engaged_at": (
                self.kill_switch_engaged_at.isoformat() if self.kill_switch_engaged_at else None
            ),
            "kill_switch_reason": self.kill_switch_reason,
            "live_armed_at": self.live_armed_at.isoformat() if self.live_armed_at else None,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class SubmissionOutcome:
    """What happened to a submission, gate decision included.

    The gate travels with the answer whether the order was accepted or not. A
    user whose order passed still wants to know which limits were checked and
    which were not configured, and an interface that shows the checks only on
    failure teaches people that no news is good news.
    """

    order: Order
    gate: GateDecision
    #: True when this submission matched an existing ``client_order_id`` and no
    #: new order was placed.
    replayed: bool = False

    def to_dict(self) -> dict:
        return {
            "order": self.order.to_dict(),
            "gate": self.gate.to_dict(),
            "replayed": self.replayed,
        }


@dataclass(frozen=True, slots=True)
class PositionView:
    instrument_id: uuid.UUID
    symbol: str | None
    quantity: Decimal
    average_price: Decimal
    realised_pnl: Decimal
    fees_paid: Decimal
    mark_price: Decimal | None = None
    mark_basis: str | None = None
    mark_exchange_timestamp: datetime | None = None
    mark_age_seconds: float | None = None

    @property
    def market_value(self) -> Decimal | None:
        if self.mark_price is None:
            return None
        return self.quantity * self.mark_price

    @property
    def unrealised_pnl(self) -> Decimal | None:
        """``None`` when there is no mark, never zero.

        An unmarked position is not a position worth nothing. Reporting zero
        here would let an unpriced book add up to a plausible-looking total.
        """
        if self.mark_price is None:
            return None
        return (self.mark_price - self.average_price) * self.quantity

    def to_dict(self) -> dict:
        value = self.market_value
        unrealised = self.unrealised_pnl
        return {
            "instrument_id": str(self.instrument_id),
            "symbol": self.symbol,
            "quantity": format(self.quantity, "f"),
            "average_price": format(self.average_price, "f"),
            "realised_pnl": format(self.realised_pnl, "f"),
            "fees_paid": format(self.fees_paid, "f"),
            "mark_price": format(self.mark_price, "f") if self.mark_price is not None else None,
            "mark_basis": self.mark_basis,
            "mark_exchange_timestamp": (
                self.mark_exchange_timestamp.isoformat() if self.mark_exchange_timestamp else None
            ),
            "mark_age_seconds": self.mark_age_seconds,
            "market_value": format(value, "f") if value is not None else None,
            "unrealised_pnl": format(unrealised, "f") if unrealised is not None else None,
        }


@dataclass(frozen=True, slots=True)
class AccountPnl:
    """The account's P&L, with the parts that could not be valued named.

    ``equity`` is ``None`` when any held position has no mark. That is
    deliberately strict: an equity figure that quietly omits a position is worse
    than no figure, because it looks like an answer.
    """

    account_id: uuid.UUID
    as_of: datetime
    cash: Decimal
    opening_cash: Decimal
    realised_pnl: Decimal
    fees_paid: Decimal
    unrealised_pnl: Decimal | None
    market_value: Decimal | None
    equity: Decimal | None
    costs_modelled: bool
    positions: tuple[PositionView, ...]
    unpriced: tuple[uuid.UUID, ...] = ()

    @property
    def total_pnl(self) -> Decimal | None:
        if self.equity is None:
            return None
        return self.equity - self.opening_cash

    def to_dict(self) -> dict:
        total = self.total_pnl
        return {
            "account_id": str(self.account_id),
            "as_of": self.as_of.isoformat(),
            "cash": format(self.cash, "f"),
            "opening_cash": format(self.opening_cash, "f"),
            "realised_pnl": format(self.realised_pnl, "f"),
            "fees_paid": format(self.fees_paid, "f"),
            "unrealised_pnl": (
                format(self.unrealised_pnl, "f") if self.unrealised_pnl is not None else None
            ),
            "market_value": (
                format(self.market_value, "f") if self.market_value is not None else None
            ),
            "equity": format(self.equity, "f") if self.equity is not None else None,
            "total_pnl": format(total, "f") if total is not None else None,
            "costs_modelled": self.costs_modelled,
            "gross_of_costs": not self.costs_modelled,
            "positions": [item.to_dict() for item in self.positions],
            "unpriced": [str(item) for item in self.unpriced],
        }


@dataclass(frozen=True, slots=True)
class RequiredTrade:
    """One trade needed to reach a target the caller stated.

    Named ``required`` rather than ``recommended`` because that is what it is:
    the difference between where the book is and where the caller said they want
    it. The platform did not choose the destination.
    """

    instrument_id: uuid.UUID
    symbol: str | None
    current_quantity: Decimal
    target_quantity: Decimal
    trade_quantity: Decimal
    side: OrderSide
    mark_price: Decimal | None
    estimated_notional: Decimal | None
    #: Fractional part discarded by whole-unit rounding, reported rather than
    #: absorbed so the residual weight drift is visible.
    rounding_residual: Decimal = Decimal(0)

    def to_dict(self) -> dict:
        return {
            "instrument_id": str(self.instrument_id),
            "symbol": self.symbol,
            "current_quantity": format(self.current_quantity, "f"),
            "target_quantity": format(self.target_quantity, "f"),
            "trade_quantity": format(self.trade_quantity, "f"),
            "side": str(self.side),
            "mark_price": format(self.mark_price, "f") if self.mark_price is not None else None,
            "estimated_notional": (
                format(self.estimated_notional, "f")
                if self.estimated_notional is not None
                else None
            ),
            "rounding_residual": format(self.rounding_residual, "f"),
        }


@dataclass(frozen=True, slots=True)
class RebalancePlan:
    account_id: uuid.UUID
    as_of: datetime
    equity: Decimal | None
    trades: tuple[RequiredTrade, ...]
    #: Instruments in the target that could not be priced, so no trade size
    #: could be computed for them. Not silently omitted.
    unpriced: tuple[uuid.UUID, ...] = ()
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "account_id": str(self.account_id),
            "as_of": self.as_of.isoformat(),
            "equity": format(self.equity, "f") if self.equity is not None else None,
            "trades": [trade.to_dict() for trade in self.trades],
            "unpriced": [str(item) for item in self.unpriced],
            "notes": list(self.notes),
        }


@dataclass
class _Marks:
    """Quotes gathered once and reused, so one request values one market."""

    prices: dict[uuid.UUID, Decimal] = field(default_factory=dict)
    quotes: dict[uuid.UUID, Quote] = field(default_factory=dict)
    as_of: datetime = field(default_factory=lambda: datetime.now(UTC))
    unpriced: list[uuid.UUID] = field(default_factory=list)


class OrderManagementService:
    """Places orders, books fills and keeps the account's story straight."""

    def __init__(
        self,
        session: AsyncSession,
        instruments: InstrumentService,
        quotes,
        settings: Settings,
        *,
        broker_auth=None,
        order_transport=None,
        order_endpoints=None,
        clock=None,
    ) -> None:
        self._session = session
        self.repository = TradingRepository(session)
        self._instruments = instruments
        #: Anything with ``async live_quote(instrument_id) -> LiveQuoteView | None``.
        self._quotes = quotes
        self._settings = settings
        #: The credential vault. Optional: a deployment that only paper trades
        #: never needs one, and a live account without one is refused rather
        #: than attempted with no token.
        self._broker_auth = broker_auth
        #: Seams, so a test can exercise the real adapter against a recorded
        #: payload and no test can reach a live broker.
        self._order_transport = order_transport
        self._order_endpoints = order_endpoints
        self._clock = clock or (lambda: datetime.now(UTC))

    @property
    def _live_trading_enabled(self) -> bool:
        return self._settings.live_trading_enabled

    # ============================================================== accounts
    async def create_account(
        self,
        user_id: uuid.UUID,
        name: str,
        *,
        opening_cash: Decimal,
        venue: OrderVenue = OrderVenue.PAPER,
        broker: str = PAPER,
        base_currency: str = "INR",
        cost_schedule: CostSchedule | None = None,
        fill_policy: PaperFillPolicy = PaperFillPolicy.QUOTE_ONLY,
        max_quote_age_seconds: int = 30,
        risk_limits: RiskLimits | None = None,
    ) -> AccountView:
        if opening_cash < 0:
            raise TradingError("an account cannot open with negative cash")
        schedule = cost_schedule or NO_COST_MODEL
        limits = risk_limits or RiskLimits()
        row = await self.repository.create_account(
            user_id=user_id,
            name=name,
            venue=str(venue),
            broker=broker,
            base_currency=base_currency,
            cash=opening_cash,
            opening_cash=opening_cash,
            cost_schedule=schedule.to_dict(),
            fill_policy=str(fill_policy),
            max_quote_age_seconds=max_quote_age_seconds,
            risk_limits=limits.to_dict(),
        )
        return _account_view(row)

    async def account(self, account_id: uuid.UUID, user_id: uuid.UUID) -> AccountView:
        return _account_view(await self._account_row(account_id, user_id))

    async def accounts(self, user_id: uuid.UUID) -> list[AccountView]:
        return [_account_view(row) for row in await self.repository.list_accounts(user_id)]

    async def set_risk_limits(
        self, account_id: uuid.UUID, user_id: uuid.UUID, limits: RiskLimits
    ) -> AccountView:
        row = await self._account_row(account_id, user_id)
        row.risk_limits = limits.to_dict()
        await self._session.flush()
        await self._event(
            row, None, EventType.GATE, detail="risk limits changed", payload=limits.to_dict()
        )
        return _account_view(row)

    async def engage_kill_switch(
        self, account_id: uuid.UUID, user_id: uuid.UUID, reason: str
    ) -> AccountView:
        """Halt the account, and cancel what is resting.

        Engaging is not merely a flag that blocks new orders: an order already
        working is exposure the switch was pulled to stop. Live orders that the
        broker refuses to cancel are reported rather than marked cancelled, so
        the account never claims to be flat when something is still out there.
        """
        if not reason.strip():
            raise TradingError(
                "a kill switch needs a reason. Whoever finds the account halted "
                "later needs to know why, and 'someone pressed it' is not an answer"
            )
        row = await self._account_row(account_id, user_id)
        now = self._clock()
        row.kill_switch_engaged_at = now
        row.kill_switch_reason = reason
        row.live_armed_at = None
        await self._session.flush()
        await self._event(
            row, None, EventType.KILL_SWITCH, detail=reason, payload={"engaged": True}
        )
        for order in await self.repository.open_orders(account_id):
            await self._cancel_row(row, order, detail=f"kill switch engaged: {reason}")
        return _account_view(row)

    async def arm_for_live(
        self, account_id: uuid.UUID, user_id: uuid.UUID
    ) -> tuple[AccountView, list[str]]:
        """Permit this account to send real orders, or say why it may not.

        Arming is deliberately separate from the deployment's
        ``live_trading_enabled`` flag. One is "this installation is allowed to
        trade real money"; the other is "this book is meant to be trading right
        now". Requiring both means neither a stray configuration change nor a
        stray API call is enough on its own.

        The checks run together and *all* failures are reported. Arming an
        account is something somebody does once, carefully, and telling them
        about one obstacle at a time wastes the care.
        """
        row = await self._account_row(account_id, user_id)
        view = _account_view(row)
        reasons: list[str] = []

        if view.venue is not OrderVenue.LIVE:
            reasons.append(
                "this is a paper account. A paper account cannot be armed for live "
                "trading; open a live account instead, so the venue recorded on every "
                "order it ever placed stays true"
            )
        if not self._live_trading_enabled:
            reasons.append(
                "live trading is disabled for this deployment. That is a "
                "configuration decision and arming does not override it"
            )
        if view.kill_switch_engaged:
            reasons.append(
                f"the kill switch is engaged: {view.kill_switch_reason}. Release it "
                "deliberately before arming"
            )
        if not view.risk_limits.any_set:
            reasons.append(
                "no risk limits are set. An unlimited live account is not a decision "
                "anybody makes deliberately, so it is not one this can be talked into"
            )
        if not view.cost_schedule.models_costs:
            reasons.append(
                "no cost schedule is set, so every P&L figure on this account would be "
                "gross. Tolerable for research, not for money"
            )
        if self._broker_auth is None:
            reasons.append(
                "the service has no access to the credential vault, so there is no token to send"
            )
        else:
            reasons.extend(await self._credential_obstacles(user_id, view))

        if reasons:
            await self._event(
                row,
                None,
                EventType.ARMED,
                detail="arming refused",
                payload={"armed": False, "reasons": reasons},
            )
            return view, reasons

        row.live_armed_at = self._clock()
        await self._session.flush()
        await self._event(
            row, None, EventType.ARMED, detail="armed for live trading", payload={"armed": True}
        )
        return _account_view(row), []

    async def disarm(self, account_id: uuid.UUID, user_id: uuid.UUID) -> AccountView:
        """Stop accepting live orders, without halting the account.

        Distinct from the kill switch: disarming says "not now" and leaves
        resting orders alone, whereas the kill switch says "stop" and cancels
        them. Collapsing the two would mean every pause threw away the book.
        """
        row = await self._account_row(account_id, user_id)
        row.live_armed_at = None
        await self._session.flush()
        await self._event(row, None, EventType.ARMED, detail="disarmed", payload={"armed": False})
        return _account_view(row)

    async def _credential_obstacles(self, user_id: uuid.UUID, view: AccountView) -> list[str]:
        """Whether a usable credential and a checked broker mapping both exist."""
        from domains.broker_auth.enums import BrokerProvider
        from domains.broker_auth.errors import BrokerAuthError

        reasons: list[str] = []
        try:
            provider = BrokerProvider(view.broker)
        except ValueError:
            return [f"broker {view.broker!r} is not one the credential vault knows about"]
        try:
            await self._broker_auth.access_token(user_id, provider)
        except BrokerAuthError as exc:
            reasons.append(f"no usable {view.broker} credential is stored: {exc}")

        adapter = self._broker(view)
        if not getattr(adapter, "mapping_verified", True):
            reasons.append(
                f"the {view.broker} adapter's endpoints, request fields and status "
                "vocabulary have not been confirmed against the broker's published "
                "contract. A wrong status mapping tells the platform an order filled "
                "when it did not, and the book is then wrong with nothing reporting a "
                "problem"
            )
        return reasons

    async def release_kill_switch(self, account_id: uuid.UUID, user_id: uuid.UUID) -> AccountView:
        row = await self._account_row(account_id, user_id)
        row.kill_switch_engaged_at = None
        row.kill_switch_reason = None
        await self._session.flush()
        await self._event(
            row, None, EventType.KILL_SWITCH, detail="released", payload={"engaged": False}
        )
        return _account_view(row)

    # ================================================================ orders
    async def submit(
        self,
        user_id: uuid.UUID,
        account_id: uuid.UUID,
        request: OrderRequest,
        *,
        session_loss: Decimal | None = None,
    ) -> AnalyticalResult[SubmissionOutcome]:
        """Gate, place, book, and record — in that order, every time."""
        account = await self._account_row(account_id, user_id)
        view = _account_view(account)
        warnings: list[AnalyticalWarning] = []
        now = self._clock()

        client_order_id = request.client_order_id or f"auto-{uuid.uuid4().hex[:24]}"
        existing = await self.repository.by_client_order_id(account_id, client_order_id)
        if existing is not None:
            await self._event(
                account,
                existing,
                EventType.REPLAY,
                detail="a submission repeated an existing client_order_id",
            )
            warnings.append(
                AnalyticalWarning.info(
                    WarningCode.IDEMPOTENT_REPLAY,
                    f"client_order_id {client_order_id!r} already exists on this "
                    "account, so the existing order is returned and nothing new was "
                    "placed. This is what makes a retried submission safe.",
                    order_id=str(existing.id),
                )
            )
            outcome = SubmissionOutcome(
                order=await self._order_view(existing),
                gate=GateDecision(allowed=False, checks=()),
                replayed=True,
            )
            return self._envelope(outcome, view, warnings, now)

        instrument = await self._instruments.get(request.instrument_id)
        marks = await self._marks(await self._instrument_ids(account_id, request.instrument_id))
        gate = evaluate(
            request,
            view.risk_limits,
            AccountSnapshot(
                cash=view.cash,
                positions=await self._position_quantities(account_id),
                prices=marks.prices,
                session_loss=session_loss,
                orders_in_last_minute=await self.repository.orders_in_last_minute(account_id, now),
                multipliers=await self._multipliers(account_id, request.instrument_id),
            ),
            marks.quotes.get(request.instrument_id),
            venue=view.venue,
            kill_switch_engaged=view.kill_switch_engaged,
            kill_switch_reason=view.kill_switch_reason or "",
        )

        order = await self.repository.create_order(
            account_id=account_id,
            user_id=user_id,
            instrument_id=request.instrument_id,
            client_order_id=client_order_id,
            side=str(request.side),
            quantity=request.quantity,
            order_type=str(request.order_type),
            time_in_force=str(request.time_in_force),
            limit_price=request.limit_price,
            status=str(OrderStatus.NEW),
            venue=str(view.venue),
            broker=view.broker,
            decision_price=request.decision_price,
            submitted_at=now,
            strategy_tag=request.strategy_tag,
            order_metadata=dict(request.metadata),
        )
        await self._event(
            account, order, EventType.SUBMITTED, detail="order accepted by the platform"
        )
        await self._event(
            account, order, EventType.GATE, detail="pre-trade checks", payload=gate.to_dict()
        )

        if instrument is None:
            await self._reject(
                account,
                order,
                Rejection(
                    reason=RejectionReason.INSTRUMENT_UNKNOWN,
                    detail=(
                        f"instrument {request.instrument_id} is not in the master, so "
                        "there is nothing to trade and nothing to value"
                    ),
                ),
            )
            return self._envelope(
                SubmissionOutcome(await self._order_view(order), gate), view, warnings, now
            )

        if not gate.allowed and gate.rejection is not None:
            await self._reject(account, order, gate.rejection)
            if gate.rejection.reason is RejectionReason.KILL_SWITCH_ENGAGED:
                warnings.append(
                    AnalyticalWarning.error(
                        WarningCode.KILL_SWITCH,
                        "the kill switch is engaged on this account; no order will be "
                        f"accepted until it is released. Reason: {view.kill_switch_reason}",
                    )
                )
            return self._envelope(
                SubmissionOutcome(await self._order_view(order), gate), view, warnings, now
            )

        broker = self._broker(view)
        missing = broker.missing_capability(request)
        if missing is not None:
            await self._reject(
                account,
                order,
                Rejection(
                    reason=RejectionReason.CAPABILITY_MISSING,
                    detail=(
                        f"the {view.broker} adapter does not support {missing}, so this "
                        "instruction cannot be carried out. It is refused rather than "
                        "translated into a different one"
                    ),
                    observed={"capability": str(missing)},
                ),
            )
            return self._envelope(
                SubmissionOutcome(await self._order_view(order), gate), view, warnings, now
            )

        if view.venue is OrderVenue.LIVE:
            refusal = self._live_refusal(view, broker)
            if refusal is not None:
                await self._reject(account, order, refusal)
                return self._envelope(
                    SubmissionOutcome(await self._order_view(order), gate), view, warnings, now
                )

        await self._event(
            account,
            order,
            EventType.BROKER_REQUEST,
            detail=f"placing with {view.broker}",
            payload={
                "side": str(request.side),
                "quantity": format(request.quantity, "f"),
                "order_type": str(request.order_type),
                "limit_price": (format(request.limit_price, "f") if request.limit_price else None),
                "time_in_force": str(request.time_in_force),
            },
        )
        try:
            update = await broker.place_order(request)
        except BrokerRejected as exc:
            await self._reject(
                account,
                order,
                Rejection(
                    reason=RejectionReason.BROKER_REJECTED,
                    detail=exc.detail,
                    observed={"broker_code": exc.code} if exc.code else {},
                ),
            )
            return self._envelope(
                SubmissionOutcome(await self._order_view(order), gate), view, warnings, now
            )
        except BrokerError as exc:
            # The order may or may not have reached the broker. It is left in
            # NEW with the failure recorded, not marked rejected: claiming a
            # definite outcome for an indefinite one is how duplicates get sent.
            await self._event(
                account,
                order,
                EventType.BROKER_RESPONSE,
                detail=f"the broker could not be reached: {exc}",
                payload={"error": str(exc), "outcome_unknown": True},
            )
            raise TradingError(
                f"the order reached the platform and its outcome with the broker is "
                f"unknown: {exc}. It is left as NEW and must be reconciled against the "
                "broker rather than resubmitted"
            ) from exc

        await self._apply_update(account, order, update, warnings)
        return self._envelope(
            SubmissionOutcome(await self._order_view(order), gate), view, warnings, now
        )

    async def cancel(
        self, user_id: uuid.UUID, order_id: uuid.UUID, reason: str = "cancelled by the user"
    ) -> Order:
        order = await self.repository.get_order(order_id, user_id)
        if order is None:
            raise OrderNotFound(str(order_id))
        account = await self._account_row(order.account_id, user_id)
        if not OrderStatus(order.status).is_open:
            raise TradingError(
                f"the order is {order.status} and cannot be cancelled. A terminal "
                "order is a record of what happened, not a thing to change"
            )
        await self._cancel_row(account, order, detail=reason)
        return await self._order_view(order)

    async def orders(
        self,
        user_id: uuid.UUID,
        account_id: uuid.UUID,
        statuses: list[str] | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> list[Order]:
        await self._account_row(account_id, user_id)
        rows = await self.repository.list_orders(account_id, statuses, limit, offset)
        return [await self._order_view(row) for row in rows]

    async def order(self, user_id: uuid.UUID, order_id: uuid.UUID) -> Order:
        row = await self.repository.get_order(order_id, user_id)
        if row is None:
            raise OrderNotFound(str(order_id))
        return await self._order_view(row)

    async def work_open_orders(self, user_id: uuid.UUID, account_id: uuid.UUID) -> list[Order]:
        """Offer the current market to every resting order.

        This is what a paper account does instead of having an exchange. A
        resting limit order that has become marketable fills here, through the
        same engine that would have filled it on arrival.
        """
        account = await self._account_row(account_id, user_id)
        view = _account_view(account)
        if view.kill_switch_engaged:
            return []
        broker = self._broker(view)
        if not isinstance(broker, PaperBroker):
            # A live broker works its own book; asking it to re-offer would be
            # placing a second order.
            return []

        touched: list[Order] = []
        for row in await self.repository.open_orders(account_id):
            update = await broker.offer(
                instrument_id=row.instrument_id,
                side=OrderSide(row.side),
                remaining_quantity=row.quantity - row.filled_quantity,
                order_type=OrderType(row.order_type),
                time_in_force=TimeInForce(row.time_in_force),
                limit_price=row.limit_price,
            )
            if update.status is OrderStatus.REJECTED and OrderStatus(row.status).is_open:
                # A resting order that cannot fill right now has not failed; it
                # is still resting. Only a change of state is applied.
                continue
            if update.status is OrderStatus.ACKNOWLEDGED and row.status in {
                str(OrderStatus.ACKNOWLEDGED),
                str(OrderStatus.PARTIALLY_FILLED),
            }:
                continue
            before = row.status
            await self._apply_update(account, row, update, [])
            if row.status != before:
                touched.append(await self._order_view(row))
        return touched

    async def work_parent_order(
        self,
        user_id: uuid.UUID,
        account_id: uuid.UUID,
        plan: ParentOrderPlan,
        *,
        session_loss: Decimal | None = None,
    ) -> AnalyticalResult[dict]:
        """Place the child orders whose slice window is open right now.

        Called repeatedly across the working window — by a scheduler, or by the
        user. Each child carries a client order id derived from the parent's, so
        the OMS's existing idempotency check makes a repeated call safe with
        nothing new to get wrong.

        A slice whose window has **passed** is reported as missed, not placed
        late. Dropping a whole missed interval into the market at once is the
        opposite of what a schedule is for, and the gap is the caller's to see.
        """
        account = await self._account_row(account_id, user_id)
        view = _account_view(account)
        now = self._clock()
        warnings: list[AnalyticalWarning] = []

        placed: list[dict] = []
        already: set[int] = set()
        for slice_ in plan.schedule.slices:
            existing = await self.repository.by_client_order_id(
                account_id, plan.child_client_order_id(slice_.index)
            )
            if existing is not None:
                already.add(slice_.index)

        for slice_ in plan.due_slices(now):
            if slice_.index in already:
                continue
            outcome = await self.submit(
                user_id, account_id, plan.child_request(slice_.index), session_loss=session_loss
            )
            warnings.extend(outcome.warnings)
            placed.append(
                {
                    "slice_index": slice_.index,
                    "order_id": str(outcome.results.order.id),
                    "status": str(outcome.results.order.status),
                    "quantity": format(slice_.quantity, "f"),
                }
            )

        missed = plan.missed_slices(now, already)
        if missed:
            warnings.append(
                AnalyticalWarning.warn(
                    WarningCode.SLICES_MISSED,
                    f"{len(missed)} slice window(s) closed without an order being "
                    "placed. They are not placed late: putting a missed interval into "
                    "the market at once is the opposite of what the schedule was for.",
                    slices=[item.index for item in missed],
                    total_quantity=format(sum((item.quantity for item in missed), Decimal(0)), "f"),
                )
            )
        if not plan.volume_profile_supplied:
            warnings.append(
                AnalyticalWarning.info(
                    WarningCode.NO_VOLUME_PROFILE,
                    "no volume expectation was supplied for this window, so the "
                    "participation of every slice is unknown rather than low.",
                )
            )

        return self._envelope(
            {
                "parent_client_order_id": plan.parent_client_order_id,
                "as_of": now.isoformat(),
                "strategy": plan.schedule.strategy,
                "slices_total": len(plan.schedule.slices),
                "slices_already_placed": len(already),
                "placed_now": placed,
                "missed": [item.index for item in missed],
                "schedule_assumptions": list(plan.schedule.assumptions),
                "schedule_warnings": list(plan.schedule.warnings),
            },
            view,
            warnings,
            now,
        )

    # ================================================================== book
    async def pnl(self, user_id: uuid.UUID, account_id: uuid.UUID) -> AnalyticalResult[AccountPnl]:
        account = await self._account_row(account_id, user_id)
        view = _account_view(account)
        rows = await self.repository.positions(account_id)
        marks = await self._marks([row.instrument_id for row in rows])
        warnings: list[AnalyticalWarning] = []

        positions: list[PositionView] = []
        unpriced: list[uuid.UUID] = []
        for row in rows:
            instrument = await self._instruments.get(row.instrument_id)
            quote = marks.quotes.get(row.instrument_id)
            price = marks.prices.get(row.instrument_id)
            if price is None and row.quantity != 0:
                unpriced.append(row.instrument_id)
            positions.append(
                PositionView(
                    instrument_id=row.instrument_id,
                    symbol=instrument.symbol if instrument else None,
                    quantity=row.quantity,
                    average_price=row.average_price,
                    realised_pnl=row.realised_pnl,
                    fees_paid=row.fees_paid,
                    mark_price=price,
                    mark_basis="QUOTE_MID" if quote and quote.mid_price else None,
                    mark_exchange_timestamp=quote.exchange_timestamp if quote else None,
                    mark_age_seconds=quote.age_seconds(marks.as_of) if quote else None,
                )
            )

        realised = sum((row.realised_pnl for row in rows), Decimal(0))
        fees = sum((row.fees_paid for row in rows), Decimal(0))
        priced = not unpriced
        market_value = (
            sum((item.market_value or Decimal(0) for item in positions), Decimal(0))
            if priced
            else None
        )
        unrealised = (
            sum((item.unrealised_pnl or Decimal(0) for item in positions), Decimal(0))
            if priced
            else None
        )
        equity = view.cash + market_value if market_value is not None else None

        if unpriced:
            warnings.append(
                AnalyticalWarning.warn(
                    WarningCode.UNPRICED_POSITIONS,
                    f"{len(unpriced)} held position(s) have no usable quote, so equity "
                    "and unrealised P&L are not reported. A total that quietly omitted "
                    "them would look like an answer.",
                    instruments=[str(item) for item in unpriced],
                )
            )
        if not view.cost_schedule.models_costs:
            warnings.append(_gross_warning(view.cost_schedule))

        result = AccountPnl(
            account_id=account_id,
            as_of=marks.as_of,
            cash=view.cash,
            opening_cash=view.opening_cash,
            realised_pnl=realised,
            fees_paid=fees,
            unrealised_pnl=unrealised,
            market_value=market_value,
            equity=equity,
            costs_modelled=view.cost_schedule.models_costs,
            positions=tuple(positions),
            unpriced=tuple(unpriced),
        )
        return self._envelope(result, view, warnings, marks.as_of)

    async def rebalance_preview(
        self,
        user_id: uuid.UUID,
        account_id: uuid.UUID,
        target_weights: dict[uuid.UUID, float],
        *,
        whole_units: bool = True,
    ) -> AnalyticalResult[RebalancePlan]:
        """The trades required to reach a target the caller supplied.

        Arithmetic on the user's own target, not a view about what to hold.
        There is no endpoint anywhere in the platform that produces a target;
        this one differences an existing book against one that was handed in.
        """
        account = await self._account_row(account_id, user_id)
        view = _account_view(account)
        held = {row.instrument_id: row for row in await self.repository.positions(account_id)}
        universe = set(held) | set(target_weights)
        marks = await self._marks(sorted(universe, key=str))
        warnings: list[AnalyticalWarning] = []

        priced_value = Decimal(0)
        unvalued: list[uuid.UUID] = []
        for instrument_id, row in held.items():
            price = marks.prices.get(instrument_id)
            if price is None:
                if row.quantity != 0:
                    unvalued.append(instrument_id)
                continue
            priced_value += row.quantity * price
        equity = view.cash + priced_value if not unvalued else None

        if equity is None:
            warnings.append(
                AnalyticalWarning.warn(
                    WarningCode.UNPRICED_POSITIONS,
                    "some held positions cannot be valued, so there is no equity to "
                    "size a target against and no trades are proposed.",
                    instruments=[str(item) for item in unvalued],
                )
            )
            return self._envelope(
                RebalancePlan(
                    account_id=account_id,
                    as_of=marks.as_of,
                    equity=None,
                    trades=(),
                    unpriced=tuple(unvalued),
                ),
                view,
                warnings,
                marks.as_of,
            )

        trades: list[RequiredTrade] = []
        unpriced_targets: list[uuid.UUID] = []
        for instrument_id in sorted(universe, key=str):
            price = marks.prices.get(instrument_id)
            current = held[instrument_id].quantity if instrument_id in held else Decimal(0)
            weight = Decimal(str(target_weights.get(instrument_id, 0.0)))
            if price is None or price <= 0:
                if weight != 0 or current != 0:
                    unpriced_targets.append(instrument_id)
                continue
            exact = (equity * weight) / price
            target = exact.to_integral_value(rounding="ROUND_DOWN") if whole_units else exact
            delta = target - current
            if delta == 0:
                continue
            instrument = await self._instruments.get(instrument_id)
            trades.append(
                RequiredTrade(
                    instrument_id=instrument_id,
                    symbol=instrument.symbol if instrument else None,
                    current_quantity=current,
                    target_quantity=target,
                    trade_quantity=abs(delta),
                    side=OrderSide.BUY if delta > 0 else OrderSide.SELL,
                    mark_price=price,
                    estimated_notional=abs(delta) * price,
                    rounding_residual=exact - target,
                )
            )

        if unpriced_targets:
            warnings.append(
                AnalyticalWarning.warn(
                    WarningCode.UNPRICED_POSITIONS,
                    f"{len(unpriced_targets)} instrument(s) in the target have no usable "
                    "quote, so no trade size could be computed for them. They are named "
                    "rather than dropped.",
                    instruments=[str(item) for item in unpriced_targets],
                )
            )

        notes = (
            "Sizes are the difference between the current book and the target supplied "
            "by the caller, at the marks shown. Nothing here is an instruction to trade: "
            "each trade becomes an order only when it is submitted.",
        )
        if whole_units:
            notes = (
                *notes,
                "Quantities are rounded down to whole units; the discarded fraction is "
                "reported per instrument as rounding_residual rather than absorbed.",
            )
        return self._envelope(
            RebalancePlan(
                account_id=account_id,
                as_of=marks.as_of,
                equity=equity,
                trades=tuple(trades),
                unpriced=tuple(unpriced_targets),
                notes=notes,
            ),
            view,
            warnings,
            marks.as_of,
        )

    async def audit_trail(
        self, user_id: uuid.UUID, account_id: uuid.UUID, order_id: uuid.UUID | None = None
    ) -> list[dict]:
        await self._account_row(account_id, user_id)
        rows = await self.repository.events(account_id, order_id)
        return [
            {
                "id": str(row.id),
                "order_id": str(row.order_id) if row.order_id else None,
                "event_type": row.event_type,
                "occurred_at": row.occurred_at.isoformat(),
                "from_status": row.from_status,
                "to_status": row.to_status,
                "detail": row.detail,
                "payload": row.payload,
            }
            for row in rows
        ]

    # ============================================================== internals
    def _broker(self, account: AccountView) -> BrokerAdapter:
        if account.broker == PAPER:
            return PaperBroker(
                self._quote_for,
                policy=account.fill_policy,
                max_quote_age_seconds=float(account.max_quote_age_seconds),
                clock=self._clock,
            )
        if account.broker == UPSTOX:
            if self._broker_auth is None:
                raise TradingError(
                    "this account routes to a live broker but the service was built "
                    "without access to the credential vault, so there is no token to "
                    "send. The order is refused rather than attempted without one"
                )
            return self._upstox(account)
        raise TradingError(
            f"no adapter is registered for broker {account.broker!r}. An order is "
            "refused rather than routed somewhere plausible"
        )

    def _upstox(self, account: AccountView) -> UpstoxBroker:
        """The live adapter, with its credential behind a callable.

        The token is fetched per request through the vault, which renews it if
        it is close to expiry. Nothing here holds a credential, and nothing
        writes one into an audit payload.
        """
        from domains.broker_auth.enums import BrokerProvider

        auth = self._broker_auth
        user_id = account.user_id

        async def token() -> str:
            grant = await auth.access_token(user_id, BrokerProvider.UPSTOX)
            return grant.token

        return UpstoxBroker(
            self._order_transport or HttpUpstoxOrderTransport(),
            token,
            _KeyDirectory(self._instruments),
            endpoints=self._order_endpoints or UpstoxOrderEndpoints(),
        )

    def _live_refusal(self, account: AccountView, broker: BrokerAdapter) -> Rejection | None:
        """All three gates must be open for a live order, and they are different.

        The deployment flag says this installation may trade; arming says this
        account is meant to right now; the adapter's own verification says
        somebody has checked its mapping against the broker's contract. Any one
        without the others is an accident waiting to be blamed on configuration.

        Each is checked **here**, before the adapter is called, so that a refusal
        is recorded as a rejection with its reason. An unverified adapter that
        raised from inside ``place_order`` would land in the broker-unreachable
        branch and be reported as an outcome nobody knows — when in fact nothing
        was sent and the outcome is entirely known.
        """
        if not self._live_trading_enabled:
            return Rejection(
                reason=RejectionReason.LIVE_TRADING_DISABLED,
                detail=(
                    "live trading is disabled for this deployment. No order will "
                    "reach a real broker until it is enabled deliberately"
                ),
                observed={"setting": "live_trading_enabled", "value": False},
            )
        if account.live_armed_at is None:
            return Rejection(
                reason=RejectionReason.LIVE_TRADING_DISABLED,
                detail=(
                    "this account is not armed for live trading. Arming is a separate, "
                    "explicit act from enabling live trading for the deployment"
                ),
                observed={"live_armed_at": None},
            )
        if not getattr(broker, "mapping_verified", True):
            return Rejection(
                reason=RejectionReason.LIVE_TRADING_DISABLED,
                detail=(
                    f"the {account.broker} adapter's endpoints, request fields and "
                    "status vocabulary have not been confirmed against the broker's "
                    "published contract. A wrong status mapping reports an order as "
                    "filled when it is not, and the book is then wrong with nothing "
                    "reporting a problem"
                ),
                observed={"verified_against_documentation": False},
            )
        return None

    async def _quote_for(self, instrument_id: uuid.UUID) -> Quote | None:
        view = await self._quotes.live_quote(instrument_id)
        return view.live.quote if view is not None else None

    async def _marks(self, instrument_ids) -> _Marks:
        marks = _Marks(as_of=self._clock())
        for instrument_id in instrument_ids:
            quote = await self._quote_for(instrument_id)
            if quote is None:
                marks.unpriced.append(instrument_id)
                continue
            marks.quotes[instrument_id] = quote
            # The mid, and nothing else. A position marked at the last trade is
            # marked at an estimate, and the rule that keeps observations and
            # estimates apart does not stop being true inside a P&L.
            if quote.mid_price is not None:
                marks.prices[instrument_id] = quote.mid_price
            else:
                marks.unpriced.append(instrument_id)
        return marks

    async def _instrument_ids(self, account_id: uuid.UUID, extra: uuid.UUID) -> list[uuid.UUID]:
        held = {row.instrument_id for row in await self.repository.positions(account_id)}
        held.add(extra)
        return sorted(held, key=str)

    async def _position_quantities(self, account_id: uuid.UUID) -> dict[uuid.UUID, Decimal]:
        return {
            row.instrument_id: row.quantity for row in await self.repository.positions(account_id)
        }

    async def _multipliers(
        self, account_id: uuid.UUID, extra: uuid.UUID
    ) -> dict[uuid.UUID, Decimal]:
        """Contract multipliers from the master, for the instruments that have one.

        An instrument with no recorded multiplier is left out rather than
        defaulted to one here: the gate names what it assumed, and it can only
        do that if the absence reaches it.
        """
        out: dict[uuid.UUID, Decimal] = {}
        for instrument_id in await self._instrument_ids(account_id, extra):
            instrument = await self._instruments.get(instrument_id)
            multiplier = getattr(instrument, "multiplier", None) if instrument else None
            if multiplier is not None:
                out[instrument_id] = Decimal(str(multiplier))
        return out

    async def _account_row(self, account_id: uuid.UUID, user_id: uuid.UUID) -> TradingAccountORM:
        row = await self.repository.get_account(account_id, user_id)
        if row is None:
            raise AccountNotFound(str(account_id))
        return row

    async def _event(
        self,
        account: TradingAccountORM,
        order: OrderORM | None,
        event_type: str,
        detail: str = "",
        payload: dict | None = None,
        from_status: str | None = None,
        to_status: str | None = None,
    ) -> None:
        await self.repository.add_event(
            order_id=order.id if order is not None else None,
            account_id=account.id,
            user_id=account.user_id,
            event_type=event_type,
            occurred_at=self._clock(),
            from_status=from_status,
            to_status=to_status,
            detail=detail,
            payload=payload or {},
        )

    async def _transition(
        self, account: TradingAccountORM, order: OrderORM, target: OrderStatus, detail: str
    ) -> None:
        current = OrderStatus(order.status)
        check_transition(current, target)
        order.status = str(target)
        if target.is_terminal:
            order.closed_at = self._clock()
        if target is OrderStatus.ACKNOWLEDGED and order.acknowledged_at is None:
            order.acknowledged_at = self._clock()
        await self._session.flush()
        await self._event(
            account,
            order,
            EventType.TRANSITION,
            detail=detail,
            from_status=str(current),
            to_status=str(target),
        )

    async def _reject(
        self, account: TradingAccountORM, order: OrderORM, rejection: Rejection
    ) -> None:
        order.rejection_reason = str(rejection.reason)
        order.rejection_detail = rejection.detail
        order.rejection_observed = rejection.observed
        await self._transition(account, order, OrderStatus.REJECTED, rejection.detail)

    async def _cancel_row(self, account: TradingAccountORM, order: OrderORM, detail: str) -> None:
        await self._event(account, order, EventType.CANCEL_REQUESTED, detail=detail)
        await self._transition(account, order, OrderStatus.CANCELLED, detail)

    async def _apply_update(
        self,
        account: TradingAccountORM,
        order: OrderORM,
        update: BrokerOrderUpdate,
        warnings: list[AnalyticalWarning],
    ) -> None:
        """Record what the broker said, book any fills, and move the state once."""
        if update.broker_order_id:
            order.broker_order_id = update.broker_order_id
        await self._event(
            account,
            order,
            EventType.BROKER_RESPONSE,
            detail=update.rejection_detail or f"broker reports {update.status}",
            payload={
                "status": str(update.status),
                "broker_order_id": update.broker_order_id,
                "fills": len(update.fills),
                "unmapped_fields": list(update.unmapped_fields),
                "raw": update.raw,
            },
        )

        if update.status is OrderStatus.REJECTED:
            await self._reject(
                account,
                order,
                Rejection(
                    reason=update.rejection_reason or RejectionReason.BROKER_REJECTED,
                    detail=update.rejection_detail or "the broker refused the order",
                ),
            )
            return

        if OrderStatus(order.status) is OrderStatus.NEW:
            await self._transition(
                account,
                order,
                OrderStatus.ACKNOWLEDGED,
                update.rejection_detail or "acknowledged by the broker",
            )

        schedule = _schedule(account)
        for broker_fill in update.fills:
            await self._book_fill(account, order, broker_fill, schedule, warnings)

        if update.status is OrderStatus.CANCELLED and OrderStatus(order.status).is_open:
            await self._transition(
                account,
                order,
                OrderStatus.CANCELLED,
                update.rejection_detail or "cancelled by the broker",
            )

    async def _book_fill(
        self,
        account: TradingAccountORM,
        order: OrderORM,
        broker_fill,
        schedule: CostSchedule,
        warnings: list[AnalyticalWarning],
    ) -> None:
        """Persist one fill, move the book, move the cash.

        The book is moved by :class:`domains.research.models.Book` — the same
        average-cost accounting a backtest uses. That is not code reuse for its
        own sake: it means a strategy's paper P&L and its backtest P&L are
        produced by one implementation, so a disagreement between them is a
        difference in the fills and never a difference in the arithmetic.
        """
        quantity = broker_fill.quantity
        is_buy = quantity > 0
        cost: TradeCost = schedule.charge(broker_fill.price, abs(quantity), is_buy)

        existing = await self.repository.fills_for_order(order.id)
        flags = list(broker_fill.flags)
        if not cost.modelled and FillFlag.COSTS_NOT_MODELLED not in flags:
            flags.append(FillFlag.COSTS_NOT_MODELLED)

        await self.repository.add_fill(
            order_id=order.id,
            account_id=account.id,
            instrument_id=order.instrument_id,
            quantity=quantity,
            price=broker_fill.price,
            price_basis=broker_fill.price_basis,
            filled_at=broker_fill.filled_at,
            reference_price=broker_fill.reference_price,
            reference_basis=broker_fill.reference_basis,
            quote_exchange_timestamp=broker_fill.quote_exchange_timestamp,
            cost_total=cost.total,
            cost_components=cost.to_dict(),
            costs_modelled=cost.modelled,
            broker_trade_id=broker_fill.broker_trade_id,
            flags={"flags": [str(flag) for flag in flags]},
            sequence=len(existing),
        )

        position_row = await self.repository.position(account.id, order.instrument_id)
        book = Book(cash=account.cash)
        if position_row is not None:
            held = book.position(order.instrument_id)
            held.quantity = position_row.quantity
            held.average_price = position_row.average_price
            held.realised_pnl = position_row.realised_pnl
        book.apply(
            Fill(
                instrument_id=order.instrument_id,
                timestamp=broker_fill.filled_at,
                quantity=quantity,
                price=broker_fill.price,
                reference_price=broker_fill.reference_price or broker_fill.price,
                cost=cost,
            )
        )
        held = book.position(order.instrument_id)
        await self.repository.upsert_position(
            account_id=account.id,
            instrument_id=order.instrument_id,
            quantity=held.quantity,
            average_price=held.average_price,
            realised_pnl=held.realised_pnl,
            fees_paid=(position_row.fees_paid if position_row else Decimal(0)) + cost.total,
            strategy_tag=order.strategy_tag,
        )
        account.cash = book.cash

        order.filled_quantity = order.filled_quantity + abs(quantity)
        order.fees = order.fees + cost.total
        order.average_fill_price = await self._average_fill_price(order.id)
        await self._session.flush()

        await self._event(
            account,
            order,
            EventType.FILL,
            detail=(f"filled {abs(quantity)} at {broker_fill.price} ({broker_fill.price_basis})"),
            payload={
                "quantity": format(quantity, "f"),
                "price": format(broker_fill.price, "f"),
                "price_basis": broker_fill.price_basis,
                "cost": format(cost.total, "f"),
                "costs_modelled": cost.modelled,
                "flags": [str(flag) for flag in flags],
            },
        )

        target = (
            OrderStatus.FILLED
            if order.filled_quantity >= order.quantity
            else OrderStatus.PARTIALLY_FILLED
        )
        await self._transition(
            account,
            order,
            target,
            f"{order.filled_quantity} of {order.quantity} filled",
        )

        qualified = [flag for flag in flags if flag is not FillFlag.COUNTERFACTUAL]
        if qualified:
            warnings.append(
                AnalyticalWarning.info(
                    WarningCode.FILL_QUALIFIED,
                    "this fill rests on less than a complete two-sided market: "
                    + ", ".join(sorted(str(flag) for flag in qualified))
                    + ". It is recorded with those flags so it is never read back as "
                    "something the market did.",
                    flags=[str(flag) for flag in qualified],
                )
            )

    async def _average_fill_price(self, order_id: uuid.UUID) -> Decimal | None:
        rows = await self.repository.fills_for_order(order_id)
        total = sum((abs(row.quantity) for row in rows), Decimal(0))
        if total == 0:
            return None
        weighted = sum((row.price * abs(row.quantity) for row in rows), Decimal(0))
        return weighted / total

    async def _order_view(self, row: OrderORM) -> Order:
        fills = await self.repository.fills_for_order(row.id)
        return _to_order(row, fills)

    def _envelope(
        self,
        results,
        account: AccountView,
        warnings: list[AnalyticalWarning],
        as_of: datetime,
    ) -> AnalyticalResult:
        return AnalyticalResult.ok(
            results=results,
            warnings=warnings,
            provenance=Provenance.now(
                code_commit=self._settings.code_commit,
                market_state_timestamp=as_of,
                market_data_sources=(self._settings.market_data_provider,),
                model_versions={"order_management": MODEL_VERSION},
                parameters={
                    "account_id": str(account.id),
                    "venue": str(account.venue),
                    "broker": account.broker,
                    "fill_policy": str(account.fill_policy),
                    "max_quote_age_seconds": account.max_quote_age_seconds,
                    # The schedule travels with every figure it produced: a net
                    # P&L is only meaningful beside a statement of what was
                    # deducted from it.
                    "cost_schedule": account.cost_schedule.name,
                    "cost_schedule_source": account.cost_schedule.source,
                    "costs_modelled": account.cost_schedule.models_costs,
                    "risk_limits_set": account.risk_limits.any_set,
                },
            ),
        )


class _KeyDirectory:
    """The instrument-key join, in the shape the broker adapter expects.

    A small adapter rather than a wider protocol on ``InstrumentService``: the
    broker needs two lookups and should not be handed a service that can also
    write to the master.
    """

    def __init__(self, instruments: InstrumentService, source: str = UPSTOX) -> None:
        self._instruments = instruments
        self._source = source
        self._metadata_key = f"{source}_instrument_key"

    async def provider_key(self, instrument_id: uuid.UUID) -> str | None:
        instrument = await self._instruments.get(instrument_id)
        if instrument is None:
            return None
        for source, alias in await self._instruments.list_aliases(instrument_id):
            if source == self._source:
                return alias
        value = (instrument.metadata or {}).get(self._metadata_key)
        return str(value) if value else None

    async def by_provider_key(self, key: str):
        return await self._instruments.find_by_alias(self._source, key)


def _schedule(account: TradingAccountORM) -> CostSchedule:
    payload = account.cost_schedule or {}
    components = payload.get("components") or []
    if not components:
        return NO_COST_MODEL
    return _schedule_from_components(
        payload.get("name", "account"), components, payload.get("source", "unspecified")
    )


def _gross_warning(schedule: CostSchedule) -> AnalyticalWarning:
    return AnalyticalWarning.warn(
        WarningCode.NO_COST_SCHEDULE,
        "no cost schedule is set on this account, so every P&L figure here is "
        "gross of brokerage, exchange charges, statutory levies and taxes. This is "
        "not a claim that trading is free: the rates are exchange and régime rules "
        "the platform will not invent.",
        schedule=schedule.name,
    )


def _account_view(row: TradingAccountORM) -> AccountView:
    limits = row.risk_limits or {}
    return AccountView(
        id=row.id,
        user_id=row.user_id,
        name=row.name,
        venue=OrderVenue(row.venue),
        broker=row.broker,
        base_currency=row.base_currency,
        cash=row.cash,
        opening_cash=row.opening_cash,
        cost_schedule=_schedule(row),
        fill_policy=PaperFillPolicy(row.fill_policy),
        max_quote_age_seconds=row.max_quote_age_seconds,
        risk_limits=_limits_from(limits),
        kill_switch_engaged_at=row.kill_switch_engaged_at,
        kill_switch_reason=row.kill_switch_reason,
        live_armed_at=row.live_armed_at,
        created_at=row.created_at,
    )


def _limits_from(payload: dict) -> RiskLimits:
    def dec(key: str) -> Decimal | None:
        value = payload.get(key)
        return Decimal(str(value)) if value is not None else None

    return RiskLimits(
        max_order_notional=dec("max_order_notional"),
        max_position_quantity=dec("max_position_quantity"),
        max_gross_exposure=dec("max_gross_exposure"),
        max_net_exposure=dec("max_net_exposure"),
        max_daily_loss=dec("max_daily_loss"),
        max_orders_per_minute=payload.get("max_orders_per_minute"),
        max_price_deviation=dec("max_price_deviation"),
        require_sufficient_cash=bool(payload.get("require_sufficient_cash", False)),
    )


def _to_order(row: OrderORM, fills: list[OrderFillORM]) -> Order:
    rejection = (
        Rejection(
            reason=RejectionReason(row.rejection_reason),
            detail=row.rejection_detail or "",
            observed=row.rejection_observed or {},
        )
        if row.rejection_reason
        else None
    )
    return Order(
        id=row.id,
        account_id=row.account_id,
        user_id=row.user_id,
        instrument_id=row.instrument_id,
        client_order_id=row.client_order_id,
        side=OrderSide(row.side),
        quantity=row.quantity,
        order_type=OrderType(row.order_type),
        time_in_force=TimeInForce(row.time_in_force),
        status=OrderStatus(row.status),
        venue=OrderVenue(row.venue),
        broker=row.broker,
        created_at=row.created_at,
        updated_at=row.updated_at,
        limit_price=row.limit_price,
        broker_order_id=row.broker_order_id,
        filled_quantity=row.filled_quantity,
        average_fill_price=row.average_fill_price,
        fees=row.fees,
        decision_price=row.decision_price,
        submitted_at=row.submitted_at,
        acknowledged_at=row.acknowledged_at,
        closed_at=row.closed_at,
        rejection=rejection,
        strategy_tag=row.strategy_tag,
        parent_order_id=row.parent_order_id,
        fills=tuple(_to_fill(item) for item in fills),
        metadata=row.order_metadata or {},
    )


def _to_fill(row: OrderFillORM) -> OrderFill:
    payload = row.cost_components or {}
    return OrderFill(
        id=row.id,
        order_id=row.order_id,
        quantity=row.quantity,
        price=row.price,
        filled_at=row.filled_at,
        price_basis=row.price_basis,
        cost=TradeCost(
            components=tuple(
                ChargedComponent(item["name"], Decimal(item["amount"]))
                for item in payload.get("components", [])
            ),
            modelled=row.costs_modelled,
        ),
        reference_price=row.reference_price,
        reference_basis=row.reference_basis,
        quote_exchange_timestamp=row.quote_exchange_timestamp,
        broker_trade_id=row.broker_trade_id,
        flags=tuple(FillFlag(flag) for flag in (row.flags or {}).get("flags", [])),
    )


__all__ = [
    "AccountNotFound",
    "AccountPnl",
    "AccountView",
    "EventType",
    "OrderManagementService",
    "OrderNotFound",
    "PositionView",
    "RebalancePlan",
    "RequiredTrade",
    "SubmissionOutcome",
    "TradingError",
    "WarningCode",
]
