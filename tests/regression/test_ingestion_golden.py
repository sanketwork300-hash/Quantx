"""Golden-file regression over the committed fixtures.

A drift beyond tolerance fails CI. Regenerating a golden file is deliberate
(``python scripts/regen_golden.py --accept <case>``) and must be justified in
the pull request, alongside a model version bump if a formula changed.
"""

from __future__ import annotations

import pytest

from tests.regression.golden import CASES, load_golden, run_case
from tests.tolerances import GOLDEN_SCORE_ABS

pytestmark = pytest.mark.regression


@pytest.fixture(scope="module", params=CASES)
def case(request):
    name = request.param
    return name, run_case(name), load_golden(name)


class TestGoldenIngestion:
    def test_counts_are_unchanged(self, case):
        _name, actual, expected = case
        assert actual["counts"] == expected["counts"]

    def test_row_conservation(self, case):
        _name, actual, _expected = case
        counts = actual["counts"]
        assert counts["input"] == counts["kept"] + counts["excluded"] + counts["rejected"]

    def test_exclusion_and_rejection_reasons_are_unchanged(self, case):
        _name, actual, expected = case
        assert actual["exclusion_counts"] == expected["exclusion_counts"]
        assert actual["rejection_counts"] == expected["rejection_counts"]

    def test_flag_counts_are_unchanged(self, case):
        _name, actual, expected = case
        assert actual["flag_counts"] == expected["flag_counts"]

    def test_warning_codes_are_unchanged(self, case):
        _name, actual, expected = case
        assert actual["warning_codes"] == expected["warning_codes"]

    def test_model_versions_are_unchanged(self, case):
        """A formula change must bump a model version, or provenance lies."""
        _name, actual, expected = case
        assert actual["model_versions"] == expected["model_versions"]

    def test_aggregate_scores_are_within_tolerance(self, case):
        _name, actual, expected = case
        for key, value in expected["aggregate_quality"].items():
            assert actual["aggregate_quality"][key] == pytest.approx(value, abs=GOLDEN_SCORE_ABS), (
                key
            )

    def test_every_quote_matches(self, case):
        _name, actual, expected = case
        assert len(actual["quotes"]) == len(expected["quotes"])
        for got, want in zip(actual["quotes"], expected["quotes"], strict=True):
            key = (want["expiry"], want["strike"], want["option_type"])
            assert (got["expiry"], got["strike"], got["option_type"]) == key
            assert got["excluded"] == want["excluded"], key
            assert got["exclusion_reason"] == want["exclusion_reason"], key
            assert got["flags"] == want["flags"], key
            for score, value in want["scores"].items():
                assert got["scores"][score] == pytest.approx(value, abs=GOLDEN_SCORE_ABS), (
                    key,
                    score,
                )

    def test_every_excluded_quote_has_a_reason(self, case):
        _name, actual, _expected = case
        for quote in actual["quotes"]:
            if quote["excluded"]:
                assert quote["exclusion_reason"], quote
                assert quote["flags"], quote


class TestDeterminism:
    def test_two_runs_of_the_same_input_agree_exactly(self):
        first = run_case("options_chain_clean")
        second = run_case("options_chain_clean")
        assert first == second
