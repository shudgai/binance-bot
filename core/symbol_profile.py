import os
import json
import time
import numpy as np
from core.config import (
    COIN_PROFILE_CONFIG, DEFAULT_SYMBOLS, CONFIG_FILE, PERSONALITY_TEMPLATES,
    SYMBOL_EXIT_OVERRIDES as _DEFAULT_SYMBOL_EXIT_OVERRIDES,
    SYMBOL_REVERSAL_SETTINGS as _DEFAULT_SYMBOL_REVERSAL_SETTINGS,
)
import core.config as _config

# These are mutable module-level vars that get overridden by load_symbol_config
SYMBOL_EXIT_OVERRIDES = dict(_DEFAULT_SYMBOL_EXIT_OVERRIDES)
SYMBOL_REVERSAL_SETTINGS = dict(_DEFAULT_SYMBOL_REVERSAL_SETTINGS)
SYMBOL_PROFILES = {}


def normalize_symbol(sym):
    if sym is None:
        return ""
    sym = str(sym).strip().upper()
    if not sym:
        return ""
    if not sym.endswith("USDT"):
        sym = f"{sym}USDT"
    return sym


def normalize_symbol_list(symbols, max_count=20):
    if isinstance(symbols, str):
        symbols = [symbols]
    if not symbols:
        return list(DEFAULT_SYMBOLS[:max_count])

    seen = []
    for item in symbols:
        sym = normalize_symbol(item)
        if sym and sym not in seen:
            seen.append(sym)

    return seen[:max_count]


def load_symbol_config():
    global SYMBOL_EXIT_OVERRIDES, SYMBOL_REVERSAL_SETTINGS
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        symbols = []
        profiles = {}
        exit_overrides = {}
        reversal_settings = {}
        if isinstance(data, dict):
            symbols = normalize_symbol_list(data.get("symbols", []))
            raw_profiles = data.get("profiles", {})
            if isinstance(raw_profiles, dict):
                for sym, profile in raw_profiles.items():
                    normalized = normalize_symbol(sym)
                    if not normalized or not isinstance(profile, dict):
                        continue
                    profile_copy = dict(profile)
                    overrides = profile_copy.get("exit_overrides")
                    if isinstance(overrides, dict):
                        exit_overrides[normalized] = overrides
                    settings = profile_copy.get("reversal_settings")
                    if isinstance(settings, dict):
                        reversal_settings[normalized] = settings
                    profiles[normalized] = profile_copy
        SYMBOL_EXIT_OVERRIDES = exit_overrides
        SYMBOL_REVERSAL_SETTINGS = reversal_settings

        try:
            with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config", "strategy_config.json"), "r") as f:
                config = json.load(f)
                priority_list = config.get("priority_symbols", [])
        except Exception:
            priority_list = []

        combined = []
        for s in priority_list:
            norm = normalize_symbol(s)
            if norm and norm not in combined:
                combined.append(norm)

        for s in (symbols or list(DEFAULT_SYMBOLS)):
            norm = normalize_symbol(s)
            if norm and norm not in combined:
                combined.append(norm)

        return combined, profiles
    except FileNotFoundError:
        return list(DEFAULT_SYMBOLS), {}
    except Exception as e:
        print(f"⚠️ 讀取幣種清單失敗: {e}")
        return list(DEFAULT_SYMBOLS), {}


def load_symbol_profiles():
    _, profiles = load_symbol_config()
    return profiles


def load_symbol_pool():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return normalize_symbol_list(data.get("symbols", []))
        return normalize_symbol_list(data)
    except FileNotFoundError:
        return list(DEFAULT_SYMBOLS)
    except Exception as e:
        print(f"⚠️ 讀取幣種清單失敗: {e}")
        return list(DEFAULT_SYMBOLS)


def save_symbol_pool(symbols):
    normalized = normalize_symbol_list(symbols)
    profiles = load_symbol_profiles()
    payload = {"symbols": normalized}
    if profiles:
        payload["profiles"] = profiles
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return normalized


def save_symbol_profiles(profiles):
    symbols = load_symbol_pool()
    normalized_profiles = {}
    for sym, profile in (profiles or {}).items():
        normalized = normalize_symbol(sym)
        if normalized and isinstance(profile, dict):
            normalized_profiles[normalized] = profile
    payload = {"symbols": symbols, "profiles": normalized_profiles}
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return normalized_profiles


