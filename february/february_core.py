#!/usr/bin/env python3
"""
FEBRUARY - core engine (January's S/R breakout engine + streak defence) shared by the paper trader,
the backtester and the optimizer.

February = January's best interpretation (2 pullback candles, take-profit at the next zone C,
structure trail) + a 4h volatility regime filter, plus optional trend filter, breakeven, partial
take-profit and an "edge monitor" (shadow trading) - all switchable in the Strategy Lab.

  * Bybit v5 public data (no API keys) + sqlite candle / funding cache
  * 4h support/resistance zones built from wicks (only CLOSED 4h candles)
  * 15m state machine: range -> breakout -> 3-candle pullback -> 61.8% touch
    (72.2% re-anchor, zone invalidation) -> engulfing -> entry next open
  * structure trailing stop
  * size-independent trade records + account replay (BTC / SOL / combined)
  * stats, journal, Kelly, Discord (trade + error webhooks)

Paper trader and backtester call exactly the same Engine.step() code path,
so a backtest trade and a paper trade are produced by identical logic.
"""
import copy
import json
import logging
import math
import os
import sqlite3
import threading
import time
import traceback
from collections import deque
from datetime import datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

import numpy as np
import requests

VERSION = "2.0.0"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
LOG_DIR = os.path.join(DATA_DIR, "logs")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
SETTINGS_PATH = os.path.join(DATA_DIR, "settings.json")
STATE_PATH = os.path.join(DATA_DIR, "state.json")
TRADES_PATH = os.path.join(DATA_DIR, "trades.json")
DB_PATH = os.path.join(DATA_DIR, "february.db")
M15 = 15 * 60 * 1000
H4 = 4 * 60 * 60 * 1000
DAY = 24 * 60 * 60 * 1000
os.makedirs(LOG_DIR, exist_ok=True)

# ---------------------------------------------------------------- defaults
DEFAULT_CONFIG = {
    "symbols": ["BTCUSDT", "SOLUSDT"],
    "category": "linear",
    "api_base": "https://api.bybit.com",
    "timezone": "Europe/Berlin",
    "account": {
        "start_balance": 100.0,
        "risk_pct": 5.0,
        "max_leverage": 50,
        "kelly_sizing": False,
        "kelly_fraction": 0.5,
        "kelly_min_trades": 20,
        "kelly_max_risk_pct": 10.0,
        "edge_n": 0,                  # edge monitor: last N closed trades (0 = off)
        "edge_low": 0.0,              # size multiplier while the last N trades are <= 0 R (0 = shadow only)
    },
    "fees": {"maker": 0.0002, "taker": 0.00055, "include_funding": True},
    "instruments": {
        "BTCUSDT": {"qty_step": 0.001, "min_qty": 0.001, "min_notional": 5.0, "tick": 0.1},
        "SOLUSDT": {"qty_step": 0.1, "min_qty": 0.1, "min_notional": 5.0, "tick": 0.01},
    },
    "strategy": {
        "zone_lookback": 180,       # 4h candles the zone finder looks back (January optimizer result)
        "zone_pivot": 3,            # 4h pivot left/right bars (wick extremes)
        "zone_max_width_atr": 0.4,  # max zone thickness in 4h ATR
        "zone_min_width_atr": 0.08, # zones thinner than this are padded
        "zone_min_touches": 2,      # zone must have acted as S/R at least twice
        "zone_min_gap_atr": 0.5,    # min free space between two zones (4h ATR)
        "zone_max_cross": 4,        # "washed" filter: max closes through the zone in the lookback
        "range_min_bars": 1,        # 15m closes that must be inside A-B before the break
        "swing_pivot": 3,           # 15m pivot strength for "the low before the move"
        "pullback_bars": 2,         # consecutive clear pullback candles (February: 2)
        "fib_entry": 0.618,
        "fib_deep": 0.722,
        "invalidate": "close",      # "close" = close back inside B, "touch" = wick touches B
        "setup_max_bars": 192,      # setup expires after N 15m bars (192 = 2 days)
        "sl_buffer_atr": 0.05,      # breathing room below the low, in 15m ATR
        "trail_pivot": 3,           # pivot strength for structure highs used for trailing
        "star_ratio": 1.5,          # hammer / shooting star: wick >= ratio * body
        "min_sl_pct": 0.1,          # skip setups with SL tighter than this (fees would dominate)
        "max_sl_pct": 6.0,
        # ---- interpretation switches (Strategy Lab). Defaults = original v1.0 behaviour.
        "zone_method": "pivots",      # pivots | wick_density
        "anchor_mode": "pivot",       # pivot | leg_low | range_low
        "pullback_mode": "strict",    # strict | color | lower_highs
        "entry_window": "after_touch",# after_touch | in_band
        "engulf_mode": "body",        # body | close_beyond
        "exit_mode": "trail_zone_tp", # trail | trail_zone_tp | trail_rr_tp   (February: zone TP)
        "tp_r": 3.0,                  # target for trail_rr_tp
        # ---- February streak defence
        "vol_max_pct": 0.75,          # skip entries when 4h ATR is above this percentile of the last 30 days (1 = off)
        "trend_filter": "off",        # off | di | ema200 | mom | di_or_mom
        "mom_days": 30,               # lookback for the momentum filter
        "be_r": 0.0,                  # move stop to breakeven after a CLOSE at +x R (0 = off)
        "partial_r": 0.0,             # take partial profit (limit, maker) at +x R (0 = off)
        "partial_frac": 0.5,          # fraction closed at partial_r
    },
    "optimizer": {
        "days": 365,
        "combos": 120,
        "train_frac": 0.7,
        "min_trades": 30,
        "workers": 0,
        "space": {
            "zone_lookback": [60, 90, 120, 180, 240],
            "zone_pivot": [2, 3],
            "zone_max_width_atr": [0.4, 0.6, 0.8],
            "zone_min_gap_atr": [0.5, 0.8, 1.2],
            "zone_max_cross": [4, 6, 10],
            "range_min_bars": [1, 4, 8],
            "swing_pivot": [1, 2, 3],
            "invalidate": ["close", "touch"],
            "setup_max_bars": [96, 192, 384],
            "sl_buffer_atr": [0.05, 0.1, 0.2],
            "trail_pivot": [1, 2, 3],
            "star_ratio": [1.5, 2.0, 2.5],
            "min_sl_pct": [0.1, 0.15, 0.25],
            "vol_max_pct": [0.7, 0.75, 0.8, 1.0],
        },
        "lab_space": {
            "zone_method": ["pivots", "wick_density"],
            "anchor_mode": ["pivot", "leg_low", "range_low"],
            "pullback_mode": ["strict", "color", "lower_highs"],
            "entry_window": ["after_touch", "in_band"],
            "engulf_mode": ["body", "close_beyond"],
            "exit_mode": ["trail", "trail_zone_tp", "trail_rr_tp"],
            "trend_filter": ["off", "di", "ema200", "mom", "di_or_mom"],
        },
        "include_lab_space": False,
    },
    "web": {"port": 8759, "host": "0.0.0.0", "password": ""},
    "discord": {
        "BTCUSDT": "",
        "SOLUSDT": "",
        "errors": "",
        "notify_start": True,
    },
    "backtest": {"default_days": 365},
}


def deep_merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k not in ("space", "lab_space"):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load_config():
    user = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            user = json.load(f)
    cfg = deep_merge(DEFAULT_CONFIG, user)
    if not os.path.exists(CONFIG_PATH):
        save_json(CONFIG_PATH, cfg)
    return cfg


def save_strategy_params(params):
    """Used by the optimizer 'apply' button: writes the strategy block into config.json."""
    user = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, encoding="utf-8") as f:
            user = json.load(f)
    st = user.get("strategy", {})
    st.update(params)
    user["strategy"] = st
    save_json(CONFIG_PATH, user)


def load_settings(cfg):
    """Website-editable settings (risk etc.). They only change sizing, never signals."""
    s = {"risk_pct": cfg["account"]["risk_pct"], "start_balance": cfg["account"]["start_balance"],
         "kelly_sizing": cfg["account"]["kelly_sizing"], "edge_n": cfg["account"].get("edge_n", 0),
         "edge_low": cfg["account"].get("edge_low", 0.0)}
    if os.path.exists(SETTINGS_PATH):
        try:
            with open(SETTINGS_PATH, encoding="utf-8") as f:
                s.update(json.load(f))
        except Exception:
            pass
    return s


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(clean_json(obj), f, indent=1)
    os.replace(tmp, path)


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def clean_json(o):
    if isinstance(o, dict):
        return {str(k): clean_json(v) for k, v in o.items()}
    if isinstance(o, (list, tuple, deque)):
        return [clean_json(v) for v in o]
    if isinstance(o, (np.floating, float)):
        f = float(o)
        return None if (math.isnan(f) or math.isinf(f)) else f
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.bool_):
        return bool(o)
    return o


def now_ms():
    return int(time.time() * 1000)


def tz(cfg):
    try:
        return ZoneInfo(cfg.get("timezone", "Europe/Berlin")) if ZoneInfo else timezone.utc
    except Exception:
        return timezone.utc


# ---------------------------------------------------------------- logging + discord
def _discord_post(url, payload):
    try:
        if isinstance(payload, str):
            payload = {"content": payload[:1990]}
        requests.post(url, json=payload, timeout=10)
    except Exception:
        pass


def discord_send(url, payload):
    if url:
        threading.Thread(target=_discord_post, args=(url, payload), daemon=True).start()


