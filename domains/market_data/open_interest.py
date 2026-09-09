"""Open interest and volume, aggregated across an option chain.

What this module does is arithmetic: sum the open interest on the calls, sum it
on the puts, divide. What it deliberately does **not** do is say what the answer
means. A put-call ratio is widely read as a sentiment indicator, usually a
contrarian one, on evidence that is thin and regime-dependent. This platform
reports the ratio and the counts it was built from; the reading is the user's.

Three measurement problems are handled explicitly rather than papered over.

**The unit of open interest is the venue's, not ours.** Some exchanges publish
open interest in contracts, some in units of the underlying, and the platform
does not know which without being told. Ratios are therefore safe — the unit
cancels — and absolute totals carry a label saying the unit is as-reported and
unnormalised. Nothing here converts one into the other.

**A missing figure is not a zero.** A contract whose open interest the feed did
not carry is counted as *missing*, not as zero open interest, because summing
absences as zeros silently understates a total and moves a ratio.

**Change requires two observations.** It is computed only between two snapshots,
only for contracts present in both, and always reported with both timestamps and
the window between them — so a figure can never be read as "today's change" when
it is the change over eleven minutes.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

from domains.instruments.enums import OptionType

#: Recorded beside every absolute total. The platform is not told whether a
#: venue publishes open interest in contracts or in units of the underlying, and
#: guessing would scale every total by the lot size.
OPEN_INTEREST_UNIT = "PROVIDER_REPORTED_UNNORMALISED"

OPEN_INTEREST_MODEL_VERSION = "open-interest@1.0.0"


def _ratio(numerator: Decimal | None, denominator: Decimal | None) -> float | None:
    """``numerator / denominator``, or ``None``.

    ``None`` for a zero or absent denominator rather than infinity or a
    substituted value: "there is no open interest on the calls" is a fact about
    the chain, and a ratio of infinity would plot as a spike.
    """
    if numerator is None or denominator is None or denominator == 0:
        return None
    return float(numerator / denominator)


def _add(total: Decimal | None, value: Decimal | None) -> Decimal | None:
    """Sum that keeps ``None`` distinct from zero until a real value arrives."""
    if value is None:
        return total
    return value if total is None else total + value


@dataclass(frozen=True, slots=True)
class ChainRow:
    """One observed contract, as this module needs it.

    A narrow input type rather than an ORM row, so the arithmetic is testable
    without a database and cannot accidentally depend on persistence details.
    """

    instrument_id: uuid.UUID
    expiry: date
    strike: Decimal
    option_type: OptionType
    open_interest: Decimal | None = None
    volume: Decimal | None = None
    excluded: bool = False


@dataclass(frozen=True, slots=True)
class StrikeOpenInterest:
    """Open interest and volume at one strike, both sides."""

    strike: Decimal
    call_open_interest: Decimal | None = None
    put_open_interest: Decimal | None = None
    call_volume: Decimal | None = None
    put_volume: Decimal | None = None

    @property
    def total_open_interest(self) -> Decimal | None:
        return _add(self.call_open_interest, self.put_open_interest)

    @property
    def put_call_ratio_open_interest(self) -> float | None:
        return _ratio(self.put_open_interest, self.call_open_interest)

    def to_dict(self) -> dict:
        return {
            "strike": format(self.strike, "f"),
            "call_open_interest": _out(self.call_open_interest),
            "put_open_interest": _out(self.put_open_interest),
            "total_open_interest": _out(self.total_open_interest),
            "call_volume": _out(self.call_volume),
            "put_volume": _out(self.put_volume),
            "put_call_ratio_open_interest": self.put_call_ratio_open_interest,
        }


@dataclass(frozen=True, slots=True)
class ExpiryOpenInterest:
    """One expiry's open interest and volume, and the ratios between them."""

    expiry: date
    call_open_interest: Decimal | None = None
    put_open_interest: Decimal | None = None
    call_volume: Decimal | None = None
    put_volume: Decimal | None = None
    strikes: tuple[StrikeOpenInterest, ...] = ()
    #: How much of the chain actually carried an open-interest figure. Reported
    #: because a total built from a third of the contracts is a different number
    #: from one built from all of them, and nothing else on the response says so.
    contracts: int = 0
    contracts_with_open_interest: int = 0

    @property
    def total_open_interest(self) -> Decimal | None:
        return _add(self.call_open_interest, self.put_open_interest)

    @property
    def total_volume(self) -> Decimal | None:
        return _add(self.call_volume, self.put_volume)

    @property
    def put_call_ratio_open_interest(self) -> float | None:
        return _ratio(self.put_open_interest, self.call_open_interest)

    @property
    def put_call_ratio_volume(self) -> float | None:
        return _ratio(self.put_volume, self.call_volume)

    @property
    def volume_to_open_interest(self) -> float | None:
        """Turnover against positions standing open. Unitless, so the venue's
        open-interest unit cancels here as it does in every other ratio."""
        return _ratio(self.total_volume, self.total_open_interest)

    @property
    def coverage(self) -> float | None:
        """Fraction of contracts that carried an open-interest figure."""
        if self.contracts == 0:
            return None
        return self.contracts_with_open_interest / self.contracts

    def most_open_interest(self, limit: int = 5) -> tuple[StrikeOpenInterest, ...]:
        """The strikes holding the most open interest.

        A description of where positions sit, and nothing more. It is not a
        forecast of where the underlying will settle, and this platform does not
        make one.
        """
        with_totals = [item for item in self.strikes if item.total_open_interest is not None]
        return tuple(
            sorted(with_totals, key=lambda item: item.total_open_interest, reverse=True)[:limit]
        )

    def to_dict(self, include_strikes: bool = True) -> dict:
        payload = {
            "expiry": self.expiry.isoformat(),
            "call_open_interest": _out(self.call_open_interest),
            "put_open_interest": _out(self.put_open_interest),
            "total_open_interest": _out(self.total_open_interest),
            "call_volume": _out(self.call_volume),
            "put_volume": _out(self.put_volume),
            "total_volume": _out(self.total_volume),
            "put_call_ratio_open_interest": self.put_call_ratio_open_interest,
            "put_call_ratio_volume": self.put_call_ratio_volume,
            "volume_to_open_interest": self.volume_to_open_interest,
            "contracts": self.contracts,
            "contracts_with_open_interest": self.contracts_with_open_interest,
            "coverage": self.coverage,
            "most_open_interest": [item.to_dict() for item in self.most_open_interest()],
        }
        if include_strikes:
            payload["strikes"] = [item.to_dict() for item in self.strikes]
        return payload


