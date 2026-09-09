"""Open-interest arithmetic, and the three ways it goes quietly wrong.

Summing absences as zeros understates a total. Dividing by a zero denominator
produces infinity where the honest answer is "there is none". Reporting a change
without the window it happened over lets eleven minutes read as a session. Each
of those produces a number that plots, and none of them raises.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from domains.instruments.enums import OptionType
from domains.market_data.open_interest import (
    OPEN_INTEREST_UNIT,
    ChainRow,
    build_change,
    build_profile,
)

UNDERLYING = uuid.uuid4()
AS_OF = datetime(2026, 9, 9, 9, 30, tzinfo=UTC)
EXPIRY = datetime(2026, 9, 24, tzinfo=UTC).date()


def row(
    strike: str,
    option_type: OptionType,
    open_interest: str | None = None,
    volume: str | None = None,
    excluded: bool = False,
    instrument_id: uuid.UUID | None = None,
) -> ChainRow:
    return ChainRow(
        instrument_id=instrument_id or uuid.uuid4(),
        expiry=EXPIRY,
        strike=Decimal(strike),
        option_type=option_type,
        open_interest=None if open_interest is None else Decimal(open_interest),
        volume=None if volume is None else Decimal(volume),
        excluded=excluded,
    )


class TestAggregation:
    def test_calls_and_puts_are_summed_separately(self):
        profile = build_profile(
            UNDERLYING,
            AS_OF,
            [
                row("24000", OptionType.CALL, "1000"),
                row("24100", OptionType.CALL, "500"),
                row("24000", OptionType.PUT, "1500"),
            ],
        )
        expiry = profile.for_expiry(EXPIRY)
        assert expiry.call_open_interest == Decimal(1500)
        assert expiry.put_open_interest == Decimal(1500)
        assert expiry.total_open_interest == Decimal(3000)

    def test_a_missing_figure_is_not_counted_as_zero(self):
        """Summing absences as zeros understates a total and moves the ratio
        built from it, and nothing about the result would say so."""
        profile = build_profile(
            UNDERLYING,
            AS_OF,
            [
                row("24000", OptionType.CALL, "1000"),
                row("24100", OptionType.CALL, None),
                row("24000", OptionType.PUT, "500"),
            ],
        )
        expiry = profile.for_expiry(EXPIRY)
        assert expiry.call_open_interest == Decimal(1000)
        assert expiry.contracts == 3
        assert expiry.contracts_with_open_interest == 2
        assert expiry.coverage == 2 / 3

    def test_a_chain_carrying_no_open_interest_at_all_reports_none_not_zero(self):
        profile = build_profile(
            UNDERLYING, AS_OF, [row("24000", OptionType.CALL), row("24000", OptionType.PUT)]
        )
        expiry = profile.for_expiry(EXPIRY)
        assert expiry.call_open_interest is None
        assert expiry.total_open_interest is None
        assert expiry.coverage == 0.0

    def test_excluded_quotes_stay_out_of_the_sums_and_are_counted(self):
        """A total that quietly includes quotes the quality engine refused
        disagrees with everything else computed from the same snapshot."""
        profile = build_profile(
            UNDERLYING,
            AS_OF,
            [
                row("24000", OptionType.CALL, "1000"),
                row("24100", OptionType.CALL, "900", excluded=True),
            ],
        )
        assert profile.call_open_interest == Decimal(1000)
        assert profile.excluded_contracts == 1

    def test_expiries_are_kept_apart(self):
        later = datetime(2026, 10, 29, tzinfo=UTC).date()
        rows = [
            row("24000", OptionType.CALL, "1000"),
            ChainRow(uuid.uuid4(), later, Decimal("24000"), OptionType.CALL, Decimal(300)),
        ]
        profile = build_profile(UNDERLYING, AS_OF, rows)
        assert [item.expiry for item in profile.expiries] == [EXPIRY, later]
        assert profile.call_open_interest == Decimal(1300)


class TestRatios:
    def test_the_put_call_ratio_is_puts_over_calls(self):
        profile = build_profile(
            UNDERLYING,
            AS_OF,
            [row("24000", OptionType.CALL, "1000"), row("24000", OptionType.PUT, "1500")],
        )
        assert profile.put_call_ratio_open_interest == 1.5

    def test_a_zero_denominator_gives_none_rather_than_infinity(self):
        """'There is no open interest on the calls' is a fact about the chain.
        An infinite ratio would plot as a spike."""
        profile = build_profile(
            UNDERLYING,
            AS_OF,
            [row("24000", OptionType.CALL, "0"), row("24000", OptionType.PUT, "1500")],
        )
        assert profile.put_call_ratio_open_interest is None

    def test_an_absent_denominator_also_gives_none(self):
        profile = build_profile(UNDERLYING, AS_OF, [row("24000", OptionType.PUT, "1500")])
        assert profile.put_call_ratio_open_interest is None

    def test_volume_to_open_interest_is_turnover_against_open_positions(self):
        profile = build_profile(
            UNDERLYING,
            AS_OF,
            [
                row("24000", OptionType.CALL, "1000", "250"),
                row("24000", OptionType.PUT, "1000", "250"),
            ],
        )
        assert profile.for_expiry(EXPIRY).volume_to_open_interest == 0.25

    def test_the_unit_of_an_absolute_total_is_labelled_not_assumed(self):
        """Some venues publish open interest in contracts and some in units of
        the underlying. Ratios cancel it; totals cannot, so they say so."""
        profile = build_profile(UNDERLYING, AS_OF, [row("24000", OptionType.CALL, "1000")])
        assert profile.to_dict()["open_interest_unit"] == OPEN_INTEREST_UNIT


class TestWhereOpenInterestSits:
    def test_the_busiest_strikes_are_reported_in_order(self):
        profile = build_profile(
            UNDERLYING,
            AS_OF,
            [
                row("24000", OptionType.CALL, "1000"),
                row("24500", OptionType.CALL, "5000"),
                row("23500", OptionType.PUT, "3000"),
            ],
        )
        busiest = profile.for_expiry(EXPIRY).most_open_interest(limit=2)
        assert [str(item.strike) for item in busiest] == ["24500", "23500"]

    def test_a_strike_carries_both_sides(self):
        profile = build_profile(
            UNDERLYING,
            AS_OF,
            [
                row("24000", OptionType.CALL, "1000", "10"),
                row("24000", OptionType.PUT, "1500", "20"),
            ],
        )
        strike = profile.for_expiry(EXPIRY).strikes[0]
        assert strike.call_open_interest == Decimal(1000)
        assert strike.put_open_interest == Decimal(1500)
        assert strike.put_call_ratio_open_interest == 1.5


class TestChangeBetweenTwoObservations:
    def _pair(self):
        call = uuid.uuid4()
        put = uuid.uuid4()
        earlier = [
            row("24000", OptionType.CALL, "1000", instrument_id=call),
            row("24000", OptionType.PUT, "800", instrument_id=put),
        ]
        later = [
            row("24000", OptionType.CALL, "1400", instrument_id=call),
            row("24000", OptionType.PUT, "600", instrument_id=put),
        ]
        return call, put, earlier, later

    def test_the_change_is_later_minus_earlier(self):
        _call, _put, earlier, later = self._pair()
        change = build_change(UNDERLYING, AS_OF, AS_OF + timedelta(minutes=11), earlier, later)
        assert change.total_change == Decimal(200)
        assert change.largest_increases(1)[0].change == Decimal(400)
        assert change.largest_decreases(1)[0].change == Decimal(-200)

    def test_the_window_travels_with_the_change(self):
        """Without it, a figure measured over eleven minutes reads exactly like
        one measured over a session."""
        _call, _put, earlier, later = self._pair()
        change = build_change(UNDERLYING, AS_OF, AS_OF + timedelta(minutes=11), earlier, later)
        assert change.window_seconds == 660.0
        assert change.to_dict()["window_seconds"] == 660.0

    def test_contracts_are_matched_on_identity_not_on_strike_and_date(self):
        """Instrument ids are derived from the canonical key, so two snapshots
        agree about which contract is which by construction rather than by
        comparing decimals and hoping the rounding matches."""
        call, _put, earlier, later = self._pair()
        change = build_change(UNDERLYING, AS_OF, AS_OF, earlier, later)
        assert {item.instrument_id for item in change.contracts} >= {call}
        assert len(change.contracts) == 2
        assert change.to_dict()["matched_contracts"] == 2

    def test_contracts_in_only_one_snapshot_are_counted_not_dropped(self):
        """A chain's listed strikes change as the underlying moves. That is
        ordinary, but a total that quietly dropped them would not add up."""
        call, _put, earlier, later = self._pair()
        later = [*later, row("25000", OptionType.CALL, "50")]
        change = build_change(UNDERLYING, AS_OF, AS_OF, earlier, later)
        assert change.only_in_later == 1
        assert change.only_in_earlier == 0
        assert len(change.contracts) == 2

    def test_a_contract_missing_a_figure_on_either_side_has_no_change(self):
        instrument = uuid.uuid4()
        change = build_change(
            UNDERLYING,
            AS_OF,
            AS_OF,
            [row("24000", OptionType.CALL, None, instrument_id=instrument)],
            [row("24000", OptionType.CALL, "1000", instrument_id=instrument)],
        )
        assert change.contracts[0].change is None
        assert change.total_change is None
