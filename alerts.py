"""
Alert System — Gmail SMTP email alerts.

Sends formatted STRAT scan results via email using Gmail App Password.
Supports plain text and HTML-formatted alerts.

Environment Variables
---------------------
  SMTP_EMAIL     — Gmail address to send from
  SMTP_PASSWORD  — Gmail App Password (NOT your regular password)
"""
from __future__ import annotations

import logging
import os
import smtplib
from datetime import date, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from config import ScannerConfig
from signals import (
    Signal,
    DominoSetup,
    TickerScanResult,
    MarketSummary,
    format_signal_line,
    format_domino_line,
    format_panel,
)
from sfp import (
    SFPSignal,
    TickerSFPResult,
    format_sfp_line,
)
from broadening_formation import (
    MagnitudeSignal,
    ActiveSetup,
    TickerMagnitudeResult,
    format_bf_signal_line,
    format_bf_active_line,
)
from sector_rotation import (
    RotationResult,
    leaders,
    laggards,
    format_rotation_line,
    format_period_label,
)

logger = logging.getLogger(__name__)


# =========================================================================
# EMAIL CONSTRUCTION
# =========================================================================

def build_alert_subject(
    signal_count: int,
    domino_count: int,
    scan_date: Optional[date] = None,
    sfp_count: int = 0,
    bf_count: int = 0,
) -> str:
    """Build the email subject line."""
    if scan_date is None:
        scan_date = date.today()

    parts = []
    if signal_count > 0:
        parts.append(f"{signal_count} Signal{'s' if signal_count != 1 else ''}")
    if domino_count > 0:
        parts.append(f"{domino_count} Domino{'s' if domino_count != 1 else ''}")
    if sfp_count > 0:
        parts.append(f"{sfp_count} SFP{'s' if sfp_count != 1 else ''}")
    if bf_count > 0:
        parts.append(f"{bf_count} BF{'s' if bf_count != 1 else ''}")

    summary = " + ".join(parts) if parts else "No Signals"
    return f"STRAT Scanner — {summary} — {scan_date.strftime('%b %d, %Y')}"


