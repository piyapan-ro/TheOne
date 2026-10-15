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
        pending = self.broker.orders()
        floating = sum(p["profit"] + p["swap"] for p in positions)
        acc = self.broker.account()
        if not acc:
            # MT5 not reachable: we cannot know the P/L, so never report the loss limit as hit
            # (that used to trip the auto Kill Switch and close everything on reconnect)
            return {"server_time": now, "day_start_balance": None, "closed_pl": round(closed, 2),
                    "floating_pl": round(floating, 2), "day_pl": round(closed + floating, 2), "loss_limit": None,
                    "loss_used_pct": 0.0, "loss_hit": False, "orders_today": entries, "open_positions": len(positions),
                    "pending_orders": len(pending), "data_ok": False}
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
            "loss_hit": limit not in (0, math.inf) and day_pl <= -limit,
            "orders_today": entries, "open_positions": len(positions),
            "pending_orders": len(pending), "data_ok": start_bal > 0,
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
    def check(self, symbol, side, volume, entry, sl, tp, market=True, lane=None, max_spread=None, symbol_risk_cap=None):
        cfg = self.cfg
        v = []
        if cfg.get("load_error"):
            v.append(cfg["load_error"])
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
        if not t.get("data_ok", True):
            v.append("อ่านข้อมูลบัญชีจาก MT5 ไม่ได้ – ตรวจลิมิตขาดทุนไม่ได้ จึงไม่ส่งออเดอร์")
        if t["loss_hit"]:
            v.append(f"ถึงขาดทุนสูงสุดต่อวันแล้ว ({t['day_pl']} / -{t['loss_limit']})")
        if cfg["max_orders_per_day"] and t["orders_today"] >= cfg["max_orders_per_day"]:
            v.append(f"ครบจำนวนออเดอร์ต่อวัน ({cfg['max_orders_per_day']})")
        # pending orders count too - otherwise LIMIT/STOP orders could be stacked beyond the limit
        busy = t["open_positions"] + t.get("pending_orders", 0)
        if cfg["max_open_positions"] and busy >= cfg["max_open_positions"]:
            v.append(f"มีออเดอร์เปิด/รอเข้า ({t['open_positions']}+{t.get('pending_orders', 0)}) ครบ {cfg['max_open_positions']} แล้ว")
        if lane:   # strategy / AI order: its own lane quota (other lanes are not counted)
            v += self.lane_violations(lane, symbol, side, cfg)
        else:      # manual order: the global same-direction rule
            v += self.same_side_violations(symbol, side, cfg)
        v += self.total_risk_violations(symbol, side, volume, entry, sl, cfg)
        if symbol_risk_cap:  # extra symbols: cap on the open risk of this symbol alone
            acc = self.broker.account() or {}
            if sl and acc.get("equity"):
                new = abs(self.broker.loss_per_lot(symbol, side, entry, sl) or 0) * volume
                pct = (self.open_risk(symbol) + new) / acc["equity"] * 100
                if pct > symbol_risk_cap + 1e-9:
                    v.append(f"ความเสี่ยงที่เปิดอยู่ของ {symbol} จะเป็น {pct:.1f}% เกินเพดาน {symbol_risk_cap}%")
        mspr = cfg["max_spread"] if max_spread is None else max_spread  # extra symbols bring their own limit
        if market and mspr:
            tk = self.broker.tick(symbol)
            if tk and tk["spread"] > mspr:
                v.append(f"Spread {symbol} {tk['spread']} สูงกว่าที่กำหนด {mspr}")
        return v

    # ------------------------------------------------------------- lanes
    def _magic(self):
        return self.get_settings().get("dashboard_magic")

    def lane_status(self, lane, symbol=None):
        """Open/pending count, entries today, last entry time and today's P/L of one lane (comment AIT:<lane>)."""
        tag = "AIT:" + lane
        magic = self._magic()
        now = self.server_now()
        day0 = now // DAY * DAY
        pos = [p for p in self.broker.positions() if p["magic"] == magic and p["comment"] == tag
               and (symbol is None or p["symbol"] == symbol)]
        pend = [o for o in self.broker.orders() if o["magic"] == magic and o["comment"] == tag]
        deals = [d for d in self.broker.deals(day0 - 2 * DAY, now + 3600) if d.get("magic") == magic]
        entries = {d["position_id"]: d for d in deals if d.get("entry") == 0 and d.get("comment") == tag}
        today_entries = [d for d in entries.values() if d["time"] >= day0]
        closed = sum(deal_net(d) for d in deals if d.get("entry") in (1, 3) and d["time"] >= day0
                     and d["position_id"] in entries)
        floating = sum(p["profit"] + p["swap"] for p in pos)
        return {"lane": lane, "open": len(pos), "pending": len(pend), "orders_today": len(today_entries),
                "last_entry": max((d["time"] for d in entries.values()), default=0),
                "day_pl": round(closed + floating, 2)}

    def lane_violations(self, lane, symbol, side, cfg):
        st_ = self.lane_status(lane)  # the comment tag already identifies the lane (and its symbol)
        out = []
        mx = int(cfg.get("lane_max_positions", 1) or 0)
        if mx and st_["open"] + st_["pending"] >= mx:
            out.append(f"เลน {lane} มีออเดอร์เปิด/รอเข้าครบ {mx} แล้ว")
        cool = float(cfg.get("lane_cooldown_min", 0) or 0)
        now = self.server_now()
        if cool and st_["last_entry"] and now - st_["last_entry"] < cool * 60:
            mins = max(1, math.ceil((cool * 60 - (now - st_["last_entry"])) / 60))
            out.append(f"เลน {lane} เพิ่งเข้าไม้ – รออีก {mins} นาที")
        mo = int(cfg.get("lane_max_orders_day", 0) or 0)
        if mo and st_["orders_today"] >= mo:
            out.append(f"เลน {lane} เข้าครบ {mo} ไม้วันนี้แล้ว")
        lim = float(cfg.get("lane_max_daily_loss_pct", 0) or 0)
        acc = self.broker.account() or {}
        if lim and acc.get("balance") and st_["day_pl"] <= -acc["balance"] * lim / 100:
            out.append(f"เลน {lane} ขาดทุนวันนี้ {st_['day_pl']} ถึงลิมิต {lim}% – หยุดเลนนี้ถึงวันใหม่")
        return out

    def open_risk(self, symbol=None):
        """Money at risk if every open position / pending order hit its SL (positions without SL are skipped)."""
        def one(sym_, side, price, sl, vol):
            if not sl or (side == "BUY" and sl >= price) or (side == "SELL" and sl <= price):
                return 0.0  # no SL, or SL already at breakeven / in profit
            return abs(self.broker.loss_per_lot(sym_, side, price, sl) or 0) * vol
        total = sum(one(p["symbol"], p["side"], p["price_open"], p["sl"], p["volume"]) for p in self.broker.positions()
                    if symbol is None or p["symbol"] == symbol)
        for o in self.broker.orders():
            if symbol is not None and o["symbol"] != symbol:
                continue
            side = "BUY" if str(o["type"]).startswith("BUY") else "SELL"
            total += one(o["symbol"], side, o["price"], o["sl"], o["volume"])
        return total

    def total_risk_violations(self, symbol, side, volume, entry, sl, cfg):
        cap = float(cfg.get("max_total_open_risk_pct", 0) or 0)
        acc = self.broker.account() or {}
        if not cap or not sl or not acc.get("equity"):
            return []
        new = max(0.0, (self.broker.loss_per_lot(symbol, side, entry, sl) or 0) * volume)
        cur = self.open_risk()
        pct = (cur + new) / acc["equity"] * 100
        if pct > cap + 1e-9:
            return [f"ความเสี่ยงรวมที่เปิดอยู่จะเป็น {pct:.1f}% เกินเพดาน {cap}% (เปิดอยู่ {cur:.2f} + ไม้นี้ {new:.2f})"]
        return []

    def same_side_violations(self, symbol, side, cfg):
        """Manual Dashboard orders (comment AIT:MANUAL): limit same-direction stacking.
        Strategy / AI orders use their lane quota instead; EAs are not affected."""
        out = []
        magic = self.get_settings().get("dashboard_magic")
        tag = "AIT:MANUAL"
        max_same = int(cfg.get("max_same_side") or 0)
        if max_same:
            n = sum(1 for p in self.broker.positions()
                    if p["magic"] == magic and p["comment"] == tag and p["symbol"] == symbol and p["side"] == side)
            n += sum(1 for o in self.broker.orders()
                     if o["magic"] == magic and o["comment"] == tag and o["symbol"] == symbol and str(o["type"]).startswith(side))
            if n >= max_same:
                out.append(f"มีออเดอร์ {side} จาก Dashboard เปิด/รอเข้าอยู่แล้ว {n} ไม้ (สูงสุด {max_same} ไม้ต่อทิศ)")
        cool = float(cfg.get("same_side_cooldown_min") or 0)
        if cool:
            now = self.server_now()
            want = 0 if side == "BUY" else 1  # deal type of an entry
            last = max((d["time"] for d in self.broker.deals(now - int(cool * 60) - 3600, now + 3600)
                        if d.get("entry") == 0 and d.get("type") == want and d.get("magic") == magic
                        and d.get("comment") == tag and d.get("symbol") == symbol), default=0)
            if last and now - last < cool * 60:
                mins = max(1, math.ceil((cool * 60 - (now - last)) / 60))
                out.append(f"เพิ่งเข้า {side} ไปเมื่อ {int((now - last) // 60)} นาทีก่อน – รออีก {mins} นาที (กันเข้าซ้อนทิศเดียวกัน)")
        return out

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
            if t["loss_hit"] and t.get("data_ok", True):
                self.kill(f"ขาดทุนถึงลิมิตรายวัน {t['day_pl']}", auto=True)
                return
        if cfg["kill_switch"]:
            # while the switch is ON, anything that opens (EA or manual) is closed again
            if self.broker.positions() or self.broker.orders():
                res = self.close_everything()
                self.log(f"Kill Switch บังคับปิด {len(res)} รายการที่เปิดใหม่", "warn")
