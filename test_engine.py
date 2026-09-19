"""
Tests for strat_engine.py — core STRAT calculation functions.

Tests cover:
  1. Bar classification (classify_bar)
  2. Failed 2 detection (detect_failed_2) — all 4 methods
  3. Hammer/shooter detection — all 3 modes
  4. FTFC calculation
  5. Exhaustion levels (find_exhaustion_levels)
  6. Signal state (compute_signal_state)
  7. Combo formatting (format_combo)
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from strat_engine import (
    BarData,
    BarClassification,
    Failed2Result,
    HammerShooterResult,
    TimeframeState,
    classify_bar,
    detect_failed_2,
    detect_hammer_shooter,
    calculate_ftfc,
    find_exhaustion_levels,
    should_draw_level,
    compute_signal_state,
    format_combo,
)
from config import ScannerConfig


# =========================================================================
# BAR CLASSIFICATION
# =========================================================================

class TestClassifyBar:
    """Test classify_bar with all bar types."""

    def test_inside_bar_green(self):
        """Bar stays within previous range, closes up."""
        result = classify_bar(
            prev_high=110, prev_low=100,
            curr_high=108, curr_low=102,
            curr_open=103, curr_close=107,
        )
        assert result.bar_type == "1u"
        assert result.bar_num == "1"
        assert result.is_inside is True
        assert result.broke_high is False
        assert result.broke_low is False

    def test_inside_bar_red(self):
        """Bar stays within previous range, closes down."""
        result = classify_bar(
            prev_high=110, prev_low=100,
            curr_high=108, curr_low=102,
            curr_open=107, curr_close=103,
        )
        assert result.bar_type == "1d"
        assert result.is_inside is True

    def test_inside_bar_doji(self):
        """Inside bar where open == close (doji)."""
        result = classify_bar(
            prev_high=110, prev_low=100,
            curr_high=108, curr_low=102,
            curr_open=105, curr_close=105,
        )
        assert result.bar_type == "1u"  # close >= open → "u"
        assert result.is_inside is True

    def test_2u_bar(self):
        """Breaks above previous high only."""
        result = classify_bar(
            prev_high=110, prev_low=100,
            curr_high=115, curr_low=102,
            curr_open=103, curr_close=113,
        )
        assert result.bar_type == "2u"
        assert result.bar_num == "2"
        assert result.is_2u is True
        assert result.is_2d is False
        assert result.is_3 is False

    def test_2d_bar(self):
        """Breaks below previous low only."""
        result = classify_bar(
            prev_high=110, prev_low=100,
            curr_high=108, curr_low=95,
            curr_open=107, curr_close=96,
        )
        assert result.bar_type == "2d"
        assert result.bar_num == "2"
        assert result.is_2d is True
        assert result.is_2u is False

    def test_3u_bar(self):
        """Outside bar (breaks both), closes green."""
        result = classify_bar(
            prev_high=110, prev_low=100,
            curr_high=115, curr_low=95,
            curr_open=98, curr_close=112,
        )
        assert result.bar_type == "3u"
        assert result.bar_num == "3"
        assert result.is_3 is True

    def test_3d_bar(self):
        """Outside bar (breaks both), closes red."""
        result = classify_bar(
            prev_high=110, prev_low=100,
            curr_high=115, curr_low=95,
            curr_open=112, curr_close=98,
        )
        assert result.bar_type == "3d"
        assert result.is_3 is True

    def test_exact_high_touch_not_break(self):
        """High exactly equals previous high — NOT a break."""
        result = classify_bar(
            prev_high=110, prev_low=100,
            curr_high=110, curr_low=102,
            curr_open=105, curr_close=108,
        )
        assert result.bar_type == "1u"
        assert result.broke_high is False

    def test_exact_low_touch_not_break(self):
        """Low exactly equals previous low — NOT a break."""
        result = classify_bar(
            prev_high=110, prev_low=100,
            curr_high=108, curr_low=100,
            curr_open=105, curr_close=103,
        )
        assert result.bar_type == "1d"
        assert result.broke_low is False


# =========================================================================
# FAILED 2 DETECTION
# =========================================================================

class TestDetectFailed2:
    """Test detect_failed_2 with all 4 methods."""

    # --- Reclaim method ---

    def test_reclaim_f2u(self):
        """2-Up bar that reclaims (closes back inside C1 range)."""
        result = detect_failed_2(
            c1_high=110, c1_low=100,
            cc_open=109, cc_close=108,     # close inside C1 range
            cc_high=115, cc_low=102,        # high breaks C1 high
            method="Reclaim",
        )
        assert result.is_2u is True
        assert result.is_f2u is True
        assert result.is_f2d is False

    def test_reclaim_f2d(self):
        """2-Down bar that reclaims (closes back inside C1 range)."""
        result = detect_failed_2(
            c1_high=110, c1_low=100,
            cc_open=101, cc_close=102,     # close inside C1 range
            cc_high=108, cc_low=95,         # low breaks C1 low
            method="Reclaim",
        )
        assert result.is_2d is True
        assert result.is_f2d is True

    def test_reclaim_no_f2_when_close_outside(self):
        """2-Up bar where close stays above C1 high — NOT a failed 2."""
        result = detect_failed_2(
            c1_high=110, c1_low=100,
            cc_open=111, cc_close=113,     # close above C1 high
            cc_high=115, cc_low=102,
            method="Reclaim",
        )
        assert result.is_2u is True
        assert result.is_f2u is False

    def test_3bar_excluded(self):
        """3-bar (outside bar) should never be flagged as F2."""
        result = detect_failed_2(
            c1_high=110, c1_low=100,
            cc_open=105, cc_close=105,
            cc_high=115, cc_low=95,         # breaks both → 3-bar
            method="Reclaim",
        )
        assert result.is_3 is True
        assert result.is_f2u is False
        assert result.is_f2d is False

    def test_disabled_detection(self):
        """When detection is disabled, no F2 flags should be set."""
        result = detect_failed_2(
            c1_high=110, c1_low=100,
            cc_open=109, cc_close=105,
            cc_high=115, cc_low=102,
            method="Reclaim",
            enable_detection=False,
        )
        assert result.is_f2u is False
        assert result.is_f2d is False

    # --- Open method ---

    def test_open_f2u(self):
        """2-Up that closes below open (bearish close despite bullish break)."""
        result = detect_failed_2(
            c1_high=110, c1_low=100,
            cc_open=112, cc_close=111,     # close < open
            cc_high=115, cc_low=102,
            method="Open",
        )
        assert result.is_f2u is True

    def test_open_f2d(self):
        """2-Down that closes above open (bullish close despite bearish break)."""
        result = detect_failed_2(
            c1_high=110, c1_low=100,
            cc_open=97, cc_close=98,       # close > open
            cc_high=108, cc_low=95,
            method="Open",
        )
        assert result.is_f2d is True

    # --- Reclaim + Open ---

    def test_reclaim_plus_open_requires_both(self):
        """Both reclaim AND open must be true."""
        # Reclaim met, Open NOT met (close > open)
        result = detect_failed_2(
            c1_high=110, c1_low=100,
            cc_open=106, cc_close=108,     # close inside C1 but > open
            cc_high=115, cc_low=102,
            method="Reclaim + Open",
        )
        assert result.is_f2u is False
        # Should be pre-F2 (reclaimed but didn't confirm with close)
        assert result.pre_f2u is True

    def test_reclaim_plus_open_both_met(self):
        """Both reclaim AND open conditions met."""
        result = detect_failed_2(
            c1_high=110, c1_low=100,
            cc_open=112, cc_close=108,     # close inside AND close < open
            cc_high=115, cc_low=102,
            method="Reclaim + Open",
        )
        assert result.is_f2u is True

    # --- Reclaim OR Open ---

    def test_reclaim_or_open_one_sufficient(self):
        """Either reclaim OR open is enough."""
        # Only Open met (close < open), no reclaim
        result = detect_failed_2(
            c1_high=110, c1_low=100,
            cc_open=113, cc_close=111,     # close < open, still above C1 high
            cc_high=115, cc_low=102,
            method="Reclaim OR Open",
        )
        assert result.is_f2u is True

    # --- Inside bar not 2 ---

    def test_inside_bar_not_f2(self):
        """Inside bar should not trigger any F2."""
        result = detect_failed_2(
            c1_high=110, c1_low=100,
            cc_open=103, cc_close=107,
            cc_high=108, cc_low=102,        # inside C1 range
            method="Reclaim",
        )
        assert result.is_2u is False
        assert result.is_2d is False
        assert result.is_f2u is False
        assert result.is_f2d is False


# =========================================================================
# HAMMER / SHOOTER
# =========================================================================

class TestHammerShooter:
    """Test detect_hammer_shooter with all three modes."""

    def test_broad_hammer(self):
        """Lower body, lower wick > upper wick → hammer in broad mode."""
        bar = BarData(open=105, high=108, low=95, close=106)
        # body center = (106+105)/2 = 105.5
        # bar center = (108+95)/2 = 101.5
        # body center > bar center → hammer candidate
        # lower wick = 105 - 95 = 10, upper wick = 108 - 106 = 2
        result = detect_hammer_shooter(bar, logic="Broad (Loose)")
        assert result.is_hammer is True
        assert result.is_shooter is False

    def test_broad_shooter(self):
        """Upper body, upper wick > lower wick → shooter in broad mode."""
        bar = BarData(open=97, high=108, low=95, close=96)
        result = detect_hammer_shooter(bar, logic="Broad (Loose)")
        assert result.is_shooter is True
        assert result.is_hammer is False

    def test_classic_hammer(self):
        """Classic hammer: small body, long lower wick, body near top."""
        bar = BarData(open=107, high=109, low=95, close=108)
        # bar range = 14, body size = 1 (≤ 14/3=4.67 ✓)
        # lower wick = 107-95 = 12 ≥ 1*3 = 3 ✓
        # body_low (107) ≥ 95 + 14*2/3 = 104.33 ✓
        result = detect_hammer_shooter(bar, logic="Classic")
        assert result.is_hammer is True

    def test_classic_shooter(self):
        """Classic shooter: small body, long upper wick, body near bottom."""
        bar = BarData(open=97, high=110, low=95, close=96)
        # bar range = 15, body size = 1 (≤ 5 ✓)
        # upper wick = 110-97 = 13 ≥ 3 ✓
        # body_high (97) ≤ 95 + 15/3 = 100 ✓
        result = detect_hammer_shooter(bar, logic="Classic")
        assert result.is_shooter is True

    def test_pin_bar_hammer(self):
        """Pin bar: tiny body, very long lower wick, no upper wick."""
        bar = BarData(open=108, high=109, low=95, close=108.5)
        # bar range = 14, body = 0.5 (≤ 14/4=3.5 ✓)
        # lower wick = 108-95 = 13 ≥ 14*2/3=9.33 ✓
        # upper wick = 109-108.5 = 0.5 ≤ 14/4=3.5 ✓
        result = detect_hammer_shooter(bar, logic="Pin Bar (Strict)")
        assert result.is_hammer is True

    def test_doji_bar(self):
        """Doji (zero range) returns neither hammer nor shooter."""
        bar = BarData(open=100, high=100, low=100, close=100)
        result = detect_hammer_shooter(bar, logic="Broad (Loose)")
        assert result.is_hammer is False
        assert result.is_shooter is False

    def test_match_color_hammer(self):
        """Hammer must be green when match_color is True."""
        bar = BarData(open=107, high=108, low=95, close=106)  # red hammer
        result = detect_hammer_shooter(bar, logic="Broad (Loose)", match_color=True)
        assert result.is_hammer is False  # blocked by red close

    def test_match_color_green_hammer(self):
        """Green hammer passes match_color filter."""
        bar = BarData(open=105, high=108, low=95, close=106)  # green
        result = detect_hammer_shooter(bar, logic="Broad (Loose)", match_color=True)
        assert result.is_hammer is True


# =========================================================================
# FTFC
# =========================================================================

class TestFTFC:
    """Test calculate_ftfc."""

    def test_all_bullish(self):
        """All TFs close above open → FTFC up."""
        states = [
            {"enabled": True, "cc_open": 100, "cc_close": 105},
            {"enabled": True, "cc_open": 200, "cc_close": 210},
            {"enabled": True, "cc_open": 300, "cc_close": 305},
        ]
        up, down = calculate_ftfc(states)
        assert up is True
        assert down is False

    def test_all_bearish(self):
        """All TFs close below open → FTFC down."""
        states = [
            {"enabled": True, "cc_open": 105, "cc_close": 100},
            {"enabled": True, "cc_open": 210, "cc_close": 200},
            {"enabled": True, "cc_open": 305, "cc_close": 300},
        ]
        up, down = calculate_ftfc(states)
        assert up is False
        assert down is True

    def test_mixed(self):
        """Mixed directions → no FTFC."""
        states = [
            {"enabled": True, "cc_open": 100, "cc_close": 105},  # up
            {"enabled": True, "cc_open": 210, "cc_close": 200},  # down
        ]
        up, down = calculate_ftfc(states)
        assert up is False
        assert down is False

    def test_disabled_tf_ignored(self):
        """Disabled TFs should not affect FTFC calculation."""
        states = [
            {"enabled": True, "cc_open": 100, "cc_close": 105},
            {"enabled": False, "cc_open": 210, "cc_close": 200},  # ignored
        ]
        up, down = calculate_ftfc(states)
        assert up is True  # only one enabled TF, it's bullish

    def test_no_enabled(self):
        """No enabled TFs → both False."""
        states = [
            {"enabled": False, "cc_open": 100, "cc_close": 105},
        ]
        up, down = calculate_ftfc(states)
        assert up is False
        assert down is False

    def test_doji_treated_as_bearish(self):
        """Close == open → not bullish (uses >, not >=)."""
        states = [
            {"enabled": True, "cc_open": 100, "cc_close": 100},
        ]
        up, down = calculate_ftfc(states)
        # close > open is False → ftfc_up breaks
        # close !> open → ftfc_down remains
        assert up is False
        assert down is True


# =========================================================================
# COMPUTE SIGNAL STATE (integration test)
# =========================================================================

class TestComputeSignalState:
    """Integration tests for compute_signal_state."""

    def _make_bars(self, *ohlc_tuples) -> list[BarData]:
        """Helper: create bars from (O,H,L,C) tuples, REVERSED order.

        Pass tuples in chronological order (oldest first); returned
        list is reversed (newest first) as the engine expects.
        """
        bars = [BarData(open=o, high=h, low=l, close=c)
                for o, h, l, c in ohlc_tuples]
        bars.reverse()
        return bars

    def test_inside_reversal_bullish(self):
        """C2 bearish, C1 inside, CC breaks high → bullish inside reversal."""
        # C3: baseline
        # C2: 2d (breaks below C3 low)
        # C1: inside (within C2)
        # CC: 2u (breaks above C1 high, which is ≤ C2 high)
        bars = self._make_bars(
            (107, 110, 104, 106),   # C3
            (106, 109, 101, 102),   # C2 — breaks below C3 low (104)
            (103, 108, 102, 106),   # C1 — inside C2 (108 ≤ 109, 102 ≥ 101)
            (106, 112, 105, 111),   # CC — breaks above C1 high (108)
        )
        config = ScannerConfig()
        state = compute_signal_state(bars, config, ftfc_up=True, ftfc_down=False)

        assert state.c1_is_inside is True
        assert state.cc_type == "2u"
        assert state.draw_high is True or state.draw_low is True

    def test_22_reversal_bearish(self):
        """C1 is 2u, CC breaks low → bearish 2-2 reversal."""
        bars = self._make_bars(
            (100, 110, 95, 105),    # C3
            (100, 108, 97, 106),    # C2
            (106, 115, 103, 112),   # C1 — 2u (breaks C2 high=108)
            (110, 113, 92, 95),     # CC — 2d (breaks C1 low=103)
        )
        config = ScannerConfig()
        state = compute_signal_state(bars, config, ftfc_up=False, ftfc_down=True)
        assert state.cc_type == "2d"
        assert state.c1_type == "2u"

    def test_failed_2_detection_in_state(self):
        """CC is a 2u that reclaims → F2u should be detected."""
        bars = self._make_bars(
            (100, 110, 95, 105),    # C3
            (100, 108, 97, 106),    # C2
            (100, 107, 97, 103),    # C1 — inside C2
            (103, 112, 100, 105),   # CC — 2u (breaks C1 high=107),
                                    #       close=105 inside C1 range
        )
        config = ScannerConfig(enable_failed_2_detection=True, failed_2_method="Reclaim")
        state = compute_signal_state(bars, config, ftfc_up=False, ftfc_down=False)
        assert state.is_f2u is True

    def test_no_f2_on_3bar(self):
        """A 3-bar should never be flagged as F2."""
        bars = self._make_bars(
            (100, 110, 95, 105),    # C3
            (100, 108, 97, 106),    # C2
            (100, 107, 97, 103),    # C1
            (103, 115, 90, 105),    # CC — 3-bar (breaks both)
        )
        config = ScannerConfig(enable_failed_2_detection=True)
        state = compute_signal_state(bars, config, ftfc_up=False, ftfc_down=False)
        assert state.is_f2u is False
        assert state.is_f2d is False


# =========================================================================
# FORMAT COMBO
# =========================================================================

class TestFormatCombo:
    """Test format_combo notation."""

    def test_inside_reversal(self):
        """Standard inside reversal: 2d-1-2u."""
        state = TimeframeState(
            c2_type="2d", c2_num="2",
            c1_type="1u", c1_num="1",
            cc_type="2u", cc_num="2",
            c1_is_inside=True,
        )
        assert format_combo(state) == "2-1-2u"

    def test_22_reversal(self):
        """2-2 reversal: 2u-2d."""
        state = TimeframeState(
            c2_type="2d", c2_num="2",
            c1_type="2u", c1_num="2",
            cc_type="2d", cc_num="2",
        )
        assert format_combo(state) == "2u-2d"

    def test_32_expansion(self):
        """3-2 expansion: 3-2u."""
        state = TimeframeState(
            c1_type="3u", c1_num="3",
            cc_type="2u", cc_num="2",
            c2_type="2d", c2_num="2",
        )
        assert format_combo(state) == "3-2u"

    def test_f2u_notation(self):
        """Failed 2-Up: shows F2u."""
        state = TimeframeState(
            c1_type="1u", c1_num="1",
            cc_type="2u", cc_num="2",
            is_f2u=True,
            c2_type="2d", c2_num="2",
        )
        assert format_combo(state) == "1u-F2u"

    def test_deep_inside_chain(self):
        """Multiple insides: c3-c2-c1-cc."""
        state = TimeframeState(
            c3_num="2",
            c2_type="1u", c2_num="1",
            c1_type="1d", c1_num="1",
            cc_type="2u", cc_num="2",
        )
        assert format_combo(state) == "2-1-1-2u"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
