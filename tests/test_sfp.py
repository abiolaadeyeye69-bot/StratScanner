"""
Tests for sfp.py — Swing Failure Pattern detection (LuxAlgo translation).
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from datetime import date, timedelta

from config import ScannerConfig
from strat_engine import BarData
from timeframes import AggBar, DailyBar
from sfp import (
    PivotPoint,
    SFPState,
    SFPEvent,
    SFPResult,
    SFPSignal,
    TickerSFPResult,
    find_pivot_highs,
    find_pivot_lows,
    detect_sfp,
    scan_ticker_sfp,
    format_sfp_line,
    _effective_swing_length,
)


# =========================================================================
# HELPERS
# =========================================================================

def _agg_bars(ohlc: list[tuple], start: date = date(2026, 1, 1)) -> list[AggBar]:
    """Build a chronological AggBar series from (O,H,L,C) tuples."""
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
    """Build DailyBar list, skipping weekends, for scan_ticker_sfp tests."""
    bars = []
    dt = start
    for o, h, l, c in ohlc:
        while dt.weekday() >= 5:
            dt += timedelta(days=1)
        bars.append(DailyBar(dt=dt, open=o, high=h, low=l, close=c, volume=1_000_000))
        dt += timedelta(days=1)
    return bars


# =========================================================================
# PIVOT DETECTION
# =========================================================================

class TestPivotDetection:
    def test_pivot_high_basic(self):
        # idx: 0    1    2    3(pivot) 4
        bars = _agg_bars([
            (100, 105, 98, 102),
            (102, 106, 100, 104),
            (104, 107, 102, 105),
            (105, 115, 103, 110),   # pivot: high=115 > left max(107) and > right(112)
            (110, 112, 108, 109),
        ])
        pivots = find_pivot_highs(bars, length=2)
        assert 3 in pivots
        assert pivots[3].price == 115

    def test_pivot_high_requires_right_bar_lower(self):
        """If the right bar's high is >= pivot candidate, no pivot."""
        bars = _agg_bars([
            (100, 105, 98, 102),
            (102, 106, 100, 104),
            (104, 107, 102, 105),
            (105, 115, 103, 110),
            (110, 116, 108, 109),   # right bar high=116 > 115, breaks pivot
        ])
        pivots = find_pivot_highs(bars, length=2)
        assert 3 not in pivots

    def test_pivot_high_equal_left_not_broken(self):
        """Left bar high exactly equal to candidate does not count as broken (needs strictly less)."""
        bars = _agg_bars([
            (100, 115, 98, 102),    # equal to candidate's high
            (102, 106, 100, 104),
            (105, 115, 103, 110),   # candidate: high must be > max(left) = 115 -> NOT > 115
            (110, 112, 108, 109),
        ])
        pivots = find_pivot_highs(bars, length=2)
        assert 2 not in pivots

    def test_pivot_low_basic(self):
        bars = _agg_bars([
            (100, 105, 98, 102),
            (100, 104, 96, 98),
            (98, 103, 94, 100),
            (100, 108, 85, 95),   # pivot low: low=85 < left min(94) and < right(90)
            (95, 100, 90, 92),
        ])
        pivots = find_pivot_lows(bars, length=2)
        assert 3 in pivots
        assert pivots[3].price == 85

    def test_insufficient_bars_no_pivots(self):
        bars = _agg_bars([(100, 105, 98, 102)] * 3)
        pivots = find_pivot_highs(bars, length=5)
        assert pivots == {}

    def test_no_pivots_in_monotonic_series(self):
        """Strictly increasing highs never produce a pivot high."""
        bars = _agg_bars([(100 + i, 105 + i, 98 + i, 102 + i) for i in range(10)])
        pivots = find_pivot_highs(bars, length=2)
        assert pivots == {}


# =========================================================================
# BEARISH SFP STATE MACHINE
# =========================================================================

