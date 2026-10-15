"""Small JSON-file settings store (data/*.json) with defaults and thread safety."""
import json
import os
import threading
from copy import deepcopy

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
os.makedirs(DATA_DIR, exist_ok=True)

DEFAULTS = {
    "settings": {
        "symbol": "XAUUSD",            # change to your broker's name, e.g. XAUUSDm / GOLD
        "timeframe": "M5",
        "dashboard_magic": 777000,      # magic used for orders sent from this dashboard
        "deviation_points": 50,
        "mt5": {                        # leave empty to attach to the MT5 terminal that is already open
            "path": "",
            "login": 0,
            "password": "",
            "server": ""
        },
        "goals": {"daily": 50.0, "weekly": 250.0, "monthly": 1000.0},
        "asian_session": {"start_hour": 1, "end_hour": 8, "trade_until_hour": 16},
        "ai": {"provider": "anthropic", "anthropic_api_key": "", "anthropic_workspace_id": "", "model": "claude-sonnet-5-5",
               "openai_api_key": "", "openai_model": "gpt-6.1-sol", "news_note": ""}
    },
    "risk": {
        "lot_mode": "fixed",            # "fixed" = use default_lot | "risk" = size from risk_per_trade_pct
        "default_lot": 0.01,
        "max_lot": 1.0,
        "risk_per_trade_pct": 1.0,
        "max_daily_loss_pct": 3.0,
        "max_daily_loss_money": 0.0,    # 0 = use % only
        "max_orders_per_day": 20,
        "max_open_positions": 3,
        "max_spread": 0.80,             # in price units ($ for gold)
        "require_sl": True,
        "auto_kill_on_daily_loss": True,
        "auto_release_new_day": True,   # auto-kill from the daily loss limit is lifted at server midnight
        "kill_switch": False,
        "kill_switch_reason": "",
        "kill_switch_time": 0,
        "kill_switch_auto": False,
        "kill_switch_day": 0
    },
    "ea_map": {
        "0": "Manual",
        "260109": "GoldScalper M1",
        "260110": "AIScalper M1",
        "777000": "Dashboard"
    },
    "scanner": {
        "enabled": True,
        "interval_sec": 30,
        "auto_trade": False,            # OFF by default - turn on only after testing on demo
        "strategies": {
            "SMC": True, "SND": True, "EMA": True, "BB": True, "ASIA": True, "AIPB": True
        },
        "auto_trade_strategies": {
            "SMC": False, "SND": False, "EMA": False, "BB": False, "ASIA": False, "AIPB": False
        },
    },
    "strategy_params": {},              # per-strategy overrides applied from the Optimizer
    "optimizer": {"history": []},       # applied changes (for rollback)
    "notes": {}
}

_lock = threading.RLock()


def _path(name):
    return os.path.join(DATA_DIR, f"{name}.json")


def _merge(default, current):
    if not isinstance(default, dict) or not isinstance(current, dict):
        return current
    out = deepcopy(default)
    for k, v in current.items():
        out[k] = _merge(default.get(k), v) if k in default else v
    return out


def load(name):
    with _lock:
        default = DEFAULTS.get(name, {})
        p = _path(name)
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    return _merge(default, json.load(f))
            except Exception:
                pass
        return deepcopy(default)


def save(name, data):
    with _lock:
        tmp = _path(name) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _path(name))
        return data


def update(name, patch):
    with _lock:
        data = load(name)
        data = _merge(data, patch)
        return save(name, data)
