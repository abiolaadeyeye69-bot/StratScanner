# StratScanner
A specially designed Strat scanner.

## Stocks
`python scanner.py` — Polygon.io daily bars, top ~1,200 US tickers by dollar volume.
Runs weekdays 21:30 UTC (`.github/workflows/scan.yml`), publishes `results/latest.json`.

## Crypto
`python scanner.py --asset crypto` — same STRAT / SFP / Broadening-Formation detectors and the
same JSON schema, fed by Yahoo Finance daily bars (no API key).
Runs every day 01:15 UTC (`.github/workflows/scan-crypto.yml`), publishes `results/latest_crypto.json`.

- Candidates are a curated list (`crypto_universe.py`) ranked by 20-day average dollar volume;
  stablecoins and wrapped/staked tokens are excluded. Add coins with `CRYPTO_EXTRA_SYMBOLS=WIF-USD,...`.
- A "day" is a UTC day, 7 days a week. Only fully closed days are scanned (`--include-partial` to override).
- The run aborts rather than publish if BTC/ETH data is missing or stale, or too few coins return data.
- No sector rotation / GEX / VIX for crypto. Email is off by default (`--email` to enable).
