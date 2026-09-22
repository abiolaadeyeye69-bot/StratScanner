"""
Signal Detection — multi-TF orchestration, FTFC, domino, formatting.

This module sits between the raw engine (strat_engine.py) and the
scanner loop.  It takes per-ticker daily bars, runs timeframe
aggregation, feeds bars through the engine for each TF, computes
cross-TF FTFC and domino, and produces structured signal output.

The engine functions operate on one timeframe at a time.  This module
wires them together across M/W/D/2D/3D.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from config import ScannerConfig
from strat_engine import (
    BarData,
    TimeframeState,
    compute_signal_state,
    calculate_ftfc,
    format_combo,
)
from timeframes import (
    DailyBar,
    AggBar,
    aggregate,
    extract_bar_window,
)


# =========================================================================
# SIGNAL OUTPUT STRUCTURES
# =========================================================================

@dataclass
class Signal:
    """A detected STRAT signal for one ticker on one timeframe."""
    ticker: str
    tf: str                          # "M", "W", "D", "2D", "3D"
    direction: str                   # "bullish", "bearish"
    signal_type: str                 # "inside_reversal", "22_reversal", etc.
    pattern_tag: str                 # finer label, e.g. "1_failed_2", "1_3"
    combo: str                       # STRAT notation, e.g. "2d-1-2u"
    trigger_level: Optional[float]   # C1 high or C1 low
    stop_level: Optional[float]      # stop loss level
    mag_level: Optional[float]       # magnitude target
    exh_level: Optional[float]       # exhaustion target
    is_hammer: bool = False
    is_shooter: bool = False
    ftfc_aligned: bool = False       # signal direction matches FTFC
    in_force: bool = False           # signal still in force (not exhausted)

    # Additional context
    c1_was_f2: bool = False
    mag_hit: bool = False
    exh_hit: bool = False


@dataclass
class DominoSetup:
    """A domino setup — consecutive inside bars across timeframes.

    Only fires when CC (the current/latest candle) is STILL inside on
    those timeframes — i.e. the setup hasn't broken out yet.  This is a
    live "coiled spring," not a played-out one.
    """
    ticker: str
    inside_tfs: list[str]            # which TFs are inside (e.g. ["D", "W"])
    count: int = 0                   # number of consecutive inside TFs
    tf_patterns: dict[str, str] = field(default_factory=dict)
    # ^ per-TF STRAT combo for display, e.g. {"D": "2-1", "W": "2-1"}


@dataclass
class TickerScanResult:
    """Complete scan result for one ticker across all timeframes."""
    ticker: str
    signals: list[Signal] = field(default_factory=list)
    tf_states: dict[str, TimeframeState] = field(default_factory=dict)
    domino: Optional[DominoSetup] = None
    ftfc_up: bool = False
    ftfc_down: bool = False
    panel_state: dict[str, str] = field(default_factory=dict)


# =========================================================================
# SIGNAL CLASSIFICATION
# =========================================================================

def _classify_signal_type(state: TimeframeState) -> list[tuple[str, str]]:
    """Determine what kind of STRAT signal(s) this TF state represents.

    Returns a list of (signal_type, direction) tuples.  Usually 0 or 1,
    but a bar can technically qualify for multiple patterns.

    Signal types:
      - "inside_reversal":  C1 is inside, CC breaks opposite to C2
      - "inside_continuation": C1 is inside, CC breaks same as C2
      - "22_reversal":      C1 is 2, CC breaks opposite
      - "22_continuation":  C1 is 2, CC continues same direction
      - "32_expansion":     C1 is 3, CC commits to one direction
      - "outside_bar":      CC is a 3 (outside bar)
      - "failing_2":        Failed 2 detection triggered
      - "inside_bar":       CC itself is inside, C1 was not (bare "1")
      - "double_inside_bar": CC is inside AND C1 was also inside ("1-1")
      - "outside_then_inside": C1 was an outside bar (3), CC is inside ("3-1")

    The last three are NOT directional breakout triggers — an inside bar by
    itself doesn't break either side, so there's no bullish/bearish call to
    make. They're returned with direction "neutral" rather than a guessed
    side; see the module-level note in scan_ticker() about how "neutral"
    signals are built (no trigger/stop/mag/exh — those concepts don't apply
    without a direction).
    """
    results: list[tuple[str, str]] = []

    # --- CC itself is inside (no break at all) — checked first since none
    #     of the directional branches below can ever match a cc_num of "1".
    if state.cc_num == "1":
        if state.c1_is_3:
            results.append(("outside_then_inside", "neutral"))
        elif state.c1_is_inside:
            results.append(("double_inside_bar", "neutral"))
        else:
            results.append(("inside_bar", "neutral"))
        return results

    # --- Failing 2 (highest priority, checked first) ---
    if state.is_f2u:
        # Failed 2-Up means bullish breakout failed → bearish signal
        results.append(("failing_2", "bearish"))
    if state.is_f2d:
        # Failed 2-Down means bearish breakout failed → bullish signal
        results.append(("failing_2", "bullish"))

    # --- Outside bar (CC is a 3) ---
    cc_num = state.cc_num
    if cc_num == "3":
        # Direction determined by close relative to open
        direction = "bullish" if state.cc_type.endswith("u") else "bearish"
        results.append(("outside_bar", direction))
        return results  # 3-bar subsumes other patterns

    # --- C1 was a 3 (3-2 expansion) ---
    if state.c1_is_3:
        if state.cc_type in ("2u", "3u"):
            results.append(("32_expansion", "bullish"))
        elif state.cc_type in ("2d", "3d"):
            results.append(("32_expansion", "bearish"))
        return results

    # --- C1 was inside (inside reversal or continuation) ---
    if state.c1_is_inside:
        # Need to determine C2's direction for reversal vs continuation
        c2_type = state.c2_type
        c2_was_up = c2_type.endswith("u") if c2_type else False
        c2_was_down = c2_type.endswith("d") if c2_type else False

        if state.cc_type in ("2u", "3u"):
            if c2_was_down or (not c2_was_up and not c2_was_down):
                # CC breaks high, C2 was bearish → reversal
                results.append(("inside_reversal", "bullish"))
            else:
                results.append(("inside_continuation", "bullish"))
        elif state.cc_type in ("2d", "3d"):
            if c2_was_up or (not c2_was_up and not c2_was_down):
                results.append(("inside_reversal", "bearish"))
            else:
                results.append(("inside_continuation", "bearish"))
        return results

    # --- C1 was a 2 (2-2 reversal or continuation) ---
    if state.c1_num == "2":
        c1_was_up = state.c1_type.endswith("u")
        c1_was_down = state.c1_type.endswith("d")

        if state.cc_type in ("2u", "3u"):
            if c1_was_down:
                results.append(("22_reversal", "bullish"))
            elif c1_was_up:
                results.append(("22_continuation", "bullish"))
        elif state.cc_type in ("2d", "3d"):
            if c1_was_up:
                results.append(("22_reversal", "bearish"))
            elif c1_was_down:
                results.append(("22_continuation", "bearish"))

    return results


def _pattern_tag(signal_type: str, state: TimeframeState) -> str:
    """Finer-grained pattern label than signal_type, for filtering.

    signal_type groups several distinct STRAT combos under one bucket
    ("outside_bar" covers both a bare 3 and a 1-3; "failing_2" covers a
    clean Failed 2, a 1-Failed 2, a 3-Failed 2, and a Double Failed 2).
    This splits those buckets into the exact named patterns traders
    actually filter for, using the same C1 context strat_engine already
    computed — no new detection logic, just a more specific label.
    """
    if signal_type == "outside_bar":
        # C1 was inside immediately before the outside bar → "1-3".
        # Anything else (bare 3, 2-3, 3-3, ...) stays the generic label.
        return "1_3" if state.c1_num == "1" else "outside_bar"

    if signal_type == "failing_2":
        if state.c1_num == "1":
            return "1_failed_2"
        if state.c1_num == "3":
            return "3_failed_2"
        if state.c1_was_f2:
            # C1 was itself a failed 2, and now CC fails again.
            return "double_failed_2"
        return "failed_2"

    if signal_type == "outside_then_inside":
        # Same "N_M" naming as 1_3 above, for a consistent flat tag space.
        return "3_1"

    return signal_type


def _signal_enabled(signal_type: str, config: ScannerConfig) -> bool:
    """Check if a signal type is enabled in config."""
    return {
        "inside_reversal":     config.show_inside_reversals,
        "inside_continuation": config.show_inside_continuations,
        "22_reversal":         config.show_22_reversals,
        "22_continuation":     config.show_22_continuations,
        "32_expansion":        config.show_32_expansions,
        "outside_bar":         config.show_outside_bars,
        "failing_2":           config.show_failing_2s,
        "inside_bar":          config.show_inside_bars,
        "double_inside_bar":   config.show_double_inside_bars,
        "outside_then_inside": config.show_outside_then_inside,
    }.get(signal_type, False)


# =========================================================================
# DOMINO DETECTION
# =========================================================================

# Timeframe ordering for domino — lowest to highest resolution.
# Domino requires CONSECUTIVE inside bars starting from a lower TF.
TF_ORDER = ["M", "W", "D", "2D", "3D"]


def detect_domino(
    tf_states: dict[str, TimeframeState],
    config: ScannerConfig,
) -> Optional[DominoSetup]:
    """Detect domino setup: consecutive inside bars across timeframes.

    A domino occurs when multiple timeframes currently have CC (the
    latest candle) as an inside bar — the setup is LIVE and hasn't
    broken out yet.  This is the "coiled spring" that's still coiling.

    Previous logic checked c1_is_inside (C1 was inside), which meant
    CC had already broken the range and the domino had played out.

    Args:
        tf_states: computed TimeframeState per TF
        config: scanner config (min_domino_tfs)

    Returns:
        DominoSetup if detected, None otherwise.
    """
    inside_tfs: list[str] = []

    # Check enabled TFs in order — CC must be a 1-bar (still inside)
    for tf in TF_ORDER:
        if tf in tf_states:
            state = tf_states[tf]
            if state.cc_num == "1":
                inside_tfs.append(tf)

    # A domino requires at least min_domino_tfs consecutive inside-bar TFs
    # "Consecutive" here means contiguous in the TF_ORDER sequence
    if len(inside_tfs) >= config.min_domino_tfs:
        # Check for consecutive run in TF_ORDER
        indices = [TF_ORDER.index(tf) for tf in inside_tfs]
        max_consecutive = 1
        current_run = 1
        consecutive_start = 0

        for i in range(1, len(indices)):
            if indices[i] == indices[i - 1] + 1:
                current_run += 1
                if current_run > max_consecutive:
                    max_consecutive = current_run
                    consecutive_start = i - current_run + 1
            else:
                current_run = 1

        if max_consecutive >= config.min_domino_tfs:
            consecutive_tfs = inside_tfs[
                consecutive_start : consecutive_start + max_consecutive
            ]
            # Build per-TF pattern string (e.g. "2-1", "3-1", "1-1")
            tf_patterns: dict[str, str] = {}
            for tf in consecutive_tfs:
                state = tf_states[tf]
                c1_label = state.c1_num or "?"
                tf_patterns[tf] = f"{c1_label}-1"

            return DominoSetup(
                ticker="",  # filled by caller
                inside_tfs=consecutive_tfs,
                count=max_consecutive,
                tf_patterns=tf_patterns,
            )

    return None


# =========================================================================
# PANEL STATE (STRAT status table)
# =========================================================================

def compute_panel_state(
    tf_states: dict[str, TimeframeState],
) -> dict[str, str]:
    """Compute the STRAT panel state for each timeframe.

    Returns a dict of TF → bar number string (e.g. "1", "2U", "2D", "3").
    This is equivalent to the TF panel in v3.1.1.
    """
    panel: dict[str, str] = {}
    for tf, state in tf_states.items():
        cc_type = state.cc_type
        if state.is_f2u:
            panel[tf] = "F2U"
        elif state.is_f2d:
            panel[tf] = "F2D"
        elif cc_type.startswith("1"):
            panel[tf] = "1"
        elif cc_type == "2u":
            panel[tf] = "2U"
        elif cc_type == "2d":
            panel[tf] = "2D"
        elif cc_type == "3u":
            panel[tf] = "3U"
        elif cc_type == "3d":
            panel[tf] = "3D"
        else:
            panel[tf] = "?"
    return panel


# =========================================================================
# MAIN SCAN FUNCTION
# =========================================================================

def scan_ticker(
    ticker: str,
    daily_bars: list[DailyBar],
    config: ScannerConfig,
) -> TickerScanResult:
    """Run the full STRAT scan for one ticker across all enabled timeframes.

    This is the main orchestration function:
    1. Aggregate daily bars into each enabled TF
    2. Extract bar windows for the engine
    3. Pre-compute FTFC across all TFs
    4. Run compute_signal_state for each TF
    5. Classify signals, apply config filters
    6. Detect domino setups
    7. Package everything into TickerScanResult

    Args:
        ticker: stock ticker symbol
        daily_bars: chronologically sorted daily bars (oldest first)
        config: scanner configuration

    Returns:
        TickerScanResult with all detected signals and TF states.
    """
    result = TickerScanResult(ticker=ticker)

    if len(daily_bars) < 4:
        return result  # not enough data

    # -----------------------------------------------------------------
    # Step 1: Aggregate into each enabled TF
    # -----------------------------------------------------------------
    tf_agg_bars: dict[str, list[AggBar]] = {}
    for tf in config.enabled_timeframes:
        try:
            agg = aggregate(daily_bars, tf)
            if len(agg) >= 4:  # need CC, C1, C2, C3 minimum
                tf_agg_bars[tf] = agg
        except (ValueError, Exception):
            continue  # skip TFs with insufficient data

    if not tf_agg_bars:
        return result

    # -----------------------------------------------------------------
    # Step 2: Extract bar windows
    # -----------------------------------------------------------------
    tf_windows: dict[str, list[BarData]] = {}
    for tf, agg in tf_agg_bars.items():
        try:
            # Extract up to 20 bars for exhaustion scan depth
            window_size = min(len(agg), 20)
            tf_windows[tf] = extract_bar_window(agg, window_size)
        except ValueError:
            continue

    if not tf_windows:
        return result

    # -----------------------------------------------------------------
    # Step 3: Pre-compute FTFC
    # -----------------------------------------------------------------
    # Build the list of TF state dicts that calculate_ftfc expects
    ftfc_inputs: list[dict] = []
    for tf, window in tf_windows.items():
        cc = window[0]  # current candle
        ftfc_inputs.append({
            "enabled": True,
            "cc_open": cc.open,
            "cc_close": cc.close,
        })

    ftfc_up, ftfc_down = calculate_ftfc(ftfc_inputs)
    result.ftfc_up = ftfc_up
    result.ftfc_down = ftfc_down

    # -----------------------------------------------------------------
    # Step 4: Run engine for each TF
    # -----------------------------------------------------------------
    for tf, window in tf_windows.items():
        state = compute_signal_state(
            bars=window,
            config=config,
            ftfc_up=ftfc_up,
            ftfc_down=ftfc_down,
            tf=tf,
        )
        result.tf_states[tf] = state

    # -----------------------------------------------------------------
    # Step 5: Classify signals, apply filters
    # -----------------------------------------------------------------
    for tf, state in result.tf_states.items():
        signal_types = _classify_signal_type(state)

        for signal_type, direction in signal_types:
            # Check if this signal type is enabled
            if not _signal_enabled(signal_type, config):
                continue

            # Apply hammer/shooter requirement filters
            if _requires_hammer_shooter(signal_type, config):
                if direction == "bullish" and not state.is_hammer:
                    continue
                if direction == "bearish" and not state.is_shooter:
                    continue

            # Apply FTFC requirement filters
            if _requires_ftfc(signal_type, config):
                if direction == "bullish" and not ftfc_up:
                    continue
                if direction == "bearish" and not ftfc_down:
                    continue

            # Apply C1 F2 requirement for 2-2 reversals
            if signal_type == "22_reversal" and config.rev_22_require_c1_f2:
                if not state.c1_was_f2:
                    continue

            # Build the signal
            combo = format_combo(state)
            is_bullish = direction == "bullish"
            is_neutral = direction == "neutral"

            # A neutral pattern (bare inside bar, double inside, 3-1) has no
            # directional break, so trigger/stop/mag/exh/FTFC-alignment/
            # in-force don't apply — those are all "which side did price
            # commit to" concepts. Leave them None/False rather than
            # defaulting to the bearish-side values, which would be a wrong
            # answer dressed up as data.
            signal = Signal(
                ticker=ticker,
                tf=tf,
                direction=direction,
                signal_type=signal_type,
                pattern_tag=_pattern_tag(signal_type, state),
                combo=combo,
                trigger_level=(
                    None if is_neutral
                    else state.prev_high if is_bullish else state.prev_low
                ),
                stop_level=(
                    None if is_neutral
                    else state.stop_low if is_bullish else state.stop_high
                ),
                mag_level=(
                    None if is_neutral
                    else state.mag_high if is_bullish else state.mag_low
                ),
                exh_level=(
                    None if is_neutral
                    else state.exh_high if is_bullish else state.exh_low
                ),
                is_hammer=state.is_hammer,
                is_shooter=state.is_shooter,
                ftfc_aligned=(
                    False if is_neutral else
                    (is_bullish and ftfc_up)
                    or (not is_bullish and ftfc_down)
                ),
                in_force=(
                    False if is_neutral else
                    state.signal_in_force_high if is_bullish
                    else state.signal_in_force_low
                ),
                c1_was_f2=state.c1_was_f2,
                mag_hit=(
                    False if is_neutral else
                    state.mag_high_crossed if is_bullish
                    else state.mag_low_crossed
                ),
                exh_hit=(
                    False if is_neutral else
                    state.exh_high_crossed if is_bullish
                    else state.exh_low_crossed
                ),
            )
            result.signals.append(signal)

    # -----------------------------------------------------------------
    # Step 6: Detect domino
    # -----------------------------------------------------------------
    if config.enable_domino_alerts:
        domino = detect_domino(result.tf_states, config)
        if domino is not None:
            domino.ticker = ticker
            result.domino = domino

    # -----------------------------------------------------------------
    # Step 7: Panel state
    # -----------------------------------------------------------------
    if config.include_panel_state:
        result.panel_state = compute_panel_state(result.tf_states)

    return result


# =========================================================================
# FILTER HELPERS
# =========================================================================

def _requires_hammer_shooter(signal_type: str, config: ScannerConfig) -> bool:
    """Check if this signal type requires a hammer/shooter C1."""
    return {
        "inside_reversal":     config.inside_rev_require_ham_sho,
        "inside_continuation": config.inside_cont_require_ham_sho,
        "22_reversal":         config.rev_22_require_ham_sho,
        "22_continuation":     config.cont_22_require_ham_sho,
        "32_expansion":        config.exp_32_require_ham_sho,
        "outside_bar":         False,
        "failing_2":           False,
    }.get(signal_type, False)


def _requires_ftfc(signal_type: str, config: ScannerConfig) -> bool:
    """Check if this signal type requires FTFC alignment."""
    return {
        "inside_reversal":     config.inside_rev_require_ftfc,
        "inside_continuation": config.inside_cont_require_ftfc,
        "22_reversal":         config.rev_22_require_ftfc,
        "22_continuation":     config.cont_22_require_ftfc,
        "32_expansion":        config.exp_32_require_ftfc,
        "outside_bar":         config.outside_require_ftfc,
        "failing_2":           config.f2_require_ftfc,
    }.get(signal_type, False)


# =========================================================================
# MARKET / SECTOR SUMMARY
# =========================================================================

@dataclass
class MarketSummary:
    """Summary of a market or sector ETF's STRAT state."""
    ticker: str
    panel_state: dict[str, str]      # TF → bar number
    ftfc_up: bool
    ftfc_down: bool
    signals: list[Signal]
    domino: Optional[DominoSetup]


