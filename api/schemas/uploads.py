from __future__ import annotations

import uuid
from datetime import datetime, time
from typing import Any

from pydantic import Field

from api.schemas.common import APIModel, DecimalStr
from domains.instruments.enums import AssetClass, ExerciseStyle, SettlementType
from domains.market_data.enums import UploadKind


class UploadOut(APIModel):
    id: uuid.UUID
    kind: str
    original_filename: str
    content_type: str
    byte_size: int
    sha256: str
    status: str
    created_at: datetime
    error: dict | None = None


class PreviewRequest(APIModel):
    #: Canonical field name -> source column header. Omit to use inference; the
    #: inferred mapping is always returned so the user can confirm or correct it
    #: before anything is committed.
    column_mapping: dict[str, str] | None = None
    limit: int = Field(default=50, ge=1, le=500)


class PreviewResponse(APIModel):
    upload_id: uuid.UUID
    headers: list[str]
    inferred_mapping: dict[str, str]
    applied_mapping: dict[str, str]
    missing_required: list[str]
    unmapped_columns: list[str]
    sample_rows: list[dict[str, Any]]
    parse_errors: list[dict[str, Any]]


class UnderlyingSpecIn(APIModel):
    symbol: str = Field(min_length=1, max_length=64)
    exchange: str = Field(min_length=1, max_length=32)
    asset_class: AssetClass = AssetClass.INDEX
    currency: str = Field(default="INR", min_length=3, max_length=3)


class ContractSpecIn(APIModel):
    #: Optional on purpose. An absent multiplier is recorded as an assumption
    #: rather than guessed, because a wrong multiplier scales every Greek and
    #: every margin number downstream.
    multiplier: DecimalStr | None = None
    tick_size: DecimalStr = Field(default="0.05")
    lot_size: DecimalStr = Field(default="1")
    exercise_style: ExerciseStyle = ExerciseStyle.EUROPEAN
    settlement_type: SettlementType = SettlementType.CASH
    expiry_time_utc: time | None = None


class IngestOptionsIn(APIModel):
    exclusion_severity_threshold: str = Field(default="ERROR", pattern="^(INFO|WARNING|ERROR)$")
    create_missing_instruments: bool = True
    source_label: str = Field(default="user-upload", max_length=64)


class IngestRequest(APIModel):
    kind: UploadKind = UploadKind.OPTION_CHAIN
    underlying: UnderlyingSpecIn
    as_of_timestamp: datetime
    column_mapping: dict[str, str]
    underlying_price: DecimalStr | None = None
    #: Supplying both enables the carry-dependent option bound checks
    #: (including the sub-intrinsic check). Omitting them keeps the checks
    #: assumption-free. They are assumptions, and are recorded as such.
    risk_free_rate: float | None = Field(default=None, ge=-0.5, le=1.0)
    dividend_yield: float | None = Field(default=None, ge=-0.5, le=1.0)
    contract: ContractSpecIn = Field(default_factory=ContractSpecIn)
    options: IngestOptionsIn = Field(default_factory=IngestOptionsIn)


class JobAcceptedOut(APIModel):
    job_id: uuid.UUID
    status: str
