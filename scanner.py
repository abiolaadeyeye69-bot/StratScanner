"""
Scanner Orchestration — main loop that ties everything together.

This is the entry point.  It:
1. Loads configuration from environment / defaults
2. Fetches market data via Polygon.io
3. Derives the stock universe
4. Scans all tickers across all enabled timeframes
5. Generates market/sector summary
6. Sends email alerts
7. Writes results to disk (JSON)

Can be run as a one-shot (python scanner.py) or on a cron/Railway schedule.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from config import ScannerConfig
from data import DataManager
from signals import (
    TickerScanResult,
    MarketSummary,
    scan_ticker,
    compute_market_summary,
    format_signal_line,
    format_domino_line,
    format_panel,
)
from sfp import (
    TickerSFPResult,
    scan_ticker_sfp,
    format_sfp_line,
)
from broadening_formation import (
    TickerMagnitudeResult,
    scan_ticker_bf,
    format_bf_signal_line,
    format_bf_active_line,
)
from sector_universe import get_sector_universe
from sector_rotation import (
    RotationResult,
    compute_sector_rotation,
    leaders,
    laggards,
    format_rotation_line,
    format_period_label,
)
from alerts import send_alert_email
from gex import GexSummary, compute_gex_summary

# =========================================================================
# LOGGING SETUP
# =========================================================================

def setup_logging(verbose: bool = False) -> None:
    """Configure logging for the scanner."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


# =========================================================================
# CONFIG FROM ENVIRONMENT
# =========================================================================

def load_config() -> ScannerConfig:
    """Load scanner config, overriding defaults from environment variables."""
    config = ScannerConfig()

    # API key
    config.polygon_api_key = os.environ.get("POLYGON_API_KEY", "")

    # SMTP
    config.smtp_email = os.environ.get("SMTP_EMAIL", "")
    config.smtp_password = os.environ.get("SMTP_PASSWORD", "")

    # Recipients (comma-separated)
    recipients_str = os.environ.get("ALERT_RECIPIENTS", "")
    if recipients_str:
        config.alert_recipients = [
            r.strip() for r in recipients_str.split(",") if r.strip()
        ]

    # Timeframes (comma-separated, default M,W,D)
    tf_str = os.environ.get("ENABLED_TIMEFRAMES", "")
    if tf_str:
        config.enabled_timeframes = [
            t.strip() for t in tf_str.split(",") if t.strip()
        ]

    # Universe size
    universe_str = os.environ.get("UNIVERSE_SIZE", "")
    if universe_str:
        try:
            config.universe_size = int(universe_str)
        except ValueError:
            pass

    return config


# =========================================================================
# RESULT SERIALIZATION
# =========================================================================

