"""
AI Trader Dashboard - backend server.

    python main.py            -> http://127.0.0.1:8000

Connects to the MT5 terminal that is open on this PC (MetaTrader5 package).
If MT5 is not available it starts a DEMO simulator instead.
"""
import csv
import io
import os
import threading
import time
from typing import Optional

import requests
import uvicorn
from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import analytics
import optimizer
import news
import regime
import market_context as mctx
import trade_manager as tmgr
import store
import strategies as st
from mt5_client import TF_SECONDS, create_broker
from risk import RiskManager

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONT = os.path.join(ROOT, "frontend")
DAY = 86400


def settings():
    return store.load("settings")


broker = create_broker(settings())
if broker.mode == "live":  # make sure the gold symbol name matches this broker
    _want = settings()["symbol"]
    _found = broker.find_symbol(_want)
    if _found and _found != _want:
        store.update("settings", {"symbol": _found})
        print(f"[MT5] symbol '{_want}' not found on this broker -> using '{_found}'")
    elif not _found:
        print(f"[MT5] cannot find a gold symbol - set it in Risk & Settings")
risk = RiskManager(broker, settings)


def enable_autotrade_on_demo():
    """On a DEMO account turn Auto-Trade on for every strategy - unless the user has already
    chosen the Auto-Trade settings themselves. Real accounts are never switched on automatically."""
    acc = broker.account() or {}
    sc = store.load("scanner")
    if acc.get("is_demo") and not sc.get("auto_trade_user_set"):
        sc["enabled"] = True
        sc["auto_trade"] = True
        sc["auto_trade_strategies"] = {k: True for k in sc["strategies"]}
        sc["ai_auto"] = True
        store.save("scanner", sc)
        print("[AUTO] Demo account -> Auto-Trade ON for all strategies")


enable_autotrade_on_demo()
tm = tmgr.TradeManager(broker, settings, risk.log)
app = FastAPI(title="AI Trader Dashboard")

SCAN = {"last_scan": 0, "tf": None, "latest": [], "history": [], "traded": set(), "error": ""}


def sym():
    return settings()["symbol"]


def now_server():
    t = broker.tick(sym())
    return t["time"] if t else int(time.time())


def num(x):
    return None if x in (None, "", 0, "0") else float(x)


# ------------------------------------------------------------------ status / account
@app.get("/api/status")
def status():
    s = settings()
    r = store.load("risk")
    return {**broker.status(), "symbol": s["symbol"], "timeframe": s["timeframe"],
            "kill_switch": r["kill_switch"], "kill_reason": r["kill_switch_reason"],
            "kill_auto": bool(r.get("kill_switch_auto")) and r.get("auto_release_new_day", True),
            "scanner": {"last_scan": SCAN["last_scan"], "error": SCAN["error"]}}


@app.get("/api/account")
def account():
    acc = broker.account()
    if acc is None:
        raise HTTPException(503, "MT5 not connected")
    s = settings()
    today = risk.today()
    trades = journal_trades(days=40)
    per = analytics.period_pnl(trades, today["server_time"])
    goals = s["goals"]
    per_goal = {k: {"pnl": per[{"daily": "today", "weekly": "week", "monthly": "month"}[k]], "goal": goals[k]}
                for k in goals}
    return {"account": acc, "today": today, "goals": per_goal, "tick": broker.tick(sym())}


@app.get("/api/tick")
def tick():
    return broker.tick(sym())


# ------------------------------------------------------------------ market data
@app.get("/api/rates")
def rates(tf: str = "M5", count: int = 500, signals: bool = True):
    if tf not in TF_SECONDS:
        raise HTTPException(400, "bad timeframe")
    df = broker.rates(sym(), tf, min(max(count, 250), 5000))
    if df.empty:
        return {"candles": [], "signals": []}
    sc = store.load("scanner")
    di, res = st.run_all(df, sc["strategies"] if signals else {}, store.load("strategy_params"), settings()["asian_session"])
    cols = ["time", "open", "high", "low", "close", "ema20", "ema50", "ema200", "bb_up", "bb_mid", "bb_lo"]
    out = di[cols].round(3).astype(object).where(di[cols].notna(), None)
    sigs = sorted([x for v in res.values() for x in v], key=lambda x: x["time"])
    return {"candles": out.to_dict("records"), "signals": sigs, "tf": tf}


# ------------------------------------------------------------------ trading
@app.get("/api/positions")
def positions():
    m = store.load("ea_map")
    ps = broker.positions()
    for p in ps:
        p["source"] = analytics.source_of(p["magic"], m)
    return ps


@app.get("/api/orders")
def orders():
    return broker.orders()


@app.post("/api/order")
def place_order(body: dict = Body(...)):
    side = body.get("side", "").upper()
    kind = body.get("kind", "MARKET").upper()
    if side not in ("BUY", "SELL") or kind not in ("MARKET", "LIMIT", "STOP"):
        raise HTTPException(400, "side must be BUY/SELL and kind MARKET/LIMIT/STOP")
    vol = float(body.get("volume") or 0)
    sl, tp = num(body.get("sl")), num(body.get("tp"))
    tk = broker.tick(sym())
    if not tk:
        raise HTTPException(503, {"violations": [f"ไม่มีราคา {sym()} จาก MT5 – ตรวจชื่อ Symbol / ตลาดปิด"]})
    entry = (tk["ask"] if side == "BUY" else tk["bid"]) if kind == "MARKET" else float(body.get("price") or 0)
    if kind != "MARKET" and not entry:
        raise HTTPException(400, "pending order needs price")
    violations = risk.check(sym(), side, vol, entry, sl, tp, market=(kind == "MARKET"))
    nb = news.blackout(manual=(body.get("strategy") or "MANUAL").upper() not in ("AI",) and not body.get("_auto"))
    if nb:
        violations.append(nb)
    if violations:
        risk.log("ปฏิเสธออเดอร์: " + "; ".join(violations), "warn")
        raise HTTPException(422, {"violations": violations})
    s = settings()
    comment = "AIT:" + (body.get("strategy") or "MANUAL").upper()
    if kind == "MARKET":
        r = broker.send_market(sym(), side, vol, sl, tp, s["dashboard_magic"], comment, s["deviation_points"])
    else:
        r = broker.send_pending(sym(), side, kind, vol, entry, sl, tp, s["dashboard_magic"], comment)
    risk.log(f"{kind} {side} {vol} @ {entry} SL {sl} TP {tp} -> {r.get('message')}", "info" if r.get("ok") else "warn")
    if not r.get("ok"):
        raise HTTPException(400, {"violations": [f"MT5: {r.get('retcode')} {r.get('message')}"]})
    return r


@app.post("/api/position/{ticket}/close")
def close_pos(ticket: int):
    return broker.close_position(ticket, settings()["deviation_points"])


@app.post("/api/position/{ticket}/modify")
def modify_pos(ticket: int, body: dict = Body(...)):
    return broker.modify_position(ticket, num(body.get("sl")), num(body.get("tp")))


@app.delete("/api/order/{ticket}")
def cancel(ticket: int):
    return broker.cancel_order(ticket)


@app.post("/api/close_all")
def close_all():
    return risk.close_everything()


@app.post("/api/calc")
def calc(body: dict = Body(...)):
    side = body.get("side", "BUY").upper()
    entry = num(body.get("entry"))
    if not entry:
        tk = broker.tick(sym())
        entry = tk["ask"] if side == "BUY" else tk["bid"]
    lot = num(body.get("lot"))
    return risk.calc(sym(), side, entry, num(body.get("sl")), num(body.get("tp")), num(body.get("risk_pct")), lot)


# ------------------------------------------------------------------ journal
def journal_trades(days=365):
    now = now_server()
    deals = broker.deals(now - days * DAY, now + 3600)
    return analytics.build_journal(deals, store.load("ea_map"), store.load("notes"))


