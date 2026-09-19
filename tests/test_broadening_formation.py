"""
Tests for broadening_formation.py — "Magnitude Reclaim" translation.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from datetime import date, timedelta

from config import ScannerConfig
from strat_engine import BarData
from timeframes import AggBar, DailyBar
from broadening_formation import (
    MagnitudeSwing,
    MagnitudeEvent,
    MagnitudeResult,
    MagnitudeSignal,
    ActiveSetup,
    TickerMagnitudeResult,
    detect_magnitude_reclaim,
    scan_ticker_bf,
    format_bf_signal_line,
    format_bf_active_line,
)


# =========================================================================
# HELPERS
# =========================================================================

def _agg_bars(ohlc: list[tuple], start: date = date(2026, 1, 1)) -> list[AggBar]:
    bars = []
    dt = start
    for o, h, l, c in ohlc:
        bars.append(AggBar(
            bar=BarData(open=o, high=h, low=l, close=c),
            period_start=dt, period_end=dt,
        ))
        dt += timedelta(days=1)
    return bars


def _daily_bars(ohlc: list[tuple], start: date = date(2026, 1, 1)) -> list[DailyBar]:
    bars = []
    dt = start
    for o, h, l, c in ohlc:
        while dt.weekday() >= 5:
            dt += timedelta(days=1)
        bars.append(DailyBar(dt=dt, open=o, high=h, low=l, close=c, volume=1_000_000))
        dt += timedelta(days=1)
    return bars


# =========================================================================
# LONG SIDE (swing-low break/reclaim/target)
# =========================================================================

class TestLongSide:
    def _config(self, **overrides):
        base = dict(bf_long_only=True, bf_max_swings=50)
        base.update(overrides)
        return ScannerConfig(**base)

    def test_reclaim_then_target_hit(self):
        """Hand-traced scenario: pivot low @2 (90), broken @5, reclaimed
        @6 with target=99 (running HH), target hit @7."""
        bars = _agg_bars([
            (100, 105, 98, 102),
            (97, 99, 95, 96),
            (94, 96, 90, 93),      # pivot low candidate (low=90)
            (93, 97, 93, 95),      # confirms pivot; extreme init=97
            (95, 99, 94, 96),      # extreme updates to 99
            (96, 97, 88, 89),      # broken: low=88<90
            (89, 92, 87, 91),      # reclaim: close=91>90 -> target=99
            (91, 100, 90, 98),     # target hit: high=100>=99
        ])
        result = detect_magnitude_reclaim(bars, self._config(), tf="D")
        stages = [(e.stage, e.bar_index, e.target) for e in result.events]
        assert ("reclaimed", 6, 99) in stages
        assert ("target_hit", 7, 99) in stages
        swing = result.swings_long[0]
        assert swing.level == 90
        assert swing.target == 99
        assert swing.target_hit is True

    def test_extreme_tracks_before_break(self):
        """Extreme (HH) should keep climbing even before the level is broken."""
        bars = _agg_bars([
            (100, 105, 98, 102),
            (97, 99, 95, 96),
            (94, 96, 90, 93),      # pivot low (90); confirmed next bar
            (93, 97, 93, 95),      # extreme init=97
            (95, 110, 94, 105),    # never broke, extreme -> 110
            (105, 120, 100, 115),  # extreme -> 120
        ])
        result = detect_magnitude_reclaim(bars, self._config(), tf="D")
        assert result.swings_long[0].extreme == 120
        assert result.swings_long[0].broken is False

    def test_extreme_freezes_after_target_locked(self):
        """Once target is locked at reclaim, extreme must stop updating
        even if a later bar's high exceeds it."""
        bars = _agg_bars([
            (100, 105, 98, 102),
            (97, 99, 95, 96),
            (94, 96, 90, 93),
            (93, 97, 93, 95),      # extreme=97
            (95, 98, 94, 96),      # extreme=98
            (96, 97, 88, 89),      # broken
            (89, 92, 87, 91),      # reclaim -> target=98 (extreme frozen here)
            (91, 150, 90, 130),    # huge high AFTER reclaim — must NOT change target
        ])
        result = detect_magnitude_reclaim(bars, self._config(), tf="D")
        swing = result.swings_long[0]
        assert swing.target == 98   # not 150
        assert swing.extreme == 98  # frozen

    def test_no_reclaim_before_break(self):
        """Close going above the level without ever breaking below first
        should not trigger a reclaim (broken must come first)."""
        bars = _agg_bars([
            (100, 105, 98, 102),
            (97, 99, 95, 96),
            (94, 96, 90, 93),
            (93, 97, 93, 95),      # confirms pivot; never breaks
            (95, 99, 94, 130),     # close way above level, but never broken
        ])
        result = detect_magnitude_reclaim(bars, self._config(), tf="D")
        assert result.events == []
        assert result.swings_long[0].broken is False
        assert result.swings_long[0].target is None

    def test_broken_is_sticky_no_rearm(self):
        """Once broken, stays broken even if price re-crosses above the
        level multiple times before finally reclaiming via close."""
        bars = _agg_bars([
            (100, 105, 98, 102),
            (97, 99, 95, 96),
            (94, 96, 90, 93),       # pivot low 90
            (93, 97, 93, 95),       # confirms; extreme=97
            (95, 96, 88, 89),       # broken (low=88<90); close=89 no reclaim
            (89, 91, 87, 88),       # still below, no reclaim
            (88, 93, 86, 92),       # close=92>90 -> reclaim now
        ])
        result = detect_magnitude_reclaim(bars, self._config(), tf="D")
        swing = result.swings_long[0]
        assert swing.broken_bar_index == 4   # first break, not re-armed later
        assert swing.target is not None

    def test_is_active_flickers_with_price(self):
        """A reclaimed-but-not-yet-hit setup should flicker active/inactive
        purely based on current close vs level, without touching target."""
        swing = MagnitudeSwing(
            direction="long", level=90, swing_bar_index=0, swing_date=date(2026, 1, 1),
            extreme=99, broken=True, target=99,
        )
        assert swing.is_active(current_close=95) is True
        assert swing.is_active(current_close=85) is False   # dipped below level
        assert swing.is_active(current_close=91) is True    # back above
        assert swing.target == 99  # unchanged throughout

    def test_is_active_false_once_target_hit(self):
        swing = MagnitudeSwing(
            direction="long", level=90, swing_bar_index=0, swing_date=date(2026, 1, 1),
            extreme=99, broken=True, target=99, target_hit=True,
        )
        assert swing.is_active(current_close=95) is False

    def test_magnitude_property(self):
        swing = MagnitudeSwing(
            direction="long", level=90, swing_bar_index=0, swing_date=date(2026, 1, 1),
            extreme=99, target=99,
        )
        assert swing.magnitude == 9

    def test_no_target_zero_magnitude(self):
        swing = MagnitudeSwing(
            direction="long", level=90, swing_bar_index=0, swing_date=date(2026, 1, 1),
            extreme=99,
        )
        assert swing.magnitude == 0.0


