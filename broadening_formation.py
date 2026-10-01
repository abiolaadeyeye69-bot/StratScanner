"""
Broadening Formation Reclaim — faithful translation of the "Magnitude
Reclaim" indicator ("Magnitude Price Discovery - Bare Bones").

Naming note (Epistemic Humility: separating the label from the mechanism)
--------------------------------------------------------------------------
This is NOT a literal broadening-formation (diverging-trendline) chart
pattern detector. The source script is a break-and-reclaim tracker:
it watches every 1-bar swing high/low, waits for price to break it,
then waits for price to reclaim it, and locks a profit target at the
most extreme price reached during the excursion. It's filed under
"Broadening Formation Reclaim" here because that's the project's name
for this detector, not because the underlying math draws a broadening
wedge. Worth knowing if the naming and mechanism ever seem to disagree
in review.

Not part of TheStrat v3.1.1 — a separate detector layered onto the
same scanner infrastructure (config, timeframe aggregation, alerts).

Pattern definition
-------------------
1. **Swing**: a 1-bar pivot high/low (bar's low lower than both
   immediate neighbors, or high higher than both) — the tightest
   possible pivot, confirmed one bar later. Reuses sfp.find_pivot_lows
   / find_pivot_highs with length=1, since the source's inline formula
   (`low[1] < low[2] and low[1] < low[0]`) is exactly that pivot rule.

2. **Extreme tracking**: from the moment a swing is recorded, its
   "running extreme" (highest high for a swing low, lowest low for a
   swing high) updates every bar — but ONLY until the level is broken
   AND reclaimed (i.e. only while no target has been locked yet).

3. **Broken**: price closes... no — price simply TRADES beyond the
   swing level (low < swing low, or high > swing high). Once broken,
   always broken — there is no re-arming.

4. **Reclaim**: once broken, if the CLOSE comes back on the origin
   side of the level, the target is locked to whatever the running
   extreme is at that moment (which may include this same bar's
   high/low, since extreme-update runs before the reclaim check on
   the same bar).

5. **Target hit**: on any later bar (never the reclaim bar itself),
   if price trades through the locked target, the setup is marked
   complete.

6. **Active**: broken + target locked + not yet hit + price currently
   back on the reclaim side of the level. This is re-evaluated every
   bar from current price — a setup that reclaims and later dips back
   below the level (without ever breaking again in a new direction)
   goes inactive, then can go active again if price climbs back above
   the level, all while carrying the same locked target.

Unlike SFP, ALL tracked swings are updated every bar simultaneously —
there is no "one active setup per side, superseded by the next" rule.
Up to `bf_max_swings` swings per side are tracked; the oldest is
dropped once a new one pushes the count over that cap. There is no
time-based invalidation in the source (no equivalent of SFP's
max_age_bars) — a setup can sit broken-but-unreclaimed indefinitely
until it ages out of the max_swings cap.

Deliberate re-scoping for the scanner
--------------------------------------
The source is a single-chart overlay: it can only ever display ONE
setup per side at a time (the single largest by magnitude), because a
chart only has one pair of lines to draw. A scanner is a data pipeline,
not a single chart — dropping every setup except the historically
biggest would throw away information a trader scanning many tickers
should get to see. So this module surfaces ALL currently-active setups
per ticker/TF, sorted by magnitude (biggest first) — preserving the
source's chosen prioritization metric as a sort order rather than as
a hard filter.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from config import ScannerConfig
from sfp import find_pivot_highs, find_pivot_lows
from strat_engine import BarData, classify_bar, detect_failed_2, detect_hammer_shooter
from timeframes import AggBar, DailyBar, aggregate


# =========================================================================
# DATA STRUCTURES — ENGINE STATE (pure simulation, no display concerns)
# =========================================================================

@dataclass
class MagnitudeSwing:
    """One tracked swing (long from a swing low, short from a swing high)
    and its full break/reclaim/target lifecycle state."""
    direction: str                        # "long" or "short"
    level: float                          # the swing low/high price
    swing_bar_index: int
    swing_date: date

    extreme: float                        # running HH (long) / LL (short)

    broken: bool = False
    broken_bar_index: Optional[int] = None
    broken_date: Optional[date] = None

    target: Optional[float] = None        # locked at reclaim
    target_bar_index: Optional[int] = None
    target_date: Optional[date] = None

    target_hit: bool = False
    target_hit_bar_index: Optional[int] = None
    target_hit_date: Optional[date] = None

    def is_active(self, current_close: float) -> bool:
        """Whether this setup is live right now, given the latest close."""
        if not self.broken or self.target is None or self.target_hit:
            return False
        if self.direction == "long":
            return current_close > self.level
        return current_close < self.level

    @property
    def magnitude(self) -> float:
        """The locked target's distance from the swing level. 0 if no target yet."""
        if self.target is None:
            return 0.0
        if self.direction == "long":
            return self.target - self.level
        return self.level - self.target


