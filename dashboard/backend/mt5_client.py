"""
MT5 access layer.

* LiveBroker  - talks to a running MetaTrader 5 terminal through the official
                `MetaTrader5` Python package (Windows only).
* DemoBroker  - an in-memory simulator with the same interface, used automatically
                when MetaTrader5 is not installed / cannot connect. Everything it
                shows is SIMULATED data - it is only for UI testing and teaching.

All times are MT5 *server time* expressed as unix seconds (the same convention
the MetaTrader5 package uses).
"""
import math
import random
import threading
import time

import numpy as np
import pandas as pd

TF_SECONDS = {"M1": 60, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600, "H4": 14400, "D1": 86400}

try:
    import MetaTrader5 as mt5  # type: ignore
except Exception:  # not installed (e.g. Linux / Mac)
    mt5 = None


def _r(x, d=2):
    try:
        return round(float(x), d)
    except Exception:
        return x


# ---------------------------------------------------------------------------
# Live MT5
# ---------------------------------------------------------------------------
class LiveBroker:
    mode = "live"

    def __init__(self, cfg):
        self.cfg = cfg or {}
        self.lock = threading.RLock()
        self.last_error = ""

    # --- connection ---------------------------------------------------------
    def connect(self):
        with self.lock:
            kw = {}
            if self.cfg.get("path"):
                kw["path"] = self.cfg["path"]
            if self.cfg.get("login"):
                kw.update(login=int(self.cfg["login"]), password=self.cfg.get("password", ""),
                          server=self.cfg.get("server", ""))
            ok = mt5.initialize(**kw)
            if not ok:
                self.last_error = str(mt5.last_error())
            return ok

    def ensure(self):
        with self.lock:
            if mt5.terminal_info() is None:
                if not getattr(self, "_lost", False):  # log the drop once, with the reason
                    self._lost = True
                    print(f"[MT5] {time.strftime('%Y-%m-%d %H:%M:%S')} connection to the terminal lost ({mt5.last_error()}) - reconnecting")
                ok = self.connect()
                if ok:
                    self._lost = False
                    print(f"[MT5] {time.strftime('%Y-%m-%d %H:%M:%S')} reconnected")
                return ok
            return True

    def status(self):
        with self.lock:
            ti = mt5.terminal_info()
            return {
                "mode": self.mode,
                "connected": ti is not None,
                "trade_allowed": bool(ti.trade_allowed) if ti else False,
                "terminal": ti.name if ti else "",
                "error": self.last_error,
            }

    # --- data -----------------------------------------------------------------
    def account(self):
        with self.lock:
            a = mt5.account_info()
            if a is None:
                return None
            a = a._asdict()
            out = {k: a.get(k) for k in ("login", "name", "server", "currency", "leverage", "balance", "equity",
                                        "credit", "profit", "margin", "margin_free", "margin_level",
                                        "trade_allowed", "trade_expert")}
            out["is_demo"] = a.get("trade_mode") == 0  # 0 = demo, 1 = contest, 2 = real
            return out

    def symbol_info(self, symbol):
        with self.lock:
            if not mt5.symbol_select(symbol, True):
                return None
            s = mt5.symbol_info(symbol)
            if s is None:
                return None
            s = s._asdict()
            return {k: s.get(k) for k in ("name", "digits", "point", "volume_min", "volume_max", "volume_step",
                                         "trade_contract_size", "trade_tick_value", "trade_tick_size",
                                         "trade_stops_level", "filling_mode", "spread", "trade_mode")}

    def find_symbol(self, preferred="XAUUSD"):
        """Return the broker's gold symbol (XAUUSD, XAUUSDm, XAUUSD.r, GOLD ...)."""
        with self.lock:
            names = [s.name for s in (mt5.symbols_get() or [])]
        if preferred in names:
            return preferred
        cands = [n for n in names if n.upper().startswith("XAUUSD")] or \
                [n for n in names if "XAU" in n.upper() and "USD" in n.upper()] or \
                [n for n in names if n.upper().startswith("GOLD")]
        return sorted(cands, key=len)[0] if cands else None

    def find_symbol_like(self, preferred):
        """Broker name of any symbol: exact, else the shortest name starting with it (BTCUSD -> BTCUSDm)."""
        with self.lock:
            names = [s.name for s in (mt5.symbols_get() or [])]
        if preferred in names:
            return preferred
        cands = [n for n in names if n.upper().startswith(preferred.upper())]
        return sorted(cands, key=len)[0] if cands else None

    def find_dollar_index(self):
        """DXY / USDX if the broker offers it (not all do)."""
        with self.lock:
            names = [x.name for x in (mt5.symbols_get() or [])]
        for pat in ("DXY", "USDX", "USDINDEX", "DOLLAR"):
            c = [n for n in names if pat in n.upper().replace("_", "").replace(".", "")]
            if c:
                return sorted(c, key=len)[0]
        return None

    def tick(self, symbol):
        with self.lock:
            mt5.symbol_select(symbol, True)
            t = mt5.symbol_info_tick(symbol)
            if t is None:
                return None
            return {"time": int(t.time), "bid": t.bid, "ask": t.ask, "spread": _r(t.ask - t.bid, 3)}

    def rates(self, symbol, tf, count=500):
        with self.lock:
            tfc = getattr(mt5, "TIMEFRAME_" + tf)
            mt5.symbol_select(symbol, True)
            arr = mt5.copy_rates_from_pos(symbol, tfc, 0, int(count))
        if arr is None or len(arr) == 0:
            return pd.DataFrame(columns=["time", "open", "high", "low", "close", "tick_volume"])
        df = pd.DataFrame(arr)[["time", "open", "high", "low", "close", "tick_volume"]]
        df["time"] = df["time"].astype("int64")
        return df

    def rates_range(self, symbol, tf, t_from, t_to):
        from datetime import datetime, timezone
        with self.lock:
            mt5.symbol_select(symbol, True)
            arr = mt5.copy_rates_range(symbol, getattr(mt5, "TIMEFRAME_" + tf),
                                       datetime.fromtimestamp(max(0, t_from), tz=timezone.utc),
                                       datetime.fromtimestamp(t_to, tz=timezone.utc))
        if arr is None or len(arr) == 0:
            return pd.DataFrame(columns=["time", "open", "high", "low", "close", "tick_volume"])
        df = pd.DataFrame(arr)[["time", "open", "high", "low", "close", "tick_volume"]]
        df["time"] = df["time"].astype("int64")
        return df

    def positions(self):
        with self.lock:
            ps = mt5.positions_get() or []
        out = []
        for p in ps:
            out.append({"ticket": p.ticket, "time": int(p.time), "symbol": p.symbol,
                        "side": "BUY" if p.type == 0 else "SELL", "volume": p.volume,
                        "price_open": p.price_open, "sl": p.sl, "tp": p.tp, "price_current": p.price_current,
                        "swap": p.swap, "profit": p.profit, "magic": p.magic, "comment": p.comment})
        return out

    def orders(self):
        names = {2: "BUY LIMIT", 3: "SELL LIMIT", 4: "BUY STOP", 5: "SELL STOP", 6: "BUY STOP LIMIT", 7: "SELL STOP LIMIT"}
        with self.lock:
            os_ = mt5.orders_get() or []
        return [{"ticket": o.ticket, "time": int(o.time_setup), "symbol": o.symbol, "type": names.get(o.type, str(o.type)),
                 "volume": o.volume_current, "price": o.price_open, "sl": o.sl, "tp": o.tp, "magic": o.magic,
                 "comment": o.comment} for o in os_]

    def deals(self, t_from, t_to):
        from datetime import datetime, timezone
        with self.lock:
            ds = mt5.history_deals_get(datetime.fromtimestamp(max(0, t_from), tz=timezone.utc),
                                       datetime.fromtimestamp(t_to, tz=timezone.utc)) or []
        return [{"ticket": d.ticket, "order": d.order, "time": int(d.time), "type": d.type, "entry": d.entry,
                 "magic": d.magic, "position_id": d.position_id, "volume": d.volume, "price": d.price,
                 "commission": d.commission, "swap": d.swap, "profit": d.profit, "fee": getattr(d, "fee", 0.0),
                 "symbol": d.symbol, "comment": d.comment} for d in ds]

    def loss_per_lot(self, symbol, side, entry, sl):
        with self.lock:
            t = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL
            v = mt5.order_calc_profit(t, symbol, 1.0, entry, sl)
        if v is None:
            s = self.symbol_info(symbol)
            if not s or not s["trade_tick_size"]:
                return None
            return abs(entry - sl) / s["trade_tick_size"] * s["trade_tick_value"]
        return abs(v)

    def profit_per_lot(self, symbol, side, entry, exit_):
        with self.lock:
            t = mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL
            v = mt5.order_calc_profit(t, symbol, 1.0, entry, exit_)
        return v

    # --- trading --------------------------------------------------------------
    def _filling(self, symbol):
        s = mt5.symbol_info(symbol)
        fm = s.filling_mode if s else 0
        if fm & 1:
            return mt5.ORDER_FILLING_FOK
        if fm & 2:
            return mt5.ORDER_FILLING_IOC
        return mt5.ORDER_FILLING_RETURN

    def _send(self, req):
        with self.lock:
            res = mt5.order_send(req)
            if res is None:
                return {"ok": False, "retcode": -1, "message": str(mt5.last_error())}
            ok = res.retcode in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED, mt5.TRADE_RETCODE_DONE_PARTIAL)
            return {"ok": ok, "retcode": res.retcode, "message": res.comment, "order": res.order,
                    "deal": res.deal, "price": res.price, "volume": res.volume}

    def send_market(self, symbol, side, volume, sl, tp, magic, comment, deviation=50):
        with self.lock:
            mt5.symbol_select(symbol, True)
            t = mt5.symbol_info_tick(symbol)
            price = t.ask if side == "BUY" else t.bid
            req = {"action": mt5.TRADE_ACTION_DEAL, "symbol": symbol, "volume": float(volume),
                   "type": mt5.ORDER_TYPE_BUY if side == "BUY" else mt5.ORDER_TYPE_SELL,
                   "price": price, "sl": float(sl or 0), "tp": float(tp or 0), "deviation": int(deviation),
                   "magic": int(magic), "comment": comment[:31], "type_time": mt5.ORDER_TIME_GTC,
                   "type_filling": self._filling(symbol)}
            return self._send(req)

    def send_pending(self, symbol, side, kind, volume, price, sl, tp, magic, comment):
        types = {("BUY", "LIMIT"): mt5.ORDER_TYPE_BUY_LIMIT, ("SELL", "LIMIT"): mt5.ORDER_TYPE_SELL_LIMIT,
                 ("BUY", "STOP"): mt5.ORDER_TYPE_BUY_STOP, ("SELL", "STOP"): mt5.ORDER_TYPE_SELL_STOP}
        req = {"action": mt5.TRADE_ACTION_PENDING, "symbol": symbol, "volume": float(volume),
               "type": types[(side, kind)], "price": float(price), "sl": float(sl or 0), "tp": float(tp or 0),
               "magic": int(magic), "comment": comment[:31], "type_time": mt5.ORDER_TIME_GTC,
               "type_filling": mt5.ORDER_FILLING_RETURN}
        return self._send(req)

    def modify_position(self, ticket, sl, tp):
        with self.lock:
            p = mt5.positions_get(ticket=int(ticket))
            if not p:
                return {"ok": False, "message": "position not found"}
            req = {"action": mt5.TRADE_ACTION_SLTP, "position": int(ticket), "symbol": p[0].symbol,
                   "sl": float(sl or 0), "tp": float(tp or 0)}
            return self._send(req)

    def close_position(self, ticket, deviation=50, volume=None, comment="close by dashboard"):
        with self.lock:
            p = mt5.positions_get(ticket=int(ticket))
            if not p:
                return {"ok": False, "message": "position not found"}
            p = p[0]
            t = mt5.symbol_info_tick(p.symbol)
            buy = p.type == 0
            vol = p.volume if not volume or volume >= p.volume else float(volume)
            req = {"action": mt5.TRADE_ACTION_DEAL, "symbol": p.symbol, "volume": vol, "position": p.ticket,
                   "type": mt5.ORDER_TYPE_SELL if buy else mt5.ORDER_TYPE_BUY, "price": t.bid if buy else t.ask,
                   "deviation": int(deviation), "magic": p.magic, "comment": comment[:31],
                   "type_time": mt5.ORDER_TIME_GTC, "type_filling": self._filling(p.symbol)}
            return self._send(req)

    def cancel_order(self, ticket):
        return self._send({"action": mt5.TRADE_ACTION_REMOVE, "order": int(ticket)})


