"""Risk manager: pre-trade checks, lot sizing, daily limits and the Kill Switch."""
import math
import time

import store
from analytics import deal_net

DAY = 86400


class RiskManager:
    def __init__(self, broker, get_settings):
        self.broker = broker
        self.get_settings = get_settings
        self.events = []  # recent risk events for the UI
        self.offset = None  # broker server time - local unix time (whole hours)

    def log(self, msg, level="info"):
        self.events.insert(0, {"time": int(time.time()), "level": level, "msg": msg})
        del self.events[50:]
        print(f"[RISK] {msg}")

    @property
    def cfg(self):
        return store.load("risk")

    def server_now(self):
        """Broker server clock. The last tick time alone is not enough: gold has a daily break
        around server midnight, so the tick stays on 'yesterday' until the market reopens.
        We learn the server's UTC offset from fresh ticks and keep the clock running."""
        now = time.time()
        t = self.broker.tick(self.get_settings()["symbol"])
        if t:
            est = round((t["time"] - now) / 3600) * 3600
            if self.offset is None or est > self.offset:
                self.offset = est
        if self.offset is None:
            return int(t["time"]) if t else int(now)
        return int(max(now + self.offset, t["time"] if t else 0))

    def server_day(self):
        return self.server_now() // DAY * DAY

    # ------------------------------------------------------------- stats
    def today(self):
        cfg = self.cfg
        now = self.server_now()
        day0 = now // DAY * DAY
        deals = self.broker.deals(day0, now + 3600)
        trade_deals = [d for d in deals if d.get("type") in (0, 1)]
        closed = sum(deal_net(d) for d in trade_deals)
        entries = sum(1 for d in trade_deals if d["entry"] == 0)
        positions = self.broker.positions()
        floating = sum(p["profit"] + p["swap"] for p in positions)
        acc = self.broker.account() or {"balance": 0, "equity": 0}
        start_bal = acc["balance"] - closed
        limit = start_bal * cfg["max_daily_loss_pct"] / 100 if cfg["max_daily_loss_pct"] > 0 else math.inf
        if cfg["max_daily_loss_money"] > 0:
            limit = min(limit, cfg["max_daily_loss_money"])
        day_pl = closed + floating
        return {
            "server_time": now, "day_start_balance": round(start_bal, 2), "closed_pl": round(closed, 2),
            "floating_pl": round(floating, 2), "day_pl": round(day_pl, 2),
            "loss_limit": None if limit == math.inf else round(limit, 2),
            "loss_used_pct": round(-day_pl / limit * 100, 1) if limit not in (0, math.inf) and day_pl < 0 else 0.0,
            "loss_hit": limit != math.inf and day_pl <= -limit,
            "orders_today": entries, "open_positions": len(positions),
        }

    # ------------------------------------------------------------- lot sizing
    def normalize_lot(self, symbol, lot):
        s = self.broker.symbol_info(symbol) or {"volume_min": 0.01, "volume_max": 100, "volume_step": 0.01}
        step = s["volume_step"] or 0.01
        lot = math.floor(lot / step + 1e-9) * step
        lot = min(lot, s["volume_max"], self.cfg["max_lot"])
        digits = max(0, int(round(-math.log10(step))))
        return round(lot, digits) if lot >= s["volume_min"] else 0.0

    def calc(self, symbol, side, entry, sl, tp=None, risk_pct=None, lot=None):
        """Lot from risk %, or risk from a given lot. Also returns R:R."""
        acc = self.broker.account() or {"equity": 0}
        risk_pct = self.cfg["risk_per_trade_pct"] if risk_pct is None else float(risk_pct)
        out = {"entry": entry, "sl": sl, "tp": tp, "equity": acc["equity"]}
        if not sl or sl == entry:
            out["error"] = "ต้องระบุ Stop Loss เพื่อคำนวณ Lot"
            return out
        lpl = self.broker.loss_per_lot(symbol, side, entry, sl)
        if not lpl:
            out["error"] = "คำนวณมูลค่าต่อ Lot ไม่ได้"
            return out
        cfg = self.cfg
        out["lot_mode"] = cfg.get("lot_mode", "risk")
        if lot is None and out["lot_mode"] == "fixed":
            lot = self.normalize_lot(symbol, float(cfg.get("default_lot") or 0.01))
        elif lot is None:
            raw = acc["equity"] * risk_pct / 100 / lpl
            lot = self.normalize_lot(symbol, raw)
            out["raw_lot"] = round(raw, 4)
        out["lot"] = lot
        out["risk_money"] = round(lpl * lot, 2)
        out["risk_pct"] = round(lpl * lot / acc["equity"] * 100, 3) if acc["equity"] else None
        out["sl_distance"] = round(abs(entry - sl), 3)
        if tp:
            out["tp_distance"] = round(abs(tp - entry), 3)
            out["reward_money"] = round(abs(tp - entry) / abs(entry - sl) * lpl * lot, 2)
            out["rr"] = round(abs(tp - entry) / abs(entry - sl), 2)
        return out

    # ------------------------------------------------------------- checks
    def check(self, symbol, side, volume, entry, sl, tp, market=True):
        cfg = self.cfg
        v = []
        if cfg["kill_switch"]:
            v.append("Kill Switch เปิดอยู่ – ระบบหยุดเทรด")
        if volume <= 0:
            v.append("Lot ต้องมากกว่า 0")
        if volume > cfg["max_lot"] + 1e-9:
            v.append(f"Lot {volume} เกิน Lot สูงสุด {cfg['max_lot']}")
        if cfg["require_sl"] and not sl:
            v.append("ต้องตั้ง Stop Loss ทุกออเดอร์")
        if sl:
            if side == "BUY" and sl >= entry:
                v.append("SL ของ BUY ต้องต่ำกว่าราคาเข้า")
            if side == "SELL" and sl <= entry:
                v.append("SL ของ SELL ต้องสูงกว่าราคาเข้า")
            c = self.calc(symbol, side, entry, sl, tp, lot=volume)
            if c.get("risk_pct") is not None and c["risk_pct"] > cfg["risk_per_trade_pct"] * 1.10:
                v.append(f"ความเสี่ยงไม้นี้ {c['risk_pct']}% เกิน Risk ต่อไม้ {cfg['risk_per_trade_pct']}% (ราคาขยับ – กดคำนวณ Lot ใหม่)")
        if tp:
            if side == "BUY" and tp <= entry:
                v.append("TP ของ BUY ต้องสูงกว่าราคาเข้า")
            if side == "SELL" and tp >= entry:
                v.append("TP ของ SELL ต้องต่ำกว่าราคาเข้า")
        t = self.today()
        if t["loss_hit"]:
            v.append(f"ถึงขาดทุนสูงสุดต่อวันแล้ว ({t['day_pl']} / -{t['loss_limit']})")
        if cfg["max_orders_per_day"] and t["orders_today"] >= cfg["max_orders_per_day"]:
            v.append(f"ครบจำนวนออเดอร์ต่อวัน ({cfg['max_orders_per_day']})")
        if cfg["max_open_positions"] and t["open_positions"] >= cfg["max_open_positions"]:
            v.append(f"มีออเดอร์เปิดครบ {cfg['max_open_positions']} แล้ว")
        if market and cfg["max_spread"]:
            tk = self.broker.tick(symbol)
            if tk and tk["spread"] > cfg["max_spread"]:
                v.append(f"Spread {tk['spread']} สูงกว่าที่กำหนด {cfg['max_spread']}")
        return v

    # ------------------------------------------------------------- kill switch
    def close_everything(self):
        results = []
        for p in self.broker.positions():
            results.append({"ticket": p["ticket"], **self.broker.close_position(p["ticket"])})
        for o in self.broker.orders():
            results.append({"ticket": o["ticket"], **self.broker.cancel_order(o["ticket"])})
        return results

    def kill(self, reason="manual", auto=False):
        store.update("risk", {"kill_switch": True, "kill_switch_reason": reason, "kill_switch_time": int(time.time()),
                              "kill_switch_auto": bool(auto), "kill_switch_day": self.server_day()})
        res = self.close_everything()
        self.log(f"KILL SWITCH ON ({reason}) – ปิด {len(res)} รายการ", "danger")
        return res

    def release(self, why="กลับมาเทรดได้"):
        store.update("risk", {"kill_switch": False, "kill_switch_reason": "", "kill_switch_time": 0,
                              "kill_switch_auto": False, "kill_switch_day": 0})
        self.log(f"Kill Switch OFF – {why}", "info")

    def _auto_kill_info(self, cfg):
        """(is_auto, day) - also recognises switches set by older versions (no kill_switch_auto field)."""
        auto = cfg.get("kill_switch_auto")
        if auto is None or (not auto and str(cfg.get("kill_switch_reason", "")).startswith("ขาดทุนถึงลิมิตรายวัน")):
            auto = str(cfg.get("kill_switch_reason", "")).startswith("ขาดทุนถึงลิมิตรายวัน")
        day = cfg.get("kill_switch_day") or 0
        if not day and cfg.get("kill_switch_time"):
            day = (cfg["kill_switch_time"] + (self.offset or 0)) // DAY * DAY
        return bool(auto), day

    def enforce(self):
        """Called periodically by the background loop."""
        cfg = self.cfg
        if cfg["kill_switch"] and cfg.get("auto_release_new_day", True):
            auto, day = self._auto_kill_info(cfg)
            if auto and day and self.server_day() > day:
                self.release("ขึ้นวันใหม่ (เวลา Server) ปลดล็อกอัตโนมัติ – ลิมิตขาดทุนรายวันเริ่มนับใหม่")
                cfg = self.cfg
        if not cfg["kill_switch"] and cfg["auto_kill_on_daily_loss"]:
            t = self.today()
            if t["loss_hit"]:
                self.kill(f"ขาดทุนถึงลิมิตรายวัน {t['day_pl']}", auto=True)
                return
        if cfg["kill_switch"]:
            # while the switch is ON, anything that opens (EA or manual) is closed again
            if self.broker.positions() or self.broker.orders():
                res = self.close_everything()
                self.log(f"Kill Switch บังคับปิด {len(res)} รายการที่เปิดใหม่", "warn")
