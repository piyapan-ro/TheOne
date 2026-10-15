"""
AI Scalper - model trainer for MT5 (XAUUSD M1)
================================================
Trains a random-forest classifier that predicts, for each closed M1 bar,
whether a BUY or a SELL with ATR-based SL/TP would hit TP before SL within
N bars. Exports the model to ONNX for the AIScalper_M1.mq5 EA.
Copy AIScalper.onnx into  <MT5 data folder>\\MQL5\\Files\\  (File > Open Data Folder).

Data source (pick one):
  1) MT5 CSV export:   python train_ai_scalper.py --csv XAUUSD_M1.csv
     (MT5: View > Symbols > Bars > XAUUSD M1 > Request > Export Bars)
  2) Live terminal:    python train_ai_scalper.py --mt5 --symbol XAUUSD --bars 300000
     (Windows only, needs `pip install MetaTrader5` and MT5 open + logged in)

Requirements:  pip install numpy pandas scikit-learn skl2onnx onnxruntime

IMPORTANT: features here are mirrored 1:1 in the EA (ComputeFeatures).
If you change anything in `compute_features`, change the EA too.
"""
import argparse
import json
import math
import sys

import numpy as np
import pandas as pd

FEATURE_NAMES = [
    "ret1", "ret3", "ret5", "ret10", "ret20",
    "dist_ema20", "dist_ema50", "ema_spread", "ema20_slope",
    "rsi", "rsi_chg3",
    "atr_ratio",
    "body", "upper_wick", "lower_wick",
    "range_pos20", "range_pos60",
    "vol_ratio",
    "hour_sin", "hour_cos",
    "atr_rel",
]
NF = len(FEATURE_NAMES)
WARMUP = 300  # bars dropped at start so recursive indicators converge (EA uses 600)


# ----------------------------------------------------------------------------
# Data loading
# ----------------------------------------------------------------------------
def load_csv(path):
    df = pd.read_csv(path, sep=None, engine="python")
    df.columns = [c.strip("<>").strip().lower() for c in df.columns]
    if "date" in df.columns and "time" in df.columns:
        dt = pd.to_datetime(df["date"].astype(str) + " " + df["time"].astype(str),
                            format="mixed")
    elif "time" in df.columns:
        dt = pd.to_datetime(df["time"])
    else:
        sys.exit("CSV needs <DATE>,<TIME> or time column")
    out = pd.DataFrame({
        "time": dt,
        "open": df["open"].astype(float),
        "high": df["high"].astype(float),
        "low": df["low"].astype(float),
        "close": df["close"].astype(float),
        "tickvol": df.get("tickvol", df.get("tick_volume", pd.Series(1, index=df.index))).astype(float),
        "spread": df.get("spread", pd.Series(np.nan, index=df.index)).astype(float),
    })
    return out.sort_values("time").reset_index(drop=True)


def load_mt5(symbol, bars):
    import MetaTrader5 as mt5
    if not mt5.initialize():
        sys.exit(f"MT5 initialize failed: {mt5.last_error()}")
    info = mt5.symbol_info(symbol)
    if info is None:
        sys.exit(f"Symbol {symbol} not found (check broker suffix, e.g. XAUUSD. or GOLD)")
    mt5.symbol_select(symbol, True)
    rates = mt5.copy_rates_from_pos(symbol, mt5.TIMEFRAME_M1, 1, bars)
    mt5.shutdown()
    df = pd.DataFrame(rates)
    df["time"] = pd.to_datetime(df["time"], unit="s")  # broker server time, same as TimeCurrent()
    df = df.rename(columns={"tick_volume": "tickvol"})
    return df[["time", "open", "high", "low", "close", "tickvol", "spread"]], info.point


# ----------------------------------------------------------------------------
# Indicators (exact recursions mirrored in the EA)
# ----------------------------------------------------------------------------
def ema(x, n):
    a = 2.0 / (n + 1.0)
    out = np.empty_like(x)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = a * x[i] + (1.0 - a) * out[i - 1]
    return out


