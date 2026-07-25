import logging
import asyncio
import math
import time
import numpy as np

from core import ctx
from core.config import (PAPER_TRADING, HARD_STOP_LOSS_PCT, MIN_PROFIT_LOCK_THRESHOLD,
    PROTECTED_PROFIT_FLOOR, MOMENTUM_EXIT_ATR_THRESHOLD, MOMENTUM_EXIT_MIN_PROFIT_PCT,
    COIN_PROFILE_CONFIG, SCALP_MODE, SCALP_TP1_PCT, SCALP_TP2_PCT,
    SL_ATR_MULTIPLIER, TP_ATR_MULTIPLIER,
    HIGH_POINT_STAGNATION_MIN_PROFIT, HIGH_POINT_STAGNATION_TIME, ROUND_TRIP_FEE_PCT)
from core.indicators import _get_atr
from core.symbol_profile import get_effective_exit_setting, get_dynamic_atr_multiplier
from core.calc import profit_pct as _profit_pct

logger = logging.getLogger(__name__)

MA_ENTRY_ROUTES = {"ma_cross", "ma_breakout", "ma25_pullback", "ma7_simple", "ma_restored"}
RANGE_ENTRY_ROUTES = {"range_support_long", "range_resistance_short"}
MA_DISASTER_STOP_PCT = 0.025
MA_WRONG_DIRECTION_PCT = 0.01
MA_WRONG_DIRECTION_WINDOW_SEC = 1800
# 微利價格鎖利層（低於 0.5% 峰值）已停用：實測 FILUSDT（ADX 55、扎實趨勢單）
# 峰值只到 +0.22% 就被這層鎖死，交易所端同步的 STOP_MARKET 隨後被急速反轉的
# 滑價打穿，小賺變小虧；而且無論鎖多緊，都是「見好就收一點點」，跟後面行情
# 有沒有真的繼續完全無關——本質上是拿一個固定價格門檻去猜「這是真反轉還是
# 正常雜訊」，猜不準。
#
# 小峰值（< MA_PEAK_LOCK_ARM_PCT）不再用「固定比例鎖死」猜反轉，改用跟 Range
# 路線一樣的 ATR 動態距離移動停利：距離依波動度自動調整，波動大的幣種留的
# 空間寬一點、不會被正常雜訊滑穿；波動小的幣種收緊一點。實測 6 筆 MA7_Simple
# 交易（BCH/XMR/WLD 等）峰值都在 0.17%~0.42%（卡在 0.5% 門檻之下完全沒有
# 保護），等 MA7 結構反轉確認出來，獲利已經全部回吐甚至變虧損；改成動態距離
# 移動停利後，這個區間也有基本保護，但不是固定價位死鎖。
MA_MICRO_TRAIL_ARM_PCT = 0.0025  # 峰值至少 0.25%（高於雙邊費用門檻）才啟動，確保峰值與地板之間留有真正的移動空間
MA_EARLY_MOMENTUM_FLIP_STOP_PCT = 0.005
MA_EARLY_MOMENTUM_FLIP_WINDOW_SEC = 1800
MA_MIN_PROFIT_TARGET_PCT = 0.010
# 0.25% 前不以價格地板猜測反轉；由 MA 結構與災難停損管理。達 0.25% 後，
# 單一 Peak Lock 棘輪隨已確認峰值向獲利方向推進，避免重疊規則搶先平倉。
MA_PEAK_LOCK_ARM_PCT = 0.0025
MA_PROFIT_FLOOR_NET_BUFFER_PCT = 0.001
MA_PROFIT_FLOOR_CONFIRM_TICKS = 3
MA_PROFIT_FLOOR_CONFIRM_SEC = 1.0
MA_PROFIT_FLOOR_TREND_CONFIRM_SEC = 2.0
MA_PEAK_LOCK_MID_PCT = 0.015
MA_PEAK_LOCK_HIGH_PCT = 0.030
MA_PEAK_LOCK_MIN_ATR_GAP = 1.0  # 移動停利線距峰值 1.0 ATR（原 1.5，適度收緊跟蹤距離）
GENERIC_TRAILING_ARM_PCT = 0.0045
PARTIAL_TP_MIN_GROSS_PCT = 0.006
# Range 在峰值達 0.25% 後，把保護線推到「雙邊費用 + 0.05%」；搭配下方
# 3 ticks / 1 秒確認，保留 0.2x% 小波段，同時避免單筆毛刺立即出場。
RANGE_TRAILING_MIN_GROSS_PCT = 0.0025
RANGE_TRAILING_NET_BUFFER_PCT = 0.0005

# 即時賣壓/買壓出場：鎖利線是等「價格」跌破才反應，本質上一定會落後於真正的
# 反轉。即時成交流（taker 主動買/賣）比價格更早反映風向轉變，因此在已有基本
# 浮盈的前提下，若逆勢方向成交量明顯主導，提前出場，減少等鎖利線被價格穿越
# 才出場所造成的回吐。刻意不跟 MA_PEAK_LOCK_ARM_PCT 綁在一起、門檻設得
# 更低：這是獨立的「真訊號」防線（看實際成交方向，不是看價格門檻），就算
# 峰值還沒到鎖利線會啟動的門檻，只要出現真的逆勢量能主導也該提前反應；會不
# 會誤觸交給下面的連續確認 (confirm ticks) 把單筆雜訊濾掉，不是靠拉高門檻。
SELL_PRESSURE_MIN_PROFIT_PCT = 0.0025
SELL_PRESSURE_WINDOW = 12
SELL_PRESSURE_MIN_SAMPLES = 6
SELL_PRESSURE_ADVERSE_RATIO = 0.70
SELL_PRESSURE_CONFIRM_TICKS = 3
SELL_PRESSURE_CONFIRM_SEC = 1.0

