"""Portfolio domain model.

Two rules shape it:

* **Quantity is signed.** ``side`` is kept as the source supplied it, for audit
  and reconciliation, and a sign that disagrees with the side is a validation
  error rather than something to reconcile silently. A position whose sign was
  quietly flipped is a portfolio whose every risk number is wrong with no error
  anywhere.
* **Market and model prices are separate fields**, and neither is ever written
  from the other. Every valuation records which one it used.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from domains.instruments.enums import AssetClass, OptionType
from domains.portfolio.enums import (
    GreekSource,
    PositionSide,
    PositionSource,
    ValuationMethod,
)


class PortfolioError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Portfolio:
    id: uuid.UUID
    user_id: uuid.UUID
    name: str
    base_currency: str
    created_at: datetime
    updated_at: datetime
    description: str | None = None

    def __post_init__(self) -> None:
        if len(self.base_currency) != 3:
            raise PortfolioError(
                f"base_currency must be a 3-letter ISO code, got {self.base_currency!r}"
            )
        if not self.name.strip():
            raise PortfolioError("a portfolio needs a name")


@dataclass(frozen=True, slots=True)
class Position:
    id: uuid.UUID
    portfolio_id: uuid.UUID
    instrument_id: uuid.UUID
    #: Signed. Negative is short.
    quantity: Decimal
    side: PositionSide
    average_price: Decimal | None = None
    source: PositionSource = PositionSource.MANUAL
    strategy_tag: str | None = None
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.quantity == 0:
            raise PortfolioError("a position with zero quantity is not a position")
        expected = PositionSide.for_quantity(self.quantity)
        if self.side is not expected:
            raise PortfolioError(
                f"side {self.side} disagrees with quantity {self.quantity}; "
                "a silently reconciled sign is a portfolio whose risk is wrong "
                "with no error anywhere"
            )


@dataclass(frozen=True, slots=True)
class PositionGreeks:
    """Position-level Greeks. Units are in the field names, deliberately.

    Per-unit Greeks are multiplied by signed quantity and the contract
    multiplier here, and nowhere else, so the scaling happens exactly once.
    """

    delta: float = 0.0
    gamma: float = 0.0
    vega_per_vol_point: float = 0.0
    theta_per_day: float = 0.0
    rho_per_bp: float = 0.0

    def scaled(self, factor: float) -> PositionGreeks:
        return PositionGreeks(
            delta=self.delta * factor,
            gamma=self.gamma * factor,
            vega_per_vol_point=self.vega_per_vol_point * factor,
            theta_per_day=self.theta_per_day * factor,
            rho_per_bp=self.rho_per_bp * factor,
        )

    def __add__(self, other: PositionGreeks) -> PositionGreeks:
        return PositionGreeks(
            delta=self.delta + other.delta,
            gamma=self.gamma + other.gamma,
            vega_per_vol_point=self.vega_per_vol_point + other.vega_per_vol_point,
            theta_per_day=self.theta_per_day + other.theta_per_day,
            rho_per_bp=self.rho_per_bp + other.rho_per_bp,
        )

    def to_dict(self) -> dict:
        return {
            "delta": self.delta,
            "gamma": self.gamma,
            "vega_per_vol_point": self.vega_per_vol_point,
            "theta_per_day": self.theta_per_day,
            "rho_per_bp": self.rho_per_bp,
        }


#: The asset class recorded when a position's instrument is no longer in the
#: master. Deliberately *not* an ``AssetClass`` member: labelling an unresolvable
#: position EQUITY would invent a fact about it, and adding UNKNOWN to the enum
#: would let an instrument be created with no asset class at all.
UNRESOLVED_ASSET_CLASS = "UNRESOLVED"

#: What each Greek means, surfaced by the API so nothing has to be guessed.
GREEK_UNITS = {
    "delta": "base-currency change per +1 unit of the underlying",
    "gamma": "delta change per +1 unit of the underlying",
    "vega_per_vol_point": "base-currency change per +1 volatility point (+0.01)",
    "theta_per_day": "base-currency change per calendar day",
    "rho_per_bp": "base-currency change per +1 basis point",
}


@dataclass(frozen=True, slots=True)
class PositionValuation:
    """One position, valued. Observations and estimates in separate fields."""

    position_id: uuid.UUID
    instrument_id: uuid.UUID
    canonical_key: str
    asset_class: AssetClass | str
    quantity: Decimal
    multiplier: Decimal
    currency: str

    #: The observed price, when there was one. Never written from the model.
    market_price: Decimal | None = None
    #: The model price from the fitted surface. Never written from the market.
    model_price: Decimal | None = None
    #: The price actually used, and how it was obtained.
    price_used: Decimal | None = None
    valuation_method: ValuationMethod = ValuationMethod.UNAVAILABLE

    #: quantity * multiplier * price, in the instrument's own currency.
    market_value: Decimal | None = None
    #: The same, converted to the portfolio's base currency.
    base_market_value: Decimal | None = None
    fx_rate: Decimal | None = None
    unrealized_pnl: Decimal | None = None

    greeks: PositionGreeks = field(default_factory=PositionGreeks)
    greek_source: GreekSource = GreekSource.UNAVAILABLE
    implied_volatility: float | None = None
    time_to_expiry: float | None = None
    underlying_id: uuid.UUID | None = None
    expiry: date | None = None
    strike: Decimal | None = None
    option_type: OptionType | None = None
    quote_age_seconds: float | None = None
    warnings: tuple[str, ...] = field(default=())

    @property
    def ok(self) -> bool:
        return self.base_market_value is not None

    def to_dict(self) -> dict:
        return {
            "position_id": str(self.position_id),
            "instrument_id": str(self.instrument_id),
            "canonical_key": self.canonical_key,
            "asset_class": str(self.asset_class),
            "quantity": format(self.quantity, "f"),
            "multiplier": format(self.multiplier, "f"),
            "currency": self.currency,
            "market_price": _fmt(self.market_price),
            "model_price": _fmt(self.model_price),
            "price_used": _fmt(self.price_used),
            "valuation_method": str(self.valuation_method),
            "market_value": _fmt(self.market_value),
            "base_market_value": _fmt(self.base_market_value),
            "fx_rate": _fmt(self.fx_rate),
            "unrealized_pnl": _fmt(self.unrealized_pnl),
            "greeks": self.greeks.to_dict(),
            "greek_source": str(self.greek_source),
            "implied_volatility": self.implied_volatility,
            "time_to_expiry": self.time_to_expiry,
            "underlying_id": str(self.underlying_id) if self.underlying_id else None,
            "expiry": self.expiry.isoformat() if self.expiry else None,
            "strike": _fmt(self.strike),
            "option_type": str(self.option_type) if self.option_type else None,
            "quote_age_seconds": self.quote_age_seconds,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True, slots=True)
class AggregateBucket:
    """One group of positions.

    Every dimension sums the *same* per-position numbers, so each grouping
    totals to the portfolio total; ``valued`` says how many of the members
    contributed, because a bucket of ten positions with two priced is not the
    same number as a bucket of two.
    """

    dimension: str
    key: str
    label: str
    base_market_value: Decimal
    greeks: PositionGreeks
    positions: int
    valued: int = 0
    gross_exposure: Decimal = Decimal(0)
    unrealized_pnl: Decimal = Decimal(0)

    @property
    def net_exposure(self) -> Decimal:
        return self.base_market_value

    def to_dict(self) -> dict:
        return {
            "dimension": self.dimension,
            "key": self.key,
            "label": self.label,
            "positions": self.positions,
            "valued": self.valued,
            "base_market_value": format(self.base_market_value, "f"),
            "gross_exposure": format(self.gross_exposure, "f"),
            "net_exposure": format(self.net_exposure, "f"),
            "unrealized_pnl": format(self.unrealized_pnl, "f"),
            "greeks": self.greeks.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class PortfolioValuation:
    portfolio_id: uuid.UUID
    base_currency: str
    as_of: datetime
    market_state_id: str | None
    valuations: tuple[PositionValuation, ...]
    aggregates: tuple[AggregateBucket, ...] = field(default=())

    @property
    def base_market_value(self) -> Decimal:
        return sum(
            (v.base_market_value for v in self.valuations if v.base_market_value is not None),
            Decimal(0),
        )

    @property
    def unrealized_pnl(self) -> Decimal:
        return sum(
            (v.unrealized_pnl for v in self.valuations if v.unrealized_pnl is not None),
            Decimal(0),
        )

    @property
    def gross_exposure(self) -> Decimal:
        return sum(
            (abs(v.base_market_value) for v in self.valuations if v.base_market_value is not None),
            Decimal(0),
        )

    @property
    def net_exposure(self) -> Decimal:
        return self.base_market_value

    @property
    def greeks(self) -> PositionGreeks:
        total = PositionGreeks()
        for valuation in self.valuations:
            total = total + valuation.greeks
        return total

    @property
    def valued(self) -> int:
        return sum(1 for v in self.valuations if v.ok)

    @property
    def method_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for valuation in self.valuations:
            key = str(valuation.valuation_method)
            counts[key] = counts.get(key, 0) + 1
        return counts

    def to_dict(self, include_positions: bool = True) -> dict:
        payload = {
            "portfolio_id": str(self.portfolio_id),
            "base_currency": self.base_currency,
            "as_of_timestamp": self.as_of.isoformat(),
            "market_state_id": self.market_state_id,
            "counts": {
                "positions": len(self.valuations),
                "valued": self.valued,
                "unvalued": len(self.valuations) - self.valued,
            },
            "valuation_methods": self.method_counts,
            "totals": {
                "base_market_value": format(self.base_market_value, "f"),
                "unrealized_pnl": format(self.unrealized_pnl, "f"),
                "gross_exposure": format(self.gross_exposure, "f"),
                "net_exposure": format(self.net_exposure, "f"),
            },
            "greeks": self.greeks.to_dict(),
            "greek_units": dict(GREEK_UNITS),
            "aggregates": [bucket.to_dict() for bucket in self.aggregates],
        }
        if include_positions:
            payload["positions"] = [v.to_dict() for v in self.valuations]
        return payload


def _fmt(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")
