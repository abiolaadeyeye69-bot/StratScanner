"""Tests for the Targets data layer and orchestrator.

No network: downloaders are injected with synthetic bars. These pin down OUR
logic (closed-bar rules, aggregation, guards, schema); they say nothing about
whether Yahoo currently serves the data. That can only be checked by a real
run (see README).
"""
from __future__ import annotations

import json
import random
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

import targets_scan
from targets_data import (
    ET, HourBar, TargetsDataError, drop_open_daily, drop_open_hourly,
    fetch_chunked, hour_bar_end, last_bar_complete, parse_daily_frame,
    parse_hourly_frame,
)
from targets_scan import Entry, daily_series, hourly_series, load_universe, run_targets
from timeframes import DailyBar

UTC = timezone.utc


def _walk(n, start_day, seed, p0=100.0, weekends=False):
    rnd = random.Random(seed)
    out, d, p = [], start_day, p0
    while len(out) < n:
        if weekends or d.weekday() < 5:
            o = p
            c = max(1.0, o * (1 + rnd.uniform(-0.03, 0.03)))
            h = max(o, c) * (1 + rnd.uniform(0, 0.015))
            l = min(o, c) * (1 - rnd.uniform(0, 0.015))
            out.append(DailyBar(d, round(o, 2), round(h, 2), round(l, 2), round(c, 2), 1e6))
            p = c
        d += timedelta(days=1)
    return out


def _walk_end(n, end, seed, p0=100.0, weekends=False):
    """n daily bars whose LAST bar is `end` (or the last trading day <= end)."""
    start = end - timedelta(days=int(n * 1.6) + 20)
    bars = [b for b in _walk(n * 2, start, seed, p0, weekends) if b.dt <= end]
    return bars[-n:]


def _hours(n, end, seed, p0=100.0, crypto=False):
    rnd = random.Random(seed)
    out, p = [], p0
    t = end - timedelta(hours=n)
    for _ in range(n):
        o = p
        c = max(1.0, o * (1 + rnd.uniform(-0.01, 0.01)))
        out.append(HourBar(t, o, max(o, c) * 1.002, min(o, c) * 0.998, c, 1e5))
        p, t = c, t + timedelta(hours=1)
    return out


NOW = datetime(2026, 10, 22, 22, 15, tzinfo=UTC)      # Thu 18:15 ET, after the close


def _universe():
    return [Entry("AAA", "AAA"), Entry("BBB", "BBB"), Entry("CCC", "CCC"), Entry("DDD", "DDD")]


def _dl_daily(syms):
    return {s: _walk_end(300, date(2026, 10, 22), seed=i, p0=50 + 10 * i)
            for i, s in enumerate(syms)}


def _dl_hourly(syms):
    return {s: _hours(300, NOW, seed=i) for i, s in enumerate(syms)}


# ---------------------------------------------------------------- closed bars

def test_drop_open_daily_stocks_before_and_after_close():
    bars = [DailyBar(date(2026, 10, 21), 1, 2, 1, 1.5), DailyBar(date(2026, 10, 22), 1, 2, 1, 1.5)]
    mid = datetime(2026, 10, 22, 15, 0, tzinfo=UTC)            # 11:00 ET, session open
    assert [b.dt for b in drop_open_daily(bars, mid, crypto=False)] == [date(2026, 10, 21)]
    after = datetime(2026, 10, 22, 21, 30, tzinfo=UTC)         # 17:30 ET
    assert len(drop_open_daily(bars, after, crypto=False)) == 2


def test_drop_open_daily_crypto_never_keeps_today():
    bars = [DailyBar(date(2026, 10, 21), 1, 2, 1, 1.5), DailyBar(date(2026, 10, 22), 1, 2, 1, 1.5)]
    assert [b.dt for b in drop_open_daily(bars, NOW, crypto=True)] == [date(2026, 10, 21)]


def test_drop_open_daily_drops_future_dated_bar():
    bars = [DailyBar(date(2026, 10, 23), 1, 2, 1, 1.5)]
    assert drop_open_daily(bars, NOW, crypto=False) == []


