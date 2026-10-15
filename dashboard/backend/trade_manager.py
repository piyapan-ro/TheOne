"""
Trade Manager: Breakeven, ATR trailing stop and partial take-profit for open positions,
plus a "profit given back" analysis of closed trades with a what-if simulator.

All distances are in multiples of ATR measured on `atr_tf` (default M5), the same
way the GoldScalper EA does it, so the rules adapt to gold's volatility.
SL is only ever moved in the trade's favour (never loosened).
"""
import math
import time

import numpy as np
import pandas as pd

import analytics
import store
import strategies as st

DEFAULTS = {
    "enabled": True,
    "sources": ["Dashboard", "Manual"],   # which journal sources to manage (EA names from the Magic map)
    "atr_tf": "M5",
    "be_enabled": True, "be_trigger_atr": 1.0, "be_lock": 0.10,          # lock-in in price units ($)
    "trail_enabled": True, "trail_start_atr": 1.5, "trail_dist_atr": 1.0,
    "partial_enabled": False, "partial_atr": 1.0, "partial_pct": 50,
    "min_step": 0.05,                    # min SL improvement before sending a modify (price units)
}

PRESETS = {
    "be_only": {"label": "Breakeven อย่างเดียว (1 ATR)", "be_enabled": True, "be_trigger_atr": 1.0, "trail_enabled": False},
    "tight": {"label": "Trail ไว: เริ่ม 1.0 ATR ห่าง 0.7 ATR", "be_enabled": True, "be_trigger_atr": 0.7,
              "trail_enabled": True, "trail_start_atr": 1.0, "trail_dist_atr": 0.7},
    "balanced": {"label": "สมดุล: BE 1.0 · Trail เริ่ม 1.5 ห่าง 1.0 ATR", "be_enabled": True, "be_trigger_atr": 1.0,
                 "trail_enabled": True, "trail_start_atr": 1.5, "trail_dist_atr": 1.0},
    "loose": {"label": "Trail หลวม: เริ่ม 2.0 ห่าง 1.5 ATR", "be_enabled": True, "be_trigger_atr": 1.5,
              "trail_enabled": True, "trail_start_atr": 2.0, "trail_dist_atr": 1.5},
    "trail_only": {"label": "Trail อย่างเดียว: เริ่ม 1.0 ห่าง 1.0 ATR", "be_enabled": False,
                   "trail_enabled": True, "trail_start_atr": 1.0, "trail_dist_atr": 1.0},
}


def config():
    return {**DEFAULTS, **store.load("trade_manager")}