def filter_trades(trades, date_from=None, date_to=None, source=None, strategy=None, side=None, result=None,
                  hour_from=None, hour_to=None):
    out = []
    for t in trades:
        ts = t["open_time"]
        if date_from and ts < date_from:
            continue
        if date_to and ts > date_to:
            continue
        if source and t["source"] != source:
            continue
        if strategy and t["strategy"] != strategy:
            continue
        if side and t["side"] != side:
            continue
        if result == "win" and not (t["status"] == "closed" and t["profit"] > 0):
            continue
        if result == "loss" and not (t["status"] == "closed" and t["profit"] <= 0):
            continue
        if result == "open" and t["status"] != "open":
            continue
        h = (ts // 3600) % 24
        if hour_from is not None and hour_to is not None:
            if hour_from <= hour_to and not (hour_from <= h <= hour_to):
                continue
            if hour_from > hour_to and not (h >= hour_from or h <= hour_to):
                continue
        out.append(t)
    return out


@app.get("/api/journal")
def journal(days: int = 365, date_from: Optional[int] = None, date_to: Optional[int] = None,
            source: Optional[str] = None, strategy: Optional[str] = None, side: Optional[str] = None,
            result: Optional[str] = None, hour_from: Optional[int] = None, hour_to: Optional[int] = None):
    all_t = journal_trades(days)
    t = filter_trades(all_t, date_from, date_to, source, strategy, side, result, hour_from, hour_to)
    return {"trades": t, "summary": analytics.summarize(t),
            "sources": sorted({x["source"] for x in all_t}), "strategies": sorted({x["strategy"] for x in all_t})}


@app.get("/api/journal.csv")
def journal_csv(days: int = 365, date_from: Optional[int] = None, date_to: Optional[int] = None,
                source: Optional[str] = None, strategy: Optional[str] = None, side: Optional[str] = None,
                result: Optional[str] = None, hour_from: Optional[int] = None, hour_to: Optional[int] = None):
    import datetime as dt
    t = filter_trades(journal_trades(days), date_from, date_to, source, strategy, side, result, hour_from, hour_to)
    cols = ["position_id", "symbol", "side", "volume", "open_time", "open_price", "close_time", "close_price",
            "points", "profit", "commission", "swap", "source", "strategy", "magic", "exit_reason",
            "duration_min", "status", "comment", "note"]
    buf = io.StringIO()
    buf.write("﻿")  # Excel-friendly UTF-8 (Thai notes)
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    fmt = lambda x: dt.datetime.fromtimestamp(x, tz=dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if x else ""
    for r in t:
        w.writerow({**r, "open_time": fmt(r["open_time"]), "close_time": fmt(r["close_time"])})
    buf.seek(0)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=trading_journal.csv"})


@app.post("/api/journal/{pid}/note")
def set_note(pid: int, body: dict = Body(...)):
    notes = store.load("notes")
    notes[str(pid)] = body.get("note", "")
    store.save("notes", notes)
    return {"ok": True}


# ------------------------------------------------------------------ analytics
@app.get("/api/analytics")
def get_analytics(days: int = 90, source: Optional[str] = None):
    now = now_server()
    deals = broker.deals(now - days * DAY, now + 3600)
    trades = analytics.build_journal(deals, store.load("ea_map"))
    if source:
        trades = [t for t in trades if t["source"] == source]
    return analytics.full_report(trades, deals)


@app.get("/api/backtest")
def backtest(tf: str = "M5", count: int = 3000, cost: Optional[float] = None):
    df = broker.rates(sym(), tf, min(count, 20000))
    sc = store.load("scanner")
    di, res = st.run_all(df, {k: True for k in st.STRATEGIES}, store.load("strategy_params"), settings()["asian_session"])
    out = {}
    for code, sigs in res.items():
        b = st.backtest(di, sigs, cost=default_cost() if cost is None else cost)
        b["name"] = st.STRATEGIES[code]
        b["trades"] = b["trades"][-50:]
        out[code] = b
    return {"tf": tf, "bars": len(di), "from": int(di["time"].iat[0]), "to": int(di["time"].iat[-1]), "results": out}


def default_cost():
    """Round-trip trading cost in price units = current spread (+ a little slippage)."""
    tk = broker.tick(sym())
    return round((tk["spread"] if tk else 0.3) + 0.05, 3)


# ------------------------------------------------------------------ scanner / signals
@app.get("/api/signals")
def signals():
    return {"last_scan": SCAN["last_scan"], "tf": SCAN["tf"], "latest": SCAN["latest"],
            "history": SCAN["history"][:100], "strategies": st.STRATEGIES, "error": SCAN["error"]}


def run_scan():
    s, sc = settings(), store.load("scanner")
    tf = s["timeframe"]
    df = broker.rates(s["symbol"], tf, 800)
    if len(df) < 300:
        SCAN["error"] = "not enough bars"
        return
    di, res = st.run_all(df, sc["strategies"], store.load("strategy_params"), s["asian_session"])
    last_closed = len(di) - 2
    latest = []
    for code, sigs in res.items():
        for x in sigs:
            if x["index"] >= last_closed - 1:  # fresh: last 2 closed bars
                x = {**x, "name": st.STRATEGIES[code], "tf": tf}
                latest.append(x)
                key = f"{code}-{x['time']}-{x['side']}"
                if not any(h.get("key") == key for h in SCAN["history"][:50]):
                    SCAN["history"].insert(0, {**x, "key": key, "seen": int(time.time())})
                    risk.log(f"สัญญาณใหม่ {code} {x['side']} @ {x['entry']}")
                    maybe_autotrade(x, key, sc)
    del SCAN["history"][300:]
    SCAN.update(latest=latest, last_scan=int(time.time()), tf=tf, error="")


AUTO_LOG = []


def _alog(sig, result, detail=""):
    AUTO_LOG.insert(0, {"time": int(time.time()), "strategy": sig["strategy"], "side": sig["side"],
                        "entry": sig["entry"], "result": result, "detail": detail})
    del AUTO_LOG[100:]


def maybe_autotrade(sig, key, sc):
    if not sc["auto_trade"]:
        return _alog(sig, "skip", "Auto-Trade ปิดอยู่")
    if not sc["auto_trade_strategies"].get(sig["strategy"]):
        return _alog(sig, "skip", f"ไม่ได้ติ๊ก Auto-trade ของ {sig['strategy']}")
    if key in SCAN["traded"]:
        return
    SCAN["traded"].add(key)
    nb = news.blackout()
    if nb:
        risk.log(f"Auto-trade {sig['strategy']} พักเพราะข่าว: {nb}", "warn")
        return _alog(sig, "blocked", nb)
    if sc.get("regime_filter", True):
        rg = market_regime()
        if sig["strategy"] not in rg.get("fit_strategies", []):
            return _alog(sig, "skip", f"ตลาดตอนนี้ {rg['label']} – {sig['strategy']} ไม่เหมาะ (เหมาะ: {', '.join(rg.get('fit_strategies', [])) or '-'})")
    tk = broker.tick(sym())
    entry = tk["ask"] if sig["side"] == "BUY" else tk["bid"]
    # keep the planned SL/TP distances relative to the live price
    sl = entry - (sig["entry"] - sig["sl"])
    tp = entry + (sig["tp"] - sig["entry"])
    c = risk.calc(sym(), sig["side"], entry, sl, tp)
    lot = c.get("lot") or 0
    v = risk.check(sym(), sig["side"], lot, entry, sl, tp, True)
    if v:
        risk.log(f"Auto-trade {sig['strategy']} ถูกบล็อก: " + "; ".join(v), "warn")
        return _alog(sig, "blocked", "; ".join(v))
    s = settings()
    r = broker.send_market(sym(), sig["side"], lot, round(sl, 2), round(tp, 2), s["dashboard_magic"],
                           "AIT:" + sig["strategy"], s["deviation_points"])
    risk.log(f"Auto-trade {sig['strategy']} {sig['side']} {lot} -> {r.get('message')}",
             "info" if r.get("ok") else "warn")
    _alog(sig, "sent" if r.get("ok") else "rejected", f"{lot} lot · MT5 {r.get('retcode')} {r.get('message')}")


def background():
    last_scan = 0
    while True:
        try:
            broker.ensure()
            risk.enforce()
            try:
                tm.run()
            except Exception as e:  # never let trade management kill the loop
                print("[TM]", repr(e))
            sc = store.load("scanner")
            if sc["enabled"] and time.time() - last_scan >= sc["interval_sec"]:
                last_scan = time.time()
                run_scan()
            try:
                ai_autotrade_tick()
            except Exception as e:
                print("[AI]", repr(e))
        except Exception as e:  # keep the loop alive
            SCAN["error"] = str(e)
            print("[BG]", repr(e))
        time.sleep(2)


# ------------------------------------------------------------------ news / economic calendar
@app.get("/api/news")
def get_news(refresh: bool = False):
    if refresh:
        news.refresh(force=True)
        news.refresh_headlines(force=True)
    off = risk.offset or 0
    ev = news.upcoming(hours_back=6, hours_ahead=24 * 7, all_impacts=True)
    for e in ev:
        e["time_server"] = e["time_utc"] + off
        e["reading"] = news.surprise(e)
    return {"config": news.config(), "events": ev, "fetched_at": news.STATE["fetched_at"], "error": news.STATE["error"],
            "blackout": news.blackout(), "next_block": news.next_block(), "headlines": news.HEAD["items"],
            "headline_error": news.HEAD["error"], "source": news.URLS["this"],
            "bias": news.news_bias()}


@app.post("/api/news/config")
def set_news(body: dict = Body(...)):
    cfg = {**news.config(), **{k: v for k, v in body.items() if k in news.DEFAULTS or k == "headlines"}}
    store.save("news", cfg)
    risk.log("อัปเดตการตั้งค่าข่าว")
    return cfg


# ------------------------------------------------------------------ diagnostics: why no trades?
@app.get("/api/diagnostics")
def diagnostics():
    st_ = broker.status()
    acc = broker.account() or {}
    sc = store.load("scanner")
    rc = store.load("risk")
    info = broker.symbol_info(sym()) or {}
    tk = broker.tick(sym())
    t = risk.today()
    checks = []

    def add(ok, name, detail, fix=""):
        checks.append({"ok": bool(ok), "name": name, "detail": detail, "fix": fix})

    add(st_["connected"] and broker.mode == "live", "เชื่อม MT5 จริง",
        "LIVE" if broker.mode == "live" else "DEMO SIMULATOR (ไม่ได้ต่อ MT5 – ออเดอร์จะไม่เข้า MT5)",
        "เปิด MT5 ล็อกอินไว้ แล้วรัน start.bat ใหม่")
    add(st_.get("trade_allowed"), "ปุ่ม Algo Trading ใน MT5", "เปิด" if st_.get("trade_allowed") else "ปิดอยู่",
        "กดปุ่ม Algo Trading บนแถบเครื่องมือ MT5 ให้เป็นสีเขียว")
    add(acc.get("trade_allowed", True) and acc.get("trade_expert", True), "บัญชีอนุญาตให้เทรดอัตโนมัติ",
        f"trade_allowed={acc.get('trade_allowed')} · expert={acc.get('trade_expert')}", "ติดต่อโบรก / ล็อกอินด้วยรหัสเทรด (ไม่ใช่ investor password)")
    add(info.get("trade_mode", 4) == 4, f"Symbol {sym()} เปิดให้เทรด", f"trade_mode={info.get('trade_mode')}",
        "ตรวจชื่อ Symbol ในหน้า Settings / ตลาดอาจปิด")
    add(tk is not None, "มีราคาล่าสุด", f"bid {tk['bid']} · spread {tk['spread']}" if tk else "ไม่มีราคา", "ตรวจชื่อ Symbol")
    add(not rc["kill_switch"], "Kill Switch", "ปิด" if not rc["kill_switch"] else f"เปิดอยู่: {rc['kill_switch_reason']}",
        "กด RESUME ที่หน้า Risk")
    add(sc["enabled"], "Scanner", f"ทำงาน · สแกนล่าสุด {int(time.time() - SCAN['last_scan'])} วินาทีก่อน" if sc["enabled"] and SCAN["last_scan"] else ("ปิดอยู่" if not sc["enabled"] else "ยังไม่สแกน"),
        "เปิด Scanner ที่หน้า Risk & Settings")
    add(sc["auto_trade"], "Auto-Trade (สวิตช์หลัก)", "เปิด" if sc["auto_trade"] else "ปิดอยู่", "กดปุ่ม 'เปิด Auto-Trade ทุกกลยุทธ์' ด้านบน")
    on = [k for k, v in sc["auto_trade_strategies"].items() if v and sc["strategies"].get(k)]
    add(bool(on), "กลยุทธ์ที่ Auto-trade", ", ".join(on) if on else "ไม่มีกลยุทธ์ที่ติ๊กทั้งสแกนและ Auto-trade", "ติ๊กในตาราง Scanner & Auto-Trade")
    if tk and rc["max_spread"]:
        add(tk["spread"] <= rc["max_spread"], "Spread", f"{tk['spread']} (สูงสุด {rc['max_spread']})", "เพิ่ม Spread สูงสุดในกฎความเสี่ยง")
    add(sc.get("ai_auto"), "AI Auto-Trade", f"เปิด · ทุก {sc.get('ai_interval_min', 15)} นาที · ความมั่นใจ ≥ {sc.get('ai_min_conf', 65)}"
        + ("" if ai_has_key() else " · ไม่มี API Key → ใช้แผนจากกฎ") if sc.get("ai_auto") else "ปิดอยู่",
        "เปิดที่การ์ด AI Market Analysis หน้า AI & Analytics")
    nb = news.blackout()
    add(not nb, "ช่วงข่าว (News filter)", nb or ("ไม่มีข่าวแรงตอนนี้" + (f" · ข่าวถัดไป {news.next_block()['title']} อีก {news.next_block()['minutes_until']} นาที" if news.next_block() else "")),
        "รอให้พ้นช่วงข่าว หรือปรับเวลาพักก่อน/หลังข่าวในการ์ดปฏิทินข่าว")
    add(not news.STATE["error"] or bool(news.STATE["events"]), "ปฏิทินข่าว", news.STATE["error"] or f"{len(news.STATE['events'])} รายการ",
        "ตรวจอินเทอร์เน็ต / ไฟร์วอลล์ ให้เข้า nfs.faireconomy.media ได้")
    add(not t["loss_hit"], "ลิมิตขาดทุนวันนี้", f"P/L {t['day_pl']} / -{t['loss_limit']}", "รอวันใหม่ หรือปรับลิมิต")
    add(not rc["max_orders_per_day"] or t["orders_today"] < rc["max_orders_per_day"], "จำนวนออเดอร์วันนี้",
        f"{t['orders_today']} / {rc['max_orders_per_day']}", "เพิ่มจำนวนออเดอร์ต่อวัน")
    add(not rc["max_open_positions"] or t["open_positions"] < rc["max_open_positions"], "Position ที่เปิดอยู่",
        f"{t['open_positions']} / {rc['max_open_positions']}", "รอให้ออเดอร์ปิด หรือเพิ่มจำนวนสูงสุด")
    return {"checks": checks, "ready": all(c["ok"] for c in checks), "is_demo": acc.get("is_demo"),
            "signals_seen": len(SCAN["history"]), "auto_log": AUTO_LOG[:30], "scan_error": SCAN["error"]}


@app.post("/api/autotrade/enable_all")
def autotrade_enable_all():
    sc = store.load("scanner")
    sc.update(enabled=True, auto_trade=True, auto_trade_user_set=True, ai_auto=True,
              strategies={k: True for k in st.STRATEGIES}, auto_trade_strategies={k: True for k in st.STRATEGIES})
    store.save("scanner", sc)
    risk.log("เปิด Auto-Trade ทุกกลยุทธ์")
    return sc


@app.post("/api/diagnostics/test_order")
def test_order():
    """Send BUY 0.01 with SL/TP and close it 2 s later - DEMO accounts only."""
    acc = broker.account() or {}
    if not acc.get("is_demo"):
        raise HTTPException(403, {"violations": ["ทดสอบส่งออเดอร์ได้เฉพาะบัญชี Demo"]})
    tk = broker.tick(sym())
    if not tk:
        raise HTTPException(503, {"violations": ["ไม่มีราคา"]})
    s = settings()
    lot = risk.normalize_lot(sym(), store.load("risk").get("default_lot", 0.01))
    r = broker.send_market(sym(), "BUY", lot, round(tk["ask"] - 10, 2), round(tk["ask"] + 20, 2),
                           s["dashboard_magic"], "AIT:TEST", s["deviation_points"])
    out = {"open": r, "mode": broker.mode}
    if r.get("ok"):
        time.sleep(2)
        pos = [p for p in broker.positions() if p["comment"].startswith("AIT:TEST")]
        out["close"] = broker.close_position(pos[0]["ticket"]) if pos else {"ok": False, "message": "ไม่พบ position"}
    risk.log(f"ทดสอบส่งออเดอร์: {r.get('retcode')} {r.get('message')}", "info" if r.get("ok") else "warn")
    return out


# ------------------------------------------------------------------ trade manager (BE / trailing)
TM_KEYS = set(tmgr.DEFAULTS)


@app.get("/api/trade_manager")
def tm_get():
    return {"config": tmgr.config(), "presets": tmgr.PRESETS,
            "sources": sorted(set(store.load("ea_map").values()) | {"Manual", "Dashboard"})}


@app.post("/api/trade_manager")
def tm_set(body: dict = Body(...)):
    patch = {k: v for k, v in body.items() if k in TM_KEYS}
    cfg = {**tmgr.config(), **patch}
    store.save("trade_manager", cfg)
    risk.log("อัปเดต Trade Manager: " + ", ".join(f"{k}={v}" for k, v in patch.items()))
    return cfg


@app.post("/api/trade_manager/preset")
def tm_preset(body: dict = Body(...)):
    p = tmgr.PRESETS.get(body.get("key"))
    if not p:
        raise HTTPException(404, "unknown preset")
    cfg = {**tmgr.config(), **{k: v for k, v in p.items() if k in TM_KEYS}}
    store.save("trade_manager", cfg)
    risk.log(f"Trade Manager ใช้ชุด: {p['label']}")
    return cfg


@app.get("/api/giveback")
def giveback(days: int = 30, source: Optional[str] = None):
    trades = journal_trades(days)
    if source:
        trades = [t for t in trades if t["source"] == source]
    info = broker.symbol_info(sym()) or {}
    cfg = tmgr.config()
    return tmgr.give_back(broker, sym(), trades, info.get("trade_contract_size") or 100.0, cfg["atr_tf"], cfg)


# ------------------------------------------------------------------ adaptive optimizer
@app.post("/api/optimize/run")
def optimize_run(body: dict = Body(default={})):
    tf = body.get("tf") or settings()["timeframe"]
    if tf not in TF_SECONDS:
        raise HTTPException(400, "bad timeframe")
    cost = body.get("cost")
    ok = optimizer.start(broker, sym(), tf=tf, bars=int(body.get("bars") or 12000),
                         cost=default_cost() if cost in (None, "") else float(cost),
                         max_combos=int(body.get("max_combos") or 40), asian=settings()["asian_session"],
                         live_trades=journal_trades(days=180))
    if not ok:
        raise HTTPException(409, "Optimizer กำลังทำงานอยู่")
    return {"started": True}


@app.get("/api/optimize")
def optimize_status():
    return optimizer.status()


@app.post("/api/optimize/apply")
def optimize_apply(body: dict = Body(...)):
    try:
        applied = optimizer.apply(body.get("ids", []))
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    for a in applied:
        risk.log(f"Optimizer: {a['title']}")
    return {"applied": applied}


@app.post("/api/optimize/rollback")
def optimize_rollback(body: dict = Body(...)):
    try:
        e = optimizer.rollback(body.get("id"))
    except RuntimeError as err:
        raise HTTPException(400, str(err))
    risk.log(f"Optimizer: ย้อนกลับ {e['title']}")
    return e


@app.get("/api/strategy_params")
def strategy_params():
    saved = store.load("strategy_params")
    return {c: {"name": st.STRATEGIES[c], "params": st.merged_params(c, saved.get(c)), "custom": c in saved}
            for c in st.STRATEGIES}


@app.post("/api/strategy_params/{code}/reset")
def strategy_params_reset(code: str):
    if code not in st.STRATEGIES:
        raise HTTPException(404, "unknown strategy")
    risk.log(f"รีเซ็ตพารามิเตอร์ {code} เป็นค่าเริ่มต้น")
    return optimizer.reset_params(code)


# ------------------------------------------------------------------ risk & settings
@app.get("/api/risk")
def get_risk():
    return {"config": store.load("risk"), "today": risk.today(), "events": risk.events[:30]}


@app.post("/api/risk")
def set_risk(body: dict = Body(...)):
    allowed = {k: v for k, v in body.items() if k in store.DEFAULTS["risk"] and not k.startswith("kill_switch")}
    return store.update("risk", allowed)


@app.post("/api/kill")
def kill(body: dict = Body(default={})):
    return {"closed": risk.kill(body.get("reason", "manual"))}


@app.post("/api/kill/release")
def release():
    risk.release()
    return {"ok": True}


@app.get("/api/settings")
def get_settings():
    s = settings()
    s = {**s, "ai": {**s["ai"], "anthropic_api_key": "****" if s["ai"]["anthropic_api_key"] else "",
                     "openai_api_key": "****" if s["ai"].get("openai_api_key") else ""}}
    return {"settings": s, "ea_map": store.load("ea_map"), "scanner": store.load("scanner"),
            "strategies": st.STRATEGIES}


@app.post("/api/settings")
def set_settings(body: dict = Body(...)):
    if "settings" in body:
        patch = body["settings"]
        for k in ("anthropic_api_key", "openai_api_key"):
            if patch.get("ai", {}).get(k) == "****":
                patch["ai"].pop(k)
        store.update("settings", patch)
    if "ea_map" in body:
        store.save("ea_map", {str(k): v for k, v in body["ea_map"].items() if v})
    if "scanner" in body:
        store.update("scanner", {**body["scanner"], "auto_trade_user_set": True})
    return get_settings()


# ------------------------------------------------------------------ AI
def market_context(tf=None):
    s = settings()
    tf = tf or s["timeframe"]
    now = risk.server_now()
    trades = journal_trades(days=14)
    closed = [t for t in trades if t["status"] == "closed"][:10]
    opt = (store.load("optimizer").get("last_result") or {}).get("strategies", {})
    extras = {
        "signals": SCAN["latest"],
        "risk_today": risk.today(),
        "risk_config": {k: v for k, v in store.load("risk").items() if not k.startswith("kill_switch_")},
        "open_positions": [{k: p[k] for k in ("side", "volume", "price_open", "sl", "tp", "profit", "source")}
                           for p in positions()],
        "recent_trades": [{"side": t["side"], "profit": t["profit"], "source": t["source"], "strategy": t["strategy"],
                           "exit": t["exit_reason"], "open": t["open_price"], "close": t["close_price"]} for t in closed],
        "strategy_edge_oos": {k: {"exp_r": v["current_oos"]["exp_r"], "trades": v["current_oos"]["trades"],
                                  "windows_positive": f"{v['current_windows_positive']}/{v['n_windows']}"}
                              for k, v in opt.items()} or "ยังไม่ได้รัน Optimizer",
        "trader_notes": s["ai"].get("news_note") or "",
        "economic_calendar_24h": [{"time_server": e["time_utc"] + (risk.offset or 0), "minutes_until": e["minutes_until"],
                                   "currency": e["currency"], "impact": e["impact"], "title": e["title"],
                                   "forecast": e["forecast"], "previous": e["previous"], "actual": e["actual"]}
                                  for e in news.upcoming(hours_back=3, hours_ahead=24)] or "ไม่มีข่าวสำคัญใน 24 ชม. (หรือดึงข้อมูลไม่ได้)",
        "news_blackout_now": news.blackout() or "ไม่มี",
        "news_results_2h": [{"title": e["title"], "impact": e["impact"], "actual": e["actual"], "forecast": e["forecast"],
                             "previous": e["previous"], "minutes_ago": e["minutes_ago"], "reading": e["surprise"]}
                            for e in news.recent_results(minutes=120)] or "ไม่มีข่าวที่ประกาศผลใน 2 ชม.",
        "news_bias": news.news_bias() or "ไม่มี",
        "market_regime": market_regime(tf),
        "headlines": [h["title"] for h in news.HEAD["items"][:10]],
    }
    extras["dollar_index"] = dollar_context(s["symbol"])
    return mctx.build(broker, s["symbol"], tf, now, risk.offset or 0, s["asian_session"], extras)


_REG = {"t": 0, "tf": None, "v": None}


def market_regime(tf=None):
    tf = tf or settings()["timeframe"]
    if time.time() - _REG["t"] > 20 or _REG["tf"] != tf:
        df = broker.rates(sym(), tf, 600)
        h1 = broker.rates(sym(), "H1", 400)
        v = regime.classify(st.add_indicators(df)) if len(df) else {"regime": "RANGE", "label": "-"}
        v["H1"] = regime.classify(st.add_indicators(h1)) if len(h1) else None
        _REG.update(t=time.time(), tf=tf, v=v)
    return _REG["v"]


@app.get("/api/regime")
def get_regime(tf: Optional[str] = None):
    return market_regime(tf)


_DXY = {"name": None, "checked": False}


def dollar_context(gold):
    """DXY trend + correlation with gold on H1, when the broker provides a dollar index symbol."""
    if not hasattr(broker, "find_dollar_index"):
        return "ไม่มี (โหมดจำลอง)"
    if not _DXY["checked"]:
        _DXY.update(name=broker.find_dollar_index(), checked=True)
    if not _DXY["name"]:
        return "โบรกนี้ไม่มี Symbol DXY/USDX"
    d = broker.rates(_DXY["name"], "H1", 220)
    g = broker.rates(gold, "H1", 220)
    if len(d) < 60 or len(g) < 60:
        return "ข้อมูล DXY ไม่พอ"
    di = st.add_indicators(d).iloc[-2]
    j = d[["time", "close"]].merge(g[["time", "close"]], on="time", suffixes=("_dxy", "_gold"))
    corr = j["close_dxy"].pct_change().corr(j["close_gold"].pct_change())
    return {"symbol": _DXY["name"], "close": round(float(di.close), 3), "trend_h1": "UP" if di.ema50 > di.ema200 else "DOWN",
            "rsi_h1": round(float(di.rsi), 1), "change_24h_pct": round(float((d["close"].iat[-1] / d["close"].iat[-25] - 1) * 100), 2),
            "corr_with_gold_h1": round(float(corr), 2) if corr == corr else None}


@app.get("/api/ai/context")
def ai_context(tf: Optional[str] = None):
    return market_context(tf)


AI_STATE = {"last_run": 0, "last": None}

PLAN_SPEC = ('รูปแบบคำตอบ (สำคัญ):\n'
             '1) เริ่มคำตอบทันทีด้วยแผนเทรดเป็น JSON ใน ```json ... ``` (ห้ามมีข้อความก่อนหน้า) รูปแบบ:\n'
             '{"action": "BUY|SELL|WAIT", "order_type": "MARKET|LIMIT|STOP", "entry": ราคา หรือ null ถ้า MARKET, '
             '"sl": ราคา, "tp": ราคา, "confidence": 0-100, "reason": "เหตุผลสั้น ๆ 1 ประโยค"}\n'
             '2) จากนั้นสรุปบทวิเคราะห์สั้น ๆ ไม่เกิน 12 บรรทัด (Bias, ตำแหน่งราคา/แนวสำคัญ, สภาวะตลาด+ข่าว, แผนและเงื่อนไขยกเลิก)\n'
             'ถ้าไม่มีจังหวะที่ชัดเจนให้ action = "WAIT" แต่ถ้าเป็น WAIT ให้บอกใน reason ว่ารอจุดไหน/เงื่อนไขอะไร · '
             'SL ต้องอยู่ฝั่งที่ถูกและห่างจากราคาอย่างน้อย 0.5×ATR · R:R อย่างน้อย 1:1.5')


def _parse_plan(text):
    import json as _json
    import re
    blocks = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S) + re.findall(r"(\{[^{}]*\"action\"[^{}]*\})", text, re.S)
    for b in blocks:
        if '"action"' not in b:
            continue
        try:
            return validate_plan(_json.loads(b))
        except Exception:
            continue
    return None


def validate_plan(p):
    """Normalise a plan dict; returns None-safe dict with 'valid' and 'problem'."""
    out = {"action": str(p.get("action", "WAIT")).upper(), "order_type": str(p.get("order_type") or "MARKET").upper(),
           "entry": num(p.get("entry")), "sl": num(p.get("sl")), "tp": num(p.get("tp")),
           "confidence": float(p.get("confidence") or 0), "reason": str(p.get("reason") or "")[:300],
           "source": p.get("source", "claude"), "valid": False, "problem": ""}
    if out["action"] not in ("BUY", "SELL"):
        out["action"] = "WAIT"
        return out
    if out["order_type"] not in ("MARKET", "LIMIT", "STOP"):
        out["order_type"] = "MARKET"
    tk = broker.tick(sym())
    if not tk:
        out["problem"] = "ไม่มีราคา"
        return out
    buy = out["action"] == "BUY"
    ref = out["entry"] if out["order_type"] != "MARKET" and out["entry"] else (tk["ask"] if buy else tk["bid"])
    if out["order_type"] != "MARKET" and not out["entry"]:
        out["order_type"] = "MARKET"
    # pending type must match where the price is - fix obvious mix-ups
    if out["order_type"] != "MARKET":
        cur = tk["ask"] if buy else tk["bid"]
        out["order_type"] = ("LIMIT" if ref < cur else "STOP") if buy else ("LIMIT" if ref > cur else "STOP")
    if not out["sl"] or (buy and out["sl"] >= ref) or (not buy and out["sl"] <= ref):
        out["problem"] = "SL ไม่ถูกต้อง"
        return out
    if out["tp"] and ((buy and out["tp"] <= ref) or (not buy and out["tp"] >= ref)):
        out["tp"] = None
    out["entry_ref"] = round(ref, 2)
    out["rr"] = round(abs(out["tp"] - ref) / abs(ref - out["sl"]), 2) if out["tp"] else None
    out["valid"] = True
    return out


def rule_plan(ctx):
    """Plan from rules (used when there is no API key): news reaction -> range edges -> trend pullback."""
    tf = ctx["timeframe"]
    a, h = ctx[tf], ctx.get("H1", ctx[tf])
    rg = ctx.get("market_regime") or {}
    reg = rg.get("regime", "")
    px = a["close"]
    candles = ctx.get(f"last_candles_{tf}") or []
    # 1) trade the news result once price confirms the direction
    nb = ctx.get("news_bias")
    if isinstance(nb, dict) and nb["bias"] in ("BUY", "SELL") and len(candles) >= 2:
        up_bars = sum(1 for c in candles[-3:] if c[3] > c[0])
        confirm = (px > a["ema20"] and up_bars >= 2) if nb["bias"] == "BUY" else (px < a["ema20"] and up_bars <= 1)
        if confirm:
            k = 1 if nb["bias"] == "BUY" else -1
            risk_ = 1.5 * a["atr"]
            return {"action": nb["bias"], "order_type": "MARKET", "sl": round(px - k * risk_, 2), "tp": round(px + k * 2 * risk_, 2),
                    "confidence": min(90, 65 + 8 * abs(nb["score"])), "source": "rules",
                    "reason": "เล่นตามผลข่าว + ราคายืนยันทิศ: " + " | ".join(nb["events"][:2])}
    # 2) range: fade the H1 support / resistance zones
    zones = ctx.get("sr_zones_h1") or {}
    if reg == "RANGE" and zones:
        sup = (zones.get("support") or [None])[0]
        res = (zones.get("resistance") or [None])[0]
        if sup and abs(sup["distance_atr"] or 9) <= 0.4 and a["rsi"] < 45 and res:
            sl = round(sup["zone"][0] - 0.5 * a["atr"], 2)
            return {"action": "BUY", "order_type": "MARKET", "sl": sl, "tp": round(res["zone"][0], 2), "confidence": 66,
                    "source": "rules", "reason": f"ตลาดไซด์เวย์ ราคาอยู่แนวรับ H1 {sup['zone']} ({sup['touches']} ครั้ง) · TP ที่แนวต้าน"}
        if res and abs(res["distance_atr"] or 9) <= 0.4 and a["rsi"] > 55 and sup:
            sl = round(res["zone"][1] + 0.5 * a["atr"], 2)
            return {"action": "SELL", "order_type": "MARKET", "sl": sl, "tp": round(sup["zone"][1], 2), "confidence": 66,
                    "source": "rules", "reason": f"ตลาดไซด์เวย์ ราคาอยู่แนวต้าน H1 {res['zone']} ({res['touches']} ครั้ง) · TP ที่แนวรับ"}
    if reg in ("QUIET", "SPIKE") or (reg.startswith("PULLBACK") and not (ctx.get("signals"))):
        return {"action": "WAIT", "confidence": 0, "source": "rules",
                "reason": f"ตลาด{rg.get('label', '')} – {rg.get('advice', 'รอ')}"}
    up, dn = a["trend"] == "UP" and h["trend"] == "UP", a["trend"] == "DOWN" and h["trend"] == "DOWN"
    sigs = ctx.get("signals") or []
    buy_sig = [s for s in sigs if s["side"] == "BUY"]
    sell_sig = [s for s in sigs if s["side"] == "SELL"]
    lo, hi = sorted((a["ema20"], a["ema50"]))
    px = a["close"]
    if up and (buy_sig or (lo - 0.3 * a["atr"] <= px <= hi + 0.3 * a["atr"] and a["rsi"] > 50)):
        sl = min([s["sl"] for s in buy_sig] + [round(lo - a["atr"], 2)])
        risk_ = max(px - sl, 0.5 * a["atr"])
        conf = 60 + 10 * min(len(buy_sig), 2) + (10 if a["rsi"] > 55 else 0)
        return {"action": "BUY", "order_type": "MARKET", "sl": round(px - risk_, 2), "tp": round(px + 2 * risk_, 2),
                "confidence": conf, "source": "rules",
                "reason": "เทรนด์ขึ้นทั้ง " + tf + " และ H1" + (" + สัญญาณ " + ", ".join(s["strategy"] for s in buy_sig) if buy_sig else " ราคาอยู่โซน EMA20/50")}
    if dn and (sell_sig or (lo - 0.3 * a["atr"] <= px <= hi + 0.3 * a["atr"] and a["rsi"] < 50)):
        sl = max([s["sl"] for s in sell_sig] + [round(hi + a["atr"], 2)])
        risk_ = max(sl - px, 0.5 * a["atr"])
        conf = 60 + 10 * min(len(sell_sig), 2) + (10 if a["rsi"] < 45 else 0)
        return {"action": "SELL", "order_type": "MARKET", "sl": round(px + risk_, 2), "tp": round(px - 2 * risk_, 2),
                "confidence": conf, "source": "rules",
                "reason": "เทรนด์ลงทั้ง " + tf + " และ H1" + (" + สัญญาณ " + ", ".join(s["strategy"] for s in sell_sig) if sell_sig else " ราคาอยู่โซน EMA20/50")}
    return {"action": "WAIT", "confidence": 0, "source": "rules",
            "reason": "ทิศทาง TF สวนกัน หรือราคาไม่อยู่ในจุดเข้า – รอ"}


def run_ai(question=None, tf=None):
    ctx = market_context(tf)
    s = settings()["ai"]
    if s.get("model") in ("claude-sonnet-4-5", "", None):  # old default from earlier versions
        s["model"] = "claude-sonnet-5-5"
        store.update("settings", {"ai": {"model": s["model"]}})
    question = question or "วิเคราะห์ภาพรวม XAUUSD ตอนนี้ และวางแผนการเทรด (Bias, โซนเข้า, SL, TP, เงื่อนไขยกเลิกแผน)"
    provider = s.get("provider", "anthropic")
    key = s.get("openai_api_key") if provider == "openai" else s.get("anthropic_api_key")
    if not key:
        plan = validate_plan(rule_plan(ctx))
        name = "OpenAI (ChatGPT)" if provider == "openai" else "Anthropic (Claude)"
        return {"source": "rules", "context": ctx, "text": rule_based_playbook(ctx), "plan": plan,
                "note": f"ยังไม่ได้ใส่ API Key ของ {name} ในหน้า Settings – ใช้แผนจากกฎแทน"}
    prompt = ("คุณคือเทรดเดอร์ทองคำ (XAUUSD) มืออาชีพ ตอบเป็นภาษาไทย กระชับ เป็นข้อ ๆ ใช้เฉพาะข้อมูลด้านล่าง ห้ามแต่งตัวเลข\n"
              "ขั้นตอนคิด: 1) Bias จาก D1/H4 + โครงสร้าง (HH/HL, LH/LL) 2) ราคาอยู่ตรงไหนเทียบแนวรับต้านสำคัญ "
              "(prev day/week H/L, Asian range, โซน S/R H1, เลขกลม) 3) Session และความผันผวน (ATR percentile, "
              "range วันนี้เทียบ ADR – ถ้าวิ่งเกิน ADR แล้วอย่าไล่ราคา) 4) Price action แท่งล่าสุด + สัญญาณกลยุทธ์ "
              "(ให้น้ำหนักกลยุทธ์ที่มี edge จาก strategy_edge_oos) 5) วาง SL หลังโครงสร้าง/โซน ไม่ใช่ตัวเลขลอย ๆ "
              "6) ระบุสภาวะตลาดจาก market_regime (เทรนด์/ไซด์เวย์/ผันผวน/กระชาก/เงียบ) และเลือกวิธีเทรดให้เข้ากับมัน "
              "7) ข่าว: ดู economic_calendar_24h และ news_results_2h (actual เทียบ forecast → USD ดี/แย่กว่าคาด → ทองลง/ขึ้น) "
              "เทรดช่วงข่าวได้ แต่ห้ามไล่แท่งแรก ให้รอราคายืนยันทิศตามผลข่าว และใช้ SL กว้างขึ้น; ข่าวที่ยังไม่ออกและอยู่ใกล้มาก ให้ประเมินความเสี่ยงสองทาง "
              "8) ดู dollar_index ถ้ามี (DXY ขึ้นมักกดทอง) และ headlines\n"
              "เคารพกฎความเสี่ยงและออเดอร์ที่เปิดอยู่ (อย่าเปิดสวนทาง position ที่มีอยู่)\n\n"
              f"ข้อมูลตลาดและบัญชี (JSON):\n{ctx}\n\nคำถาม: {question}\n\n{PLAN_SPEC}")
    try:
        if provider == "openai":
            model = s.get("openai_model") or "gpt-6.1-sol"
            text = call_openai(key, model, prompt)
            src = "openai"
        else:
            model = s["model"]
            r = requests.post("https://api.anthropic.com/v1/messages", timeout=90, headers=anthropic_headers(key),
                json={"model": model, "max_tokens": 4000, "messages": [{"role": "user", "content": prompt}]})
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
            text = "".join(b.get("text", "") for b in r.json().get("content", []))
            src = "claude"
        plan = _parse_plan(text)
        if plan is None:  # answer was cut or had no JSON -> ask once more for the plan only (short, cheap)
            follow = (f"จากบทวิเคราะห์นี้:\n{text[-3000:]}\n\nและข้อมูลราคา: {ctx.get(tf or settings()['timeframe'], {})}\n"
                      "ตอบเป็น JSON อย่างเดียวใน ```json``` ตามรูปแบบ: "
                      '{"action": "BUY|SELL|WAIT", "order_type": "MARKET|LIMIT|STOP", "entry": null, "sl": 0, "tp": 0, "confidence": 0, "reason": ""}')
            try:
                if provider == "openai":
                    t2 = call_openai(key, model, follow)
                else:
                    r2 = requests.post("https://api.anthropic.com/v1/messages", timeout=60, headers=anthropic_headers(key),
                                       json={"model": model, "max_tokens": 500, "messages": [{"role": "user", "content": follow}]})
                    t2 = "".join(b.get("text", "") for b in r2.json().get("content", [])) if r2.status_code == 200 else ""
                plan = _parse_plan(t2)
            except Exception:
                plan = None
        plan = plan or {"action": "WAIT", "valid": False, "problem": "อ่านแผน JSON จากคำตอบไม่ได้", "confidence": 0}
        plan["source"] = src
        return {"source": src, "model": model, "context": ctx, "text": text, "plan": plan}
    except Exception as e:
        who = "ChatGPT" if provider == "openai" else "Claude"
        return {"source": "rules", "context": ctx, "text": rule_based_playbook(ctx), "plan": validate_plan(rule_plan(ctx)),
                "note": f"เรียก {who} ไม่สำเร็จ: {e} – ใช้แผนจากกฎแทน"}


@app.post("/api/ai/test")
def ai_test():
    """Check the selected AI provider: key valid? model available? one tiny request."""
    a = settings()["ai"]
    prov = a.get("provider", "anthropic")
    out = {"provider": prov, "steps": []}

    def step(name, ok, detail):
        out["steps"].append({"name": name, "ok": ok, "detail": detail})
        return ok

    if prov == "openai":
        key, model = a.get("openai_api_key"), a.get("openai_model") or "gpt-6.1-sol"
        if not step("มี API Key", bool(key), "ใส่แล้ว" if key else "ยังไม่ได้ใส่ OpenAI API Key"):
            return out
        try:
            r = requests.get("https://api.openai.com/v1/models", headers={"Authorization": f"Bearer {key}"}, timeout=20)
            if not step("ยืนยัน Key กับ OpenAI", r.status_code == 200, f"HTTP {r.status_code}" + ("" if r.status_code == 200 else f" {r.text[:160]}")):
                return out
            ids = sorted(x["id"] for x in r.json().get("data", []))
            has = model in ids
            sugg = [i for i in ids if i.startswith("gpt-")][-8:]
            if not step(f"โมเดล {model}", has, "พร้อมใช้" if has else f"ไม่พบในบัญชีนี้ – ลองใช้: {', '.join(sugg)}"):
                out["available_models"] = sugg
                return out
            t = time.time()
            txt = call_openai(key, model, "ตอบคำเดียวว่า OK")
            step("ส่งคำขอทดสอบ", bool(txt), f"ตอบว่า '{txt.strip()[:40]}' ใน {time.time() - t:.1f} วินาที")
        except Exception as e:
            step("เชื่อมต่อ api.openai.com", False, f"{type(e).__name__}: {str(e)[:160]}")
    else:
        key, model = a.get("anthropic_api_key"), a.get("model")
        if not step("มี API Key", bool(key), "ใส่แล้ว" if key else "ยังไม่ได้ใส่ Anthropic API Key"):
            return out
        try:
            t = time.time()
            r = requests.post("https://api.anthropic.com/v1/messages", timeout=60, headers=anthropic_headers(key),
                json={"model": model, "max_tokens": 20, "messages": [{"role": "user", "content": "ตอบคำเดียวว่า OK"}]})
            ok = r.status_code == 200
            txt = "".join(b.get("text", "") for b in r.json().get("content", [])) if ok else r.text[:160]
            step(f"ส่งคำขอทดสอบ ({model})", ok, (f"ตอบว่า '{txt.strip()[:40]}' ใน {time.time() - t:.1f} วินาที") if ok else f"HTTP {r.status_code} {txt}")
        except Exception as e:
            step("เชื่อมต่อ api.anthropic.com", False, f"{type(e).__name__}: {str(e)[:160]}")
    return out


def anthropic_headers(key):
    h = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    ws = (settings()["ai"].get("anthropic_workspace_id") or "").strip()
    if ws:
        h["anthropic-workspace-id"] = ws  # needed when the key is not scoped to a single workspace
    return h


def call_openai(key, model, prompt):
    """OpenAI Responses API (https://api.openai.com/v1/responses)."""
    r = requests.post("https://api.openai.com/v1/responses", timeout=120,
                      headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                      json={"model": model, "input": prompt, "max_output_tokens": 4000})
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    d = r.json()
    if d.get("output_text"):
        return d["output_text"]
    parts = []
    for item in d.get("output", []):
        for c in item.get("content", []) or []:
            if c.get("type") in ("output_text", "text") and c.get("text"):
                parts.append(c["text"])
    return "".join(parts)


@app.post("/api/ai/analyze")
def ai_analyze(body: dict = Body(default={})):
    res = run_ai(body.get("question"), body.get("tf"))
    AI_STATE.update(last=res["plan"], last_run=int(time.time()))
    return res


def execute_plan(plan, origin="AI"):
    plan = validate_plan(plan)
    if plan["action"] == "WAIT":
        raise HTTPException(400, {"violations": ["แผนคือ WAIT – ไม่มีออเดอร์ให้ส่ง"]})
    if not plan["valid"]:
        raise HTTPException(400, {"violations": [f"แผนใช้ไม่ได้: {plan['problem']}"]})
    c = risk.calc(sym(), plan["action"], plan["entry_ref"], plan["sl"], plan["tp"])
    body = {"side": plan["action"], "kind": plan["order_type"], "volume": c.get("lot") or 0, "price": plan["entry"],
            "sl": round(plan["sl"], 2), "tp": round(plan["tp"], 2) if plan["tp"] else None, "strategy": origin}
    return place_order(body)


@app.post("/api/ai/execute")
def ai_execute(body: dict = Body(...)):
    return execute_plan(body.get("plan") or {}, "AI")


@app.get("/api/ai/state")
def ai_state():
    sc = store.load("scanner")
    return {**AI_STATE, "ai_auto": sc.get("ai_auto", False), "interval_min": sc.get("ai_interval_min", 15),
            "min_conf": sc.get("ai_min_conf", 65), "has_key": ai_has_key(), "provider": settings()["ai"].get("provider", "anthropic")}


def ai_has_key():
    a = settings()["ai"]
    return bool(a.get("openai_api_key") if a.get("provider") == "openai" else a.get("anthropic_api_key"))


def ai_autotrade_tick():
    sc = store.load("scanner")
    if not sc.get("ai_auto") or store.load("risk")["kill_switch"]:
        return
    if time.time() - AI_STATE["last_run"] < max(1, sc.get("ai_interval_min", 15)) * 60:
        return
    AI_STATE["last_run"] = int(time.time())
    res = run_ai()
    plan = res["plan"]
    AI_STATE["last"] = plan
    sig = {"strategy": "AI", "side": plan.get("action", "WAIT"), "entry": plan.get("entry_ref")}
    if plan.get("action") == "WAIT":
        return _alog(sig, "skip", "AI: WAIT – " + plan.get("reason", ""))
    if plan.get("confidence", 0) < sc.get("ai_min_conf", 65):
        return _alog(sig, "skip", f"AI ความมั่นใจ {plan.get('confidence')} < {sc.get('ai_min_conf', 65)}")
    nb = news.blackout()
    if nb:
        return _alog(sig, "blocked", nb)
    if any(p["comment"].startswith("AIT:AI") for p in broker.positions()):
        return _alog(sig, "skip", "มีออเดอร์ของ AI เปิดอยู่แล้ว")
    try:
        r = execute_plan(plan, "AI")
        _alog(sig, "sent", f"AI {plan['action']} conf {plan['confidence']} · {r.get('message')}")
    except HTTPException as e:
        d = e.detail
        _alog(sig, "blocked", "; ".join(d["violations"]) if isinstance(d, dict) else str(d))


def rule_based_playbook(ctx):
    tf = ctx["timeframe"]
    a, h = ctx[tf], ctx.get("H1", ctx[tf])
    bias = "ขาขึ้น (เน้น BUY)" if a["trend"] == "UP" and h["trend"] == "UP" else (
        "ขาลง (เน้น SELL)" if a["trend"] == "DOWN" and h["trend"] == "DOWN" else "ไม่ชัด / สวนกันระหว่าง TF (ลดขนาดหรือรอ)")
    lines = [f"• Bias: {bias}  | {tf} RSI {a['rsi']} ATR {a['atr']}",
             f"• โครงสร้าง {tf}: High50 {a['high_50']} / Low50 {a['low_50']}  | EMA20 {a['ema20']} EMA50 {a['ema50']}",
             f"• H1: trend {h['trend']}, EMA50 {h['ema50']} / EMA200 {h['ema200']}"]
    lo, hi = sorted((a["ema20"], a["ema50"]))
    px = a["close"]
    if a["trend"] == "UP":
        sl = round(lo - a["atr"], 2)
        where = ("ราคาอยู่เหนือโซน รอย่อลงมาก่อน อย่าไล่ราคา" if px > hi else
                 "ราคาอยู่ในโซนแล้ว รอแท่งยืนยัน (ปิดเขียวเหนือ EMA20)" if px >= lo else
                 "ราคาหลุดใต้โซน = โครงสร้างขาขึ้นอ่อนแรง ควรรอ")
        lines.append(f"• แผน BUY: โซนย่อ {lo}–{hi} (EMA20/50), SL ใต้ {sl}, TP แรก {a['high_50']} – {where}")
    else:
        sl = round(hi + a["atr"], 2)
        where = ("ราคาอยู่ใต้โซน รอดีดขึ้นมาก่อน อย่าไล่ราคา" if px < lo else
                 "ราคาอยู่ในโซนแล้ว รอแท่งยืนยัน (ปิดแดงใต้ EMA20)" if px <= hi else
                 "ราคายืนเหนือโซน = แรงขายอ่อน ควรรอให้ปิดกลับใต้ EMA20 ก่อน")
        lines.append(f"• แผน SELL: โซนดีด {lo}–{hi} (EMA20/50), SL เหนือ {sl}, TP แรก {a['low_50']} – {where}")
    if a["trend"] != h["trend"]:
        lines.append("• TF ใหญ่กับ TF เล็กสวนกัน: ลด Lot ครึ่งหนึ่ง หรือรอให้ทิศทางตรงกัน")
    if ctx.get("mtf_alignment"):
        mt = ctx["mtf_alignment"]["trends"]
        lines.insert(1, "• หลาย TF: " + " · ".join(f"{k} {v}" for k, v in mt.items()))
    rg = ctx.get("market_regime") or {}
    if rg.get("label"):
        lines.insert(1, f"• สภาวะตลาด {tf}: {rg['label']} (ความแรงเทรนด์ {rg.get('trend_strength', '-')}, ATR pct {rg.get('atr_percentile', '-')}) → {rg.get('advice', '')}")
    if ctx.get("session"):
        lines.append(f"• Session: {ctx['session']['name']} ({ctx['session']['weekday']})")
    nr = ctx.get("news_results_2h")
    if isinstance(nr, list):
        for e in nr[:3]:
            lines.append(f"• ผลข่าว {e['title']}: จริง {e['actual']} vs คาด {e['forecast'] or '-'} → {e['reading'].get('usd')} · {e['reading'].get('gold')} ({e['minutes_ago']} นาทีที่แล้ว)")
    cal = ctx.get("economic_calendar_24h")
    if isinstance(cal, list):
        nxt = [e for e in cal if e["minutes_until"] > 0][:3]
        if nxt:
            lines.append("• ข่าวที่จะออก: " + " · ".join(f"{e['title']} ({e['impact']}, อีก {e['minutes_until']} นาที)" for e in nxt))
    lv = ctx.get("levels") or {}
    if lv:
        parts = [f"{n} {lv[k]}" for k, n in (("prev_day_high", "PDH"), ("prev_day_low", "PDL"), ("asian_high", "Asia H"),
                                             ("asian_low", "Asia L"), ("week_high", "Week H"), ("week_low", "Week L")) if lv.get(k)]
        if parts:
            lines.append("• แนวสำคัญ: " + " · ".join(parts))
        if lv.get("today_range_vs_adr_pct") is not None:
            pct = lv["today_range_vs_adr_pct"]
            lines.append(f"• วันนี้วิ่งไป {pct}% ของ ADR ({lv.get('adr14')})" + (" – ใกล้/เกินค่าเฉลี่ยแล้ว ระวังไล่ราคา" if pct >= 90 else ""))
    zones = ctx.get("sr_zones_h1") or {}
    if zones.get("resistance") or zones.get("support"):
        r_ = zones.get("resistance", [])[:1]; s_ = zones.get("support", [])[:1]
        lines.append("• โซน H1: " + " · ".join([f"ต้าน {z['zone'][0]}–{z['zone'][1]} ({z['touches']} ครั้ง)" for z in r_] +
                                               [f"รับ {z['zone'][0]}–{z['zone'][1]} ({z['touches']} ครั้ง)" for z in s_]))
    if ctx.get("trader_notes"):
        lines.append(f"• โน้ตของคุณ: {ctx['trader_notes']}")
    if ctx["signals"]:
        lines.append("• สัญญาณล่าสุด: " + ", ".join(f"{x['strategy']} {x['side']} @{x['entry']}" for x in ctx["signals"]))
    r = ctx["risk_today"]
    lines.append(f"• ความเสี่ยงวันนี้: P/L {r['day_pl']} | ใช้ลิมิตขาดทุนไป {r['loss_used_pct']}% | ออเดอร์ {r['orders_today']}")
    return "\n".join(lines)


# ------------------------------------------------------------------ frontend
app.mount("/static", StaticFiles(directory=FRONT), name="static")


@app.get("/")
def index():
    return FileResponse(os.path.join(FRONT, "index.html"))


if __name__ == "__main__":
    threading.Thread(target=background, daemon=True).start()
    threading.Thread(target=news.loop, daemon=True).start()
    port = int(os.environ.get("PORT", 8000))
    print(f"\n  AI Trader Dashboard  ->  http://127.0.0.1:{port}   (mode: {broker.mode.upper()})\n")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
