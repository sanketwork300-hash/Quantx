"""Features that cannot see the future.

Look-ahead is the defect that makes a backtest look brilliant and be worthless,
and it is almost never introduced deliberately — it arrives through an off-by-one
in a shift, or a statistic standardised over the whole sample.

So the central test here is a *property*: computing a feature over a truncated
series must give the same value at its last bar as computing it over the whole
series. A feature that peeked would disagree, and it would disagree silently.
"""

from __future__ import annotations

import math
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from domains.research.features import (
    BarSeries,
    FeatureError,
    average_true_range,
    build,
    compute,
    ema,
    log_return,
    momentum,
    realised_volatility,
    simple_return,
    sma,
    zscore,
)

INSTRUMENT = uuid.UUID(int=5)
START = datetime(2026, 1, 1, tzinfo=UTC)


def series(closes: list[float]) -> BarSeries:
    values = [Decimal(str(round(value, 6))) for value in closes]
    return BarSeries(
        instrument_id=INSTRUMENT,
        timestamps=tuple(START + timedelta(days=index) for index in range(len(values))),
        open=tuple(values),
        high=tuple(value * Decimal("1.01") for value in values),
        low=tuple(value * Decimal("0.99") for value in values),
        close=tuple(values),
        volume=tuple(Decimal(1000) for _ in values),
    )


def wiggly(count: int = 120) -> BarSeries:
    return series([100 * (1 + 0.001 * i) + 4 * math.sin(i / 6.0) for i in range(count)])


ALL_FEATURES = [
    simple_return(),
    log_return(),
    sma(20),
    ema(20),
    realised_volatility(20),
    momentum(10),
    average_true_range(14),
    zscore(20),
]


class TestNoLookAhead:
    """The property the whole module exists to guarantee."""

    @pytest.mark.parametrize("spec", ALL_FEATURES, ids=lambda spec: spec.name)
    def test_truncating_the_series_does_not_change_past_values(self, spec):
        full = wiggly()
        whole = compute(full, [spec])

        for cut in (25, 40, 77, 119):
            truncated = compute(full.head(cut + 1), [spec])
            assert whole.values[spec.name][cut] == truncated.values[spec.name][cut], (
                f"{spec.name} at bar {cut} changed when later bars were removed, "
                "which means it was reading them"
            )

    def test_a_feature_is_none_until_it_has_its_window(self):
        """Not computed from a shorter window: a 20-day mean of three days is
        not a 20-day mean, and a strategy trading on one traded on nothing."""
        frame = compute(wiggly(30), [sma(20)])
        assert all(value is None for value in frame.values["sma_20"][:19])
        assert frame.values["sma_20"][19] is not None

    def test_the_frame_says_when_every_feature_is_ready(self):
        frame = compute(wiggly(60), [sma(20), momentum(10)])
        assert frame.ready(5) is False
        assert frame.ready(59) is True


class TestValues:
    def test_a_simple_return_is_close_over_previous_close(self):
        frame = compute(series([100, 110, 99]), [simple_return()])
        assert frame.values["return"][1] == pytest.approx(0.10)
        assert frame.values["return"][2] == pytest.approx(-0.1)

    def test_a_moving_average_includes_the_current_bar(self):
        """The convention every moving average is quoted on. A strategy
        deciding *at* this bar therefore acts on the next one, which is the
        engine's rule rather than this function's job."""
        frame = compute(series([1, 2, 3, 4, 5]), [sma(3)])
        assert frame.values["sma_3"][2] == pytest.approx(2.0)
        assert frame.values["sma_3"][4] == pytest.approx(4.0)

    def test_momentum_is_the_return_over_the_lookback(self):
        frame = compute(series([100, 101, 102, 103, 110]), [momentum(4)])
        assert frame.values["momentum_4"][4] == pytest.approx(0.10)

    def test_volatility_uses_a_sample_variance(self):
        """The mean is estimated from the same window, so the denominator is
        n-1. Using n understates dispersion, and on a short window enough to
        matter."""
        closes = [100.0]
        for index in range(1, 40):
            closes.append(closes[-1] * (1.01 if index % 2 else 0.99))
        frame = compute(series(closes), [realised_volatility(20, annualise=False)])
        value = frame.values["volatility_20_raw"][30]
        assert value is not None and 0.005 < value < 0.02

    def test_a_window_with_no_dispersion_gives_no_zscore(self):
        """Rather than a large one manufactured from rounding noise."""
        frame = compute(series([100.0] * 30), [zscore(20)])
        assert frame.values["zscore_20"][25] is None

    def test_an_ema_responds_faster_to_a_recent_move(self):
        """On a straight-line trend an EMA and an SMA have the same lag, so the
        property worth asserting is the one about *recent* information: after a
        step change the EMA is nearer the new level."""
        stepped = series([100.0] * 30 + [120.0] * 5)
        frame = compute(stepped, [ema(10), sma(10)])
        assert frame.values["ema_10"][34] > frame.values["sma_10"][34]
        assert frame.values["ema_10"][34] < 120.0

    def test_the_true_range_accounts_for_gaps(self):
        """A bar that gaps away from the previous close has a true range wider
        than its own high-low, which is the whole point of the measure."""
        gapped = series([100, 100, 100, 130])
        frame = compute(gapped, [average_true_range(1)])
        assert frame.values["atr_1"][3] > float(gapped.high[3] - gapped.low[3])


class TestTheSeriesItself:
    def test_bars_must_be_in_ascending_time_order(self):
        values = (Decimal(1), Decimal(2))
        with pytest.raises(FeatureError, match="ascending"):
            BarSeries(
                instrument_id=INSTRUMENT,
                timestamps=(START + timedelta(days=1), START),
                open=values,
                high=values,
                low=values,
                close=values,
                volume=values,
            )

    def test_naive_timestamps_are_refused(self):
        values = (Decimal(1),)
        with pytest.raises(FeatureError, match="timezone-aware"):
            BarSeries(
                instrument_id=INSTRUMENT,
                timestamps=(datetime(2026, 1, 1),),
                open=values,
                high=values,
                low=values,
                close=values,
                volume=values,
            )

    def test_ragged_columns_are_refused(self):
        with pytest.raises(FeatureError, match="different lengths"):
            BarSeries(
                instrument_id=INSTRUMENT,
                timestamps=(START,),
                open=(Decimal(1), Decimal(2)),
                high=(Decimal(1),),
                low=(Decimal(1),),
                close=(Decimal(1),),
                volume=(Decimal(1),),
            )


class TestTheRegistry:
    def test_a_feature_rebuilds_from_its_name_and_parameters(self):
        """Which is what lets a stored experiment be re-run from the record
        rather than from code that has since moved."""
        spec = build("sma", window=30)
        assert spec.name == "sma_30"

    def test_an_unknown_feature_names_what_is_available(self):
        with pytest.raises(FeatureError, match="momentum"):
            build("telepathy")