class DiscordHandler(logging.Handler):
    """Every ERROR/CRITICAL record goes to the error webhook (identical messages max once / 5 min)."""

    def __init__(self, url, source):
        super().__init__(level=logging.ERROR)
        self.url, self.source, self._recent, self._lock = url, source, {}, threading.Lock()

    def emit(self, record):
        if not self.url:
            return
        try:
            msg = record.getMessage()
            key = msg[:200]
            with self._lock:
                if time.time() - self._recent.get(key, 0) < 300:
                    return
                self._recent[key] = time.time()
            tb = "".join(traceback.format_exception(*record.exc_info)) if record.exc_info else ""
            body = msg + ("\n\n" + tb if tb else "")
            if len(body) > 1700:
                body = body[:500] + "\n...\n" + body[-1150:]
            discord_send(self.url, f"**❄ FEBRUARY {self.source} | {record.levelname}** "
                                   f"`{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`\n```{body}```")
        except Exception:
            pass


def setup_logging(source, cfg):
    log = logging.getLogger("february")
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s [" + source + "] %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = RotatingFileHandler(os.path.join(LOG_DIR, "february.log"), maxBytes=2_000_000, backupCount=3,
                             encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    log.addHandler(fh)
    log.addHandler(sh)
    log.addHandler(DiscordHandler(cfg["discord"].get("errors", ""), source))

    import sys

    def hook(t, v, tb):
        log.critical("uncaught exception", exc_info=(t, v, tb))

    sys.excepthook = hook
    return log


# ---------------------------------------------------------------- Bybit
class Bybit:
    def __init__(self, cfg):
        self.base = cfg["api_base"].rstrip("/")
        self.cat = cfg["category"]
        self.s = requests.Session()
        self.s.headers["User-Agent"] = f"February-paper/{VERSION}"

    def _get(self, path, params):
        err = None
        for attempt in range(4):
            try:
                r = self.s.get(self.base + path, params=params, timeout=15)
                r.raise_for_status()
                j = r.json()
                if j.get("retCode") != 0:
                    raise RuntimeError(f"retCode={j.get('retCode')} {j.get('retMsg')}")
                return j["result"]
            except Exception as e:
                err = e
                time.sleep(1.5 * (attempt + 1))
        raise RuntimeError(f"Bybit request failed {path} {params}: {err}")

    def klines(self, sym, interval="15", start=None, end=None, limit=1000):
        p = {"category": self.cat, "symbol": sym, "interval": interval, "limit": limit}
        if start is not None:
            p["start"] = int(start)
        if end is not None:
            p["end"] = int(end)
        rows = self._get("/v5/market/kline", p).get("list", [])
        out = [(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])) for r in rows]
        out.sort()
        return out

    def klines_range(self, sym, start, end, interval="15", progress=None):
        res, cur_end, total = {}, int(end), max(1, int(end) - int(start))
        while True:
            rows = self.klines(sym, interval, start=start, end=cur_end)
            if not rows:
                break
            for r in rows:
                res[r[0]] = r
            first = rows[0][0]
            if progress:
                progress(min(1.0, (end - first) / total))
            if first <= start or len(rows) < 1000:
                break
            cur_end = first - 1
            time.sleep(0.04)
        return [res[k] for k in sorted(res) if start <= k <= end]

    def ticker(self, sym):
        return self._get("/v5/market/tickers", {"category": self.cat, "symbol": sym})["list"][0]

    def funding_range(self, sym, start, end):
        res, cur_end = {}, int(end)
        while True:
            rows = self._get("/v5/market/funding/history", {"category": self.cat, "symbol": sym,
                                                             "startTime": int(start), "endTime": cur_end,
                                                             "limit": 200}).get("list", [])
            if not rows:
                break
            for r in rows:
                res[int(r["fundingRateTimestamp"])] = float(r["fundingRate"])
            oldest = min(int(r["fundingRateTimestamp"]) for r in rows)
            if oldest <= start or len(rows) < 200:
                break
            cur_end = oldest - 1
            time.sleep(0.04)
        return res


class Store:
    """sqlite cache of CLOSED 15m candles and funding rates (thread-safe via per-call connections)."""

    def __init__(self, api):
        self.api = api
        self.lock = threading.Lock()
        con = self._con()
        con.execute("CREATE TABLE IF NOT EXISTS candles (sym TEXT, ts INTEGER, o REAL, h REAL, l REAL, c REAL, "
                    "v REAL, PRIMARY KEY(sym, ts))")
        con.execute("CREATE TABLE IF NOT EXISTS funding (sym TEXT, ts INTEGER, rate REAL, PRIMARY KEY(sym, ts))")
        con.commit()
        con.close()

    def _con(self):
        con = sqlite3.connect(DB_PATH, timeout=30)
        con.execute("PRAGMA journal_mode=WAL")
        return con

    def insert(self, sym, rows):
        lim = now_ms()
        rows = [(sym,) + tuple(r) for r in rows if r[0] + M15 <= lim]  # closed candles only
        if rows:
            with self.lock:
                con = self._con()
                con.executemany("INSERT OR REPLACE INTO candles VALUES(?,?,?,?,?,?,?)", rows)
                con.commit()
                con.close()
        return len(rows)

    def bounds(self, sym):
        con = self._con()
        r = con.execute("SELECT MIN(ts), MAX(ts) FROM candles WHERE sym=?", (sym,)).fetchone()
        con.close()
        return r

    def ensure(self, sym, start, end, progress=None):
        end = min(int(end), now_ms())
        mn, mx = self.bounds(sym)
        segs = []
        if mn is None:
            segs.append((start, end))
        else:
            if start < mn:
                segs.append((start, mn - 1))
            if end > mx + M15:
                segs.append((mx, end))
        for s, e in segs:
            self.insert(sym, self.api.klines_range(sym, s, e, progress=progress))

    def load(self, sym, start, end):
        con = self._con()
        rows = con.execute("SELECT ts,o,h,l,c,v FROM candles WHERE sym=? AND ts>=? AND ts<=? ORDER BY ts",
                           (sym, int(start), int(end))).fetchall()
        con.close()
        if not rows:
            return {k: np.array([]) for k in ("ts", "o", "h", "l", "c", "v")}
        a = np.array(rows, dtype=float)
        return {"ts": a[:, 0].astype(np.int64), "o": a[:, 1], "h": a[:, 2], "l": a[:, 3], "c": a[:, 4],
                "v": a[:, 5]}

    def ensure_funding(self, sym, start, end):
        con = self._con()
        mn, mx = con.execute("SELECT MIN(ts), MAX(ts) FROM funding WHERE sym=?", (sym,)).fetchone()
        con.close()
        segs = []
        if mn is None:
            segs.append((start, end))
        else:
            if start < mn - 8 * 3_600_000:
                segs.append((start, mn - 1))
            if end > mx + 8 * 3_600_000:
                segs.append((mx + 1, end))
        for s, e in segs:
            d = self.api.funding_range(sym, s, e)
            if d:
                with self.lock:
                    con = self._con()
                    con.executemany("INSERT OR REPLACE INTO funding VALUES(?,?,?)",
                                    [(sym, t, r) for t, r in d.items()])
                    con.commit()
                    con.close()

    def load_funding(self, sym, start, end):
        con = self._con()
        rows = con.execute("SELECT ts, rate FROM funding WHERE sym=? AND ts>=? AND ts<=?",
                           (sym, int(start), int(end))).fetchall()
        con.close()
        return {int(t): float(r) for t, r in rows}


# ---------------------------------------------------------------- candle helpers
def candle_shape(o, h, l, c, ratio):
    """returns 'hammer', 'star' (shooting star) or '' (wicks are used, never closes)."""
    rng = h - l
    if rng <= 0:
        return ""
    body = abs(c - o)
    lower = min(o, c) - l
    upper = h - max(o, c)
    b = max(body, rng * 0.02)
    if lower >= ratio * b and lower >= 0.5 * rng and upper <= 0.25 * rng:
        return "hammer"
    if upper >= ratio * b and upper >= 0.5 * rng and lower <= 0.25 * rng:
        return "star"
    return ""


def aggregate_4h(ts, o, h, l, c):
    """15m -> closed 4h candles. Returns list of dicts and, per 4h candle, the index of the
    15m candle whose close completes it (zones may only be used AFTER that bar closed)."""
    out = []
    cur = None
    for i in range(len(ts)):
        b = int(ts[i]) - int(ts[i]) % H4
        if cur is None or cur["t"] != b:
            cur = {"t": b, "o": o[i], "h": h[i], "l": l[i], "c": c[i], "n": 0}
            out.append(cur)
        else:
            cur["h"] = max(cur["h"], h[i])
            cur["l"] = min(cur["l"], l[i])
            cur["c"] = c[i]
        cur["n"] += 1
        cur["last_i"] = i
    closed = [x for x in out if (int(ts[x["last_i"]]) + M15) % H4 == 0]
    return closed


def atr_simple(h, l, c, n=14):
    if len(h) < 2:
        return float(h[-1] - l[-1]) if len(h) else 0.0
    tr = [h[0] - l[0]]
    for i in range(1, len(h)):
        tr.append(max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1])))
    tr = tr[-n:]
    return float(sum(tr) / len(tr))


