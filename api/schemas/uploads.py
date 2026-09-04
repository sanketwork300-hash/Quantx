from __future__ import annotations

import uuid
from datetime import date, datetime, time
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


class TwoSidedLayoutIn(APIModel):
    """Where each side's fields sit, by 0-based column index.

    Chain exports put calls to the left of the strike and puts to the right,
    repeating every header name once per side. Indices, not names, because the
    names cannot tell the two sides apart -- reading such a file by name gives
    every call the put's prices, with no error anywhere.

    ``expiry`` is required: a chain export names one expiry in its filename and
    in no column. The preview offers the filename's date as a suggestion; it is
    never applied on its own, because a wrong expiry silently moves every
    contract along the term structure.
    """

    header_row: int = Field(default=0, ge=0, le=64)
    strike_column: int = Field(ge=0)
    call_columns: dict[str, int]
    put_columns: dict[str, int]
    shared_columns: dict[str, int] = Field(default_factory=dict)
    expiry: date


class PreviewRequest(APIModel):
    #: Canonical field name -> source column header. Omit to use inference; the
    #: inferred mapping is always returned so the user can confirm or correct it
    #: before anything is committed.
    column_mapping: dict[str, str] | None = None
    #: Omit to use the detected layout. The detection is always returned with
    #: its evidence so the user can confirm or override it.
    layout: TwoSidedLayoutIn | None = None
    limit: int = Field(default=50, ge=1, le=500)


class DetectedLayoutOut(APIModel):
    layout: str
    headers: list[str]
    two_sided: dict[str, Any] | None = None
    #: Why the file was read this way, in the user's terms. Shown in the preview
    #: because a layout, like a column mapping, is a reading the user confirms.
    evidence: list[str] = Field(default_factory=list)
    unmapped_columns: list[str] = Field(default_factory=list)
    suggested_expiry: date | None = None
    suggested_symbol: str | None = None
    #: Where a suggestion came from, e.g. "filename". Never a data column.
    suggestion_source: str | None = None


class PreviewResponse(APIModel):
    upload_id: uuid.UUID
    headers: list[str]
    inferred_mapping: dict[str, str]
    applied_mapping: dict[str, str]
    missing_required: list[str]
    unmapped_columns: list[str]
    sample_rows: list[dict[str, Any]]
    parse_errors: list[dict[str, Any]]
    detected_layout: DetectedLayoutOut | None = None


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
    #: Ignored when ``layout`` is supplied: a two-sided file is resolved by
    #: column index and the mapping that follows is the identity over the
    #: fields the layout named.
    column_mapping: dict[str, str] = Field(default_factory=dict)
    #: Present for a two-sided chain export. Confirm it from the preview.
    layout: TwoSidedLayoutIn | None = None
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
