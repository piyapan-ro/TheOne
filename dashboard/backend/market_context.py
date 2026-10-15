"""
Rich market context for the AI: what a discretionary gold trader looks at before a trade.

- Multi-timeframe trend & structure (D1 / H4 / H1 / M15 / execution TF)
- Key levels: previous day & week high/low/close, today's open, Asian range,
  H1 swing support/resistance clusters, round numbers
- Session (Asia / London / New York) and volatility regime (ATR percentile, today's range vs ADR)
- Recent candles (price action) on the execution TF and H1
- Live context: open positions, recent closed trades, strategy edge from the Optimizer
"""
import numpy as np

import strategies as st

DAY = 86400


def _r(x, d=2):
    return None if x is None or (isinstance(x, float) and np.isnan(x)) else round(float(x), d)


def structure(df, k=3, n=4):
    """Last swing highs/lows and the resulting market structure label."""
    sh, sl = st.swings(df, k)
    hs = [(int(df["time"].iat[i]), float(df["high"].iat[i])) for i in np.where(sh)[0]][-n:]
    ls = [(int(df["time"].iat[i]), float(df["low"].iat[i])) for i in np.where(sl)[0]][-n:]
    label = "RANGE"
    if len(hs) >= 2 and len(ls) >= 2:
        hh, hl = hs[-1][1] > hs[-2][1], ls[-1][1] > ls[-2][1]
        lh, ll = hs[-1][1] < hs[-2][1], ls[-1][1] < ls[-2][1]
        label = "BULLISH (HH+HL)" if hh and hl else "BEARISH (LH+LL)" if lh and ll else \
                "EXPANDING" if hh and ll else "CONTRACTING" if lh and hl else "RANGE"
    return {"label": label, "swing_highs": [_r(h) for _, h in hs], "swing_lows": [_r(l) for _, l in ls]}


def tf_summary(df):
    df = st.add_indicators(df)
    r = df.iloc[-2]  # last closed bar
    atr_hist = df["atr"].tail(200)
    stack = ("UP_STRONG" if r.ema20 > r.ema50 > r.ema200 else "DOWN_STRONG" if r.ema20 < r.ema50 < r.ema200 else
             "UP" if r.ema50 > r.ema200 else "DOWN")
    slope = (df["ema50"].iat[-2] - df["ema50"].iat[-12]) / max(r.atr, 1e-9)
    return {
        "close": _r(r.close), "ema20": _r(r.ema20), "ema50": _r(r.ema50), "ema200": _r(r.ema200),
        "rsi": _r(r.rsi, 1), "atr": _r(r.atr), "atr_percentile": int((atr_hist < r.atr).mean() * 100),
        "trend": "UP" if r.ema50 > r.ema200 else "DOWN", "ema_stack": stack, "ema50_slope_atr": _r(slope),
        "dist_ema50_atr": _r((r.close - r.ema50) / max(r.atr, 1e-9)),
        "bb_position": _r((r.close - r.bb_lo) / max(r.bb_up - r.bb_lo, 1e-9)),
        "high_50": _r(df.high.tail(50).max()), "low_50": _r(df.low.tail(50).min()),
        "structure": structure(df.iloc[:-1]),
    }, df


def sr_clusters(df, atr, price, k=3, tol=0.35):
    """Group H1 swing highs/lows into support/resistance zones; more touches = stronger."""
    sh, sl = st.swings(df, k)
    pts = list(df["high"].values[sh]) + list(df["low"].values[sl])
    pts.sort()
    zones = []
    for p in pts:
        if zones and p - zones[-1]["hi"] <= tol * atr:
            z = zones[-1]
            z["hi"], z["n"] = p, z["n"] + 1
        else:
            zones.append({"lo": p, "hi": p, "n": 1})
    out = [{"zone": [_r(z["lo"]), _r(z["hi"])], "touches": z["n"], "mid": (z["lo"] + z["hi"]) / 2} for z in zones]
    above = sorted([z for z in out if z["mid"] > price], key=lambda z: z["mid"])[:3]
    below = sorted([z for z in out if z["mid"] <= price], key=lambda z: -z["mid"])[:3]
    for z in above + below:
        z["distance_atr"] = _r((z["mid"] - price) / max(atr, 1e-9))
        z.pop("mid")
    return {"resistance": above, "support": below}