def build_alert_body_text(
    scan_results: list[TickerScanResult],
    market_summaries: list[MarketSummary],
    config: ScannerConfig,
    sfp_results: Optional[list[TickerSFPResult]] = None,
    bf_results: Optional[list[TickerMagnitudeResult]] = None,
    rotation_result: Optional[RotationResult] = None,
) -> str:
    """Build plain-text alert body."""
    sfp_results = sfp_results or []
    bf_results = bf_results or []
    lines: list[str] = []
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines.append(f"STRAT Scanner Report — {now}")
    lines.append("=" * 60)

    # --- Market Summary ---
    if market_summaries:
        lines.append("")
        lines.append("MARKET OVERVIEW")
        lines.append("-" * 40)
        for ms in market_summaries:
            panel_str = format_panel(ms.panel_state)
            ftfc = (
                "FTFC ▲" if ms.ftfc_up
                else "FTFC ▼" if ms.ftfc_down
                else "No FTFC"
            )
            lines.append(f"  {ms.ticker:6s} {panel_str}  [{ftfc}]")
            for sig in ms.signals:
                lines.append(f"    → {format_signal_line(sig)}")
        lines.append("")

    # --- Collect all signals and dominos ---
    all_signals: list[Signal] = []
    all_dominos: list[DominoSetup] = []
    for result in scan_results:
        all_signals.extend(result.signals)
        if result.domino:
            all_dominos.append(result.domino)

    # --- Domino Setups ---
    if all_dominos:
        lines.append("DOMINO SETUPS")
        lines.append("-" * 40)
        for domino in all_dominos:
            lines.append(f"  {format_domino_line(domino)}")
        lines.append("")

    # --- Signals by Direction ---
    bullish = [s for s in all_signals if s.direction == "bullish"]
    bearish = [s for s in all_signals if s.direction == "bearish"]

    if bullish:
        lines.append(f"BULLISH SIGNALS ({len(bullish)})")
        lines.append("-" * 40)

        # Group by signal type
        by_type: dict[str, list[Signal]] = {}
        for sig in bullish:
            by_type.setdefault(sig.signal_type, []).append(sig)
        for stype, sigs in by_type.items():
            for sig in sorted(sigs, key=lambda s: s.ticker):
                lines.append(f"  {format_signal_line(sig)}")
        lines.append("")

    if bearish:
        lines.append(f"BEARISH SIGNALS ({len(bearish)})")
        lines.append("-" * 40)
        by_type = {}
        for sig in bearish:
            by_type.setdefault(sig.signal_type, []).append(sig)
        for stype, sigs in by_type.items():
            for sig in sorted(sigs, key=lambda s: s.ticker):
                lines.append(f"  {format_signal_line(sig)}")
        lines.append("")

    # --- Swing Failure Patterns ---
    all_sfp_signals: list[SFPSignal] = []
    for r in sfp_results:
        all_sfp_signals.extend(r.signals)

    if all_sfp_signals:
        formed = [s for s in all_sfp_signals if s.stage == "formed"]
        confirmed = [s for s in all_sfp_signals if s.stage == "confirmed"]

        lines.append(f"SWING FAILURE PATTERNS ({len(all_sfp_signals)})")
        lines.append("-" * 40)
        if confirmed:
            lines.append(f"  Confirmed ({len(confirmed)}):")
            for sig in sorted(confirmed, key=lambda s: s.ticker):
                lines.append(f"    {format_sfp_line(sig)}")
        if formed:
            lines.append(f"  Formed / watching ({len(formed)}):")
            for sig in sorted(formed, key=lambda s: s.ticker):
                lines.append(f"    {format_sfp_line(sig)}")
        lines.append("")

    # --- Broadening Formation Reclaim ---
    all_bf_signals: list[MagnitudeSignal] = []
    all_bf_active_long: list[ActiveSetup] = []
    all_bf_active_short: list[ActiveSetup] = []
    for r in bf_results:
        all_bf_signals.extend(r.signals)
        all_bf_active_long.extend(r.active_long)
        all_bf_active_short.extend(r.active_short)

    if all_bf_signals:
        reclaimed = [s for s in all_bf_signals if s.stage == "reclaimed"]
        target_hit = [s for s in all_bf_signals if s.stage == "target_hit"]

        lines.append(f"BROADENING FORMATION RECLAIM ({len(all_bf_signals)})")
        lines.append("-" * 40)
        if target_hit:
            lines.append(f"  Target hit ({len(target_hit)}):")
            for sig in sorted(target_hit, key=lambda s: s.ticker):
                lines.append(f"    {format_bf_signal_line(sig)}")
        if reclaimed:
            lines.append(f"  Reclaimed today ({len(reclaimed)}):")
            for sig in sorted(reclaimed, key=lambda s: s.ticker):
                lines.append(f"    {format_bf_signal_line(sig)}")
        lines.append("")

    if all_bf_active_long or all_bf_active_short:
        all_bf_active_long.sort(key=lambda s: s.magnitude, reverse=True)
        all_bf_active_short.sort(key=lambda s: s.magnitude, reverse=True)
        lines.append(
            f"BF ACTIVE SETUPS ({len(all_bf_active_long)} long, "
            f"{len(all_bf_active_short)} short)"
        )
        lines.append("-" * 40)
        for setup in all_bf_active_long[:15]:
            lines.append(f"  {format_bf_active_line(setup)}")
        for setup in all_bf_active_short[:15]:
            lines.append(f"  {format_bf_active_line(setup)}")
        lines.append("")

    if (not all_signals and not all_dominos and not all_sfp_signals
            and not all_bf_signals and not all_bf_active_long and not all_bf_active_short):
        lines.append("No signals detected.")

    # --- Sector / Subsector / Thematic Rotation (context, not a trigger) ---
    if rotation_result and rotation_result.entries:
        lines.append(
            f"SECTOR ROTATION ({len(rotation_result.entries)} tracked "
            f"as of {rotation_result.as_of_date})"
        )
        lines.append("-" * 40)
        for period in rotation_result.periods:
            label = format_period_label(period)
            top = leaders(rotation_result, period, config.sector_rotation_leaders_count)
            bottom = laggards(rotation_result, period, config.sector_rotation_laggards_count)
            lines.append(f"  {label} Leaders:")
            for entry in top:
                lines.append(f"    {format_rotation_line(entry, period)}")
            lines.append(f"  {label} Laggards:")
            for entry in bottom:
                lines.append(f"    {format_rotation_line(entry, period)}")
        if rotation_result.skipped_tickers:
            lines.append(
                f"  ({len(rotation_result.skipped_tickers)} tracked tickers had no data)"
            )
        lines.append("")

    lines.append("")
    lines.append(
        f"Total: {len(all_signals)} STRAT signals, {len(all_dominos)} dominos, "
        f"{len(all_sfp_signals)} SFP signals, {len(all_bf_signals)} BF signals "
        f"({len(all_bf_active_long) + len(all_bf_active_short)} active setups)"
    )
    lines.append(f"Universe: {len(scan_results)} tickers scanned")
    lines.append(f"Timeframes: {', '.join(config.enabled_timeframes)}")
    if config.sfp_enabled:
        lines.append(f"SFP timeframes: {', '.join(config.sfp_timeframes)}")
    if config.bf_enabled:
        lines.append(f"BF timeframes: {', '.join(config.bf_timeframes)}")

    return "\n".join(lines)


