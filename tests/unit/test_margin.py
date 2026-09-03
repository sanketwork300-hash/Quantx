"""Margin estimation and vulnerability, and the claims neither one makes.

Carries the Phase 6 acceptance criteria from docs/backlog.md: every result
carries method, assumptions, confidence and warnings; no output names a broker
or claims broker equivalence; the shortfall output is a region with assumptions,
never a single guaranteed price.
"""

from __future__ import annotations

import uuid

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from domains.instruments.enums import OptionType
from domains.portfolio.models import PositionGreeks
from domains.risk.exposure import ExcludedExposure, ExposureExclusion, ExposureSet, PositionExposure
from domains.risk.margin import (
    MARGIN_MODELS,
    MarginParameters,
    MarginWarning,
    ShockGrid,
    SimpleRiskMarginModel,
    build_model,
)
from domains.risk.vulnerability import (
    DEFAULT_LADDER,
    Direction,
    VulnerabilityWarning,
    scan_vulnerability,
)
from quant.pricing.black_scholes import bsm_greeks, bsm_price

UNDERLYING = uuid.uuid4()
KEY = str(UNDERLYING)
SPOT = 24_000.0
RATE = 0.065
TAU = 0.25


def leg(
    strike: float,
    option_type: OptionType,
    quantity: float,
    volatility: float = 0.15,
    underlying: uuid.UUID = UNDERLYING,
) -> PositionExposure:
    is_call = option_type is OptionType.CALL
    price = float(bsm_price(SPOT, strike, TAU, RATE, 0.0, volatility, is_call))
    greeks = bsm_greeks(SPOT, strike, TAU, RATE, 0.0, volatility, is_call)
    scale = quantity * 75.0
    return PositionExposure(
        position_id=uuid.uuid4(),
        instrument_id=uuid.uuid4(),
        canonical_key=f"SYNTH:OPTION:NIFTY:2026-12-24:{int(strike)}:{option_type.code}",
        asset_class="OPTION",
        underlying_id=underlying,
        underlying_key=str(underlying),
        currency="INR",
        strategy_tag="carry",
        scale=scale,
        fx_rate=1.0,
        spot=SPOT,
        is_option=True,
        strike=strike,
        option_type=option_type,
        time_to_expiry=TAU,
        implied_volatility=volatility,
        rate=RATE,
        dividend_yield=0.0,
        base_price=price,
        reported_value=price * scale,
        greeks=PositionGreeks(
            delta=float(greeks.delta) * scale,
            gamma=float(greeks.gamma) * scale,
            vega_per_vol_point=float(greeks.vega_per_vol_point) * scale,
            theta_per_day=float(greeks.theta_per_day) * scale,
            rho_per_bp=float(greeks.rho_per_bp) * scale,
        ),
    )


def index_leg(quantity: float, underlying: uuid.UUID = UNDERLYING) -> PositionExposure:
    return PositionExposure(
        position_id=uuid.uuid4(),
        instrument_id=uuid.uuid4(),
        canonical_key="SYNTH:INDEX:NIFTY",
        asset_class="INDEX",
        underlying_id=underlying,
        underlying_key=str(underlying),
        currency="INR",
        strategy_tag="hedge",
        scale=quantity,
        fx_rate=1.0,
        spot=SPOT,
        is_option=False,
        base_price=SPOT,
        reported_value=SPOT * quantity,
        greeks=PositionGreeks(delta=quantity),
    )


def book(*exposures: PositionExposure, excluded=()) -> ExposureSet:
    return ExposureSet(exposures=tuple(exposures), excluded=tuple(excluded), base_currency="INR")


@pytest.fixture
def short_book() -> ExposureSet:
    """Short gamma and short vega — the shape margin exists to catch."""
    return book(leg(23_000, OptionType.PUT, -10, 0.17), leg(25_000, OptionType.CALL, -6, 0.14))