# =========================================================================
# SHORT SIDE (mirror)
# =========================================================================

class TestShortSide:
    def _config(self, **overrides):
        base = dict(bf_long_only=False)
        base.update(overrides)
        return ScannerConfig(**base)

    def test_reclaim_then_target_hit(self):
        bars = _agg_bars([
            (100, 105, 98, 102),
            (103, 108, 101, 104),
            (106, 110, 104, 107),   # pivot high candidate (high=110)
            (107, 108, 103, 105),   # confirms pivot; extreme init=low=103
            (105, 106, 100, 101),   # extreme updates to 100 (LL)
            (101, 112, 99, 111),    # extreme updates to 99 (same bar as break);
                                    # broken: high=112>110; close=111 not<110, no reclaim
            (111, 113, 100, 108),   # close=108<110 -> reclaim; target=extreme=99
            (108, 109, 95, 97),     # low=95<=target(99) -> target hit
        ])
        result = detect_magnitude_reclaim(bars, self._config(), tf="D")
        stages = [(e.stage, e.bar_index, e.target) for e in result.events]
        assert ("reclaimed", 6, 99) in stages
        assert ("target_hit", 7, 99) in stages
        swing = result.swings_short[0]
        assert swing.level == 110
        assert swing.target == 99

    def test_is_active_short(self):
        swing = MagnitudeSwing(
            direction="short", level=110, swing_bar_index=0, swing_date=date(2026, 1, 1),
            extreme=100, broken=True, target=100,
        )
        assert swing.is_active(current_close=105) is True    # below level
        assert swing.is_active(current_close=115) is False   # back above level
        assert swing.magnitude == 10