def compute_market_summary(
    etf_results: dict[str, TickerScanResult],
) -> list[MarketSummary]:
    """Compute market/sector summary from ETF scan results.

    Args:
        etf_results: ticker → TickerScanResult for market/sector ETFs

    Returns:
        List of MarketSummary, one per ETF.
    """
    summaries: list[MarketSummary] = []
    for ticker, result in sorted(etf_results.items()):
        summaries.append(MarketSummary(
            ticker=ticker,
            panel_state=result.panel_state,
            ftfc_up=result.ftfc_up,
            ftfc_down=result.ftfc_down,
            signals=result.signals,
            domino=result.domino,
        ))
    return summaries


# =========================================================================
# SIGNAL FORMATTING
# =========================================================================

def format_signal_line(signal: Signal) -> str:
    """Format a single signal as a compact text line for alerts.

    Example: "AAPL D ▲ 2d-1-2u (Inside Rev) | Trigger: 178.50 Stop: 175.20 Mag: 182.00"
    """
    arrow = (
        "▲" if signal.direction == "bullish"
        else "▼" if signal.direction == "bearish"
        else "◆"
    )
    label = _signal_type_label(signal.signal_type)

    parts = [f"{signal.ticker} {signal.tf} {arrow} {signal.combo} ({label})"]

    if signal.trigger_level is not None:
        parts.append(f"Trigger: {signal.trigger_level:.2f}")
    if signal.stop_level is not None:
        parts.append(f"Stop: {signal.stop_level:.2f}")
    if signal.mag_level is not None:
        parts.append(f"Mag: {signal.mag_level:.2f}")
    if signal.exh_level is not None:
        parts.append(f"Exh: {signal.exh_level:.2f}")

    extras: list[str] = []
    if signal.is_hammer:
        extras.append("Hammer")
    if signal.is_shooter:
        extras.append("Shooter")
    if signal.ftfc_aligned:
        extras.append("FTFC")
    if signal.in_force:
        extras.append("In-Force")
    if signal.c1_was_f2:
        extras.append("C1-F2")
    if signal.mag_hit:
        extras.append("Mag-Hit")
    if signal.exh_hit:
        extras.append("Exh-Hit")

    line = " | ".join(parts)
    if extras:
        line += f" [{', '.join(extras)}]"
    return line


