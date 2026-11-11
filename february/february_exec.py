#!/usr/bin/env python3
"""
FEBRUARY - Bybit order executor for two fully separated modes:

  DEMO  -> https://api-demo.bybit.com   (paper money on your Bybit demo account)
  LIVE  -> https://api.bybit.com        (REAL MONEY, bybit.com global, USDT perpetuals)

Each mode has its own API keys, PIN, control file, state file and limits (config.json "demo" / "live").
The February engine is always the master; the executor only mirrors its events:

  open    -> market order, stop-loss on the position (Set Trading Stop), reduce-only limit take-profit
             at zone C (+ reduce-only limit partial if partial_r is on)
  trail   -> stop moved on the exchange
  close   -> remaining position closed with a reduce-only market order, open orders cancelled
  every 60 s reconcile: re-apply a stop that differs, close BTC/SOL positions the engine does not hold

LIVE safety (checked before EVERY entry, independent of the strategy):
  * hard cap on the loss of a single trade (stop distance + round-trip fees) -> trade skipped if above
  * daily loss stop (realised wallet change since local midnight) -> no new entries until tomorrow
  * equity floor -> HALT: no new entries until you reset with the PIN on the website
  * margin check, leverage cap, positions need a stop on the exchange (re-applied every minute)
  * arming needs the PIN (+ typing LIVE on the website); FLATTEN closes everything and disarms
  * the host can only be one of the two constants below
"""
import hashlib
import hmac
import json
import math
import os
import threading
import time
import urllib.parse
from datetime import datetime

import requests

import february_core as core

HOSTS = {"demo": "https://api-demo.bybit.com", "live": "https://api.bybit.com"}
RECV = "10000"


def paths(kind):
    return (os.path.join(core.DATA_DIR, f"{kind}_control.json"), os.path.join(core.DATA_DIR, f"{kind}_state.json"))


class ExecError(RuntimeError):
    pass


