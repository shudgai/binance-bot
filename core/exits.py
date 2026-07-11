import logging
import asyncio
import math
import time
import numpy as np

from core import ctx
from core.config import (PAPER_TRADING, HARD_STOP_LOSS_PCT, MIN_PROFIT_LOCK_THRESHOLD,
    PROTECTED_PROFIT_FLOOR, MOMENTUM_EXIT_ATR_THRESHOLD, MOMENTUM_EXIT_MIN_PROFIT_PCT,
    TREND_PERSISTENCE_WINDOW, PRICE_MOVEMENT_THRESHOLD,
    COIN_PROFILE_CONFIG, DEFAULT_REVERSAL_SETTINGS, SYMBOL_REVERSAL_SETTINGS,
    SL_ATR_MULTIPLIER, TP_ATR_MULTIPLIER,
    HIGH_POINT_STAGNATION_MIN_PROFIT, HIGH_POINT_STAGNATION_TIME, ROUND_TRIP_FEE_PCT)
from core.indicators import _get_atr, _macd_vals, calculate_ema, calculate_macd
from core.symbol_profile import get_effective_exit_setting, has_strong_momentum, get_dynamic_atr_multiplier
from core.calc import profit_pct as _profit_pct

logger = logging.getLogger(__name__)


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
            # 計算初始耐心時間
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
        elapsed_time = time.time() - self.wait_start_time
        time_since_high = time.time() - self.last_high_time
        
        # 1. 動態回撤比例 (Dynamic Retracement) - 快速反應防線
        # 容忍正常 1m 雜訊：至少 0.12%，並隨峰值放寬至峰值的 25%（上限 0.5%）。
        tolerance_pct = max(0.0012, min((self.max_profit_pct / 100.0) * 0.25, 0.0050))
        
        is_retracing = False
        if self.is_long and current_price < (self.current_max_price * (1 - tolerance_pct)):
            is_retracing = True
        elif not self.is_long and current_price > (self.current_max_price * (1 + tolerance_pct)):
            is_retracing = True

        if is_retracing:
            print(f"💰 [觸發：回撤比例] 價格從最高點回落超過 {tolerance_pct*100:.3f}%，快速落袋為安。")
            return "SELL"

        # 2. 動態耐心極限 (Time-out) - 強制入帳防線
        if elapsed_time >= self.wait_time_limit:
            print(f"💰 [觸發：耐心極限] 已等待 {elapsed_time:.1f}秒 (限時 {self.wait_time_limit:.1f}秒)，強制落袋為安。")
            return "SELL"

        # 3. 盤整最高點 (Stagnation) - 動能耗盡防線
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


