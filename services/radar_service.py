import os
import json
import time
import threading
from services.system_log_service import add_system_log
from services.bot_manager_service import get_bot_status, start_bot, kill_bot, save_symbol_config
from services.binance_service import get_atr_ranked_coins, get_hot_movers as _get_hot_movers
from core.config import COIN_PROFILE_CONFIG

SYMBOL_CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "bot_symbols.json")


def _resolve_follow_symbols_from(base_dir: str | None = None) -> str:
    """Resolve the shared symbol source path for strategy sync between deployments.

    Priority:
    1. Explicit FOLLOW_SYMBOLS_FROM environment variable.
    2. Shared sibling deployment at ../binance-bot/data/bot_symbols.json.
    3. Current deployment's local data/bot_symbols.json.
    """
    configured = os.getenv("FOLLOW_SYMBOLS_FROM", "").strip()
    if configured:
        return configured

    repo_root = os.path.abspath(base_dir or os.path.dirname(os.path.dirname(__file__)))
    parent_dir = os.path.dirname(repo_root)
    own_config_path = os.path.join(repo_root, "data", "bot_symbols.json")
    candidates = [
        os.path.join(parent_dir, "binance-bot", "data", "bot_symbols.json"),
        os.path.join(parent_dir, "binance-bot-live", "data", "bot_symbols.json"),
    ]

    for candidate in candidates:
        # 若候選路徑其實就是自己（例如本部署自己就叫 binance-bot），不能拿自己
        # 當作跟隨來源——否則會變成每次都在讀自己剛寫入的清單、判定「榜單未變」，
        # 導致真正的 ATR 排名/波動度過濾邏輯整個被跳過，形同雷達失效（實際發生過：
        # RADAR_SELECT_COUNT 已改成 8，但幣池一直卡在舊的 12～13 檔不會縮減）。
        if os.path.exists(candidate) and os.path.abspath(candidate) != os.path.abspath(own_config_path):
            return candidate
    return ""


# 若設定此環境變數（指向另一份部署的 bot_symbols.json 絕對路徑），本部署不再自己跑 ATR 雷達
# 掃描，而是直接跟隨來源部署選出的幣種清單，用來讓 8006 長期跟隨 8005 的幣池。
# 若未明確指定，則會自動從相鄰部署的 bot_symbols.json 讀取，保留各自帳務但同步策略幣池。
FOLLOW_SYMBOLS_FROM = _resolve_follow_symbols_from()
# 若在 8006 部署中設定此變數，則會跟隨來源部署的 bot_symbols.json，不自行跑 ATR 雷達掃描。
# 來源清單會寫入本地 bot_symbols.json，並保留本地持倉幣種。


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
    # ATR 停損可隨波動放寬，但 hard SL 是最後防線；雷達動態幣若缺省不寫，
    # 會退回全域 3%，讓 10 分鐘內的大虧被放太遠。這裡固定給保守上限。
    base_hard_sl = base.get("hard_sl_pct", 0.015)
    if atr_pct > 4.0:
        sl_mult   = round(base.get("sl_atr_multiplier", 2.5) + 1.0, 1)
        lev_cap   = min(lev_cap, 2)
        hard_sl   = min(max(base_hard_sl, 0.015), 0.020)
        trail_on  = True
        vol_tag   = "中高動能"
    elif atr_pct > 2.5:
        sl_mult   = round(base.get("sl_atr_multiplier", 2.5) + 0.5, 1)
        lev_cap   = min(lev_cap, 3)
        hard_sl   = min(max(base_hard_sl, 0.015), 0.025)
        trail_on  = True
        vol_tag   = "穩定動能"
    elif atr_pct > 1.5:
        sl_mult   = base.get("sl_atr_multiplier", 2.5)
        hard_sl   = min(max(base_hard_sl, 0.015), 0.025)
        trail_on  = True
        vol_tag   = "中波動"
    else:
        sl_mult   = max(round(base.get("sl_atr_multiplier", 2.5) - 0.3, 1), 1.5)
        hard_sl   = min(max(base_hard_sl, 0.012), 0.020)
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
        # 雷達動態選中、沒有寫在 COIN_PROFILE_CONFIG 裡的幣種（例如 BCHUSDT）本來
        # 對這些幣完全不認識，號損時還是會照樣觸發 Rescue DCA 加碼攞平，等於在還
        # 沒真正驗證過、風險認知不足的幣種上額外加碼冒険。跟 HOT_MOVER_PROFILE_BASE
        # 一樣的保守做法，雷達選出的幣預設關閉 Rescue DCA，號損就照 SL 出場，不加碼。
        "disable_rescue_dca": True,
        # 保留 min_signal_strength：雷達動態 profile 如果沒有寫入此欄位，
        # apply_symbol_profile 在合併時會用預設値 10.0 （is_relaxed=True），
        # 導致 MTF 放行門溻降至 12.0，對逆大趨勢空單放行。
        # 保留原從 COIN_PROFILE_CONFIG 諦得的設定，找不到就用 17.0 作保守預設。
    }
    _base_min_sig = base.get("min_signal_strength")
    if _base_min_sig is not None:
        profile["min_signal_strength"] = _base_min_sig
    else:
        profile["min_signal_strength"] = 17.0  # 保守預設，避免 is_relaxed 誤判
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