def infer_symbol_personality(sym):
    if sym in ("BTCUSDT", "ETHUSDT"):
        return "calm"
    aggressive_coins = {"DOGEUSDT", "SHIBUSDT"}
    balanced_coins = {"ADAUSDT", "SOLUSDT", "LINKUSDT", "AVAXUSDT", "NEARUSDT", "SUIUSDT", "INJUSDT", "RENDERUSDT"}
    if sym in aggressive_coins:
        return "aggressive"
    if sym in balanced_coins:
        return "balanced"
    return "adaptive"


def get_personality_template(personality):
    if not personality:
        return PERSONALITY_TEMPLATES["balanced"]
    return PERSONALITY_TEMPLATES.get(personality.lower(), PERSONALITY_TEMPLATES["balanced"])


def get_symbol_exit_override(sym):
    return SYMBOL_EXIT_OVERRIDES.get(sym, {})


def should_require_strong_exit(overrides):
    return bool(
        overrides.get("require_strong_momentum")
        or overrides.get("volume_threshold") is not None
        or overrides.get("momentum_threshold") is not None
    )


def has_strong_momentum(sym, is_long):
    from core import ctx
    s = ctx.STATES[sym]
    if s.get("vol_ma20", 0.0) <= 0 or len(s.get("closes", [])) < 4:
        return False
    volume_ratio = s["current_vol"] / max(s["vol_ma20"], 1e-8)
    recent_return = (s["closes"][-1] - s["closes"][-4]) / max(abs(s["closes"][-4]), 1e-8)
    if is_long:
        return volume_ratio > 1.2 and s["close_price"] > s.get("bb_mid", 0.0) and s["current_rsi"] > 52 and s.get("macd_hist", 0.0) > 0 and recent_return > 0.005
    return volume_ratio > 1.2 and s["close_price"] < s.get("bb_mid", 0.0) and s["current_rsi"] < 48 and s.get("macd_hist", 0.0) < 0 and recent_return < -0.005


def is_strong_exit_condition(sym, is_long):
    from core import ctx
    overrides = get_symbol_exit_override(sym)
    if not overrides:
        return False
    if overrides.get("require_strong_momentum"):
        return has_strong_momentum(sym, is_long)
    volume_threshold = overrides.get("volume_threshold")
    momentum_threshold = overrides.get("momentum_threshold")
    s = ctx.STATES[sym]
    if s.get("vol_ma20", 0.0) <= 0 or len(s.get("closes", [])) < 4:
        return False
    volume_ratio = s["current_vol"] / max(s["vol_ma20"], 1e-8)
    recent_return = abs((s["closes"][-1] - s["closes"][-4]) / max(abs(s["closes"][-4]), 1e-8))
    if volume_threshold is not None and volume_ratio < float(volume_threshold):
        return False
    if momentum_threshold is not None and recent_return < float(momentum_threshold):
        return False
    return True


def get_effective_exit_setting(sym, key, base_value, is_long):
    profile = SYMBOL_PROFILES.get(sym)
    if profile and key in profile:
        return profile[key]

    overrides = get_symbol_exit_override(sym)
    if not overrides:
        return base_value
    value = overrides.get(key)
    if not isinstance(value, (int, float)):
        return base_value
    if key == "tp_atr_multiplier":
        if should_require_strong_exit(overrides) and not is_strong_exit_condition(sym, is_long):
            return base_value
        return value
    if key in ("sl_atr_multiplier", "hard_stop_loss_pct"):
        if value > base_value:
            return base_value
    return value


def get_dynamic_atr_multiplier(sym, base_multiplier):
    from core import ctx
    s = ctx.STATES.get(sym)
    if not s:
        return base_multiplier

    atr_history = s.get("atr_history", [])
    atr_24h_avg = float(np.mean(atr_history)) if len(atr_history) > 0 else 0.0
    current_atr = s.get("current_atr", 0.0)

    if atr_24h_avg > 0:
        vol_ratio = current_atr / atr_24h_avg
        if vol_ratio > 1.5:
            return base_multiplier * min(vol_ratio, 1.2)
        elif vol_ratio < 0.7:
            return base_multiplier * 0.8

    return base_multiplier


