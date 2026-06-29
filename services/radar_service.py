import os
import json
import time
import threading
from services.system_log_service import add_system_log
from services.bot_manager_service import get_bot_status, start_bot, kill_bot, save_symbol_config
from services.binance_service import get_top_volume_altcoins, get_atr_ranked_coins
from core.config import COIN_PROFILE_CONFIG

SYMBOL_CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "bot_symbols.json")


def _compute_dynamic_profile(symbol: str, atr_pct: float, price: float, rank: int, total: int) -> dict:
    """
    根據 ATR%、單價、排名，AI 輔助計算當期最佳個性參數。
    結果寫入 bot_symbols.json profiles，覆蓋靜態 COIN_PROFILE_CONFIG。
    """
    base = dict(COIN_PROFILE_CONFIG.get(symbol, {}))

    # ── 槓桿上限（依單價）──
    if price < 0.10:
        lev_cap, price_tag = 2, "超低價"
    elif price < 1.0:
        lev_cap, price_tag = 3, "低價"
    else:
        lev_cap, price_tag = 4, "正常"

    # ── 依 ATR% 決定 SL 寬度、槓桿、追蹤停利 ──
    if atr_pct > 4.0:
        sl_mult   = round(base.get("sl_atr_multiplier", 2.5) + 1.0, 1)
        lev_cap   = min(lev_cap, 2)
        hard_sl   = max(base.get("hard_sl_pct", 0.0), 0.030)
        trail_on  = True
        vol_tag   = "超高波動"
    elif atr_pct > 2.5:
        sl_mult   = round(base.get("sl_atr_multiplier", 2.5) + 0.5, 1)
        lev_cap   = min(lev_cap, 3)
        hard_sl   = base.get("hard_sl_pct", 0.0)
        trail_on  = True
        vol_tag   = "高波動"
    elif atr_pct > 1.5:
        sl_mult   = base.get("sl_atr_multiplier", 2.5)
        hard_sl   = base.get("hard_sl_pct", 0.0)
        trail_on  = True
        vol_tag   = "中波動"
    else:
        sl_mult   = max(round(base.get("sl_atr_multiplier", 2.5) - 0.3, 1), 1.5)
        hard_sl   = base.get("hard_sl_pct", 0.0)
        trail_on  = False
        vol_tag   = "低波動"

    # ── ATR 排名越高 → TP 放大（讓強勢幣跑更遠）──
    rank_factor  = 1.0 + (total - rank) / max(total, 1) * 0.5   # rank1=+50%, rank=total=+0%
    tp_mult      = round(base.get("tp_atr_multiplier", 10.0) * rank_factor, 1)

    # ── 最終槓桿 ──
    final_lev = min(base.get("leverage", 3), lev_cap)

    profile = {
        "sl_atr_multiplier": sl_mult,
        "tp_atr_multiplier": tp_mult,
        "leverage":          final_lev,
        "_radar_atr_pct":    round(atr_pct, 3),
        "_radar_rank":       rank,
        "_radar_tag":        f"{price_tag}/{vol_tag}",
    }
    if hard_sl > 0:
        profile["hard_sl_pct"] = hard_sl
    if trail_on:
        profile["trailing_activation_atr"] = base.get("trailing_activation_atr", 1.2)
        profile["trailing_distance_atr"]   = base.get("trailing_distance_atr",   0.7)
    return profile


