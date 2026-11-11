#!/usr/bin/env python3
"""
FEBRUARY - Bybit DEMO TRADING executor (paper trading on your Bybit demo account).

SAFETY
  * The REST host is hard-coded to https://api-demo.bybit.com and checked before every request.
    Demo API keys only work there, real-account keys are rejected there -> no real money can move.
  * Real-money (live) trading is NOT implemented anywhere in this project.
  * Arming / disarming / flattening from the website needs the PIN from config.json ("demo.arm_pin").

HOW IT WORKS
  The February engine stays the master. Every engine event is mirrored on the demo account:
    open    -> market order, then stop-loss on the position (Set Trading Stop) + reduce-only limit
               take-profit at zone C (+ reduce-only limit for a partial take-profit if enabled)
    trail   -> stop-loss moved on the exchange
    close   -> whatever is left is closed with a reduce-only market order, open orders cancelled
  A reconcile loop (every 60 s) re-applies the stop if it differs, and closes exchange positions
  the engine does not hold (for example after a restart).

  Sizing: risk % from the website x the demo wallet balance (or "capital_usd" if set and smaller),
  stop distance + round-trip taker fees = 1 R, exactly like the paper accounts.
"""
import hashlib
import hmac
import json
import math
import os
import threading
import time
import urllib.parse

import requests

import february_core as core

DEMO_HOST = "https://api-demo.bybit.com"   # never change: demo only
CONTROL_PATH = os.path.join(core.DATA_DIR, "demo_control.json")   # written by the website
STATE_PATH = os.path.join(core.DATA_DIR, "demo_state.json")       # written by the trader
RECV = "10000"


class DemoError(RuntimeError):
    pass


