"""
ETF Holdings Manager — auto-fetches and caches ETF constituent data.

Uses yfinance (already a dependency for VIX) to fetch top holdings for
each ETF in the sector/subsector/thematic universe.  Results are cached
to disk with a weekly refresh cycle.

When yfinance fails for a given ETF, falls back to a hardcoded defaults
map.  The hardcoded data is a reasonable starting point; once the auto-
update runs successfully, it replaces them with current data.

Output format:  dict mapping ETF ticker → list of [symbol, name] pairs.
This matches the dashboard's ETF_HOLDINGS format so the frontend can
consume it directly.

Environment notes:
  - yfinance calls hit Yahoo Finance (no API key needed).
  - Rate-limited at ~1 req/1.5s to avoid throttling.
  - Cache stored alongside other scanner caches (SCANNER_CACHE_DIR).
"""
from __future__ import annotations

import json
import logging
import time
from datetime import date
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Pause between yfinance calls to avoid Yahoo throttling
_YF_DELAY_SECONDS = 1.5


# =========================================================================
# HELPERS
# =========================================================================

def _is_valid_ticker(sym: str) -> bool:
    """Check if a string looks like a valid US equity ticker."""
    if not sym or len(sym) > 6 or len(sym) == 0:
        return False
    # Allow letters and a single dot (BRK.B) — filter out cash, futures, etc.
    alpha_count = sum(1 for c in sym if c.isalpha())
    return alpha_count >= 1 and all(c.isalpha() or c == '.' for c in sym)


def _clean_name(name: str) -> str:
    """Trim verbose legal suffixes for compact display."""
    for suffix in (
        " Inc.", " Inc", " Corp.", " Corp", " Co.", " Co",
        " Ltd.", " Ltd", " PLC", " plc", " N.V.", " SE",
        " Group", " Holdings", " Holding",
        ", Inc.", ", Inc", ", Corp.", ", Corp",
        " Class A", " Class B", " Class C", " Cl A", " Cl B",
    ):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name.strip()


# =========================================================================
# SINGLE-ETF FETCHER
# =========================================================================

def fetch_single_etf(ticker: str) -> Optional[list[list[str]]]:
    """Fetch holdings for one ETF via yfinance.

    Returns list of [symbol, name] pairs, or None if fetch fails.
    Filters out non-equity entries (cash, futures, swaps).
    """
    try:
        import yfinance as yf

        etf = yf.Ticker(ticker)
        fd = etf.funds_data

        if fd is None:
            return None

        th = fd.top_holdings
        if th is None:
            return None

        holdings: list[list[str]] = []

        # ── DataFrame format (most common in recent yfinance) ──
        if hasattr(th, "iterrows"):
            for idx, row in th.iterrows():
                sym = None
                # Try explicit symbol columns
                if hasattr(row, "index"):
                    for col in ("Symbol", "symbol", "Ticker", "ticker"):
                        if col in row.index and row[col]:
                            sym = str(row[col]).strip()
                            break
                # Fall back to DataFrame index
                if not sym and isinstance(idx, str):
                    sym = idx.strip()

                if not sym or not _is_valid_ticker(sym):
                    continue

                sym = sym.upper()

                # Extract human-readable name
                name = sym
                if hasattr(row, "index"):
                    for col in (
                        "holdingName", "Name", "name",
                        "shortName", "longName",
                    ):
                        if col in row.index and row[col]:
                            name = _clean_name(str(row[col]).strip())
                            break

                holdings.append([sym, name])

        # ── Series format (symbol → weight) ──
        elif hasattr(th, "items") and hasattr(th, "index"):
            for sym in th.index:
                sym = str(sym).strip()
                if _is_valid_ticker(sym):
                    holdings.append([sym.upper(), sym.upper()])

        # ── Plain dict format ──
        elif isinstance(th, dict):
            for sym in th:
                sym = str(sym).strip()
                if _is_valid_ticker(sym):
                    holdings.append([sym.upper(), sym.upper()])

        return holdings if holdings else None

    except Exception as e:
        logger.debug(f"yfinance holdings fetch failed for {ticker}: {e}")
        return None


# =========================================================================
# HOLDINGS MANAGER
# =========================================================================