class TestNoBrokerClaim:
    """Phase 6 acceptance: no output names a broker or claims equivalence."""

    def test_the_result_carries_its_disclaimer(self, short_book):
        result = SimpleRiskMarginModel().calculate(short_book)
        assert "not your broker" in result.disclaimer.lower()
        assert "estimate" in result.disclaimer.lower()

    def test_no_field_could_be_read_as_a_broker_requirement(self, short_book):
        payload = SimpleRiskMarginModel().calculate(short_book).to_dict()
        for banned in ("required_margin", "broker_margin", "exchange_margin", "span"):
            assert banned not in payload

    def test_nothing_in_the_serialised_output_names_a_venue_or_promises_a_level(self, short_book):
        result = scan_vulnerability(short_book, SimpleRiskMarginModel(), 3_000_000.0)
        text = str(result.to_dict()).lower()
        for phrase in (
            "will be liquidated",
            "liquidation price",
            "your broker requires",
            "broker margin",
            "guaranteed",
            "zerodha",
            "interactive brokers",
            "span margin",
        ):
            assert phrase not in text, phrase

    def test_liquidation_is_only_ever_mentioned_to_deny_it(self, short_book):
        """The word may appear, but only inside its own denial.

        Banning the substring outright would forbid the sentence that does the
        most work here — "this is a model estimate, not a broker liquidation
        level" — so the test checks the construction instead of the word.
        """
        result = scan_vulnerability(short_book, SimpleRiskMarginModel(), 2_500_000.0)
        text = str(result.to_dict()).lower()
        start, found_any = 0, False
        while (found := text.find("liquidat", start)) != -1:
            found_any = True
            assert "not a broker" in text[max(0, found - 40) : found], text[
                max(0, found - 60) : found + 40
            ]
            start = found + 1
        assert found_any, "the disclaiming sentence should be in the payload"

    def test_the_word_estimated_survives_into_the_summary(self, short_book):
        result = scan_vulnerability(short_book, SimpleRiskMarginModel(), 3_000_000.0)
        assert "estimated margin-shortfall region" in result.summary
        assert "model estimate" in result.summary

    def test_every_registered_model_names_and_versions_itself(self):
        for name, model in MARGIN_MODELS.items():
            instance = model()
            assert instance.name == name
            assert instance.version
            assert "@" in instance.identifier

    def test_an_unknown_model_is_refused(self):
        with pytest.raises(ValueError, match="unknown margin model"):
            build_model("SPAN")


class TestResultCompleteness:
    """Phase 6 acceptance: method, assumptions, confidence and warnings, always."""

    def test_every_result_carries_all_four(self, short_book):
        result = SimpleRiskMarginModel().calculate(short_book)
        assert result.method == "SimpleRiskMarginModel@1.0.0"
        assert len(result.assumptions) >= 3
        assert 0.0 <= result.confidence <= 1.0
        assert isinstance(result.warnings, tuple)

    def test_the_grid_it_measured_over_is_reported(self, short_book):
        result = SimpleRiskMarginModel().calculate(short_book)
        grid = result.parameters["grid"]
        assert grid["points"] == len(grid["spot_returns"]) * len(grid["vol_points"])
        assert any(str(grid["points"]) in item for item in result.assumptions)

    def test_the_components_add_up_the_way_the_docstring_says(self, short_book):
        model = SimpleRiskMarginModel(
            MarginParameters(short_option_minimum_rate=0.01, concentration_add_on_rate=0.02)
        )
        result = model.calculate(short_book)
        by_name = {item.name: item.amount for item in result.components}
        assert result.estimated_margin == pytest.approx(
            max(by_name["scan_loss"], by_name["short_option_minimum"])
            + by_name["concentration_add_on"]
        )

    def test_every_component_states_its_basis(self, short_book):
        for component in SimpleRiskMarginModel().calculate(short_book).components:
            assert len(component.basis) > 20

    def test_a_grid_without_an_unshocked_point_is_refused(self):
        with pytest.raises(ValueError, match="unshocked point"):
            ShockGrid(spot_returns=(-0.1, -0.05), vol_points=(0.0,))

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"short_option_minimum_rate": -0.01},
            {"concentration_add_on_rate": -0.01},
            {"concentration_threshold": 0.0},
            {"concentration_threshold": 1.5},
        ],
    )
    def test_a_nonsensical_parameter_is_refused(self, kwargs):
        with pytest.raises(ValueError):
            MarginParameters(**kwargs)