class BybitDemo:
    """Minimal signed Bybit v5 REST client, locked to the demo host."""

    def __init__(self, key, secret, host=DEMO_HOST):
        if host != DEMO_HOST and not host.startswith("http://127.0.0.1"):  # 127.0.0.1 only for the offline test mock
            raise DemoError("refusing to trade on anything but the Bybit DEMO host")
        self.host, self.key, self.secret = host, key or "", secret or ""
        self.s = requests.Session()
        self.s.headers["User-Agent"] = f"February-demo/{core.VERSION}"
        self.lock = threading.Lock()
        self.time_offset = 0

    def ready(self):
        return bool(self.key and self.secret)

    def _sign(self, ts, payload):
        msg = f"{ts}{self.key}{RECV}{payload}"
        return hmac.new(self.secret.encode(), msg.encode(), hashlib.sha256).hexdigest()

    def _req(self, method, path, params=None, signed=True, retries=2):
        assert self.host == DEMO_HOST or self.host.startswith("http://127.0.0.1"), "demo host only"
        params = {k: v for k, v in (params or {}).items() if v is not None}
        err = None
        for attempt in range(retries + 1):
            try:
                ts = str(int(time.time() * 1000) + self.time_offset)
                headers = {}
                if method == "GET":
                    qs = urllib.parse.urlencode(params)
                    url = self.host + path + ("?" + qs if qs else "")
                    payload, body = qs, None
                else:
                    url = self.host + path
                    body = json.dumps(params, separators=(",", ":"))
                    payload = body
                    headers["Content-Type"] = "application/json"
                if signed:
                    if not self.ready():
                        raise DemoError("demo API key / secret missing in config.json")
                    headers.update({"X-BAPI-API-KEY": self.key, "X-BAPI-TIMESTAMP": ts, "X-BAPI-RECV-WINDOW": RECV,
                                    "X-BAPI-SIGN": self._sign(ts, payload)})
                with self.lock:
                    r = self.s.request(method, url, data=body, headers=headers, timeout=15)
                j = r.json()
                rc = j.get("retCode")
                if rc == 10002 and attempt < retries:  # timestamp out of window -> resync once
                    self.sync_time()
                    continue
                if rc not in (0, None):
                    raise DemoError(f"{path} retCode={rc} {j.get('retMsg')}")
                return j.get("result", {})
            except DemoError:
                raise
            except Exception as e:
                err = e
                time.sleep(1.0 * (attempt + 1))
        raise DemoError(f"{path} request failed: {err}")

    def sync_time(self):
        try:
            r = self.s.get(self.host + "/v5/market/time", timeout=10).json()
            self.time_offset = int(r["time"]) - int(time.time() * 1000)
        except Exception:
            pass

    # ---- wrappers
    def wallet(self):
        res = self._req("GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED"})
        acc = (res.get("list") or [{}])[0]
        usdt = next((c for c in acc.get("coin", []) if c.get("coin") == "USDT"), {})
        f = lambda x: float(x) if x not in (None, "") else 0.0
        return {"equity": f(acc.get("totalEquity")), "wallet": f(acc.get("totalWalletBalance")),
                "available": f(acc.get("totalAvailableBalance")), "usdt_wallet": f(usdt.get("walletBalance")),
                "usdt_equity": f(usdt.get("equity")), "upnl": f(acc.get("totalPerpUPL"))}

    def instrument(self, sym):
        res = self._req("GET", "/v5/market/instruments-info", {"category": "linear", "symbol": sym}, signed=False)
        it = res["list"][0]
        lf, pf = it["lotSizeFilter"], it["priceFilter"]
        return {"qty_step": float(lf["qtyStep"]),
                "min_qty": float(lf["minOrderQty"]), "min_notional": float(lf.get("minNotionalValue") or 5),
                "tick": float(pf["tickSize"]), "max_lev": float(it.get("leverageFilter", {}).get("maxLeverage") or 50)}

    def positions(self, sym=None):
        p = {"category": "linear", "settleCoin": "USDT"} if sym is None else {"category": "linear", "symbol": sym}
        res = self._req("GET", "/v5/position/list", p)
        out = []
        for x in res.get("list", []):
            if float(x.get("size") or 0) > 0:
                out.append({"sym": x["symbol"], "side": x["side"], "dir": 1 if x["side"] == "Buy" else -1,
                            "size": float(x["size"]), "entry": float(x.get("avgPrice") or 0),
                            "sl": float(x.get("stopLoss") or 0), "tp": float(x.get("takeProfit") or 0),
                            "upnl": float(x.get("unrealisedPnl") or 0), "lev": x.get("leverage"),
                            "mark": float(x.get("markPrice") or 0), "value": float(x.get("positionValue") or 0)})
        return out

    def open_orders(self, sym=None):
        p = {"category": "linear", "settleCoin": "USDT"} if sym is None else {"category": "linear", "symbol": sym}
        res = self._req("GET", "/v5/order/realtime", p)
        return [{"sym": o["symbol"], "side": o["side"], "type": o["orderType"], "price": float(o.get("price") or 0),
                 "qty": float(o.get("qty") or 0), "reduce": o.get("reduceOnly"), "id": o["orderId"],
                 "link": o.get("orderLinkId"), "status": o.get("orderStatus")} for o in res.get("list", [])]

    def closed_pnl(self, limit=50):
        res = self._req("GET", "/v5/position/closed-pnl", {"category": "linear", "limit": limit})
        return [{"sym": x["symbol"], "side": x["side"], "qty": float(x.get("qty") or 0),
                 "entry": float(x.get("avgEntryPrice") or 0), "exit": float(x.get("avgExitPrice") or 0),
                 "pnl": float(x.get("closedPnl") or 0), "ts": int(x.get("updatedTime") or x.get("createdTime") or 0)}
                for x in res.get("list", [])]

    def set_leverage(self, sym, lev):
        try:
            self._req("POST", "/v5/position/set-leverage", {"category": "linear", "symbol": sym,
                                                             "buyLeverage": str(lev), "sellLeverage": str(lev)})
        except DemoError as e:
            if "110043" not in str(e):  # "leverage not modified" is fine
                raise

    def order(self, sym, side, qty, otype="Market", price=None, reduce=False, link=None):
        p = {"category": "linear", "symbol": sym, "side": side, "orderType": otype, "qty": qty,
             "positionIdx": 0, "reduceOnly": reduce, "orderLinkId": link}
        if otype == "Limit":
            p["price"] = price
            p["timeInForce"] = "GTC"
        return self._req("POST", "/v5/order/create", p)

    def set_stop(self, sym, sl):
        return self._req("POST", "/v5/position/trading-stop", {"category": "linear", "symbol": sym,
                                                               "tpslMode": "Full", "positionIdx": 0,
                                                               "stopLoss": sl})

    def cancel_all(self, sym):
        return self._req("POST", "/v5/order/cancel-all", {"category": "linear", "symbol": sym})


def fmt_num(x, step):
    """round DOWN to the exchange step and format without float noise."""
    dec = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    v = math.floor(x / step + 1e-9) * step
    return f"{v:.{dec}f}"


def fmt_px(x, tick):
    dec = max(0, -int(math.floor(math.log10(tick)))) if tick < 1 else 0
    v = round(round(x / tick) * tick, dec)
    return f"{v:.{dec}f}"


