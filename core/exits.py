import logging
import asyncio
import math
import time
import numpy as np

from core import ctx
from core.config import (PAPER_TRADING, HARD_STOP_LOSS_PCT, MIN_PROFIT_LOCK_THRESHOLD,
    PROTECTED_PROFIT_FLOOR, MOMENTUM_EXIT_ATR_THRESHOLD, MOMENTUM_EXIT_MIN_PROFIT_PCT,
    COIN_PROFILE_CONFIG,
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
# 微利價格鎖利層（0.2%~0.6% 峰值）已停用：實測 FILUSDT（ADX 55、扎實趨勢單）
# 峰值只到 +0.22% 就被這層鎖死，交易所端同步的 STOP_MARKET 隨後被急速反轉的
# 滑價打穿，小賺變小虧；而且無論鎖多緊，都是「見好就收一點點」，跟後面行情
# 有沒有真的繼續完全無關——本質上是拿一個固定價格門檻去猜「這是真反轉還是
# 正常雜訊」，猜不準。
#
# 現在小峰值（< MA_PEAK_LOCK_ARM_PCT）不再武裝任何價格鎖利線，client 端跟
MA_EARLY_MOMENTUM_FLIP_STOP_PCT = 0.005
MA_EARLY_MOMENTUM_FLIP_WINDOW_SEC = 1800
MA_MIN_PROFIT_TARGET_PCT = 0.010
# 微利地板只負責 0.3%~0.5% 的小峰值保護；0.5% 以上直接進入移動停利（棘輪）機制
MA_MICRO_PROFIT_ARM_PCT = 0.003  # 0.3% 啟動微利地板（最小保護層）
MA_MICRO_PROFIT_KEEP_RATIO = 0.70  # 微利層保留 70% 峰值
# 實測 FILUSDT 案例：鎖利價同步到交易所端 STOP_MARKET 後，行情急速反轉時，
# 停損單觸發後市價成交實際滑價達 0.29%（0.7235 觸發 -> 0.7214 成交），遠大於
# 原本只留 0.05% 的緩衝，導致理論上鎖住的小賺變成實際虧損。兩層緩衝都拉高到
# 0.20%，讓「手續費 0.10% + 緩衝 0.20%」= 0.30% 的總門檻能扛住這種急反轉滑價，
# 不再是滑一下就穿。
MA_MICRO_PROFIT_NET_BUFFER_PCT = 0.001
MA_PROFIT_FLOOR_ARM_PCT = 0.005  # 0.5% 就進入移動停利（原 0.8% 固定地板層，改讓移動停利更早接管）
MA_PROFIT_FLOOR_NET_BUFFER_PCT = 0.001
MA_PROFIT_FLOOR_CONFIRM_TICKS = 3
MA_PROFIT_FLOOR_CONFIRM_SEC = 1.0
MA_PROFIT_FLOOR_TREND_CONFIRM_SEC = 2.0
# 移動停利（棘輪鎖利）：峰值達 0.5% 就啟動真正的追蹤停利，隨利潤往上移動
# 原本設為 MA_MIN_PROFIT_TARGET_PCT（1.0%），ETH 這次峰值 0.42% 根本沒機會進入棘輪，
# 改為 0.5% 讓中等波段也能享有移動停利保護。
MA_PEAK_LOCK_ARM_PCT = 0.005  # 0.5% 峰值啟動棘輪移動停利（原 1.0% 太晚）
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
# 才出場所造成的回吐。刻意不跟 MA_MICRO_PROFIT_ARM_PCT 綁在一起、門檻設得
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
        # 初始保護單尚未建立時交由 _ensure_exchange_exit_orders 一次完成。
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
        if confirmed_peak < MA_MICRO_PROFIT_ARM_PCT:
            _reset_ma_profit_floor_confirmation(s)
            return False, float(s.get("ma_profit_floor_price", 0.0) or 0.0)
        # 0.20%~0.30% 是微利保護層：保留峰值 60%，並至少涵蓋雙邊費用 + 0.05%。
        # 0.30%~0.60% 改為保留 80%；兩層都只建立移動地板，不是固定價停利。
        is_micro_profit = confirmed_peak < MA_PROFIT_FLOOR_ARM_PCT
        keep_ratio = MA_MICRO_PROFIT_KEEP_RATIO if is_micro_profit else 0.80
        protective_fee_floor = (
            ROUND_TRIP_FEE_PCT + MA_MICRO_PROFIT_NET_BUFFER_PCT
            if is_micro_profit else fee_floor
        )
        previous_floor = float(s.get("ma_profit_floor_price", 0.0) or 0.0)
        locked_mid = max(protective_fee_floor, confirmed_peak * keep_ratio)
        if is_long:
            floor_price = avg * (1.0 + locked_mid)
            # 棘輪：只能往更保護的方向推進（更高）
            floor_price = max(float(s.get("ma_profit_floor_price", 0.0) or 0.0), floor_price)
        else:
            floor_price = avg * (1.0 - locked_mid)
            prev_floor = float(s.get("ma_profit_floor_price", 0.0) or 0.0)
            floor_price = min(prev_floor if prev_floor > 0 else float("inf"), floor_price)
        s["ma_profit_floor_armed"] = True
        s["ma_profit_floor_price"] = floor_price
        if abs(floor_price - previous_floor) > avg * 0.000001:
            _schedule_ma_exchange_profit_stop(sym)
        # [保護] 若當前利潤明顯高於 floor（代表價格在 floor 上方往上走），不要觸發出場。
        # 「price <= floor」只有在真正跌穿 floor 時才成立；若 current_price 還在 floor 上方，
        # crossed=False，讓利潤繼續跑。
        crossed = current_price <= floor_price if is_long else current_price >= floor_price
        # 鎖利線是為了保住淨利，不可在價格已跳空越過底線、連雙邊費用都無法
        # 涵蓋時，仍等待數秒確認後追價虧損平倉。此時把出場權交回 MA 轉折及
        # 災難停損；若價格重新回到可獲利區，鎖利線仍可再次正常生效。
        if crossed and profit < ROUND_TRIP_FEE_PCT:
            # 如果已經跌穿，為了避免進一步虧損，強行平倉（不重設為未確認狀態），
            # 確保不會再放大虧損退回災難停損。
            return True, floor_price
        if not crossed:
            s["ma_profit_floor_missed"] = False
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
        self.profit_threshold = 0.30        # 至少覆蓋來回費用與一般市場雜訊後才啟動
        self.stagnation_range = 0.0005       # 盤整區間 (0.05%)
        self.no_high_time_limit = 60         # 盤整判定時間 (60秒內沒創新高)

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
        tolerance_pct = max(0.0012, min((self.max_profit_pct / 100.0) * 0.25, 0.0050))
        is_retracing = False
        if self.is_long and current_price < (self.current_max_price * (1 - tolerance_pct)):
            is_retracing = True
        elif not self.is_long and current_price > (self.current_max_price * (1 + tolerance_pct)):
            is_retracing = True

        if is_retracing:
            print(f"💰 [觸發：回撤比例(極限)] 價格從最高點回落超過 {tolerance_pct*100:.3f}%，快速落袋為安。")
            return "SELL"

        # 2. 動態耐心極限 (Time-out)
        if elapsed_time >= self.wait_time_limit:
            print(f"💰 [觸發：耐心極限] 已等待 {elapsed_time:.1f}秒 (限時 {self.wait_time_limit:.1f}秒)，強制落袋為安。")
            return "SELL"

        # 3. 盤整最高點 (Stagnation)
        is_stagnant = abs(current_price - self.current_max_price) <= (self.current_max_price * self.stagnation_range)
        if time_since_high > self.no_high_time_limit and is_stagnant:
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
    實作非對稱移動停損 (Asymmetric Trailing Stop)
    當價格創新高/新低時，上移停損點，且加入保本緩衝區防止被雜訊洗出場。
    """
    s = ctx.STATES[sym]
    atr_val = s.get("current_atr", 0.0)
    if atr_val <= 0:
        return False, s["trailing_stop_price"]

    atr_history = s.get("atr_history", [])
    atr_avg = float(np.mean(atr_history)) if len(atr_history) > 0 else atr_val
    safe_atr = min(atr_val, atr_avg * 3) if atr_avg > 0 else atr_val

    trailing_activation_atr = s.get("trailing_activation_atr", 0.0)
    # 0.35%(高彈) / 0.25%(一般) 開始建立全倉保護線；實際獲利出場仍由
    # 高點量縮全平或 trailing 穿越決定，不再拆分倉位。
    profile_type = str(s.get("profile_type", ""))
    is_high_beta = "High_Beta" in profile_type or "Speculative" in profile_type
    breakeven_threshold = 0.015 if is_high_beta else 0.012
    
    fee_safe_profit = ROUND_TRIP_FEE_PCT + 0.0015
    trailing_distance_atr = s.get("trailing_distance_atr", s.get("trailing_stop_multiplier", 2.5))
    profit_lock_atr = s.get("profit_lock_atr", 0.0)

    avg_price = s["avg_price"]
    leverage = s.get("leverage", 8)
    mm_ratio = 0.004
    if is_long:
        liq_price = avg_price * (1 - 1.0 / leverage) / (1 - mm_ratio) if leverage > 0 else 0.0
    else:
        liq_price = avg_price * (1 + 1.0 / leverage) / (1 + mm_ratio) if leverage > 0 else 0.0

    profit_pct = _profit_pct(current_price, avg_price, is_long)
    _prev_peak = s.get("highest_profit_pct", 0.0)
    if update_peak:
        s["highest_profit_pct"] = max(_prev_peak, profit_pct)
    if update_peak and s["highest_profit_pct"] > _prev_peak:
        # 即時把新高點存檔，而不是只在重啟時呼叫
        # save_peak，兩次重啟之間爬到的真正高點從未落地，若中途又重啟（例如部署修改），
        # 峰值記憶會被打回重啟當下的價位，讓所有靠 highest_profit_pct 判斷的鎖利機制
        # 都以為從沒漲那麼高過（實測 SUIUSDT 真實高點 1.25%，因為中途重啟兩次，
        # 最後只記得 0.25%，鎖利鎖在遠低於真正高點的地方）。
        from core.peak_store import save_peak
        save_peak(sym, s["highest_profit_pct"])

    # MA 路線的價格型停利只由 update_ma_peak_lock() 管理。通用 trailing 在此永遠
    # 不參與，避免主循環與即時 tick 各自產生不同回吐線。
    # MA7/25 生命週期、錯向風控及交易所災難止損仍由各自的風險路徑保留。
    route = str(s.get("entry_reason", "") or "").lower()
    if route in MA_ENTRY_ROUTES:
        trailing_stop = float(s.get("trailing_stop_price", 0.0) or 0.0)
        stop_loss = float(s.get("stop_loss", 0.0) or 0.0)
        trailing_is_profit_side = trailing_stop > 0 and (
            (is_long and trailing_stop >= avg_price) or
            (not is_long and trailing_stop <= avg_price)
        )
        stop_is_profit_side = stop_loss > 0 and (
            (is_long and stop_loss >= avg_price) or
            (not is_long and stop_loss <= avg_price)
        )
        if trailing_is_profit_side or stop_is_profit_side:
            from core.config import HARD_STOP_LOSS_PCT as _HARD_SL_DEFAULT
            hard_sl_pct = float(s.get("hard_stop_loss_pct", _HARD_SL_DEFAULT) or _HARD_SL_DEFAULT)
            fallback_stop = (
                avg_price * (1.0 - hard_sl_pct) if is_long
                else avg_price * (1.0 + hard_sl_pct)
            )
            s["trailing_stop_price"] = fallback_stop
            s["stop_loss"] = fallback_stop
            s["is_breakeven_locked"] = False
            s["soft_trailing_armed"] = False
            s["soft_trailing_profit_floor"] = 0.0
            logger.info(
                f"♻️ [MA_Trailing_Reset] {sym} 峰值 {s['highest_profit_pct']*100:.2f}% "
                f"使用專用 MA PeakLock，移除重疊的通用獲利追蹤線"
            )
        return False, s.get("trailing_stop_price", 0.0)

    # --- [Updated] Break-Even Mechanism ---
    # 保本線必須涵蓋雙邊 taker fee，另加 0.02% 滑價緩衝；否則名義保本仍會淨虧。
    # [2026-07-14 修正B] 門檻從 1.0%(高彈)•0.6%(一般) 降至 0.6%/0.4%：
    # 實測 64 筆交易平均峰值僅 0.331%，舊門檻 0.6%/1.0% 與峰值完全對不上——
    # 峰值到了卻沒觸發保本鎖，最後度被 [Peak_Giveback] 小與張出場。
    # 降至 0.4%(一般) / 0.6%(高彈)，讓保本鎖在真實峰值範圍內生效。
    profile_type = str(s.get("profile_type", ""))
    is_high_beta = "High_Beta" in profile_type or "Speculative" in profile_type
    is_range_route = route in RANGE_ENTRY_ROUTES
    breakeven_threshold = RANGE_TRAILING_MIN_GROSS_PCT if is_range_route else 0.0025

    fee_safe_profit = ROUND_TRIP_FEE_PCT + (
        RANGE_TRAILING_NET_BUFFER_PCT if is_range_route else 0.0015
    )
    _hp_soft = s.get("highest_profit_pct", 0.0)
    if _hp_soft >= breakeven_threshold:
        should_log_breakeven = not bool(s.get("is_breakeven_locked", False))
        # Ensure the stop-loss is at least at the entry price (+ 0.01% buffer)
        # For long: new_sl >= entry; For short: new_sl <= entry
        if is_long:
            new_be_sl = avg_price * (1.0 + fee_safe_profit)
            s["trailing_stop_price"] = max(s.get("trailing_stop_price", 0.0), new_be_sl)
            s["stop_loss"] = s["trailing_stop_price"]
            s["is_breakeven_locked"] = True
        else:
            new_be_sl = avg_price * (1.0 - fee_safe_profit)
            _cur_ts_short = s.get("trailing_stop_price", 0.0)
            s["trailing_stop_price"] = min(_cur_ts_short if _cur_ts_short > 0 else float('inf'), new_be_sl)
            s["stop_loss"] = s["trailing_stop_price"]
            s["is_breakeven_locked"] = True
        if should_log_breakeven:
            logger.info(f"🛡️ [Break-Even] {sym} profit {profit_pct*100:.2f}% > {breakeven_threshold*100}%, SL moved to entry")

    profit_atr_multiple = (current_price - avg_price) / atr_val if is_long else (avg_price - current_price) / atr_val
    profile_type = str(s.get("profile_type", ""))
    min_trailing_profit = 0.002

    from core.config import HARD_STOP_LOSS_PCT as _HARD_SL_DEFAULT
    _hard_sl_pct = float(s.get("hard_stop_loss_pct", _HARD_SL_DEFAULT) or _HARD_SL_DEFAULT)
    _LIN_TRAIL_ACTIVATION_PCT = 0.002   # 0.2% 浮盈開始移動停利
    _LIN_TRAIL_CATCH_UP_RATIO = 0.75    # 利潤往上，移動停利以 0.75 追隨比例緊密向上推升

    if not s.get("_lin_trail_armed", False) and s["highest_profit_pct"] >= _LIN_TRAIL_ACTIVATION_PCT:
        s["_lin_trail_armed"] = True
        s["_lin_trail_activation_price"] = (
            avg_price * (1 + _LIN_TRAIL_ACTIVATION_PCT) if is_long
            else avg_price * (1 - _LIN_TRAIL_ACTIVATION_PCT)
        )
        s["_lin_trail_activation_stop"] = (
            avg_price * (1 - _hard_sl_pct) if is_long
            else avg_price * (1 + _hard_sl_pct)
        )

    _linear_trail_candidate = None
    if s.get("_lin_trail_armed", False):
        _activation_price = s["_lin_trail_activation_price"]
        _activation_stop = s["_lin_trail_activation_stop"]
        _extension = (current_price - _activation_price) if is_long else (_activation_price - current_price)
        _extension = max(_extension, 0.0)  # 只在價格持續延伸時推進，回落不會倒退活化基準
        _linear_trail_candidate = (
            _activation_stop + _LIN_TRAIL_CATCH_UP_RATIO * _extension if is_long
            else _activation_stop - _LIN_TRAIL_CATCH_UP_RATIO * _extension
        )

    if is_long:
        if current_price > s.get("trailing_highest", 0.0):
            s["trailing_highest"] = current_price

        trail_sl = s["trailing_stop_price"]

        if _linear_trail_candidate is not None:
            trail_sl = max(trail_sl, _linear_trail_candidate)

        # 使用者要求「碰到小獲利就先入袋，不要冒風險等它變大，但利潤往上就跟上」，
        # 後續再加碼：「動能一直往上就回吐容忍度加寬，利潤到高處盤整時容忍度再收緊」。
        # 啟動門檻 0.20%（低於來回費用緩衝 0.15% 會導致門檻剛觸發那一刻停利線就高於
        # 現價、瞬間誤砍，實測驗證過 0.20% 有安全空間）。回吐容忍度不再是固定值，改
        # 回吐容忍度依 ATR 波動環境調整，避免依賴已移除的 MACD 交易規則。
        # Soft Trailing 啟動門檻拉高至 0.45%，給予利潤足夠的奔跑與震盪空間
        if GENERIC_TRAILING_ARM_PCT <= _hp_soft:
            # ── 動態緩衝區 (Dynamic ATR Buffer) ──
            # 根據當前幣種的 ATR% (ATR / 現價) 動態調整回撤緩衝比例。
            # 波動劇烈時自動放寬從 0.2% 至 0.4% (1.2 * ATR_PCT)，避免被「毛刺」洗出場；
            # 平緩市場維持 0.2% 緊密護航。
            _atr_pct = (atr_val / current_price) if current_price > 0 else 0.002
            _soft_tolerance = max(0.0020, min(0.0040, _atr_pct * 1.2))
                
            # 保本低限：進場價 + 雙邊費用 + 0.05% 安全微利
            _soft_floor = avg_price * (1.0 + ROUND_TRIP_FEE_PCT + 0.0005)
            # 獲利回吐平倉點，硬性要求不可低於保本低限 _soft_floor，確保不虧損
            _soft_sl = max(s["trailing_highest"] * (1.0 - _soft_tolerance), _soft_floor)
            trail_sl = max(trail_sl, _soft_sl)
            s["soft_trailing_armed"] = True
            s["soft_trailing_profit_floor"] = _soft_floor
            # 強制將目前的移動停損更新為保本以上的價格
            s["trailing_stop_price"] = max(s.get("trailing_stop_price", 0.0), _soft_sl)

        if profit_lock_atr > 0 and profit_atr_multiple >= profit_lock_atr and profit_pct >= min_trailing_profit:
            locked_sl = avg_price * 1.001
            trail_sl = max(trail_sl, locked_sl)
        elif trailing_activation_atr > 0 and profit_atr_multiple >= trailing_activation_atr and profit_pct >= min_trailing_profit:
            dynamic_sl = s["trailing_highest"] - (atr_val * trailing_distance_atr)
            trail_sl = max(trail_sl, dynamic_sl)
        elif trailing_activation_atr == 0:
            # 獲利區間動態縮緊 Trailing Stop (Profit-Tier Dynamic Tightening)
            # 獲利越高，追蹤網越緊；獲利初期給對價格呼吸空間
            _hp_f = s["highest_profit_pct"]
            personality = s.get("personality", "balanced")
            profile_type = s.get("profile_type", "")
            if personality == "aggressive" or "High_Beta" in profile_type:
                if _hp_f > 0.10:    trailing_multiplier = 0.8
                elif _hp_f > 0.07:  trailing_multiplier = 0.9
                elif _hp_f > 0.04:  trailing_multiplier = 1.2
                else:               trailing_multiplier = 1.6
            else:
                if _hp_f > 0.10:    trailing_multiplier = 0.8
                elif _hp_f > 0.07:  trailing_multiplier = 0.9
                elif _hp_f > 0.04:  trailing_multiplier = 1.2
                else:               trailing_multiplier = 1.5
            # 最小距離防護：確保至少 0.5% 緩衝 (根據分析結果，避免在 0.4%-0.5% 回撤時被掃出場)
            _min_gap_l = max(atr_val * trailing_multiplier, s["trailing_highest"] * 0.005)
            dynamic_sl = s["trailing_highest"] - _min_gap_l

            trigger_mult = s.get("breakeven_trigger", s.get("sl_atr_multiplier", 1.5))
            sl_dist_atr = trigger_mult * atr_val
            breakeven_trigger = avg_price + sl_dist_atr
            if current_price >= breakeven_trigger:
                dynamic_sl = max(dynamic_sl, avg_price)

            trail_sl = max(trail_sl, dynamic_sl)

        # 這是「在真的被交易所強平前，自己先出場」的安全下限，理應落在 liq_price 跟
        # avg_price 之間。原本寫成 liq_price * 1.2：liq_price 本身已經在 avg_price 下方
        # (例如 8x 槓桿時只有 avg 的 0.8785 倍)，直接乘 1.2 會把它推過 avg_price，變成
        # 「安全下限」高於進場價，導致還沒虧錢就被這道底線強制停損（XLMUSDT 實測案例：
        # 8x 槓桿 liq_price=avg*0.8785，*1.2 後 =avg*1.054，直接高於進場價，開倉沒多久
        # 淨值還沒跌破 0.1% 就被這條「安全線」洗出場）。改成在 liq_price 到 avg_price
        # 的距離上取一個固定比例當緩衝，確保這條線永遠嚴格落在兩者之間。
        _liq_buffer_frac = 0.15
        safe_min_sl = liq_price + (avg_price - liq_price) * _liq_buffer_frac
        new_sl = max(trail_sl, safe_min_sl)

        if new_sl > s["trailing_stop_price"]:
            s["trailing_stop_price"] = new_sl
            logger.info(f"🛡️ [Trailing_SL] {sym} 移動止損上移至 {new_sl:.4f} (獲利倍數: {profit_atr_multiple:.1f}x ATR)")

    else:
        if current_price < s.get("trailing_lowest", float('inf')):
            s["trailing_lowest"] = current_price

        trail_sl = s["trailing_stop_price"]
        if trail_sl == 0.0:
            trail_sl = float('inf')

        if _linear_trail_candidate is not None:
            trail_sl = min(trail_sl, _linear_trail_candidate)

        # 空單對稱版：Soft Trailing 啟動門檻拉高至 0.45%
        if GENERIC_TRAILING_ARM_PCT <= _hp_soft:
            atr_history_v = s.get("atr_history", [])
            atr_24h_avg_v = float(np.mean(atr_history_v)) if len(atr_history_v) > 0 else 0.0
            is_low_vol_exit = atr_val <= atr_24h_avg_v if atr_24h_avg_v > 0 else False
            _soft_tolerance = 0.0020 if is_low_vol_exit else 0.0012

            # 保本高限：進場價 - 雙邊費用 - 0.05% 安全微利
            _soft_ceiling = avg_price * (1.0 - ROUND_TRIP_FEE_PCT - 0.0005)
            # 獲利回吐平倉點，硬性要求不可高於保本高限 _soft_ceiling，確保不虧損
            _soft_sl = min(s["trailing_lowest"] * (1.0 + _soft_tolerance), _soft_ceiling)
            trail_sl = min(trail_sl, _soft_sl)
            s["soft_trailing_armed"] = True
            s["soft_trailing_profit_floor"] = _soft_ceiling
            # 強制將目前的移動停損更新為保本高限以下的價格（空單需要小於保本價）
            ts_price_val = s.get("trailing_stop_price", float('inf'))
            s["trailing_stop_price"] = min(ts_price_val if ts_price_val > 0 else float('inf'), _soft_sl)

        if profit_lock_atr > 0 and profit_atr_multiple >= profit_lock_atr and profit_pct >= min_trailing_profit:
            locked_sl = avg_price * 0.999
            trail_sl = min(trail_sl, locked_sl)
        elif trailing_activation_atr > 0 and profit_atr_multiple >= trailing_activation_atr and profit_pct >= min_trailing_profit:
            dynamic_sl = s["trailing_lowest"] + (atr_val * trailing_distance_atr)
            trail_sl = min(trail_sl, dynamic_sl)
        elif trailing_activation_atr == 0:
            # 獲利區間動態縮緊 Trailing Stop (空單)
            _hp_fs = s["highest_profit_pct"]
            personality = s.get("personality", "balanced")
            profile_type = s.get("profile_type", "")
            if personality == "aggressive" or "High_Beta" in profile_type:
                if _hp_fs > 0.10:   trailing_multiplier = 0.6
                elif _hp_fs > 0.07: trailing_multiplier = 0.7
                elif _hp_fs > 0.04: trailing_multiplier = 0.9
                else:               trailing_multiplier = 1.3
            else:
                if _hp_fs > 0.10:   trailing_multiplier = 0.6
                elif _hp_fs > 0.07: trailing_multiplier = 0.7
                elif _hp_fs > 0.04: trailing_multiplier = 0.9
                else:               trailing_multiplier = 1.1
            # 最小距離防護：確保至少 0.5% 緩衝 (根據分析結果，避免在 0.4%-0.5% 回撤時被掃出場)
            _min_gap_s = max(atr_val * trailing_multiplier, s["trailing_lowest"] * 0.005)
            dynamic_sl = s["trailing_lowest"] + _min_gap_s

            trigger_mult = s.get("breakeven_trigger", s.get("sl_atr_multiplier", 1.5))
            sl_dist_atr = trigger_mult * atr_val
            breakeven_trigger = avg_price - sl_dist_atr
            if current_price <= breakeven_trigger:
                dynamic_sl = min(dynamic_sl, avg_price)

            trail_sl = min(trail_sl, dynamic_sl)

        # 空單對稱版：liq_price 在 avg_price 上方，同樣在兩者距離上取固定比例當緩衝，
        # 而不是直接對 liq_price 乘一個係數（原本 *0.98 對高槓桿來說緩衝太薄，多空兩邊
        # 的安全係數也不一致，屬於同一個計算方式錯誤的兩個症狀）。
        _liq_buffer_frac = 0.15
        safe_max_sl = liq_price - (liq_price - avg_price) * _liq_buffer_frac
        new_sl = min(trail_sl, safe_max_sl)

        if s["trailing_stop_price"] == 0.0 or new_sl < s["trailing_stop_price"]:
            s["trailing_stop_price"] = new_sl
            logger.info(f"🛡️ [Trailing_SL] {sym} 移動止損下移至 {new_sl:.4f} (獲利倍數: {profit_atr_multiple:.1f}x ATR)")

    return False, s["trailing_stop_price"]


async def check_exits(sym):
    from core.orders import close_position, execute_order
    s = ctx.STATES[sym]
    if s.get("_external_close_record_pending", False):
        return
    if s.get("adjusted_this_tick", False):
        return
    if abs(s["qty"]) < 0.000001 or s["avg_price"] <= 0:
        return

    if s.get("current_atr", 0.0) <= 0:
        return

    p = s["close_price"]
    avg = s["avg_price"]
    is_long = s["qty"] > 0
    profit_pct = (p - avg) / avg if is_long else (avg - p) / avg
    if profit_pct > s.get("highest_profit_pct", 0.0):
        s["highest_profit_pct"] = profit_pct

    entry_reason = str(s.get("entry_reason", "") or "")
    is_range_route = entry_reason.lower() in RANGE_ENTRY_ROUTES

    # ── 第一階段：扣除費用與滑價後仍有實質利潤，才先平 50% ──
    if profit_pct >= PARTIAL_TP_MIN_GROSS_PCT and not s.get("partial_tp_done", False):
        cs = "sell" if is_long else "buy"
        half_qty = abs(s["qty"]) * 0.5
        logger.info(
            f"💰 [Partial_TP_50Pct] {sym} 浮盈達 {profit_pct*100:.2f}% "
            f">= {PARTIAL_TP_MIN_GROSS_PCT*100:.2f}%，"
            f"先平倉 50% 倉位 ({half_qty:.4f}) 落袋為安，剩餘 50% 鎖定保本放飛！"
        )
        s["partial_tp_done"] = True
        await close_position(
            sym, cs, half_qty, p, avg,
            reason="[Partial_TP_50Pct]", is_stop_loss=False,
        )
        return

    # ── 第二階段：剩餘倉位 70% 利潤保留鎖定 (70% Profit Retained Lock) ──
    # 最高浮盈達 >= 0.40% 後，若利潤回吐僅剩 70%，平倉剩餘 50% 倉位！
    highest_profit = float(s.get("highest_profit_pct", 0.0) or 0.0)
    if not is_range_route and highest_profit >= 0.0040 and profit_pct <= (highest_profit * 0.70):
        cs = "sell" if is_long else "buy"
        logger.info(
            f"🛡️ [Profit_70Pct_Retained_TP] {sym} 最高浮盈 {highest_profit*100:.2f}% "
            f"回吐至 {profit_pct*100:.2f}% (保留 70% 利潤)，急煞停利落袋為安！"
        )
        await close_position(
            sym, cs, abs(s["qty"]), p, avg,
            reason="[Profit_70Pct_Retained_TP]", is_stop_loss=False,
        )
        return

    current_atr = s.get("current_atr", 0.0)

    if is_range_route:
        # Range 的結構停損是真突破安全線，仍採盤中立即退出；只有較貼近價格的
        # Dynamic Trailing 改成收線確認，不能讓延遲機制蓋掉真正的結構破壞。
        range_sl = float(s.get("range_sl_price", 0.0) or 0.0)
        range_sl_hit = range_sl > 0 and (
            (is_long and p <= range_sl) or (not is_long and p >= range_sl)
        )
        if range_sl_hit:
            cs = "sell" if is_long else "buy"
            logger.info(f"🚨 [Range_Mode_SL] {sym} 觸及區間結構停損 {range_sl:.6f}，立即平倉")
            await close_position(
                sym, cs, abs(s["qty"]), p, avg,
                reason="[Range_SL]", is_stop_loss=True,
            )
            return

        # 移動停利穿越：持倉前 60 秒屬於「初始呼吸保護期」，禁止任何移動停利/保本平倉，避免剛進場被雜訊秒平。
        hold_sec = max(0.0, time.time() - float(s.get("open_time", time.time()) or time.time()))
        ts_price = float(s.get("trailing_stop_price", 0.0) or 0.0)
        trailing_crossed = (hold_sec >= 60.0) and ts_price > 0 and (
            (is_long and p <= ts_price) or (not is_long and p >= ts_price)
        )
        # 未覆蓋最低毛利時，即使舊保本線被穿越也不可當成停利。
        if highest_profit < RANGE_TRAILING_MIN_GROSS_PCT:
            _reset_range_trailing_confirmation(s)
            trailing_crossed = False
        if _range_trailing_cross_confirmed(sym, trailing_crossed, time.time()):
            cs = "sell" if is_long else "buy"
            # ── 多級獲利目標 (Multi-stage TP) ──
            # 當達到最高點回撤觸發停利線時，若浮盈 > 0 且尚未分批，先賣出 50% 落袋為安
            if (highest_profit >= PARTIAL_TP_MIN_GROSS_PCT
                    and not s.get("_multistage_tp1_done", False)
                    and profit_pct > 0.0015):
                s["_multistage_tp1_done"] = True
                tp1_qty = abs(float(s["qty"])) * 0.50
                logger.info(
                    f"🎯 [MultiStage_TP1] {sym} 浮盈 {profit_pct*100:.2f}% 觸發動態停利線，"
                    f"先賣出 50% 部位 ({tp1_qty:.4f}) 鎖定獲利！剩餘 50% 鎖定保本繼續追蹤"
                )
                await close_position(
                    sym, cs, tp1_qty, p, avg,
                    reason="[MultiStage_TP1]",
                    is_stop_loss=False,
                )
                fee_safe_profit = ROUND_TRIP_FEE_PCT + 0.0015
                s["trailing_stop_price"] = avg * (1.0 + fee_safe_profit if is_long else 1.0 - fee_safe_profit)
                return

            logger.info(
                f"🚨 [Range_Trailing_Closed_Confirm] {sym} 現價 {p:.6f} 持續穿越保護線 "
                f"{ts_price:.6f}，確認出場"
            )
            await close_position(
                sym, cs, abs(s["qty"]), p, avg,
                reason="[Range_Trailing_Closed_Confirm]",
                is_stop_loss=(profit_pct <= 0),
            )
            return

    # MA 波段以高點回吐鎖利或反向交叉結束；持倉初期若兩根已收線 K 棒確認開錯方向，立即止損。
    route = str(s.get("entry_reason", "") or "").lower()
    if route in MA_ENTRY_ROUTES:
        ma7 = float(s.get("ma7", 0.0) or 0.0)
        ma25 = float(s.get("ma25", 0.0) or 0.0)
        prev_ma7 = float(s.get("prev_ma7", ma7) or ma7)
        prev_ma25 = float(s.get("prev_ma25", ma25) or ma25)
        ma_candle_ts = int(s.get("ma_candle_ts", 0) or 0)
        candles = s.get("ohlcv", [])
        hold_sec = max(0.0, time.time() - float(s.get("open_time", time.time()) or time.time()))


        wrong_direction_confirmed = False
        if len(candles) >= 3 and hold_sec <= MA_WRONG_DIRECTION_WINDOW_SEC and profit_pct <= -MA_WRONG_DIRECTION_PCT:
            previous_closed, latest_closed = candles[-3], candles[-2]
            open_ms = int(float(s.get("open_time", 0.0) or 0.0) * 1000)
            both_closed_after_entry = int(latest_closed[0]) >= open_ms > 0
            if is_long:
                two_opposite = float(previous_closed[4]) < float(previous_closed[1]) and float(latest_closed[4]) < float(latest_closed[1])
            else:
                two_opposite = float(previous_closed[4]) > float(previous_closed[1]) and float(latest_closed[4]) > float(latest_closed[1])
            avg_reversal_volume = (float(previous_closed[5]) + float(latest_closed[5])) / 2.0
            reversal_vol_ma20 = float(s.get("vol_ma20", 0.0) or 0.0)
            volume_confirmed = reversal_vol_ma20 > 0 and avg_reversal_volume >= reversal_vol_ma20 * 0.8
            wrong_direction_confirmed = both_closed_after_entry and two_opposite and volume_confirmed

        if wrong_direction_confirmed:
            cs = "sell" if is_long else "buy"
            logger.info(
                f"🛑 [MA_Wrong_Direction_Confirmed] {sym} 持倉 {hold_sec:.0f} 秒，"
                f"連續兩根反向收線且逆勢 {abs(profit_pct)*100:.2f}%，立即平倉"
            )
            await close_position(
                sym, cs, abs(s["qty"]), p, avg,
                reason="[MA_Wrong_Direction_Confirmed]", is_stop_loss=True,
            )
            return

        # 峰值回吐安全網：不等 MA7 轉彎收線確認，浮盈已經從峰值回吐超過一半
        # 就直接出清（見 MA7_PROFIT_TURN_GIVEBACK_KEEP_RATIO 定義說明）。
        _turn_peak = float(s.get("highest_profit_pct", 0.0) or 0.0)
        if (
            _turn_peak >= MA7_PROFIT_TURN_MIN_PCT
            and profit_pct < _turn_peak * MA7_PROFIT_TURN_GIVEBACK_KEEP_RATIO
        ):
            cs = "sell" if is_long else "buy"
            logger.info(
                f"⚠️ [MA7_Profit_Turn_Giveback] {sym} 峰值 {_turn_peak*100:.2f}% 已回吐至 "
                f"{profit_pct*100:.2f}%（低於保留門檻 {MA7_PROFIT_TURN_GIVEBACK_KEEP_RATIO*100:.0f}%），"
                f"不再等下一根收線確認，直接出清剩餘部位"
            )
            await close_position(
                sym, cs, abs(s["qty"]), p, avg,
                reason="[MA7_Profit_Turn_Giveback]", is_stop_loss=(profit_pct <= 0),
            )
            s["ma7_profit_turn_stage"] = 0
            s["ma7_profit_turn_signal_ts"] = 0
            s["ma7_profit_turn_signal_extreme"] = 0.0
            return

        # 獲利中的 MA7 轉彎分批出場：只採已收線 K 棒，避免盤中 MA7 抖動誤殺。
        # 第一根轉彎先平 60%；下一根 MA7 繼續反向，或價格突破訊號棒低/高點，再全平。
        # 若下一根重新回到原趨勢並站回 MA7，解除等待，讓剩餘部位繼續奔跑。
        turn_triggered, turn_data = _ma7_closed_turn(candles, is_long)
        turn_stage = int(s.get("ma7_profit_turn_stage", 0) or 0)
        turn_signal_ts = int(s.get("ma7_profit_turn_signal_ts", 0) or 0)
        turn_candle_ts = int(turn_data.get("candle_ts", 0) or 0)

        if turn_stage == 1 and turn_candle_ts > turn_signal_ts:
            turn_slope = float(turn_data.get("current_slope", 0.0) or 0.0)
            turn_close = float(turn_data.get("close", 0.0) or 0.0)
            signal_extreme = float(s.get("ma7_profit_turn_signal_extreme", 0.0) or 0.0)
            continuation = (
                (is_long and (turn_slope < 0 or (signal_extreme > 0 and turn_close < signal_extreme)))
                or (not is_long and (turn_slope > 0 or (signal_extreme > 0 and turn_close > signal_extreme)))
            )
            recovered = (
                (is_long and turn_slope > 0 and turn_close >= float(turn_data.get("ma7", 0.0) or 0.0))
                or (not is_long and turn_slope < 0 and turn_close <= float(turn_data.get("ma7", 0.0) or 0.0))
            )
            if continuation:
                cs = "sell" if is_long else "buy"
                logger.info(
                    f"💰 [MA7_Profit_Turn_Confirmed] {sym} 下一根收線確認 MA7 反轉，"
                    f"斜率={turn_slope:.8f}、close={turn_close:.6f}，平掉剩餘部位"
                )
                await close_position(
                    sym, cs, abs(s["qty"]), p, avg,
                    reason="[MA7_Profit_Turn_Confirmed]", is_stop_loss=(profit_pct <= 0),
                )
                return
            if recovered:
                logger.info(f"↗️ [MA7_Profit_Turn_Recovered] {sym} MA7 恢復原趨勢，保留剩餘部位")
                s["ma7_profit_turn_stage"] = 0
                s["ma7_profit_turn_signal_ts"] = 0
                s["ma7_profit_turn_signal_extreme"] = 0.0

        elif (
            turn_stage == 0 and turn_triggered
            and profit_pct >= MA7_PROFIT_TURN_MIN_PCT
        ):
            cs = "sell" if is_long else "buy"
            qty_before = abs(float(s["qty"]))
            partial_qty = qty_before * MA7_PROFIT_TURN_PARTIAL_RATIO
            logger.info(
                f"💰 [MA7_Profit_Turn_Partial] {sym} MA7 收線由"
                f"{'上轉下' if is_long else '下轉上'}且目前毛利 {profit_pct*100:.2f}%，"
                f"先平 {MA7_PROFIT_TURN_PARTIAL_RATIO*100:.0f}%"
            )
            await close_position(
                sym, cs, partial_qty, p, avg,
                reason="[MA7_Profit_Turn_Partial]", is_stop_loss=False,
            )
            # 只有數量確實下降且仍有剩餘倉位才進入第二階段；送單失敗時下個 tick 會重試。
            qty_after = abs(float(s.get("qty", 0.0) or 0.0))
            if 0.000001 < qty_after < qty_before - 0.000001:
                s["ma7_profit_turn_stage"] = 1
                s["ma7_profit_turn_signal_ts"] = turn_candle_ts
                s["ma7_profit_turn_signal_extreme"] = float(
                    turn_data.get("low" if is_long else "high", 0.0) or 0.0
                )
                s["has_partial_closed"] = True
            return


        # MA 路線只使用同一套 PeakLock／ProfitFloor。
        peak_lock_hit, peak_lock_price = update_ma_peak_lock(
            sym, p, is_long, require_confirmation=True,
        )
        if peak_lock_hit:
            cs = "sell" if is_long else "buy"
            peak_profit = float(s.get("highest_profit_pct", 0.0) or 0.0)
            reason = "[MA_Peak_Lock]" if s.get("ma_peak_lock_armed", False) else "[MA_Profit_Floor]"
            logger.info(
                f"💰 {reason} {sym} 峰值 {peak_profit*100:.2f}% 回吐至 "
                f"鎖利價 {peak_lock_price:.6f}，結束本段波段"
            )
            await close_position(
                sym, cs, abs(s["qty"]), p, avg,
                reason=reason, is_stop_loss=False,
            )
            return

        disaster_hit = (
            (is_long and p <= avg * (1.0 - MA_DISASTER_STOP_PCT)) or
            (not is_long and p >= avg * (1.0 + MA_DISASTER_STOP_PCT))
        )
        if disaster_hit:
            cs = "sell" if is_long else "buy"
            logger.info(f"🛡️ [MA_Disaster_Stop] {sym} 觸及 {MA_DISASTER_STOP_PCT*100:.1f}% 災難止損")
            await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[MA_Disaster_Stop]", is_stop_loss=True)
            return

        # MA lifecycle exit: a completed-candle MA7 break or opposite MA7/25 cross exits after 2 consecutive confirmations.
        closed_price = float(s.get("ohlcv", [])[-2][4]) if len(s.get("ohlcv", [])) >= 2 else 0.0
        if ma7 > 0 and ma25 > 0 and closed_price > 0 and ma_candle_ts:
            cross_invalid = (
                (is_long and prev_ma7 >= prev_ma25 and ma7 < ma25) or
                (not is_long and prev_ma7 <= prev_ma25 and ma7 > ma25)
            )
            if route == "ma7_simple":
                # MA7_Simple 是依 MA7 底部/頂部轉彎進場，出場也必須對稱地確認
                # MA7 已朝反方向轉彎並連續延伸；不能只因價格兩根收在線的另一側，
                # 就套用其他 MA 路由的雙均線跌破規則停損。
                ma7_broken, ma7_break_buffer = _ma7_simple_turn_break(
                    is_long, closed_price, ma7, current_atr, avg,
                    turn_triggered, turn_data, s.get("ma_exit_invalid_count", 0),
                )
            else:
                ma7_broken, ma7_break_buffer = _meaningful_ma7_break(
                    is_long, closed_price, ma7, ma25, prev_ma7, current_atr, avg
                )

            if s.get("ma_exit_last_candle_ts") != ma_candle_ts:
                s["ma_exit_last_candle_ts"] = ma_candle_ts
                if cross_invalid or ma7_broken:
                    s["ma_exit_invalid_count"] = s.get("ma_exit_invalid_count", 0) + 1
                else:
                    s["ma_exit_invalid_count"] = 0

            if s.get("ma_exit_invalid_count", 0) >= 2:
                cs = "sell" if is_long else "buy"
                if cross_invalid:
                    reason = "[MA7_MA25_Death_Cross]" if is_long else "[MA7_MA25_Golden_Cross]"
                else:
                    reason = "[MA7_Closed_Break]"
                logger.info(
                    f"🎯 [MA_Lifecycle_Exit] {sym} {reason} | closed={closed_price:.6f}, "
                    f"MA7={ma7:.6f}, MA25={ma25:.6f}, buffer={ma7_break_buffer:.6f}, confirms={s.get('ma_exit_invalid_count', 0)}"
                )
                await close_position(sym, cs, abs(s["qty"]), p, avg, reason=reason, is_stop_loss=(profit_pct <= 0))
                return

    # --- [新增] 動態退出管理器 (Dynamic Exit Manager) ---
    if "dynamic_exit_manager" not in s:
        s["dynamic_exit_manager"] = DynamicExitManager(avg, restored_peak_pct=s.get("highest_profit_pct", 0.0) * 100, is_long=is_long)
    
    manager = s["dynamic_exit_manager"]
    # 預設停用重疊的舊 DynamicExitManager，由單一 trailing/partial TP 管理停利。
    exit_signal = manager.update(p) if s.get("use_dynamic_exit_manager", False) else "HOLD"
    if exit_signal == "SELL":
        cs = 'sell' if is_long else 'buy'
        logger.info(f"🎯 [Dynamic_Exit_Trigger] {sym} 觸發動態退出機制 (耐心極限/盤整/回落)，執行平倉")
        await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[Dynamic_Exit_Manager]", is_stop_loss=False)
        return

    # --- [新增] 極速止損 (Fast-Exit Guard / Instant Trap) ---
    # 檢查開倉後 60 秒內的「瞬間陷阱」
    hold_sec = time.time() - s.get("open_time", time.time())
    if hold_sec < 60 and s.get("open_time", 0) > 0 and not s.get("restored_from_exchange", False):
        guard = FastReversalGuard()
        trap_signal = guard.check_instant_trap(avg, p, 'buy' if is_long else 'sell')
        current_vol = float(s.get("current_vol", 0.0) or 0.0)
        vol_ma20 = float(s.get("vol_ma20", 0.0) or 0.0)
        vol_ratio = current_vol / vol_ma20 if vol_ma20 > 0 else 0.0
        if trap_signal == "EXIT_INSTANT_TRAP" and vol_ratio > 3.0:
            cs = 'sell' if is_long else 'buy'
            logger.info(f"⚡ [Instant_Trap_Trigger] {sym} 開倉 {hold_sec:.1f} 秒內出現嚴重背離，立即砍倉。")
            await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[Fast_Reversal_Guard]", is_stop_loss=True)
            return

    # ── 急速逆勢提早出場 (Rapid Reversal Early Exit) ──
    # 用「距離上一次進場/攤平的時間」而不是「距離最初開倉的時間」，這樣攤平救援後
    # 才發生的急速逆勢也抓得到（例如：攤平加碼後不到 1 分鐘價格又急速創新高/新低，
    # 遠超正常波動，代表方向判斷可能真的錯了、而且錯得很快，不必等一般停損/盲區
    # 保護期跑完，提早出場並評估反手，避免虧損在等待期間繼續擴大）。
    # 用 ATR 倍數而非固定百分比衡量「急速」，高低價幣都適用同一套標準。
    _time_since_entry = time.time() - s.get("last_entry_time", 0)
    _ref_price = s.get("last_entry_price", avg) or avg
    if _time_since_entry < 180 and current_atr > 0 and _ref_price > 0:
        _adverse_atr_mult = (_ref_price - p) / current_atr if is_long else (p - _ref_price) / current_atr
        # 門檻原本是 1.2x，實際上線後對 ETH/SOL 這類主流大幣太敏感，短暫回檔（現貨
        # 換算損益只有 -0.24%~-0.6%）就被誤判成「急速逆勢」提前出場，反而讓單子沒機會
        # 等回本。拉高到 2.0x，只讓真正劇烈的逆勢（例如 MUSDT 那種閃崩）才觸發。
        # 最初 60 秒仍遵守盲區：只有 3 倍放量才允許急速逆勢提前砍倉。
        _rapid_volume_confirmed = vol_ratio > 3.0 if hold_sec < 60 else True
        if profit_pct < -0.005 and _adverse_atr_mult >= 3.5 and _rapid_volume_confirmed:
            cs = 'sell' if is_long else 'buy'
            logger.info(f"⚡ [急速不利走勢] {sym} 距上次進場僅 {_time_since_entry:.0f} 秒，價格已逆勢達 {_adverse_atr_mult:.2f}x ATR (虧損: {profit_pct*100:.2f}%)，提早風控出場")
            await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[Rapid_Adverse_Move]", is_stop_loss=True)
            return

    hold_sec = time.time() - s["open_time"] if s["open_time"] > 0 else 0
    atr_history = s.get("atr_history", [])
    atr_24h_avg = float(np.mean(atr_history)) if len(atr_history) > 0 else 0.0
    # 進場後觀察期拉長：給倉位更多時間脫離雜訊區，高波動 45s，正常 90s
    # 原本 20s/60s 太短，5m K 線一根就 300s，進場後瞬間的影線震盪容易直接觸發停損
    cooldown_limit = 45.0 if (current_atr > atr_24h_avg and atr_24h_avg > 0) else 90.0
    # ── 盲區保護 (Entry Blind Zone Protection) ──
    # 在進場最初 60 秒內，除非發生極高成交量 (Vol Ratio > 3.0) 的崩盤，否則不觸發一般停損。
    # 這可以防止開倉瞬間的影線（Wicks）直接洗出場。
    if hold_sec < 60:
        current_vol = s.get("current_vol", 0.0)
        vol_ma20 = s.get("vol_ma20", 1.0)
        vol_ratio = current_vol / vol_ma20 if vol_ma20 > 0 else 1.0
        
        if vol_ratio <= 3.0:
            # 在盲區內且成交量不足以證明是「真崩盤」，跳過後續所有停損檢查
            return

        logger.info(f"⚠️ [防插針豁免] {sym} 瞬時爆發量 (Ratio: {vol_ratio:.2f}x)，視為真崩盤，取消盲區保護！")

    # ══ 峰值更新（最優先，必須在所有出場機制之前執行）══
    # 含 K 線盤中尖峰（HIGH/LOW），讓 1 秒內的暴漲/暴跌也能被保本/PeakLock 捕捉
    # ⚠️ 舊版本此更新在 update_trailing_stop(line~642) 才跑，保本/PeakLock 全讀舊值
    # 為了讓保本/PeakLock 捕捉到正確的高點，我們必須在 check_exits 的最前面更新
    _ohlcv_early = s.get("ohlcv", [])
    _intra_peak_early = 0.0
    if _ohlcv_early and avg > 0:
        _lc = _ohlcv_early[-1]
        # 若本根 K 線在進場前已開始，HIGH/LOW 可能發生於持倉建立前；
        # 該根內真正的進場後峰值只採用即時成交流紀錄。
        _candle_started_sec = float(_lc[0] or 0.0) / 1000.0
        _opened_sec = float(s.get("open_time", 0.0) or 0.0)
        if _opened_sec <= 0 or _candle_started_sec >= _opened_sec:
            _intra_peak_early = (_lc[2] - avg) / avg if is_long else (avg - _lc[3]) / avg
    _prev_peak_early = s.get("highest_profit_pct", 0.0)
    s["highest_profit_pct"] = max(
        _prev_peak_early,
        profit_pct,
        max(0.0, _intra_peak_early)
    )
    if s["highest_profit_pct"] > _prev_peak_early:
        # 即時把新高點存檔，不要只在重啟校準那一刻存一次。這是每個 tick 最先跑到的峰值
        # 更新點，兩次重啟之間真正的最高點都要落地，否則中途再重啟（例如部署別的修改），
        # 峰值記憶會被打回重啟當下的價位，讓所有靠 highest_profit_pct 判斷的鎖利機制都
        # 以為從沒漲那麼高過（實測 SUIUSDT 真實高點 1.25%，因為中途重啟兩次，最後系統
        # 只記得 0.25%，鎖利鎖在遠低於真正高點的地方）。
        from core.peak_store import save_peak
        save_peak(sym, s["highest_profit_pct"])

    # ── 獲利高點量縮：不再分批，確認趨勢在高獲利區失去量能後一次全平。
    # 僅使用「上一根已收完」K 棒的成交量，避免新 K 棒剛開始時因累積量很小而誤判。
    # 目前獲利須仍保留峰值 85% 且至少 0.40%，避免已大幅回吐後才用量縮理由出場。
    _completed = _ohlcv_early[:-1] if len(_ohlcv_early) >= 2 else []
    _peak_profit = float(s.get("highest_profit_pct", 0.0) or 0.0)
    _peak_volume_contracting = False
    _completed_vol_ratio = 1.0
    if len(_completed) >= 6:
        _latest_completed_vol = float(_completed[-1][5] or 0.0)
        _baseline_volumes = [float(c[5] or 0.0) for c in _completed[-21:-1] if float(c[5] or 0.0) > 0]
        _baseline_vol = float(np.mean(_baseline_volumes)) if _baseline_volumes else 0.0
        _completed_vol_ratio = _latest_completed_vol / _baseline_vol if _baseline_vol > 0 else 1.0
        # 盤中若正在創新高（現根 K 棒的 HIGH 超越前一根的 HIGH），代表價格仍往上走，
        # 此時量縮可能只是「新 K 棒剛開始、成交量尚未累積」，不應視為動能衰竭。
        _cur_candle_high = float(_ohlcv_early[-1][2]) if _ohlcv_early else 0.0
        _prev_candle_high = float(_completed[-1][2]) if _completed else 0.0
        _intra_candle_making_new_high = (
            is_long and _cur_candle_high > _prev_candle_high
        ) or (
            not is_long and float(_ohlcv_early[-1][3]) < float(_completed[-1][3]) if _ohlcv_early and _completed else False
        )
        # 量縮只結束已經走出有效報酬的波段，不再把 0.3% 內的一般雜訊當成停利。
        # 若盤中價格仍在創新高（往上走），不觸發量縮出場，讓利潤繼續奔跑。
        _peak_volume_contracting = (
            _peak_profit >= MA_MIN_PROFIT_TARGET_PCT
            and profit_pct >= max(MA_MIN_PROFIT_TARGET_PCT * 0.85, _peak_profit * 0.85)
            and _completed_vol_ratio <= 0.70
            and not _intra_candle_making_new_high
        )

    s["peak_volume_contraction_count"] = (
        int(s.get("peak_volume_contraction_count", 0)) + 1
        if _peak_volume_contracting else 0
    )
    if s["peak_volume_contraction_count"] >= 2:
        cs = "sell" if is_long else "buy"
        logger.info(
            f"💰 [高點量縮全平] {sym} 峰值 {_peak_profit*100:.2f}%、"
            f"目前 {profit_pct*100:.2f}%、已完成K棒量比 {_completed_vol_ratio:.2f}x，"
            f"連續確認量能衰退，一次平倉 {abs(s['qty'])}"
        )
        await close_position(
            sym, cs, abs(s["qty"]), p, avg,
            reason="[Peak_Volume_Contraction]", is_stop_loss=False,
        )
        return

    # ── RSI 頂背離（底背離）出場保護 ───────────────────────────────────────────
    # 條件：已有足夠浮盈 + 從 OHLCV 收盤動態計算 RSI 序列，對比前波高點RSI
    # OHLCV 格式為 [ts, open, high, low, close, volume]（共6欄），需自行算 RSI。
    _rsi_diverge_triggered = False
    _RSI_CALC_PERIOD = 9
    if (
        _peak_profit >= RSI_DIVERGENCE_MIN_PROFIT_PCT
        and profit_pct >= RSI_DIVERGENCE_MIN_PROFIT_PCT * 0.7
        and len(_completed) >= RSI_DIVERGENCE_LOOKBACK + _RSI_CALC_PERIOD + 2
    ):
        _close_vals = [float(c[4]) for c in _completed]
        _latest_close = _close_vals[-1]

        # 計算每根已收 K 棒的 RSI（從 index RSI_PERIOD 開始才有足夠資料）
        def _rolling_rsi(closes, period=9):
            rsi_list = [None] * len(closes)
            for i in range(period, len(closes)):
                deltas = np.diff(closes[i - period: i + 1])
                gains = deltas[deltas > 0]
                losses = -deltas[deltas < 0]
                avg_g = gains.mean() if len(gains) > 0 else 1e-10
                avg_l = losses.mean() if len(losses) > 0 else 1e-10
                rs = avg_g / avg_l if avg_l > 0 else 99.0
                rsi_list[i] = min(99.0, 100.0 - (100.0 / (1.0 + rs)))
            return rsi_list

        _all_rsi = _rolling_rsi(_close_vals, _RSI_CALC_PERIOD)
        # 只取最近 RSI_DIVERGENCE_LOOKBACK + 1 個有效值
        _valid_pairs = [(c, r) for c, r in zip(_close_vals, _all_rsi) if r is not None]
        _recent_pairs = _valid_pairs[-(RSI_DIVERGENCE_LOOKBACK + 1):]

        if len(_recent_pairs) >= 4:
            _lookback_closes = [p[0] for p in _recent_pairs[:-1]]
            _lookback_rsis   = [p[1] for p in _recent_pairs[:-1]]
            _latest_rsi      = _recent_pairs[-1][1]

            if is_long:
                # 多單頂背離：現價接近前高，但現 RSI 比前高點 RSI 低 >= RSI_DIVERGENCE_MIN_DROP
                _prev_high_idx = int(np.argmax(_lookback_closes))
                _prev_high_rsi = _lookback_rsis[_prev_high_idx]
                _prev_high_close = _lookback_closes[_prev_high_idx]
                if (
                    _latest_close >= _prev_high_close * 0.9995
                    and (_prev_high_rsi - _latest_rsi) >= RSI_DIVERGENCE_MIN_DROP
                ):
                    _rsi_diverge_triggered = True
                    logger.info(
                        f"⚠️ [RSI_Divergence] {sym} 多單頂背離："
                        f"前高RSI={_prev_high_rsi:.1f} → 現RSI={_latest_rsi:.1f} "
                        f"(差={_prev_high_rsi - _latest_rsi:.1f}) | 浮盈={profit_pct*100:.2f}%"
                    )
            else:
                # 空單底背離：現價接近前低，但現 RSI 比前低點 RSI 高 >= RSI_DIVERGENCE_MIN_DROP
                _prev_low_idx = int(np.argmin(_lookback_closes))
                _prev_low_rsi = _lookback_rsis[_prev_low_idx]
                _prev_low_close = _lookback_closes[_prev_low_idx]
                if (
                    _latest_close <= _prev_low_close * 1.0005
                    and (_latest_rsi - _prev_low_rsi) >= RSI_DIVERGENCE_MIN_DROP
                ):
                    _rsi_diverge_triggered = True
                    logger.info(
                        f"⚠️ [RSI_Divergence] {sym} 空單底背離（反轉）："
                        f"前低RSI={_prev_low_rsi:.1f} → 現RSI={_latest_rsi:.1f} "
                        f"(差={_latest_rsi - _prev_low_rsi:.1f}) | 浮盈={profit_pct*100:.2f}%"
                    )

    s["rsi_divergence_count"] = (
        int(s.get("rsi_divergence_count", 0)) + 1
        if _rsi_diverge_triggered else 0
    )
    if s.get("rsi_divergence_count", 0) >= RSI_DIVERGENCE_CONFIRM:
        cs = "sell" if is_long else "buy"
        logger.info(
            f"📉 [RSI_Divergence_Exit] {sym} 連續 {RSI_DIVERGENCE_CONFIRM} 根確認背離，"
            f"峰值={_peak_profit*100:.2f}% 浮盈={profit_pct*100:.2f}%，提前落袋保護利潤"
        )
        s["rsi_divergence_count"] = 0
        await close_position(
            sym, cs, abs(s["qty"]), p, avg,
            reason="[RSI_Divergence_Exit]", is_stop_loss=False,
        )
        return

    # --- [新增] 執行動態移動停損更新 ---
    # 這會根據當前價格更新 s["trailing_stop_price"]
    update_trailing_stop(sym, p, is_long)

    ts_price = s.get("trailing_stop_price")
    # 尚未達保本門檻時，停損不可能位於獲利側；若出現代表沿用了舊倉狀態。
    _peak_for_sl = float(s.get("highest_profit_pct", 0.0) or 0.0)
    _profit_side_sl_is_valid = (
        bool(s.get("is_breakeven_locked", False))
        or bool(s.get("soft_trailing_armed", False))
        or (is_range_route and _peak_for_sl >= RANGE_TRAILING_MIN_GROSS_PCT)
    )
    _invalid_profit_side_sl = (
        _peak_for_sl < 0.003
        and not _profit_side_sl_is_valid
        and ts_price is not None and ts_price > 0
        and ((is_long and ts_price >= avg) or (not is_long and ts_price <= avg))
    )
    if _invalid_profit_side_sl:
        logger.info(f"⚠️ [Trailing_SL_Reset] {sym} 峰值僅 {_peak_for_sl*100:.3f}% 卻出現獲利側停損 {ts_price:.6f}，判定為舊倉殘值並重置")
        s["trailing_stop_price"] = 0.0
        s["stop_loss"] = 0.0
        ts_price = 0.0
    if ts_price is not None and ts_price > 0:
        trailing_hit = (is_long and p <= ts_price) or (not is_long and p >= ts_price)
        if trailing_hit:
            _soft_limit = float(s.get("soft_trailing_profit_floor", 0.0) or 0.0)
            soft_gap = s.get("soft_trailing_armed", False) and _soft_limit > 0 and (
                (is_long and p < _soft_limit) or (not is_long and p > _soft_limit)
            )
            if soft_gap:
                logger.info(
                    f"⚠️ [Soft_Trailing_Gap] {sym} 現價 {p:.6f} 已跳過淨利保護線 "
                    f"{_soft_limit:.6f}"
                )

            if is_range_route:
                # Range 路線的移動停利穿越確認已經在 check_exits 前段用
                # _range_trailing_cross_confirmed()（連續多筆/秒數）處理過；
                # 這裡不用重複判斷，也不能直接落地用 [Dynamic_Trailing] 立即
                # 出場，否則會繞過還沒確認完成的計數、變回單筆雜訊就出場。
                return

            cs = "sell" if is_long else "buy"
            comparator = "<=" if is_long else ">="
            logger.info(
                f"🚨 [Trailing_SL_Trigger] {sym} 觸發移動停損/保本平倉："
                f"當前價 {p:.6f} {comparator} 停損價 {ts_price:.6f}"
            )
            await close_position(
                sym, cs, abs(s["qty"]), p, avg,
                reason="[Dynamic_Trailing]", is_stop_loss=(profit_pct <= 0),
            )
            return

    # 小幅峰值回吐到負報酬時，不另設超窄 Peak_Giveback 停損；
    # 真正失效交由下方 Rapid_Adverse_Move、ATR 與 Hard_Stop 管理。

    _entry_atr = s.get("entry_atr", s.get("current_atr", avg * 0.003))
    # Specifically handle BCH and XLM with higher ATR multipliers to account for their higher volatility
    base_sl_mult = s.get("sl_atr_multiplier", SL_ATR_MULTIPLIER)
    if sym in ["BCH", "XLM"]:
        base_sl_mult *= 1.2  # Increase by 20% for high-volatility assets
    _sl_mult   = get_effective_exit_setting(sym, "sl_atr_multiplier", base_sl_mult, is_long)
    _rr_thresh = min(get_effective_exit_setting(sym, "rr_threshold", 1.3, is_long), 2.0)
    _hard_sl   = get_effective_exit_setting(sym, "hard_stop_loss_pct", s.get("hard_stop_loss_pct", HARD_STOP_LOSS_PCT), is_long)
    _atr_sl_pct = (_sl_mult * _entry_atr / avg) if avg > 0 else 0.006
    expected_loss_pct = max(_hard_sl, _atr_sl_pct, 0.005)
    min_tp_pct = expected_loss_pct * _rr_thresh

    # ── 停滯超時 (Stagnation Timeout) ──
    # 使用者要求：虧損/持平的單子如果盤整很久沒動靜，不要無限期等下去；但如果動能仍在
    # 往有利方向擴張（代表趨勢還在發展中，只是還沒轉正），就繼續給機會，不要單純因為
    # 「時間到」就把一個可能正在醞釀的單子砍掉——只砍真正停滯、方向不明的單子。
    # 必須放在 Rescue DCA 判斷之前：虧損不夠攤平價差門檻的停滯倉位，DCA 那段每個 tick
    # 都會嘗試攤平又被自己的價差保護擋下（RescueDCAIneffective），但那個分支呼叫完
    # execute_order 後一律 return，導致這裡永遠排不到、卡在無效重試裡出不來。
    _st_entry_layers = len(s.get("entries", []))
    # 優先使用配置文件中的 stagnation_base_limit，若無則依據進入層數與動能強度計算預設值
    custom_limit = COIN_PROFILE_CONFIG.get(sym, {}).get("stagnation_base_limit")
    if custom_limit:
        _st_base_limit = custom_limit
    else:
        _st_base_limit = 3600 if _st_entry_layers <= 1 else 5400
    # 使用者反映 LTCUSDT/LINKUSDT 兩筆都曾經有過 +0.3% 左右的峰值，中間一直在小賺小賠
    # 之間原地震盪，停滯超時觸發那一刻剛好卡在小賠，整段持倉的峰值就這樣浪費掉。
    # 兩個調整：(1) 基礎等待時間全面拉長 1.5 倍，給單子更多時間發展；(2) 曾經有過
    # 像樣峰值（>0.2%，扣掉來回手續費後還有剩）的單子，再多給 1.5 倍時間，讓它有
    # 更多機會回到峰值附近再了結，而不是一到時間到、剛好卡在小賠的當下就被迫出場。
    # 市價單無法指定成交在「峰值附近的價位」（成交價由當下真實市場決定），所以用
    # 「多給時間、提高回到峰值附近的機率」取代直接指定出場價，避免走上今天稍早
    # 已經證實會出問題的路線（HBARUSDT 案例：等待追價反而讓虧損擴大）。
    _st_had_peak = s.get("highest_profit_pct", 0.0) > 0.002
    # [2026-07-14 修正] 外層倍數從 2.0 縮回 1.2：實測 7/14 凌晨大量空單（DOGE/HYPE/
    # LINK/AVAX）因等待 80 分鐘才觸發 Stagnation_Timeout，虧損合計超過 -3.5 USDT。
    # 縮短後：無峰值弱動能 48 分鐘（原 80 分鐘）；有峰值強動能 243 分鐘（原 405 分鐘）。
    # 保留「有峰值再多給 1.5 倍」的邏輯，只砍「從未回到有利側的卡住倉位」。
    _st_time_decay_limit = int(_st_base_limit * 1.2 * (1.5 if _st_had_peak else 1.0))
    _ma7_turn_managed = route == "ma7_simple"
    # 使用者要求擴大範圍：不只虧損/持平的單子要超時了結，「有獲利但一直沒有再創新高、
    # 時間拖很久」的單子也一樣——與其耗著等一個已經不再發展的小獲利，不如先落袋，把
    # 倉位空出來讓新訊號進場。虧損那邊維持停損標記；獲利那邊改標記一般平倉，不算停損。
    if hold_sec > _st_time_decay_limit and not _ma7_turn_managed:
        
        # 檢查是否在「獲利區間」且「沒創新高」
        # 這裡加入針對獲利單的 Peak Stagnation 檢查：
        # 如果獲利 > HIGH_POINT_STAGNATION_MIN_PROFIT 且 已經持倉超過動態計算的等待時間，且 價格在該時間內沒有創新高，
        # 且 目前動能未往有利方向擴張，則執行「獲利了結」。
        is_profitable = profit_pct > HIGH_POINT_STAGNATION_MIN_PROFIT
        if is_profitable:
            # 獲利越高，要求的「沒創新高」時間越短，以便更快落袋為安
            # 將動態滯留時間上調，給予更多空間。
            # 從 HIGH_POINT_STAGNATION_TIME (300s) 到最低 120s 之間線性縮減，但基礎值更高。
            dynamic_stagnation_time = max(120, int(HIGH_POINT_STAGNATION_TIME * 1.5 * (1 - (profit_pct - HIGH_POINT_STAGNATION_MIN_PROFIT) / 0.05)))
            peak_profit = s.get("highest_profit_pct", 0.0)
            allowed_giveback = max(0.0010, peak_profit * 0.25)
            is_near_peak = profit_pct >= max(0.0, peak_profit - allowed_giveback)
            
            # ── [新增] 盤整行情處理 ──
            # 即使價格一直在變動（不到 300 秒就變動），但若處於盤整區間且價格在峰值附近，則判定為停滯
            recent_candles = s.get("ohlcv", [])
            is_ranging = False
            if len(recent_candles) >= 20:
                highs = np.array([x[2] for x in recent_candles])
                lows = np.array([x[3] for x in recent_candles])
                recent_high = float(np.max(highs))
                recent_low = float(np.min(lows))
                range_width_pct = (recent_high - recent_low) / recent_low if recent_low > 0 else 0
                atr_val_curr = _get_atr(s, p)
                atr_pct = atr_val_curr / p if p > 0 else 0
                is_ranging = range_width_pct < 0.025 and atr_pct < 0.015
            
            if is_ranging and is_near_peak:
                is_stagnant_peak = True
            else:
                is_stagnant_peak = hold_sec > dynamic_stagnation_time and is_near_peak
        else:
            is_stagnant_peak = False
        
        if True:
            # 情況 A：虧損或持平的單子，動能沒轉好 -> 停滯超時強制平倉
            if profit_pct <= 0:
                cs = 'sell' if is_long else 'buy'
                logger.info(f"⏳ [停滯超時] {sym} 持倉超過 {_st_time_decay_limit/60:.0f} 分鐘仍處虧損/持平 ({profit_pct*100:.2f}%) 且動能未往有利方向擴張，平倉了結不再等待")
                await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[Stagnation_Timeout]", is_stop_loss=True)
                return
            
            # 情況 B：獲利中的單子，動能沒轉好
            # 若滿足「獲利且停滯」的條件，則落袋為安
            if is_profitable and is_stagnant_peak:
                cs = 'sell' if is_long else 'buy'
                logger.info(f"⏳ [停滯超時-獲利了結] {sym} 持倉超過 {_st_time_decay_limit/60:.0f} 分鐘獲利 {profit_pct*100:.2f}% 未再創新高且動能未擴張，先落袋讓新倉進場")
                await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[Stagnation_Timeout]", is_stop_loss=False)
                return
            
            # 情況 C：獲利中的單子，動能沒轉好，但還在發展中（還沒達到停滯峰值）
            # 則不執行任何操作，讓它繼續跑
            logger.info(f"ℹ️ [停滯過濾] {sym} 獲利 {profit_pct*100:.2f}% 中，動能未擴張但仍處於發展期，繼續持倉")

    # 舊布林突破反手路線已刪除。

    atr_val = _get_atr(s, p)
    profit_atr_mult = (p - avg) / atr_val if is_long else (avg - p) / atr_val

    # ── 無峰值早期停損 (NoPeak_EarlyStop) ──
    # [2026-07-14 新增] 針對 7/14 凌晨 DOGE/HYPE/LINK/AVAX 等空單模式：
    # 開倉後從未到達保本門檻（最高獲利 < 0.3%），持倉超過 30 分鐘仍在虧損中且
    # 虧損幅度超過 0.8%，直接提前停損，不等 Stagnation_Timeout 的 48 分鐘。
    # 條件：(1)未保本鎖定 (2)無有效峰值 (3)持倉>=30分鐘 (4)虧損>0.8%
    # 0.8% 選取依據：覆蓋雙邊手續費(~0.08%)後仍有明顯淨虧，且比 hard_sl_pct(1.5%) 更緊。
    _NO_PEAK_TIMEOUT_SEC = 1800   # 30 分鐘
    _NO_PEAK_HARD_SL_PCT = 0.008  # 0.8%
    if (
        not _ma7_turn_managed
        and
        not s.get("is_breakeven_locked", False)
        and float(s.get("highest_profit_pct", 0.0) or 0.0) < 0.003
        and hold_sec >= _NO_PEAK_TIMEOUT_SEC
        and profit_pct < -_NO_PEAK_HARD_SL_PCT
        and not s.get("is_ordering")
    ):
        cs = 'sell' if is_long else 'buy'
        logger.info(
            f"🛑 [NoPeak_EarlyStop] {sym} 進場 {hold_sec/60:.0f} 分鐘從未達保本門檻，"
            f"虧損 {profit_pct*100:.2f}% > 0.8%，提前停損（不等停滯超時）"
        )
        await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[NoPeak_EarlyStop]", is_stop_loss=True)
        return

    # ── 停滯攤平 (Stagnation Rescue) ──
    # 持倉超過 60 分鐘、還沒攤平過、目前仍在虧損（不管有沒有接近停損線），就評估
    # 攤平一次，讓均價貼近市價，早點有機會平倉、不要一直佔著交易槽位。跟接刀防呆
    # 共用同一套判斷（_attempt_forced_rescue 內建），急跌/急漲中不會硬攤。
    if (
        not _ma7_turn_managed
        and hold_sec >= 3600
        and profit_pct < 0
        and s.get("entry_count", 0) == 1
        and not s.get("is_ordering")
    ):
        if await _attempt_forced_rescue(sym, s, is_long, p):
            return

    # 未個別配置 hard_sl_pct 的幣種（例如 MUSDT）過去 fallback 是 0.0，等於整段 Hard_SL
    # 直接被跳過，完全沒有固定百分比的硬停損防線，只能靠 ATR 動態停損（Universal SL）——
    # 但 ATR 停損距離沒有上限，暴漲暴跌時 get_dynamic_atr_multiplier 還會把倍數放寬到 1.2x，
    # 兩者疊加曾讓單筆虧損跑到 -14%（MUSDT 實際案例）。改用全域 HARD_STOP_LOSS_PCT 當預設值，
    # 讓每個幣種至少都有一道固定百分比的最後防線。
    _hard_sl = COIN_PROFILE_CONFIG.get(sym, {}).get("hard_sl_pct", HARD_STOP_LOSS_PCT)
    if _hard_sl > 0:
        # 提早在門檻 75% 處就評估要不要攤平，而不是等真正跌破停損線才評估——
        # 這樣攤平才有機會買在明顯優於原始停損線的價位，真正達到降低風險的效果，
        # 而不是在已經要停損的當下才硬加碼，反而放大虧損（曾實際發生：攤平價幾乎
        # 貼著原始停損線，加碼後部位變大、停損線卻被新均價往下拖，最終虧損翻倍）。
        _rescue_eval_pct = _hard_sl * 0.75
        if (
            not _ma7_turn_managed
            and s.get("entry_count", 0) == 1
            and not s.get("is_ordering")
            and profit_pct <= -_rescue_eval_pct
        ):
            if await _attempt_forced_rescue(sym, s, is_long, p):
                return

        # 攤平後的停損線不能比「原始進場價的停損線」更寬鬆：取新均價停損線跟原始
        # 進場價停損線中「較緊」的那一個，避免攤平失敗時虧損被無限放大。
        first_ep = float(s.get("first_entry_price", 0.0) or avg)
        if is_long:
            _hard_sl_price = max(avg * (1 - _hard_sl), first_ep * (1 - _hard_sl))
            _hard_sl_hit = p <= _hard_sl_price
        else:
            _hard_sl_price = min(avg * (1 + _hard_sl), first_ep * (1 + _hard_sl))
            _hard_sl_hit = p >= _hard_sl_price

        if _hard_sl_hit:
            cs = 'sell' if is_long else 'buy'
            logger.info(f"🛑 [Hard_Stop_Loss] {sym} 觸發硬停損線 {(_hard_sl_price if is_long else _hard_sl_price):.4f}，執行平倉")
            await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[Hard_Stop_Loss]", is_stop_loss=True)
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