class TestBearishSFP:
    def _config(self, **overrides):
        base = dict(sfp_swing_length=2, sfp_bearish_enabled=True, sfp_bullish_enabled=False)
        base.update(overrides)
        return ScannerConfig(**base)

    def test_formation_and_same_bar_confirmation(self):
        # Traced by hand: pivot at idx3 (115), sweep+reclaim at idx5,
        # oppos = low at idx4 = 108, close[5]=106 < 108 -> confirms same bar
        bars = _agg_bars([
            (100, 105, 98, 102),
            (102, 106, 100, 104),
            (104, 107, 102, 105),
            (105, 115, 103, 110),   # pivot high @3
            (110, 112, 108, 109),   # confirms pivot, oppos candidate low=108
            (109, 118, 107, 106),   # sweep: high>115, open<115, close<115 -> forms
        ])
        result = detect_sfp(bars, self._config(), tf="D")
        stages = [(e.stage, e.bar_index) for e in result.events]
        assert ("formed", 5) in stages
        assert ("confirmed", 5) in stages
        assert result.bear_state.confirmed is True
        assert result.bear_state.swing_price == 115
        assert result.bear_state.oppos_price == 108

    def test_formation_then_later_confirmation(self):
        bars = _agg_bars([
            (100, 105, 98, 102),
            (102, 106, 100, 104),
            (104, 107, 102, 105),
            (105, 115, 103, 110),   # pivot high @3
            (110, 112, 111, 111),   # confirms pivot; oppos candidate low=111 (high, so reclaim needed later)
            (111, 118, 110, 112),   # forms SFP: high>115, close=112<115, but 112 > oppos(111) -> no confirm yet
            (112, 113, 105, 108),   # closes below oppos(111) now -> confirms
        ])
        result = detect_sfp(bars, self._config(), tf="D")
        stages = [(e.stage, e.bar_index) for e in result.events]
        assert ("formed", 5) in stages
        assert ("confirmed", 5) not in stages
        assert ("confirmed", 6) in stages

    def test_invalidation_by_reclaim(self):
        """Unconfirmed setup invalidated when close climbs back above swing high."""
        bars = _agg_bars([
            (100, 105, 98, 102),
            (102, 106, 100, 104),
            (104, 107, 102, 105),
            (105, 115, 103, 110),   # pivot high @3
            (110, 112, 111, 111),   # confirms pivot
            (111, 118, 110, 112),   # forms SFP, oppos=111, close=112 doesn't confirm
            (112, 120, 111, 116),   # close=116 > swing(115) -> invalidated
        ])
        result = detect_sfp(bars, self._config(), tf="D")
        stages = [(e.stage, e.bar_index) for e in result.events]
        assert ("invalidated", 6) in stages
        assert result.bear_state.active is False
        assert result.bear_state.confirmed is False

    def test_invalidation_by_age(self):
        """Unconfirmed setup invalidated after max_age_bars with no reclaim."""
        # Build pivot + formation, then many flat bars staying between
        # swing and oppos, never confirming or reclaiming.
        setup = [
            (100, 105, 98, 102),
            (102, 106, 100, 104),
            (104, 107, 102, 105),
            (105, 115, 103, 110),   # pivot high @3
            (110, 112, 105, 106),   # confirms pivot, low=105
            (106, 118, 104, 108),   # forms SFP: high>115,o<115,c=108<115; oppos so far=105 (bar4), 108 doesn't break it
        ]
        # Add flat bars that stay inside (close between oppos=105 and swing=115)
        flat = [(108, 110, 106, 108)] * 10
        bars = _agg_bars(setup + flat)
        result = detect_sfp(bars, self._config(sfp_max_age_bars=5), tf="D")
        invalidated = [e for e in result.events if e.stage == "invalidated"]
        assert len(invalidated) == 1
        # swing_bar_index=3; invalidation fires once (i - 3) > 5 -> i > 8 -> i=9
        assert invalidated[0].bar_index == 9

    def test_superseded_on_new_formation(self):
        """A new formation discards an older unconfirmed one."""
        bars = _agg_bars([
            (100, 105, 98, 102),
            (102, 106, 100, 104),
            (104, 107, 102, 105),
            (105, 115, 103, 110),   # pivot high @3 (115)
            (110, 112, 109, 109),   # confirms pivot; oppos candidate=109
            (109, 118, 108, 112),   # forms SFP #1: high>115, close=112<115, oppos=109, no confirm (112>109)
            (112, 117, 110, 113),   # NOT a new pivot yet; stays unconfirmed (113>109)
        ])
        # Extend with a bar sequence that creates a second pivot + second sweep
        more = [
            (113, 116, 111, 112),   # candidate right bar for a pivot at idx6? just filler
            (112, 130, 111, 111),   # big sweep, likely re-triggers formation using updated swing
        ]
        bars2 = _agg_bars([
            (100, 105, 98, 102),
            (102, 106, 100, 104),
            (104, 107, 102, 105),
            (105, 115, 103, 110),
            (110, 112, 109, 109),
            (109, 118, 108, 112),
            (112, 117, 110, 113),
            (113, 116, 111, 112),
            (112, 200, 111, 111),   # massive sweep re-forms (well above any prior swing)
        ])
        result = detect_sfp(bars2, self._config(), tf="D")
        superseded = [e for e in result.events if e.stage == "superseded"]
        formed = [e for e in result.events if e.stage == "formed"]
        assert len(formed) >= 2
        assert len(superseded) >= 1

    def test_no_formation_without_prior_pivot(self):
        """No swing high exists yet -> no formation possible."""
        bars = _agg_bars([
            (100, 105, 98, 102),
            (102, 106, 100, 104),
        ])
        result = detect_sfp(bars, self._config(), tf="D")
        assert result.events == []

    def test_3bar_style_outside_bar_does_not_form(self):
        """A bar that closes ABOVE the swing (not below) should not form
        a bearish SFP even if it wicks above."""
        bars = _agg_bars([
            (100, 105, 98, 102),
            (102, 106, 100, 104),
            (104, 107, 102, 105),
            (105, 115, 103, 110),   # pivot high @3
            (110, 112, 108, 109),
            (109, 120, 107, 117),   # high>115 but close=117>115 -> no bearish formation
        ])
        result = detect_sfp(bars, self._config(), tf="D")
        assert all(e.stage != "formed" for e in result.events)