# 區間移動停利穿越確認：原本要等整根 K 棒收線才確認出場（5 分鐘線就是等最多
# 5 分鐘），比 MA 路線的鎖利確認（1~2 秒）慢了兩個數量級。實測 FILUSDT 案例：
# 進場後沒多久就被穿越保護線，等一整根 K 棒收線確認完畢，本來就很薄的獲利被
# 磨到只剩 0.02%。改成跟 MA_Profit_Floor 同一套「連續多筆/秒數確認」，不再
# 綁定 K 棒週期。
RANGE_TRAILING_CONFIRM_TICKS = MA_PROFIT_FLOOR_CONFIRM_TICKS
RANGE_TRAILING_CONFIRM_SEC = MA_PROFIT_FLOOR_CONFIRM_SEC
_MA_EXCHANGE_STOP_SYNC_TASKS = {}

# MA7 獲利轉彎出場：第一根確認轉彎的收線先落袋 60%，下一根仍往反方向才清倉。
# 最低毛利需涵蓋雙邊手續費及一小段滑價，避免把接近成本的 MA7 抖動當成停利。
MA7_PROFIT_TURN_PARTIAL_RATIO = 0.60
MA7_PROFIT_TURN_MIN_PCT = 0.010  # 至少峰值達 1.0% 才啟動峰值回吐保護，避免 0.3%~0.4% 小峰值誤觸發慌忙平倉
# 峰值回吐安全網：MA7_Profit_Turn_Partial／Confirmed 只看「MA7 收線後有沒有
# 轉彎」，完全不參考峰值，導致峰值再高、每次出場都貼著成本價（實測 BCHUSDT
# 案例：同一倉位連續三次分批出場，峰值一路墊高到 0.44%，三次都在成本價附近
# 結算）。這裡不等 MA7 轉彎收線確認，只要浮盈已經從峰值回吐超過一半，直接
# 出清剩餘部位，把「已經確認到手的獲利」跟「等下一根 K 棒確認轉彎」的延遲
# 脫鉤。刻意用比例而非固定價格門檻，不會重新把小峰值的獲利空間鎖死。
MA7_PROFIT_TURN_GIVEBACK_KEEP_RATIO = 0.5

# RSI 頂背離出場：持倉有基本浮盈時，若價格創近期新高但 RSI 比前一波高點
# 低 RSI_DIVERGENCE_MIN_DROP 以上（代表上漲動能衰竭），連續 2 根 K 棒確認後出場。
# 只在 _peak_profit >= RSI_DIVERGENCE_MIN_PROFIT_PCT 時啟用，避免剛進場就誤觸。
RSI_DIVERGENCE_MIN_PROFIT_PCT = 0.004   # 至少獲利 0.4% 才啟用背離保護
RSI_DIVERGENCE_MIN_DROP = 4.0           # 前高 RSI - 現高 RSI >= 4 視為有效背離
RSI_DIVERGENCE_LOOKBACK = 20            # 向前找「前波高點」的 K 棒數
RSI_DIVERGENCE_CONFIRM = 2              # 連續幾根 K 棒確認才出場


def _ma7_closed_turn(candles, is_long):
    """Return a completed-candle MA7 turn signal and its confirmation data.

    The live candle is deliberately excluded. Long exits require MA7 to change
    from rising to falling, with a bearish close below MA7; shorts are mirrored.
    """
    if len(candles) < 10:
        return False, {}
    completed = candles[:-1]
    if len(completed) < 9:
        return False, {}

    closes = [float(c[4]) for c in completed]
    current_ma7 = float(np.mean(closes[-7:]))
    previous_ma7 = float(np.mean(closes[-8:-1]))
    previous_previous_ma7 = float(np.mean(closes[-9:-2]))
    previous_slope = previous_ma7 - previous_previous_ma7
    current_slope = current_ma7 - previous_ma7
    latest = completed[-1]
    candle_open = float(latest[1])
    candle_high = float(latest[2])
    candle_low = float(latest[3])
    candle_close = float(latest[4])

    if is_long:
        triggered = (
            previous_slope > 0 and current_slope < 0
            and candle_close < candle_open and candle_close < current_ma7
        )
    else:
        triggered = (
            previous_slope < 0 and current_slope > 0
            and candle_close > candle_open and candle_close > current_ma7
        )
    return triggered, {
        "candle_ts": int(latest[0]),
        "ma7": current_ma7,
        "previous_slope": previous_slope,
        "current_slope": current_slope,
        "close": candle_close,
        "low": candle_low,
        "high": candle_high,
    }


def _meaningful_ma7_break(is_long, closed_price, ma7, ma25, prev_ma7, atr, avg):
    """忽略 MA7/MA25 附近的正常回踩；兩條均線都明顯失守才確認生命週期破壞。"""
    closed_price = float(closed_price or 0.0)
    ma7 = float(ma7 or 0.0)
    ma25 = float(ma25 or 0.0)
    prev_ma7 = float(prev_ma7 or 0.0)
    atr = float(atr or 0.0)
    avg = float(avg or 0.0)
    if min(closed_price, ma7, ma25, avg) <= 0:
        return False, 0.0
    break_buffer = max(atr * 0.15, avg * 0.0005)
    structure_buffer = max(atr * 0.25, avg * 0.0008)
    if is_long:
        beyond_buffer = closed_price < ma7 - break_buffer
        ma25_lost = closed_price < ma25 - structure_buffer
    else:
        beyond_buffer = closed_price > ma7 + break_buffer
        ma25_lost = closed_price > ma25 + structure_buffer
    return beyond_buffer and ma25_lost, break_buffer


def _ma7_simple_turn_break(
    is_long, closed_price, ma7, atr, avg,
    turn_triggered, turn_data, invalid_count,
):
    """MA7_Simple exits follow an actual opposite MA7 turn, not price alone."""
    closed_price = float(closed_price or 0.0)
    ma7 = float(ma7 or 0.0)
    atr = float(atr or 0.0)
    avg = float(avg or 0.0)
    if min(closed_price, ma7, avg) <= 0:
        return False, 0.0

    break_buffer = max(atr * 0.15, avg * 0.0005)
    current_slope = float((turn_data or {}).get("current_slope", 0.0) or 0.0)
    turn_started = bool(turn_triggered) or int(invalid_count or 0) > 0
    if is_long:
        adverse_slope = current_slope < 0
        beyond_buffer = closed_price < ma7 - break_buffer
    else:
        adverse_slope = current_slope > 0
        beyond_buffer = closed_price > ma7 + break_buffer
    return turn_started and adverse_slope and beyond_buffer, break_buffer


