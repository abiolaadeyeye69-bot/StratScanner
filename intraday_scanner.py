#!/usr/bin/env python3
"""
Intraday Index Simultaneous Break Scanner
==========================================

Monitors SPY, QQQ, DIA, IWM on 60-minute bars and detects STRAT patterns:
  * Inside bars (1s)
  * Failed 2s
  * 1-3s (inside bar → outside bar)
  * 2-2 Reversals
  * 1-Failed 2 (inside bar → failed 2)
  * 3-Failed 2 (outside bar → failed 2)

When 2+ indexes form the same pattern in the same direction on the same
60-minute bar, that's an Intraday Index Simultaneous Break — the highest-
conviction intraday signal in The Strat framework.

Usage:
    python intraday_scanner.py                # scan & alert
    python intraday_scanner.py --dry-run      # print without sending
    python intraday_scanner.py --force        # run outside market hours

Requires:
    TELEGRAM_BOT_TOKEN  env var (or --dry-run)
    TELEGRAM_CHAT_ID    env var (auto-discovered if missing)
    yfinance            pip package

The sent log (results/intraday_sent.json) prevents the same bar's patterns
from being alerted twice.
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import math
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from strat_engine import BarClassification, Failed2Result, classify_bar, detect_failed_2

logger = logging.getLogger("intraday")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
INDEXES = ["SPY", "QQQ", "DIA", "IWM"]
ET = ZoneInfo("America/New_York")
TV = "https://www.tradingview.com/chart/?symbol="
API = "https://api.telegram.org/bot{token}/sendMessage"
MAX_LEN = 3800          # Telegram hard limit is 4096; leave margin

PATTERN_LABELS = {
    "inside_bar":  "Inside Bar (1)",
    "failed_2":    "Failed 2",
    "1_3":         "1-3 (Inside→Outside)",
    "22_rev":      "2-2 Reversal",
    "1_failed_2":  "1-Failed 2",
    "3_failed_2":  "3-Failed 2",
}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
@dataclass
class Bar60:
    """One 60-minute OHLC bar with timestamp."""
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class PatternHit:
    """A detected STRAT pattern on one index."""
    symbol: str
    bar_time: datetime       # bar's timestamp
    pattern: str             # key into PATTERN_LABELS
    direction: str           # "bullish", "bearish", "neutral"
    bar_type: str            # CC bar type: "1u", "2u", "2d", "3u", "3d", …
    combo: str               # STRAT combo notation, e.g. "2d-1-2u"
    close: float
    detail: str = ""


# ---------------------------------------------------------------------------
# Data fetching — yfinance 60-minute bars
# ---------------------------------------------------------------------------
def fetch_bars(symbols: list[str], days: int = 5) -> dict[str, list[Bar60]]:
    """Fetch 60-minute bars for *symbols* over the last *days* trading days.

    Returns a dict mapping each symbol to a chronologically sorted list of
    Bar60 objects.
    """
    import yfinance as yf

    logger.info("Fetching %dd of 60-min bars for %s", days, ", ".join(symbols))
    df = yf.download(
        symbols,
        period=f"{days}d",
        interval="1h",
        auto_adjust=False,
        progress=False,
        threads=True,
    )

    if df is None or df.empty:
        logger.error("yfinance returned no intraday data")
        return {}

    multi = len(symbols) > 1
    result: dict[str, list[Bar60]] = {}

    for sym in symbols:
        bars: list[Bar60] = []
        for ts in df.index:
            try:
                if multi:
                    o = float(df.loc[ts, ("Open", sym)])
                    h = float(df.loc[ts, ("High", sym)])
                    lo = float(df.loc[ts, ("Low", sym)])
                    c = float(df.loc[ts, ("Close", sym)])
                    v = float(df.loc[ts, ("Volume", sym)])
                else:
                    o = float(df.loc[ts, "Open"])
                    h = float(df.loc[ts, "High"])
                    lo = float(df.loc[ts, "Low"])
                    c = float(df.loc[ts, "Close"])
                    v = float(df.loc[ts, "Volume"])

                if any(math.isnan(x) for x in (o, h, lo, c)):
                    continue

                # Normalise timestamp to US/Eastern aware datetime
                raw_ts = ts.to_pydatetime()
                if raw_ts.tzinfo is None:
                    raw_ts = raw_ts.replace(tzinfo=timezone.utc)
                et_ts = raw_ts.astimezone(ET)

                bars.append(Bar60(
                    timestamp=et_ts,
                    open=o, high=h, low=lo, close=c,
                    volume=v if not math.isnan(v) else 0.0,
                ))
            except (KeyError, ValueError, TypeError):
                continue

        bars.sort(key=lambda b: b.timestamp)
        result[sym] = bars
        logger.info("  %s: %d bars (latest %s)",
                     sym, len(bars),
                     bars[-1].timestamp.strftime("%Y-%m-%d %H:%M %Z") if bars else "–")
    return result


# ---------------------------------------------------------------------------
# Pattern detection per symbol
# ---------------------------------------------------------------------------
def _detect_patterns(bars: list[Bar60], symbol: str) -> list[PatternHit]:
    """Detect the six STRAT patterns on the latest COMPLETE 60-min bar.

    Requires at least 4 consecutive bars: CC, C1, C2, C3.
    """
    if len(bars) < 4:
        return []

    cc, c1, c2, c3 = bars[-1], bars[-2], bars[-3], bars[-4]

    # --- Classify bars ---------------------------------------------------
    cc_cls: BarClassification = classify_bar(
        c1.high, c1.low, cc.high, cc.low, cc.open, cc.close)
    c1_cls: BarClassification = classify_bar(
        c2.high, c2.low, c1.high, c1.low, c1.open, c1.close)
    c2_cls: BarClassification = classify_bar(
        c3.high, c3.low, c2.high, c2.low, c2.open, c2.close)

    cc_type = cc_cls.bar_type       # e.g. "2u", "1d", "3u"
    c1_type = c1_cls.bar_type
    c2_type = c2_cls.bar_type
    combo = f"{c2_type}-{c1_type}-{cc_type}"

    # --- Failed 2 detection ----------------------------------------------
    f2: Failed2Result = detect_failed_2(
        c1.high, c1.low, cc.open, cc.close, cc.high, cc.low, method="Reclaim")

    hits: list[PatternHit] = []
    bt = cc.timestamp

    # 1. Inside Bar — CC is a 1 (inside C1's range)
    if cc_cls.is_inside:
        hits.append(PatternHit(
            symbol=symbol, bar_time=bt, pattern="inside_bar",
            direction="neutral", bar_type=cc_type, combo=combo,
            close=cc.close,
            detail="Coiling — watch for break of range"))

    # 2. Failed 2 — CC broke one side of C1 but closed back inside
    #    is_f2u = broke UP and failed → bearish reversal
    #    is_f2d = broke DOWN and failed → bullish reversal
    has_f2 = f2.is_f2u or f2.is_f2d
    if has_f2:
        f2_dir = "bearish" if f2.is_f2u else "bullish"
        hits.append(PatternHit(
            symbol=symbol, bar_time=bt, pattern="failed_2",
            direction=f2_dir, bar_type=cc_type, combo=combo,
            close=cc.close,
            detail=f"Broke {'high' if f2.is_f2u else 'low'} but reclaimed"))

    # 3. 1-3 — C1 was inside (1), CC is outside (3)
    if c1_cls.is_inside and cc_cls.is_3:
        d = "bullish" if cc_type == "3u" else "bearish"
        hits.append(PatternHit(
            symbol=symbol, bar_time=bt, pattern="1_3",
            direction=d, bar_type=cc_type, combo=combo,
            close=cc.close,
            detail="Inside → Outside expansion"))

    # 4. 2-2 Reversal — C1 was a 2 in one direction, CC is a 2 the other way
    c1_is_2u = c1_cls.bar_type == "2u"
    c1_is_2d = c1_cls.bar_type == "2d"
    cc_is_2u = cc_cls.bar_type == "2u"
    cc_is_2d = cc_cls.bar_type == "2d"
    if (c1_is_2u and cc_is_2d) or (c1_is_2d and cc_is_2u):
        d = "bullish" if cc_is_2u else "bearish"
        hits.append(PatternHit(
            symbol=symbol, bar_time=bt, pattern="22_rev",
            direction=d, bar_type=cc_type, combo=combo,
            close=cc.close,
            detail=f"{c1_type}→{cc_type} reversal"))

    # 5. 1-Failed 2 — C1 was inside, CC is a failed 2
    if c1_cls.is_inside and has_f2:
        f2_dir = "bearish" if f2.is_f2u else "bullish"
        hits.append(PatternHit(
            symbol=symbol, bar_time=bt, pattern="1_failed_2",
            direction=f2_dir, bar_type=cc_type, combo=combo,
            close=cc.close,
            detail="Inside → Failed 2 (high conviction)"))

    # 6. 3-Failed 2 — C1 was outside (3), CC is a failed 2
    if c1_cls.is_3 and has_f2:
        f2_dir = "bearish" if f2.is_f2u else "bullish"
        hits.append(PatternHit(
            symbol=symbol, bar_time=bt, pattern="3_failed_2",
            direction=f2_dir, bar_type=cc_type, combo=combo,
            close=cc.close,
            detail="Outside → Failed 2 (exhaustion reversal)"))

    return hits


# ---------------------------------------------------------------------------
# Simultaneous break detection
# ---------------------------------------------------------------------------
def scan_all(bar_data: dict[str, list[Bar60]]) -> dict:
    """Run pattern detection on every index and find simultaneous breaks.

    Returns a result dict with 'simultaneous', 'individual', and metadata.
    """
    all_hits: dict[str, list[PatternHit]] = {}
    for sym, bars in bar_data.items():
        if bars:
            all_hits[sym] = _detect_patterns(bars, sym)

    # Group by (pattern, direction) to find simultaneous occurrences
    groups: dict[tuple[str, str], list[PatternHit]] = defaultdict(list)
    for sym, hits in all_hits.items():
        for h in hits:
            groups[(h.pattern, h.direction)].append(h)

    simultaneous = []
    for (pattern, direction), hits in sorted(groups.items()):
        if len(hits) >= 2:
            simultaneous.append({
                "pattern": pattern,
                "direction": direction,
                "count": len(hits),
                "total": len(INDEXES),
                "hits": hits,
            })
    simultaneous.sort(key=lambda s: (-s["count"], s["pattern"]))

    # Individual hits = anything NOT part of a simultaneous group
    sim_keys: set[tuple[str, str]] = set()
    for s in simultaneous:
        for h in s["hits"]:
            sim_keys.add((h.symbol, h.pattern))

    individual = [
        h for sym_hits in all_hits.values()
        for h in sym_hits
        if (h.symbol, h.pattern) not in sim_keys
    ]

    scan_time = datetime.now(ET)

    # Latest bar time across all symbols (for dedup key)
    latest_bar = None
    for bars in bar_data.values():
        if bars and (latest_bar is None or bars[-1].timestamp > latest_bar):
            latest_bar = bars[-1].timestamp

    return {
        "simultaneous": simultaneous,
        "individual": individual,
        "all_hits": all_hits,
        "scan_time": scan_time,
        "latest_bar": latest_bar,
    }


# ---------------------------------------------------------------------------
# Telegram message formatting
# ---------------------------------------------------------------------------
def _dir_emoji(d: str) -> str:
    if d == "bullish":
        return "\U0001f7e2 ▲"
    if d == "bearish":
        return "\U0001f534 ▼"
    return "⚪ ◆"


def _fmt(v: float) -> str:
    return f"{v:,.2f}" if abs(v) >= 1 else f"{v:.4f}"


def format_alert(result: dict) -> list[str]:
    """Build one or more HTML messages for Telegram (split under MAX_LEN)."""
    sim = result["simultaneous"]
    ind = result["individual"]
    if not sim and not ind:
        return []

    ts = result["scan_time"].strftime("%Y-%m-%d %H:%M ET")
    bar_ts = ""
    if result.get("latest_bar"):
        bar_ts = result["latest_bar"].strftime(" · bar %H:%M")

    head = (f"<b>\U0001f514 Intraday Index STRAT Scan</b>\n"
            f"<i>{html.escape(ts)}{html.escape(bar_ts)} · 60-min bars</i>")
    blocks: list[str] = [head]

    # --- Simultaneous breaks (the headline) ---
    if sim:
        blocks.append("\n\n<b>⚡ SIMULTANEOUS BREAKS</b>")
        for s in sim:
            label = PATTERN_LABELS.get(s["pattern"], s["pattern"])
            arrow = _dir_emoji(s["direction"])
            n, total = s["count"], s["total"]
            dir_cap = s["direction"].upper()
            blocks.append(
                f"\n\n{arrow} <b>{html.escape(label)}</b> · {dir_cap} "
                f"({n}/{total} indexes)")
            for h in s["hits"]:
                link = TV + urllib.parse.quote(h.symbol)
                blocks.append(
                    f'  <a href="{link}">{html.escape(h.symbol)}</a> '
                    f'{html.escape(h.combo)} @ {_fmt(h.close)}')

    # --- Individual patterns ---
    if ind:
        blocks.append("\n\n<b>\U0001f4ca INDIVIDUAL PATTERNS</b>")
        for h in ind:
            arrow = _dir_emoji(h.direction)
            label = PATTERN_LABELS.get(h.pattern, h.pattern)
            link = TV + urllib.parse.quote(h.symbol)
            blocks.append(
                f'\n{arrow} <a href="{link}"><b>{html.escape(h.symbol)}</b></a>'
                f' · {html.escape(label)}\n'
                f'  {html.escape(h.combo)} @ {_fmt(h.close)}')

    # Merge blocks and split into messages under Telegram's limit
    text = "".join(blocks)
    msgs: list[str] = []
    while text:
        if len(text) <= MAX_LEN:
            msgs.append(text)
            break
        cut = text.rfind("\n\n", 0, MAX_LEN)
        if cut < 100:
            cut = MAX_LEN
        msgs.append(text[:cut])
        text = text[cut:]
    return msgs


# ---------------------------------------------------------------------------
# Deduplication via sent log
# ---------------------------------------------------------------------------
def _sent_key(h: PatternHit) -> str:
    """Unique key for a pattern hit: bar time + pattern + symbol."""
    bar_iso = h.bar_time.strftime("%Y-%m-%dT%H:%M")
    return f"{bar_iso}|{h.pattern}|{h.direction}|{h.symbol}"


def _filter_new(result: dict, sent: dict) -> dict:
    """Remove already-sent hits from the result; return a new result dict."""
    def _is_new(h: PatternHit) -> bool:
        return _sent_key(h) not in sent

    new_sim = []
    for s in result["simultaneous"]:
        new_hits = [h for h in s["hits"] if _is_new(h)]
        if len(new_hits) >= 2:
            new_sim.append({**s, "hits": new_hits, "count": len(new_hits)})

    # Rebuild the sim_keys to properly assign individual
    sim_keys: set[tuple[str, str]] = set()
    for s in new_sim:
        for h in s["hits"]:
            sim_keys.add((h.symbol, h.pattern))

    new_ind = [h for h in result["individual"] if _is_new(h)]
    # Also check hits that were sim but now have < 2 new members
    demoted: list[PatternHit] = []
    for s in result["simultaneous"]:
        new_hits = [h for h in s["hits"] if _is_new(h)]
        if 0 < len(new_hits) < 2:
            for h in new_hits:
                if (h.symbol, h.pattern) not in sim_keys:
                    demoted.append(h)

    return {
        **result,
        "simultaneous": new_sim,
        "individual": new_ind + demoted,
    }


def _record_sent(result: dict, sent: dict) -> None:
    """Add all hits in the result to the sent log (mutates *sent*)."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for s in result["simultaneous"]:
        for h in s["hits"]:
            sent[_sent_key(h)] = {"at": now, "sim": True}
    for h in result["individual"]:
        sent[_sent_key(h)] = {"at": now, "sim": False}