def apply_symbol_profile(sym, profile):
    from core import ctx
    if sym not in ctx.STATES:
        return
    state = ctx.STATES[sym]
    if isinstance(profile, str):
        profile = {"personality": profile}
    personality = profile.get("personality") or state.get("personality") or infer_symbol_personality(sym)
    personality_source = "manual" if profile.get("personality") else state.get("personality_source", "infer")
    template = get_personality_template(personality)
    state.update(template)
    for key in [
        "risk_multiplier", "volume_multiplier", "entry_cooldown_sec",
        "max_additional_entries", "entry_size_pct", "add_entry_pct",
        "sl_atr_multiplier", "tp_atr_multiplier", "hard_stop_loss_pct",
        "volume_threshold_factor", "min_flip_time", "breakeven_trigger",
        "profile_type", "leverage", "mtf_filter",
        "rsi_extreme_low", "rsi_extreme_high", "rsi_recovery_hook", "volatility_cap",
        "min_rr", "min_profit_pct",
        "trailing_activation_atr", "trailing_distance_atr", "profit_lock_atr"
    ]:
        if key in profile:
            state[key] = profile[key]
    state["personality"] = personality
    state["personality_source"] = personality_source
    if personality_source == "manual":
        state["last_personality_update"] = time.time()


def apply_all_symbol_profiles():
    from core import ctx
    default_profile = {
        "sl_atr_multiplier": 1.5,
        "tp_atr_multiplier": 3.0,
        "min_rr": 1.2,
        "min_profit_pct": 0.001,
        "trailing_activation_atr": 1.0,
        "trailing_distance_atr": 0.8,
        "profit_lock_atr": 2.0
    }
    for sym in ctx.ALL_SYMBOLS:
        json_profile = SYMBOL_PROFILES.get(sym, {})
        if not json_profile:
            json_profile = default_profile.copy()
        py_profile = COIN_PROFILE_CONFIG.get(sym, {})
        merged_profile = {**json_profile, **py_profile}
        apply_symbol_profile(sym, merged_profile)


def has_manual_personality(sym):
    profile = SYMBOL_PROFILES.get(sym, {})
    return isinstance(profile, dict) and "personality" in profile


def evaluate_dynamic_personality(sym):
    from core import ctx
    s = ctx.STATES[sym]
    if s["current_atr"] <= 0 or s["vol_ma20"] <= 0 or len(s["ohlcv"]) < 20:
        return s.get("personality", "balanced")

    close = s["close_price"]
    atr_pct = s["current_atr"] / max(close, 1e-8)
    volume_ratio = s["current_vol"] / max(s["vol_ma20"], 1e-8)
    recent_candles = s["ohlcv"][-20:]
    highs = np.array([x[2] for x in recent_candles])
    lows = np.array([x[3] for x in recent_candles])
    recent_high = float(np.max(highs))
    recent_low = float(np.min(lows))
    range_width_pct = (recent_high - recent_low) / max(recent_low, 1e-8)
    rsi = s.get("current_rsi", 50.0)
    macd_hist = s.get("macd_hist", 0.0)

    quiet_market = volume_ratio < 1.15 and atr_pct < 0.008 and range_width_pct < 0.02
    high_volatility = volume_ratio > 1.9 or atr_pct > 0.02 or range_width_pct > 0.04
    strong_trend = abs(rsi - 50.0) > 12.0 or abs(macd_hist) > close * 0.0006

    if quiet_market:
        return "calm"
    if high_volatility or strong_trend:
        return "aggressive"
    if range_width_pct >= 0.03 or abs(rsi - 50.0) > 8.0:
        return "balanced"
    return "adaptive"


def measure_personality_traits(sym):
    from core import ctx
    s = ctx.STATES[sym]
    close = max(s.get("close_price", 0.0), 1e-8)
    atr_pct = s.get("current_atr", 0.0) / close
    volume_ratio = s.get("current_vol", 0.0) / max(s.get("vol_ma20", 1e-8), 1e-8)
    recent_candles = s.get("ohlcv", [])[-20:]
    highs = np.array([x[2] for x in recent_candles]) if recent_candles else np.array([close])
    lows = np.array([x[3] for x in recent_candles]) if recent_candles else np.array([close])
    recent_high = float(np.max(highs))
    recent_low = float(np.min(lows))
    range_width_pct = (recent_high - recent_low) / max(recent_low, 1e-8)
    rsi = s.get("current_rsi", 50.0)
    return volume_ratio, atr_pct, rsi, range_width_pct


