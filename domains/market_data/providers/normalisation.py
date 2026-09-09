"""Turning a provider's payload into canonical schemas, verifiably.

A provider's response shape is the provider's business, and it changes without
telling us. The dangerous failure is not a crash — it is a payload whose fields
have moved, read by code that quietly produces a ``Quote`` full of ``None`` and
a last price of nothing. Every downstream calculation then reports an absence
that looks like a quiet market rather than a broken integration.

So the mapping lives here as **data**, and reading it produces an account of
what happened:

* which canonical field came from which path in the payload,
* which mapped paths were **not present** in the payload,
* which keys the payload carried that we mapped nowhere.

The last two are the alarm. A provider that renames ``last_price`` shows up as
one missing field and one unmapped key, on the first response, rather than as
market data that slowly stops arriving.

Nothing here supplies a value the payload did not carry. A field that is absent
stays ``None``; it is never defaulted to zero, to the previous value, or to
another field that happens to look similar.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

#: Separator between path segments. Integer segments index into a list, so
#: ``depth.buy.0.price`` reaches the top of the bid side.
PATH_SEPARATOR = "."


class NormalisationError(Exception):
    """A payload could not be read as the schema it was supposed to be."""


def resolve_path(payload: Any, path: str) -> tuple[bool, Any]:
    """Walk a dotted path. Returns ``(found, value)``.

    ``found`` is False when any segment is missing, which is different from a
    segment being present and null — a provider that explicitly sends
    ``"oi": null`` is telling us something a missing key does not.
    """
    current = payload
    for segment in path.split(PATH_SEPARATOR):
        if isinstance(current, Mapping):
            if segment not in current:
                return False, None
            current = current[segment]
        elif isinstance(current, Sequence) and not isinstance(current, str | bytes):
            try:
                index = int(segment)
            except ValueError:
                return False, None
            if index >= len(current) or index < -len(current):
                return False, None
            current = current[index]
        else:
            return False, None
    return True, current


def iter_leaf_paths(payload: Any, prefix: str = "") -> Iterator[str]:
    """Every leaf path in a payload, for the unmapped-key report."""
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            yield from iter_leaf_paths(value, f"{prefix}{PATH_SEPARATOR}{key}" if prefix else key)
    elif isinstance(payload, Sequence) and not isinstance(payload, str | bytes):
        for index, value in enumerate(payload):
            yield from iter_leaf_paths(
                value, f"{prefix}{PATH_SEPARATOR}{index}" if prefix else str(index)
            )
    elif prefix:
        yield prefix


def to_decimal(value: Any) -> Decimal | None:
    """Exact conversion, or ``None``. Never a partial or rounded reading.

    Floats go through ``str`` because the platform's premise is that prices are
    exact; ``Decimal(24000.05)`` would carry the binary representation error
    into every downstream number.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, str):
        token = value.strip().replace(",", "")
        if not token:
            return None
        try:
            return Decimal(token)
        except InvalidOperation:
            return None
    return None


