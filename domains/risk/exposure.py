"""What a position needs in order to be repriced in a different world.

A stress test is not a re-read of the market: under a shock, nobody quoted
anything, so every stressed price must come from a model. That makes the *base*
price a modelling choice too. If the base were the observed market price and the
stressed price came from a model, the difference would mix the shock with the
model's disagreement with the market, and a null scenario would show a P&L.

So both sides are priced by the same function from the same anchors, and the
anchor volatility is the one implied by the position's own observed price
wherever there was one. A null scenario then reprices to the base value exactly,
and a test asserts it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from decimal import Decimal
from enum import StrEnum

from domains.instruments.enums import AssetClass, OptionType
from domains.portfolio.models import PortfolioValuation, PositionGreeks, PositionValuation
from domains.portfolio.valuation import ValuationContext
from quant.pricing.black_scholes import bsm_price

#: Volatility floor for a shocked slice, mirrored from the scenarios domain so
#: this module does not depend on it.
MIN_VOLATILITY = 1e-4


class ExposureExclusion(StrEnum):
    NO_PRICE = "EXPOSURE_NO_PRICE"
    NO_VOLATILITY = "EXPOSURE_NO_VOLATILITY"
    NO_UNDERLYING_LEVEL = "EXPOSURE_NO_UNDERLYING_LEVEL"
    NO_TIME_TO_EXPIRY = "EXPOSURE_NO_TIME_TO_EXPIRY"
    NO_FX_RATE = "EXPOSURE_NO_FX_RATE"


@dataclass(frozen=True, slots=True)
class ExcludedExposure:
    """A position that cannot be stressed, and why. Never counted as flat."""

    position_id: uuid.UUID
    canonical_key: str
    reason: ExposureExclusion
    base_value: float | None

    def to_dict(self) -> dict:
        return {
            "position_id": str(self.position_id),
            "canonical_key": self.canonical_key,
            "reason": str(self.reason),
            "base_value": self.base_value,
        }


@dataclass(frozen=True, slots=True)
class PositionExposure:
    """One position, with the anchors both the base and the shocked price use."""

    position_id: uuid.UUID
    instrument_id: uuid.UUID
    canonical_key: str
    asset_class: AssetClass | str
    underlying_id: uuid.UUID | None
    underlying_key: str
    currency: str
    strategy_tag: str | None

    #: signed quantity * contract multiplier, applied once, here.
    scale: float
    fx_rate: float
    spot: float

    is_option: bool
    strike: float | None = None
    option_type: OptionType | None = None
    time_to_expiry: float | None = None
    implied_volatility: float | None = None
    rate: float = 0.0
    dividend_yield: float = 0.0

    #: Per-unit price at the anchors, and the position value it implies.
    base_price: float = 0.0
    #: The market value the portfolio valuation recorded, in base currency. Kept
    #: alongside so the gap between "what it is marked at" and "what the model
    #: says at the same anchors" is visible rather than assumed to be zero.
    reported_value: float = 0.0
    greeks: PositionGreeks = PositionGreeks()

    @property
    def base_value(self) -> float:
        return self.base_price * self.scale * self.fx_rate

    def price_at(
        self,
        spot: float,
        volatility: float | None = None,
        rate: float | None = None,
        dividend_yield: float | None = None,
        time_to_expiry: float | None = None,
    ) -> float:
        """Per-unit price in this world. The same function prices both sides."""
        if not self.is_option:
            # Linear in its own level: a future or an index leg is worth its
            # level, and a shocked level is the whole of the repricing.
            return spot

        tau = self.time_to_expiry if time_to_expiry is None else time_to_expiry
        sigma = self.implied_volatility if volatility is None else volatility
        return float(
            bsm_price(
                spot,
                self.strike,
                max(tau, 0.0),
                self.rate if rate is None else rate,
                self.dividend_yield if dividend_yield is None else dividend_yield,
                max(sigma, MIN_VOLATILITY),
                self.option_type is OptionType.CALL,
            )
        )

    def value_at(self, **kwargs) -> float:
        return self.price_at(**kwargs) * self.scale * self.fx_rate

    def shifted(
        self,
        spot_return: float = 0.0,
        vol_points: float = 0.0,
        rate_shift: float = 0.0,
        min_volatility: float = MIN_VOLATILITY,
    ) -> PositionExposure:
        """This position as it would stand in a moved market.

        The anchors move and the base price is recomputed from them, so the
        result is a book that can be measured *again* — which is what a margin
        ladder needs: at every rung, the margin model must run on the state the
        market would be in, not on today's.
        """
        spot = self.spot * (1.0 + spot_return)
        volatility = (
            max((self.implied_volatility or 0.0) + vol_points, min_volatility)
            if self.is_option
            else self.implied_volatility
        )
        rate = self.rate + rate_shift
        price = self.price_at(
            spot=spot, volatility=volatility, rate=rate, dividend_yield=self.dividend_yield
        )
        return replace(
            self,
            spot=spot,
            implied_volatility=volatility,
            rate=rate,
            base_price=price,
            # The mark belongs to the market that was actually observed; a
            # hypothetical state has no mark, so it carries the model value and
            # the two agree by construction.
            reported_value=price * self.scale * self.fx_rate,
        )

    @property
    def notional(self) -> float:
        """Contract notional in base currency: what one contract controls.

        For an option that is strike times multiplier — the amount that changes
        hands on exercise — not the premium, which is what it costs.
        """
        reference = self.strike if self.is_option and self.strike is not None else self.spot
        return abs(reference * self.scale * self.fx_rate)

    @property
    def is_short(self) -> bool:
        return self.scale < 0.0

    def to_dict(self) -> dict:
        return {
            "position_id": str(self.position_id),
            "instrument_id": str(self.instrument_id),
            "canonical_key": self.canonical_key,
            "asset_class": str(self.asset_class),
            "underlying_id": str(self.underlying_id) if self.underlying_id else None,
            "currency": self.currency,
            "strategy_tag": self.strategy_tag,
            "scale": self.scale,
            "fx_rate": self.fx_rate,
            "spot": self.spot,
            "is_option": self.is_option,
            "strike": self.strike,
            "option_type": str(self.option_type) if self.option_type else None,
            "time_to_expiry": self.time_to_expiry,
            "implied_volatility": self.implied_volatility,
            "rate": self.rate,
            "dividend_yield": self.dividend_yield,
            "base_price": self.base_price,
            "base_value": self.base_value,
            "reported_value": self.reported_value,
        }


@dataclass(frozen=True, slots=True)
class ExposureSet:
    """Every position that can be repriced, and every one that cannot."""

    exposures: tuple[PositionExposure, ...]
    excluded: tuple[ExcludedExposure, ...]
    base_currency: str

    @property
    def base_value(self) -> float:
        return sum(exposure.base_value for exposure in self.exposures)

    @property
    def reported_value(self) -> float:
        return sum(exposure.reported_value for exposure in self.exposures)

    @property
    def excluded_reported_value(self) -> float:
        return sum(e.base_value or 0.0 for e in self.excluded)

    @property
    def repricing_gap(self) -> float:
        """Model value minus marked value at the unshocked anchors.

        Zero for a book valued from market prices, because the anchor volatility
        was inverted from those very prices. A non-zero gap means some position
        was marked to model with a reference volatility that does not reprice
        its own mark, and it is reported rather than absorbed into the P&L.
        """
        return self.base_value - self.reported_value

    def shifted(
        self,
        spot_return: float = 0.0,
        vol_points: float = 0.0,
        rate_shift: float = 0.0,
    ) -> ExposureSet:
        """The whole book in a moved market, still carrying its exclusions."""
        return ExposureSet(
            exposures=tuple(
                exposure.shifted(spot_return, vol_points, rate_shift) for exposure in self.exposures
            ),
            excluded=self.excluded,
            base_currency=self.base_currency,
        )

    @property
    def gross_notional(self) -> float:
        return sum(exposure.notional for exposure in self.exposures)

    def gross_notional_by_underlying(self) -> dict[str, float]:
        totals: dict[str, float] = {}
        for exposure in self.exposures:
            totals[exposure.underlying_key] = (
                totals.get(exposure.underlying_key, 0.0) + exposure.notional
            )
        return totals

    def by_underlying(self) -> dict[str, tuple[PositionExposure, ...]]:
        groups: dict[str, list[PositionExposure]] = {}
        for exposure in self.exposures:
            groups.setdefault(exposure.underlying_key, []).append(exposure)
        return {key: tuple(value) for key, value in sorted(groups.items())}

    def underlying_keys(self) -> tuple[str, ...]:
        return tuple(sorted({exposure.underlying_key for exposure in self.exposures}))

    def to_dict(self) -> dict:
        return {
            "base_currency": self.base_currency,
            "positions": len(self.exposures),
            "excluded": [item.to_dict() for item in self.excluded],
            "base_value": self.base_value,
            "reported_value": self.reported_value,
            "repricing_gap": self.repricing_gap,
        }


def build_exposures(
    valuation: PortfolioValuation,
    context: ValuationContext,
    strategy_tags: dict[uuid.UUID, str] | None = None,
) -> ExposureSet:
    """Turn a completed valuation into something that can be repriced."""
    tags = strategy_tags or {}
    exposures: list[PositionExposure] = []
    excluded: list[ExcludedExposure] = []

    for position in valuation.valuations:
        outcome = _exposure_for(position, context, tags.get(position.position_id))
        if isinstance(outcome, ExcludedExposure):
            excluded.append(outcome)
        else:
            exposures.append(outcome)

    return ExposureSet(
        exposures=tuple(exposures),
        excluded=tuple(excluded),
        base_currency=valuation.base_currency,
    )


def _exposure_for(
    position: PositionValuation, context: ValuationContext, strategy_tag: str | None
) -> PositionExposure | ExcludedExposure:
    def exclude(reason: ExposureExclusion) -> ExcludedExposure:
        return ExcludedExposure(
            position_id=position.position_id,
            canonical_key=position.canonical_key,
            reason=reason,
            base_value=_as_float(position.base_market_value),
        )

    if position.price_used is None:
        return exclude(ExposureExclusion.NO_PRICE)
    if position.fx_rate is None:
        return exclude(ExposureExclusion.NO_FX_RATE)

    is_option = position.option_type is not None and position.strike is not None
    underlying_id = position.underlying_id or position.instrument_id
    spot = context.market_state.spot_prices.get(underlying_id)

    if is_option:
        if spot is None:
            return exclude(ExposureExclusion.NO_UNDERLYING_LEVEL)
        if position.implied_volatility is None:
            return exclude(ExposureExclusion.NO_VOLATILITY)
        if position.time_to_expiry is None or position.time_to_expiry <= 0:
            return exclude(ExposureExclusion.NO_TIME_TO_EXPIRY)
        base_price = float(position.price_used)
    else:
        # A linear instrument's own level is its price, whether or not the
        # platform separately holds a spot for it.
        spot = spot if spot is not None else position.price_used
        base_price = float(spot)

    scale = float(position.quantity) * float(position.multiplier)
    fx_rate = float(position.fx_rate)

    return PositionExposure(
        position_id=position.position_id,
        instrument_id=position.instrument_id,
        canonical_key=position.canonical_key,
        asset_class=position.asset_class,
        underlying_id=underlying_id,
        underlying_key=str(underlying_id),
        currency=position.currency,
        strategy_tag=strategy_tag,
        scale=scale,
        fx_rate=fx_rate,
        spot=float(spot),
        is_option=is_option,
        strike=float(position.strike) if position.strike is not None else None,
        option_type=position.option_type,
        time_to_expiry=position.time_to_expiry,
        implied_volatility=position.implied_volatility,
        rate=context.risk_free_rate,
        dividend_yield=context.dividend_yield,
        base_price=base_price,
        reported_value=_as_float(position.base_market_value) or 0.0,
        greeks=position.greeks,
    )


def _as_float(value: Decimal | None) -> float | None:
    return None if value is None else float(value)
