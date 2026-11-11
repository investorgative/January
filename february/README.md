# 💜 FEBRUARY — January's S/R breakout engine + streak defence

February is a separate app inside the January repo (`~/january/february`). It has its own paper
trader, dashboard (port **8753**), data, database, config and Discord webhooks. **January's files are not
touched**, and both can run side by side.

## Why January has long losing streaks (research on Bybit data, Nov 2024 – Nov 2026)
* **Most of the streak length is plain statistics.** January wins ~33 % of its trades. If you shuffle the
  same 131 trades randomly, the longest losing streak is ~10 in the median case; the real worst was 11.
  So the streaks are what a 33 % win-rate / big-winner system normally produces, not a broken strategy.
* **Losses don't predict losses.** After a run of losers, the next trades were *better* than average.
  A "pause after N losses" rule would have cost money.
* **The painful part is a regime.** From Nov 2024 to Nov 2025 the breakout edge was ~0 (the market was
  ranging and breakouts kept failing): **382 days under water**. Breakouts entered when 4h volatility was
  in the top third of the last month earned nothing; trades with the 4h trend did better than against it.
* **Ideas that did NOT work:**
  * Stop-and-reverse after a stop-out: −6 to −21 R.
  * Fading failed breakouts: about −0.1 R per trade over 1,000 tests.
  * 15m zone-bounce range trading: −0.16 R per trade.
  * Breakeven / partial take-profit: shorter streaks counted in trades, but they cut the big winners,
    so total R drops and time under water gets *longer*.

## What February does
| | January (live config) | **February** |
|---|---|---|
| pullback candles | 3 | **2** |
| exit | structure trail | structure trail **+ limit take-profit at the next zone C** (maker fee) |
| regime filter | – | **no new entries while 4h ATR > 75th percentile of the last 30 days** |
| trades (2 y) | 131 | 133 |
| total R (after fees) | +24.3 | **+47.8** |
| win rate | 33 % | **47 %** |
| max drawdown | 14.9 R | **10.2 R** |
| longest losing streak | 11 | **5** |
| first year / second year | −3.4 R / +27.6 R | **+17.3 R / +30.5 R** |
| BTC / SOL | +19.9 / +4.4 R | **+27.5 / +20.3 R** |
| Apr–Dec 2025 (the bad phase) | −8.8 R | ≈ −2 R (sideways instead of bleeding) |

The volatility threshold is robust: anything from 0.70 to 0.85 gives +40 to +48 R with a drawdown of 10–11 R.
Be honest with yourself about one thing: the zone parameters come from January's optimizer run on Nov 2025–Nov 2026,
and pb2 / zone-TP / vol-filter were chosen on these same two years. **Fresh data is the real test**: a backtest on
older data (your Pi has candles back to Feb 2024) and the live paper trading.

Optional switches (Strategy Lab and config): `trend_filter` (di / ema200 / mom / di_or_mom), `be_r` (breakeven),
`partial_r` + `partial_frac` (partial take-profit), and the **edge monitor** in the header. The edge monitor
shadow-trades every signal and only sizes real positions while the last N closed trades are net positive. All of
them are off by default because on this data they cost more R than they saved.

**Live:** see the execution section. With $8, Bybit's minimum order sizes, not the risk %, decide many trade sizes.

Mind the risk: with a 10 R drawdown, 5 % risk per trade means roughly a −40 % equity drawdown (8.25 % ≈ −55 to −60 %).

## Setup on the Pi
```bash
cd ~/january && git pull
cd february
chmod +x setup.sh && ./setup.sh      # own venv, copies January's candle database, chart library
nano config.json                     # Discord webhooks for February (BTCUSDT, SOLUSDT, errors) - can be new channels
./start.sh                           # tmux sessions February, FebruaryWeb, FebruaryCloud
```
Dashboard: `http://<pi-ip>:8753`. The public trycloudflare URL is printed by `start.sh` and shown in the header.
`./stop.sh` stops only February. January keeps running.

```bash
tmux attach -t February       # paper trader      (leave with Ctrl+B, then D)
tmux attach -t FebruaryWeb    # web server
./stop.sh && ./venv/bin/python february_trader.py --reset && ./start.sh   # wipe February's paper account
python3 debug_bundle.py --serve --notes "..."                              # data export for Claude
```

## Bybit execution: DEMO and LIVE
February's engine stays the master. Two independent executors can mirror its trades on Bybit:

| | DEMO | LIVE (real money) |
|---|---|---|
| host (hard-coded) | `api-demo.bybit.com` | `api.bybit.com` (bybit.com global, USDT perpetuals) |
| keys | created inside Demo Trading | bybit.com → API, **Contract: Orders + Positions only, never withdrawals** |
| config block | `"demo"` | `"live"` |
| arm | header DEMO → ARM + PIN | header LIVE → ARM LIVE + PIN + type `LIVE` |
| risk | website risk % (or `risk_pct`) | `risk_pct` (default 10 %) |

**LIVE defaults (sized for an ~$8 account):**
* 10 % risk per trade, 20× leverage cap.
* **Minimum size:** Bybit's minimum order is allowed when the risk-based size is smaller. On BTC that means 0.001 BTC (~$73), which is a ~$0.60–1.50 loss at the stop.
* **Hard cap of $1.50 loss per trade:** a BTC setup with a stop wider than ~2 % is skipped.
* **Daily loss stop of −$3.00:** no new entries for the rest of the day.
* **Equity floor of $3.00:** trading HALTS until you reset it with the PIN (RESET HALT button).
* **Free-margin check before every order.**
* **The stop is mandatory:** if Bybit rejects the stop-loss after the entry fill, the position is closed immediately.
* **FLATTEN** closes everything at market and disarms.

Config (`nano ~/january/february/config.json`):
```json
"live": {"api_key": "YOUR_KEY", "api_secret": "YOUR_SECRET", "arm_pin": "a PIN",
         "risk_pct": 10, "leverage": 20, "max_loss_per_trade": 1.5, "max_daily_loss": 3, "min_equity": 3},
"demo": {"api_key": "DEMO_KEY", "api_secret": "DEMO_SECRET", "arm_pin": "another PIN"}
```
Every order is mirrored as follows:
* **Entry:** market order, then a stop-loss on the position, then reduce-only limit take-profit at zone C (and partial, if on).
* **Trailing:** the stop moves on Bybit.
* **Engine close:** the rest is closed and orders cancelled.
* **Reconcile:** every 60 s.

While armed, don't trade BTCUSDT/SOLUSDT by hand on that account: positions the engine doesn't hold get closed.
Every action is logged on the dashboard, and LIVE entries and errors also go to Discord.

## Verified
* No look-ahead: truncated-data replays give identical trades. All regime numbers come from closed 4h candles only.
* Executors (demo + live): tested end-to-end against a signature-checking mock of the Bybit v5 API (60 simulated days, armed):
  every request correctly signed, 5 % risk sizing, stops/trails/take-profits/partials/closes mirrored.
* Paper trader == backtester: a 60-day accelerated live simulation on real candles (with breakeven and partials
  switched on) produced exactly the same 9 trades, exits and partial fills as the backtester.
* Two fixes compared to January: the warm-up keeps a breakout setup that is in progress at startup (January drops it,
  which can miss one trade right after a restart), and the warm-up loads enough 4h history for the regime filter.
