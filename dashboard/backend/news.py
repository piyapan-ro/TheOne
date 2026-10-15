"""
Economic calendar (Forex Factory weekly feed) + news blackout for auto-trading.

Source: https://nfs.faireconomy.media/ff_calendar_thisweek.json  (free, no key, updated ~hourly).
The feed asks clients not to poll aggressively, so results are cached in data/news_cache.json
and refreshed at most once per `refresh_min` minutes.
"""
import json
import os
import threading
import time
from datetime import datetime

import requests

import store

URLS = {"this": "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
        "next": "https://nfs.faireconomy.media/ff_calendar_nextweek.json"}
CACHE = os.path.join(store.DATA_DIR, "news_cache.json")
DEFAULTS = {"enabled": True, "currencies": ["USD"], "impacts": ["High", "Medium"],
            "block_impacts": ["High"], "block_before_min": 30, "block_after_min": 30,
            "block_auto": False, "block_manual": False, "refresh_min": 60}
_lock = threading.Lock()
STATE = {"events": [], "fetched_at": 0, "error": "", "source": URLS["this"]}


def config():
    return {**DEFAULTS, **store.load("news")}


def _parse_time(s):
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp())
    except Exception:
        return None


def _normalise(raw):
    out = []
    for e in raw or []:
        t = _parse_time(str(e.get("date", "")))
        if t is None:
            continue
        out.append({"time_utc": t, "title": e.get("title", ""), "currency": e.get("country", ""),
                    "impact": e.get("impact", ""), "forecast": e.get("forecast", ""), "previous": e.get("previous", ""),
                    "actual": e.get("actual", "")})
    out.sort(key=lambda x: x["time_utc"])
    return out


def _load_cache():
    if not STATE["events"] and os.path.exists(CACHE):
        try:
            with open(CACHE, encoding="utf-8") as f:
                STATE.update(json.load(f))
        except Exception:
            pass


def refresh(force=False):
    cfg = config()
    _load_cache()
    if not cfg["enabled"]:
        return STATE
    if not force and time.time() - STATE["fetched_at"] < cfg["refresh_min"] * 60:
        return STATE
    with _lock:
        events, errors = [], []
        wd = datetime.utcnow().weekday()  # Fri/Sat/Sun -> also load next week
        for key in (["this", "next"] if wd >= 4 else ["this"]):
            try:
                r = requests.get(URLS[key], timeout=15, headers={"User-Agent": "AITraderDashboard/1.0"})
                r.raise_for_status()
                events += _normalise(r.json())
            except Exception as e:  # keep the old cache on failure
                errors.append(f"{key}: {type(e).__name__} {str(e)[:80]}")
        if events:
            seen, uniq = set(), []
            for e in sorted(events, key=lambda x: x["time_utc"]):
                k = (e["time_utc"], e["title"], e["currency"])
                if k not in seen:
                    seen.add(k); uniq.append(e)
            STATE.update(events=uniq, fetched_at=int(time.time()), error="; ".join(errors))
            try:
                with open(CACHE, "w", encoding="utf-8") as f:
                    json.dump({k: STATE[k] for k in ("events", "fetched_at")}, f, ensure_ascii=False)
            except Exception:
                pass
        else:
            STATE.update(error="ดึงปฏิทินข่าวไม่สำเร็จ: " + "; ".join(errors), fetched_at=int(time.time()) - cfg["refresh_min"] * 60 + 300)
    return STATE


def upcoming(now_utc=None, hours_back=2, hours_ahead=24, all_impacts=False):
    cfg = config()
    now_utc = now_utc or int(time.time())
    out = []
    for e in STATE["events"]:
        if e["currency"] not in cfg["currencies"]:
            continue
        if not all_impacts and e["impact"] not in cfg["impacts"]:
            continue
        if now_utc - hours_back * 3600 <= e["time_utc"] <= now_utc + hours_ahead * 3600:
            out.append({**e, "minutes_until": int((e["time_utc"] - now_utc) / 60)})
    return out


def blackout(now_utc=None, manual=False):
    """Return a reason string if trading should be paused because of a news event, else None."""
    cfg = config()
    if not cfg["enabled"] or (manual and not cfg["block_manual"]) or (not manual and not cfg["block_auto"]):
        return None
    now_utc = now_utc or int(time.time())
    for e in STATE["events"]:
        if e["currency"] in cfg["currencies"] and e["impact"] in cfg["block_impacts"]:
            if e["time_utc"] - cfg["block_before_min"] * 60 <= now_utc <= e["time_utc"] + cfg["block_after_min"] * 60:
                m = int((e["time_utc"] - now_utc) / 60)
                when = f"อีก {m} นาที" if m > 0 else f"ผ่านไป {-m} นาที"
                return f"ช่วงข่าว {e['currency']} {e['impact']}: {e['title']} ({when})"
    return None


def next_block(now_utc=None):
    cfg = config()
    now_utc = now_utc or int(time.time())
    for e in STATE["events"]:
        if e["currency"] in cfg["currencies"] and e["impact"] in cfg["block_impacts"] and e["time_utc"] >= now_utc - cfg["block_after_min"] * 60:
            return {**e, "minutes_until": int((e["time_utc"] - now_utc) / 60)}
    return None