def _save_radar_profiles(profiles: dict):
    """將動態個性寫入 bot_symbols.json profiles 區塊。"""
    try:
        with open(SYMBOL_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {"symbols": data}
        data["profiles"] = profiles
        with open(SYMBOL_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        add_system_log(f"⚠️ [AI個性] 寫入 profiles 失敗: {e}", "warning")

CORE_SYMBOLS = list(COIN_PROFILE_CONFIG.keys())
RADAR_SELECT_COUNT = 8

# 雷達掃描冷卻
last_radar_scan = 0
RADAR_SCAN_COOLDOWN = 10.0
radar_lock = threading.Lock()
last_api_call = 0
API_RATE_LIMIT = 1.0

# 換倉重啟冷卻：5 分鐘內不重複重啟（避免雷達頻繁觸發）
last_bot_restart = 0.0
BOT_RESTART_COOLDOWN = 300.0  # 5 minutes

# 熔斷黑名單 {symbol: expire_timestamp}
BLACKLIST = {}

def clean_blacklist():
    global BLACKLIST
    now = time.time()
    BLACKLIST = {k: v for k, v in BLACKLIST.items() if v > now}

def blacklist_coin(symbol: str, duration_sec: int = 86400):
    global BLACKLIST
    BLACKLIST[symbol] = time.time() + duration_sec
    add_system_log(f"🚨 [熔斷機制] {symbol} 已被列入黑名單，{duration_sec//3600} 小時內不會再被選中", "danger")

def get_radar_cooldown():
    global last_radar_scan
    elapsed = time.time() - last_radar_scan
    return max(0.0, RADAR_SCAN_COOLDOWN - elapsed)

def trigger_manual_radar():
    global last_radar_scan
    elapsed = time.time() - last_radar_scan
    if elapsed < RADAR_SCAN_COOLDOWN:
        return {
            "status": "success",
            "active_symbols": get_bot_status().get("active_symbols", []),
            "best_symbols": get_bot_status().get("active_symbols", []),
            "cooldown": round(RADAR_SCAN_COOLDOWN - elapsed, 1)
        }
    last_radar_scan = time.time()
    best_symbols = auto_radar_switch(force_start=True)
    return {
        "status": "success",
        "active_symbols": best_symbols,
        "best_symbols": best_symbols
    }

def _get_recently_traded_symbols(hours=24):
    try:
        state_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "paper_state.json")
        if not os.path.exists(state_path):
            return []
        with open(state_path, "r") as f:
            state = json.load(f)
        symbols = set()
        now_ms = time.time() * 1000
        for t in state.get("trades", []):
            if now_ms - t.get("time", 0) < hours * 3600 * 1000:
                sym = t.get("symbol", "").replace(":USDT", "USDT").replace(":", "")
                if sym:
                    symbols.add(sym)
        return list(symbols)
    except Exception as e:
        print(f"⚠️ [讀取交易歷史] 失敗: {e}")
        return []

def _get_open_position_symbols():
    try:
        state_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "paper_state.json")
        if not os.path.exists(state_path):
            return []
        with open(state_path, "r") as f:
            state = json.load(f)
        symbols = []
        for key, pos in state.get("positions", {}).items():
            if abs(float(pos.get("qty", 0.0))) > 0.000001:
                sym = key.replace(":USDT", "USDT").replace(":", "")
                symbols.append(sym)
        return symbols
    except Exception as e:
        print(f"⚠️ [讀取持倉] 失敗: {e}")
        return []

