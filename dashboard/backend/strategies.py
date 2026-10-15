"""
Six signal strategies + a simple bar-by-bar backtester.

Every strategy takes an OHLC DataFrame (time, open, high, low, close, tick_volume)
and returns a list of signals:
    {"time", "index", "strategy", "side", "entry", "sl", "tp", "reason"}
Signals are only produced on CLOSED bars (the last, still-forming bar is ignored).
"""
import numpy as np
import pandas as pd

STRATEGIES = {
    "SMC": "SMC Liquidity Sweep + BOS",
    "SND": "Supply / Demand Retest",
    "EMA": "EMA Trend Pullback",
    "BB": "Bollinger Bands Pullback",
    "ASIA": "Asian Range Breakout",
    "AIPB": "AI Playbook (Confluence)",
}

# Default parameters (used until the Optimizer proposes and you apply something else)
DEFAULT_PARAMS = {
    "SMC": {"rr": 2.0, "swing_k": 3, "bos_window": 12, "sl_buffer_atr": 0.2},
    "SND": {"rr": 2.0, "impulse_atr": 1.6, "max_age": 150, "sl_buffer_atr": 0.2},
    "EMA": {"rr": 2.0, "pullback_ema": 20, "rsi_filter": 45, "sl_buffer_atr": 0.3, "cooldown": 6},
    "BB": {"rr": 2.0, "bb_period": 20, "bb_k": 2.0, "sl_buffer_atr": 0.2, "cooldown": 6},
    "ASIA": {"rr": 1.5, "trade_until_hour": 16, "max_range_atr": 8.0},
    "AIPB": {"rr": 2.0, "threshold": 65, "sl_atr": 1.5, "cooldown": 10},
}

# Search space for the walk-forward optimizer
PARAM_GRID = {
    "SMC": {"rr": [1.5, 2.0, 2.5, 3.0], "swing_k": [2, 3, 5], "bos_window": [6, 12, 20], "sl_buffer_atr": [0.1, 0.3, 0.6]},
    "SND": {"rr": [1.5, 2.0, 2.5, 3.0], "impulse_atr": [1.2, 1.6, 2.0], "max_age": [60, 150, 300], "sl_buffer_atr": [0.1, 0.3, 0.6]},
    "EMA": {"rr": [1.5, 2.0, 2.5, 3.0], "pullback_ema": [20, 50], "rsi_filter": [40, 45, 50], "sl_buffer_atr": [0.2, 0.5],
            "cooldown": [3, 6, 12]},
    "BB": {"rr": [1.5, 2.0, 2.5, 3.0], "bb_period": [20, 30], "bb_k": [2.0, 2.5], "sl_buffer_atr": [0.1, 0.3],
           "cooldown": [3, 6, 12]},
    "ASIA": {"rr": [1.0, 1.5, 2.0, 2.5], "trade_until_hour": [12, 16, 20], "max_range_atr": [4.0, 8.0, 12.0]},
    "AIPB": {"rr": [1.5, 2.0, 2.5, 3.0], "threshold": [50, 60, 70, 80], "sl_atr": [1.0, 1.5, 2.0], "cooldown": [5, 10, 20]},
}

PARAM_LABELS = {
    "rr": "R:R", "swing_k": "Swing (แท่ง)", "bos_window": "รอ BOS (แท่ง)", "sl_buffer_atr": "SL buffer (×ATR)",
    "impulse_atr": "Impulse (×ATR)", "max_age": "อายุโซน (แท่ง)", "pullback_ema": "EMA ที่ย่อ", "rsi_filter": "RSI filter",
    "cooldown": "พัก (แท่ง)", "bb_period": "BB period", "bb_k": "BB SD", "trade_until_hour": "เทรดถึง (ชม.)",
    "max_range_atr": "กรอบสูงสุด (×ATR)", "threshold": "Score ขั้นต่ำ", "sl_atr": "SL (×ATR)",
}


def merged_params(code, override=None):
    p = dict(DEFAULT_PARAMS[code])
    if override:
        p.update({k: v for k, v in override.items() if k in p})
    return p


# ---------------------------------------------------------------- indicators
def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def rsi(s, n=14):
    d = s.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = up / dn.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(50)