@dataclass
class MagnitudeEvent:
    """A single lifecycle event: reclaimed or target_hit."""
    stage: str            # "reclaimed", "target_hit"
    direction: str         # "long", "short"
    bar_index: int
    dt: date
    level: float
    swing_date: date
    target: float
    target_date: Optional[date] = None


@dataclass
class MagnitudeResult:
    """Full detection result for one ticker on one timeframe."""
    tf: str
    events: list[MagnitudeEvent] = field(default_factory=list)
    swings_long: list[MagnitudeSwing] = field(default_factory=list)   # final tracked set
    swings_short: list[MagnitudeSwing] = field(default_factory=list)
    bar_count: int = 0


# =========================================================================
# PER-BAR UPDATE — mirrors the source's two update loops exactly
# =========================================================================

def _update_long_swing(
    swing: MagnitudeSwing, bar, bar_index: int, dt: date
) -> Optional[str]:
    """Update one swing-low record for the current bar.

    Returns "reclaimed" or "target_hit" if one occurred this bar, else None.
    Order matches the source precisely: extreme update -> broken check ->
    reclaim check -> target-hit check (excluding the reclaim bar itself).
    """
    if swing.target is None and bar.high > swing.extreme:
        swing.extreme = bar.high

    if not swing.broken and bar.low < swing.level:
        swing.broken = True
        swing.broken_bar_index = bar_index
        swing.broken_date = dt

    just_reclaimed = False
    if swing.broken and swing.target is None and bar.close > swing.level:
        swing.target = swing.extreme
        swing.target_bar_index = bar_index
        swing.target_date = dt
        just_reclaimed = True

    if (
        swing.target is not None
        and not swing.target_hit
        and not just_reclaimed
        and bar.high >= swing.target
    ):
        swing.target_hit = True
        swing.target_hit_bar_index = bar_index
        swing.target_hit_date = dt
        return "target_hit"

    if just_reclaimed:
        return "reclaimed"

    return None


def _update_short_swing(
    swing: MagnitudeSwing, bar, bar_index: int, dt: date
) -> Optional[str]:
    """Update one swing-high record for the current bar. Mirror of
    _update_long_swing with high/low/close comparisons flipped."""
    if swing.target is None and bar.low < swing.extreme:
        swing.extreme = bar.low

    if not swing.broken and bar.high > swing.level:
        swing.broken = True
        swing.broken_bar_index = bar_index
        swing.broken_date = dt

    just_reclaimed = False
    if swing.broken and swing.target is None and bar.close < swing.level:
        swing.target = swing.extreme
        swing.target_bar_index = bar_index
        swing.target_date = dt
        just_reclaimed = True

    if (
        swing.target is not None
        and not swing.target_hit
        and not just_reclaimed
        and bar.low <= swing.target
    ):
        swing.target_hit = True
        swing.target_hit_bar_index = bar_index
        swing.target_hit_date = dt
        return "target_hit"

    if just_reclaimed:
        return "reclaimed"

    return None


# =========================================================================
# PUBLIC: single-TF detection
# =========================================================================

