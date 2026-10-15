"""
Liquidity pools & liquidity sweeps (stop hunts).

Pool  = a price where stop orders are likely resting: swing highs/lows (stronger when several are
        "equal" within a small distance) and obvious levels (prev day / week high-low, Asian range).
        Buy-side liquidity (BSL) rests above highs, sell-side liquidity (SSL) below lows.
Sweep = a CLOSED bar trades through a resting pool and closes back on the other side
        (wick above a high then close below it = BSL swept -> bearish; the mirror -> bullish).

Only closed bars are used (the forming bar is dropped by the caller).
"""
import numpy as np

import strategies as st

FRESH_BARS = {"M5": 3, "M15": 2, "H1": 1}   # how long a sweep counts as "fresh" for the plan check
LOOKBACK = {"M5": 6, "M15": 4, "H1": 3}     # closed bars scanned for sweeps
POOL_BARS = 200                              # bars used to find swing pools


def _r(x):
    return None if x is None else round(float(x), 2)


def swing_pools(df, atr, k=3, eq_tol=0.15):
    """Resting swing pools from closed bars. Equal highs/lows (within eq_tol*ATR) are merged and
    get more 'touches'. A pool is resting while no later bar has traded through it."""
    d = df.tail(POOL_BARS).reset_index(drop=True)
    if len(d) < 2 * k + 5:
        return []
    sh, sl = st.swings(d, k)
    h, l = d["high"].values, d["low"].values
    pools = []
    for kind, idx, arr in (("BSL", np.where(sh)[0], h), ("SSL", np.where(sl)[0], l)):
        for i in idx:
            p = float(arr[i])
            later = h[i + 1:] if kind == "BSL" else l[i + 1:]
            taken = bool(len(later) and ((later.max() > p) if kind == "BSL" else (later.min() < p)))
            if taken:
                continue
            for z in pools:  # merge equal highs / equal lows
                if z["kind"] == kind and abs(z["price"] - p) <= eq_tol * atr:
                    z["touches"] += 1
                    z["price"] = max(z["price"], p) if kind == "BSL" else min(z["price"], p)
                    z["age_bars"] = min(z["age_bars"], len(d) - 1 - int(i))
                    break
            else:
                pools.append({"kind": kind, "price": p, "touches": 1, "age_bars": len(d) - 1 - int(i), "src": "swing"})
    return pools


def level_pools(levels):
    """Obvious levels that attract stops."""
    out = []
    for key, kind, name in (("prev_day_high", "BSL", "PDH"), ("prev_day_low", "SSL", "PDL"),
                            ("week_high", "BSL", "Week H"), ("week_low", "SSL", "Week L"),
                            ("last_week_high", "BSL", "Last week H"), ("last_week_low", "SSL", "Last week L"),
                            ("asian_high", "BSL", "Asia H"), ("asian_low", "SSL", "Asia L")):
        if levels.get(key) is not None:
            out.append({"kind": kind, "price": float(levels[key]), "touches": 1, "src": name})
    return out


def find_sweeps(df, tf, atr, pools):
    """Sweeps of the given pools by the last LOOKBACK closed bars of df."""
    n = LOOKBACK.get(tf, 4)
    d = df.reset_index(drop=True)
    if len(d) < n + 2:
        return []
    out = []
    h, l, c = d["high"].values, d["low"].values, d["close"].values
    for j in range(len(d) - n, len(d)):
        prev_h, prev_l = h[max(0, j - 30):j].max(), l[max(0, j - 30):j].min()
        for p in pools:
            lvl = p["price"]
            if p["kind"] == "BSL" and h[j] > lvl and c[j] < lvl and prev_h <= lvl + 1e-9:
                bias, depth = "SELL", h[j] - lvl
            elif p["kind"] == "SSL" and l[j] < lvl and c[j] > lvl and prev_l >= lvl - 1e-9:
                bias, depth = "BUY", lvl - l[j]
            else:
                continue
            after = c[j + 1:]
            # invalidated when price later CLOSES back beyond the swept level (the sweep failed)
            failed = bool(len(after) and ((after.max() > lvl) if bias == "SELL" else (after.min() < lvl)))
            out.append({"tf": tf, "level": p.get("src", "swing"), "pool": p["kind"], "price": _r(lvl),
                        "touches": p.get("touches", 1), "bias": bias, "age_bars": len(d) - 1 - j,
                        "wick": _r(h[j] if bias == "SELL" else l[j]), "depth_atr": _r(depth / max(atr, 1e-9)),
                        "failed": failed})
    # one entry per level: keep the most recent sweep
    best = {}
    for s in out:
        key = (s["tf"], s["pool"], s["price"])
        if key not in best or s["age_bars"] < best[key]["age_bars"]:
            best[key] = s
    return sorted(best.values(), key=lambda s: s["age_bars"])