def test_hour_bar_end_half_hour_last_bar_for_stocks():
    start = datetime(2026, 10, 22, 15, 30, tzinfo=ET).astimezone(UTC)
    b = HourBar(start, 1, 2, 1, 1.5)
    assert hour_bar_end(b, crypto=False) - start == timedelta(minutes=30)
    assert hour_bar_end(b, crypto=True) - start == timedelta(hours=1)
    ten = HourBar(datetime(2026, 10, 22, 10, 30, tzinfo=ET).astimezone(UTC), 1, 2, 1, 1.5)
    assert hour_bar_end(ten, crypto=False) - ten.ts == timedelta(hours=1)


def test_drop_open_hourly_removes_the_forming_bar():
    now = datetime(2026, 10, 22, 14, 45, tzinfo=UTC)           # 10:45 ET
    done = HourBar(datetime(2026, 10, 22, 9, 30, tzinfo=ET).astimezone(UTC), 1, 2, 1, 1.5)
    forming = HourBar(datetime(2026, 10, 22, 10, 30, tzinfo=ET).astimezone(UTC), 1, 2, 1, 1.5)
    assert drop_open_hourly([done, forming], now, crypto=False) == [done]


# ------------------------------------------------------- W / M completeness

def test_last_bar_complete_rules():
    fri, thu, sun = date(2026, 10, 23), date(2026, 10, 22), date(2026, 10, 25)
    assert last_bar_complete("W", fri, crypto=False) is True
    assert last_bar_complete("W", thu, crypto=False) is False
    assert last_bar_complete("W", sun, crypto=True) is True
    assert last_bar_complete("W", fri, crypto=True) is False
    # month: Fri 30 Oct 2026 -> next business day is 2 Nov
    assert last_bar_complete("M", date(2026, 10, 30), crypto=False) is True
    assert last_bar_complete("M", date(2026, 10, 29), crypto=False) is False
    assert last_bar_complete("M", date(2026, 10, 31), crypto=True) is True
    assert last_bar_complete("D", thu, crypto=False) is True


# ------------------------------------------------------------------ parsing

def _frame(tz=None, multi=True, cols_first=True):
    idx = pd.to_datetime(["2026-10-20", "2026-10-21", "2026-10-22"])
    if tz:
        idx = idx.tz_localize(tz)
    data = {}
    for sym in ("AAA", "BBB"):
        for f, v in (("Open", 10), ("High", 12), ("Low", 9), ("Close", 11), ("Volume", 1000)):
            data[(f, sym) if cols_first else (sym, f)] = [v, v + 1, v + 2]
    return pd.DataFrame(data, index=idx)


@pytest.mark.parametrize("cols_first", [True, False])
def test_parse_daily_both_column_layouts(cols_first):
    out = parse_daily_frame(_frame(cols_first=cols_first), ["AAA", "BBB"])
    assert set(out) == {"AAA", "BBB"} and len(out["AAA"]) == 3
    assert out["AAA"][0].dt == date(2026, 10, 20)


def test_parse_daily_drops_garbage_rows_and_clamps_range():
    df = _frame()
    df.loc[df.index[1], ("Close", "AAA")] = float("nan")
    df.loc[df.index[2], ("High", "AAA")] = 1.0                  # high below body
    out = parse_daily_frame(df, ["AAA", "BBB"])["AAA"]
    assert len(out) == 2
    assert out[-1].high >= max(out[-1].open, out[-1].close)


def test_parse_hourly_converts_to_utc_and_dedups():
    idx = pd.DatetimeIndex(["2026-10-22 09:30", "2026-10-22 10:30", "2026-10-22 10:30"],
                           tz="America/New_York")
    df = pd.DataFrame({("Open", "AAA"): [10, 11, 12], ("High", "AAA"): [12, 13, 14],
                       ("Low", "AAA"): [9, 10, 11], ("Close", "AAA"): [11, 12, 13],
                       ("Volume", "AAA"): [1, 1, 1]}, index=idx)
    out = parse_hourly_frame(df, ["AAA"])["AAA"]
    assert len(out) == 2
    assert out[0].ts == datetime(2026, 10, 22, 13, 30, tzinfo=UTC)   # 09:30 EDT