def _ma_peak_keep_ratio(peak_profit):
    # 使用者要求適度收緊，儘量鎖在接近當下高點的位置，減少獲利回吐幅度。
    if peak_profit >= MA_PEAK_LOCK_HIGH_PCT:
        return 0.90
    if peak_profit >= MA_PEAK_LOCK_MID_PCT:
        return 0.85
    # 0.3%~0.6% 的盈利地板已保留 80%；跨過 0.6% 後不可反而降成 75%。
    return 0.80


def _schedule_ma_exchange_profit_stop(sym):
    """非阻塞同步 MA 鎖利；同步期間的新價位不可遺失。"""
    if PAPER_TRADING:
        return
    state = ctx.STATES.get(sym, {})
    if not state.get("exchange_stop_order_id"):
        # 重啟校準時，即時價格處理可能比 _ensure_exchange_exit_orders 更早算出
        # 獲利地板；保護單 ID 尚未建立時保留 pending，建立後立即補同步。
        state["_ma_exchange_stop_sync_pending"] = True
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    previous = _MA_EXCHANGE_STOP_SYNC_TASKS.get(sym)
    if previous is not None and not previous.done():
        state["_ma_exchange_stop_sync_pending"] = True
        return

    async def _sync():
        from core.orders import _sync_ma_exchange_profit_stop
        while True:
            state["_ma_exchange_stop_sync_pending"] = False
            try:
                await _sync_ma_exchange_profit_stop(sym)
            except Exception as exc:
                logger.info(f"⚠️ [MA交易所鎖利同步失敗] {sym}: {exc}")
                return
            if not state.get("_ma_exchange_stop_sync_pending", False):
                return

    _MA_EXCHANGE_STOP_SYNC_TASKS[sym] = loop.create_task(_sync())


def _reset_ma_profit_floor_confirmation(state):
    state["ma_profit_floor_cross_count"] = 0
    state["ma_profit_floor_cross_since"] = 0.0


def _ma_profit_floor_cross_confirmed(sym, crossed, is_long, now):
    """Require a persistent multi-tick floor breach; reclaim cancels the pending exit."""
    state = ctx.STATES[sym]
    previous_count = int(state.get("ma_profit_floor_cross_count", 0) or 0)
    if not crossed:
        if previous_count:
            logger.info(f"✅ [MA_Profit_Floor_Reclaim] {sym} 已重新站回鎖利線，取消出場確認")
        _reset_ma_profit_floor_confirmation(state)
        return False

    since = float(state.get("ma_profit_floor_cross_since", 0.0) or 0.0)
    if previous_count <= 0 or since <= 0 or now < since:
        since = now
        count = 1
        state["ma_profit_floor_cross_since"] = since
        logger.info(f"⏳ [MA_Profit_Floor_Confirm] {sym} 首次穿越鎖利線，等待連續成交確認")
    else:
        count = previous_count + 1
    state["ma_profit_floor_cross_count"] = count

    ma7 = float(state.get("ma7", 0.0) or 0.0)
    prev_ma7 = float(state.get("prev_ma7", ma7) or ma7)
    trend_still_favorable = (
        ma7 > 0 and prev_ma7 > 0
        and ((ma7 > prev_ma7) if is_long else (ma7 < prev_ma7))
    )
    required_sec = (MA_PROFIT_FLOOR_TREND_CONFIRM_SEC if trend_still_favorable
                    else MA_PROFIT_FLOOR_CONFIRM_SEC)
    confirmed = (count >= MA_PROFIT_FLOOR_CONFIRM_TICKS
                 and now - since >= required_sec)
    if confirmed:
        logger.info(
            f"🛑 [MA_Profit_Floor_Confirmed] {sym} 連續 {count} 筆且維持 "
            f"{now - since:.1f}s 穿越鎖利線，確認出場"
        )
    return confirmed


SELL_PRESSURE_DEBUG_LOG_INTERVAL_SEC = 3.0


def _reset_sell_pressure_confirmation(sym, state, now=None):
    previous_count = int(state.get("sell_pressure_cross_count", 0) or 0)
    if previous_count > 0:
        since = float(state.get("sell_pressure_cross_since", 0.0) or 0.0)
        held_sec = max(0.0, float(now if now is not None else time.time()) - since) if since > 0 else 0.0
        logger.info(
            f"↩️ [SellPressure_Reset] {sym} 累積 {previous_count} 筆確認後中斷"
            f"（維持 {held_sec:.1f}s 未達 {SELL_PRESSURE_CONFIRM_TICKS} 筆/"
            f"{SELL_PRESSURE_CONFIRM_SEC:.0f}s 門檻），成交流轉回平衡或浮盈已消失"
        )
    state["sell_pressure_cross_count"] = 0
    state["sell_pressure_cross_since"] = 0.0


def _sell_pressure_confirmed(sym, qualifies, now):
    """比照 MA_Profit_Floor 的多筆連續確認模式，避免單一筆大單造成誤判。"""
    state = ctx.STATES[sym]
    if not qualifies:
        _reset_sell_pressure_confirmation(sym, state, now)
        return False

    previous_count = int(state.get("sell_pressure_cross_count", 0) or 0)
    since = float(state.get("sell_pressure_cross_since", 0.0) or 0.0)
    if previous_count <= 0 or since <= 0 or now < since:
        since = now
        count = 1
        state["sell_pressure_cross_since"] = since
    else:
        count = previous_count + 1
    state["sell_pressure_cross_count"] = count

    confirmed = (count >= SELL_PRESSURE_CONFIRM_TICKS
                 and now - since >= SELL_PRESSURE_CONFIRM_SEC)
    return confirmed