def find_zones(bars, p):
    """Support/resistance zones from CLOSED 4h candles (wick based).
    bars: list of 4h dicts (only the lookback window). Returns zones sorted by price."""
    n = len(bars)
    if n < 10:
        return []
    o = [b["o"] for b in bars]
    h = [b["h"] for b in bars]
    l = [b["l"] for b in bars]
    c = [b["c"] for b in bars]
    atr = atr_simple(h, l, c, 14)
    if atr <= 0:
        return []
    k = int(p["zone_pivot"])
    pts = []  # (price, wick_inner, kind, idx)
    for j in range(k, n - k):  # j + k <= n - 1 -> confirmed by closed candles only
        if h[j] >= max(h[j - k:j + k + 1]) and h[j] > max(h[j - k:j]):
            pts.append((h[j], max(o[j], c[j]), "R", j))
        if l[j] <= min(l[j - k:j + k + 1]) and l[j] < min(l[j - k:j]):
            pts.append((l[j], min(o[j], c[j]), "S", j))
    if len(pts) < 2:
        return []
    pts.sort(key=lambda x: x[0])
    maxw = p["zone_max_width_atr"] * atr
    minw = p["zone_min_width_atr"] * atr
    if p.get("zone_method", "pivots") == "wick_density":
        return _zones_density(o, h, l, c, pts, atr, p)
    clusters, cur = [], [pts[0]]
    for q in pts[1:]:
        if q[0] - cur[0][0] <= maxw:
            cur.append(q)
        else:
            clusters.append(cur)
            cur = [q]
    clusters.append(cur)
    cands = []
    for cl in clusters:
        idxs = {q[3] for q in cl}
        if len(idxs) < p["zone_min_touches"]:
            continue
        bot, top = min(q[0] for q in cl), max(q[0] for q in cl)
        if top - bot < minw:
            mid = (top + bot) / 2
            bot, top = mid - minw / 2, mid + minw / 2
        # count additional wick touches that rejected the area (acting as S/R)
        touches = 0
        for j in range(n):
            if l[j] <= top and h[j] >= bot and (c[j] > top or c[j] < bot):
                touches += 1
        crosses = 0
        for j in range(1, n):
            if (c[j - 1] < bot and c[j] > top) or (c[j - 1] > top and c[j] < bot):
                crosses += 1
        if crosses > p["zone_max_cross"]:
            continue  # washed out level
        kinds = "".join(sorted({q[2] for q in cl}))
        cands.append({"bot": float(bot), "top": float(top), "pivots": len(idxs), "touches": touches,
                      "kind": kinds, "last": max(idxs), "score": len(idxs) * 2 + touches * 0.25 + max(idxs) / n})
    cands.sort(key=lambda z: -z["score"])
    gap = p["zone_min_gap_atr"] * atr
    acc = []
    for z in cands:
        if all(z["bot"] - a["top"] >= gap or a["bot"] - z["top"] >= gap for a in acc):
            acc.append(z)
    acc.sort(key=lambda z: z["bot"])
    for z in acc:
        z.pop("score", None)
        z.pop("last", None)
    return acc


def _zones_density(o, h, l, c, pts, atr, p):
    """'as many wicks as possible touch the area': for every pivot extreme try a band of max width
    that contains it, count all 4h WICKS that poke into the band and close outside of it
    (rejections), tighten the band to the touching wick extremes, keep the best non-overlapping bands."""
    n = len(h)
    maxw = p["zone_max_width_atr"] * atr
    minw = p["zone_min_width_atr"] * atr
    cands = []
    for x, _, kind, j in pts:
        for lo_, hi_ in ((x - maxw, x), (x - maxw / 2, x + maxw / 2), (x, x + maxw)):
            ext, idxs = [], set()
            for i in range(n):
                if l[i] <= hi_ and h[i] >= lo_:
                    if c[i] > hi_ or c[i] < lo_:  # wick into the band, close outside = reaction
                        e = l[i] if c[i] > hi_ else h[i]
                        e = min(max(e, lo_), hi_)
                        ext.append(e)
                        idxs.add(i)
            if len(idxs) < p["zone_min_touches"]:
                continue
            bot, top = min(ext), max(ext)
            if top - bot < minw:
                mid = (top + bot) / 2
                bot, top = mid - minw / 2, mid + minw / 2
            crosses = sum(1 for i in range(1, n) if (c[i - 1] < bot and c[i] > top) or (c[i - 1] > top and c[i] < bot))
            if crosses > p["zone_max_cross"]:
                continue
            piv = sum(1 for q in pts if bot <= q[0] <= top)
            if piv < 1:
                continue
            cands.append({"bot": float(bot), "top": float(top), "pivots": piv, "touches": len(idxs),
                          "kind": kind, "score": len(idxs) + piv * 2 + max(idxs) / n})
    cands.sort(key=lambda z: -z["score"])
    gap = p["zone_min_gap_atr"] * atr
    acc = []
    for z in cands:
        if all(z["bot"] - a["top"] >= gap or a["bot"] - z["top"] >= gap for a in acc):
            acc.append(z)
    acc.sort(key=lambda z: z["bot"])
    for z in acc:
        z.pop("score", None)
    return acc


