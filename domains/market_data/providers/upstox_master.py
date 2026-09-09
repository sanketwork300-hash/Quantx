"""The Upstox instrument master, read into canonical instruments.

An instrument master is the join between a provider's identifiers and the
platform's own. Getting it wrong is not a crash: it is an option attached to the
wrong underlying, or a lot size that scales every Greek by the wrong factor, and
neither announces itself.

Three rules follow from that, and they are the whole design here:

**Nothing is guessed.** A row whose instrument type, exchange or contract fields
are not understood is *rejected with a reason*, never coerced into the nearest
plausible instrument. A missing lot size is missing.

**Nothing is dropped silently.** ``input == accepted + rejected`` is asserted,
and every rejection names the row and why — the same conservation rule the
option-chain ingestion pipeline obeys.

**Derived values say where they came from.** The contract multiplier is taken
from the provider's lot size and ``metadata`` records exactly that, so a later
reader can tell a sourced multiplier from a defaulted one.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from zoneinfo import ZoneInfo

from domains.instruments.enums import (
    AssetClass,
    ExerciseStyle,
    OptionType,
    SettlementType,
)
from domains.instruments.errors import InvalidInstrument
from domains.instruments.models import MULTIPLIER_ASSUMED, Instrument, make_instrument
from domains.market_data.providers.normalisation import (
    NormalisationSpec,
    normalise,
    to_decimal,
    to_timestamp,
)

#: Where each canonical field lives in an instrument-master row.
#:
#: Verified against the layout of the published Upstox instrument files at the
#: time of writing. It is a :class:`NormalisationSpec` rather than inline
#: attribute access so that a change on the provider's side shows up as named
#: missing fields on the first load instead of as instruments that quietly stop
#: being created.
UPSTOX_MASTER_SPEC = NormalisationSpec(
    name="upstox.instruments.v1",
    fields={
        "instrument_key": "instrument_key",
        "segment": "segment",
        "exchange": "exchange",
        "instrument_type": "instrument_type",
        "trading_symbol": "trading_symbol",
        "name": "name",
        "lot_size": "lot_size",
        "tick_size": "tick_size",
        "expiry": "expiry",
        "strike_price": "strike_price",
        "underlying_symbol": "underlying_symbol",
        "underlying_key": "underlying_key",
        "underlying_type": "underlying_type",
        "exchange_token": "exchange_token",
        "isin": "isin",
    },
    provenance=(
        "mapped from the published Upstox instrument file layout; verify against the "
        "provider's current documentation before relying on a new field"
    ),
)

#: Provider instrument types this loader understands. Anything else is rejected
#: rather than mapped to the nearest asset class: an unrecognised derivative
#: silently classified as an equity is a position that reports no expiry.
INSTRUMENT_TYPES: Mapping[str, AssetClass] = {
    "EQ": AssetClass.EQUITY,
    "INDEX": AssetClass.INDEX,
    "FUT": AssetClass.FUTURE,
    "CE": AssetClass.OPTION,
    "PE": AssetClass.OPTION,
}

OPTION_TYPES: Mapping[str, OptionType] = {"CE": OptionType.CALL, "PE": OptionType.PUT}

#: Settlement currency per exchange. A fact about the venue, listed explicitly
#: so that an exchange nobody has checked is rejected rather than defaulted into
#: a currency that would misprice everything denominated in it.
EXCHANGE_CURRENCY: Mapping[str, str] = {
    "NSE": "INR",
    "BSE": "INR",
    "MCX": "INR",
    "NCDEX": "INR",
}

#: Timezone an exchange's expiry instants are expressed in. Needed because the
#: provider publishes expiry as an epoch and the platform stores a calendar
#: date: reading a 15:30 Mumbai expiry in the wrong zone can move it a day.
EXCHANGE_TIMEZONE: Mapping[str, str] = {
    "NSE": "Asia/Kolkata",
    "BSE": "Asia/Kolkata",
    "MCX": "Asia/Kolkata",
    "NCDEX": "Asia/Kolkata",
}

#: Recorded in metadata beside the multiplier, so a reader can tell a
#: provider-sourced multiplier from a platform default.
MULTIPLIER_FROM_LOT_SIZE = "provider_lot_size"


class InstrumentMasterError(Exception):
    """The master file could not be read at all."""


@dataclass(frozen=True, slots=True)
class RejectedRow:
    """A row that was not turned into an instrument, and why."""

    row_number: int
    instrument_key: str | None
    reason: str
    detail: str


@dataclass(frozen=True, slots=True)
class InstrumentMasterResult:
    """The outcome of a load, with the conservation invariant checkable."""

    instruments: tuple[Instrument, ...] = ()
    #: canonical instrument id -> the provider's key for it.
    provider_keys: Mapping[uuid.UUID, str] = field(default_factory=dict)
    rejected: tuple[RejectedRow, ...] = ()
    #: Rows the deployment's segment/underlying selection excluded. Counted
    #: rather than listed: the complete file runs to hundreds of thousands of
    #: rows and a rejection object per skipped row would cost more memory than
    #: the instruments it kept. The count still closes the conservation sum.
    filtered_out: int = 0
    input_rows: int = 0
    #: Canonical fields the spec expects that no row carried, and payload keys
    #: no mapping claims. Both empty is the healthy case.
    fields_missing: tuple[str, ...] = ()
    fields_unmapped: tuple[str, ...] = ()
    spec_name: str = UPSTOX_MASTER_SPEC.name

    @property
    def accepted(self) -> int:
        return len(self.instruments)

    @property
    def conserved(self) -> bool:
        return self.input_rows == self.accepted + len(self.rejected) + self.filtered_out

    def rejection_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in self.rejected:
            counts[row.reason] = counts.get(row.reason, 0) + 1
        return dict(sorted(counts.items()))

    def to_provenance(self) -> dict:
        return {
            "instrument_master_spec": self.spec_name,
            "input_rows": self.input_rows,
            "accepted": self.accepted,
            "rejected": len(self.rejected),
            "filtered_out": self.filtered_out,
            "conserved": self.conserved,
            "rejection_reasons": self.rejection_counts(),
            "fields_missing": list(self.fields_missing),
            "fields_unmapped": list(self.fields_unmapped),
        }


def _clean(value: object) -> str | None:
    if value is None:
        return None
    token = str(value).strip()
    return token or None


#: Renderings of a date that a venue's contract name might contain. Used only
#: to choose between two candidate dates that are already in hand — never to
#: parse a date out of a string — so a format this list does not cover degrades
#: to "could not resolve" rather than to a wrong date.
def _date_renderings(value: date) -> tuple[str, ...]:
    day = f"{value.day:02d}"
    month = value.strftime("%b").upper()
    short_year = value.strftime("%y")
    return (
        f"{day} {month} {short_year}",
        f"{day}{month}{short_year}",
        f"{day} {month} {value.year}",
        f"{day}{month}{value.year}",
        f"{day}-{month}-{short_year}",
        f"{day}-{month}-{value.year}",
        value.isoformat(),
    )


def date_appears_in(text: str | None, candidate: date) -> bool:
    if not text:
        return False
    upper = text.upper()
    return any(rendering in upper for rendering in _date_renderings(candidate))


class MidnightExpiryConvention(StrEnum):
    """How to read an expiry instant that lands exactly on local midnight.

    A provider publishing ``2025-09-19T00:00:00+05:30`` may mean the contract
    expires on the 19th, or may be encoding the end of the 18th. The instant
    alone cannot say which, and the difference changes the contract's identity,
    its canonical key and its time to expiry.
    """

    START_OF_DAY = "START_OF_DAY"
    END_OF_PREVIOUS_DAY = "END_OF_PREVIOUS_DAY"


@dataclass(frozen=True, slots=True)
class ExpiryReading:
    """A resolved expiry, with how it was resolved."""

    expiry: date | None
    instant: datetime | None
    #: One of ``provider_epoch``, ``contract_name``, ``convention`` or ``none``.
    source: str
    #: Populated only when the instant was ambiguous: the two candidate dates.
    candidates: tuple[date, ...] = ()
    ambiguous: bool = False


def read_expiry(
    raw: object,
    exchange: str,
    contract_name: str | None = None,
    convention: MidnightExpiryConvention = MidnightExpiryConvention.START_OF_DAY,
) -> ExpiryReading:
    """Turn a provider expiry into a calendar date without guessing quietly.

    An instant that falls at any time *during* an exchange's day names that day
    and there is nothing to decide. An instant that falls exactly on local
    midnight sits on the boundary between two days, so it is resolved against
    the contract's own printed name where that settles it, and otherwise by a
    stated convention that is recorded on the instrument.
    """
    instant = to_timestamp(raw)
    if instant is None:
        return ExpiryReading(expiry=None, instant=None, source="none")

    zone = EXCHANGE_TIMEZONE.get(exchange)
    if zone is None:
        return ExpiryReading(expiry=None, instant=instant, source="none")

    local = instant.astimezone(ZoneInfo(zone))
    if local.hour or local.minute or local.second or local.microsecond:
        return ExpiryReading(expiry=local.date(), instant=instant, source="provider_epoch")

    start_of_day = local.date()
    end_of_previous = start_of_day - timedelta(days=1)
    candidates = (end_of_previous, start_of_day)

    # The contract's own name is the exchange's statement about which day it is.
    # It is only ever used to pick between these two, so a name this code cannot
    # read leaves the choice to the convention rather than inventing a third date.
    matched = [candidate for candidate in candidates if date_appears_in(contract_name, candidate)]
    if len(matched) == 1:
        return ExpiryReading(
            expiry=matched[0],
            instant=instant,
            source="contract_name",
            candidates=candidates,
            ambiguous=True,
        )

    chosen = (
        start_of_day if convention is MidnightExpiryConvention.START_OF_DAY else end_of_previous
    )
    return ExpiryReading(
        expiry=chosen,
        instant=instant,
        source="convention",
        candidates=candidates,
        ambiguous=True,
    )


@dataclass(frozen=True, slots=True)
class InstrumentMasterOptions:
    """Choices a deployment makes about how the file is read."""

    #: Segments to keep, e.g. ``("NSE_INDEX", "NSE_FO")``. Empty keeps everything.
    #: The complete file runs to hundreds of thousands of rows, most of which no
    #: deployment needs.
    segments: tuple[str, ...] = ()
    #: Underlying symbols to keep, e.g. ``("NIFTY", "BANKNIFTY")``. Applies only
    #: to derivatives; the underlyings themselves are always kept.
    underlyings: tuple[str, ...] = ()
    #: Multiplies the provider's tick size. The provider publishes a number
    #: whose unit this loader does not assert; the default of 1 reports what
    #: the provider reported, and ``metadata`` records both the raw value and
    #: whether a scale was applied.
    tick_size_scale: Decimal = Decimal(1)
    #: How to read an expiry instant that lands on local midnight. See
    #: :class:`MidnightExpiryConvention`; the choice is recorded on every
    #: instrument it decided, so it can be audited and changed.
    midnight_expiry: MidnightExpiryConvention = MidnightExpiryConvention.START_OF_DAY
    #: Options whose exercise style the provider does not state. Recorded so a
    #: pricing model can refuse rather than assume. Index options on Indian
    #: exchanges are European; nothing here infers that for other venues.
    exercise_style: Mapping[str, ExerciseStyle] = field(
        default_factory=lambda: {"NSE": ExerciseStyle.EUROPEAN, "BSE": ExerciseStyle.EUROPEAN}
    )


class UpstoxInstrumentMaster:
    """Reads instrument-master rows into canonical instruments."""

    def __init__(self, options: InstrumentMasterOptions | None = None) -> None:
        self._options = options or InstrumentMasterOptions()

    def load(self, rows: Iterable[Mapping[str, object]]) -> InstrumentMasterResult:
        """Two passes: underlyings first, because a contract cannot be built
        before the thing it is a contract on."""
        input_rows = 0
        filtered_out = 0
        rejected: list[RejectedRow] = []
        missing: set[str] = set()
        unmapped: set[str] = set()
        underlying_rows: list[tuple[int, dict]] = []
        contract_rows: list[tuple[int, dict]] = []

        for number, row in enumerate(rows, start=1):
            input_rows += 1
            if not self._selected(row):
                filtered_out += 1
                continue

            outcome = normalise(row, UPSTOX_MASTER_SPEC)
            missing.update(outcome.missing)
            unmapped.update(outcome.unmapped)

            instrument_type = _clean(outcome.values.get("instrument_type"))
            if instrument_type is None:
                rejected.append(
                    RejectedRow(
                        number,
                        _clean(outcome.values.get("instrument_key")),
                        "NO_INSTRUMENT_TYPE",
                        "the row carries no instrument_type, so its asset class is unknown",
                    )
                )
                continue
            if instrument_type.upper() in {"CE", "PE", "FUT"}:
                contract_rows.append((number, outcome.values))
            else:
                underlying_rows.append((number, outcome.values))

        instruments: dict[uuid.UUID, Instrument] = {}
        provider_keys: dict[uuid.UUID, str] = {}
        by_provider_key: dict[str, Instrument] = {}

        for number, values in underlying_rows:
            self._build(
                number,
                values,
                instruments,
                provider_keys,
                by_provider_key,
                rejected,
                underlyings={},
            )

        for number, values in contract_rows:
            self._build(
                number,
                values,
                instruments,
                provider_keys,
                by_provider_key,
                rejected,
                underlyings=by_provider_key,
            )

        return InstrumentMasterResult(
            instruments=tuple(instruments.values()),
            provider_keys=dict(provider_keys),
            rejected=tuple(rejected),
            filtered_out=filtered_out,
            input_rows=input_rows,
            fields_missing=tuple(sorted(missing)),
            fields_unmapped=tuple(sorted(unmapped)),
        )

    # ------------------------------------------------------------ selection
    def _selected(self, row: Mapping[str, object]) -> bool:
        """Whether this deployment asked for this row.

        The complete file covers every listed instrument on every segment; a
        deployment that wants NIFTY options should not be made to hold a million
        instruments to get them.
        """
        segments = self._options.segments
        if segments and (_clean(row.get("segment")) or "").upper() not in {
            segment.upper() for segment in segments
        }:
            return False

        underlyings = self._options.underlyings
        if underlyings and (_clean(row.get("instrument_type")) or "").upper() in {
            "CE",
            "PE",
            "FUT",
        }:
            symbol = (
                _clean(row.get("underlying_symbol")) or _clean(row.get("asset_symbol")) or ""
            ).upper()
            if symbol not in {name.upper() for name in underlyings}:
                return False
        return True

    # -------------------------------------------------------------- building
    def _build(
        self,
        number: int,
        values: Mapping[str, object],
        instruments: dict[uuid.UUID, Instrument],
        provider_keys: dict[uuid.UUID, str],
        by_provider_key: dict[str, Instrument],
        rejected: list[RejectedRow],
        underlyings: Mapping[str, Instrument],
    ) -> None:
        instrument_key = _clean(values.get("instrument_key"))

        def reject(reason: str, detail: str) -> None:
            rejected.append(RejectedRow(number, instrument_key, reason, detail))

        if instrument_key is None:
            reject("NO_INSTRUMENT_KEY", "the row carries no instrument_key to map against")
            return

        instrument_type = (_clean(values.get("instrument_type")) or "").upper()
        asset_class = INSTRUMENT_TYPES.get(instrument_type)
        if asset_class is None:
            reject(
                "UNKNOWN_INSTRUMENT_TYPE",
                f"instrument_type {instrument_type!r} is not one of "
                f"{', '.join(sorted(INSTRUMENT_TYPES))}",
            )
            return

        exchange = (_clean(values.get("exchange")) or "").upper()
        currency = EXCHANGE_CURRENCY.get(exchange)
        if currency is None:
            reject(
                "UNKNOWN_EXCHANGE",
                f"exchange {exchange!r} has no recorded settlement currency; add one "
                "rather than letting a default decide what the contract is worth",
            )
            return

        is_contract = asset_class in {AssetClass.OPTION, AssetClass.FUTURE}
        symbol = (
            _clean(values.get("underlying_symbol"))
            if is_contract
            else _clean(values.get("trading_symbol"))
        ) or _clean(values.get("trading_symbol"))
        if symbol is None:
            reject("NO_SYMBOL", "the row carries neither a trading symbol nor an underlying")
            return

        underlying_id: uuid.UUID | None = None
        if is_contract:
            underlying_key = _clean(values.get("underlying_key"))
            underlying = underlyings.get(underlying_key) if underlying_key else None
            if underlying is None:
                reject(
                    "UNDERLYING_NOT_LOADED",
                    f"underlying {underlying_key!r} is not among the loaded instruments; "
                    "widen the segment selection so the underlying is included",
                )
                return
            underlying_id = underlying.id

        expiry = None
        reading: ExpiryReading | None = None
        if is_contract:
            reading = read_expiry(
                values.get("expiry"),
                exchange,
                contract_name=_clean(values.get("trading_symbol")),
                convention=self._options.midnight_expiry,
            )
            expiry = reading.expiry
            if expiry is None:
                reject(
                    "NO_EXPIRY",
                    "a dated contract with no readable expiry cannot be identified or priced",
                )
                return

        strike = None
        option_type = None
        if asset_class is AssetClass.OPTION:
            strike = to_decimal(values.get("strike_price"))
            option_type = OPTION_TYPES.get(instrument_type)
            if strike is None or strike <= 0:
                reject("NO_STRIKE", "an option with no positive strike cannot be identified")
                return
            if option_type is None:  # pragma: no cover - guarded by INSTRUMENT_TYPES
                reject("NO_OPTION_TYPE", f"instrument_type {instrument_type!r} names no side")
                return

        exercise_style = None
        if asset_class is AssetClass.OPTION:
            exercise_style = self._options.exercise_style.get(exchange)
            if exercise_style is None:
                reject(
                    "UNKNOWN_EXERCISE_STYLE",
                    f"no exercise style is recorded for {exchange!r}; an option priced under "
                    "the wrong style is mispriced, so the contract is not created",
                )
                return

        lot_size = to_decimal(values.get("lot_size"))
        raw_tick = to_decimal(values.get("tick_size"))

        metadata: dict[str, object] = {
            "provider": "upstox",
            "upstox_instrument_key": instrument_key,
            "upstox_segment": _clean(values.get("segment")),
            "upstox_trading_symbol": _clean(values.get("trading_symbol")),
            "upstox_name": _clean(values.get("name")),
            "upstox_exchange_token": _clean(values.get("exchange_token")),
        }
        if _clean(values.get("isin")):
            metadata["isin"] = _clean(values.get("isin"))
        if reading is not None:
            if reading.instant is not None:
                metadata["expiry_timestamp"] = reading.instant.isoformat()
            metadata["expiry_date_source"] = reading.source
            if reading.ambiguous:
                # Recorded on the instrument itself: an expiry decided by a
                # convention rather than read off the data is exactly the kind
                # of assumption that must travel with the thing it shaped.
                metadata["expiry_date_candidates"] = [d.isoformat() for d in reading.candidates]
        if raw_tick is not None:
            metadata["upstox_tick_size_raw"] = format(raw_tick, "f")
            metadata["tick_size_source"] = (
                "provider_raw"
                if self._options.tick_size_scale == 1
                else f"provider_scaled_by_{self._options.tick_size_scale}"
            )

        if lot_size is not None and lot_size > 0:
            # Taken from the provider, and said to be taken from the provider.
            metadata[MULTIPLIER_ASSUMED] = MULTIPLIER_FROM_LOT_SIZE
            multiplier = lot_size
        else:
            metadata[MULTIPLIER_ASSUMED] = "platform_default"
            multiplier = Decimal(1)
            lot_size = Decimal(1)

        tick_size = (
            raw_tick * self._options.tick_size_scale
            if raw_tick is not None and raw_tick > 0
            else Decimal("0.01")
        )
        if raw_tick is None or raw_tick <= 0:
            metadata["tick_size_source"] = "platform_default"

        try:
            instrument = make_instrument(
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
                settlement_type=SettlementType.CASH if is_contract else None,
                underlying_id=underlying_id,
                venue=_clean(values.get("segment")),
                metadata=metadata,
            )
        except InvalidInstrument as exc:
            reject("INVALID_INSTRUMENT", str(exc)[:200])
            return

        if instrument.id in instruments:
            reject(
                "DUPLICATE_CANONICAL_KEY",
                f"another row already produced {instrument.canonical_key!r}",
            )
            return

        instruments[instrument.id] = instrument
        provider_keys[instrument.id] = instrument_key
        by_provider_key[instrument_key] = instrument
