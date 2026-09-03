"""Scenario definitions, and the refusal to invent historical facts.

Carries the Phase 5 rule from docs/risk.md §4: a scenario that claims to come
from history must carry the data it came from, and shipped templates make no
such claim.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest

from domains.scenarios.library import (
    HYPOTHETICAL_NOTE,
    derive_from_returns,
    template_by_name,
    templates,
)
from domains.scenarios.models import (
    MIN_SHOCKED_VOLATILITY,
    RiskFactorKind,
    Scenario,
    ScenarioError,
    ScenarioSource,
    Shock,
    ShockType,
)

DAYS = [date(2026, 1, 1) + timedelta(days=i) for i in range(60)]


def falling_series(crash_at: int = 30, crash: float = -0.11) -> list[float]:
    prices = [24000.0]
    for index in range(1, 60):
        move = crash if index == crash_at else 0.001 * ((-1) ** index)
        prices.append(prices[-1] * (1.0 + move))
    return prices


class TestShockUnits:
    @pytest.mark.parametrize(
        ("kind", "shock_type", "value", "level", "expected"),
        [
            (RiskFactorKind.UNDERLYING_PRICE, ShockType.PERCENTAGE, -0.10, 24000.0, 21600.0),
            (RiskFactorKind.UNDERLYING_PRICE, ShockType.ABSOLUTE, -500.0, 24000.0, 23500.0),
            (RiskFactorKind.VOLATILITY, ShockType.VOL_POINTS, 0.05, 0.12, 0.17),
            (RiskFactorKind.VOLATILITY, ShockType.PERCENTAGE, 0.25, 0.12, 0.15),
            (RiskFactorKind.RISK_FREE_RATE, ShockType.BASIS_POINTS, 100.0, 0.065, 0.075),
            (RiskFactorKind.RISK_FREE_RATE, ShockType.ABSOLUTE, 0.01, 0.065, 0.075),
        ],
    )
    def test_each_shock_type_means_what_its_name_says(
        self, kind, shock_type, value, level, expected
    ):
        assert Shock(kind, shock_type, value).apply(level) == pytest.approx(expected)

    @pytest.mark.parametrize(
        ("kind", "shock_type"),
        [
            (RiskFactorKind.RISK_FREE_RATE, ShockType.PERCENTAGE),
            (RiskFactorKind.UNDERLYING_PRICE, ShockType.VOL_POINTS),
            (RiskFactorKind.VOLATILITY, ShockType.BASIS_POINTS),
        ],
    )
    def test_a_shock_type_that_makes_no_sense_for_the_factor_is_refused(self, kind, shock_type):
        """A percentage move in a rate is ambiguous, so it is not guessed at."""
        with pytest.raises(ScenarioError, match="not a meaningful shock"):
            Shock(kind, shock_type, 0.1)

    def test_a_percentage_shock_to_zero_or_below_is_refused(self):
        with pytest.raises(ScenarioError, match="not a market state"):
            Shock(RiskFactorKind.UNDERLYING_PRICE, ShockType.PERCENTAGE, -1.0)

    def test_a_shock_scopes_to_its_target(self):
        scoped = Shock(RiskFactorKind.UNDERLYING_PRICE, ShockType.PERCENTAGE, -0.1, target="A")
        market_wide = Shock(RiskFactorKind.UNDERLYING_PRICE, ShockType.PERCENTAGE, -0.1)
        assert scoped.applies_to("A") and not scoped.applies_to("B")
        assert market_wide.applies_to("A") and market_wide.applies_to("B")

    def test_the_label_states_the_unit(self):
        assert Shock(RiskFactorKind.VOLATILITY, ShockType.VOL_POINTS, 0.05).label == "+5.00 vol pts"
        assert Shock(RiskFactorKind.RISK_FREE_RATE, ShockType.BASIS_POINTS, 25).label == "+25 bp"
        assert (
            Shock(RiskFactorKind.UNDERLYING_PRICE, ShockType.PERCENTAGE, -0.075).label == "-7.50%"
        )


class TestNoInventedHistory:
    """The rule this module exists to enforce."""

    def test_a_historical_claim_without_a_derivation_is_refused(self):
        with pytest.raises(ScenarioError, match="must carry the series"):
            Scenario(
                id=uuid.uuid4(),
                name="COVID crash",
                shocks=(Shock(RiskFactorKind.UNDERLYING_PRICE, ShockType.PERCENTAGE, -0.35),),
                source=ScenarioSource.DERIVED_FROM_HISTORY,
            )

    def test_every_shipped_template_is_labelled_hypothetical(self):
        for template in templates():
            assert template.source is ScenarioSource.HYPOTHETICAL
            assert template.is_historical_claim is False

    def test_every_template_says_in_its_own_words_that_it_is_not_history(self):
        for template in templates():
            assert HYPOTHETICAL_NOTE in (template.description or "")

    def test_no_template_names_a_real_market_event(self):
        """A round number under a real event's name reads as a measurement."""
        forbidden = ("covid", "2008", "lehman", "black monday", "taper", "demonetis", "flash crash")
        for template in templates():
            text = f"{template.name} {template.description}".lower()
            for word in forbidden:
                assert word not in text

    def test_template_ids_are_stable_across_processes(self):
        assert [t.id for t in templates()] == [t.id for t in templates()]
        assert template_by_name("Underlying -10%").id == template_by_name("Underlying -10%").id


