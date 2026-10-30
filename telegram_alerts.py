"""
Telegram alerts for the Strat Targets scan: a short list of high-conviction
setups instead of a stream of every signal.

    python telegram_alerts.py               # send (needs TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)
    python telegram_alerts.py --dry-run     # print what would be sent, send nothing

A setup is sent only when ALL of these hold (defaults, all overridable):
  * Daily or Weekly signal, adopted on the latest CLOSED bar (fresh, not forming)
  * status IN-FORCE (the trigger has already been taken out)
  * full timeframe continuity: the current D, W and M candles all trade in the
    signal's direction (from results/latest.json)
  * not choppy (fewer than chop_thr back-to-back reversals that missed T1)
  * no target on the ladder has been touched yet, and at least 3 remain open
  * room to T1 of at least 1.0% of price, last price >= $10
They are ranked by room to T1 (capped), +1 for a pizazz bar, a little for ladder
depth, and only the top N (default 8) are sent. Each signal is sent once:
results/telegram_sent.json remembers what went out.

NOTE: these rules are a judgment call, not a backtested edge. The sent log
(sym, timeframe, direction, signal bar, trigger, T1) is there so outcomes can be
measured before trusting the filter.
"""
from __future__ import annotations

import argparse
import html
import json
import logging
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("telegram")

API = "https://api.telegram.org/bot{token}/sendMessage"
TV = "https://www.tradingview.com/chart/?symbol="
MAX_LEN = 3800            # Telegram's hard limit is 4096; leave margin


def ftfc_all(panel: Optional[dict], direction: int) -> bool:
    """D, W and M candles all trading in `direction` (+1 up / -1 down)."""
    fd = (panel or {}).get("fd") or {}
    need = "u" if direction == 1 else "d"
    return all(fd.get(tf) == need for tf in ("D", "W", "M"))


def select(rows: list[dict], panels: dict, *, tfs=("D", "W"), min_room=1.0,
           min_open=3, min_price=10.0, top=8) -> list[dict]:
    """Apply the 'solid setup' rules and return the ranked top `top` rows,
    each annotated with a `_score`."""
    out = []
    for r in rows:
        if r.get("tf") not in tfs or not r.get("fresh") or r.get("partial"):
            continue
        if r.get("st") != "IN-FORCE" or r.get("chop"):
            continue
        tg = r.get("tg") or []
        if not tg or any(t[4] != "o" for t in tg):
            continue
        if len(tg) < min_open:
            continue
        room = tg[0][3]
        if room is None or room < min_room:
            continue
        if (r.get("lc") or 0) < min_price:
            continue
        if not ftfc_all(panels.get(r["sym"]), r["dir"]):
            continue
        r = dict(r)
        r["_room"] = room
        r["_score"] = min(room, 6.0) + (1.0 if r.get("pz") else 0.0) + 0.25 * min(len(tg), 6)
        out.append(r)
    out.sort(key=lambda r: (-r["_score"], r["sym"]))
    return out[:top]


def sent_key(r: dict) -> str:
    return f'{r["sym"]}|{r["tf"]}|{r["dir"]}|{r.get("sd")}'


def fmt_price(v) -> str:
    if v is None:
        return "–"
    return f"{v:,.2f}" if abs(v) >= 1 else f"{v:.4f}"


