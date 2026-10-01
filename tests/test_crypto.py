"""Tests for the crypto scan: registry, Yahoo parsing, data guards, universe
rules, and an end-to-end run on synthetic data.

No network. The Yahoo downloader is injected, so these pin down OUR logic;
they say nothing about whether Yahoo currently serves the data (that can
only be checked by a real run).
"""
from __future__ import annotations

import json
import random
import re
from datetime import date, timedelta

import pandas as pd
import pytest

import crypto_data
import scanner
from config import ScannerConfig
from crypto_data import (
    CryptoDataError,
    CryptoDataManager,
    derive_crypto_universe,
    last_closed_day,
    parse_yf_frame,
)
from crypto_universe import CRYPTO_CANDIDATES, EXCLUDED_BASES, get_candidates
from timeframes import DailyBar, aggregate_weekly

TODAY = date(2026, 9, 30)          # a Wednesday
CLOSED = TODAY - timedelta(days=1)


def _fake_utc_today(now=None):
    """Fixed 'today' unless a specific time is passed (as the real one allows)."""
    from datetime import timezone
    return TODAY if now is None else now.astimezone(timezone.utc).date()


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr(crypto_data, "utc_today", _fake_utc_today)


def _bars(seed: int, end: date, days: int = 200, px: float = 100.0,
          vol: float = 5e7) -> list[DailyBar]:
    r = random.Random(seed)
    out, d = [], end - timedelta(days=days - 1)
    while d <= end:
        o = px * (1 + r.uniform(-0.01, 0.01))
        c = o * (1 + r.uniform(-0.04, 0.04))
        h = max(o, c) * (1 + r.uniform(0, 0.02))
        l = min(o, c) * (1 - r.uniform(0, 0.02))
        out.append(DailyBar(dt=d, open=o, high=h, low=l, close=c, volume=vol))
        px = c
        d += timedelta(days=1)
    return out


def make_downloader(*, drop=(), end=TODAY, low_volume=(), prices=None):
    """Synthetic Yahoo: returns bars through `end` (incl. a partial TODAY bar)
    for every requested symbol except those in `drop`."""
    prices = prices or {}
    calls = []

    def dl(symbols, start, stop):
        calls.append((list(symbols), start, stop))
        out = {}
        for i, sym in enumerate(symbols):
            if sym in drop:
                continue
            vol = 1e5 if sym in low_volume else 5e7 + i * 1e6
            bars = _bars(abs(hash(sym)) % 10_000, end,
                         px=prices.get(sym, 100.0), vol=vol)
            out[sym] = [b for b in bars if start <= b.dt <= stop]
        return out

    dl.calls = calls
    return dl


def mgr(downloader, **cfg):
    config = ScannerConfig()
    for k, v in cfg.items():
        setattr(config, k, v)
    return CryptoDataManager(config, downloader=downloader, sleep=lambda s: None)


# =========================================================================
# REGISTRY
# =========================================================================

class TestRegistry:
    def test_unique_tickers_and_yahoo_symbols(self):
        tickers = [a.ticker for a in CRYPTO_CANDIDATES]
        yahoo = [a.yahoo for a in CRYPTO_CANDIDATES]
        assert len(tickers) == len(set(tickers))
        assert len(yahoo) == len(set(yahoo))

    def test_symbol_shape(self):
        for a in CRYPTO_CANDIDATES:
            assert re.fullmatch(r"[A-Z0-9]+-USD", a.ticker), a
            assert re.fullmatch(r"[A-Z0-9]+-USD", a.yahoo), a
            # clean ticker never carries Yahoo's numeric disambiguator
            assert not re.search(r"\d{3,}-USD$", a.ticker), a

    def test_no_stable_wrapped_or_staked_candidates(self):
        for a in CRYPTO_CANDIDATES:
            base = a.yahoo.split("-")[0]
            assert base not in EXCLUDED_BASES, a

    def test_btc_eth_present(self):
        t = {a.ticker for a in CRYPTO_CANDIDATES}
        assert {"BTC-USD", "ETH-USD"} <= t

    def test_extras_added_and_deduped(self):
        out = get_candidates(["wif-usd", "BTC-USD", "UNI7083-USD", " ", "WIF-USD"])
        tickers = [a.ticker for a in out]
        assert tickers.count("WIF-USD") == 1
        assert tickers.count("BTC-USD") == 1
        assert "UNI7083-USD" not in tickers      # already a built-in's yahoo symbol
        assert len(out) == len(CRYPTO_CANDIDATES) + 1