class TradeManager:
    def __init__(self, broker, get_settings, log):
        self.broker = broker
        self.get_settings = get_settings
        self.log = log
        self._atr = {"t": 0, "v": None}
        self.partial_done = set()
        self.last_action = {}

    def atr(self, cfg):
        if time.time() - self._atr["t"] > 30 or self._atr["v"] is None:
            df = self.broker.rates(self.get_settings()["symbol"], cfg["atr_tf"], 120)
            if len(df) > 20:
                self._atr = {"t": time.time(), "v": float(st.atr(df).iloc[-2])}
        return self._atr["v"]

    def run(self):
        cfg = config()
        if not cfg["enabled"] or store.load("risk")["kill_switch"]:
            return
        positions = self.broker.positions()
        if not positions:
            return
        a = self.atr(cfg)
        if not a:
            return
        ea_map = store.load("ea_map")
        sym = self.get_settings()["symbol"]
        tick = self.broker.tick(sym)
        info = self.broker.symbol_info(sym) or {}
        if not tick:
            return
        min_dist = (info.get("trade_stops_level") or 0) * (info.get("point") or 0.01) + tick["spread"] + 0.02
        for p in positions:
            if p["symbol"] != sym or analytics.source_of(p["magic"], ea_map) not in cfg["sources"]:
                continue
            self.manage(p, cfg, a, tick, info, min_dist)

    def manage(self, p, cfg, a, tick, info, min_dist):
        buy = p["side"] == "BUY"
        px = tick["bid"] if buy else tick["ask"]
        entry, sl = p["price_open"], p["sl"] or 0.0
        fav = (px - entry) if buy else (entry - px)
        if fav <= 0:
            return
        # partial take-profit
        if cfg["partial_enabled"] and p["ticket"] not in self.partial_done and fav >= cfg["partial_atr"] * a:
            step = info.get("volume_step") or 0.01
            vmin = info.get("volume_min") or 0.01
            vol = math.floor(p["volume"] * cfg["partial_pct"] / 100 / step + 1e-9) * step
            self.partial_done.add(p["ticket"])
            if vol >= vmin and p["volume"] - vol >= vmin - 1e-9:
                r = self.broker.close_position(p["ticket"], volume=round(vol, 2), comment="partial TP")
                self.log(f"Partial TP #{p['ticket']} ปิด {vol:.2f} lot ที่กำไร {fav:.2f} -> {r.get('message')}")
        # new stop candidate
        new_sl = sl
        if cfg["be_enabled"] and fav >= cfg["be_trigger_atr"] * a:
            be = entry + cfg["be_lock"] if buy else entry - cfg["be_lock"]
            new_sl = max(new_sl, be) if buy else (be if not new_sl else min(new_sl, be))
        if cfg["trail_enabled"] and fav >= cfg["trail_start_atr"] * a:
            tr = px - cfg["trail_dist_atr"] * a if buy else px + cfg["trail_dist_atr"] * a
            new_sl = max(new_sl, tr) if buy else (tr if not new_sl else min(new_sl, tr))
        if not new_sl:
            return
        # respect broker minimum distance
        new_sl = min(new_sl, px - min_dist) if buy else max(new_sl, px + min_dist)
        digits = info.get("digits") or 2
        new_sl = round(new_sl, digits)
        improves = (new_sl > sl + cfg["min_step"]) if buy else (not sl or new_sl < sl - cfg["min_step"])
        if not improves:
            return
        if time.time() - self.last_action.get(p["ticket"], 0) < 3:
            return
        self.last_action[p["ticket"]] = time.time()
        r = self.broker.modify_position(p["ticket"], new_sl, p["tp"])
        kind = "Trailing" if cfg["trail_enabled"] and fav >= cfg["trail_start_atr"] * a else "Breakeven"
        self.log(f"{kind} #{p['ticket']} {p['side']} SL {sl or '-'} → {new_sl} (กำไร {fav:.2f}, ATR {a:.2f}) "
                 f"{'' if r.get('ok') else '✗ ' + str(r.get('message'))}", "info" if r.get("ok") else "warn")


# ---------------------------------------------------------------- give-back analysis
def _simulate(trade, bars, atr_entry, cfg, mpp):
    """Replay one closed trade on M1 bars with the given BE/trailing rules.
    Conservative: stop is checked with the previous bar's stop before updating from the new extreme."""
    buy = trade["side"] == "BUY"
    entry = trade["open_price"]
    sl = None
    best = 0.0
    for h, l in bars:
        if sl is not None and ((buy and l <= sl) or (not buy and h >= sl)):
            pts = (sl - entry) if buy else (entry - sl)
            return pts * mpp + trade["commission"] + trade["swap"], True
        best = max(best, (h - entry) if buy else (entry - l))
        cand = None
        if cfg.get("be_enabled") and best >= cfg["be_trigger_atr"] * atr_entry:
            cand = entry + cfg.get("be_lock", 0.1) if buy else entry - cfg.get("be_lock", 0.1)
        if cfg.get("trail_enabled") and best >= cfg["trail_start_atr"] * atr_entry:
            tr = (entry + best - cfg["trail_dist_atr"] * atr_entry) if buy else (entry - best + cfg["trail_dist_atr"] * atr_entry)
            cand = tr if cand is None else (max(cand, tr) if buy else min(cand, tr))
        if cand is not None:
            sl = cand if sl is None else (max(sl, cand) if buy else min(sl, cand))
    return trade["profit"], False


