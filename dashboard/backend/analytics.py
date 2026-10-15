"""Turns MT5 deals into a trade journal and computes performance statistics."""
import math
from collections import defaultdict

DAY = 86400


def deal_net(d):
    return (d.get("profit") or 0) + (d.get("commission") or 0) + (d.get("swap") or 0) + (d.get("fee") or 0)


def source_of(magic, ea_map):
    name = ea_map.get(str(magic))
    if name:
        return name
    return "Manual" if not magic else f"EA #{magic}"


def build_journal(deals, ea_map, notes=None):
    notes = notes or {}
    by_pos = defaultdict(list)
    for d in deals:
        if d.get("type") in (0, 1) and d.get("symbol"):
            by_pos[d["position_id"]].append(d)
    trades = []
    for pid, ds in by_pos.items():
        ds.sort(key=lambda x: x["time"])
        ins = [d for d in ds if d["entry"] == 0]
        outs = [d for d in ds if d["entry"] in (1, 2, 3)]
        if not ins:
            continue  # position opened before the history window
        first = ins[0]
        vin = sum(d["volume"] for d in ins)
        vout = sum(d["volume"] for d in outs)
        open_price = sum(d["price"] * d["volume"] for d in ins) / vin if vin else first["price"]
        close_price = sum(d["price"] * d["volume"] for d in outs) / vout if vout else None
        side = "BUY" if first["type"] == 0 else "SELL"
        closed = vout >= vin - 1e-9 and bool(outs)
        net = sum(deal_net(d) for d in ds)
        magic = first["magic"]
        comment = first.get("comment") or ""
        source = source_of(magic, ea_map)
        strategy = comment[4:].split(" ")[0] if comment.startswith("AIT:") else source
        exit_comment = outs[-1].get("comment", "") if outs else ""
        ec = exit_comment.lower()
        reason = ("SL" if "[sl" in ec or ec.startswith("sl") else "TP" if "[tp" in ec or ec.startswith("tp") else
                  "Dashboard" if "dashboard" in ec else "Manual/EA" if outs else "")
        points = None
        if close_price is not None:
            points = round((close_price - open_price) * (1 if side == "BUY" else -1), 2)
        trades.append({
            "position_id": pid, "symbol": first["symbol"], "side": side, "volume": round(vin, 2),
            "open_time": first["time"], "close_time": outs[-1]["time"] if outs else None,
            "open_price": round(open_price, 3), "close_price": round(close_price, 3) if close_price else None,
            "profit": round(net, 2), "gross": round(sum(d["profit"] for d in ds), 2),
            "commission": round(sum((d.get("commission") or 0) + (d.get("fee") or 0) for d in ds), 2),
            "swap": round(sum(d.get("swap") or 0 for d in ds), 2),
            "magic": magic, "source": source, "strategy": strategy, "comment": comment,
            "exit_reason": reason, "status": "closed" if closed else "open", "points": points,
            "duration_min": round((outs[-1]["time"] - first["time"]) / 60, 1) if outs else None,
            "note": notes.get(str(pid), ""),
        })
    trades.sort(key=lambda t: t["open_time"], reverse=True)
    return trades


def _streaks(results):
    best_w = best_l = cw = cl = 0
    for r in results:
        if r > 0:
            cw += 1; cl = 0
        else:
            cl += 1; cw = 0
        best_w, best_l = max(best_w, cw), max(best_l, cl)
    return best_w, best_l


def summarize(trades):
    closed = [t for t in trades if t["status"] == "closed"]
    pl = [t["profit"] for t in closed]
    wins = [p for p in pl if p > 0]
    losses = [p for p in pl if p <= 0]
    gp, gl = sum(wins), -sum(losses)
    n = len(pl)
    avg_w = gp / len(wins) if wins else 0.0
    avg_l = gl / len(losses) if losses else 0.0
    sw, sl = _streaks([t["profit"] for t in sorted(closed, key=lambda t: t["close_time"])])
    durs = [t["duration_min"] for t in closed if t["duration_min"] is not None]
    return {
        "trades": n, "wins": len(wins), "losses": len(losses),
        "win_rate": round(len(wins) / n * 100, 1) if n else 0.0,
        "net": round(sum(pl), 2), "gross_profit": round(gp, 2), "gross_loss": round(gl, 2),
        "profit_factor": round(gp / gl, 2) if gl > 0 else (None if not wins else 99.0),
        "avg_win": round(avg_w, 2), "avg_loss": round(avg_l, 2),
        "payoff": round(avg_w / avg_l, 2) if avg_l else None,
        "expectancy": round(sum(pl) / n, 2) if n else 0.0,
        "largest_win": round(max(pl), 2) if pl else 0.0, "largest_loss": round(min(pl), 2) if pl else 0.0,
        "max_consec_wins": sw, "max_consec_losses": sl,
        "avg_duration_min": round(sum(durs) / len(durs), 1) if durs else 0.0,
    }


