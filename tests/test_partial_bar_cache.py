"""Regression: a mid-session run must not freeze the day's bar in the cache."""
import json, sys, types
from datetime import date, datetime
from zoneinfo import ZoneInfo
import pandas as pd
import pytest
import data as D

ET = ZoneInfo("America/New_York")
DAY = date(2026, 9, 22)


class FakeResp:
    def __init__(self, code, payload=None):
        self.status_code, self._p = code, payload
    def raise_for_status(self): pass
    def json(self): return self._p


def make_dm(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYGON_API_KEY", "x")
    monkeypatch.setenv("SCANNER_CACHE_DIR", str(tmp_path))
    cfg = types.SimpleNamespace(polygon_api_key="x", history_calendar_days=3)
    dm = D.DataManager(cfg)
    dm.client.limiter.wait = lambda: None
    return dm


def fake_yf(close):
    idx = pd.DatetimeIndex([pd.Timestamp(DAY)])
    cols = pd.MultiIndex.from_product([["Open", "High", "Low", "Close", "Volume"], ["SPY", "QQQ"]])
    df = pd.DataFrame([[770.0, 740.0, close + 1, 745.0, 769.0, 739.0, close, 744.0, 1e6, 1e6]],
                      index=idx, columns=cols)
    mod = types.SimpleNamespace(download=lambda *a, **k: df)
    return mod


def at(h, m):
    return datetime(2026, 9, 22, h, m, tzinfo=ET)


def test_no_backfill_while_session_open(tmp_path, monkeypatch):
    dm = make_dm(tmp_path, monkeypatch)
    monkeypatch.setattr(D, "now_et", lambda: at(9, 41))
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf(774.88))
    assert dm.backfill_recent_yfinance(["SPY", "QQQ"], lookback_days=0) == 0
    assert not (tmp_path / "daily" / "2026-09-22.json").exists()


def test_after_close_backfill_is_final_and_used(tmp_path, monkeypatch):
    dm = make_dm(tmp_path, monkeypatch)
    monkeypatch.setattr(D, "now_et", lambda: at(21, 33))
    monkeypatch.setitem(sys.modules, "yfinance", fake_yf(776.10))
    assert dm.backfill_recent_yfinance(["SPY", "QQQ"], lookback_days=0) == 1
    c = json.loads((tmp_path / "daily" / "2026-09-22.json").read_text())
    assert c["_final"] and {r["T"]: r["c"] for r in c["results"]}["SPY"] == 776.10
    # next run, Polygon still 403 -> final yfinance copy is used
    dm.client.session.get = lambda *a, **k: FakeResp(403)
    assert dm.client.get_daily_bars(DAY)["SPY"].close == 776.10


def test_poisoned_legacy_cache_discarded(tmp_path, monkeypatch):
    dm = make_dm(tmp_path, monkeypatch)
    f = tmp_path / "daily" / "2026-09-22.json"
    f.write_text(json.dumps({"resultsCount": 1, "_source": "yfinance",
        "results": [{"T": "SPY", "o": 770, "h": 775, "l": 769, "c": 774.88, "v": 1}]}))
    dm.client.session.get = lambda *a, **k: FakeResp(403)
    assert dm.client.get_daily_bars(DAY) == {}
    assert not f.exists()


def test_polygon_replaces_yfinance(tmp_path, monkeypatch):
    dm = make_dm(tmp_path, monkeypatch)
    f = tmp_path / "daily" / "2026-09-22.json"
    f.write_text(json.dumps({"resultsCount": 1, "_source": "yfinance", "_final": True,
        "results": [{"T": "SPY", "o": 770, "h": 777, "l": 769, "c": 776.10, "v": 1}]}))
    poly = {"resultsCount": 1, "results": [{"T": "SPY", "o": 770, "h": 777, "l": 769, "c": 776.05, "v": 2}]}
    dm.client.session.get = lambda *a, **k: FakeResp(200, poly)
    assert dm.client.get_daily_bars(DAY)["SPY"].close == 776.05
    assert "_source" not in json.loads(f.read_text())


def test_session_clock():
    assert not D.session_is_final(DAY, at(9, 41))
    assert not D.session_is_final(DAY, at(16, 5))
    assert D.session_is_final(DAY, at(16, 20))
    assert D.session_is_final(date(2026, 9, 21), at(9, 41))
    # 21:33 ET = 01:33 UTC next day -> market date is still the 22nd
    assert datetime(2026, 9, 23, 1, 33, tzinfo=ZoneInfo("UTC")).astimezone(ET).date() == DAY
