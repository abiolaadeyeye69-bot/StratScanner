"""
Tests for signals.py — signal classification, domino, formatting.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from datetime import date, timedelta

from config import ScannerConfig
from strat_engine import TimeframeState
from timeframes import DailyBar
from signals import (
    Signal,
    DominoSetup,
    TickerScanResult,
    scan_ticker,
    detect_domino,
    compute_panel_state,
    format_signal_line,
    format_domino_line,
    format_panel,
    _classify_signal_type,
    _signal_enabled,
    _pattern_tag,
)


# =========================================================================
# HELPERS
# =========================================================================

def _build_daily(prices: list[tuple], start: date = date(2026, 6, 1)) -> list[DailyBar]:
    """Build daily bars from (O, H, L, C) tuples, skipping weekends."""
    bars = []
    dt = start
    for o, h, l, c in prices:
        while dt.weekday() >= 5:
            dt += timedelta(days=1)
        bars.append(DailyBar(dt=dt, open=o, high=h, low=l, close=c, volume=1_000_000))
        dt += timedelta(days=1)
    return bars


# =========================================================================
# SIGNAL CLASSIFICATION
# =========================================================================

class TestClassifySignalType:
    """Test _classify_signal_type from a TimeframeState."""

    def test_inside_reversal_bullish(self):
        state = TimeframeState(
            c1_is_inside=True, c1_is_3=False,
            c1_num="1", cc_type="2u", cc_num="2",
            c2_type="2d", c2_num="2",
        )
        results = _classify_signal_type(state)
        assert len(results) == 1
        assert results[0] == ("inside_reversal", "bullish")

    def test_inside_continuation_bearish(self):
        state = TimeframeState(
            c1_is_inside=True, c1_is_3=False,
            c1_num="1", cc_type="2d", cc_num="2",
            c2_type="2d", c2_num="2",  # C2 was bearish, CC same
        )
        results = _classify_signal_type(state)
        assert len(results) == 1
        assert results[0] == ("inside_continuation", "bearish")

    def test_22_reversal(self):
        state = TimeframeState(
            c1_is_inside=False, c1_is_3=False,
            c1_type="2u", c1_num="2",
            cc_type="2d", cc_num="2",
            c2_type="2d", c2_num="2",
        )
        results = _classify_signal_type(state)
        assert ("22_reversal", "bearish") in results

    def test_32_expansion(self):
        state = TimeframeState(
            c1_is_inside=False, c1_is_3=True,
            c1_type="3u", c1_num="3",
            cc_type="2u", cc_num="2",
            c2_type="2d", c2_num="2",
        )
        results = _classify_signal_type(state)
        assert ("32_expansion", "bullish") in results

    def test_outside_bar(self):
        state = TimeframeState(
            c1_is_inside=False, c1_is_3=False,
            c1_type="2u", c1_num="2",
            cc_type="3u", cc_num="3",
            c2_type="2d", c2_num="2",
        )
        results = _classify_signal_type(state)
        assert ("outside_bar", "bullish") in results

    def test_failing_2(self):
        state = TimeframeState(
            is_f2u=True, is_f2d=False,
            c1_is_inside=False, c1_is_3=False,
            c1_type="2u", c1_num="2",
            cc_type="2u", cc_num="2",
            c2_type="2d", c2_num="2",
        )
        results = _classify_signal_type(state)
        assert ("failing_2", "bearish") in results

    def test_double_inside_bar(self):
        """C1 and CC both inside ('1-1') — a bare context pattern, not a
        directional break. Was asserted as producing NO signal before the
        bare-bar-pattern types existed; now it's an explicit neutral one."""
        state = TimeframeState(
            c1_is_inside=True, c1_is_3=False,
            c1_num="1", cc_type="1u", cc_num="1",
            c2_type="2d", c2_num="2",
        )
        results = _classify_signal_type(state)
        assert results == [("double_inside_bar", "neutral")]

    def test_inside_bar(self):
        """CC is inside, C1 was not — bare '1'."""
        state = TimeframeState(
            c1_is_inside=False, c1_is_3=False,
            c1_type="2u", c1_num="2", cc_type="1d", cc_num="1",
            c2_type="2d", c2_num="2",
        )
        results = _classify_signal_type(state)
        assert results == [("inside_bar", "neutral")]

    def test_outside_then_inside(self):
        """C1 was an outside bar, CC is inside — '3-1'."""
        state = TimeframeState(
            c1_is_inside=False, c1_is_3=True,
            c1_type="3u", c1_num="3", cc_type="1u", cc_num="1",
            c2_type="2d", c2_num="2",
        )
        results = _classify_signal_type(state)
        assert results == [("outside_then_inside", "neutral")]


