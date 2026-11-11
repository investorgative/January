#!/usr/bin/env python3
"""
FEBRUARY - dashboard + backtester/optimizer API (port 8753, tmux session "FebruaryWeb").
Cloudflare-tunnel friendly: relative URLs, plain HTTP polling (no websockets),
Cache-Control: no-store on every API route, gzip, optional basic-auth password.
"""
import copy
import gzip
import json
import os
import re
import subprocess
import sys
import threading
import time

from flask import Flask, Response, request, send_from_directory
from werkzeug.exceptions import HTTPException

import hmac as _hmac

import february_core as core
import february_backtest as bt
import february_exec as ex

cfg = core.load_config()
log = core.setup_logging("web", cfg)
app = Flask(__name__, static_folder=None)
STATIC = os.path.join(core.BASE_DIR, "static")
EVENTS_PATH = os.path.join(core.DATA_DIR, "events.json")
OPT_PROGRESS = os.path.join(core.DATA_DIR, "optimizer_progress.json")
CF_LOG = os.path.join(core.LOG_DIR, "cloudflared.log")
LAB_DIR = os.path.join(core.DATA_DIR, "lab")
LAB_RUNS = os.path.join(core.DATA_DIR, "lab_runs.json")
LAB_KEEP = 30
os.makedirs(LAB_DIR, exist_ok=True)
BT = {"running": False, "stage": "idle", "pct": 0.0, "error": None, "result": None, "started": None}
BT_LOCK = threading.Lock()
CACHE = {}
CACHE_LOCK = threading.Lock()
API = core.Bybit(cfg)
OPT_PROC = {"p": None}


def _load_bt():
    r = core.read_json(bt.BT_PATH)
    if r and "raw" in r:
        BT["result"] = r
        BT["stage"] = "done"
        BT["pct"] = 1.0


_load_bt()


def cfg_now():
    global cfg
    try:
        cfg = core.load_config()
    except Exception:
        log.exception("config reload failed")
    return cfg


# ------------------------------------------------------------------ helpers
def jresp(obj, status=200):
    data = json.dumps(core.clean_json(obj), separators=(",", ":")).encode()
    headers = {"Content-Type": "application/json", "Cache-Control": "no-store"}
    if "gzip" in request.headers.get("Accept-Encoding", "") and len(data) > 2048:
        data = gzip.compress(data, 5)
        headers["Content-Encoding"] = "gzip"
        headers["Vary"] = "Accept-Encoding"
    return Response(data, status=status, headers=headers)


def cached(key, ttl, fn):
    with CACHE_LOCK:
        hit = CACHE.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
    val = fn()
    with CACHE_LOCK:
        CACHE[key] = (time.time(), val)
    return val


@app.before_request
def _auth():
    pw = cfg["web"].get("password")
    if not pw or request.path == "/healthz":
        return None
    a = request.authorization
    if not a or a.password != pw:
        return Response("Authentication required", 401, {"WWW-Authenticate": 'Basic realm="FEBRUARY"'})


@app.errorhandler(Exception)
def _err(e):
    if isinstance(e, HTTPException):  # 404 favicon etc. are not errors worth a Discord ping
        return e
    log.exception(f"web error on {request.path}")
    return jresp({"error": str(e)}, 500)


def acct_param():
    a = (request.args.get("acct") or "ALL").upper()
    return a if a in core.ACCOUNTS else "ALL"


def tunnel_url():
    try:
        if os.path.exists(CF_LOG):
            with open(CF_LOG, encoding="utf-8", errors="ignore") as f:
                txt = f.read()[-200_000:]
            m = re.findall(r"https://[a-z0-9-]+\.trycloudflare\.com", txt)
            if m:
                return m[-1]
    except Exception:
        pass
    return None