def atr(df, n=14):
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def bollinger(s, n=20, k=2.0):
    mid = s.rolling(n).mean()
    sd = s.rolling(n).std(ddof=0)
    return mid, mid + k * sd, mid - k * sd


def add_indicators(df):
    df = df.copy()
    df["ema20"] = ema(df["close"], 20)
    df["ema50"] = ema(df["close"], 50)
    df["ema200"] = ema(df["close"], 200)
    df["rsi"] = rsi(df["close"], 14)
    df["atr"] = atr(df, 14)
    df["bb_mid"], df["bb_up"], df["bb_lo"] = bollinger(df["close"])
    return df


def _sig(df, i, code, side, entry, sl, rr, reason):
    risk = abs(entry - sl)
    if risk <= 0:
        return None
    tp = entry + risk * rr if side == "BUY" else entry - risk * rr
    return {"time": int(df["time"].iat[i]), "index": int(i), "strategy": code, "side": side,
            "entry": round(float(entry), 2), "sl": round(float(sl), 2), "tp": round(float(tp), 2),
            "reason": reason}


def swings(df, k=3):
    """Fractal swing highs/lows. A swing at bar i is confirmed at bar i+k."""
    h, l = df["high"].values, df["low"].values
    n = len(df)
    sh = np.zeros(n, bool)
    sl = np.zeros(n, bool)
    for i in range(k, n - k):
        if h[i] == h[i - k:i + k + 1].max():
            sh[i] = True
        if l[i] == l[i - k:i + k + 1].min():
            sl[i] = True
    return sh, sl


# ---------------------------------------------------------------- strategies
def smc_sweep_bos(df, rr=2.0, swing_k=3, bos_window=12, sl_buffer_atr=0.2):
    """Bullish: price sweeps below the last swing low then closes back above it,
    and within `bos_window` bars closes above the most recent swing high (BOS)."""
    out = []
    k = int(swing_k)
    sh, sl_ = swings(df, k)
    h, l, c, a = df["high"].values, df["low"].values, df["close"].values, df["atr"].values
    n = len(df) - 1  # last closed bar index = n-1
    last_sh = last_sl = None
    pending = None  # (side, bar, sweep_extreme, bos_level)
    for i in range(k, n):
        j = i - k  # swing confirmed now
        if sh[j]:
            last_sh = h[j]
        if sl_[j]:
            last_sl = l[j]
        if last_sl is not None and l[i] < last_sl and c[i] > last_sl and last_sh is not None:
            pending = ("BUY", i, l[i], last_sh)
        elif last_sh is not None and h[i] > last_sh and c[i] < last_sh and last_sl is not None:
            pending = ("SELL", i, h[i], last_sl)
        if pending and i > pending[1]:
            side, b, ext, lvl = pending
            if i - b > bos_window:
                pending = None
            elif side == "BUY" and c[i] > lvl:
                s = _sig(df, i, "SMC", "BUY", c[i], ext - sl_buffer_atr * a[i], rr,
                         f"Sweep SSL {ext:.2f} แล้ว BOS เหนือ {lvl:.2f}")
                if s:
                    out.append(s)
                pending = None
            elif side == "SELL" and c[i] < lvl:
                s = _sig(df, i, "SMC", "SELL", c[i], ext + sl_buffer_atr * a[i], rr,
                         f"Sweep BSL {ext:.2f} แล้ว BOS ใต้ {lvl:.2f}")
                if s:
                    out.append(s)
                pending = None
    return out