# =========================================================================
# YAHOO PARSING
# =========================================================================

def _frame(symbols, layout="field_first", tz=None, n=6):
    idx = pd.date_range("2026-09-20", periods=n, freq="D", tz=tz)
    cols = {}
    for s in symbols:
        for f, base in (("Open", 100), ("High", 105), ("Low", 95),
                        ("Close", 102), ("Volume", 1e9)):
            cols[(f, s) if layout == "field_first" else (s, f)] = [base + i for i in range(n)]
    return pd.DataFrame(cols, index=idx)


class TestParse:
    @pytest.mark.parametrize("layout", ["field_first", "ticker_first"])
    def test_multiindex_layouts(self, layout):
        df = _frame(["BTC-USD", "ETH-USD"], layout)
        out = parse_yf_frame(df, ["BTC-USD", "ETH-USD"])
        assert set(out) == {"BTC-USD", "ETH-USD"}
        assert len(out["BTC-USD"]) == 6
        assert out["BTC-USD"][0].dt == date(2026, 9, 20)
        assert out["BTC-USD"][0].open == 100 and out["BTC-USD"][0].close == 102

    def test_flat_single_symbol(self):
        df = _frame(["BTC-USD"]).droplevel(1, axis=1)
        out = parse_yf_frame(df, ["BTC-USD"])
        assert list(out) == ["BTC-USD"] and len(out["BTC-USD"]) == 6

    def test_tz_aware_index_converted_to_utc_date(self):
        df = _frame(["BTC-USD"], tz="UTC")
        out = parse_yf_frame(df, ["BTC-USD"])
        assert out["BTC-USD"][0].dt == date(2026, 9, 20)

    def test_empty_and_none(self):
        assert parse_yf_frame(None, ["X"]) == {}
        assert parse_yf_frame(pd.DataFrame(), ["X"]) == {}

    def test_bad_rows_dropped(self):
        df = _frame(["BTC-USD"])
        df.loc[df.index[1], ("Close", "BTC-USD")] = float("nan")     # NaN
        df.loc[df.index[2], ("Low", "BTC-USD")] = 0.0                # zero price
        for f in ("Open", "High", "Low", "Close"):                   # stale flat
            df.loc[df.index[3], (f, "BTC-USD")] = 50.0
        df.loc[df.index[3], ("Volume", "BTC-USD")] = 0.0
        out = parse_yf_frame(df, ["BTC-USD"])["BTC-USD"]
        assert [b.dt.day for b in out] == [20, 24, 25]

    def test_flat_bar_with_volume_is_kept(self):
        df = _frame(["BTC-USD"])
        for f in ("Open", "High", "Low", "Close"):
            df.loc[df.index[0], (f, "BTC-USD")] = 50.0
        out = parse_yf_frame(df, ["BTC-USD"])["BTC-USD"]
        assert out[0].dt.day == 20

    def test_range_clamped_to_contain_body(self):
        df = _frame(["BTC-USD"])
        df.loc[df.index[0], ("High", "BTC-USD")] = 90.0     # below open/close
        b = parse_yf_frame(df, ["BTC-USD"])["BTC-USD"][0]
        assert b.high >= max(b.open, b.close) and b.low <= min(b.open, b.close)

    def test_duplicate_dates_last_wins(self):
        df = _frame(["BTC-USD"])
        dup = df.iloc[[0]].copy()
        dup[("Close", "BTC-USD")] = 101.5
        df = pd.concat([df, dup]).sort_index(kind="stable")
        out = parse_yf_frame(df, ["BTC-USD"])["BTC-USD"]
        assert len(out) == 6 and out[0].close == 101.5


# =========================================================================
# CALENDAR
# =========================================================================