def to_timestamp(value: Any) -> datetime | None:
    """Read a provider timestamp without assuming a zone it did not state.

    Epoch numbers are unambiguous. An ISO string with an offset is unambiguous.
    An ISO string *without* an offset is not, and is refused rather than being
    read as UTC — mislabelling an exchange's local time as UTC shifts every
    staleness measurement by hours and looks like a healthy feed.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else None
    if isinstance(value, int | float):
        seconds = float(value)
        # Feeds publish seconds or milliseconds; the magnitude separates them
        # unambiguously for any date this platform will ever see.
        if seconds > 1e11:
            seconds /= 1000.0
        try:
            return datetime.fromtimestamp(seconds, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        token = value.strip()
        if not token:
            return None
        if token.isdigit():
            return to_timestamp(int(token))
        try:
            parsed = datetime.fromisoformat(token.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else None
    return None


#: How a canonical field's raw value is converted. Anything not listed is
#: carried through unchanged.
CONVERTERS = {
    "exchange_timestamp": to_timestamp,
    "last_trade_time": to_timestamp,
    "sequence_number": lambda value: int(value) if isinstance(value, int | float) else None,
}


@dataclass(frozen=True, slots=True)
class NormalisationSpec:
    """Canonical field -> path in the provider payload.

    A named, versioned object rather than a dict literal in the middle of a
    provider, because this is the thing that has to be checked against a
    provider's documentation and the thing that has to change when they alter a
    response.
    """

    name: str
    fields: Mapping[str, str]
    #: Fields whose absence is ordinary rather than a signal. An equity has no
    #: open interest and not every venue stamps a last-trade time; reporting
    #: those as missing on every healthy response would make the missing-field
    #: report noise, and a report nobody reads catches nothing.
    optional: frozenset[str] = frozenset()
    #: Paths that exist in the payload and are deliberately not mapped. Listing
    #: them keeps the unmapped-key report meaningful: without it the report is
    #: noise and nobody reads it, which is how a renamed field goes unnoticed.
    ignored_prefixes: tuple[str, ...] = ()
    #: Free text recording where this mapping came from and how sure we are of
    #: it. Surfaced in provenance, because a normalisation nobody has verified
    #: against live data is an assumption and should read like one.
    provenance: str = "unverified"

    def with_overrides(self, overrides: Mapping[str, str]) -> NormalisationSpec:
        """Apply operator overrides. Used when a provider changes a response
        and the deployment must not wait for a release."""
        if not overrides:
            return self
        merged = dict(self.fields)
        merged.update(overrides)
        return NormalisationSpec(
            name=f"{self.name}+overrides",
            fields=merged,
            optional=self.optional,
            ignored_prefixes=self.ignored_prefixes,
            provenance=f"{self.provenance}; overridden by configuration",
        )


@dataclass(frozen=True, slots=True)
class NormalisationOutcome:
    """What the mapping actually managed to read."""

    values: dict[str, Any] = field(default_factory=dict)
    #: canonical field -> the path it was read from.
    matched: dict[str, str] = field(default_factory=dict)
    #: Mapped fields whose path was absent from this payload.
    missing: tuple[str, ...] = ()
    #: Paths present in the payload that no mapping claims.
    unmapped: tuple[str, ...] = ()
    spec_name: str = ""
    spec_provenance: str = ""

    @property
    def looks_like_a_schema_change(self) -> bool:
        """A payload that has fields we do not read *and* lacks fields we do.

        Either alone is ordinary — providers send extras, and optional fields
        are optional. Both together is the signature of a renamed field, which
        is the failure this module exists to make visible.
        """
        return bool(self.missing) and bool(self.unmapped)

    def to_provenance(self) -> dict:
        return {
            "normalisation_spec": self.spec_name,
            "normalisation_provenance": self.spec_provenance,
            "fields_read": dict(sorted(self.matched.items())),
            "fields_missing": list(self.missing),
            "fields_unmapped": list(self.unmapped),
        }


def normalise(payload: Mapping[str, Any], spec: NormalisationSpec) -> NormalisationOutcome:
    """Read a payload through a spec, and report what happened."""
    values: dict[str, Any] = {}
    matched: dict[str, str] = {}
    missing: list[str] = []

    for canonical_field, path in spec.fields.items():
        found, raw = resolve_path(payload, path)
        if not found:
            if canonical_field not in spec.optional:
                missing.append(canonical_field)
            continue
        matched[canonical_field] = path
        converter = CONVERTERS.get(canonical_field)
        values[canonical_field] = converter(raw) if converter is not None else raw

    claimed = set(spec.fields.values())
    unmapped = tuple(
        sorted(
            leaf
            for leaf in iter_leaf_paths(payload)
            if leaf not in claimed
            and not any(leaf.startswith(prefix) for prefix in spec.ignored_prefixes)
        )
    )

    return NormalisationOutcome(
        values=values,
        matched=matched,
        missing=tuple(sorted(missing)),
        unmapped=unmapped,
        spec_name=spec.name,
        spec_provenance=spec.provenance,
    )