def check_realtime_sell_pressure(sym, is_long, current_price, event_time=None):
    return False

def _unused_check_realtime_sell_pressure(sym, is_long, current_price, event_time=None):
    state = ctx.STATES.get(sym)
    if not state:
        return False
    avg = float(state.get("avg_price", 0.0) or 0.0)
    if avg <= 0 or current_price <= 0:
        return False

    profit = (current_price - avg) / avg if is_long else (avg - current_price) / avg
    peak = float(state.get("highest_profit_pct", 0.0) or 0.0)
    now = float(event_time if event_time is not None else time.time())

    # 還沒有基本浮盈、或現在已經不賺錢了：不是這個機制要處理的情境，
    # 保留給鎖利線／MA 生命週期／災難止損各自的規則判斷。
    if peak < SELL_PRESSURE_MIN_PROFIT_PCT or profit <= 0:
        _reset_sell_pressure_confirmation(sym, state, now)
        return False

    window = list(state.get("trade_side_history", []) or [])[-SELL_PRESSURE_WINDOW:]
    if len(window) < SELL_PRESSURE_MIN_SAMPLES:
        return False

    total_volume = sum(abs(x) for x in window)
    if total_volume <= 0:
        return False
    adverse_volume = sum(abs(x) for x in window if (x < 0 if is_long else x > 0))
    ratio = adverse_volume / total_volume
    qualifies = ratio >= SELL_PRESSURE_ADVERSE_RATIO

    # 診斷用：不管有沒有過門檻都定期記錄實際算出來的比值，方便事後回頭比對
    # 「這次差多少沒觸發」。用時間節流避免快速行情下洗版。
    last_log_at = float(state.get("_sell_pressure_debug_log_at", 0.0) or 0.0)
    if now - last_log_at >= SELL_PRESSURE_DEBUG_LOG_INTERVAL_SEC:
        state["_sell_pressure_debug_log_at"] = now
        count = int(state.get("sell_pressure_cross_count", 0) or 0)
        logger.info(
            f"🔬 [SellPressure_Ratio] {sym} 逆勢量占比 {ratio*100:.1f}% "
            f"(門檻 {SELL_PRESSURE_ADVERSE_RATIO*100:.0f}%, 樣本 {len(window)}/{SELL_PRESSURE_WINDOW}, "
            f"浮盈 {profit*100:.2f}%, 峰值 {peak*100:.2f}%, 已累積確認 {count} 筆)"
        )

    return _sell_pressure_confirmed(sym, qualifies, now)


def _reset_range_trailing_confirmation(state):
    state["range_trailing_cross_count"] = 0
    state["range_trailing_cross_since"] = 0.0


def _range_trailing_cross_confirmed(sym, crossed, now):
    """區間移動停利穿越確認：比照 MA_Profit_Floor 的連續多筆/秒數確認，取代
    原本要等整根 K 棒收線才確認出場的做法（見上方常數定義的說明）。"""
    state = ctx.STATES[sym]
    if not crossed:
        _reset_range_trailing_confirmation(state)
        return False

    previous_count = int(state.get("range_trailing_cross_count", 0) or 0)
    since = float(state.get("range_trailing_cross_since", 0.0) or 0.0)
    if previous_count <= 0 or since <= 0 or now < since:
        since = now
        count = 1
        state["range_trailing_cross_since"] = since
    else:
        count = previous_count + 1
    state["range_trailing_cross_count"] = count

    confirmed = (count >= RANGE_TRAILING_CONFIRM_TICKS
                 and now - since >= RANGE_TRAILING_CONFIRM_SEC)
    return confirmed


