import logging
import time
from core.config import (
    COIN_PROFILE_CONFIG, HARD_STOP_LOSS_PCT,
    MAX_STOPS_IN_WINDOW, BAN_WINDOW, BAN_DURATION,
)

logger = logging.getLogger(__name__)


def build_symbol_state(sym):
    conf = COIN_PROFILE_CONFIG.get(sym, {})
    return {
        "status": "ACTIVE",
        "error_strikes": 0,
        "is_banned": False,
        "sync_required": False,
        "last_exit_time": 0,
        "status_reason": "",
        "next_status_time": 0,
        "stop_count": 0,
        "first_stop_time": 0,
        "qty": 0.0,
        "avg_price": 0.0,
        "trailing_stop_price": 0.0,
        "open_time": 0.0,
        "current_atr": 0.0,
        "atr_history": [],
        "atr_ma20": 0.0,
        "current_rsi": 50.0,
        "ema20": 0.0,
        "ema50": 0.0,
        "macd_line": 0.0,
        "macd_signal": 0.0,
        "macd_hist": 0.0,
        "prev_macd_line": 0.0,
        "prev_macd_signal": 0.0,
        "bb_up": 0.0,
        "bb_mid": 0.0,
        "bb_low": 0.0,
        "vol_ma10": 0.0,
        "vol_ma20": 0.0,
        "current_vol": 0.0,
        "trailing_highest": 0.0,
        "trailing_lowest": float('inf'),
        "highest_profit_pct": 0.0,
        "has_partial_closed": False,
        "pending_stop_loss": False,
        "stop_loss_price": 0.0,
        "exchange_stop_order_id": None,
        "exchange_take_profit_order_id": None,
        "ohlcv": [],
        "closes": [],
        "tr_list": [],
        "prev_close": None,
        "last_trade_price": 0.0,
        "last_trade_qty": 0.0,
        "last_trade_side": "",
        "last_trade_time": 0.0,
        "trade_qty_history": [],
        "trade_price_history": [],
        "trade_signal_strength": 0.0,
        "trade_signal_reason": "",
        "pending_side": None,
        "pending_time": 0,
        "pending_signal_price": 0.0,
        "pending_confirm_high": 0,
        "pending_confirm_low": 0,
        "close_price": 0.0,
        "last_buy_time": 0,
        "signal_strength": 0.0,
        "pnl_history": [],
        "has_been_negative": False,
        "trail_tp_price": 0.0,
        "entry_count": 0,
        "avg_entry_price": 0.0,
        "max_additional_entries": 3,
        "entry_cooldown_sec": conf.get("entry_cooldown_sec", 45),
        "min_flip_time": conf.get("min_flip_time", 300),
        "profile_type": conf.get("profile_type", "Core_Trend"),
        "entry_size_pct": 0.5,
        "add_entry_pct": 0.25,
        "risk_multiplier": 1.0,
        "volume_threshold_factor": conf.get("volume_threshold_factor", 1.4),
        "volume_multiplier": conf.get("volume_multiplier", 1.0),
        "sl_atr_multiplier": conf.get("sl_atr_multiplier", 1.5),
        "tp_atr_multiplier": conf.get("tp_atr_multiplier", 2.5),
        # 原本這裡不管 conf 內容、一律寫死用全域 HARD_STOP_LOSS_PCT，導致每個幣種
        # 個別配置的 hard_sl_pct（COIN_PROFILE_CONFIG）從未真正套用到交易所實際掛的
        # STOP_MARKET 止損單（core/orders.py 讀的是這個 hard_stop_loss_pct 欄位算價）——
        # 欄位名稱對不起來，等於所有幣種的交易所止損單永遠都是同一個全域百分比。
        "hard_stop_loss_pct": conf.get("hard_sl_pct", HARD_STOP_LOSS_PCT),
        "personality": "balanced",
        "personality_source": "infer",
        "last_personality_update": 0.0,
        "last_entry_time": 0.0,
        "is_ordering": False,
        "last_action_time": 0.0,
        "rsi_extreme_low": conf.get("rsi_extreme_low", 20),
        "rsi_extreme_high": conf.get("rsi_extreme_high", 75),
        "rsi_recovery_hook": conf.get("rsi_recovery_hook", 30),
        "volatility_cap": conf.get("volatility_cap", 3.0),
    }