def session_name(utc_hour):
    if 0 <= utc_hour < 7:
        return "ASIA"
    if 7 <= utc_hour < 12:
        return "LONDON"
    if 12 <= utc_hour < 16:
        return "LONDON/NEW YORK OVERLAP (ผันผวนสูง ข่าว US มักออกช่วงนี้)"
    if 16 <= utc_hour < 21:
        return "NEW YORK"
    return "LATE NY / ROLLOVER (สภาพคล่องต่ำ spread กว้าง)"


def build(broker, symbol, tf, server_now, utc_offset, asian, extras=None):
    tick = broker.tick(symbol)
    out = {"symbol": symbol, "timeframe": tf, "tick": tick, "server_time": server_now}
    frames = {}
    for t in ("D1", "H4", "H1", "M15", tf):
        if t in frames:
            continue
        df = broker.rates(symbol, t, 400)
        if len(df) < (15 if t == "D1" else 60):
            continue
        out[t], frames[t] = tf_summary(df)
    if tf not in out:
        return out
    a = out[tf]
    price = tick["bid"] if tick else a["close"]

    # --- key levels
    lv = {}
    d1 = frames.get("D1")
    if d1 is not None and len(d1) > 3:
        prev, today = d1.iloc[-2], d1.iloc[-1]
        lv.update(prev_day_high=_r(prev.high), prev_day_low=_r(prev.low), prev_day_close=_r(prev.close),
                  today_open=_r(today.open), today_high=_r(today.high), today_low=_r(today.low))
        adr = float((d1["high"] - d1["low"]).iloc[-15:-1].mean())
        lv["adr14"] = _r(adr)
        lv["today_range_vs_adr_pct"] = int((today.high - today.low) / max(adr, 1e-9) * 100)
        wk = (d1["time"] // DAY + 3) // 7
        cur_w = wk.iat[-1]
        tw, lw = d1[wk == cur_w], d1[wk == cur_w - 1]
        if len(tw):
            lv.update(week_high=_r(tw.high.max()), week_low=_r(tw.low.min()))
        if len(lw):
            lv.update(last_week_high=_r(lw.high.max()), last_week_low=_r(lw.low.min()))
    m15 = frames.get("M15")
    if m15 is not None:
        day0 = server_now // DAY * DAY
        hrs = (m15["time"] - day0) // 3600
        asia = m15[(m15["time"] >= day0) & (hrs >= asian.get("start_hour", 1)) & (hrs < asian.get("end_hour", 8))]
        if len(asia):
            lv["asian_high"], lv["asian_low"] = _r(asia.high.max()), _r(asia.low.min())
    base = round(price / 10) * 10
    lv["round_numbers"] = sorted({base - 20, base - 10, base, base + 10, base + 20, round(price / 50) * 50})
    out["levels"] = lv
    h1 = frames.get("H1")
    if h1 is not None:
        out["sr_zones_h1"] = sr_clusters(h1.iloc[:-1], out["H1"]["atr"], price)

    # --- time & session
    utc_hour = int(((server_now - utc_offset) // 3600) % 24)
    out["session"] = {"server_hour": int((server_now // 3600) % 24), "utc_hour": utc_hour, "name": session_name(utc_hour),
                      "weekday": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][int((server_now // DAY + 3) % 7)]}

    # --- recent price action
    ex = frames[tf].iloc[-13:-1]
    out["last_candles_" + tf] = [[_r(r.open), _r(r.high), _r(r.low), _r(r.close)] for r in ex.itertuples()]
    if h1 is not None:
        out["last_candles_H1"] = [[_r(r.open), _r(r.high), _r(r.low), _r(r.close)] for r in h1.iloc[-7:-1].itertuples()]

    # --- MTF alignment summary
    trends = {t: out[t]["trend"] for t in ("D1", "H4", "H1", "M15", tf) if t in out}
    ups = sum(1 for v in trends.values() if v == "UP")
    out["mtf_alignment"] = {"trends": trends, "up_count": ups, "down_count": len(trends) - ups}
    out["not_available"] = ["Order flow / volume จริง (มีแค่ tick volume)"]
    if extras:
        out.update(extras)
    return out