def test_last_closed_day_is_yesterday_utc():
    from datetime import datetime, timezone
    assert last_closed_day(datetime(2026, 10, 1, 0, 10, tzinfo=timezone.utc)) == date(2026, 9, 30)
    assert last_closed_day(datetime(2026, 9, 30, 23, 59, tzinfo=timezone.utc)) == date(2026, 9, 29)


def test_weekly_bar_has_seven_days_for_crypto():
    # 14 consecutive days starting Monday -> exactly two full weeks of 7
    start = date(2026, 9, 7)
    bars = [DailyBar(start + timedelta(days=i), 1, 2, 0.5, 1.5, 1) for i in range(14)]
    wk = aggregate_weekly(bars)
    assert [w.bar_count for w in wk] == [7, 7]


# =========================================================================
# UNIVERSE
# =========================================================================

def _universe_inputs(n_liquid=30, n_thin=5):
    bars = {}
    for i in range(n_liquid):
        bars[f"C{i:02d}-USD"] = _bars(i, CLOSED, days=40, vol=2e7 + i * 1e6)
    for i in range(n_thin):
        bars[f"THIN{i}-USD"] = _bars(100 + i, CLOSED, days=40, vol=1e5)
    bars["BTC-USD"] = _bars(900, CLOSED, days=40, vol=1e5)    # thin on paper
    return bars


class TestUniverse:
    def test_floor_and_topn_and_market_force_include(self):
        cfg = ScannerConfig()
        cfg.crypto_universe_size = 25
        u = derive_crypto_universe(_universe_inputs(), cfg)
        assert len(u) == 26                      # top 25 + BTC force-included
        assert "BTC-USD" in u
        assert not any(t.startswith("THIN") for t in u)
        # highest-volume liquid coin kept, lowest dropped by the top-N cut
        assert "C29-USD" in u and "C00-USD" not in u

    def test_needs_half_the_lookback(self):
        cfg = ScannerConfig()
        bars = _universe_inputs()
        bars["NEWCOIN-USD"] = _bars(5, CLOSED, days=6, vol=9e9)   # 6 < 10 days
        assert "NEWCOIN-USD" not in derive_crypto_universe(bars, cfg)

    def test_refuses_implausibly_small_universe(self):
        cfg = ScannerConfig()
        bars = {f"C{i}-USD": _bars(i, CLOSED, days=40, vol=1e3) for i in range(30)}
        with pytest.raises(CryptoDataError, match="refusing to publish"):
            derive_crypto_universe(bars, cfg)


# =========================================================================
# DATA MANAGER
# =========================================================================