def supply_demand(df, rr=2.0, impulse_atr=1.6, max_age=150, sl_buffer_atr=0.2):
    """Zone = base candle before an impulsive candle (body > impulse_atr*ATR).
    Trade the first retest of a fresh zone with a rejection close."""
    out = []
    o, h, l, c, a = (df[x].values for x in ("open", "high", "low", "close", "atr"))
    n = len(df) - 1
    zones = []  # dict(type, lo, hi, born)
    for i in range(2, n):
        body = c[i - 1] - o[i - 1]
        if a[i - 1] > 0 and abs(body) > impulse_atr * a[i - 1]:
            b = i - 2
            if body > 0:
                zones.append({"type": "D", "lo": l[b], "hi": max(o[b], c[b]), "born": i - 1})
            else:
                zones.append({"type": "S", "lo": min(o[b], c[b]), "hi": h[b], "born": i - 1})
        alive = []
        for z in zones:
            if i - z["born"] > max_age:
                continue  # expired
            if i <= z["born"] + 1:
                alive.append(z)  # give the zone at least one bar to form
                continue
            if z["type"] == "D":
                if c[i] < z["lo"]:
                    continue  # broken
                if l[i] <= z["hi"] and c[i] > z["hi"] and c[i] > o[i]:
                    s = _sig(df, i, "SND", "BUY", c[i], z["lo"] - sl_buffer_atr * a[i], rr,
                             f"Retest Demand {z['lo']:.2f}-{z['hi']:.2f}")
                    if s:
                        out.append(s)
                    continue  # zone used
            else:
                if c[i] > z["hi"]:
                    continue
                if h[i] >= z["lo"] and c[i] < z["lo"] and c[i] < o[i]:
                    s = _sig(df, i, "SND", "SELL", c[i], z["hi"] + sl_buffer_atr * a[i], rr,
                             f"Retest Supply {z['lo']:.2f}-{z['hi']:.2f}")
                    if s:
                        out.append(s)
                    continue
            alive.append(z)
        zones = alive[-30:]
    return out


def ema_pullback(df, rr=2.0, pullback_ema=20, rsi_filter=45, sl_buffer_atr=0.3, cooldown=6):
    out = []
    o, h, l, c, a = (df[x].values for x in ("open", "high", "low", "close", "atr"))
    e20, e50, e200, r = df["ema20"].values, df["ema50"].values, df["ema200"].values, df["rsi"].values
    pb = e50 if int(pullback_ema) == 50 else e20
    n = len(df) - 1
    last = -999
    for i in range(205, n):
        if i - last < cooldown:
            continue
        up = e50[i] > e200[i] and e20[i] > e50[i] and e50[i] > e50[i - 5]
        dn = e50[i] < e200[i] and e20[i] < e50[i] and e50[i] < e50[i - 5]
        if up and l[i] <= pb[i] and c[i] > pb[i] and c[i] > o[i] and r[i] > rsi_filter:
            sl = min(l[i - 4:i + 1].min(), e50[i]) - sl_buffer_atr * a[i]
            s = _sig(df, i, "EMA", "BUY", c[i], sl, rr, f"Uptrend (EMA20>50>200) ย่อแตะ EMA{int(pullback_ema)} แล้วเด้ง")
            if s:
                out.append(s); last = i
        elif dn and h[i] >= pb[i] and c[i] < pb[i] and c[i] < o[i] and r[i] < 100 - rsi_filter:
            sl = max(h[i - 4:i + 1].max(), e50[i]) + sl_buffer_atr * a[i]
            s = _sig(df, i, "EMA", "SELL", c[i], sl, rr, f"Downtrend (EMA20<50<200) ดีดแตะ EMA{int(pullback_ema)} แล้วลง")
            if s:
                out.append(s); last = i
    return out


def bb_pullback(df, rr=2.0, bb_period=20, bb_k=2.0, sl_buffer_atr=0.2, cooldown=6):
    out = []
    o, h, l, c, a = (df[x].values for x in ("open", "high", "low", "close", "atr"))
    if int(bb_period) == 20 and float(bb_k) == 2.0 and "bb_mid" in df:
        mid, up_, lo_ = df["bb_mid"].values, df["bb_up"].values, df["bb_lo"].values
    else:
        m, u, lo = bollinger(df["close"], int(bb_period), float(bb_k))
        mid, up_, lo_ = m.values, u.values, lo.values
    e200 = df["ema200"].values
    n = len(df) - 1
    last = -999
    for i in range(205, n):
        if np.isnan(mid[i]) or i - last < cooldown:
            continue
        trend_up = c[i] > e200[i] and mid[i] > mid[i - 5]
        trend_dn = c[i] < e200[i] and mid[i] < mid[i - 5]
        # pullback to the middle band in a trend, close back on trend side
        if trend_up and l[i] <= mid[i] and c[i] > mid[i] and c[i] > o[i]:
            s = _sig(df, i, "BB", "BUY", c[i], min(l[i], lo_[i]) - sl_buffer_atr * a[i], rr,
                     "เทรนด์ขึ้น ย่อแตะ BB Mid แล้วปิดเหนือ")
            if s:
                out.append(s); last = i
        elif trend_dn and h[i] >= mid[i] and c[i] < mid[i] and c[i] < o[i]:
            s = _sig(df, i, "BB", "SELL", c[i], max(h[i], up_[i]) + sl_buffer_atr * a[i], rr,
                     "เทรนด์ลง ดีดแตะ BB Mid แล้วปิดใต้")
            if s:
                out.append(s); last = i
    return out


