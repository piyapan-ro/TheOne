"""
Adaptive Optimizer - walk-forward parameter tuning for the 6 strategies.

How it works (per strategy):
1. Split the history into 6 equal chunks.
2. Walk-forward: for each window w = 2..5, pick the best parameter set on the TRAIN
   data (chunks w-2, w-1) and record how it does on the next, unseen TEST chunk (w).
   -> "WF out-of-sample" = how well the *optimisation process itself* works on new data.
3. The CURRENT parameters are scored on the same test chunks for a fair comparison.
4. The PROPOSED parameters are fitted on the recent half (chunks 3-5) and checked on
   the older half (chunks 0-2), which they never saw.
5. A recommendation is only made when the evidence clears minimum thresholds; nothing
   is changed until the user presses Apply (every change can be rolled back).

All results are in R (multiples of the stop-loss) after deducting trading cost.
"""
import itertools
import math
import random
import threading
import time

import store
import strategies as st

N_CHUNKS = 6
MIN_TRADES_TRAIN = 12
MIN_TRADES_OOS = 20

STATE = {"running": False, "progress": 0, "stage": "", "started": 0, "finished": 0, "result": None, "error": ""}
_lock = threading.Lock()


# ------------------------------------------------------------------ helpers
def _stats(rs):
    rs = [float(r) for r in rs]
    n = len(rs)
    if not n:
        return {"trades": 0, "exp_r": 0.0, "total_r": 0.0, "win_rate": 0.0, "pf": None, "max_dd_r": 0.0}
    wins = [r for r in rs if r > 0]
    gw, gl = sum(wins), -sum(r for r in rs if r <= 0)
    eq = peak = mdd = 0.0
    for r in rs:
        eq += r; peak = max(peak, eq); mdd = max(mdd, peak - eq)
    return {"trades": n, "exp_r": round(eq / n, 3), "total_r": round(eq, 2),
            "win_rate": round(len(wins) / n * 100, 1),
            "pf": round(gw / gl, 2) if gl > 0 else (99.0 if wins else None), "max_dd_r": round(mdd, 2)}


def _score(rs):
    """t-stat-like score: rewards a positive edge AND a decent sample size."""
    n = len(rs)
    if n < MIN_TRADES_TRAIN:
        return -1e9
    mean = sum(rs) / n
    sd = math.sqrt(sum((r - mean) ** 2 for r in rs) / (n - 1)) or 1e-9
    return mean / sd * math.sqrt(n)


def _combos(code, current, max_combos, rng):
    grid = st.PARAM_GRID[code]
    keys = list(grid)
    allc = [dict(zip(keys, v)) for v in itertools.product(*(grid[k] for k in keys))]
    if len(allc) > max_combos:
        allc = rng.sample(allc, max_combos)
    cur = {k: current[k] for k in keys}
    if cur not in allc:
        allc.append(cur)
    return allc


def _trades_by_chunk(df, signals, bounds, cost):
    bt = st.backtest(df, signals, cost=cost)
    per = [[] for _ in range(N_CHUNKS)]
    for t in bt["trades"]:
        if t["result"] == "open":
            continue
        i = t["index"]
        for c in range(N_CHUNKS):
            if bounds[c] <= i < bounds[c + 1]:
                per[c].append(t["r"])
                break
    return per


def _pick(chunks):
    return [r for c in chunks for r in c]


