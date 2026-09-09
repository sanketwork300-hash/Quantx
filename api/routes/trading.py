"""Paper and live trading.

The surface build spec §23 and §25 ask for, and nothing beyond it. In
particular there is no endpoint that decides what to trade. ``rebalance-preview``
differences the book against a target the caller supplies; every order is
submitted explicitly; and no response carries a recommendation field.

Every write here passes the same pre-trade gate, and the gate's full decision is
returned whether the order was accepted or refused. An interface that shows its
checks only when something fails teaches people that silence means safety.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Query, status

from api.dependencies.core import CurrentUser, SessionDep, TradingServiceDep
from api.errors import Conflict, NotFound, UnprocessableEntity
from api.schemas.common import Envelope, ProvenanceOut
from api.schemas.trading import (
    CancelOrderRequest,
    CreateAccountRequest,
    KillSwitchRequest,
    RebalancePreviewRequest,
    RiskLimitsIn,
    SubmitOrderRequest,
    WorkParentOrderRequest,
)
from domains.execution.oms.algorithms import (
    AlgorithmError,
    WorkingWindow,
    plan_parent_order,
)
from domains.execution.oms.models import (
    IllegalTransition,
    OrderError,
    OrderRequest,
)
from domains.execution.oms.risk_gate import RiskLimits
from domains.execution.oms.service import (
    AccountNotFound,
    OrderNotFound,
    TradingError,
)
from domains.research.costs import CostSchedule, schedule_from_components

router = APIRouter(prefix="/trading", tags=["trading"])


def _limits(payload: RiskLimitsIn | None) -> RiskLimits:
    if payload is None:
        return RiskLimits()
    return RiskLimits(
        max_order_notional=payload.max_order_notional,
        max_position_quantity=payload.max_position_quantity,
        max_gross_exposure=payload.max_gross_exposure,
        max_net_exposure=payload.max_net_exposure,
        max_daily_loss=payload.max_daily_loss,
        max_orders_per_minute=payload.max_orders_per_minute,
        max_price_deviation=payload.max_price_deviation,
        require_sufficient_cash=payload.require_sufficient_cash,
    )


def _schedule(payload: CreateAccountRequest) -> CostSchedule | None:
    if not payload.cost_components:
        return None
    return schedule_from_components(
        payload.cost_schedule_name or "account",
        [item.model_dump() for item in payload.cost_components],
        payload.cost_schedule_source or "supplied by the account owner",
    )


@router.post("/accounts", status_code=status.HTTP_201_CREATED)
async def create_account(
    payload: CreateAccountRequest,
    user: CurrentUser,
    trading: TradingServiceDep,
    session: SessionDep,
) -> dict:
    """Open a book.

    A live account is created halted and unarmed: creating it is not the same
    act as deciding it may trade real money, and the two are kept apart so
    neither can happen by accident.
    """
    try:
        account = await trading.create_account(
            user.id,
            payload.name,
            opening_cash=payload.opening_cash,
            venue=payload.venue,
            base_currency=payload.base_currency,
            cost_schedule=_schedule(payload),
            fill_policy=payload.fill_policy,
            max_quote_age_seconds=payload.max_quote_age_seconds,
            risk_limits=_limits(payload.risk_limits),
        )
    except TradingError as exc:
        raise UnprocessableEntity("INVALID_ACCOUNT", str(exc)) from exc
    await session.commit()
    return account.to_dict()


@router.get("/accounts")
async def list_accounts(user: CurrentUser, trading: TradingServiceDep) -> list[dict]:
    return [account.to_dict() for account in await trading.accounts(user.id)]


@router.get("/accounts/{account_id}")
async def get_account(account_id: uuid.UUID, user: CurrentUser, trading: TradingServiceDep) -> dict:
    try:
        return (await trading.account(account_id, user.id)).to_dict()
    except AccountNotFound as exc:
        raise NotFound("ACCOUNT_NOT_FOUND", "no such trading account") from exc


@router.put("/accounts/{account_id}/risk-limits")
async def set_risk_limits(
    account_id: uuid.UUID,
    payload: RiskLimitsIn,
    user: CurrentUser,
    trading: TradingServiceDep,
    session: SessionDep,
) -> dict:
    try:
        account = await trading.set_risk_limits(account_id, user.id, _limits(payload))
    except AccountNotFound as exc:
        raise NotFound("ACCOUNT_NOT_FOUND", "no such trading account") from exc
    await session.commit()
    return account.to_dict()


@router.post("/accounts/{account_id}/kill-switch")
async def engage_kill_switch(
    account_id: uuid.UUID,
    payload: KillSwitchRequest,
    user: CurrentUser,
    trading: TradingServiceDep,
    session: SessionDep,
) -> dict:
    """Halt the account and cancel what is resting.

    Not only a block on new orders: an order already working is exposure the
    switch was pulled to stop.
    """
    try:
        account = await trading.engage_kill_switch(account_id, user.id, payload.reason)
    except AccountNotFound as exc:
        raise NotFound("ACCOUNT_NOT_FOUND", "no such trading account") from exc
    except TradingError as exc:
        raise UnprocessableEntity("KILL_SWITCH_REFUSED", str(exc)) from exc
    await session.commit()
    return account.to_dict()


@router.delete("/accounts/{account_id}/kill-switch")
async def release_kill_switch(
    account_id: uuid.UUID,
    user: CurrentUser,
    trading: TradingServiceDep,
    session: SessionDep,
) -> dict:
    try:
        account = await trading.release_kill_switch(account_id, user.id)
    except AccountNotFound as exc:
        raise NotFound("ACCOUNT_NOT_FOUND", "no such trading account") from exc
    await session.commit()
    return account.to_dict()


@router.post("/accounts/{account_id}/orders", status_code=status.HTTP_201_CREATED)
async def submit_order(
    account_id: uuid.UUID,
    payload: SubmitOrderRequest,
    user: CurrentUser,
    trading: TradingServiceDep,
    session: SessionDep,
) -> Envelope:
    """Submit one order.

    A refused order is still created and returned, with the reason on it. That
    is deliberate: a rejection is a thing that happened to an order, and an
    interface that answers a refusal with an error and no record leaves the
    user unable to see what the gate objected to.
    """
    try:
        request = OrderRequest(
            instrument_id=payload.instrument_id,
            side=payload.side,
            quantity=payload.quantity,
            order_type=payload.order_type,
            limit_price=payload.limit_price,
            time_in_force=payload.time_in_force,
            client_order_id=payload.client_order_id,
            strategy_tag=payload.strategy_tag,
            decision_price=payload.decision_price,
        )
    except OrderError as exc:
        raise UnprocessableEntity("INVALID_ORDER", str(exc)) from exc

    try:
        result = await trading.submit(
            user.id, account_id, request, session_loss=payload.session_loss
        )
    except AccountNotFound as exc:
        raise NotFound("ACCOUNT_NOT_FOUND", "no such trading account") from exc
    except IllegalTransition as exc:
        raise Conflict("ILLEGAL_TRANSITION", str(exc)) from exc
    except TradingError as exc:
        raise UnprocessableEntity("ORDER_NOT_PLACED", str(exc)) from exc

    # Committed whether the order traded or was refused: a rejection is a record
    # of what happened to an order, and losing it would leave the user unable to
    # see what the gate objected to.
    await session.commit()
    return Envelope(
        status=str(result.status),
        results=result.results.to_dict(),
        warnings=[warning.to_dict() for warning in result.warnings],
        provenance=ProvenanceOut(**result.provenance.to_dict()),
    )


@router.get("/accounts/{account_id}/orders")
async def list_orders(
    account_id: uuid.UUID,
    user: CurrentUser,
    trading: TradingServiceDep,
    order_status: list[str] | None = Query(default=None, alias="status"),
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> list[dict]:
    try:
        orders = await trading.orders(user.id, account_id, order_status, limit, offset)
    except AccountNotFound as exc:
        raise NotFound("ACCOUNT_NOT_FOUND", "no such trading account") from exc
    return [order.to_dict() for order in orders]


@router.get("/orders/{order_id}")
async def get_order(order_id: uuid.UUID, user: CurrentUser, trading: TradingServiceDep) -> dict:
    try:
        return (await trading.order(user.id, order_id)).to_dict()
    except OrderNotFound as exc:
        raise NotFound("ORDER_NOT_FOUND", "no such order") from exc


@router.post("/orders/{order_id}/cancel")
async def cancel_order(
    order_id: uuid.UUID,
    payload: CancelOrderRequest,
    user: CurrentUser,
    trading: TradingServiceDep,
    session: SessionDep,
) -> dict:
    try:
        cancelled = await trading.cancel(user.id, order_id, payload.reason)
        await session.commit()
        return cancelled.to_dict()
    except OrderNotFound as exc:
        raise NotFound("ORDER_NOT_FOUND", "no such order") from exc
    except IllegalTransition as exc:
        raise Conflict("ILLEGAL_TRANSITION", str(exc)) from exc
    except TradingError as exc:
        raise Conflict("ORDER_NOT_CANCELLABLE", str(exc)) from exc


@router.post("/accounts/{account_id}/work")
async def work_open_orders(
    account_id: uuid.UUID,
    user: CurrentUser,
    trading: TradingServiceDep,
    session: SessionDep,
) -> list[dict]:
    """Offer the current market to every resting order on a paper account.

    What an exchange would do continuously, done on request. The background
    worker calls exactly this, so a manual poke and the automatic path cannot
    disagree about what a resting order does.
    """
    try:
        worked = await trading.work_open_orders(user.id, account_id)
        await session.commit()
        return [order.to_dict() for order in worked]
    except AccountNotFound as exc:
        raise NotFound("ACCOUNT_NOT_FOUND", "no such trading account") from exc


@router.get("/accounts/{account_id}/positions")
async def positions(
    account_id: uuid.UUID, user: CurrentUser, trading: TradingServiceDep
) -> Envelope:
    return await _pnl_envelope(trading, user.id, account_id)


@router.get("/accounts/{account_id}/pnl")
async def pnl(account_id: uuid.UUID, user: CurrentUser, trading: TradingServiceDep) -> Envelope:
    """Cash, positions, realised and unrealised P&L.

    ``equity`` is null when any held position has no usable quote, and the
    unpriced instruments are named. A total that quietly omitted them would look
    like an answer.
    """
    return await _pnl_envelope(trading, user.id, account_id)


async def _pnl_envelope(trading, user_id: uuid.UUID, account_id: uuid.UUID) -> Envelope:
    try:
        result = await trading.pnl(user_id, account_id)
    except AccountNotFound as exc:
        raise NotFound("ACCOUNT_NOT_FOUND", "no such trading account") from exc
    return Envelope(
        status=str(result.status),
        results=result.results.to_dict(),
        warnings=[warning.to_dict() for warning in result.warnings],
        provenance=ProvenanceOut(**result.provenance.to_dict()),
    )


@router.post("/accounts/{account_id}/rebalance-preview")
async def rebalance_preview(
    account_id: uuid.UUID,
    payload: RebalancePreviewRequest,
    user: CurrentUser,
    trading: TradingServiceDep,
) -> Envelope:
    """The trades required to reach a target the caller supplied.

    Nothing here is an instruction to trade. The target came from the caller —
    typically from the portfolio optimiser, which they ran with their own
    objective and their own constraints — and this endpoint subtracts the
    current book from it.
    """
    weights: dict[uuid.UUID, float] = {}
    for item in payload.targets:
        if item.instrument_id in weights:
            raise UnprocessableEntity(
                "DUPLICATE_TARGET",
                f"instrument {item.instrument_id} appears twice in the target; "
                "one of the two weights would have been silently discarded",
            )
        weights[item.instrument_id] = item.weight

    try:
        result = await trading.rebalance_preview(
            user.id, account_id, weights, whole_units=payload.whole_units
        )
    except AccountNotFound as exc:
        raise NotFound("ACCOUNT_NOT_FOUND", "no such trading account") from exc
    return Envelope(
        status=str(result.status),
        results=result.results.to_dict(),
        warnings=[warning.to_dict() for warning in result.warnings],
        provenance=ProvenanceOut(**result.provenance.to_dict()),
    )


@router.post("/accounts/{account_id}/arm")
async def arm_for_live(
    account_id: uuid.UUID,
    user: CurrentUser,
    trading: TradingServiceDep,
    session: SessionDep,
) -> dict:
    """Permit this account to send real orders, or say why it may not.

    Independent of the deployment's `live_trading_enabled` flag: one says this
    installation may trade real money, the other says this book is meant to be
    trading now. Both are required, so neither a stray configuration change nor
    a stray API call is enough on its own.

    Returns `200` with every obstacle listed when it refuses. Arming is done
    once, carefully, and reporting one obstacle at a time wastes the care.
    """
    try:
        account, reasons = await trading.arm_for_live(account_id, user.id)
    except AccountNotFound as exc:
        raise NotFound("ACCOUNT_NOT_FOUND", "no such trading account") from exc
    except TradingError as exc:
        raise UnprocessableEntity("ARMING_REFUSED", str(exc)) from exc
    await session.commit()
    return {"account": account.to_dict(), "armed": not reasons, "refusals": reasons}


@router.delete("/accounts/{account_id}/arm")
async def disarm(
    account_id: uuid.UUID,
    user: CurrentUser,
    trading: TradingServiceDep,
    session: SessionDep,
) -> dict:
    """Stop accepting live orders without halting the account.

    Distinct from the kill switch: this says "not now" and leaves resting orders
    alone; the kill switch says "stop" and cancels them.
    """
    try:
        account = await trading.disarm(account_id, user.id)
    except AccountNotFound as exc:
        raise NotFound("ACCOUNT_NOT_FOUND", "no such trading account") from exc
    await session.commit()
    return account.to_dict()


@router.post("/accounts/{account_id}/parent-orders")
async def work_parent_order(
    account_id: uuid.UUID,
    payload: WorkParentOrderRequest,
    user: CurrentUser,
    trading: TradingServiceDep,
    session: SessionDep,
) -> Envelope:
    """Place the child orders whose slice window is open right now.

    Called repeatedly across the working window. Child order ids are derived
    from the parent's, so a repeated call places each slice exactly once.

    A slice whose window has closed is reported as **missed**, not placed late:
    dropping a whole missed interval into the market at once is the opposite of
    what a schedule is for.
    """
    try:
        plan = plan_parent_order(
            parent_client_order_id=payload.parent_client_order_id,
            instrument_id=payload.instrument_id,
            side=payload.side,
            quantity=payload.quantity,
            strategy=payload.strategy,
            window=WorkingWindow(
                start=payload.window.start,
                end=payload.window.end,
                slices=payload.window.slices,
                reference_price=payload.window.reference_price,
                volatility=payload.window.volatility,
                average_daily_volume=payload.window.average_daily_volume,
                lot_size=payload.window.lot_size,
                spread=payload.window.spread,
                expected_volumes=(
                    tuple(payload.window.expected_volumes)
                    if payload.window.expected_volumes is not None
                    else None
                ),
            ),
            order_type=payload.order_type,
            limit_price=payload.limit_price,
            strategy_tag=payload.strategy_tag,
            parameters=payload.parameters,
        )
    except AlgorithmError as exc:
        raise UnprocessableEntity("SCHEDULE_REFUSED", str(exc)) from exc

    try:
        result = await trading.work_parent_order(
            user.id, account_id, plan, session_loss=payload.session_loss
        )
    except AccountNotFound as exc:
        raise NotFound("ACCOUNT_NOT_FOUND", "no such trading account") from exc
    except TradingError as exc:
        raise UnprocessableEntity("ORDER_NOT_PLACED", str(exc)) from exc

    await session.commit()
    results = dict(result.results)
    results["plan"] = plan.to_dict()
    return Envelope(
        status=str(result.status),
        results=results,
        warnings=[warning.to_dict() for warning in result.warnings],
        provenance=ProvenanceOut(**result.provenance.to_dict()),
    )


@router.get("/accounts/{account_id}/audit")
async def audit_trail(
    account_id: uuid.UUID,
    user: CurrentUser,
    trading: TradingServiceDep,
    order_id: uuid.UUID | None = Query(default=None),
) -> list[dict]:
    """Every decision taken about this account's orders, newest first.

    Append-only. Gate decisions, broker exchanges and state transitions are all
    here, so "why did this order do that" has an answer that does not depend on
    application logs having been kept.
    """
    try:
        return await trading.audit_trail(user.id, account_id, order_id)
    except AccountNotFound as exc:
        raise NotFound("ACCOUNT_NOT_FOUND", "no such trading account") from exc
