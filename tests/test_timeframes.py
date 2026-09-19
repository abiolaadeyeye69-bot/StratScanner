"""
Tests for timeframes.py — aggregation correctness.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from datetime import date, timedelta
from timeframes import (
    DailyBar,
    AggBar,
    aggregate_daily,
    aggregate_weekly,
    aggregate_monthly,
    aggregate_2day,
    aggregate_3day,
    aggregate,
    extract_bar_window,
    bars_needed,
)
from strat_engine import BarData


def _make_daily(start: date, count: int, skip_weekends: bool = True) -> list[DailyBar]:
    """Generate synthetic daily bars."""
    bars = []
    dt = start
    price = 100.0
    for i in range(count):
        if skip_weekends:
            while dt.weekday() >= 5:
                dt += timedelta(days=1)
        bars.append(DailyBar(
            dt=dt, open=price, high=price + 2,
            low=price - 1, close=price + 1, volume=1_000_000,
        ))
        price += 0.5
        dt += timedelta(days=1)
    return bars


class TestAggregateDaily:
    def test_passthrough(self):
        bars = _make_daily(date(2026, 9, 1), 5)
        agg = aggregate_daily(bars)
        assert len(agg) == 5
        for i, ab in enumerate(agg):
            assert ab.bar.open == bars[i].open
            assert ab.bar_count == 1


class TestAggregateWeekly:
    def test_full_weeks(self):
        # 10 trading days = 2 full weeks
        bars = _make_daily(date(2026, 9, 7), 10)  # Monday
        agg = aggregate_weekly(bars)
        assert len(agg) == 2
        assert agg[0].bar_count == 5
        assert agg[1].bar_count == 5

    def test_partial_week_included(self):
        # 7 trading days: 1 full week + partial
        bars = _make_daily(date(2026, 9, 7), 7)
        agg = aggregate_weekly(bars)
        assert len(agg) == 2
        assert agg[0].bar_count == 5
        assert agg[1].bar_count == 2  # partial week = CC

    def test_ohlc_aggregation(self):
        """Verify O = first open, H = max high, L = min low, C = last close."""
        bars = [
            DailyBar(dt=date(2026, 9, 7), open=100, high=105, low=98, close=103, volume=100),
            DailyBar(dt=date(2026, 9, 8), open=103, high=110, low=101, close=108, volume=200),
            DailyBar(dt=date(2026, 9, 9), open=108, high=112, low=95, close=97, volume=150),
        ]
        agg = aggregate_weekly(bars)
        assert len(agg) == 1
        assert agg[0].bar.open == 100
        assert agg[0].bar.high == 112
        assert agg[0].bar.low == 95
        assert agg[0].bar.close == 97
        assert agg[0].volume == 450

    def test_empty_input(self):
        assert aggregate_weekly([]) == []


class TestAggregateMonthly:
    def test_month_boundary(self):
        # Bars spanning Aug-Sep
        bars = [
            DailyBar(dt=date(2026, 8, 28), open=100, high=102, low=99, close=101, volume=100),
            DailyBar(dt=date(2026, 8, 31), open=101, high=103, low=100, close=102, volume=100),
            DailyBar(dt=date(2026, 9, 1), open=102, high=105, low=101, close=104, volume=100),
            DailyBar(dt=date(2026, 9, 2), open=104, high=106, low=103, close=105, volume=100),
        ]
        agg = aggregate_monthly(bars)
        assert len(agg) == 2
        # Aug bars
        assert agg[0].period_start.month == 8
        assert agg[0].bar_count == 2
        # Sep bars
        assert agg[1].period_start.month == 9
        assert agg[1].bar_count == 2


class TestAggregateNDay:
    def test_2day_even(self):
        bars = _make_daily(date(2026, 9, 7), 6)
        agg = aggregate_2day(bars)
        assert len(agg) == 3
        for ab in agg:
            assert ab.bar_count == 2

    def test_2day_odd(self):
        bars = _make_daily(date(2026, 9, 7), 5)
        agg = aggregate_2day(bars)
        assert len(agg) == 3
        assert agg[-1].bar_count == 1  # last group is incomplete CC

    def test_3day(self):
        bars = _make_daily(date(2026, 9, 7), 7)
        agg = aggregate_3day(bars)
        assert len(agg) == 3
        assert agg[0].bar_count == 3
        assert agg[1].bar_count == 3
        assert agg[2].bar_count == 1


class TestExtractBarWindow:
    def test_window_4(self):
        bars = _make_daily(date(2026, 9, 1), 10)
        agg = aggregate_daily(bars)
        window = extract_bar_window(agg, 4)
        assert len(window) == 4
        # [0] = CC (most recent), [3] = C3 (oldest of window)
        assert isinstance(window[0], BarData)
        assert window[0].open == agg[-1].bar.open  # CC
        assert window[3].open == agg[-4].bar.open   # C3

    def test_window_too_large(self):
        bars = _make_daily(date(2026, 9, 1), 3)
        agg = aggregate_daily(bars)
        with pytest.raises(ValueError):
            extract_bar_window(agg, 4)


class TestAggregate:
    def test_unknown_tf(self):
        bars = _make_daily(date(2026, 9, 1), 5)
        with pytest.raises(ValueError):
            aggregate(bars, "12H")

    def test_all_valid_tfs(self):
        bars = _make_daily(date(2026, 7, 1), 60)
        for tf in ["D", "W", "M", "2D", "3D"]:
            agg = aggregate(bars, tf)
            assert len(agg) > 0


class TestBarsNeeded:
    def test_daily(self):
        assert bars_needed("D") == 4

    def test_weekly(self):
        assert bars_needed("W") == 20

    def test_monthly(self):
        assert bars_needed("M") == 84


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
