"""Derivatives domain results.

The separation the whole platform rests on is visible in the field names here:
``market_iv`` is the volatility implied by an *observed* price under a stated
model, and it lives apart from the ``reference_iv`` a fitted surface will
produce in Phase 2. Neither is ever written from the other.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import StrEnum

from domains.derivatives.forward import ForwardEstimateSet
from domains.instruments.enums import OptionType


class PriceSource(StrEnum):
    """Which observed price the implied volatility was solved from."""

    MID = "MID"
    LAST = "LAST"
    NONE = "NONE"


class SmileExclusion(StrEnum):
    """Why an otherwise valid quote is not used to build the smile.

    Distinct from a data-quality exclusion: these quotes are fine, they are
    simply not the ones a smile should be built from.
    """

    NOT_SELECTED_SIDE = "NOT_SELECTED_SIDE"
    NO_IMPLIED_VOL = "NO_IMPLIED_VOL"
    ILL_CONDITIONED = "ILL_CONDITIONED"


@dataclass(frozen=True, slots=True)
class ImpliedVolPoint:
    """One quote's implied volatility, with everything needed to judge it."""

    instrument_id: uuid.UUID
    expiry: date
    strike: Decimal
    option_type: OptionType

    price_used: float | None
    price_source: PriceSource

    market_iv: float | None
    #: Quoted ask minus bid at the time of the solve. Carried here because it is
    #: the scale an arbitrage violation's severity is judged against: a breach
    #: smaller than the spread is not exploitable.
    price_spread: float | None = None
    market_iv_bid: float | None = None
    market_iv_ask: float | None = None

    converged: bool = False
    iterations: int = 0
    solver: str = "none"
    error: str | None = None
    vega: float | None = None
    #: Volatility uncertainty implied by one price ulp; see quant.volatility.
    uncertainty: float | None = None

    #: Carried from the Phase 0 quality engine so downstream confidence scoring
    #: uses the same measurement rather than a second, divergent one.
    data_quality_score: float = 1.0
    liquidity_score: float = 1.0

    time_to_expiry: float | None = None
    log_moneyness: float | None = None
    total_variance: float | None = None
    #: Spread and liquidity weight, carried forward for Phase 2 calibration.
    weight: float = 0.0

    used_for_smile: bool = False
    smile_exclusion: SmileExclusion | None = None

    @property
    def iv_envelope_width(self) -> float | None:
        """Bid-ask implied volatility width: how much of an apparent deviation
        from any reference is simply the spread."""
        if self.market_iv_bid is None or self.market_iv_ask is None:
            return None
        return self.market_iv_ask - self.market_iv_bid

    def to_dict(self) -> dict:
        return {
            "instrument_id": str(self.instrument_id),
            "expiry": self.expiry.isoformat(),
            "strike": format(self.strike, "f"),
            "option_type": str(self.option_type),
            "price_used": self.price_used,
            "price_source": str(self.price_source),
            "price_spread": self.price_spread,
            "market_iv": self.market_iv,
            "market_iv_bid": self.market_iv_bid,
            "market_iv_ask": self.market_iv_ask,
            "iv_envelope_width": self.iv_envelope_width,
            "converged": self.converged,
            "iterations": self.iterations,
            "solver": self.solver,
            "error": self.error,
            "vega": self.vega,
            "uncertainty": self.uncertainty,
            "data_quality_score": self.data_quality_score,
            "liquidity_score": self.liquidity_score,
            "time_to_expiry": self.time_to_expiry,
            "log_moneyness": self.log_moneyness,
            "total_variance": self.total_variance,
            "weight": self.weight,
            "used_for_smile": self.used_for_smile,
            "smile_exclusion": (str(self.smile_exclusion) if self.smile_exclusion else None),
        }


@dataclass(frozen=True, slots=True)
class SmileSlice:
    """One expiry's observed smile and the forward it is measured against."""

    expiry: date
    time_to_expiry: float | None
    forward: ForwardEstimateSet
    points: tuple[ImpliedVolPoint, ...]
    atm_volatility: float | None = None
    skew: float | None = None
    curvature: float | None = None
    settlement_time_assumed: bool = False
    reason: str | None = None

    @property
    def solved(self) -> int:
        return sum(1 for point in self.points if point.market_iv is not None)

    @property
    def used(self) -> int:
        return sum(1 for point in self.points if point.used_for_smile)

    @property
    def solve_failures(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for point in self.points:
            if point.error:
                counts[point.error] = counts.get(point.error, 0) + 1
        return counts

    def to_dict(self, include_points: bool = True) -> dict:
        payload = {
            "expiry": self.expiry.isoformat(),
            "time_to_expiry": self.time_to_expiry,
            "settlement_time_assumed": self.settlement_time_assumed,
            "forward": self.forward.to_dict(),
            "counts": {
                "quotes": len(self.points),
                "solved": self.solved,
                "used_for_smile": self.used,
            },
            "solve_failures": self.solve_failures,
            "atm_volatility": self.atm_volatility,
            "skew": self.skew,
            "curvature": self.curvature,
            "reason": self.reason,
        }
        if include_points:
            payload["points"] = [point.to_dict() for point in self.points]
        return payload


@dataclass(frozen=True, slots=True)
class ChainAnalysis:
    """The Phase 1 output: forwards, implied volatilities and raw smiles."""

    snapshot_id: uuid.UUID
    underlying_id: uuid.UUID
    as_of: str
    slices: tuple[SmileSlice, ...] = field(default=())
    underlying_price: Decimal | None = None
    curve_id: str | None = None

    @property
    def total_quotes(self) -> int:
        return sum(len(slice_.points) for slice_ in self.slices)

    @property
    def total_solved(self) -> int:
        return sum(slice_.solved for slice_ in self.slices)

    def to_dict(self, include_points: bool = True) -> dict:
        return {
            "snapshot_id": str(self.snapshot_id),
            "underlying_id": str(self.underlying_id),
            "as_of_timestamp": self.as_of,
            "underlying_price": (
                format(self.underlying_price, "f") if self.underlying_price is not None else None
            ),
            "curve_id": self.curve_id,
            "counts": {
                "quotes": self.total_quotes,
                "solved": self.total_solved,
                "expiries": len(self.slices),
            },
            "slices": [slice_.to_dict(include_points) for slice_ in self.slices],
        }