def ticks():
    st = core.read_json(core.STATE_PATH, {}) or {}
    tk = st.get("ticks") or {}
    fresh = all(core.now_ms() - (tk.get(s, {}).get("ts") or 0) < 30_000 for s in cfg["symbols"])
    if fresh:
        return tk

    def fetch():
        out = {}
        for s in cfg["symbols"]:
            try:
                t = API.ticker(s)
                out[s] = {"last": float(t["lastPrice"]), "mark": float(t.get("markPrice") or t["lastPrice"]),
                          "chg24": float(t.get("price24hPcnt") or 0) * 100,
                          "funding": float(t.get("fundingRate") or 0), "ts": core.now_ms()}
            except Exception as e:
                out[s] = {"error": str(e)}
        return out
    return cached("ticks", 8, fetch)


# ------------------------------------------------------------------ pages
@app.route("/")
def index():
    r = send_from_directory(STATIC, "index.html")
    r.headers["Cache-Control"] = "no-cache"
    return r


@app.route("/static/<path:p>")
def static_files(p):
    return send_from_directory(STATIC, p)


@app.route("/healthz")
def healthz():
    return "ok"


# ------------------------------------------------------------------ paper
@app.route("/api/status")
def api_status():
    c = cfg_now()
    st = core.read_json(core.STATE_PATH, {}) or {}
    hb = st.get("heartbeat") or 0
    return jresp({"version": core.VERSION, "now": core.now_ms(), "heartbeat": hb,
                  "trader_alive": core.now_ms() - hb < 60_000, "started": st.get("started"),
                  "status": st.get("status", {}), "positions": st.get("positions", {}), "ticks": ticks(),
                  "settings": core.load_settings(c), "symbols": c["symbols"], "tunnel": tunnel_url(),
                  "fees": c["fees"], "strategy": c["strategy"], "timezone": c["timezone"],
                  "bt": {k: BT[k] for k in ("running", "stage", "pct", "error")},
                  "opt": core.read_json(OPT_PROGRESS, {"running": False, "stage": "idle"})})


def paper_bundle(acct, settings=None):
    c = cfg
    st = core.read_json(core.STATE_PATH, {}) or {}
    trades = core.read_json(core.TRADES_PATH, []) or []
    opens = [p for p in (st.get("positions") or {}).values() if p]
    tk = ticks()
    marks = {s: (tk.get(s) or {}).get("last") for s in c["symbols"]}
    started = st.get("started") or core.now_ms()
    settings = settings or core.load_settings(c)
    return core.account_bundle(trades, opens, c, settings, core.ACCOUNTS[acct], started, core.now_ms(), marks)


@app.route("/api/overview")
def api_overview():
    acct = acct_param()
    b = paper_bundle(acct)
    lim = int(request.args.get("limit", 500))
    b["trades"] = list(reversed(b["trades"]))[:lim]
    b["acct"] = acct
    return jresp(b)


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    c = cfg_now()
    s = core.load_settings(c)
    if request.method == "POST":
        j = request.get_json(force=True, silent=True) or {}
        if "risk_pct" in j:
            v = float(j["risk_pct"])
            if not 0.05 <= v <= 50:
                return jresp({"error": "risk must be between 0.05 and 50 %"}, 400)
            s["risk_pct"] = v
        if "start_balance" in j:
            v = float(j["start_balance"])
            if v <= 0:
                return jresp({"error": "balance must be > 0"}, 400)
            s["start_balance"] = v
        if "kelly_sizing" in j:
            s["kelly_sizing"] = bool(j["kelly_sizing"])
        if "edge_n" in j:
            s["edge_n"] = max(0, min(200, int(j["edge_n"])))
        if "edge_low" in j:
            s["edge_low"] = max(0.0, min(1.0, float(j["edge_low"])))
        core.save_json(core.SETTINGS_PATH, s)
        log.info(f"settings changed: {s}")
    return jresp(s)


@app.route("/api/events")
def api_events():
    ev = core.read_json(EVENTS_PATH, []) or []
    return jresp(list(reversed(ev))[:200])


