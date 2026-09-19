"""
Timeframe Aggregation — daily bars → W, M, 2D, 3D.

Converts a chronologically ordered list of daily OHLCV bars into
higher-timeframe OHLC bars.  Each aggregator produces complete bars
only — the current (incomplete) period is always included as the last
bar so the engine can classify the "current candle" (CC).

Boundary rules
--------------
Weekly   : Monday–Friday.  Week boundary = Monday.
Monthly  : Calendar month boundary.
2-Day    : Rolling non-overlapping pairs of trading days, oldest-first.
3-Day    : Rolling non-overlapping triples of trading days, oldest-first.

For 2D/3D the grouping resets at the START of the history window —
there's no "correct" universal anchor, so we anchor at the first bar
in the series.  This matches how v3.1.1 handles custom-period charts.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Optional

from strat_engine import BarData


# =========================================================================
# AGGREGATED BAR (carries date metadata the engine doesn't need but the
# scanner uses for labelling and dedup)
# =========================================================================

@dataclass
class AggBar:
    """An aggregated OHLC bar with period metadata."""
    bar: BarData
    period_start: date      # first trading day in this bar
    period_end: date        # last trading day in this bar (may == start)
    volume: float = 0.0     # summed volume across constituent days
    bar_count: int = 1      # number of daily bars aggregated


@dataclass
class DailyBar:
    """A daily bar with its date, used as input to aggregation."""
    dt: date
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


# =========================================================================
# INTERNAL HELPERS
# =========================================================================

def _merge_bars(bars: list[DailyBar]) -> AggBar:
    """Merge a list of daily bars into one aggregated bar.

    Standard OHLC aggregation:
      O = first bar's open
      H = max of all highs
      L = min of all lows
      C = last bar's close
      V = sum of all volumes
    """
    if not bars:
        raise ValueError("Cannot merge empty bar list")

    o = bars[0].open
    h = max(b.high for b in bars)
    l = min(b.low for b in bars)
    c = bars[-1].close
    v = sum(b.volume for b in bars)

    return AggBar(
        bar=BarData(open=o, high=h, low=l, close=c),
        period_start=bars[0].dt,
        period_end=bars[-1].dt,
        volume=v,
        bar_count=len(bars),
    )


def _iso_week_key(dt: date) -> tuple[int, int]:
    """Return (ISO year, ISO week number) for grouping into weeks.

    ISO weeks start on Monday.  This matches how TradingView groups
    weekly bars — the week containing Monday is the boundary.
    """
    iso = dt.isocalendar()
    return (iso[0], iso[1])


def _month_key(dt: date) -> tuple[int, int]:
    """Return (year, month) for grouping into months."""
    return (dt.year, dt.month)


# =========================================================================
# PUBLIC AGGREGATION FUNCTIONS
# =========================================================================

def aggregate_daily(daily_bars: list[DailyBar]) -> list[AggBar]:
    """Pass-through: each daily bar becomes its own AggBar.

    Included for API uniformity so the scanner can treat all TFs
    the same way.
    """
    return [
        AggBar(
            bar=BarData(open=b.open, high=b.high, low=b.low, close=b.close),
            period_start=b.dt,
            period_end=b.dt,
            volume=b.volume,
            bar_count=1,
        )
        for b in daily_bars
    ]


def aggregate_weekly(daily_bars: list[DailyBar]) -> list[AggBar]:
    """Aggregate daily bars into weekly bars (Monday boundary).

    The last group is always included even if the week is incomplete —
    that's the current candle (CC).
    """
    if not daily_bars:
        return []

    groups: list[list[DailyBar]] = []
    current_key: Optional[tuple[int, int]] = None
    current_group: list[DailyBar] = []

    for bar in daily_bars:
        key = _iso_week_key(bar.dt)
        if key != current_key:
            if current_group:
                groups.append(current_group)
            current_group = [bar]
            current_key = key
        else:
            current_group.append(bar)

    if current_group:
        groups.append(current_group)

    return [_merge_bars(g) for g in groups]


def aggregate_monthly(daily_bars: list[DailyBar]) -> list[AggBar]:
    """Aggregate daily bars into monthly bars (calendar month boundary).

    The last group is always included even if the month is incomplete —
    that's the current candle (CC).
    """
    if not daily_bars:
        return []

    groups: list[list[DailyBar]] = []
    current_key: Optional[tuple[int, int]] = None
    current_group: list[DailyBar] = []

    for bar in daily_bars:
        key = _month_key(bar.dt)
        if key != current_key:
            if current_group:
                groups.append(current_group)
            current_group = [bar]
            current_key = key
        else:
            current_group.append(bar)

    if current_group:
        groups.append(current_group)

    return [_merge_bars(g) for g in groups]


def aggregate_nday(daily_bars: list[DailyBar], n: int) -> list[AggBar]:
    """Aggregate daily bars into N-day bars (rolling non-overlapping groups).

    Groups are anchored at the FIRST bar in the series.  The last group
    is always included even if it has fewer than N bars — that's the
    current candle (CC).

    Args:
        daily_bars: chronologically sorted daily bars
        n: number of trading days per group (2 or 3)
    """
    if not daily_bars:
        return []
    if n < 1:
        raise ValueError(f"n must be >= 1, got {n}")

    result: list[AggBar] = []
    for i in range(0, len(daily_bars), n):
        chunk = daily_bars[i : i + n]
        result.append(_merge_bars(chunk))

    return result


def aggregate_2day(daily_bars: list[DailyBar]) -> list[AggBar]:
    """Aggregate daily bars into 2-day bars."""
    return aggregate_nday(daily_bars, 2)


def aggregate_3day(daily_bars: list[DailyBar]) -> list[AggBar]:
    """Aggregate daily bars into 3-day bars."""
    return aggregate_nday(daily_bars, 3)


# =========================================================================
# UNIFIED DISPATCHER
# =========================================================================

# Map timeframe codes → aggregation functions
AGGREGATORS = {
    "D":  aggregate_daily,
    "W":  aggregate_weekly,
    "M":  aggregate_monthly,
    "2D": aggregate_2day,
    "3D": aggregate_3day,
}


def aggregate(daily_bars: list[DailyBar], tf: str) -> list[AggBar]:
    """Aggregate daily bars into the requested timeframe.

    Args:
        daily_bars: chronologically sorted daily bars (oldest first)
        tf: timeframe code — one of "D", "W", "M", "2D", "3D"

    Returns:
        List of AggBar, oldest first.  The last bar is always the
        current (possibly incomplete) period.

    Raises:
        ValueError: if tf is not a recognized timeframe code.
    """
    if tf not in AGGREGATORS:
        raise ValueError(
            f"Unknown timeframe '{tf}'. "
            f"Valid options: {sorted(AGGREGATORS.keys())}"
        )
    return AGGREGATORS[tf](daily_bars)


def bars_needed(tf: str, min_bars: int = 4) -> int:
    """Estimate the minimum number of daily bars needed to produce
    at least `min_bars` aggregated bars for a given timeframe.

    The engine needs at minimum: CC, C1, C2, C3 = 4 bars.
    For exhaustion scans it helps to have more history, but 4 is
    the hard floor.

    This is a rough guide for the data layer to know how much
    history to request.  The actual count may vary because of
    holidays, half-days, etc.

    Args:
        tf: timeframe code
        min_bars: minimum number of aggregated bars needed (default 4)

    Returns:
        Approximate number of daily trading bars to request.
    """
    multipliers = {
        "D":  1,
        "2D": 2,
        "3D": 3,
        "W":  5,     # ~5 trading days per week
        "M":  21,    # ~21 trading days per month
    }
    m = multipliers.get(tf, 1)
    return min_bars * m


def extract_bar_window(agg_bars: list[AggBar], window: int = 4) -> list[BarData]:
    """Extract the last `window` BarData objects from aggregated bars.

    Returns them in reverse chronological order:
      [0] = CC (current candle)
      [1] = C1 (previous candle)
      [2] = C2
      [3] = C3
      ...

    This is the format the engine functions expect.

    Args:
        agg_bars: aggregated bars, oldest-first
        window: how many bars to return (default 4 for CC/C1/C2/C3)

    Returns:
        List of BarData in reverse chronological order.

    Raises:
        ValueError: if fewer bars than requested are available.
    """
    if len(agg_bars) < window:
        raise ValueError(
            f"Need at least {window} aggregated bars, got {len(agg_bars)}"
        )

    # Take the last `window` bars and reverse
    tail = agg_bars[-window:]
    return [ab.bar for ab in reversed(tail)]