def test_fetch_chunked_retries_then_gives_up_without_raising():
    calls = []

    def flaky(part):
        calls.append(tuple(part))
        if part[0] == "B":
            raise RuntimeError("boom")
        return {s: [] for s in part}

    got = fetch_chunked(["A", "B", "C"], flaky, chunk=1, pause=0, retries=1)
    assert set(got) == {"A", "C"}
    assert calls.count(("B",)) == 2


# -------------------------------------------------------------- series build

def test_daily_series_labels_and_weekly_aggregation():
    d = _walk(30, date(2026, 8, 3), seed=1)
    assert daily_series(d, "D")[-1].label == d[-1].dt.isoformat()
    w = daily_series(d, "W")
    assert len(w) < len(d) and w[-1].label == d[-1].dt.isoformat()
    m = daily_series(d, "M")
    assert len(m) in (2, 3)


def test_hourly_series_is_capped_and_labelled_utc():
    h = _hours(900, NOW, seed=2)
    s = hourly_series(h)
    assert len(s) == targets_scan.HOURLY_MAX_BARS
    assert s[-1].label.endswith("Z") and "T" in s[-1].label


# ---------------------------------------------------------------- end to end

def test_run_targets_end_to_end_schema(tmp_path):
    res = run_targets("stocks", universe=_universe(), daily_downloader=_dl_daily,
                      hourly_downloader=_dl_hourly, now=NOW, output_dir=tmp_path,
                      pause=0, chunk_size=2)
    assert res["asset_class"] == "stocks" and res["timeframes"] == ["60", "D", "W", "M"]
    assert res["universe_size"] == 4 and set(res["scanned"]) == {"60", "D", "W", "M"}
    assert all(res["scanned"][t] == 4 for t in res["scanned"])
    assert res["rows"], "random walks should hold at least one signal"
    r = res["rows"][0]
    for k in ("sym", "tf", "dir", "pat", "ps", "st", "fresh", "age", "sd", "trig", "tg",
              "lc", "revs", "chop", "partial", "rev", "wk", "pz"):
        assert k in r, k
    assert r["dir"] in (1, -1) and r["tf"] in ("60", "D", "W", "M")
    for lvl in r["tg"]:
        assert len(lvl) == 5 and lvl[4] in ("o", "x", "r")
    assert res["held"]["D"] == sum(1 for x in res["rows"] if x["tf"] == "D")
    saved = list(tmp_path.glob("targets_stocks_*.json"))
    assert len(saved) == 1
    assert json.loads(saved[0].read_text())["scan_date"] == res["scan_date"]


def test_stock_scan_date_is_newest_daily_bar_and_no_open_bar_is_used(tmp_path):
    # session still open: today's bar must not be scanned
    mid = datetime(2026, 10, 22, 15, 0, tzinfo=UTC)

    def dl(syms):
        return {s: _walk_end(300, date(2026, 10, 22), seed=3) for s in syms}

    res = run_targets("stocks", ["D"], universe=_universe(), daily_downloader=dl,
                      now=mid, output_dir=tmp_path, pause=0, save=False)
    assert date.fromisoformat(res["scan_date"]) < date(2026, 10, 22)
    assert all(r["sd"] < "2026-10-22" for r in res["rows"])


def test_weekly_partial_flag_follows_last_daily_bar(tmp_path):
    def dl(syms):    # series ending on a Thursday => forming week
        end = date(2026, 10, 22)
        start = end - timedelta(days=420)
        return {s: [b for b in _walk(400, start, seed=4) if b.dt <= end] for s in syms}

    res = run_targets("stocks", ["W"], universe=_universe(), daily_downloader=dl,
                      now=NOW, output_dir=tmp_path, pause=0, save=False)
    assert res["rows"] and all(r["partial"] is True for r in res["rows"])


