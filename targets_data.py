"""
Data layer for the Strat Targets scan (daily + 60-minute bars from Yahoo).

Why a separate data layer
-------------------------
The nightly scan runs on Polygon's free grouped-daily endpoint (5 calls a
minute, ~120 calendar days of history). That cannot supply 60-minute bars for
1,300 tickers, and 120 days is only ~4 monthly bars, which is too shallow for
the Targets ladder. This module uses Yahoo (yfinance) for both stocks and
crypto: ~3 years of daily bars and ~60 days of hourly bars, a handful of bulk
calls per run, no API key.

Reliability caveats (same family as gex.py / crypto_data.py)
------------------------------------------------------------
* yfinance scrapes unofficial Yahoo endpoints. It can rate-limit, change
  shape, or return partial frames. Every guard below exists because of that.
* Stock OHLC is split-adjusted but not dividend-adjusted (auto_adjust=False),
  which matches what TradingView charts by default.
* Hourly stock bars are regular session only: 09:30, 10:30 ... 15:30 ET (the
  last one is a half-hour bar), matching TradingView's 60-minute US session.

Downloaders are injectable so tests never touch the network.
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from timeframes import DailyBar

logger = logging.getLogger("targets.data")

ET = ZoneInfo("America/New_York")
UTC = timezone.utc

DAILY_PERIOD = "3y"
HOURLY_PERIOD = "60d"
CHUNK_SIZE = 100


class TargetsDataError(RuntimeError):
    """Raised when the feed is too thin or stale to publish."""


@dataclass
class HourBar:
    ts: datetime            # bar START, timezone-aware UTC
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


DailyDownloader = Callable[[list[str]], dict[str, list[DailyBar]]]
HourlyDownloader = Callable[[list[str]], dict[str, list[HourBar]]]


# =========================================================================
# PARSING
# =========================================================================

def _num(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return float("nan")


def _clean(o, h, l, c, v) -> Optional[tuple[float, float, float, float, float]]:
    """Validate one OHLCV row. None => the row must not be scanned."""
    o, h, l, c, v = _num(o), _num(h), _num(l), _num(c), _num(v)
    if any(math.isnan(x) or math.isinf(x) for x in (o, h, l, c)):
        return None
    if min(o, h, l, c) <= 0:
        return None
    if math.isnan(v) or math.isinf(v) or v < 0:
        v = 0.0
    h = max(h, o, c, l)          # a range always contains its body
    l = min(l, o, c, h)
    if o == h == l == c and v == 0.0:
        return None              # placeholder fill, not a print
    return o, h, l, c, v


def _symbol_frames(df, symbols: list[str]):
    """Yield (symbol, sub-frame) for either yfinance column layout."""
    import pandas as pd

    if df is None or len(df) == 0:
        return
    fields = {"Open", "High", "Low", "Close", "Volume"}
    if isinstance(df.columns, pd.MultiIndex):
        if df.columns.nlevels != 2:
            return
        field_lvl = next(
            (i for i in (0, 1) if fields & set(df.columns.get_level_values(i))),
            None,
        )
        if field_lvl is None:
            return
        sym_lvl = 1 - field_lvl
        for sym in dict.fromkeys(df.columns.get_level_values(sym_lvl)):
            yield str(sym), df.xs(sym, axis=1, level=sym_lvl)
    elif len(symbols) == 1 and fields & set(df.columns):
        yield symbols[0], df


def parse_daily_frame(df, symbols: list[str]) -> dict[str, list[DailyBar]]:
    import pandas as pd

    out: dict[str, list[DailyBar]] = {}
    for sym, sub in _symbol_frames(df, symbols):
        by_date: dict[date, DailyBar] = {}
        for ts, row in sub.iterrows():
            t = pd.Timestamp(ts)
            if t.tzinfo is not None:
                t = t.tz_convert(UTC) if sym.endswith("-USD") else t.tz_localize(None)
            r = _clean(row.get("Open"), row.get("High"), row.get("Low"),
                       row.get("Close"), row.get("Volume"))
            if r is None:
                continue
            d = t.date()
            by_date[d] = DailyBar(dt=d, open=r[0], high=r[1], low=r[2],
                                  close=r[3], volume=r[4])
        if by_date:
            out[sym] = [by_date[d] for d in sorted(by_date)]
    return out


def parse_hourly_frame(df, symbols: list[str]) -> dict[str, list[HourBar]]:
    import pandas as pd

    out: dict[str, list[HourBar]] = {}
    for sym, sub in _symbol_frames(df, symbols):
        by_ts: dict[datetime, HourBar] = {}
        for ts, row in sub.iterrows():
            t = pd.Timestamp(ts)
            if t.tzinfo is None:
                t = t.tz_localize(UTC)       # Yahoo hourly is tz-aware; be safe
            t = t.tz_convert(UTC)
            r = _clean(row.get("Open"), row.get("High"), row.get("Low"),
                       row.get("Close"), row.get("Volume"))
            if r is None:
                continue
            dt = t.to_pydatetime()
            by_ts[dt] = HourBar(ts=dt, open=r[0], high=r[1], low=r[2],
                                close=r[3], volume=r[4])
        if by_ts:
            out[sym] = [by_ts[k] for k in sorted(by_ts)]
    return out


# =========================================================================
# REAL DOWNLOADERS (one bulk yfinance call per chunk)
# =========================================================================

def yahoo_daily_downloader(symbols: list[str]) -> dict[str, list[DailyBar]]:
    import yfinance as yf

    df = yf.download(
        symbols, period=DAILY_PERIOD, interval="1d",
        auto_adjust=False, progress=False, threads=True, group_by="column",
    )
    return parse_daily_frame(df, symbols)


def yahoo_hourly_downloader(symbols: list[str]) -> dict[str, list[HourBar]]:
    import yfinance as yf

    df = yf.download(
        symbols, period=HOURLY_PERIOD, interval="60m",
        auto_adjust=False, progress=False, threads=True, group_by="column",
        prepost=False,
    )
    return parse_hourly_frame(df, symbols)


def fetch_chunked(symbols: list[str], fn: Callable, chunk: int = CHUNK_SIZE,
                  pause: float = 1.0, retries: int = 1) -> dict:
    """Call `fn` over chunks of symbols; one retry per failed chunk; merge."""
    merged: dict = {}
    for i in range(0, len(symbols), chunk):
        part = symbols[i:i + chunk]
        for attempt in range(retries + 1):
            try:
                got = fn(part)
                merged.update(got)
                break
            except Exception as e:          # network / parsing: never kill the run
                logger.warning("chunk %d-%d failed (attempt %d): %s",
                               i, i + len(part), attempt + 1, e)
                if attempt < retries:
                    time.sleep(pause * 3)
        if pause and i + chunk < len(symbols):
            time.sleep(pause)
        if (i // chunk) % 5 == 4:
            logger.info("  downloaded %d/%d symbols", min(i + chunk, len(symbols)),
                        len(symbols))
    return merged


# =========================================================================
# "CLOSED BARS ONLY"
# =========================================================================

def today_utc(now: Optional[datetime] = None) -> date:
    return (now or datetime.now(UTC)).astimezone(UTC).date()


def drop_open_daily(bars: list[DailyBar], now: datetime, crypto: bool) -> list[DailyBar]:
    """Remove a daily bar that is still forming.

    Crypto: a UTC day is closed once it has ended, so today's bar always goes.
    Stocks: today's bar goes until 16:00 ET; anything dated in the future
    (timezone skew) always goes.
    """
    if crypto:
        cutoff = today_utc(now)
        return [b for b in bars if b.dt < cutoff]
    now_et = now.astimezone(ET)
    today = now_et.date()
    out = []
    for b in bars:
        if b.dt > today:
            continue
        if b.dt == today and now_et.time() < dtime(16, 0):
            continue
        out.append(b)
    return out


def hour_bar_end(b: HourBar, crypto: bool) -> datetime:
    """When this hourly bar ends (stocks: the 15:30 bar is a half hour)."""
    if crypto:
        return b.ts + timedelta(hours=1)
    start_et = b.ts.astimezone(ET)
    if start_et.time() >= dtime(15, 30):
        return b.ts + timedelta(minutes=30)
    return b.ts + timedelta(hours=1)


def drop_open_hourly(bars: list[HourBar], now: datetime, crypto: bool) -> list[HourBar]:
    return [b for b in bars if hour_bar_end(b, crypto) <= now]


# =========================================================================
# W / M COMPLETENESS (decides whether the last aggregated bar is "forming")
# =========================================================================

def _next_business_day(d: date) -> date:
    n = d + timedelta(days=1)
    while n.weekday() >= 5:
        n += timedelta(days=1)
    return n


def last_bar_complete(tf: str, last_day: date, crypto: bool) -> bool:
    """True if the last daily bar closes its week / month.

    Stocks: week closes on Friday, month closes when the next business day is
    in another month. Crypto: week closes on Sunday, month on its last day.
    Exchange holidays are not modelled (a holiday Friday reads as a forming
    week); that only affects the `partial` flag, never a level.
    """
    if tf == "D":
        return True
    if tf == "W":
        return last_day.weekday() == (6 if crypto else 4)
    if tf == "M":
        nxt = last_day + timedelta(days=1) if crypto else _next_business_day(last_day)
        return nxt.month != last_day.month
    return True