def update_ma_peak_lock(sym, current_price, is_long, event_time=None, require_confirmation=False):
    """Track an MA-wave peak and return whether its ratcheting profit lock was crossed."""
    s = ctx.STATES[sym]
    avg = float(s.get("avg_price", 0.0) or 0.0)
    atr = float(s.get("current_atr", 0.0) or 0.0)
    if avg <= 0 or current_price <= 0 or atr <= 0:
        return False, 0.0

    profit = (current_price - avg) / avg if is_long else (avg - current_price) / avg
    confirmed_peak = float(s.get("highest_profit_pct", 0.0) or 0.0)
    now = float(event_time if event_time is not None else time.time())
    if require_confirmation and profit > confirmed_peak:
        candidate = float(s.get("realtime_peak_candidate_profit", 0.0) or 0.0)
        candidate_time = float(s.get("realtime_peak_candidate_time", 0.0) or 0.0)
        tolerance = max(0.0005, min(0.002, (atr / avg) * 0.25))
        if candidate > confirmed_peak and 0 <= now - candidate_time <= 1.0 and abs(profit - candidate) <= tolerance:
            confirmed_peak = max(candidate, profit)
            s["realtime_peak_candidate_profit"] = 0.0
            s["realtime_peak_candidate_price"] = 0.0
            s["realtime_peak_candidate_time"] = 0.0
        else:
            s["realtime_peak_candidate_profit"] = profit
            s["realtime_peak_candidate_price"] = current_price
            s["realtime_peak_candidate_time"] = now
    else:
        confirmed_peak = max(confirmed_peak, profit)

    if confirmed_peak > float(s.get("highest_profit_pct", 0.0) or 0.0):
        s["highest_profit_pct"] = confirmed_peak
    if confirmed_peak > float(s.get("ma_peak_saved_pct", 0.0) or 0.0):
        from core.peak_store import save_peak
        save_peak(sym, confirmed_peak)
        s["ma_peak_saved_pct"] = confirmed_peak

    # 保利地板要涵蓋雙邊 taker fee、低價幣的最小跳動與市價平倉滑點。
    # 只多留 0.05% 時，ENA 這類 tick 約 0.12% 的合約會把理論鎖利價夾在
    # 兩個可成交價之間；回吐的下一格就是進場價，結果毛利 0、淨損雙邊費用。
    fee_floor = ROUND_TRIP_FEE_PCT + MA_PROFIT_FLOOR_NET_BUFFER_PCT
    if confirmed_peak < MA_PEAK_LOCK_ARM_PCT:
        if confirmed_peak < MA_MICRO_TRAIL_ARM_PCT:
            _reset_ma_profit_floor_confirmation(s)
            s["ma_profit_floor_armed"] = False
            s["ma_profit_floor_price"] = 0.0
            return False, 0.0

        # ATR 動態距離移動停利：距離依波動度自動調整（0.15%~0.35%），
        # 而不是鎖死一個固定比例的獲利價位。
        atr_pct = (atr / avg) if avg > 0 else 0.0
        trail_tolerance = max(0.0015, min(0.0035, atr_pct * 1.2))
        if is_long:
            peak_price = avg * (1.0 + confirmed_peak)
            floor_price = max(peak_price * (1.0 - trail_tolerance), avg * (1.0 + fee_floor))
            floor_price = min(floor_price, peak_price)  # 地板永遠不可高於目前峰值價
            floor_price = max(float(s.get("ma_profit_floor_price", 0.0) or 0.0), floor_price)
            crossed = current_price <= floor_price
        else:
            peak_price = avg * (1.0 - confirmed_peak)
            floor_price = min(peak_price * (1.0 + trail_tolerance), avg * (1.0 - fee_floor))
            floor_price = max(floor_price, peak_price)  # 地板永遠不可低於目前峰值價
            previous_floor = float(s.get("ma_profit_floor_price", 0.0) or 0.0)
            floor_price = min(previous_floor if previous_floor > 0 else float("inf"), floor_price)
            crossed = current_price >= floor_price
        previous_floor = float(s.get("ma_profit_floor_price", 0.0) or 0.0)
        s["ma_profit_floor_armed"] = True
        s["ma_profit_floor_price"] = floor_price
        if abs(floor_price - previous_floor) > avg * 0.000001:
            _schedule_ma_exchange_profit_stop(sym)
        return _ma_profit_floor_cross_confirmed(sym, crossed, is_long, now), floor_price

    locked_profit = max(fee_floor, confirmed_peak * _ma_peak_keep_ratio(confirmed_peak))
    if is_long:
        peak_price = avg * (1.0 + confirmed_peak)
        proposed = min(avg * (1.0 + locked_profit), peak_price - atr * MA_PEAK_LOCK_MIN_ATR_GAP)
        # 避免 ATR 波動度過大時，鎖利目標被過度拉回，確保最少鎖住 60% 峰值利潤
        proposed = max(proposed, avg * (1.0 + confirmed_peak * 0.60))
        proposed = max(proposed, avg * (1.0 + fee_floor))
        lock_price = max(float(s.get("ma_peak_lock_price", 0.0) or 0.0), proposed)
        crossed = current_price <= lock_price
    else:
        peak_price = avg * (1.0 - confirmed_peak)
        proposed = max(avg * (1.0 - locked_profit), peak_price + atr * MA_PEAK_LOCK_MIN_ATR_GAP)
        # 避免 ATR 波動度過大時，鎖利目標被過度拉回，確保最少鎖住 60% 峰值利潤
        proposed = min(proposed, avg * (1.0 - confirmed_peak * 0.60))
        proposed = min(proposed, avg * (1.0 - fee_floor))
        previous = float(s.get("ma_peak_lock_price", 0.0) or 0.0)
        lock_price = min(previous if previous > 0 else float("inf"), proposed)
        crossed = current_price >= lock_price
    previous_lock = float(s.get("ma_peak_lock_price", 0.0) or 0.0)
    s["ma_peak_lock_armed"] = True
    s["ma_peak_lock_price"] = lock_price
    if abs(lock_price - previous_lock) > avg * 0.000001:
        _schedule_ma_exchange_profit_stop(sym)
    return crossed, lock_price


class FastReversalGuard:
    def __init__(self, max_immediate_deviation=0.005): # 0.5% 立即背離就砍
        self.max_immediate_deviation = max_immediate_deviation

    def check_instant_trap(self, entry_price, current_price, side):
        """
        偵測開倉後的「瞬間陷阱」
        """
        if side == 'buy':
            deviation = (entry_price - current_price) / entry_price
        else:
            deviation = (current_price - entry_price) / entry_price
            
        # 如果在極短時間內背離超過門檻，立即觸發平倉訊號
        if deviation > self.max_immediate_deviation:
            return "EXIT_INSTANT_TRAP"
        
        return "KEEP_HOLDING"