def _remove_cooldown_substitute(sym):
    """冷卻/封禁結束時，將原幣種復位到監控池，並移除候補幣種（若未開倉）。"""
    from core import ctx
    from core.symbol_profile import save_symbol_pool
    
    sub = ctx.COOLDOWN_SUBSTITUTES.pop(sym, None)
    
    # 🔄 [恢復冷卻幣種] 將原幣種加回到 ALL_SYMBOLS
    if sym not in ctx.ALL_SYMBOLS:
        ctx.ALL_SYMBOLS.append(sym)
        logger.info(f"🔄 [冷卻恢復] {sym} 冷卻期結束，已重新加入監控池")
        save_symbol_pool(ctx.ALL_SYMBOLS)
    
    if not sub:
        return

    # ♻️ [候補幣種清理] 若候補幣種未開倉，則移除
    sub_state = ctx.STATES.get(sub)
    if sub_state and abs(sub_state.get("qty", 0.0)) < 0.000001 and sub_state.get("entry_count", 0) == 0:
        if sub in ctx.ALL_SYMBOLS:
            ctx.ALL_SYMBOLS.remove(sub)
            save_symbol_pool(ctx.ALL_SYMBOLS)
        logger.info(f"♻️ [冷卻補位] {sym} 已恢復，移除暫時候補幣種 {sub}")
    else:
        # 候補幣種已有持倉或進場，保留為正式監控幣種——但這樣監控池會比冷卻開始前
        # 多一個（原幣種歸隊 + 候補轉正），一天下來好幾輪冷卻循環，池子會不斷往上
        # 長、從來不會縮回去（實測從 12 一路長到 21）。這裡順便檢查一次上限，超過
        # 就從池子裡挑一個「沒有持倉、沒有任何進場紀錄」的幣種移除，把池子縮回
        # RADAR_SELECT_COUNT，不影響任何現有持倉或正在進行中的其他冷卻。
        logger.info(f"✅ [冷卻補位] {sym} 已恢復，候補幣種 {sub} 已有部位或進場，轉為正式監控幣種")
        _enforce_symbol_pool_cap()


def _enforce_symbol_pool_cap():
    """監控池超過 RADAR_SELECT_COUNT 時，移除沒有持倉、沒有進場紀錄、也不是其他
    冷卻幣種正在使用中的候補，把池子縮回上限。"""
    from core import ctx
    from core.symbol_profile import save_symbol_pool
    try:
        from services.radar_service import RADAR_SELECT_COUNT
    except Exception:
        return
    if len(ctx.ALL_SYMBOLS) <= RADAR_SELECT_COUNT:
        return
    active_substitutes = set(ctx.COOLDOWN_SUBSTITUTES.values())
    changed = False
    for candidate in list(ctx.ALL_SYMBOLS):
        if len(ctx.ALL_SYMBOLS) <= RADAR_SELECT_COUNT:
            break
        if candidate in active_substitutes:
            continue
        st = ctx.STATES.get(candidate)
        if not st:
            continue
        if abs(st.get("qty", 0.0)) < 0.000001 and st.get("entry_count", 0) == 0:
            ctx.ALL_SYMBOLS.remove(candidate)
            changed = True
            logger.info(f"✂️ [監控池瘦身] {candidate} 無持倉無進場，移除以維持監控池上限 {RADAR_SELECT_COUNT}（現有 {len(ctx.ALL_SYMBOLS)}）")
    if changed:
        save_symbol_pool(ctx.ALL_SYMBOLS)


def _add_cooldown_substitute(sym):
    """幣種進入冷卻/封禁時，暫時從幣安永續合約市場找一個候補幣種補入，
    避免監控池在這段期間少一個可交易幣種。實際抓取放在背景執行緒，避免卡住主循環。"""
    import threading

    def _worker():
        from core import ctx
        from core.symbol_profile import apply_symbol_profile, SYMBOL_PROFILES, save_symbol_pool
        try:
            from services.binance_service import get_top_volume_altcoins
            from services.radar_service import clean_blacklist, BLACKLIST

            clean_blacklist()
            ignore_list = list(BLACKLIST.keys()) + list(ctx.ALL_SYMBOLS) + list(ctx.COOLDOWN_SUBSTITUTES.values())
            candidates = get_top_volume_altcoins(15, ignore_list=ignore_list)

            new_sym = None
            for c in candidates:
                if c not in ctx.ALL_SYMBOLS and c not in ctx.COOLDOWN_SUBSTITUTES.values():
                    new_sym = c
                    break

            if not new_sym:
                logger.info(f"⚠️ [冷卻補位] {sym} 冷卻中，幣安永續合約市場找不到合適的候補幣種")
                return

            ctx.ALL_SYMBOLS.append(new_sym)
            ctx.STATES[new_sym] = build_symbol_state(new_sym)
            apply_symbol_profile(new_sym, SYMBOL_PROFILES.get(new_sym, {}))
            ctx.COOLDOWN_SUBSTITUTES[sym] = new_sym
            save_symbol_pool(ctx.ALL_SYMBOLS)
            logger.info(f"✨ [冷卻補位] {sym} 進入冷卻，從幣安永續合約市場補入候補幣種 {new_sym} 維持監控池數量")
        except Exception as e:
            logger.info(f"🚨 [冷卻補位異常] {sym}: {e}")

    threading.Thread(target=_worker, daemon=True).start()


