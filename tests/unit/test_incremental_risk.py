"""Incremental risk: what a proposed order adds, and when it must be refused.

The dangerous output of this module is a row of zeros. If the proposed position
cannot be repriced it never enters the book, every difference is exactly zero,
and the analysis reads as an order that adds no risk. These tests pin the
refusal that stops that, and the arithmetic that makes a non-zero difference
mean what it says.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from domains.instruments.enums import AssetClass, OptionType
from domains.portfolio.models import PositionGreeks
from domains.risk.exposure import (
    ExcludedExposure,
    ExposureExclusion,
    ExposureSet,
    PositionExposure,
)
from domains.risk.incremental import (
    IncrementalGreeks,
    IncrementalWarning,
    combine,
    greeks_of,
    incremental_margin,
)
from domains.risk.margin import MarginParameters, SimpleRiskMarginModel

AS_OF = datetime(2026, 9, 24, 9, 20, tzinfo=UTC)
UNDERLYING = uuid.uuid5(uuid.NAMESPACE_DNS, "nifty")
OTHER_UNDERLYING = uuid.uuid5(uuid.NAMESPACE_DNS, "banknifty")


def exposure(
    scale: float = 75.0,
    underlying: uuid.UUID = UNDERLYING,
    strike: float = 24_000.0,
    delta: float = 0.5,
) -> PositionExposure:
    return PositionExposure(
        position_id=uuid.uuid4(),
        instrument_id=uuid.uuid4(),
        canonical_key=f"SYNTH:OPTION:NIFTY:2026-12-24:{strike:g}:C",
        asset_class=AssetClass.OPTION,
        underlying_id=underlying,
        underlying_key=str(underlying),
        currency="INR",
        strategy_tag=None,
        scale=scale,
        fx_rate=1.0,
        spot=24_000.0,
        is_option=True,
        strike=strike,
        option_type=OptionType.CALL,
        time_to_expiry=0.25,
        implied_volatility=0.18,
        rate=0.065,
        dividend_yield=0.0,
        base_price=850.0,
        reported_value=850.0 * scale,
        greeks=PositionGreeks(
            delta=delta * scale, gamma=0.0001 * scale, vega_per_vol_point=40.0 * scale
        ),
    )


def book(*exposures: PositionExposure) -> ExposureSet:
    return ExposureSet(exposures=tuple(exposures), excluded=(), base_currency="INR")


class TestCombining:
    def test_the_order_joins_the_book_and_nothing_is_lost(self):
        current = book(exposure(), exposure(strike=24_500.0))
        order = book(exposure(scale=-75.0, strike=25_000.0))

        combined = combine(current, order)

        assert combined.order_is_repriceable is True
        assert len(combined.proposed.exposures) == 3
        assert combined.current is current
        assert combined.proposed.base_currency == "INR"
        assert combined.proposed.base_value == pytest.approx(current.base_value + order.base_value)

    def test_an_order_that_cannot_be_repriced_is_reported_not_absorbed(self):
        current = book(exposure())
        order = ExposureSet(
            exposures=(),
            excluded=(
                ExcludedExposure(
                    position_id=uuid.uuid4(),
                    canonical_key="SYNTH:OPTION:NIFTY:2026-12-24:26000:C",
                    reason=ExposureExclusion.NO_VOLATILITY,
                    base_value=None,
                ),
            ),
            base_currency="INR",
        )

        combined = combine(current, order)

        assert combined.order_is_repriceable is False
        assert combined.order_exclusion_reason == str(ExposureExclusion.NO_VOLATILITY)
        # Every difference downstream would be exactly zero, which is why the
        # caller must read the flag rather than the numbers.
        assert combined.proposed.exposures == current.exposures
        assert greeks_of(combined.proposed) == greeks_of(combined.current)

    def test_an_order_on_an_underlying_the_book_does_not_hold_is_flagged(self):
        combined = combine(book(exposure()), book(exposure(underlying=OTHER_UNDERLYING)))
        assert combined.order_is_on_a_new_underlying is True

    def test_an_order_on_an_underlying_the_book_holds_is_not(self):
        combined = combine(book(exposure()), book(exposure(strike=25_000.0)))
        assert combined.order_is_on_a_new_underlying is False

    def test_the_book_keeps_its_own_exclusions(self):
        excluded = ExcludedExposure(
            position_id=uuid.uuid4(),
            canonical_key="SYNTH:OPTION:NIFTY:2026-12-24:9000:P",
            reason=ExposureExclusion.NO_PRICE,
            base_value=None,
        )
        current = ExposureSet(exposures=(exposure(),), excluded=(excluded,), base_currency="INR")
        combined = combine(current, book(exposure(strike=25_000.0)))
        assert excluded in combined.proposed.excluded


class TestMovements:
    def test_a_change_is_the_difference_and_is_never_supplied(self):
        current = book(exposure(delta=0.5))
        order = book(exposure(scale=-75.0, delta=0.4))
        combined = combine(current, order)

        greeks = IncrementalGreeks(greeks_of(combined.current), greeks_of(combined.proposed))
        movements = {item.name: item for item in greeks.movements}

        delta = movements["delta"]
        assert delta.change == pytest.approx(delta.proposed - delta.current)
        # A sold call takes delta out of the book.
        assert delta.change < 0
        assert delta.unit

    def test_a_movement_with_a_missing_side_has_no_change_rather_than_a_zero(self):
        from domains.risk.incremental import Movement

        assert Movement("utilisation", "fraction", None, 0.4).change is None
        assert Movement("utilisation", "fraction", 0.4, None).change is None

    def test_doubling_the_order_doubles_its_greek_contribution(self):
        current = book(exposure())
        one = combine(current, book(exposure(scale=-75.0)))
        two = combine(current, book(exposure(scale=-150.0)))

        def delta_change(combined) -> float:
            return greeks_of(combined.proposed).delta - greeks_of(combined.current).delta

        assert delta_change(two) == pytest.approx(2 * delta_change(one))


class TestMargin:
    def test_both_sides_are_measured_by_one_model_on_one_grid(self):
        current = book(exposure())
        combined = combine(current, book(exposure(scale=-150.0, strike=25_000.0)))
        model = SimpleRiskMarginModel(MarginParameters())

        measured = incremental_margin(
            combined, model, eligible_capital=5_000_000.0, ladder=(0.0, -0.05), vol_co_shock=0.0
        )

        assert measured.current.base.model_version == measured.proposed.base.model_version
        assert measured.current.base.parameters == measured.proposed.base.parameters
        movements = {item.name: item for item in measured.movements}
        assert movements["estimated_margin"].change is not None
        assert movements["estimated_buffer"].change is not None
        assert "not" in measured.proposed.base.disclaimer.lower()

    def test_without_eligible_capital_the_buffer_is_absent_on_both_sides(self):
        combined = combine(book(exposure()), book(exposure(scale=-75.0)))
        measured = incremental_margin(
            combined,
            SimpleRiskMarginModel(MarginParameters()),
            eligible_capital=None,
            ladder=(0.0,),
            vol_co_shock=0.0,
        )
        movements = {item.name: item for item in measured.movements}
        assert movements["estimated_buffer"].current is None
        assert movements["estimated_buffer"].proposed is None
        assert movements["estimated_buffer"].change is None
        # The margin itself is still measured; only the buffer is undefined.
        assert movements["estimated_margin"].change is not None


class TestVocabulary:
    def test_the_warning_codes_say_what_they_mean(self):
        assert IncrementalWarning.ORDER_NOT_REPRICEABLE.startswith("INCREMENTAL_")
        assert IncrementalWarning.SHARED_PANEL.startswith("INCREMENTAL_")

    def test_nothing_in_a_margin_payload_promises_a_liquidation(self):
        combined = combine(book(exposure()), book(exposure(scale=-75.0)))
        payload = incremental_margin(
            combined,
            SimpleRiskMarginModel(MarginParameters()),
            eligible_capital=1_000_000.0,
            ladder=(0.0, -0.1),
            vol_co_shock=0.0,
        ).to_dict()
        blob = str(payload).lower()
        for phrase in ("will be liquidated", "broker margin", "you should", "recommend"):
            assert phrase not in blob, phrase
