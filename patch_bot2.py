import sys

with open('multi_coin_bot.py', 'r') as f:
    content = f.read()

old_config_start = """COIN_PROFILE_CONFIG = {"""
old_config_block = """COIN_PROFILE_CONFIG = {
    # --- 第一類：核心趨勢層 (Core Trend) - 穩健趨勢，較高槓桿 ---
    "SOLUSDT": {"sl_atr_multiplier": 3.0, "tp_atr_multiplier": 6.0, "volume_threshold_factor": 1.2, "breakeven_trigger": 0.5, "min_flip_time": 300, "mtf_filter": True, "profile_type": "Core_Trend", "leverage": 8, "k_factor": 2.5},
    "LINKUSDT": {"sl_atr_multiplier": 2.0, "tp_atr_multiplier": 4.0, "volume_threshold_factor": 1.1, "breakeven_trigger": 0.4, "min_flip_time": 180, "mtf_filter": True, "profile_type": "Core_Trend", "leverage": 8, "k_factor": 2.5},
    "TRXUSDT": {"sl_atr_multiplier": 2.5, "tp_atr_multiplier": 5.0, "volume_threshold_factor": 1.3, "breakeven_trigger": 0.5, "min_flip_time": 240, "mtf_filter": True, "profile_type": "Core_Trend", "leverage": 8, "k_factor": 2.5},

    # --- 第二類：高彈性動能層 (High-Beta Momentum) - 快速爆發，中等槓桿 ---
    "RENDERUSDT": {"sl_atr_multiplier": 2.0, "tp_atr_multiplier": 4.0, "volume_threshold_factor": 1.5, "breakeven_trigger": 0.6, "min_flip_time": 120, "mtf_filter": True, "profile_type": "High_Beta_Momentum", "leverage": 4, "k_factor": 4.5},
    "SUIUSDT": {"sl_atr_multiplier": 1.8, "tp_atr_multiplier": 3.6, "volume_threshold_factor": 1.8, "breakeven_trigger": 0.7, "min_flip_time": 90, "mtf_filter": True, "profile_type": "High_Beta_Momentum", "leverage": 4, "k_factor": 4.5},
    "INJUSDT": {"sl_atr_multiplier": 2.2, "tp_atr_multiplier": 4.4, "volume_threshold_factor": 1.5, "breakeven_trigger": 0.6, "min_flip_time": 120, "mtf_filter": True, "profile_type": "High_Beta_Momentum", "leverage": 4, "k_factor": 4.5},
    "NEARUSDT": {"sl_atr_multiplier": 2.3, "tp_atr_multiplier": 4.6, "volume_threshold_factor": 1.3, "breakeven_trigger": 0.5, "min_flip_time": 180, "mtf_filter": True, "profile_type": "High_Beta_Momentum", "leverage": 4, "k_factor": 4.5},
    "VELVETUSDT": {"sl_atr_multiplier": 2.0, "tp_atr_multiplier": 4.0, "volume_threshold_factor": 1.6, "breakeven_trigger": 0.6, "min_flip_time": 120, "mtf_filter": True, "profile_type": "High_Beta_Momentum", "leverage": 4, "k_factor": 4.5},
    "LABUSDT": {"sl_atr_multiplier": 2.0, "tp_atr_multiplier": 4.0, "volume_threshold_factor": 1.6, "breakeven_trigger": 0.6, "min_flip_time": 120, "mtf_filter": True, "profile_type": "High_Beta_Momentum", "leverage": 4, "k_factor": 4.5},

    # --- 第三類：投機與特定風險層 (Speculative_Risk) - 極端防禦，低槓桿 ---
    "AVAXUSDT": {"sl_atr_multiplier": 2.5, "tp_atr_multiplier": 5.0, "volume_threshold_factor": 1.3, "breakeven_trigger": 0.5, "min_flip_time": 240, "mtf_filter": True, "profile_type": "Speculative_Risk", "leverage": 2, "k_factor": 6.0},
    "DOGEUSDT": {"sl_atr_multiplier": 3.5, "tp_atr_multiplier": 7.0, "volume_threshold_factor": 2.0, "breakeven_trigger": 0.8, "min_flip_time": 600, "mtf_filter": True, "profile_type": "Speculative_Risk", "leverage": 2, "k_factor": 6.0},
    "PEPEUSDT": {"sl_atr_multiplier": 4.0, "tp_atr_multiplier": 8.0, "volume_threshold_factor": 2.0, "breakeven_trigger": 0.8, "min_flip_time": 600, "mtf_filter": True, "profile_type": "Speculative_Risk", "leverage": 2, "k_factor": 6.0},
    
    # --- 新增分析調校幣種 ---
    "ESPORTSUSDT": {"sl_atr_multiplier": 3.0, "tp_atr_multiplier": 6.0, "volume_threshold_factor": 1.5, "breakeven_trigger": 0.8, "min_flip_time": 300, "mtf_filter": True, "profile_type": "Speculative_Risk", "leverage": 2, "k_factor": 6.0, "mmp": 0.005},
    "HEIUSDT": {"sl_atr_multiplier": 3.0, "tp_atr_multiplier": 6.0, "volume_threshold_factor": 1.5, "breakeven_trigger": 0.8, "min_flip_time": 300, "mtf_filter": True, "profile_type": "Speculative_Risk", "leverage": 2, "k_factor": 6.0, "mmp": 0.005},
    "BSBUSDT": {"sl_atr_multiplier": 4.0, "tp_atr_multiplier": 8.0, "volume_threshold_factor": 2.0, "breakeven_trigger": 0.8, "min_flip_time": 600, "mtf_filter": True, "profile_type": "Speculative_Risk", "leverage": 2, "k_factor": 6.0},
    "BELUSDT": {"sl_atr_multiplier": 2.5, "tp_atr_multiplier": 5.0, "volume_threshold_factor": 1.5, "breakeven_trigger": 0.6, "min_flip_time": 300, "mtf_filter": True, "profile_type": "High_Beta_Momentum", "leverage": 4, "k_factor": 4.5},
    "LABUSDT": {"sl_atr_multiplier": 3.0, "tp_atr_multiplier": 6.0, "volume_threshold_factor": 1.5, "breakeven_trigger": 0.8, "min_flip_time": 300, "mtf_filter": True, "profile_type": "Speculative_Risk", "leverage": 2, "k_factor": 6.0, "stalemate_time_sec": 1800},
    "HUSDT": {"sl_atr_multiplier": 4.0, "tp_atr_multiplier": 8.0, "volume_threshold_factor": 2.5, "breakeven_trigger": 0.8, "min_flip_time": 600, "mtf_filter": True, "profile_type": "Wild", "leverage": 2, "k_factor": 6.0, "mmp": 0.01, "volatility_circuit_breaker": True}
}"""

