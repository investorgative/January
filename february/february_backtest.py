#!/usr/bin/env python3
"""
FEBRUARY - backtester + optimizer (uses the exact same Engine as the paper trader).

CLI:
  ./venv/bin/python february_backtest.py --days 365            # backtest with config.json params
  ./venv/bin/python february_backtest.py --optimize --combos 150
"""
import argparse
import copy
import json
import math
import multiprocessing as mp
import os
import random
import time

import numpy as np

import february_core as core

BT_PATH = os.path.join(core.DATA_DIR, "backtest_last.json")
OPT_PATH = os.path.join(core.DATA_DIR, "optimizer_last.json")


def warmup_ms(params):
    """enough history for the zone lookback (4h) + 15m swing buffers."""
    lb = max(int(params.get("zone_lookback", 120)), 270)  # >= 260 closed 4h candles for the regime filter
    return lb * core.H4 + 4 * core.DAY


def load_data(cfg, days, params, progress=None, max_lookback=None):
    api = core.Bybit(cfg)
    store = core.Store(api)
    end = core.now_ms() - core.now_ms() % core.M15 - 1
    start = end - int(days * core.DAY)
    wp = dict(params)
    if max_lookback:
        wp["zone_lookback"] = max_lookback
    data_start = start - warmup_ms(wp)
    out = {}
    syms = cfg["symbols"]
    for n, sym in enumerate(syms):
        def pr(x, n=n):
            if progress:
                progress("download " + sym, (n + x) / len(syms) * 0.5)
        try:
            store.ensure(sym, data_start, end, progress=pr)
        except Exception as e:  # Bybit unreachable: use what is cached, never block a backtest
            core.logging.getLogger("february").warning(f"{sym}: download failed, using cached candles ({e})")
        d = store.load(sym, data_start, end)
        if not len(d["ts"]):
            raise RuntimeError(f"no candles for {sym} (no cache and Bybit unreachable)")
        fund = {}
        if cfg["fees"].get("include_funding", True):
            try:
                store.ensure_funding(sym, data_start, end)
            except Exception as e:  # funding is optional, never block a backtest
                core.logging.getLogger("february").warning(f"funding download failed {sym}: {e}")
            fund = store.load_funding(sym, data_start, end)
        out[sym] = {"data": d, "funding": fund}
    last = min(int(v["data"]["ts"][-1]) for v in out.values()) + core.M15 - 1
    if last < end:  # cached data ends earlier -> shift the window so every symbol has data
        end = last
        start = end - int(days * core.DAY)
    return out, start, end


def run_backtest(cfg, days, settings, params=None, progress=None):
    params = params or cfg["strategy"]
    t0 = time.time()
    data, start, end = load_data(cfg, days, params, progress)
    trades, opens, charts = [], [], {}
    syms = cfg["symbols"]
    for n, sym in enumerate(syms):
        if progress:
            progress("simulate " + sym, 0.5 + n / len(syms) * 0.45)
        r = core.run_symbol(sym, data[sym]["data"], params, cfg, start, data[sym]["funding"], collect_chart=True)
        trades += r["trades"]
        if r["open"]:
            opens.append(r["open"])
        d = data[sym]["data"]
        eng = r["engine"]
        charts[sym] = {"marks": r["marks"][-400:], "zone_hist": r["zone_hist"], "zones": eng.zones,
                       "last": float(d["c"][-1]) if len(d["c"]) else None}
    marks = {s: charts[s]["last"] for s in syms}
    accounts = {}
    for name, ss in core.ACCOUNTS.items():
        accounts[name] = core.account_bundle(trades, opens, cfg, settings, ss, start, end, marks)
    res = {"meta": {"days": days, "start": start, "end": end, "settings": settings, "params": params,
                    "run_at": core.now_ms(), "secs": time.time() - t0, "raw_trades": len(trades),
                    "lab": lab_summary(trades, cfg, start, end)},
           "accounts": accounts, "raw": {"trades": trades, "open": opens}, "chart": charts}
    if progress:
        progress("done", 1.0)
    return res