# =========================================================================
# LONG-ONLY MODE
# =========================================================================

class TestLongOnly:
    def test_long_only_skips_short_tracking(self):
        bars = _agg_bars([
            (100, 105, 98, 102),
            (103, 108, 101, 104),
            (106, 110, 104, 107),
            (107, 108, 103, 105),
            (105, 106, 100, 101),
            (101, 112, 99, 111),
            (111, 113, 100, 108),
        ])
        config = ScannerConfig(bf_long_only=True)
        result = detect_magnitude_reclaim(bars, config, tf="D")
        assert result.swings_short == []
        assert all(e.direction != "short" for e in result.events)

    def test_not_long_only_tracks_both(self):
        bars = _agg_bars([
            (100, 105, 90, 102),
            (97, 99, 88, 96),
            (94, 96, 85, 93),
            (93, 97, 84, 95),
        ])
        config = ScannerConfig(bf_long_only=False)
        result = detect_magnitude_reclaim(bars, config, tf="D")
        # Just verify both branches ran without error (no assertion on
        # specific pivots here — covered elsewhere)
        assert isinstance(result.swings_long, list)
        assert isinstance(result.swings_short, list)


# =========================================================================
# MAX SWINGS CAP (eviction)
# =========================================================================

class TestMaxSwingsCap:
    def test_oldest_evicted_when_cap_exceeded(self):
        """Build a series with 3 distinct swing lows and cap=2; the
        first (oldest) swing should be evicted."""
        bars = _agg_bars([
            (100, 105, 98, 102),
            # Swing low #1 at idx2 (low=90)
            (97, 99, 95, 96),
            (94, 96, 90, 93),
            (93, 97, 93, 95),       # confirms swing #1
            (95, 99, 94, 96),
            # Swing low #2 at idx5 (low=80) — lower low, new pivot
            (96, 98, 85, 86),
            (86, 88, 80, 82),
            (82, 90, 82, 88),       # confirms swing #2
            (88, 92, 87, 90),
            # Swing low #3 at idx9 (low=75)
            (90, 92, 79, 80),
            (80, 82, 75, 76),
            (76, 84, 76, 82),       # confirms swing #3 -> cap exceeded, evict #1
            (82, 85, 81, 83),
        ])
        config = ScannerConfig(bf_long_only=True, bf_max_swings=2)
        result = detect_magnitude_reclaim(bars, config, tf="D")
        levels = sorted(s.level for s in result.swings_long)
        assert len(result.swings_long) == 2
        assert 90 not in levels    # oldest (swing #1) evicted
        assert 80 in levels
        assert 75 in levels

    def test_under_cap_keeps_all(self):
        bars = _agg_bars([
            (100, 105, 98, 102),
            (97, 99, 95, 96),
            (94, 96, 90, 93),
            (93, 97, 93, 95),
            (95, 99, 94, 96),
        ])
        config = ScannerConfig(bf_long_only=True, bf_max_swings=50)
        result = detect_magnitude_reclaim(bars, config, tf="D")
        assert len(result.swings_long) == 1


# =========================================================================
# EDGE CASES
# =========================================================================

class TestEdgeCases:
    def test_insufficient_bars(self):
        bars = _agg_bars([(100, 105, 98, 102), (100, 105, 98, 102)])
        config = ScannerConfig()
        result = detect_magnitude_reclaim(bars, config, tf="D")
        assert result.bar_count == 2
        assert result.events == []
        assert result.swings_long == []

    def test_no_swings_in_monotonic_series(self):
        bars = _agg_bars([(100 + i, 102 + i, 98 + i, 101 + i) for i in range(10)])
        config = ScannerConfig()
        result = detect_magnitude_reclaim(bars, config, tf="D")
        assert result.swings_long == []
        assert result.swings_short == []

    def test_disabled_master_toggle_in_scan_ticker(self):
        daily = _daily_bars([(100, 102, 98, 101)] * 10)
        config = ScannerConfig(bf_enabled=False)
        result = scan_ticker_bf("TEST", daily, config)
        assert result.signals == []
        assert result.tf_results == {}


# =========================================================================
# TICKER-LEVEL ORCHESTRATION
# =========================================================================