def update_states():
    from core import ctx
    now = time.time()
    
    # 處理 ALL_SYMBOLS 中的幣種狀態轉移
    for sym in ctx.ALL_SYMBOLS:
        s = ctx.STATES[sym]
        if s["status"] == "COOLDOWN" and now >= s["next_status_time"]:
            s["status"] = "ACTIVE"
            s["status_reason"] = ""
            logger.info(f"🔄 [狀態] {sym} 冷卻結束 → ACTIVE")
            _remove_cooldown_substitute(sym)
        if s["status"] == "BANNED" and now >= s["next_status_time"]:
            s["status"] = "ACTIVE"
            s["status_reason"] = ""
            s["stop_count"] = 0
            s["first_stop_time"] = 0
            logger.info(f"🔄 [狀態] {sym} 封禁解除 → ACTIVE")
            _remove_cooldown_substitute(sym)
    
    # 處理被移出後仍在冷卻的幣種（可能被暫時汰換，但冷卻結束後應恢復）
    for sym in list(ctx.STATES.keys()):
        if sym not in ctx.ALL_SYMBOLS:
            s = ctx.STATES[sym]
            if s["status"] in ("COOLDOWN", "BANNED") and now >= s["next_status_time"]:
                s["status"] = "ACTIVE"
                s["status_reason"] = ""
                if s["status"] == "BANNED":  # 若是封禁狀態解除
                    s["stop_count"] = 0
                    s["first_stop_time"] = 0
                # 檢查是否需要恢復到 ALL_SYMBOLS
                if sym in ctx.COOLDOWN_SUBSTITUTES:
                    _remove_cooldown_substitute(sym)
                elif sym not in ctx.ALL_SYMBOLS:
                    # 被汰換的幣種在冷卻結束後，若沒有候補紀錄，先不主動恢復（保留現有汰換決定）
                    logger.info(f"ℹ️  [離線恢復] {sym} 冷卻結束但不在監控池，保持待命狀態")