def detect_magnitude_reclaim(
    bars: list[AggBar],
    config: ScannerConfig,
    tf: str = "D",
) -> MagnitudeResult:
    """Run full break/reclaim/target detection (both sides) on one
    timeframe's bar series.

    Args:
        bars: chronologically sorted AggBar list (oldest first)
        config: scanner config (bf_long_only, bf_max_swings)
        tf: timeframe label, for result tagging

    Returns:
        MagnitudeResult with the full event history and the final set
        of tracked swings per side (post max_swings eviction).
    """
    result = MagnitudeResult(tf=tf, bar_count=len(bars))
    n = len(bars)
    if n < 3:
        return result

    pivot_lows = find_pivot_lows(bars, length=1)
    pivot_highs = {} if config.bf_long_only else find_pivot_highs(bars, length=1)

    long_swings: list[MagnitudeSwing] = []
    short_swings: list[MagnitudeSwing] = []

    for i in range(n):
        bar = bars[i].bar
        dt = bars[i].period_end

        # --- Insert new swings confirmed this bar ---
        if (i - 1) in pivot_lows:
            p = pivot_lows[i - 1]
            long_swings.append(MagnitudeSwing(
                direction="long", level=p.price,
                swing_bar_index=p.bar_index, swing_date=p.dt,
                extreme=bar.high,
            ))
            if len(long_swings) > config.bf_max_swings:
                long_swings.pop(0)   # drop oldest tracked swing

        if not config.bf_long_only and (i - 1) in pivot_highs:
            p = pivot_highs[i - 1]
            short_swings.append(MagnitudeSwing(
                direction="short", level=p.price,
                swing_bar_index=p.bar_index, swing_date=p.dt,
                extreme=bar.low,
            ))
            if len(short_swings) > config.bf_max_swings:
                short_swings.pop(0)

        # --- Update every tracked long swing (including one just added) ---
        for swing in long_swings:
            event = _update_long_swing(swing, bar, i, dt)
            if event:
                result.events.append(MagnitudeEvent(
                    stage=event, direction="long", bar_index=i, dt=dt,
                    level=swing.level, swing_date=swing.swing_date,
                    target=swing.target, target_date=swing.target_date,
                ))

        # --- Update every tracked short swing ---
        if not config.bf_long_only:
            for swing in short_swings:
                event = _update_short_swing(swing, bar, i, dt)
                if event:
                    result.events.append(MagnitudeEvent(
                        stage=event, direction="short", bar_index=i, dt=dt,
                        level=swing.level, swing_date=swing.swing_date,
                        target=swing.target, target_date=swing.target_date,
                    ))

    result.swings_long = long_swings
    result.swings_short = short_swings
    result.events.sort(key=lambda e: e.bar_index)
    return result


# =========================================================================
# TICKER-LEVEL ORCHESTRATION
# =========================================================================

@dataclass
class MagnitudeSignal:
    """A fresh (current-bar) reclaim/target-hit event, ready for alerting."""
    ticker: str
    tf: str
    direction: str         # "long", "short"
    stage: str              # "reclaimed", "target_hit"
    dt: date
    level: float
    swing_date: date
    target: float
    target_date: date


@dataclass
class ActiveSetup:
    """A currently-active (post-reclaim, pre-target) setup, ready for display."""
    ticker: str
    tf: str
    direction: str          # "long", "short"
    level: float
    swing_date: date
    target: float
    target_date: date
    magnitude: float        # |target - level|
    expected_move: float    # distance from current price to target
    risk: float             # distance from current price back to the level


@dataclass
class BFReversal:
    """A reversal pattern that printed as price came back into (or tested
    the edge of) an active setup's level -> target range."""
    ticker: str
    tf: str
    direction: str          # "long" (bullish reversal) or "short"
    patterns: list[str]     # e.g. ["2-2 Rev", "Hammer"]
    entry: str              # "reclaim" (the reclaim bar itself) or "retest"
    dt: date                # reversal bar's period end
    bars_ago: int           # 0 = latest bar
    level: float
    swing_date: date
    target: float
    target_date: date
    close: float            # reversal bar's close
    bar_extreme: float      # reversal bar's low (long) / high (short)
    magnitude: float        # |target - level|
    expected_move: float    # reversal-bar close -> target
    risk: float             # reversal-bar close -> level