def update_trailing_stop(sym, current_price, is_long):
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
    # Increase trailing distance slightly to give more "room to breathe"
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
    s["highest_profit_pct"] = max(_prev_peak, profit_pct)
    if s["highest_profit_pct"] > _prev_peak:
        # 即時把新高點存檔，而不是只在重啟時呼叫
        # save_peak，兩次重啟之間爬到的真正高點從未落地，若中途又重啟（例如部署修改），
        # 峰值記憶會被打回重啟當下的價位，讓所有靠 highest_profit_pct 判斷的鎖利機制
        # 都以為從沒漲那麼高過（實測 SUIUSDT 真實高點 1.25%，因為中途重啟兩次，
        # 最後只記得 0.25%，鎖利鎖在遠低於真正高點的地方）。
        from core.peak_store import save_peak
        save_peak(sym, s["highest_profit_pct"])

    # --- [Updated] Break-Even Mechanism ---
    # 保本線必須涵蓋雙邊 taker fee，另加 0.02% 滑價緩衝；否則名義保本仍會淨虧。
    # 0.6% 以下仍屬發展區，不用 Dynamic Trailing 提前停潤；達 0.6% 才啟動保本。
    fee_safe_profit = ROUND_TRIP_FEE_PCT + 0.0015
    breakeven_threshold = 0.0060
    if profit_pct > breakeven_threshold:
        # Ensure the stop-loss is at least at the entry price (+ 0.01% buffer)
        # For long: new_sl >= entry; For short: new_sl <= entry
        if is_long:
            new_be_sl = avg_price * (1.0 + fee_safe_profit)
            s["trailing_stop_price"] = max(s.get("trailing_stop_price", 0.0), new_be_sl)
            s["stop_loss"] = s["trailing_stop_price"]
            s["is_breakeven_locked"] = True
        else:
            new_be_sl = avg_price * (1.0 - fee_safe_profit)
            # trailing_stop_price 初始值是 0.0（不是缺項），對空單而言 0.0 代表「尚未設定」
            # 而不是「停損價=0」。直接 min(0.0, new_be_sl) 恆等於 0.0，保本鎖永遠鎖不上，
            # 回檔時只能退到後面 ATR 動態停損那組更寬鬆的距離，等於整段保本機制形同虛設
            # 0.6% 以上才屬於需要保本鎖利的區間。
            _cur_ts_short = s.get("trailing_stop_price", 0.0)
            s["trailing_stop_price"] = min(_cur_ts_short if _cur_ts_short > 0 else float('inf'), new_be_sl)
            s["stop_loss"] = s["trailing_stop_price"]
            s["is_breakeven_locked"] = True
        logger.info(f"🛡️ [Break-Even] {sym} profit {profit_pct*100:.2f}% > {breakeven_threshold*100}%, SL moved to entry")

    profit_atr_multiple = (current_price - avg_price) / atr_val if is_long else (avg_price - current_price) / atr_val
    profile_type = str(s.get("profile_type", ""))
    min_trailing_profit = 0.010 if ("High_Beta" in profile_type or "Speculative" in profile_type) else 0.006

    if is_long:
        if current_price > s.get("trailing_highest", 0.0):
            s["trailing_highest"] = current_price

        trail_sl = s["trailing_stop_price"]

        # 峰值 0.3%-0.6% 使用軟移動停利：允許回吐 0.2%，並覆蓋交易摩擦。
        _hp_soft = s["highest_profit_pct"]
        if 0.003 <= _hp_soft < breakeven_threshold:
            _soft_floor = avg_price * (1.0 + ROUND_TRIP_FEE_PCT + 0.0003)
            _soft_sl = max(s["trailing_highest"] * (1.0 - 0.002), _soft_floor)
            trail_sl = max(trail_sl, _soft_sl)

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

        _hp_soft = s["highest_profit_pct"]
        if 0.003 <= _hp_soft < breakeven_threshold:
            _soft_ceiling = avg_price * (1.0 - ROUND_TRIP_FEE_PCT - 0.0003)
            _soft_sl = min(s["trailing_lowest"] * (1.0 + 0.002), _soft_ceiling)
            trail_sl = min(trail_sl, _soft_sl)

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


def detect_market_regime(sym, current_price, avg_price, is_long):
    s = ctx.STATES[sym]
    if len(s["ohlcv"]) < 20 or avg_price <= 0:
        return "HOLD", "資料不足"

    recent_candles = s["ohlcv"][-20:]
    highs = np.array([x[2] for x in recent_candles])
    lows = np.array([x[3] for x in recent_candles])
    closes = np.array([x[4] for x in recent_candles])
    recent_high = float(np.max(highs))
    recent_low = float(np.min(lows))
    range_width_pct = (recent_high - recent_low) / recent_low if recent_low > 0 else 0

    atr_val = _get_atr(s, current_price)
    atr_pct = atr_val / current_price if current_price > 0 else 0

    reversal_settings = DEFAULT_REVERSAL_SETTINGS.copy()
    reversal_settings.update(SYMBOL_REVERSAL_SETTINGS.get(sym, {}))
    trade_signal = s.get("trade_signal_strength", 0.0)
    reversal_threshold = reversal_settings["trade_signal_threshold"]
    prev_close = s.get("prev_close")
    if trade_signal >= reversal_threshold and prev_close:
        price_move_pct = (current_price - prev_close) / max(prev_close, 1e-8)
        if (is_long and price_move_pct < -max(reversal_settings["min_reverse_pct"], atr_pct * 1.2)) or \
           (not is_long and price_move_pct > max(reversal_settings["min_reverse_pct"], atr_pct * 1.2)):
            return "BREAKOUT_REVERSAL", f"即時大額成交異常 {s['trade_signal_reason']}"

    volume_surge = s["current_vol"] > s["vol_ma20"] * reversal_settings["volume_multiplier"]
    if prev_close:
        price_jump = (prev_close - current_price) / max(prev_close, 1e-8) > max(reversal_settings["price_jump_pct"], atr_pct * 1.2) if is_long else \
                     (current_price - prev_close) / max(prev_close, 1e-8) > max(reversal_settings["price_jump_pct"], atr_pct * 1.2)
    else:
        price_jump = False
    if volume_surge and price_jump:
        return "BREAKOUT_REVERSAL", "放量突發且價格急速變動"

    is_ranging = range_width_pct < 0.025 and atr_pct < 0.015
    if is_ranging:
        profit_pct = _profit_pct(current_price, avg_price, is_long)
        if profit_pct >= 0.010:
            return "RANGE_PROFIT_TAKE", f"盤整區間內已獲利 {profit_pct * 100:.2f}%"

    return "HOLD", "未達出場條件"