# ---------------------------------------------------------------------------
# Demo simulator
# ---------------------------------------------------------------------------
class DemoBroker:
    """Simulated XAUUSD market + account. NOT real data."""
    mode = "demo"
    CONTRACT = 100.0  # oz per lot

    def __init__(self, symbol="XAUUSD", seed=7):
        self.lock = threading.RLock()
        self.symbol = symbol
        self.rng = random.Random(seed)
        self.np_rng = np.random.default_rng(seed)
        self.server_offset = 3 * 3600
        self.spread = 0.25
        self.next_ticket = 5_000_000
        self.positions_ = []
        self.orders_ = []
        self.deals_ = []
        self.last_error = ""
        self._build_history()
        self._build_trade_history()

    # --- clock & price -------------------------------------------------------
    def now(self):
        return int(time.time()) + self.server_offset

    def _ticket(self):
        self.next_ticket += 1
        return self.next_ticket

    def _build_history(self, n=30 * 1440):
        now = self.now()
        start = (now // 60) * 60 - n * 60
        times = start + np.arange(n) * 60
        hours = (times // 3600) % 24
        session_vol = np.where((hours >= 9) & (hours < 22), 1.6, 0.8)
        vol = np.empty(n)
        v = 1.0
        for i in range(n):  # volatility clustering
            v = 0.97 * v + 0.03 * abs(self.np_rng.normal(1, 0.6))
            vol[i] = v
        # gentle regime drift so trends exist
        drift = np.repeat(self.np_rng.normal(0, 0.03, n // 240 + 1), 240)[:n]
        rets = drift + self.np_rng.normal(0, 0.55, n) * vol * session_vol
        close = 3950 + np.cumsum(rets)
        open_ = np.concatenate([[close[0]], close[:-1]])
        wick = np.abs(self.np_rng.normal(0, 0.35, (2, n))) * vol * session_vol
        high = np.maximum(open_, close) + wick[0]
        low = np.minimum(open_, close) - wick[1]
        tv = (self.np_rng.integers(40, 160, n) * session_vol * vol).astype(int)
        self.m1 = pd.DataFrame({"time": times.astype("int64"), "open": open_, "high": high, "low": low,
                                "close": close, "tick_volume": tv})
        self.price = float(close[-1])
        self.last_tick = time.time()

    def _advance(self):
        """Move the simulated price forward to 'now'."""
        t = time.time()
        dt = max(0.0, t - self.last_tick)
        self.last_tick = t
        steps = min(int(dt / 0.5) + 1, 400)
        for _ in range(steps):
            self.price += self.rng.gauss(0, 0.12)
        now = self.now()
        cur = (now // 60) * 60
        last = int(self.m1["time"].iloc[-1])
        if cur > last:
            rows = []
            prev = float(self.m1["close"].iloc[-1])
            for bt in range(last + 60, cur + 60, 60):
                o = prev
                c = self.price if bt == cur else prev + self.rng.gauss(0, 0.5)
                rows.append({"time": bt, "open": o, "high": max(o, c) + abs(self.rng.gauss(0, 0.2)),
                             "low": min(o, c) - abs(self.rng.gauss(0, 0.2)), "close": c,
                             "tick_volume": self.rng.randint(30, 150)})
                prev = c
            self.m1 = pd.concat([self.m1, pd.DataFrame(rows)], ignore_index=True)
            if len(self.m1) > 40 * 1440:
                self.m1 = self.m1.iloc[-35 * 1440:].reset_index(drop=True)
        i = self.m1.index[-1]
        self.m1.at[i, "close"] = self.price
        self.m1.at[i, "high"] = max(self.m1.at[i, "high"], self.price)
        self.m1.at[i, "low"] = min(self.m1.at[i, "low"], self.price)
        self.spread = round(0.18 + abs(self.rng.gauss(0, 0.08)), 2)
        self._process_orders()

    def _bid_ask(self):
        return round(self.price, 2), round(self.price + self.spread, 2)

    # --- fake past trades so the journal / analytics have something to show --
    def _build_trade_history(self):
        df = self.m1
        n = len(df)
        balance_time = int(df["time"].iloc[0]) - 3600
        self.deals_.append({"ticket": self._ticket(), "order": 0, "time": balance_time, "type": 2, "entry": 0,
                            "magic": 0, "position_id": 0, "volume": 0, "price": 0, "commission": 0, "swap": 0,
                            "profit": 10000.0, "fee": 0, "symbol": "", "comment": "Deposit"})
        sources = [(260109, "GoldScalperM1", 0.52, 1.3), (260110, "AIScalperM1 p=0.6", 0.55, 1.0),
                   (0, "", 0.45, 1.6), (777000, None, 0.5, 2.0)]
        strat_codes = ["SMC", "SND", "EMA", "BB", "ASIA", "AIPB"]
        idx = sorted(self.rng.sample(range(500, n - 200), 260))
        for i in idx:
            magic, comment, edge, rr = self.rng.choice(sources)
            if comment is None:
                comment = "AIT:" + self.rng.choice(strat_codes)
            side = self.rng.choice(["BUY", "SELL"])
            vol = self.rng.choice([0.01, 0.02, 0.03, 0.05, 0.1])
            entry = float(df["close"].iloc[i])
            risk = self.rng.uniform(2.0, 6.0)
            win = self.rng.random() < edge
            move = risk * rr * self.rng.uniform(0.6, 1.1) if win else -risk * self.rng.uniform(0.5, 1.0)
            hold = self.rng.randint(3, 120)
            exit_ = entry + move if side == "BUY" else entry - move
            pos = self._ticket()
            t_in = int(df["time"].iloc[i]) + self.rng.randint(0, 59)
            t_out = t_in + hold * 60
            comm = -round(vol * 7, 2)
            profit = round(move * vol * self.CONTRACT, 2)
            self.deals_.append({"ticket": self._ticket(), "order": pos, "time": t_in, "type": 0 if side == "BUY" else 1,
                                "entry": 0, "magic": magic, "position_id": pos, "volume": vol, "price": round(entry, 2),
                                "commission": comm / 2, "swap": 0, "profit": 0, "fee": 0, "symbol": self.symbol,
                                "comment": comment})
            self.deals_.append({"ticket": self._ticket(), "order": pos + 1, "time": t_out,
                                "type": 1 if side == "BUY" else 0, "entry": 1, "magic": magic, "position_id": pos,
                                "volume": vol, "price": round(exit_, 2), "commission": comm / 2, "swap": 0,
                                "profit": profit, "fee": 0, "symbol": self.symbol,
                                "comment": "[tp]" if win else "[sl]"})
        self.deals_.sort(key=lambda d: d["time"])

    # --- interface -----------------------------------------------------------
    def connect(self):
        return True

    def ensure(self):
        return True

    def status(self):
        return {"mode": self.mode, "connected": True, "trade_allowed": True, "terminal": "DEMO SIMULATOR",
                "error": self.last_error}

    def account(self):
        with self.lock:
            self._advance()
            bal = sum(d["profit"] + d["commission"] + d["swap"] + d["fee"] for d in self.deals_)
            fl = sum(p["profit"] + p["swap"] for p in self.positions_)
            margin = sum(p["volume"] * self.CONTRACT * p["price_current"] / 100 for p in self.positions_)
            eq = bal + fl
            return {"login": 12345678, "name": "Demo Account", "server": "DEMO-Simulator", "currency": "USD",
                    "leverage": 100, "balance": round(bal, 2), "equity": round(eq, 2), "credit": 0.0,
                    "profit": round(fl, 2), "margin": round(margin, 2), "margin_free": round(eq - margin, 2),
                    "margin_level": round(eq / margin * 100, 2) if margin else 0.0,
                    "trade_allowed": True, "trade_expert": True, "is_demo": True}

    def find_symbol_like(self, preferred):
        return preferred  # the simulator answers for any name (same price series)

    def symbol_info(self, symbol):
        return {"name": symbol, "digits": 2, "point": 0.01, "volume_min": 0.01, "volume_max": 100.0,
                "volume_step": 0.01, "trade_contract_size": self.CONTRACT, "trade_tick_value": 1.0,
                "trade_tick_size": 0.01, "trade_stops_level": 0, "filling_mode": 1, "trade_mode": 4, "spread": int(self.spread * 100)}

    def tick(self, symbol):
        with self.lock:
            self._advance()
            bid, ask = self._bid_ask()
            return {"time": self.now(), "bid": bid, "ask": ask, "spread": round(ask - bid, 3)}

    def rates(self, symbol, tf, count=500):
        with self.lock:
            self._advance()
            df = self.m1.copy()
        sec = TF_SECONDS[tf]
        if sec > 60:
            df["g"] = (df["time"] // sec) * sec
            df = df.groupby("g").agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
                                     close=("close", "last"), tick_volume=("tick_volume", "sum")).reset_index()
            df = df.rename(columns={"g": "time"})
        df = df[["time", "open", "high", "low", "close", "tick_volume"]].tail(int(count)).reset_index(drop=True)
        for c in ("open", "high", "low", "close"):
            df[c] = df[c].round(2)
        return df

    def rates_range(self, symbol, tf, t_from, t_to):
        with self.lock:
            self._advance()
            df = self.m1[(self.m1["time"] >= t_from) & (self.m1["time"] <= t_to)].copy()
        sec = TF_SECONDS[tf]
        if sec > 60 and len(df):
            df["g"] = (df["time"] // sec) * sec
            df = df.groupby("g").agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
                                     close=("close", "last"), tick_volume=("tick_volume", "sum")).reset_index()
            df = df.rename(columns={"g": "time"})
        return df[["time", "open", "high", "low", "close", "tick_volume"]].reset_index(drop=True)

    def positions(self):
        with self.lock:
            self._advance()
            return [dict(p) for p in self.positions_]

    def orders(self):
        with self.lock:
            self._advance()
            return [dict(o) for o in self.orders_]

    def deals(self, t_from, t_to):
        with self.lock:
            return [dict(d) for d in self.deals_ if t_from <= d["time"] <= t_to]

    def loss_per_lot(self, symbol, side, entry, sl):
        return abs(entry - sl) * self.CONTRACT

    def profit_per_lot(self, symbol, side, entry, exit_):
        return (exit_ - entry) * self.CONTRACT * (1 if side == "BUY" else -1)

    # --- trading -------------------------------------------------------------
    def _open(self, side, volume, price, sl, tp, magic, comment):
        t = self._ticket()
        now = self.now()
        self.positions_.append({"ticket": t, "time": now, "symbol": self.symbol, "side": side, "volume": volume,
                                "price_open": price, "sl": sl or 0.0, "tp": tp or 0.0, "price_current": price,
                                "swap": 0.0, "profit": 0.0, "magic": magic, "comment": comment[:31]})
        self.deals_.append({"ticket": self._ticket(), "order": t, "time": now, "type": 0 if side == "BUY" else 1,
                            "entry": 0, "magic": magic, "position_id": t, "volume": volume, "price": price,
                            "commission": -round(volume * 3.5, 2), "swap": 0, "profit": 0, "fee": 0,
                            "symbol": self.symbol, "comment": comment[:31]})
        return t

    def _close(self, p, price, reason="close", volume=None):
        vol = p["volume"] if not volume or volume >= p["volume"] - 1e-9 else round(float(volume), 2)
        pl = round((price - p["price_open"]) * (1 if p["side"] == "BUY" else -1) * vol * self.CONTRACT, 2)
        self.deals_.append({"ticket": self._ticket(), "order": self._ticket(), "time": self.now(),
                            "type": 1 if p["side"] == "BUY" else 0, "entry": 1, "magic": p["magic"],
                            "position_id": p["ticket"], "volume": vol, "price": price,
                            "commission": -round(vol * 3.5, 2), "swap": 0, "profit": pl, "fee": 0,
                            "symbol": self.symbol, "comment": reason})
        if vol < p["volume"] - 1e-9:
            p["volume"] = round(p["volume"] - vol, 2)
        else:
            self.positions_ = [x for x in self.positions_ if x["ticket"] != p["ticket"]]

    def _process_orders(self):
        bid, ask = self._bid_ask()
        for p in list(self.positions_):
            cur = bid if p["side"] == "BUY" else ask
            p["price_current"] = cur
            p["profit"] = round((cur - p["price_open"]) * (1 if p["side"] == "BUY" else -1) * p["volume"] * self.CONTRACT, 2)
            if p["side"] == "BUY":
                if p["sl"] and bid <= p["sl"]:
                    self._close(p, p["sl"], "[sl]")
                elif p["tp"] and bid >= p["tp"]:
                    self._close(p, p["tp"], "[tp]")
            else:
                if p["sl"] and ask >= p["sl"]:
                    self._close(p, p["sl"], "[sl]")
                elif p["tp"] and ask <= p["tp"]:
                    self._close(p, p["tp"], "[tp]")
        for o in list(self.orders_):
            side, kind = o["type"].split(" ")
            hit = ((side == "BUY" and kind == "LIMIT" and ask <= o["price"]) or
                   (side == "SELL" and kind == "LIMIT" and bid >= o["price"]) or
                   (side == "BUY" and kind == "STOP" and ask >= o["price"]) or
                   (side == "SELL" and kind == "STOP" and bid <= o["price"]))
            if hit:
                self.orders_.remove(o)
                self._open(side, o["volume"], o["price"], o["sl"], o["tp"], o["magic"], o["comment"])

    def send_market(self, symbol, side, volume, sl, tp, magic, comment, deviation=50):
        with self.lock:
            self._advance()
            bid, ask = self._bid_ask()
            price = ask if side == "BUY" else bid
            t = self._open(side, float(volume), price, float(sl or 0), float(tp or 0), int(magic), comment)
            return {"ok": True, "retcode": 10009, "message": "Request executed (demo)", "order": t, "price": price,
                    "volume": volume}

    def send_pending(self, symbol, side, kind, volume, price, sl, tp, magic, comment):
        with self.lock:
            self._advance()
            bid, ask = self._bid_ask()
            ref = ask if side == "BUY" else bid
            if kind == "LIMIT" and ((side == "BUY" and price >= ref) or (side == "SELL" and price <= ref)):
                return {"ok": False, "retcode": 10015, "message": "Invalid price for LIMIT order"}
            if kind == "STOP" and ((side == "BUY" and price <= ref) or (side == "SELL" and price >= ref)):
                return {"ok": False, "retcode": 10015, "message": "Invalid price for STOP order"}
            t = self._ticket()
            self.orders_.append({"ticket": t, "time": self.now(), "symbol": self.symbol, "type": f"{side} {kind}",
                                 "volume": float(volume), "price": float(price), "sl": float(sl or 0),
                                 "tp": float(tp or 0), "magic": int(magic), "comment": comment[:31]})
            return {"ok": True, "retcode": 10008, "message": "Order placed (demo)", "order": t}

    def modify_position(self, ticket, sl, tp):
        with self.lock:
            for p in self.positions_:
                if p["ticket"] == int(ticket):
                    p["sl"], p["tp"] = float(sl or 0), float(tp or 0)
                    return {"ok": True, "message": "modified (demo)"}
            return {"ok": False, "message": "position not found"}

    def close_position(self, ticket, deviation=50, volume=None, comment="close by dashboard"):
        with self.lock:
            self._advance()
            bid, ask = self._bid_ask()
            for p in self.positions_:
                if p["ticket"] == int(ticket):
                    self._close(p, bid if p["side"] == "BUY" else ask, comment, volume)
                    return {"ok": True, "message": "closed (demo)"}
            return {"ok": False, "message": "position not found"}

    def cancel_order(self, ticket):
        with self.lock:
            n = len(self.orders_)
            self.orders_ = [o for o in self.orders_ if o["ticket"] != int(ticket)]
            return {"ok": len(self.orders_) < n, "message": "cancelled (demo)"}


def create_broker(settings):
    """Try the real terminal first; fall back to the simulator."""
    if mt5 is not None:
        b = LiveBroker(settings.get("mt5", {}))
        if b.connect():
            return b
        print("[MT5] cannot connect:", b.last_error, "-> starting DEMO simulator")
    else:
        print("[MT5] MetaTrader5 package not available -> starting DEMO simulator")
    return DemoBroker(settings.get("symbol", "XAUUSD"))