@app.route("/api/log")
def api_log():
    p = os.path.join(core.LOG_DIR, "february.log")
    n = int(request.args.get("n", 300))
    lines = []
    if os.path.exists(p):
        with open(p, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 200_000))
            lines = f.read().decode("utf-8", "ignore").splitlines()[-n:]
    return jresp({"lines": lines})


# ------------------------------------------------------------------ chart
def _klines(sym, iv, limit):
    def f():
        rows = API.klines(sym, iv, limit=min(1000, limit))
        if limit > 1000:
            older = API.klines(sym, iv, end=rows[0][0] - 1, limit=min(1000, limit - 1000))
            rows = older + rows
        return rows
    return cached(f"k_{sym}_{iv}_{limit}", 10, f)


def _agg(rows, ms):
    out = []
    for r in rows:
        b = r[0] - r[0] % ms
        if out and out[-1][0] == b:
            x = out[-1]
            x[2] = max(x[2], r[2])
            x[3] = min(x[3], r[3])
            x[4] = r[4]
            x[5] += r[5]
        else:
            out.append([b, r[1], r[2], r[3], r[4], r[5]])
    return out


def _markers(trades, sym, t0):
    m = []
    for t in trades:
        if t["sym"] != sym or (t.get("exit_ts") or t["entry_ts"]) < t0:
            continue
        m.append({"t": t["entry_ts"], "kind": "entry", "dir": t["dir"], "price": t["entry"], "sl": t["sl0"]})
        if t.get("exit_ts"):
            r = t.get("r", core.unit_r(t, cfg["fees"]))
            m.append({"t": t["exit_ts"], "kind": "exit", "dir": t["dir"], "price": t["exit"], "r": r,
                      "reason": t.get("reason")})
    return m


def _zone_segments(zone_hist, t_end):
    """zone history -> boxes [t0, t1, bot, top] (a zone lives while it is in the active set)."""
    live, out = {}, []
    for snap in zone_hist:
        keys = {(round(z[0], 8), round(z[1], 8)) for z in snap["zones"]}
        for k in list(live):
            if k not in keys:
                out.append([live.pop(k), snap["ts"], k[0], k[1]])
        for k in keys:
            live.setdefault(k, snap["ts"])
    for k, t0 in live.items():
        out.append([t0, t_end, k[0], k[1]])
    return out


@app.route("/api/chart")
def api_chart():
    sym = request.args.get("sym", cfg["symbols"][0])
    if sym not in cfg["symbols"]:
        return jresp({"error": "unknown symbol"}, 400)
    tf = request.args.get("tf", "15")
    mode = request.args.get("mode", "paper")
    if mode == "bt":
        res = BT.get("result")
        if not res:
            return jresp({"candles": [], "boxes": [], "markers": []})
        store = core.Store(API)
        d = store.load(sym, res["meta"]["start"], res["meta"]["end"])
        rows = [[int(d["ts"][i]), float(d["o"][i]), float(d["h"][i]), float(d["l"][i]), float(d["c"][i]),
                 0.0] for i in range(len(d["ts"]))]
        if tf == "240":
            rows = _agg(rows, core.H4)
        elif tf == "60":
            rows = _agg(rows, 3_600_000)
        rows = [[r[0]] + [float(f"{x:.6g}") for x in r[1:5]] for r in rows]
        ch = res["chart"].get(sym, {})
        boxes = _zone_segments(ch.get("zone_hist", []), res["meta"]["end"])
        settings = _bt_settings()
        rep = core.replay(res["raw"]["trades"], cfg, settings, [sym])
        return jresp({"candles": rows, "boxes": boxes, "markers": _markers(rep["trades"], sym, 0),
                      "events": ch.get("marks", [])[-300:], "levels": {}, "zones": ch.get("zones", [])})
    limit = int(request.args.get("limit", 1000 if tf == "15" else 500))
    rows = _klines(sym, tf if tf in ("15", "60", "240", "D") else "15", max(50, min(limit, 2000)))
    st = core.read_json(core.STATE_PATH, {}) or {}
    s = (st.get("status") or {}).get(sym, {})
    trades = core.read_json(core.TRADES_PATH, []) or []
    pos = (st.get("positions") or {}).get(sym)
    allt = trades + ([pos] if pos else [])
    t0 = rows[0][0] if rows else 0
    end = core.now_ms() + 6 * core.H4
    boxes = [[t0, end, z["bot"], z["top"]] for z in s.get("zones", [])]
    lv = {}
    if s.get("setup"):
        su = s["setup"]
        lv = {"fib": su.get("fib"), "deep": su.get("deep"), "anchor": su.get("L"), "ext": su.get("ext"),
              "zone": su.get("zone"), "dir": su.get("dir"), "bo_ts": su.get("bo_ts"), "stage": su.get("stage")}
    if pos:
        lv["entry"] = pos["entry"]
        lv["sl"] = pos["sl"]
        lv["pos_dir"] = pos["dir"]
    ev = [e for e in (core.read_json(EVENTS_PATH, []) or []) if e.get("sym") == sym and e.get("ts", 0) >= t0]
    return jresp({"candles": rows, "boxes": boxes, "markers": _markers(allt, sym, t0), "levels": lv,
                  "events": ev[-200:], "zones": s.get("zones", [])})


