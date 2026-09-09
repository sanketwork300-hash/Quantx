"""Live options intelligence: open interest, delta skew, and the pipeline."""

from __future__ import annotations

import uuid
from datetime import date, datetime, time

from pydantic import Field

from api.schemas.common import APIModel


class LiveChainAnalysisRequest(APIModel):
    """Capture a live chain and take it through to a fitted surface."""

    underlying_id: uuid.UUID
    #: Restrict the capture to one expiry. Omitted, every mapped contract on the
    #: underlying is captured, which is what a term structure needs.
    expiry: date | None = None
    #: Used to discount. Left unset it is zero, and the analysis records that
    #: the rate was an assumption rather than an observation.
    risk_free_rate: float | None = Field(default=None, ge=-0.5, le=1.0)
    #: Omitted means the dividend yield is recorded as assumed, which gates the
    #: option bound checks that cannot run without a carry assumption.
    dividend_yield: float | None = Field(default=None, ge=-0.5, le=1.0)
    #: The venue's settlement instant. Without it a time to expiry cannot be
    #: measured to anything finer than a date.
    settlement_time_utc: time | None = None
    calibrate: bool = True


class DeltaStrikeOut(APIModel):
    target_delta: float
    option_type: str
    status: str
    #: Which delta was solved for. Carried on every result because a spot delta
    #: and a forward delta give different strikes, and a number whose convention
    #: is not stated cannot be reconciled with anyone else's.
    delta_convention: str
    log_moneyness: float | None
    strike: float | None
    volatility: float | None
    residual: float | None


class DeltaSmileOut(APIModel):
    delta_level: float
    delta_convention: str
    atm_volatility: float
    #: ``sigma(call) - sigma(put)``. Null when either strike could not be found —
    #: a skew of zero and a skew that could not be measured are different facts.
    risk_reversal: float | None
    butterfly: float | None
    call: DeltaStrikeOut
    put: DeltaStrikeOut


class SliceDeltaSkewOut(APIModel):
    expiry: date
    time_to_expiry: float
    forward: float
    atm_volatility: float
    degraded: bool
    smiles: list[DeltaSmileOut]


class UnmeasuredSliceOut(APIModel):
    expiry: date
    reason: str


class SurfaceDeltaSkewOut(APIModel):
    surface_id: str
    as_of: datetime
    delta_convention: str
    model_version: str
    levels: list[float]
    slices: list[SliceDeltaSkewOut]
    #: Expiries with no measurement. Listed rather than omitted: a term
    #: structure with a silent hole reads as a smooth curve.
    unmeasured: list[UnmeasuredSliceOut]


class StrikeOpenInterestOut(APIModel):
    strike: str
    call_open_interest: str | None
    put_open_interest: str | None
    total_open_interest: str | None
    call_volume: str | None
    put_volume: str | None
    put_call_ratio_open_interest: float | None


class ExpiryOpenInterestOut(APIModel):
    expiry: date
    call_open_interest: str | None
    put_open_interest: str | None
    total_open_interest: str | None
    call_volume: str | None
    put_volume: str | None
    total_volume: str | None
    #: A measured ratio. This platform reports it and does not interpret it.
    put_call_ratio_open_interest: float | None
    put_call_ratio_volume: float | None
    volume_to_open_interest: float | None
    contracts: int
    #: How many of those carried an open-interest figure at all. A total built
    #: from a third of the chain is a different number from one built from all.
    contracts_with_open_interest: int
    coverage: float | None
    most_open_interest: list[StrikeOpenInterestOut]
    strikes: list[StrikeOpenInterestOut] = Field(default_factory=list)


class OpenInterestProfileOut(APIModel):
    underlying_id: uuid.UUID
    snapshot_id: uuid.UUID | None
    as_of: datetime
    #: The venue's unit, unnormalised. Some exchanges publish open interest in
    #: contracts and some in units of the underlying; the platform is not told
    #: which, so ratios are safe and absolute totals carry this label.
    open_interest_unit: str
    model_version: str
    call_open_interest: str | None
    put_open_interest: str | None
    put_call_ratio_open_interest: float | None
    excluded_contracts: int
    expiries: list[ExpiryOpenInterestOut]


class ContractOpenInterestChangeOut(APIModel):
    instrument_id: uuid.UUID
    expiry: date
    strike: str
    option_type: str
    earlier_open_interest: str | None
    later_open_interest: str | None
    change: str | None


class OpenInterestChangeOut(APIModel):
    underlying_id: uuid.UUID
    earlier_as_of: datetime
    later_as_of: datetime
    #: The window the change happened over. Carried on every response, because
    #: without it a figure measured over eleven minutes reads exactly like one
    #: measured over a session.
    window_seconds: float
    earlier_snapshot_id: uuid.UUID | None
    later_snapshot_id: uuid.UUID | None
    open_interest_unit: str
    matched_contracts: int
    #: Contracts in only one of the two snapshots. Ordinary as strikes are
    #: listed and delisted, but a total that dropped them would not add up.
    only_in_earlier: int
    only_in_later: int
    total_change: str | None
    largest_increases: list[ContractOpenInterestChangeOut]
    largest_decreases: list[ContractOpenInterestChangeOut]


class ContractGreeksOut(APIModel):
    """Per unit contract. Position scaling belongs to the portfolio domain."""

    instrument_id: uuid.UUID
    expiry: date
    strike: float
    option_type: str
    market_iv: float
    time_to_expiry: float
    price: float
    delta: float
    gamma: float
    vega_per_vol_point: float
    theta_per_day: float
    rho_per_bp: float


class UnavailableGreeksOut(APIModel):
    """A contract with no Greeks, and why. Never a row of zeros — a zero delta
    reads as an option that carries no risk."""

    instrument_id: uuid.UUID
    expiry: date
    strike: float
    option_type: str
    reason: str


class ExpiryGreeksOut(APIModel):
    expiry: date
    time_to_expiry: float | None
    forward: float | None
    counts: dict[str, int]
    unavailable: list[UnavailableGreeksOut]
    contracts: list[ContractGreeksOut] = Field(default_factory=list)


class ChainGreeksOut(APIModel):
    analysis_id: uuid.UUID | None
    underlying_id: uuid.UUID
    as_of: datetime
    underlying_price: float | None
    risk_free_rate: float
    dividend_yield: float
    #: True when no yield was supplied and zero was used instead. A wrong carry
    #: moves every delta, so the assumption travels with the answer.
    dividend_yield_assumed: bool
    #: Which volatility the Greeks were measured against.
    volatility_source: str
    model_version: str
    #: What each number is per. An unlabelled vega of 0.42 could be per 1.00 of
    #: volatility or per volatility point, and those differ by a factor of 100.
    units: dict[str, str]
    counts: dict[str, int]
    expiries: list[ExpiryGreeksOut]