def lab_summary(trades, cfg, start, end):
    """size-independent R summary: whole period, first half vs second half, per symbol."""
    fees = cfg["fees"]
    mid = start + (end - start) // 2

    def m(ts):
        rs = [core.unit_r(t, fees) for t in sorted(ts, key=lambda t: t["exit_ts"])]
        if not rs:
            return {"n": 0, "r": 0.0, "wr": 0.0, "pf": 0.0, "dd": 0.0}
        c = np.cumsum(rs)
        pk = np.maximum.accumulate(np.concatenate([[0.0], c]))
        gw = sum(r for r in rs if r > 0)
        gl = -sum(r for r in rs if r <= 0)
        return {"n": len(rs), "r": float(sum(rs)), "wr": sum(1 for r in rs if r > 0) / len(rs) * 100,
                "pf": gw / gl if gl > 0 else (99.0 if gw > 0 else 0.0),
                "dd": float(np.max(pk - np.concatenate([[0.0], c])))}
    out = {"all": m(trades), "h1": m([t for t in trades if t["entry_ts"] < mid]),
           "h2": m([t for t in trades if t["entry_ts"] >= mid])}
    for sym in cfg["symbols"]:
        out[sym] = m([t for t in trades if t["sym"] == sym])
    return out


# ---------------------------------------------------------------- optimizer
_G = {}


def _init(data, cfg, split, start, end):
    _G["data"], _G["cfg"], _G["split"], _G["start"], _G["end"] = data, cfg, split, start, end


def _eval(params):
    cfg = _G["cfg"]
    fees = cfg["fees"]
    trades = []
    try:
        for sym in cfg["symbols"]:
            r = core.run_symbol(sym, _G["data"][sym]["data"], params, cfg, _G["start"], _G["data"][sym]["funding"])
            trades += r["trades"]
    except Exception as e:
        return {"params": params, "error": str(e)}
    split = _G["split"]
    trades.sort(key=lambda t: t["exit_ts"])
    rs_tr = [core.unit_r(t, fees) for t in trades if t["entry_ts"] < split]
    rs_te = [core.unit_r(t, fees) for t in trades if t["entry_ts"] >= split]

    def m(rs):
        if not rs:
            return {"n": 0, "r": 0.0, "wr": 0.0, "pf": 0.0, "dd": 0.0, "avg": 0.0}
        c = np.cumsum(rs)
        pk = np.maximum.accumulate(np.concatenate([[0.0], c]))
        dd = float(np.max(pk - np.concatenate([[0.0], c])))
        gw = sum(r for r in rs if r > 0)
        gl = -sum(r for r in rs if r <= 0)
        return {"n": len(rs), "r": float(sum(rs)), "wr": sum(1 for r in rs if r > 0) / len(rs) * 100,
                "pf": gw / gl if gl > 0 else (99.0 if gw > 0 else 0.0), "dd": dd, "avg": float(np.mean(rs))}
    a, b = m(rs_tr), m(rs_te)
    allm = m(rs_tr + rs_te)
    return {"params": params, "train": a, "test": b, "all": allm}


