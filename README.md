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

## Strat Targets (60m / D / W / M)
`python targets_scan.py [--asset crypto] [--timeframes 60,D,W,M]` — a Python port of the Pine indicator
"TheStrat Magnitude + Targets (MTF)" (`strat_targets.py`). For every ticker and timeframe it reports the
signal the indicator would be **holding** right now: pattern, trigger, target ladder (open / taken /
reclaimed), status (IN-FORCE / STILL GOOD / RETRACEMENT / POTENTIAL 3) and the chop counter. `fresh` rows are
signals adopted on the latest bar.
Runs weekdays 22:15 UTC (`scan-targets.yml`) and daily 01:45 UTC for crypto (`scan-targets-crypto.yml`);
publishes `results/latest_targets.json` and `results/latest_targets_crypto.json`.

### Telegram "solid setups" (`telegram_alerts.py`)
Runs after the stock Targets scan and sends a short list instead of every signal. A setup must be Daily/Weekly,
fresh (adopted on the last closed bar), IN-FORCE, not choppy, with the current D, W and M candles all in its
direction, no target touched yet and at least 3 open, T1 room >= 1.0% and price >= $10. Ranked by room to T1
(+1 pizazz), top 8, each signal sent once (`results/telegram_sent.json`).
A second section, **Active setups**, reproduces the dashboard tab preset (reclaimed today, FTFC, R:R >= 2, room >= 10%, move done < 1/3, latest per ticker, Daily/Weekly, never capped) from `results/latest.json`; it matches the tab's count (19 on 2026-10-29).
A third section, **Weekly reversals**, reproduces the Strat Targets tab preset (FTFC D/W/M, Weekly, In force or Potential 3, Reversal, Pizazz, no target touched; held signals, forming weekly bar allowed; never capped).
Setup: create a bot with @BotFather, message it once, get your chat id, then add repo secrets
`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`. Without them the step skips. Preview: `python telegram_alerts.py --dry-run`.
The filter is a judgment call, not a backtested edge.

- Universe = the tickers already in `results/latest.json` / `latest_crypto.json` (no Polygon key needed).
- Data = Yahoo (yfinance): ~3y daily, ~60d hourly. Closed bars only; the last W/M bar may be forming (`partial`).
- The run aborts rather than publish if too few symbols return data or the newest bar is stale.
- Held-signal state is replayed bar by bar, so results depend on history; chart timeframe is assumed equal to the
  signal timeframe. See the `strat_targets.py` docstring for fidelity notes.
