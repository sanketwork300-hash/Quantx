"""``MarketState``: the timestamp-consistency guarantee."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from domains.market_data.curves import YieldCurve
from domains.market_data.market_state import MarketState, MarketStateBuilder
from domains.market_data.models import Quote

NOW = datetime(2026, 9, 24, 9, 20, tzinfo=UTC)
FIRST, SECOND = uuid.uuid4(), uuid.uuid4()


def quote(instrument_id, offset_seconds: float, bid: str = "100") -> Quote:
    stamp = NOW + timedelta(seconds=offset_seconds)
    return Quote(
        instrument_id=instrument_id,
        exchange_timestamp=stamp,
        receive_timestamp=stamp,
        source="test",
        bid_price=Decimal(bid),
        ask_price=Decimal(bid) + Decimal(1),
    )


class TestAdmission:
    def test_a_quote_from_after_the_as_of_is_rejected_with_a_reason(self):
        """A quote from after the decision time cannot belong to it."""
        builder = MarketStateBuilder(NOW).add_quote(quote(FIRST, +5))
        state = builder.build()
        assert FIRST not in state.quotes
        assert builder.rejected == ((FIRST, "AFTER_AS_OF"),)

    def test_the_latest_admissible_quote_wins(self):
        state = (
            MarketStateBuilder(NOW)
            .add_quote(quote(FIRST, -60, "100"))
            .add_quote(quote(FIRST, -10, "101"))
            .build()
        )
        assert state.quotes[FIRST].bid_price == Decimal("101")

    def test_an_older_quote_does_not_displace_a_newer_one(self):
        state = (
            MarketStateBuilder(NOW)
            .add_quote(quote(FIRST, -10, "101"))
            .add_quote(quote(FIRST, -60, "100"))
            .build()
        )
        assert state.quotes[FIRST].bid_price == Decimal("101")

    def test_stale_quotes_are_labelled_not_rejected(self):
        """Refusing to value a book because one leg is old is worse than
        valuing it with a visible age."""
        state = MarketStateBuilder(NOW).add_quote(quote(FIRST, -3600)).build()
        assert FIRST in state.quotes
        assert state.quote_age_seconds(FIRST) == pytest.approx(3600.0)

    def test_a_naive_as_of_is_rejected(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            MarketStateBuilder(datetime(2026, 9, 24, 9, 20))


class TestContentAddressing:
    def _build(self) -> MarketState:
        return (
            MarketStateBuilder(NOW)
            .add_quote(quote(FIRST, -10, "101"))
            .add_spot(FIRST, Decimal("24000"))
            .add_curve(YieldCurve.flat(0.065, NOW, "INR"))
            .add_surface(FIRST, "RAW_SVI", "surface:abc")
            .add_source("csv:test", "sha256:1")
            .build()
        )

    def test_identical_inputs_give_an_identical_id(self):
        assert self._build().state_id == self._build().state_id

    def test_a_different_quote_changes_the_id(self):
        other = (
            MarketStateBuilder(NOW)
            .add_quote(quote(FIRST, -10, "102"))
            .add_spot(FIRST, Decimal("24000"))
            .add_curve(YieldCurve.flat(0.065, NOW, "INR"))
            .add_surface(FIRST, "RAW_SVI", "surface:abc")
            .add_source("csv:test", "sha256:1")
            .build()
        )
        assert other.state_id != self._build().state_id

    def test_a_different_as_of_changes_the_id(self):
        other = MarketStateBuilder(NOW - timedelta(minutes=1)).build()
        assert other.state_id != MarketStateBuilder(NOW).build().state_id

    def test_a_different_surface_changes_the_id(self):
        other = (
            MarketStateBuilder(NOW)
            .add_quote(quote(FIRST, -10, "101"))
            .add_spot(FIRST, Decimal("24000"))
            .add_curve(YieldCurve.flat(0.065, NOW, "INR"))
            .add_surface(FIRST, "RAW_SVI", "surface:zzz")
            .add_source("csv:test", "sha256:1")
            .build()
        )
        assert other.state_id != self._build().state_id

    def test_insertion_order_does_not_change_the_id(self):
        forward = (
            MarketStateBuilder(NOW)
            .add_quote(quote(FIRST, -10))
            .add_quote(quote(SECOND, -20))
            .build()
        )
        reverse = (
            MarketStateBuilder(NOW)
            .add_quote(quote(SECOND, -20))
            .add_quote(quote(FIRST, -10))
            .build()
        )
        assert forward.state_id == reverse.state_id

    def test_the_id_is_prefixed_and_short(self):
        state_id = self._build().state_id
        assert state_id.startswith("state:")
        assert len(state_id) == len("state:") + 16


class TestImmutability:
    def test_the_mappings_cannot_be_mutated(self):
        state = MarketStateBuilder(NOW).add_quote(quote(FIRST, -10)).build()
        with pytest.raises(TypeError):
            state.quotes[SECOND] = quote(SECOND, -10)

    def test_the_state_itself_is_frozen(self):
        state = MarketStateBuilder(NOW).build()
        with pytest.raises(AttributeError):
            state.as_of = NOW

    def test_mutating_the_builder_afterwards_does_not_change_a_built_state(self):
        builder = MarketStateBuilder(NOW).add_quote(quote(FIRST, -10))
        state = builder.build()
        builder.add_quote(quote(SECOND, -10))
        assert SECOND not in state.quotes


class TestAccessors:
    def test_curve_lookup_falls_back_to_the_only_curve(self):
        curve = YieldCurve.flat(0.065, NOW, "INR")
        state = MarketStateBuilder(NOW).add_curve(curve).build()
        assert state.curve(curve.curve_id) is curve
        assert state.curve(None) is curve
        assert state.curve("curve:missing") is None

    def test_quote_age_is_none_for_an_absent_instrument(self):
        assert MarketStateBuilder(NOW).build().quote_age_seconds(FIRST) is None

    def test_provenance_names_the_state_and_its_sources(self):
        state = (
            MarketStateBuilder(NOW)
            .add_quote(quote(FIRST, -10))
            .add_source("csv:test", "sha256:1")
            .add_surface(FIRST, "RAW_SVI", "surface:abc")
            .build()
        )
        payload = state.to_provenance()
        assert payload["market_state_id"] == state.state_id
        assert payload["market_data_sources"] == ["csv:test"]
        assert payload["dataset_versions"] == {"csv:test": "sha256:1"}
        assert payload["volatility_surfaces"][0]["surface_id"] == "surface:abc"

    def test_quotes_are_omitted_from_the_summary_by_default(self):
        state = MarketStateBuilder(NOW).add_quote(quote(FIRST, -10)).build()
        assert "quotes" not in state.to_dict()
        assert "quotes" in state.to_dict(include_quotes=True)
