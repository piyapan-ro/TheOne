"""
AI brain of the AI EA (EA/AIEA_M5.mq5).

The EA computes everything that can be measured (closed M5 bars, S/R zones, order blocks,
market structure, H1/H4 context) and posts it to /api/ea/decide. This module asks the AI to
score BUY and SELL separately, then checks the answer with fixed rules before the EA sees it:

- the chosen side needs score >= min_score and a lead of >= min_gap over the other side,
  otherwise the decision is NONE (score = relative weight of evidence, not a win probability)
- the zone must exist in the EA's own zone list (the AI cannot invent levels)
- the zone must belong to the chosen side, and for a retest the nearest same-side zone wins
  (e.g. resistance vs bearish OB: wait at whichever is closer to price)
- SMC retest needs a sweep + CHOCH/MSS in the same direction in the EA's structure data
- entry / invalidation come from the zone edges, not from free numbers in the AI text

Every call is costed from the token usage; a daily budget stops new AI calls (and with them new
orders) once it is spent. Stats keep the AI cost per order the EA actually opened.
"""
import json
import threading
import time
import uuid

import requests

import store

# USD per 1M tokens (input, output). Unknown models use ea_ai.price_in / price_out from the settings.
PRICES = {
    "claude-opus-5-5": (4.0, 20.0), "claude-opus-5": (5.0, 25.0), "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-5-5": (2.0, 10.0), "claude-sonnet-5": (2.0, 10.0), "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-5-5": (0.10, 0.50), "claude-haiku-4-5": (1.0, 5.0),
}
SIDE_KINDS = {"BUY": ("support", "bull_ob"), "SELL": ("resistance", "bear_ob")}
SETUPS = ("breakout", "retest", "smc_retest", "none")
LOG_MAX = 300
_lock = threading.Lock()

_SIDE = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "description": "0-100 weight of evidence for this side"},
        "setup": {"type": "string", "enum": list(SETUPS)},
        "zone_id": {"type": "string", "description": "id from the zones list, empty when setup is none"},
        "reason": {"type": "string", "description": "short, max 25 words"},
    },
    "required": ["score", "setup", "zone_id", "reason"],
    "additionalProperties": False,
}
SCHEMA = {
    "type": "object",
    "properties": {
        "buy": _SIDE,
        "sell": _SIDE,
        "decision": {"type": "string", "enum": ["BUY", "SELL", "NONE"]},
        "valid_bars": {"type": "integer", "description": "closed M5 bars the plan stays valid"},
        "note": {"type": "string", "description": "one short sentence for the trader (Thai)"},
    },
    "required": ["buy", "sell", "decision", "valid_bars", "note"],
    "additionalProperties": False,
}

SYSTEM = """You are the decision engine of a gold (XAUUSD) M5 expert advisor. One trade at a time, always with SL/TP.
Score BUY and SELL SEPARATELY from 0 to 100. A score is the relative weight of evidence for that side, not a win probability.
Use only the data given: closed M5 bars, zones (S/R and order blocks with ids), M5 structure (swings, BOS, CHOCH/MSS, liquidity sweeps) and H1/H4 context.

Allowed entry plans:
- BUY breakout: a closed M5 bar broke ABOVE a level that now sits under price (zone kind support/bull_ob with flip=broken_up), entry on the level.
- BUY retest: price comes back down to a support or bullish OB and holds.
- SELL breakdown (setup "breakout"): a closed M5 bar broke BELOW a level that now sits above price (kind resistance/bear_ob with flip=broken_down).
- SELL retest: price comes back up to a resistance or bearish OB and rejects.
- smc_retest: liquidity sweep -> CHOCH/MSS in the new direction -> retest of the OB/zone that caused the move.
For BUY only zones of kind support or bull_ob are valid; for SELL only resistance or bear_ob.

Weigh: H4/H1 trend alignment (strong weight), M5 structure and recent BOS/CHOCH, sweep of liquidity in the opposite direction,
zone quality (touches, freshness, OB not mitigated), room to the next opposing zone (at least 1.5x the risk), momentum of the last bars,
spread and volatility. Penalise trading straight into a nearby opposing zone, chasing extended moves, and choppy overlapping bars.

decision = NONE when both scores are low, when they are close to each other, or when no plan fits. Waiting is a good outcome.
valid_bars: how many closed M5 bars the plan stays valid (1-12, breakout usually 1-3, retest 3-12).
Keep reasons short. Write note in Thai."""


