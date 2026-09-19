"""
Tests for sector_rotation.py — cross-sectional return ranking.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from datetime import date, timedelta

from config import ScannerConfig
from timeframes import DailyBar
from sector_universe import SECTORS, SUBSECTORS, THEMATIC, get_sector_universe
from sector_rotation import (
    RotationEntry,
    RotationResult,
    compute_return,
    compute_sector_rotation,
    leaders,
    laggards,
    top_by_composite,
    format_rotation_line,
    format_period_label,
)


# =========================================================================
# HELPERS
# =========================================================================

def _bars(closes: list[float], start: date = date(2026, 6, 1)) -> list[DailyBar]:
    """Build daily bars from a list of closes, skipping weekends."""
    bars = []
    dt = start
    for c in closes:
        while dt.weekday() >= 5:
            dt += timedelta(days=1)
        bars.append(DailyBar(dt=dt, open=c, high=c + 1, low=c - 1, close=c, volume=1_000_000))
        dt += timedelta(days=1)
    return bars


# =========================================================================
# REGISTRY SANITY
# =========================================================================

class TestSectorUniverse:
    def test_no_duplicate_tickers(self):
        all_groups = SECTORS + SUBSECTORS + THEMATIC
        tickers = [g.ticker for g in all_groups]
        assert len(tickers) == len(set(tickers))

    def test_eleven_broad_sectors(self):
        assert len(SECTORS) == 11

    def test_subsectors_have_valid_parents(self):
        sector_tickers = {g.ticker for g in SECTORS}
        for sub in SUBSECTORS:
            assert sub.parent in sector_tickers, f"{sub.ticker} has unknown parent {sub.parent}"

    def test_thematic_groups_have_no_parent(self):
        for theme in THEMATIC:
            assert theme.parent is None

    def test_get_sector_universe_respects_toggles(self):
        config_all = ScannerConfig()
        config_sectors_only = ScannerConfig(
            sector_rotation_include_subsectors=False,
            sector_rotation_include_thematic=False,
        )
        assert len(get_sector_universe(config_all)) == len(SECTORS) + len(SUBSECTORS) + len(THEMATIC)
        assert len(get_sector_universe(config_sectors_only)) == len(SECTORS)


# =========================================================================
# RETURN CALCULATION
# =========================================================================

class TestComputeReturn:
    def test_simple_positive_return(self):
        bars = _bars([100, 101, 102, 103, 104])
        # 1D: 104 vs 103 -> +0.9709%
        r = compute_return(bars, 1)
        assert r == pytest.approx((104 / 103 - 1) * 100)

    def test_negative_return(self):
        bars = _bars([100, 95, 90, 85, 80])
        r = compute_return(bars, 1)
        assert r < 0
        assert r == pytest.approx((80 / 85 - 1) * 100)

    def test_lookback_beyond_available_history(self):
        bars = _bars([100, 101, 102])
        assert compute_return(bars, 5) is None

    def test_exact_boundary_bars(self):
        """Exactly lookback_days+1 bars should just barely work."""
        bars = _bars([100, 105, 110, 115, 120, 125])  # 6 bars
        r = compute_return(bars, 5)
        assert r == pytest.approx((125 / 100 - 1) * 100)

    def test_zero_prior_price_returns_none(self):
        bars = _bars([0, 10])
        assert compute_return(bars, 1) is None

    def test_invalid_lookback_raises(self):
        bars = _bars([100, 101])
        with pytest.raises(ValueError):
            compute_return(bars, 0)


# =========================================================================
# CROSS-SECTIONAL RANKING
# =========================================================================

class TestComputeSectorRotation:
    def _config(self, **overrides):
        base = dict(sector_rotation_include_subsectors=False, sector_rotation_include_thematic=False)
        base.update(overrides)
        return ScannerConfig(**base)

    def test_ranks_all_entries_together(self):
        """Three sectors with clearly different growth rates should rank
        1/2/3 consistently across every period."""
        strong = _bars([100 + i * 3 for i in range(25)])   # fastest growth
        medium = _bars([100 + i * 2 for i in range(25)])
        weak = _bars([100 + i * 1 for i in range(25)])

        bars_by_ticker = {"XLK": strong, "XLF": medium, "XLE": weak}
        config = self._config()
        result = compute_sector_rotation(bars_by_ticker, config)

        by_ticker = {e.ticker: e for e in result.entries}
        for period in [1, 5, 20]:
            assert by_ticker["XLK"].ranks[period] == 1
            assert by_ticker["XLF"].ranks[period] == 2
            assert by_ticker["XLE"].ranks[period] == 3

    def test_composite_rank_averages_periods(self):
        """An entry ranked 1,2,3 across three periods should have
        composite rank 2.0."""
        bars_by_ticker = {
            "XLK": _bars([100 + i for i in range(25)]),
            "XLF": _bars([100 + i * 0.5 for i in range(25)]),
        }
        config = self._config(sector_rotation_periods=[1, 5, 20])
        result = compute_sector_rotation(bars_by_ticker, config)
        by_ticker = {e.ticker: e for e in result.entries}
        # XLK grows faster -> rank 1 every period -> composite 1.0
        assert by_ticker["XLK"].composite_rank == 1.0
        assert by_ticker["XLF"].composite_rank == 2.0

    def test_missing_ticker_data_skipped_not_errored(self):
        config = ScannerConfig()  # full registry (61 groups)
        bars_by_ticker = {"XLK": _bars([100 + i for i in range(25)])}
        result = compute_sector_rotation(bars_by_ticker, config)
        assert len(result.entries) == 1
        assert result.entries[0].ticker == "XLK"
        assert "XLF" in result.skipped_tickers
        assert len(result.skipped_tickers) == len(get_sector_universe_count(config)) - 1

    def test_insufficient_history_leaves_period_out_of_ranks(self):
        """A ticker with only 3 bars can't have a 20D return — it should
        be excluded from the 20D ranking but still present for 1D."""
        bars_by_ticker = {
            "XLK": _bars([100, 101, 102, 103]),   # only 4 bars total
            "XLF": _bars([100 + i for i in range(25)]),  # full history
        }
        config = self._config()
        result = compute_sector_rotation(bars_by_ticker, config)
        by_ticker = {e.ticker: e for e in result.entries}
        assert 20 not in by_ticker["XLK"].returns
        assert 1 in by_ticker["XLK"].returns
        # XLF should be the only one ranked at 20D
        assert by_ticker["XLF"].ranks[20] == 1
        assert 20 not in by_ticker["XLK"].ranks

    def test_as_of_date_tracks_latest_bar(self):
        bars = _bars([100, 101, 102], start=date(2026, 6, 1))
        config = self._config()
        result = compute_sector_rotation({"XLK": bars}, config)
        assert result.as_of_date == bars[-1].dt

    def test_subsectors_and_sectors_ranked_in_one_table(self):
        """A subsector ETF should be able to outrank a broad sector ETF
        in the SAME combined ranking — this is the literal ask."""
        # SMH (subsector under XLK) grows much faster than XLK itself
        config = ScannerConfig(sector_rotation_include_subsectors=True, sector_rotation_include_thematic=False)
        bars_by_ticker = {
            "XLK": _bars([100 + i * 0.5 for i in range(25)]),
            "SMH": _bars([100 + i * 5 for i in range(25)]),
        }
        result = compute_sector_rotation(bars_by_ticker, config)
        by_ticker = {e.ticker: e for e in result.entries}
        assert by_ticker["SMH"].category == "subsector"
        assert by_ticker["XLK"].category == "sector"
        # SMH should rank #1 despite being a subsector, XLK #2
        assert by_ticker["SMH"].ranks[1] == 1
        assert by_ticker["XLK"].ranks[1] == 2


def get_sector_universe_count(config):
    from sector_universe import get_sector_universe
    return get_sector_universe(config)


# =========================================================================
# LEADERBOARD HELPERS
# =========================================================================

class TestLeadersLaggards:
    def _result(self):
        config = ScannerConfig(sector_rotation_include_subsectors=False, sector_rotation_include_thematic=False)
        bars_by_ticker = {
            "XLK": _bars([100 + i * 3 for i in range(25)]),   # best
            "XLF": _bars([100 + i * 2 for i in range(25)]),   # middle
            "XLE": _bars([100 - i * 1 for i in range(25)]),   # worst (declining)
        }
        return compute_sector_rotation(bars_by_ticker, config)

    def test_leaders_top_n(self):
        result = self._result()
        top = leaders(result, period=1, top_n=2)
        assert len(top) == 2
        assert top[0].ticker == "XLK"
        assert top[1].ticker == "XLF"

    def test_laggards_bottom_n(self):
        result = self._result()
        bottom = laggards(result, period=1, bottom_n=1)
        assert len(bottom) == 1
        assert bottom[0].ticker == "XLE"

    def test_leaders_sorted_descending(self):
        result = self._result()
        top = leaders(result, period=20, top_n=3)
        returns = [e.returns[20] for e in top]
        assert returns == sorted(returns, reverse=True)

    def test_top_by_composite(self):
        result = self._result()
        top = top_by_composite(result, top_n=1)
        assert top[0].ticker == "XLK"

    def test_category_filter(self):
        config = ScannerConfig()
        bars_by_ticker = {
            "XLK": _bars([100 + i * 1 for i in range(25)]),
            "SMH": _bars([100 + i * 5 for i in range(25)]),
        }
        result = compute_sector_rotation(bars_by_ticker, config)
        # Unfiltered: SMH (subsector) wins
        top_all = leaders(result, period=1, top_n=1)
        assert top_all[0].ticker == "SMH"
        # Filtered to sectors only: XLK should be the (only) result
        top_sectors = leaders(result, period=1, top_n=1, category="sector")
        assert top_sectors[0].ticker == "XLK"


# =========================================================================
# FORMATTING
# =========================================================================

class TestFormatting:
    def test_format_rotation_line(self):
        entry = RotationEntry(
            ticker="SMH", label="Semiconductors", category="subsector", parent="XLK",
            returns={1: 3.42}, ranks={1: 1}, composite_rank=2.3,
        )
        line = format_rotation_line(entry, period=1)
        assert "SMH" in line
        assert "+3.42%" in line
        assert "#1" in line
        assert "2.3" in line

    def test_format_rotation_line_missing_period(self):
        entry = RotationEntry(ticker="XLK", label="Technology", category="sector")
        line = format_rotation_line(entry, period=20)
        assert "n/a" in line

    def test_format_period_label(self):
        assert format_period_label(1) == "1D"
        assert format_period_label(5) == "5D"
        assert format_period_label(20) == "20D"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