def format_domino_line(domino: DominoSetup) -> str:
    """Format a domino setup as a text line."""
    tfs = "/".join(domino.inside_tfs)
    return f"🎯 DOMINO {domino.ticker}: {domino.count} TFs inside ({tfs})"


def format_panel(panel_state: dict[str, str], tf_order: Optional[list[str]] = None) -> str:
    """Format the TF panel state as a compact string.

    Example: "M:2U | W:1 | D:2D"
    """
    if tf_order is None:
        tf_order = TF_ORDER
    parts = []
    for tf in tf_order:
        if tf in panel_state:
            parts.append(f"{tf}:{panel_state[tf]}")
    return " | ".join(parts)


def _signal_type_label(signal_type: str) -> str:
    """Human-readable label for signal type."""
    return {
        "inside_reversal":     "Inside Rev",
        "inside_continuation": "Inside Cont",
        "22_reversal":         "2-2 Rev",
        "22_continuation":     "2-2 Cont",
        "32_expansion":        "3-2 Exp",
        "outside_bar":         "Outside Bar",
        "failing_2":           "Failing 2",
        "inside_bar":          "Inside Bar",
        "double_inside_bar":   "Double Inside Bar",
        "outside_then_inside": "Outside->Inside (3-1)",
    }.get(signal_type, signal_type)
