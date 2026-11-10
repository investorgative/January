# ❄ JANUARY — 4h structure · 15m execution (BTCUSDT · SOLUSDT, Bybit)

> 💜 **February** (January + streak defence) lives in [`february/`](february/README.md) as a separate app on port 8759.

Paper trader + dashboard + backtester + optimizer for a Raspberry Pi.
The paper trader and the backtester share one engine (`january_core.Engine`), so a
paper trade and a backtest trade come from exactly the same code. In testing, a
simulated 60-day paper run (with 11 restarts in between) produced the same trades
as the backtester did on the same candles.

## Files
| File | Purpose |
|---|---|
| `january_core.py` | data (Bybit + sqlite cache), 4h zones, 15m state machine, trailing stop, account replay, stats, journal, Kelly, Discord |
| `january_trader.py` | live paper trader, tmux session **January** |
| `january_web.py` | dashboard + API on port **8756**, tmux session **JanuaryWeb** |
| `january_backtest.py` | backtester + optimizer (used by the website, also works from the command line) |
| `static/index.html` | the dashboard |
| `config.json` | all settings |
| `setup.sh` / `start.sh` / `stop.sh` | install / start the 3 tmux sessions / stop |

## Setup on the Pi (Raspberry Pi OS Bookworm, 64-bit recommended)
```bash
cd ~
git clone https://github.com/investorgative/January.git january
cd january
chmod +x setup.sh && ./setup.sh        # python venv, packages, chart library, cloudflared
nano config.json                       # paste your Discord webhooks (see below), save with Ctrl+O, Enter, Ctrl+X
./start.sh                             # starts January, JanuaryWeb, JanuaryCloud
```
Dashboard on your network: `http://<pi-ip>:8756`. The public trycloudflare URL is printed by
`start.sh` and also shown in the dashboard header (chip "PUBLIC").

### Discord
In `config.json` → `"discord"`:
```json
"discord": {
  "BTCUSDT": "https://discord.com/api/webhooks/...   (BTC trades)",
  "SOLUSDT": "https://discord.com/api/webhooks/...   (SOL trades)",
  "errors":  "https://discord.com/api/webhooks/...   (every ERROR/CRITICAL from all processes)",
  "notify_start": true
}
```
Discord: Server settings → Integrations → Webhooks → New webhook → copy URL. One per channel.
Trade messages show open (entry, SL, size for each account), every stop trail, and close (PnL, R, fees per account).
Errors are deduplicated: the same message is posted at most once every 5 minutes.

### Daily use
```bash
tmux attach -t January        # paper trader      (leave with Ctrl+B, then D)
tmux attach -t JanuaryWeb     # web server
tmux attach -t JanuaryCloud   # cloudflare tunnel (shows the public URL)
./stop.sh && ./start.sh       # restart everything
git pull && ./stop.sh && ./start.sh                  # update to the newest version
./stop.sh && ./venv/bin/python january_trader.py --reset && ./start.sh   # wipe the paper account
```
Autostart after reboot: `crontab -e`, add `@reboot sleep 30 && /home/<user>/january/start.sh`.

Note: a quick tunnel gets a **new random URL** every time `JanuaryCloud` restarts. The current URL is
always in the dashboard header and in `data/logs/cloudflared.log`.
Because the page is public, you can set `"web": {"password": "something"}`. The browser then asks
for a login (any username, that password).

### Debug bundle (send your data to Claude)
```bash
cd ~/january && git pull
python3 debug_bundle.py --serve --notes "what felt off"
```
This creates `~/january_debug_<date>.zip` with config (webhooks/password removed), trades, state, logs, the last
backtest/optimizer and the candle database, plus system info. With `--serve` it prints a link
(`http://<pi-ip>:8799/...`) that you open on your phone or iPad (same WiFi) to download the zip, then upload it in the chat.
Stop the download server with Ctrl+C.

### Command line (optional)
```bash
./venv/bin/python january_backtest.py --days 365 --risk 5 --balance 100
./venv/bin/python january_backtest.py --optimize --days 365 --combos 150
```

## Accounts & sizing
* **BTC account $100**, **SOL account $100**, **Combined account $100** (BTC + SOL share one balance).
* Risk per trade default **5 %** of the realized balance = −1 R **including fees**:
  `qty = risk$ / (|entry − SL| + taker × (entry + SL))`. Quantity is rounded down to the Bybit step
  (BTC 0.001, SOL 0.1). If the rounded size is below Bybit's minimum, the trade is shown as *skipped*
  for that account.
* Combined: the risk is taken from the realized balance at entry, so two setups opened while the
  balance hasn't changed carry **the same $ risk**.
* Changing the risk on the website re-sizes **every stored trade instantly**: the paper history, open
  positions and the backtest. Trades are stored size-independently and replayed with the
  current risk.
* **Kelly**: full and ½-Kelly are always shown. The "½-KELLY x %" chip in the header applies it as
  your risk. In the backtester you can switch sizing to rolling ½-Kelly, which only uses trades
  that had already closed at the time of each entry (min. 20 trades, 0.25 %–10 % clamp).
* Max leverage cap 50× (`account.max_leverage`).