def build_alert_body_html(
    scan_results: list[TickerScanResult],
    market_summaries: list[MarketSummary],
    config: ScannerConfig,
    sfp_results: Optional[list[TickerSFPResult]] = None,
    bf_results: Optional[list[TickerMagnitudeResult]] = None,
    rotation_result: Optional[RotationResult] = None,
) -> str:
    """Build HTML-formatted alert body."""
    sfp_results = sfp_results or []
    bf_results = bf_results or []
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    all_signals: list[Signal] = []
    all_dominos: list[DominoSetup] = []
    for result in scan_results:
        all_signals.extend(result.signals)
        if result.domino:
            all_dominos.append(result.domino)

    all_sfp_signals: list[SFPSignal] = []
    for r in sfp_results:
        all_sfp_signals.extend(r.signals)

    all_bf_signals: list[MagnitudeSignal] = []
    all_bf_active_long: list[ActiveSetup] = []
    all_bf_active_short: list[ActiveSetup] = []
    for r in bf_results:
        all_bf_signals.extend(r.signals)
        all_bf_active_long.extend(r.active_long)
        all_bf_active_short.extend(r.active_short)
    all_bf_active_long.sort(key=lambda s: s.magnitude, reverse=True)
    all_bf_active_short.sort(key=lambda s: s.magnitude, reverse=True)

    bullish = [s for s in all_signals if s.direction == "bullish"]
    bearish = [s for s in all_signals if s.direction == "bearish"]

    html_parts = [
        "<html><body style='font-family: monospace; font-size: 14px;'>",
        f"<h2>STRAT Scanner Report — {now}</h2>",
    ]

    # Market summary
    if market_summaries:
        html_parts.append("<h3>Market Overview</h3>")
        html_parts.append(
            "<table border='1' cellpadding='4' cellspacing='0' "
            "style='border-collapse: collapse;'>"
        )
        html_parts.append(
            "<tr style='background: #333; color: white;'>"
            "<th>Ticker</th><th>Panel</th><th>FTFC</th><th>Signals</th></tr>"
        )
        for ms in market_summaries:
            panel = format_panel(ms.panel_state)
            ftfc = (
                "<span style='color: green;'>▲ Up</span>" if ms.ftfc_up
                else "<span style='color: red;'>▼ Down</span>" if ms.ftfc_down
                else "—"
            )
            sig_text = ", ".join(
                f"{s.combo} {s.direction[0].upper()}" for s in ms.signals
            ) or "—"
            html_parts.append(
                f"<tr><td><b>{ms.ticker}</b></td>"
                f"<td>{panel}</td><td>{ftfc}</td><td>{sig_text}</td></tr>"
            )
        html_parts.append("</table><br>")

    # Dominos
    if all_dominos:
        html_parts.append("<h3>🎯 Domino Setups</h3><ul>")
        for domino in all_dominos:
            tfs = "/".join(domino.inside_tfs)
            html_parts.append(
                f"<li><b>{domino.ticker}</b>: {domino.count} TFs inside ({tfs})</li>"
            )
        html_parts.append("</ul>")

    # Signals table
    for label, signals, color in [
        ("Bullish", bullish, "#006400"),
        ("Bearish", bearish, "#8B0000"),
    ]:
        if not signals:
            continue
        html_parts.append(f"<h3 style='color: {color};'>{label} Signals ({len(signals)})</h3>")
        html_parts.append(
            "<table border='1' cellpadding='4' cellspacing='0' "
            "style='border-collapse: collapse;'>"
        )
        html_parts.append(
            "<tr style='background: #333; color: white;'>"
            "<th>Ticker</th><th>TF</th><th>Combo</th><th>Type</th>"
            "<th>Trigger</th><th>Stop</th><th>Mag</th><th>Exh</th>"
            "<th>Flags</th></tr>"
        )
        for sig in sorted(signals, key=lambda s: s.ticker):
            flags = []
            if sig.is_hammer:
                flags.append("H")
            if sig.is_shooter:
                flags.append("S")
            if sig.ftfc_aligned:
                flags.append("FTFC")
            if sig.in_force:
                flags.append("IF")
            if sig.c1_was_f2:
                flags.append("F2")

            arrow = "▲" if sig.direction == "bullish" else "▼"
            trigger_str = f"{sig.trigger_level:.2f}" if sig.trigger_level is not None else "—"
            stop_str = f"{sig.stop_level:.2f}" if sig.stop_level is not None else "—"
            mag_str = f"{sig.mag_level:.2f}" if sig.mag_level is not None else "—"
            exh_str = f"{sig.exh_level:.2f}" if sig.exh_level is not None else "—"
            html_parts.append(
                f"<tr>"
                f"<td><b>{sig.ticker}</b></td>"
                f"<td>{sig.tf}</td>"
                f"<td>{arrow} {sig.combo}</td>"
                f"<td>{sig.signal_type.replace('_', ' ').title()}</td>"
                f"<td>{trigger_str}</td>"
                f"<td>{stop_str}</td>"
                f"<td>{mag_str}</td>"
                f"<td>{exh_str}</td>"
                f"<td>{', '.join(flags) or '—'}</td>"
                f"</tr>"
            )
        html_parts.append("</table><br>")

    # Swing Failure Patterns
    if all_sfp_signals:
        html_parts.append(f"<h3 style='color: #b8860b;'>Swing Failure Patterns ({len(all_sfp_signals)})</h3>")
        html_parts.append(
            "<table border='1' cellpadding='4' cellspacing='0' "
            "style='border-collapse: collapse;'>"
        )
        html_parts.append(
            "<tr style='background: #333; color: white;'>"
            "<th>Ticker</th><th>TF</th><th>Dir</th><th>Stage</th>"
            "<th>Swept Level</th><th>Swept On</th>"
            "<th>Confirm Level</th></tr>"
        )
        for sig in sorted(all_sfp_signals, key=lambda s: (s.stage != "confirmed", s.ticker)):
            arrow = "▼" if sig.direction == "bearish" else "▲"
            stage_label = "CONFIRMED" if sig.stage == "confirmed" else "Formed"
            html_parts.append(
                f"<tr>"
                f"<td><b>{sig.ticker}</b></td>"
                f"<td>{sig.tf}</td>"
                f"<td>{arrow}</td>"
                f"<td>{stage_label}</td>"
                f"<td>{sig.swing_price:.2f}</td>"
                f"<td>{sig.swing_date}</td>"
                f"<td>{sig.oppos_price:.2f}</td>"
                f"</tr>"
            )
        html_parts.append("</table><br>")

    # Broadening Formation Reclaim — fresh signals
    if all_bf_signals:
        html_parts.append(f"<h3 style='color: #4b0082;'>Broadening Formation Reclaim ({len(all_bf_signals)})</h3>")
        html_parts.append(
            "<table border='1' cellpadding='4' cellspacing='0' "
            "style='border-collapse: collapse;'>"
        )
        html_parts.append(
            "<tr style='background: #333; color: white;'>"
            "<th>Ticker</th><th>TF</th><th>Dir</th><th>Stage</th>"
            "<th>Level</th><th>Level Set</th><th>Target</th></tr>"
        )
        for sig in sorted(all_bf_signals, key=lambda s: (s.stage != "target_hit", s.ticker)):
            arrow = "▲" if sig.direction == "long" else "▼"
            stage_label = "TARGET HIT" if sig.stage == "target_hit" else "Reclaimed"
            html_parts.append(
                f"<tr>"
                f"<td><b>{sig.ticker}</b></td>"
                f"<td>{sig.tf}</td>"
                f"<td>{arrow}</td>"
                f"<td>{stage_label}</td>"
                f"<td>{sig.level:.2f}</td>"
                f"<td>{sig.swing_date}</td>"
                f"<td>{sig.target:.2f}</td>"
                f"</tr>"
            )
        html_parts.append("</table><br>")

    # Broadening Formation Reclaim — active setups (context, not new events)
    if all_bf_active_long or all_bf_active_short:
        html_parts.append(
            f"<h3 style='color: #4b0082;'>BF Active Setups "
            f"({len(all_bf_active_long)} long, {len(all_bf_active_short)} short)</h3>"
        )
        html_parts.append(
            "<table border='1' cellpadding='4' cellspacing='0' "
            "style='border-collapse: collapse;'>"
        )
        html_parts.append(
            "<tr style='background: #333; color: white;'>"
            "<th>Ticker</th><th>TF</th><th>Dir</th><th>Level</th>"
            "<th>Target</th><th>Magnitude</th><th>Move Left</th><th>Risk</th></tr>"
        )
        for setup, direction_arrow in (
            [(s, "▲") for s in all_bf_active_long[:15]]
            + [(s, "▼") for s in all_bf_active_short[:15]]
        ):
            html_parts.append(
                f"<tr>"
                f"<td><b>{setup.ticker}</b></td>"
                f"<td>{setup.tf}</td>"
                f"<td>{direction_arrow}</td>"
                f"<td>{setup.level:.2f}</td>"
                f"<td>{setup.target:.2f}</td>"
                f"<td>{setup.magnitude:.2f}</td>"
                f"<td>{setup.expected_move:.2f}</td>"
                f"<td>{setup.risk:.2f}</td>"
                f"</tr>"
            )
        html_parts.append("</table><br>")

    # Sector / Subsector / Thematic Rotation (context, not a trigger)
    if rotation_result and rotation_result.entries:
        html_parts.append(
            f"<h3 style='color: #1a5276;'>Sector Rotation "
            f"({len(rotation_result.entries)} tracked as of {rotation_result.as_of_date})</h3>"
        )
        for period in rotation_result.periods:
            label = format_period_label(period)
            top = leaders(rotation_result, period, config.sector_rotation_leaders_count)
            bottom = laggards(rotation_result, period, config.sector_rotation_laggards_count)
            html_parts.append(f"<h4>{label}</h4>")
            html_parts.append(
                "<table border='1' cellpadding='4' cellspacing='0' "
                "style='border-collapse: collapse; display: inline-block; "
                "vertical-align: top; margin-right: 12px;'>"
            )
            html_parts.append(
                "<tr style='background: #333; color: white;'>"
                "<th colspan='5'>Leaders</th></tr>"
                "<tr style='background: #eee;'>"
                "<th>Rank</th><th>Ticker</th><th>Name</th>"
                "<th>Return</th><th>Composite</th></tr>"
            )
            for entry in top:
                ret = entry.returns.get(period)
                ret_str = f"{ret:+.2f}%" if ret is not None else "n/a"
                comp_str = f"{entry.composite_rank:.1f}" if entry.composite_rank is not None else "n/a"
                html_parts.append(
                    f"<tr><td>#{entry.ranks.get(period, '—')}</td>"
                    f"<td><b>{entry.ticker}</b></td><td>{entry.label}</td>"
                    f"<td style='color: green;'>{ret_str}</td><td>{comp_str}</td></tr>"
                )
            html_parts.append("</table>")
            html_parts.append(
                "<table border='1' cellpadding='4' cellspacing='0' "
                "style='border-collapse: collapse; display: inline-block; "
                "vertical-align: top;'>"
            )
            html_parts.append(
                "<tr style='background: #333; color: white;'>"
                "<th colspan='5'>Laggards</th></tr>"
                "<tr style='background: #eee;'>"
                "<th>Rank</th><th>Ticker</th><th>Name</th>"
                "<th>Return</th><th>Composite</th></tr>"
            )
            for entry in bottom:
                ret = entry.returns.get(period)
                ret_str = f"{ret:+.2f}%" if ret is not None else "n/a"
                comp_str = f"{entry.composite_rank:.1f}" if entry.composite_rank is not None else "n/a"
                html_parts.append(
                    f"<tr><td>#{entry.ranks.get(period, '—')}</td>"
                    f"<td><b>{entry.ticker}</b></td><td>{entry.label}</td>"
                    f"<td style='color: #8B0000;'>{ret_str}</td><td>{comp_str}</td></tr>"
                )
            html_parts.append("</table><br><br>")
        if rotation_result.skipped_tickers:
            html_parts.append(
                f"<p style='color: #999; font-size: 12px;'>"
                f"{len(rotation_result.skipped_tickers)} tracked tickers had no data.</p>"
            )

    # Footer
    html_parts.append(
        f"<hr><p style='color: #666;'>"
        f"Total: {len(all_signals)} STRAT signals, {len(all_dominos)} dominos, "
        f"{len(all_sfp_signals)} SFP signals, {len(all_bf_signals)} BF signals "
        f"({len(all_bf_active_long) + len(all_bf_active_short)} active setups) | "
        f"Universe: {len(scan_results)} tickers | "
        f"Timeframes: {', '.join(config.enabled_timeframes)}</p>"
    )
    html_parts.append("</body></html>")

    return "\n".join(html_parts)