# ------------------------------------------------------------------ backtest
def _bt_settings():
    s = dict(core.load_settings(cfg))
    res = BT.get("result")
    if res:
        s.update(res["meta"].get("settings") or {})
    for k, cast in (("risk", float), ("balance", float), ("kelly", str), ("edge_n", int), ("edge_low", float)):
        v = request.args.get(k)
        if v is None:
            continue
        if k in ("edge_n", "edge_low"):
            s[k] = cast(v)
            continue
        if k == "risk":
            s["risk_pct"] = cast(v)
        elif k == "balance":
            s["start_balance"] = cast(v)
        else:
            s["kelly_sizing"] = v in ("1", "true", "yes")
    return s


def _lab_record(res, label):
    live = cfg["strategy"]
    p = res["meta"]["params"]
    rid = str(res["meta"]["run_at"])
    core.save_json(os.path.join(LAB_DIR, rid + ".json"), res)
    runs = core.read_json(LAB_RUNS, []) or []
    runs.insert(0, {"id": rid, "run_at": res["meta"]["run_at"], "days": res["meta"]["days"], "label": label or "",
                    "start": res["meta"]["start"], "end": res["meta"]["end"], "params": p,
                    "diff": {k: v for k, v in p.items() if live.get(k) != v}, "lab": res["meta"]["lab"]})
    for old in runs[LAB_KEEP:]:
        try:
            os.remove(os.path.join(LAB_DIR, old["id"] + ".json"))
        except OSError:
            pass
    core.save_json(LAB_RUNS, runs[:LAB_KEEP])


def clean_params(raw):
    """only known strategy keys, cast to the type of the default value."""
    out = dict(cfg["strategy"])
    for k, v in (raw or {}).items():
        if k not in core.DEFAULT_CONFIG["strategy"]:
            continue
        d = core.DEFAULT_CONFIG["strategy"][k]
        try:
            if isinstance(d, str):
                v = str(v)
                opts = cfg["optimizer"].get("lab_space", {}).get(k) or (["close", "touch"] if k == "invalidate" else None)
                if opts and v not in opts:
                    continue
            elif isinstance(d, bool):
                v = bool(v)
            elif isinstance(d, int):
                v = int(round(float(v)))
            else:
                v = float(v)
        except (TypeError, ValueError):
            continue
        out[k] = v
    return out