def auto_radar_switch(force_start=False):
    global last_api_call
    if not radar_lock.acquire(blocking=False):
        add_system_log("⚠️ [雷達掃描] 前一次掃描尚未完成，跳過", "warning")
        return get_bot_status().get("active_symbols", [])
    try:
        add_system_log(f"📡 [雷達掃描] 從 {len(CORE_SYMBOLS)} 個核心幣種中，依 ATR% 選出最強 {RADAR_SELECT_COUNT} 個...", "warning")

        elapsed = time.time() - last_api_call
        if elapsed < API_RATE_LIMIT:
            time.sleep(API_RATE_LIMIT - elapsed)
        last_api_call = time.time()

        bot_status = get_bot_status()
        current_syms = bot_status.get("active_symbols", [])

        clean_blacklist()
        scan_pool = [s for s in CORE_SYMBOLS if s not in BLACKLIST]
        top_symbols, full_ranking = get_atr_ranked_coins(scan_pool, limit=RADAR_SELECT_COUNT)

        if not top_symbols:
            add_system_log("⚠️ [雷達掃描] 無法計算 ATR 排名，維持原狀", "warning")
            return current_syms

        # 記錄排名供 UI 顯示
        ranking_str = " | ".join([f"{r['symbol'].replace('USDT','')} {r['atr_pct']:.2f}%" for r in full_ranking[:10]])
        add_system_log(f"📊 [ATR排名] {ranking_str}", "info")

        # 保留仍有持倉的幣種，避免被換掉
        open_syms = _get_open_position_symbols()
        all_preserved = [s for s in open_syms if s not in top_symbols]
        if all_preserved:
            add_system_log(f"🔒 [持倉保護] 強制保留持倉幣種: {', '.join(all_preserved)}", "warning")

        final_symbols = all_preserved + top_symbols
        if len(final_symbols) > RADAR_SELECT_COUNT + 2:
            final_symbols = final_symbols[:RADAR_SELECT_COUNT + 2]

        # ── AI 輔助自動分析：為每個入選幣種計算動態個性，寫入 bot_symbols.json ──
        rank_map = {r["symbol"]: (i + 1, r["atr_pct"], r["price"]) for i, r in enumerate(full_ranking)}
        dynamic_profiles = {}
        analysis_lines = []
        for i, sym in enumerate(top_symbols):
            rank, atr_pct, price = rank_map.get(sym, (i + 1, 0.0, 0.0))
            prof = _compute_dynamic_profile(sym, atr_pct, price, rank, len(full_ranking))
            dynamic_profiles[sym] = prof
            tag = prof.get("_radar_tag", "")
            analysis_lines.append(
                f"{sym.replace('USDT','')} ATR{atr_pct:.2f}% → "
                f"lev{prof['leverage']}x SL{prof['sl_atr_multiplier']}x TP{prof['tp_atr_multiplier']}x [{tag}]"
            )
        _save_radar_profiles(dynamic_profiles)
        add_system_log(f"🤖 [AI個性] 已為 {len(dynamic_profiles)} 幣自動設定個性:", "info")
        for line in analysis_lines:
            add_system_log(f"   ↳ {line}", "info")

        # 排序讓比較不受順序影響
        if sorted(final_symbols) == sorted(current_syms):
            add_system_log(f"✅ [雷達掃描] 榜單未變 ({', '.join(final_symbols)})，維持不變", "success")
            if force_start and not bot_status.get("is_running"):
                start_bot(final_symbols, bot_status.get("trade_amount", 150.0))
            return final_symbols

        add_system_log(f"🎯 [雷達鎖定] ATR最強 {RADAR_SELECT_COUNT} 幣: {', '.join(top_symbols)}", "success")
        if all_preserved:
            add_system_log(f"🔒 [持倉保護] 保留持倉幣種: {', '.join(all_preserved)}", "warning")
        bot_status["active_symbols"] = final_symbols

        # 換倉冷卻：5 分鐘內不重複重啟，避免雷達頻繁換倉
        global last_bot_restart
        since_restart = time.time() - last_bot_restart
        if since_restart < BOT_RESTART_COOLDOWN:
            remaining = int(BOT_RESTART_COOLDOWN - since_restart)
            add_system_log(f"⏳ [雷達冷卻] 換倉冷卻中，剩餘 {remaining} 秒，暫不重啟", "warning")
            return final_symbols

        if bot_status.get("is_running") or force_start:
            last_bot_restart = time.time()
            start_bot(final_symbols, bot_status.get("trade_amount", 150.0))

        return final_symbols
    except Exception as e:
        add_system_log(f"🚨 [雷達掃描] 掃描失敗: {e}", "danger")
        bot_status = get_bot_status()
        if not bot_status.get("is_running") and not force_start:
            kill_bot()
        return bot_status.get("active_symbols", [])
    finally:
        radar_lock.release()

def replace_dead_coin(symbol: str):
    try:
        bot_status = get_bot_status()
        current_syms = bot_status.get("active_symbols", [])
        if symbol in current_syms:
            current_syms.remove(symbol)
            
        add_system_log(f"💀 [死水汰換] 剔除無波動死水幣 {symbol}，尋找替補...", "warning")
        
        # 抓取前 15 名來尋找替補
        clean_blacklist()
        ignore_list = list(BLACKLIST.keys())
        top_15 = get_top_volume_altcoins(15, ignore_list=ignore_list)
        new_coin = None
        for coin in top_15:
            if coin not in current_syms:
                new_coin = coin
                break
                
        if new_coin:
            current_syms.append(new_coin)
            bot_status["active_symbols"] = current_syms
            add_system_log(f"✨ [自動補位] 成功選入候補熱門小幣: {new_coin}", "success")
            start_bot(current_syms, bot_status.get("trade_amount", 10.0))
        else:
            add_system_log(f"⚠️ [自動補位] 找不到合適的候補小幣", "danger")
    except Exception as e:
        add_system_log(f"🚨 [自動補位] 發生錯誤: {e}", "danger")