class DemoExecutor:
    """Lives inside the paper trader. Never raises into the trader loop."""

    def __init__(self, cfg, log, host=DEMO_HOST):
        self.cfg, self.log = cfg, log
        d = cfg.get("demo", {})
        self.client = BybitDemo(d.get("api_key", ""), d.get("api_secret", ""), host=host)
        self.st = core.read_json(STATE_PATH, {}) or {}
        self.st.setdefault("mirror", {})   # sym -> {entry_ts, qty, dir, tp_link, part_link}
        self.st.setdefault("actions", [])
        self.instr = {}
        self.last_reconcile = 0
        self.lev_done = set()

    # ---- control / state
    def control(self):
        c = core.read_json(CONTROL_PATH, {}) or {}
        return c

    def armed(self):
        c = self.control()
        if "armed" in c:
            return bool(c["armed"]) and self.client.ready()
        return bool(self.cfg.get("demo", {}).get("enabled")) and self.client.ready()

    def act(self, sym, what, ok=True, detail=""):
        self.st["actions"].append({"ts": core.now_ms(), "sym": sym, "what": what, "ok": ok, "detail": str(detail)[:300]})
        self.st["actions"] = self.st["actions"][-200:]
        (self.log.info if ok else self.log.error)(f"DEMO {sym}: {what} {detail}")
        self.save()

    def save(self):
        self.st["armed"] = self.armed()
        self.st["ts"] = core.now_ms()
        core.save_json(STATE_PATH, self.st)

    def inst(self, sym):
        if sym not in self.instr:
            try:
                self.instr[sym] = self.client.instrument(sym)
            except Exception:
                self.instr[sym] = dict(self.cfg["instruments"].get(sym, {}), max_lev=50)
                self.instr[sym].setdefault("tick", 0.01)
        return self.instr[sym]

    # ---- sizing (same 1 R definition as the paper accounts)
    def size(self, sym, d, entry, sl, settings, trades):
        dc = self.cfg.get("demo", {})
        w = self.client.wallet()
        base = w["usdt_wallet"] or w["wallet"]   # USDT only (demo also holds BTC/ETH which must not count)
        cap = float(dc.get("capital_usd") or 0)
        if cap > 0:
            base = min(base, cap)
        risk = float(settings.get("risk_pct", 5)) / 100.0
        # edge monitor (same rule as the website): shadow / half size while the last N trades are <= 0 R
        n = int(settings.get("edge_n") or 0)
        if n:
            rs = [core.unit_r(t, self.cfg["fees"]) for t in sorted(trades, key=lambda x: x["exit_ts"])]
            if len(rs) >= n and sum(rs[-n:]) <= 0:
                risk *= float(settings.get("edge_low") or 0)
        tk = self.cfg["fees"]["taker"]
        unit = abs(entry - sl) + tk * (entry + sl)
        if risk <= 0 or unit <= 0 or base <= 0:
            return 0.0, base, 0.0
        risk_usd = base * risk
        qty = risk_usd / unit
        lev = float(dc.get("leverage", 10))
        qty = min(qty, lev * base / entry * 0.95)
        it = self.inst(sym)
        q = math.floor(qty / it["qty_step"] + 1e-9) * it["qty_step"]
        if q < it["min_qty"] or q * entry < it.get("min_notional", 5):
            return 0.0, base, risk_usd
        return q, base, risk_usd

    # ---- event handlers (called by the trader)
    def on_open(self, sym, pos, mark, settings, trades):
        if not self.armed():
            return
        try:
            it = self.inst(sym)
            lev = int(min(it.get("max_lev", 50), float(self.cfg.get("demo", {}).get("leverage", 10))))
            if sym not in self.lev_done:
                self.client.set_leverage(sym, lev)
                self.lev_done.add(sym)
            d = pos["dir"]
            entry = mark or pos["entry"]
            q, base, risk_usd = self.size(sym, d, entry, pos["sl"], settings, trades)
            if q <= 0:
                self.act(sym, "entry skipped", True, f"size below Bybit minimum or edge monitor shadow (risk ${risk_usd:.2f})")
                return
            if (d == 1 and entry <= pos["sl"]) or (d == -1 and entry >= pos["sl"]):
                self.act(sym, "entry skipped", True, "price already beyond the stop")
                return
            qs = fmt_num(q, it["qty_step"])
            tag = f"feb-{sym[:3].lower()}-{pos['entry_ts'] // 1000}"
            self.client.order(sym, "Buy" if d == 1 else "Sell", qs, link=tag + "-in")
            time.sleep(0.6)
            self.client.set_stop(sym, fmt_px(pos["sl"], it["tick"]))
            m = {"entry_ts": pos["entry_ts"], "qty": float(qs), "dir": d, "tag": tag, "sl": pos["sl"], "risk_usd": risk_usd}
            exit_side = "Sell" if d == 1 else "Buy"
            pr = self.cfg["strategy"].get("partial_r", 0)
            rest = float(qs)
            if pr:
                pq = fmt_num(float(qs) * float(self.cfg["strategy"].get("partial_frac", 0.5)), it["qty_step"])
                if float(pq) >= it["min_qty"]:
                    ptp = pos["entry"] + d * pr * abs(pos["entry"] - pos["sl0"])
                    self.client.order(sym, exit_side, pq, "Limit", fmt_px(ptp, it["tick"]), True, tag + "-part")
                    rest -= float(pq)
            if pos.get("tp"):
                tq = fmt_num(rest, it["qty_step"])
                if float(tq) >= it["min_qty"]:
                    self.client.order(sym, exit_side, tq, "Limit", fmt_px(pos["tp"], it["tick"]), True, tag + "-tp")
            self.st["mirror"][sym] = m
            self.act(sym, f"OPEN {'LONG' if d == 1 else 'SHORT'} {qs}", True,
                     f"SL {fmt_px(pos['sl'], it['tick'])} TP {pos.get('tp') and fmt_px(pos['tp'], it['tick'])} "
                     f"risk ${risk_usd:.2f} of ${base:.2f}")
        except Exception as e:
            self.act(sym, "OPEN failed", False, e)

    def on_trail(self, sym, sl):
        m = self.st["mirror"].get(sym)
        if not m or not self.client.ready():
            return
        try:
            if not self.client.positions(sym):
                return
            self.client.set_stop(sym, fmt_px(sl, self.inst(sym)["tick"]))
            m["sl"] = sl
            self.act(sym, "stop moved", True, fmt_px(sl, self.inst(sym)["tick"]))
        except Exception as e:
            self.act(sym, "stop move failed", False, e)

    def on_close(self, sym, reason):
        m = self.st["mirror"].pop(sym, None)
        if m is None or not self.client.ready():
            self.save()
            return
        self.flatten(sym, f"engine closed ({reason})")

    def flatten(self, sym, why):
        try:
            for p in self.client.positions(sym):
                self.client.order(sym, "Sell" if p["dir"] == 1 else "Buy", fmt_num(p["size"], self.inst(sym)["qty_step"]),
                                  reduce=True, link=f"feb-close-{core.now_ms() // 1000}")
                self.act(sym, "position closed (market)", True, why)
            self.client.cancel_all(sym)
            self.st["mirror"].pop(sym, None)
            self.save()
        except Exception as e:
            self.act(sym, "close failed", False, e)

    # ---- periodic: control requests + reconcile
    def tick(self, engines):
        c = self.control()
        if c.get("flatten_req") and c.get("flatten_req") != self.st.get("flatten_done"):
            for sym in self.cfg["symbols"]:
                self.flatten(sym, "flatten requested from the website")
            self.st["flatten_done"] = c["flatten_req"]
            self.save()
        if not self.client.ready() or time.time() - self.last_reconcile < 60:
            return
        self.last_reconcile = time.time()
        try:
            ex = {p["sym"]: p for p in self.client.positions()}
            self.st["last_ok"] = core.now_ms()
            for sym in self.cfg["symbols"]:
                eng = engines.get(sym)
                ps = eng.pos if eng else None
                m = self.st["mirror"].get(sym)
                p = ex.get(sym)
                if p and (not ps or not m):
                    if self.armed() or m:
                        self.flatten(sym, "exchange position without an engine position")
                elif m and not p:
                    self.st["mirror"].pop(sym, None)
                    self.act(sym, "exchange position gone", True, "stop / take-profit filled on Bybit")
                elif m and p and ps:
                    want = float(fmt_px(ps["sl"], self.inst(sym)["tick"]))
                    if abs(p["sl"] - want) > self.inst(sym)["tick"] / 2:
                        self.client.set_stop(sym, fmt_px(ps["sl"], self.inst(sym)["tick"]))
                        self.act(sym, "stop re-synced", True, f"{p['sl']} -> {want}")
            self.save()
        except Exception as e:
            self.log.warning(f"DEMO reconcile failed: {e}")


def snapshot(cfg):
    """read-only view for the website (never returns keys)."""
    d = cfg.get("demo", {})
    c = core.read_json(CONTROL_PATH, {}) or {}
    st = core.read_json(STATE_PATH, {}) or {}
    out = {"configured": bool(d.get("api_key") and d.get("api_secret")), "pin_set": bool(d.get("arm_pin")),
           "armed": bool(c.get("armed", d.get("enabled", False))) and bool(d.get("api_key")),
           "host": DEMO_HOST, "leverage": d.get("leverage", 10), "capital_usd": d.get("capital_usd", 0),
           "actions": list(reversed(st.get("actions", [])))[:60], "mirror": st.get("mirror", {}),
           "trader_seen": st.get("ts"), "live_locked": True}
    if not out["configured"]:
        return out
    try:
        cl = BybitDemo(d["api_key"], d["api_secret"], host=d.get("_test_host", DEMO_HOST))
        out["wallet"] = cl.wallet()
        out["positions"] = cl.positions()
        out["orders"] = cl.open_orders()
        out["closed"] = cl.closed_pnl(50)
        out["api_ok"] = True
    except Exception as e:
        out["api_ok"] = False
        out["error"] = str(e)
    return out