def mark_exit(sym, is_stop_loss=False, reason="", loss_pct=0.0):
    from core import ctx
    s = ctx.STATES[sym]
    now = time.time()
    s["status"] = "COOLDOWN"

    actual_cooldown = 1800 if is_stop_loss else 3600
    if abs(loss_pct) >= 0.02:
        actual_cooldown += 3600
        logger.info(f"⚠️ [大虧延罰] {sym} 虧損 {loss_pct*100:.2f}% ≥ 2%，冷卻額外延長 60 分鐘")
    s["next_status_time"] = now + actual_cooldown

    cd_min = actual_cooldown // 60
    s["status_reason"] = f"冷卻中 ({cd_min}分鐘) - {reason}"
    logger.info(f"⏳ [狀態] {sym} 平倉 ({reason}) → COOLDOWN {cd_min}分鐘")
    if is_stop_loss:
        s["stop_count"] += 1
        if s["stop_count"] == 1:
            s["first_stop_time"] = now
        if s["stop_count"] >= MAX_STOPS_IN_WINDOW and (now - s["first_stop_time"]) <= BAN_WINDOW:
            s["status"] = "BANNED"
            s["next_status_time"] = now + BAN_DURATION
            s["status_reason"] = f"封禁中 (24h，{MAX_STOPS_IN_WINDOW}次停損)"
            logger.info(f"🚫 [狀態] {sym} 1h內{MAX_STOPS_IN_WINDOW}次停損 → BANNED 24h")
        elif s["stop_count"] >= MAX_STOPS_IN_WINDOW:
            s["stop_count"] = 1
            s["first_stop_time"] = now

        # 連續虧損汰換機制 (consecutive_losses >= 2)
        losses = s.get("consecutive_losses", 0)
        if losses >= 2:
            logger.info(f"🔄 [連續虧損偵測] {sym} 已連續虧損 {losses} 次，準備進行幣種汰換...")
            try:
                from core.config import DEFAULT_SYMBOLS
                from core.symbol_profile import save_symbol_pool, apply_symbol_profile, SYMBOL_PROFILES
                
                # 尋找不在目前監聽列表中的候選幣種
                candidate_pool = list(DEFAULT_SYMBOLS)
                for c in COIN_PROFILE_CONFIG.keys():
                    if c not in candidate_pool:
                        candidate_pool.append(c)
                
                new_sym = None
                for c in candidate_pool:
                    if c not in ctx.ALL_SYMBOLS:
                        new_sym = c
                        break
                
                if new_sym:
                    # 執行汰換
                    if sym in ctx.ALL_SYMBOLS:
                        idx = ctx.ALL_SYMBOLS.index(sym)
                        ctx.ALL_SYMBOLS[idx] = new_sym
                    else:
                        ctx.ALL_SYMBOLS.append(new_sym)
                        
                    # 初始化新幣種狀態
                    ctx.STATES[new_sym] = build_symbol_state(new_sym)
                    apply_symbol_profile(new_sym, SYMBOL_PROFILES.get(new_sym, {}))
                    
                    # 存檔持久化
                    save_symbol_pool(ctx.ALL_SYMBOLS)
                    logger.info(f"✨ [連續虧損汰換成功] {sym} 被移出監控池，由新幣種 {new_sym} 替補監控！")
                else:
                    logger.info(f"⚠️ [連續虧損汰換失敗] 候選池中已無可用幣種來替補 {sym}")
            except Exception as replacement_err:
                logger.info(f"🚨 [連續虧損汰換異常] {sym}: {replacement_err}")
            return  # 已被永久汰換，不需再補位

    # 冷卻/封禁期間補位：讓監控池在這段期間維持原本可交易的幣種數量
    if sym in ctx.ALL_SYMBOLS and sym not in ctx.COOLDOWN_SUBSTITUTES:
        _add_cooldown_substitute(sym)


def update_state_with_fill(sym, order_data):
    """
    使用交易所回傳的真實成交資料更新內部狀態。
    """
    from core import ctx
    if sym not in ctx.STATES:
        return

    s = ctx.STATES[sym]
    
    # 提取成交數量與價格
    # 幣安的成交資料中，qty 可能為負數（代表賣出），所以取絕對值
    fill_qty = abs(float(order_data.get("filledQty", 0)))
    fill_price = float(order_data.get("avgPrice", 0))
    
    if fill_qty > 0 and fill_price > 0:
        # 更新持倉數量 (做多為正，做空為負)
        # 這裡的 logic 需要根據訂單方向來決定，但我們通常在成交後會重新同步或根據 order_data 的 side 判斷
        # 為了簡單且準確，我們直接將成交量加到目前的 qty 上（如果是買入，qty 增加；如果是賣出，qty 減少）
        # 但因為 we are calling this in a context where we just placed a market buy/short,
        # we can just set it directly if it's the first fill.
        
        # 獲取訂單方向
        side = order_data.get("side") # "BUY" 或 "SELL"
        
        if side == "BUY":
            s["qty"] += fill_qty
        else:
            s["qty"] -= fill_qty
            
        # 更新平均價格
        # 簡單處理：如果是第一次進場，直接設為成交價。若是多次進場，則進行加權平均。
        if s["entry_count"] == 0:
            s["avg_price"] = fill_price
            s["open_time"] = time.time()
        else:
            # 加權平均公式: (舊總成本 + 新成交成本) / 新總數量
            old_total_cost = s["avg_price"] * abs(s["qty"] - fill_qty) # 這邊邏輯略複雜，因為 qty 已經變動了
            # 重新計算：
            # 假設我們知道之前的 qty 和 avg_price
            # 因為我們剛剛才更新了 s["qty"]，所以我們需要先知道之前的量
            # 為了避免複雜邏輯，我們在執行時傳入之前的量，或者這裡我們採用簡單的「最新成交價」覆蓋
            # 或是在 ExecutionEngine 裡處理。
            pass
            
        s["entry_count"] += 1
        s["last_trade_price"] = fill_price
        s["last_trade_qty"] = fill_qty
        s["last_trade_time"] = time.time()
        
        logger.info(f"✅ [Post-Fill Update] {sym} 成交: {side} {fill_qty} @ {fill_price}")

    # 標記訂單已處理
    s["is_ordering"] = False

