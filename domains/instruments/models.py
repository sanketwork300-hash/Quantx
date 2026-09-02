"""Instrument domain model.

Invariants are checked at construction. An instrument that violates them cannot
exist, so no pricing, risk or margin code has to ask "is this option missing a
strike?".
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal

from domains.instruments.enums import (
    UNDATED_CLASSES,
    AssetClass,
    ExerciseStyle,
    InstrumentStatus,
    OptionType,
    SettlementType,
)
from domains.instruments.errors import InvalidInstrument
from domains.instruments.identity import (
    build_canonical_key,
    format_strike,
    instrument_id_for,
    normalize_exchange,
    normalize_symbol,
)

#: Recorded in ``metadata`` when a multiplier/lot size was not supplied by the
#: data source. Build spec 1.1 forbids fabricating contract multipliers, and a
#: wrong multiplier silently scales every Greek and every margin number.
MULTIPLIER_ASSUMED = "multiplier_source"


@dataclass(frozen=True, slots=True)
class Instrument:
    canonical_key: str
    asset_class: AssetClass
    exchange: str
    symbol: str
    currency: str
    multiplier: Decimal = Decimal(1)
    tick_size: Decimal = Decimal("0.01")
    lot_size: Decimal = Decimal(1)
    id: uuid.UUID | None = None
    venue: str | None = None
    underlying_id: uuid.UUID | None = None
    expiry: date | None = None
    strike: Decimal | None = None
    option_type: OptionType | None = None
    exercise_style: ExerciseStyle | None = None
    settlement_type: SettlementType | None = None
    status: InstrumentStatus = InstrumentStatus.ACTIVE
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "exchange", normalize_exchange(self.exchange))
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        object.__setattr__(self, "currency", self.currency.strip().upper())
        if self.strike is not None:
            # Normalise to the same representation the canonical key uses, so
            # 24000, 24000.00 and 2.4E+4 are one value everywhere downstream --
            # in the key, in the model, and in every persisted row.
            object.__setattr__(self, "strike", Decimal(format_strike(self.strike)))

        if self.id is None:
            object.__setattr__(self, "id", instrument_id_for(self.canonical_key))

        self._validate()

    # ------------------------------------------------------------- validation
    def _validate(self) -> None:
        if len(self.currency) != 3:
            raise InvalidInstrument(f"currency must be a 3-letter ISO code, got {self.currency!r}")

        for name in ("multiplier", "tick_size", "lot_size"):
            value = getattr(self, name)
            if not isinstance(value, Decimal):
                raise InvalidInstrument(f"{name} must be a Decimal, got {type(value).__name__}")
            if value <= 0:
                raise InvalidInstrument(f"{name} must be strictly positive, got {value}")

        if self.asset_class is AssetClass.OPTION:
            self._validate_option()
        elif self.asset_class is AssetClass.FUTURE:
            self._validate_future()
        elif self.asset_class in UNDATED_CLASSES:
            self._validate_undated()

        expected = build_canonical_key(
            exchange=self.exchange,
            asset_class=self.asset_class,
            symbol=self.symbol,
            expiry=self.expiry,
            strike=self.strike,
            option_type=self.option_type,
        )
        if self.canonical_key != expected:
            raise InvalidInstrument(
                f"canonical_key {self.canonical_key!r} does not match the instrument's "
                f"own fields (expected {expected!r})"
            )

        if self.id != instrument_id_for(self.canonical_key):
            raise InvalidInstrument(
                "id is not the uuid5 of canonical_key; ids are derived, never assigned"
            )

    def _validate_option(self) -> None:
        missing = [
            name
            for name in ("expiry", "strike", "option_type", "exercise_style", "underlying_id")
            if getattr(self, name) is None
        ]
        if missing:
            raise InvalidInstrument(f"OPTION requires {', '.join(missing)}")
        if self.strike is not None and self.strike <= 0:
            raise InvalidInstrument(f"strike must be strictly positive, got {self.strike}")

    def _validate_future(self) -> None:
        if self.expiry is None:
            raise InvalidInstrument("FUTURE requires expiry")
        if self.underlying_id is None:
            raise InvalidInstrument("FUTURE requires underlying_id")
        if self.strike is not None or self.option_type is not None:
            raise InvalidInstrument("FUTURE must not carry strike or option_type")

    def _validate_undated(self) -> None:
        for name in ("expiry", "strike", "option_type"):
            if getattr(self, name) is not None:
                raise InvalidInstrument(f"{self.asset_class} must not carry {name}")

    # ------------------------------------------------------------ properties
    @property
    def is_option(self) -> bool:
        return self.asset_class is AssetClass.OPTION

    @property
    def is_derivative(self) -> bool:
        return self.asset_class in {
            AssetClass.OPTION,
            AssetClass.FUTURE,
            AssetClass.CRYPTO_PERPETUAL,
        }

    @property
    def multiplier_is_assumed(self) -> bool:
        return self.metadata.get(MULTIPLIER_ASSUMED) in {"user_default", "platform_default"}

    def with_underlying(self, underlying_id: uuid.UUID) -> Instrument:
        return replace(self, underlying_id=underlying_id)


def make_instrument(
    *,
    asset_class: AssetClass,
    exchange: str,
    symbol: str,
    currency: str,
    multiplier: Decimal = Decimal(1),
    tick_size: Decimal = Decimal("0.01"),
    lot_size: Decimal = Decimal(1),
    expiry: date | None = None,
    strike: Decimal | None = None,
    option_type: OptionType | None = None,
    exercise_style: ExerciseStyle | None = None,
    settlement_type: SettlementType | None = None,
    underlying_id: uuid.UUID | None = None,
    venue: str | None = None,
    status: InstrumentStatus = InstrumentStatus.ACTIVE,
    metadata: dict | None = None,
) -> Instrument:
    """Build an :class:`Instrument`, deriving the canonical key and id."""
    canonical_key = build_canonical_key(
        exchange=exchange,
        asset_class=asset_class,
        symbol=symbol,
        expiry=expiry,
        strike=strike,
        option_type=option_type,
    )
    return Instrument(
        canonical_key=canonical_key,
        asset_class=asset_class,
        exchange=exchange,
        symbol=symbol,
        currency=currency,
        multiplier=multiplier,
        tick_size=tick_size,
        lot_size=lot_size,
        expiry=expiry,
        strike=strike,
        option_type=option_type,
        exercise_style=exercise_style,
        settlement_type=settlement_type,
        underlying_id=underlying_id,
        venue=venue,
        status=status,
        metadata=dict(metadata or {}),
    )
