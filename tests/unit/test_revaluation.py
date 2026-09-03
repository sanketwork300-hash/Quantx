"""Full revaluation, the Greek approximation, and risk decomposition.

Carries the Phase 5 acceptance criterion from docs/backlog.md that stress on an
option book reprices rather than extrapolating Greeks, with a test proving the
two differ materially for a large shock.
"""

from __future__ import annotations

import uuid

import numpy as np
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from domains.instruments.enums import OptionType
from domains.portfolio.models import PositionGreeks
from domains.risk.exposure import ExcludedExposure, ExposureExclusion, ExposureSet, PositionExposure
from domains.risk.factors import FactorKind, FactorSeries, HistorySource, build_panel
from domains.risk.revaluation import FactorShock, resolve_scenario, revalue, revalue_many
from domains.risk.stress import (
    ContributionDimension,
    apply_scenario,
    contributions_by_holding_flat,
)
from domains.risk.var import historical_var, monte_carlo_var, parametric_var
from domains.scenarios.library import template_by_name
from domains.scenarios.models import RiskFactorKind, Scenario, ScenarioSource, Shock, ShockType
from quant.pricing.black_scholes import bsm_greeks, bsm_price

UNDERLYING = uuid.uuid4()
KEY = str(UNDERLYING)
SPOT = 24000.0
RATE = 0.065
DIVIDEND = 0.0
TAU = 0.25