# =========================================================================
# SIGNAL ENABLED
# =========================================================================

class TestSignalEnabled:
    def test_defaults(self):
        config = ScannerConfig()
        assert _signal_enabled("inside_reversal", config) is True
        assert _signal_enabled("22_reversal", config) is True
        assert _signal_enabled("inside_continuation", config) is True
        assert _signal_enabled("22_continuation", config) is False  # off by default
        assert _signal_enabled("32_expansion", config) is False
        # outside_bar / failing_2: on by default now — they're the master
        # toggles the dashboard's "1-3" and "Failed 2" family filters need.
        assert _signal_enabled("outside_bar", config) is True
        assert _signal_enabled("failing_2", config) is True
        # Bare bar patterns: on by default — see config.py's comment on
        # show_inside_bars for why (explicit user ask to scan "closed as
        # a 1", volume impact still unmeasured).
        assert _signal_enabled("inside_bar", config) is True
        assert _signal_enabled("double_inside_bar", config) is True
        assert _signal_enabled("outside_then_inside", config) is True


# =========================================================================
# PATTERN TAG (finer-grained labels for the dashboard's filter chips)
# =========================================================================

class TestPatternTag:
    def test_bare_failed_2(self):
        """C1 was a clean 2 (not inside, not outside, not itself failed)."""
        state = TimeframeState(c1_num="2", c1_was_f2=False)
        assert _pattern_tag("failing_2", state) == "failed_2"

    def test_1_failed_2(self):
        state = TimeframeState(c1_num="1", c1_was_f2=False)
        assert _pattern_tag("failing_2", state) == "1_failed_2"

    def test_3_failed_2(self):
        state = TimeframeState(c1_num="3", c1_was_f2=False)
        assert _pattern_tag("failing_2", state) == "3_failed_2"

    def test_double_failed_2(self):
        """C1 was itself a failed 2, and now CC fails again."""
        state = TimeframeState(c1_num="2", c1_was_f2=True)
        assert _pattern_tag("failing_2", state) == "double_failed_2"

    def test_1_3(self):
        """Outside bar preceded by an inside C1."""
        state = TimeframeState(c1_num="1")
        assert _pattern_tag("outside_bar", state) == "1_3"

    def test_outside_bar_generic_when_c1_not_inside(self):
        """A bare/2-3/3-3 outside bar keeps the generic label."""
        state = TimeframeState(c1_num="2")
        assert _pattern_tag("outside_bar", state) == "outside_bar"

    def test_3_1(self):
        state = TimeframeState()
        assert _pattern_tag("outside_then_inside", state) == "3_1"

    def test_passthrough_for_other_signal_types(self):
        state = TimeframeState()
        assert _pattern_tag("inside_reversal", state) == "inside_reversal"
        assert _pattern_tag("22_reversal", state) == "22_reversal"
        assert _pattern_tag("inside_bar", state) == "inside_bar"
        assert _pattern_tag("double_inside_bar", state) == "double_inside_bar"


# =========================================================================
# DOMINO
# =========================================================================