# ---------------------------------------------------------------- engine
class Engine:
    """Per-symbol strategy state machine. Feed CLOSED 15m bars in order with step()."""

    MAXBUF = 2500

    def __init__(self, symbol, params, cfg):
        self.sym = symbol
        self.p = dict(params)
        self.cfg = cfg
        self.ts, self.o, self.h, self.l, self.c = [], [], [], [], []
        self.off = 0  # absolute index of buffer[0]
        self.h4 = []  # closed 4h bars
        self.cur4 = None
        self.zones = []
        self.zones_ts = None
        self.atr15 = None
        self.trs = deque(maxlen=14)
        self.setup = None
        self.pos = None
        self.pending = None
        self.events = []
        self.n = 0
        self.last_status = ""
        self.reg = {}

    # ---- buffer access by absolute index
    def _i(self, a):
        return a - self.off

    def H(self, a):
        return self.h[a - self.off]

    def L(self, a):
        return self.l[a - self.off]

    def C(self, a):
        return self.c[a - self.off]

    def O(self, a):
        return self.o[a - self.off]

    # ---- 4h
    def _update_4h(self, t, o, h, l, c):
        b = t - t % H4
        if self.cur4 is None or self.cur4["t"] != b:
            self.cur4 = {"t": b, "o": o, "h": h, "l": l, "c": c}
        else:
            self.cur4["h"] = max(self.cur4["h"], h)
            self.cur4["l"] = min(self.cur4["l"], l)
            self.cur4["c"] = c
        if (t + M15) % H4 == 0:  # this 15m close completes the 4h candle
            self.h4.append(dict(self.cur4))
            self.cur4 = None
            if len(self.h4) > 600:
                self.h4 = self.h4[-600:]
            lb = int(self.p["zone_lookback"])
            self.zones = find_zones(self.h4[-lb:], self.p)
            self.zones_ts = t + M15
            self._regime()

    # ---- February: market regime from CLOSED 4h candles only
    def _regime(self):
        b = self.h4[-260:]
        if len(b) < 60:
            self.reg = {}
            return
        C = [x["c"] for x in b]
        H = [x["h"] for x in b]
        L = [x["l"] for x in b]
        a = 2 / 201
        e = C[0]
        for x in C[1:]:
            e = a * x + (1 - a) * e
        k = 1 / 14
        at = pp = nn = None
        atrs = []
        for i in range(1, len(C)):
            tr = max(H[i] - L[i], abs(H[i] - C[i - 1]), abs(L[i] - C[i - 1]))
            up, dn = H[i] - H[i - 1], L[i - 1] - L[i]
            pdm = up if (up > dn and up > 0) else 0.0
            ndm = dn if (dn > up and dn > 0) else 0.0
            if at is None:
                at, pp, nn = tr, pdm, ndm
            else:
                at += k * (tr - at)
                pp += k * (pdm - pp)
                nn += k * (ndm - nn)
                atrs.append(at)
        md = int(self.p.get("mom_days", 30)) * 6
        hist = atrs[-181:-1]
        self.reg = {"ema200": e, "di": 1 if pp > nn else -1, "close": C[-1],
                    "mom": (C[-1] / C[-1 - md] - 1) if len(C) > md else 0.0,
                    "atr_pct": atrs[-1] / C[-1] * 100 if atrs else 0.0,
                    "vol_pct": (sum(1 for x in hist if x < atrs[-1]) / len(hist)) if len(hist) >= 60 else 0.5}

    def regime_ok(self, d):
        """returns (allowed, reason) for a trade in direction d."""
        r = self.reg
        if not r:
            return True, ""
        tf = self.p.get("trend_filter", "off")
        if tf == "di" and r["di"] != d:
            return False, "against the 4h DI trend"
        if tf == "ema200" and d * (r["close"] - r["ema200"]) < 0:
            return False, "against the 4h EMA200"
        if tf == "mom" and d * r["mom"] < 0:
            return False, "against the momentum"
        if tf == "di_or_mom" and r["di"] != d and d * r["mom"] < 0:
            return False, "against 4h DI and momentum"
        if r["vol_pct"] > self.p.get("vol_max_pct", 1.0):
            return False, f"4h volatility too high ({r['vol_pct'] * 100:.0f}th pct)"
        return True, ""

    def _zone_index(self, price):
        """returns ('in', k) when inside zone k, ('between', k) when between zone k-1 and k
        (k == len(zones) means above all zones, k == 0 below all)."""
        for k, z in enumerate(self.zones):
            if z["bot"] <= price <= z["top"]:
                return "in", k
            if price < z["bot"]:
                return "between", k
        return "between", len(self.zones)

    # ---- pivots on 15m (confirmed only)
    def _pivot_lows_before(self, a_end, k, below=None):
        """confirmed pivot lows with index j, j+k <= a_end, newest first."""
        out = []
        start = max(self.off + k, a_end - 600)
        for j in range(a_end - k, start - 1, -1):
            lj = self.L(j)
            ok = all(lj <= self.L(x) for x in range(j - k, j + k + 1)) and all(lj < self.L(x) for x in range(j - k, j))
            if ok and (below is None or lj < below):
                out.append(j)
                if len(out) >= 12:
                    break
        return out

    def _pivot_highs_before(self, a_end, k, above=None):
        out = []
        start = max(self.off + k, a_end - 600)
        for j in range(a_end - k, start - 1, -1):
            hj = self.H(j)
            ok = all(hj >= self.H(x) for x in range(j - k, j + k + 1)) and all(hj > self.H(x) for x in range(j - k, j))
            if ok and (above is None or hj > above):
                out.append(j)
                if len(out) >= 12:
                    break
        return out

    # ---- main entry point
    def step(self, t, o, h, l, c):
        """process one CLOSED 15m candle. Returns list of events."""
        self.events = []
        self.ts.append(int(t))
        self.o.append(o)
        self.h.append(h)
        self.l.append(l)
        self.c.append(c)
        a = self.off + len(self.c) - 1
        prev_c = self.c[-2] if len(self.c) > 1 else c
        self.trs.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
        self.atr15 = sum(self.trs) / len(self.trs)

        # 1) pending entry fills at THIS bar's open (signal came from the previous close)
        if self.pending is not None:
            self._fill(self.pending, t, o)
            self.pending = None
        # 2) stop check inside this bar with the stop that was valid at bar open
        if self.pos is not None and self.pos.get("entry_ts", 0) <= t:
            self._check_stop(t, o, h, l)
        # 3) close based logic
        if self.pos is not None:
            self._trail(a)
        else:
            had = self.pending
            self._setup_logic(a)
            if had is None and self.pending is not None:
                ok, why = self.regime_ok(self.pending["dir"])
                if not ok:
                    self.events = [e for e in self.events if e["type"] != "signal"]
                    self.events.append({"type": "skip", "sym": self.sym, "ts": self.ts[-1],
                                        "why": "regime filter: " + why})
                    self.pending = None
                    self.last_status = "engulfing filtered out - " + why
        # 4) 4h zones update only after the 4h candle completed (used from the next bar on)
        self._update_4h(int(t), o, h, l, c)
        if len(self.c) > self.MAXBUF:
            cut = len(self.c) - self.MAXBUF
            del self.ts[:cut], self.o[:cut], self.h[:cut], self.l[:cut], self.c[:cut]
            self.off += cut
        self.n += 1
        return self.events

    # ---- setup state machine
    def _setup_logic(self, a):
        p = self.p
        c, h, l, o = self.C(a), self.H(a), self.L(a), self.O(a)
        if not self.zones or a - self.off < 30:
            self.last_status = "collecting data / no zones"
            return
        s = self.setup
        # breakout detection (also replaces an older setup)
        bo = self._detect_breakout(a)
        if bo is not None:
            s = self._new_setup(a, bo)
            self.setup = s
            if s is None:
                return
        if s is None:
            where, k = self._zone_index(c)
            self.last_status = "waiting for a breakout" if where == "between" else "price inside a zone"
            return
        d = s["dir"]
        # expiry
        if a - s["bo_a"] > p["setup_max_bars"]:
            self._drop("expired")
            return
        # invalidation: back into the broken zone
        zt, zb = s["zone"]["top"], s["zone"]["bot"]
        if d == 1:
            back = (c < zt) if p["invalidate"] == "close" else (l <= zt)
        else:
            back = (c > zb) if p["invalidate"] == "close" else (h >= zb)
        if back and a > s["bo_a"]:
            self._drop("price back in the broken zone")
            return
        st = s["stage"]
        if st == "impulse":
            # track extreme of the move, count clear pullback candles
            if (d == 1 and h > s["ext"]) or (d == -1 and l < s["ext"]):
                s["ext"], s["ext_a"] = (h if d == 1 else l), a
                s["pb"] = 0
            shape = candle_shape(o, h, l, c, p["star_ratio"])
            bad = "star" if d == 1 else "hammer"
            prev_c = self.C(a - 1)
            pm = p.get("pullback_mode", "strict")
            if pm == "color":
                clear = (c < o) if d == 1 else (c > o)
            elif pm == "lower_highs":
                clear = (h < self.H(a - 1) and l < self.L(a - 1)) if d == 1 else (l > self.L(a - 1) and h > self.H(a - 1))
            else:
                clear = (c < o and c < prev_c) if d == 1 else (c > o and c > prev_c)
            if a == s["ext_a"]:
                clear = clear and False  # the candle that made the extreme is not a pullback candle
            if clear and shape != bad:
                s["pb"] += 1
            elif a != s["ext_a"]:
                s["pb"] = 0
            if s["pb"] >= p["pullback_bars"]:
                s["stage"] = "pullback"
                self._levels(s)
                self.events.append({"type": "pullback", "sym": self.sym, "ts": self.ts[-1], "dir": d})
            else:
                self.last_status = f"broke {'above' if d == 1 else 'below'} zone - waiting for {p['pullback_bars']}-candle pullback ({s['pb']}/{p['pullback_bars']})"
                return
        if s["stage"] in ("pullback", "touched"):
            # new extreme before entry -> the move continues, restart structure
            if (d == 1 and c > s["ext"]) or (d == -1 and c < s["ext"]):
                s["stage"] = "impulse"
                s["ext"], s["ext_a"] = (h if d == 1 else l), a
                s["pb"] = 0
                piv = self._pivot_lows_before(a, p["swing_pivot"]) if d == 1 else self._pivot_highs_before(a, p["swing_pivot"])
                piv = [j for j in piv if j > s["bo_a"] - 400]
                if piv:
                    s["anchors"] = piv
                    s["anc_k"] = 0
                self.last_status = "new extreme - structure restarted, waiting for pullback"
                return
            # deeper than 72.2% (close) -> the low was wrong, take the one before
            while (d == 1 and c < s["deep"]) or (d == -1 and c > s["deep"]):
                if not self._reanchor(s):
                    self._drop("no earlier swing point left")
                    return
            if s["stage"] == "pullback":
                if (d == 1 and l <= s["fib"]) or (d == -1 and h >= s["fib"]):
                    s["stage"] = "touched"
                    s["touch_a"] = a
                    self.events.append({"type": "touch", "sym": self.sym, "ts": self.ts[-1], "dir": d})
                else:
                    self.last_status = f"pullback running - waiting for {p['fib_entry'] * 100:.1f}% ({s['fib']:.4f})"
                    return
            # engulfing in the breakout direction (may be the touch bar itself)
            po, pc = self.O(a - 1), self.C(a - 1)
            ph, pl = self.H(a - 1), self.L(a - 1)
            prev_shape = candle_shape(po, ph, pl, pc, p["star_ratio"])
            em = p.get("engulf_mode", "body")
            if d == 1:
                eng = c > o and pc < po and o <= pc and c >= po and (c - o) > (po - pc)
                if em == "close_beyond":
                    eng = eng and c > ph
            else:
                eng = c < o and pc > po and o >= pc and c <= po and (o - c) > (pc - po)
                if em == "close_beyond":
                    eng = eng and c < pl
            if eng and p.get("entry_window", "after_touch") == "in_band":
                eng = (min(l, pl) <= s["fib"]) if d == 1 else (max(h, ph) >= s["fib"])
                eng = eng and ((c > s["deep"]) if d == 1 else (c < s["deep"]))
            if eng and prev_shape == "":
                sl = self._stop_price(s)
                dist = abs(c - sl) / c * 100
                if (d == 1 and sl >= c) or (d == -1 and sl <= c):
                    self._drop("stop on the wrong side")
                    return
                if dist < p["min_sl_pct"] or dist > p["max_sl_pct"]:
                    self.last_status = f"engulfing found but SL {dist:.2f}% outside limits - still waiting"
                    return
                self.pending = {"dir": d, "sl": sl, "signal_ts": self.ts[-1], "anchor": s["L"], "next": s.get("next"),
                                "ext": s["ext"], "ext_a": s["ext_a"], "zone": s["zone"],
                                "fib": s["fib"], "deep": s["deep"]}
                self.events.append({"type": "signal", "sym": self.sym, "ts": self.ts[-1], "dir": d, "sl": sl})
                self.last_status = "ENGULFING - entering at next open"
                self.setup = None
                return
            self.last_status = f"{p['fib_entry'] * 100:.1f}% touched - waiting for {'bullish' if d == 1 else 'bearish'} engulfing"

    def _detect_breakout(self, a):
        c = self.C(a)
        pc = self.C(a - 1)
        p = self.p
        for k, z in enumerate(self.zones):
            # up: first close above zone top coming from the range below
            if c > z["top"] and pc <= z["top"]:
                lower = self.zones[k - 1]["top"] if k > 0 else -math.inf
                if self._ranged(a, z, lower, 1):
                    up = self.zones[k + 1] if k + 1 < len(self.zones) else None
                    return {"dir": 1, "zone": z, "next": up, "prev_zone": self.zones[k - 1] if k > 0 else None}
            if c < z["bot"] and pc >= z["bot"]:
                upper = self.zones[k + 1]["bot"] if k + 1 < len(self.zones) else math.inf
                if self._ranged(a, z, upper, -1):
                    dn = self.zones[k - 1] if k > 0 else None
                    return {"dir": -1, "zone": z, "next": dn, "prev_zone": self.zones[k + 1] if k + 1 < len(self.zones) else None}
        return None

    def _ranged(self, a, z, other, d):
        """walk back from a-1: skip closes inside the broken zone, then require >= range_min_bars
        consecutive closes strictly between the other zone and this zone."""
        j = a - 1
        lim = max(self.off + 1, a - 800)
        while j >= lim and z["bot"] <= self.C(j) <= z["top"]:
            j -= 1
        cnt = 0
        while j >= lim:
            cj = self.C(j)
            inside = (other < cj < z["bot"]) if d == 1 else (z["top"] < cj < other)
            if not inside:
                break
            cnt += 1
            if cnt >= self.p["range_min_bars"]:
                return True
            j -= 1
        return False

    def _new_setup(self, a, bo):
        d = bo["dir"]
        k = self.p["swing_pivot"]
        if d == 1:
            anchors = self._pivot_lows_before(a, k)
            anchors = [j for j in anchors if self.L(j) < self.C(a)]
        else:
            anchors = self._pivot_highs_before(a, k)
            anchors = [j for j in anchors if self.H(j) > self.C(a)]
        am = self.p.get("anchor_mode", "pivot")
        if am != "pivot":
            j0 = self._special_anchor(a, d, am, bo)
            if j0 is not None:
                v0 = self.L(j0) if d == 1 else self.H(j0)
                rest = [j for j in anchors if (self.L(j) < v0 if d == 1 else self.H(j) > v0)]
                anchors = [j0] + rest
        if not anchors:
            self.last_status = "breakout without a usable swing point"
            return None
        ext_a = a
        s = {"dir": d, "zone": bo["zone"], "next": bo["next"], "prev_zone": bo["prev_zone"], "bo_a": a,
             "bo_ts": self.ts[-1], "anchors": anchors, "anc_k": 0, "stage": "impulse", "pb": 0,
             "ext": self.H(a) if d == 1 else self.L(a), "ext_a": ext_a}
        # extreme of the move = max high since the anchor
        j0 = anchors[0]
        if d == 1:
            s["ext"] = max(self.H(x) for x in range(j0, a + 1))
        else:
            s["ext"] = min(self.L(x) for x in range(j0, a + 1))
        self.events.append({"type": "breakout", "sym": self.sym, "ts": self.ts[-1], "dir": d,
                            "zone": [bo["zone"]["bot"], bo["zone"]["top"]]})
        self.last_status = f"BREAKOUT {'up' if d == 1 else 'down'} - waiting for pullback"
        return s

    def _special_anchor(self, a, d, mode, bo):
        """leg_low: extreme since the last swing point against the move (start of the breakout leg).
        range_low: extreme of the whole A-B range stretch before the breakout."""
        lim = max(self.off + 1, a - 400)
        if mode == "leg_low":
            k = max(1, int(self.p["swing_pivot"]))
            opp = self._pivot_highs_before(a, k) if d == 1 else self._pivot_lows_before(a, k)
            j_start = opp[0] if opp else max(lim, a - 96)
            rng = range(max(j_start, lim), a + 1)
        else:
            z = bo["zone"]
            j = a - 1
            while j >= lim and z["bot"] <= self.C(j) <= z["top"]:
                j -= 1
            j_end = j
            while j >= lim and ((self.C(j) < z["bot"]) if d == 1 else (self.C(j) > z["top"])):
                j -= 1
                if a - j > 400:
                    break
            rng = range(max(j + 1, lim), a + 1)
        if not len(rng):
            return None
        if d == 1:
            return min(rng, key=lambda x: self.L(x))
        return max(rng, key=lambda x: self.H(x))

    def _levels(self, s):
        j = s["anchors"][s["anc_k"]]
        d = s["dir"]
        s["L"] = self.L(j) if d == 1 else self.H(j)
        s["L_a"] = j
        rng = s["ext"] - s["L"]
        s["fib"] = s["ext"] - self.p["fib_entry"] * rng
        s["deep"] = s["ext"] - self.p["fib_deep"] * rng

    def _reanchor(self, s):
        """take the swing point before the current one (it must be beyond the current one)."""
        d = s["dir"]
        cur = s["L"]
        while True:
            s["anc_k"] += 1
            if s["anc_k"] >= len(s["anchors"]):
                # look further back
                j_old = s["anchors"][-1]
                k = self.p["swing_pivot"]
                more = self._pivot_lows_before(j_old - 1 + k, k, below=cur) if d == 1 else \
                    self._pivot_highs_before(j_old - 1 + k, k, above=cur)
                more = [j for j in more if j < j_old]
                if not more:
                    return False
                s["anchors"].extend(more)
            j = s["anchors"][s["anc_k"]]
            v = self.L(j) if d == 1 else self.H(j)
            if (d == 1 and v < cur) or (d == -1 and v > cur):
                self._levels(s)
                self.events.append({"type": "reanchor", "sym": self.sym, "ts": self.ts[-1], "dir": d, "L": v})
                return True

    def _stop_price(self, s):
        buf = self.p["sl_buffer_atr"] * (self.atr15 or 0)
        return s["L"] - buf if s["dir"] == 1 else s["L"] + buf

    def _drop(self, why):
        if self.setup is not None:
            self.events.append({"type": "drop", "sym": self.sym, "ts": self.ts[-1], "why": why})
        self.setup = None
        self.last_status = f"setup cancelled: {why}"

    # ---- position handling
    def _fill(self, pend, t, price):
        d = pend["dir"]
        if (d == 1 and price <= pend["sl"]) or (d == -1 and price >= pend["sl"]):
            self.events.append({"type": "skip", "sym": self.sym, "ts": t, "why": "gapped through stop"})
            return
        self.pos = {"sym": self.sym, "dir": d, "entry_ts": int(t), "entry": float(price), "sl": float(pend["sl"]),
                    "sl0": float(pend["sl"]), "anchor": pend["anchor"], "signal_ts": pend["signal_ts"],
                    "struct": float(pend["ext"]), "struct_ts": int(self.ts[self._i(pend["ext_a"])]) if self._i(pend["ext_a"]) >= 0 else int(t),
                    "struct_a": pend["ext_a"], "last_break_a": None, "trail": [], "funding": [],
                    "zone": pend["zone"], "fib": pend["fib"], "mfe": 0.0, "mae": 0.0, "tp": None,
                    "partials": [], "be_done": False}
        em = self.p.get("exit_mode", "trail")
        if em == "trail_zone_tp" and pend.get("next"):
            nz = pend["next"]
            tp = nz["bot"] if d == 1 else nz["top"]
            if (d == 1 and tp > price) or (d == -1 and tp < price):
                self.pos["tp"] = float(tp)
        elif em == "trail_rr_tp":
            self.pos["tp"] = float(price + d * self.p.get("tp_r", 3.0) * abs(price - pend["sl"]))
        self.events.append({"type": "open", "sym": self.sym, "ts": int(t), "pos": copy.deepcopy(self.pos)})

    def fill_pending_now(self, t, price):
        """live: fill the pending entry immediately at the open of the new candle."""
        if self.pending is not None:
            self.events = []
            self._fill(self.pending, t, price)
            self.pending = None
            return self.events
        return []

    def _check_stop(self, t, o, h, l):
        ps = self.pos
        d = ps["dir"]
        pr = self.p.get("partial_r", 0)
        rk = abs(ps["entry"] - ps["sl0"])
        if pr and not ps.get("partials") and rk > 0:
            ptp = ps["entry"] + d * pr * rk
            sl_hit = (l <= ps["sl"]) if d == 1 else (h >= ps["sl"])
            if not sl_hit and ((d == 1 and h >= ptp) or (d == -1 and l <= ptp)):
                px = max(o, ptp) if d == 1 else min(o, ptp)
                ps.setdefault("partials", []).append([int(t), float(px), float(self.p.get("partial_frac", 0.5)), True])
                self.events.append({"type": "partial", "sym": self.sym, "ts": int(t), "price": float(px)})
        hit = (l <= ps["sl"]) if d == 1 else (h >= ps["sl"])
        risk = abs(ps["entry"] - ps["sl0"])
        if risk > 0:
            ps["mfe"] = max(ps["mfe"], ((h - ps["entry"]) if d == 1 else (ps["entry"] - l)) / risk)
            ps["mae"] = min(ps["mae"], ((l - ps["entry"]) if d == 1 else (ps["entry"] - h)) / risk)
        if hit:  # stop first if stop and target are both inside the bar (worst case)
            px = min(o, ps["sl"]) if d == 1 else max(o, ps["sl"])  # gap through the stop fills at the open
            self.close(t, px, "trail stop" if ps["trail"] else "stop loss")
            return
        tp = ps.get("tp")
        if tp and ((d == 1 and h >= tp) or (d == -1 and l <= tp)):
            px = max(o, tp) if d == 1 else min(o, tp)  # resting limit: fills at the target or better
            ps["exit_maker"] = True
            self.close(t, px, "take profit")

    def close(self, t, price, reason):
        ps = self.pos
        if ps is None:
            return
        ps["exit_ts"] = int(t)
        ps["exit"] = float(price)
        ps["reason"] = reason
        self.events.append({"type": "close", "sym": self.sym, "ts": int(t), "pos": copy.deepcopy(ps)})
        self.pos = None
        self.setup = None

    def _trail(self, a):
        ps = self.pos
        d = ps["dir"]
        c = self.C(a)
        br = self.p.get("be_r", 0)
        if br and not ps.get("be_done"):
            if d * (c - ps["entry"]) >= br * abs(ps["entry"] - ps["sl0"]):
                be = ps["entry"] * (1 + d * 2 * self.cfg["fees"]["taker"])
                if (d == 1 and be > ps["sl"] and be < c) or (d == -1 and be < ps["sl"] and be > c):
                    ps["sl"] = float(be)
                    ps["trail"].append([int(self.ts[-1]), float(be)])
                    self.events.append({"type": "trail", "sym": self.sym, "ts": int(self.ts[-1]), "sl": float(be)})
                ps["be_done"] = True
        k = int(self.p["trail_pivot"])
        buf = self.p["sl_buffer_atr"] * (self.atr15 or 0)
        # find a fresh structure extreme after the last break
        if ps["struct"] is None:
            lb = ps["last_break_a"]
            j = a - k
            if j - k >= self.off and j > lb:
                if d == 1:
                    hj = self.H(j)
                    if all(hj >= self.H(x) for x in range(j - k, j + k + 1)):
                        ps["struct"], ps["struct_a"] = hj, j
                else:
                    lj = self.L(j)
                    if all(lj <= self.L(x) for x in range(j - k, j + k + 1)):
                        ps["struct"], ps["struct_a"] = lj, j
        if ps["struct"] is not None:
            broke = (c > ps["struct"]) if d == 1 else (c < ps["struct"])
            if broke:
                j0 = max(ps["struct_a"], self.off)
                if d == 1:
                    lo = min(self.L(x) for x in range(j0, a + 1))
                    new = lo - buf
                    improve = new > ps["sl"] and new < c
                else:
                    hi = max(self.H(x) for x in range(j0, a + 1))
                    new = hi + buf
                    improve = new < ps["sl"] and new > c
                if improve:
                    ps["sl"] = float(new)
                    ps["trail"].append([int(self.ts[-1]), float(new)])
                    self.events.append({"type": "trail", "sym": self.sym, "ts": int(self.ts[-1]), "sl": float(new)})
                ps["struct"] = None
                ps["last_break_a"] = a
        self.last_status = "in position - trailing below structure" if d == 1 else "in position - trailing above structure"

    # ---- status for the dashboard
    def status(self):
        p = self.p
        s = self.setup
        st = {"symbol": self.sym, "text": self.last_status, "zones": self.zones, "zones_ts": self.zones_ts,
              "price": self.c[-1] if self.c else None, "atr15": self.atr15, "bars": self.n,
              "pending": self.pending is not None}
        nodes = [{"k": "zones", "l": "4h zones", "ok": bool(self.zones), "v": f"{len(self.zones)} active"}]
        if self.reg:
            okl, okh = self.regime_ok(1), self.regime_ok(-1)
            st["regime"] = dict(self.reg, long_ok=okl[0], short_ok=okh[0], long_why=okl[1], short_why=okh[1],
                                vol_max=self.p.get("vol_max_pct", 1.0), trend_filter=self.p.get("trend_filter", "off"))
        if self.c and self.zones:
            where, k = self._zone_index(self.c[-1])
            if where == "in":
                rng = f"inside zone {k + 1}"
            else:
                lo = self.zones[k - 1]["top"] if k > 0 else None
                hi = self.zones[k]["bot"] if k < len(self.zones) else None
                rng = "between zones" if lo and hi else ("above all zones" if lo else "below all zones")
                st["range_lo"], st["range_hi"] = lo, hi
            st["range"] = rng
        if self.pos is not None:
            stage = 6
        elif s is None:
            stage = 1
        else:
            stage = {"impulse": 2, "pullback": 3, "touched": 4}.get(s["stage"], 1)
            if s["stage"] == "impulse" and s["pb"] > 0:
                stage = 2
        labels = [("range", "range A-B"), ("breakout", "close through B"), ("pullback", f"{p['pullback_bars']}-candle pullback"),
                  ("fib", f"{p['fib_entry'] * 100:.1f}% touch"), ("engulf", "engulfing"), ("pos", "in position")]
        for n_, (key, lab) in enumerate(labels, start=1):
            nodes.append({"k": key, "l": lab, "ok": stage > n_ or (stage == 6 and n_ == 6),
                          "act": stage == n_})
        st["nodes"] = nodes
        if s is not None:
            st["setup"] = {"dir": s["dir"], "stage": s["stage"], "zone": s["zone"], "pb": s["pb"],
                           "ext": s["ext"], "bo_ts": s["bo_ts"], "L": s.get("L"), "fib": s.get("fib"),
                           "deep": s.get("deep"), "bars_left": p["setup_max_bars"] - (self.off + len(self.c) - 1 - s["bo_a"])}
        if self.pos is not None:
            ps = self.pos
            st["position"] = {k: ps.get(k) for k in ("dir", "entry", "entry_ts", "sl", "sl0", "struct", "trail", "tp", "partials")}
        return st

    # ---- persistence of an open position (paper trader)
    def export_pos(self):
        return copy.deepcopy(self.pos) if self.pos else None

    def import_pos(self, ps):
        """re-attach a stored live position after warm-up. struct indices are re-mapped by timestamp."""
        if not ps:
            if self.pos is not None:  # warm-up was in a trade the live account never took -> start clean
                self.pos = None
                self.setup = None
                self.pending = None
            return  # otherwise keep the setup the warm-up is tracking (a breakout may be in progress)
        self.setup = None
        self.pending = None
        ps = copy.deepcopy(ps)
        ts_list = self.ts

        def a_of(t):
            if t is None:
                return None
            for i in range(len(ts_list) - 1, -1, -1):
                if ts_list[i] <= t:
                    return self.off + i
            return self.off

        ps["struct_a"] = a_of(ps.get("struct_ts")) if ps.get("struct") is not None else None
        ps["last_break_a"] = a_of(ps.get("last_break_ts"))
        self.pos = ps

    def export_pos_for_store(self):
        ps = self.export_pos()
        if ps:
            sa = ps.get("struct_a")
            ps["struct_ts"] = int(self.ts[sa - self.off]) if sa is not None and 0 <= sa - self.off < len(self.ts) else None
            lb = ps.get("last_break_a")
            ps["last_break_ts"] = int(self.ts[lb - self.off]) if lb is not None and 0 <= lb - self.off < len(self.ts) else None
        return ps