def asian_breakout(df, rr=1.5, start_hour=1, end_hour=8, trade_until_hour=16, max_range_atr=8.0):
    out = []
    t = df["time"].values
    h, l, c, a = df["high"].values, df["low"].values, df["close"].values, df["atr"].values
    day = t // 86400
    hour = (t // 3600) % 24
    n = len(df) - 1
    rng = {}
    for i in range(n):
        if start_hour <= hour[i] < end_hour:
            d = day[i]
            hi, lo = rng.get(d, (-1e18, 1e18))
            rng[d] = (max(hi, h[i]), min(lo, l[i]))
    done = set()
    for i in range(1, n):
        d = day[i]
        if d not in rng or not (end_hour <= hour[i] < trade_until_hour):
            continue
        hi, lo = rng[d]
        width = hi - lo
        if a[i] <= 0 or width <= 0 or width > max_range_atr * a[i] * 1.5:
            continue
        if (d, "BUY") not in done and c[i] > hi and c[i - 1] <= hi:
            s = _sig(df, i, "ASIA", "BUY", c[i], (hi + lo) / 2, rr, f"Breakout Asian High {hi:.2f} (range {width:.2f})")
            if s:
                out.append(s)
            done.add((d, "BUY"))
        elif (d, "SELL") not in done and c[i] < lo and c[i - 1] >= lo:
            s = _sig(df, i, "ASIA", "SELL", c[i], (hi + lo) / 2, rr, f"Breakout Asian Low {lo:.2f} (range {width:.2f})")
            if s:
                out.append(s)
            done.add((d, "SELL"))
    return out


def confluence_score(df, i, others_by_index):
    """Score -100..+100 combining trend, momentum, structure and other strategies."""
    c, e20, e50, e200 = df["close"].iat[i], df["ema20"].iat[i], df["ema50"].iat[i], df["ema200"].iat[i]
    r, a = df["rsi"].iat[i], df["atr"].iat[i]
    score = 0.0
    why = []
    if e50 > e200:
        score += 25; why.append("EMA50>200")
    else:
        score -= 25; why.append("EMA50<200")
    score += 10 if c > e20 else -10
    if r > 55:
        score += 15; why.append(f"RSI {r:.0f}")
    elif r < 45:
        score -= 15; why.append(f"RSI {r:.0f}")
    if i >= 20:
        mom = (c - df["close"].iat[i - 20]) / max(a, 1e-9)
        score += float(np.clip(mom * 5, -20, 20))
    votes = 0
    for j in range(max(0, i - 5), i + 1):
        for s in others_by_index.get(j, []):
            votes += 1 if s["side"] == "BUY" else -1
            why.append(s["strategy"])
    score += float(np.clip(votes * 15, -30, 30))
    return float(np.clip(score, -100, 100)), why


def ai_playbook(df, others, rr=2.0, threshold=65, sl_atr=1.5, cooldown=10):
    out = []
    by_idx = {}
    for s in others:
        by_idx.setdefault(s["index"], []).append(s)
    n = len(df) - 1
    last = -999
    a = df["atr"].values
    for i in range(205, n):
        if i - last < cooldown:
            continue
        if not any(j in by_idx for j in range(i - 5, i + 1)):
            continue
        score, why = confluence_score(df, i, by_idx)
        c = df["close"].iat[i]
        if score >= threshold:
            s = _sig(df, i, "AIPB", "BUY", c, c - sl_atr * a[i], rr, f"Confluence {score:.0f}: " + ", ".join(dict.fromkeys(why)))
            if s:
                s["score"] = score; out.append(s); last = i
        elif score <= -threshold:
            s = _sig(df, i, "AIPB", "SELL", c, c + sl_atr * a[i], rr, f"Confluence {score:.0f}: " + ", ".join(dict.fromkeys(why)))
            if s:
                s["score"] = score; out.append(s); last = i
    return out


FUNCS = {"SMC": smc_sweep_bos, "SND": supply_demand, "EMA": ema_pullback, "BB": bb_pullback}


def run_one(df, code, params, asian=None, others=None):
    """Run a single strategy on a df that already has indicators."""
    p = merged_params(code, params)
    if code in FUNCS:
        return FUNCS[code](df, **p)
    if code == "ASIA":
        asian = asian or {}
        return asian_breakout(df, start_hour=asian.get("start_hour", 1), end_hour=asian.get("end_hour", 8), **p)
    if code == "AIPB":
        return ai_playbook(df, others or [], **p)
    raise ValueError(code)


def run_all(df, enabled=None, params=None, asian=None):
    """Run every enabled strategy with its parameters. Returns (df_with_indicators, {code: [signals]})."""
    enabled = {k: True for k in STRATEGIES} if enabled is None else enabled
    params = params or {}
    df = add_indicators(df)
    res = {}
    for code in ("SMC", "SND", "EMA", "BB", "ASIA"):
        if enabled.get(code):
            res[code] = run_one(df, code, params.get(code), asian)
    if enabled.get("AIPB"):
        others = [s for k, v in res.items() if k != "ASIA" for s in v]
        for code in ("SMC", "SND", "EMA", "BB"):  # the playbook always needs the other engines as inputs
            if code not in res:
                others += run_one(df, code, params.get(code))
        res["AIPB"] = run_one(df, "AIPB", params.get("AIPB"), others=others)
    return df, res


# ---------------------------------------------------------------- backtest
def backtest(df, signals, max_bars=200, cost=0.0):
    """Walk forward bar by bar: first touch of SL or TP wins (SL assumed first if both).
    `cost` = round-trip cost in price units (spread + slippage); it is deducted from every trade in R."""
    h, l, c = df["high"].values, df["low"].values, df["close"].values
    n = len(df)
    trades = []
    for s in signals:
        i = s["index"]
        res, r_mult, exit_i = "open", 0.0, None
        risk = abs(s["entry"] - s["sl"])
        for j in range(i + 1, min(n, i + 1 + max_bars)):
            if s["side"] == "BUY":
                if l[j] <= s["sl"]:
                    res, r_mult, exit_i = "loss", -1.0, j; break
                if h[j] >= s["tp"]:
                    res, r_mult, exit_i = "win", abs(s["tp"] - s["entry"]) / risk, j; break
            else:
                if h[j] >= s["sl"]:
                    res, r_mult, exit_i = "loss", -1.0, j; break
                if l[j] <= s["tp"]:
                    res, r_mult, exit_i = "win", abs(s["tp"] - s["entry"]) / risk, j; break
        if res == "open" and exit_i is None and i + max_bars < n:
            exit_i = i + max_bars
            move = (c[exit_i] - s["entry"]) * (1 if s["side"] == "BUY" else -1)
            res, r_mult = ("win" if move > 0 else "loss"), move / risk
        if res != "open" and cost:
            r_mult -= cost / risk
            res = "win" if r_mult > 0 else "loss"
        trades.append({**s, "result": res, "r": round(r_mult, 3),
                       "exit_time": int(df["time"].iat[exit_i]) if exit_i is not None else None})
    closed = [t for t in trades if t["result"] != "open"]
    wins = [t for t in closed if t["r"] > 0]
    gross_w = sum(t["r"] for t in wins)
    gross_l = -sum(t["r"] for t in closed if t["r"] <= 0)
    eq, peak, mdd = 0.0, 0.0, 0.0
    for t in closed:
        eq += t["r"]; peak = max(peak, eq); mdd = max(mdd, peak - eq)
    return {
        "signals": len(trades), "closed": len(closed), "wins": len(wins),
        "win_rate": round(len(wins) / len(closed) * 100, 1) if closed else 0.0,
        "total_r": round(eq, 2),
        "profit_factor": round(gross_w / gross_l, 2) if gross_l > 0 else (None if not wins else 99.0),
        "expectancy_r": round(eq / len(closed), 3) if closed else 0.0,
        "max_dd_r": round(mdd, 2),
        "trades": trades,
    }