@dataclass
class TickerMagnitudeResult:
    """Complete broadening-formation-reclaim scan result for one ticker
    across all configured timeframes."""
    ticker: str
    signals: list[MagnitudeSignal] = field(default_factory=list)     # fresh events
    active_long: list[ActiveSetup] = field(default_factory=list)     # magnitude desc
    active_short: list[ActiveSetup] = field(default_factory=list)    # magnitude desc
    reversals: list[BFReversal] = field(default_factory=list)        # reversal into range
    tf_results: dict[str, MagnitudeResult] = field(default_factory=dict)


# =========================================================================
# REVERSAL INTO THE BF RANGE
# =========================================================================

_PATTERN_LABELS = {
    "22rev": "2-2 Rev",
    "f2": "Failed 2",
    "pin": "Pin Bar",
}


def detect_reversal_patterns(
    bars: list[AggBar], j: int, direction: str, config: ScannerConfig,
) -> list[str]:
    """Which enabled reversal patterns bar `j` prints, pointing toward the
    target of a `direction` setup ("long" = bullish reversal).

    Every pattern is judged on bar j against the bars before it:
      2-2 Rev  long: bar j-1 was a 2D and bar j is a 2U.  Short: 2U then 2D.
      Failed 2 long: bar j is a 2D that closed back inside bar j-1's range
               (Failed 2D).  Short: Failed 2U.  Uses config.failed_2_method.
      Hammer / Shooter: long wants a hammer, short a shooter, under
               config.hammer_shooter_logic.
      Pin Bar: same direction test but always the "Pin Bar (Strict)" shape.
    """
    enabled = set(config.bf_reversal_patterns)
    found: list[str] = []
    bar = bars[j].bar
    is_long = direction == "long"

    if j >= 1:
        prev = bars[j - 1].bar

        if "22rev" in enabled and j >= 2:
            pp = bars[j - 2].bar
            prev_cls = classify_bar(
                pp.high, pp.low, prev.high, prev.low, prev.open, prev.close)
            cur_cls = classify_bar(
                prev.high, prev.low, bar.high, bar.low, bar.open, bar.close)
            if is_long and prev_cls.is_2d and cur_cls.is_2u:
                found.append(_PATTERN_LABELS["22rev"])
            elif not is_long and prev_cls.is_2u and cur_cls.is_2d:
                found.append(_PATTERN_LABELS["22rev"])

        if "f2" in enabled:
            f2 = detect_failed_2(
                prev.high, prev.low, bar.open, bar.close, bar.high, bar.low,
                method=config.failed_2_method,
                enable_detection=True,
            )
            if (is_long and f2.is_f2d) or (not is_long and f2.is_f2u):
                found.append(_PATTERN_LABELS["f2"])

    if "hammer_shooter" in enabled:
        hs = detect_hammer_shooter(
            bar, config.hammer_shooter_logic, config.hammer_shooter_match_color,
            min_wick_body_ratio=config.hammer_shooter_min_wick_body_ratio,
            min_wick_pct=config.hammer_shooter_min_wick_pct,
            max_opp_wick_pct=config.hammer_shooter_max_opp_wick_pct,
        )
        if is_long and hs.is_hammer:
            found.append("Hammer")
        elif not is_long and hs.is_shooter:
            found.append("Shooter")

    if "pin" in enabled:
        pin = detect_hammer_shooter(bar, "Pin Bar (Strict)")
        if (is_long and pin.is_hammer) or (not is_long and pin.is_shooter):
            found.append(_PATTERN_LABELS["pin"])

    return found