@dataclass(frozen=True, slots=True)
class OpenInterestProfile:
    """Open interest across a whole chain snapshot."""

    underlying_id: uuid.UUID
    snapshot_id: uuid.UUID | None
    as_of: datetime
    expiries: tuple[ExpiryOpenInterest, ...] = ()
    open_interest_unit: str = OPEN_INTEREST_UNIT
    model_version: str = OPEN_INTEREST_MODEL_VERSION
    #: Contracts the quality engine excluded. Left out of the sums, counted
    #: here, because a total that quietly includes quotes the platform refused
    #: to analyse disagrees with everything else computed from the snapshot.
    excluded_contracts: int = 0

    @property
    def call_open_interest(self) -> Decimal | None:
        total: Decimal | None = None
        for item in self.expiries:
            total = _add(total, item.call_open_interest)
        return total

    @property
    def put_open_interest(self) -> Decimal | None:
        total: Decimal | None = None
        for item in self.expiries:
            total = _add(total, item.put_open_interest)
        return total

    @property
    def put_call_ratio_open_interest(self) -> float | None:
        return _ratio(self.put_open_interest, self.call_open_interest)

    def for_expiry(self, expiry: date) -> ExpiryOpenInterest | None:
        return next((item for item in self.expiries if item.expiry == expiry), None)

    def to_dict(self, include_strikes: bool = False) -> dict:
        return {
            "underlying_id": str(self.underlying_id),
            "snapshot_id": str(self.snapshot_id) if self.snapshot_id else None,
            "as_of": self.as_of.isoformat(),
            "open_interest_unit": self.open_interest_unit,
            "model_version": self.model_version,
            "call_open_interest": _out(self.call_open_interest),
            "put_open_interest": _out(self.put_open_interest),
            "put_call_ratio_open_interest": self.put_call_ratio_open_interest,
            "excluded_contracts": self.excluded_contracts,
            "expiries": [item.to_dict(include_strikes) for item in self.expiries],
        }