def give_back(broker, symbol, trades, contract=100.0, atr_tf="M5", current_cfg=None):
    closed = [t for t in trades if t["status"] == "closed" and t["symbol"] == symbol and t["close_time"]
              and t["points"] is not None]
    if not closed:
        return {"trades": 0}
    t0 = min(t["open_time"] for t in closed) - 3600 * 6
    t1 = max(t["close_time"] for t in closed) + 120
    m1 = broker.rates_range(symbol, "M1", t0, t1)
    atr_df = broker.rates_range(symbol, atr_tf, t0 - 86400, t1)
    if m1.empty or atr_df.empty:
        return {"trades": 0, "error": "ไม่มีข้อมูลราคา M1 ย้อนหลังพอ (เพิ่ม Max bars in chart ใน MT5)"}
    atr_s = st.atr(atr_df).values
    atr_t = atr_df["time"].values
    mt, mh, ml = m1["time"].values, m1["high"].values, m1["low"].values
    rows = []
    for t in closed:
        i0 = np.searchsorted(mt, t["open_time"], side="right")  # first full bar after entry
        i1 = np.searchsorted(mt, t["close_time"], side="right") - 1
        if i1 < i0:
            continue
        hs, ls = mh[i0:i1 + 1], ml[i0:i1 + 1]
        buy = t["side"] == "BUY"
        mfe = float((hs.max() - t["open_price"]) if buy else (t["open_price"] - ls.min()))
        mae = float((t["open_price"] - ls.min()) if buy else (hs.max() - t["open_price"]))
        k = np.searchsorted(atr_t, t["open_time"], side="right") - 2
        a = float(atr_s[max(k, 0)])
        mpp = t["volume"] * contract
        rows.append({"t": t, "bars": list(zip(hs, ls)), "atr": a, "mpp": mpp,
                     "mfe": max(mfe, 0.0), "mae": max(mae, 0.0)})
    if not rows:
        return {"trades": 0}

    # --- give-back statistics
    gb_money = 0.0
    red_after_green = []
    captured = []
    for r in rows:
        t = r["t"]
        realized = t["points"]
        if r["mfe"] > 0:
            captured.append(max(min(realized / r["mfe"], 1.0), -1.0))
        gb = max(r["mfe"] - realized, 0.0)
        gb_money += gb * r["mpp"]
        if t["profit"] <= 0 and r["mfe"] >= 1.0 * r["atr"]:
            red_after_green.append({"position_id": t["position_id"], "open_time": t["open_time"], "side": t["side"],
                                    "source": t["source"], "mfe": round(r["mfe"], 2), "mfe_atr": round(r["mfe"] / r["atr"], 2),
                                    "profit": t["profit"], "mfe_money": round(r["mfe"] * r["mpp"], 2)})
    actual = sum(r["t"]["profit"] for r in rows)
    summary = {
        "trades": len(rows), "actual_net": round(actual, 2),
        "avg_mfe": round(float(np.mean([r["mfe"] for r in rows])), 2),
        "avg_mae": round(float(np.mean([r["mae"] for r in rows])), 2),
        "avg_mfe_atr": round(float(np.mean([r["mfe"] / r["atr"] for r in rows if r["atr"] > 0])), 2),
        "capture_pct": round(float(np.mean(captured)) * 100, 1) if captured else None,
        "given_back_money": round(gb_money, 2),
        "red_after_green": len(red_after_green),
        "red_after_green_money": round(sum(-x["profit"] for x in red_after_green), 2),
    }

    # --- what-if presets (+ the current settings)
    presets = dict(PRESETS)
    if current_cfg:
        presets = {"current": {"label": "ค่าที่ตั้งไว้ตอนนี้", **{k: current_cfg[k] for k in
                   ("be_enabled", "be_trigger_atr", "be_lock", "trail_enabled", "trail_start_atr", "trail_dist_atr")}},
                   **presets}
    whatif = []
    for key, p in presets.items():
        cfg = {"be_lock": 0.10, **p}
        net, stopped, wins = 0.0, 0, 0
        by_src = {}
        for r in rows:
            v, hit = _simulate(r["t"], r["bars"], r["atr"], cfg, r["mpp"])
            net += v; stopped += hit; wins += v > 0
            s = by_src.setdefault(r["t"]["source"], [0.0, 0.0])
            s[0] += v; s[1] += r["t"]["profit"]
        whatif.append({"key": key, "label": p["label"], "net": round(net, 2), "delta": round(net - actual, 2),
                       "win_rate": round(wins / len(rows) * 100, 1), "stopped_early": stopped,
                       "by_source": {k: {"sim": round(v[0], 2), "actual": round(v[1], 2)} for k, v in by_src.items()},
                       "params": {k: v for k, v in p.items() if k != "label"}})
    whatif.sort(key=lambda x: -x["net"])
    best = whatif[0] if whatif and whatif[0]["delta"] > 0 else None
    return {"summary": summary, "whatif": whatif, "best": best["key"] if best else None,
            "red_after_green": sorted(red_after_green, key=lambda x: x["profit"])[:30],
            "sources": sorted({r["t"]["source"] for r in rows})}