def cfg():
    return store.load("ea_ai")


def _today():
    return time.strftime("%Y-%m-%d", time.gmtime())


def _stats_day(st, day):
    return st.setdefault("days", {}).setdefault(day, {"calls": 0, "errors": 0, "cost": 0.0, "tokens_in": 0, "tokens_out": 0,
                                                       "buy": 0, "sell": 0, "none": 0, "orders": 0})


def spent_today():
    return float(store.load("ea_ai_stats").get("days", {}).get(_today(), {}).get("cost", 0.0))


def _price(model, c):
    if model in PRICES:
        return PRICES[model]
    return float(c.get("price_in") or 2.0), float(c.get("price_out") or 10.0)


def _prompt(req):
    """Compact JSON (one line) - the EA's data is the whole prompt, so tokens stay low."""
    return ("Market data from the EA (prices in quote currency, bars oldest->newest, all bars closed):\n"
            + json.dumps(req, separators=(",", ":"), ensure_ascii=False)
            + "\nReturn the JSON decision.")


def _call_anthropic(key, model, prompt, c, headers_fn):
    body = {"model": model, "max_tokens": int(c.get("max_tokens") or 3000), "system": SYSTEM,
            "messages": [{"role": "user", "content": prompt}],
            "output_config": {"format": {"type": "json_schema", "schema": SCHEMA}}}
    if c.get("effort"):
        body["output_config"]["effort"] = c["effort"]
    r = requests.post("https://api.anthropic.com/v1/messages", timeout=int(c.get("timeout_sec") or 45),
                      headers=headers_fn(key), json=body)
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    d = r.json()
    if d.get("stop_reason") in ("refusal", "max_tokens"):
        raise RuntimeError(f"AI stopped: {d.get('stop_reason')}")
    u = d.get("usage") or {}
    text = "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text")
    return text, int(u.get("input_tokens") or 0) + int(u.get("cache_read_input_tokens") or 0) \
        + int(u.get("cache_creation_input_tokens") or 0), int(u.get("output_tokens") or 0)


def _call_openai(key, model, prompt, c):
    r = requests.post("https://api.openai.com/v1/responses", timeout=int(c.get("timeout_sec") or 45),
                      headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                      json={"model": model, "instructions": SYSTEM, "input": prompt,
                            "max_output_tokens": int(c.get("max_tokens") or 3000),
                            "text": {"format": {"type": "json_schema", "name": "ea_decision", "schema": SCHEMA, "strict": True}}})
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:200]}")
    d = r.json()
    text = d.get("output_text") or "".join(
        c_.get("text", "") for item in d.get("output", []) for c_ in (item.get("content") or [])
        if c_.get("type") in ("output_text", "text"))
    u = d.get("usage") or {}
    return text, int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0)


def _zone_side_ok(side, z):
    return z.get("kind") in SIDE_KINDS[side]


def _dist(side, z, price):
    """Distance from price to the edge where a BUY/SELL would wait."""
    return abs(price - (z["high"] if side == "BUY" else z["low"]))


def _has_smc(side, structure):
    """Sweep of the opposite liquidity followed by a CHOCH/MSS in our direction."""
    want = "up" if side == "BUY" else "down"
    sweep_side = "sell_side" if side == "BUY" else "buy_side"
    ch = structure.get("last_choch") or {}
    if ch.get("dir") != want:
        return False
    return any(s.get("side") == sweep_side and int(s.get("bars_ago", 999)) >= int(ch.get("bars_ago", 0))
               for s in structure.get("sweeps") or [])