def test_crypto_uses_closed_utc_days_and_yahoo_symbols(tmp_path):
    uni = [Entry("BTC-USD", "BTC-USD", "Bitcoin"), Entry("APT-USD", "APT21794-USD", "Aptos")]
    asked = []

    def dl(syms):
        asked.extend(syms)
        return {s: _walk_end(300, date(2026, 10, 22), seed=5, weekends=True) for s in syms}

    res = run_targets("crypto", ["D", "W"], universe=uni, daily_downloader=dl,
                      now=NOW, output_dir=tmp_path, pause=0, save=False)
    assert "APT21794-USD" in asked and "APT-USD" not in asked
    assert {r["sym"] for r in res["rows"]} <= {"BTC-USD", "APT-USD"}
    assert date.fromisoformat(res["scan_date"]) <= date(2026, 10, 21)
    assert any(r.get("name") == "Bitcoin" for r in res["rows"])


# ------------------------------------------------------------------- guards

def test_aborts_when_too_few_symbols_return_data():
    def dl(syms):
        return {syms[0]: _walk_end(300, date(2026, 10, 22), seed=1)}   # 1 of 4

    with pytest.raises(TargetsDataError, match="feed outage"):
        run_targets("stocks", ["D"], universe=_universe(), daily_downloader=dl,
                    now=NOW, pause=0, save=False, chunk_size=10)


def test_aborts_on_stale_feed():
    def dl(syms):
        return {s: _walk(300, date(2025, 1, 6), seed=1) for s in syms}   # ends ~Feb 2026

    with pytest.raises(TargetsDataError, match="stale"):
        run_targets("stocks", ["D"], universe=_universe(), daily_downloader=dl,
                    now=NOW, pause=0, save=False)


def test_aborts_on_stale_hourly_feed():
    def dl(syms):
        return {s: _hours(300, NOW - timedelta(days=10), seed=1) for s in syms}

    with pytest.raises(TargetsDataError, match="stale"):
        run_targets("stocks", ["60"], universe=_universe(), hourly_downloader=dl,
                    now=NOW, pause=0, save=False)


def test_invalid_timeframes_rejected():
    with pytest.raises(ValueError):
        run_targets("stocks", ["2D"], universe=_universe(), now=NOW, save=False)


def test_one_bad_symbol_does_not_kill_the_run(tmp_path):
    def dl(syms):
        out = _dl_daily(syms)
        out["BBB"] = out["BBB"][:2]                               # too short: skipped by engine
        return out

    res = run_targets("stocks", ["D"], universe=_universe(), daily_downloader=dl,
                      now=NOW, output_dir=tmp_path, pause=0, save=False)
    assert "BBB" not in {r["sym"] for r in res["rows"]}


# ----------------------------------------------------------------- universe

def test_load_universe_stocks_and_crypto(tmp_path):
    (tmp_path / "latest.json").write_text(json.dumps({"panels": {"MSFT": {}, "AAPL": {}}}))
    (tmp_path / "latest_crypto.json").write_text(json.dumps({
        "panels": {"APT-USD": {}, "BTC-USD": {}},
        "crypto_meta": {"APT-USD": {"name": "Aptos", "yahoo": "APT21794-USD"},
                        "BTC-USD": {"name": "Bitcoin", "yahoo": "BTC-USD"}}}))
    s = load_universe("stocks", tmp_path)
    assert [e.symbol for e in s] == ["AAPL", "MSFT"] and s[0].yahoo == "AAPL"
    c = load_universe("crypto", tmp_path)
    assert {(e.symbol, e.yahoo) for e in c} == {("APT-USD", "APT21794-USD"), ("BTC-USD", "BTC-USD")}


def test_load_universe_missing_or_empty(tmp_path):
    with pytest.raises(TargetsDataError, match="not found"):
        load_universe("stocks", tmp_path)
    (tmp_path / "latest.json").write_text(json.dumps({"panels": {}}))
    with pytest.raises(TargetsDataError, match="empty"):
        load_universe("stocks", tmp_path)


def test_real_published_universe_loads_if_present():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "results"
    if not (root / "latest.json").exists():
        pytest.skip("no published results in this checkout")
    u = load_universe("stocks", root)
    assert len(u) > 500 and all(e.symbol == e.yahoo for e in u)
    if (root / "latest_crypto.json").exists():
        c = load_universe("crypto", root)
        assert any(e.yahoo != e.symbol for e in c)      # e.g. APT-USD -> APT21794-USD