class DynamicExitManager:
    """
    動態退出管理器：結合 ATR 趨勢追蹤與非線性耐心衰減模型。
    解決點：防止在利潤區間因市場微小震盪而導致計時器重置，導致無法入帳。
    """
    def __init__(self, entry_price, restored_peak_pct=0.0, is_long=True):
        self.entry_price = entry_price
        self.is_long = is_long

        # 配置參數
        self.profit_threshold = 0.35        # 利潤 > 0.35% 才啟動高位盤整防護，留足趨勢空間
        self.stagnation_range = 0.0015       # 盤整區間 (0.15%)
        self.no_high_time_limit = 180        # 盤整判定時間 (180秒內沒創新高)
        self.min_exit_profit_pct = 0.25    # 百分比單位；至少覆蓋雙邊費用與摩擦後才允許動態停利

        # 還原重啟前已經記錄的峰值百分比
        self.max_profit_pct = max(0.0, restored_peak_pct)
        if self.is_long:
            self.current_max_price = entry_price * (1 + self.max_profit_pct / 100.0)
        else:
            self.current_max_price = entry_price * (1 - self.max_profit_pct / 100.0)

        # 狀態變數
        self.is_active = restored_peak_pct >= self.profit_threshold
        if self.is_active:
            self.wait_start_time = time.time()
            self.last_high_time = time.time()
            self.wait_time_limit = self._calculate_wait_time(restored_peak_pct)
        else:
            self.wait_start_time = None          # 進入狀態的時間點
            self.wait_time_limit = 300           # 動態計算出的耐心秒數
            self.last_high_time = None           # 上次創下最高點的時間

    def update(self, current_price):
        """
        每秒執行一次的檢查函式
        :param current_price: 當前市場價格
        :return: "HOLD" (繼續持有) 或 "SELL" (立即賣出)
        """
        # 計算當前利潤百分比
        if self.is_long:
            current_profit = ((current_price - self.entry_price) / self.entry_price) * 100
        else:
            current_profit = ((self.entry_price - current_price) / self.entry_price) * 100
        
        # --- 第一階段：啟動門檻檢查 ---
        if not self.is_active and current_profit >= self.profit_threshold:
            self.is_active = True
            self.wait_start_time = time.time()
            self.current_max_price = current_price
            self.max_profit_pct = current_profit
            self.last_high_time = time.time()
            self.wait_time_limit = self._calculate_wait_time(current_profit)
            print(f"🚀 [啟動] 利潤達 {current_profit:.2f}%，啟動動態退出機制。耐心限時: {self.wait_time_limit:.1f}秒")

        if not self.is_active:
            return "HOLD"

        # --- 第二階段：更新最高點與重置計時器 ---
        is_new_high = False
        if self.is_long and current_price > (self.current_max_price * (1 + self.stagnation_range)):
            is_new_high = True
        elif not self.is_long and current_price < (self.current_max_price * (1 - self.stagnation_range)):
            is_new_high = True

        if is_new_high:
            self.current_max_price = current_price
            self.max_profit_pct = current_profit
            self.last_high_time = time.time()
            self.wait_start_time = time.time()
            self.wait_time_limit = self._calculate_wait_time(current_profit)
            print(f"📈 [顯著創新高] 價格: {current_price}，重置計時器。新耐心限時: {self.wait_time_limit:.1f}秒")

        # --- 第三階段：多重退出判定 (OR 關係) ---
        # (之前這裡有一版想接 IndustrialRiskManager，用
        # ctx.STATES[ctx.STATES.keys()[0]] 去抓「當前 ATR」——dict_keys 不能用 [0]
        # 索引，一執行就是 TypeError。這個 class 目前只有 use_dynamic_exit_manager=True
        # 時才會被呼叫，預設關閉所以還沒炸過，但保留這種寫法遲早會在打開開關的那一刻
        # 讓機器人整個崩潰，所以先移除這段沒接上、本來就沒用到的 current_atr。)
        elapsed_time = time.time() - self.wait_start_time
        time_since_high = time.time() - self.last_high_time
        
        # 1. 傳統動態回撤 (保留作為極限防線)
        # 對於極小的利潤(例如 0.15%)，回撤容忍度 0.12% 會直接導致出場在 0.03% (扣手續費後變虧損)。
        # 因此，若回撤觸發時已經處於虧損狀態，則交由原始的停損機制處理，不要在這裡強制平倉。
        tolerance_pct = max(0.0012, min((self.max_profit_pct / 100.0) * 0.25, 0.0050))
        is_retracing = False
        if self.is_long and current_price < (self.current_max_price * (1 - tolerance_pct)):
            is_retracing = True
        elif not self.is_long and current_price > (self.current_max_price * (1 + tolerance_pct)):
            is_retracing = True

        if is_retracing:
            if current_profit >= self.min_exit_profit_pct:  # 不得把接近成本或負報酬誤當停利
                print(f"💰 [觸發：回撤比例(極限)] 價格從最高點回落超過 {tolerance_pct*100:.3f}%，快速落袋為安。")
                return "SELL"
            else:
                # 未保留至少 0.12% 毛利，取消動態停利，讓正常風控接手
                is_retracing = False

        # 2. 動態耐心極限 (Time-out)
        if elapsed_time >= self.wait_time_limit:
            if current_profit >= self.min_exit_profit_pct:
                print(f"💰 [觸發：耐心極限] 已等待 {elapsed_time:.1f}秒 (限時 {self.wait_time_limit:.1f}秒)，強制落袋為安。")
                return "SELL"

        # 3. 盤整最高點 (Stagnation)
        is_stagnant = abs(current_price - self.current_max_price) <= (self.current_max_price * self.stagnation_range)
        if time_since_high > self.no_high_time_limit and is_stagnant:
            if current_profit >= self.min_exit_profit_pct:
                print(f"🛑 [觸發：盤整最高點] 價格在 {self.current_max_price} 附近停滯過久，動能耗盡，執行停利。")
                return "SELL"

        return "HOLD"

    def _calculate_wait_time(self, profit):
        """
        非線性衰減公式: WaitTime = max(60, 300 - (sqrt(P - 0.15) * 76))
        這確保了利潤越高，耐心越短，但不會在小利潤時就過快賣出。
        """
        base_time = 300
        threshold = 0.15
        min_time = 60
        decay_factor = 76
        
        if profit <= threshold:
            return base_time
        
        # 核心非線性計算
        wait_time = base_time - (math.sqrt(profit - threshold) * decay_factor)
        return max(min_time, wait_time)
from core.calc import profit_pct as _profit_pct

logger = logging.getLogger(__name__)