ATR_ELIGIBLE_SYMBOLS = [
    # 中大型、流動性較好的動能池；排除超低價與容易事件暴衝的幣。
    # DOGEUSDT/SOLUSDT 加入候選：兩者均有完整策略設定且流動性充足。
    "XRPUSDT", "ADAUSDT", "LINKUSDT", "DOTUSDT", "LTCUSDT",
    "BCHUSDT", "UNIUSDT", "ETCUSDT", "AAVEUSDT", "ATOMUSDT",
    "HBARUSDT", "XLMUSDT", "AVAXUSDT", "NEARUSDT", "APTUSDT",
    "SUIUSDT", "INJUSDT", "RENDERUSDT",
    "DOGEUSDT", "SOLUSDT",
]
CORE_SYMBOLS = list(ATR_ELIGIBLE_SYMBOLS)
# 選幣數從 8 縮至 5：只挑當下動能最強的精銳幣種，避免持有太多半死不活的幣。
# 3 個倉位槽 + 2 個備用，確保每檔都有足夠資金和信號密度。
RADAR_SELECT_COUNT = 5
HOT_MOVERS_COUNT   = 0    # 不追熱門暴衝榜，避免急升急跌標的進入監控池
CORE_SELECT_COUNT  = len(ATR_ELIGIBLE_SYMBOLS)

# 動能篩選門檻（收緊）：
#   ATR 2.5%~6.3%：有真實波動但不過度劇烈（舊 2.0%~6.5% 太寬，NEARUSDT/ADAUSDT 的 6.9%/6.6% 會被納入）
#   1h 波動 0.60%~2.8%：最近一小時要有明確方向（舊 0.35% 太低，XRPUSDT 的 0.48% 也能過）
MIN_ATR_PCT_FOR_ENTRY = 2.5
MAX_ATR_PCT_FOR_ENTRY = 6.3
MIN_1H_VOL_PCT_FOR_ENTRY = 0.60
MAX_1H_VOL_PCT_FOR_ENTRY = 2.8
MAX_24H_ABS_CHANGE_PCT_FOR_ENTRY = 14.0

# 熱門幣保守 profile（只走有強訊號的機會）
HOT_MOVER_PROFILE_BASE = {
    "sl_atr_multiplier":       3.0,
    "tp_atr_multiplier":       6.0,
    "leverage":                2,
    "hard_sl_pct":             0.015,
    "min_signal_strength":     20.0,
    "disable_rescue_dca":      True,
    "trailing_activation_atr": 0.8,
    "trailing_distance_atr":   0.7,
    "profile_type":            "Speculative_Risk",
    "mtf_filter":              True,
    "rr_threshold":            2.0,
    "breakeven_trigger":       0.5,
    "volume_threshold_factor": 1.2,

}