# ---------------------------------------------------------------- simulation (backtest / optimizer)
def run_symbol(sym, data, params, cfg, start_ms, funding=None, collect_chart=False):
    """Runs the engine over the whole array (warm-up included). Trades whose ENTRY is before start_ms
    are discarded (they belong to the warm-up). Returns trades + optional chart annotations."""
    eng = Engine(sym, params, cfg)
    ts, o, h, l, c = data["ts"], data["o"], data["h"], data["l"], data["c"]
    trades, marks = [], []
    zone_hist = []
    fund_ts = sorted(funding) if funding else []
    fk = 0
    for i in range(len(ts)):
        t = int(ts[i])
        # funding settlements that happen during this bar for an open position
        if eng.pos is not None and fund_ts:
            while fk < len(fund_ts) and fund_ts[fk] < t:
                fk += 1
            while fk < len(fund_ts) and fund_ts[fk] < t + M15:
                ft = fund_ts[fk]
                if eng.pos is not None and eng.pos["entry_ts"] <= ft:
                    eng.pos["funding"].append([ft, funding[ft], float(o[i])])
                fk += 1
        z_before = eng.zones_ts
        evs = eng.step(t, float(o[i]), float(h[i]), float(l[i]), float(c[i]))
        for e in evs:
            if e["type"] == "close":
                ps = e["pos"]
                if ps["entry_ts"] >= start_ms:
                    trades.append(trade_record(ps))
            if collect_chart and t >= start_ms and e["type"] in ("breakout", "touch", "reanchor", "drop"):
                marks.append(e)
        if collect_chart and eng.zones_ts != z_before and t >= start_ms:
            zone_hist.append({"ts": eng.zones_ts, "zones": [[z["bot"], z["top"]] for z in eng.zones]})
    open_pos = eng.export_pos()
    return {"trades": trades, "marks": marks, "zone_hist": zone_hist, "engine": eng,
            "open": open_pos if open_pos and open_pos["entry_ts"] >= start_ms else None}