def update_trailing_stop(sym, current_price, is_long, update_peak=True):
    """
    [2026-07-25] 使用者指示（方案三：動態追蹤止利／趨勢獵人模式）：完全取代舊版
    移動停損/MA_Peak_Lock/Range trailing 邏輯。規則：
      開倉：sl_price = entry ∓ EXIT_SL_ATR_MULTIPLIER x ATR(10)，
            tp_price = entry ± EXIT_TP_ATR_MULTIPLIER x ATR(10)
      獲利達 EXIT_BREAKEVEN_ATR_MULTIPLIER x ATR：sl_price 移到保本價（entry）
      保本後每創新高/新低：sl_price 追蹤到「峰值獲利的 EXIT_TRAIL_LOCK_RATIO」，
        tp_price 同步延展（新高/新低 ± EXIT_TP_EXTEND_ATR_MULTIPLIER x ATR），無上限
      sl_price 只會越收越緊、tp_price 只會越推越遠，兩者都不會往回鬆。
    真正的 SL/TP 觸發判斷與防插針保護在 check_exits() 執行；這裡只負責維護
    sl_price/tp_price/highest_price/lowest_price/is_breakeven_moved 這幾個欄位。
    """
    from core.config import (
        EXIT_ATR_PERIOD, EXIT_SL_ATR_MULTIPLIER, EXIT_TP_ATR_MULTIPLIER,
        EXIT_BREAKEVEN_ATR_MULTIPLIER, EXIT_TRAIL_LOCK_RATIO, EXIT_TP_EXTEND_ATR_MULTIPLIER,
    )
    from core.indicators import get_atr_from_ohlcv

    s = ctx.STATES[sym]
    avg_price = float(s.get("avg_price", 0.0) or 0.0)
    if avg_price <= 0:
        return False, s.get("sl_price", 0.0)

    atr10 = get_atr_from_ohlcv(s.get("ohlcv", []), EXIT_ATR_PERIOD)
    if atr10 <= 0:
        return False, s.get("sl_price", 0.0)

    # 首次呼叫（剛開倉，sl_price 尚未初始化）：設定固定初始 SL/TP，峰值 = 進場價。
    if float(s.get("sl_price", 0.0) or 0.0) <= 0:
        if is_long:
            s["sl_price"] = avg_price - EXIT_SL_ATR_MULTIPLIER * atr10
            s["tp_price"] = avg_price + EXIT_TP_ATR_MULTIPLIER * atr10
        else:
            s["sl_price"] = avg_price + EXIT_SL_ATR_MULTIPLIER * atr10
            s["tp_price"] = avg_price - EXIT_TP_ATR_MULTIPLIER * atr10
        s["highest_price"] = avg_price
        s["lowest_price"] = avg_price
        s["is_breakeven_moved"] = False
        s["trailing_stop_price"] = s["sl_price"]
        logger.info(
            f"🎯 [Exit_Init] {sym} 初始 SL={s['sl_price']:.6f} TP={s['tp_price']:.6f} "
            f"(ATR10={atr10:.6f})"
        )
        return True, s["sl_price"]

    if not update_peak:
        return True, s.get("sl_price", 0.0)

    profit_dist = (current_price - avg_price) if is_long else (avg_price - current_price)

    if not s.get("is_breakeven_moved", False):
        if profit_dist >= EXIT_BREAKEVEN_ATR_MULTIPLIER * atr10:
            s["sl_price"] = avg_price
            s["is_breakeven_moved"] = True
            s["trailing_stop_price"] = s["sl_price"]
            logger.info(f"🛡️ [保本觸發] {sym} 獲利達 {EXIT_BREAKEVEN_ATR_MULTIPLIER}xATR，止損鎖定保本價 {avg_price:.6f}")
            _schedule_ma_exchange_profit_stop(sym)
            # 保本觸發跟峰值追蹤鎖定不能分兩次呼叫才生效：觸發保本的這個價位本身
            # 就是目前的峰值，若在這裡直接 return，會讓 sl_price 卡在「剛好 0% 保本」，
            # 要等「下一筆比這次還更高」的價格才會補算 75% 鎖利，萬一觸發保本後價格
            # 立刻回落（沒有再創新高），這筆單就永遠只鎖在保本，白白吐掉已經到手的
            # 那段獲利（實測 XMRUSDT 案例：觸發保本後 2 秒內反轉，最終在接近保本處
            # 出場，等於完全沒鎖到那段已經走到的漲幅）。這裡讓同一次呼叫直接往下走，
            # 用觸發保本當下的價格立即計算一次 75% 鎖利。
        else:
            return True, s.get("sl_price", 0.0)

    if is_long:
        if current_price > float(s.get("highest_price", avg_price) or avg_price):
            s["highest_price"] = current_price
            new_sl = avg_price + (s["highest_price"] - avg_price) * EXIT_TRAIL_LOCK_RATIO
            if new_sl > float(s.get("sl_price", 0.0) or 0.0):
                s["sl_price"] = new_sl
                s["tp_price"] = s["highest_price"] + EXIT_TP_EXTEND_ATR_MULTIPLIER * atr10
                s["trailing_stop_price"] = s["sl_price"]
                logger.info(f"📈 [動態追蹤] {sym} 創新高 {current_price:.6f}，SL→{s['sl_price']:.6f} TP→{s['tp_price']:.6f}")
                _schedule_ma_exchange_profit_stop(sym)
    else:
        if current_price < float(s.get("lowest_price", avg_price) or avg_price):
            s["lowest_price"] = current_price
            new_sl = avg_price - (avg_price - s["lowest_price"]) * EXIT_TRAIL_LOCK_RATIO
            if new_sl < float(s.get("sl_price", 0.0) or 0.0):
                s["sl_price"] = new_sl
                s["tp_price"] = s["lowest_price"] - EXIT_TP_EXTEND_ATR_MULTIPLIER * atr10
                s["trailing_stop_price"] = s["sl_price"]
                logger.info(f"📉 [動態追蹤] {sym} 創新低 {current_price:.6f}，SL→{s['sl_price']:.6f} TP→{s['tp_price']:.6f}")
                _schedule_ma_exchange_profit_stop(sym)

    return True, s.get("sl_price", 0.0)


