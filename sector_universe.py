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


# =========================================================================
# ETF CONSTITUENT HOLDINGS — used to force-include tickers in the scanner
# universe for accurate Simultaneous Breaks coverage.  Each ETF maps to
# its top holdings by weight.  This is the single source of truth: the
# frontend dashboard's ETF_HOLDINGS mirror must match this.
# =========================================================================

ETF_HOLDINGS: dict[str, list[str]] = {
    # ── SECTORS ──────────────────────────────────────────────
    "XLK": ["NVDA", "AAPL", "MSFT", "AMD", "AVGO", "MU", "INTC", "CSCO", "PLTR", "LRCX", "AMAT", "PANW", "ORCL", "CRWD", "TXN", "IBM", "KLAC", "MRVL", "ANET", "QCOM", "CRM", "APH", "STX", "ADI", "INTU", "NOW", "ADBE", "SNPS", "CDNS", "ACN", "FTNT"],
    "XLF": ["JPM", "V", "MA", "BAC", "GS", "WFC", "MS", "C", "SCHW", "AXP", "BLK", "PGR", "COF", "CB", "SPGI", "CME", "USB", "PNC", "BX", "HOOD", "ICE", "TRV", "MET", "AIG", "AFL", "ALL", "MCO"],
    "XLE": ["XOM", "CVX", "COP", "MPC", "PSX", "VLO", "SLB", "EOG", "WMB", "KMI", "TRGP", "OKE", "DVN", "BKR", "OXY", "FANG", "EQT", "HAL", "APA"],
    "XLI": ["CAT", "GE", "RTX", "DE", "UNP", "ETN", "BA", "UBER", "PH", "LMT", "ADP", "TT", "VRT", "PWR", "HWM", "GD", "CSX", "JCI", "MMM", "EMR", "WM", "UPS", "CMI", "NSC"],
    "XLV": ["LLY", "JNJ", "ABBV", "UNH", "ABT", "MRK", "TMO", "ISRG", "BSX", "AMGN", "GILD", "PFE", "SYK", "DHR", "MDT", "VRTX", "BMY", "MCK", "CI", "ELV", "HCA", "ZTS", "REGN", "BDX"],
    "XLY": ["AMZN", "TSLA", "HD", "MCD", "BKNG", "TJX", "SBUX", "LOW", "DASH", "GM", "CMG", "ROST", "ORLY", "AZO", "DHI", "LEN", "YUM", "DPZ", "LULU"],
    "XLC": ["META", "GOOGL", "T", "VZ", "DIS", "NFLX", "TMUS", "CMCSA", "LYV", "OMC", "TTWO", "CHTR", "APP", "TTD", "RDDT", "WBD"],
    "XLP": ["PG", "KO", "PEP", "COST", "WMT", "PM", "MO", "CL", "MDLZ", "GIS", "EL", "KHC", "STZ", "HSY", "MKC", "CHD", "CLX", "KMB", "TSN", "SYY", "ADM"],
    "XLU": ["NEE", "SO", "DUK", "CEG", "VST", "AEP", "SRE", "D", "EXC", "XEL", "PEG", "ED", "WEC", "ETR", "AEE", "CMS", "AWK", "ES", "EIX", "DTE", "FE", "PPL", "NI", "EVRG"],
    "XLB": ["LIN", "APD", "SHW", "ECL", "CTVA", "NEM", "FCX", "NUE", "VMC", "MLM", "IFF", "PPG", "DD", "DOW", "ALB", "CF", "IP", "AMCR", "MOS"],
    "XLRE": ["WELL", "PLD", "AMT", "EQIX", "SPG", "DLR", "PSA", "O", "CCI", "CBRE", "IRM", "EXR", "VTR", "ESS", "HST"],
    # ── SUBSECTORS ───────────────────────────────────────────
    "SMH": ["NVDA", "TSM", "AMD", "AVGO", "MU", "ASML", "INTC", "QCOM", "MRVL", "ADI", "TXN", "AMAT", "LRCX", "KLAC", "CDNS", "SNPS", "TER", "MPWR", "NXPI", "ARM", "ALAB", "MCHP", "ON", "SWKS"],
    "SOXX": ["NVDA", "AMD", "MU", "AVGO", "INTC", "MRVL", "TSM", "AMAT", "KLAC", "ADI", "TXN", "QCOM", "LRCX", "CDNS", "SNPS", "TER", "MPWR", "NXPI", "ARM", "MCHP", "ON", "SWKS"],
    "IGV": ["PLTR", "MSFT", "PANW", "CRWD", "CRM", "ORCL", "NOW", "ADBE", "FTNT", "INTU", "SNPS", "CDNS", "WDAY", "HUBS", "ZS", "DDOG", "TTD"],
    "SKYY": ["NTNX", "ANET", "MSFT", "AMZN", "GOOGL", "MDB", "NET", "DOCN", "TEAM", "CRM", "SNOW", "DDOG", "ORCL", "ZS"],
    "HACK": ["PANW", "CRWD", "FTNT", "ZS", "CSCO", "OKTA", "NET", "AVGO", "GD", "QLYS", "GEN"],
    "CIBR": ["CRWD", "PANW", "FTNT", "CSCO", "AVGO", "NET", "OKTA", "ZS", "RBRK", "FFIV", "PLTR"],
    "FINX": ["HOOD", "XYZ", "PYPL", "COIN", "FI", "GPN", "INTU", "SOFI", "AFRM", "V", "MA"],
    "KRE": ["CFR", "HOMB", "SSB", "CFG", "CBSH", "HWC", "PNFP", "UBSI", "ASB", "ONB", "FITB", "KEY", "RF", "HBAN", "MTB", "TFC", "WAL"],
    "KBE": ["JPM", "BAC", "WFC", "GS", "MS", "C", "SCHW", "COF", "USB", "PNC", "BX", "EQH", "CRBG", "JXN", "VOYA", "ALLY", "FITB", "KEY", "RF", "HBAN", "CFG", "MTB"],
    "KIE": ["PGR", "CB", "TRV", "ALL", "AIG", "MET", "AFL", "HIG", "WRB", "WTW", "RGA", "KNSL", "PLMR", "OSCR"],
    "IAI": ["GS", "MS", "SCHW", "HOOD", "ICE", "CME", "MCO", "SPGI", "NDAQ", "MSCI", "BX", "IBKR", "RJF", "LPLA"],
    "XBI": ["MRNA", "TWST", "NTRA", "HALO", "KYMR", "IOVA", "ROIV", "TVTX", "CORT", "VRTX", "REGN", "ALNY", "INCY", "IONS", "NBIX"],
    "IBB": ["VRTX", "AMGN", "GILD", "REGN", "MRNA", "ARGX", "NTRA", "RVMD", "ALNY", "BIIB", "ILMN", "IDXX", "DXCM"],
    "IHI": ["ABT", "ISRG", "SYK", "BDX", "DXCM", "MDT", "RMD", "EW", "GEHC", "IDXX"],
    "XPH": ["LLY", "JNJ", "MRK", "PFE", "BMY", "ABBV", "VRTX", "CORT", "ETON", "CRNX", "NUVB", "OMER"],
    "IHF": ["UNH", "CVS", "ELV", "VEEV", "HCA", "HUM", "CNC", "CI", "DGX", "LH", "MCK", "CAH"],
    "XOP": ["PBF", "DINO", "MPC", "VLO", "PSX", "COP", "EOG", "DVN", "FANG", "OXY", "EQT", "APA", "OVV", "SM", "PR", "CRGY"],
    "OIH": ["SLB", "BKR", "FTI", "HAL", "RIG", "WFRD", "NE", "OII", "NOV"],
    "AMLP": ["ET", "MPLX", "EPD", "WES", "PAA", "SUN", "HESM", "CQP"],
    "XRT": ["AMZN", "COST", "WMT", "TJX", "ROST", "DG", "DLTR", "FIVE", "BBY", "M", "BURL", "WSM", "W", "ETSY", "EBAY", "TGT", "ANF", "KMX"],
    "XHB": ["DHI", "LEN", "NVR", "PHM", "BLDR", "OC", "MAS", "ALLE", "WSM", "IBP", "JCI", "HD", "LOW", "WMS", "CSL"],
    "ITB": ["DHI", "PHM", "LEN", "NVR", "TOL", "SHW", "HD", "LOW", "MAS", "LII", "BLDR"],
    "PEJ": ["SBUX", "ABNB", "SYY", "LYV", "DAL", "MAR", "EXPE", "BKNG", "DPZ", "YUM", "CMG", "DIS", "NCLH", "RCL", "CCL", "HLT"],
    "ITA": ["GE", "RTX", "BA", "GD", "LMT", "NOC", "HWM", "TDG", "LHX", "AXON", "TXT", "HEI", "LDOS", "KTOS"],
    "PPA": ["RTX", "GE", "BA", "LMT", "GD", "NOC", "HWM", "PH", "AXON", "LHX", "TDG", "HEI"],
    "IYT": ["UNP", "UBER", "CSX", "UPS", "NSC", "FDX", "DAL", "UAL", "ODFL", "EXPD", "JBHT", "XPO"],
    "JETS": ["UAL", "DAL", "AAL", "LUV", "SKYW", "JBLU", "ULCC", "ALK", "ALGT"],
    "GDX": ["NEM", "AEM", "B", "WPM", "AU", "FNV", "KGC", "GFI", "AGI", "BTG", "HL", "EGO", "PAAS"],
    "GDXJ": ["EQX", "AGI", "CDE", "HL", "AG", "EGO", "IAG", "KGC", "BTG", "AU"],
    "SIL": ["WPM", "PAAS", "CDE", "HL", "AG", "SSRM", "BVN", "FSM", "EXK", "SVM"],
    "COPX": ["FCX", "SCCO", "TECK", "HBM", "BHP", "ERO", "TGB", "IE"],
    "SLX": ["NUE", "STLD", "BHP", "RIO", "VALE", "MT", "PKX", "RS", "GGB", "CLF"],
    "REMX": ["ALB", "MP", "SQM", "LAC", "TROX"],
    "REM": ["NLY", "AGNC", "STWD", "RITM", "DX", "ARR", "EFC", "BXMT", "ORC", "LADR"],
    "MORT": ["NLY", "AGNC", "STWD", "RITM", "DX", "ARR", "EFC", "ORC", "BXMT", "TWO"],
    "REZ": ["WELL", "PSA", "EQR", "VTR", "EXR", "ESS", "INVH", "SUI", "MAA", "DOC"],
    # ── THEMATIC ─────────────────────────────────────────────
    "ARKK": ["TSLA", "COIN", "HOOD", "PLTR", "SHOP", "AMD", "NVDA", "AMZN", "META", "AVGO", "GOOG", "NET", "ILMN", "RKLB", "TSM", "KTOS", "TER", "SOFI", "LLY", "CRSP", "BEAM", "TWST", "TXG", "NTLA", "NTRA", "ACHR", "RXRX", "BWXT", "TEM", "VCYT", "PACB"],
    "BOTZ": ["ISRG", "ABB", "NVDA", "AUR", "CGNX", "GOOGL", "TSLA", "DE", "SYM", "ROK"],
    "ROBO": ["ILMN", "ZBRA", "ISRG", "IOT", "NDSN", "EMR", "AMBA", "ROK", "DE", "TER"],
    "AIQ": ["PLTR", "MSFT", "ORCL", "TSLA", "META", "GOOGL", "NFLX", "INTC", "AMZN", "CRM", "SNOW", "DDOG"],
    "ICLN": ["FSLR", "ENPH", "BE", "PLUG", "RUN", "HASI"],
    "TAN": ["FSLR", "ENPH", "HASI", "RUN", "CSIQ", "JKS", "ARRY"],
    "PBW": ["ACHR", "DAR", "ITRI", "GEVO", "PLUG", "FSLR", "BE", "ENPH"],
    "URA": ["CCJ", "NXE", "UEC", "OKLO", "SMR", "EFR"],
    "LIT": ["ALB", "TSLA", "SQM", "ENS", "LAC", "RIO"],
    "BLOK": ["HOOD", "COIN", "CIFR", "HUT", "WULF", "AMD", "CLSK", "DELL", "MARA", "RIOT"],
    "KWEB": ["BABA", "PDD", "JD", "BIDU", "TCOM", "NTES", "TME", "MNSO", "ZTO"],
    "UFO": ["RKLB", "ASTS", "IRDM", "GSAT", "GRMN", "VSAT"],
    "WCLD": ["OKTA", "CRWD", "CRM", "GTLB", "PD", "DOCU", "DT", "NET", "NOW", "DDOG", "ZS", "SNOW", "MDB", "HUBS"],
    "MJ": ["TLRY", "CRON", "SNDL", "VFF", "ACB", "OGI", "HITI"],
}


def get_etf_holding_tickers() -> set[str]:
    """Return the flat set of all constituent tickers across every ETF.

    Used by derive_universe() to force-include these tickers so that
    Simultaneous Breaks has full coverage regardless of liquidity rank.
    """
    tickers: set[str] = set()
    for holdings in ETF_HOLDINGS.values():
        tickers.update(holdings)
    return tickers


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