# ------------------------------------------------------------------ main routine
def optimize(broker, symbol, tf="M5", bars=12000, cost=0.3, max_combos=40, asian=None, live_trades=None, seed=42):
    rng = random.Random(seed)
    _set(stage="ดึงข้อมูลราคา", progress=2)
    raw = broker.rates(symbol, tf, bars)
    if len(raw) < 3000:
        raise RuntimeError(f"ข้อมูลไม่พอ ({len(raw)} แท่ง) - ต้องการอย่างน้อย 3,000 แท่ง")
    df = st.add_indicators(raw)
    n = len(df)
    bounds = [int(n * k / N_CHUNKS) for k in range(N_CHUNKS + 1)]
    bounds[0] = 210  # skip indicator warm-up
    saved = store.load("strategy_params")
    current = {c: st.merged_params(c, saved.get(c)) for c in st.STRATEGIES}

    # playbook inputs: other engines with their current parameters
    others = []
    for c in ("SMC", "SND", "EMA", "BB"):
        others += st.run_one(df, c, current[c])

    codes = list(st.STRATEGIES)
    plans = {c: _combos(c, current[c], max_combos, rng) for c in codes}
    total = sum(len(v) for v in plans.values())
    done = 0
    results = {}
    for code in codes:
        evals = []
        for combo in plans[code]:
            p = {**current[code], **combo}
            sigs = st.run_one(df, code, p, asian, others if code == "AIPB" else None)
            evals.append({"params": p, "chunks": _trades_by_chunk(df, sigs, bounds, cost)})
            done += 1
            _set(stage=f"ทดสอบ {code} ({st.STRATEGIES[code]})", progress=5 + int(90 * done / total))
        results[code] = _analyse(code, evals, current[code], live_trades or [])
    meta = {"symbol": symbol, "tf": tf, "bars": n, "from": int(df["time"].iat[bounds[0]]), "to": int(df["time"].iat[-1]),
            "cost": cost, "combos_tested": total, "chunk_bounds": [int(df["time"].iat[min(b, n - 1)]) for b in bounds]}
    return {"meta": meta, "strategies": results, "recommendations": _recommend(results)}


def _analyse(code, evals, current, live_trades):
    cur = next(e for e in evals if all(e["params"][k] == current[k] for k in st.PARAM_GRID[code]))
    # 1) walk-forward: choose on train chunks (w-2, w-1), measure on chunk w
    windows, wf_oos, cur_oos = [], [], []
    for w in range(2, N_CHUNKS):
        best = max(evals, key=lambda e: _score(e["chunks"][w - 2] + e["chunks"][w - 1]))
        if _score(best["chunks"][w - 2] + best["chunks"][w - 1]) <= -1e9:
            best = cur
        test, ctest = best["chunks"][w], cur["chunks"][w]
        wf_oos += test; cur_oos += ctest
        windows.append({"window": w, "chosen": {k: best["params"][k] for k in st.PARAM_GRID[code]},
                        "oos": _stats(test), "current_oos": _stats(ctest)})
    # 2) proposal: fit on recent half, check on the older (unseen) half
    recent = lambda e: _pick(e["chunks"][3:])
    older = lambda e: _pick(e["chunks"][:3])
    prop = max(evals, key=lambda e: _score(recent(e)))
    if _score(recent(prop)) <= -1e9:
        prop = cur
    # stability: how often were the proposal's values chosen during walk-forward
    keys = list(st.PARAM_GRID[code])
    stability = round(sum(sum(1 for k in keys if wnd["chosen"][k] == prop["params"][k]) / len(keys)
                          for wnd in windows) / len(windows) * 100)
    live = [t for t in live_trades if t.get("strategy") == code and t.get("status") == "closed"]
    return {
        "name": st.STRATEGIES[code],
        "current_params": {k: current[k] for k in keys},
        "proposed_params": {k: prop["params"][k] for k in keys},
        "changed": {k: [current[k], prop["params"][k]] for k in keys if current[k] != prop["params"][k]},
        "wf_oos": _stats(wf_oos), "current_oos": _stats(cur_oos),
        "wf_windows_positive": sum(1 for w in windows if w["oos"]["exp_r"] > 0), "n_windows": len(windows),
        "current_windows_positive": sum(1 for w in windows if w["current_oos"]["exp_r"] > 0),
        "proposed_recent_is": _stats(recent(prop)), "proposed_older_oos": _stats(older(prop)),
        "current_recent": _stats(recent(cur)), "current_older": _stats(older(cur)),
        "current_all": _stats(_pick(cur["chunks"])), "proposed_all": _stats(_pick(prop["chunks"])),
        "stability_pct": stability, "windows": windows,
        "live": {"trades": len(live), "net": round(sum(t["profit"] for t in live), 2),
                 "win_rate": round(sum(1 for t in live if t["profit"] > 0) / len(live) * 100, 1) if live else None},
    }