def build_profile(
    underlying_id: uuid.UUID,
    as_of: datetime,
    rows: Sequence[ChainRow],
    snapshot_id: uuid.UUID | None = None,
    include_excluded: bool = False,
) -> OpenInterestProfile:
    """Aggregate observed contracts into an open-interest profile."""
    by_expiry: dict[date, dict] = {}
    excluded = 0

    for row in rows:
        if row.excluded and not include_excluded:
            excluded += 1
            continue

        bucket = by_expiry.setdefault(
            row.expiry,
            {
                "call_oi": None,
                "put_oi": None,
                "call_vol": None,
                "put_vol": None,
                "strikes": {},
                "contracts": 0,
                "with_oi": 0,
            },
        )
        bucket["contracts"] += 1
        if row.open_interest is not None:
            bucket["with_oi"] += 1

        strike = bucket["strikes"].setdefault(
            row.strike,
            {"call_oi": None, "put_oi": None, "call_vol": None, "put_vol": None},
        )
        side = "call" if row.option_type is OptionType.CALL else "put"
        bucket[f"{side}_oi"] = _add(bucket[f"{side}_oi"], row.open_interest)
        bucket[f"{side}_vol"] = _add(bucket[f"{side}_vol"], row.volume)
        strike[f"{side}_oi"] = _add(strike[f"{side}_oi"], row.open_interest)
        strike[f"{side}_vol"] = _add(strike[f"{side}_vol"], row.volume)

    expiries = tuple(
        ExpiryOpenInterest(
            expiry=expiry,
            call_open_interest=bucket["call_oi"],
            put_open_interest=bucket["put_oi"],
            call_volume=bucket["call_vol"],
            put_volume=bucket["put_vol"],
            contracts=bucket["contracts"],
            contracts_with_open_interest=bucket["with_oi"],
            strikes=tuple(
                StrikeOpenInterest(
                    strike=strike,
                    call_open_interest=values["call_oi"],
                    put_open_interest=values["put_oi"],
                    call_volume=values["call_vol"],
                    put_volume=values["put_vol"],
                )
                for strike, values in sorted(bucket["strikes"].items())
            ),
        )
        for expiry, bucket in sorted(by_expiry.items())
    )

    return OpenInterestProfile(
        underlying_id=underlying_id,
        snapshot_id=snapshot_id,
        as_of=as_of,
        expiries=expiries,
        excluded_contracts=excluded,
    )


@dataclass(frozen=True, slots=True)
class ContractOpenInterestChange:
    """How one contract's open interest moved between two observations."""

    instrument_id: uuid.UUID
    expiry: date
    strike: Decimal
    option_type: OptionType
    earlier_open_interest: Decimal | None
    later_open_interest: Decimal | None

    @property
    def change(self) -> Decimal | None:
        if self.earlier_open_interest is None or self.later_open_interest is None:
            return None
        return self.later_open_interest - self.earlier_open_interest

    def to_dict(self) -> dict:
        return {
            "instrument_id": str(self.instrument_id),
            "expiry": self.expiry.isoformat(),
            "strike": format(self.strike, "f"),
            "option_type": str(self.option_type),
            "earlier_open_interest": _out(self.earlier_open_interest),
            "later_open_interest": _out(self.later_open_interest),
            "change": _out(self.change),
        }


