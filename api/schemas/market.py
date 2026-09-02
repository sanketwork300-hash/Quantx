from __future__ import annotations

import uuid
from datetime import date, datetime

from api.schemas.common import APIModel, DecimalStr


class QualityFlagOut(APIModel):
    code: str
    severity: str
    message: str
    context: dict = {}


class QualityOut(APIModel):
    stale_score: float
    spread_score: float
    liquidity_score: float
    consistency_score: float
    completeness_score: float
    overall_score: float
    flags: list[QualityFlagOut] = []


class OptionQuoteOut(APIModel):
    instrument_id: uuid.UUID
    expiry: date
    strike: DecimalStr
    option_type: str
    exchange_timestamp: datetime
    receive_timestamp: datetime
    bid_price: DecimalStr | None = None
    bid_size: DecimalStr | None = None
    ask_price: DecimalStr | None = None
    ask_size: DecimalStr | None = None
    last_price: DecimalStr | None = None
    #: Derived from the observed two-sided market. Null when there is none;
    #: it is never backfilled from last_price.
    mid_price: DecimalStr | None = None
    relative_spread: float | None = None
    volume: DecimalStr | None = None
    open_interest: DecimalStr | None = None
    underlying_price: DecimalStr | None = None
    source_row_number: int | None = None
    excluded: bool
    exclusion_reason: str | None = None
    quality: QualityOut


class ChainCountsOut(APIModel):
    input: int
    kept: int
    excluded: int
    rejected: int


class ChainSnapshotOut(APIModel):
    snapshot_id: uuid.UUID
    underlying_id: uuid.UUID
    as_of_timestamp: datetime
    source: str
    provider: str
    dataset_digest: str | None = None
    underlying_price: DecimalStr | None = None
    counts: ChainCountsOut
    quality_summary: dict = {}
    expiries: list[date] = []
    quotes: list[OptionQuoteOut] = []


class ChainSnapshotSummaryOut(APIModel):
    snapshot_id: uuid.UUID
    underlying_id: uuid.UUID
    as_of_timestamp: datetime
    source: str
    counts: ChainCountsOut
    overall_score: float | None = None


class MarketQuoteOut(APIModel):
    instrument_id: uuid.UUID
    exchange_timestamp: datetime
    receive_timestamp: datetime
    bid_price: DecimalStr | None = None
    ask_price: DecimalStr | None = None
    last_price: DecimalStr | None = None
    mid_price: DecimalStr | None = None
    volume: DecimalStr | None = None
    open_interest: DecimalStr | None = None
    source: str
    quality: QualityOut | None = None
