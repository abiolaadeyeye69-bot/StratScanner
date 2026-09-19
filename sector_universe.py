"""
Sector / Subsector / Thematic ETF universe — reference data for the
sector rotation ranking. Not from Pine Script, not a translation of
anything — this is scanner-native reference data.

Confidence note (Epistemic Humility)
-------------------------------------
These are real, established US-listed ETFs. Confidence varies by age
and liquidity of the product:

  HIGH confidence (large, long-established, heavily traded — the 11
  SPDR sector funds plus SMH, XBI, KRE, GDX, XOP, ITB, ITA, IYT, IBB,
  KBE): these have traded under these tickers for well over a decade
  and are very unlikely to have changed.

  MEDIUM confidence (real but thinner/newer thematic products — ARKK,
  BOTZ, ROBO, AIQ, URA, BLOK, MJ, UFO, WCLD, PBW, ICLN, KWEB, COPX,
  REMX, SLX, MORT, AMLP, PEJ, PPA): thematic ETFs see more churn than
  broad sector funds — renames, issuer changes, liquidations happen.
  Verify against Polygon (or your broker) before relying on any of
  these in production, especially the ones that never show up in the
  scan output — that's the fastest tell that a ticker moved or died.

This list is a starting point, not a source of truth. Update it as
tickers prove themselves live or dead against real data.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class SectorGroup:
    """One tracked ETF in the rotation universe."""
    ticker: str
    label: str
    category: str                  # "sector", "subsector", "thematic"
    parent: Optional[str] = None   # parent sector ticker, for subsectors only


# =========================================================================
# BROAD SECTORS — SPDR Select Sector ETFs (matches config.sector_etfs)
# =========================================================================

SECTORS: list[SectorGroup] = [
    SectorGroup("XLB", "Materials", "sector"),
    SectorGroup("XLC", "Communication Services", "sector"),
    SectorGroup("XLE", "Energy", "sector"),
    SectorGroup("XLF", "Financials", "sector"),
    SectorGroup("XLI", "Industrials", "sector"),
    SectorGroup("XLK", "Technology", "sector"),
    SectorGroup("XLP", "Consumer Staples", "sector"),
    SectorGroup("XLRE", "Real Estate", "sector"),
    SectorGroup("XLU", "Utilities", "sector"),
    SectorGroup("XLV", "Health Care", "sector"),
    SectorGroup("XLY", "Consumer Discretionary", "sector"),
]

# =========================================================================
# SUBSECTORS — grouped under their parent sector
# =========================================================================

SUBSECTORS: list[SectorGroup] = [
    # --- Technology (XLK) ---
    SectorGroup("SMH", "Semiconductors", "subsector", parent="XLK"),
    SectorGroup("SOXX", "Semiconductors (alt)", "subsector", parent="XLK"),
    SectorGroup("IGV", "Software", "subsector", parent="XLK"),
    SectorGroup("SKYY", "Cloud Computing", "subsector", parent="XLK"),
    SectorGroup("HACK", "Cybersecurity", "subsector", parent="XLK"),
    SectorGroup("CIBR", "Cybersecurity (alt)", "subsector", parent="XLK"),
    SectorGroup("FINX", "Fintech", "subsector", parent="XLK"),

    # --- Financials (XLF) ---
    SectorGroup("KRE", "Regional Banks", "subsector", parent="XLF"),
    SectorGroup("KBE", "Banks", "subsector", parent="XLF"),
    SectorGroup("KIE", "Insurance", "subsector", parent="XLF"),
    SectorGroup("IAI", "Broker-Dealers", "subsector", parent="XLF"),

    # --- Health Care (XLV) ---
    SectorGroup("XBI", "Biotech (equal-weight)", "subsector", parent="XLV"),
    SectorGroup("IBB", "Biotech (cap-weight)", "subsector", parent="XLV"),
    SectorGroup("IHI", "Medical Devices", "subsector", parent="XLV"),
    SectorGroup("XPH", "Pharmaceuticals", "subsector", parent="XLV"),
    SectorGroup("IHF", "Health Care Providers", "subsector", parent="XLV"),

    # --- Energy (XLE) ---
    SectorGroup("XOP", "Oil & Gas E&P", "subsector", parent="XLE"),
    SectorGroup("OIH", "Oil Services", "subsector", parent="XLE"),
    SectorGroup("AMLP", "MLP / Midstream", "subsector", parent="XLE"),

    # --- Consumer Discretionary (XLY) ---
    SectorGroup("XRT", "Retail", "subsector", parent="XLY"),
    SectorGroup("XHB", "Homebuilders", "subsector", parent="XLY"),
    SectorGroup("ITB", "Home Construction", "subsector", parent="XLY"),
    SectorGroup("PEJ", "Leisure & Entertainment", "subsector", parent="XLY"),

    # --- Industrials (XLI) ---
    SectorGroup("ITA", "Aerospace & Defense", "subsector", parent="XLI"),
    SectorGroup("PPA", "Aerospace & Defense (alt)", "subsector", parent="XLI"),
    SectorGroup("IYT", "Transportation", "subsector", parent="XLI"),
    SectorGroup("JETS", "Airlines", "subsector", parent="XLI"),

    # --- Materials (XLB) ---
    SectorGroup("GDX", "Gold Miners", "subsector", parent="XLB"),
    SectorGroup("GDXJ", "Junior Gold Miners", "subsector", parent="XLB"),
    SectorGroup("SIL", "Silver Miners", "subsector", parent="XLB"),
    SectorGroup("COPX", "Copper Miners", "subsector", parent="XLB"),
    SectorGroup("SLX", "Steel", "subsector", parent="XLB"),
    SectorGroup("REMX", "Rare Earth / Strategic Metals", "subsector", parent="XLB"),

    # --- Real Estate (XLRE) ---
    SectorGroup("REM", "Mortgage REITs", "subsector", parent="XLRE"),
    SectorGroup("MORT", "Mortgage REITs (alt)", "subsector", parent="XLRE"),
    SectorGroup("REZ", "Residential REITs", "subsector", parent="XLRE"),
]

# =========================================================================
# THEMATIC — cross-sector, not tied to one GICS sector
# =========================================================================

THEMATIC: list[SectorGroup] = [
    SectorGroup("ARKK", "Disruptive Innovation", "thematic"),
    SectorGroup("BOTZ", "Robotics & AI", "thematic"),
    SectorGroup("ROBO", "Robotics & Automation (alt)", "thematic"),
    SectorGroup("AIQ", "Artificial Intelligence", "thematic"),
    SectorGroup("ICLN", "Clean Energy (global)", "thematic"),
    SectorGroup("TAN", "Solar", "thematic"),
    SectorGroup("PBW", "Clean Energy (alt)", "thematic"),
    SectorGroup("URA", "Uranium", "thematic"),
    SectorGroup("LIT", "Lithium & Battery Tech", "thematic"),
    SectorGroup("BLOK", "Blockchain", "thematic"),
    SectorGroup("KWEB", "China Internet", "thematic"),
    SectorGroup("MJ", "Cannabis", "thematic"),
    SectorGroup("UFO", "Space", "thematic"),
    SectorGroup("WCLD", "Cloud Computing (alt)", "thematic"),
]


def get_sector_universe(config) -> list[SectorGroup]:
    """Build the full sector/subsector/thematic registry per config toggles.

    Broad sectors are always included. Subsectors and thematic groups
    can each be toggled off independently via config.

    Args:
        config: ScannerConfig (uses sector_rotation_include_subsectors
                and sector_rotation_include_thematic)

    Returns:
        Flat list of SectorGroup, deduplicated by construction (no
        ticker appears in more than one of SECTORS/SUBSECTORS/THEMATIC).
    """
    groups = list(SECTORS)
    if config.sector_rotation_include_subsectors:
        groups += SUBSECTORS
    if config.sector_rotation_include_thematic:
        groups += THEMATIC
    return groups
