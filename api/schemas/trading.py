"""Trading: accounts, orders, fills, the risk gate and the book."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import Field, model_validator

from api.schemas.common import APIModel
from domains.execution.brokers.paper_fills import PaperFillPolicy
from domains.execution.oms.models import OrderSide, OrderType, OrderVenue, TimeInForce


class CostComponentIn(APIModel):
    """One line of a cost schedule, as the person who knows the rates states it.

    There is no default schedule anywhere in the platform. Brokerage, STT and
    GST are exchange and régime rules, and an invented rate would quietly change
    every P&L computed under it.
    """

    name: str = Field(min_length=1, max_length=64)
    #: ``TURNOVER``, ``PER_UNIT``, ``PER_ORDER`` or ``ON_OTHER_COMPONENTS``.
    basis: str
    rate: Decimal
    #: ``BOTH``, ``BUY`` or ``SELL`` — STT on intraday is charged on the sell alone.
    side: str = "BOTH"
    maximum: Decimal | None = None
    minimum: Decimal | None = None
    applies_to: list[str] = Field(default_factory=list)


class RiskLimitsIn(APIModel):
    """Limits the account's owner sets. Every one is optional and none is assumed.

    An account with none set can trade on paper and is refused live: an
    unlimited live account is not something anybody decides on purpose.
    """

    max_order_notional: Decimal | None = None
    max_position_quantity: Decimal | None = None
    max_gross_exposure: Decimal | None = None
    max_net_exposure: Decimal | None = None
    max_daily_loss: Decimal | None = None
    max_orders_per_minute: int | None = Field(default=None, ge=1)
    #: A fat-finger guard the user declares, as a fraction of the quote mid.
    #: Not an exchange price band: the platform does not hold those.
    max_price_deviation: Decimal | None = None
    require_sufficient_cash: bool = False


class CreateAccountRequest(APIModel):
    name: str = Field(min_length=1, max_length=120)
    opening_cash: Decimal = Field(ge=0)
    venue: OrderVenue = OrderVenue.PAPER
    base_currency: str = Field(default="INR", min_length=3, max_length=3)
    #: Named so it travels into provenance with the schedule it describes.
    cost_schedule_name: str | None = None
    cost_schedule_source: str | None = None
    cost_components: list[CostComponentIn] = Field(default_factory=list)
    fill_policy: PaperFillPolicy = PaperFillPolicy.QUOTE_ONLY
    max_quote_age_seconds: int = Field(default=30, ge=1, le=3600)
    risk_limits: RiskLimitsIn | None = None


class SubmitOrderRequest(APIModel):
    """One order. Direction is the side; quantity is always positive."""

    instrument_id: uuid.UUID
    side: OrderSide
    quantity: Decimal = Field(gt=0)
    order_type: OrderType = OrderType.MARKET
    limit_price: Decimal | None = None
    time_in_force: TimeInForce = TimeInForce.DAY
    #: An idempotency key. Resubmitting one returns the existing order rather
    #: than placing a second, which is what makes a retry safe.
    client_order_id: str | None = Field(default=None, max_length=64)
    strategy_tag: str | None = Field(default=None, max_length=64)
    #: The price the decision was taken against. Without it no slippage figure
    #: is reported, rather than one measured against a baseline chosen later.
    decision_price: Decimal | None = None
    #: Session loss so far, positive for a loss. Supplied by the caller because
    #: what counts as the session is theirs to define; a daily-loss limit set
    #: with none supplied refuses the order rather than passing unmeasured.
    session_loss: Decimal | None = None

    @model_validator(mode="after")
    def _price_matches_type(self) -> SubmitOrderRequest:
        if self.order_type is OrderType.LIMIT and self.limit_price is None:
            raise ValueError("a limit order needs a limit price")
        if self.order_type is OrderType.MARKET and self.limit_price is not None:
            raise ValueError(
                "a market order with a limit price is two different instructions; send one of them"
            )
        return self


class CancelOrderRequest(APIModel):
    reason: str = Field(default="cancelled by the user", max_length=500)


class KillSwitchRequest(APIModel):
    """Halting an account needs a reason: someone will read it later."""

    reason: str = Field(min_length=1, max_length=500)


class TargetWeightIn(APIModel):
    instrument_id: uuid.UUID
    weight: float


class RebalancePreviewRequest(APIModel):
    """A target the caller supplies, and the trades needed to reach it.

    The platform produces no targets. This differences the book against one that
    was handed in, which is arithmetic and not advice.
    """

    targets: list[TargetWeightIn] = Field(min_length=1)
    whole_units: bool = True


class WorkingWindowIn(APIModel):
    """The window a parent order is worked over, as the caller states it.

    ``expected_volumes`` is not filled in from anywhere. A volume forecast the
    platform invented would make VWAP and POV produce confident-looking
    schedules built on a number nobody supplied, so a strategy that needs one
    and does not get one is refused.
    """

    start: datetime
    end: datetime
    slices: int = Field(ge=1, le=500)
    reference_price: Decimal = Field(gt=0)
    volatility: float = 0.0
    average_daily_volume: float = 0.0
    lot_size: Decimal = Field(default=Decimal(1), gt=0)
    spread: Decimal | None = None
    expected_volumes: list[float] | None = None


class WorkParentOrderRequest(APIModel):
    """Split a quantity the caller has decided to trade across a window.

    Not a view on whether to trade, and not a claim that this split beats
    another one — that claim needs a counterfactual, which is what the execution
    simulator is for.
    """

    #: The parent's idempotency key. Child ids are derived from it, so calling
    #: this repeatedly across the window places each slice exactly once.
    parent_client_order_id: str = Field(min_length=1, max_length=48)
    instrument_id: uuid.UUID
    side: OrderSide
    quantity: Decimal = Field(gt=0)
    #: ``TWAP``, ``VWAP``, ``POV`` or ``LIQUIDITY_ADAPTIVE``.
    strategy: str
    window: WorkingWindowIn
    order_type: OrderType = OrderType.MARKET
    limit_price: Decimal | None = None
    strategy_tag: str | None = Field(default=None, max_length=64)
    parameters: dict = Field(default_factory=dict)
    session_loss: Decimal | None = None