def format_message(rows: list[dict], scan_date: str) -> list[str]:
    """One or more HTML messages (split under Telegram's size limit)."""
    head = f"<b>Strat Targets: {len(rows)} solid setup{'s' if len(rows) != 1 else ''}</b> ({html.escape(scan_date)})\n"
    head += "<i>Fresh, in force, D/W/M all in favour, no target touched.</i>\n"
    blocks = []
    for r in rows:
        arrow = "🟢 ▲ LONG" if r["dir"] == 1 else "🔴 ▼ SHORT"
        tf = {"D": "Daily", "W": "Weekly"}.get(r["tf"], r["tf"])
        tg = r["tg"]
        ladder = "  ".join(f"T{i + 1} {fmt_price(t[0])}" for i, t in enumerate(tg[:4]))
        kind = "reversal" if r.get("rev") else "continuation"
        pz = " · PZ" if r.get("pz") else ""
        sym = html.escape(r["sym"])
        link = TV + urllib.parse.quote(r["sym"])
        blocks.append(
            f'\n\n{arrow} <a href="{link}"><b>{sym}</b></a> · {tf}\n'
            f'{html.escape(str(r.get("ps") or r.get("pat") or ""))} ({kind}{pz})\n'
            f'Last {fmt_price(r.get("lc"))} · Trigger {fmt_price(r.get("trig"))}\n'
            f'{ladder}\n'
            f'Room to T1: {r["_room"]:.1f}%'
        )
    msgs, cur = [], head
    for b in blocks:
        if len(cur) + len(b) > MAX_LEN:
            msgs.append(cur)
            cur = ""
        cur += b
    msgs.append(cur)
    return msgs


def send(token: str, chat_id: str, text: str, timeout: int = 20) -> None:
    data = urllib.parse.urlencode({
        "chat_id": chat_id, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(API.format(token=token), data=data)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode())
    if not body.get("ok"):
        raise RuntimeError(f"Telegram refused the message: {body}")


def run(results_dir: Path, *, dry_run: bool = False, token: Optional[str] = None,
        chat_id: Optional[str] = None, top: int = 8, min_room: float = 1.0,
        tfs=("D", "W"), sender=send) -> dict:
    results_dir = Path(results_dir)
    tpath, mpath = results_dir / "latest_targets.json", results_dir / "latest.json"
    spath = results_dir / "telegram_sent.json"
    targets = json.loads(tpath.read_text())
    panels = json.loads(mpath.read_text()).get("panels", {})
    sent = json.loads(spath.read_text()) if spath.exists() else {}

    picks = select(targets.get("rows", []), panels, tfs=tfs, min_room=min_room, top=top)
    new = [r for r in picks if sent_key(r) not in sent]
    summary = {"scan_date": targets.get("scan_date"), "selected": len(picks),
               "new": len(new), "sent": 0, "state_changed": False}
    if not new:
        logger.info("No new solid setups (%d selected, all already sent).", len(picks))
        return summary

    msgs = format_message(new, targets.get("scan_date", "?"))
    if dry_run:
        for m in msgs:
            print(m, "\n" + "-" * 40)
        return summary
    if not token or not chat_id:
        logger.warning("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set: nothing sent.")
        return summary

    for m in msgs:
        sender(token, chat_id, m)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for r in new:
        sent[sent_key(r)] = {"at": now, "trig": r.get("trig"), "t1": r["tg"][0][0],
                             "lc": r.get("lc"), "room": round(r["_room"], 2)}
    # keep the log bounded: newest 2,000 entries
    if len(sent) > 2000:
        keep = sorted(sent.items(), key=lambda kv: kv[1].get("at", ""))[-2000:]
        sent = dict(keep)
    spath.write_text(json.dumps(sent, separators=(",", ":")))
    summary["sent"] = len(new)
    summary["state_changed"] = True
    return summary


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Telegram alerts for solid Strat Targets setups")
    ap.add_argument("--results-dir", default="results")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--top", type=int, default=8)
    ap.add_argument("--min-room", type=float, default=1.0, help="min %% room to T1")
    ap.add_argument("--timeframes", default="D,W")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        s = run(Path(args.results_dir), dry_run=args.dry_run,
                token=os.environ.get("TELEGRAM_BOT_TOKEN"),
                chat_id=os.environ.get("TELEGRAM_CHAT_ID"),
                top=args.top, min_room=args.min_room,
                tfs=tuple(t.strip() for t in args.timeframes.split(",") if t.strip()))
    except (urllib.error.URLError, RuntimeError, OSError, ValueError) as e:
        logger.error("Telegram alert failed: %s", e)
        return 1
    logger.info("%s", s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
