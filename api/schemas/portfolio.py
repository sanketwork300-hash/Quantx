"""Portfolio request and response schemas."""

from __future__ import annotations

import uuid
from datetime import date, datetime, time
from decimal import Decimal
from typing import Any

from pydantic import Field, field_validator

from api.schemas.common import APIModel, DecimalStr
from domains.instruments.enums import AssetClass, ExerciseStyle, SettlementType


class PortfolioCreateRequest(APIModel):
    name: str = Field(min_length=1, max_length=120)
    base_currency: str = Field(default="INR", min_length=3, max_length=3)
    description: str | None = Field(default=None, max_length=2000)

    @field_validator("base_currency")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()


class PortfolioUpdateRequest(APIModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)


class PortfolioOut(APIModel):
    id: uuid.UUID
    name: str
    base_currency: str
    description: str | None = None
    created_at: datetime
    updated_at: datetime


class PositionCreateRequest(APIModel):
    instrument_id: uuid.UUID
    #: Signed. A short is a negative quantity; ``side`` is optional and is
    #: checked against the sign rather than used to infer it.
    quantity: DecimalStr
    side: str | None = None
    average_price: DecimalStr | None = None
    strategy_tag: str | None = Field(default=None, max_length=64)


class PositionUpdateRequest(APIModel):
    quantity: DecimalStr | None = None
    average_price: DecimalStr | None = None
    strategy_tag: str | None = Field(default=None, max_length=64)


class PositionOut(APIModel):
    id: uuid.UUID
    portfolio_id: uuid.UUID
    instrument_id: uuid.UUID
    quantity: DecimalStr
    side: str
    average_price: DecimalStr | None = None
    source: str
    strategy_tag: str | None = None
    metadata: dict[str, Any] = {}


class ImportDefaultsIn(APIModel):
    """What the file does not say, stated explicitly rather than guessed.

    ``multiplier`` has no default: an assumed contract multiplier silently
    rescales every value and Greek in the portfolio. Left unset, the importer
    records ``MULTIPLIER_ASSUMED`` on the instruments it creates.
    """

    currency: str = Field(default="INR", min_length=3, max_length=3)
    exchange: str | None = Field(default=None, max_length=32)
    asset_class: AssetClass | None = None
    multiplier: DecimalStr | None = None
    tick_size: DecimalStr = Decimal("0.05")
    lot_size: DecimalStr = Decimal("1")
    exercise_style: ExerciseStyle = ExerciseStyle.EUROPEAN
    settlement_type: SettlementType = SettlementType.CASH
    create_missing_instruments: bool = True

    def to_payload(self) -> dict[str, Any]:
        return {
            "currency": self.currency.upper(),
            "exchange": self.exchange,
            "asset_class": str(self.asset_class) if self.asset_class else None,
            "multiplier": (format(self.multiplier, "f") if self.multiplier is not None else None),
            "tick_size": format(self.tick_size, "f"),
            "lot_size": format(self.lot_size, "f"),
            "exercise_style": str(self.exercise_style),
            "settlement_type": str(self.settlement_type),
            "create_missing_instruments": self.create_missing_instruments,
        }


class ImportPreviewRequest(APIModel):
    upload_id: uuid.UUID
    column_mapping: dict[str, str] | None = None
    defaults: ImportDefaultsIn = ImportDefaultsIn()
    limit: int | None = Field(default=None, ge=1, le=1000)


class ImportCommitRequest(APIModel):
    upload_id: uuid.UUID
    column_mapping: dict[str, str]
    defaults: ImportDefaultsIn = ImportDefaultsIn()
    #: Replacing wipes the existing positions before inserting. Off by default:
    #: an import that silently discards a portfolio is not recoverable.
    replace_existing: bool = False


class ImportPreviewOut(APIModel):
    """Three buckets. ``committable`` is false while any row is ambiguous."""

    upload_id: uuid.UUID
    headers: list[str]
    inferred_mapping: dict[str, str]
    applied_mapping: dict[str, str]
    rows_in: int
    committable: bool
    resolved: list[dict[str, Any]]
    ambiguous: list[dict[str, Any]]
    invalid: list[dict[str, Any]]


class ValuePortfolioRequest(APIModel):
    risk_free_rate: float = Field(default=0.0, ge=-0.5, le=1.0)
    dividend_yield: float = Field(default=0.0, ge=-0.5, le=1.0)
    #: Without it, time to expiry is undefined and option Greeks are omitted
    #: rather than computed against a guessed settlement moment.
    settlement_time_utc: time | None = None
    as_of: datetime | None = None


class GreeksOut(APIModel):
    delta: float
    gamma: float
    vega_per_vol_point: float
    theta_per_day: float
    rho_per_bp: float
    units: dict[str, str] = {}


class AggregateBucketOut(APIModel):
    dimension: str
    key: str
    label: str
    positions: int
    valued: int
    base_market_value: DecimalStr
    gross_exposure: DecimalStr
    net_exposure: DecimalStr
    unrealized_pnl: DecimalStr
    greeks: GreeksOut


class PositionValuationOut(APIModel):
    position_id: uuid.UUID
    instrument_id: uuid.UUID
    canonical_key: str
    asset_class: str
    underlying_id: uuid.UUID | None = None
    expiry: date | None = None
    strike: DecimalStr | None = None
    option_type: str | None = None
    quantity: DecimalStr
    multiplier: DecimalStr
    currency: str
    #: Observation and estimate are separate fields and neither is ever written
    #: from the other. ``price_used`` names which one entered the total.
    market_price: DecimalStr | None = None
    model_price: DecimalStr | None = None
    price_used: DecimalStr | None = None
    valuation_method: str
    market_value: DecimalStr | None = None
    base_market_value: DecimalStr | None = None
    fx_rate: DecimalStr | None = None
    unrealized_pnl: DecimalStr | None = None
    greeks: GreeksOut
    greek_source: str
    implied_volatility: float | None = None
    time_to_expiry: float | None = None
    quote_age_seconds: float | None = None
    warnings: list[str] = []


class PortfolioValuationOut(APIModel):
    valuation_id: uuid.UUID
    portfolio_id: uuid.UUID
    as_of_timestamp: datetime
    base_currency: str
    market_state_id: str | None = None
    positions: int
    valued: int
    base_market_value: DecimalStr
    unrealized_pnl: DecimalStr
    gross_exposure: DecimalStr
    net_exposure: DecimalStr
    greeks: GreeksOut
    valuation_methods: dict[str, int] = {}
    aggregates: list[AggregateBucketOut] = []
    created_at: datetime