def _recommend(results):
    recs = []
    for code, r in results.items():
        wf, cur, old = r["wf_oos"], r["current_oos"], r["proposed_older_oos"]
        reasons = []
        if r["changed"] and wf["trades"] >= MIN_TRADES_OOS and old["trades"] >= MIN_TRADES_OOS \
                and wf["exp_r"] > 0 and old["exp_r"] > 0 and wf["exp_r"] >= cur["exp_r"] + 0.05 \
                and old["exp_r"] >= r["current_older"]["exp_r"]:
            score = (r["wf_windows_positive"] >= 3) + (wf["trades"] >= 60) + (r["stability_pct"] >= 50) + (old["exp_r"] >= 0.1)
            conf = "สูง" if score >= 3 and r["wf_windows_positive"] >= 3 and old["trades"] >= 40 else "กลาง" if score >= 2 else "ต่ำ"
            reasons.append(f"Walk-forward OOS {wf['exp_r']:+.3f}R/ไม้ ({wf['trades']} ไม้) ดีกว่าค่าปัจจุบัน {cur['exp_r']:+.3f}R")
            reasons.append(f"ค่าใหม่ทดสอบกับข้อมูลครึ่งเก่าที่ไม่เคยเห็น: {old['exp_r']:+.3f}R/ไม้ ({old['trades']} ไม้)")
            reasons.append(f"ช่วงทดสอบที่กำไร {r['wf_windows_positive']}/{r['n_windows']} · ความนิ่งของค่า {r['stability_pct']}%")
            recs.append({"id": f"{code}-params", "strategy": code, "type": "params", "confidence": conf,
                         "title": f"ปรับพารามิเตอร์ {code}", "changes": r["changed"], "reasons": reasons,
                         "expected_gain_r": round(wf["exp_r"] - cur["exp_r"], 3)})
        elif cur["trades"] >= MIN_TRADES_OOS and cur["exp_r"] < -0.03 and wf["exp_r"] <= 0:
            reasons.append(f"ค่าปัจจุบันขาดทุนในข้อมูลทดสอบ {cur['exp_r']:+.3f}R/ไม้ ({cur['trades']} ไม้)")
            reasons.append(f"จูนแล้วก็ยังไม่เป็นบวกใน OOS ({wf['exp_r']:+.3f}R) = ยังไม่มี edge ในตลาดช่วงนี้")
            if r["live"]["trades"]:
                reasons.append(f"ผลจริงจาก Journal: {r['live']['trades']} ไม้, Net {r['live']['net']:+.2f}")
            conf = "สูง" if cur["trades"] >= 60 and r["current_windows_positive"] <= 1 else "กลาง"
            recs.append({"id": f"{code}-disable", "strategy": code, "type": "disable", "confidence": conf,
                         "title": f"พักกลยุทธ์ {code} (ปิดสแกน + Auto-trade)", "changes": {}, "reasons": reasons,
                         "expected_gain_r": round(-cur["exp_r"], 3)})
        if r["live"]["trades"] >= 20 and r["live"]["net"] < 0 and r["current_all"]["exp_r"] > 0.05:
            recs.append({"id": f"{code}-drift", "strategy": code, "type": "warning", "confidence": "-",
                         "title": f"{code}: ผลเทรดจริงแย่กว่า Backtest",
                         "changes": {}, "expected_gain_r": 0,
                         "reasons": [f"Backtest {r['current_all']['exp_r']:+.3f}R/ไม้ แต่ของจริง {r['live']['trades']} ไม้ ขาดทุน {r['live']['net']:.2f}",
                                     "ตรวจ slippage/spread ตอนเข้า, การเข้าช้ากว่าสัญญาณ หรือการแทรกแซงด้วยมือ"]})
    order = {"สูง": 0, "กลาง": 1, "ต่ำ": 2, "-": 3}
    return sorted(recs, key=lambda x: (order[x["confidence"]], -x["expected_gain_r"]))


