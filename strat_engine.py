"""
Core STRAT Engine — faithful translation of TheStrat Suite v3.1.1.

This module contains the pure calculation functions that classify bars,
detect Failed 2s, identify hammers/shooters, compute FTFC, and determine
magnitude/exhaustion levels.  No I/O, no side effects — pure functions
operating on OHLC data.

Reference sections in the Pine Script:
  Section 4: Pattern detection (findExhaustionLevels, isInsideBar,
             calcBarType, calcBarNum)
  Section 5: Decision logic (detectFailed2, shouldDrawC1Level, calculateFTFC)
  Section 7: computeSignalState (signal-in-force, magnitude, exhaustion,
             suppressions, stops)

Fixes applied:
  FIX CONT22-PRIOR-1: c2_was_3 guard removed from 2-2 continuation
  FIX CSS-1: flat/doji C2 treated as reversal context (not c2_closed_up)
  FIX BTC-1: momo reversal uses Reversals-FTFC, momo cont uses Cont-FTFC
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from config import ScannerConfig, FailedMethod, HammerShooterLogic


# =========================================================================
# DATA STRUCTURES
# =========================================================================

@dataclass
class BarData:
    """OHLC data for a single bar."""
    open: float
    high: float
    low: float
    close: float


@dataclass
class BarClassification:
    """Result of classifying a bar relative to the previous bar."""
    bar_type: str       # "1u", "1d", "2u", "2d", "3u", "3d"
    bar_num: str        # "1", "2", "3"
    is_inside: bool
    broke_high: bool
    broke_low: bool
    is_2u: bool
    is_2d: bool
    is_3: bool


@dataclass
class Failed2Result:
    """Result of Failed 2 detection for a single timeframe."""
    is_2u: bool = False     # CC is pure 2-Up (excludes 3-bars)
    is_2d: bool = False     # CC is pure 2-Down (excludes 3-bars)
    is_3: bool = False      # CC is a 3-bar (outside)
    is_f2u: bool = False    # confirmed Failed 2-Up (bullish breakout failed)
    is_f2d: bool = False    # confirmed Failed 2-Down (bearish breakout failed)
    pre_f2u: bool = False   # pre-confirmation Failed 2-Up
    pre_f2d: bool = False   # pre-confirmation Failed 2-Down


@dataclass
class HammerShooterResult:
    """Whether C1 is a hammer and/or shooter."""
    is_hammer: bool = False
    is_shooter: bool = False


@dataclass
class TimeframeState:
    """Complete STRAT state for one timeframe at a point in time.

    This is what the scanner computes per TF per ticker — the equivalent
    of v3.1.1's computeSignalState return minus all rendering fields.
    """
    tf: str = ""                        # "M", "W", "D", "2D", "3D"

    # Bar classification
    cc_type: str = ""                   # "1u", "1d", "2u", "2d", "3u", "3d"
    c1_type: str = ""
    c2_type: str = ""
    c1_num: str = ""
    c2_num: str = ""
    c3_num: str = ""
    cc_num: str = ""

    # Failed 2
    is_f2u: bool = False
    is_f2d: bool = False
    pre_f2u: bool = False
    pre_f2d: bool = False
    c1_was_f2: bool = False
    c2_was_f2: bool = False

    # Patterns
    is_hammer: bool = False
    is_shooter: bool = False
    c1_is_inside: bool = False
    c1_is_3: bool = False

    # Signal qualification flags
    draw_high: bool = False
    draw_low: bool = False
    signal_in_force_high: bool = False  # final (after exhaustion gate)
    signal_in_force_low: bool = False
    raw_in_force_high: bool = False     # before exhaustion gate
    raw_in_force_low: bool = False

    # Continuation tracking
    c1_is_high_continuation: bool = False
    c1_is_low_continuation: bool = False

    # Levels
    prev_high: Optional[float] = None
    prev_low: Optional[float] = None
    curr_open: Optional[float] = None

    # Magnitude / Exhaustion
    mag_high: Optional[float] = None
    mag_low: Optional[float] = None
    exh_high: Optional[float] = None
    exh_low: Optional[float] = None
    mag_high_crossed: bool = False
    mag_low_crossed: bool = False
    exh_high_crossed: bool = False
    exh_low_crossed: bool = False

    # Stop levels
    stop_high: Optional[float] = None
    stop_low: Optional[float] = None


# =========================================================================
# BAR CLASSIFICATION
# =========================================================================

def classify_bar(
    prev_high: float,
    prev_low: float,
    curr_high: float,
    curr_low: float,
    curr_open: float,
    curr_close: float,
) -> BarClassification:
    """Classify the current bar relative to the previous bar.

    - Inside (1): curr_high <= prev_high AND curr_low >= prev_low
    - 2-Up:  broke high only
    - 2-Down: broke low only
    - 3 (outside): broke both → 3u if close >= open, 3d otherwise
    """
    broke_high = curr_high > prev_high
    broke_low = curr_low < prev_low

    is_3 = broke_high and broke_low
    is_inside = not broke_high and not broke_low
    is_2u = broke_high and not broke_low
    is_2d = broke_low and not broke_high

    if is_3:
        bar_type = "3u" if curr_close >= curr_open else "3d"
        bar_num = "3"
    elif is_2u:
        bar_type = "2u"
        bar_num = "2"
    elif is_2d:
        bar_type = "2d"
        bar_num = "2"
    else:
        bar_type = "1u" if curr_close >= curr_open else "1d"
        bar_num = "1"

    return BarClassification(
        bar_type=bar_type,
        bar_num=bar_num,
        is_inside=is_inside,
        broke_high=broke_high,
        broke_low=broke_low,
        is_2u=is_2u,
        is_2d=is_2d,
        is_3=is_3,
    )


# =========================================================================
# FAILED 2 DETECTION
# =========================================================================
# Exact translation of v3.1.1 detectFailed2 (Pine Script lines 861-891).
#
# CRITICAL: No 3-bar reclassification.  F2 detection fires ONLY on pure
# 2-bars.  3-bars are explicitly excluded.
# The "Reclaim" method checks if CC's CLOSE came back inside C1's range,
# NOT whether CC's low broke below.

def detect_failed_2(
    c1_high: float,
    c1_low: float,
    cc_open: float,
    cc_close: float,
    cc_high: float,
    cc_low: float,
    method: FailedMethod = "Reclaim",
    enable_detection: bool = True,
) -> Failed2Result:
    """Detect whether the current bar (CC) is a Failed 2.

    A Failed 2 occurs when price breaks out of C1's range (making a 2-bar)
    but then reverses.  The detection method determines what counts as
    "reversed":

    - Reclaim:         CC's CLOSE came back inside C1's range
    - Open:            CC closed against the breakout direction
    - Reclaim + Open:  both conditions required
    - Reclaim OR Open: either condition sufficient
    """
    cc_is_3 = cc_high > c1_high and cc_low < c1_low
    cc_is_2u = cc_high > c1_high and cc_low >= c1_low and not cc_is_3
    cc_is_2d = cc_low < c1_low and cc_high <= c1_high and not cc_is_3

    is_f2u = False
    is_f2d = False
    pre_f2u = False
    pre_f2d = False

    # F2 ONLY fires on pure 2-bars with detection enabled
    if enable_detection and (cc_is_2u or cc_is_2d) and not cc_is_3:
        # Reclaim: CC's close is back inside C1's range
        cc_inside_c1 = cc_close <= c1_high and cc_close >= c1_low
        # Open: CC closed above its open
        cc_above_open = cc_close > cc_open

        if method == "Open":
            is_f2u = cc_is_2u and not cc_above_open
            is_f2d = cc_is_2d and cc_above_open
        elif method == "Reclaim":
            is_f2u = cc_is_2u and cc_inside_c1
            is_f2d = cc_is_2d and cc_inside_c1
        elif method == "Reclaim + Open":
            is_f2u = cc_is_2u and not cc_above_open and cc_inside_c1
            is_f2d = cc_is_2d and cc_above_open and cc_inside_c1
        else:  # "Reclaim OR Open"
            is_f2u = cc_is_2u and (not cc_above_open or cc_inside_c1)
            is_f2d = cc_is_2d and (cc_above_open or cc_inside_c1)

        # Pre-F2: for Open and Reclaim+Open methods only.
        # Fires when CC reclaimed inside C1 but close hasn't crossed open.
        if (method == "Open" or method == "Reclaim + Open") \
                and not is_f2u and not is_f2d:
            if cc_is_2u and cc_inside_c1 and cc_above_open:
                pre_f2u = True
            if cc_is_2d and cc_inside_c1 and not cc_above_open:
                pre_f2d = True

    return Failed2Result(
        is_2u=cc_is_2u,
        is_2d=cc_is_2d,
        is_3=cc_is_3,
        is_f2u=is_f2u,
        is_f2d=is_f2d,
        pre_f2u=pre_f2u,
        pre_f2d=pre_f2d,
    )


# =========================================================================
# HAMMER / SHOOTER DETECTION
# =========================================================================

def detect_hammer_shooter(
    bar: BarData,
    logic: HammerShooterLogic = "Broad (Loose)",
    match_color: bool = False,
) -> HammerShooterResult:
    """Detect if a bar is a hammer and/or shooter."""
    body_high = max(bar.open, bar.close)
    body_low = min(bar.open, bar.close)
    body_size = body_high - body_low
    bar_range = bar.high - bar.low
    upper_wick = bar.high - body_high
    lower_wick = body_low - bar.low

    if bar_range == 0:
        return HammerShooterResult()

    is_green = bar.close >= bar.open
    is_red = bar.close < bar.open
    is_hammer = False
    is_shooter = False

    if logic == "Broad (Loose)":
        body_center = (body_high + body_low) / 2.0
        bar_center = (bar.high + bar.low) / 2.0
        is_hammer = body_center > bar_center and lower_wick > upper_wick
        is_shooter = body_center < bar_center and upper_wick > lower_wick

    elif logic == "Classic":
        small_body = body_size <= bar_range / 3.0
        is_hammer = (
            small_body
            and lower_wick >= body_size * 3
            and body_low >= bar.low + bar_range * 2 / 3
        )
        is_shooter = (
            small_body
            and upper_wick >= body_size * 3
            and body_high <= bar.low + bar_range / 3
        )

    elif logic == "Pin Bar (Strict)":
        tiny_body = body_size <= bar_range / 4.0
        is_hammer = (
            tiny_body
            and lower_wick >= bar_range * 2 / 3
            and upper_wick <= bar_range / 4
        )
        is_shooter = (
            tiny_body
            and upper_wick >= bar_range * 2 / 3
            and lower_wick <= bar_range / 4
        )

    if match_color:
        if is_hammer and not is_green:
            is_hammer = False
        if is_shooter and not is_red:
            is_shooter = False

    return HammerShooterResult(is_hammer=is_hammer, is_shooter=is_shooter)


# =========================================================================
# FTFC (Full Timeframe Continuity)
# =========================================================================

def calculate_ftfc(
    tf_states: list[dict],
) -> tuple[bool, bool]:
    """Calculate Full Timeframe Continuity.

    FTFC is achieved when ALL enabled timeframes agree on direction.
    Direction is determined by close vs open for each TF's current bar.

    Returns:
        (ftfc_up, ftfc_down): True if all TFs agree bullish/bearish.
    """
    ftfc_up = True
    ftfc_down = True
    any_enabled = False

    for tf in tf_states:
        if not tf.get("enabled", False):
            continue
        cc_open = tf.get("cc_open")
        cc_close = tf.get("cc_close")
        if cc_open is None or cc_close is None:
            continue
        any_enabled = True
        if cc_close > cc_open:
            ftfc_down = False
        else:
            ftfc_up = False

    if not any_enabled:
        return False, False

    return ftfc_up, ftfc_down


# =========================================================================
# EXHAUSTION — intact swing pivot scan
# =========================================================================

def find_exhaustion_levels(
    bars: list[BarData],
) -> tuple[Optional[float], Optional[float]]:
    """Find exhaustion levels using intact swing pivot scan.

    Exact translation of v3.1.1's findExhaustionLevels.

    Scans backwards from C2 for the first intact swing pivot that exceeds
    the running max/min seeded from C1.  An "intact" pivot at index i is:
    - A local high: bars[i].high > neighbors on both sides
    - Higher than the running max of all bars from C1 through bars[i-1]

    The running max is seeded from C1 (bars[1]) and updated AFTER each
    pivot check — NOT from CC (bars[0]), to avoid repaint.

    Args:
        bars: OHLC bars indexed [0]=CC, [1]=C1, [2]=C2, ...
              Need at least 4 bars for any exhaustion scan.

    Returns:
        (exh_high, exh_low)
    """
    if len(bars) < 4:
        return None, None

    exh_high: Optional[float] = None
    exh_low: Optional[float] = None

    # Seed from C1, not CC (FIX P0-2: forming bar excluded)
    max_cur = bars[1].high
    min_cur = bars[1].low

    # Scan from C2 (i=2), need neighbors at i-1 and i+1
    max_i = min(48, len(bars) - 2)
    for i in range(2, max_i + 1):
        is_pivot_high = (bars[i].high > bars[i - 1].high
                         and bars[i].high > bars[i + 1].high)
        is_pivot_low = (bars[i].low < bars[i - 1].low
                        and bars[i].low < bars[i + 1].low)

        if exh_high is None and is_pivot_high and max_cur < bars[i].high:
            exh_high = bars[i].high
        if exh_low is None and is_pivot_low and min_cur > bars[i].low:
            exh_low = bars[i].low

        max_cur = max(max_cur, bars[i].high)
        min_cur = min(min_cur, bars[i].low)

        if exh_high is not None and exh_low is not None:
            break

    return exh_high, exh_low


# =========================================================================
# DRAW QUALIFICATION — shouldDrawC1Level priority cascade
# =========================================================================
# Exact translation of v3.1.1 shouldDrawC1Level.
# This is an if-else cascade — the FIRST matching branch determines result.

def should_draw_level(
    is_bullish: bool,
    c1_is_inside: bool,
    c1_is_hammer: bool,
    c1_is_shooter: bool,
    cc_is_2u: bool,
    cc_is_2d: bool,
    cc_is_3: bool,
    is_f2u: bool,
    is_f2d: bool,
    ftfc_up: bool,
    ftfc_down: bool,
    c1_was_2u: bool,
    c1_was_2d: bool,
    c1_is_3: bool,
    c2_closed_up: bool,
    c2_closed_down: bool,
    config: ScannerConfig,
) -> tuple[bool, bool]:
    """Determine if a C1 level should be drawn (signal qualifies).

    Returns:
        (result, is_continuation)
    """
    result = False
    is_continuation = False

    # Direction-mapped variables
    cc_is_inside = not cc_is_2u and not cc_is_2d and not cc_is_3
    ftfc_blocks = ftfc_down if is_bullish else ftfc_up
    c2_same_dir = c2_closed_up if is_bullish else c2_closed_down
    c2_opp_dir = c2_closed_down if is_bullish else c2_closed_up
    momo_pattern = c1_is_hammer if is_bullish else c1_is_shooter
    cc_in_force = cc_is_2u if is_bullish else cc_is_2d
    cc_opposite = cc_is_2d if is_bullish else cc_is_2u
    c1_was_same = c1_was_2u if is_bullish else c1_was_2d
    c1_was_opp = c1_was_2d if is_bullish else c1_was_2u
    failed_same = is_f2u if is_bullish else is_f2d
    failed_opp = is_f2d if is_bullish else is_f2u

    # Per-signal FTFC blocks
    inside_rev_ftfc_blocks = config.inside_rev_require_ftfc and ftfc_blocks
    reversals_ftfc_blocks = config.rev_22_require_ftfc and ftfc_blocks
    continuations_ftfc_blocks = config.cont_22_require_ftfc and ftfc_blocks
    inside_cont_ftfc_blocks = config.inside_cont_require_ftfc and ftfc_blocks
    expansions_ftfc_blocks = config.exp_32_require_ftfc and ftfc_blocks
    threeexp_ftfc_blocks = config.outside_require_ftfc and ftfc_blocks
    p3_ftfc_blocks = config.f2_require_ftfc and ftfc_blocks

    # Inside bar sub-classification
    inside_is_continuation = c1_is_inside and c2_same_dir
    # FIX CSS-1: flat (doji) C2 counts as reversal
    inside_is_reversal = c1_is_inside and (
        c2_opp_dir or (not c2_closed_up and not c2_closed_down)
    )

    # =================================================================
    # PRIORITY CASCADE — first matching branch wins
    # =================================================================

    if cc_is_3:
        if config.show_outside_bars and not threeexp_ftfc_blocks:
            result = True

    elif config.show_failing_2s and failed_same:
        result = not p3_ftfc_blocks

    elif config.show_failing_2s and failed_opp:
        result = not p3_ftfc_blocks

    elif c1_is_inside:
        if inside_is_reversal and config.show_inside_reversals:
            if config.inside_rev_require_ham_sho and not momo_pattern:
                result = False
            else:
                result = not inside_rev_ftfc_blocks
        elif inside_is_reversal and not config.show_inside_reversals:
            result = False
        elif inside_is_continuation and config.show_inside_continuations:
            if config.inside_cont_require_ham_sho and not momo_pattern:
                result = False
            else:
                result = not inside_cont_ftfc_blocks
                is_continuation = result
        elif inside_is_continuation and not config.show_inside_continuations:
            result = False
        else:
            # Flat C2 (doji) — FIX CSS-1: treat as reversal context
            if config.show_inside_reversals:
                if config.inside_rev_require_ham_sho and not momo_pattern:
                    result = False
                else:
                    result = not inside_rev_ftfc_blocks

    elif c1_is_3 and cc_in_force and config.show_32_expansions:
        if config.exp_32_require_ham_sho and not momo_pattern:
            result = False
        else:
            result = not expansions_ftfc_blocks
            is_continuation = result

    elif c1_is_3 and cc_is_inside and config.show_32_expansions:
        if config.exp_32_require_ham_sho and not momo_pattern:
            result = False
        else:
            result = not expansions_ftfc_blocks
            is_continuation = result

    elif momo_pattern and not (c1_is_3 and not config.show_32_expansions):
        is_momo_reversal = (
            c1_was_opp
            or (c2_opp_dir and not c1_was_same)
            or (not c1_was_2u and not c1_was_2d and not c2_same_dir)
        )
        is_momo_cont = c1_was_same or (c2_same_dir and not c1_was_opp)
        cc_is_opp_continuation = cc_opposite and c1_was_opp

        if cc_is_opp_continuation:
            result = False
        elif is_momo_reversal and not cc_opposite:
            # FIX BTC-1: momo reversal obeys Reversals-FTFC toggle
            result = not reversals_ftfc_blocks
        elif is_momo_cont and config.show_22_continuations:
            # FIX BTC-1: momo continuation obeys Cont-FTFC toggle
            result = not continuations_ftfc_blocks
            is_continuation = result

    elif c1_was_opp and cc_in_force and config.show_22_reversals:
        if momo_pattern:
            result = not reversals_ftfc_blocks
        elif config.rev_22_require_ham_sho:
            result = False
        else:
            result = not reversals_ftfc_blocks

    elif c1_was_opp and config.show_22_reversals and not cc_opposite:
        if momo_pattern:
            result = not reversals_ftfc_blocks
        elif config.rev_22_require_ham_sho:
            result = False
        else:
            result = not reversals_ftfc_blocks

    # FIX CONT22-PRIOR-1: c2_was_3 guard removed
    elif c1_was_same and cc_in_force and config.show_22_continuations:
        if config.cont_22_require_ham_sho and not momo_pattern:
            result = False
        else:
            result = not continuations_ftfc_blocks
            is_continuation = result

    elif c1_was_same and cc_is_inside and config.show_22_continuations:
        if config.cont_22_require_ham_sho and not momo_pattern:
            result = False
        else:
            result = not continuations_ftfc_blocks
            is_continuation = result

    return result, is_continuation


# =========================================================================
# SIGNAL STATE — computeSignalState
# =========================================================================

def compute_signal_state(
    bars: list[BarData],
    config: ScannerConfig,
    ftfc_up: bool,
    ftfc_down: bool,
    tf: str = "D",
) -> TimeframeState:
    """Compute the complete STRAT signal state for one timeframe.

    Args:
        bars: OHLC bars, most recent first: [0]=CC, [1]=C1, [2]=C2, ...
              Minimum 3 bars required.
        config: Scanner configuration
        ftfc_up/ftfc_down: Pre-computed FTFC across all TFs
        tf: Timeframe label
    """
    assert len(bars) >= 3, f"Need at least 3 bars, got {len(bars)}"

    cc = bars[0]
    c1 = bars[1]
    c2 = bars[2]
    c3 = bars[3] if len(bars) > 3 else None

    state = TimeframeState(tf=tf)

    # --- Classify bars ---
    cc_class = classify_bar(c1.high, c1.low, cc.high, cc.low, cc.open, cc.close)
    state.cc_type = cc_class.bar_type
    state.cc_num = cc_class.bar_num

    c1_class = classify_bar(c2.high, c2.low, c1.high, c1.low, c1.open, c1.close)
    state.c1_type = c1_class.bar_type
    state.c1_num = c1_class.bar_num
    state.c1_is_inside = c1_class.is_inside
    state.c1_is_3 = c1_class.is_3

    if c3 is not None:
        c2_class = classify_bar(c3.high, c3.low, c2.high, c2.low, c2.open, c2.close)
        state.c2_type = c2_class.bar_type
        state.c2_num = c2_class.bar_num
    else:
        state.c2_type = ""
        state.c2_num = ""

    if len(bars) > 4 and c3 is not None:
        c3_class = classify_bar(
            bars[4].high, bars[4].low, c3.high, c3.low, c3.open, c3.close,
        )
        state.c3_num = c3_class.bar_num
    else:
        state.c3_num = ""

    # --- Failed 2 detection on CC ---
    f2 = detect_failed_2(
        c1.high, c1.low, cc.open, cc.close, cc.high, cc.low,
        method=config.failed_2_method,
        enable_detection=config.enable_failed_2_detection,
    )
    cc_is_2u = f2.is_2u
    cc_is_2d = f2.is_2d
    cc_is_3 = f2.is_3
    is_f2u = f2.is_f2u
    is_f2d = f2.is_f2d
    state.is_f2u = is_f2u
    state.is_f2d = is_f2d
    state.pre_f2u = f2.pre_f2u
    state.pre_f2d = f2.pre_f2d

    # --- C1/C2 were F2? (simplified Reclaim check, always) ---
    c1_was_2u = c1_class.is_2u
    c1_was_2d = c1_class.is_2d
    c1_was_f2d = c1_was_2d and c1.close > c2.low
    c1_was_f2u = c1_was_2u and c1.close < c2.high
    state.c1_was_f2 = c1_was_f2d or c1_was_f2u

    if c3 is not None:
        c2_was_2u = c2_class.is_2u
        c2_was_2d = c2_class.is_2d
        state.c2_was_f2 = (
            (c2_was_2d and c2.close > c3.low)
            or (c2_was_2u and c2.close < c3.high)
        )
    else:
        state.c2_was_f2 = False

    # --- Hammer / Shooter on C1 ---
    ham_sho = detect_hammer_shooter(
        c1, config.hammer_shooter_logic, config.hammer_shooter_match_color,
    )
    is_hammer = ham_sho.is_hammer
    is_shooter = ham_sho.is_shooter
    state.is_hammer = is_hammer
    state.is_shooter = is_shooter

    # --- Levels ---
    state.prev_high = c1.high
    state.prev_low = c1.low
    state.curr_open = cc.open

    # --- Intermediate variables ---
    is_inside = c1_class.is_inside
    c1_is_3_flag = c1_class.is_3

    c2_closed_up = c2.close > c2.open
    c2_closed_down = c2.close < c2.open

    inside_bullish = is_inside and c2_closed_up
    inside_bearish = is_inside and c2_closed_down

    # Hammer context
    is_hammer_reversal = (
        c1_was_2d or inside_bearish
        or (not c1_was_2u and not c1_was_2d and not is_inside)
    )
    is_shooter_reversal = (
        c1_was_2u or inside_bullish
        or (not c1_was_2u and not c1_was_2d and not is_inside)
    )
    is_hammer_momo = c1_was_2u or inside_bullish
    is_shooter_momo = c1_was_2d or inside_bearish

    hammer_shows = is_hammer and (
        is_hammer_reversal
        or (is_hammer_momo and config.show_22_continuations)
    )
    shooter_shows = is_shooter and (
        is_shooter_reversal
        or (is_shooter_momo and config.show_22_continuations)
    )

    # Momo filter passes
    inside_cont_high_passes_momo = (
        not config.inside_cont_require_ham_sho or is_hammer
    )
    inside_cont_low_passes_momo = (
        not config.inside_cont_require_ham_sho or is_shooter
    )
    twotwo_cont_high_passes_momo = (
        not config.cont_22_require_ham_sho or is_hammer
    )
    twotwo_cont_low_passes_momo = (
        not config.cont_22_require_ham_sho or is_shooter
    )

    # 2-2 reversal passes
    twotwo_rev_high_passes = (
        (not config.rev_22_require_ham_sho or is_hammer)
        and (not config.rev_22_require_c1_f2 or c1_was_f2d)
    )
    twotwo_rev_low_passes = (
        (not config.rev_22_require_ham_sho or is_shooter)
        and (not config.rev_22_require_c1_f2 or c1_was_f2u)
    )

    # P3 FTFC passes
    p3_high_passes_ftfc = not config.f2_require_ftfc or ftfc_up
    p3_low_passes_ftfc = not config.f2_require_ftfc or ftfc_down

    # CC broke flags (includes 3-bars, unlike cc_is_2u)
    cc_broke_high = cc.high > c1.high
    cc_broke_low = cc.low < c1.low

    # =================================================================
    # SIGNAL IN-FORCE TERMS (seven per side, flat OR)
    # =================================================================
    # FIX CSS-1: t_insideRev uses `not c2_closed_up` (includes doji)

    # HIGH side
    t_inside_rev_h = is_inside and not c2_closed_up and cc_is_2u
    t_inside_cont_h = (
        inside_bullish and config.show_inside_continuations
        and inside_cont_high_passes_momo and cc_is_2u
    )
    t_ham_sho_h = hammer_shows and cc_broke_high
    t_rev22_h = (
        config.show_22_reversals and c1_was_2d and cc_is_2u
        and twotwo_rev_high_passes
    )
    t_cont22_h = (
        config.show_22_continuations and c1_was_2u and cc_is_2u
        and twotwo_cont_high_passes_momo
    )
    t_exp32_h = config.show_32_expansions and c1_is_3_flag and cc_is_2u
    t_f2_h = config.show_failing_2s and is_f2d and p3_high_passes_ftfc

    # LOW side
    t_inside_rev_l = is_inside and not c2_closed_down and cc_is_2d
    t_inside_cont_l = (
        inside_bearish and config.show_inside_continuations
        and inside_cont_low_passes_momo and cc_is_2d
    )
    t_ham_sho_l = shooter_shows and cc_broke_low
    t_rev22_l = (
        config.show_22_reversals and c1_was_2u and cc_is_2d
        and twotwo_rev_low_passes
    )
    t_cont22_l = (
        config.show_22_continuations and c1_was_2d and cc_is_2d
        and twotwo_cont_low_passes_momo
    )
    t_exp32_l = config.show_32_expansions and c1_is_3_flag and cc_is_2d
    t_f2_l = config.show_failing_2s and is_f2u and p3_low_passes_ftfc

    sig_in_force_high = (
        t_inside_rev_h or t_inside_cont_h or t_ham_sho_h
        or t_rev22_h or t_cont22_h or t_exp32_h or t_f2_h
    )
    sig_in_force_low = (
        t_inside_rev_l or t_inside_cont_l or t_ham_sho_l
        or t_rev22_l or t_cont22_l or t_exp32_l or t_f2_l
    )

    # =================================================================
    # SUPPRESSIONS
    # =================================================================

    three_2u_suppress_low = c1_is_3_flag and cc_is_2u and not is_f2u
    three_2d_suppress_high = c1_is_3_flag and cc_is_2d and not is_f2d

    inside_breakout_suppress_low = (
        is_inside and cc_broke_high and not is_f2u and not cc_is_3
    )
    inside_breakout_suppress_high = (
        is_inside and cc_broke_low and not is_f2d and not cc_is_3
    )

    twotwo_rev_f2_suppress_high = (
        config.rev_22_require_c1_f2 and c1_was_2d and not c1_was_f2d
        and not c1_is_3_flag and not is_inside
        and not is_f2u and not is_f2d
    )
    twotwo_rev_f2_suppress_low = (
        config.rev_22_require_c1_f2 and c1_was_2u and not c1_was_f2u
        and not c1_is_3_flag and not is_inside
        and not is_f2u and not is_f2d
    )

    # =================================================================
    # DRAW DECISION via shouldDrawC1Level CASCADE
    # =================================================================

    draw_high_result, is_high_cont = should_draw_level(
        is_bullish=True,
        c1_is_inside=is_inside, c1_is_hammer=is_hammer, c1_is_shooter=is_shooter,
        cc_is_2u=cc_is_2u, cc_is_2d=cc_is_2d, cc_is_3=cc_is_3,
        is_f2u=is_f2u, is_f2d=is_f2d,
        ftfc_up=ftfc_up, ftfc_down=ftfc_down,
        c1_was_2u=c1_was_2u, c1_was_2d=c1_was_2d,
        c1_is_3=c1_is_3_flag,
        c2_closed_up=c2_closed_up, c2_closed_down=c2_closed_down,
        config=config,
    )
    draw_low_result, is_low_cont = should_draw_level(
        is_bullish=False,
        c1_is_inside=is_inside, c1_is_hammer=is_hammer, c1_is_shooter=is_shooter,
        cc_is_2u=cc_is_2u, cc_is_2d=cc_is_2d, cc_is_3=cc_is_3,
        is_f2u=is_f2u, is_f2d=is_f2d,
        ftfc_up=ftfc_up, ftfc_down=ftfc_down,
        c1_was_2u=c1_was_2u, c1_was_2d=c1_was_2d,
        c1_is_3=c1_is_3_flag,
        c2_closed_up=c2_closed_up, c2_closed_down=c2_closed_down,
        config=config,
    )

    draw_high = (
        draw_high_result
        and not three_2d_suppress_high
        and not inside_breakout_suppress_high
        and not twotwo_rev_f2_suppress_high
    )
    draw_low = (
        draw_low_result
        and not three_2u_suppress_low
        and not inside_breakout_suppress_low
        and not twotwo_rev_f2_suppress_low
    )

    state.draw_high = draw_high
    state.draw_low = draw_low
    state.c1_is_high_continuation = is_high_cont
    state.c1_is_low_continuation = is_low_cont

    # =================================================================
    # MAGNITUDE / EXHAUSTION
    # =================================================================

    mag_high = c2.high
    mag_low = c2.low
    state.mag_high = mag_high
    state.mag_low = mag_low

    exh_high, exh_low = find_exhaustion_levels(bars)
    state.exh_high = exh_high
    state.exh_low = exh_low

    mag_high_crossed = cc.high >= mag_high
    mag_low_crossed = cc.low <= mag_low
    exh_high_crossed = exh_high is not None and cc.high >= exh_high
    exh_low_crossed = exh_low is not None and cc.low <= exh_low

    state.mag_high_crossed = mag_high_crossed
    state.mag_low_crossed = mag_low_crossed
    state.exh_high_crossed = exh_high_crossed
    state.exh_low_crossed = exh_low_crossed

    # =================================================================
    # IN-FORCE GATING
    # =================================================================

    currently_above_trigger = cc.close > c1.high
    currently_below_trigger = cc.close < c1.low

    non_f2_in_force_high = (
        sig_in_force_high and not is_f2d and currently_above_trigger
    )
    f2_in_force_high = (
        config.show_failing_2s and is_f2d
        and draw_high and p3_high_passes_ftfc
    )
    non_f2_in_force_low = (
        sig_in_force_low and not is_f2u and currently_below_trigger
    )
    f2_in_force_low = (
        config.show_failing_2s and is_f2u
        and draw_low and p3_low_passes_ftfc
    )

    raw_in_force_high = (
        (non_f2_in_force_high or f2_in_force_high) and draw_high
    )
    raw_in_force_low = (
        (non_f2_in_force_low or f2_in_force_low) and draw_low
    )

    # Exhaustion disables in-force
    mag_fallback_high = (
        exh_high is None and mag_high is not None and mag_high_crossed
    )
    mag_fallback_low = (
        exh_low is None and mag_low is not None and mag_low_crossed
    )
    exh_disables_high = config.exh_disables_in_force and (
        exh_high_crossed or mag_fallback_high
    )
    exh_disables_low = config.exh_disables_in_force and (
        exh_low_crossed or mag_fallback_low
    )

    state.signal_in_force_high = raw_in_force_high and not exh_disables_high
    state.signal_in_force_low = raw_in_force_low and not exh_disables_low
    state.raw_in_force_high = raw_in_force_high
    state.raw_in_force_low = raw_in_force_low

    # =================================================================
    # STOP LEVELS
    # =================================================================

    if config.show_stop_levels:
        use_cc = config.stop_reference == "CC"

        if raw_in_force_high:
            if is_f2d or use_cc:
                state.stop_high = cc.low
            else:
                state.stop_high = c1.low

            mag_hit_h = mag_high_crossed and mag_high is not None
            exh_hit_h = exh_high_crossed and exh_high is not None
            no_mag_h = mag_high is None
            no_exh_h = exh_high is None
            be_mag_h = config.stop_be_at_mag and (
                mag_hit_h or (no_mag_h and exh_hit_h)
            )
            be_exh_h = config.stop_be_at_exh and (
                exh_hit_h or (no_exh_h and mag_hit_h)
            )
            if be_mag_h or be_exh_h:
                state.stop_high = c1.high

        if raw_in_force_low:
            if is_f2u or use_cc:
                state.stop_low = cc.high
            else:
                state.stop_low = c1.high

            mag_hit_l = mag_low_crossed and mag_low is not None
            exh_hit_l = exh_low_crossed and exh_low is not None
            no_mag_l = mag_low is None
            no_exh_l = exh_low is None
            be_mag_l = config.stop_be_at_mag and (
                mag_hit_l or (no_mag_l and exh_hit_l)
            )
            be_exh_l = config.stop_be_at_exh and (
                exh_hit_l or (no_exh_l and mag_hit_l)
            )
            if be_mag_l or be_exh_l:
                state.stop_low = c1.low

    return state


# =========================================================================
# COMBO NOTATION
# =========================================================================

def format_combo(
    state: TimeframeState,
    use_dash: bool = True,
) -> str:
    """Format the STRAT combo notation string for a signal.

    Examples: "2d-1-2u" (inside reversal), "2u-2d" (2-2 reversal),
    "3-2u" (3-2 expansion), "F2d" (failing 2 down / bullish reclaim)
    """
    sep = "-" if use_dash else ""

    c1_display = ("F" if state.c1_was_f2 else "") + state.c1_type
    c1_num_display = ("F" if state.c1_was_f2 else "") + state.c1_num
    c2_num_display = ("F" if state.c2_was_f2 else "") + state.c2_num
    cc_type = state.cc_type

    if state.is_f2u:
        return f"{c1_display}{sep}F2u"
    elif state.is_f2d:
        return f"{c1_display}{sep}F2d"
    elif state.c1_num == "3":
        return f"3{sep}{cc_type}"
    elif state.c1_num == "1":
        if state.c2_num == "1":
            return (
                f"{state.c3_num}{sep}{c2_num_display}"
                f"{sep}{c1_num_display}{sep}{cc_type}"
            )
        else:
            return f"{c2_num_display}{sep}{c1_num_display}{sep}{cc_type}"
    else:
        c2_prefix = "3" + sep if state.c2_num == "3" else ""
        return f"{c2_prefix}{c1_display}{sep}{cc_type}"