def _bt_thread(days, settings, params=None, label=""):
    def pr(stage, x):
        BT["stage"], BT["pct"] = stage, x
    try:
        res = bt.run_backtest(cfg_now(), days, settings, params=params, progress=pr)
        core.save_json(bt.BT_PATH, res)
        _lab_record(res, label)
        BT["result"] = res
        BT["stage"], BT["pct"] = "done", 1.0
        log.info(f"backtest done: {days} days, {res['meta']['raw_trades']} trades, {res['meta']['secs']:.1f}s")
    except Exception as e:
        BT["error"] = str(e)
        BT["stage"] = "error"
        log.exception("backtest failed")
    finally:
        BT["running"] = False


@app.route("/api/backtest", methods=["POST"])
def api_backtest():
    j = request.get_json(force=True, silent=True) or {}
    with BT_LOCK:
        if BT["running"]:
            return jresp({"error": "backtest already running"}, 409)
        days = float(j.get("days") or cfg["backtest"]["default_days"])
        days = max(7, min(days, 1500))
        s = dict(core.load_settings(cfg))
        if j.get("risk") is not None:
            s["risk_pct"] = float(j["risk"])
        if j.get("balance") is not None:
            s["start_balance"] = float(j["balance"])
        if j.get("kelly") is not None:
            s["kelly_sizing"] = bool(j["kelly"])
        for k, cast in (("edge_n", int), ("edge_low", float)):
            if j.get(k) is not None:
                s[k] = cast(j[k])
        params = clean_params(j.get("params")) if j.get("params") else None
        BT.update(running=True, stage="starting", pct=0.0, error=None, started=core.now_ms())
        threading.Thread(target=_bt_thread, args=(days, s, params, str(j.get("label") or "")[:60]),
                         daemon=True).start()
    return jresp({"ok": True})


@app.route("/api/bt/overview")
def api_bt_overview():
    res = BT.get("result")
    if not res:
        return jresp({"empty": True, "bt": {k: BT[k] for k in ("running", "stage", "pct", "error")}})
    acct = acct_param()
    s = _bt_settings()
    b = core.account_bundle(res["raw"]["trades"], res["raw"]["open"], cfg, s, core.ACCOUNTS[acct],
                            res["meta"]["start"], res["meta"]["end"],
                            {k: v.get("last") for k, v in res["chart"].items()})
    b["trades"] = list(reversed(b["trades"]))
    b["meta"] = res["meta"]
    b["meta"]["settings"] = s
    b["acct"] = acct
    b["bt"] = {k: BT[k] for k in ("running", "stage", "pct", "error")}
    return jresp(b)


# ------------------------------------------------------------------ optimizer
@app.route("/api/optimize", methods=["POST"])
def api_optimize():
    j = request.get_json(force=True, silent=True) or {}
    p = OPT_PROC["p"]
    if p is not None and p.poll() is None:
        return jresp({"error": "optimizer already running"}, 409)
    days = max(30, min(float(j.get("days") or cfg["optimizer"]["days"]), 1500))
    combos = max(5, min(int(j.get("combos") or cfg["optimizer"]["combos"]), 2000))
    core.save_json(OPT_PROGRESS, {"running": True, "stage": "starting", "pct": 0, "ts": core.now_ms()})
    out = open(os.path.join(core.LOG_DIR, "optimizer.out"), "w")
    cmd = [sys.executable, os.path.join(core.BASE_DIR, "february_backtest.py"), "--optimize", "--days", str(days),
           "--combos", str(combos), "--progress-file", OPT_PROGRESS]
    if j.get("lab"):
        cmd.append("--lab")
    OPT_PROC["p"] = subprocess.Popen(cmd, cwd=core.BASE_DIR, stdout=out, stderr=subprocess.STDOUT)
    log.info(f"optimizer started: {days} days, {combos} combos")
    return jresp({"ok": True})


