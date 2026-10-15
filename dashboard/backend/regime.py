"""
Market regime: what kind of market is gold in right now?

Uses only closed bars of an OHLC frame with indicators (strategies.add_indicators):
- Efficiency ratio (net move / total path over 20 bars): high = trending, low = choppy
- EMA50 slope in ATRs and EMA stack: direction and strength of the trend
- ATR percentile + Bollinger width percentile: volatility expansion / contraction
- Last bar range vs ATR: news-type spike
"""
import numpy as np

LABELS = {
    "TREND_UP": "เทรนด์ขาขึ้น", "TREND_DOWN": "เทรนด์ขาลง", "RANGE": "ไซด์เวย์ / กรอบ",
    "VOLATILE": "ผันผวนสูง / เบรกเอาต์", "SPIKE": "ราคากระชาก (ข่าว/เหตุการณ์)", "QUIET": "เงียบ / สภาพคล่องต่ำ",
    "PULLBACK_UP": "ขาขึ้นหลัก กำลังย่อตัว", "PULLBACK_DOWN": "ขาลงหลัก กำลังดีดกลับ",
}
# which strategies fit each regime (used by the regime filter for Auto-Trade)
FIT = {
    "TREND_UP": ["EMA", "BB", "SMC", "AIPB", "SND"],
    "TREND_DOWN": ["EMA", "BB", "SMC", "AIPB", "SND"],
    "RANGE": ["SND", "SMC"],
    "VOLATILE": ["SMC", "ASIA", "AIPB", "AI"],
    "SPIKE": ["AI"],
    "QUIET": ["ASIA"],
    "PULLBACK_UP": ["EMA", "BB", "SND", "SMC", "AIPB"],
    "PULLBACK_DOWN": ["EMA", "BB", "SND", "SMC", "AIPB"],
}
ADVICE = {
    "TREND_UP": "เทรดตามเทรนด์: รอย่อเข้าโซน EMA/Demand แล้ว BUY · อย่าสวนเทรนด์",
    "TREND_DOWN": "เทรดตามเทรนด์: รอดีดเข้าโซน EMA/Supply แล้ว SELL · อย่าสวนเทรนด์",
    "RANGE": "เทรดขอบกรอบ: ขายใกล้ต้าน ซื้อใกล้รับ · TP สั้น · ระวังเบรกหลอก",
    "VOLATILE": "ความผันผวนสูง: ใช้ SL กว้างขึ้น/ลด lot · เล่นเบรกเอาต์ที่ปิดยืนยัน",
    "SPIKE": "ราคากระชากแรง: อย่าไล่ราคาแท่งแรก รอแท่งยืนยันหรือรีเทสต์ · ใช้ผลข่าวจริงเทียบคาดการณ์ช่วยตัดสิน",
    "QUIET": "ตลาดเงียบ: สัญญาณหลอกเยอะ ลดการเทรด หรือรอเบรกกรอบ",
    "PULLBACK_UP": "เทรนด์หลักยังขึ้น (EMA50>200) แต่กำลังย่อ: รอให้ย่อจบที่โซนรับ/EMA แล้วค่อย BUY · อย่ารีบ SELL ตามแรงย่อ",
    "PULLBACK_DOWN": "เทรนด์หลักยังลง (EMA50<200) แต่กำลังดีด: รอให้ดีดจบที่โซนต้าน/EMA แล้วค่อย SELL · อย่ารีบ BUY ตามแรงดีด",
}


def _pct_rank(series, value):
    s = series.dropna()
    return int((s < value).mean() * 100) if len(s) else 50


def classify(df):
    d = df.iloc[:-1]  # closed bars only
    if len(d) < 220:
        return {"regime": "RANGE", "label": LABELS["RANGE"], "note": "ข้อมูลไม่พอ"}
    c = d["close"].values
    r = d.iloc[-1]
    net = abs(c[-1] - c[-21])
    path = np.abs(np.diff(c[-21:])).sum()
    er = float(net / path) if path > 0 else 0.0
    slope = float((d["ema50"].iat[-1] - d["ema50"].iat[-11]) / max(r.atr, 1e-9))
    atr_pct = _pct_rank(d["atr"].tail(300), r.atr)
    bbw = (d["bb_up"] - d["bb_lo"]) / d["bb_mid"]
    bbw_pct = _pct_rank(bbw.tail(300), bbw.iat[-1])
    last_rng = float((r.high - r.low) / max(r.atr, 1e-9))
    max_rng3 = float(((d["high"] - d["low"]).tail(3) / d["atr"].tail(3)).max())
    up_stack = r.ema20 > r.ema50 > r.ema200
    dn_stack = r.ema20 < r.ema50 < r.ema200

    major_up = r.ema50 > r.ema200
    with_major = (slope > 0) == major_up  # short-term move agrees with the main trend?
    if max_rng3 >= 3.0:
        reg = "SPIKE"
    elif er >= 0.35 and abs(slope) >= 0.8 and ((up_stack and slope > 0) or (dn_stack and slope < 0)):
        reg = "TREND_UP" if slope > 0 else "TREND_DOWN"
    elif atr_pct >= 80 and bbw_pct >= 70:
        reg = "VOLATILE"
    elif atr_pct <= 15 and bbw_pct <= 20:
        reg = "QUIET"
    elif abs(slope) >= 0.6 and with_major:
        reg = "TREND_UP" if slope > 0 else "TREND_DOWN"
    elif abs(slope) >= 0.4 and not with_major:
        reg = "PULLBACK_UP" if major_up else "PULLBACK_DOWN"
    else:
        reg = "RANGE"
    strength = int(min(100, er * 100 * 0.6 + min(abs(slope), 3) / 3 * 40))
    return {"regime": reg, "label": LABELS[reg], "advice": ADVICE[reg], "fit_strategies": FIT[reg],
            "efficiency": round(er, 2), "ema50_slope_atr": round(slope, 2), "trend_strength": strength,
            "atr_percentile": atr_pct, "bb_width_percentile": bbw_pct, "last_bar_range_atr": round(last_rng, 2)}