class TestDataManager:
    def test_partial_today_bar_never_scanned(self):
        dm = mgr(make_downloader())
        dm.fetch_history()
        assert dm.as_of == CLOSED
        assert all(b[-1].dt == CLOSED for b in dm.bars.values())

    def test_include_partial_keeps_today(self):
        dm = mgr(make_downloader())
        dm.fetch_history(include_partial=True)
        assert dm.as_of == TODAY
        assert dm.bars["BTC-USD"][-1].dt == TODAY

    def test_explicit_open_day_clamped(self):
        dm = mgr(make_downloader())
        dm.fetch_history(as_of=TODAY)
        assert dm.as_of == CLOSED

    def test_explicit_past_date_for_backtest(self):
        past = TODAY - timedelta(days=10)
        dm = mgr(make_downloader())
        dm.fetch_history(as_of=past)
        assert dm.as_of == past
        assert all(b[-1].dt == past for b in dm.bars.values())

    def test_chunked_downloads_cover_every_candidate_once(self):
        dl = make_downloader()
        dm = mgr(dl)
        dm.fetch_history()
        asked = [s for call in dl.calls for s in call[0]]
        assert sorted(asked) == sorted(a.yahoo for a in dm.assets)
        assert all(len(c[0]) <= crypto_data.DOWNLOAD_CHUNK_SIZE for c in dl.calls)

    def test_missing_symbols_reported_not_fatal(self):
        dm = mgr(make_downloader(drop={"UNI7083-USD", "SUI20947-USD"}))
        dm.fetch_history()
        assert set(dm.skipped) == {"UNI-USD", "SUI-USD"}
        assert "UNI-USD" not in dm.bars

    def test_too_few_bars_is_skipped(self):
        def dl(symbols, start, stop):
            out = make_downloader()(symbols, start, stop)
            if "PEPE24478-USD" in out:       # symbol lives in only one chunk
                out["PEPE24478-USD"] = out["PEPE24478-USD"][-3:]
            return out
        dm = mgr(dl)
        dm.fetch_history()
        assert "PEPE-USD" in dm.skipped

    def test_missing_market_ticker_aborts(self):
        dm = mgr(make_downloader(drop={"BTC-USD"}))
        with pytest.raises(CryptoDataError, match="market ticker"):
            dm.fetch_history()

    def test_low_coverage_aborts(self):
        keep = {"BTC-USD", "ETH-USD", "SOL-USD"}
        drop = {a.yahoo for a in CRYPTO_CANDIDATES} - keep
        dm = mgr(make_downloader(drop=drop))
        with pytest.raises(CryptoDataError, match="Refusing to publish"):
            dm.fetch_history()

    def test_total_outage_aborts(self):
        dm = mgr(lambda s, a, b: {})
        with pytest.raises(CryptoDataError):
            dm.fetch_history()

    def test_download_exception_is_retried_then_recovers(self):
        good = make_downloader()
        state = {"n": 0}

        def flaky(symbols, a, b):
            state["n"] += 1
            if state["n"] <= 2:
                raise RuntimeError("yahoo 429")
            return good(symbols, a, b)

        dm = mgr(flaky)
        dm.fetch_history()
        assert "BTC-USD" in dm.bars

    def test_stale_market_data_waits_then_recovers(self):
        sleeps = []
        old = make_downloader(end=CLOSED - timedelta(days=1))   # yesterday missing
        new = make_downloader(end=TODAY)
        calls = {"round": 0}

        def dl(symbols, a, b):
            # first full pass (all chunks) is stale, afterwards fresh
            n_chunks = -(-len(CRYPTO_CANDIDATES) // crypto_data.DOWNLOAD_CHUNK_SIZE)
            calls["round"] += 1
            return (old if calls["round"] <= n_chunks else new)(symbols, a, b)

        dm = CryptoDataManager(ScannerConfig(), downloader=dl,
                               sleep=sleeps.append, freshness_wait_seconds=300)
        dm.fetch_history()
        assert 300 in sleeps
        assert dm.bars["BTC-USD"][-1].dt == CLOSED

    def test_persistently_stale_market_data_aborts(self):
        dm = CryptoDataManager(
            ScannerConfig(),
            downloader=make_downloader(end=CLOSED - timedelta(days=2)),
            sleep=lambda s: None, freshness_retries=2,
        )
        with pytest.raises(CryptoDataError, match="stale"):
            dm.fetch_history()


# =========================================================================
# PRICE ROUNDING
# =========================================================================

class TestRounding:
    def test_sub_penny_survives(self):
        assert scanner.round_crypto_price(0.00000882) == 0.00000882
        assert scanner.round_crypto_price(0.0000123456789) == 0.0000123457

    def test_large_prices_stay_sane(self):
        assert scanner.round_crypto_price(97123.456789) == 97123.5
        assert scanner.round_crypto_price(1.5) == 1.5

    def test_stock_rounding_unchanged(self):
        assert scanner._round_stock_price(172.3149) == 172.31
        assert scanner._round_stock_price(0.456) == 0.46


# =========================================================================
# CONFIG / EMAIL PLUMBING
# =========================================================================

def test_env_overrides(monkeypatch):
    monkeypatch.setenv("CRYPTO_EXTRA_SYMBOLS", "wif-usd, jup-usd ,")
    monkeypatch.setenv("CRYPTO_UNIVERSE_SIZE", "42")
    cfg = scanner.load_config()
    assert cfg.crypto_extra_symbols == ["WIF-USD", "JUP-USD"]
    assert cfg.crypto_universe_size == 42


def test_email_subject_label_defaults_unchanged():
    from alerts import build_alert_subject
    d = date(2026, 9, 30)
    assert build_alert_subject(3, 1, d).startswith("STRAT Scanner — 3 Signals")
    assert build_alert_subject(3, 1, d, label="STRAT Scanner (Crypto)").startswith(
        "STRAT Scanner (Crypto) — 3 Signals")


# =========================================================================
# END-TO-END
# =========================================================================

STOCK_KEYS = {
    "scan_date", "timestamp", "config", "summary", "market", "signals",
    "dominos", "market_breadth", "panels", "sfp_signals", "bf_signals",
    "bf_active", "bf_reversals", "sector_rotation", "gex", "etf_holdings",
}


@pytest.fixture(scope="module")
def e2e(tmp_path_factory):
    # module-scoped fixtures can't use the function-scoped clock fixture
    mp = pytest.MonkeyPatch()
    mp.setattr(crypto_data, "utc_today", _fake_utc_today)
    try:
        out = tmp_path_factory.mktemp("crypto_out")
        dl = make_downloader(prices={"SHIB-USD": 0.0000088, "PEPE24478-USD": 0.0000095,
                                     "BTC-USD": 97000.0})
        res = scanner.run_crypto_scan(
            config=ScannerConfig(), output_dir=out, downloader=dl,
        )
        yield res, out
    finally:
        mp.undo()


class TestEndToEnd:
    def test_schema_is_stock_schema_plus_crypto_keys(self, e2e):
        res, _ = e2e
        assert STOCK_KEYS <= set(res)
        assert set(res) - STOCK_KEYS == {"asset_class", "crypto_meta", "skipped_tickers"}
        assert res["asset_class"] == "crypto"

    def test_scan_date_is_last_closed_utc_day(self, e2e):
        res, _ = e2e
        assert res["scan_date"] == CLOSED.isoformat()

    def test_not_applicable_sections_are_null(self, e2e):
        res, _ = e2e
        assert res["sector_rotation"] is None and res["gex"] is None
        assert res["etf_holdings"] is None
        assert res["market_breadth"]["vix"] is None

    def test_real_scan_happened(self, e2e):
        res, _ = e2e
        assert res["summary"]["tickers_scanned"] > 20
        assert res["summary"]["total_signals"] > 0
        assert len(res["panels"]) > 20

    def test_market_summary_is_btc_eth(self, e2e):
        res, _ = e2e
        assert [m["ticker"] for m in res["market"]] == ["BTC-USD", "ETH-USD"]

    def test_panel_and_signal_fields_match_stock_shape(self, e2e):
        res, _ = e2e
        p = res["panels"]["BTC-USD"]
        assert {"M", "W", "D", "ft", "fd", "cb", "lc", "chg"} <= set(p)
        sig_keys = {"ticker", "tf", "direction", "signal_type", "pattern_tag",
                    "combo", "trigger", "stop", "mag", "exh", "hammer",
                    "shooter", "ftfc", "in_force", "c1_f2"}
        assert all(set(s) == sig_keys for s in res["signals"])

    def test_sub_penny_last_close_not_zeroed(self, e2e):
        res, _ = e2e
        assert 0 < res["panels"]["SHIB-USD"]["lc"] < 0.001
        assert 0 < res["panels"]["PEPE-USD"]["lc"] < 0.001

    def test_tickers_are_clean_not_yahoo_ids(self, e2e):
        res, _ = e2e
        assert not any(re.search(r"\d{3,}", t) for t in res["panels"])
        meta = res["crypto_meta"]["PEPE-USD"]
        assert meta == {"name": "Pepe", "yahoo": "PEPE24478-USD"}
        assert set(res["panels"]) <= set(res["crypto_meta"])

    def test_no_stablecoins_or_wrapped_in_output(self, e2e):
        res, _ = e2e
        bases = {t.split("-")[0] for t in res["panels"]}
        assert not (bases & EXCLUDED_BASES)

    def test_output_file_is_strict_json_in_crypto_folder(self, e2e):
        res, out = e2e
        f = out / f"scan_{CLOSED.isoformat()}.json"
        assert f.exists()

        def bad(c):
            raise ValueError(f"non-finite number {c} in output")

        data = json.loads(f.read_text(), parse_constant=bad)
        assert data["asset_class"] == "crypto"

    def test_ftfc_flags_consistent(self, e2e):
        res, _ = e2e
        for t, p in res["panels"].items():
            assert p["ft"] in ("u", "d", "")
            assert set(p["fd"]) <= {"M", "W", "D"}