class HoldingsManager:
    """Fetches, caches, and serves ETF constituent data.

    Usage::

        mgr = HoldingsManager(Path("./cache"))
        holdings = mgr.get_holdings(["XLK", "XLF", "ARKK", ...])
        # → {"XLK": [["NVDA","NVIDIA"], ["AAPL","Apple"], ...], ...}
    """

    def __init__(self, cache_dir: Path, refresh_days: int = 7):
        self.cache_dir = cache_dir
        self.cache_path = cache_dir / "etf_holdings.json"
        self.refresh_days = refresh_days

    def get_holdings(
        self,
        etf_tickers: list[str],
        force_refresh: bool = False,
    ) -> dict[str, list[list[str]]]:
        """Get holdings for all ETFs.  Uses cache if fresh.

        Args:
            etf_tickers: ETF tickers to fetch holdings for.
            force_refresh: bypass cache and re-fetch everything.

        Returns:
            Dict mapping ETF ticker → list of [symbol, name] pairs.
        """
        # ── Try cache first ──
        if not force_refresh:
            cached = self._load_cache()
            if cached is not None:
                return cached

        # ── Fetch fresh from yfinance ──
        logger.info(
            f"Fetching ETF holdings for {len(etf_tickers)} ETFs "
            f"(this takes ~{len(etf_tickers) * _YF_DELAY_SECONDS / 60:.0f} min "
            f"on first run)…"
        )

        holdings: dict[str, list[list[str]]] = {}
        fetched = 0
        fallback_used = 0
        failed = 0

        for i, ticker in enumerate(etf_tickers):
            result = fetch_single_etf(ticker)

            if result:
                holdings[ticker] = result
                fetched += 1
                logger.debug(
                    f"  {ticker}: {len(result)} holdings (yfinance)"
                )
            else:
                # Fall back to hardcoded defaults
                if ticker in DEFAULT_HOLDINGS:
                    holdings[ticker] = DEFAULT_HOLDINGS[ticker]
                    fallback_used += 1
                    logger.debug(
                        f"  {ticker}: {len(DEFAULT_HOLDINGS[ticker])} "
                        f"holdings (fallback)"
                    )
                else:
                    failed += 1
                    logger.debug(f"  {ticker}: no data available")

            # Rate-limit yfinance calls
            if i < len(etf_tickers) - 1:
                time.sleep(_YF_DELAY_SECONDS)

            if (i + 1) % 10 == 0:
                logger.info(
                    f"  Holdings progress: {i + 1}/{len(etf_tickers)} "
                    f"({fetched} live, {fallback_used} fallback, "
                    f"{failed} missing)"
                )

        logger.info(
            f"ETF holdings complete: {fetched} from yfinance, "
            f"{fallback_used} from fallback, {failed} unavailable"
        )

        # ── Save cache ──
        self._save_cache(holdings)

        return holdings

    # -----------------------------------------------------------------
    # Cache I/O
    # -----------------------------------------------------------------

    def _load_cache(self) -> Optional[dict]:
        """Load holdings from disk cache if fresh enough."""
        if not self.cache_path.exists():
            return None

        try:
            with open(self.cache_path) as f:
                data = json.load(f)

            cached_date = date.fromisoformat(data.get("date", "2000-01-01"))
            age_days = (date.today() - cached_date).days

            if age_days < self.refresh_days:
                holdings = data.get("holdings", {})
                logger.info(
                    f"ETF holdings from cache: {len(holdings)} ETFs "
                    f"(cached {cached_date}, {age_days}d old)"
                )
                return holdings

            logger.info(
                f"ETF holdings cache stale ({age_days}d old), refreshing"
            )
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning(f"ETF holdings cache corrupt: {e}")

        return None

    def _save_cache(self, holdings: dict) -> None:
        """Save holdings to disk cache."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            with open(self.cache_path, "w") as f:
                json.dump(
                    {
                        "date": date.today().isoformat(),
                        "etf_count": len(holdings),
                        "total_constituents": sum(
                            len(v) for v in holdings.values()
                        ),
                        "holdings": holdings,
                    },
                    f,
                    indent=2,
                )
            logger.info(f"ETF holdings cache saved: {len(holdings)} ETFs")
        except Exception as e:
            logger.warning(f"Failed to save holdings cache: {e}")


# =========================================================================
# HARDCODED FALLBACK HOLDINGS
# =========================================================================
# Used when yfinance is down or returns no data for a given ETF.
# Compact pipe-delimited format, parsed at import time.
# These are approximations — the auto-update via yfinance overrides them.

_RAW: dict[str, str] = {
    # ── SECTORS (verified Sep 2026) ──────────────────────────────
    "XLK": (
        "NVDA:NVIDIA|AAPL:Apple|MSFT:Microsoft|AMD:AMD|AVGO:Broadcom|"
        "MU:Micron|INTC:Intel|CSCO:Cisco|PLTR:Palantir|LRCX:Lam Research|"
        "AMAT:Applied Materials|PANW:Palo Alto Networks|ORCL:Oracle|"
        "CRWD:CrowdStrike|TXN:Texas Instruments|IBM:IBM|KLAC:KLA Corp|"
        "MRVL:Marvell|ANET:Arista Networks|QCOM:Qualcomm|CRM:Salesforce|"
        "APH:Amphenol|STX:Seagate|ADI:Analog Devices|INTU:Intuit|"
        "NOW:ServiceNow|ADBE:Adobe|SNPS:Synopsys|CDNS:Cadence|"
        "ACN:Accenture|FTNT:Fortinet"
    ),
    "XLF": (
        "JPM:JPMorgan|V:Visa|MA:Mastercard|BAC:Bank of America|"
        "GS:Goldman Sachs|WFC:Wells Fargo|MS:Morgan Stanley|C:Citigroup|"
        "SCHW:Charles Schwab|AXP:American Express|BLK:BlackRock|"
        "PGR:Progressive|COF:Capital One|CB:Chubb|SPGI:S&P Global|"
        "CME:CME Group|USB:US Bancorp|PNC:PNC Financial|BX:Blackstone|"
        "HOOD:Robinhood|ICE:Intercontinental Exchange|TRV:Travelers|"
        "MET:MetLife|AIG:AIG|AFL:Aflac|ALL:Allstate|MCO:Moody's"
    ),
    "XLE": (
        "XOM:ExxonMobil|CVX:Chevron|COP:ConocoPhillips|"
        "MPC:Marathon Petroleum|PSX:Phillips 66|VLO:Valero|SLB:SLB|"
        "EOG:EOG Resources|WMB:Williams Cos|KMI:Kinder Morgan|"
        "TRGP:Targa Resources|OKE:ONEOK|DVN:Devon Energy|BKR:Baker Hughes|"
        "OXY:Occidental|FANG:Diamondback|EQT:EQT Corp|HAL:Halliburton|"
        "APA:APA Corp"
    ),
    "XLI": (
        "CAT:Caterpillar|GE:GE Aerospace|RTX:RTX Corp|DE:Deere|"
        "UNP:Union Pacific|ETN:Eaton|BA:Boeing|UBER:Uber|"
        "PH:Parker-Hannifin|LMT:Lockheed Martin|ADP:Automatic Data|"
        "TT:Trane Technologies|VRT:Vertiv|PWR:Quanta Services|"
        "HWM:Howmet Aerospace|GD:General Dynamics|CSX:CSX Corp|"
        "JCI:Johnson Controls|MMM:3M|EMR:Emerson|WM:Waste Management|"
        "UPS:UPS|CMI:Cummins|NSC:Norfolk Southern"
    ),
    "XLV": (
        "LLY:Eli Lilly|JNJ:Johnson & Johnson|ABBV:AbbVie|"
        "UNH:UnitedHealth|ABT:Abbott Labs|MRK:Merck|TMO:Thermo Fisher|"
        "ISRG:Intuitive Surgical|BSX:Boston Scientific|AMGN:Amgen|"
        "GILD:Gilead|PFE:Pfizer|SYK:Stryker|DHR:Danaher|MDT:Medtronic|"
        "VRTX:Vertex|BMY:Bristol Myers|MCK:McKesson|CI:Cigna|"
        "ELV:Elevance|HCA:HCA Healthcare|ZTS:Zoetis|REGN:Regeneron|"
        "BDX:Becton Dickinson"
    ),
    "XLY": (
        "AMZN:Amazon|TSLA:Tesla|HD:Home Depot|MCD:McDonald's|"
        "BKNG:Booking Holdings|TJX:TJX Cos|SBUX:Starbucks|LOW:Lowe's|"
        "DASH:DoorDash|GM:General Motors|CMG:Chipotle|ROST:Ross Stores|"
        "ORLY:O'Reilly Auto|AZO:AutoZone|DHI:D.R. Horton|LEN:Lennar|"
        "YUM:Yum Brands|DPZ:Domino's|LULU:Lululemon"
    ),
    "XLC": (
        "META:Meta|GOOGL:Alphabet A|T:AT&T|VZ:Verizon|DIS:Disney|"
        "NFLX:Netflix|TMUS:T-Mobile|CMCSA:Comcast|LYV:Live Nation|"
        "OMC:Omnicom|TTWO:Take-Two|CHTR:Charter|APP:AppLovin|"
        "TTD:Trade Desk|RDDT:Reddit|WBD:Warner Bros Discovery"
    ),
    "XLP": (
        "PG:Procter & Gamble|KO:Coca-Cola|PEP:PepsiCo|COST:Costco|"
        "WMT:Walmart|PM:Philip Morris|MO:Altria|CL:Colgate-Palmolive|"
        "MDLZ:Mondelez|GIS:General Mills|EL:Estee Lauder|"
        "KHC:Kraft Heinz|STZ:Constellation Brands|HSY:Hershey|"
        "MKC:McCormick|CHD:Church & Dwight|CLX:Clorox|"
        "KMB:Kimberly-Clark|TSN:Tyson|SYY:Sysco|ADM:ADM"
    ),
    "XLU": (
        "NEE:NextEra Energy|SO:Southern Co|DUK:Duke Energy|"
        "CEG:Constellation Energy|VST:Vistra|AEP:AEP|SRE:Sempra|"
        "D:Dominion|EXC:Exelon|XEL:Xcel Energy|PEG:PSEG|"
        "ED:Consolidated Edison|WEC:WEC Energy|ETR:Entergy|AEE:Ameren|"
        "CMS:CMS Energy|AWK:American Water|ES:Eversource|EIX:Edison Intl|"
        "DTE:DTE Energy|FE:FirstEnergy|PPL:PPL Corp|NI:NiSource|"
        "EVRG:Evergy"
    ),
    "XLB": (
        "LIN:Linde|APD:Air Products|SHW:Sherwin-Williams|ECL:Ecolab|"
        "CTVA:Corteva|NEM:Newmont|FCX:Freeport-McMoRan|NUE:Nucor|"
        "VMC:Vulcan Materials|MLM:Martin Marietta|IFF:IFF|"
        "PPG:PPG Industries|DD:DuPont|DOW:Dow|ALB:Albemarle|"
        "CF:CF Industries|IP:Intl Paper|AMCR:Amcor|MOS:Mosaic"
    ),
    "XLRE": (
        "WELL:Welltower|PLD:Prologis|AMT:American Tower|EQIX:Equinix|"
        "SPG:Simon Property|DLR:Digital Realty|PSA:Public Storage|"
        "O:Realty Income|CCI:Crown Castle|CBRE:CBRE Group|"
        "IRM:Iron Mountain|EXR:Extra Space|VTR:Ventas|"
        "ESS:Essex Property|HST:Host Hotels"
    ),

    # ── SUBSECTORS (verified Sep 2026) ───────────────────────────
    "SMH": (
        "NVDA:NVIDIA|TSM:Taiwan Semi|AMD:AMD|AVGO:Broadcom|MU:Micron|"
        "ASML:ASML|INTC:Intel|QCOM:Qualcomm|MRVL:Marvell|"
        "ADI:Analog Devices|TXN:Texas Instruments|AMAT:Applied Materials|"
        "LRCX:Lam Research|KLAC:KLA Corp|CDNS:Cadence|SNPS:Synopsys|"
        "TER:Teradyne|MPWR:Monolithic Power|NXPI:NXP Semi|"
        "ARM:ARM Holdings|ALAB:Astera Labs|MCHP:Microchip|"
        "ON:ON Semi|SWKS:Skyworks"
    ),
    "SOXX": (
        "NVDA:NVIDIA|AMD:AMD|MU:Micron|AVGO:Broadcom|INTC:Intel|"
        "MRVL:Marvell|TSM:Taiwan Semi|AMAT:Applied Materials|"
        "KLAC:KLA Corp|ADI:Analog Devices|TXN:Texas Instruments|"
        "QCOM:Qualcomm|LRCX:Lam Research|CDNS:Cadence|SNPS:Synopsys|"
        "TER:Teradyne|MPWR:Monolithic Power|NXPI:NXP Semi|"
        "ARM:ARM Holdings|MCHP:Microchip|ON:ON Semi|SWKS:Skyworks"
    ),
    "IGV": (
        "PLTR:Palantir|MSFT:Microsoft|PANW:Palo Alto Networks|"
        "CRWD:CrowdStrike|CRM:Salesforce|ORCL:Oracle|NOW:ServiceNow|"
        "ADBE:Adobe|FTNT:Fortinet|INTU:Intuit|SNPS:Synopsys|"
        "CDNS:Cadence|WDAY:Workday|HUBS:HubSpot|ZS:Zscaler|"
        "DDOG:Datadog|TTD:Trade Desk"
    ),
    "SKYY": (
        "NTNX:Nutanix|ANET:Arista Networks|MSFT:Microsoft|"
        "AMZN:Amazon|GOOGL:Alphabet|MDB:MongoDB|NET:Cloudflare|"
        "DOCN:DigitalOcean|TEAM:Atlassian|CRM:Salesforce|"
        "SNOW:Snowflake|DDOG:Datadog|ORCL:Oracle|ZS:Zscaler"
    ),
    "HACK": (
        "PANW:Palo Alto|CRWD:CrowdStrike|FTNT:Fortinet|ZS:Zscaler|"
        "CSCO:Cisco|OKTA:Okta|NET:Cloudflare|AVGO:Broadcom|"
        "GD:General Dynamics|QLYS:Qualys|GEN:Gen Digital"
    ),
    "CIBR": (
        "CRWD:CrowdStrike|PANW:Palo Alto|FTNT:Fortinet|CSCO:Cisco|"
        "AVGO:Broadcom|NET:Cloudflare|OKTA:Okta|ZS:Zscaler|"
        "RBRK:Rubrik|FFIV:F5|PLTR:Palantir"
    ),
    "FINX": (
        "HOOD:Robinhood|XYZ:Block|PYPL:PayPal|COIN:Coinbase|"
        "FI:Fiserv|GPN:Global Payments|INTU:Intuit|SOFI:SoFi|"
        "AFRM:Affirm|V:Visa|MA:Mastercard"
    ),
    "KRE": (
        "CFR:Cullen Frost|HOMB:Home BancShares|SSB:SouthState|"
        "CFG:Citizens Financial|CBSH:Commerce Bancshares|"
        "HWC:Hancock Whitney|PNFP:Pinnacle Financial|"
        "UBSI:United Bankshares|ASB:Associated Banc-Corp|"
        "ONB:Old National|FITB:Fifth Third|KEY:KeyCorp|"
        "RF:Regions Financial|HBAN:Huntington|MTB:M&T Bank|"
        "TFC:Truist|WAL:Western Alliance"
    ),
    "KBE": (
        "JPM:JPMorgan|BAC:Bank of America|WFC:Wells Fargo|"
        "GS:Goldman Sachs|MS:Morgan Stanley|C:Citigroup|SCHW:Schwab|"
        "COF:Capital One|USB:US Bancorp|PNC:PNC|BX:Blackstone|"
        "EQH:Equitable|CRBG:Corebridge|JXN:Jackson Financial|"
        "VOYA:Voya Financial|ALLY:Ally Financial|"
        "FITB:Fifth Third|KEY:KeyCorp|RF:Regions|HBAN:Huntington|"
        "CFG:Citizens|MTB:M&T Bank"
    ),
    "KIE": (
        "PGR:Progressive|CB:Chubb|TRV:Travelers|ALL:Allstate|AIG:AIG|"
        "MET:MetLife|AFL:Aflac|HIG:Hartford|WRB:Berkley|"
        "WTW:Willis Towers Watson|RGA:Reinsurance Group|"
        "KNSL:Kinsale Capital|PLMR:Palomar|OSCR:Oscar Health"
    ),
    "IAI": (
        "GS:Goldman Sachs|MS:Morgan Stanley|SCHW:Schwab|HOOD:Robinhood|"
        "ICE:Intercontinental Exchange|CME:CME Group|MCO:Moody's|"
        "SPGI:S&P Global|NDAQ:Nasdaq|MSCI:MSCI|"
        "BX:Blackstone|IBKR:Interactive Brokers|RJF:Raymond James|"
        "LPLA:LPL Financial"
    ),
    "XBI": (
        "MRNA:Moderna|TWST:Twist Bioscience|NTRA:Natera|HALO:Halozyme|"
        "KYMR:Kymera|IOVA:Iovance|ROIV:Roivant|TVTX:Travere|"
        "CORT:Corcept|VRTX:Vertex|REGN:Regeneron|ALNY:Alnylam|"
        "INCY:Incyte|IONS:Ionis|NBIX:Neurocrine"
    ),
    "IBB": (
        "VRTX:Vertex|AMGN:Amgen|GILD:Gilead|REGN:Regeneron|"
        "MRNA:Moderna|ARGX:argenx|NTRA:Natera|RVMD:Revolution Medicines|"
        "ALNY:Alnylam|BIIB:Biogen|ILMN:Illumina|IDXX:IDEXX|"
        "DXCM:DexCom"
    ),
    "IHI": (
        "ABT:Abbott|ISRG:Intuitive Surgical|SYK:Stryker|"
        "BDX:Becton Dickinson|DXCM:DexCom|MDT:Medtronic|"
        "RMD:ResMed|EW:Edwards Life|GEHC:GE HealthCare|IDXX:IDEXX"
    ),
    "XPH": (
        "LLY:Eli Lilly|JNJ:Johnson & Johnson|MRK:Merck|PFE:Pfizer|"
        "BMY:Bristol Myers|ABBV:AbbVie|VRTX:Vertex|"
        "CORT:Corcept|ETON:Eton Pharma|CRNX:Crinetics|"
        "NUVB:Nuvation Bio|OMER:Omeros"
    ),
    "IHF": (
        "UNH:UnitedHealth|CVS:CVS Health|ELV:Elevance|VEEV:Veeva|"
        "HCA:HCA Healthcare|HUM:Humana|CNC:Centene|CI:Cigna|"
        "DGX:Quest Diagnostics|LH:Labcorp|MCK:McKesson|CAH:Cardinal Health"
    ),
    "XOP": (
        "PBF:PBF Energy|DINO:HF Sinclair|MPC:Marathon Petroleum|"
        "VLO:Valero|PSX:Phillips 66|COP:ConocoPhillips|"
        "EOG:EOG Resources|DVN:Devon Energy|FANG:Diamondback|"
        "OXY:Occidental|EQT:EQT Corp|APA:APA Corp|OVV:Ovintiv|"
        "SM:SM Energy|PR:Permian Resources|CRGY:Crescent Energy"
    ),
    "OIH": (
        "SLB:SLB|BKR:Baker Hughes|FTI:TechnipFMC|HAL:Halliburton|"
        "RIG:Transocean|WFRD:Weatherford|NE:Noble Corp|"
        "OII:Oceaneering|NOV:NOV Inc"
    ),
    "AMLP": (
        "ET:Energy Transfer|MPLX:MPLX|EPD:Enterprise Products|"
        "WES:Western Midstream|PAA:Plains All American|SUN:Sunoco|"
        "HESM:Hess Midstream|CQP:Cheniere Partners"
    ),
    "XRT": (
        "AMZN:Amazon|COST:Costco|WMT:Walmart|TJX:TJX Cos|"
        "ROST:Ross Stores|DG:Dollar General|DLTR:Dollar Tree|"
        "FIVE:Five Below|BBY:Best Buy|M:Macy's|BURL:Burlington|"
        "WSM:Williams-Sonoma|W:Wayfair|ETSY:Etsy|EBAY:eBay|"
        "TGT:Target|ANF:Abercrombie|KMX:CarMax"
    ),
    "XHB": (
        "DHI:D.R. Horton|LEN:Lennar|NVR:NVR|PHM:PulteGroup|"
        "BLDR:Builders First|OC:Owens Corning|MAS:Masco|"
        "ALLE:Allegion|WSM:Williams-Sonoma|IBP:Installed Building|"
        "JCI:Johnson Controls|HD:Home Depot|LOW:Lowe's|"
        "WMS:Advanced Drainage|CSL:Carlisle"
    ),
    "ITB": (
        "DHI:D.R. Horton|PHM:PulteGroup|LEN:Lennar|NVR:NVR|"
        "TOL:Toll Brothers|SHW:Sherwin-Williams|HD:Home Depot|"
        "LOW:Lowe's|MAS:Masco|LII:Lennox|BLDR:Builders First"
    ),
    "PEJ": (
        "SBUX:Starbucks|ABNB:Airbnb|SYY:Sysco|LYV:Live Nation|"
        "DAL:Delta Air|MAR:Marriott|EXPE:Expedia|"
        "BKNG:Booking|DPZ:Domino's|YUM:Yum Brands|CMG:Chipotle|"
        "DIS:Disney|NCLH:Norwegian Cruise|RCL:Royal Caribbean|"
        "CCL:Carnival|HLT:Hilton"
    ),
    "ITA": (
        "GE:GE Aerospace|RTX:RTX Corp|BA:Boeing|GD:General Dynamics|"
        "LMT:Lockheed Martin|NOC:Northrop Grumman|HWM:Howmet|"
        "TDG:TransDigm|LHX:L3Harris|AXON:Axon Enterprise|"
        "TXT:Textron|HEI:Heico|LDOS:Leidos|KTOS:Kratos"
    ),
    "PPA": (
        "RTX:RTX Corp|GE:GE Aerospace|BA:Boeing|LMT:Lockheed Martin|"
        "GD:General Dynamics|NOC:Northrop|HWM:Howmet|PH:Parker-Hannifin|"
        "AXON:Axon Enterprise|LHX:L3Harris|TDG:TransDigm|HEI:Heico"
    ),
    "IYT": (
        "UNP:Union Pacific|UBER:Uber|CSX:CSX Corp|UPS:UPS|"
        "NSC:Norfolk Southern|FDX:FedEx|DAL:Delta Air|UAL:United Airlines|"
        "ODFL:Old Dominion|EXPD:Expeditors|JBHT:J.B. Hunt|"
        "XPO:XPO Logistics"
    ),
    "JETS": (
        "UAL:United Airlines|DAL:Delta Air|AAL:American Airlines|"
        "LUV:Southwest|SKYW:SkyWest|JBLU:JetBlue|"
        "ULCC:Frontier Group|ALK:Alaska Air|ALGT:Allegiant Travel"
    ),
    "GDX": (
        "NEM:Newmont|AEM:Agnico Eagle|B:Barrick Mining|"
        "WPM:Wheaton Precious Metals|AU:AngloGold|FNV:Franco-Nevada|"
        "KGC:Kinross|GFI:Gold Fields|AGI:Alamos Gold|"
        "BTG:B2Gold|HL:Hecla Mining|EGO:Eldorado Gold|"
        "PAAS:Pan American Silver"
    ),
    "GDXJ": (
        "EQX:Equinox Gold|AGI:Alamos Gold|CDE:Coeur Mining|"
        "HL:Hecla Mining|AG:First Majestic|EGO:Eldorado|"
        "IAG:IAMGOLD|KGC:Kinross|BTG:B2Gold|AU:AngloGold"
    ),
    "SIL": (
        "WPM:Wheaton Precious Metals|PAAS:Pan American Silver|"
        "CDE:Coeur Mining|HL:Hecla Mining|AG:First Majestic|"
        "SSRM:SSR Mining|BVN:Buenaventura|FSM:Fortuna Mining|"
        "EXK:Endeavour Silver|SVM:Silvercorp"
    ),
    "COPX": (
        "FCX:Freeport-McMoRan|SCCO:Southern Copper|TECK:Teck Resources|"
        "HBM:Hudbay Minerals|BHP:BHP Group|ERO:Ero Copper|"
        "TGB:Taseko Mines|IE:Ivanhoe Electric"
    ),
    "SLX": (
        "NUE:Nucor|STLD:Steel Dynamics|BHP:BHP Group|RIO:Rio Tinto|"
        "VALE:Vale|MT:ArcelorMittal|PKX:POSCO|RS:Reliance Steel|"
        "GGB:Gerdau|CLF:Cleveland-Cliffs"
    ),
    "REMX": (
        "ALB:Albemarle|MP:MP Materials|SQM:Sociedad Quimica|"
        "LAC:Lithium Americas|TROX:Tronox"
    ),
    "REM": (
        "NLY:Annaly Capital|AGNC:AGNC Investment|STWD:Starwood Property|"
        "RITM:Rithm Capital|DX:Dynex Capital|ARR:ARMOUR Residential|"
        "EFC:Ellington Financial|BXMT:Blackstone Mortgage|"
        "ORC:Orchid Island|LADR:Ladder Capital"
    ),
    "MORT": (
        "NLY:Annaly Capital|AGNC:AGNC Investment|STWD:Starwood Property|"
        "RITM:Rithm Capital|DX:Dynex Capital|ARR:ARMOUR Residential|"
        "EFC:Ellington Financial|ORC:Orchid Island|"
        "BXMT:Blackstone Mortgage|TWO:Two Harbors"
    ),
    "REZ": (
        "WELL:Welltower|PSA:Public Storage|EQR:Equity Residential|"
        "VTR:Ventas|EXR:Extra Space|ESS:Essex Property|"
        "INVH:Invitation Homes|SUI:Sun Communities|"
        "MAA:Mid-America Apartment|DOC:Healthpeak"
    ),

    # ── THEMATIC (verified Sep 2026) ─────────────────────────────
    # ARKK updated from ARK Invest holdings report (Sep 2026)
    "ARKK": (
        "TSLA:Tesla|COIN:Coinbase|HOOD:Robinhood|PLTR:Palantir|"
        "SHOP:Shopify|AMD:AMD|NVDA:NVIDIA|AMZN:Amazon|META:Meta|"
        "AVGO:Broadcom|GOOG:Alphabet|NET:Cloudflare|ILMN:Illumina|"
        "RKLB:Rocket Lab|TSM:Taiwan Semi|KTOS:Kratos|TER:Teradyne|"
        "SOFI:SoFi|LLY:Eli Lilly|CRSP:CRISPR Therapeutics|"
        "BEAM:Beam Therapeutics|TWST:Twist Bioscience|TXG:10x Genomics|"
        "NTLA:Intellia|NTRA:Natera|ACHR:Archer Aviation|"
        "RXRX:Recursion|BWXT:BWX Technologies|TEM:Tempus AI|"
        "VCYT:Veracyte|PACB:PacBio"
    ),
    "BOTZ": (
        "ISRG:Intuitive Surgical|ABB:ABB Ltd|NVDA:NVIDIA|"
        "AUR:Aurora Innovation|CGNX:Cognex|GOOGL:Alphabet|"
        "TSLA:Tesla|DE:Deere|SYM:Symbotic|ROK:Rockwell Automation"
    ),
    "ROBO": (
        "ILMN:Illumina|ZBRA:Zebra Technologies|ISRG:Intuitive Surgical|"
        "IOT:Samsara|NDSN:Nordson|EMR:Emerson|AMBA:Ambarella|"
        "ROK:Rockwell Automation|DE:Deere|TER:Teradyne"
    ),
    "AIQ": (
        "PLTR:Palantir|MSFT:Microsoft|ORCL:Oracle|TSLA:Tesla|"
        "META:Meta|GOOGL:Alphabet|NFLX:Netflix|INTC:Intel|"
        "AMZN:Amazon|CRM:Salesforce|SNOW:Snowflake|DDOG:Datadog"
    ),
    "ICLN": (
        "FSLR:First Solar|ENPH:Enphase|BE:Bloom Energy|"
        "PLUG:Plug Power|RUN:Sunrun|HASI:HA Sustainable"
    ),
    "TAN": (
        "FSLR:First Solar|ENPH:Enphase|HASI:HA Sustainable|"
        "RUN:Sunrun|CSIQ:Canadian Solar|JKS:JinkoSolar|ARRY:Array Tech"
    ),
    "PBW": (
        "ACHR:Archer Aviation|DAR:Darling Ingredients|ITRI:Itron|"
        "GEVO:Gevo|PLUG:Plug Power|FSLR:First Solar|"
        "BE:Bloom Energy|ENPH:Enphase"
    ),
    "URA": (
        "CCJ:Cameco|NXE:NexGen Energy|UEC:Uranium Energy|"
        "OKLO:Oklo|SMR:NuScale Power|EFR:Energy Fuels"
    ),
    "LIT": (
        "ALB:Albemarle|TSLA:Tesla|SQM:Sociedad Quimica|"
        "ENS:EnerSys|LAC:Lithium Americas|RIO:Rio Tinto"
    ),
    "BLOK": (
        "HOOD:Robinhood|COIN:Coinbase|CIFR:Cipher Mining|"
        "HUT:Hut 8|WULF:TeraWulf|AMD:AMD|CLSK:CleanSpark|"
        "DELL:Dell|MARA:Marathon Digital|RIOT:Riot Platforms"
    ),
    "KWEB": (
        "BABA:Alibaba|PDD:PDD Holdings|JD:JD.com|BIDU:Baidu|"
        "TCOM:Trip.com|NTES:NetEase|TME:Tencent Music|"
        "MNSO:MINISO|ZTO:ZTO Express"
    ),
    "MJ": (
        "TLRY:Tilray|CRON:Cronos|SNDL:SNDL|VFF:Village Farms|"
        "ACB:Aurora Cannabis|OGI:Organigram|HITI:High Tide"
    ),
    "UFO": (
        "RKLB:Rocket Lab|ASTS:AST SpaceMobile|IRDM:Iridium|"
        "GSAT:Globalstar|GRMN:Garmin|VSAT:Viasat"
    ),
    "WCLD": (
        "OKTA:Okta|CRWD:CrowdStrike|CRM:Salesforce|GTLB:GitLab|"
        "PD:PagerDuty|DOCU:DocuSign|DT:Dynatrace|NET:Cloudflare|"
        "NOW:ServiceNow|DDOG:Datadog|ZS:Zscaler|"
        "SNOW:Snowflake|MDB:MongoDB|HUBS:HubSpot"
    ),
}

# Parse the pipe-delimited format into [symbol, name] lists
DEFAULT_HOLDINGS: dict[str, list[list[str]]] = {}
for _etf, _raw_str in _RAW.items():
    DEFAULT_HOLDINGS[_etf] = [
        [pair.split(":")[0], pair.split(":")[1]]
        for pair in _raw_str.split("|")
        if ":" in pair
    ]