def leg(
    strike: float,
    option_type: OptionType,
    quantity: float,
    volatility: float = 0.14,
    tag: str = "carry",
    expiry: str = "2026-12-24",
) -> PositionExposure:
    is_call = option_type is OptionType.CALL
    price = float(bsm_price(SPOT, strike, TAU, RATE, DIVIDEND, volatility, is_call))
    greeks = bsm_greeks(SPOT, strike, TAU, RATE, DIVIDEND, volatility, is_call)
    scale = quantity * 75.0
    return PositionExposure(
        position_id=uuid.uuid4(),
        instrument_id=uuid.uuid4(),
        canonical_key=f"SYNTH:OPTION:NIFTY:{expiry}:{int(strike)}:{option_type.code}",
        asset_class="OPTION",
        underlying_id=UNDERLYING,
        underlying_key=KEY,
        currency="INR",
        strategy_tag=tag,
        scale=scale,
        fx_rate=1.0,
        spot=SPOT,
        is_option=True,
        strike=strike,
        option_type=option_type,
        time_to_expiry=TAU,
        implied_volatility=volatility,
        rate=RATE,
        dividend_yield=DIVIDEND,
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


def linear_leg(quantity: float, tag: str = "hedge") -> PositionExposure:
    scale = quantity
    return PositionExposure(
        position_id=uuid.uuid4(),
        instrument_id=uuid.uuid4(),
        canonical_key="SYNTH:INDEX:NIFTY",
        asset_class="INDEX",
        underlying_id=UNDERLYING,
        underlying_key=KEY,
        currency="INR",
        strategy_tag=tag,
        scale=scale,
        fx_rate=1.0,
        spot=SPOT,
        is_option=False,
        base_price=SPOT,
        reported_value=SPOT * scale,
        greeks=PositionGreeks(delta=scale),
    )


@pytest.fixture
def book() -> ExposureSet:
    """Short gamma, which is where a linearisation goes most wrong."""
    return ExposureSet(
        exposures=(
            leg(23000, OptionType.PUT, -10, 0.165, tag="carry"),
            leg(24000, OptionType.CALL, 3, 0.140, tag="atm"),
            leg(25400, OptionType.CALL, 6, 0.150, tag="wings", expiry="2027-03-25"),
            linear_leg(150),
        ),
        excluded=(),
        base_currency="INR",
    )


class TestTheNullScenario:
    """The property that makes every P&L below meaningful."""

    def test_no_shock_reprices_to_exactly_the_base_value(self, book):
        result = revalue(book, {KEY: FactorShock()})
        assert result.pnl == 0.0
        assert result.shocked_value == pytest.approx(result.base_value, rel=1e-15)

    def test_the_model_value_equals_the_marked_value_at_the_anchors(self, book):
        """The anchor volatility was inverted from the mark, so it reprices it."""
        assert book.repricing_gap == pytest.approx(0.0, abs=1e-9)

    def test_a_null_scenario_moves_no_position(self, book):
        for position in revalue(book, {KEY: FactorShock()}).positions:
            assert position.pnl == 0.0


class TestFullRevaluationVersusGreeks:
    """Phase 5 acceptance: the two must differ, and by more the larger the move."""

    def test_a_large_shock_makes_them_disagree_materially(self, book):
        result = apply_scenario(book, template_by_name("Underlying -10%")).revaluation
        relative = abs(result.approximation_error) / abs(result.pnl)
        assert relative > 0.05, (
            "a second-order expansion agreeing with the full repricing to within "
            "5% on a 10% move would mean the repricing was not earning its cost"
        )

    def test_a_small_shock_makes_them_nearly_agree(self, book):
        result = revalue(book, {KEY: FactorShock(spot_return=-0.001)})
        assert abs(result.approximation_error) / abs(result.pnl) < 0.01

    def test_the_error_grows_with_the_size_of_the_shock(self, book):
        errors = [
            abs(revalue(book, {KEY: FactorShock(spot_return=-size)}).approximation_error)
            for size in (0.01, 0.05, 0.10, 0.20)
        ]
        assert errors == sorted(errors)

    def test_the_reported_pnl_is_the_full_repricing_not_the_estimate(self, book):
        result = apply_scenario(book, template_by_name("Underlying -10%")).revaluation
        payload = result.to_dict()
        assert payload["pnl"] == result.pnl
        assert payload["greek_approximation"]["pnl"] == result.greek_estimate
        assert payload["pnl"] != payload["greek_approximation"]["pnl"]
        assert "approximation" in payload["greek_approximation"]["caveat"].lower()

    def test_a_linear_book_is_matched_exactly_by_its_delta(self):
        """The approximation is not wrong in general — only for convexity."""
        linear = ExposureSet(exposures=(linear_leg(150),), excluded=(), base_currency="INR")
        result = revalue(linear, {KEY: FactorShock(spot_return=-0.10)})
        assert result.greek_estimate == pytest.approx(result.pnl, rel=1e-12)


class TestShockSemantics:
    def test_a_volatility_shock_moves_an_option_and_not_a_future(self, book):
        result = revalue(book, {KEY: FactorShock(vol_points=0.05)})
        by_key = {p.canonical_key: p for p in result.positions}
        assert by_key["SYNTH:INDEX:NIFTY"].pnl == 0.0
        assert by_key["SYNTH:OPTION:NIFTY:2026-12-24:24000:C"].pnl > 0.0

    def test_a_short_option_loses_when_volatility_rises(self, book):
        result = revalue(book, {KEY: FactorShock(vol_points=0.05)})
        short_put = next(p for p in result.positions if p.canonical_key.endswith("23000:P"))
        assert short_put.pnl < 0.0

    def test_time_decay_costs_a_long_option_holder(self, book):
        result = revalue(book, {KEY: FactorShock()}, time_decay_days=10.0)
        long_call = next(p for p in result.positions if p.canonical_key.endswith("24000:C"))
        assert long_call.pnl < 0.0

    def test_a_volatility_driven_below_the_floor_is_clipped_and_counted(self, book):
        result = revalue(book, {KEY: FactorShock(vol_points=-0.50)})
        assert result.floored_volatilities == 3
        assert all(
            position.volatility_was_floored
            for position in result.positions
            if position.shocked_volatility is not None
        )

    def test_shocks_of_the_same_kind_compose_rather_than_overriding(self):
        scenario = Scenario(
            id=uuid.uuid4(),
            name="market plus name",
            source=ScenarioSource.USER_DEFINED,
            shocks=(
                Shock(RiskFactorKind.UNDERLYING_PRICE, ShockType.PERCENTAGE, -0.05),
                Shock(RiskFactorKind.UNDERLYING_PRICE, ShockType.PERCENTAGE, -0.03, target=KEY),
            ),
        )
        resolved = resolve_scenario(scenario, (KEY, "other"))
        assert resolved[KEY].spot_return == pytest.approx(-0.08)
        assert resolved["other"].spot_return == pytest.approx(-0.05)

    def test_basis_points_reach_the_pricer_as_a_decimal_rate(self):
        scenario = Scenario(
            id=uuid.uuid4(),
            name="rates",
            source=ScenarioSource.USER_DEFINED,
            shocks=(Shock(RiskFactorKind.RISK_FREE_RATE, ShockType.BASIS_POINTS, 100.0),),
        )
        assert resolve_scenario(scenario, (KEY,))[KEY].rate_shift == pytest.approx(0.01)


class TestVectorisedAgreesWithScalar:
    def test_many_scenarios_at_once_match_one_at_a_time(self, book):
        returns = np.array([-0.20, -0.10, -0.01, 0.0, 0.05, 0.15])
        vols = np.array([0.10, 0.05, 0.00, 0.0, -0.02, -0.04])
        vectorised = revalue_many(book, {KEY: returns}, {KEY: vols})
        scalar = [
            revalue(book, {KEY: FactorShock(spot_return=float(r), vol_points=float(v))}).pnl
            for r, v in zip(returns, vols, strict=True)
        ]
        assert vectorised == pytest.approx(scalar, rel=1e-12, abs=1e-9)

    def test_mismatched_factor_lengths_are_refused(self, book):
        with pytest.raises(ValueError, match="same number of scenarios"):
            revalue_many(book, {KEY: np.zeros(4), "other": np.zeros(3)})

    @given(
        spot=st.floats(min_value=-0.35, max_value=0.35),
        vol=st.floats(min_value=-0.08, max_value=0.15),
    )
    @settings(
        max_examples=30, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
    )
    def test_they_agree_for_any_shock(self, book, spot, vol):
        vectorised = revalue_many(book, {KEY: np.array([spot])}, {KEY: np.array([vol])})[0]
        scalar = revalue(book, {KEY: FactorShock(spot_return=spot, vol_points=vol)}).pnl
        assert vectorised == pytest.approx(scalar, rel=1e-9, abs=1e-6)


class TestRiskContribution:
    def test_contributions_sum_to_the_total_with_nothing_left_over(self, book):
        result = apply_scenario(book, template_by_name("Sell-off with volatility spike"))
        for breakdown in result.breakdowns:
            assert breakdown.residual == pytest.approx(0.0, abs=1e-6)
            grouped = sum(item.contribution for item in breakdown.contributions)
            assert grouped + breakdown.ungrouped_pnl == pytest.approx(breakdown.total_pnl, rel=1e-9)

    def test_the_cheap_decomposition_matches_holding_each_group_flat(self, book):
        """The claim that the decomposition is exact, actually checked."""
        scenario = template_by_name("Underlying -10%")
        shocks = resolve_scenario(scenario, book.underlying_keys())
        oracle = contributions_by_holding_flat(book, shocks, ContributionDimension.STRATEGY_TAG)
        breakdown = next(
            b for b in apply_scenario(book, scenario).breakdowns if b.dimension == "STRATEGY_TAG"
        )
        for item in breakdown.contributions:
            assert item.contribution == pytest.approx(oracle[item.key], rel=1e-9, abs=1e-6)

    def test_contributions_are_ordered_worst_first(self, book):
        breakdown = next(
            b
            for b in apply_scenario(book, template_by_name("Underlying -10%")).breakdowns
            if b.dimension == "UNDERLYING"
        )
        values = [item.contribution for item in breakdown.contributions]
        assert values == sorted(values)

    def test_a_position_with_no_key_for_a_dimension_is_counted_not_bucketed(self, book):
        """The index leg has no expiry; it is not filed under a made-up one."""
        breakdown = next(
            b
            for b in apply_scenario(book, template_by_name("Underlying -10%")).breakdowns
            if b.dimension == "EXPIRY"
        )
        assert breakdown.ungrouped_positions == 1
        assert breakdown.ungrouped_pnl != 0.0
        assert {item.key for item in breakdown.contributions} == {"2026-12-24", "2027-03-25"}

    def test_shares_are_omitted_rather_than_faked_when_the_total_is_zero(self, book):
        flat = Scenario(
            id=uuid.uuid4(),
            name="nothing",
            source=ScenarioSource.USER_DEFINED,
            shocks=(Shock(RiskFactorKind.RISK_FREE_RATE, ShockType.BASIS_POINTS, 0.0),),
        )
        breakdown = apply_scenario(book, flat).breakdowns[0]
        assert all(item.share is None for item in breakdown.contributions)


class TestExclusions:
    def test_an_unpriceable_position_is_reported_not_treated_as_riskless(self, book):
        excluded = ExcludedExposure(
            position_id=uuid.uuid4(),
            canonical_key="SYNTH:OPTION:NIFTY:2026-12-24:99999:C",
            reason=ExposureExclusion.NO_VOLATILITY,
            base_value=12_345.0,
        )
        with_gap = ExposureSet(exposures=book.exposures, excluded=(excluded,), base_currency="INR")
        result = apply_scenario(with_gap, template_by_name("Underlying -10%"))
        assert result.excluded_positions == 1
        assert result.excluded_value == pytest.approx(12_345.0)
        assert all(p.position_id != excluded.position_id for p in result.revaluation.positions)


class TestVaROnARepricedBook:
    @pytest.fixture
    def panel(self):
        rng = np.random.default_rng(20_260_924)
        from datetime import date, timedelta

        days = [date(2025, 1, 1) + timedelta(days=i) for i in range(401)]
        prices, vols = [SPOT], [0.15]
        for _ in range(400):
            prices.append(prices[-1] * float(np.exp(rng.normal(0.0, 0.011))))
            vols.append(max(0.05, vols[-1] + float(rng.normal(0.0, 0.004))))
        return build_panel(
            [
                FactorSeries(
                    f"spot:{KEY}",
                    FactorKind.SPOT_RETURN,
                    KEY,
                    HistorySource.CHAIN_SNAPSHOTS,
                    tuple(days),
                    tuple(prices),
                ),
                FactorSeries(
                    f"vol:{KEY}",
                    FactorKind.VOLATILITY_CHANGE,
                    KEY,
                    HistorySource.SURFACE_CHARACTERISTICS,
                    tuple(days),
                    tuple(vols),
                ),
            ]
        )

    def test_every_method_produces_a_positive_loss_threshold(self, book, panel):
        for result in (
            historical_var(book, panel),
            parametric_var(book, panel),
            monte_carlo_var(book, panel, paths=4_000, seed=1),
        ):
            for tail in result.tail_risks:
                assert tail.value_at_risk > 0.0
                assert tail.expected_shortfall >= tail.value_at_risk

    def test_a_higher_confidence_is_a_larger_loss(self, book, panel):
        result = historical_var(book, panel, confidences=(0.90, 0.95, 0.99))
        values = [tail.value_at_risk for tail in result.tail_risks]
        assert values == sorted(values)

    def test_the_parametric_answer_warns_that_the_book_is_not_linear(self, book, panel):
        result = parametric_var(book, panel)
        assert "RISK_PARAMETRIC_ON_NONLINEAR_BOOK" in result.warnings
        assert "options" in result.assumptions["validity"]
        assert result.assumptions["repricing"].startswith("none")

    def test_the_repricing_methods_say_they_repriced(self, book, panel):
        for result in (historical_var(book, panel), monte_carlo_var(book, panel, paths=2_000)):
            assert result.assumptions["repricing"] == "full"

    def test_monte_carlo_is_reproducible_from_its_seed(self, book, panel):
        first = monte_carlo_var(book, panel, paths=4_000, seed=42)
        second = monte_carlo_var(book, panel, paths=4_000, seed=42)
        assert [t.value_at_risk for t in first.tail_risks] == [
            t.value_at_risk for t in second.tail_risks
        ]

    def test_a_different_seed_gives_a_different_but_comparable_answer(self, book, panel):
        first = monte_carlo_var(book, panel, paths=4_000, seed=42)
        second = monte_carlo_var(book, panel, paths=4_000, seed=43)
        assert first.tail_risks[0].value_at_risk != second.tail_risks[0].value_at_risk
        assert first.tail_risks[0].value_at_risk == pytest.approx(
            second.tail_risks[0].value_at_risk, rel=0.15
        )

    def test_more_paths_narrow_the_interval_around_the_estimate(self, book, panel):
        few = monte_carlo_var(book, panel, paths=1_000, seed=5).estimate_intervals["0.95"]
        many = monte_carlo_var(book, panel, paths=40_000, seed=5).estimate_intervals["0.95"]
        assert (many[1] - many[0]) < (few[1] - few[0])

    def test_the_historical_answer_names_the_dates_that_hurt_most(self, book, panel):
        result = historical_var(book, panel)
        assert len(result.worst_scenario_dates) == 5
        assert all(isinstance(day, str) for day in result.worst_scenario_dates)

    def test_holding_volatility_constant_is_declared_when_it_happens(self, book):
        from datetime import date, timedelta

        days = [date(2025, 1, 1) + timedelta(days=i) for i in range(200)]
        rng = np.random.default_rng(3)
        prices = [SPOT]
        for _ in range(199):
            prices.append(prices[-1] * float(np.exp(rng.normal(0.0, 0.01))))
        spot_only = build_panel(
            [
                FactorSeries(
                    f"spot:{KEY}",
                    FactorKind.SPOT_RETURN,
                    KEY,
                    HistorySource.CHAIN_SNAPSHOTS,
                    tuple(days),
                    tuple(prices),
                )
            ]
        )
        result = historical_var(book, spot_only)
        assert "RISK_VOLATILITY_HELD_CONSTANT" in result.warnings
        assert "held constant" in result.assumptions["volatility"]


class TestFactorPanel:
    def test_nothing_is_forward_filled_across_a_gap(self):
        from datetime import date, timedelta

        days = [date(2026, 1, 1) + timedelta(days=i) for i in range(20)]
        prices = tuple(100.0 + i for i in range(20))
        vols = tuple(0.12 + 0.001 * i for i in range(20))
        panel = build_panel(
            [
                FactorSeries(
                    "spot:a",
                    FactorKind.SPOT_RETURN,
                    "a",
                    HistorySource.CHAIN_SNAPSHOTS,
                    tuple(days),
                    prices,
                ),
                FactorSeries(
                    "vol:a",
                    FactorKind.VOLATILITY_CHANGE,
                    "a",
                    HistorySource.SURFACE_CHARACTERISTICS,
                    tuple(days[:12]),
                    vols[:12],
                ),
            ]
        )
        assert panel.observations == 11
        assert "RISK_OBSERVATIONS_DROPPED_BY_ALIGNMENT" in panel.warnings
        assert not np.any(panel.column("vol:a") == 0.0)

    def test_a_short_history_is_refused_rather_than_extrapolated(self):
        from datetime import date, timedelta

        days = [date(2026, 1, 1) + timedelta(days=i) for i in range(5)]
        panel = build_panel(
            [
                FactorSeries(
                    "spot:a",
                    FactorKind.SPOT_RETURN,
                    "a",
                    HistorySource.CHAIN_SNAPSHOTS,
                    tuple(days),
                    tuple(100.0 + i for i in range(5)),
                )
            ]
        )
        assert panel.is_sufficient is False
        assert "RISK_INSUFFICIENT_HISTORY" in panel.warnings

    def test_overlapping_windows_are_declared(self):
        from datetime import date, timedelta

        days = [date(2026, 1, 1) + timedelta(days=i) for i in range(60)]
        panel = build_panel(
            [
                FactorSeries(
                    "spot:a",
                    FactorKind.SPOT_RETURN,
                    "a",
                    HistorySource.CHAIN_SNAPSHOTS,
                    tuple(days),
                    tuple(100.0 + i for i in range(60)),
                )
            ],
            window_days=5,
        )
        assert "RISK_OVERLAPPING_WINDOWS" in panel.warnings
        assert panel.window_days == 5

    def test_the_missing_data_policy_is_stated_in_the_payload(self):
        from datetime import date, timedelta

        days = [date(2026, 1, 1) + timedelta(days=i) for i in range(30)]
        panel = build_panel(
            [
                FactorSeries(
                    "spot:a",
                    FactorKind.SPOT_RETURN,
                    "a",
                    HistorySource.CHAIN_SNAPSHOTS,
                    tuple(days),
                    tuple(100.0 + i for i in range(30)),
                )
            ]
        )
        assert "forward-filled" in panel.to_dict()["missing_data_policy"]