def finalize(ai, req, c):
    """Turn the AI answer into a plan the EA can check. Returns dict with decision BUY/SELL/NONE."""
    price = float(req.get("price", {}).get("bid") or 0)
    atr = float(req.get("atr_m5") or 0) or 1.0
    zones = {str(z.get("id")): z for z in req.get("zones") or [] if z.get("id")}
    out = {"decision": "NONE", "buy_score": int(ai["buy"]["score"]), "sell_score": int(ai["sell"]["score"]),
           "setup": "none", "zone_id": "", "entry": 0.0, "invalidation": 0.0, "zone_low": 0.0, "zone_high": 0.0,
           "valid_bars": 0, "note": str(ai.get("note") or "")[:200], "reject": "",
           # per-side detail for the EA panel (what each side would trade and why)
           "buy_setup": str(ai["buy"].get("setup") or "none"), "buy_zone": str(ai["buy"].get("zone_id") or ""),
           "buy_reason": str(ai["buy"].get("reason") or "")[:120],
           "sell_setup": str(ai["sell"].get("setup") or "none"), "sell_zone": str(ai["sell"].get("zone_id") or ""),
           "sell_reason": str(ai["sell"].get("reason") or "")[:120]}
    side = ai.get("decision")
    if side not in ("BUY", "SELL"):
        out["reject"] = "AI เลือก NONE"
        return out
    me, other = (ai["buy"], ai["sell"]) if side == "BUY" else (ai["sell"], ai["buy"])
    min_score, min_gap = int(c.get("min_score", 60)), int(c.get("min_gap", 15))
    if me["score"] < min_score:
        out["reject"] = f"คะแนน {side} {me['score']} < {min_score}"
        return out
    if me["score"] - other["score"] < min_gap:
        out["reject"] = f"คะแนนสูสี ({ai['buy']['score']} vs {ai['sell']['score']})"
        return out
    setup = me.get("setup")
    z = zones.get(str(me.get("zone_id")))
    if setup not in ("breakout", "retest", "smc_retest"):
        out["reject"] = "ไม่มีรูปแบบเข้า"
        return out
    if not z or not _zone_side_ok(side, z):
        out["reject"] = f"โซน {me.get('zone_id')!r} ไม่มีอยู่จริงหรือไม่ใช่ฝั่ง {side}"
        return out
    if setup == "breakout":
        want = "broken_up" if side == "BUY" else "broken_down"
        if z.get("flip") != want:
            out["reject"] = f"โซน {z['id']} ยังไม่ถูก {'breakout' if side == 'BUY' else 'breakdown'} ด้วยแท่ง M5 ที่ปิดแล้ว"
            return out
    else:
        if setup == "smc_retest" and not _has_smc(side, req.get("structure") or {}):
            out["reject"] = "SMC: ไม่พบ Liquidity sweep -> CHOCH/MSS ในทิศเดียวกัน"
            return out
        # retest: compare zones of the same side (S/R vs OB) and wait at the one nearer to price
        same = [x for x in zones.values() if _zone_side_ok(side, x)
                and ((side == "BUY" and x["high"] <= price + 0.2 * atr) or (side == "SELL" and x["low"] >= price - 0.2 * atr))]
        if same:
            near = min(same, key=lambda x: _dist(side, x, price))
            if near["id"] != z["id"]:
                out["note"] = (out["note"] + f" | เลือกโซน {near['id']} แทน {z['id']} เพราะใกล้ราคากว่า")[:240]
                z = near
        if _dist(side, z, price) > float(c.get("max_wait_atr", 2.0)) * atr:
            out["reject"] = f"โซน {z['id']} ไกลราคาเกิน {c.get('max_wait_atr', 2.0)} ATR"
            return out
    out.update(decision=side, setup=setup, zone_id=z["id"], zone_low=float(z["low"]), zone_high=float(z["high"]),
               entry=float(z["high"] if side == "BUY" else z["low"]),
               invalidation=float(z["low"] if side == "BUY" else z["high"]),
               valid_bars=max(1, min(int(ai.get("valid_bars") or 3), int(c.get("max_valid_bars", 12)))))
    return out