def trade_record(ps):
    return {"sym": ps["sym"], "dir": ps["dir"], "side": "LONG" if ps["dir"] == 1 else "SHORT",
            "entry_ts": ps["entry_ts"], "entry": ps["entry"], "sl0": ps["sl0"], "sl": ps["sl"],
            "exit_ts": ps.get("exit_ts"), "exit": ps.get("exit"), "reason": ps.get("reason"),
            "trail": ps.get("trail", []), "funding": ps.get("funding", []), "signal_ts": ps.get("signal_ts"),
            "mfe": ps.get("mfe", 0.0), "mae": ps.get("mae", 0.0), "tp": ps.get("tp"),
            "exit_maker": bool(ps.get("exit_maker")), "partials": ps.get("partials", [])}


def unit_r(t, fees):
    """R multiple of a trade independent of the position size (fees and funding included).
    size = risk / (stop distance + round-trip taker fees) -> a full stop is exactly -1 R."""
    tk = fees["taker"]
    d = t["dir"]
    risk_unit = abs(t["entry"] - t["sl0"]) + tk * (t["entry"] + t["sl0"])
    if risk_unit <= 0:
        return 0.0
    ex = t["exit"]
    mk = fees.get("maker", tk)
    rem, gross, fee = 1.0, 0.0, tk * t["entry"]
    for _, px, f, maker in t.get("partials", []) or []:
        gross += f * d * (px - t["entry"])
        fee += f * (mk if maker else tk) * px
        rem -= f
    gross += rem * d * (ex - t["entry"])
    fee += rem * (mk if t.get("exit_maker") else tk) * ex
    fund = sum(d * r * px for _, r, px in t.get("funding", [])) if fees.get("include_funding", True) else 0.0
    return (gross - fee - fund) / risk_unit


