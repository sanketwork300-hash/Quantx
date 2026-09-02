from __future__ import annotations

import uuid
from datetime import date

from pydantic import Field

from api.schemas.common import APIModel, DecimalStr, PageMeta
from domains.instruments.enums import (
    AssetClass,
    ExerciseStyle,
    InstrumentStatus,
    OptionType,
    SettlementType,
)


class InstrumentOut(APIModel):
    id: uuid.UUID
    canonical_key: str
    asset_class: AssetClass
    exchange: str
    venue: str | None = None
    symbol: str
    underlying_id: uuid.UUID | None = None
    currency: str
    multiplier: DecimalStr
    tick_size: DecimalStr
    lot_size: DecimalStr
    expiry: date | None = None
    strike: DecimalStr | None = None
    option_type: OptionType | None = None
    exercise_style: ExerciseStyle | None = None
    settlement_type: SettlementType | None = None
    status: InstrumentStatus
    metadata: dict = {}


class InstrumentListOut(APIModel):
    items: list[InstrumentOut]
    meta: PageMeta


class InstrumentCreate(APIModel):
    asset_class: AssetClass
    exchange: str
    symbol: str
    currency: str = Field(min_length=3, max_length=3)
    multiplier: DecimalStr = Field(default="1")
    tick_size: DecimalStr = Field(default="0.01")
    lot_size: DecimalStr = Field(default="1")
    venue: str | None = None
    expiry: date | None = None
    strike: DecimalStr | None = None
    option_type: OptionType | None = None
    exercise_style: ExerciseStyle | None = None
    settlement_type: SettlementType | None = None
    underlying_id: uuid.UUID | None = None
    metadata: dict = {}


class ResolutionRequestIn(APIModel):
    instrument_id: uuid.UUID | None = None
    canonical_key: str | None = None
    symbol: str | None = None
    exchange: str | None = None
    asset_class: AssetClass | None = None
    expiry: date | None = None
    strike: DecimalStr | None = None
    option_type: OptionType | None = None
    source: str | None = None


class ResolveRequest(APIModel):
    requests: list[ResolutionRequestIn] = Field(min_length=1, max_length=1000)


class ResolutionResultOut(APIModel):
    status: str
    instrument_id: uuid.UUID | None = None
    method: str | None = None
    confidence: float = 0.0
    reason: str | None = None
    #: Populated on AMBIGUOUS. The caller must choose; the platform never picks
    #: a "most likely" contract on the user's behalf.
    candidates: list[InstrumentOut] = []


class ResolveResponse(APIModel):
    results: list[ResolutionResultOut]


class AliasIn(APIModel):
    source: str = Field(min_length=1, max_length=64)
    alias_symbol: str = Field(min_length=1, max_length=128)


class AliasOut(APIModel):
    source: str
    alias_symbol: str