def analyze(frames, levels, price, atrs):
    """frames: {tf: indicator df incl. forming bar}. Returns pools near price and recent sweeps."""
    res = {"sweeps": [], "pools_above": [], "pools_below": []}
    lv_pools = level_pools(levels or {})
    for tf in ("M5", "M15", "H1"):
        df = frames.get(tf)
        atr = atrs.get(tf)
        if df is None or not atr or len(df) < 30:
            continue
        closed = df.iloc[:-1]
        # pools that were resting BEFORE the scanned bars (a swept pool is no longer "resting" afterwards)
        before = closed.iloc[:-LOOKBACK.get(tf, 4)]
        res["sweeps"] += find_sweeps(closed, tf, atr, swing_pools(before, atr) + lv_pools)
        if tf == "H1":  # pools still resting now = targets / stop-hunt zones
            for p in swing_pools(closed, atr) + lv_pools:
                item = {"kind": p["kind"], "price": _r(p["price"]), "touches": p.get("touches", 1), "src": p.get("src"),
                        "distance_atr": _r((p["price"] - price) / atr)}
                if p["kind"] == "BSL" and p["price"] > price:   # buy stops resting above
                    res["pools_above"].append(item)
                elif p["kind"] == "SSL" and p["price"] < price:  # sell stops resting below
                    res["pools_below"].append(item)
    res["pools_above"] = sorted(res["pools_above"], key=lambda p: p["price"])[:4]
    res["pools_below"] = sorted(res["pools_below"], key=lambda p: -p["price"])[:4]
    fresh = [s for s in res["sweeps"] if s["age_bars"] < FRESH_BARS.get(s["tf"], 2) and not s["failed"]]
    res["fresh_bias"] = (fresh[0]["bias"] if fresh and len({s["bias"] for s in fresh}) == 1 else
                         "MIXED" if fresh else None)
    res["note"] = ("sweep = แท่งที่ปิดแล้วกวาดทะลุ pool แล้วปิดกลับ · bias SELL = กวาด buy-side (เหนือ high) "
                   "· failed = ภายหลังราคาปิดกลับทะลุระดับเดิม (สัญญาณใช้ไม่ได้)")
    return res


def check_plan(action, sl, liq, atr):
    """Liquidity check of an AI plan. Returns (problem or '', notes, alignment)."""
    if not liq or action not in ("BUY", "SELL"):
        return "", [], None
    notes = []
    fresh = [s for s in liq.get("sweeps", []) if s["age_bars"] < FRESH_BARS.get(s["tf"], 2) and not s["failed"]]
    against = [s for s in fresh if s["bias"] != action]
    with_ = [s for s in fresh if s["bias"] == action]
    problem = ""
    if against and not with_:
        s = against[0]
        problem = (f"สวน Liquidity Sweep ล่าสุด: {s['tf']} กวาด {s['level']} {s['price']} "
                   f"แล้วปิดกลับ ({s['age_bars']} แท่งก่อน) → bias {s['bias']}")
    alignment = "with" if with_ and not against else "against" if against and not with_ else "mixed" if fresh else "none"
    if with_:
        s = with_[0]
        notes.append(f"ตามทิศ Liquidity Sweep {s['tf']} {s['level']} {s['price']} (wick {s['wick']})")
        # SL should be beyond the sweep wick, otherwise the same hunt can stop us out
        if sl and atr and ((action == "BUY" and sl > s["wick"]) or (action == "SELL" and sl < s["wick"])):
            notes.append(f"⚠ SL {sl} อยู่ในระยะ wick ที่เพิ่งกวาด ({s['wick']}) – ควรวาง SL เลย wick")
    # SL resting right at an untouched pool = classic stop-hunt target
    if sl and atr:
        pools = liq.get("pools_below" if action == "BUY" else "pools_above", [])
        for p in pools:
            if abs(sl - p["price"]) <= 0.2 * atr:
                notes.append(f"⚠ SL {sl} อยู่ติด pool {p['src']} {p['price']} – เสี่ยงโดนกวาด SL")
                break
    return problem, notes, alignment
