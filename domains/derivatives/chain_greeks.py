"""Greeks for every contract in an analysed chain.

The platform already prices and differentiates one contract at a time. What this
adds is the chain-wide view: delta, gamma, vega, theta and rho for every quote
whose implied volatility was solved, read off a stored analysis rather than
recomputed from a live feed.

Three choices worth stating, because each is a place a Greek can quietly become
the wrong number.

**The volatility is the one solved from that contract's own quote**, not the
surface's fitted value at its strike. Chain Greeks in this sense describe the
market as quoted: the delta of the option you can actually trade, at the price
it is actually shown. A fitted-surface Greek is a different, also useful,
quantity — and mixing them silently would produce a table where neighbouring
strikes were measured against different things.

**A contract whose implied volatility did not solve has no Greeks at all.** It
is listed with the reason rather than given zeros. A row of zeros reads as an
option that carries no risk, which is the single most dangerous output this
module could produce.

**Units are named on the payload**, because an unlabelled vega of 0.42 could be
per 1.00 of volatility or per volatility point, and those differ by a factor of
one hundred.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date

from domains.derivatives.models import ChainAnalysis, ImpliedVolPoint, SmileSlice
from domains.instruments.enums import OptionType
from quant.pricing.black_scholes import bsm_greeks

CHAIN_GREEKS_MODEL_VERSION = "chain-greeks@1.0.0"

#: What each returned number is per. Mirrors ``quant.pricing.greeks``.
GREEK_UNITS = {
    "delta": "currency change per 1.00 of underlying",
    "gamma": "delta change per 1.00 of underlying",
    "vega_per_vol_point": "currency change per +0.01 of volatility",
    "theta_per_day": "currency change per calendar day",
    "rho_per_bp": "currency change per +1 basis point of rate",
}


class GreekUnavailable:
    NO_IMPLIED_VOL = "NO_IMPLIED_VOL"
    NO_TIME_TO_EXPIRY = "NO_TIME_TO_EXPIRY"
    NO_UNDERLYING_PRICE = "NO_UNDERLYING_PRICE"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True, slots=True)
class ContractGreeks:
    """One contract's sensitivities, per unit contract.

    Position-level Greeks multiply by signed quantity and the contract
    multiplier; that scaling belongs to the portfolio domain, not here.
    """

    instrument_id: uuid.UUID
    expiry: date
    strike: float
    option_type: OptionType
    market_iv: float
    time_to_expiry: float
    price: float
    delta: float
    gamma: float
    vega_per_vol_point: float
    theta_per_day: float
    rho_per_bp: float

    def to_dict(self) -> dict:
        return {
            "instrument_id": str(self.instrument_id),
            "expiry": self.expiry.isoformat(),
            "strike": self.strike,
            "option_type": str(self.option_type),
            "market_iv": self.market_iv,
            "time_to_expiry": self.time_to_expiry,
            "price": self.price,
            "delta": self.delta,
            "gamma": self.gamma,
            "vega_per_vol_point": self.vega_per_vol_point,
            "theta_per_day": self.theta_per_day,
            "rho_per_bp": self.rho_per_bp,
        }


@dataclass(frozen=True, slots=True)
class UnavailableGreeks:
    """A contract with no Greeks, and why. Never a row of zeros."""

    instrument_id: uuid.UUID
    expiry: date
    strike: float
    option_type: OptionType
    reason: str

    def to_dict(self) -> dict:
        return {
            "instrument_id": str(self.instrument_id),
            "expiry": self.expiry.isoformat(),
            "strike": self.strike,
            "option_type": str(self.option_type),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ExpiryGreeks:
    expiry: date
    time_to_expiry: float | None
    forward: float | None
    contracts: tuple[ContractGreeks, ...] = ()
    unavailable: tuple[UnavailableGreeks, ...] = ()

    def to_dict(self, include_contracts: bool = True) -> dict:
        payload = {
            "expiry": self.expiry.isoformat(),
            "time_to_expiry": self.time_to_expiry,
            "forward": self.forward,
            "counts": {
                "priced": len(self.contracts),
                "unavailable": len(self.unavailable),
            },
            "unavailable": [item.to_dict() for item in self.unavailable],
        }
        if include_contracts:
            payload["contracts"] = [item.to_dict() for item in self.contracts]
        return payload


@dataclass(frozen=True, slots=True)
class ChainGreeks:
    """Greeks across a whole analysed chain."""

    analysis_id: uuid.UUID | None
    underlying_id: uuid.UUID
    #: ISO-8601, carried through from ``ChainAnalysis.as_of`` unchanged. Kept as
    #: the analysis holds it rather than reparsed, so the moment these Greeks
    #: describe is the same string the analysis reports and cannot drift from it
    #: through a round trip.
    as_of: str
    underlying_price: float | None
    risk_free_rate: float
    dividend_yield: float
    #: True when the yield was not supplied and a zero was used in its place.
    #: A wrong carry moves every delta, so the assumption travels with the answer.
    dividend_yield_assumed: bool
    #: Which volatility the Greeks were measured against.
    volatility_source: str = "market_implied_per_contract"
    expiries: tuple[ExpiryGreeks, ...] = ()
    model_version: str = CHAIN_GREEKS_MODEL_VERSION

    @property
    def priced(self) -> int:
        return sum(len(item.contracts) for item in self.expiries)

    @property
    def unavailable(self) -> int:
        return sum(len(item.unavailable) for item in self.expiries)

    def to_dict(self, include_contracts: bool = True) -> dict:
        return {
            "analysis_id": str(self.analysis_id) if self.analysis_id else None,
            "underlying_id": str(self.underlying_id),
            "as_of": self.as_of,
            "underlying_price": self.underlying_price,
            "risk_free_rate": self.risk_free_rate,
            "dividend_yield": self.dividend_yield,
            "dividend_yield_assumed": self.dividend_yield_assumed,
            "volatility_source": self.volatility_source,
            "model_version": self.model_version,
            "units": dict(GREEK_UNITS),
            "counts": {"priced": self.priced, "unavailable": self.unavailable},
            "expiries": [item.to_dict(include_contracts) for item in self.expiries],
        }


def _reason_for(point: ImpliedVolPoint, slice_: SmileSlice, spot: float | None) -> str | None:
    if spot is None:
        return GreekUnavailable.NO_UNDERLYING_PRICE
    tau = point.time_to_expiry or slice_.time_to_expiry
    if tau is None:
        return GreekUnavailable.NO_TIME_TO_EXPIRY
    if tau <= 0:
        return GreekUnavailable.EXPIRED
    if point.market_iv is None or point.market_iv <= 0:
        return GreekUnavailable.NO_IMPLIED_VOL
    return None


def chain_greeks(
    analysis: ChainAnalysis,
    underlying_price: float | None,
    risk_free_rate: float = 0.0,
    dividend_yield: float = 0.0,
    dividend_yield_assumed: bool = True,
    analysis_id: uuid.UUID | None = None,
) -> ChainGreeks:
    """Greeks for every solved contract in a stored analysis.

    Computed on read rather than stored: they are a deterministic function of
    the persisted implied volatilities and the carry assumption, so a stored
    copy could only drift from the analysis it describes.
    """
    expiries: list[ExpiryGreeks] = []

    for slice_ in analysis.slices:
        priced: list[ContractGreeks] = []
        unavailable: list[UnavailableGreeks] = []
        selected = slice_.forward.selected

        for point in slice_.points:
            reason = _reason_for(point, slice_, underlying_price)
            if reason is not None:
                unavailable.append(
                    UnavailableGreeks(
                        instrument_id=point.instrument_id,
                        expiry=point.expiry,
                        strike=float(point.strike),
                        option_type=point.option_type,
                        reason=reason,
                    )
                )
                continue

            tau = float(point.time_to_expiry or slice_.time_to_expiry)
            greeks = bsm_greeks(
                float(underlying_price),
                float(point.strike),
                tau,
                risk_free_rate,
                dividend_yield,
                float(point.market_iv),
                point.option_type is OptionType.CALL,
            )
            priced.append(
                ContractGreeks(
                    instrument_id=point.instrument_id,
                    expiry=point.expiry,
                    strike=float(point.strike),
                    option_type=point.option_type,
                    market_iv=float(point.market_iv),
                    time_to_expiry=tau,
                    price=float(greeks.price),
                    delta=float(greeks.delta),
                    gamma=float(greeks.gamma),
                    vega_per_vol_point=float(greeks.vega_per_vol_point),
                    theta_per_day=float(greeks.theta_per_day),
                    rho_per_bp=float(greeks.rho_per_bp),
                )
            )

        expiries.append(
            ExpiryGreeks(
                expiry=slice_.expiry,
                time_to_expiry=slice_.time_to_expiry,
                forward=float(selected.value) if selected is not None else None,
                contracts=tuple(priced),
                unavailable=tuple(unavailable),
            )
        )

    return ChainGreeks(
        analysis_id=analysis_id,
        underlying_id=analysis.underlying_id,
        as_of=analysis.as_of,
        underlying_price=float(underlying_price) if underlying_price is not None else None,
        risk_free_rate=risk_free_rate,
        dividend_yield=dividend_yield,
        dividend_yield_assumed=dividend_yield_assumed,
        expiries=tuple(expiries),
    )