@app.route("/api/optimize/stop", methods=["POST"])
def api_optimize_stop():
    p = OPT_PROC["p"]
    if p is not None and p.poll() is None:
        p.terminate()
    core.save_json(OPT_PROGRESS, {"running": False, "stage": "stopped", "pct": 0})
    return jresp({"ok": True})


@app.route("/api/optimize/result")
def api_optimize_result():
    prog = core.read_json(OPT_PROGRESS, {"running": False, "stage": "idle"}) or {}
    p = OPT_PROC["p"]
    if prog.get("running") and (p is None or p.poll() is not None) and \
            core.now_ms() - (prog.get("ts") or 0) > 120_000:
        prog = {"running": False, "stage": "interrupted", "pct": 0}
    return jresp({"progress": prog, "result": core.read_json(bt.OPT_PATH), "current": cfg_now()["strategy"]})


@app.route("/api/optimize/apply", methods=["POST"])
def api_optimize_apply():
    j = request.get_json(force=True, silent=True) or {}
    res = core.read_json(bt.OPT_PATH)
    if not res:
        return jresp({"error": "no optimizer result"}, 400)
    i = int(j.get("rank", 0))
    if not 0 <= i < len(res["top"]):
        return jresp({"error": "bad rank"}, 400)
    params = res["top"][i]["params"]
    keys = set(core.DEFAULT_CONFIG["strategy"])
    core.save_strategy_params({k: v for k, v in params.items() if k in keys})
    cfg_now()
    log.info(f"optimizer rank #{i + 1} applied to config.json (paper trader reloads automatically)")
    return jresp({"ok": True, "strategy": cfg["strategy"]})


# ------------------------------------------------------------------ bybit execution (demo / live)
PIN_FAILS = []


def _pin_ok(kind, pin):
    want = str(cfg.get(kind, {}).get("arm_pin") or "")
    now = time.time()
    PIN_FAILS[:] = [t for t in PIN_FAILS if now - t < 600]
    if len(PIN_FAILS) >= 5:
        return False, "too many wrong PINs - wait 10 minutes"
    if not want:
        return False, f"set {kind}.arm_pin in config.json first"
    if not _hmac.compare_digest(str(pin or ""), want):
        PIN_FAILS.append(now)
        time.sleep(1)
        return False, "wrong PIN"
    return True, ""


def _kind(k):
    if k not in ("demo", "live"):
        raise ValueError("unknown executor")
    return k


def _ctl_update(kind, **kw):
    cp, _ = ex.paths(kind)
    ctl = core.read_json(cp, {}) or {}
    ctl.update(kw)
    core.save_json(cp, ctl)
    with CACHE_LOCK:
        CACHE.pop("exec_" + kind, None)
    return ctl


@app.route("/api/exec/<kind>")
def api_exec(kind):
    kind = _kind(kind)
    c = cfg_now()
    return jresp(cached("exec_" + kind, 8, lambda: ex.snapshot(kind, c)))


@app.route("/api/exec/<kind>/arm", methods=["POST"])
def api_exec_arm(kind):
    kind = _kind(kind)
    j = request.get_json(force=True, silent=True) or {}
    ok, why = _pin_ok(kind, j.get("pin"))
    if not ok:
        return jresp({"error": why}, 403)
    d = cfg_now().get(kind, {})
    if not d.get("api_key"):
        return jresp({"error": f"add your {kind} api_key / api_secret to config.json first"}, 400)
    arm = bool(j.get("armed"))
    if arm and kind == "live":
        if str(j.get("confirm", "")).strip().upper() != "LIVE":
            return jresp({"error": "type LIVE to confirm real-money trading"}, 400)
        try:  # the key must work for USDT perpetuals before we arm
            cl = ex.BybitPrivate(d["api_key"], d["api_secret"], d.get("_test_host") or ex.HOSTS["live"])
            w = cl.wallet()
            cl.positions()
        except Exception as e:
            return jresp({"error": f"API check failed - not armed: {e}"}, 400)
        log.warning(f"REAL-MONEY trading ARMED from the website (USDT balance ${w['usdt_wallet']:.2f})")
    _ctl_update(kind, armed=arm, changed=core.now_ms())
    log.info(f"{kind.upper()} execution {'ARMED' if arm else 'disarmed'} from the website")
    return jresp({"ok": True, "armed": arm})