def serialize_results(
    scan_results: list[TickerScanResult],
    market_summaries: list[MarketSummary],
    config: ScannerConfig,
    scan_date: date,
    sfp_results: Optional[list[TickerSFPResult]] = None,
    bf_results: Optional[list[TickerMagnitudeResult]] = None,
    rotation_result: Optional[RotationResult] = None,
    gex_summary: Optional[GexSummary] = None,
    all_panels: Optional[dict[str, dict]] = None,
    market_breadth: Optional[dict] = None,
    etf_holdings: Optional[dict] = None,
) -> dict:
    """Serialize scan results to a JSON-compatible dict."""
    sfp_results = sfp_results or []
    bf_results = bf_results or []
    all_sfp_signals = [s for r in sfp_results for s in r.signals]
    all_bf_signals = [s for r in bf_results for s in r.signals]
    all_bf_active_long = [s for r in bf_results for s in r.active_long]
    all_bf_active_short = [s for r in bf_results for s in r.active_short]

    rotation_json = None
    if rotation_result is not None:
        rotation_json = {
            "as_of_date": (
                rotation_result.as_of_date.isoformat()
                if rotation_result.as_of_date else None
            ),
            "periods": rotation_result.periods,
            "skipped_tickers": rotation_result.skipped_tickers,
            "entries": [
                {
                    "ticker": e.ticker,
                    "label": e.label,
                    "category": e.category,
                    "parent": e.parent,
                    "returns": e.returns,
                    "ranks": e.ranks,
                    "composite_rank": e.composite_rank,
                    "last_close": e.last_close,
                }
                for e in rotation_result.entries
            ],
        }

    gex_json = None
    if gex_summary is not None:
        gex_json = {
            "as_of_date": gex_summary.as_of_date.isoformat(),
            "skipped_tickers": gex_summary.skipped_tickers,
            "results": [
                {
                    "ticker": r.ticker,
                    "spot": r.spot,
                    "error": r.error,
                    "net_gex": r.net_gex,
                    "call_gex": r.call_gex,
                    "put_gex": r.put_gex,
                    "call_oi_total": r.call_oi_total,
                    "put_oi_total": r.put_oi_total,
                    "zero_gamma": r.zero_gamma,
                    "call_wall": r.call_wall,
                    "put_wall": r.put_wall,
                    "contracts_used": r.contracts_used,
                    "by_strike": [
                        {
                            "strike": s.strike,
                            "call_gex": s.call_gex,
                            "put_gex": s.put_gex,
                            "net_gex": s.net_gex,
                            "call_oi": s.call_oi,
                            "put_oi": s.put_oi,
                        }
                        for s in r.by_strike
                    ],
                    "by_expiry": [
                        {
                            "expiry": e.expiry.isoformat(),
                            "dte": e.dte,
                            "bucket": e.bucket,
                            "net_gex": e.net_gex,
                            "gross_gex": e.gross_gex,
                            "pct_of_total": e.pct_of_total,
                        }
                        for e in r.by_expiry
                    ],
                    "heatmap": [
                        {
                            "strike": h.strike,
                            "expiry": h.expiry.isoformat(),
                            "dte": h.dte,
                            "net_gex": h.net_gex,
                        }
                        for h in r.heatmap
                    ],
                    "price_profile": [
                        {"spot": p.spot, "net_gex": p.net_gex}
                        for p in r.price_profile
                    ],
                }
                for r in gex_summary.results
            ],
        }

    return {
        "scan_date": scan_date.isoformat(),
        "timestamp": datetime.now().isoformat(),
        "config": {
            "timeframes": config.enabled_timeframes,
            "universe_size": config.universe_size,
            "sfp_timeframes": config.sfp_timeframes if config.sfp_enabled else [],
            "bf_timeframes": config.bf_timeframes if config.bf_enabled else [],
        },
        "summary": {
            "tickers_scanned": len(scan_results),
            "total_signals": sum(len(r.signals) for r in scan_results),
            "total_dominos": sum(1 for r in scan_results if r.domino),
            "bullish_signals": sum(
                1 for r in scan_results
                for s in r.signals if s.direction == "bullish"
            ),
            "bearish_signals": sum(
                1 for r in scan_results
                for s in r.signals if s.direction == "bearish"
            ),
            "neutral_signals": sum(
                1 for r in scan_results
                for s in r.signals if s.direction == "neutral"
            ),
            "total_sfp_signals": len(all_sfp_signals),
            "sfp_formed": sum(1 for s in all_sfp_signals if s.stage == "formed"),
            "sfp_confirmed": sum(1 for s in all_sfp_signals if s.stage == "confirmed"),
            "total_bf_signals": len(all_bf_signals),
            "bf_reclaimed": sum(1 for s in all_bf_signals if s.stage == "reclaimed"),
            "bf_target_hit": sum(1 for s in all_bf_signals if s.stage == "target_hit"),
            "bf_active_long": len(all_bf_active_long),
            "bf_active_short": len(all_bf_active_short),
            "sector_rotation_tracked": (
                len(rotation_result.entries) if rotation_result else 0
            ),
            "sector_rotation_skipped": (
                len(rotation_result.skipped_tickers) if rotation_result else 0
            ),
            "gex_tracked": (
                sum(1 for r in gex_summary.results if r.error is None)
                if gex_summary else 0
            ),
            "gex_failed": (
                sum(1 for r in gex_summary.results if r.error is not None)
                if gex_summary else 0
            ),
        },
        "market": [
            {
                "ticker": ms.ticker,
                "panel": ms.panel_state,
                "ftfc_up": ms.ftfc_up,
                "ftfc_down": ms.ftfc_down,
                "signals": [
                    {
                        "combo": s.combo,
                        "direction": s.direction,
                        "signal_type": s.signal_type,
                    }
                    for s in ms.signals
                ],
            }
            for ms in market_summaries
        ],
        "signals": [
            {
                "ticker": s.ticker,
                "tf": s.tf,
                "direction": s.direction,
                "signal_type": s.signal_type,
                "pattern_tag": s.pattern_tag,
                "combo": s.combo,
                "trigger": s.trigger_level,
                "stop": s.stop_level,
                "mag": s.mag_level,
                "exh": s.exh_level,
                "hammer": s.is_hammer,
                "shooter": s.is_shooter,
                "ftfc": s.ftfc_aligned,
                "in_force": s.in_force,
                "c1_f2": s.c1_was_f2,
            }
            for r in scan_results
            for s in r.signals
        ],
        "dominos": [
            {
                "ticker": r.domino.ticker,
                "count": r.domino.count,
                "tfs": r.domino.inside_tfs,
            }
            for r in scan_results
            if r.domino
        ],
        "market_breadth": market_breadth,
        "panels": all_panels if all_panels is not None else {
            r.ticker: r.panel_state
            for r in scan_results
            if r.panel_state
        },
        "sfp_signals": [
            {
                "ticker": s.ticker,
                "tf": s.tf,
                "direction": s.direction,
                "stage": s.stage,
                "date": s.dt.isoformat(),
                "swing_price": s.swing_price,
                "swing_date": s.swing_date.isoformat(),
                "oppos_price": s.oppos_price,
                "oppos_date": s.oppos_date.isoformat(),
                "sfp_extreme": s.sfp_extreme,
            }
            for s in all_sfp_signals
        ],
        "bf_signals": [
            {
                "ticker": s.ticker,
                "tf": s.tf,
                "direction": s.direction,
                "stage": s.stage,
                "date": s.dt.isoformat(),
                "level": s.level,
                "swing_date": s.swing_date.isoformat(),
                "target": s.target,
                "target_date": s.target_date.isoformat(),
            }
            for s in all_bf_signals
        ],
        "bf_active": {
            "long": [
                {
                    "ticker": s.ticker,
                    "tf": s.tf,
                    "level": s.level,
                    "swing_date": s.swing_date.isoformat(),
                    "target": s.target,
                    "target_date": s.target_date.isoformat(),
                    "magnitude": s.magnitude,
                    "expected_move": s.expected_move,
                    "risk": s.risk,
                }
                for s in all_bf_active_long
            ],
            "short": [
                {
                    "ticker": s.ticker,
                    "tf": s.tf,
                    "level": s.level,
                    "swing_date": s.swing_date.isoformat(),
                    "target": s.target,
                    "target_date": s.target_date.isoformat(),
                    "magnitude": s.magnitude,
                    "expected_move": s.expected_move,
                    "risk": s.risk,
                }
                for s in all_bf_active_short
            ],
        },
        "sector_rotation": rotation_json,
        "gex": gex_json,
        "etf_holdings": etf_holdings,
    }