def find_bf_reversals(
    ticker: str,
    tf: str,
    bars: list[AggBar],
    tf_result: MagnitudeResult,
    config: ScannerConfig,
) -> list[BFReversal]:
    """Reversal patterns printed at an ACTIVE setup's level within the
    last `bf_reversal_lookback_bars` bars.

    A bar j qualifies for a setup when ALL hold:
      * the setup's target was already locked by bar j (range exists)
      * bar j reached the level: long low <= level*(1+zone%), short
        high >= level*(1-zone%)  (a pierce always passes this)
      * bar j closed on the inside of the level (long > level, short <
        level) -- price is back in the range
      * bar j's close is short of the target
      * the setup is still active on the latest bar (not failed since,
        target not hit)
      * at least one enabled reversal pattern points toward the target
    """
    out: list[BFReversal] = []
    n = len(bars)
    if n == 0 or not config.bf_reversal_enabled:
        return out

    last_close = bars[-1].bar.close
    first_j = max(0, n - max(1, config.bf_reversal_lookback_bars))
    zone = config.bf_reversal_zone_pct / 100.0

    for direction, swings in (
        ("long", tf_result.swings_long), ("short", tf_result.swings_short)
    ):
        for swing in swings:
            if not swing.is_active(last_close):
                continue
            for j in range(first_j, n):
                if swing.target_bar_index is None or swing.target_bar_index > j:
                    continue
                bar = bars[j].bar
                if direction == "long":
                    reached = bar.low <= swing.level * (1 + zone)
                    inside = bar.close > swing.level and bar.close < swing.target
                    extreme = bar.low
                else:
                    reached = bar.high >= swing.level * (1 - zone)
                    inside = bar.close < swing.level and bar.close > swing.target
                    extreme = bar.high
                if not (reached and inside):
                    continue

                patterns = detect_reversal_patterns(bars, j, direction, config)
                if not patterns:
                    continue

                move = (swing.target - bar.close if direction == "long"
                        else bar.close - swing.target)
                risk = (bar.close - swing.level if direction == "long"
                        else swing.level - bar.close)
                out.append(BFReversal(
                    ticker=ticker, tf=tf, direction=direction,
                    patterns=patterns,
                    entry="reclaim" if j == swing.target_bar_index else "retest",
                    dt=bars[j].period_end, bars_ago=n - 1 - j,
                    level=swing.level, swing_date=swing.swing_date,
                    target=swing.target, target_date=swing.target_date,
                    close=bar.close, bar_extreme=extreme,
                    magnitude=swing.magnitude,
                    expected_move=move, risk=risk,
                ))
    return out


def scan_ticker_bf(
    ticker: str,
    daily_bars: list[DailyBar],
    config: ScannerConfig,
) -> TickerMagnitudeResult:
    """Run broadening-formation-reclaim detection for one ticker across
    all configured timeframes.

    Fresh `signals` mirror sfp.scan_ticker_sfp's philosophy: only events
    on the most recent bar of each timeframe are alertable — a scanner
    run once a day cares about "did a reclaim or target-hit happen
    today." `active_long`/`active_short` carry every currently-live
    setup (not just the historically biggest, unlike the source's
    single-chart display), sorted with the biggest-magnitude setup
    first so the "primary" pick still lines up with what the source
    indicator would have highlighted.

    Args:
        ticker: stock ticker symbol
        daily_bars: chronologically sorted daily bars (oldest first)
        config: scanner configuration

    Returns:
        TickerMagnitudeResult with fresh signals, active setups, and
        full per-TF detail.
    """
    result = TickerMagnitudeResult(ticker=ticker)

    if not config.bf_enabled:
        return result

    for tf in config.bf_timeframes:
        try:
            agg = aggregate(daily_bars, tf)
        except ValueError:
            continue

        tf_result = detect_magnitude_reclaim(agg, config, tf=tf)
        result.tf_results[tf] = tf_result

        if tf_result.bar_count == 0:
            continue

        last_index = tf_result.bar_count - 1
        last_close = agg[-1].bar.close

        # --- Fresh signals: events on the most recent bar ---
        for event in tf_result.events:
            if event.bar_index != last_index:
                continue
            result.signals.append(MagnitudeSignal(
                ticker=ticker, tf=tf,
                direction=event.direction, stage=event.stage,
                dt=event.dt, level=event.level, swing_date=event.swing_date,
                target=event.target, target_date=event.target_date,
            ))

        # --- Reversal patterns at the level of an active setup ---
        result.reversals.extend(
            find_bf_reversals(ticker, tf, agg, tf_result, config)
        )

        # --- Active setups: every live swing as of the last bar ---
        for swing in tf_result.swings_long:
            if swing.is_active(last_close):
                result.active_long.append(ActiveSetup(
                    ticker=ticker, tf=tf, direction="long",
                    level=swing.level, swing_date=swing.swing_date,
                    target=swing.target, target_date=swing.target_date,
                    magnitude=swing.magnitude,
                    expected_move=swing.target - last_close,
                    risk=last_close - swing.level,
                ))

        for swing in tf_result.swings_short:
            if swing.is_active(last_close):
                result.active_short.append(ActiveSetup(
                    ticker=ticker, tf=tf, direction="short",
                    level=swing.level, swing_date=swing.swing_date,
                    target=swing.target, target_date=swing.target_date,
                    magnitude=swing.magnitude,
                    expected_move=last_close - swing.target,
                    risk=swing.level - last_close,
                ))

    result.active_long.sort(key=lambda s: s.magnitude, reverse=True)
    result.active_short.sort(key=lambda s: s.magnitude, reverse=True)
    result.reversals.sort(key=lambda r: (r.bars_ago, -r.magnitude))

    return result