def update_dynamic_personality(sym):
    from core import ctx
    if has_manual_personality(sym):
        return False
    s = ctx.STATES[sym]
    new_personality = evaluate_dynamic_personality(sym)
    old_personality = s.get("personality", "balanced")
    if new_personality == old_personality and s.get("personality_source") == "dynamic":
        s["last_personality_update"] = time.time()
        return False
    if new_personality != old_personality:
        s.update(get_personality_template(new_personality))
        s["personality"] = new_personality
        s["personality_source"] = "dynamic"
        s["last_personality_update"] = time.time()
        coin_conf = COIN_PROFILE_CONFIG.get(sym, {})
        for key in ("sl_atr_multiplier", "tp_atr_multiplier", "hard_stop_loss_pct",
                    "leverage", "breakeven_trigger", "volume_threshold_factor"):
            if key in coin_conf:
                s[key] = coin_conf[key]
        volume_ratio, atr_pct, rsi, range_width_pct = measure_personality_traits(sym)
        print(f"🔧 [動態個性] {sym} 由 {old_personality} 變更為 {new_personality} | vol={volume_ratio:.2f} atr_pct={atr_pct:.4f} rsi={rsi:.1f} range={range_width_pct:.3f}")
        return True
    return False


def update_all_dynamic_personalities():
    from core import ctx
    now = time.time()
    for sym in ctx.ALL_SYMBOLS:
        s = ctx.STATES[sym]
        if has_manual_personality(sym):
            continue
        if now - s.get("last_personality_update", 0.0) < 300:
            continue
        update_dynamic_personality(sym)


def filter_valid_symbols(exchange, symbols):
    from core.exchange_client import exchange_futures
    if not exchange_futures.markets:
        return list(symbols)
    valid = []
    for sym in symbols:
        found = False
        for m in exchange_futures.markets.values():
            if m['id'] == sym or m['symbol'] == sym:
                found = True
                break
        if found:
            valid.append(sym)
        else:
            print(f"⚠️ [過濾無效幣種] 交易所目前不支援/已下架此幣種，已自動移出監聽清單: {sym}")
    return valid


def apply_symbol_pool_change(requested_symbols):
    import core.ctx as ctx
    from core.exchange_client import exchange_futures
    desired = filter_valid_symbols(exchange_futures, normalize_symbol_list(requested_symbols))
    locked_symbols = [sym for sym in ctx.ALL_SYMBOLS if _is_symbol_locked(sym)]

    new_symbols = []
    used = set()
    target_count = min(20, max(len(desired), len(ctx.ALL_SYMBOLS)))

    for sym in locked_symbols:
        if sym not in used:
            new_symbols.append(sym)
            used.add(sym)
    for sym in desired:
        if sym in used or len(new_symbols) >= target_count:
            continue
        new_symbols.append(sym)
        used.add(sym)
    for sym in ctx.ALL_SYMBOLS:
        if sym in used or len(new_symbols) >= target_count:
            continue
        new_symbols.append(sym)
        used.add(sym)
    for sym in DEFAULT_SYMBOLS:
        if sym in used or len(new_symbols) >= target_count:
            continue
        new_symbols.append(sym)
        used.add(sym)

    ctx.ALL_SYMBOLS = new_symbols[:target_count]
    for sym in ctx.ALL_SYMBOLS:
        ctx.STATES.setdefault(sym, _build_symbol_state(sym))
    apply_all_symbol_profiles()
    save_symbol_pool(ctx.ALL_SYMBOLS)
    return list(ctx.ALL_SYMBOLS)


def _is_symbol_locked(sym):
    from core import ctx
    s = ctx.STATES.get(sym)
    if not s:
        return False
    return abs(s["qty"]) > 0.000001 or s["entry_count"] > 0 or s["open_time"] > 0 or s["status"] in ("COOLDOWN", "BANNED") or s.get("pending_side") is not None


def _build_symbol_state(sym):
    """Lazy import to avoid circular dep with state_manager."""
    from core.state_manager import build_symbol_state
    return build_symbol_state(sym)