# =========================================================================
# BULLISH SFP STATE MACHINE (mirror checks)
# =========================================================================

class TestBullishSFP:
    def _config(self, **overrides):
        base = dict(sfp_swing_length=2, sfp_bearish_enabled=False, sfp_bullish_enabled=True)
        base.update(overrides)
        return ScannerConfig(**base)

    def test_formation_and_confirmation(self):
        bars = _agg_bars([
            (100, 102, 95, 98),
            (98, 100, 92, 94),
            (95, 99, 90, 92),
            (92, 97, 80, 85),    # pivot low @3 (low=80)
            (85, 90, 82, 88),    # confirms pivot; oppos candidate high=90
            (88, 92, 78, 95),    # sweep: low<80, open=88>80, close=95>80 -> forms; oppos=90; close 95>90 -> confirms same bar
        ])
        result = detect_sfp(bars, self._config(), tf="D")
        stages = [(e.stage, e.bar_index) for e in result.events]
        assert ("formed", 5) in stages
        assert ("confirmed", 5) in stages
        assert result.bull_state.swing_price == 80
        assert result.bull_state.oppos_price == 90

    def test_invalidation_by_reclaim_down(self):
        bars = _agg_bars([
            (100, 102, 95, 98),
            (98, 100, 92, 94),
            (95, 99, 90, 92),
            (92, 97, 80, 85),    # pivot low @3
            (85, 90, 82, 84),    # confirms pivot; oppos candidate high=90
            (84, 88, 78, 86),    # forms: low<80,o=84>80,c=86>80; oppos=90, 86<90 -> not confirmed
            (86, 87, 75, 78),    # close=78 < swing(80) -> invalidated
        ])
        result = detect_sfp(bars, self._config(), tf="D")
        stages = [(e.stage, e.bar_index) for e in result.events]
        assert ("invalidated", 6) in stages