## Fees
Bybit non-VIP: taker 0.055 %, maker 0.020 %. Entry is a market order and the stop is a
stop-market order, so **both sides pay taker**. There is no take-profit; the trailing stop
closes every trade. Funding is charged every 8 h while a position is open: the backtest uses
Bybit's funding history, the paper trader uses the live rate.

## Strategy as implemented (long; short is the exact mirror)
**4h zones** (rebuilt after every closed 4h candle, used from the next 15m bar on):
1. Pivot highs/lows (wicks) in the last `zone_lookback` 4h candles.
2. Wick extremes within `zone_max_width_atr` × ATR(4h) are clustered into one zone (top/bottom from the wicks).
3. A zone needs ≥ `zone_min_touches` (2) separate pivots, i.e. it acted as S/R at least twice. Highs and
   lows mix, so a zone can be support and resistance at once.
4. "Washed" levels (more than `zone_max_cross` closes straight through it) are dropped.
5. Zones must keep at least `zone_min_gap_atr` × ATR of free space between each other. When two zones
   overlap or sit too close, the stronger one wins.

**15m execution:**
1. **Range**: at least `range_min_bars` closes strictly between zone A and zone B.
2. **Breakout**: the first close above B's top (closes inside B before that are allowed).
3. **Anchor**: the last confirmed 15m swing low before the breakout (wick, pivot strength `swing_pivot`).
4. **Pullback**: at least 3 **consecutive** clear pullback candles (close < open and close < previous
   close). None of them may be a shooting star. A new high before the pullback restarts the count.
5. **Fib** from the anchor low to the swing high: wait for a wick touch of **61.8 %**.
6. **Close below 72.2 %** → the low was wrong: re-anchor to the swing low before it (repeat as needed).
   Price back in zone B (close, or touch if `invalidate = "touch"`) → setup cancelled. Setups also expire
   after `setup_max_bars`.
7. **Bullish engulfing** (body engulfs body) any time after the 61.8 % touch, as long as 6. hasn't
   happened. It doesn't count if the engulfed candle is a hammer or shooting star.
8. **Entry** at the open of the next candle. **SL** = anchor low − `sl_buffer_atr` × ATR(15m) as breathing room.
9. **Trailing**: the structure high starts as the swing high of the move. When a candle **closes** above it,
   the SL moves to the lowest low between that high and the breaking candle (minus the buffer).
   The next structure high is the next confirmed swing high (`trail_pivot`), and so on until the stop is hit.
   Stop checks are intrabar against the stop that was valid at the bar's open; gaps through the stop fill at the open.

Setups with a stop tighter than `min_sl_pct` (fees would dominate) or wider than `max_sl_pct` are not taken.

## No look-ahead
* Only closed candles are evaluated. 4h zones exist only after their 4h candle closed.
* Swing points need their right-hand bars closed before they can be used.
* Signals fill at the next bar's open. Trailing updates apply from the next bar.
* Tested: running the engine on truncated data reproduces exactly the same trades up to the cut.
* The paper trader ignores signals it sees more than 120 s after the candle closed (e.g. after a reboot).

## Strategy Lab (interpretation switches)
Every ambiguous rule has named variants. The defaults are the v1.0 behaviour, so nothing changes until you apply something.
| switch | variants |
|---|---|
| `zone_method` | `pivots` (4h swing wicks clustered) · `wick_density` (band where the most 4h wicks react) |
| `anchor_mode` ("the low") | `pivot` (last confirmed 15m swing low) · `leg_low` (lowest low of the breakout leg) · `range_low` (lowest low of the A-B range) |
| `pullback_mode` | `strict` (red, each close lower) · `color` (red only) · `lower_highs` (lower highs + lows, colour ignored) |
| `entry_window` | `after_touch` (engulfing anywhere after the 61.8 % touch) · `in_band` (engulfing must be in the 61.8-72.2 band) |
| `engulf_mode` | `body` · `close_beyond` (body engulfs and closes beyond the previous wick) |
| `exit_mode` | `trail` · `trail_zone_tp` (+ limit take-profit at the next zone C, maker fee) · `trail_rr_tp` (+ fixed `tp_r` target) |
| `invalidate` | `close` · `touch` |

Plus every number (`pullback_bars`, `fib_entry`, `fib_deep`, `trail_pivot` 0 = every candle, ...).
On the website (section **08 LAB**) pick variants, run them on the backtest period, and compare the runs
(R after fees, first half vs second half, BTC vs SOL). **VIEW** opens a run in the backtest view,
**EDIT** loads it back into the form, and **APPLY** sends it to the live paper trader, which rebuilds itself
and keeps open positions. The optimizer can also search the variants ("parameters + interpretation variants").
Backtests fall back to the cached candles if Bybit is briefly unreachable.

## Optimizer
Random search over `optimizer.space` (all parameters above). Each combination runs on both
instruments with fees and funding. It ranks by **net R on the first 70 %** of the period and
shows the last 30 % as an out-of-sample test. Prefer rows that are positive in **both** columns.
"Apply" writes the parameters to `config.json`, and the paper trader rebuilds itself automatically
(open positions are kept). Uses all CPU cores. On a Pi 4/5, 120 combinations × 365 days take
roughly 5–15 minutes.