def reset_coin_state(sym):
    from core import ctx
    from core.peak_store import clear_peak
    from core.entry_time_store import clear_entry_time
    s = ctx.STATES[sym]
    s["qty"] = 0.0
    s["avg_price"] = 0.0
    s["entries"] = []
    s["open_time"] = 0.0
    s["trailing_highest"] = 0.0
    s["trailing_lowest"] = float('inf')
    s["highest_profit_pct"] = 0.0
    clear_peak(sym)
    clear_entry_time(sym)
    s["highest_close_pct"] = 0.0
    s["peak_time"] = 0.0
    s["has_partial_closed"] = False
    s["is_breakeven_locked"] = False
    s["stop_loss"] = 0.0
    s["pending_side"] = None
    s["pending_time"] = 0
    s["pending_signal_price"] = 0.0
    s["pending_confirm_high"] = 0
    s["pending_confirm_low"] = 0
    s["has_been_negative"] = False
    s["trail_tp_price"] = 0.0
    s["entry_count"] = 0
    s["avg_entry_price"] = 0.0
    s["first_entry_price"] = 0.0
    s["max_additional_entries"] = 1  # 只允許攤平一次，避免虧損倉位越攤越大
    s["entry_cooldown_sec"] = 180
    s["entry_size_pct"] = 0.5
    s["add_entry_pct"] = 0.25
    s["risk_multiplier"] = 1.0
    s["volume_multiplier"] = 1.0
    s["sl_atr_multiplier"] = 1.5
    s["tp_atr_multiplier"] = 2.5
    s["hard_stop_loss_pct"] = COIN_PROFILE_CONFIG.get(sym, {}).get("hard_sl_pct", HARD_STOP_LOSS_PCT)
    s["exchange_stop_order_id"] = None
    s["exchange_take_profit_order_id"] = None
    s["personality"] = "balanced"
    s["personality_source"] = "infer"
    s["last_personality_update"] = 0.0
    s["last_entry_time"] = 0.0
    s["last_flip_time"] = 0.0
    s.pop("highest_sl", None)
    s.pop("lowest_sl", None)
    s["trailing_stop_price"] = 0.0
    s.pop("rescue_highest", None)
    s.pop("rescue_lowest", None)
    s["rescue_tracking_active"] = False
    s.pop("debug_start_time", None)
    s.pop("last_debug_pressure_time", None)
    s.pop("last_price_check", None)
    s.pop("last_price_check_time", None)
    s.pop("_hard_tp_reached_logged", None)


def get_active_count():
    from core import ctx
    return sum(1 for s in ctx.STATES.values() if s["status"] == "ACTIVE")


def get_open_position_count():
    """算目前佔用倉位額度的幣種數（給 MAX_POSITIONS 開倉上限判斷用）。
    原本只算 qty!=0 的幣種，但 check_entries() 的進場單是用 asyncio.create_task
    背景派發，不會等訂單真的成交才回傳——execute_order() 從查委託簿、算保證金
    到真的送出訂單，實測要 1~3 秒以上，比主迴圈一輪的間隔還久。這段「已經派發
    但 qty 還沒更新」的空窗期完全不算在這裡，導致下一輪主迴圈重新計算開倉數時
    看不到這些已經在路上的訂單，若剛好又有其他幣種同時觸發訊號，會一路疊加派發
    超過 MAX_POSITIONS 的上限（實際發生過同時開到 7 筆，遠超過設定的 3 筆）。
    改成連 is_ordering（訂單正在派發中，還沒確認成交）也一起算進佔用額度。"""
    from core import ctx
    return sum(
        1 for s in ctx.STATES.values()
        if abs(s["qty"]) > 0.000001 or s.get("is_ordering")
    )


def get_open_symbols():
    from core import ctx
    return [sym for sym in ctx.ALL_SYMBOLS if sym in ctx.STATES and abs(ctx.STATES[sym]["qty"]) > 0.000001]


def is_symbol_locked(sym):
    from core import ctx
    s = ctx.STATES.get(sym)
    if not s:
        return False
    return abs(s["qty"]) > 0.000001 or s["entry_count"] > 0 or s["open_time"] > 0 or s["status"] in ("COOLDOWN", "BANNED") or s.get("pending_side") is not None