class BybitPrivate:
    """Minimal signed Bybit v5 REST client. Host must be one of HOSTS (or a localhost mock for tests)."""

    def __init__(self, key, secret, host):
        if host not in HOSTS.values() and not host.startswith("http://127.0.0.1"):
            raise ExecError(f"host {host} not allowed")
        self.host, self.key, self.secret = host, key or "", secret or ""
        self.s = requests.Session()
        self.s.headers["User-Agent"] = f"February/{core.VERSION}"
        self.lock = threading.Lock()
        self.time_offset = 0

    def ready(self):
        return bool(self.key and self.secret)

    def _sign(self, ts, payload):
        return hmac.new(self.secret.encode(), f"{ts}{self.key}{RECV}{payload}".encode(), hashlib.sha256).hexdigest()

    def _req(self, method, path, params=None, signed=True, retries=2):
        params = {k: v for k, v in (params or {}).items() if v is not None}
        err = None
        for attempt in range(retries + 1):
            try:
                ts = str(int(time.time() * 1000) + self.time_offset)
                headers = {}
                if method == "GET":
                    qs = urllib.parse.urlencode(params)
                    url, payload, body = self.host + path + ("?" + qs if qs else ""), qs, None
                else:
                    url = self.host + path
                    body = json.dumps(params, separators=(",", ":"))
                    payload = body
                    headers["Content-Type"] = "application/json"
                if signed:
                    if not self.ready():
                        raise ExecError("API key / secret missing in config.json")
                    headers.update({"X-BAPI-API-KEY": self.key, "X-BAPI-TIMESTAMP": ts, "X-BAPI-RECV-WINDOW": RECV,
                                    "X-BAPI-SIGN": self._sign(ts, payload)})
                with self.lock:
                    r = self.s.request(method, url, data=body, headers=headers, timeout=15)
                j = r.json()
                rc = j.get("retCode")
                if rc == 10002 and attempt < retries:
                    self.sync_time()
                    continue
                if rc not in (0, None):
                    raise ExecError(f"{path} retCode={rc} {j.get('retMsg')}")
                return j.get("result", {})
            except ExecError:
                raise
            except Exception as e:
                err = e
                time.sleep(1.0 * (attempt + 1))
        raise ExecError(f"{path} request failed: {err}")

    def sync_time(self):
        try:
            r = self.s.get(self.host + "/v5/market/time", timeout=10).json()
            self.time_offset = int(r["time"]) - int(time.time() * 1000)
        except Exception:
            pass

    def wallet(self):
        res = self._req("GET", "/v5/account/wallet-balance", {"accountType": "UNIFIED"})
        acc = (res.get("list") or [{}])[0]
        usdt = next((c for c in acc.get("coin", []) if c.get("coin") == "USDT"), {})
        f = lambda x: float(x) if x not in (None, "") else 0.0
        return {"equity": f(acc.get("totalEquity")), "wallet": f(acc.get("totalWalletBalance")),
                "available": f(acc.get("totalAvailableBalance")), "usdt_wallet": f(usdt.get("walletBalance")),
                "usdt_equity": f(usdt.get("equity")), "upnl": f(acc.get("totalPerpUPL"))}

    def account_info(self):
        return self._req("GET", "/v5/account/info")

    def instrument(self, sym):
        it = self._req("GET", "/v5/market/instruments-info", {"category": "linear", "symbol": sym}, signed=False)["list"][0]
        lf, pf = it["lotSizeFilter"], it["priceFilter"]
        return {"qty_step": float(lf["qtyStep"]), "min_qty": float(lf["minOrderQty"]),
                "min_notional": float(lf.get("minNotionalValue") or 5), "tick": float(pf["tickSize"]),
                "max_lev": float(it.get("leverageFilter", {}).get("maxLeverage") or 50)}

    def positions(self, sym=None):
        p = {"category": "linear", "settleCoin": "USDT"} if sym is None else {"category": "linear", "symbol": sym}
        out = []
        for x in self._req("GET", "/v5/position/list", p).get("list", []):
            if float(x.get("size") or 0) > 0:
                out.append({"sym": x["symbol"], "side": x["side"], "dir": 1 if x["side"] == "Buy" else -1,
                            "size": float(x["size"]), "entry": float(x.get("avgPrice") or 0),
                            "sl": float(x.get("stopLoss") or 0), "tp": float(x.get("takeProfit") or 0),
                            "upnl": float(x.get("unrealisedPnl") or 0), "lev": x.get("leverage"),
                            "mark": float(x.get("markPrice") or 0), "value": float(x.get("positionValue") or 0),
                            "liq": float(x.get("liqPrice") or 0)})
        return out

    def open_orders(self, sym=None):
        p = {"category": "linear", "settleCoin": "USDT"} if sym is None else {"category": "linear", "symbol": sym}
        return [{"sym": o["symbol"], "side": o["side"], "type": o["orderType"], "price": float(o.get("price") or 0),
                 "qty": float(o.get("qty") or 0), "reduce": o.get("reduceOnly"), "id": o["orderId"],
                 "link": o.get("orderLinkId"), "status": o.get("orderStatus")}
                for o in self._req("GET", "/v5/order/realtime", p).get("list", [])]

    def closed_pnl(self, limit=50):
        return [{"sym": x["symbol"], "side": x["side"], "qty": float(x.get("qty") or 0),
                 "entry": float(x.get("avgEntryPrice") or 0), "exit": float(x.get("avgExitPrice") or 0),
                 "pnl": float(x.get("closedPnl") or 0), "ts": int(x.get("updatedTime") or x.get("createdTime") or 0)}
                for x in self._req("GET", "/v5/position/closed-pnl", {"category": "linear", "limit": limit}).get("list", [])]

    def set_leverage(self, sym, lev):
        try:
            self._req("POST", "/v5/position/set-leverage", {"category": "linear", "symbol": sym,
                                                             "buyLeverage": str(lev), "sellLeverage": str(lev)})
        except ExecError as e:
            if "110043" not in str(e):
                raise

    def order(self, sym, side, qty, otype="Market", price=None, reduce=False, link=None):
        p = {"category": "linear", "symbol": sym, "side": side, "orderType": otype, "qty": qty,
             "positionIdx": 0, "reduceOnly": reduce, "orderLinkId": link}
        if otype == "Limit":
            p["price"], p["timeInForce"] = price, "GTC"
        return self._req("POST", "/v5/order/create", p)

    def set_stop(self, sym, sl):
        return self._req("POST", "/v5/position/trading-stop", {"category": "linear", "symbol": sym, "tpslMode": "Full",
                                                               "positionIdx": 0, "stopLoss": sl})

    def cancel_all(self, sym):
        return self._req("POST", "/v5/order/cancel-all", {"category": "linear", "symbol": sym})