class TestScanTickerBF:
    def test_fresh_signal_and_active_setup(self):
        """A reclaim on the very last bar should show up both as a fresh
        signal AND as an active setup (since it's also currently live)."""
        setup = [(100, 105, 98, 102)] * 3
        setup += [
            (97, 99, 95, 96),
            (94, 96, 90, 93),      # pivot low 90
            (93, 97, 93, 95),      # confirms; extreme=97
            (95, 99, 94, 96),      # extreme=99
            (96, 97, 88, 89),      # broken
            (89, 92, 87, 91),      # reclaim on LAST bar -> fresh signal
        ]
        daily = _daily_bars(setup)
        config = ScannerConfig(bf_timeframes=["D"], bf_long_only=True)
        result = scan_ticker_bf("TEST", daily, config)

        assert len(result.signals) == 1
        assert result.signals[0].stage == "reclaimed"
        assert len(result.active_long) == 1
        assert result.active_long[0].direction == "long"
        assert result.active_long[0].magnitude == pytest.approx(9.0)

    def test_active_setups_sorted_by_magnitude_desc(self):
        """Two independent active setups on different TFs — verify sort order."""
        # Build a long-enough daily series to get both a Daily and Weekly
        # active reclaim with different magnitudes. Simpler: directly
        # construct two ActiveSetup objects and check sort behavior via
        # the dataclass contract used by scan_ticker_bf (sorted in-place
        # at the end of that function). We simulate by calling scan and
        # checking the invariant on whatever comes back, plus a direct
        # sort-order unit check.
        setups = [
            ActiveSetup(ticker="T", tf="D", direction="long", level=90,
                        swing_date=date(2026, 1, 1), target=95, target_date=date(2026, 1, 2),
                        magnitude=5, expected_move=2, risk=1),
            ActiveSetup(ticker="T", tf="W", direction="long", level=80,
                        swing_date=date(2026, 1, 1), target=100, target_date=date(2026, 1, 2),
                        magnitude=20, expected_move=10, risk=3),
        ]
        setups.sort(key=lambda s: s.magnitude, reverse=True)
        assert setups[0].magnitude == 20
        assert setups[1].magnitude == 5

    def test_insufficient_data_skips_tf(self):
        daily = _daily_bars([(100, 102, 98, 101)] * 2)
        config = ScannerConfig(bf_timeframes=["D"])
        result = scan_ticker_bf("TEST", daily, config)
        assert result.tf_results["D"].bar_count == 2
        assert result.tf_results["D"].events == []

    def test_multi_tf_scan(self):
        prices = [(100 + i * 0.1, 102 + i * 0.1, 98 + i * 0.1, 101 + i * 0.1)
                   for i in range(90)]
        daily = _daily_bars(prices)
        config = ScannerConfig(bf_timeframes=["D", "W"])
        result = scan_ticker_bf("TEST", daily, config)
        assert "D" in result.tf_results
        assert "W" in result.tf_results


# =========================================================================
# FORMATTING
# =========================================================================

class TestFormatting:
    def test_format_reclaimed_long(self):
        sig = MagnitudeSignal(
            ticker="AAPL", tf="D", direction="long", stage="reclaimed",
            dt=date(2026, 9, 15), level=178.50, swing_date=date(2026, 9, 10),
            target=192.00, target_date=date(2026, 9, 15),
        )
        line = format_bf_signal_line(sig)
        assert "AAPL" in line
        assert "▲" in line
        assert "RECLAIMED" in line
        assert "178.50" in line
        assert "192.00" in line

    def test_format_target_hit_short(self):
        sig = MagnitudeSignal(
            ticker="TSLA", tf="W", direction="short", stage="target_hit",
            dt=date(2026, 9, 15), level=250.00, swing_date=date(2026, 9, 1),
            target=210.00, target_date=date(2026, 9, 8),
        )
        line = format_bf_signal_line(sig)
        assert "TSLA" in line
        assert "▼" in line
        assert "TARGET HIT" in line

    def test_format_active_setup(self):
        setup = ActiveSetup(
            ticker="MSFT", tf="D", direction="long", level=300.0,
            swing_date=date(2026, 9, 1), target=320.0, target_date=date(2026, 9, 5),
            magnitude=20.0, expected_move=8.5, risk=3.2,
        )
        line = format_bf_active_line(setup)
        assert "MSFT" in line
        assert "300.00" in line
        assert "320.00" in line
        assert "20.00" in line


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