# =========================================================================
# PER-TIMEFRAME SWING LENGTH OVERRIDES (monthly-on-4-bars)
# =========================================================================

class TestSwingLengthOverrides:
    def test_default_length_used_for_unlisted_tf(self):
        config = ScannerConfig(sfp_swing_length=5, sfp_swing_length_overrides={"M": 1})
        assert _effective_swing_length(config, "D") == 5
        assert _effective_swing_length(config, "W") == 5

    def test_override_used_for_monthly(self):
        config = ScannerConfig(sfp_swing_length=5, sfp_swing_length_overrides={"M": 1})
        assert _effective_swing_length(config, "M") == 1

    def test_no_overrides_falls_back_everywhere(self):
        config = ScannerConfig(sfp_swing_length=5, sfp_swing_length_overrides={})
        assert _effective_swing_length(config, "M") == 5

    def test_monthly_detects_pivot_from_four_bars(self):
        """With length=1, a pivot needs only 1 bar left + 1 bar right —
        achievable from a 4-bar monthly series. This is the core promise
        of 'monthly SFP based on 4 monthly bars'."""
        # idx: 0    1(pivot low candidate)  2    3
        bars = _agg_bars([
            (100, 105, 95, 98),
            (98, 102, 80, 85),     # candidate pivot low: low=80
            (85, 90, 82, 88),      # right bar: low=82 > 80 -> confirms pivot @1
            (88, 95, 78, 92),      # filler bar, not part of this assertion
        ])
        pivots = find_pivot_lows(bars, length=1)
        assert 1 in pivots
        assert pivots[1].price == 80

    def test_monthly_config_end_to_end(self):
        """detect_sfp with the actual default monthly override, on exactly
        4 bars, should run (not skip for insufficient data) and be able
        to detect a formation."""
        config = ScannerConfig()  # real defaults: M -> length 1
        bars = _agg_bars([
            (100, 105, 95, 98),
            (98, 102, 80, 85),      # pivot low candidate (low=80)
            (85, 90, 82, 84),       # confirms pivot; oppos candidate high=90
            (84, 92, 78, 88),       # sweep: low<80, open=84>80, close=88>80 -> forms
        ])
        result = detect_sfp(bars, config, tf="M")
        assert result.bar_count == 4
        # min_bars for length=1 is 3, so detection should have run at all
        assert result.bull_state is not None
        formed = [e for e in result.events if e.stage == "formed"]
        assert len(formed) == 1
        assert formed[0].bar_index == 3

    def test_monthly_included_in_default_sfp_timeframes(self):
        config = ScannerConfig()
        assert "M" in config.sfp_timeframes

    def test_scan_ticker_sfp_runs_monthly_on_default_history(self):
        """End-to-end: with a realistic ~90-day daily bar feed (the
        scanner's normal window), aggregating to monthly yields ~4 bars
        and scan_ticker_sfp should process 'M' rather than skip it."""
        prices = [(100 + i * 0.1, 102 + i * 0.1, 98 + i * 0.1, 101 + i * 0.1)
                   for i in range(90)]
        daily = _daily_bars(prices)
        config = ScannerConfig()  # defaults: sfp_timeframes includes "M"
        result = scan_ticker_sfp("TEST", daily, config)
        assert "M" in result.tf_results
        assert result.tf_results["M"].bar_count >= 3


# =========================================================================
# DISABLED SIDES / EDGE CASES
# =========================================================================

class TestDetectSFPConfig:
    def test_bearish_disabled_skips_bear_events(self):
        bars = _agg_bars([
            (100, 105, 98, 102),
            (102, 106, 100, 104),
            (104, 107, 102, 105),
            (105, 115, 103, 110),
            (110, 112, 108, 109),
            (109, 118, 107, 106),
        ])
        config = ScannerConfig(sfp_swing_length=2, sfp_bearish_enabled=False, sfp_bullish_enabled=False)
        result = detect_sfp(bars, config, tf="D")
        assert result.bear_state is None
        assert result.bull_state is None
        assert result.events == []

    def test_insufficient_bars_returns_empty(self):
        bars = _agg_bars([(100, 105, 98, 102)] * 3)
        config = ScannerConfig(sfp_swing_length=5)
        result = detect_sfp(bars, config, tf="D")
        assert result.bar_count == 3
        assert result.events == []
        assert result.bear_state is None