# ---------------------------------------------------------------- account replay
def replay(trades, cfg, settings, symbols, open_positions=None, marks=None, start_ms=None, end_ms=None):
    """Replays size-independent trade records into a $ account for the given symbols.
    Risk in $ = risk% x realized balance at the moment of entry -> two positions opened while the
    balance is unchanged carry the same absolute risk (combined account). Kelly sizing (if enabled)
    only uses trades CLOSED before the entry (no look-ahead)."""
    acc = cfg["account"]
    fees = cfg["fees"]
    tk = fees["taker"]
    bal = float(settings.get("start_balance", acc["start_balance"]))
    start_bal = bal
    base_risk = float(settings.get("risk_pct", acc["risk_pct"])) / 100.0
    kelly_on = bool(settings.get("kelly_sizing", False))
    kfrac, kmin, kmax = acc["kelly_fraction"], acc["kelly_min_trades"], acc["kelly_max_risk_pct"] / 100.0
    edge_n = int(settings.get("edge_n", acc.get("edge_n", 0)) or 0)
    edge_low = float(settings.get("edge_low", acc.get("edge_low", 0.0)) or 0.0)
    unit_hist = []  # size-independent R of CLOSED trades (edge monitor, no look-ahead)
    maxlev = acc["max_leverage"]
    sel = [t for t in trades if t["sym"] in symbols]
    opens = [dict(p) for p in (open_positions or []) if p and p["sym"] in symbols]
    ev = []
    for idx, t in enumerate(sel):
        ev.append((t["entry_ts"], 1, idx, "t"))
        ev.append((t["exit_ts"], 2, idx, "t"))
    for idx, p in enumerate(opens):
        ev.append((p["entry_ts"], 1, idx, "o"))
    ev.sort(key=lambda x: (x[0], x[1]))
    closed_r = []
    sizes = {}
    out_trades = []
    curve = [[int(start_ms or (ev[0][0] if ev else now_ms())), bal]]
    for ts_, kind, idx, src in ev:
        rec = sel[idx] if src == "t" else opens[idx]
        if kind == 1:
            r_pct = base_risk
            if kelly_on and len(closed_r) >= kmin:
                kf = kelly(closed_r)["kelly"]
                r_pct = min(kmax, max(0.0025, kf * kfrac)) if kf > 0 else 0.0025
            edge_w = 1.0
            if edge_n and len(unit_hist) >= edge_n and sum(unit_hist[-edge_n:]) <= 0:
                edge_w = edge_low
            risk_usd = bal * r_pct * edge_w
            inst = cfg["instruments"].get(rec["sym"], {"qty_step": 0.001, "min_qty": 0.001, "min_notional": 5})
            unit = abs(rec["entry"] - rec["sl0"]) + tk * (rec["entry"] + rec["sl0"])
            qty = risk_usd / unit if unit > 0 else 0
            cap = maxlev * bal / rec["entry"]
            qty = min(qty, cap)
            step = inst["qty_step"]
            qty = math.floor(qty / step + 1e-9) * step
            ok = qty >= inst["min_qty"] and qty * rec["entry"] >= inst.get("min_notional", 5) and bal > 0
            shadow = edge_w == 0
            sizes[(src, idx)] = {"qty": qty if ok else 0.0, "risk_usd": risk_usd, "risk_pct": r_pct * 100 * edge_w,
                                 "bal_at_entry": bal, "ok": ok, "shadow": shadow, "edge_w": edge_w}
        else:
            sz = sizes.get((src, idx))
            unit_hist.append(unit_r(rec, fees))
            if not sz or not sz["ok"]:
                out_trades.append(dict(rec, skipped=True, shadow=bool(sz and sz.get("shadow")), qty=0, pnl=0, r=0, fees=0, funding_usd=0,
                                       sl_pct=abs(rec["entry"] - rec["sl0"]) / rec["entry"] * 100,
                                       risk_usd=sz["risk_usd"] if sz else 0, notional=0, lev=0, bal_after=bal))
                continue
            q = sz["qty"]
            d = rec["dir"]
            mkf = fees.get("maker", tk)
            rem, gross_u, fee_u = 1.0, 0.0, tk * rec["entry"]
            for _, ppx, pf, pm in rec.get("partials", []) or []:
                gross_u += pf * d * (ppx - rec["entry"])
                fee_u += pf * (mkf if pm else tk) * ppx
                rem -= pf
            gross = (gross_u + rem * d * (rec["exit"] - rec["entry"])) * q
            fee = (fee_u + rem * (mkf if rec.get("exit_maker") else tk) * rec["exit"]) * q
            fund = sum(d * r * px for _, r, px in rec.get("funding", [])) * q if fees.get("include_funding", True) else 0.0
            pnl = gross - fee - fund
            bal += pnl
            r_mult = pnl / sz["risk_usd"] if sz["risk_usd"] > 0 else 0
            closed_r.append(r_mult)
            out_trades.append(dict(rec, skipped=False, shadow=False, edge_w=sz.get("edge_w", 1.0), qty=q, pnl=pnl, r=r_mult, fees=fee, funding_usd=fund,
                                   risk_usd=sz["risk_usd"], risk_pct=sz["risk_pct"], notional=q * rec["entry"],
                                   lev=q * rec["entry"] / sz["bal_at_entry"] if sz["bal_at_entry"] > 0 else 0,
                                   sl_pct=abs(rec["entry"] - rec["sl0"]) / rec["entry"] * 100, bal_after=bal,
                                   r_unit=unit_r(rec, fees)))
            curve.append([int(rec["exit_ts"]), bal])
    # open positions: size + unrealized
    out_open = []
    unreal = 0.0
    for idx, p in enumerate(opens):
        sz = sizes.get(("o", idx), {"qty": 0, "risk_usd": 0, "ok": False})
        mk = (marks or {}).get(p["sym"])
        q = sz["qty"]
        u = 0.0
        if mk and q:
            u = p["dir"] * (mk - p["entry"]) * q - tk * p["entry"] * q
            u -= sum(p["dir"] * r * px for _, r, px in p.get("funding", [])) * q
        unreal += u
        risk_now = p["dir"] * (p["entry"] - p["sl"]) * q + tk * (p["entry"] + p["sl"]) * q
        out_open.append(dict(p, qty=q, risk_usd=sz["risk_usd"], notional=q * p["entry"], mark=mk, upnl=u,
                             r_open=u / sz["risk_usd"] if sz["risk_usd"] else 0, risk_now=risk_now,
                             lev=q * p["entry"] / sz.get("bal_at_entry", bal) if sz.get("bal_at_entry") else 0,
                             sl_pct=abs(p["entry"] - p["sl0"]) / p["entry"] * 100, ok=sz["ok"]))
    if end_ms:
        curve.append([int(end_ms), bal])
    return {"trades": out_trades, "open": out_open, "balance": bal, "start_balance": start_bal,
            "curve": curve, "unrealized": unreal}


def kelly(rs):
    rs = [r for r in rs if r is not None]
    if len(rs) < 2:
        return {"kelly": 0.0, "half": 0.0, "w": 0.0, "b": 0.0}
    wins = [r for r in rs if r > 0]
    losses = [-r for r in rs if r <= 0]
    w = len(wins) / len(rs)
    aw = sum(wins) / len(wins) if wins else 0.0
    al = sum(losses) / len(losses) if losses else 0.0
    if al <= 0:
        return {"kelly": 1.0 if aw > 0 else 0.0, "half": 0.5, "w": w, "b": math.inf}
    b = aw / al
    k = w - (1 - w) / b if b > 0 else -1.0
    return {"kelly": k, "half": k / 2, "w": w, "b": b}