# ---------------------------------------------------------------- headlines (RSS)
HEADLINE_DEFAULTS = {"rss": ["https://www.fxstreet.com/rss/news"],
                     "keywords": ["gold", "xau", "fed", "fomc", "powell", "dollar", "usd", "inflation", "cpi",
                                  "payroll", "nfp", "yield", "treasury", "rate", "geopolit", "war", "tariff"]}
HEAD = {"items": [], "fetched_at": 0, "error": ""}


def refresh_headlines(force=False):
    cfg = {**HEADLINE_DEFAULTS, **store.load("news").get("headlines", {})}
    if not config()["enabled"] or (not force and time.time() - HEAD["fetched_at"] < 15 * 60):
        return HEAD
    import xml.etree.ElementTree as ET
    from email.utils import parsedate_to_datetime
    items, errs = [], []
    for url in cfg["rss"]:
        try:
            r = requests.get(url, timeout=15, headers={"User-Agent": "AITraderDashboard/1.0"})
            r.raise_for_status()
            root = ET.fromstring(r.content)
            for it in root.iter("item"):
                title = (it.findtext("title") or "").strip()
                if not title:
                    continue
                try:
                    ts = int(parsedate_to_datetime(it.findtext("pubDate")).timestamp())
                except Exception:
                    ts = 0
                low = title.lower()
                if any(k in low for k in cfg["keywords"]):
                    items.append({"time_utc": ts, "title": title, "link": it.findtext("link") or "", "source": url.split("/")[2]})
        except Exception as e:
            errs.append(f"{url.split('/')[2]}: เชื่อมต่อไม่ได้ ({type(e).__name__})")
    if items:
        items.sort(key=lambda x: -x["time_utc"])
        HEAD.update(items=items[:25], fetched_at=int(time.time()), error="; ".join(errs))
    else:
        HEAD.update(error="; ".join(errs) or "ไม่มีพาดหัวที่เกี่ยวข้อง", fetched_at=int(time.time()))
    return HEAD


def loop():
    """Background thread: keeps calendar + headlines fresh without blocking the trading loop."""
    while True:
        try:
            refresh()
            refresh_headlines()
        except Exception as e:  # noqa
            print("[NEWS]", repr(e))
        time.sleep(60)


# ---------------------------------------------------------------- reading the news result
INVERSE = ("unemployment rate", "unemployment claims", "jobless", "continuing claims", "trade balance deficit")


def _num(s):
    """'3.2%' -> 3.2, '250K' -> 250000, '-1.1B' -> -1.1e9, '<0.1%' -> 0.1"""
    if s is None:
        return None
    t = str(s).strip().replace(",", "").replace("<", "").replace(">", "")
    if not t:
        return None
    mult = 1.0
    for suf, m in (("%", 1), ("K", 1e3), ("M", 1e6), ("B", 1e9), ("T", 1e12)):
        if t.upper().endswith(suf):
            t, mult = t[:-1], m
            break
    try:
        return float(t) * mult
    except ValueError:
        return None


def surprise(e):
    """How the released number compares with the forecast and what it usually means for gold.
    USD better than expected -> USD up -> gold usually down (and vice versa)."""
    a, f, p = _num(e.get("actual")), _num(e.get("forecast")), _num(e.get("previous"))
    if a is None:
        return None
    ref = f if f is not None else p
    if ref is None:
        return None
    diff = a - ref
    if diff == 0:
        return {"direction": "inline", "usd": "เท่าคาด", "gold": "เป็นกลาง", "diff": 0.0}
    inverse = any(k in e.get("title", "").lower() for k in INVERSE)
    good_for_ccy = (diff < 0) if inverse else (diff > 0)
    size = abs(diff) / max(abs(ref), 1e-9) * 100
    out = {"direction": "better" if good_for_ccy else "worse", "diff": round(diff, 4), "size_pct": round(size, 1),
           "vs": "forecast" if f is not None else "previous",
           "usd": f"{e.get('currency')} ดีกว่าคาด" if good_for_ccy else f"{e.get('currency')} แย่กว่าคาด"}
    if e.get("currency") == "USD":
        out["gold"] = "กดดันทอง (Bearish)" if good_for_ccy else "หนุนทอง (Bullish)"
        out["gold_bias"] = "SELL" if good_for_ccy else "BUY"
    else:
        out["gold"] = "ผลต่อทองไม่ตรง (ไม่ใช่ USD)"
    return out


def recent_results(now_utc=None, minutes=120):
    cfg = config()
    now_utc = now_utc or int(time.time())
    out = []
    for e in STATE["events"]:
        if e["currency"] in cfg["currencies"] and e["impact"] in ("High", "Medium") and \
                now_utc - minutes * 60 <= e["time_utc"] <= now_utc and e.get("actual"):
            s = surprise(e)
            if s:
                out.append({**e, "surprise": s, "minutes_ago": int((now_utc - e["time_utc"]) / 60)})
    return out


def news_bias(now_utc=None, minutes=90):
    """Net gold bias from USD news released in the last `minutes` (High counts double)."""
    score, why = 0, []
    for e in recent_results(now_utc, minutes):
        s = e["surprise"]
        if s.get("gold_bias"):
            w = 2 if e["impact"] == "High" else 1
            score += w if s["gold_bias"] == "BUY" else -w
            why.append(f"{e['title']} จริง {e['actual']} vs คาด {e['forecast'] or '-'} → {s['gold']}")
    if not why:
        return None
    return {"bias": "BUY" if score > 0 else "SELL" if score < 0 else "MIXED", "score": score, "events": why}
