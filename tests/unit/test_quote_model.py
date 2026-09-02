"""Derived quote values, and the observation/estimate separation."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from domains.market_data.models import OrderBookLevel, OrderBookSnapshot, Quote

NOW = datetime(2026, 9, 24, 9, 20, tzinfo=UTC)
INSTRUMENT = uuid.uuid4()


def quote(**overrides) -> Quote:
    kwargs = {
        "instrument_id": INSTRUMENT,
        "exchange_timestamp": NOW,
        "receive_timestamp": NOW,
        "source": "test",
        "bid_price": Decimal("100"),
        "ask_price": Decimal("102"),
        "bid_size": Decimal("10"),
        "ask_size": Decimal("30"),
        "last_price": Decimal("101.5"),
    }
    kwargs.update(overrides)
    return Quote(**kwargs)


class TestDerivedValues:
    def test_mid_spread_and_relative_spread(self):
        q = quote()
        assert q.mid_price == Decimal("101")
        assert q.spread == Decimal("2")
        assert q.relative_spread == Decimal("2") / Decimal("101")

    def test_mid_is_none_without_a_two_sided_market(self):
        assert quote(bid_price=None).mid_price is None
        assert quote(ask_price=None).mid_price is None

    def test_mid_never_falls_back_to_last_price(self):
        """Substituting a trade print for a mid would replace an observation
        with an estimate, which the platform forbids by construction."""
        one_sided = quote(ask_price=None, last_price=Decimal("101.5"))
        assert one_sided.mid_price is None

    def test_zero_bid_is_not_a_two_sided_market(self):
        assert quote(bid_price=Decimal(0)).mid_price is None

    def test_microprice_is_weighted_by_the_opposite_side(self):
        # A large resting ask (30) against a small bid (10) pulls the price down
        # toward the bid: (100*30 + 102*10) / 40 = 100.5
        assert quote().microprice == Decimal("100.5")

    def test_microprice_requires_sizes(self):
        assert quote(bid_size=None).microprice is None

    def test_crossed_and_locked_detection(self):
        assert quote(bid_price=Decimal("103")).is_crossed
        assert quote(ask_price=Decimal("100")).is_locked
        assert not quote().is_crossed
        assert not quote().is_locked

    def test_age_is_measured_from_exchange_time(self):
        q = quote(exchange_timestamp=NOW - timedelta(seconds=90))
        assert q.age_seconds(NOW) == pytest.approx(90.0)


class TestTimezoneDiscipline:
    def test_naive_timestamps_are_rejected(self):
        with pytest.raises(ValueError, match="timezone-aware"):
            quote(exchange_timestamp=datetime(2026, 9, 24, 9, 20))


class TestOrderBook:
    def _book(self, bids, asks) -> OrderBookSnapshot:
        return OrderBookSnapshot(
            instrument_id=INSTRUMENT,
            exchange_timestamp=NOW,
            receive_timestamp=NOW,
            bids=tuple(OrderBookLevel(Decimal(p), Decimal(q)) for p, q in bids),
            asks=tuple(OrderBookLevel(Decimal(p), Decimal(q)) for p, q in asks),
            source="test",
        )

    def test_levels_must_be_ordered_best_first(self):
        with pytest.raises(ValueError, match="best-first"):
            self._book([("100", "5"), ("101", "5")], [("102", "5")])
        with pytest.raises(ValueError, match="best-first"):
            self._book([("101", "5")], [("103", "5"), ("102", "5")])

    def test_imbalance(self):
        book = self._book([("100", "30")], [("102", "10")])
        assert book.imbalance() == pytest.approx((30 - 10) / 40)

    def test_imbalance_is_none_on_an_empty_book(self):
        assert self._book([], []).imbalance() is None

    def test_multi_level_imbalance(self):
        book = self._book([("100", "10"), ("99", "10")], [("102", "5"), ("103", "5")])
        assert book.imbalance(levels=2) == pytest.approx((20 - 10) / 30)