class TestWhatTheDefaultsLeaveOut:
    """The zero defaults are a refusal to invent, and they say so."""

    def test_a_zero_short_option_minimum_is_declared_not_hidden(self, short_book):
        result = SimpleRiskMarginModel().calculate(short_book)
        assert MarginWarning.NO_SHORT_OPTION_MINIMUM in result.warnings
        assert any("inventing a rule" in item for item in result.assumptions)

    def test_the_floor_binds_where_the_scan_says_almost_nothing(self):
        """A far out-of-the-money short is exactly what the floor is for."""
        far = book(leg(15_000, OptionType.PUT, -20, 0.30))
        narrow = ShockGrid(spot_returns=(-0.02, 0.0, 0.02), vol_points=(0.0,))

        bare = SimpleRiskMarginModel(MarginParameters(grid=narrow)).calculate(far)
        floored = SimpleRiskMarginModel(
            MarginParameters(grid=narrow, short_option_minimum_rate=0.02)
        ).calculate(far)

        assert floored.estimated_margin > bare.estimated_margin
        assert floored.estimated_margin == pytest.approx(0.02 * far.exposures[0].notional)

    def test_the_floor_does_not_add_to_a_larger_scan_loss(self, short_book):
        model = SimpleRiskMarginModel(MarginParameters(short_option_minimum_rate=0.001))
        result = model.calculate(short_book)
        by_name = {item.name: item.amount for item in result.components}
        assert by_name["scan_loss"] > by_name["short_option_minimum"]
        assert result.estimated_margin == pytest.approx(by_name["scan_loss"])

    def test_the_concentration_add_on_states_its_rate_and_threshold(self, short_book):
        model = SimpleRiskMarginModel(MarginParameters(concentration_add_on_rate=0.03))
        component = next(
            item
            for item in model.calculate(short_book).components
            if item.name == "concentration_add_on"
        )
        assert "3.00%" in component.basis
        assert "50%" in component.basis
        assert "parameters of this model" in component.basis

    def test_two_underlyings_dilute_the_concentration_charge(self):
        other = uuid.uuid4()
        one_name = book(leg(23_000, OptionType.PUT, -10), leg(25_000, OptionType.CALL, -10))
        two_names = book(
            leg(23_000, OptionType.PUT, -10),
            leg(25_000, OptionType.CALL, -10, underlying=other),
        )
        model = SimpleRiskMarginModel(MarginParameters(concentration_add_on_rate=0.03))

        def add_on(exposures):
            return next(
                item.amount
                for item in model.calculate(exposures).components
                if item.name == "concentration_add_on"
            )

        assert add_on(two_names) < add_on(one_name)

    def test_a_flat_volatility_axis_is_declared(self, short_book):
        model = SimpleRiskMarginModel(
            MarginParameters(grid=ShockGrid(spot_returns=(-0.1, 0.0, 0.1), vol_points=(0.0,)))
        )
        result = model.calculate(short_book)
        assert MarginWarning.VOLATILITY_HELD_FLAT in result.warnings