def balance_curve(deals):
    pts, bal = [], 0.0
    for d in sorted(deals, key=lambda x: x["time"]):
        v = deal_net(d)
        if v == 0 and d.get("type") in (0, 1):
            continue
        bal += v
        pts.append({"time": d["time"], "balance": round(bal, 2), "deposit": d.get("type") == 2})
    return pts


def drawdown(curve):
    peak, mdd, mdd_pct = -1e18, 0.0, 0.0
    for p in curve:
        b = p["balance"]
        if b > peak:
            peak = b
        dd = peak - b
        if dd > mdd:
            mdd = dd
        if peak > 0:
            mdd_pct = max(mdd_pct, dd / peak * 100)
    cur = (peak - curve[-1]["balance"]) if curve else 0.0
    return {"max_dd": round(mdd, 2), "max_dd_pct": round(mdd_pct, 2), "current_dd": round(cur, 2)}


def daily(trades):
    d = defaultdict(lambda: {"pnl": 0.0, "trades": 0, "wins": 0})
    for t in trades:
        if t["status"] != "closed":
            continue
        key = t["close_time"] // DAY * DAY
        d[key]["pnl"] += t["profit"]; d[key]["trades"] += 1; d[key]["wins"] += t["profit"] > 0
    return [{"day": k, "pnl": round(v["pnl"], 2), "trades": v["trades"], "wins": v["wins"]} for k, v in sorted(d.items())]


def consistency(days):
    if not days:
        return {"days": 0}
    p = [x["pnl"] for x in days]
    n = len(p)
    mean = sum(p) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in p) / n) if n > 1 else 0.0
    pos = sum(x for x in p if x > 0)
    return {
        "days": n, "green_days": sum(1 for x in p if x > 0), "red_days": sum(1 for x in p if x <= 0),
        "green_pct": round(sum(1 for x in p if x > 0) / n * 100, 1),
        "avg_day": round(mean, 2), "std_day": round(sd, 2),
        "best_day": round(max(p), 2), "worst_day": round(min(p), 2),
        "best_day_share_pct": round(max(p) / pos * 100, 1) if pos > 0 else None,
        "sharpe_daily_ann": round(mean / sd * math.sqrt(252), 2) if sd > 0 else None,
    }


def group(trades, key):
    g = defaultdict(list)
    for t in trades:
        g[key(t)].append(t)
    return {k: summarize(v) for k, v in g.items()}


def full_report(trades, deals):
    closed = [t for t in trades if t["status"] == "closed"]
    curve = balance_curve(deals)
    days = daily(closed)
    by_hour = defaultdict(lambda: {"pnl": 0.0, "trades": 0})
    by_wd = defaultdict(lambda: {"pnl": 0.0, "trades": 0})
    for t in closed:
        h = (t["open_time"] // 3600) % 24
        wd = ((t["open_time"] // DAY) + 3) % 7  # 0 = Monday
        by_hour[h]["pnl"] += t["profit"]; by_hour[h]["trades"] += 1
        by_wd[wd]["pnl"] += t["profit"]; by_wd[wd]["trades"] += 1
    return {
        "summary": summarize(closed),
        "drawdown": drawdown(curve),
        "curve": curve,
        "daily": days,
        "consistency": consistency(days),
        "by_source": group(closed, lambda t: t["source"]),
        "by_strategy": group(closed, lambda t: t["strategy"]),
        "by_side": group(closed, lambda t: t["side"]),
        "by_hour": [{"hour": h, "pnl": round(by_hour[h]["pnl"], 2), "trades": by_hour[h]["trades"]} for h in range(24)],
        "by_weekday": [{"wd": w, "pnl": round(by_wd[w]["pnl"], 2), "trades": by_wd[w]["trades"]} for w in range(7)],
    }


def period_pnl(trades, now):
    day0 = now // DAY * DAY
    wd = ((now // DAY) + 3) % 7
    week0 = day0 - wd * DAY
    import datetime as _dt
    dt = _dt.datetime.fromtimestamp(now, tz=_dt.timezone.utc)
    month0 = int(_dt.datetime(dt.year, dt.month, 1, tzinfo=_dt.timezone.utc).timestamp())
    out = {"today": 0.0, "week": 0.0, "month": 0.0}
    for t in trades:
        if t["status"] != "closed":
            continue
        if t["close_time"] >= day0:
            out["today"] += t["profit"]
        if t["close_time"] >= week0:
            out["week"] += t["profit"]
        if t["close_time"] >= month0:
            out["month"] += t["profit"]
    return {k: round(v, 2) for k, v in out.items()}