def sample_space(space, base, n, seed=None):
    rnd = random.Random(seed)
    keys = list(space)
    seen, out = set(), [dict(base)]
    seen.add(json.dumps({k: base.get(k) for k in keys}, sort_keys=True))
    total = 1
    for k in keys:
        total *= len(space[k])
    tries = 0
    while len(out) < min(n, total) and tries < n * 50:
        tries += 1
        p = dict(base)
        for k in keys:
            p[k] = rnd.choice(space[k])
        key = json.dumps({k: p[k] for k in keys}, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def optimize(cfg, days, combos, progress=None, stop_flag=None, lab=None):
    oc = cfg["optimizer"]
    base = dict(cfg["strategy"])
    space = dict(oc["space"])
    if lab if lab is not None else oc.get("include_lab_space"):
        space.update(oc.get("lab_space", {}))
    max_lb = max(space.get("zone_lookback", [base["zone_lookback"]]) + [base["zone_lookback"]])
    data, start, end = load_data(cfg, days, base, progress, max_lookback=max_lb)
    split = start + int((end - start) * oc["train_frac"])
    cands = sample_space(space, base, combos)
    workers = oc.get("workers") or max(1, (os.cpu_count() or 2) - 0)
    results = []
    t0 = time.time()
    ctx = mp.get_context("fork") if hasattr(os, "fork") else mp.get_context()
    with ctx.Pool(workers, initializer=_init, initargs=(data, cfg, split, start, end)) as pool:
        for i, r in enumerate(pool.imap_unordered(_eval, cands)):
            results.append(r)
            if progress:
                el = time.time() - t0
                eta = el / (i + 1) * (len(cands) - i - 1)
                progress(f"combo {i + 1}/{len(cands)} · eta {eta / 60:.1f} min", 0.5 + 0.5 * (i + 1) / len(cands))
            if stop_flag and stop_flag():
                pool.terminate()
                break
    mt = oc["min_trades"]
    good = [r for r in results if "error" not in r]
    # rank by TRAIN net R (needs enough trades), test is shown as the out-of-sample check
    def score(r):
        if r["train"]["n"] < mt * oc["train_frac"]:
            return -1e9 + r["train"]["n"]
        return r["train"]["r"]
    good.sort(key=score, reverse=True)
    for r in good:
        r["valid"] = r["train"]["n"] >= mt * oc["train_frac"]
        r["changed"] = {k: r["params"][k] for k in space if r["params"].get(k) != base.get(k)}
    base_res = next((r for r in good if all(r["params"].get(k) == base.get(k) for k in space)), None)
    out = {"meta": {"days": days, "start": start, "end": end, "split": split, "combos": len(results),
                    "secs": time.time() - t0, "run_at": core.now_ms(), "min_trades": mt,
                    "objective": "net R after fees (train), checked out-of-sample"},
           "base": base_res, "top": good[:25]}
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=None)
    ap.add_argument("--optimize", action="store_true")
    ap.add_argument("--combos", type=int, default=None)
    ap.add_argument("--risk", type=float, default=None)
    ap.add_argument("--balance", type=float, default=None)
    ap.add_argument("--progress-file", default=None, help="write progress json (used by the website)")
    ap.add_argument("--lab", action="store_true", help="also search the interpretation variants")
    a = ap.parse_args()
    cfg = core.load_config()
    log = core.setup_logging("optimizer" if a.optimize else "backtest", cfg)
    last = {"t": 0}

    def pr(stage, x):
        print(f"\r{stage:40s} {x * 100:5.1f}%", end="", flush=True)
        if a.progress_file and (time.time() - last["t"] > 1 or x >= 1):
            last["t"] = time.time()
            core.save_json(a.progress_file, {"running": True, "stage": stage, "pct": x, "pid": os.getpid(),
                                             "ts": core.now_ms()})
    if a.optimize:
        try:
            res = optimize(cfg, a.days or cfg["optimizer"]["days"], a.combos or cfg["optimizer"]["combos"], pr,
                           lab=True if a.lab else None)
        except Exception as e:
            log.exception("optimizer failed")
            if a.progress_file:
                core.save_json(a.progress_file, {"running": False, "stage": "error", "error": str(e), "pct": 0})
            raise
        core.save_json(OPT_PATH, res)
        if a.progress_file:
            core.save_json(a.progress_file, {"running": False, "stage": "done", "pct": 1.0, "ts": core.now_ms()})
        print()
        for r in res["top"][:10]:
            print(f"train {r['train']['r']:+7.1f}R n={r['train']['n']:4d} | test {r['test']['r']:+7.1f}R "
                  f"n={r['test']['n']:4d} pf={r['test']['pf']:.2f} | {r['changed']}")
    else:
        st = core.load_settings(cfg)
        if a.risk:
            st["risk_pct"] = a.risk
        if a.balance:
            st["start_balance"] = a.balance
        res = run_backtest(cfg, a.days or cfg["backtest"]["default_days"], st, progress=pr)
        core.save_json(BT_PATH, res)
        print()
        for k, acc in res["accounts"].items():
            s = acc["stats"]
            print(f"{k:4s} trades={s['total_trades']:4d} wr={s['winrate']:5.1f}% R={s['cum_r']:+7.2f} "
                  f"pnl=${s['net_pnl']:+9.2f} pf={s['profit_factor']:.2f} maxDD={s['max_dd_pct']:.1f}%")
