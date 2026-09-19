"""
Gamma Exposure (GEX) — estimated dealer gamma positioning per ticker,
styled after InsiderFinance's GEX page layout.

Epistemic Humility notes (read before trusting these numbers)
---------------------------------------------------------------
1. NOT sourced from InsiderFinance. Their terms state "you are only
   allowed to access the content through the platform" and they publish
   no API on any tier (checked directly, 2026-09-19). Scraping their
   page would violate that. This module only reuses their PAGE LAYOUT
   as a design reference — spot/net-GEX/wall metrics bar, expiry-scope
   breakdown, per-strike bars, a gamma price-profile curve — never their
   data or their (undisclosed, proprietary) formula.

2. Data source is Yahoo Finance's unofficial options-chain endpoint, via
   the `yfinance` library. It is free and requires no API key, but it is
   NOT a published or contracted API: Yahoo's general terms restrict
   automated access, and yfinance has open, unresolved GitHub issues
   about Yahoo rate-limiting it unpredictably (e.g. ranaroussi/yfinance
   #2128, closed "not planned" as of this writing). Treat this feed as
   "free but can break without warning," not as something with an SLA.
   Every ticker fetch is isolated in a try/except — one Yahoo hiccup
   degrades that ticker's GEX to an error field, it does not fail the
   scan.

3. Yahoo supplies open interest and implied volatility per contract.
   It does NOT supply gamma. Gamma here is computed with a plain
   Black-Scholes formula (flat risk-free rate, no dividend yield, each
   contract's own observed IV plugged in) — the same approximation most
   public/open-source GEX calculators use. It is a model output, not a
   vendor-supplied greek, and it inherits Black-Scholes' assumptions
   (European exercise, constant volatility, no early-exercise premium).
   American-style equity/ETF options and real-world skew mean this will
   diverge from any vendor's "true" greeks to some degree — expect
   directional agreement (regime, walls, rough flip level), not
   penny-for-penny accuracy against a paid provider.

4. Dealer-positioning sign convention (calls = dealers net short gamma
   -> counted positive; puts = dealers net long gamma -> counted
   negative) is the standard public convention, not a proven fact about
   any specific market's actual dealer books. Different vendors flip
   or scale this differently — see config.py's gex_* fields docstring.

5. "Zero gamma" / the price-profile curve holds every contract's
   observed IV FIXED while sliding the hypothetical spot price up and
   down. Real IV would shift with spot (skew), so this is a first-order
   approximation of where the flip sits, again the standard
   simplification used by public GEX write-ups, not a forecast.

Scope: options liquidity concentrates in a handful of names, so this
only runs for config.gex_tickers (default: the market ETFs already
tracked elsewhere), not the full scan universe.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from math import erf, exp, log, sqrt, pi
from typing import Optional

from config import ScannerConfig

logger = logging.getLogger("scanner.gex")


# =========================================================================
# DATA STRUCTURES
# =========================================================================

@dataclass
class StrikeGex:
    strike: float
    call_gex: float = 0.0
    put_gex: float = 0.0        # already signed negative
    call_oi: int = 0
    put_oi: int = 0

    @property
    def net_gex(self) -> float:
        return self.call_gex + self.put_gex


@dataclass
class ExpiryGex:
    expiry: date
    dte: int
    bucket: str                  # "0DTE" / "Weekly" / "Monthly" / "Other"
    net_gex: float = 0.0
    gross_gex: float = 0.0       # |call_gex| + |put_gex| — used for the
                                  # "% of total gamma" breakdown
    pct_of_total: float = 0.0


@dataclass
class HeatmapCell:
    strike: float
    expiry: date
    dte: int
    net_gex: float


@dataclass
class PriceProfilePoint:
    spot: float
    net_gex: float


@dataclass
class GexResult:
    ticker: str
    as_of_date: date
    spot: float

    net_gex: float = 0.0
    call_gex: float = 0.0
    put_gex: float = 0.0
    call_oi_total: int = 0
    put_oi_total: int = 0

    zero_gamma: Optional[float] = None
    call_wall: Optional[float] = None
    put_wall: Optional[float] = None

    by_strike: list[StrikeGex] = field(default_factory=list)
    by_expiry: list[ExpiryGex] = field(default_factory=list)
    heatmap: list[HeatmapCell] = field(default_factory=list)
    price_profile: list[PriceProfilePoint] = field(default_factory=list)

    contracts_used: int = 0
    error: Optional[str] = None   # set (and everything else left at
                                   # defaults) if this ticker's fetch failed


@dataclass
class GexSummary:
    as_of_date: date
    results: list[GexResult] = field(default_factory=list)
    skipped_tickers: list[str] = field(default_factory=list)


# =========================================================================
# BLACK-SCHOLES GAMMA
# =========================================================================

def _norm_pdf(x: float) -> float:
    return exp(-0.5 * x * x) / sqrt(2 * pi)


def black_scholes_gamma(spot: float, strike: float, t_years: float,
                         r: float, sigma: float) -> float:
    """Standard Black-Scholes gamma. Returns 0.0 for degenerate inputs
    (expired/zero-DTE, zero or negative vol/price) rather than raising —
    callers are iterating hundreds of contracts and a single bad quote
    should not abort the batch.
    """
    if spot <= 0 or strike <= 0 or t_years <= 0 or sigma <= 0:
        return 0.0
    try:
        d1 = (log(spot / strike) + (r + 0.5 * sigma ** 2) * t_years) / (sigma * sqrt(t_years))
    except (ValueError, ZeroDivisionError):
        return 0.0
    return _norm_pdf(d1) / (spot * sigma * sqrt(t_years))


def dollar_gamma_exposure(gamma: float, open_interest: int, spot: float,
                           contract_multiplier: int = 100) -> float:
    """Per-contract dollar GEX: gamma * OI * multiplier * spot^2 * 0.01.

    This is the standard public convention (same one used by most
    open-source GEX calculators) — a 1% move in spot is assumed to
    require this much notional in dealer hedging. Not vendor-verified.
    """
    return gamma * open_interest * contract_multiplier * spot * spot * 0.01


def classify_expiry_bucket(dte: int) -> str:
    if dte <= 0:
        return "0DTE"
    if dte <= 7:
        return "Weekly"
    if dte <= 35:
        return "Monthly"
    return "Other"


# =========================================================================
# YAHOO FETCH — isolated so the unofficial/fragile part of this module
# is contained to one function. Everything above and below is plain,
# testable math with no network dependency.
# =========================================================================

@dataclass
class RawContract:
    expiry: date
    strike: float
    is_call: bool
    open_interest: int
    implied_vol: float


def fetch_raw_chain(ticker: str, as_of_date: date, max_expiry_days: int,
                     sleep_between_calls: float = 0.4) -> list[RawContract]:
    """Pull open interest + implied vol for every contract within
    `max_expiry_days` of `as_of_date`, for both calls and puts, across
    all expirations in that window.

    Raises on failure — callers must catch. Isolated on purpose: this is
    the one part of the module touching Yahoo's unofficial endpoint.
    """
    import yfinance as yf   # imported lazily so the rest of this module
                             # (and its unit tests) never require yfinance
                             # or network access to be importable

    tk = yf.Ticker(ticker)
    expiries = tk.options  # tuple of "YYYY-MM-DD" strings, nearest first
    if not expiries:
        raise RuntimeError(f"Yahoo returned no option expirations for {ticker}")

    contracts: list[RawContract] = []
    for exp_str in expiries:
        exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
        dte = (exp_date - as_of_date).days
        if dte > max_expiry_days:
            break   # expiries come back sorted nearest-first
        if dte < 0:
            continue

        chain = tk.option_chain(exp_str)
        for is_call, df in ((True, chain.calls), (False, chain.puts)):
            for _, row in df.iterrows():
                oi = row.get("openInterest")
                iv = row.get("impliedVolatility")
                strike = row.get("strike")
                if oi is None or iv is None or strike is None:
                    continue
                if oi <= 0 or iv <= 0:
                    continue   # no position or no usable vol quote
                contracts.append(RawContract(
                    expiry=exp_date, strike=float(strike),
                    is_call=is_call, open_interest=int(oi),
                    implied_vol=float(iv),
                ))

        if sleep_between_calls:
            time.sleep(sleep_between_calls)   # light rate-limit courtesy

    return contracts


# =========================================================================
# AGGREGATION
# =========================================================================

def compute_gex_from_contracts(ticker: str, spot: float, as_of_date: date,
                                contracts: list[RawContract],
                                config: ScannerConfig) -> GexResult:
    """Pure function, no network — takes raw contracts (already fetched)
    and does all the math. Kept separate from fetch_raw_chain so this
    part is fully unit-testable with synthetic data.
    """
    result = GexResult(ticker=ticker, as_of_date=as_of_date, spot=spot)
    if not contracts:
        result.error = "no usable contracts (empty chain or all quotes filtered out)"
        return result

    r = config.gex_risk_free_rate
    by_strike: dict[float, StrikeGex] = {}
    by_expiry: dict[date, ExpiryGex] = {}
    heatmap_cells: dict[tuple[float, date], HeatmapCell] = {}

    for c in contracts:
        dte = max((c.expiry - as_of_date).days, 0)
        t_years = max(dte, 1) / 365.0   # 1-day floor — see module docstring
        gamma = black_scholes_gamma(spot, c.strike, t_years, r, c.implied_vol)
        gex = dollar_gamma_exposure(gamma, c.open_interest, spot)
        signed_gex = gex if c.is_call else -gex

        strike_row = by_strike.setdefault(c.strike, StrikeGex(strike=c.strike))
        if c.is_call:
            strike_row.call_gex += gex
            strike_row.call_oi += c.open_interest
        else:
            strike_row.put_gex += signed_gex
            strike_row.put_oi += c.open_interest

        expiry_row = by_expiry.setdefault(
            c.expiry, ExpiryGex(expiry=c.expiry, dte=dte,
                                 bucket=classify_expiry_bucket(dte))
        )
        expiry_row.net_gex += signed_gex
        expiry_row.gross_gex += gex

        cell_key = (c.strike, c.expiry)
        cell = heatmap_cells.setdefault(
            cell_key, HeatmapCell(strike=c.strike, expiry=c.expiry, dte=dte, net_gex=0.0)
        )
        cell.net_gex += signed_gex

    result.contracts_used = len(contracts)
    result.by_strike = sorted(by_strike.values(), key=lambda s: s.strike)
    result.by_expiry = sorted(by_expiry.values(), key=lambda e: e.expiry)
    result.heatmap = sorted(heatmap_cells.values(), key=lambda h: (h.expiry, h.strike))

    result.call_gex = sum(s.call_gex for s in result.by_strike)
    result.put_gex = sum(s.put_gex for s in result.by_strike)
    result.net_gex = result.call_gex + result.put_gex
    result.call_oi_total = sum(s.call_oi for s in result.by_strike)
    result.put_oi_total = sum(s.put_oi for s in result.by_strike)

    total_gross = sum(e.gross_gex for e in result.by_expiry) or 1.0
    for e in result.by_expiry:
        e.pct_of_total = e.gross_gex / total_gross

    if result.by_strike:
        # "Wall" = strike with the strongest one-sided concentration,
        # matching InsiderFinance's stated definition for these labels.
        call_wall_row = max(result.by_strike, key=lambda s: s.call_gex)
        put_wall_row = min(result.by_strike, key=lambda s: s.put_gex)
        if call_wall_row.call_gex > 0:
            result.call_wall = call_wall_row.strike
        if put_wall_row.put_gex < 0:
            result.put_wall = put_wall_row.strike

    result.price_profile = _compute_price_profile(spot, contracts, r, as_of_date,
                                                    config.gex_price_profile_pct)
    result.zero_gamma = _find_zero_gamma(result.price_profile, spot)

    return result


def _compute_price_profile(spot: float, contracts: list[RawContract], r: float,
                            as_of_date: date, pct_range: float,
                            steps: int = 41) -> list[PriceProfilePoint]:
    """Net GEX re-priced at a grid of hypothetical spot levels, holding
    each contract's observed IV and OI fixed (see module docstring,
    point 5, for why that's a simplification worth knowing about).
    """
    if not contracts or spot <= 0:
        return []
    low = spot * (1 - pct_range)
    high = spot * (1 + pct_range)
    if steps < 3:
        steps = 3
    step_size = (high - low) / (steps - 1)

    profile = []
    for i in range(steps):
        hypo_spot = low + i * step_size
        net = 0.0
        for c in contracts:
            dte = max((c.expiry - as_of_date).days, 0)
            t_years = max(dte, 1) / 365.0
            gamma = black_scholes_gamma(hypo_spot, c.strike, t_years, r, c.implied_vol)
            gex = dollar_gamma_exposure(gamma, c.open_interest, hypo_spot)
            net += gex if c.is_call else -gex
        profile.append(PriceProfilePoint(spot=round(hypo_spot, 2), net_gex=net))
    return profile


def _find_zero_gamma(profile: list[PriceProfilePoint], current_spot: float) -> Optional[float]:
    """Linear-interpolated sign-crossing nearest the current spot price."""
    if len(profile) < 2:
        return None
    crossings = []
    for a, b in zip(profile, profile[1:]):
        if a.net_gex == 0:
            crossings.append(a.spot)
        elif (a.net_gex < 0) != (b.net_gex < 0):
            span = b.net_gex - a.net_gex
            if span == 0:
                continue
            frac = -a.net_gex / span
            crossings.append(a.spot + frac * (b.spot - a.spot))
    if not crossings:
        return None
    return min(crossings, key=lambda level: abs(level - current_spot))


# =========================================================================
# ENTRY POINT
# =========================================================================

def compute_gex_summary(config: ScannerConfig, spot_by_ticker: dict[str, float],
                         as_of_date: date) -> GexSummary:
    """Run GEX for every ticker in config.gex_tickers. Spot prices are
    passed in (sourced from the scanner's own Polygon/Massive daily
    bars, already fetched in Phase 1) rather than trusted from Yahoo's
    quote — Yahoo is only used here for open interest + IV, not price.

    One ticker's Yahoo failure never aborts the others.
    """
    summary = GexSummary(as_of_date=as_of_date)
    if not config.gex_enabled:
        return summary

    for ticker in config.gex_tickers:
        spot = spot_by_ticker.get(ticker)
        if spot is None:
            logger.warning(f"GEX: no spot price available for {ticker}, skipping")
            summary.skipped_tickers.append(ticker)
            continue
        try:
            contracts = fetch_raw_chain(ticker, as_of_date, config.gex_max_expiry_days)
            result = compute_gex_from_contracts(ticker, spot, as_of_date, contracts, config)
            summary.results.append(result)
            if result.error:
                logger.warning(f"GEX: {ticker}: {result.error}")
        except Exception as e:
            logger.warning(f"GEX: failed to fetch/compute for {ticker}: {e}")
            summary.results.append(GexResult(
                ticker=ticker, as_of_date=as_of_date,
                spot=spot, error=str(e),
            ))
            summary.skipped_tickers.append(ticker)

    return summary