# ---------------------------------------------------------------- stats + journal
def compute_stats(rep, cfg, settings, start_ms, end_ms):
    tr = [t for t in rep["trades"] if not t.get("skipped")]
    sb = rep["start_balance"]
    bal = rep["balance"]
    n = len(tr)
    wins = [t for t in tr if t["pnl"] > 0]
    losses = [t for t in tr if t["pnl"] <= 0]
    rs = [t["r"] for t in tr]
    cum = np.cumsum(rs) if rs else np.array([0.0])
    peak_r = np.maximum.accumulate(np.concatenate([[0.0], cum]))
    dd_r = float(np.max(peak_r - np.concatenate([[0.0], cum]))) if rs else 0.0
    eq = [p[1] for p in rep["curve"]]
    peak, mdd = -1e18, 0.0
    for v in eq:
        peak = max(peak, v)
        if peak > 0:
            mdd = max(mdd, (peak - v) / peak)
    # longest time under water (days from an equity high to the next new high, or to the end)
    uw, pk_v, pk_t = 0.0, -1e18, start_ms
    for t_, v in rep["curve"]:
        if v >= pk_v:
            uw = max(uw, (t_ - pk_t) / DAY)
            pk_v, pk_t = v, t_
    uw = max(uw, (end_ms - pk_t) / DAY)
    # daily drawdown (worst calendar day, local tz)
    z = tz(cfg)
    day_pnl = {}
    day_start = {}
    for t in tr:
        dkey = datetime.fromtimestamp(t["exit_ts"] / 1000, z).strftime("%Y-%m-%d")
        day_pnl[dkey] = day_pnl.get(dkey, 0.0) + t["pnl"]
        day_start.setdefault(dkey, t["bal_after"] - t["pnl"])
    max_daily = 0.0
    for k, v in day_pnl.items():
        if v < 0 and day_start[k] > 0:
            max_daily = max(max_daily, -v / day_start[k])
    cw = cl = mcw = mcl = 0
    for t in tr:
        if t["pnl"] > 0:
            cw, cl = cw + 1, 0
        else:
            cl, cw = cl + 1, 0
        mcw, mcl = max(mcw, cw), max(mcl, cl)
    gw = sum(t["pnl"] for t in wins)
    gl = -sum(t["pnl"] for t in losses)
    days = max((end_ms - start_ms) / DAY, 1e-9)
    years = days / 365.25
    cagr = ((bal / sb) ** (1 / years) - 1) * 100 if sb > 0 and bal > 0 and years > 0.02 else \
        ((bal / sb - 1) * 100 / years if years > 0 else 0)
    avg_w_r = sum(t["r"] for t in wins) / len(wins) if wins else 0
    avg_l_r = sum(t["r"] for t in losses) / len(losses) if losses else 0
    fees_total = sum(t["fees"] for t in tr)
    k = kelly(rs)
    tk = cfg["fees"]["taker"] * 100
    mk = cfg["fees"]["maker"] * 100
    return {
        "total_trades": n, "wins": len(wins), "losses": len(losses),
        "winrate": len(wins) / n * 100 if n else 0.0,
        "cum_r": float(sum(rs)), "max_dd_r": dd_r,
        "account": bal, "equity": bal + rep.get("unrealized", 0.0), "start_balance": sb,
        "risk_pct": float(settings.get("risk_pct", cfg["account"]["risk_pct"])),
        "kelly_sizing": bool(settings.get("kelly_sizing")),
        "fee_setup": f"Bybit taker {tk:.3f}% / maker {mk:.3f}% (market in, stop-market out)",
        "days": days, "net_pnl": bal - sb, "net_pct": (bal / sb - 1) * 100 if sb else 0,
        "profit_factor": (gw / gl) if gl > 0 else (math.inf if gw > 0 else 0.0),
        "annual_return": cagr,
        "avg_win": gw / len(wins) if wins else 0.0, "avg_loss": -gl / len(losses) if losses else 0.0,
        "avg_win_r": avg_w_r, "avg_loss_r": avg_l_r,
        "max_dd_pct": mdd * 100, "max_daily_dd_pct": max_daily * 100,
        "max_consec_wins": mcw, "max_consec_losses": mcl, "underwater_days": uw,
        "avg_rtr": (avg_w_r / abs(avg_l_r)) if avg_l_r else (avg_w_r if wins else 0.0),
        "avg_r": float(np.mean(rs)) if rs else 0.0,
        "total_fees": fees_total, "avg_fees": fees_total / n if n else 0.0,
        "total_funding": sum(t["funding_usd"] for t in tr),
        "avg_order": sum(t["notional"] for t in tr) / n if n else 0.0,
        "avg_leverage": sum(t["lev"] for t in tr) / n if n else 0.0,
        "avg_time_h": sum((t["exit_ts"] - t["entry_ts"]) for t in tr) / n / 3_600_000 if n else 0.0,
        "avg_sl_pct": sum(t["sl_pct"] for t in tr) / n if n else 0.0,
        "kelly": k["kelly"] * 100, "half_kelly": k["half"] * 100,
        "skipped": sum(1 for t in rep["trades"] if t.get("skipped")),
        "open_positions": len(rep["open"]), "unrealized": rep.get("unrealized", 0.0),
        "longs": sum(1 for t in tr if t["dir"] == 1), "shorts": sum(1 for t in tr if t["dir"] == -1),
        "best_r": max(rs) if rs else 0.0, "worst_r": min(rs) if rs else 0.0,
    }


def journal(rep, cfg, start_ms, end_ms):
    """Balances per year / month / ISO-week / day (local timezone). Each row knows its parent."""
    z = tz(cfg)
    tr = sorted([t for t in rep["trades"] if not t.get("skipped")], key=lambda t: t["exit_ts"])
    sb = rep["start_balance"]
    d0 = datetime.fromtimestamp(start_ms / 1000, z).date()
    d1 = datetime.fromtimestamp(end_ms / 1000, z).date()
    if (d1 - d0).days > 3700:
        d0 = d1 - timedelta(days=3700)
    days = {}
    bal = sb
    ti = 0
    cur = d0
    while cur <= d1:
        key = cur.isoformat()
        st = bal
        pnl, n, w, r = 0.0, 0, 0, 0.0
        while ti < len(tr) and datetime.fromtimestamp(tr[ti]["exit_ts"] / 1000, z).date() == cur:
            t = tr[ti]
            pnl += t["pnl"]
            r += t["r"]
            n += 1
            w += 1 if t["pnl"] > 0 else 0
            ti += 1
        while ti < len(tr) and datetime.fromtimestamp(tr[ti]["exit_ts"] / 1000, z).date() < cur:
            ti += 1
        bal = st + pnl
        iso = cur.isocalendar()
        days[key] = {"key": key, "label": cur.strftime("%a %d %b"), "start": st, "end": bal, "pnl": pnl, "r": r,
                     "trades": n, "wins": w, "week": f"{iso[0]}-W{iso[1]:02d}", "month": cur.strftime("%Y-%m"),
                     "year": cur.strftime("%Y")}
        cur += timedelta(days=1)

    def group(field, label_fn, parent_fn):
        g = {}
        for d in days.values():
            k = d[field]
            if k not in g:
                g[k] = {"key": k, "label": label_fn(k, d), "start": d["start"], "end": d["end"], "pnl": 0.0, "r": 0.0,
                        "trades": 0, "wins": 0, "parent": parent_fn(d), "first": d["key"]}
            x = g[k]
            x["end"] = d["end"]
            x["pnl"] += d["pnl"]
            x["r"] += d["r"]
            x["trades"] += d["trades"]
            x["wins"] += d["wins"]
        return list(g.values())

    def wk_label(k, d):
        y, w = k.split("-W")
        mon = datetime.fromisocalendar(int(y), int(w), 1).date()
        return f"W{w} · {mon.strftime('%d %b')} - {(mon + timedelta(days=6)).strftime('%d %b')}"

    years = group("year", lambda k, d: k, lambda d: None)
    months = group("month", lambda k, d: datetime.strptime(k, "%Y-%m").strftime("%B %Y"), lambda d: d["year"])
    weeks = group("week", wk_label, lambda d: d["month"])
    dl = list(days.values())
    for d in dl:
        d["parent"] = d["week"]
    for rows in (years, months, weeks, dl):
        for x in rows:
            x["pct"] = (x["end"] / x["start"] - 1) * 100 if x["start"] else 0.0
    return {"year": years, "month": months, "week": weeks, "day": dl}


def account_bundle(trades, open_positions, cfg, settings, symbols, start_ms, end_ms, marks=None,
                   with_trades=True):
    rep = replay(trades, cfg, settings, symbols, open_positions, marks, start_ms, end_ms)
    st = compute_stats(rep, cfg, settings, start_ms, end_ms)
    jr = journal(rep, cfg, start_ms, end_ms)
    curve = rep["curve"]
    if rep["open"] and end_ms:
        curve = curve + [[int(end_ms), rep["balance"] + rep["unrealized"]]]
    out = {"stats": st, "curve": curve, "open": rep["open"], "journal": jr}
    if with_trades:
        out["trades"] = rep["trades"]
    return out


ACCOUNTS = {"BTC": ["BTCUSDT"], "SOL": ["SOLUSDT"], "ALL": ["BTCUSDT", "SOLUSDT"]}


def fmt_discord_trade(kind, t, bundle_rows):
    """bundle_rows: list of (account_name, sized_row)"""
    side = "🟢 LONG" if t["dir"] == 1 else "🔴 SHORT"
    lines = []
    if kind == "open":
        title = f"💜 FEBRUARY · {t['sym']} · {side} OPENED"
        lines.append(f"Entry `{t['entry']:.6g}`  SL `{t['sl']:.6g}`  ({abs(t['entry'] - t['sl']) / t['entry'] * 100:.2f}%)")
        for name, r in bundle_rows:
            lines.append(f"**{name}** qty `{r.get('qty', 0):.6g}` · notional `${r.get('notional', 0):.2f}` · risk `${r.get('risk_usd', 0):.2f}`")
        color = 0x1fe0a0 if t["dir"] == 1 else 0xff4d6d
    elif kind == "partial":
        title = f"💜 FEBRUARY · {t['sym']} · partial take-profit"
        lines.append(f"{t.get('partials', [[0, 0, 0.5]])[-1][2] * 100:.0f}% closed at `{t.get('price', 0):.6g}` (entry `{t['entry']:.6g}`)")
        color = 0xc79bff
    elif kind == "trail":
        title = f"💜 FEBRUARY · {t['sym']} · stop trailed"
        lines.append(f"New SL `{t['sl']:.6g}` (entry `{t['entry']:.6g}`)")
        color = 0x7cc4ff
    else:
        title = f"💜 FEBRUARY · {t['sym']} · {side} CLOSED ({t.get('reason', '')})"
        lines.append(f"Entry `{t['entry']:.6g}` → Exit `{t['exit']:.6g}`")
        for name, r in bundle_rows:
            lines.append(f"**{name}** PnL `${r.get('pnl', 0):+.2f}` · `{r.get('r', 0):+.2f} R` · fees `${r.get('fees', 0):.3f}`")
        color = 0x1fe0a0 if any(r.get("pnl", 0) > 0 for _, r in bundle_rows) else 0xff4d6d
    return {"embeds": [{"title": title, "description": "\n".join(lines), "color": color,
                        "timestamp": datetime.now(timezone.utc).isoformat()}]}
