"""
Swing Failure Pattern (SFP) — faithful translation of LuxAlgo's
"Swing Failure Pattern [LuxAlgo]" Pine Script v5 indicator.

Not part of TheStrat v3.1.1 — a separate liquidity-sweep/reversal
detector layered onto the same scanner infrastructure (config,
timeframe aggregation, alerts).

Pattern definition
-------------------
1. **Pivot**: a swing high/low confirmed with `swing_length` bars on
   the left and exactly 1 bar on the right (matches Pine's
   `ta.pivothigh(len, 1)` / `ta.pivotlow(len, 1)` — fast confirmation,
   only needs one bar after the pivot).

2. **Formation** (the "sweep"): a later bar wicks beyond the pivot
   (sweeping the liquidity resting there) but CLOSES back on the
   origin side of it — high > swing_high but open & close < swing_high
   for bearish; low < swing_low but open & close > swing_low for
   bullish. This is the "SFP" label in the original indicator.

3. **Confirmation**: after formation, price later closes beyond the
   "opposite" level — the most extreme low (bearish) / high (bullish)
   reached between the pivot bar and the formation bar. This is the
   ▼ / ▲ arrow in the original.

4. **Invalidation**: an unconfirmed setup is dropped if either
   (a) more than `max_age_bars` have passed since the pivot, or
   (b) price closes back beyond the swing level itself (undoing the
   sweep).  Only one unconfirmed setup per side is tracked at a time —
   a new formation silently discards an older, still-unconfirmed one.
   This is preserved exactly as the original behaves; it is not a bug.

Deliberate omission
-------------------
The original's volume validation (`iVal` input) requires intrabar
data below the chart's timeframe via `request.security_lower_tf`.
Polygon.io's free-tier grouped-daily endpoint provides exactly one
OHLCV bar per ticker per day — there is no intrabar volume to
validate against, so it is left out entirely rather than kept as a
dead config option.

Monthly timeframe caveat
------------------------
Monthly bars are scarce under the scanner's 120-day history window
(~4 bars). config.sfp_swing_length_overrides drops the pivot lookback
to 1 bar for "M" so it can run at all — every local high/low against
a single neighboring bar counts as a pivot. This is a materially
weaker signal than the length-5 pivot D/W get; treat monthly SFP hits
as lower-conviction until history_calendar_days is extended.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from config import ScannerConfig
from timeframes import AggBar, DailyBar, aggregate

logger = logging.getLogger(__name__)


# =========================================================================
# DATA STRUCTURES
# =========================================================================

@dataclass
class PivotPoint:
    """A confirmed swing pivot."""
    bar_index: int
    price: float
    dt: date


@dataclass
class SFPState:
    """Working state of one side's (bull or bear) active setup.

    Mirrors the Pine `piv` UDT (minus drawing objects).
    """
    swing_price: Optional[float] = None
    swing_bar_index: Optional[int] = None
    swing_date: Optional[date] = None

    oppos_price: Optional[float] = None      # confirmation trigger level
    oppos_bar_index: Optional[int] = None
    oppos_date: Optional[date] = None

    sfp_extreme: Optional[float] = None      # the sweeping wick's high/low
    formed_bar_index: Optional[int] = None
    formed_date: Optional[date] = None

    confirmed_bar_index: Optional[int] = None
    confirmed_date: Optional[date] = None

    active: bool = False
    confirmed: bool = False


@dataclass
class SFPEvent:
    """A single lifecycle event: formed / confirmed / invalidated / superseded."""
    stage: str            # "formed", "confirmed", "invalidated", "superseded"
    direction: str        # "bullish", "bearish"
    bar_index: int
    dt: date
    swing_price: float
    swing_date: date
    oppos_price: float
    oppos_date: date
    sfp_extreme: float


@dataclass
class SFPResult:
    """Full SFP detection result for one ticker on one timeframe."""
    tf: str
    events: list[SFPEvent] = field(default_factory=list)
    bear_state: Optional[SFPState] = None    # final state as of last bar
    bull_state: Optional[SFPState] = None
    bar_count: int = 0


# =========================================================================
# PIVOT DETECTION
# =========================================================================

def find_pivot_highs(bars: list[AggBar], length: int) -> dict[int, PivotPoint]:
    """Find all confirmed pivot highs in a chronological bar series.

    Matches Pine's `ta.pivothigh(length, 1)`: bar i is a pivot high if
    its high exceeds the `length` bars strictly to its left AND the
    1 bar immediately to its right. Confirmed as soon as bar i+1 closes
    (rightbars=1), i.e. it becomes "known" starting from index i+1.

    Returns:
        Dict mapping pivot bar_index -> PivotPoint.
    """
    pivots: dict[int, PivotPoint] = {}
    n = len(bars)
    for i in range(length, n - 1):
        left = bars[i - length : i]
        if not left:
            continue
        left_max = max(b.bar.high for b in left)
        if bars[i].bar.high > left_max and bars[i].bar.high > bars[i + 1].bar.high:
            pivots[i] = PivotPoint(
                bar_index=i, price=bars[i].bar.high, dt=bars[i].period_end
            )
    return pivots


def find_pivot_lows(bars: list[AggBar], length: int) -> dict[int, PivotPoint]:
    """Find all confirmed pivot lows. See find_pivot_highs for the rule."""
    pivots: dict[int, PivotPoint] = {}
    n = len(bars)
    for i in range(length, n - 1):
        left = bars[i - length : i]
        if not left:
            continue
        left_min = min(b.bar.low for b in left)
        if bars[i].bar.low < left_min and bars[i].bar.low < bars[i + 1].bar.low:
            pivots[i] = PivotPoint(
                bar_index=i, price=bars[i].bar.low, dt=bars[i].period_end
            )
    return pivots


# =========================================================================
# SFP DETECTION — single side (bearish or bullish), full-series simulation
# =========================================================================

def _run_bearish(
    bars: list[AggBar],
    pivots_high: dict[int, PivotPoint],
    max_age_bars: int,
) -> tuple[list[SFPEvent], Optional[SFPState]]:
    """Replay the bearish SFP state machine across the full bar series.

    Mirrors the Pine `if bear` block exactly, bar by bar:
      1. Update the tracked swing high if a new pivot confirms this bar.
      2. Check for a new SFP formation (wick above swing, close below).
      3. Check for confirmation (close below the oppos level).
      4. Check for invalidation (age or close back above swing).
    """
    events: list[SFPEvent] = []
    sw: Optional[float] = None
    sw_idx: Optional[int] = None
    sw_dt: Optional[date] = None
    piv: Optional[SFPState] = None

    n = len(bars)
    for i in range(n):
        bar = bars[i].bar
        dt = bars[i].period_end

        # Step 1: update tracked swing high if bar (i-1) just confirmed as pivot
        if (i - 1) in pivots_high:
            p = pivots_high[i - 1]
            sw, sw_idx, sw_dt = p.price, p.bar_index, p.dt

        # Step 2: check for new formation
        if sw is not None and bar.high > sw and bar.open < sw and bar.close < sw:
            # Compute oppos: min low strictly between sw_idx and i
            oppos_price, oppos_idx, oppos_dt = sw, i, dt
            for j in range(sw_idx + 1, i):
                if bars[j].bar.low < oppos_price:
                    oppos_price = bars[j].bar.low
                    oppos_idx = j
                    oppos_dt = bars[j].period_end

            # Discard prior unconfirmed setup (superseded, matches Pine)
            if piv is not None and piv.active and not piv.confirmed:
                events.append(SFPEvent(
                    stage="superseded", direction="bearish",
                    bar_index=i, dt=dt,
                    swing_price=piv.swing_price, swing_date=piv.swing_date,
                    oppos_price=piv.oppos_price, oppos_date=piv.oppos_date,
                    sfp_extreme=piv.sfp_extreme,
                ))

            piv = SFPState(
                swing_price=sw, swing_bar_index=sw_idx, swing_date=sw_dt,
                oppos_price=oppos_price, oppos_bar_index=oppos_idx, oppos_date=oppos_dt,
                sfp_extreme=bar.high,
                formed_bar_index=i, formed_date=dt,
                active=True, confirmed=False,
            )
            events.append(SFPEvent(
                stage="formed", direction="bearish",
                bar_index=i, dt=dt,
                swing_price=sw, swing_date=sw_dt,
                oppos_price=oppos_price, oppos_date=oppos_dt,
                sfp_extreme=bar.high,
            ))

        # Step 3: check confirmation (same bar as formation is possible)
        if piv is not None and piv.active and not piv.confirmed:
            if bar.close < piv.oppos_price:
                piv.confirmed = True
                piv.confirmed_bar_index = i
                piv.confirmed_date = dt
                events.append(SFPEvent(
                    stage="confirmed", direction="bearish",
                    bar_index=i, dt=dt,
                    swing_price=piv.swing_price, swing_date=piv.swing_date,
                    oppos_price=piv.oppos_price, oppos_date=piv.oppos_date,
                    sfp_extreme=piv.sfp_extreme,
                ))

        # Step 4: invalidation check
        if piv is not None and piv.active:
            aged_out = (i - piv.swing_bar_index) > max_age_bars
            reclaimed = bar.close > piv.swing_price
            if aged_out or reclaimed:
                if not piv.confirmed:
                    events.append(SFPEvent(
                        stage="invalidated", direction="bearish",
                        bar_index=i, dt=dt,
                        swing_price=piv.swing_price, swing_date=piv.swing_date,
                        oppos_price=piv.oppos_price, oppos_date=piv.oppos_date,
                        sfp_extreme=piv.sfp_extreme,
                    ))
                piv.active = False

    return events, piv


def _run_bullish(
    bars: list[AggBar],
    pivots_low: dict[int, PivotPoint],
    max_age_bars: int,
) -> tuple[list[SFPEvent], Optional[SFPState]]:
    """Replay the bullish SFP state machine. Mirror image of _run_bearish."""
    events: list[SFPEvent] = []
    sw: Optional[float] = None
    sw_idx: Optional[int] = None
    sw_dt: Optional[date] = None
    piv: Optional[SFPState] = None

    n = len(bars)
    for i in range(n):
        bar = bars[i].bar
        dt = bars[i].period_end

        if (i - 1) in pivots_low:
            p = pivots_low[i - 1]
            sw, sw_idx, sw_dt = p.price, p.bar_index, p.dt

        if sw is not None and bar.low < sw and bar.open > sw and bar.close > sw:
            oppos_price, oppos_idx, oppos_dt = sw, i, dt
            for j in range(sw_idx + 1, i):
                if bars[j].bar.high > oppos_price:
                    oppos_price = bars[j].bar.high
                    oppos_idx = j
                    oppos_dt = bars[j].period_end

            if piv is not None and piv.active and not piv.confirmed:
                events.append(SFPEvent(
                    stage="superseded", direction="bullish",
                    bar_index=i, dt=dt,
                    swing_price=piv.swing_price, swing_date=piv.swing_date,
                    oppos_price=piv.oppos_price, oppos_date=piv.oppos_date,
                    sfp_extreme=piv.sfp_extreme,
                ))

            piv = SFPState(
                swing_price=sw, swing_bar_index=sw_idx, swing_date=sw_dt,
                oppos_price=oppos_price, oppos_bar_index=oppos_idx, oppos_date=oppos_dt,
                sfp_extreme=bar.low,
                formed_bar_index=i, formed_date=dt,
                active=True, confirmed=False,
            )
            events.append(SFPEvent(
                stage="formed", direction="bullish",
                bar_index=i, dt=dt,
                swing_price=sw, swing_date=sw_dt,
                oppos_price=oppos_price, oppos_date=oppos_dt,
                sfp_extreme=bar.low,
            ))

        if piv is not None and piv.active and not piv.confirmed:
            if bar.close > piv.oppos_price:
                piv.confirmed = True
                piv.confirmed_bar_index = i
                piv.confirmed_date = dt
                events.append(SFPEvent(
                    stage="confirmed", direction="bullish",
                    bar_index=i, dt=dt,
                    swing_price=piv.swing_price, swing_date=piv.swing_date,
                    oppos_price=piv.oppos_price, oppos_date=piv.oppos_date,
                    sfp_extreme=piv.sfp_extreme,
                ))

        if piv is not None and piv.active:
            aged_out = (i - piv.swing_bar_index) > max_age_bars
            reclaimed = bar.close < piv.swing_price
            if aged_out or reclaimed:
                if not piv.confirmed:
                    events.append(SFPEvent(
                        stage="invalidated", direction="bullish",
                        bar_index=i, dt=dt,
                        swing_price=piv.swing_price, swing_date=piv.swing_date,
                        oppos_price=piv.oppos_price, oppos_date=piv.oppos_date,
                        sfp_extreme=piv.sfp_extreme,
                    ))
                piv.active = False

    return events, piv


# =========================================================================
# PUBLIC: single-TF detection
# =========================================================================

def _effective_swing_length(config: ScannerConfig, tf: str) -> int:
    """Resolve the pivot lookback length for a given timeframe.

    Falls back to config.sfp_swing_length unless tf has an entry in
    config.sfp_swing_length_overrides (used for Monthly, which typically
    only has ~4 bars available and can't support the default length-5
    pivot).
    """
    return config.sfp_swing_length_overrides.get(tf, config.sfp_swing_length)


def detect_sfp(
    bars: list[AggBar],
    config: ScannerConfig,
    tf: str = "D",
) -> SFPResult:
    """Run full SFP detection (both sides) on one timeframe's bar series.

    Args:
        bars: chronologically sorted AggBar list (oldest first)
        config: scanner config (sfp_swing_length / sfp_swing_length_overrides,
                sfp_bullish_enabled, sfp_bearish_enabled, sfp_max_age_bars)
        tf: timeframe label — also selects the per-TF swing length override

    Returns:
        SFPResult with the full event history and final state per side.
    """
    result = SFPResult(tf=tf, bar_count=len(bars))

    swing_length = _effective_swing_length(config, tf)
    min_bars = swing_length + 2  # left bars + pivot bar + 1 right
    if len(bars) < min_bars:
        return result

    if config.sfp_bearish_enabled:
        pivots_high = find_pivot_highs(bars, swing_length)
        bear_events, bear_state = _run_bearish(
            bars, pivots_high, config.sfp_max_age_bars
        )
        result.events.extend(bear_events)
        result.bear_state = bear_state

    if config.sfp_bullish_enabled:
        pivots_low = find_pivot_lows(bars, swing_length)
        bull_events, bull_state = _run_bullish(
            bars, pivots_low, config.sfp_max_age_bars
        )
        result.events.extend(bull_events)
        result.bull_state = bull_state

    result.events.sort(key=lambda e: e.bar_index)
    return result


# =========================================================================
# TICKER-LEVEL ORCHESTRATION
# =========================================================================

@dataclass
class SFPSignal:
    """A fresh (current-bar) SFP event, ready for alerting."""
    ticker: str
    tf: str
    direction: str        # "bullish", "bearish"
    stage: str             # "formed", "confirmed"
    dt: date
    swing_price: float
    swing_date: date
    oppos_price: float     # the level that must break to confirm (or just broke)
    oppos_date: date
    sfp_extreme: float     # the sweeping wick's extreme price


@dataclass
class TickerSFPResult:
    """Complete SFP scan result for one ticker across all configured TFs."""
    ticker: str
    signals: list[SFPSignal] = field(default_factory=list)   # fresh events only
    tf_results: dict[str, SFPResult] = field(default_factory=dict)


def scan_ticker_sfp(
    ticker: str,
    daily_bars: list[DailyBar],
    config: ScannerConfig,
) -> TickerSFPResult:
    """Run SFP detection for one ticker across all configured timeframes.

    Only events on the MOST RECENT bar of each timeframe are surfaced as
    alertable `signals` — a scanner run once a day cares about "did an
    SFP form or confirm today," not full history. Full history is still
    available in `tf_results` for context/backtesting.

    Args:
        ticker: stock ticker symbol
        daily_bars: chronologically sorted daily bars (oldest first)
        config: scanner configuration

    Returns:
        TickerSFPResult with fresh signals and full per-TF detail.
    """
    result = TickerSFPResult(ticker=ticker)

    if not config.sfp_enabled:
        return result

    for tf in config.sfp_timeframes:
        try:
            agg = aggregate(daily_bars, tf)
        except ValueError:
            continue

        tf_result = detect_sfp(agg, config, tf=tf)
        result.tf_results[tf] = tf_result

        if tf_result.bar_count == 0:
            continue

        last_index = tf_result.bar_count - 1
        for event in tf_result.events:
            if event.bar_index != last_index:
                continue
            if event.stage not in ("formed", "confirmed"):
                continue
            result.signals.append(SFPSignal(
                ticker=ticker,
                tf=tf,
                direction=event.direction,
                stage=event.stage,
                dt=event.dt,
                swing_price=event.swing_price,
                swing_date=event.swing_date,
                oppos_price=event.oppos_price,
                oppos_date=event.oppos_date,
                sfp_extreme=event.sfp_extreme,
            ))

    return result


# =========================================================================
# FORMATTING
# =========================================================================

def format_sfp_line(signal: SFPSignal) -> str:
    """Format an SFP signal as a compact text line for alerts.

    Example:
      "AAPL D SFP▼ formed | Swept 178.50 (2026-09-10) | Confirm below 172.30"
      "AAPL D SFP▲ CONFIRMED | Swept 165.00 (2026-09-08) | Broke above 170.10"
    """
    arrow = "▼" if signal.direction == "bearish" else "▲"
    sweep_word = "Swept" if signal.direction == "bearish" else "Swept"

    if signal.stage == "formed":
        confirm_word = "below" if signal.direction == "bearish" else "above"
        return (
            f"{signal.ticker} {signal.tf} SFP{arrow} formed | "
            f"{sweep_word} {signal.swing_price:.2f} ({signal.swing_date}) | "
            f"Confirm {confirm_word} {signal.oppos_price:.2f}"
        )
    else:  # confirmed
        break_word = "below" if signal.direction == "bearish" else "above"
        return (
            f"{signal.ticker} {signal.tf} SFP{arrow} CONFIRMED | "
            f"{sweep_word} {signal.swing_price:.2f} ({signal.swing_date}) | "
            f"Broke {break_word} {signal.oppos_price:.2f}"
        )