last_radar_scan = 0
RADAR_SCAN_COOLDOWN = 45.0
radar_lock = threading.Lock()
last_api_call = 0
API_RATE_LIMIT = 3.0

last_bot_restart = 0.0
BOT_RESTART_COOLDOWN = 300.0  

BLACKLIST = {"WLDUSDT": float('inf'), "OGNUSDT": float('inf')}

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
    from core.config import PAPER_TRADING
    if not PAPER_TRADING:
        try:
            from services.binance_service import get_all_positions
            positions = get_all_positions()
            return [pos["symbol"].replace(":USDT", "USDT") for pos in positions.values()]
        except Exception as e:
            print(f"⚠️ [讀取持倉] 失敗: {e}")
            return []
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

def _follow_source_radar_switch(force_start=False):
    global last_bot_restart
    try:
        with open(FOLLOW_SYMBOLS_FROM, "r", encoding="utf-8") as f:
            source = json.load(f)
        final_symbols = source.get("symbols", []) if isinstance(source, dict) else source
        profiles = source.get("profiles", {}) if isinstance(source, dict) else {}
    except Exception as e:
        add_system_log(f"🚨 [跟隨幣池] 讀取來源清單失敗 ({FOLLOW_SYMBOLS_FROM}): {e}", "danger")
        return get_bot_status().get("active_symbols", [])

    if not final_symbols:
        add_system_log("⚠️ [跟隨幣池] 來源清單為空，維持原狀", "warning")
        return get_bot_status().get("active_symbols", [])

    open_syms = _get_open_position_symbols()
    missing_open = [s for s in open_syms if s not in final_symbols]
    if missing_open:
        add_system_log(f"🔒 [持倉保護] 跟隨幣池但強制保留本地持倉幣種: {', '.join(missing_open)}", "warning")
        final_symbols = final_symbols + missing_open

    with open(SYMBOL_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump({"symbols": final_symbols, "profiles": profiles}, f, ensure_ascii=False, indent=2)

    bot_status = get_bot_status()
    current_syms = bot_status.get("active_symbols", [])
    if sorted(final_symbols) == sorted(current_syms):
        add_system_log(f"✅ [跟隨幣池] 榜單未變 ({', '.join(final_symbols)})，維持不變", "success")
        if force_start and not bot_status.get("is_running"):
            start_bot(final_symbols, bot_status.get("trade_amount", 150.0))
        return final_symbols

    add_system_log(f"🎯 [跟隨幣池] 跟隨來源更新為: {', '.join(final_symbols)}", "success")
    bot_status["active_symbols"] = final_symbols

    since_restart = time.time() - last_bot_restart
    if since_restart < BOT_RESTART_COOLDOWN:
        remaining = int(BOT_RESTART_COOLDOWN - since_restart)
        add_system_log(f"⏳ [跟隨幣池] 換倉冷卻中，剩餘 {remaining} 秒，暫不重啟", "warning")
        return final_symbols

    if bot_status.get("is_running") or force_start:
        last_bot_restart = time.time()
        start_bot(final_symbols, bot_status.get("trade_amount", 150.0))

    return final_symbols


def auto_radar_switch(force_start=False):
    global last_api_call
    if not radar_lock.acquire(blocking=False):
        add_system_log("⚠️ [雷達掃描] 前一次掃描尚未完成，跳過", "warning")
        return get_bot_status().get("active_symbols", [])
    try:
        if FOLLOW_SYMBOLS_FROM:
            add_system_log(f"🔗 [跟隨幣池] FOLLOW_SYMBOLS_FROM={FOLLOW_SYMBOLS_FROM}，本部署將跟隨來源幣種清單", "info")
            return _follow_source_radar_switch(force_start=force_start)

        add_system_log(f"📡 [雷達掃描] 中高動能 {RADAR_SELECT_COUNT} 幣 + 排除急升急跌...", "warning")

        elapsed = time.time() - last_api_call
        if elapsed < API_RATE_LIMIT:
            time.sleep(API_RATE_LIMIT - elapsed)
        last_api_call = time.time()

        bot_status = get_bot_status()
        current_syms = bot_status.get("active_symbols", [])

        clean_blacklist()
        scan_pool = [s for s in ATR_ELIGIBLE_SYMBOLS if s not in BLACKLIST]
        _, full_ranking = get_atr_ranked_coins(scan_pool, limit=CORE_SELECT_COUNT)

        if not full_ranking:
            add_system_log("⚠️ [雷達掃描] 無法計算 ATR 排名，維持原狀", "warning")
            return current_syms

        ranking_str = " | ".join([f"{r['symbol'].replace('USDT','')} ATR{r['atr_pct']:.2f}% 1h{r.get('one_h_vol_pct', 0):.2f}%" for r in full_ranking[:10]])
        add_system_log(f"📊 [ATR排名] {ranking_str}", "info")

        open_syms = _get_open_position_symbols()

        rank_map_raw = {r["symbol"]: (i + 1, r["atr_pct"], r["price"], r.get("one_h_vol_pct", 0.0)) for i, r in enumerate(full_ranking)}
        filtered_top = []
        filtered_out = []
        for r in full_ranking:
            if len(filtered_top) >= CORE_SELECT_COUNT:
                break
            sym = r["symbol"]
            atr_pct = r["atr_pct"]
            one_h_vol = float(r.get("one_h_vol_pct", 0.0) or 0.0)
            change_pct = abs(float(r.get("change_pct", 0.0) or 0.0))
            if atr_pct < MIN_ATR_PCT_FOR_ENTRY:
                filtered_out.append(f"{sym}(ATR{atr_pct:.2f}%太低)")
            elif atr_pct > MAX_ATR_PCT_FOR_ENTRY:
                filtered_out.append(f"{sym}(ATR{atr_pct:.2f}%太高)")
            elif one_h_vol < MIN_1H_VOL_PCT_FOR_ENTRY:
                filtered_out.append(f"{sym}(1h{one_h_vol:.2f}%太靜)")
            elif one_h_vol > MAX_1H_VOL_PCT_FOR_ENTRY:
                filtered_out.append(f"{sym}(1h{one_h_vol:.2f}%太急)")
            elif change_pct > MAX_24H_ABS_CHANGE_PCT_FOR_ENTRY:
                filtered_out.append(f"{sym}(24h±{change_pct:.1f}%過熱)")
            else:
                filtered_top.append(sym)

        if filtered_out:
            add_system_log(f"⚠️ [動能濾網] 已排除不合適幣種: {', '.join(filtered_out)}", "warning")

        top_symbols = filtered_top
        all_preserved = [s for s in open_syms if s not in top_symbols]
        if all_preserved:
            add_system_log(f"🔒 [持倉保護] 強制保留持倉幣種: {', '.join(all_preserved)}", "warning")

        # ── AI 輔助自動分析：為核心幣種計算動態個性 ──
        rank_map = rank_map_raw
        dynamic_profiles = {}
        analysis_lines = []
        for i, sym in enumerate(top_symbols):
            rank, atr_pct, price, one_h_vol = rank_map.get(sym, (i + 1, 0.0, 0.0, 0.0))
            prof = _compute_dynamic_profile(sym, atr_pct, price, rank, len(full_ranking))
            dynamic_profiles[sym] = prof
            tag = prof.get("_radar_tag", "")
            analysis_lines.append(
                f"{sym.replace('USDT','')} ATR{atr_pct:.2f}% 1h{one_h_vol:.2f}% → "
                f"lev{prof['leverage']}x SL{prof['sl_atr_multiplier']}x TP{prof['tp_atr_multiplier']}x [{tag}]"
            )

        # ── 熱門動能幣掃描 ───────────────────────────────────────────────
        # 防範：成交量>$5M、漲幅8-40%（動能強但未極端拉爆）、確認永續合約
        ignore_for_hot = set(CORE_SYMBOLS) | set(BLACKLIST.keys()) | set(open_syms)
        hot_raw = _get_hot_movers(
            min_vol_usdt=5_000_000,
            min_change_pct=8.0,
            max_change_pct=40.0,
            limit=HOT_MOVERS_COUNT,
            ignore_list=list(ignore_for_hot),
        )
        hot_symbols = [h["symbol"] for h in hot_raw]
        if hot_symbols:
            hot_info = "  ".join(
                f"{h['symbol'].replace('USDT','')} +{h['change_pct']:.1f}% vol${h['q_vol']/1e6:.0f}M"
                for h in hot_raw
            )
            add_system_log(f"🔥 [熱門動能] 發現 {len(hot_symbols)} 個新星: {hot_info}", "warning")
            for idx, h in enumerate(hot_raw):
                sym = h["symbol"]
                prof = dict(HOT_MOVER_PROFILE_BASE)
                prof["_radar_atr_pct"] = 0.0
                prof["_radar_rank"]    = len(full_ranking) + idx + 1
                prof["_radar_tag"]     = f"熱門動能/24h+{h['change_pct']:.1f}%"
                dynamic_profiles[sym] = prof
                analysis_lines.append(
                    f"{sym.replace('USDT','')} +{h['change_pct']:.1f}%24h → lev2x SL3x TP6x [熱門動能/保守]"
                )
        else:
            add_system_log("🔥 [熱門動能] 無符合條件幣種（需 24h漲8-40%、成交量>$5M）", "info")

        _save_radar_profiles(dynamic_profiles)
        add_system_log(f"🤖 [AI個性] 已為 {len(dynamic_profiles)} 幣設定個性:", "info")
        for line in analysis_lines:
            add_system_log(f"   ↳ {line}", "info")

        # 合併最終幣列：持倉保護 + 核准清單內的 ATR 核心幣。
        core_limit = max(0, RADAR_SELECT_COUNT - len(all_preserved))
        final_symbols = all_preserved + top_symbols[:core_limit]

        # 排序讓比較不受順序影響
        if sorted(final_symbols) == sorted(current_syms):
            add_system_log(f"✅ [雷達掃描] 榜單未變 ({', '.join(final_symbols)})，維持不變", "success")
            if force_start and not bot_status.get("is_running"):
                start_bot(final_symbols, bot_status.get("trade_amount", 150.0))
            return final_symbols

        active_core = top_symbols[:core_limit]
        hot_str = f" + 熱門 {', '.join(hot_symbols)}" if hot_symbols else ""
        add_system_log(f"🎯 [雷達鎖定] 核心 {', '.join(active_core)}{hot_str}", "success")
        if all_preserved:
            add_system_log(f"🔒 [持倉保護] 保留持倉幣種: {', '.join(all_preserved)}", "warning")
        bot_status["active_symbols"] = final_symbols
        save_symbol_config(final_symbols)

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


def _find_atr_replacement(current_syms):
    try:
        clean_blacklist()
        ignore_list = list(set(current_syms) | set(BLACKLIST.keys()))
        scan_pool = [s for s in ATR_ELIGIBLE_SYMBOLS if s not in ignore_list]
        replacement_candidates, _ = get_atr_ranked_coins(scan_pool, limit=RADAR_SELECT_COUNT + 5)
        for sym in replacement_candidates:
            if sym not in current_syms:
                return sym
    except Exception as e:
        add_system_log(f"⚠️ [補位] ATR 補幣失敗: {e}", "warning")
    return None


def replace_dead_coin(symbol: str):
    try:
        bot_status = get_bot_status()
        current_syms = bot_status.get("active_symbols", [])
        if symbol in current_syms:
            current_syms.remove(symbol)

        add_system_log(f"💀 [死水汰換] 剔除無波動死水幣 {symbol}，尋找替補...", "warning")

        new_coin = _find_atr_replacement(current_syms)
        if new_coin:
            current_syms.append(new_coin)
            bot_status["active_symbols"] = current_syms
            save_symbol_config(current_syms)
            add_system_log(f"✨ [自動補位] 成功選入候補幣種: {new_coin}", "success")
            start_bot(current_syms, bot_status.get("trade_amount", 10.0))
        else:
            add_system_log(f"⚠️ [自動補位] 找不到合適的候補小幣", "danger")
    except Exception as e:
        add_system_log(f"🚨 [自動補位] 發生錯誤: {e}", "danger")


# 動能不足自動換幣冷卻：同一幣種兩次換幣間至少間隔 10 分鐘，避免頻繁重啟
_momentum_swap_cooldown: dict[str, float] = {}
MOMENTUM_SWAP_COOLDOWN_SEC = 600  # 10 分鐘


def check_momentum_and_swap():
    """
    自動動能監控換幣。
    每次呼叫時掃描目前監控幣種的 ATR% 與 1h 波動度；
    若某幣無持倉且動能持續不足（低於篩選門檻），就將其汰換為池中動能最高的替補。

    設計原則：
    - 有持倉的幣種絕對不換（避免倉位被強制移除）
    - 同一幣種 10 分鐘內只換一次（避免連續重啟震盪）
    - 只有在找得到更好替補幣時才換，找不到就維持原狀
    """
    global _momentum_swap_cooldown
    try:
        from services.binance_service import get_atr_ranked_coins
        bot_status = get_bot_status()
        current_syms = list(bot_status.get("active_symbols", []))
        if not current_syms:
            return

        # 取得目前這些幣的即時 ATR / 1h 波動
        _, ranks = get_atr_ranked_coins(current_syms, limit=len(current_syms) + 5)
        rank_map = {r["symbol"]: r for r in ranks}

        # 取得目前有持倉的幣（這些不可換）
        open_syms = set(_get_open_position_symbols())

        swapped = []
        for sym in list(current_syms):
            if sym in open_syms:
                continue  # 有倉位不動

            # 冷卻中也跳過
            cooldown_until = _momentum_swap_cooldown.get(sym, 0)
            if time.time() < cooldown_until:
                continue

            r = rank_map.get(sym)
            if not r:
                continue

            atr_pct  = float(r.get("atr_pct", 0) or 0)
            one_h    = float(r.get("one_h_vol_pct", 0) or 0)
            change24 = abs(float(r.get("change_pct", 0) or 0))

            # 判斷動能是否不足
            too_low_atr  = atr_pct < MIN_ATR_PCT_FOR_ENTRY
            too_high_atr = atr_pct > MAX_ATR_PCT_FOR_ENTRY
            too_quiet_1h = one_h < MIN_1H_VOL_PCT_FOR_ENTRY
            too_hot_1h   = one_h > MAX_1H_VOL_PCT_FOR_ENTRY
            too_hot_24h  = change24 > MAX_24H_ABS_CHANGE_PCT_FOR_ENTRY
            momentum_dead = too_low_atr or too_high_atr or too_quiet_1h or too_hot_1h or too_hot_24h

            if not momentum_dead:
                continue

            # 找出動能不足原因（供 log 使用）
            reason_parts = []
            if too_low_atr:  reason_parts.append(f"ATR={atr_pct:.2f}%<{MIN_ATR_PCT_FOR_ENTRY}%")
            if too_high_atr: reason_parts.append(f"ATR={atr_pct:.2f}%>{MAX_ATR_PCT_FOR_ENTRY}%")
            if too_quiet_1h: reason_parts.append(f"1h={one_h:.2f}%<{MIN_1H_VOL_PCT_FOR_ENTRY}%")
            if too_hot_1h:   reason_parts.append(f"1h={one_h:.2f}%>{MAX_1H_VOL_PCT_FOR_ENTRY}%")
            if too_hot_24h:  reason_parts.append(f"24h={change24:.1f}%過熱")
            reason_str = "、".join(reason_parts)

            add_system_log(
                f"📉 [動能不足] {sym} 動能衰退（{reason_str}），尋找替補幣種...",
                "warning"
            )

            # 執行換幣
            _momentum_swap_cooldown[sym] = time.time() + MOMENTUM_SWAP_COOLDOWN_SEC
            replace_dead_coin(sym)
            swapped.append(sym)
            # 一次只換一檔，避免連鎖重啟
            break

        if not swapped:
            # 所有幣動能正常，靜默通過（不輸出 log 以免刷版）
            pass

    except Exception as e:
        add_system_log(f"⚠️ [動能監控] 掃描失敗: {e}", "warning")