# =========================================================================
# EMAIL SENDING
# =========================================================================

def send_alert_email(
    config: ScannerConfig,
    scan_results: list[TickerScanResult],
    market_summaries: list[MarketSummary],
    scan_date: Optional[date] = None,
    sfp_results: Optional[list[TickerSFPResult]] = None,
    bf_results: Optional[list[TickerMagnitudeResult]] = None,
    rotation_result: Optional[RotationResult] = None,
) -> bool:
    """Send the scan alert via Gmail SMTP.

    Args:
        config: scanner config (smtp_email, smtp_password, alert_recipients)
        scan_results: all ticker scan results
        market_summaries: market/sector ETF summaries
        scan_date: date of the scan (defaults to today)
        sfp_results: Swing Failure Pattern results (separate detector)
        bf_results: Broadening Formation Reclaim results (separate detector)
        rotation_result: sector/subsector/thematic rotation ranking —
            informational context only; never itself a reason this
            function gets called (that decision is the caller's, per
            the alert-fatigue rule applied throughout this scanner)

    Returns:
        True if email sent successfully, False otherwise.
    """
    sfp_results = sfp_results or []
    bf_results = bf_results or []

    smtp_email = config.smtp_email or os.environ.get("SMTP_EMAIL", "")
    smtp_password = config.smtp_password or os.environ.get("SMTP_PASSWORD", "")

    if not smtp_email or not smtp_password:
        logger.warning("SMTP credentials not configured, skipping email alert")
        return False

    recipients = config.alert_recipients
    if not recipients:
        logger.warning("No alert recipients configured, skipping email alert")
        return False

    # Count signals, dominos, SFP events, and BF fresh events
    total_signals = sum(len(r.signals) for r in scan_results)
    total_dominos = sum(1 for r in scan_results if r.domino)
    total_sfp = sum(len(r.signals) for r in sfp_results)
    total_bf = sum(len(r.signals) for r in bf_results)

    # Build email
    subject = build_alert_subject(total_signals, total_dominos, scan_date, total_sfp, total_bf)
    text_body = build_alert_body_text(
        scan_results, market_summaries, config, sfp_results, bf_results, rotation_result
    )
    html_body = build_alert_body_html(
        scan_results, market_summaries, config, sfp_results, bf_results, rotation_result
    )

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_email
    msg["To"] = ", ".join(recipients)

    msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
            server.login(smtp_email, smtp_password)
            server.sendmail(smtp_email, recipients, msg.as_string())
        logger.info(f"Alert email sent to {recipients}")
        return True
    except Exception as e:
        logger.error(f"Failed to send alert email: {e}")
        return False
