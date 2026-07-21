import os
import json
import time
import threading
from services.system_log_service import add_system_log
from services.bot_manager_service import get_bot_status, start_bot, kill_bot, save_symbol_config
from services.binance_service import get_atr_ranked_coins, get_hot_movers as _get_hot_movers
from core.ctx import CACHE
from core.config import COIN_PROFILE_CONFIG, TRADE_POOL_SIZE

SYMBOL_CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "bot_symbols.json")


def _resolve_follow_symbols_from(base_dir: str | None = None) -> str:
    """Resolve the shared symbol source path for strategy sync between deployments.

    Only follows when explicitly set via FOLLOW_SYMBOLS_FROM environment variable.
    """
    configured = os.getenv("FOLLOW_SYMBOLS_FROM", "").strip()
    if configured:
        return configured
    return ""


# 若設定此環境變數（指向另一份部署的 bot_symbols.json 絕對路徑），本部署不再自己跑 ATR 雷達
# 掃描，而是直接跟隨來源部署選出的幣種清單，用來讓 8006 長期跟隨 8005 的幣池。
# 若未明確指定，則會自動從相鄰部署的 bot_symbols.json 讀取，保留各自帳務但同步策略幣池。
FOLLOW_SYMBOLS_FROM = _resolve_follow_symbols_from()
# 若在 8006 部署中設定此變數，則會跟隨來源部署的 bot_symbols.json，不自行跑 ATR 雷達掃描。
# 來源清單會寫入本地 bot_symbols.json，並保留本地持倉幣種。


def _radar_route_class(route: str | None) -> str:
    route_key = str(route or "").lower()
    if route_key in ("range", "range_support_long", "range_resistance_short"):
        return "range"
    if route_key in ("ma_cross", "cross", "ma25_pullback", "trend_wait"):
        return "ma"
    return "strict"


def radar_eligibility(row: dict, route: str | None = None) -> tuple[bool, str, str]:
    """依實際策略分類雷達波動資格，並回傳可供介面顯示的精確原因。"""
    route_class = _radar_route_class(route or row.get("entry_setup"))
    atr_pct = float(row.get("atr_pct", row.get("_radar_atr_pct", 0.0)) or 0.0)
    one_h = float(row.get("one_h_vol_pct", row.get("_radar_one_h_vol_pct", 0.0)) or 0.0)
    change_pct = abs(float(row.get("change_pct", row.get("_radar_change_pct", 0.0)) or 0.0))

    if route_class == "range":
        max_atr, max_one_h, label = MAX_ATR_PCT_FOR_RANGE_ENTRY, MAX_1H_VOL_PCT_FOR_RANGE_ENTRY, "Range"
    elif route_class == "ma":
        max_atr, max_one_h, label = MAX_ATR_PCT_FOR_MA_ENTRY, MAX_1H_VOL_PCT_FOR_MA_ENTRY, "MA"
    else:
        max_atr, max_one_h, label = MAX_ATR_PCT_FOR_ENTRY, MAX_1H_VOL_PCT_FOR_ENTRY, "Breakout"

    if atr_pct <= 0 or one_h <= 0:
        missing = "ATR" if atr_pct <= 0 else "1H 波動"
        return False, f"僅監控：{missing}資料不足，等待下次雷達更新", route_class
    if atr_pct < MIN_ATR_PCT_FOR_ENTRY:
        return False, f"僅監控：{label} ATR {atr_pct:.2f}% 低於 {MIN_ATR_PCT_FOR_ENTRY:.2f}%", route_class
    if atr_pct > max_atr:
        return False, f"僅監控：{label} ATR {atr_pct:.2f}% 高於 {max_atr:.2f}%", route_class
    if one_h > max_one_h:
        return False, f"僅監控：{label} 1H 波動 {one_h:.2f}% 高於 {max_one_h:.2f}%", route_class
    if change_pct > MAX_24H_ABS_CHANGE_PCT_FOR_ENTRY:
        return False, f"僅監控：24H 漲跌 {change_pct:.2f}% 超過 {MAX_24H_ABS_CHANGE_PCT_FOR_ENTRY:.2f}%", route_class
    return True, f"{label} 波動資格通過", route_class