async def check_exits(sym):
    """
    [2026-07-25] 使用者指示（方案三：動態追蹤止利／趨勢獵人模式）：完全取代舊版
    出場系統（硬停損、移動停利、停滯超時、MA_Peak_Lock、TP1/TP2分批、
    DynamicExitManager、Range trailing 等全部停用）。規則：
      開倉：SL/TP 由 update_trailing_stop() 初始化為 entry ∓/± 固定 ATR 倍數
      停利：現價觸及 tp_price 立即平倉，不受防插針影響
      停損：現價觸及 sl_price 平倉，但若當下已收盤K棒振幅 > EXIT_SPIKE_ATR_MULTIPLIER
            x ATR（判定為插針/爆倉針），暫停這次觸發，等下一輪再確認
      24 小時強制出場：不論盈虧，持倉滿 EXIT_MAX_HOLD_SEC 直接平倉
    """
    from core.orders import close_position
    from core.config import EXIT_ATR_PERIOD, EXIT_SPIKE_ATR_MULTIPLIER, EXIT_MAX_HOLD_SEC
    from core.indicators import get_atr_from_ohlcv, is_candle_spike

    s = ctx.STATES[sym]
    if s.get("_external_close_record_pending", False):
        return
    if s.get("adjusted_this_tick", False):
        return
    if abs(s["qty"]) < 0.000001 or s["avg_price"] <= 0:
        return

    if s.get("_auto_close_restored", False):
        s["_auto_close_restored"] = False
        cs = "sell" if s["qty"] > 0 else "buy"
        logger.info(f"🚨 [Restored_Auto_Close] {sym} 屬舊殘留持倉 (MA_Restored)，發起當下即時市價平倉！")
        await close_position(sym, cs, abs(s["qty"]), s["close_price"], s["avg_price"], reason="[Restored_Auto_Close]", is_stop_loss=True)
        return

    p = s["close_price"]
    # 保本鎖定/峰值追蹤/停損比對都用防插針確認價，避免單筆插針瞬間偽造一個
    # 「新高」把保本/移動停損永久鎖在雜訊價位（實測 XMRUSDT 案例：單筆插到
    # +0.33% 觸發保本，下一筆就打回真實價位，結果在接近保本處平倉，但那個
    # 高點從未真的走到過）；停利仍用即時 close_price，保持對真實獲利的敏感度。
    p_sf = float(s.get("close_price_spike_filtered", p) or p)
    avg = s["avg_price"]
    is_long = s["qty"] > 0

    profit_pct = (p - avg) / avg if is_long else (avg - p) / avg
    if profit_pct > s.get("highest_profit_pct", 0.0):
        s["highest_profit_pct"] = profit_pct
        from core.peak_store import save_peak
        save_peak(sym, s["highest_profit_pct"])

    # 維護 sl_price/tp_price（含開倉初始化、保本鎖定、峰值追蹤延展）
    update_trailing_stop(sym, p_sf, is_long)

    sl_price = float(s.get("sl_price", 0.0) or 0.0)
    tp_price = float(s.get("tp_price", 0.0) or 0.0)
    if sl_price <= 0 or tp_price <= 0:
        return

    # ── 停利 (Take-Profit)：不受防插針影響，達標立即觸發鎖定收益 ──
    tp_hit = (is_long and p >= tp_price) or (not is_long and p <= tp_price)
    if tp_hit:
        cs = "sell" if is_long else "buy"
        logger.info(f"💰 [Take_Profit] {sym} 現價 {p:.6f} 觸及止利價 {tp_price:.6f}，鎖定收益")
        await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[Take_Profit]", is_stop_loss=False)
        return

    # ── 防插針保護：單根已收盤K棒振幅 > N x ATR 時，暫停觸發止損（止利不受影響）──
    atr10 = get_atr_from_ohlcv(s.get("ohlcv", []), EXIT_ATR_PERIOD)
    if atr10 > 0 and is_candle_spike(s.get("ohlcv", []), atr10, EXIT_SPIKE_ATR_MULTIPLIER):
        logger.info(f"⚠️ [防插針保護] {sym} 單根K棒振幅 > {EXIT_SPIKE_ATR_MULTIPLIER}xATR，判定為異常插針，本輪暫停觸發止損")
    else:
        sl_hit = (is_long and p_sf <= sl_price) or (not is_long and p_sf >= sl_price)
        if sl_hit:
            cs = "sell" if is_long else "buy"
            reason_tag = "[Breakeven_Stop]" if s.get("is_breakeven_moved", False) else "[Stop_Loss]"
            logger.info(f"🛑 {reason_tag} {sym} 確認價 {p_sf:.6f} 觸及止損價 {sl_price:.6f}，執行平倉")
            await close_position(sym, cs, abs(s["qty"]), p_sf, avg, reason=reason_tag, is_stop_loss=True)
            return

    # ── 24 小時強制出場（不論盈虧）──
    open_time = float(s.get("open_time", 0.0) or 0.0)
    if open_time > 0 and (time.time() - open_time) >= EXIT_MAX_HOLD_SEC:
        cs = "sell" if is_long else "buy"
        logger.info(f"⏰ [Max_Hold_Timeout] {sym} 持倉已滿 {EXIT_MAX_HOLD_SEC/3600:.0f} 小時，強制平倉")
        await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[Max_Hold_Timeout]", is_stop_loss=(profit_pct <= 0))
        return

async def _attempt_forced_rescue(sym, s, is_long, p):
    from core.symbol_profile import is_rescue_dca_disabled
    if is_rescue_dca_disabled(sym):
        logger.info(f"🚫 [攤平限制] {sym} 被配置為禁止自動攤平救援")
        return False

    if s.get("entry_count", 0) >= 2:
        logger.info(f"🚫 [攤平限制] {sym} 已攤平過 (次數: {s.get('entry_count')})，不允許重複攤平")
        return False

    s["is_ordering"] = True
    try:
        cs = 'buy' if is_long else 'sell'
        
        from core.orders import execute_order
        logger.info(f"🚑 [緊急攤平救援] {sym} 觸發攤平機制，新下單 0.2x 倉位以拉低成本均價，並觀察 60 秒...")
        await execute_order(sym, cs, p, allocation_pct=0.20, is_rescue_dca=True)
        s["last_rescue_time"] = time.time()
        s["is_breakeven_locked"] = False
        s["highest_profit_pct"] = 0.0
    finally:
        s["is_ordering"] = False
    return True