# ---------------------------------------------------------------------------
# Telegram send
# ---------------------------------------------------------------------------
def send_telegram(token: str, chat_id: str, text: str,
                  timeout: int = 20) -> None:
    data = urllib.parse.urlencode({
        "chat_id": chat_id, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(API.format(token=token), data=data)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode())
    if not body.get("ok"):
        raise RuntimeError(f"Telegram refused: {body}")


# ---------------------------------------------------------------------------
# Market hours check
# ---------------------------------------------------------------------------
def is_market_hours() -> bool:
    """Rough check: 9:30–16:00 ET, weekdays only."""
    now = datetime.now(ET)
    if now.weekday() >= 5:
        return False
    t = now.hour * 60 + now.minute
    return 9 * 60 + 30 <= t <= 16 * 60


# ---------------------------------------------------------------------------
# Main run()
# ---------------------------------------------------------------------------
def run(results_dir: Path, *, dry_run: bool = False,
        token: Optional[str] = None, chat_id: Optional[str] = None,
        force: bool = False) -> dict:
    """Fetch bars, detect patterns, send alerts. Returns a summary dict."""

    if not force and not is_market_hours():
        logger.info("Market closed — skipping scan (use --force to override).")
        return {"status": "market_closed", "sent": 0}

    # --- Fetch data -------------------------------------------------------
    bar_data = fetch_bars(INDEXES, days=5)
    if not bar_data or all(len(b) == 0 for b in bar_data.values()):
        logger.error("No bar data fetched.")
        return {"status": "no_data", "sent": 0}

    # --- Detect patterns --------------------------------------------------
    result = scan_all(bar_data)
    total = sum(len(h) for h in result["all_hits"].values())

    # --- Dedup against sent log -------------------------------------------
    sent_path = results_dir / "intraday_sent.json"
    sent: dict = {}
    if sent_path.exists():
        try:
            sent = json.loads(sent_path.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    filtered = _filter_new(result, sent)
    new_sim = len(filtered["simultaneous"])
    new_ind = len(filtered["individual"])
    new_total = sum(len(s["hits"]) for s in filtered["simultaneous"]) + new_ind

    summary = {
        "status": "ok",
        "scan_time": result["scan_time"].isoformat(),
        "total_patterns": total,
        "new_sim_groups": new_sim,
        "new_individual": new_ind,
        "new_total": new_total,
        "sent": 0,
        "state_changed": False,
    }

    if not new_total:
        logger.info("No new patterns (%d total detected, all already sent).",
                     total)
        return summary

    msgs = format_alert(filtered)
    if not msgs:
        return summary

    # --- Dry run ----------------------------------------------------------
    if dry_run:
        for m in msgs:
            print(m, "\n" + "-" * 60)
        return summary

    # --- Resolve Telegram credentials -------------------------------------
    if token and not chat_id:
        from telegram_alerts import discover_chat_id
        chat_id = discover_chat_id(token)
        if chat_id:
            logger.info("Auto-discovered chat id.")
        else:
            logger.warning("No chat id: message your bot once, then re-run.")
            return summary

    if not token or not chat_id:
        logger.warning("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set.")
        return summary

    # --- Send -------------------------------------------------------------
    for m in msgs:
        send_telegram(token, chat_id, m)

    # --- Update sent log --------------------------------------------------
    _record_sent(filtered, sent)
    # Prune log to last 1000 entries
    if len(sent) > 1000:
        sent = dict(sorted(sent.items(),
                            key=lambda kv: kv[1].get("at", ""))[-1000:])
    results_dir.mkdir(parents=True, exist_ok=True)
    sent_path.write_text(json.dumps(sent, separators=(",", ":")))

    summary["sent"] = len(msgs)
    summary["state_changed"] = True
    logger.info("Sent %d message(s): %d sim groups, %d individual.",
                len(msgs), new_sim, new_ind)
    return summary


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Intraday Index Simultaneous Break Scanner")
    ap.add_argument("--results-dir", default="results",
                    help="Directory for sent log (default: results)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print alerts without sending")
    ap.add_argument("--force", action="store_true",
                    help="Run even outside market hours")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    try:
        s = run(
            Path(args.results_dir),
            dry_run=args.dry_run,
            token=os.environ.get("TELEGRAM_BOT_TOKEN"),
            chat_id=os.environ.get("TELEGRAM_CHAT_ID"),
            force=args.force,
        )
    except Exception as e:
        logger.error("Intraday scan failed: %s", e, exc_info=True)
        return 1

    logger.info("Result: %s", s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