def is_strict_radar_eligible(row: dict) -> bool:
    """自動、手動與 UI 共用；依雷達辨識到的 setup 套用分類門檻。"""
    return radar_eligibility(row)[0]


def prioritize_entry_ready(rows):
    """Rank safe markets by MA setup proximity before general momentum."""
    return sorted(
        rows,
        key=lambda row: (
            row.get("entry_direction", "none") in ("long", "short"),
            float(row.get("entry_readiness_score", 0.0) or 0.0),
            float(row.get("momentum_score", 0.0) or 0.0),
        ),
        reverse=True,
    )


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
        lev_cap   = min(lev_cap, 3)
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
    tp_mult      = round(min(8.0, max(4.0, base.get("tp_atr_multiplier", 6.0) * rank_factor)), 1)

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



def save_symbol_config(symbols: list):
    try:
        data = {}
        if os.path.exists(SYMBOL_CONFIG_PATH):
            with open(SYMBOL_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        
        # 保留雷達選出的完整 12 幣清單。
        data["symbols"] = symbols
        with open(SYMBOL_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"⚠️ [Radar] save config failed: {e}")


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
        # auto_radar_switch 也會在交易主程序內免重啟執行；寫檔後同步更新
        # 原 dict，避免 UI 已是新資格、交易核心卻仍使用舊資格。
        import core.symbol_profile as _symbol_profile
        _symbol_profile.SYMBOL_PROFILES.clear()
        _symbol_profile.SYMBOL_PROFILES.update(profiles)
    except Exception as e:
        add_system_log(f"⚠️ [AI個性] 寫入 profiles 失敗: {e}", "warning")

ATR_ELIGIBLE_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "DOGEUSDT", "ADAUSDT", "LINKUSDT", "AVAXUSDT", "SUIUSDT",
    "NEARUSDT", "AAVEUSDT", "XLMUSDT", "HYPEUSDT", "ZECUSDT",
]
CORE_SYMBOLS = list(ATR_ELIGIBLE_SYMBOLS)
# 選幣數擴大到 15：新倉條件變嚴後，需要更多候選給 5 個倉位槽篩選。
# 最大持倉仍由 MAX_POSITIONS 控制，不會因監控 15 檔而同時開更多單。
RADAR_SELECT_COUNT = 25
HOT_MOVERS_COUNT   = 0
CORE_SELECT_COUNT  = len(ATR_ELIGIBLE_SYMBOLS)

# 雷達門檻依策略分類：Breakout 維持嚴格；MA 適度放寬；Range 由局部結構風控。
# 共用最低波動與 24H 極端漲跌保護，實際送單前再依真正 route 重驗。
from core.config import (MIN_ATR_PCT_FOR_ENTRY, MAX_ATR_PCT_FOR_ENTRY,
    MIN_1H_VOL_PCT_FOR_ENTRY, MAX_1H_VOL_PCT_FOR_ENTRY,
    MAX_ATR_PCT_FOR_MA_ENTRY, MAX_1H_VOL_PCT_FOR_MA_ENTRY,
    MAX_ATR_PCT_FOR_RANGE_ENTRY, MAX_1H_VOL_PCT_FOR_RANGE_ENTRY,
    MAX_24H_ABS_CHANGE_PCT_FOR_ENTRY)

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