# =========================================================================
# FORMATTING
# =========================================================================

def format_bf_signal_line(signal: MagnitudeSignal) -> str:
    """Format a fresh reclaim/target-hit event as a compact alert line.

    Example:
      "AAPL D BF-long RECLAIMED | Level 178.50 (2026-09-10) | Target 192.00"
      "AAPL D BF-long TARGET HIT | Level 178.50 | Target 192.00"
    """
    arrow = "▲" if signal.direction == "long" else "▼"
    if signal.stage == "reclaimed":
        return (
            f"{signal.ticker} {signal.tf} BF{arrow} RECLAIMED | "
            f"Level {signal.level:.2f} ({signal.swing_date}) | "
            f"Target {signal.target:.2f}"
        )
    else:  # target_hit
        return (
            f"{signal.ticker} {signal.tf} BF{arrow} TARGET HIT | "
            f"Level {signal.level:.2f} ({signal.swing_date}) | "
            f"Target {signal.target:.2f}"
        )


def format_bf_active_line(setup: ActiveSetup) -> str:
    """Format an active (in-progress) setup as a compact info line.

    Example:
      "AAPL D BF▲ active | Level 178.50 -> Target 192.00 (mag 13.50) | Move 8.20 Risk 3.10"
    """
    arrow = "▲" if setup.direction == "long" else "▼"
    return (
        f"{setup.ticker} {setup.tf} BF{arrow} active | "
        f"Level {setup.level:.2f} -> Target {setup.target:.2f} "
        f"(mag {setup.magnitude:.2f}) | "
        f"Move {setup.expected_move:.2f} Risk {setup.risk:.2f}"
    )


def format_bf_reversal_line(r: BFReversal) -> str:
    """Format a reversal-into-range as a compact alert line.

    Example:
      "AAPL D BF▲ REVERSAL (Hammer + Failed 2, retest) | Level 178.50 -> Target 192.00 | Move 8.20 Risk 3.10"
    """
    arrow = "▲" if r.direction == "long" else "▼"
    age = "" if r.bars_ago == 0 else f", {r.bars_ago} bar{'s' if r.bars_ago != 1 else ''} ago"
    return (
        f"{r.ticker} {r.tf} BF{arrow} REVERSAL "
        f"({' + '.join(r.patterns)}, {r.entry}{age}) | "
        f"Level {r.level:.2f} -> Target {r.target:.2f} | "
        f"Move {r.expected_move:.2f} Risk {r.risk:.2f}"
    )