def rsi_wilder(c, n):
    out = np.full_like(c, 50.0)
    ag = al = 0.0
    for i in range(1, len(c)):
        d = c[i] - c[i - 1]
        g = d if d > 0 else 0.0
        l = -d if d < 0 else 0.0
        ag = (ag * (n - 1) + g) / n
        al = (al * (n - 1) + l) / n
        out[i] = 100.0 if al == 0 else 100.0 - 100.0 / (1.0 + ag / al)
    return out


def true_range(h, l, c):
    tr = h - l
    pc = np.roll(c, 1)
    tr[1:] = np.maximum.reduce([h[1:] - l[1:], np.abs(h[1:] - pc[1:]), np.abs(l[1:] - pc[1:])])
    return tr


def sma(x, n):
    return pd.Series(x).rolling(n, min_periods=1).mean().to_numpy()


def compute_features(df):
    o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close"))
    v = df["tickvol"].to_numpy(float)
    e20, e50 = ema(c, 20), ema(c, 50)
    r = rsi_wilder(c, 14)
    tr = true_range(h, l, c)
    atr = sma(tr, 14)
    atr100 = sma(tr, 100)
    A = np.where(atr > 0, atr, np.nan)

    def lag(x, k):
        y = np.roll(x, k)
        y[:k] = np.nan
        return y

    def range_pos(n):
        hh = pd.Series(h).rolling(n, min_periods=1).max().to_numpy()
        ll = pd.Series(l).rolling(n, min_periods=1).min().to_numpy()
        rng = hh - ll
        return np.where(rng > 0, (c - ll) / np.where(rng > 0, rng, 1), 0.5)

    vmean = sma(v, 20)
    hrs = df["time"].dt.hour.to_numpy() + df["time"].dt.minute.to_numpy() / 60.0

    F = np.column_stack([
        (c - lag(c, 1)) / A, (c - lag(c, 3)) / A, (c - lag(c, 5)) / A,
        (c - lag(c, 10)) / A, (c - lag(c, 20)) / A,
        (c - e20) / A, (c - e50) / A, (e20 - e50) / A, (e20 - lag(e20, 5)) / A,
        r / 100.0, (r - lag(r, 3)) / 100.0,
        np.where(atr100 > 0, atr / np.where(atr100 > 0, atr100, 1), 1.0),
        (c - o) / A, (h - np.maximum(o, c)) / A, (np.minimum(o, c) - l) / A,
        range_pos(20), range_pos(60),
        np.where(vmean > 0, v / np.where(vmean > 0, vmean, 1), 1.0),
        np.sin(2 * math.pi * hrs / 24.0), np.cos(2 * math.pi * hrs / 24.0),
        atr / c * 10000.0,
    ]).astype(np.float32)
    return F, atr


# ----------------------------------------------------------------------------
# Labels: does BUY / SELL hit TP before SL within `horizon` bars?
# ----------------------------------------------------------------------------
def make_labels(df, atr, sl_mult, tp_mult, horizon, spread_price):
    h, l, c = (df[k].to_numpy(float) for k in ("high", "low", "close"))
    n = len(c)
    s = spread_price
    slD, tpD = atr * sl_mult, atr * tp_mult

    # BUY: enter at ask = close+spread, exits trigger on bid (bars are bid)
    b_entry = c + s
    b_sl, b_tp = b_entry - slD, b_entry + tpD
    # SELL: enter at bid = close, exits trigger on ask = bid+spread
    s_entry = c
    s_sl, s_tp = s_entry + slD, s_entry - tpD

    INF = horizon + 1
    b_win_t = np.full(n, INF); b_loss_t = np.full(n, INF)
    s_win_t = np.full(n, INF); s_loss_t = np.full(n, INF)
    for k in range(1, horizon + 1):
        hk = np.roll(h, -k); lk = np.roll(l, -k)
        valid = np.arange(n) + k < n
        m = valid & (b_loss_t == INF) & (lk <= b_sl); b_loss_t[m & (b_win_t == INF)] = k
        m = valid & (b_win_t == INF) & (b_loss_t == INF) & (hk >= b_tp); b_win_t[m] = k
        m = valid & (s_loss_t == INF) & (hk + s >= s_sl); s_loss_t[m & (s_win_t == INF)] = k
        m = valid & (s_win_t == INF) & (s_loss_t == INF) & (lk + s <= s_tp); s_win_t[m] = k

    buy_win = b_win_t < b_loss_t
    sell_win = s_win_t < s_loss_t

    # realised R (timeouts closed at horizon close)
    ch = np.roll(c, -horizon)
    rr = tp_mult / sl_mult
    R_buy = np.where(buy_win, rr, np.where(b_loss_t <= horizon, -1.0, (ch - b_entry) / slD))
    R_sell = np.where(sell_win, rr, np.where(s_loss_t <= horizon, -1.0, (s_entry - (ch + s)) / slD))

    y = np.zeros(n, dtype=np.int64)
    y[buy_win & ~sell_win] = 1
    y[sell_win & ~buy_win] = 2
    both = buy_win & sell_win
    y[both & (b_win_t <= s_win_t)] = 1
    y[both & (s_win_t < b_win_t)] = 2
    y[-horizon:] = -1  # unknown future
    return y, R_buy, R_sell


