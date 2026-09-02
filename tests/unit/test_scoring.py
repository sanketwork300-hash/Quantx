"""Score transforms: shape, bounds and the aggregation choice."""

from __future__ import annotations

import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from quant.numerical.tolerances import clamp, is_close, safe_divide
from quant.statistics.scoring import (
    exponential_decay_score,
    geometric_mean,
    ratio_penalty_score,
    saturating_score,
    weighted_geometric_mean,
)
from tests.tolerances import SCORE_ABS


class TestExponentialDecay:
    def test_anchors(self):
        assert exponential_decay_score(0, 300) == 1.0
        assert exponential_decay_score(300, 300) == pytest.approx(0.5, abs=SCORE_ABS)
        assert exponential_decay_score(600, 300) == pytest.approx(0.25, abs=SCORE_ABS)

    def test_negative_age_is_treated_as_fresh(self):
        assert exponential_decay_score(-10, 300) == 1.0

    def test_rejects_non_positive_half_life(self):
        with pytest.raises(ValueError):
            exponential_decay_score(10, 0)

    @given(
        value=st.floats(min_value=0, max_value=1e6, allow_nan=False),
        half_life=st.floats(min_value=1e-3, max_value=1e5, allow_nan=False),
    )
    def test_is_bounded_and_monotone(self, value, half_life):
        score = exponential_decay_score(value, half_life)
        assert 0.0 <= score <= 1.0
        assert exponential_decay_score(value + 1.0, half_life) <= score + 1e-12


class TestRatioPenalty:
    def test_half_at_the_reference(self):
        assert ratio_penalty_score(0.02, 0.02) == pytest.approx(0.5, abs=SCORE_ABS)

    def test_collapses_for_multiples_of_the_reference(self):
        assert ratio_penalty_score(0.2, 0.02) < 0.02

    def test_zero_spread_scores_perfectly(self):
        assert ratio_penalty_score(0.0, 0.02) == 1.0


class TestSaturating:
    def test_half_at_the_reference(self):
        assert saturating_score(1000, 1000) == pytest.approx(0.5, abs=SCORE_ABS)

    def test_saturates_rather_than_rewarding_extremes(self):
        """Below the reference the score must move a lot; far above it, barely.

        The difference between 0.1x and 1x the reference volume decides whether
        a quote can be trusted; the difference between 10x and 100x does not.
        """
        gain_below = saturating_score(1_000, 1000) - saturating_score(100, 1000)
        gain_above = saturating_score(100_000, 1000) - saturating_score(10_000, 1000)
        assert gain_above < gain_below / 4

    def test_zero_scores_zero(self):
        assert saturating_score(0, 1000) == 0.0


class TestWeightedGeometricMean:
    def test_equals_the_plain_mean_of_identical_values(self):
        assert geometric_mean([0.5] * 4) == pytest.approx(0.5, abs=SCORE_ABS)

    def test_a_single_zero_zeroes_the_result(self):
        """The reason the overall quality score is geometric: one catastrophic
        dimension must not be averaged away by four healthy ones."""
        assert weighted_geometric_mean([1.0, 1.0, 1.0, 0.0], [1, 1, 1, 1]) == 0.0

    def test_weights_shift_the_result_toward_the_heavier_dimension(self):
        light = weighted_geometric_mean([0.2, 0.9], [1.0, 1.0])
        heavy = weighted_geometric_mean([0.2, 0.9], [3.0, 1.0])
        assert heavy < light

    def test_zero_weight_dimensions_are_ignored(self):
        assert weighted_geometric_mean([0.0, 0.8], [0.0, 1.0]) == pytest.approx(0.8, abs=SCORE_ABS)

    def test_rejects_mismatched_lengths(self):
        with pytest.raises(ValueError):
            weighted_geometric_mean([0.5], [1.0, 1.0])

    def test_rejects_all_zero_weights(self):
        with pytest.raises(ValueError):
            weighted_geometric_mean([0.5, 0.5], [0.0, 0.0])

    @given(
        values=st.lists(
            st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
            min_size=1,
            max_size=6,
        )
    )
    def test_lies_between_the_min_and_max(self, values):
        result = geometric_mean(values)
        assert min(values) - 1e-9 <= result <= max(values) + 1e-9


class TestNumericalHelpers:
    def test_safe_divide_guards_a_zero_denominator(self):
        assert safe_divide(1.0, 0.0) is None
        assert safe_divide(1.0, 0.0, default=0.0) == 0.0
        assert safe_divide(1.0, 4.0) == 0.25

    def test_safe_divide_rejects_non_finite_results(self):
        assert safe_divide(math.inf, 1.0) is None

    def test_clamp(self):
        assert clamp(5.0, 0.0, 1.0) == 1.0
        assert clamp(-1.0, 0.0, 1.0) == 0.0
        with pytest.raises(ValueError):
            clamp(0.5, 1.0, 0.0)

    def test_is_close_uses_declared_tolerances(self):
        assert is_close(1.0, 1.0 + 1e-12)
        assert not is_close(1.0, 1.001)