new_config_block = """import json

DEFAULT_CONFIG = {
    # --- 第一類：核心趨勢層 (Core Trend) - 穩健趨勢，較高槓桿 ---
    "SOLUSDT": {"sl_atr_multiplier": 3.0, "tp_atr_multiplier": 6.0, "volume_threshold_factor": 1.2, "breakeven_trigger": 0.5, "min_flip_time": 300, "mtf_filter": True, "profile_type": "Core_Trend", "leverage": 8, "k_factor": 2.5},
    "LINKUSDT": {"sl_atr_multiplier": 2.0, "tp_atr_multiplier": 4.0, "volume_threshold_factor": 1.1, "breakeven_trigger": 0.4, "min_flip_time": 180, "mtf_filter": True, "profile_type": "Core_Trend", "leverage": 8, "k_factor": 2.5},
    "TRXUSDT": {"sl_atr_multiplier": 2.5, "tp_atr_multiplier": 5.0, "volume_threshold_factor": 1.3, "breakeven_trigger": 0.5, "min_flip_time": 240, "mtf_filter": True, "profile_type": "Core_Trend", "leverage": 8, "k_factor": 2.5},

    # --- 第二類：高彈性動能層 (High-Beta Momentum) - 快速爆發，中等槓桿 ---
    "RENDERUSDT": {"sl_atr_multiplier": 2.0, "tp_atr_multiplier": 4.0, "volume_threshold_factor": 1.5, "breakeven_trigger": 0.6, "min_flip_time": 120, "mtf_filter": True, "profile_type": "High_Beta_Momentum", "leverage": 4, "k_factor": 4.5},
    "SUIUSDT": {"sl_atr_multiplier": 1.8, "tp_atr_multiplier": 3.6, "volume_threshold_factor": 1.8, "breakeven_trigger": 0.7, "min_flip_time": 90, "mtf_filter": True, "profile_type": "High_Beta_Momentum", "leverage": 4, "k_factor": 4.5},
    "INJUSDT": {"sl_atr_multiplier": 2.2, "tp_atr_multiplier": 4.4, "volume_threshold_factor": 1.5, "breakeven_trigger": 0.6, "min_flip_time": 120, "mtf_filter": True, "profile_type": "High_Beta_Momentum", "leverage": 4, "k_factor": 4.5},
    "NEARUSDT": {"sl_atr_multiplier": 2.3, "tp_atr_multiplier": 4.6, "volume_threshold_factor": 1.3, "breakeven_trigger": 0.5, "min_flip_time": 180, "mtf_filter": True, "profile_type": "High_Beta_Momentum", "leverage": 4, "k_factor": 4.5},
    "VELVETUSDT": {"sl_atr_multiplier": 2.0, "tp_atr_multiplier": 4.0, "volume_threshold_factor": 1.6, "breakeven_trigger": 0.6, "min_flip_time": 120, "mtf_filter": True, "profile_type": "High_Beta_Momentum", "leverage": 4, "k_factor": 4.5},
    "LABUSDT": {"sl_atr_multiplier": 2.0, "tp_atr_multiplier": 4.0, "volume_threshold_factor": 1.6, "breakeven_trigger": 0.6, "min_flip_time": 120, "mtf_filter": True, "profile_type": "High_Beta_Momentum", "leverage": 4, "k_factor": 4.5},

    # --- 第三類：投機與特定風險層 (Speculative_Risk) - 極端防禦，低槓桿 ---
    "AVAXUSDT": {"sl_atr_multiplier": 2.5, "tp_atr_multiplier": 5.0, "volume_threshold_factor": 1.3, "breakeven_trigger": 0.5, "min_flip_time": 240, "mtf_filter": True, "profile_type": "Speculative_Risk", "leverage": 2, "k_factor": 6.0},
    "DOGEUSDT": {"sl_atr_multiplier": 3.5, "tp_atr_multiplier": 7.0, "volume_threshold_factor": 2.0, "breakeven_trigger": 0.8, "min_flip_time": 600, "mtf_filter": True, "profile_type": "Speculative_Risk", "leverage": 2, "k_factor": 6.0},
    "PEPEUSDT": {"sl_atr_multiplier": 4.0, "tp_atr_multiplier": 8.0, "volume_threshold_factor": 2.0, "breakeven_trigger": 0.8, "min_flip_time": 600, "mtf_filter": True, "profile_type": "Speculative_Risk", "leverage": 2, "k_factor": 6.0},
    
    # --- 新增分析調校幣種 ---
    "ESPORTSUSDT": {"sl_atr_multiplier": 3.0, "tp_atr_multiplier": 6.0, "volume_threshold_factor": 1.5, "breakeven_trigger": 0.8, "min_flip_time": 300, "mtf_filter": True, "profile_type": "Speculative_Risk", "leverage": 2, "k_factor": 6.0, "mmp": 0.005},
    "HEIUSDT": {"sl_atr_multiplier": 3.0, "tp_atr_multiplier": 6.0, "volume_threshold_factor": 1.5, "breakeven_trigger": 0.8, "min_flip_time": 300, "mtf_filter": True, "profile_type": "Speculative_Risk", "leverage": 2, "k_factor": 6.0, "mmp": 0.005},
    "BSBUSDT": {"sl_atr_multiplier": 4.0, "tp_atr_multiplier": 8.0, "volume_threshold_factor": 2.0, "breakeven_trigger": 0.8, "min_flip_time": 600, "mtf_filter": True, "profile_type": "Speculative_Risk", "leverage": 2, "k_factor": 6.0},
    "BELUSDT": {"sl_atr_multiplier": 2.5, "tp_atr_multiplier": 5.0, "volume_threshold_factor": 1.5, "breakeven_trigger": 0.6, "min_flip_time": 300, "mtf_filter": True, "profile_type": "High_Beta_Momentum", "leverage": 4, "k_factor": 4.5},
    "LABUSDT": {"sl_atr_multiplier": 3.0, "tp_atr_multiplier": 6.0, "volume_threshold_factor": 1.5, "breakeven_trigger": 0.8, "min_flip_time": 300, "mtf_filter": True, "profile_type": "Speculative_Risk", "leverage": 2, "k_factor": 6.0, "stalemate_time_sec": 1800},
    "HUSDT": {"sl_atr_multiplier": 4.0, "tp_atr_multiplier": 8.0, "volume_threshold_factor": 2.5, "breakeven_trigger": 0.8, "min_flip_time": 600, "mtf_filter": True, "profile_type": "Wild", "leverage": 2, "k_factor": 6.0, "mmp": 0.01, "volatility_circuit_breaker": True}
}

def load_coin_profiles():
    try:
        with open('config/coin_profiles.json', 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        print("⚠️ 找不到配置文件，使用預設配置...")
        return DEFAULT_CONFIG

COIN_PROFILE_CONFIG = load_coin_profiles()"""

content = content.replace(old_config_block, new_config_block)

with open('multi_coin_bot.py', 'w') as f:
    f.write(content)

print("Patch 2 applied.")