@app.route("/api/exec/<kind>/flatten", methods=["POST"])
def api_exec_flatten(kind):
    kind = _kind(kind)
    j = request.get_json(force=True, silent=True) or {}
    ok, why = _pin_ok(kind, j.get("pin"))
    if not ok:
        return jresp({"error": why}, 403)
    _ctl_update(kind, armed=False, flatten_req=core.now_ms())
    log.warning(f"{kind.upper()}: flatten requested from the website (and disarmed)")
    return jresp({"ok": True})


@app.route("/api/exec/<kind>/reset", methods=["POST"])
def api_exec_reset(kind):
    kind = _kind(kind)
    j = request.get_json(force=True, silent=True) or {}
    ok, why = _pin_ok(kind, j.get("pin"))
    if not ok:
        return jresp({"error": why}, 403)
    _ctl_update(kind, reset_halt=core.now_ms())
    log.warning(f"{kind.upper()}: halt reset from the website")
    return jresp({"ok": True})


# ------------------------------------------------------------------ strategy lab
@app.route("/api/lab")
def api_lab():
    c = cfg_now()
    opts = dict(c["optimizer"].get("lab_space", {}))
    opts["invalidate"] = ["close", "touch"]
    return jresp({"live": c["strategy"], "defaults": core.DEFAULT_CONFIG["strategy"], "options": opts,
                  "runs": core.read_json(LAB_RUNS, []) or []})


def _lab_get(rid):
    runs = core.read_json(LAB_RUNS, []) or []
    return next((r for r in runs if r["id"] == str(rid)), None)


@app.route("/api/lab/view", methods=["POST"])
def api_lab_view():
    j = request.get_json(force=True, silent=True) or {}
    p = os.path.join(LAB_DIR, str(j.get("id")) + ".json")
    res = core.read_json(p)
    if not res:
        return jresp({"error": "run not found"}, 404)
    BT["result"] = res
    BT["stage"], BT["pct"], BT["error"] = "done", 1.0, None
    core.save_json(bt.BT_PATH, res)
    return jresp({"ok": True})


@app.route("/api/lab/apply", methods=["POST"])
def api_lab_apply():
    j = request.get_json(force=True, silent=True) or {}
    r = _lab_get(j.get("id"))
    if not r:
        return jresp({"error": "run not found"}, 404)
    keys = set(core.DEFAULT_CONFIG["strategy"])
    core.save_strategy_params({k: v for k, v in r["params"].items() if k in keys})
    cfg_now()
    log.info(f"lab run {r['id']} applied to the paper trader: {r['diff']}")
    return jresp({"ok": True})


@app.route("/api/lab/delete", methods=["POST"])
def api_lab_delete():
    j = request.get_json(force=True, silent=True) or {}
    runs = [r for r in (core.read_json(LAB_RUNS, []) or []) if r["id"] != str(j.get("id"))]
    try:
        os.remove(os.path.join(LAB_DIR, str(j.get("id")) + ".json"))
    except OSError:
        pass
    core.save_json(LAB_RUNS, runs)
    return jresp({"ok": True})


if __name__ == "__main__":
    port = int(cfg["web"].get("port", 8753))
    log.info(f"FEBRUARY web on http://{cfg['web'].get('host', '0.0.0.0')}:{port}")
    try:
        from waitress import serve
        serve(app, host=cfg["web"].get("host", "0.0.0.0"), port=port, threads=8)
    except ImportError:
        app.run(host=cfg["web"].get("host", "0.0.0.0"), port=port, threaded=True)
