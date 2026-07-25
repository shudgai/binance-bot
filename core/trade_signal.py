import logging
import time
import numpy as np
from core import ctx
from core.config import ROUND_TRIP_FEE_PCT
from core.peak_store import save_peak

logger = logging.getLogger(__name__)
STALE_TRADE_LOG_INTERVAL_SEC = 15.0


def _log_stale_trade_tick(sym, state, ts_value, last_market_ts, open_time):
    """Throttle reconnect backfill warnings while keeping every stale tick blocked."""
    now = time.time()
    last_log_at = float(state.get("_last_stale_trade_log_at", 0.0) or 0.0)
    if last_log_at and now - last_log_at < STALE_TRADE_LOG_INTERVAL_SEC:
        state["_stale_trade_log_suppressed"] = int(
            state.get("_stale_trade_log_suppressed", 0) or 0
        ) + 1
        return

    suppressed = int(state.get("_stale_trade_log_suppressed", 0) or 0)
    suppressed_text = f"；前期間另合併 {suppressed} 筆" if suppressed else ""
    logger.info(
        f"⚠️ [Stale_Trade_Tick] {sym} 忽略亂序/進場前成交 "
        f"event={ts_value:.3f} last={last_market_ts:.3f} open={open_time:.3f}"
        f"{suppressed_text}"
    )
    state["_last_stale_trade_log_at"] = now
    state["_stale_trade_log_suppressed"] = 0


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
        _log_stale_trade_tick(sym, s, ts_value, last_market_ts, open_time)
        return
    s["last_market_trade_time"] = ts_value

    s["last_trade_price"] = price
    s["close_price"] = price
    s["last_ohlcv_update"] = time.time()

    # ── 防插針確認價（Anti-Spike Filter — 雙重保護）──
    # 幣安設計 Mark Price 就是用跨交易所指數均價防止插針誤觸強制清算。
    # 此處以兩層等效保護取代即時 mark price API 查詢（節省 API 權重）：
    #
    # 【第1層】最近 5 筆成交中位數（擴大窗口，更難被連發插針污染）
    #   - 正常行情：連續5筆都在同方向，中位數 = 真實趨勢價格
    #   - 插針行情：插針1~2筆偏離，中位數仍落在正常成交區間
    #
    # 【第2層】與 OHLCV K線收盤偏差 > 0.5% 交叉確認
    #   - K線收盤是分批定時更新的，比即時成交流平滑很多，
    #     類似 Mark Price 的「跨時間平均」特性。
    #   - 若成交流瞬間價格偏離K線收盤 > 0.5%，直接採用K線收盤
    #     作為確認價，而非成交流中位數（更保守）。
    #
    # 停利追蹤仍用即時 close_price，保持對獲利行情的敏感度。
    _rp = s.get("_recent_prices_5", [])
    _rp = (_rp + [price])[-5:]
    s["_recent_prices_5"] = _rp
    _sorted5 = sorted(_rp)
    _median5 = _sorted5[len(_sorted5) // 2]  # 5筆中位數

    # 第2層：與 OHLCV K線收盤交叉確認（Mark Price 替代品）
    _ohlcv_close = 0.0
    if s.get("ohlcv") and len(s["ohlcv"]) >= 2:
        # 使用倒數第2根（已收盤的完整K線），比當前未完成K線更穩定
        _ohlcv_close = float(s["ohlcv"][-2][4] or 0.0)
    if _ohlcv_close > 0 and _median5 > 0:
        _ohlcv_dev = abs(_median5 - _ohlcv_close) / _ohlcv_close
        if _ohlcv_dev > 0.005:  # 中位數仍偏離K線收盤 > 0.5% → 疑似連發插針
            _now = time.time()
            _last_log_at = float(s.get("_last_spike_filter_log_at", 0.0) or 0.0)
            if _now - _last_log_at >= 15.0:
                logger.info(
                    f"⚡ [SpikeFilter_L2] {sym} 成交中位數 {_median5:.6f} 偏離K線收盤 "
                    f"{_ohlcv_close:.6f} 達 {_ohlcv_dev*100:.2f}% > 0.5%，"
                    f"採用K線收盤作為確認價（疑似連發插針，已節流 15s）"
                )
                s["_last_spike_filter_log_at"] = _now
            s["close_price_spike_filtered"] = _ohlcv_close
        else:
            s["close_price_spike_filtered"] = _median5
    else:
        s["close_price_spike_filtered"] = _median5

    # 同步修改當前開著的 K 線 (ohlcv[-1])，確保即時指標計算與止損判斷最精準
    if "ohlcv" in s and s["ohlcv"]:
        _lc = s["ohlcv"][-1]
        if len(_lc) >= 5:
            _lc[4] = price
            _lc[2] = max(_lc[2], price)
            _lc[3] = min(_lc[3], price)
            
    s["last_trade_qty"] = amount
    trade_side = str(trade.get("side", "buy") or "buy")
    s["last_trade_side"] = trade_side
    s["last_trade_time"] = ts_value
    s["trade_price_history"].append(price)
    s["trade_qty_history"].append(amount)
    # 帶方向的成交量：taker 買方吃賣一記正值、taker 賣方砸買一記負值，
    # 供 check_realtime_sell_pressure 判斷即時成交流是否出現逆勢方向的量能主導。
    s.setdefault("trade_side_history", []).append(amount if trade_side == "buy" else -amount)

    if len(s["trade_price_history"]) > 20:
        s["trade_price_history"] = s["trade_price_history"][-20:]
    if len(s["trade_side_history"]) > 20:
        s["trade_side_history"] = s["trade_side_history"][-20:]
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

    # ── 即時高點/低點追蹤（不等 5 秒主循環）──
    # [2026-07-25] 方案三：SL/TP 觸發判斷統一交給 exits.check_exits()（5秒一輪，含
    # 防插針保護），這裡只在每筆成交時即時更新 highest_price/lowest_price/sl_price/
    # tp_price，讓追蹤停損/延展停利的峰值不必等到下一輪主迴圈才反應；不在這裡直接
    # 平倉，避免跟 check_exits() 對同一倉位重複觸發平倉的競態。
    # 用 close_price_spike_filtered（上面剛算好的防插針確認價）而非原始 price，
    # 避免單筆插針瞬間偽造一個「新高」，把保本/追蹤停損永久鎖在雜訊價位上
    # （實測 XMRUSDT 案例：單筆成交插到 +0.33%，保本剛觸發下一筆就打回真實價位，
    # 平倉在接近保本的位置，明明沒有真的走到那個高點）。
    if (
        abs(s.get("qty", 0)) > 0.000001
        and s.get("avg_price", 0) > 0
    ):
        _is_long = s["qty"] > 0
        _sf_price = float(s.get("close_price_spike_filtered", price) or price)
        from core.exits import update_trailing_stop
        update_trailing_stop(sym, _sf_price, _is_long, update_peak=True)