def fmt_num(x, step):
    dec = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    return f"{math.floor(x / step + 1e-9) * step:.{dec}f}"


def fmt_px(x, tick):
    dec = max(0, -int(math.floor(math.log10(tick)))) if tick < 1 else 0
    return f"{round(round(x / tick) * tick, dec):.{dec}f}"


class Executor:
    """Lives inside the paper trader. Never raises into the trader loop."""

    def __init__(self, kind, cfg, log):
        assert kind in HOSTS
        self.kind, self.cfg, self.log = kind, cfg, log
        self.c = cfg.get(kind, {})
        host = self.c.get("_test_host") or HOSTS[kind]
        self.client = BybitPrivate(self.c.get("api_key", ""), self.c.get("api_secret", ""), host)
        self.control_path, self.state_path = paths(kind)
        self.st = core.read_json(self.state_path, {}) or {}
        self.st.setdefault("mirror", {})
        self.st.setdefault("actions", [])
        self.instr = {}
        self.last_reconcile = 0
        self.lev_done = set()
        self.label = "LIVE" if kind == "live" else "DEMO"

    # ---- control / state
    def control(self):
        return core.read_json(self.control_path, {}) or {}

    def armed(self):
        c = self.control()
        a = bool(c["armed"]) if "armed" in c else bool(self.c.get("enabled"))
        return a and self.client.ready()

    def act(self, sym, what, ok=True, detail=""):
        self.st["actions"].append({"ts": core.now_ms(), "sym": sym, "what": what, "ok": ok, "detail": str(detail)[:300]})
        self.st["actions"] = self.st["actions"][-300:]
        (self.log.info if ok else self.log.error)(f"{self.label} {sym}: {what} {detail}")
        self.save()

    def save(self):
        self.st["armed"] = self.armed()
        self.st["ts"] = core.now_ms()
        core.save_json(self.state_path, self.st)

    def inst(self, sym):
        if sym not in self.instr:
            self.instr[sym] = self.client.instrument(sym)
        return self.instr[sym]

    # ---- live guards
    def _day_key(self):
        return datetime.now(core.tz(self.cfg)).strftime("%Y-%m-%d")

    def guards(self, w):
        """returns (allowed, reason). Updates day-start balance and the halt flag."""
        if self.kind != "live":
            return True, ""
        day = self._day_key()
        if self.st.get("day") != day:
            self.st["day"], self.st["day_start"] = day, w["usdt_wallet"] or w["wallet"]
        floor = float(self.c.get("min_equity", 3.0))
        eq = w["usdt_equity"] or w["equity"]
        if eq < floor and not self.st.get("halted"):
            self.st["halted"] = f"equity ${eq:.2f} below floor ${floor:.2f}"
            self.act("ALL", "HALTED", False, self.st["halted"])
        if self.st.get("halted"):
            return False, "halted: " + self.st["halted"]
        day_pnl = (w["usdt_wallet"] or w["wallet"]) - (self.st.get("day_start") or 0)
        mx = float(self.c.get("max_daily_loss", 3.0))
        if day_pnl <= -mx:
            return False, f"daily loss stop hit ({day_pnl:+.2f} <= -{mx:.2f})"
        return True, ""

    # ---- sizing
    def size(self, sym, d, entry, sl, settings, trades, w):
        tk = self.cfg["fees"]["taker"]
        base = w["usdt_wallet"] or w["wallet"]
        cap = float(self.c.get("capital_usd") or 0)
        if cap > 0:
            base = min(base, cap)
        risk = float(self.c.get("risk_pct") or settings.get("risk_pct", 5)) / 100.0
        n = int(settings.get("edge_n") or 0)
        if n:
            rs = [core.unit_r(t, self.cfg["fees"]) for t in sorted(trades, key=lambda x: x["exit_ts"])]
            if len(rs) >= n and sum(rs[-n:]) <= 0:
                risk *= float(settings.get("edge_low") or 0)
        unit = abs(entry - sl) + tk * (entry + sl)
        if risk <= 0 or unit <= 0 or base <= 0:
            return 0.0, base, 0.0, "no risk budget"
        it = self.inst(sym)
        q = math.floor(base * risk / unit / it["qty_step"] + 1e-9) * it["qty_step"]
        min_q = max(it["min_qty"], math.ceil(it.get("min_notional", 5) / entry / it["qty_step"] - 1e-9) * it["qty_step"])
        if q < min_q:
            if self.c.get("allow_min_size", {}).get(sym, False) if isinstance(self.c.get("allow_min_size"), dict) else False:
                q = min_q
            else:
                return 0.0, base, q * unit, f"size {q:g} below Bybit minimum {min_q:g}"
        lev = float(self.c.get("leverage", 10))
        if q * entry > lev * base * 0.95:
            q = math.floor(lev * base * 0.95 / entry / it["qty_step"] + 1e-9) * it["qty_step"]
            if q < min_q:
                return 0.0, base, 0.0, f"needs more than {lev:g}x leverage"
        risk_usd = q * unit
        if self.kind == "live":
            mx = float(self.c.get("max_loss_per_trade", 1.5))
            if risk_usd > mx:
                return 0.0, base, risk_usd, f"loss at stop ${risk_usd:.2f} > hard cap ${mx:.2f}"
            margin = q * entry / lev
            if margin > w["available"] * 0.95:
                return 0.0, base, risk_usd, f"not enough free margin (${w['available']:.2f} for ${margin:.2f})"
        return q, base, risk_usd, ""

    # ---- engine events
    def on_open(self, sym, pos, mark, settings, trades):
        if not self.armed() or sym not in self.c.get("symbols", self.cfg["symbols"]):
            return
        try:
            w = self.client.wallet()
            ok, why = self.guards(w)
            if not ok:
                self.act(sym, "entry blocked", True, why)
                return
            it = self.inst(sym)
            lev = int(min(it.get("max_lev", 50), float(self.c.get("leverage", 10))))
            if sym not in self.lev_done:
                self.client.set_leverage(sym, lev)
                self.lev_done.add(sym)
            d = pos["dir"]
            entry = mark or pos["entry"]
            if (d == 1 and entry <= pos["sl"]) or (d == -1 and entry >= pos["sl"]):
                self.act(sym, "entry skipped", True, "price already beyond the stop")
                return
            q, base, risk_usd, why = self.size(sym, d, entry, pos["sl"], settings, trades, w)
            if q <= 0:
                self.act(sym, "entry skipped", True, why)
                return
            qs = fmt_num(q, it["qty_step"])
            tag = f"feb{self.kind[0]}-{sym[:3].lower()}-{pos['entry_ts'] // 1000}"
            self.client.order(sym, "Buy" if d == 1 else "Sell", qs, link=tag + "-in")
            stop_ok = False
            for attempt in range(4):  # the stop is mandatory - retry, then close if it cannot be placed
                time.sleep(0.5 + attempt)
                try:
                    self.client.set_stop(sym, fmt_px(pos["sl"], it["tick"]))
                    stop_ok = True
                    break
                except Exception as e:
                    err = e
            if not stop_ok:
                self.act(sym, "STOP COULD NOT BE PLACED - closing", False, err)
                self.flatten(sym, "no stop on the exchange")
                return
            exit_side = "Sell" if d == 1 else "Buy"
            rest = float(qs)
            pr = self.cfg["strategy"].get("partial_r", 0)
            if pr:
                pq = fmt_num(float(qs) * float(self.cfg["strategy"].get("partial_frac", 0.5)), it["qty_step"])
                if float(pq) >= it["min_qty"] and float(qs) - float(pq) >= it["min_qty"]:
                    ptp = pos["entry"] + d * pr * abs(pos["entry"] - pos["sl0"])
                    self.client.order(sym, exit_side, pq, "Limit", fmt_px(ptp, it["tick"]), True, tag + "-part")
                    rest -= float(pq)
            if pos.get("tp"):
                tq = fmt_num(rest, it["qty_step"])
                if float(tq) >= it["min_qty"]:
                    self.client.order(sym, exit_side, tq, "Limit", fmt_px(pos["tp"], it["tick"]), True, tag + "-tp")
            self.st["mirror"][sym] = {"entry_ts": pos["entry_ts"], "qty": float(qs), "dir": d, "tag": tag,
                                      "sl": pos["sl"], "risk_usd": risk_usd}
            self.act(sym, f"OPEN {'LONG' if d == 1 else 'SHORT'} {qs}", True,
                     f"SL {fmt_px(pos['sl'], it['tick'])} TP {pos.get('tp') and fmt_px(pos['tp'], it['tick'])} "
                     f"loss at stop ${risk_usd:.2f} (base ${base:.2f}, {lev}x)")
            if self.kind == "live":
                url = self.cfg["discord"].get(sym) or self.cfg["discord"].get("errors")
                core.discord_send(url, f"🔴 **FEBRUARY LIVE** · {sym} {'LONG' if d == 1 else 'SHORT'} {qs} · "
                                       f"SL {fmt_px(pos['sl'], it['tick'])} · loss at stop ${risk_usd:.2f}")
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
                                  reduce=True, link=f"feb{self.kind[0]}-close-{core.now_ms() // 1000}")
                self.act(sym, "position closed (market)", True, why)
            self.client.cancel_all(sym)
            self.st["mirror"].pop(sym, None)
            self.save()
        except Exception as e:
            self.act(sym, "close failed", False, e)

    def tick(self, engines):
        c = self.control()
        if c.get("flatten_req") and c.get("flatten_req") != self.st.get("flatten_done"):
            for sym in self.cfg["symbols"]:
                self.flatten(sym, "flatten requested from the website")
            self.st["flatten_done"] = c["flatten_req"]
            self.save()
        if c.get("reset_halt") and c.get("reset_halt") != self.st.get("reset_done"):
            self.st["halted"] = None
            self.st["reset_done"] = c["reset_halt"]
            self.act("ALL", "halt reset from the website", True)
        if not self.client.ready() or time.time() - self.last_reconcile < 60:
            return
        self.last_reconcile = time.time()
        try:
            if self.kind == "live" and self.armed():
                self.guards(self.client.wallet())
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
            self.log.warning(f"{self.label} reconcile failed: {e}")


