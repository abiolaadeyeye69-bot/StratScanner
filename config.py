"""
STRAT Scanner Configuration — v3.1.1 faithful translation.

All configurable parameters in one place, mapped 1-to-1 from
TheStrat Suite v3.1.1 Pine Script inputs.
"""
from dataclasses import dataclass, field
from typing import Literal


# ---------------------------------------------------------------------------
# Type aliases matching v3.1.1 input option strings
# ---------------------------------------------------------------------------
FailedMethod = Literal["Reclaim", "Open", "Reclaim + Open", "Reclaim OR Open"]
HammerShooterLogic = Literal["Broad (Loose)", "Classic", "Pin Bar (Strict)"]
StopReference = Literal["CC", "C1"]


@dataclass
class ScannerConfig:
    """Master configuration for the STRAT scanner.

    Every field maps to a v3.1.1 input.  Scanner-only fields (universe,
    data source, alerts) are appended at the end.
    """

    # =================================================================
    # TIMEFRAMES
    # =================================================================
    # Available: "M", "W", "D", "2D", "3D"
    # (12H excluded — requires intraday bars, too expensive on free tier)
    enabled_timeframes: list[str] = field(
        default_factory=lambda: ["M", "W", "D"]
    )

    # =================================================================
    # SIGNALS — Reversals
    # =================================================================
    # Inside Reversals: C1 inside, CC breaks opposite C2's direction
    show_inside_reversals: bool = True
    inside_rev_require_ham_sho: bool = False
    inside_rev_require_ftfc: bool = False

    # 2-2 Reversals: CC immediately breaks opposite C1
    show_22_reversals: bool = True
    rev_22_require_ham_sho: bool = False
    rev_22_require_c1_f2: bool = False      # C1 was itself a Failed 2
    rev_22_require_ftfc: bool = False

    # =================================================================
    # SIGNALS — Continuations
    # =================================================================
    # Inside Continuations: C1 inside, CC breaks same direction as C2
    show_inside_continuations: bool = True
    inside_cont_require_ham_sho: bool = False
    inside_cont_require_ftfc: bool = False

    # 2-2 Continuations: CC keeps pushing in C1's direction
    show_22_continuations: bool = False      # off by default (noisy)
    cont_22_require_ham_sho: bool = False
    cont_22_require_ftfc: bool = False

    # =================================================================
    # SIGNALS — Failing 2s / Range Reclaims
    # =================================================================
    show_failing_2s: bool = False            # "Failing 2s (Range Reclaims)"
    f2_require_ftfc: bool = False

    # =================================================================
    # SIGNALS — Expansions
    # =================================================================
    # 3-2 Expansions: C1 was outside bar, CC commits to one direction
    show_32_expansions: bool = False
    exp_32_require_ham_sho: bool = False
    exp_32_require_ftfc: bool = False

    # Outside Bars (3 Exp): CC itself is a 3-bar engulfing C1
    show_outside_bars: bool = False
    outside_require_ftfc: bool = False

    # =================================================================
    # HAMMER / SHOOTER
    # =================================================================
    hammer_shooter_logic: HammerShooterLogic = "Broad (Loose)"
    hammer_shooter_match_color: bool = False  # hammer=green, shooter=red

    # =================================================================
    # DOMINO
    # =================================================================
    min_domino_tfs: int = 2                  # consecutive inside-bar TFs
    enable_domino_alerts: bool = True

    # =================================================================
    # LEAD SIGNAL / DIRECTIONAL FILTER
    # =================================================================
    enable_directional_filter: bool = False

    # =================================================================
    # FAILED 2 — Advanced
    # =================================================================
    enable_failed_2_detection: bool = True   # master toggle for F2 detect
    # Detection method: how a breakout is judged to be failing
    #   Reclaim:         close back inside C1 range
    #   Open:            close against breakout direction (vs open)
    #   Reclaim + Open:  both conditions required
    #   Reclaim OR Open: either condition sufficient
    failed_2_method: FailedMethod = "Reclaim"

    # =================================================================
    # MAGNITUDE / EXHAUSTION
    # =================================================================
    show_magnitude: bool = True
    mag_only_when_in_force: bool = False
    show_exhaustion: bool = True
    exh_only_when_in_force: bool = False
    exh_requires_mag_hit: bool = False       # only show after mag reached

    # =================================================================
    # STOP LEVELS
    # =================================================================
    show_stop_levels: bool = False
    stop_reference: StopReference = "CC"
    stop_smallest_only: bool = False
    stop_be_at_mag: bool = False             # break-even at magnitude
    stop_be_at_exh: bool = False             # break-even at exhaustion

    # =================================================================
    # EXHAUSTION — Advanced
    # =================================================================
    # Once exhaustion (or mag if no exh) hit, signal no longer "in force"
    exh_disables_in_force: bool = False

    # =================================================================
    # FTFC
    # =================================================================
    # When exhausted TF hits target, exclude it from FTFC (table only)
    exh_disables_ftfc: bool = False

    # =================================================================
    # ALERT CONTENT (what to include in alert messages)
    # =================================================================
    alert_show_trigger: bool = True
    alert_show_magnitude: bool = True
    alert_show_exhaustion: bool = True
    alert_show_stop: bool = True
    alert_show_ftfc: bool = True

    # =================================================================
    # SWING FAILURE PATTERN (SFP) — separate indicator, LuxAlgo v5
    # =================================================================
    # Not part of TheStrat v3.1.1 — a distinct liquidity-sweep/reversal
    # detector bolted onto the same scanner infrastructure.  Field names
    # map 1-to-1 to the LuxAlgo Pine Script inputs where one exists.
    sfp_enabled: bool = True                 # master toggle for this detector
    sfp_swing_length: int = 5                # 'len' — pivot lookback (left bars),
                                              # default used for any TF not listed
                                              # in sfp_swing_length_overrides
    sfp_bullish_enabled: bool = True         # 'bull'
    sfp_bearish_enabled: bool = True         # 'bear'
    sfp_max_age_bars: int = 500              # hardcoded "500" in Pine — bar-age
                                              # cutoff before an unconfirmed
                                              # setup is invalidated

    # Timeframes to run SFP detection on.
    sfp_timeframes: list[str] = field(default_factory=lambda: ["D", "W", "M"])

    # Per-TF override of pivot lookback length.  Monthly only has ~4 bars
    # in a 120-calendar-day window, which can't support the default
    # length-5 pivot (needs 7 bars minimum: 5 left + pivot + 1 right).
    # length=1 is the minimum viable pivot (1 left + pivot + 1 right = 3
    # bars) and is what "monthly SFP on 4 bars" actually means in
    # practice — every local high/low against a single neighbor counts
    # as a pivot. Confidence: this will be noisy. A monthly SFP off a
    # 2-3 month lookback is a much weaker signal than the same pattern
    # on D/W, which have a full length-5 pivot to work with. Treat
    # monthly SFP hits as lower-conviction until you extend
    # history_calendar_days enough to raise this back toward 5.
    sfp_swing_length_overrides: dict[str, int] = field(
        default_factory=lambda: {"M": 1}
    )

    # =================================================================
    # BROADENING FORMATION RECLAIM — separate indicator ("Magnitude
    # Reclaim" / "Magnitude Price Discovery - Bare Bones")
    # =================================================================
    # Not part of TheStrat v3.1.1 and NOT a literal broadening-formation
    # (diverging-trendline) pattern detector — the source script tracks
    # every 1-bar swing high/low, watches for it to be broken, then for
    # price to reclaim it, and locks a target at the most extreme price
    # reached during the break. Field names map 1-to-1 to the source
    # Pine inputs where one exists.
    bf_enabled: bool = True                  # master toggle for this detector
    bf_long_only: bool = False               # 'Long Side Only' — when True,
                                              # swing-high/short tracking is
                                              # skipped entirely (matches source)
    bf_max_swings: int = 20                  # 'Max Swings to Track' — oldest
                                              # tracked swing is dropped once
                                              # this cap is exceeded

    # Timeframes to run detection on (scanner-specific — the original runs
    # on whatever single chart TF is loaded). Monthly excluded by default:
    # swing detection here is a fixed 1-bar pivot (no configurable length),
    # and a ~4-bar monthly window gives too few pivots for the "largest
    # active setup by magnitude" ranking to mean much.
    bf_timeframes: list[str] = field(default_factory=lambda: ["D", "W"])

    # =================================================================
    # SECTOR / SUBSECTOR / THEMATIC ROTATION — scanner-native, not from
    # any Pine Script. Cross-sectional relative-strength ranking of the
    # broad sector ETFs, their subsectors, and thematic groups against
    # each other. See sector_universe.py for the tracked ETF registry.
    # =================================================================
    sector_rotation_enabled: bool = True
    # Lookback windows in TRADING days (bars), not calendar days —
    # 1D = today vs yesterday's close, 5D ~= 1 week, 20D ~= 1 month.
    sector_rotation_periods: list[int] = field(
        default_factory=lambda: [1, 5, 20]
    )
    sector_rotation_include_subsectors: bool = True
    sector_rotation_include_thematic: bool = True
    # How many top/bottom entries to show in logs and email leaderboards
    # (the full ranked list is always in the JSON output regardless).
    sector_rotation_leaders_count: int = 10
    sector_rotation_laggards_count: int = 5

    # =================================================================
    # GAMMA EXPOSURE (GEX) — scanner-native, not from any Pine Script.
    # Estimated dealer gamma positioning per ticker, styled after
    # InsiderFinance's GEX page layout. NOT sourced from InsiderFinance —
    # their terms restrict access to "through the platform" only and they
    # publish no API. Data comes from Yahoo Finance's unofficial options-
    # chain endpoint (via the yfinance library): free, no API key, but not
    # a contracted API — Yahoo's general terms restrict automated access,
    # and yfinance has had unresolved rate-limiting issues reported against
    # it (see gex.py docstring). Yahoo supplies open interest + implied
    # volatility per contract; gamma itself is computed here with a
    # standard Black-Scholes formula (no dividend yield, flat risk-free
    # rate) — this is the same approximation most public/open-source GEX
    # calculators use, not a proprietary or vendor-supplied greek.
    # =================================================================
    gex_enabled: bool = True
    # Full 701-ticker universe isn't meaningful here — options liquidity
    # (and open interest) is concentrated in a handful of names. Default
    # to the market ETFs already tracked elsewhere in this config.
    gex_tickers: list[str] = field(
        default_factory=lambda: ["SPY", "QQQ", "DIA", "IWM"]
    )
    gex_risk_free_rate: float = 0.045     # flat annualized rate for Black-Scholes
    gex_max_expiry_days: int = 60         # ignore expirations further out —
                                           # keeps payload size sane and
                                           # matches where gamma concentration
                                           # actually matters for hedging flow
    gex_price_profile_pct: float = 0.15   # +/- range (as a fraction of spot)
                                           # scanned for the zero-gamma /
                                           # price-profile curve

    # =================================================================
    # SCANNER-SPECIFIC (not in Pine Script)
    # =================================================================

    # --- Universe ---
    universe_size: int = 900
    min_dollar_volume: float = 5_000_000.0
    liquidity_lookback_days: int = 20

    # --- Data ---
    history_calendar_days: int = 120
    polygon_api_key: str = ""                # env var POLYGON_API_KEY

    # --- Email Alerts ---
    smtp_email: str = ""                     # env var SMTP_EMAIL
    smtp_password: str = ""                  # env var SMTP_PASSWORD
    alert_recipients: list[str] = field(default_factory=list)

    # --- Market & Sector ETFs ---
    market_etfs: list[str] = field(
        default_factory=lambda: ["SPY", "QQQ", "DIA", "IWM"]
    )
    sector_etfs: list[str] = field(
        default_factory=lambda: [
            "XLB", "XLC", "XLE", "XLF", "XLI",
            "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY",
        ]
    )

    # --- Output ---
    include_panel_state: bool = True
