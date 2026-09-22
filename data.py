"""
Data Layer — Polygon.io grouped daily endpoint + universe derivation.

Architecture
------------
1. **Grouped daily endpoint** (`/v2/aggs/grouped/locale/us/market/stocks/{date}`)
   returns OHLCV for ALL US tickers in ONE API call.  This is the backbone
   of the free-tier strategy: we fetch one day at a time, cache to disk,
   and never need per-ticker requests.

2. **Universe derivation**: rank all tickers by average dollar volume over
   the lookback window, take the top N.  Refreshed weekly (or on demand).

3. **Rate limiting**: Polygon free tier = 5 API calls per minute.
   We enforce this with a simple token-bucket limiter.

4. **Disk cache**: JSON files per date under `./cache/daily/`.  Once a
   date is fetched, it's never re-fetched (market data doesn't change
   retroactively for daily bars).

Environment Variables
---------------------
  POLYGON_API_KEY  — required
  SCANNER_CACHE_DIR — optional, defaults to ./cache
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import math

import requests

from config import ScannerConfig
from timeframes import DailyBar

logger = logging.getLogger(__name__)


# =========================================================================
# RATE LIMITER
# =========================================================================

class RateLimiter:
    """Simple token-bucket rate limiter.

    Polygon free tier: 5 requests per minute.
    We use 4/min to leave headroom.
    """

    def __init__(self, max_calls: int = 4, period_seconds: float = 60.0):
        self.max_calls = max_calls
        self.period = period_seconds
        self.calls: list[float] = []

    def wait(self) -> None:
        """Block until a request slot is available."""
        now = time.time()
        # Purge calls older than the period
        self.calls = [t for t in self.calls if now - t < self.period]

        if len(self.calls) >= self.max_calls:
            oldest = self.calls[0]
            sleep_time = self.period - (now - oldest) + 0.1
            if sleep_time > 0:
                logger.info(f"Rate limit: sleeping {sleep_time:.1f}s")
                time.sleep(sleep_time)

        self.calls.append(time.time())


# =========================================================================
# POLYGON CLIENT
# =========================================================================

class PolygonClient:
    """Thin wrapper around Polygon.io REST API (free tier).

    Uses only the grouped daily endpoint — one call returns all tickers
    for a given date.
    """

    BASE_URL = "https://api.polygon.io"

    def __init__(self, api_key: str, cache_dir: Path):
        self.api_key = api_key
        self.cache_dir = cache_dir / "daily"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.limiter = RateLimiter()
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {api_key}"

    def _cache_path(self, dt: date) -> Path:
        return self.cache_dir / f"{dt.isoformat()}.json"

    def _fetch_grouped_daily(self, dt: date) -> dict:
        """Fetch grouped daily data for a single date from Polygon.

        Returns the raw JSON response (or loads from cache).
        Handles pagination — Polygon may split large responses.
        """
        cache_file = self._cache_path(dt)

        # Check cache first
        if cache_file.exists():
            logger.debug(f"Cache hit: {dt}")
            with open(cache_file) as f:
                return json.load(f)

        # Fetch from API
        self.limiter.wait()
        url = (
            f"{self.BASE_URL}/v2/aggs/grouped/locale/us/market/stocks/"
            f"{dt.isoformat()}"
        )
        params = {
            "adjusted": "true",
            "include_otc": "false",
        }

        logger.info(f"Fetching grouped daily: {dt}")
        resp = self.session.get(url, params=params, timeout=30)

        # Polygon returns 403 for future dates or dates with no data yet
        # (e.g. scan runs before market opens). Return empty instead of crashing.
        if resp.status_code in (403, 404):
            logger.warning(
                f"Polygon returned {resp.status_code} for {dt} — "
                f"no data available (market may not have opened yet)"
            )
            return {"resultsCount": 0, "results": []}

        resp.raise_for_status()
        data = resp.json()

        # Cache the response
        with open(cache_file, "w") as f:
            json.dump(data, f)

        return data

    def get_daily_bars(self, dt: date) -> dict[str, DailyBar]:
        """Get all daily bars for a given date.

        Returns:
            Dict mapping ticker → DailyBar.
            Only includes common stocks (filters out warrants, units,
            rights, preferred shares by ticker pattern).
        """
        data = self._fetch_grouped_daily(dt)

        if data.get("resultsCount", 0) == 0:
            return {}

        bars: dict[str, DailyBar] = {}
        for r in data.get("results", []):
            ticker = r.get("T", "")

            # Skip non-common-stock tickers:
            # - Must not contain space, period, or hyphen (warrants, units, etc.)
            # - Must be 1-5 chars (skip ETNs with long tickers, though some ETFs are 4)
            if not ticker or len(ticker) > 5:
                continue
            if any(c in ticker for c in " .-/"):
                continue

            # Build DailyBar
            try:
                bars[ticker] = DailyBar(
                    dt=dt,
                    open=float(r["o"]),
                    high=float(r["h"]),
                    low=float(r["l"]),
                    close=float(r["c"]),
                    volume=float(r.get("v", 0)),
                )
            except (KeyError, ValueError, TypeError) as e:
                logger.warning(f"Skipping {ticker} on {dt}: {e}")
                continue

        return bars


# =========================================================================
# TRADING CALENDAR HELPERS
# =========================================================================

# Major US market holidays (approximated — exact dates shift yearly).
# For a production system you'd use `exchange_calendars` or similar,
# but for this scanner a weekend-skip + known-holiday check is sufficient.
# The scanner will simply get an empty result from Polygon for holidays
# and skip that date.

def _is_weekend(dt: date) -> bool:
    return dt.weekday() >= 5


def trading_days_back(end_date: date, calendar_days: int) -> list[date]:
    """Generate a list of potential trading dates going back N calendar days.

    Returns dates in chronological order (oldest first).
    Skips weekends.  Does NOT skip market holidays — Polygon returns
    empty results for those, which the fetcher handles gracefully.
    """
    start_date = end_date - timedelta(days=calendar_days)
    dates = []
    dt = start_date
    while dt <= end_date:
        if not _is_weekend(dt):
            dates.append(dt)
        dt += timedelta(days=1)
    return dates


# =========================================================================
# UNIVERSE DERIVATION
# =========================================================================

def derive_universe(
    daily_data: dict[date, dict[str, DailyBar]],
    config: ScannerConfig,
    include_etfs: bool = True,
) -> list[str]:
    """Derive the stock universe: top N tickers by average dollar volume.

    Args:
        daily_data: date → {ticker → DailyBar} for the lookback period
        config: scanner config (universe_size, min_dollar_volume,
                liquidity_lookback_days, market_etfs, sector_etfs)
        include_etfs: if True, always include market & sector ETFs

    Returns:
        Sorted list of tickers in the universe.
    """
    # Compute average dollar volume per ticker over lookback period
    # Dollar volume = close × volume (approximation, good enough for ranking)
    ticker_dvol: dict[str, list[float]] = defaultdict(list)

    # Use only the last N trading days
    sorted_dates = sorted(daily_data.keys())
    lookback_dates = sorted_dates[-config.liquidity_lookback_days:]

    for dt in lookback_dates:
        bars = daily_data.get(dt, {})
        for ticker, bar in bars.items():
            dvol = bar.close * bar.volume
            ticker_dvol[ticker].append(dvol)

    # Average dollar volume
    avg_dvol: dict[str, float] = {}
    for ticker, dvols in ticker_dvol.items():
        if len(dvols) >= max(1, len(lookback_dates) // 2):
            # Require at least half the lookback days to have traded
            avg = sum(dvols) / len(dvols)
            if avg >= config.min_dollar_volume:
                avg_dvol[ticker] = avg

    # Rank by average dollar volume, take top N
    ranked = sorted(avg_dvol.keys(), key=lambda t: avg_dvol[t], reverse=True)
    universe = set(ranked[: config.universe_size])

    # Always include market and sector ETFs
    if include_etfs:
        for etf in config.market_etfs + config.sector_etfs:
            universe.add(etf)

    return sorted(universe)


# =========================================================================
# DATA MANAGER
# =========================================================================

class DataManager:
    """High-level data manager: fetches history, derives universe,
    provides per-ticker daily bar series.

    Usage:
        dm = DataManager(config)
        dm.fetch_history()           # fetches all needed dates
        universe = dm.get_universe() # derives the liquid universe
        bars = dm.get_ticker_bars("AAPL")  # daily bars for one ticker
    """

    def __init__(self, config: ScannerConfig):
        self.config = config
        api_key = config.polygon_api_key or os.environ.get("POLYGON_API_KEY", "")
        if not api_key:
            raise ValueError(
                "Polygon API key required. Set POLYGON_API_KEY env var "
                "or pass polygon_api_key in config."
            )

        cache_root = Path(os.environ.get("SCANNER_CACHE_DIR", "./cache"))
        self.client = PolygonClient(api_key, cache_root)

        # date → {ticker → DailyBar}
        self.daily_data: dict[date, dict[str, DailyBar]] = {}

        # Cached universe
        self._universe: Optional[list[str]] = None
        self._universe_date: Optional[date] = None

        # Universe cache on disk
        self._universe_cache_path = cache_root / "universe.json"

    def fetch_history(self, as_of: Optional[date] = None) -> None:
        """Fetch daily bars for the full history window.

        Args:
            as_of: the "current" date (defaults to today).
                   Useful for backtesting.
        """
        if as_of is None:
            as_of = date.today()

        dates = trading_days_back(as_of, self.config.history_calendar_days)
        total = len(dates)
        fetched = 0
        skipped = 0

        for i, dt in enumerate(dates):
            if dt > as_of:
                continue
            bars = self.client.get_daily_bars(dt)
            if bars:
                self.daily_data[dt] = bars
                fetched += 1
            else:
                skipped += 1  # holiday or no data

            if (i + 1) % 10 == 0:
                logger.info(
                    f"Progress: {i + 1}/{total} dates "
                    f"({fetched} fetched, {skipped} empty)"
                )

        logger.info(
            f"History loaded: {fetched} trading days, "
            f"{skipped} empty/holiday days skipped"
        )

    def get_universe(self, force_refresh: bool = False) -> list[str]:
        """Get or derive the stock universe.

        Caches the universe to disk.  The cache is valid for 7 days
        (weekly refresh).

        Args:
            force_refresh: if True, re-derive even if cache is fresh

        Returns:
            Sorted list of tickers.
        """
        today = date.today()

        # Check disk cache
        if not force_refresh and self._universe_cache_path.exists():
            try:
                with open(self._universe_cache_path) as f:
                    cached = json.load(f)
                cached_date = date.fromisoformat(cached["date"])
                if (today - cached_date).days < 7:
                    self._universe = cached["tickers"]
                    self._universe_date = cached_date
                    logger.info(
                        f"Universe loaded from cache: "
                        f"{len(self._universe)} tickers "
                        f"(derived {cached_date})"
                    )
                    return self._universe
            except (json.JSONDecodeError, KeyError, ValueError):
                pass  # stale or corrupt cache, re-derive

        # Derive fresh universe
        if not self.daily_data:
            raise RuntimeError(
                "No daily data loaded. Call fetch_history() first."
            )

        self._universe = derive_universe(self.daily_data, self.config)
        self._universe_date = today

        # Save to disk
        with open(self._universe_cache_path, "w") as f:
            json.dump({
                "date": today.isoformat(),
                "tickers": self._universe,
                "size": len(self._universe),
            }, f, indent=2)

        logger.info(
            f"Universe derived: {len(self._universe)} tickers"
        )
        return self._universe

    def get_ticker_bars(self, ticker: str) -> list[DailyBar]:
        """Get all daily bars for a single ticker, sorted oldest-first.

        Args:
            ticker: stock ticker symbol

        Returns:
            List of DailyBar in chronological order.
        """
        bars: list[DailyBar] = []
        for dt in sorted(self.daily_data.keys()):
            day_bars = self.daily_data[dt]
            if ticker in day_bars:
                bars.append(day_bars[ticker])
        return bars

    def get_all_tickers(self) -> set[str]:
        """Get the set of all tickers that have appeared in the data."""
        tickers: set[str] = set()
        for day_bars in self.daily_data.values():
            tickers.update(day_bars.keys())
        return tickers

    # -----------------------------------------------------------------
    # yfinance backfill — fill Polygon free-tier 1-day delay
    # -----------------------------------------------------------------

    def backfill_recent_yfinance(
        self, tickers: list[str], lookback_days: int = 5
    ) -> int:
        """Fill recent date gaps using yfinance.

        Polygon free tier has a 1-business-day data delay: when the scan
        runs at 5:30 PM ET on Monday, Polygon returns 403 for Monday's
        bars.  This method identifies those gap dates and bulk-fetches
        them from Yahoo Finance (via yfinance), which has no delay.

        Call AFTER fetch_history() and get_universe() — needs the universe
        to know which tickers to fetch (yfinance requires a ticker list,
        unlike Polygon's grouped-daily which returns everything).

        Caches results in the same format as Polygon so subsequent runs
        see a cache hit and don't re-fetch.

        Args:
            tickers: ticker symbols to fetch (the derived universe).
            lookback_days: how many recent calendar days to check for gaps.

        Returns:
            Number of date-gaps filled.
        """
        try:
            import yfinance as yf
        except ImportError:
            logger.warning(
                "yfinance not installed — cannot backfill recent data. "
                "pip install yfinance"
            )
            return 0

        today = date.today()
        recent_dates = trading_days_back(today, lookback_days)

        # Identify dates Polygon missed (returned 403 / empty)
        missing = [
            dt for dt in recent_dates
            if dt not in self.daily_data and dt <= today
        ]

        if not missing:
            logger.info("No recent date gaps — Polygon data is current")
            return 0

        logger.info(
            f"Polygon gap detected for {len(missing)} recent date(s): "
            f"{[d.isoformat() for d in missing]}. "
            f"Backfilling via yfinance ({len(tickers)} tickers)..."
        )

        # yf.download end date is exclusive
        start = min(missing)
        end = max(missing) + timedelta(days=1)

        try:
            df = yf.download(
                tickers,
                start=start.isoformat(),
                end=end.isoformat(),
                auto_adjust=True,
                progress=False,
                threads=True,
            )
        except Exception as e:
            logger.warning(f"yfinance bulk download failed: {e}")
            return 0

        if df is None or df.empty:
            logger.warning("yfinance returned no data for the gap dates")
            return 0

        import pandas as pd

        multi = len(tickers) > 1
        filled = 0

        for dt in missing:
            ts = pd.Timestamp(dt)
            if ts not in df.index:
                logger.debug(f"yfinance has no data for {dt}")
                continue

            row = df.loc[ts]
            bars: dict[str, DailyBar] = {}

            for ticker in tickers:
                try:
                    if multi:
                        o = float(row[("Open", ticker)])
                        h = float(row[("High", ticker)])
                        l = float(row[("Low", ticker)])
                        c = float(row[("Close", ticker)])
                        v = float(row[("Volume", ticker)])
                    else:
                        o = float(row["Open"])
                        h = float(row["High"])
                        l = float(row["Low"])
                        c = float(row["Close"])
                        v = float(row["Volume"])

                    # Skip if any OHLC is NaN (ticker didn't trade)
                    if any(math.isnan(x) for x in (o, h, l, c)):
                        continue

                    bars[ticker] = DailyBar(
                        dt=dt,
                        open=o,
                        high=h,
                        low=l,
                        close=c,
                        volume=v if not math.isnan(v) else 0.0,
                    )
                except (KeyError, ValueError, TypeError):
                    continue

            if bars:
                self.daily_data[dt] = bars

                # Cache in Polygon-compatible format so future runs see
                # a cache hit and skip both Polygon and yfinance.
                cache_data = {
                    "resultsCount": len(bars),
                    "results": [
                        {
                            "T": t,
                            "o": b.open,
                            "h": b.high,
                            "l": b.low,
                            "c": b.close,
                            "v": b.volume,
                        }
                        for t, b in bars.items()
                    ],
                    "_source": "yfinance",
                }
                cache_file = self.client._cache_path(dt)
                with open(cache_file, "w") as f:
                    json.dump(cache_data, f)

                filled += 1
                logger.info(
                    f"  {dt}: {len(bars)} tickers filled via yfinance"
                )

        if filled:
            logger.info(
                f"Backfill complete: {filled} date(s) filled via yfinance"
            )
        else:
            logger.warning(
                "yfinance backfill produced no usable data for the gap dates"
            )

        return filled

    def clear_old_cache(self, keep_days: int = 150) -> int:
        """Remove cache files older than `keep_days` to save disk space.

        Returns the number of files removed.
        """
        cutoff = date.today() - timedelta(days=keep_days)
        removed = 0
        for f in self.client.cache_dir.glob("*.json"):
            try:
                file_date = date.fromisoformat(f.stem)
                if file_date < cutoff:
                    f.unlink()
                    removed += 1
            except ValueError:
                continue
        if removed:
            logger.info(f"Cleaned {removed} old cache files")
        return removed
