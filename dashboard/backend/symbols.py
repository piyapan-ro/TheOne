"""
Extra symbols (besides the main gold symbol in Settings), e.g. BTCUSD.

Each extra symbol has its own lanes "<SYMBOL>:<STRATEGY>" (order comment AIT:<SYMBOL>:<STRATEGY>) with
mode off / shadow / live, its own max spread and a cap on the open risk of that symbol. Account-level
rules (Kill Switch, daily loss, total open risk, max positions) are shared with gold.
"""
import store

STRATS = ("SMC", "SND", "EMA", "BB", "AIPB", "LSR", "ASIA", "ORB")
DEFAULT = {
    "enabled": False,
    "timeframe": "M5",
    "max_spread": 30.0,      # price units ($); BTC spreads are ~10-20, wider at weekends
    "max_risk_pct": 6.0,     # open SL risk of this symbol, % of equity
    # session strategies (ASIA / ORB) are built around gold market hours -> off for 24/7 crypto by default
    "modes": {"SMC": "shadow", "SND": "shadow", "EMA": "shadow", "BB": "shadow", "AIPB": "shadow",
              "LSR": "shadow", "ASIA": "off", "ORB": "off"},
    "params": {},            # per-strategy parameter overrides for this symbol (defaults otherwise)
}
MODES = ("off", "shadow", "live")


def _merged(cfg):
    out = {**DEFAULT, **(cfg or {})}
    out["modes"] = {**DEFAULT["modes"], **((cfg or {}).get("modes") or {})}
    out["modes"] = {k: (v if v in MODES else "off") for k, v in out["modes"].items() if k in STRATS}
    return out


def all_cfg():
    return {name: _merged(cfg) for name, cfg in store.load("symbols").items()}


def enabled():
    return {name: cfg for name, cfg in all_cfg().items() if cfg["enabled"]}


def lane_id(symbol, code):
    return f"{symbol}:{code}"


def update(name, patch):
    name = str(name).upper().strip()

    def fn(d):
        cur = _merged(d.get(name))
        for k in ("enabled", "timeframe", "max_spread", "max_risk_pct"):
            if k in patch:
                cur[k] = patch[k]
        if isinstance(patch.get("modes"), dict):
            cur["modes"].update({k: v for k, v in patch["modes"].items() if k in STRATS and v in MODES})
        cur["enabled"] = bool(cur["enabled"])
        cur["max_spread"] = float(cur["max_spread"] or 0)
        cur["max_risk_pct"] = float(cur["max_risk_pct"] or 0)
        d[name] = cur
    return _merged(store.modify("symbols", fn)[name])
