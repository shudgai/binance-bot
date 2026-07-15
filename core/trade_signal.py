import logging
import time
import numpy as np
from core import ctx
from core.config import ROUND_TRIP_FEE_PCT
from core.peak_store import save_peak

logger = logging.getLogger(__name__)


async def update_trade_signal(sym, trade):
    s = ctx.STATES[sym]
    price = float(trade.get("price", 0) or 0)
    amount = float(trade.get("amount", 0) or 0)
    if price <= 0 or amount <= 0:
        return

    ts = trade.get("timestamp", time.time() * 1000)
    if isinstance(ts, (int, float)):
        ts_value = float(ts) / 1000.0
    else:
        ts_value = time.time()

    # watch_trades 在重連時可能補送舊成交；行情事件必須單調，且不能早於本筆進場。
    last_market_ts = float(s.get("last_market_trade_time", 0.0) or 0.0)
    open_time = float(s.get("open_time", 0.0) or 0.0)
    if (last_market_ts and ts_value < last_market_ts) or (
        abs(s.get("qty", 0.0)) > 0.000001 and open_time and ts_value < open_time
    ):
        logger.info(
            f"⚠️ [Stale_Trade_Tick] {sym} 忽略亂序/進場前成交 "
            f"event={ts_value:.3f} last={last_market_ts:.3f} open={open_time:.3f}"
        )
        return
    s["last_market_trade_time"] = ts_value

    s["last_trade_price"] = price
    s["close_price"] = price
    s["last_ohlcv_update"] = time.time()
    
    # 同步修改當前開著的 K 線 (ohlcv[-1])，確保即時指標計算與止損判斷最精準
    if "ohlcv" in s and s["ohlcv"]:
        _lc = s["ohlcv"][-1]
        if len(_lc) >= 5:
            _lc[4] = price
            _lc[2] = max(_lc[2], price)
            _lc[3] = min(_lc[3], price)
            
    s["last_trade_qty"] = amount
    s["last_trade_side"] = str(trade.get("side", "buy") or "buy")
    s["last_trade_time"] = ts_value
    s["trade_price_history"].append(price)
    s["trade_qty_history"].append(amount)

    if len(s["trade_price_history"]) > 20:
        s["trade_price_history"] = s["trade_price_history"][-20:]
    if len(s["trade_qty_history"]) > 20:
        s["trade_qty_history"] = s["trade_qty_history"][-20:]

    if len(s["trade_price_history"]) < 2:
        return

    prev_price = s["trade_price_history"][-2]
    prev_qty = s["trade_qty_history"][-2] if len(s["trade_qty_history"]) >= 2 else amount
    if prev_price <= 0:
        prev_price = price

    price_change_pct = abs(price - prev_price) / max(prev_price, 1e-8)
    avg_qty = float(np.mean(s["trade_qty_history"][-5:])) if len(s["trade_qty_history"]) >= 5 else amount
    qty_ratio = amount / max(avg_qty, 1e-8)
    score = min(3.0, qty_ratio * 0.35 + price_change_pct * 25.0)

    if qty_ratio >= 4.0 and price_change_pct >= 0.004:
        s["trade_signal_strength"] = score
        s["trade_signal_reason"] = f"即時大額成交 {amount:.3f} / {qty_ratio:.1f}x 均量"
    else:
        s["trade_signal_strength"] = max(0.0, s["trade_signal_strength"] * 0.85 - 0.05)
        if s["trade_signal_strength"] < 0.15:
            s["trade_signal_strength"] = 0.0
            s["trade_signal_reason"] = ""

    # ── 即時高點追蹤 + 保本鎖定（不等 25 秒主循環）──
    if (
        abs(s.get("qty", 0)) > 0.000001
        and s.get("avg_price", 0) > 0
    ):
        avg_p = s["avg_price"]
        _is_long = s["qty"] > 0
        rt_profit = (price - avg_p) / avg_p if _is_long else (avg_p - price) / avg_p

        # MA 波段使用專用高點鎖利；下方較緊的通用 TrailTP 仍不套用。
        if str(s.get("entry_reason", "") or "").lower() in {"ma_cross", "ma_breakout", "ma25_pullback", "ma_restored"}:
            from core.exits import update_ma_peak_lock
            peak_hit, peak_lock_price = update_ma_peak_lock(
                sym, price, _is_long, event_time=ts_value, require_confirmation=True
            )
            if peak_hit and not s.get("_is_closing", False):
                from core.orders import close_position
                close_side = "sell" if _is_long else "buy"
                logger.info(
                    f"⚡ [Realtime_MA_Peak_Lock] {sym} 即時價格 {price:.6f} "
                    f"穿越高點鎖利 {peak_lock_price:.6f}，結束本段波段"
                )
                await close_position(
                    sym, close_side, abs(s["qty"]), price, avg_p,
                    reason="[MA_Peak_Lock]", is_stop_loss=False,
                )
            return

        # 單一公開成交不能立刻抬高移動停利；新峰值需由下一筆相近成交確認。
        confirmed_peak = float(s.get("highest_profit_pct", 0.0) or 0.0)
        if rt_profit > confirmed_peak:
            candidate_profit = float(s.get("realtime_peak_candidate_profit", 0.0) or 0.0)
            candidate_time = float(s.get("realtime_peak_candidate_time", 0.0) or 0.0)
            atr_pct = float(s.get("current_atr", 0.0) or 0.0) / max(avg_p, 1e-8)
            confirm_tolerance = max(0.0005, min(0.002, atr_pct * 0.25))
            candidate_is_near = (
                candidate_profit > confirmed_peak
                and 0 <= ts_value - candidate_time <= 1.0
                and abs(rt_profit - candidate_profit) <= confirm_tolerance
            )
            if candidate_is_near:
                confirmed_peak = max(candidate_profit, rt_profit)
                confirmed_price = float(s.get("realtime_peak_candidate_price", price) or price)
                if (_is_long and price > confirmed_price) or (not _is_long and price < confirmed_price):
                    confirmed_price = price
                s["highest_profit_pct"] = confirmed_peak
                if _is_long:
                    s["trailing_highest"] = max(s.get("trailing_highest", avg_p), confirmed_price)
                else:
                    s["trailing_lowest"] = min(s.get("trailing_lowest", avg_p), confirmed_price)
                save_peak(sym, confirmed_peak)
                s["realtime_peak_candidate_price"] = 0.0
                s["realtime_peak_candidate_profit"] = 0.0
                s["realtime_peak_candidate_time"] = 0.0
            else:
                s["realtime_peak_candidate_price"] = price
                s["realtime_peak_candidate_profit"] = rt_profit
                s["realtime_peak_candidate_time"] = ts_value
        elif s.get("realtime_peak_candidate_profit", 0.0) > confirmed_peak:
            # 候選峰值沒有第二筆相近成交支持，回落後立即作廢。
            s["realtime_peak_candidate_price"] = 0.0
            s["realtime_peak_candidate_profit"] = 0.0
            s["realtime_peak_candidate_time"] = 0.0

        # 0.6% 以下保留發展空間；達 0.6% 才即時鎖定成本與摩擦緩衝。
        if confirmed_peak >= 0.006 and not s.get("is_breakeven_locked", False):
            _buf = 0.001
            _be = avg_p * (1 + _buf) if _is_long else avg_p * (1 - _buf)
            _sl_now = s.get("stop_loss", 0)
            if _is_long and (_sl_now == 0 or _be > _sl_now):
                s["stop_loss"] = _be
                s["is_breakeven_locked"] = True
                logger.info(f"⚡ [即時保本] {sym} 即時確認達到 {confirmed_peak*100:.2f}%，SL 鎖定 {_be:.4f}")
            elif not _is_long and (_sl_now == 0 or _be < _sl_now):
                s["stop_loss"] = _be
                s["is_breakeven_locked"] = True
                logger.info(f"⚡ [即時保本] {sym} 即時確認達到 {confirmed_peak*100:.2f}%，SL 鎖定 {_be:.4f}")

        # ── TrailTP 即時同步至 stop_loss（每個 trade tick 執行）──
        _atr_rt = s.get("current_atr", 0.0)
        if _atr_rt > 0 and price > 0:
            _ts_atr_pct_rt = _atr_rt / price
            _lev_rt = s.get("leverage", 4)
            _hp_rt = s.get("highest_profit_pct", 0.0)
            _ts_act_rt = 0.006 if _hp_rt < 0.012 else max(0.020 / _lev_rt, _ts_atr_pct_rt * 0.3)
            if _hp_rt > 0.03:       _ts_ret_rt = 0.003
            elif _hp_rt > 0.015:    _ts_ret_rt = 0.004
            elif _hp_rt >= 0.006:   _ts_ret_rt = 0.005
            else:                   _ts_ret_rt = min(max(0.0015, _hp_rt * 0.5), 0.005) if _hp_rt > 0 else 0.002
            if _hp_rt >= _ts_act_rt:
                if _is_long:
                    _ttp_sl = s.get("trailing_highest", avg_p) * (1 - _ts_ret_rt)
                    if _hp_rt < 0.006:
                        _ttp_sl = max(_ttp_sl, avg_p * 1.0011)
                    if _ttp_sl > s.get("stop_loss", 0):
                        s["stop_loss"] = _ttp_sl
                        s["trailing_stop_price"] = max(s.get("trailing_stop_price", 0), _ttp_sl)
                else:
                    _ttp_sl = s.get("trailing_lowest", avg_p) * (1 + _ts_ret_rt)
                    if _hp_rt < 0.006:
                        _ttp_sl = min(_ttp_sl, avg_p * 0.9989)
                    _cur_sl_rt = s.get("stop_loss", 0)
                    if _cur_sl_rt == 0 or _ttp_sl < _cur_sl_rt:
                        s["stop_loss"] = _ttp_sl
                        _cur_ts_rt = s.get("trailing_stop_price", 0)
                        s["trailing_stop_price"] = min(_cur_ts_rt if _cur_ts_rt > 0 else float("inf"), _ttp_sl)

        # 成交流每個 tick 直接檢查移動停利穿越，不再等待主退出循環。
        _rt_ts = float(s.get("trailing_stop_price", 0.0) or 0.0)
        _rt_peak = float(s.get("highest_profit_pct", 0.0) or 0.0)
        _rt_crossed = (
            _rt_peak >= 0.003 and _rt_ts > 0
            and ((_is_long and price <= _rt_ts) or (not _is_long and price >= _rt_ts))
        )
        if _rt_crossed and not s.get("_is_closing", False):
            # 停利線一旦被穿越就必須退出。舊邏輯在跳價後若目前毛利已低於費用安全線，
            # 反而拒絕平倉，會把已鎖定的小利繼續拖成虧損（XLM 0.37% 峰值案例）。
            # 費用安全線只用來決定停利線位置，不能在穿越後變成「禁止止盈」。
            _fee_safe_floor = ROUND_TRIP_FEE_PCT + 0.0005
            if _rt_peak < 0.006 and rt_profit < _fee_safe_floor:
                logger.info(
                    f"⚠️ [Realtime_Trailing_Gap] {sym} 價格跳過軟停利線，當前毛利 "
                    f"{rt_profit*100:.3f}% 已低於費用安全線 {_fee_safe_floor*100:.3f}%，立即退出防止擴大回吐"
                )
            from core.orders import close_position
            close_side = "sell" if _is_long else "buy"
            logger.info(
                f"⚡ [Realtime_Trailing_Trigger] {sym} 即時價格 {price:.6f} "
                f"穿越移動停利 {_rt_ts:.6f}，立即平倉"
            )
            await close_position(
                sym, close_side, abs(s["qty"]), price, avg_p,
                reason="[Dynamic_Trailing]", is_stop_loss=(rt_profit <= 0),
            )
