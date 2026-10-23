"""
Strat Targets scan — runs the strat_targets engine (a port of the Pine
indicator "TheStrat Magnitude + Targets (MTF)") over the stock or crypto
universe on 60-minute, Daily, Weekly and Monthly bars.

    python targets_scan.py                      # stocks, all four timeframes
    python targets_scan.py --asset crypto
    python targets_scan.py --timeframes D,W     # subset

Universe: the tickers already published by the nightly scan
(results/latest.json for stocks, results/latest_crypto.json for crypto), so
this job needs no Polygon key and never disagrees with the other tabs about
which names exist.

Output: output/targets/targets_<asset>_<date>.json, which the workflows copy
to results/latest_targets.json / results/latest_targets_crypto.json.

Each row is the signal the Pine indicator would be HOLDING on that
symbol/timeframe right now (it keeps the most recent signal until a new one
replaces it or it is negated), with its trigger, target ladder (open / taken /
reclaimed), status and chop count. `fresh` marks rows whose signal was adopted
on the latest bar, which is the "new signals only" view.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from strat_targets import Bar, TargetsConfig, scan_series, tick_for
from targets_data import (
    ET,
    TargetsDataError,
    drop_open_daily,
    drop_open_hourly,
    fetch_chunked,
    last_bar_complete,
    today_utc,
    yahoo_daily_downloader,
    yahoo_hourly_downloader,
    CHUNK_SIZE,
)
from timeframes import DailyBar, aggregate

logger = logging.getLogger("targets")

ALL_TFS = ["60", "D", "W", "M"]
DAILY_TFS = {"D", "W", "M"}
# Replaying more hourly bars than this adds runtime but no information: only
# the most recent signal matters and Pine itself keeps just 250 bars.
HOURLY_MAX_BARS = 600
MIN_DAILY_COVERAGE = 0.60
MIN_HOURLY_COVERAGE = 0.50
SCHEMA_VERSION = 1


@dataclass
class Entry:
    symbol: str          # what the dashboard shows (e.g. AAPL, BTC-USD)
    yahoo: str           # what Yahoo is asked for
    name: str = ""


# =========================================================================
# UNIVERSE
# =========================================================================

def load_universe(asset: str, results_dir: Path) -> list[Entry]:
    crypto = asset == "crypto"
    path = Path(results_dir) / ("latest_crypto.json" if crypto else "latest.json")
    if not path.exists():
        raise TargetsDataError(
            f"{path} not found: the Targets scan takes its universe from the "
            f"published nightly results. Run the main scan first.")
    data = json.loads(path.read_text())
    panels = data.get("panels") or {}
    if not panels:
        raise TargetsDataError(f"{path} has no 'panels' (empty universe).")
    meta = data.get("crypto_meta", {}) if crypto else {}
    out = []
    for sym in sorted(panels):
        m = meta.get(sym, {})
        out.append(Entry(symbol=sym, yahoo=m.get("yahoo", sym), name=m.get("name", "")))
    return out


# =========================================================================
# BAR BUILDING
# =========================================================================

def daily_series(daily: list[DailyBar], tf: str) -> list[Bar]:
    if tf == "D":
        return [Bar(b.open, b.high, b.low, b.close, b.dt.isoformat()) for b in daily]
    aggs = aggregate(daily, tf)
    return [Bar(a.bar.open, a.bar.high, a.bar.low, a.bar.close,
                a.period_end.isoformat()) for a in aggs]


def hourly_series(hours: list) -> list[Bar]:
    hours = hours[-HOURLY_MAX_BARS:]
    return [Bar(h.open, h.high, h.low, h.close, h.ts.strftime("%Y-%m-%dT%H:%MZ"))
            for h in hours]


# =========================================================================
# RUN
# =========================================================================

def run_targets(
    asset: str = "stocks",
    timeframes: Optional[list[str]] = None,
    results_dir: Path = Path("results"),
    output_dir: Optional[Path] = None,
    daily_downloader: Optional[Callable] = None,
    hourly_downloader: Optional[Callable] = None,
    now: Optional[datetime] = None,
    universe: Optional[list[Entry]] = None,
    ext_targets: int = 5,
    chop_thr: int = 2,
    invalidate: bool = True,
    chunk_size: int = CHUNK_SIZE,
    pause: float = 1.0,
    save: bool = True,
) -> dict:
    crypto = asset == "crypto"
    tfs = [t for t in (timeframes or ALL_TFS) if t in ALL_TFS]
    if not tfs:
        raise ValueError(f"no valid timeframes in {timeframes!r}; choose from {ALL_TFS}")
    now = now or datetime.now(timezone.utc)
    t0 = time.time()

    if universe is None:
        universe = load_universe(asset, results_dir)
    logger.info("Targets scan (%s): %d symbols, timeframes %s", asset, len(universe), tfs)
    yahoo_syms = [e.yahoo for e in universe]

    # ---- data -----------------------------------------------------------
    daily: dict[str, list[DailyBar]] = {}
    hourly: dict[str, list] = {}
    if DAILY_TFS & set(tfs):
        logger.info("Fetching daily bars...")
        raw = fetch_chunked(yahoo_syms, daily_downloader or yahoo_daily_downloader,
                            chunk=chunk_size, pause=pause)
        daily = {s: drop_open_daily(b, now, crypto) for s, b in raw.items()}
        daily = {s: b for s, b in daily.items() if b}
        if len(daily) < MIN_DAILY_COVERAGE * len(universe):
            raise TargetsDataError(
                f"Only {len(daily)}/{len(universe)} symbols returned daily bars "
                f"(need >= {MIN_DAILY_COVERAGE:.0%}). That is a feed outage, not a "
                f"quiet market; refusing to publish.")
    if "60" in tfs:
        logger.info("Fetching hourly bars...")
        raw = fetch_chunked(yahoo_syms, hourly_downloader or yahoo_hourly_downloader,
                            chunk=chunk_size, pause=pause)
        hourly = {s: drop_open_hourly(b, now, crypto) for s, b in raw.items()}
        hourly = {s: b for s, b in hourly.items() if b}
        if len(hourly) < MIN_HOURLY_COVERAGE * len(universe):
            raise TargetsDataError(
                f"Only {len(hourly)}/{len(universe)} symbols returned hourly bars "
                f"(need >= {MIN_HOURLY_COVERAGE:.0%}); refusing to publish.")

    # ---- freshness ------------------------------------------------------
    as_of: Optional[date] = None
    if daily:
        as_of = max(b[-1].dt for b in daily.values())
        limit = 2 if crypto else 5            # stocks: allow a long weekend
        ref_today = today_utc(now) if crypto else now.astimezone(ET).date()
        if (ref_today - as_of).days > limit:
            raise TargetsDataError(
                f"Newest daily bar is {as_of} ({(ref_today - as_of).days} days old); "
                f"the feed is stale, refusing to publish.")
    if hourly:
        newest = max(b[-1].ts for b in hourly.values())
        if (now - newest).total_seconds() > (3 * 86400 if not crypto else 6 * 3600):
            raise TargetsDataError(
                f"Newest hourly bar is {newest:%Y-%m-%d %H:%MZ}; the feed is "
                f"stale, refusing to publish.")

    # ---- scan -----------------------------------------------------------
    rows: list[dict] = []
    skipped: list[str] = []
    scanned = {t: 0 for t in tfs}
    for e in universe:
        d = daily.get(e.yahoo)
        h = hourly.get(e.yahoo)
        ref_close = (d[-1].close if d else h[-1].close if h else None)
        if ref_close is None:
            skipped.append(e.symbol)
            continue
        cfg = TargetsConfig(tick=tick_for(ref_close, crypto),
                            ext_targets=ext_targets, chop_thr=chop_thr,
                            invalidate=invalidate)
        try:
            for tf in tfs:
                if tf == "60":
                    if not h:
                        continue
                    bars, complete = hourly_series(h), True
                else:
                    if not d:
                        continue
                    bars = daily_series(d, tf)
                    complete = last_bar_complete(tf, d[-1].dt, crypto)
                scanned[tf] += 1
                snap = scan_series(bars, cfg, last_confirmed=complete)
                if snap is None:
                    continue
                row = {"sym": e.symbol, "tf": tf}
                if e.name:
                    row["name"] = e.name
                row.update(snap)
                rows.append(row)
        except Exception as ex:                  # one bad symbol never kills the run
            logger.warning("Error scanning %s: %s", e.symbol, ex)
            skipped.append(e.symbol)

    held = {t: sum(1 for r in rows if r["tf"] == t) for t in tfs}
    fresh = {t: sum(1 for r in rows if r["tf"] == t and r["fresh"]) for t in tfs}
    result = {
        "schema": SCHEMA_VERSION,
        "asset_class": asset,
        "scan_date": (as_of or now.date()).isoformat(),
        "timestamp": now.isoformat(timespec="seconds"),
        "timeframes": tfs,
        "universe_size": len(universe),
        "scanned": scanned,
        "held": held,
        "fresh": fresh,
        "skipped": sorted(set(skipped)),
        "params": {
            "ext_targets": ext_targets,
            "chop_thr": chop_thr,
            "invalidate": invalidate,
            "tick": "0.01 (0.0001 under $1)" if not crypto else "~5 significant digits, max 0.01",
            "history_bars": 250,
            "data": "Yahoo Finance (yfinance), closed bars only; W/M last bar may be forming",
        },
        "rows": rows,
    }
    logger.info("Targets scan done in %.1fs: held %s, fresh %s, skipped %d",
                time.time() - t0, held, fresh, len(set(skipped)))

    if save:
        out = Path(output_dir or "output/targets")
        out.mkdir(parents=True, exist_ok=True)
        suffix = "crypto" if crypto else "stocks"
        path = out / f"targets_{suffix}_{result['scan_date']}.json"
        path.write_text(json.dumps(result, separators=(",", ":")))
        logger.info("Saved %s (%.0f KB)", path, path.stat().st_size / 1024)
    return result


# =========================================================================
# CLI
# =========================================================================

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Strat Targets scanner")
    ap.add_argument("--asset", choices=["stocks", "crypto"], default="stocks")
    ap.add_argument("--timeframes", default=",".join(ALL_TFS),
                    help="comma-separated subset of 60,D,W,M")
    ap.add_argument("--results-dir", default="results")
    ap.add_argument("--output-dir", default="output/targets")
    ap.add_argument("--ext-targets", type=int, default=5)
    ap.add_argument("--chop-thr", type=int, default=2)
    ap.add_argument("--no-invalidate", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        run_targets(
            asset=args.asset,
            timeframes=[t.strip() for t in args.timeframes.split(",") if t.strip()],
            results_dir=Path(args.results_dir),
            output_dir=Path(args.output_dir),
            ext_targets=args.ext_targets,
            chop_thr=args.chop_thr,
            invalidate=not args.no_invalidate,
        )
    except TargetsDataError as e:
        logger.error("%s", e)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
