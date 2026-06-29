import logging
import time
import numpy as np
from core import ctx

logger = logging.getLogger(__name__)


def update_trade_signal(sym, trade):
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

    s["last_trade_price"] = price
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
    if abs(s.get("qty", 0)) > 0.000001 and s.get("avg_price", 0) > 0:
        avg_p = s["avg_price"]
        _is_long = s["qty"] > 0
        rt_profit = (price - avg_p) / avg_p if _is_long else (avg_p - price) / avg_p

        if _is_long:
            if price > s.get("trailing_highest", 0):
                s["trailing_highest"] = price
        else:
            if price < s.get("trailing_lowest", float("inf")):
                s["trailing_lowest"] = price

        if rt_profit > s.get("highest_profit_pct", 0.0):
            s["highest_profit_pct"] = rt_profit

        if rt_profit >= 0.003 and not s.get("is_breakeven_locked", False):
            _buf = 0.003
            _be = avg_p * (1 + _buf)
            _sl_now = s.get("stop_loss", 0)
            if _is_long and (_sl_now == 0 or _be > _sl_now):
                s["stop_loss"] = _be
                s["is_breakeven_locked"] = True
                logger.info(f"⚡ [即時保本] {sym} 即時達到 {rt_profit*100:.2f}%，SL 鎖定 {_be:.4f}")
            elif not _is_long and (_sl_now == 0 or _be < _sl_now):
                s["stop_loss"] = _be
                s["is_breakeven_locked"] = True
                logger.info(f"⚡ [即時保本] {sym} 即時達到 {rt_profit*100:.2f}%，SL 鎖定 {_be:.4f}")

        # ── TrailTP 即時同步至 stop_loss（每個 trade tick 執行）──
        _atr_rt = s.get("current_atr", 0.0)
        if _atr_rt > 0 and price > 0:
            _ts_atr_pct_rt = _atr_rt / price
            _lev_rt = s.get("leverage", 4)
            _hp_rt = s.get("highest_profit_pct", 0.0)
            _ts_act_rt = max(0.020 / _lev_rt, _ts_atr_pct_rt * 0.3)
            if _hp_rt > 0.02:       _ts_ret_rt = 0.001
            elif _hp_rt > 0.008:    _ts_ret_rt = 0.0015
            elif _hp_rt > 0.004:    _ts_ret_rt = 0.002
            elif _hp_rt > 0.002:    _ts_ret_rt = 0.003
            else:                   _ts_ret_rt = min(max(0.0008, _hp_rt * 0.5), 0.002) if _hp_rt > 0 else 0.001
            if _hp_rt >= _ts_act_rt:
                if _is_long:
                    _ttp_sl = s.get("trailing_highest", avg_p) * (1 - _ts_ret_rt)
                    if _ttp_sl > s.get("stop_loss", 0):
                        s["stop_loss"] = _ttp_sl
                else:
                    _ttp_sl = s.get("trailing_lowest", avg_p) * (1 + _ts_ret_rt)
                    _cur_sl_rt = s.get("stop_loss", 0)
                    if _cur_sl_rt == 0 or _ttp_sl < _cur_sl_rt:
                        s["stop_loss"] = _ttp_sl