def check_trend_persistence(sym):
    s = ctx.STATES[sym]
    if not s.get("ohlcv") or len(s["ohlcv"]) < 2:
        return True
    return True


async def check_exits(sym):
    from core.orders import close_position, execute_order
    s = ctx.STATES[sym]
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
    current_atr = s.get("current_atr", 0.0)

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
    if hold_sec < 60 and s.get("open_time", 0) > 0:
        guard = FastReversalGuard()
        trap_signal = guard.check_instant_trap(avg, p, 'buy' if is_long else 'sell')
        if trap_signal == "EXIT_INSTANT_TRAP":
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
        if profit_pct < 0 and _adverse_atr_mult >= 2.0:
            cs = 'sell' if is_long else 'buy'
            logger.info(f"⚡ [急速逆勢] {sym} 距上次進場僅 {_time_since_entry:.0f} 秒，價格已逆勢達 {_adverse_atr_mult:.2f}x ATR，提早出場評估反手")
            await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[Rapid_Reversal]", is_stop_loss=True)
            if _check_reversal_allowed(sym, s):
                last_reverse = s.get("last_reverse_time", 0)
                if time.time() - last_reverse > 1800:
                    rev_side = "buy" if not is_long else "sell"
                    s["pending_reverse"] = rev_side
                    s["pending_reverse_time"] = time.time()
                    s["last_reverse_time"] = time.time()
                    # 沿用攤平失敗後的放寬動能確認標準：急速逆勢代表方向已經被價格
                    # 明確打臉，MACD 這種落後指標可能還來不及完整反映，不用等它擴張。
                    s["pending_reverse_after_rescue"] = True
                    logger.info(f"🔄 [Rapid_Reverse] {sym} 急速逆勢出場後設置反手 → {rev_side}")
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

    # ── 分批停利：先落袋一半，剩餘部位繼續交給 trailing 捕捉趨勢。
    # 高彈性幣要求 1.0%，其餘要求 max(0.6%, 3 ATR)，避免小利潤被手續費吃掉。
    if not s.get("has_partial_closed", False) and not s.get("partial_tp_pending", False):
        profile_type = str(s.get("profile_type", ""))
        partial_floor = 0.010 if ("High_Beta" in profile_type or "Speculative" in profile_type) else 0.006
        partial_trigger = max(partial_floor, (current_atr / avg) * 3.0 if avg > 0 else partial_floor)
        if profit_pct >= partial_trigger:
            before_qty = abs(s["qty"])
            s["partial_tp_pending"] = True
            try:
                cs = "sell" if is_long else "buy"
                await close_position(sym, cs, before_qty * 0.5, p, avg, reason="[Partial_Take_Profit]", is_stop_loss=False)
                if abs(s.get("qty", 0.0)) < before_qty - 1e-8:
                    s["has_partial_closed"] = True
                    from core.peak_store import save_partial_take_profit
                    save_partial_take_profit(sym)
                    logger.info(f"💰 [分批停利] {sym} 已落袋 50%，剩餘部位繼續追蹤趨勢")
                    return
            finally:
                s["partial_tp_pending"] = False

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

    # --- [新增] 執行動態移動停損更新 ---
    # 這會根據當前價格更新 s["trailing_stop_price"]
    update_trailing_stop(sym, p, is_long)

    ts_price = s.get("trailing_stop_price")
    # 尚未達保本門檻時，停損不可能位於獲利側；若出現代表沿用了舊倉狀態。
    _peak_for_sl = float(s.get("highest_profit_pct", 0.0) or 0.0)
    _invalid_profit_side_sl = (
        _peak_for_sl < 0.003
        and ts_price is not None and ts_price > 0
        and ((is_long and ts_price >= avg) or (not is_long and ts_price <= avg))
    )
    if _invalid_profit_side_sl:
        logger.info(f"⚠️ [Trailing_SL_Reset] {sym} 峰值僅 {_peak_for_sl*100:.3f}% 卻出現獲利側停損 {ts_price:.6f}，判定為舊倉殘值並重置")
        s["trailing_stop_price"] = 0.0
        s["stop_loss"] = 0.0
        ts_price = 0.0
    if ts_price is not None and ts_price > 0:
        if is_long:
            if p <= ts_price:
                logger.info(f"🚨 [Trailing_SL_Trigger] {sym} 觸發移動停損/保本平倉：當前價 {p:.6f} <= 停損價 {ts_price:.6f}")
                await close_position(sym, 'sell', abs(s["qty"]), p, avg, reason="[Dynamic_Trailing]", is_stop_loss=True)
                return
        else:
            if p >= ts_price:
                logger.info(f"🚨 [Trailing_SL_Trigger] {sym} 觸發移動停損/保本平倉：當前價 {p:.6f} >= 停損價 {ts_price:.6f}")
                await close_position(sym, 'buy', abs(s["qty"]), p, avg, reason="[Dynamic_Trailing]", is_stop_loss=True)
                return

    _entry_atr = s.get("entry_atr", s.get("current_atr", avg * 0.003))
    # Specifically handle BCH and XLM with higher ATR multipliers to account for their higher volatility
    base_sl_mult = s.get("sl_atr_multiplier", SL_ATR_MULTIPLIER)
    if sym in ["BCH", "XLM"]:
        base_sl_mult *= 1.2  # Increase by 20% for high-volatility assets
    _sl_mult   = get_effective_exit_setting(sym, "sl_atr_multiplier", base_sl_mult, is_long)
    _rr_thresh = get_effective_exit_setting(sym, "rr_threshold", 1.3, is_long)
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
    _st_macd_hist_now = s.get("macd_hist", 0.0)
    _st_is_strong = (
        (is_long and s.get("current_rsi", 50.0) > 55 and _st_macd_hist_now > 0) or
        (not is_long and s.get("current_rsi", 50.0) < 45 and _st_macd_hist_now < 0)
    )
    _st_entry_layers = len(s.get("entries", []))
    # 優先使用配置文件中的 stagnation_base_limit，若無則依據進入層數與動能強度計算預設值
    custom_limit = COIN_PROFILE_CONFIG.get(sym, {}).get("stagnation_base_limit")
    if custom_limit:
        _st_base_limit = custom_limit
    else:
        _st_base_limit = (5400 if _st_entry_layers <= 1 else 7200) if _st_is_strong else (2400 if _st_entry_layers <= 1 else 5400)
    # 使用者反映 LTCUSDT/LINKUSDT 兩筆都曾經有過 +0.3% 左右的峰值，中間一直在小賺小賠
    # 之間原地震盪，停滯超時觸發那一刻剛好卡在小賠，整段持倉的峰值就這樣浪費掉。
    # 兩個調整：(1) 基礎等待時間全面拉長 1.5 倍，給單子更多時間發展；(2) 曾經有過
    # 像樣峰值（>0.2%，扣掉來回手續費後還有剩）的單子，再多給 1.5 倍時間，讓它有
    # 更多機會回到峰值附近再了結，而不是一到時間到、剛好卡在小賠的當下就被迫出場。
    # 市價單無法指定成交在「峰值附近的價位」（成交價由當下真實市場決定），所以用
    # 「多給時間、提高回到峰值附近的機率」取代直接指定出場價，避免走上今天稍早
    # 已經證實會出問題的路線（HBARUSDT 案例：等待追價反而讓虧損擴大）。
    _st_had_peak = s.get("highest_profit_pct", 0.0) > 0.002
    # 使用者要求進一步拉長等待時間、少用攤平化解虧損單，改用「給更多時間」取代「加碼
    # 攤平成本」——外層倍數從 1.5 拉高到 2.0，讓所有停滯超時判斷等比例延後生效
    # （原本弱動能無峰值 60 分鐘變 80 分鐘，強動能有峰值 202 分鐘變 270 分鐘）。
    _st_time_decay_limit = int(_st_base_limit * 2.0 * (1.5 if _st_had_peak else 1.0))
    # 使用者要求擴大範圍：不只虧損/持平的單子要超時了結，「有獲利但一直沒有再創新高、
    # 時間拖很久」的單子也一樣——與其耗著等一個已經不再發展的小獲利，不如先落袋，把
    # 倉位空出來讓新訊號進場。虧損那邊維持停損標記；獲利那邊改標記一般平倉，不算停損。
    if hold_sec > _st_time_decay_limit:
        _sd_macd_h, _sd_prev_macd_h = _macd_vals(s)
        _sd_trending_favorably = (_sd_macd_h > _sd_prev_macd_h) if is_long else (_sd_macd_h < _sd_prev_macd_h)
        
        # 檢查是否在「獲利區間」且「沒創新高」
        # 這裡加入針對獲利單的 Peak Stagnation 檢查：
        # 如果獲利 > HIGH_POINT_STAGNATION_MIN_PROFIT 且 已經持倉超過動態計算的等待時間，且 價格在該時間內沒有創新高，
        # 且 目前動能未往有利方向擴張，則執行「獲利了結」。
        is_profitable = profit_pct > HIGH_POINT_STAGNATION_MIN_PROFIT
        if is_profitable:
            # 獲利越高，要求的「沒創新高」時間越短，以便更快落袋為安
            # 從 HIGH_POINT_STAGNATION_TIME (300s) 到最低 120s 之間線性縮減 (以 5% 超過門檻為基準)
            dynamic_stagnation_time = max(120, int(HIGH_POINT_STAGNATION_TIME * (1 - (profit_pct - HIGH_POINT_STAGNATION_MIN_PROFIT) / 0.05)))
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
        
        if not _sd_trending_favorably:
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

    bb_upper = s.get('bb_up', 0)
    bb_lower = s.get('bb_low', 0)
    vol_ma20 = s.get('vol_ma20', 0)
    current_vol = s.get('current_vol', 0)

    if not s.get("debug_start_time"):
        s["debug_start_time"] = time.time()

    if time.time() - s["debug_start_time"] < 600:
        if time.time() - s.get('last_debug_pressure_time', 0) > 60:
            logger.info(f" [DEBUG_PRESSURE] {sym}: Upper={bb_upper:.4f}, Lower={bb_lower:.4f}, Vol_MA={vol_ma20:.2f}")
            s['last_debug_pressure_time'] = time.time()

    is_breakout_up = (not is_long and bb_upper > 0 and p > bb_upper and current_vol > (vol_ma20 * 1.5))
    is_breakout_down = (is_long and bb_lower > 0 and p < bb_lower and current_vol > (vol_ma20 * 1.5))

    if is_breakout_up or is_breakout_down:
        last_reverse = s.get('last_reverse_time', 0)
        hold_sec = time.time() - s.get("open_time", time.time())
        if (time.time() - last_reverse > 1800 and hold_sec > 300
                and not s.get("pending_reverse_trigger")):
            new_direction = "buy" if is_breakout_up else "sell"
            s["pending_reverse_trigger"] = {
                "side": new_direction,
                "time": s["ohlcv"][-1][0] if s["ohlcv"] else 0,
                "strength": 18.0,
                "source": "BB_Breakout",
            }
            logger.info(f"⚠️ [REVERSE_PENDING] {sym} BB 突破偵測 → 等待下一根 K 收盤確認再反手 ({new_direction})")

    atr_val = _get_atr(s, p)
    profit_atr_mult = (p - avg) / atr_val if is_long else (avg - p) / atr_val

    if profit_atr_mult > MOMENTUM_EXIT_ATR_THRESHOLD and profit_pct >= MOMENTUM_EXIT_MIN_PROFIT_PCT:
        macd_hist = s.get("macd_hist", 0.0)
        prev_macd_hist = s.get("prev_macd_hist", 0.0)
        rsi = s.get("current_rsi", 50.0)
        prev_rsi = s.get("prev_rsi", rsi)

        momentum_failing = False
        if is_long:
            if macd_hist < prev_macd_hist or rsi <= prev_rsi:
                momentum_failing = True
        else:
            if macd_hist > prev_macd_hist or rsi >= prev_rsi:
                momentum_failing = True

        if momentum_failing:
            logger.info(f"✅ [Momentum_Exit] {sym} 獲利達標 ({MOMENTUM_EXIT_ATR_THRESHOLD:.1f} ATR) 且動能衰竭，早期獲利平倉！")
            cs = "sell" if is_long else "buy"
            await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[Momentum_Exit]")
            return

    # ── 停滯攤平 (Stagnation Rescue) ──
    # 持倉超過 60 分鐘、還沒攤平過、目前仍在虧損（不管有沒有接近停損線），就評估
    # 攤平一次，讓均價貼近市價，早點有機會平倉、不要一直佔著交易槽位。跟接刀防呆
    # 共用同一套判斷（_attempt_forced_rescue 內建），急跌/急漲中不會硬攤。
    if hold_sec >= 3600 and profit_pct < 0 and s.get("entry_count", 0) == 1 and not s.get("is_ordering"):
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
        if s.get("entry_count", 0) == 1 and not s.get("is_ordering") and profit_pct <= -_rescue_eval_pct:
            if await _attempt_forced_rescue(sym, s, is_long, p):
                return

        # 攤平後的停損線不能比「原始進場價的停損線」更寬鬆：取新均價停損線跟原始
        # 進場價停損線中「較緊」的那一個，避免攤平失敗時虧損被無限放大。
        first_ep = s.get("first_entry_price", avg)
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