@dataclass(frozen=True, slots=True)
class OpenInterestChange:
    """The move between two snapshots, with the window it happened over."""

    underlying_id: uuid.UUID
    earlier_as_of: datetime
    later_as_of: datetime
    earlier_snapshot_id: uuid.UUID | None = None
    later_snapshot_id: uuid.UUID | None = None
    contracts: tuple[ContractOpenInterestChange, ...] = field(default_factory=tuple)
    #: Contracts that appear in only one of the two snapshots. A chain's listed
    #: strikes change as the underlying moves, so this is ordinary — but a
    #: change total that quietly dropped them would not add up.
    only_in_earlier: int = 0
    only_in_later: int = 0
    open_interest_unit: str = OPEN_INTEREST_UNIT

    @property
    def window_seconds(self) -> float:
        """How long the change happened over.

        Carried on every response. Without it a figure computed over eleven
        minutes reads exactly like one computed over a session.
        """
        return (self.later_as_of - self.earlier_as_of).total_seconds()

    @property
    def total_change(self) -> Decimal | None:
        total: Decimal | None = None
        for item in self.contracts:
            total = _add(total, item.change)
        return total

    def largest_increases(self, limit: int = 5) -> tuple[ContractOpenInterestChange, ...]:
        measured = [item for item in self.contracts if item.change is not None]
        return tuple(sorted(measured, key=lambda item: item.change, reverse=True)[:limit])

    def largest_decreases(self, limit: int = 5) -> tuple[ContractOpenInterestChange, ...]:
        measured = [item for item in self.contracts if item.change is not None]
        return tuple(sorted(measured, key=lambda item: item.change)[:limit])

    def to_dict(self, include_contracts: bool = False) -> dict:
        payload = {
            "underlying_id": str(self.underlying_id),
            "earlier_as_of": self.earlier_as_of.isoformat(),
            "later_as_of": self.later_as_of.isoformat(),
            "window_seconds": self.window_seconds,
            "earlier_snapshot_id": (
                str(self.earlier_snapshot_id) if self.earlier_snapshot_id else None
            ),
            "later_snapshot_id": str(self.later_snapshot_id) if self.later_snapshot_id else None,
            "open_interest_unit": self.open_interest_unit,
            "matched_contracts": len(self.contracts),
            "only_in_earlier": self.only_in_earlier,
            "only_in_later": self.only_in_later,
            "total_change": _out(self.total_change),
            "largest_increases": [item.to_dict() for item in self.largest_increases()],
            "largest_decreases": [item.to_dict() for item in self.largest_decreases()],
        }
        if include_contracts:
            payload["contracts"] = [item.to_dict() for item in self.contracts]
        return payload


def build_change(
    underlying_id: uuid.UUID,
    earlier_as_of: datetime,
    later_as_of: datetime,
    earlier: Sequence[ChainRow],
    later: Sequence[ChainRow],
    earlier_snapshot_id: uuid.UUID | None = None,
    later_snapshot_id: uuid.UUID | None = None,
) -> OpenInterestChange:
    """Match two snapshots by contract and report how open interest moved.

    Matching is on instrument id, which is derived from the contract's canonical
    key — so two snapshots agree about which contract is which by construction
    rather than by comparing strikes and dates and hoping the rounding matches.
    """
    earlier_by_id = {row.instrument_id: row for row in earlier}
    later_by_id = {row.instrument_id: row for row in later}

    matched = sorted(set(earlier_by_id) & set(later_by_id), key=str)
    changes = tuple(
        ContractOpenInterestChange(
            instrument_id=instrument_id,
            expiry=later_by_id[instrument_id].expiry,
            strike=later_by_id[instrument_id].strike,
            option_type=later_by_id[instrument_id].option_type,
            earlier_open_interest=earlier_by_id[instrument_id].open_interest,
            later_open_interest=later_by_id[instrument_id].open_interest,
        )
        for instrument_id in matched
    )

    return OpenInterestChange(
        underlying_id=underlying_id,
        earlier_as_of=earlier_as_of,
        later_as_of=later_as_of,
        earlier_snapshot_id=earlier_snapshot_id,
        later_snapshot_id=later_snapshot_id,
        contracts=changes,
        only_in_earlier=len(set(earlier_by_id) - set(later_by_id)),
        only_in_later=len(set(later_by_id) - set(earlier_by_id)),
    )


def _out(value: Decimal | None) -> str | None:
    return None if value is None else format(value, "f")