class TestDerivation:
    def test_it_finds_the_worst_move_actually_in_the_series(self):
        scenario = derive_from_returns("Worst day", DAYS, falling_series(), "test series")
        assert scenario.source is ScenarioSource.DERIVED_FROM_HISTORY
        assert scenario.shocks[0].value == pytest.approx(-0.11)
        assert scenario.derivation.event_date == DAYS[30]
        assert scenario.derivation.observations == 60

    def test_the_description_names_the_series_and_the_date(self):
        scenario = derive_from_returns("Worst day", DAYS, falling_series(), "NIFTY chains")
        assert "NIFTY chains" in scenario.description
        assert DAYS[30].isoformat() in scenario.description

    def test_a_percentile_selects_a_quantile_rather_than_the_extreme(self):
        worst = derive_from_returns("worst", DAYS, falling_series(), "s")
        typical = derive_from_returns("p05", DAYS, falling_series(), "s", percentile=0.05)
        assert typical.shocks[0].value > worst.shocks[0].value
        assert "quantile" in typical.derivation.method

    def test_a_multi_day_window_uses_a_multi_day_move(self):
        scenario = derive_from_returns("Worst week", DAYS, falling_series(), "s", window_days=5)
        assert scenario.derivation.window_days == 5
        assert scenario.shocks[0].value < -0.10

    def test_the_volatility_shock_is_the_move_that_series_actually_made(self):
        # The worst return spans DAYS[29] -> DAYS[30], so that is the pair of
        # dates the volatility move must be read between.
        levels = [0.12 + 0.0002 * i for i in range(60)]
        levels[30] = levels[29] + 0.06
        scenario = derive_from_returns(
            "Worst day",
            DAYS,
            falling_series(),
            "s",
            volatility_dates=DAYS,
            volatility_levels=levels,
        )
        volatility = [s for s in scenario.shocks if s.kind is RiskFactorKind.VOLATILITY]
        assert len(volatility) == 1
        assert volatility[0].value == pytest.approx(0.06)

    def test_no_volatility_shock_is_invented_when_that_date_is_missing(self):
        scenario = derive_from_returns(
            "Worst day",
            DAYS,
            falling_series(),
            "s",
            volatility_dates=DAYS[:10],
            volatility_levels=[0.12] * 10,
        )
        assert all(s.kind is RiskFactorKind.UNDERLYING_PRICE for s in scenario.shocks)

    def test_a_series_with_no_moves_in_it_cannot_produce_a_scenario(self):
        with pytest.raises(ScenarioError, match="at least two observations"):
            derive_from_returns("x", DAYS[:1], [24000.0], "s")

    def test_a_window_longer_than_the_series_is_refused(self):
        with pytest.raises(ScenarioError, match="needs more than"):
            derive_from_returns("x", DAYS[:5], falling_series()[:5], "s", window_days=10)

    def test_a_non_positive_price_is_refused(self):
        prices = falling_series()
        prices[3] = 0.0
        with pytest.raises(ScenarioError, match="must be positive"):
            derive_from_returns("x", DAYS, prices, "s")

    def test_unordered_input_is_sorted_rather_than_misread(self):
        pairs = list(zip(DAYS, falling_series(), strict=True))
        shuffled = pairs[30:] + pairs[:30]
        scenario = derive_from_returns(
            "Worst day", [d for d, _ in shuffled], [p for _, p in shuffled], "s"
        )
        assert scenario.shocks[0].value == pytest.approx(-0.11)
        assert scenario.derivation.event_date == DAYS[30]


class TestScenarioValidation:
    def test_a_scenario_with_no_shocks_is_refused(self):
        with pytest.raises(ScenarioError, match="not a scenario"):
            Scenario(id=uuid.uuid4(), name="empty", shocks=(), source=ScenarioSource.USER_DEFINED)

    def test_a_scenario_needs_a_name(self):
        with pytest.raises(ScenarioError, match="needs a name"):
            Scenario(
                id=uuid.uuid4(),
                name="   ",
                shocks=(Shock(RiskFactorKind.UNDERLYING_PRICE, ShockType.PERCENTAGE, -0.1),),
                source=ScenarioSource.USER_DEFINED,
            )

    def test_the_volatility_floor_is_a_stated_constant(self):
        assert 0.0 < MIN_SHOCKED_VOLATILITY < 0.01