# 永久黑名單：只排除幣安 Futures 上架的「非加密幣」合約（股票/ETF/商品）
# TUSDT（Threshold Network）、KITEUSDT（KiteAI）等為真實加密幣，不列入。
# 用 float('inf') 代表永不解除。
BLACKLIST = {
    "OGNUSDT":    float('inf'),  # 問題幣
    # --- 股票/ETF/商品型合約（幣安近期上架的非加密幣衍生品）---
    "SKHYNIXUSDT":float('inf'),  # SK Hynix 半導體股票
    "KORUUSDT":   float('inf'),  # iShares MSCI Korea ETF
    "SNDKUSDT":   float('inf'),  # SanDisk 股票
    "SOXLUSDT":   float('inf'),  # Direxion 半導體 3x ETF
    "XAUUSDT":    float('inf'),  # 黃金現貨
    "XAGUSDT":    float('inf'),  # 白銀現貨
    "MUSDT":      float('inf'),  # Micron Technology 股票
    "CLUSDT":     float('inf'),  # WTI 原油
    "SPCXUSDT":   float('inf'),  # SPCX ETF
    "LABUSDT":    float('inf'),  # LABU 生技 ETF
    "DRAMUSDT":   float('inf'),  # DRAM 記憶體指數
    "BZUSDT":     float('inf'),  # Brent 原油
    "EWYUSDT":    float('inf'),  # iShares MSCI Korea ETF
    "MRVLUSDT":   float('inf'),  # Marvell Technology 股票
    "MSTRUUSDT":  float('inf'),  # MicroStrategy 股票
    "NVDAAUSDT":  float('inf'),  # NVIDIA 股票
    "INTCUSDT":   float('inf'),  # Intel 股票
    "PAXGUSDT":   float('inf'),  # PAX Gold（黃金代幣）
    "QQQUUSDT":   float('inf'),  # QQQ ETF
    "BILLUSDT":   float('inf'),  # 異常超高漲幅幣，排除
    "ZBTUUSDT":   float('inf'),  # 異常超高漲幅幣，排除
    "TRIAUSDT":   float('inf'),  # 異常超高漲幅幣，排除
}

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


from services.binance_service import get_dynamic_top_15_coins