# ------------------------------------------------------------------ state / threading
def _set(**kw):
    STATE.update(kw)


def start(broker, symbol, **kw):
    with _lock:
        if STATE["running"]:
            return False
        STATE.update(running=True, progress=0, stage="เริ่ม", started=int(time.time()), error="")

    def job():
        try:
            res = optimize(broker, symbol, **kw)
            res["meta"]["run_time"] = int(time.time())
            STATE.update(result=res, progress=100, stage="เสร็จ")
            opt = store.load("optimizer")
            opt["last_result"] = res
            store.save("optimizer", opt)
        except Exception as e:  # noqa
            STATE.update(error=str(e), stage="ผิดพลาด")
        finally:
            STATE.update(running=False, finished=int(time.time()))

    threading.Thread(target=job, daemon=True).start()
    return True


def status():
    res = STATE["result"]
    if res is None:
        res = store.load("optimizer").get("last_result")
    return {**{k: v for k, v in STATE.items() if k != "result"}, "result": res,
            "history": store.load("optimizer").get("history", [])[:30], "labels": st.PARAM_LABELS}


# ------------------------------------------------------------------ apply / rollback
def apply(rec_ids):
    res = status()["result"]
    if not res:
        raise RuntimeError("ยังไม่มีผลวิเคราะห์ - กดรัน Optimizer ก่อน")
    recs = {r["id"]: r for r in res["recommendations"]}
    params = store.load("strategy_params")
    scanner = store.load("scanner")
    hist = store.load("optimizer").get("history", [])
    applied = []
    for rid in rec_ids:
        r = recs.get(rid)
        if not r or r["type"] == "warning":
            continue
        code = r["strategy"]
        entry = {"id": f"{rid}-{int(time.time())}", "time": int(time.time()), "strategy": code, "type": r["type"],
                 "title": r["title"], "rolled_back": False}
        if r["type"] == "params":
            before = st.merged_params(code, params.get(code))
            after = {**before, **{k: v[1] for k, v in r["changes"].items()}}
            params[code] = after
            entry.update(before={"params": before}, after={"params": after})
        elif r["type"] == "disable":
            entry.update(before={"scan": scanner["strategies"].get(code), "auto": scanner["auto_trade_strategies"].get(code)},
                         after={"scan": False, "auto": False})
            scanner["strategies"][code] = False
            scanner["auto_trade_strategies"][code] = False
        hist.insert(0, entry)
        applied.append(entry)
    store.save("strategy_params", params)
    store.save("scanner", scanner)
    store.update("optimizer", {"history": hist[:100]})
    return applied


def rollback(entry_id):
    opt = store.load("optimizer")
    hist = opt.get("history", [])
    e = next((h for h in hist if h["id"] == entry_id), None)
    if not e or e["rolled_back"]:
        raise RuntimeError("ไม่พบรายการ หรือย้อนกลับไปแล้ว")
    code = e["strategy"]
    if e["type"] == "params":
        params = store.load("strategy_params")
        params[code] = e["before"]["params"]
        store.save("strategy_params", params)
    elif e["type"] == "disable":
        sc = store.load("scanner")
        sc["strategies"][code] = bool(e["before"]["scan"])
        sc["auto_trade_strategies"][code] = bool(e["before"]["auto"])
        store.save("scanner", sc)
    e["rolled_back"] = True
    e["rolled_back_time"] = int(time.time())
    store.update("optimizer", {"history": hist})
    return e


def reset_params(code):
    params = store.load("strategy_params")
    params.pop(code, None)
    store.save("strategy_params", params)
    return st.merged_params(code)
