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
# 選幣數擴大到 12：新倉條件變嚴後，需要更多候選給 3 個倉位槽篩選。
# 最大持倉仍由 MAX_POSITIONS 控制，不會因監控 12 檔而同時開更多單。
RADAR_SELECT_COUNT = 23
HOT_MOVERS_COUNT   = 0    # 不追熱門暴衝榜，避免急升急跌標的進入監控池
CORE_SELECT_COUNT  = len(ATR_ELIGIBLE_SYMBOLS)

# 動能篩選門檻（12 檔候選版）：
#   ATR 2.5%~7.2%：保留中高動能，允許 NEAR/ADA/AAVE 這類高流動性強波動候選進池。
#   1h 波動 0.42%~2.8%：條件變嚴後放寬候選池，但仍排除完全不動的死水幣。
from core.config import MIN_ATR_PCT_FOR_ENTRY, MAX_ATR_PCT_FOR_ENTRY, MIN_1H_VOL_PCT_FOR_ENTRY, MAX_1H_VOL_PCT_FOR_ENTRY, MAX_24H_ABS_CHANGE_PCT_FOR_ENTRY

MIN_ATR_PCT_FOR_ENTRY = MIN_ATR_PCT_FOR_ENTRY
MAX_ATR_PCT_FOR_ENTRY = MAX_ATR_PCT_FOR_ENTRY
MIN_1H_VOL_PCT_FOR_ENTRY = MIN_1H_VOL_PCT_FOR_ENTRY
MAX_1H_VOL_PCT_FOR_ENTRY = MAX_1H_VOL_PCT_FOR_ENTRY
MAX_24H_ABS_CHANGE_PCT_FOR_ENTRY = MAX_24H_ABS_CHANGE_PCT_FOR_ENTRY

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
    # 固定監控幣種，不再跑動態 ATR 雷達掃描/換幣。
    # 15 個大幣（BTC/ETH/BNB 撐流動性 + 其餘波動度較高的大型幣）+ 8 個中小型幣
    # （實測峰值平均 0.32%，是大幣 0.13% 的 2.5 倍，波動度明顯更夠，加回來補足
    # 大幣過悶、單子跑不動的問題），中小型幣沿用原 ATR_ELIGIBLE_SYMBOLS 篩過的
    # 名單（已排除過低價/易暴衝幣）。
    return [
        "BTCUSDT", "ETHUSDT", "BNBUSDT",
        "SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT",
        "LINKUSDT", "SUIUSDT", "DOTUSDT", "NEARUSDT", "APTUSDT",
        "LTCUSDT", "BCHUSDT",
        "UNIUSDT", "ETCUSDT", "AAVEUSDT", "ATOMUSDT", "HBARUSDT",
        "XLMUSDT", "INJUSDT", "RENDERUSDT",
    ]


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
            current_syms = current_syms[:RADAR_SELECT_COUNT]
            bot_status["active_symbols"] = current_syms
            bot_status["watch_symbols"] = current_syms
            save_symbol_config(current_syms)

            # 這個函式有兩種呼叫情境：(1) 正在運行的 main.py 自己的背景工作
            # （core/runner.py periodic_momentum_swap，同一個 process、同一份 ctx
            # 記憶體），(2) API 行程監看舊架構 exit code 分支（目前無實際觸發路徑，
            # 屬於舊架構殘留，ctx 在那邊是空的）。原本不分情境一律呼叫 start_bot()
            # 整個重啟，但單純換一檔幣完全不需要砍掉重練——實測換一次要花快 1 分鐘
            # 重抓 SMA200/EMA50(1H)/EMA15m + ATR 暖機，市場安靜、動能不足幣種一多，
            # 幾乎每 60~70 秒就換一次，程序 9 成時間都在初始化，真正在跑進場掃描的
            # 時間少得可憐（實測一小時內重啟 17 次，完全開不了倉）。改成偵測到自己
            # 就是活著的 bot process（symbol 在目前 ctx.STATES 裡）時，直接在記憶體
            # 內更新監控池，不重啟；不是同一個 process 才退回原本整個重啟。
            from core import ctx
            if symbol in ctx.STATES and ctx.ALL_SYMBOLS:
                from core.state_manager import build_symbol_state
                from core.symbol_profile import apply_symbol_profile, SYMBOL_PROFILES, save_symbol_pool
                if symbol in ctx.ALL_SYMBOLS:
                    ctx.ALL_SYMBOLS.remove(symbol)
                if new_coin not in ctx.ALL_SYMBOLS:
                    ctx.ALL_SYMBOLS.append(new_coin)
                    ctx.STATES[new_coin] = build_symbol_state(new_coin)
                    apply_symbol_profile(new_coin, SYMBOL_PROFILES.get(new_coin, {}))
                save_symbol_pool(ctx.ALL_SYMBOLS)
                add_system_log(f"✨ [自動補位-免重啟] 成功選入候補幣種: {new_coin}（原地換幣，不重啟程序）", "success")
            else:
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
    # 固定 15 幣清單模式下停用：這個函式原本會每 5 分鐘掃描監控池，把「動能不足」
    # 的幣自動換成 _find_atr_replacement() 從 ATR_ELIGIBLE_SYMBOLS（跟使用者精選的
    # 15 幣清單不是同一份名單，例如包含 UNIUSDT）挑出的替補——這正是 APTUSDT 被
    # 靜默換成 UNIUSDT 的真正原因，跟已停用的冷卻補位是同一類問題：幣池固定後，
    # 任何「自動找替補」的機制都會用未經篩選的名單稀釋掉精選清單。停用後，監控池
    # 完全由 auto_radar_switch() 的固定清單決定，不再有背景機制動態替換。
    return