def evaluate(proba, R_buy, R_sell, th):
    pb, ps = proba[:, 1], proba[:, 2]
    buy = (pb >= th) & (pb > ps)
    sell = (ps >= th) & (ps > pb)
    R = np.concatenate([R_buy[buy], R_sell[sell]])
    if len(R) == 0:
        return dict(th=th, trades=0, winrate=0.0, expR=0.0, totalR=0.0)
    return dict(th=round(th, 2), trades=int(len(R)), winrate=float((R > 0).mean()),
                expR=float(R.mean()), totalR=float(R.sum()))


# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv")
    ap.add_argument("--mt5", action="store_true")
    ap.add_argument("--symbol", default="XAUUSD")
    ap.add_argument("--bars", type=int, default=300000)
    ap.add_argument("--point", type=float, default=0.01, help="symbol point (CSV mode)")
    ap.add_argument("--spread", type=float, default=0.0,
                    help="spread in PRICE ($) if CSV has none, e.g. 0.30")
    ap.add_argument("--sl", type=float, default=1.5, help="SL = ATR x")
    ap.add_argument("--tp", type=float, default=1.5, help="TP = ATR x")
    ap.add_argument("--horizon", type=int, default=30, help="max bars in trade")
    ap.add_argument("--min-trades", type=int, default=100)
    ap.add_argument("--trees", type=int, default=150)
    ap.add_argument("--depth", type=int, default=10)
    ap.add_argument("--out", default="AIScalper.onnx")
    a = ap.parse_args()

    if a.mt5:
        df, point = load_mt5(a.symbol, a.bars)
    elif a.csv:
        df, point = load_csv(a.csv), a.point
    else:
        sys.exit("Use --csv FILE or --mt5")

    if a.spread > 0:
        spread_price = a.spread
    elif df["spread"].notna().any() and df["spread"].median() > 0:
        spread_price = float(df["spread"].median()) * point
    else:
        spread_price = 0.30
    print(f"Bars: {len(df):,}  {df.time.iloc[0]} -> {df.time.iloc[-1]}")
    print(f"Spread used for labels: {spread_price:.3f}  (point={point})")

    F, atr = compute_features(df)
    y, R_buy, R_sell = make_labels(df, atr, a.sl, a.tp, a.horizon, spread_price)

    ok = np.isfinite(F).all(axis=1) & (y >= 0)
    ok[:WARMUP] = False
    X, Y, Rb, Rs = F[ok], y[ok], R_buy[ok], R_sell[ok]
    print(f"Samples: {len(X):,}  class mix none/buy/sell = "
          f"{np.bincount(Y, minlength=3) / len(Y)}")

    # chronological split with an embargo gap (= horizon) to avoid leakage
    n = len(X)
    i1, i2 = int(n * 0.70), int(n * 0.85)
    g = a.horizon
    Xtr, Ytr = X[:i1 - g], Y[:i1 - g]
    Xva, Yva, Rbva, Rsva = X[i1:i2 - g], Y[i1:i2 - g], Rb[i1:i2 - g], Rs[i1:i2 - g]
    Xte, Rbte, Rste = X[i2:], Rb[i2:], Rs[i2:]

    # RandomForest: robust on noisy data and converts cleanly to ONNX on all versions
    from sklearn.ensemble import RandomForestClassifier
    model = RandomForestClassifier(
        n_estimators=a.trees, max_depth=a.depth, min_samples_leaf=300,
        max_features="sqrt", n_jobs=-1, random_state=42)
    print("Training ...")
    model.fit(Xtr, Ytr)
    imp = sorted(zip(FEATURE_NAMES, model.feature_importances_), key=lambda t: -t[1])
    print("Top features:", ", ".join(f"{k}={v:.3f}" for k, v in imp[:8]))

    # pick probability threshold on validation set (best expectancy w/ enough trades)
    pva = model.predict_proba(Xva)
    rows = [evaluate(pva, Rbva, Rsva, th) for th in np.arange(0.40, 0.81, 0.02)]
    print("\nValidation:\n" + pd.DataFrame(rows).to_string(index=False))
    cands = [r for r in rows if r["trades"] >= a.min_trades]
    best = max(cands, key=lambda r: r["expR"]) if cands else max(rows, key=lambda r: r["trades"])
    th = float(best["th"])

    pte = model.predict_proba(Xte)
    test = evaluate(pte, Rbte, Rste, th)
    print(f"\nChosen threshold {th:.2f}")
    print(f"TEST (unseen last 15%): trades={test['trades']}  winrate={test['winrate']:.1%}  "
          f"expectancy={test['expR']:+.3f}R  total={test['totalR']:+.1f}R")
    if test["expR"] <= 0:
        print("WARNING: no positive edge on test data. Do NOT trade this model live.")

    # ---- export ONNX
    from skl2onnx import to_onnx
    onx = to_onnx(model, X[:1], options={id(model): {"zipmap": False}},
                  target_opset={"": 15, "ai.onnx.ml": 3})
    with open(a.out, "wb") as f:
        f.write(onx.SerializeToString())

    import onnxruntime as ort
    sess = ort.InferenceSession(a.out, providers=["CPUExecutionProvider"])
    outs = sess.run(None, {sess.get_inputs()[0].name: Xte[:2000]})
    diff = np.abs(outs[1] - pte[:2000]).max()
    print(f"\nSaved {a.out}  (ONNX vs sklearn max diff {diff:.2e})")
    print("ONNX outputs:", [(o.name, o.shape) for o in sess.get_outputs()])

    # ---- parity file: compare with EA debug prints
    idx = np.where(ok)[0][-20:]
    par = pd.DataFrame(F[idx], columns=FEATURE_NAMES)
    par.insert(0, "time", df.time.iloc[idx].dt.strftime("%Y.%m.%d %H:%M").values)
    par.to_csv("parity_check.csv", index=False, float_format="%.5f")

    settings = {"InpSlAtrMult": a.sl, "InpTpAtrMult": a.tp, "InpMaxBarsInTrade": a.horizon,
                "InpProbThreshold": th, "test": test, "trained_until": str(df.time.iloc[-1])}
    if a.mt5:  # copy model straight into the terminal's MQL5\\Files folder
        try:
            import os, shutil, MetaTrader5 as mt5
            mt5.initialize()
            dst = os.path.join(mt5.terminal_info().data_path, "MQL5", "Files", "AIScalper.onnx")
            mt5.shutdown()
            shutil.copyfile(a.out, dst)
            print(f"Copied model to {dst}")
        except Exception as e:
            print(f"Could not copy to MQL5\\Files automatically ({e}) - copy it manually.")

    with open("AIScalper_settings.json", "w") as f:
        json.dump(settings, f, indent=2)
    print("\nSet these EA inputs:", {k: settings[k] for k in list(settings)[:4]})


if __name__ == "__main__":
    main()