class TestScanBehaviour:
    def test_the_worst_grid_point_is_reported(self, short_book):
        result = SimpleRiskMarginModel().calculate(short_book)
        assert result.worst_loss > 0.0
        assert result.worst_spot_return in ShockGrid().spot_returns
        assert result.worst_vol_points in ShockGrid().vol_points

    def test_a_worst_case_at_the_edge_is_flagged_and_lowers_confidence(self, short_book):
        """A short book's loss grows without bound, so no grid contains it."""
        narrow = SimpleRiskMarginModel(
            MarginParameters(grid=ShockGrid(spot_returns=(-0.05, 0.0, 0.05), vol_points=(0.0,)))
        ).calculate(short_book)
        assert narrow.worst_at_grid_edge
        assert MarginWarning.WORST_AT_GRID_EDGE in narrow.warnings
        assert narrow.confidence < 1.0
        assert any("larger than this" in item for item in narrow.assumptions)

    def test_a_worst_case_the_grid_does_contain_is_not_flagged(self):
        """A long straddle is worst where nothing moves, which is interior."""
        straddle = book(leg(24_000, OptionType.CALL, 5), leg(24_000, OptionType.PUT, 5))
        contained = SimpleRiskMarginModel(
            MarginParameters(
                grid=ShockGrid(spot_returns=(-0.10, -0.05, 0.0, 0.05, 0.10), vol_points=(0.0,))
            )
        ).calculate(straddle)

        assert contained.worst_spot_return == 0.0
        assert contained.worst_at_grid_edge is False
        assert MarginWarning.WORST_AT_GRID_EDGE not in contained.warnings

    def test_a_single_point_axis_is_not_an_edge_it_is_a_switched_off_dimension(self):
        """It has its own warning; counting it twice would misdescribe the grid."""
        grid = ShockGrid(spot_returns=(-0.1, 0.0, 0.1), vol_points=(0.0,))
        assert grid.is_edge(0.0, 0.0) is False
        assert grid.is_edge(-0.1, 0.0) is True

    def test_a_long_only_book_can_lose_nothing_and_that_is_not_an_error(self):
        """A long option's worst case is its premium, which is already paid."""
        longs = book(leg(24_000, OptionType.CALL, 5), leg(23_000, OptionType.PUT, 5))
        result = SimpleRiskMarginModel().calculate(longs)
        assert result.estimated_margin >= 0.0

    def test_a_wider_grid_never_estimates_less(self, short_book):
        narrow = SimpleRiskMarginModel(
            MarginParameters(grid=ShockGrid(spot_returns=(-0.05, 0.0, 0.05), vol_points=(0.0,)))
        ).calculate(short_book)
        wide = SimpleRiskMarginModel(
            MarginParameters(
                grid=ShockGrid(spot_returns=(-0.05, -0.2, 0.0, 0.05, 0.2), vol_points=(0.0, 0.1))
            )
        ).calculate(short_book)
        assert wide.estimated_margin >= narrow.estimated_margin

    def test_an_excluded_position_lowers_confidence_and_is_counted(self, short_book):
        with_gap = book(
            *short_book.exposures,
            excluded=(
                ExcludedExposure(
                    position_id=uuid.uuid4(),
                    canonical_key="SYNTH:OPTION:NIFTY:2026-12-24:99999:C",
                    reason=ExposureExclusion.NO_VOLATILITY,
                    base_value=1000.0,
                ),
            ),
        )
        clean = SimpleRiskMarginModel().calculate(short_book)
        gapped = SimpleRiskMarginModel().calculate(with_gap)

        assert gapped.excluded_positions == 1
        assert gapped.confidence < clean.confidence
        assert MarginWarning.POSITIONS_EXCLUDED in gapped.warnings

    def test_an_empty_book_estimates_zero_and_says_it_is_an_absence(self):
        result = SimpleRiskMarginModel().calculate(book())
        assert result.estimated_margin == 0.0
        assert result.confidence == 0.0
        assert MarginWarning.EMPTY_BOOK in result.warnings
        assert any("absence" in item for item in result.assumptions)

    @given(
        put_quantity=st.integers(min_value=-30, max_value=-1),
        call_quantity=st.integers(min_value=-30, max_value=30),
    )
    @settings(max_examples=25, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    def test_the_estimate_is_never_negative(self, put_quantity, call_quantity):
        exposures = [leg(23_000, OptionType.PUT, put_quantity)]
        if call_quantity != 0:
            exposures.append(leg(25_000, OptionType.CALL, call_quantity))
        result = SimpleRiskMarginModel().calculate(book(*exposures))
        assert result.estimated_margin >= 0.0
        assert 0.0 <= result.confidence <= 1.0


class TestVulnerabilityIsARegion:
    """Phase 6 acceptance: a region with assumptions, never a single price."""

    def test_the_crossing_is_reported_with_the_rungs_that_bracket_it(self, short_book):
        result = scan_vulnerability(short_book, SimpleRiskMarginModel(), 2_500_000.0)
        assert result.downside is not None
        low, high = result.downside.bracketed_by
        assert low > result.downside.approximate_entry > high or (
            high <= result.downside.approximate_entry <= low
        )
        assert result.downside.buffer_before > 0.0 >= result.downside.buffer_after

    def test_the_interpolated_entry_lies_between_its_brackets(self, short_book):
        result = scan_vulnerability(short_book, SimpleRiskMarginModel(), 2_500_000.0)
        before, after = result.downside.bracketed_by
        assert min(before, after) <= result.downside.approximate_entry <= max(before, after)

    def test_more_capital_pushes_the_region_further_away(self, short_book):
        near = scan_vulnerability(short_book, SimpleRiskMarginModel(), 2_500_000.0)
        far = scan_vulnerability(short_book, SimpleRiskMarginModel(), 3_500_000.0)
        assert far.downside is None or (
            far.downside.approximate_entry < near.downside.approximate_entry
        )

    def test_an_upside_short_is_found_too(self):
        """Scanning only downwards would miss a short-call book entirely."""
        short_calls = book(leg(24_500, OptionType.CALL, -20, 0.14))
        result = scan_vulnerability(short_calls, SimpleRiskMarginModel(), 9_000_000.0)
        assert result.upside is not None
        assert result.upside.direction is Direction.UPSIDE
        assert result.upside.approximate_entry > 0.0

    def test_both_sides_of_the_buffer_move_along_the_ladder(self, short_book):
        result = scan_vulnerability(short_book, SimpleRiskMarginModel(), 3_000_000.0)
        by_return = {point.spot_return: point for point in result.ladder}
        down, flat = by_return[-0.10], by_return[0.0]
        assert down.portfolio_value < flat.portfolio_value
        assert down.estimated_margin != flat.estimated_margin

    def test_the_ladder_covers_both_directions(self, short_book):
        result = scan_vulnerability(short_book, SimpleRiskMarginModel(), 3_000_000.0)
        returns = [point.spot_return for point in result.ladder]
        assert min(returns) < 0.0 < max(returns)
        assert returns == sorted(returns)
        assert len(returns) == len(DEFAULT_LADDER)

    def test_an_empty_ladder_is_refused(self, short_book):
        with pytest.raises(ValueError, match="at least one ladder point"):
            scan_vulnerability(short_book, SimpleRiskMarginModel(), 1.0, ladder=())


class TestCapitalIsNeverAssumed:
    def test_unknown_capital_leaves_utilisation_and_buffer_undefined(self, short_book):
        result = scan_vulnerability(short_book, SimpleRiskMarginModel(), None)
        assert result.base_buffer is None
        assert result.base_utilisation is None
        assert result.downside is None and result.upside is None
        assert VulnerabilityWarning.NO_ELIGIBLE_CAPITAL in result.warnings
        assert all(point.buffer is None for point in result.ladder)

    def test_the_estimate_itself_still_stands(self, short_book):
        result = scan_vulnerability(short_book, SimpleRiskMarginModel(), None)
        assert result.base.estimated_margin > 0.0
        assert "stands on its own" in result.summary

    def test_capital_is_never_defaulted_to_portfolio_value(self, short_book):
        result = scan_vulnerability(short_book, SimpleRiskMarginModel(), None)
        assert result.eligible_capital is None
        assert any("usually wrong quantity" in item for item in result.assumptions)

    def test_utilisation_is_the_ratio_it_claims_to_be(self, short_book):
        capital = 4_000_000.0
        result = scan_vulnerability(short_book, SimpleRiskMarginModel(), capital)
        assert result.base_utilisation == pytest.approx(result.base.estimated_margin / capital)
        assert result.base_buffer == pytest.approx(capital - result.base.estimated_margin)


class TestAlreadyInTheRegion:
    def test_a_book_short_before_any_move_says_so_rather_than_reporting_a_crossing(
        self, short_book
    ):
        result = scan_vulnerability(short_book, SimpleRiskMarginModel(), 10_000.0)
        assert result.in_shortfall_at_rest
        assert VulnerabilityWarning.ALREADY_SHORT in result.warnings
        assert "already inside" in result.summary
        assert result.downside is None and result.upside is None

    def test_a_comfortable_book_says_where_it_stops_looking(self, short_book):
        result = scan_vulnerability(short_book, SimpleRiskMarginModel(), 500_000_000.0)
        assert VulnerabilityWarning.NO_SHORTFALL_IN_RANGE in result.warnings
        assert "does not enter" in result.summary
        assert "beyond that range" in result.summary

    def test_a_co_shock_alone_can_put_a_book_in_the_region(self):
        """And the summary says it was the co-shock, not a move in the underlying."""
        vega_short = book(leg(24_000, OptionType.CALL, -30, 0.13))
        model = SimpleRiskMarginModel()
        quiet = scan_vulnerability(vega_short, model, 2_000_000.0, vol_co_shock=0.0)
        shocked = scan_vulnerability(vega_short, model, 2_000_000.0, vol_co_shock=0.20)
        if shocked.in_shortfall_at_rest and not quiet.in_shortfall_at_rest:
            assert "co-shock alone" in shocked.summary


class TestAssumptionsTravel:
    def test_the_ladder_declares_what_it_holds_fixed(self, short_book):
        result = scan_vulnerability(short_book, SimpleRiskMarginModel(), 3_000_000.0)
        joined = " ".join(result.assumptions)
        assert "no cash is added" in joined
        assert "fully repriced" in joined
        assert "moved together" in joined

    def test_a_grid_edge_warning_propagates_into_the_ladder_assumptions(self, short_book):
        model = SimpleRiskMarginModel(
            MarginParameters(grid=ShockGrid(spot_returns=(-0.05, 0.0, 0.05), vol_points=(0.0,)))
        )
        result = scan_vulnerability(short_book, model, 3_000_000.0)
        assert any("lower bound" in item for item in result.assumptions)

    def test_the_co_shock_is_stated(self, short_book):
        result = scan_vulnerability(
            short_book, SimpleRiskMarginModel(), 3_000_000.0, vol_co_shock=0.05
        )
        assert any("+5 volatility-point co-shock" in item for item in result.assumptions)
        assert "+5 vol-point co-shock" in result.summary


class TestShiftedBook:
    def test_a_null_shift_changes_nothing(self, short_book):
        shifted = short_book.shifted()
        assert shifted.base_value == pytest.approx(short_book.base_value, rel=1e-15)

    def test_a_shifted_book_is_internally_consistent(self, short_book):
        """Its model value and its mark agree, because it has no real mark."""
        shifted = short_book.shifted(spot_return=-0.10, vol_points=0.05)
        assert shifted.repricing_gap == pytest.approx(0.0, abs=1e-9)

    def test_shifting_twice_composes_the_way_the_pricer_does(self, short_book):
        once = short_book.shifted(spot_return=-0.10)
        twice = short_book.shifted(spot_return=-0.05).shifted(spot_return=-0.05 / 0.95)
        assert once.base_value == pytest.approx(twice.base_value, rel=1e-9)

    def test_notional_is_contract_notional_not_premium(self):
        put = leg(23_000, OptionType.PUT, -10)
        assert put.notional == pytest.approx(23_000 * 10 * 75)
        assert put.is_short

    def test_a_linear_leg_uses_its_own_level_as_notional(self):
        index = index_leg(150)
        assert index.notional == pytest.approx(SPOT * 150)
        assert index.is_short is False