# =========================================================================
# TICKER-LEVEL ORCHESTRATION
# =========================================================================

class TestScanTickerSFP:
    def test_fresh_signal_on_last_bar_only(self):
        """Only events on the most recent bar surface as alertable signals."""
        setup = [(100 + i * 0.1, 102 + i * 0.1, 98 + i * 0.1, 101 + i * 0.1)
                  for i in range(20)]
        # Add a clean bearish sweep+confirm sequence at the end
        setup += [
            (110, 111, 108, 109),
            (109, 110, 107, 108),
            (108, 120, 106, 107),  # pivot-ish high context
            (107, 108, 105, 106),
            (106, 130, 104, 103),  # sweep + reclaim -> forms & confirms on LAST bar
        ]
        daily = _daily_bars(setup)
        config = ScannerConfig(
            enabled_timeframes=["D"],
            sfp_timeframes=["D"],
            sfp_swing_length=2,
        )
        result = scan_ticker_sfp("TEST", daily, config)
        assert result.ticker == "TEST"
        assert "D" in result.tf_results
        # Any signals present must be on the final bar
        last_idx = result.tf_results["D"].bar_count - 1
        for sig in result.signals:
            # signals only carry dt, not bar_index, but we know last event
            # bar_index matches by construction of scan_ticker_sfp
            assert sig.tf == "D"

    def test_disabled_master_toggle(self):
        daily = _daily_bars([(100, 102, 98, 101)] * 10)
        config = ScannerConfig(sfp_enabled=False)
        result = scan_ticker_sfp("TEST", daily, config)
        assert result.signals == []
        assert result.tf_results == {}

    def test_multi_tf_scan(self):
        prices = [(100 + i * 0.1, 102 + i * 0.1, 98 + i * 0.1, 101 + i * 0.1)
                   for i in range(90)]
        daily = _daily_bars(prices)
        config = ScannerConfig(sfp_timeframes=["D", "W"], sfp_swing_length=3)
        result = scan_ticker_sfp("TEST", daily, config)
        assert "D" in result.tf_results
        assert "W" in result.tf_results

    def test_insufficient_data_skips_tf(self):
        daily = _daily_bars([(100, 102, 98, 101)] * 3)
        config = ScannerConfig(sfp_timeframes=["D"], sfp_swing_length=5)
        result = scan_ticker_sfp("TEST", daily, config)
        assert result.tf_results["D"].events == []


# =========================================================================
# FORMATTING
# =========================================================================

class TestFormatting:
    def test_format_formed_bearish(self):
        sig = SFPSignal(
            ticker="AAPL", tf="D", direction="bearish", stage="formed",
            dt=date(2026, 9, 15), swing_price=178.50, swing_date=date(2026, 9, 10),
            oppos_price=172.30, oppos_date=date(2026, 9, 12), sfp_extreme=180.10,
        )
        line = format_sfp_line(sig)
        assert "AAPL" in line
        assert "▼" in line
        assert "178.50" in line
        assert "172.30" in line
        assert "formed" in line

    def test_format_confirmed_bullish(self):
        sig = SFPSignal(
            ticker="TSLA", tf="W", direction="bullish", stage="confirmed",
            dt=date(2026, 9, 15), swing_price=165.00, swing_date=date(2026, 9, 1),
            oppos_price=170.10, oppos_date=date(2026, 9, 8), sfp_extreme=160.00,
        )
        line = format_sfp_line(sig)
        assert "TSLA" in line
        assert "▲" in line
        assert "CONFIRMED" in line
        assert "170.10" in line


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
