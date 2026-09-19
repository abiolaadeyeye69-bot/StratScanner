"""
Sector / Subsector / Thematic Rotation Ranking.

Scanner-native — not a translation of any Pine Script. Computes
trading-day returns for every tracked ETF (broad sector, subsector, or
theme — see sector_universe.py) over configurable lookback periods
(default 1D / 5D / 20D) and ranks ALL of them against each other in one
cross-sectional table per period, not sector-only vs subsector-only —
"rank the sectors and subsectors against each other" means one combined
leaderboard.

"1D / 5D / 20D" means N TRADING days of daily bars (today's close vs
the close N bars back), the standard convention for relative-strength
rotation tools — not N calendar days.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from config import ScannerConfig
from sector_universe import SectorGroup, get_sector_universe
from timeframes import DailyBar


# =========================================================================
# DATA STRUCTURES
# =========================================================================

@dataclass
class RotationEntry:
    """One tracked ETF's returns and ranks across all configured periods."""
    ticker: str
    label: str
    category: str                              # "sector", "subsector", "thematic"
    parent: Optional[str] = None               # parent sector ticker, for subsectors

    returns: dict[int, float] = field(default_factory=dict)   # period -> % return
    ranks: dict[int, int] = field(default_factory=dict)       # period -> rank (1=best)
    composite_rank: Optional[float] = None                    # avg rank across periods

    last_close: Optional[float] = None
    as_of_date: Optional[date] = None


@dataclass
class RotationResult:
    """Full rotation ranking result for one scan."""
    periods: list[int]
    entries: list[RotationEntry] = field(default_factory=list)
    as_of_date: Optional[date] = None
    skipped_tickers: list[str] = field(default_factory=list)   # no data available


# =========================================================================
# RETURN CALCULATION
# =========================================================================

def compute_return(daily_bars: list[DailyBar], lookback_days: int) -> Optional[float]:
    """Percent return over `lookback_days` TRADING days.

    Compares the most recent close to the close `lookback_days` bars
    back in the same chronologically-sorted series.

    Args:
        daily_bars: chronologically sorted daily bars (oldest first)
        lookback_days: number of trading days to look back

    Returns:
        Percent return (e.g. 2.35 for +2.35%), or None if there isn't
        enough history to compute it.
    """
    if lookback_days < 1:
        raise ValueError(f"lookback_days must be >= 1, got {lookback_days}")
    if len(daily_bars) < lookback_days + 1:
        return None

    latest = daily_bars[-1].close
    prior = daily_bars[-1 - lookback_days].close
    if prior == 0:
        return None

    return (latest / prior - 1.0) * 100.0


# =========================================================================
# MAIN RANKING FUNCTION
# =========================================================================

def compute_sector_rotation(
    bars_by_ticker: dict[str, list[DailyBar]],
    config: ScannerConfig,
) -> RotationResult:
    """Compute returns and cross-sectional ranks for every tracked
    sector/subsector/thematic ETF.

    Every tracked group (sector, subsector, and theme alike) is ranked
    in ONE combined table per period — this is what "rank the sectors
    and subsectors against each other" means: a semiconductor subsector
    ETF can outrank a broad sector ETF, and does whenever its return is
    actually higher.

    Args:
        bars_by_ticker: ticker -> chronologically sorted daily bars.
                         A ticker with no data (delisted, renamed, not
                         yet returned by the data source) is skipped,
                         not treated as an error.
        config: scanner config (sector_rotation_periods,
                sector_rotation_include_subsectors/thematic)

    Returns:
        RotationResult with one RotationEntry per ticker that had data,
        per-period ranks, and a composite (average) rank.
    """
    periods = config.sector_rotation_periods
    groups = get_sector_universe(config)

    entries: list[RotationEntry] = []
    skipped: list[str] = []
    as_of: Optional[date] = None

    for group in groups:
        bars = bars_by_ticker.get(group.ticker)
        if not bars:
            skipped.append(group.ticker)
            continue

        entry = RotationEntry(
            ticker=group.ticker, label=group.label,
            category=group.category, parent=group.parent,
            last_close=bars[-1].close, as_of_date=bars[-1].dt,
        )
        if as_of is None or bars[-1].dt > as_of:
            as_of = bars[-1].dt

        for period in periods:
            ret = compute_return(bars, period)
            if ret is not None:
                entry.returns[period] = ret

        entries.append(entry)

    # --- Rank each period independently across ALL entries (one
    #     combined cross-sectional ranking, not per-category) ---
    for period in periods:
        ranked = sorted(
            (e for e in entries if period in e.returns),
            key=lambda e: e.returns[period],
            reverse=True,   # highest return = rank 1
        )
        for i, entry in enumerate(ranked):
            entry.ranks[period] = i + 1

    # --- Composite rank: average of available per-period ranks ---
    for entry in entries:
        if entry.ranks:
            entry.composite_rank = sum(entry.ranks.values()) / len(entry.ranks)

    return RotationResult(
        periods=periods, entries=entries, as_of_date=as_of, skipped_tickers=skipped,
    )


# =========================================================================
# LEADERBOARD HELPERS
# =========================================================================

def leaders(
    result: RotationResult,
    period: int,
    top_n: int = 10,
    category: Optional[str] = None,
) -> list[RotationEntry]:
    """Top-N performers for a given period.

    Args:
        result: a computed RotationResult
        period: which lookback period to rank by (must be in result.periods)
        top_n: how many entries to return
        category: optional filter — "sector", "subsector", or "thematic";
                  None (default) ranks across all categories combined
    """
    pool = [e for e in result.entries if period in e.returns]
    if category is not None:
        pool = [e for e in pool if e.category == category]
    return sorted(pool, key=lambda e: e.returns[period], reverse=True)[:top_n]


def laggards(
    result: RotationResult,
    period: int,
    bottom_n: int = 5,
    category: Optional[str] = None,
) -> list[RotationEntry]:
    """Bottom-N performers for a given period. See leaders() for args."""
    pool = [e for e in result.entries if period in e.returns]
    if category is not None:
        pool = [e for e in pool if e.category == category]
    return sorted(pool, key=lambda e: e.returns[period])[:bottom_n]


def top_by_composite(result: RotationResult, top_n: int = 10) -> list[RotationEntry]:
    """Entries with the strongest composite (average) rank across all
    configured periods — the most consistent leaders, not just the
    single best day."""
    pool = [e for e in result.entries if e.composite_rank is not None]
    return sorted(pool, key=lambda e: e.composite_rank)[:top_n]


# =========================================================================
# FORMATTING
# =========================================================================

def format_rotation_line(entry: RotationEntry, period: int) -> str:
    """Format one entry's line for a single period's leaderboard.

    Example: "  #1  SMH    Semiconductors                 +3.42%  (composite 2.3)"
    """
    ret = entry.returns.get(period)
    rank = entry.ranks.get(period)
    ret_str = f"{ret:+.2f}%" if ret is not None else "n/a"
    rank_str = f"#{rank}" if rank is not None else "n/a"
    composite_str = f"{entry.composite_rank:.1f}" if entry.composite_rank is not None else "n/a"
    return (
        f"{rank_str:>5s} {entry.ticker:6s} {entry.label:32s} "
        f"{ret_str:>8s}  (composite {composite_str})"
    )


def format_period_label(period: int) -> str:
    """1 -> '1D', 5 -> '5D', 20 -> '20D'."""
    return f"{period}D"