def auto_radar_switch(force_start=False, restart_on_change=True):
    """更新固定 15 檔交易池的波動資格與動態風控參數。"""
    status_before_scan = get_bot_status()
    clean_blacklist()
    # 全市場動態掃描 (limit=80 確保足夠候選)
    _, ranking = get_atr_ranked_coins(symbols=None, limit=80, blacklist=BLACKLIST)

    # ── 加密幣過濾：排除股票/ETF/商品型合約 ──
    # 只用明確的關鍵字排除，避免誤傷真實加密幣（如 KITEUSDT、TUSDT 等）
    _NON_CRYPTO_KEYWORDS = [
        'SKHYNIX','KORU','SNDK','SOXL','XAU','XAG','SPCX','LABU',
        'DRAM','EWY','MRVL','MSTR','NVDA','INTC','PAXG','QQQ',
        'MSFT','GOOGL','AMZN','AAPL','TSLA','NFLX',
    ]
    
    def _is_crypto(sym: str) -> bool:
        # 排除已知股票/ETF/商品關鍵字
        if any(kw in sym for kw in _NON_CRYPTO_KEYWORDS):
            return False
        # 排除包含底線的衍生合約（如 BTC_DOM）
        if '_' in sym:
            return False
        # 排除穩定幣
        base = sym.replace('USDT', '')
        if base in ('BUSD','USDC','DAI','TUSD','FDUSD','PYUSD'):
            return False
        # 排除槓桿代幣
        if any(base.endswith(s) for s in ('BULL','BEAR','UP','DOWN','3L','3S','2L','2S')):
            return False
            
        return True

    ranking = [r for r in ranking if _is_crypto(r['symbol'])]
    eligible = prioritize_entry_ready([r for r in ranking if is_strict_radar_eligible(r)])
    from core.config import DEFAULT_SYMBOLS
    ranking_by_symbol = {row["symbol"]: row for row in ranking}
    selected_rows = [ranking_by_symbol[sym] for sym in DEFAULT_SYMBOLS if sym in ranking_by_symbol]

    try:
        with open(SYMBOL_CONFIG_PATH, "r", encoding="utf-8") as f:
            previous_profiles = (json.load(f) or {}).get("profiles", {})
    except Exception:
        previous_profiles = {}
    strict_symbols = {r["symbol"] for r in eligible}

    now = time.time()
    profiles = {}
    for idx, row in enumerate(selected_rows):
        sym = row["symbol"]
        profile = _compute_dynamic_profile(sym, row["atr_pct"], row["price"], idx + 1, len(selected_rows))
        previous = previous_profiles.get(sym, {}) if isinstance(previous_profiles, dict) else {}
        strict_now = sym in strict_symbols
        strict_ok, strict_reason, route_class = radar_eligibility(row)
        # Range 可能要等 1m 支撐／壓力成形後才辨識；先以 Range 上限累積觀察期，
        # 實際送單時仍會依真正 route 重新檢查，不讓高波動 Breakout 借道放行。
        observation_now = strict_ok or radar_eligibility(row, "Range")[0]
        observation_before = bool(previous.get("_radar_observation_eligible", previous.get("_radar_strict_eligible", False)))
        confirmations = int(previous.get("_radar_confirmations", 0) or 0) + 1 if observation_now and observation_before else (1 if observation_now else 0)
        first_seen = float(previous.get("_radar_candidate_since", now) or now) if observation_now and observation_before else now
        observed_sec = max(0.0, now - first_seen)
        trade_eligible = strict_now and confirmations >= 2 and observed_sec >= 1800
        if not strict_now:
            reason = strict_reason
        elif confirmations < 2:
            reason = "觀察中：等待第二次雷達確認"
        elif observed_sec < 1800:
            reason = f"觀察中：尚需 {int((1800-observed_sec)/60)+1} 分鐘"
        else:
            reason = "可交易：連續兩次雷達合格且觀察滿 30 分鐘"
        profile.update({
            "_radar_strict_eligible": strict_now,
            "_radar_observation_eligible": observation_now,
            "_radar_observation_mature": confirmations >= 2 and observed_sec >= 1800,
            "_radar_confirmations": confirmations,
            "_radar_candidate_since": first_seen,
            "_radar_route_class": route_class,
            "_radar_atr_pct": float(row.get("atr_pct", 0.0) or 0.0),
            "_radar_one_h_vol_pct": float(row.get("one_h_vol_pct", 0.0) or 0.0),
            "_radar_change_pct": float(row.get("change_pct", 0.0) or 0.0),
            "_radar_entry_readiness": float(row.get("entry_readiness_score", 0.0) or 0.0),
            "_radar_entry_direction": row.get("entry_direction", "none"),
            "_trade_eligible": trade_eligible,
            "_trade_eligibility_reason": reason,
        })
        profiles[sym] = profile

    best_symbols = list(DEFAULT_SYMBOLS)

    if not best_symbols:
        add_system_log("⚠️ [動態選幣] 無法取得任何幣種，維持現狀", "warning")
        return get_bot_status().get("active_symbols", [])

    # 2. 將選出的主流幣種寫入 bot_symbols.json
    trade_symbols = best_symbols[:TRADE_POOL_SIZE]
    save_symbol_config(trade_symbols)
    _save_radar_profiles(profiles)
    
    add_system_log(f"🎯 [固定幣池] 已更新 15 檔交易資格: {', '.join(trade_symbols)}", "success")
    
    # 3. 雷達保留 25 檔候選資料，但交易核心只接收資格排序後前 15 檔。
    symbols_changed = set(status_before_scan.get("active_symbols", [])) != set(trade_symbols)
    if restart_on_change and (force_start or (status_before_scan.get("is_running") and symbols_changed)):
        # 注意：start_bot 會處理重新啟動邏輯
        start_bot(trade_symbols, status_before_scan.get("trade_amount", 150.0))
    
    return trade_symbols


def _find_atr_replacement(current_syms):
    try:
        clean_blacklist()
        ignore_list = list(set(current_syms) | set(BLACKLIST.keys()))
        replacement_candidates, _ = get_atr_ranked_coins(symbols=None, limit=RADAR_SELECT_COUNT + 5, blacklist=ignore_list)
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