def decide(req, ai_settings, headers_fn, overrides=None):
    """Main entry for /api/ea/decide. Never raises: errors come back as {"ok": false, "error": ...}.
    overrides: config keys for one call (ea_backtest.py uses model / effort / no_budget / no_log)."""
    c = {**cfg(), **(overrides or {})}
    plan_id = uuid.uuid4().hex[:12]
    if not c.get("enabled", True):
        return {"ok": False, "error": "AI EA ถูกปิดใน Dashboard", "plan_id": plan_id}
    budget = 0 if c.get("no_budget") else float(c.get("daily_budget_usd") or 0)
    if budget > 0 and spent_today() >= budget:
        return {"ok": False, "error": f"ใช้งบ AI วันนี้ครบ {budget} USD แล้ว", "plan_id": plan_id}
    prov = ai_settings.get("provider", "anthropic")
    key = ai_settings.get("openai_api_key") if prov == "openai" else ai_settings.get("anthropic_api_key")
    if not key:
        return {"ok": False, "error": "ยังไม่ได้ใส่ API Key ใน Dashboard", "plan_id": plan_id}
    model = c.get("model") or (ai_settings.get("openai_model") if prov == "openai" else ai_settings.get("model")) \
        or ("gpt-6.1-sol" if prov == "openai" else "claude-sonnet-5-5")
    t0 = time.time()
    tin = tout = 0
    try:
        if prov == "openai":
            text, tin, tout = _call_openai(key, model, _prompt(req), c)
        else:
            text, tin, tout = _call_anthropic(key, model, _prompt(req), c, headers_fn)
        ai = json.loads(text)
        res = finalize(ai, req, c)
        err = ""
    except Exception as e:
        ai, res, err = None, None, f"{type(e).__name__}: {str(e)[:180]}"
    pin, pout = _price(model, c)
    cost = round(tin / 1e6 * pin + tout / 1e6 * pout, 5)
    if not c.get("no_log"):
        _log(plan_id, req, model, ai, res, err, cost, tin, tout, time.time() - t0)
    if err:
        return {"ok": False, "error": err, "plan_id": plan_id, "cost_usd": cost}
    return {"ok": True, "plan_id": plan_id, "model": model, "cost_usd": cost,
            "spent_today": round(spent_today(), 4), "budget": float(c.get("daily_budget_usd") or 0), **res}


def _log(plan_id, req, model, ai, res, err, cost, tin, tout, secs):
    def upd(st):
        d = _stats_day(st, _today())
        d["calls"] += 1
        d["cost"] = round(d["cost"] + cost, 5)
        d["tokens_in"] += tin
        d["tokens_out"] += tout
        if err:
            d["errors"] += 1
        else:
            d[res["decision"].lower()] += 1
        days = st["days"]
        for k in sorted(days)[:-60]:
            days.pop(k)
        st.setdefault("log", []).append({
            "id": plan_id, "time": int(time.time()), "bar_time": req.get("bar_time"), "symbol": req.get("symbol"),
            "model": model, "cost": cost, "tokens": [tin, tout], "secs": round(secs, 1), "error": err,
            "ai": ai, "result": res, "order": None})
        st["log"] = st["log"][-LOG_MAX:]
    with _lock:
        store.modify("ea_ai_stats", upd)


def report(body):
    """The EA tells us it opened (or dropped) a plan - used for cost per order."""
    pid, ticket = str(body.get("plan_id") or ""), body.get("ticket")
    status = str(body.get("status") or "opened")

    def upd(st):
        for rec in reversed(st.get("log", [])):
            if rec["id"] == pid:
                rec["order"] = {"status": status, "ticket": ticket, "price": body.get("price"), "lot": body.get("lot"),
                                "reason": str(body.get("reason") or "")[:160], "time": int(time.time())}
                break
        if status == "opened":
            _stats_day(st, _today())["orders"] += 1
    with _lock:
        store.modify("ea_ai_stats", upd)
    return {"ok": True}


def stats(days=7):
    st = store.load("ea_ai_stats")
    keys = sorted(st.get("days", {}))[-days:]
    rows = [{"day": k, **st["days"][k]} for k in keys]
    calls = sum(r["calls"] for r in rows)
    cost = sum(r["cost"] for r in rows)
    orders = sum(r["orders"] for r in rows)
    return {"days": rows, "calls": calls, "cost_usd": round(cost, 4), "orders": orders,
            "cost_per_order": round(cost / orders, 4) if orders else None,
            "cost_per_call": round(cost / calls, 5) if calls else None,
            "spent_today": round(spent_today(), 4), "budget": cfg().get("daily_budget_usd"),
            "log": st.get("log", [])[-30:][::-1]}
