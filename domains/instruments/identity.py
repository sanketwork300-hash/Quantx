"""Canonical instrument identity.

The same contract arrives as ``NIFTY26SEP24000CE`` from one broker and
``NIFTY 24SEP2026 24000 CE`` from another. If those become two instruments,
portfolio netting, Greeks aggregation, margin and TCA are all silently wrong and
nothing raises. So identity is defined once, canonically, and every provider
adapter maps into it.

Ids are ``uuid5`` of the canonical key, which makes them deterministic across
processes, environments and databases. That is what lets a provenance record
written six months ago still name the same contract today.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from domains.instruments.enums import AssetClass, OptionType
from domains.instruments.errors import CanonicalKeyError

#: Fixed namespace for instrument ids. Changing this is a breaking data
#: migration: every stored instrument id and every provenance reference to one
#: would change meaning.
INSTRUMENT_NAMESPACE = uuid.UUID("6f6b9d1e-6a1e-5b6e-9a3d-0c2f1b7a4e10")

SEPARATOR = ":"


def normalize_symbol(symbol: str) -> str:
    token = symbol.strip().upper()
    if not token:
        raise CanonicalKeyError("symbol must not be empty")
    if SEPARATOR in token:
        raise CanonicalKeyError(f"symbol must not contain {SEPARATOR!r}: {symbol!r}")
    return token


def normalize_exchange(exchange: str) -> str:
    token = exchange.strip().upper()
    if not token:
        raise CanonicalKeyError("exchange must not be empty")
    if SEPARATOR in token:
        raise CanonicalKeyError(f"exchange must not contain {SEPARATOR!r}: {exchange!r}")
    return token


def format_strike(strike: Decimal) -> str:
    """Render a strike without trailing zeros or scientific notation.

    ``24000``, ``24000.00`` and ``2.4E+4`` are the same strike and must produce
    the same canonical key, or the same contract acquires several identities.
    """
    if not isinstance(strike, Decimal):
        strike = Decimal(str(strike))
    normalized = strike.normalize()
    # normalize() turns 24000 into 2.4E+4; 'f' formatting undoes that.
    return format(normalized, "f")


def parse_strike(value: str | Decimal | int | float) -> Decimal:
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise CanonicalKeyError(f"unparseable strike: {value!r}") from exc


@dataclass(frozen=True, slots=True)
class CanonicalKeyParts:
    exchange: str
    asset_class: AssetClass
    symbol: str
    expiry: date | None = None
    strike: Decimal | None = None
    option_type: OptionType | None = None


def build_canonical_key(
    *,
    exchange: str,
    asset_class: AssetClass,
    symbol: str,
    expiry: date | None = None,
    strike: Decimal | None = None,
    option_type: OptionType | None = None,
) -> str:
    parts = [normalize_exchange(exchange), str(asset_class), normalize_symbol(symbol)]

    if asset_class is AssetClass.OPTION:
        if expiry is None or strike is None or option_type is None:
            raise CanonicalKeyError("OPTION canonical key requires expiry, strike and option_type")
        parts += [expiry.isoformat(), format_strike(strike), option_type.code]
    elif asset_class is AssetClass.FUTURE:
        if expiry is None:
            raise CanonicalKeyError("FUTURE canonical key requires expiry")
        if strike is not None or option_type is not None:
            raise CanonicalKeyError("FUTURE canonical key must not carry strike/option_type")
        parts.append(expiry.isoformat())
    else:
        if expiry is not None or strike is not None or option_type is not None:
            raise CanonicalKeyError(
                f"{asset_class} canonical key must not carry expiry/strike/option_type"
            )

    return SEPARATOR.join(parts)


def parse_canonical_key(key: str) -> CanonicalKeyParts:
    """Inverse of :func:`build_canonical_key`.

    ``parse(build(x)) == x`` is a property test, because a key format that does
    not round-trip is a key format that will eventually be parsed wrongly.
    """
    tokens = key.split(SEPARATOR)
    if len(tokens) < 3:
        raise CanonicalKeyError(f"canonical key needs at least 3 segments: {key!r}")

    exchange, asset_class_token, symbol, *rest = tokens
    try:
        asset_class = AssetClass(asset_class_token)
    except ValueError as exc:
        raise CanonicalKeyError(f"unknown asset class in key: {asset_class_token!r}") from exc

    if asset_class is AssetClass.OPTION:
        if len(rest) != 3:
            raise CanonicalKeyError(f"OPTION key needs expiry:strike:type, got {key!r}")
        expiry_token, strike_token, type_token = rest
        return CanonicalKeyParts(
            exchange=exchange,
            asset_class=asset_class,
            symbol=symbol,
            expiry=date.fromisoformat(expiry_token),
            strike=parse_strike(strike_token),
            option_type=OptionType.parse(type_token),
        )

    if asset_class is AssetClass.FUTURE:
        if len(rest) != 1:
            raise CanonicalKeyError(f"FUTURE key needs an expiry segment, got {key!r}")
        return CanonicalKeyParts(
            exchange=exchange,
            asset_class=asset_class,
            symbol=symbol,
            expiry=date.fromisoformat(rest[0]),
        )

    if rest:
        raise CanonicalKeyError(f"{asset_class} key must have exactly 3 segments, got {key!r}")
    return CanonicalKeyParts(exchange=exchange, asset_class=asset_class, symbol=symbol)


def instrument_id_for(canonical_key: str) -> uuid.UUID:
    return uuid.uuid5(INSTRUMENT_NAMESPACE, canonical_key)
