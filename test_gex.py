"""
Tests for gex.py — Black-Scholes gamma math and GEX aggregation.

Deliberately does NOT test fetch_raw_chain() against live Yahoo — that
function is a thin, isolated wrapper around an unofficial network
endpoint (see gex.py's module docstring) and is exercised in practice
by the scheduled scan run, not by CI. Everything tested here is the
pure math: given contracts, does the aggregation come out right.
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from datetime import date

from config import ScannerConfig
from gex import (
    RawContract,
    PriceProfilePoint,
    black_scholes_gamma,
    dollar_gamma_exposure,
    classify_expiry_bucket,
    compute_gex_from_contracts,
    compute_gex_summary,
    _find_zero_gamma,
)


# =========================================================================
# BLACK-SCHOLES GAMMA
# =========================================================================

def test_gamma_matches_known_reference_value():
    # S=100, K=100, T=1y, r=5%, sigma=20% is a textbook reference case;
    # widely-published gamma for these inputs is ~0.018762.
    g = black_scholes_gamma(100, 100, 1.0, 0.05, 0.20)
    assert g == pytest.approx(0.018762, abs=1e-4)


def test_gamma_symmetric_around_atm():
    # Gamma peaks at-the-money and is lower on both sides of it.
    atm = black_scholes_gamma(100, 100, 0.5, 0.03, 0.25)
    itm = black_scholes_gamma(100, 80, 0.5, 0.03, 0.25)
    otm = black_scholes_gamma(100, 120, 0.5, 0.03, 0.25)
    assert atm > itm
    assert atm > otm


@pytest.mark.parametrize("spot,strike,t,r,sigma", [
    (0, 100, 1.0, 0.05, 0.2),      # zero spot
    (100, 0, 1.0, 0.05, 0.2),      # zero strike
    (100, 100, 0.0, 0.05, 0.2),    # zero time (0DTE, unfloored)
    (100, 100, 1.0, 0.05, 0.0),    # zero vol
    (-5, 100, 1.0, 0.05, 0.2),     # negative spot
])
def test_gamma_degenerate_inputs_return_zero_not_raise(spot, strike, t, r, sigma):
    assert black_scholes_gamma(spot, strike, t, r, sigma) == 0.0


def test_dollar_gamma_exposure_scales_with_oi_and_spot_squared():
    gamma = 0.02
    gex_1x = dollar_gamma_exposure(gamma, open_interest=100, spot=100)
    gex_2x_oi = dollar_gamma_exposure(gamma, open_interest=200, spot=100)
    assert gex_2x_oi == pytest.approx(2 * gex_1x)
    # spot doubles -> spot^2 quadruples the dollar exposure
    gex_2x_spot = dollar_gamma_exposure(gamma, open_interest=100, spot=200)
    assert gex_2x_spot == pytest.approx(4 * gex_1x)


# =========================================================================
# EXPIRY BUCKETS
# =========================================================================

@pytest.mark.parametrize("dte,expected", [
    (0, "0DTE"), (-1, "0DTE"),
    (1, "Weekly"), (7, "Weekly"),
    (8, "Monthly"), (35, "Monthly"),
    (36, "Other"), (200, "Other"),
])
def test_classify_expiry_bucket(dte, expected):
    assert classify_expiry_bucket(dte) == expected


# =========================================================================
# AGGREGATION
# =========================================================================

def test_compute_gex_empty_contracts_sets_error_not_crash():
    result = compute_gex_from_contracts(
        "SPY", 500.0, date(2026, 9, 19), [], ScannerConfig()
    )
    assert result.error is not None
    assert result.net_gex == 0.0


def test_compute_gex_call_only_book_is_net_positive():
    contracts = [
        RawContract(expiry=date(2026, 9, 26), strike=500, is_call=True,
                    open_interest=1000, implied_vol=0.15),
        RawContract(expiry=date(2026, 10, 17), strike=510, is_call=True,
                    open_interest=500, implied_vol=0.14),
    ]
    result = compute_gex_from_contracts("SPY", 500.0, date(2026, 9, 19),
                                         contracts, ScannerConfig())
    assert result.error is None
    assert result.net_gex > 0
    assert result.put_gex == 0.0
    assert result.put_wall is None      # no put-side concentration to report
    assert result.call_wall in (500, 510)


def test_compute_gex_put_only_book_is_net_negative():
    contracts = [
        RawContract(expiry=date(2026, 9, 26), strike=490, is_call=False,
                    open_interest=1000, implied_vol=0.15),
    ]
    result = compute_gex_from_contracts("SPY", 500.0, date(2026, 9, 19),
                                         contracts, ScannerConfig())
    assert result.net_gex < 0
    assert result.call_gex == 0.0
    assert result.call_wall is None
    assert result.put_wall == 490


def test_by_strike_and_by_expiry_sum_back_to_totals():
    contracts = [
        RawContract(expiry=date(2026, 9, 26), strike=500, is_call=True,
                    open_interest=1000, implied_vol=0.15),
        RawContract(expiry=date(2026, 9, 26), strike=500, is_call=False,
                    open_interest=800, implied_vol=0.16),
        RawContract(expiry=date(2026, 10, 17), strike=510, is_call=True,
                    open_interest=500, implied_vol=0.14),
    ]
    result = compute_gex_from_contracts("SPY", 500.0, date(2026, 9, 19),
                                         contracts, ScannerConfig())
    assert sum(s.net_gex for s in result.by_strike) == pytest.approx(result.net_gex)
    assert sum(e.net_gex for e in result.by_expiry) == pytest.approx(result.net_gex)
    # one heatmap cell per distinct (strike, expiry) pair, not per contract
    assert len(result.heatmap) == 2
    pcts = [e.pct_of_total for e in result.by_expiry]
    assert sum(pcts) == pytest.approx(1.0)


def test_price_profile_has_expected_point_count_and_brackets_spot():
    contracts = [
        RawContract(expiry=date(2026, 9, 26), strike=500, is_call=True,
                    open_interest=1000, implied_vol=0.15),
    ]
    result = compute_gex_from_contracts("SPY", 500.0, date(2026, 9, 19),
                                         contracts, ScannerConfig())
    assert len(result.price_profile) == 41
    lows = [p.spot for p in result.price_profile]
    assert min(lows) < 500 < max(lows)


def test_zero_gamma_finds_nearest_crossing():
    profile = [
        PriceProfilePoint(spot=90, net_gex=-100),
        PriceProfilePoint(spot=100, net_gex=50),
        PriceProfilePoint(spot=110, net_gex=200),
    ]
    # linear interpolation between (90,-100) and (100,50): crosses at 96.67
    assert _find_zero_gamma(profile, 100) == pytest.approx(96.667, abs=0.01)


def test_zero_gamma_none_when_no_sign_change():
    profile = [PriceProfilePoint(spot=x, net_gex=100) for x in (90, 100, 110)]
    assert _find_zero_gamma(profile, 100) is None


# =========================================================================
# SUMMARY / GRACEFUL DEGRADATION
# =========================================================================

def test_compute_gex_summary_disabled_returns_empty():
    config = ScannerConfig(gex_enabled=False)
    summary = compute_gex_summary(config, {"SPY": 500.0}, date(2026, 9, 19))
    assert summary.results == []


def test_compute_gex_summary_skips_ticker_with_no_spot_price():
    config = ScannerConfig(gex_tickers=["SPY", "MISSING"])
    summary = compute_gex_summary(config, {"SPY": 500.0}, date(2026, 9, 19))
    assert "MISSING" in summary.skipped_tickers
    # SPY still attempted (and will fail here since fetch_raw_chain hits
    # the real network — acceptable: the point of this test is that a
    # missing spot price short-circuits gracefully, not what SPY does)


def test_compute_gex_summary_one_ticker_failure_does_not_abort_others(monkeypatch):
    import gex as gex_module

    def fake_fetch(ticker, as_of_date, max_expiry_days, sleep_between_calls=0.0):
        if ticker == "QQQ":
            raise RuntimeError("simulated Yahoo rate limit")
        return [RawContract(expiry=date(2026, 9, 26), strike=500, is_call=True,
                             open_interest=100, implied_vol=0.15)]

    monkeypatch.setattr(gex_module, "fetch_raw_chain", fake_fetch)
    config = ScannerConfig(gex_tickers=["SPY", "QQQ"])
    summary = compute_gex_summary(config, {"SPY": 500.0, "QQQ": 400.0},
                                   date(2026, 9, 19))
    by_ticker = {r.ticker: r for r in summary.results}
    assert by_ticker["SPY"].error is None
    assert by_ticker["QQQ"].error is not None
    assert "QQQ" in summary.skipped_tickers