def save_results(results_dict: dict, output_dir: Path) -> Path:
    """Save results to a JSON file with date-stamped name."""
    output_dir.mkdir(parents=True, exist_ok=True)
    scan_date = results_dict["scan_date"]
    path = output_dir / f"scan_{scan_date}.json"
    with open(path, "w") as f:
        json.dump(results_dict, f, indent=2, default=str)
    return path


# =========================================================================
# MAIN SCAN LOOP
# =========================================================================

def run_scan(
    config: Optional[ScannerConfig] = None,
    scan_date: Optional[date] = None,
    output_dir: Optional[Path] = None,
    skip_email: bool = False,
    verbose: bool = False,
) -> dict:
    """Run the complete scanner pipeline.

    Args:
        config: scanner config (defaults to load_config())
        scan_date: date to scan as-of (defaults to today)
        output_dir: where to save JSON results (defaults to ./output)
        skip_email: if True, don't send email alerts
        verbose: enable debug logging

    Returns:
        The serialized results dict.
    """
    setup_logging(verbose)
    logger = logging.getLogger("scanner")

    if config is None:
        config = load_config()
    if scan_date is None:
        scan_date = date.today()
    if output_dir is None:
        output_dir = Path("./output")

    start_time = time.time()

    # -----------------------------------------------------------------
    # Phase 1: Fetch data
    # -----------------------------------------------------------------
    logger.info("=" * 60)
    logger.info("STRAT Scanner starting")
    logger.info(f"Scan date: {scan_date}")
    logger.info(f"Timeframes: {config.enabled_timeframes}")
    logger.info("=" * 60)

    logger.info("Phase 1: Fetching market data...")
    dm = DataManager(config)
    dm.fetch_history(as_of=scan_date)

    # -----------------------------------------------------------------
    # Phase 2: Derive universe
    # -----------------------------------------------------------------
    logger.info("Phase 2: Deriving universe...")
    universe = dm.get_universe()
    logger.info(f"Universe: {len(universe)} tickers")

    # -----------------------------------------------------------------
    # Phase 2b: Backfill recent gaps via yfinance
    # -----------------------------------------------------------------
    # Polygon free tier has a 1-business-day delay — today's bars return
    # 403. Use yfinance (no delay, already a dependency) to fill the gap
    # for just the universe tickers.
    yf_filled = dm.backfill_recent_yfinance(universe)
    if yf_filled:
        logger.info(f"Backfilled {yf_filled} recent date(s) via yfinance")

    # -----------------------------------------------------------------
    # Phase 3: Scan all tickers
    # -----------------------------------------------------------------
    logger.info("Phase 3: Scanning tickers...")
    scan_results: list[TickerScanResult] = []
    sfp_results: list[TickerSFPResult] = []
    bf_results: list[TickerMagnitudeResult] = []
    etf_results: dict[str, TickerScanResult] = {}
    all_panels: dict[str, dict] = {}
    above_open = 0
    below_open = 0
    errors = 0

    all_etfs = set(config.market_etfs + config.sector_etfs)

    for i, ticker in enumerate(universe):
        try:
            daily_bars = dm.get_ticker_bars(ticker)
            if len(daily_bars) < 4:
                continue

            result = scan_ticker(ticker, daily_bars, config)

            # Collect panel + price for ALL tickers (drill-down coverage)
            if result.panel_state:
                panel_entry = dict(result.panel_state)
                if len(daily_bars) >= 2:
                    panel_entry["lc"] = round(daily_bars[-1].close, 2)
                    prev_close = daily_bars[-2].close
                    if prev_close > 0:
                        panel_entry["chg"] = round(
                            (daily_bars[-1].close - prev_close)
                            / prev_close * 100, 2
                        )
                elif daily_bars:
                    panel_entry["lc"] = round(daily_bars[-1].close, 2)
                all_panels[ticker] = panel_entry

            # Breadth: above/below today's open
            if daily_bars:
                if daily_bars[-1].close >= daily_bars[-1].open:
                    above_open += 1
                else:
                    below_open += 1

            # Separate ETFs from stocks for market summary
            if ticker in all_etfs:
                etf_results[ticker] = result

            # Only include results with signals or dominos
            if result.signals or result.domino:
                scan_results.append(result)

            # Swing Failure Pattern (separate detector, same bar data)
            if config.sfp_enabled:
                sfp_result = scan_ticker_sfp(ticker, daily_bars, config)
                if sfp_result.signals:
                    sfp_results.append(sfp_result)

            # Broadening Formation Reclaim (separate detector, same bar data)
            if config.bf_enabled:
                bf_result = scan_ticker_bf(ticker, daily_bars, config)
                if bf_result.signals or bf_result.active_long or bf_result.active_short:
                    bf_results.append(bf_result)

        except Exception as e:
            logger.warning(f"Error scanning {ticker}: {e}")
            errors += 1

        if (i + 1) % 100 == 0:
            logger.info(f"  Scanned {i + 1}/{len(universe)} tickers")

    total_sfp_signals = sum(len(r.signals) for r in sfp_results)
    total_bf_signals = sum(len(r.signals) for r in bf_results)
    total_bf_active = sum(
        len(r.active_long) + len(r.active_short) for r in bf_results
    )
    logger.info(
        f"Scan complete: {len(scan_results)} tickers with STRAT signals, "
        f"{len(sfp_results)} tickers with SFP signals ({total_sfp_signals} total), "
        f"{len(bf_results)} tickers with BF activity "
        f"({total_bf_signals} fresh signals, {total_bf_active} active setups), "
        f"{errors} errors"
    )

    # -----------------------------------------------------------------
    # Phase 4: Market/sector summary
    # -----------------------------------------------------------------
    logger.info("Phase 4: Computing market summary...")
    market_summaries = compute_market_summary(etf_results)

    # -----------------------------------------------------------------
    # Phase 4b: Market breadth + VIX
    # -----------------------------------------------------------------
    breadth_total = above_open + below_open
    pct_above = (
        round((above_open / breadth_total) * 100, 1)
        if breadth_total > 0
        else 50.0
    )

    vix_data = None
    try:
        import yfinance as yf

        vix_hist = yf.Ticker("^VIX").history(period="5d")
        if len(vix_hist) >= 1:
            vix_last = vix_hist.iloc[-1]
            vix_data = {"value": round(float(vix_last["Close"]), 2)}
            if len(vix_hist) >= 2:
                vix_prev = vix_hist.iloc[-2]
                vc = float(vix_last["Close"] - vix_prev["Close"])
                vix_data["change"] = round(vc, 2)
                if float(vix_prev["Close"]) > 0:
                    vix_data["change_pct"] = round(
                        vc / float(vix_prev["Close"]) * 100, 1
                    )
            logger.info(f"VIX: {vix_data}")
    except Exception as e:
        logger.warning(f"VIX fetch failed: {e}")

    market_breadth = {
        "above_open": above_open,
        "below_open": below_open,
        "total": breadth_total,
        "pct_above": pct_above,
        "vix": vix_data,
    }

    # -----------------------------------------------------------------
    # Phase 4c: ETF holdings (auto-updated weekly via yfinance)
    # -----------------------------------------------------------------
    etf_holdings_dict: Optional[dict] = None
    try:
        from holdings import HoldingsManager
        from sector_universe import SECTORS, SUBSECTORS, THEMATIC

        holdings_etfs = [sg.ticker for sg in SECTORS + SUBSECTORS + THEMATIC]
        cache_root = Path(os.environ.get("SCANNER_CACHE_DIR", "./cache"))
        holdings_mgr = HoldingsManager(cache_root)
        etf_holdings_dict = holdings_mgr.get_holdings(holdings_etfs)
        logger.info(
            f"Phase 4c: ETF holdings loaded for "
            f"{len(etf_holdings_dict)} ETFs"
        )
    except Exception as e:
        logger.warning(f"Phase 4c: ETF holdings fetch failed: {e}")

    # -----------------------------------------------------------------
    # Phase 5: Sector / subsector / thematic rotation ranking
    # -----------------------------------------------------------------
    rotation_result: Optional[RotationResult] = None
    if config.sector_rotation_enabled:
        logger.info("Phase 5: Computing sector rotation ranking...")
        rotation_universe = get_sector_universe(config)
        rotation_bars: dict[str, list] = {}
        for group in rotation_universe:
            bars = dm.get_ticker_bars(group.ticker)
            if bars:
                rotation_bars[group.ticker] = bars
        rotation_result = compute_sector_rotation(rotation_bars, config)
        logger.info(
            f"  Ranked {len(rotation_result.entries)}/{len(rotation_universe)} "
            f"tracked ETFs across periods {rotation_result.periods}"
        )
        if rotation_result.skipped_tickers:
            logger.warning(
                f"  No data for {len(rotation_result.skipped_tickers)} tracked "
                f"tickers (verify against Polygon — may be delisted/renamed): "
                f"{', '.join(rotation_result.skipped_tickers)}"
            )
    else:
        logger.info("Phase 5: Sector rotation disabled, skipping")

    # -----------------------------------------------------------------
    # Phase 5.5: Gamma Exposure (GEX) — separate options-data source
    # (Yahoo Finance, unofficial), scoped to config.gex_tickers only.
    # See gex.py module docstring for reliability/ToS caveats.
    # -----------------------------------------------------------------
    gex_summary: Optional[GexSummary] = None
    if config.gex_enabled:
        logger.info("Phase 5.5: Computing gamma exposure (GEX)...")
        spot_by_ticker: dict[str, float] = {}
        for ticker in config.gex_tickers:
            bars = dm.get_ticker_bars(ticker)
            if bars:
                spot_by_ticker[ticker] = bars[-1].close
        gex_summary = compute_gex_summary(config, spot_by_ticker, scan_date)
        ok = [r for r in gex_summary.results if r.error is None]
        logger.info(
            f"  GEX computed for {len(ok)}/{len(config.gex_tickers)} tickers"
            + (f", failed: {', '.join(gex_summary.skipped_tickers)}"
               if gex_summary.skipped_tickers else "")
        )
    else:
        logger.info("Phase 5.5: GEX disabled, skipping")

    # -----------------------------------------------------------------
    # Phase 6: Print summary
    # -----------------------------------------------------------------
    total_signals = sum(len(r.signals) for r in scan_results)
    total_dominos = sum(1 for r in scan_results if r.domino)

    logger.info("-" * 60)
    logger.info(f"RESULTS: {total_signals} signals, {total_dominos} dominos")
    logger.info("-" * 60)

    # Print market summary
    for ms in market_summaries:
        panel = format_panel(ms.panel_state, config.enabled_timeframes)
        ftfc = (
            "FTFC ▲" if ms.ftfc_up
            else "FTFC ▼" if ms.ftfc_down
            else ""
        )
        logger.info(f"  {ms.ticker:6s} {panel}  {ftfc}")

    # Print top signals
    all_signals = [s for r in scan_results for s in r.signals]
    # Sort: FTFC-aligned first, then by ticker
    all_signals.sort(key=lambda s: (not s.ftfc_aligned, s.ticker))

    for sig in all_signals[:20]:  # top 20 in console
        logger.info(f"  {format_signal_line(sig)}")

    if total_signals > 20:
        logger.info(f"  ... and {total_signals - 20} more signals")

    # Print dominos
    for r in scan_results:
        if r.domino:
            logger.info(f"  {format_domino_line(r.domino)}")

    # Print SFP signals
    all_sfp_signals = [s for r in sfp_results for s in r.signals]
    if all_sfp_signals:
        logger.info("-" * 60)
        logger.info(f"SFP SIGNALS: {len(all_sfp_signals)}")
        for sig in all_sfp_signals[:20]:
            logger.info(f"  {format_sfp_line(sig)}")
        if len(all_sfp_signals) > 20:
            logger.info(f"  ... and {len(all_sfp_signals) - 20} more SFP signals")

    # Print BF (broadening formation reclaim) signals + top active setups
    all_bf_signals = [s for r in bf_results for s in r.signals]
    all_bf_active_long = [s for r in bf_results for s in r.active_long]
    all_bf_active_short = [s for r in bf_results for s in r.active_short]
    if all_bf_signals:
        logger.info("-" * 60)
        logger.info(f"BF SIGNALS: {len(all_bf_signals)}")
        for sig in all_bf_signals[:20]:
            logger.info(f"  {format_bf_signal_line(sig)}")
        if len(all_bf_signals) > 20:
            logger.info(f"  ... and {len(all_bf_signals) - 20} more BF signals")
    if all_bf_active_long or all_bf_active_short:
        all_bf_active_long.sort(key=lambda s: s.magnitude, reverse=True)
        all_bf_active_short.sort(key=lambda s: s.magnitude, reverse=True)
        logger.info(f"BF ACTIVE SETUPS: {len(all_bf_active_long)} long, {len(all_bf_active_short)} short")
        for setup in (all_bf_active_long + all_bf_active_short)[:20]:
            logger.info(f"  {format_bf_active_line(setup)}")

    # Print sector rotation leaderboards (context only — never an email trigger)
    if rotation_result and rotation_result.entries:
        logger.info("-" * 60)
        logger.info(f"SECTOR ROTATION ({len(rotation_result.entries)} tracked)")
        for period in rotation_result.periods:
            label = format_period_label(period)
            top = leaders(rotation_result, period, config.sector_rotation_leaders_count)
            bottom = laggards(rotation_result, period, config.sector_rotation_laggards_count)
            logger.info(f"  --- {label} Leaders ---")
            for entry in top:
                logger.info(f"    {format_rotation_line(entry, period)}")
            logger.info(f"  --- {label} Laggards ---")
            for entry in bottom:
                logger.info(f"    {format_rotation_line(entry, period)}")

    # Print GEX (context only — never an email trigger, same rule as
    # sector rotation and BF's active-setup list)
    if gex_summary and gex_summary.results:
        logger.info("-" * 60)
        logger.info(f"GAMMA EXPOSURE ({len(gex_summary.results)} tickers)")
        for r in gex_summary.results:
            if r.error:
                logger.info(f"  {r.ticker}: error — {r.error}")
                continue
            zero_gamma_str = f"{r.zero_gamma:.2f}" if r.zero_gamma is not None else "n/a"
            logger.info(
                f"  {r.ticker:6s} spot={r.spot:.2f}  net_gex=${r.net_gex/1e6:,.0f}M  "
                f"zero_gamma={zero_gamma_str}  "
                f"call_wall={r.call_wall}  put_wall={r.put_wall}"
            )

    # -----------------------------------------------------------------
    # Phase 7: Save results
    # -----------------------------------------------------------------
    logger.info("Phase 7: Saving results...")
    results_dict = serialize_results(
        scan_results, market_summaries, config, scan_date,
        sfp_results, bf_results, rotation_result, gex_summary,
        all_panels=all_panels,
        market_breadth=market_breadth,
        etf_holdings=etf_holdings_dict,
    )
    results_path = save_results(results_dict, output_dir)
    logger.info(f"Results saved to {results_path}")

    # -----------------------------------------------------------------
    # Phase 8: Send alerts
    # -----------------------------------------------------------------
    # NOTE: sector rotation never independently triggers an email — it is
    # informational context included whenever an email is already going
    # out for another reason (same alert-fatigue rule applied to BF's
    # active-setup list: "ongoing state" alone never fires a send).
    if not skip_email and (
        total_signals > 0 or total_dominos > 0 or all_sfp_signals or all_bf_signals
    ):
        logger.info("Phase 8: Sending email alert...")
        success = send_alert_email(
            config, scan_results, market_summaries, scan_date,
            sfp_results, bf_results, rotation_result,
        )
        if success:
            logger.info("Email alert sent successfully")
        else:
            logger.warning("Email alert failed or not configured")
    else:
        logger.info("Phase 8: Skipping email (no signals or --skip-email)")

    # -----------------------------------------------------------------
    # Cleanup
    # -----------------------------------------------------------------
    dm.clear_old_cache()

    elapsed = time.time() - start_time
    logger.info(f"Scanner finished in {elapsed:.1f}s")

    return results_dict


# =========================================================================
# CLI ENTRY POINT
# =========================================================================

def main():
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description="STRAT Scanner — TheStrat v3.1.1 stock scanner"
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="Scan date (YYYY-MM-DD, default: today)",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="./output",
        help="Directory for result JSON files",
    )
    parser.add_argument(
        "--skip-email",
        action="store_true",
        help="Don't send email alerts",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--timeframes",
        type=str,
        default=None,
        help="Comma-separated timeframes (e.g. M,W,D,2D,3D)",
    )

    args = parser.parse_args()

    config = load_config()

    scan_date = None
    if args.date:
        scan_date = date.fromisoformat(args.date)

    if args.timeframes:
        config.enabled_timeframes = [
            t.strip() for t in args.timeframes.split(",")
        ]

    run_scan(
        config=config,
        scan_date=scan_date,
        output_dir=Path(args.output_dir),
        skip_email=args.skip_email,
        verbose=args.verbose,
    )


if __name__ == "__main__":
    main()
