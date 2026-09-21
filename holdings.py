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
    # ── SECTORS ──────────────────────────────────────────────────
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
        "TTD:Trade Desk|RDDT:Reddit"
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
        "PLD:Prologis|AMT:American Tower|EQIX:Equinix|WELL:Welltower|"
        "SPG:Simon Property|DLR:Digital Realty|PSA:Public Storage|"
        "O:Realty Income|CCI:Crown Castle|CBRE:CBRE Group|"
        "IRM:Iron Mountain|EXR:Extra Space|VTR:Ventas|"
        "ESS:Essex Property|HST:Host Hotels"
    ),

    # ── SUBSECTORS ───────────────────────────────────────────────
    "SMH": (
        "NVDA:NVIDIA|AMD:AMD|AVGO:Broadcom|MU:Micron|INTC:Intel|"
        "QCOM:Qualcomm|MRVL:Marvell|ADI:Analog Devices|"
        "TXN:Texas Instruments|AMAT:Applied Materials|LRCX:Lam Research|"
        "KLAC:KLA Corp|CDNS:Cadence|SNPS:Synopsys|TER:Teradyne|"
        "MPWR:Monolithic Power|NXPI:NXP Semi|ARM:ARM Holdings|"
        "ALAB:Astera Labs|MCHP:Microchip|ON:ON Semi|SWKS:Skyworks"
    ),
    "SOXX": (
        "NVDA:NVIDIA|AMD:AMD|AVGO:Broadcom|MU:Micron|INTC:Intel|"
        "QCOM:Qualcomm|MRVL:Marvell|ADI:Analog Devices|"
        "TXN:Texas Instruments|AMAT:Applied Materials|LRCX:Lam Research|"
        "KLAC:KLA Corp|CDNS:Cadence|SNPS:Synopsys|TER:Teradyne|"
        "MPWR:Monolithic Power|NXPI:NXP Semi|ARM:ARM Holdings|"
        "MCHP:Microchip|ON:ON Semi|SWKS:Skyworks"
    ),
    "IGV": (
        "MSFT:Microsoft|CRM:Salesforce|ORCL:Oracle|ADBE:Adobe|"
        "NOW:ServiceNow|INTU:Intuit|SNPS:Synopsys|CDNS:Cadence|"
        "PANW:Palo Alto|CRWD:CrowdStrike|WDAY:Workday|HUBS:HubSpot|"
        "ZS:Zscaler|DDOG:Datadog|FTNT:Fortinet|TTD:Trade Desk"
    ),
    "SKYY": (
        "AMZN:Amazon|MSFT:Microsoft|GOOGL:Alphabet|ORCL:Oracle|"
        "CRM:Salesforce|SNOW:Snowflake|NET:Cloudflare|DDOG:Datadog|"
        "MDB:MongoDB|ZS:Zscaler"
    ),
    "HACK": (
        "PANW:Palo Alto|CRWD:CrowdStrike|FTNT:Fortinet|ZS:Zscaler|"
        "CSCO:Cisco|OKTA:Okta|GEN:Gen Digital"
    ),
    "CIBR": (
        "PANW:Palo Alto|CRWD:CrowdStrike|FTNT:Fortinet|ZS:Zscaler|"
        "CSCO:Cisco|AVGO:Broadcom|PLTR:Palantir"
    ),
    "FINX": (
        "V:Visa|MA:Mastercard|PYPL:PayPal|SQ:Block|COIN:Coinbase|"
        "AFRM:Affirm|HOOD:Robinhood|GPN:Global Payments|"
        "FIS:Fidelity Natl|FI:Fiserv"
    ),
    "KRE": (
        "FITB:Fifth Third|KEY:KeyCorp|RF:Regions Financial|"
        "HBAN:Huntington|CFG:Citizens Financial|MTB:M&T Bank|TFC:Truist"
    ),
    "KBE": (
        "JPM:JPMorgan|BAC:Bank of America|WFC:Wells Fargo|"
        "GS:Goldman Sachs|MS:Morgan Stanley|C:Citigroup|SCHW:Schwab|"
        "COF:Capital One|USB:US Bancorp|PNC:PNC|BX:Blackstone|"
        "FITB:Fifth Third|KEY:KeyCorp|RF:Regions|HBAN:Huntington|"
        "CFG:Citizens|MTB:M&T Bank|ALLY:Ally Financial"
    ),
    "KIE": (
        "PGR:Progressive|CB:Chubb|TRV:Travelers|ALL:Allstate|AIG:AIG|"
        "MET:MetLife|AFL:Aflac|HIG:Hartford|WRB:Berkley"
    ),
    "IAI": (
        "GS:Goldman Sachs|MS:Morgan Stanley|SCHW:Schwab|BX:Blackstone|"
        "IBKR:Interactive Brokers|RJF:Raymond James|LPLA:LPL Financial|"
        "HOOD:Robinhood"
    ),
    "XBI": (
        "MRNA:Moderna|VRTX:Vertex|REGN:Regeneron|ALNY:Alnylam|"
        "INCY:Incyte|HALO:Halozyme|IONS:Ionis|NBIX:Neurocrine|"
        "CORT:Corcept"
    ),
    "IBB": (
        "AMGN:Amgen|GILD:Gilead|VRTX:Vertex|REGN:Regeneron|"
        "BIIB:Biogen|MRNA:Moderna|ALNY:Alnylam|ILMN:Illumina|"
        "IDXX:IDEXX|DXCM:DexCom|ALGN:Align Tech|ZBH:Zimmer Biomet"
    ),
    "IHI": (
        "ISRG:Intuitive Surgical|BSX:Boston Scientific|SYK:Stryker|"
        "ABT:Abbott|MDT:Medtronic|DHR:Danaher|ZBH:Zimmer Biomet|"
        "BDX:Becton Dickinson|ALGN:Align Tech|DXCM:DexCom|IDXX:IDEXX|"
        "EW:Edwards Life"
    ),
    "XPH": (
        "LLY:Eli Lilly|JNJ:Johnson & Johnson|MRK:Merck|PFE:Pfizer|"
        "BMY:Bristol Myers|ABBV:AbbVie|VRTX:Vertex"
    ),
    "IHF": (
        "UNH:UnitedHealth|CI:Cigna|ELV:Elevance|HCA:HCA Healthcare|"
        "MCK:McKesson|CAH:Cardinal Health"
    ),
    "XOP": (
        "COP:ConocoPhillips|EOG:EOG Resources|DVN:Devon Energy|"
        "FANG:Diamondback|OXY:Occidental|MPC:Marathon Petroleum|"
        "VLO:Valero|PSX:Phillips 66|EQT:EQT Corp|APA:APA Corp|"
        "OVV:Ovintiv|SM:SM Energy|PR:Permian Resources"
    ),
    "OIH": "SLB:SLB|BKR:Baker Hughes|HAL:Halliburton|FTI:TechnipFMC",
    "AMLP": (
        "WMB:Williams|KMI:Kinder Morgan|TRGP:Targa|OKE:ONEOK|"
        "ET:Energy Transfer"
    ),
    "XRT": (
        "AMZN:Amazon|COST:Costco|WMT:Walmart|TJX:TJX Cos|"
        "ROST:Ross Stores|DG:Dollar General|DLTR:Dollar Tree|"
        "FIVE:Five Below|BBY:Best Buy|M:Macy's|BURL:Burlington|"
        "WSM:Williams-Sonoma|W:Wayfair|ETSY:Etsy|EBAY:eBay"
    ),
    "XHB": (
        "HD:Home Depot|LOW:Lowe's|DHI:D.R. Horton|LEN:Lennar|NVR:NVR|"
        "PHM:PulteGroup|BLDR:Builders First|MAS:Masco|"
        "SWK:Stanley Black|OC:Owens Corning"
    ),
    "ITB": (
        "DHI:D.R. Horton|LEN:Lennar|NVR:NVR|PHM:PulteGroup|"
        "BLDR:Builders First"
    ),
    "PEJ": (
        "BKNG:Booking|LYV:Live Nation|DPZ:Domino's|YUM:Yum Brands|"
        "CMG:Chipotle|DIS:Disney|NCLH:Norwegian Cruise|"
        "RCL:Royal Caribbean|CCL:Carnival|MAR:Marriott|HLT:Hilton"
    ),
    "ITA": (
        "RTX:RTX Corp|LMT:Lockheed Martin|GD:General Dynamics|"
        "BA:Boeing|GE:GE Aerospace|NOC:Northrop Grumman|HWM:Howmet|"
        "LHX:L3Harris|TXT:Textron|HEI:Heico|TDG:TransDigm|"
        "LDOS:Leidos|KTOS:Kratos"
    ),
    "PPA": (
        "RTX:RTX Corp|LMT:Lockheed Martin|GD:General Dynamics|"
        "BA:Boeing|NOC:Northrop|GE:GE Aerospace|HWM:Howmet|"
        "LHX:L3Harris|TDG:TransDigm|HEI:Heico"
    ),
    "IYT": (
        "UNP:Union Pacific|CSX:CSX Corp|NSC:Norfolk Southern|UPS:UPS|"
        "FDX:FedEx|JBHT:J.B. Hunt|ODFL:Old Dominion|"
        "XPO:XPO Logistics|DAL:Delta Air|UAL:United Airlines"
    ),
    "JETS": "DAL:Delta Air|UAL:United Airlines|LUV:Southwest",
    "GDX": (
        "NEM:Newmont|AEM:Agnico Eagle|AGI:Alamos Gold|KGC:Kinross|"
        "AU:AngloGold|BTG:B2Gold|HL:Hecla Mining|EGO:Eldorado Gold|"
        "PAAS:Pan American Silver"
    ),
    "GDXJ": (
        "AGI:Alamos Gold|KGC:Kinross|HL:Hecla Mining|"
        "PAAS:Pan American|BTG:B2Gold|AU:AngloGold|EGO:Eldorado"
    ),
    "SIL": (
        "PAAS:Pan American Silver|AG:First Majestic|HL:Hecla Mining|"
        "EGO:Eldorado|CDE:Coeur Mining"
    ),
    "COPX": "FCX:Freeport-McMoRan|SCCO:Southern Copper|TECK:Teck Resources",
    "SLX": "NUE:Nucor|STLD:Steel Dynamics|CLF:Cleveland-Cliffs",
    "REMX": "ALB:Albemarle|MP:MP Materials",
    "REM": "AGNC:AGNC Investment|NLY:Annaly Capital",
    "MORT": "AGNC:AGNC Investment|NLY:Annaly Capital",
    "REZ": (
        "EQIX:Equinix|WELL:Welltower|DLR:Digital Realty|"
        "PSA:Public Storage|EXR:Extra Space|IRM:Iron Mountain|"
        "O:Realty Income|SPG:Simon Property"
    ),

    # ── THEMATIC ─────────────────────────────────────────────────
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
        "NVDA:NVIDIA|ISRG:Intuitive Surgical|INTC:Intel|TER:Teradyne|"
        "MRVL:Marvell"
    ),
    "ROBO": "ISRG:Intuitive Surgical|TER:Teradyne|NVDA:NVIDIA|INTC:Intel",
    "AIQ": (
        "NVDA:NVIDIA|MSFT:Microsoft|GOOGL:Alphabet|META:Meta|"
        "CRM:Salesforce|AMZN:Amazon|PLTR:Palantir|SNOW:Snowflake|"
        "DDOG:Datadog"
    ),
    "ICLN": "ENPH:Enphase|FSLR:First Solar|BE:Bloom Energy",
    "TAN": "ENPH:Enphase|FSLR:First Solar",
    "PBW": "ENPH:Enphase|FSLR:First Solar|BE:Bloom Energy",
    "URA": "CCJ:Cameco",
    "LIT": "ALB:Albemarle",
    "BLOK": (
        "COIN:Coinbase|HOOD:Robinhood|MARA:Marathon Digital|"
        "RIOT:Riot Platforms|MSTR:MicroStrategy"
    ),
    "KWEB": (
        "BABA:Alibaba|PDD:PDD Holdings|JD:JD.com|BIDU:Baidu|"
        "TCOM:Trip.com"
    ),
    "MJ": "TLRY:Tilray|CGC:Canopy Growth|ACB:Aurora Cannabis",
    "UFO": (
        "GOOG:Alphabet|LMT:Lockheed Martin|RTX:RTX Corp|"
        "NOC:Northrop|BA:Boeing|RKLB:Rocket Lab"
    ),
    "WCLD": (
        "DDOG:Datadog|ZS:Zscaler|CRWD:CrowdStrike|NET:Cloudflare|"
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
