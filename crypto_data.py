"""
Crypto data layer — Yahoo Finance daily bars -> the same DailyBar series the
stock scan uses.

Why Yahoo and not Polygon
-------------------------
The stock scan's Polygon call is a stock-only endpoint, and whether
Polygon's FREE plan includes crypto could not be established (its own
blog says yes, a third-party pricing page says no, and it cannot be tested
without your key).  yfinance is already a dependency, needs no key, and is
already running successfully in this repo's CI (VIX, GEX).  The tradeoff
is the same one gex.py documents: it is an unofficial endpoint, not a
contracted API.  The download step is isolated behind `Downloader` so a
Polygon (or Coinbase/Kraken) provider can be swapped in without touching
anything else.

Conventions (these matter for correctness)
------------------------------------------
* A crypto "day" is a UTC calendar day (what TradingView uses for crypto).
  Weekends are real bars, so a weekly bar has 7 days, and the existing
  ISO-week (Mon-Sun) aggregation in timeframes.py is already right.
* Only FULLY CLOSED UTC days are scanned by default.  The in-progress
  day's bar is a partial bar and would be classified as if final — the
  same trap data.py avoids for stocks with session_is_final().
* Yahoo's crypto `Volume` is already denominated in USD (BTC-USD shows
  ~$40B).  Stocks use close x volume for dollar volume; here the volume
  IS the dollar volume.  Multiplying by price would inflate it by 5-10
  orders of magnitude for BTC.  (Confidence: medium-high — consistent with
  Yahoo's published 24h volumes; not testable from the build sandbox.
  derive_crypto_universe() fails loudly if the result is implausibly
  small, which is what a wrong unit would produce.)
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Optional

from config import ScannerConfig
from crypto_universe import CryptoAsset, get_candidates
from timeframes import DailyBar

logger = logging.getLogger(__name__)

# yahoo symbol -> bars (only symbols that returned data are present)
Downloader = Callable[[list[str], date, date], dict[str, list[DailyBar]]]

DOWNLOAD_CHUNK_SIZE = 40
DOWNLOAD_ATTEMPTS = 3
MIN_COVERAGE = 0.5          # fraction of candidates that must return data
MIN_UNIVERSE = 20           # below this, something is wrong (units / outage)


class CryptoDataError(RuntimeError):
    """Raised when the data is too incomplete to publish a trustworthy scan."""


# =========================================================================
# CALENDAR
# =========================================================================

def utc_today(now: Optional[datetime] = None) -> date:
    now = now or datetime.now(timezone.utc)
    return now.astimezone(timezone.utc).date()


def last_closed_day(now: Optional[datetime] = None) -> date:
    """Most recent UTC calendar day that has fully ended (i.e. yesterday)."""
    return utc_today(now) - timedelta(days=1)


# =========================================================================
# YAHOO PARSING
# =========================================================================

def _num(x) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return math.nan
    return v


def _row_to_bar(d: date, o, h, l, c, v) -> Optional[DailyBar]:
    """Validate one OHLCV row; return None for rows that must not be scanned."""
    o, h, l, c, v = _num(o), _num(h), _num(l), _num(c), _num(v)
    if any(math.isnan(x) or math.isinf(x) for x in (o, h, l, c)):
        return None
    if min(o, h, l, c) <= 0:
        return None
    if math.isnan(v) or math.isinf(v) or v < 0:
        v = 0.0
    # Yahoo occasionally emits a high/low that does not contain the body.
    # A candle's range is by definition at least its body; clamp.
    h = max(h, o, c, l)
    l = min(l, o, c, h)
    # Stale fill: a perfectly flat bar with zero volume is a placeholder,
    # not a market print, and would read as a fake "inside bar".
    if o == h == l == c and v == 0.0:
        return None
    return DailyBar(dt=d, open=o, high=h, low=l, close=c, volume=v)


def _frame_to_bars(sub) -> list[DailyBar]:
    import pandas as pd

    by_date: dict[date, DailyBar] = {}
    for ts, row in sub.iterrows():
        t = pd.Timestamp(ts)
        if t.tzinfo is not None:
            t = t.tz_convert("UTC")
        bar = _row_to_bar(
            t.date(),
            row.get("Open"), row.get("High"), row.get("Low"),
            row.get("Close"), row.get("Volume"),
        )
        if bar is not None:
            by_date[bar.dt] = bar          # duplicate dates: last one wins
    return [by_date[d] for d in sorted(by_date)]


def parse_yf_frame(df, symbols: list[str]) -> dict[str, list[DailyBar]]:
    """Turn a yfinance download frame into {yahoo_symbol: [DailyBar]}.

    Handles both column layouts yfinance has shipped: (Field, Ticker) and
    (Ticker, Field), plus the flat layout older versions return for a
    single symbol.
    """
    import pandas as pd

    if df is None or len(df) == 0:
        return {}

    fields = {"Open", "High", "Low", "Close", "Volume"}
    out: dict[str, list[DailyBar]] = {}

    if isinstance(df.columns, pd.MultiIndex):
        if df.columns.nlevels != 2:
            return {}
        field_lvl = next(
            (i for i in (0, 1) if fields & set(df.columns.get_level_values(i))),
            None,
        )
        if field_lvl is None:
            return {}
        sym_lvl = 1 - field_lvl
        for sym in dict.fromkeys(df.columns.get_level_values(sym_lvl)):
            bars = _frame_to_bars(df.xs(sym, axis=1, level=sym_lvl))
            if bars:
                out[str(sym)] = bars
    elif len(symbols) == 1 and fields & set(df.columns):
        bars = _frame_to_bars(df)
        if bars:
            out[symbols[0]] = bars
    return out


def yahoo_downloader(
    symbols: list[str], start: date, end: date
) -> dict[str, list[DailyBar]]:
    """Default Downloader: one bulk yfinance call for a chunk of symbols.

    `end` is inclusive here; yfinance's own end is exclusive.
    """
    import yfinance as yf

    df = yf.download(
        symbols,
        start=start.isoformat(),
        end=(end + timedelta(days=1)).isoformat(),
        interval="1d",
        auto_adjust=False,   # crypto has no splits; keep raw OHLC
        progress=False,
        threads=True,
        group_by="column",
    )
    return parse_yf_frame(df, symbols)


# =========================================================================
# UNIVERSE
# =========================================================================

def derive_crypto_universe(
    bars_by_ticker: dict[str, list[DailyBar]],
    config: ScannerConfig,
) -> list[str]:
    """Top N coins by average dollar volume, plus the market tickers.

    Same rule as data.derive_universe(): average over the lookback window,
    require at least half the window to have traded, apply the floor, rank,
    truncate.  The one difference is the unit — Yahoo crypto volume is
    already USD, so dollar volume == volume (see module docstring).
    """
    lookback = config.liquidity_lookback_days
    need = max(4, lookback // 2)
    avg_dvol: dict[str, float] = {}
    for ticker, bars in bars_by_ticker.items():
        recent = bars[-lookback:]
        if len(recent) < need:
            continue
        avg = sum(b.volume for b in recent) / len(recent)
        if avg >= config.crypto_min_dollar_volume:
            avg_dvol[ticker] = avg

    ranked = sorted(avg_dvol, key=lambda t: avg_dvol[t], reverse=True)
    universe = set(ranked[: config.crypto_universe_size])
    for m in config.crypto_market_tickers:
        if m in bars_by_ticker:
            universe.add(m)

    if len(universe) < MIN_UNIVERSE:
        raise CryptoDataError(
            f"Only {len(universe)} coins cleared the "
            f"${config.crypto_min_dollar_volume:,.0f}/day liquidity floor "
            f"(need >= {MIN_UNIVERSE}). That points to a data outage or a "
            f"volume-unit problem, not a quiet market — refusing to publish."
        )
    return sorted(universe)


# =========================================================================
# DATA MANAGER
# =========================================================================

class CryptoDataManager:
    """Fetches, validates and serves daily bars for the crypto scan.

    Mirrors the slice of DataManager the scan loop uses: fetch_history(),
    get_universe(), get_ticker_bars().
    """

    def __init__(
        self,
        config: ScannerConfig,
        downloader: Optional[Downloader] = None,
        sleep: Callable[[float], None] = time.sleep,
        freshness_retries: int = 3,
        freshness_wait_seconds: float = 300.0,
    ):
        self.config = config
        self.downloader = downloader or yahoo_downloader
        self._sleep = sleep
        self.freshness_retries = freshness_retries
        self.freshness_wait = freshness_wait_seconds

        self.assets: list[CryptoAsset] = get_candidates(config.crypto_extra_symbols)
        self.bars: dict[str, list[DailyBar]] = {}    # ticker -> bars
        self.skipped: list[str] = []                 # candidates with no usable data
        self.as_of: Optional[date] = None
        self._universe: Optional[list[str]] = None

    # -- download ------------------------------------------------------

    def _download_chunk(
        self, symbols: list[str], start: date, end: date
    ) -> dict[str, list[DailyBar]]:
        for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
            try:
                return self.downloader(symbols, start, end)
            except Exception as e:  # network / Yahoo / parsing
                logger.warning(
                    f"Crypto download chunk failed (attempt {attempt}/"
                    f"{DOWNLOAD_ATTEMPTS}): {e}"
                )
                if attempt < DOWNLOAD_ATTEMPTS:
                    self._sleep(2.0 * attempt)
        return {}

    def _fetch_all(self, start: date, end: date) -> dict[str, list[DailyBar]]:
        got: dict[str, list[DailyBar]] = {}
        symbols = [a.yahoo for a in self.assets]
        for i in range(0, len(symbols), DOWNLOAD_CHUNK_SIZE):
            chunk = symbols[i : i + DOWNLOAD_CHUNK_SIZE]
            got.update(self._download_chunk(chunk, start, end))
            if i + DOWNLOAD_CHUNK_SIZE < len(symbols):
                self._sleep(0.5)
        return got

    def _market_is_fresh(self, as_of: date) -> bool:
        return all(
            self.bars.get(m) and self.bars[m][-1].dt >= as_of
            for m in self.config.crypto_market_tickers
        )

    def fetch_history(
        self,
        as_of: Optional[date] = None,
        include_partial: bool = False,
    ) -> None:
        """Download history ending at `as_of` (inclusive).

        Default: the last fully closed UTC day.  `include_partial=True`
        scans through today's in-progress bar (ad-hoc runs only).  An
        explicit `as_of` that is today or later is clamped to the last
        closed day unless include_partial is set — a partial bar is never
        scanned by accident.
        """
        today = utc_today()
        if as_of is None:
            as_of = today if include_partial else today - timedelta(days=1)
        elif as_of >= today and not include_partial:
            logger.warning(
                f"as_of {as_of} is not a closed UTC day; using "
                f"{today - timedelta(days=1)} (pass --include-partial to override)"
            )
            as_of = today - timedelta(days=1)
        self.as_of = as_of

        start = as_of - timedelta(days=self.config.crypto_history_calendar_days)
        by_yahoo: dict[str, list[DailyBar]] = {}

        for attempt in range(self.freshness_retries + 1):
            by_yahoo = self._fetch_all(start, as_of)
            self.bars = {}
            self.skipped = []
            for asset in self.assets:
                bars = [b for b in by_yahoo.get(asset.yahoo, []) if b.dt <= as_of]
                if len(bars) < 4:        # engine needs CC, C1, C2, C3
                    self.skipped.append(asset.ticker)
                else:
                    self.bars[asset.ticker] = bars
            if self._market_is_fresh(as_of) or attempt == self.freshness_retries:
                break
            logger.warning(
                f"Market tickers have no bar for {as_of} yet (Yahoo lag?). "
                f"Waiting {self.freshness_wait:.0f}s and retrying "
                f"({attempt + 1}/{self.freshness_retries})..."
            )
            self._sleep(self.freshness_wait)

        # ---- publish guards: fail loudly rather than ship a bad scan ----
        missing_market = [
            m for m in self.config.crypto_market_tickers if m not in self.bars
        ]
        if missing_market:
            raise CryptoDataError(
                f"No data for market ticker(s) {missing_market} — Yahoo "
                f"outage or blocked. Refusing to publish."
            )
        if not self._market_is_fresh(as_of):
            raise CryptoDataError(
                f"Market tickers' newest bar is older than {as_of} after "
                f"{self.freshness_retries} retries. Refusing to publish a "
                f"stale scan."
            )
        coverage = len(self.bars) / max(1, len(self.assets))
        if coverage < MIN_COVERAGE:
            raise CryptoDataError(
                f"Only {len(self.bars)}/{len(self.assets)} candidates "
                f"returned data ({coverage:.0%} < {MIN_COVERAGE:.0%}). "
                f"Refusing to publish."
            )
        logger.info(
            f"Crypto history loaded through {as_of}: {len(self.bars)} coins "
            f"with data, {len(self.skipped)} skipped"
            + (f" ({', '.join(self.skipped)})" if self.skipped else "")
        )

    # -- accessors -----------------------------------------------------

    def get_universe(self) -> list[str]:
        if self._universe is None:
            self._universe = derive_crypto_universe(self.bars, self.config)
        return self._universe

    def get_ticker_bars(self, ticker: str) -> list[DailyBar]:
        return self.bars.get(ticker, [])

    def meta(self, tickers: list[str]) -> dict[str, dict]:
        """Display name + Yahoo symbol per ticker, for the JSON/dashboard."""
        by_ticker = {a.ticker: a for a in self.assets}
        return {
            t: {"name": by_ticker[t].name, "yahoo": by_ticker[t].yahoo}
            for t in tickers
            if t in by_ticker
        }
