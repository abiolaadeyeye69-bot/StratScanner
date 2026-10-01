"""
Crypto candidate registry.

The stock scan derives its universe from Polygon's grouped-daily endpoint,
which returns every US ticker in one call.  There is no equivalent free
"all coins" call for crypto, so the crypto scan starts from this curated
candidate list and then applies the SAME liquidity rule the stock scan
uses (rank by 20-day average dollar volume, drop anything under a floor,
keep the top N).  The list is a pool to rank, not the final universe: a
candidate that is dead, illiquid or has no Yahoo data is dropped
automatically on each run and shows up in the output's
`skipped_tickers`.

Symbols
-------
Yahoo Finance names most coins `<TICKER>-USD` (BTC-USD, SOL-USD) but, where
a ticker collides with another listing, appends a numeric id
(UNI7083-USD is Uniswap; plain UNI-USD is a different asset).  Every
`yahoo` value below was copied from Yahoo's own crypto listing
(finance.yahoo.com/markets/crypto/all, top 200 by market cap, checked
2026-10-01), NOT typed from memory.  `ticker` is the clean name the
dashboard and JSON use; `yahoo` is only used to download data.

What is deliberately NOT here
-----------------------------
Stablecoins (a flat peg prints a permanent inside bar), wrapped / staked /
bridged / restaked copies of another asset (they just duplicate the
underlying's chart), gold-pegged tokens, and entries whose market cap is
wildly out of line with their traded volume.

Maintenance
-----------
Numeric ids on Yahoo symbols can change if Yahoo re-lists an asset.  A
stale symbol fails soft (no data -> listed in skipped_tickers), it does
not produce wrong data.  Add coins without editing this file via the
CRYPTO_EXTRA_SYMBOLS env var (comma-separated Yahoo symbols).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class CryptoAsset:
    ticker: str   # clean display / JSON key, e.g. "UNI-USD"
    yahoo: str    # Yahoo Finance symbol used for download, e.g. "UNI7083-USD"
    name: str


def _a(ticker: str, name: str, yahoo: str | None = None) -> CryptoAsset:
    return CryptoAsset(ticker=ticker, yahoo=yahoo or ticker, name=name)


CRYPTO_CANDIDATES: list[CryptoAsset] = [
    # --- Majors ---
    _a("BTC-USD", "Bitcoin"),
    _a("ETH-USD", "Ethereum"),
    _a("BNB-USD", "BNB"),
    _a("XRP-USD", "XRP"),
    _a("SOL-USD", "Solana"),
    _a("TRX-USD", "TRON"),
    _a("DOGE-USD", "Dogecoin"),
    _a("ADA-USD", "Cardano"),
    _a("HYPE-USD", "Hyperliquid", "HYPE32196-USD"),
    _a("LINK-USD", "Chainlink"),
    _a("XLM-USD", "Stellar"),
    _a("BCH-USD", "Bitcoin Cash"),
    _a("LTC-USD", "Litecoin"),
    _a("AVAX-USD", "Avalanche"),
    _a("HBAR-USD", "Hedera"),
    _a("SHIB-USD", "Shiba Inu"),
    _a("TON-USD", "Toncoin", "TON11419-USD"),
    _a("SUI-USD", "Sui", "SUI20947-USD"),
    _a("DOT-USD", "Polkadot"),
    _a("UNI-USD", "Uniswap", "UNI7083-USD"),
    _a("NEAR-USD", "NEAR Protocol"),
    _a("XMR-USD", "Monero"),
    _a("ZEC-USD", "Zcash"),
    _a("AAVE-USD", "Aave"),
    _a("ETC-USD", "Ethereum Classic"),
    _a("ICP-USD", "Internet Computer"),
    _a("ALGO-USD", "Algorand"),
    _a("ATOM-USD", "Cosmos"),
    _a("FIL-USD", "Filecoin"),
    _a("VET-USD", "VeChain"),
    _a("DASH-USD", "Dash"),
    _a("XTZ-USD", "Tezos"),
    _a("BSV-USD", "Bitcoin SV"),
    _a("KAS-USD", "Kaspa"),
    _a("XDC-USD", "XDC Network"),
    _a("FLR-USD", "Flare"),
    _a("LUNC-USD", "Terra Classic"),
    _a("CC-USD", "Canton", "CC37263-USD"),
    # --- Exchange tokens ---
    _a("OKB-USD", "OKB"),
    _a("CRO-USD", "Cronos"),
    _a("HTX-USD", "HTX"),
    _a("BGB-USD", "Bitget Token"),
    _a("KCS-USD", "KuCoin Token"),
    _a("GT-USD", "GateToken"),
    _a("NEXO-USD", "Nexo"),
    # --- L1 / L2 / infrastructure ---
    _a("TAO-USD", "Bittensor", "TAO22974-USD"),
    _a("MNT-USD", "Mantle", "MNT27075-USD"),
    _a("POL-USD", "Polygon (prev. MATIC)", "POL28321-USD"),
    _a("ARB-USD", "Arbitrum", "ARB11841-USD"),
    _a("OP-USD", "Optimism"),
    _a("APT-USD", "Aptos", "APT21794-USD"),
    _a("SEI-USD", "Sei"),
    _a("TIA-USD", "Celestia"),
    _a("INJ-USD", "Injective"),
    _a("STX-USD", "Stacks", "STX4847-USD"),
    _a("IMX-USD", "Immutable", "IMX10603-USD"),
    _a("STRK-USD", "Starknet", "STRK22691-USD"),
    _a("MON-USD", "Monad", "MON30495-USD"),
    _a("XPL-USD", "Plasma"),
    _a("CFX-USD", "Conflux"),
    _a("AR-USD", "Arweave"),
    _a("PI-USD", "Pi", "PI35697-USD"),
    _a("ZRO-USD", "LayerZero", "ZRO26997-USD"),
    _a("NIGHT-USD", "Midnight", "NIGHT39064-USD"),
    # --- DeFi ---
    _a("ENA-USD", "Ethena"),
    _a("ONDO-USD", "Ondo"),
    _a("SKY-USD", "Sky", "SKY33038-USD"),
    _a("ASTER-USD", "Aster", "ASTER36341-USD"),
    _a("WLFI-USD", "World Liberty Financial", "WLFI33251-USD"),
    _a("MORPHO-USD", "Morpho", "MORPHO34104-USD"),
    _a("LIT-USD", "Lighter", "LIT39125-USD"),
    _a("CAKE-USD", "PancakeSwap"),
    _a("AERO-USD", "Aerodrome Finance", "AERO29270-USD"),
    _a("ETHFI-USD", "ether.fi"),
    _a("RAY-USD", "Raydium"),
    _a("CRV-USD", "Curve DAO"),
    _a("PENDLE-USD", "Pendle"),
    _a("LDO-USD", "Lido DAO"),
    _a("JTO-USD", "Jito"),
    _a("SYRUP-USD", "Maple Finance"),
    _a("ENS-USD", "Ethereum Name Service"),
    _a("GRT-USD", "The Graph", "GRT6719-USD"),
    _a("PYTH-USD", "Pyth Network"),
    _a("QNT-USD", "Quant"),
    _a("JST-USD", "JUST"),
    _a("SUN-USD", "Sun [New]"),
    # --- AI / data / compute ---
    _a("RENDER-USD", "Render"),
    _a("FET-USD", "Artificial Superintelligence Alliance"),
    _a("VIRTUAL-USD", "Virtuals Protocol"),
    _a("WLD-USD", "Worldcoin"),
    # --- Memecoins / launchpads ---
    _a("PEPE-USD", "Pepe", "PEPE24478-USD"),
    _a("PUMP-USD", "Pump.fun", "PUMP36507-USD"),
    _a("PENGU-USD", "Pudgy Penguins", "PENGU34466-USD"),
    _a("TRUMP-USD", "OFFICIAL TRUMP", "TRUMP35336-USD"),
    _a("BONK-USD", "Bonk"),
    _a("FLOKI-USD", "FLOKI"),
    _a("SPX-USD", "SPX6900", "SPX28081-USD"),
    _a("BTT-USD", "BitTorrent [New]"),
]

# Bases that must never be candidates (stable, wrapped, staked, pegged).
# Used by the test-suite as a guard against someone pasting Yahoo's raw
# listing in here.
EXCLUDED_BASES: frozenset[str] = frozenset({
    "USDT", "USDC", "USDS", "DAI", "USDE", "USDT0", "USD1", "USDG", "PYUSD",
    "RLUSD", "USDY", "SUSDE", "FDUSD", "TUSD", "EURC", "USDD", "BFUSD",
    "GHO", "USD0", "XAUT", "PAXG",
    "STETH", "WSTETH", "WBTC", "WBETH", "WETH", "CBBTC", "BTCB", "WEETH",
    "CBETH", "RETH", "RSETH", "WBNB", "WTRX", "WHYPE", "JITOSOL", "BNSOL",
    "MSOL", "JLP",
})


def get_candidates(extra_yahoo_symbols: Iterable[str] = ()) -> list[CryptoAsset]:
    """Built-in candidates plus any user-supplied Yahoo symbols.

    Extras keep their Yahoo symbol as the ticker.  Duplicates (by ticker or
    by Yahoo symbol) are ignored so an extra can never shadow a built-in.
    """
    out = list(CRYPTO_CANDIDATES)
    seen_tickers = {a.ticker for a in out}
    seen_yahoo = {a.yahoo for a in out}
    for raw in extra_yahoo_symbols:
        sym = (raw or "").strip().upper()
        if not sym or sym in seen_tickers or sym in seen_yahoo:
            continue
        out.append(CryptoAsset(ticker=sym, yahoo=sym, name=sym))
        seen_tickers.add(sym)
        seen_yahoo.add(sym)
    return out