def snapshot(kind, cfg):
    """read-only view for the website (never returns keys)."""
    d = cfg.get(kind, {})
    control_path, state_path = paths(kind)
    c = core.read_json(control_path, {}) or {}
    st = core.read_json(state_path, {}) or {}
    out = {"kind": kind, "configured": bool(d.get("api_key") and d.get("api_secret")), "pin_set": bool(d.get("arm_pin")),
           "armed": bool(c.get("armed", d.get("enabled", False))) and bool(d.get("api_key")),
           "host": HOSTS[kind], "leverage": d.get("leverage", 10), "capital_usd": d.get("capital_usd", 0),
           "actions": list(reversed(st.get("actions", [])))[:80], "mirror": st.get("mirror", {}),
           "trader_seen": st.get("ts"), "halted": st.get("halted"), "day_start": st.get("day_start"),
           "limits": {"risk_pct": d.get("risk_pct"), "max_loss_per_trade": d.get("max_loss_per_trade"),
                      "max_daily_loss": d.get("max_daily_loss"), "min_equity": d.get("min_equity"),
                      "allow_min_size": d.get("allow_min_size"), "symbols": d.get("symbols", cfg["symbols"])}}
    if not out["configured"]:
        return out
    try:
        cl = BybitPrivate(d["api_key"], d["api_secret"], d.get("_test_host") or HOSTS[kind])
        out["wallet"] = cl.wallet()
        out["positions"] = cl.positions()
        out["orders"] = cl.open_orders()
        out["closed"] = cl.closed_pnl(50)
        out["api_ok"] = True
        if out.get("day_start") is not None:
            out["day_pnl"] = (out["wallet"]["usdt_wallet"] or out["wallet"]["wallet"]) - out["day_start"]
    except Exception as e:
        out["api_ok"] = False
        out["error"] = str(e)
    return out