class TestDomino:
    def test_3tf_domino(self):
        states = {
            "M": TimeframeState(tf="M", c1_is_inside=True),
            "W": TimeframeState(tf="W", c1_is_inside=True),
            "D": TimeframeState(tf="D", c1_is_inside=True),
        }
        config = ScannerConfig(min_domino_tfs=2)
        domino = detect_domino(states, config)
        assert domino is not None
        assert domino.count == 3
        assert domino.inside_tfs == ["M", "W", "D"]

    def test_no_domino_single_tf(self):
        states = {
            "D": TimeframeState(tf="D", c1_is_inside=True),
        }
        config = ScannerConfig(min_domino_tfs=2)
        domino = detect_domino(states, config)
        assert domino is None

    def test_non_consecutive_tfs(self):
        """M and D inside but W is not → no consecutive run of 2."""
        states = {
            "M": TimeframeState(tf="M", c1_is_inside=True),
            "W": TimeframeState(tf="W", c1_is_inside=False),
            "D": TimeframeState(tf="D", c1_is_inside=True),
        }
        config = ScannerConfig(min_domino_tfs=2)
        domino = detect_domino(states, config)
        assert domino is None


# =========================================================================
# PANEL STATE
# =========================================================================

class TestPanelState:
    def test_panel_format(self):
        states = {
            "M": TimeframeState(tf="M", cc_type="2u"),
            "W": TimeframeState(tf="W", cc_type="1d"),
            "D": TimeframeState(tf="D", cc_type="3u"),
        }
        panel = compute_panel_state(states)
        assert panel == {"M": "2U", "W": "1", "D": "3"}


# =========================================================================
# FORMATTING
# =========================================================================

class TestFormatting:
    def test_signal_line(self):
        sig = Signal(
            ticker="AAPL", tf="D", direction="bullish",
            signal_type="inside_reversal", pattern_tag="inside_reversal",
            combo="2d-1-2u",
            trigger_level=178.50, stop_level=175.20,
            mag_level=182.00, exh_level=None,
            ftfc_aligned=True, in_force=True,
        )
        line = format_signal_line(sig)
        assert "AAPL" in line
        assert "D" in line
        assert "▲" in line
        assert "2d-1-2u" in line
        assert "178.50" in line
        assert "FTFC" in line

    def test_domino_line(self):
        domino = DominoSetup(ticker="TSLA", inside_tfs=["W", "D"], count=2)
        line = format_domino_line(domino)
        assert "TSLA" in line
        assert "W/D" in line

    def test_panel_format(self):
        panel = {"M": "2U", "W": "1", "D": "2D"}
        line = format_panel(panel)
        assert "M:2U" in line
        assert "W:1" in line
        assert "D:2D" in line


# =========================================================================
# INTEGRATION: scan_ticker
# =========================================================================

class TestScanTicker:
    def test_scan_with_clear_signal(self):
        """Full pipeline test with bars that produce a known signal."""
        # Build 70 bars of baseline + 4 bars with clear inside reversal
        prices = []
        for i in range(70):
            p = 100 + i * 0.1
            prices.append((p, p + 2, p - 1, p + 1))

        # Clear setup: C2 bearish, C1 inside, CC bullish break
        prices.append((108, 112, 106, 110))  # C2-ish
        prices.append((110, 111, 105, 106))  # C1: 2d (breaks low)
        prices.append((106, 110, 106, 109))  # C1: inside
        prices.append((109, 115, 108, 114))  # CC: 2u breaks high

        daily = _build_daily(prices)
        config = ScannerConfig(
            enabled_timeframes=["D"],
            show_inside_reversals=True,
        )
        result = scan_ticker("TEST", daily, config)

        assert result.ticker == "TEST"
        assert "D" in result.tf_states

    def test_scan_insufficient_data(self):
        """Should return empty result for < 4 bars."""
        daily = _build_daily([(100, 102, 99, 101)] * 3)
        config = ScannerConfig(enabled_timeframes=["D"])
        result = scan_ticker("TEST", daily, config)
        assert result.signals == []

    def test_scan_multi_tf(self):
        """Should produce TF states for each enabled timeframe with enough data."""
        prices = [(100 + i * 0.1, 102 + i * 0.1, 99 + i * 0.1, 101 + i * 0.1)
                   for i in range(90)]
        daily = _build_daily(prices)
        config = ScannerConfig(enabled_timeframes=["D", "W", "M"])
        result = scan_ticker("TEST", daily, config)
        assert "D" in result.tf_states
        assert "W" in result.tf_states
        assert "M" in result.tf_states


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
