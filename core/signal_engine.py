import logging
import json

from core import ctx
from core.config import CONFIG_FILE

logger = logging.getLogger(__name__)



from core.config import ENTRY_SURGE_THRESHOLD, MA_CROSS_MIN_GAP_PCT, MIN_TREND_ADX

CORE_LIQUID_SYMBOLS = {"BTCUSDT", "ETHUSDT", "BNBUSDT"}

def _ma_base_volume_limit(sym, atr_pct):
    if atr_pct > 5.0:
        return 0.8
    return 0.50 if sym in CORE_LIQUID_SYMBOLS else 0.55


def compute_signal_strength(sym, realtime_trigger=False):
    """Generate entries exclusively from completed-candle MA7/25/99 setups."""
    s = ctx.STATES[sym]
    s["entry_block_reason"] = ""
    candles = s.get("ohlcv", [])
    ma7 = float(s.get("ma7", 0.0) or 0.0)
    ma25 = float(s.get("ma25", 0.0) or 0.0)
    ma99 = float(s.get("ma99", 0.0) or 0.0)
    prev_ma7 = float(s.get("prev_ma7", 0.0) or 0.0)
    prev_ma25 = float(s.get("prev_ma25", 0.0) or 0.0)
    vol_ma20 = float(s.get("vol_ma20", 0.0) or 0.0)
    adx = float(s.get("adx", 0.0) or 0.0)
    prev_adx = float(s.get("prev_adx", 0.0) or 0.0)
    if len(candles) < 22 or min(ma7, ma25, ma99, prev_ma7, prev_ma25, vol_ma20) <= 0:
        s["entry_block_reason"] = "MA7／MA25／MA99 或成交量資料尚未完成"
        return (None, 0, None)

    # 實測 DOTUSDT（ADX=0.0）、BCHUSDT（ADX=1.4）、AVAXUSDT（ADX=2.7）三筆案例：
    # MA_Cross／MA7_Simple 在幾乎沒有趨勢的盤整行情下一樣會觸發訊號（這幾條路線
    # 原本完全沒有 ADX 下限），進場後幾十秒內就整段反轉回吐。四條 MA 趨勢路線
    # 共用同一個最低 ADX 門檻，低於門檻直接不產生任何 MA 訊號——沒有趨勢的
    # 環境，趨勢跟隨策略本來就不該進場，不分路線都一樣。
    # 模式 A（高品質杜絕假突破）：趨勢強度 ADX 低於 18.0 直接過濾，拒絕死水盤整
    if adx < MIN_TREND_ADX:
        s["entry_block_reason"] = f"ADX={adx:.1f} < {MIN_TREND_ADX:.0f}，盤整無趨勢，暫停 MA 訊號"
        return (None, 0, None)

    signal_candle = candles[-2]
    signal_ts = int(signal_candle[0])
    candle_open, candle_high, candle_low, candle_close, candle_volume = map(float, signal_candle[1:6])
    volume_ratio = candle_volume / vol_ma20
    golden_cross = prev_ma7 <= prev_ma25 and ma7 > ma25
    death_cross = prev_ma7 >= prev_ma25 and ma7 < ma25
    gap, prev_gap = ma7 - ma25, prev_ma7 - prev_ma25
    long_spreading = ma7 > ma25 and ma7 > prev_ma7 and gap > max(prev_gap, 0.0)
    short_spreading = ma7 < ma25 and ma7 < prev_ma7 and gap < min(prev_gap, 0.0)
    above_ma99, below_ma99 = candle_close > ma99, candle_close < ma99

    current_rsi = float(s.get("current_rsi", 50.0))
    vol_surge = float(s.get("vol_surge", 0.0))
    personality = s.get("personality", "calm")
    atr_pct = float(s.get("atr_pct", 0.0))

    # 模式 A（高品質杜絕假突破）：量能必須達到 20 週期均量的 0.80x 以上，真金白銀掃盤才放行
    base_limit = _ma_base_volume_limit(sym, atr_pct)
    breakout_limit = base_limit * 1.3

    long_stack = ma7 > ma25 and ma7 > prev_ma7 and ma25 >= prev_ma25
    short_stack = ma7 < ma25 and ma7 < prev_ma7 and ma25 <= prev_ma25

    # 即時量只用來確認當下價格方向；MA 策略的參與度門檻必須使用訊號 K 棒的已收線量。
    # 否則新 K 棒剛開始時 vol_surge 接近 0 會誤擋有效訊號，也可能被未收線瞬時量誤放行。
    is_realtime_strong = realtime_trigger and (vol_surge >= 1.5)

    # 防假突破倒鉤 / 上下影線反轉過濾 (Fake Breakout Wick Guard)
    c_range = max(candle_high - candle_low, 1e-8)
    upper_wick_ratio = (candle_high - max(candle_close, candle_open)) / c_range
    lower_wick_ratio = (min(candle_close, candle_open) - candle_low) / c_range
    long_no_fake_breakout = upper_wick_ratio <= 0.45  # 上影線不可超過 45% (拒絕高位倒鉤/假突破吸頂)
    short_no_fake_breakout = lower_wick_ratio <= 0.45  # 下影線不可超過 45% (拒絕低位反彈/假向下破位)

    # 偏離度過大過濾 (過度延伸追高拒絕：超過 1.5% 遠離均線拒絕進場)
    dev_from_ma25 = (candle_close - ma25) / ma25 if ma25 > 0 else 0.0
    long_not_overextended = dev_from_ma25 <= 0.015  # 開多時離 MA25 不可拉開 > 1.5%
    short_not_overextended = dev_from_ma25 >= -0.015  # 開空時離 MA25 不可跌開 > 1.5%

    # MA7／MA25 必須在交叉後拉開最小距離；斜率與 GAP
    ma_gap_pct = abs(gap) / candle_close if candle_close > 0 else 0.0
    ma7_slope = abs(ma7 - prev_ma7) / candle_close if candle_close > 0 else 0.0
    ma25_slope = abs(ma25 - prev_ma25) / candle_close if candle_close > 0 else 0.0
    is_flat_chop = ma_gap_pct < 0.001 and ma7_slope < 0.0005 and ma25_slope < 0.0005
    cross_direction_confirmed = ma_gap_pct >= MA_CROSS_MIN_GAP_PCT

    # 嚴格量能爆發與防假突破確認 (拒絕無量假突破/偽交叉)
    cross_long_volume_ok = volume_ratio >= base_limit or (
        volume_ratio >= 0.45 and above_ma99 and atr_pct <= 5.0
    )
    cross_short_volume_ok = volume_ratio >= base_limit or (
        volume_ratio >= 0.45 and below_ma99 and atr_pct <= 5.0
    )
    cross_long = (golden_cross and ma7 > prev_ma7 and ma25 >= prev_ma25
                  and (candle_close > candle_open or is_realtime_strong)
                  and cross_long_volume_ok and current_rsi < 68
                  and cross_direction_confirmed and not is_flat_chop
                  and long_no_fake_breakout and long_not_overextended)
    cross_short = (death_cross and ma7 < prev_ma7 and ma25 <= prev_ma25
                   and (candle_close < candle_open or is_realtime_strong)
                   and cross_short_volume_ok and current_rsi > 32
                   and cross_direction_confirmed and not is_flat_chop
                   and short_no_fake_breakout and short_not_overextended)

    atr = float(s.get("current_atr", 0.0) or 0.0)
    touch_tolerance = max(0.0015, min(0.008, (atr / candle_close) * 0.5 if candle_close > 0 else 0.002))
    from core.config import STRICT_ENTRY_SYMBOLS
    pullback_rebound_limit = max(candle_close * 0.0015, atr * 0.35)
    pullback_long_rebound = candle_close - ma25
    pullback_short_rebound = ma25 - candle_close

    # 回調路線：量能 + K 棒方向確認 + 杜絕盤整黏合與上影線假突破
    pullback_long = (long_spreading and long_stack and candle_low <= ma25 * (1 + touch_tolerance)
                     and candle_close >= ma25 and (candle_close > candle_open or is_realtime_strong)
                     and volume_ratio >= base_limit and current_rsi < 70 and not is_flat_chop
                     and long_no_fake_breakout
                     and (sym not in STRICT_ENTRY_SYMBOLS or pullback_long_rebound <= pullback_rebound_limit))
    pullback_short = (short_spreading and short_stack and candle_high >= ma25 * (1 - touch_tolerance)
                      and candle_close <= ma25 and (candle_close < candle_open or is_realtime_strong)
                      and volume_ratio >= base_limit and current_rsi > 30 and not is_flat_chop
                      and short_no_fake_breakout
                      and (sym not in STRICT_ENTRY_SYMBOLS or pullback_short_rebound <= pullback_rebound_limit))

    from core.config import DISABLE_MA_BREAKOUT, DISABLE_MA25_PULLBACK
    completed = candles[:-1]
    breakout_long = breakout_short = False
    if len(completed) >= 21 and not DISABLE_MA_BREAKOUT:
        prior = completed[-21:-1]
        prior_high = max(float(c[2]) for c in prior)
        prior_low = min(float(c[3]) for c in prior)
        # 突破路線：嚴格真量能 (RVOL >= 1.0) + 影線過濾 + 偏離過大過濾 (杜絕假突破追高)
        breakout_long = (long_spreading and long_stack and candle_close > prior_high
                         and (candle_close > candle_open or is_realtime_strong)
                         and volume_ratio >= max(1.0, breakout_limit) and current_rsi < 68
                         and long_no_fake_breakout and long_not_overextended)
        breakout_short = (short_spreading and short_stack and candle_close < prior_low
                           and (candle_close < candle_open or is_realtime_strong)
                           and volume_ratio >= max(1.0, breakout_limit) and current_rsi > 32
                           and short_no_fake_breakout and short_not_overextended)

    from core.config import DISABLE_MA_BREAKOUT, DISABLE_MA25_PULLBACK, DISABLE_MA_CROSS
    if (cross_long or cross_short) and not DISABLE_MA_CROSS:
        side, route = ("buy" if cross_long else "sell"), "MA_Cross"
    elif (breakout_long or breakout_short) and not DISABLE_MA_BREAKOUT:
        side, route = ("buy" if breakout_long else "sell"), "MA_Breakout"
    elif (pullback_long or pullback_short) and not DISABLE_MA25_PULLBACK:
        side, route = ("buy" if pullback_long else "sell"), "MA25_Pullback"
    else:
        # 既有三條路線都沒觸發時，才嘗試簡化路線 (MA7_Simple)
        #
        # 實測（peak_giveback_stats.py + trade_history.json）：MA7_Simple 40 筆只有
        # 20% 勝率，是所有路線裡最差、單一路線就吃掉全部虧損過半。原因是它唯一
        # 沒有要求 MA25 中期趨勢配合方向（其他三條路線都要求 long/short_spreading
        # 或 long/short_stack），等於允許在 MA25 明顯走跌時，只因 MA7 這條最快的
        # 均線單根蠟燭翻頭向上就做多——這種逆著中期趨勢的早期轉折，本質上更容易
        # 只是雜訊，不是真反轉。這裡補上「MA25 不能是逆勢方向」的最低限度要求，
        # 量能門檻也拉齊到跟其他路線一樣的 0.6x（原本 0.5x 比全部路線都寬鬆，
        # 等於連平均以下的量都放行）。
        prev_ma7_2 = float(s.get("prev_ma7_2", 0.0) or 0.0)
        prev_slope = prev_ma7 - prev_ma7_2
        curr_slope = ma7 - prev_ma7
        turn_up = prev_slope <= 0 and curr_slope > 0
        turn_down = prev_slope >= 0 and curr_slope < 0
        bullish_candle = candle_close > candle_open
        bearish_candle = candle_close < candle_open
        volume_ok = volume_ratio >= base_limit
        ma25_not_against_long = ma25 >= prev_ma25
        ma25_not_against_short = ma25 <= prev_ma25
        # 實測 LINKUSDT 案例：ADX 在短短 15 秒內從 7.7 暴衝到 35.4，同一輪掃描
        # 就翻出 MA7_Simple 訊號，看起來像扎實趨勢，其實只是單根尖刺行情帶動，
        # 進場後浮盈只到 +0.41% 就反轉停損。正常累積出來的趨勢，ADX 不會在
        # 相鄰兩次掃描（約 10 秒）間跳這麼多，用這個過濾掉尖刺型態的假訊號。
        ADX_SPIKE_GUARD_PCT = 20.0
        adx_not_spiking = (adx - prev_adx) <= ADX_SPIKE_GUARD_PCT

        # 規則 1：RSI 邊界保護 - MA7 轉折方向要與 RSI 動能空間一致
        # 做空時 RSI < 52 表示已在下跌中途（超賣風險高），不跟進
        # 做多時 RSI > 65 表示已在上漲中途（超買風險高），不跟進
        # 規則 1：RSI 邊界保護 - MA7 轉折方向要與 RSI 動能空間一致
        MA7_SIMPLE_SHORT_RSI_FLOOR = 20.0   # 重新放寬做空最低 RSI 要求，允許在極度跌勢中追空
        MA7_SIMPLE_LONG_RSI_CEIL   = 80.0   # 做多最高 RSI 要求放寬至 80

        # 規則 2：15m RSI 多時間框架確認
        rsi_15m = float(s.get("rsi_15m", 0.0) or 0.0)
        MTF_RSI_SHORT_FLOOR = 35.0  # 15m RSI 防超賣地板放寬
        MTF_RSI_SHORT_CEIL  = 50.0  # 15m RSI 防逆勢天花板放寬
        MTF_RSI_LONG_CEIL   = 75.0  # 15m RSI 防超買天花板放寬
        MTF_RSI_LONG_FLOOR  = 50.0  # 15m RSI 防逆勢地板放寬

        # MA7_Simple 不再享有低量豁免；單根均線勾頭至少要有與其他 MA 路線相同的已收線量能。
        ma7_simple_volume_ok = volume_ratio >= base_limit
        curr_slope_pct = (ma7 - prev_ma7) / candle_close if candle_close > 0 else 0.0
        slope_confirmed = curr_slope_pct > 0.0
        price_above_ma7 = candle_close >= (ma7 * 0.9992)
        rsi_bottom_ok = current_rsi >= 35.0
        ma25_extension_limit = max(candle_close * 0.0035, atr * 1.5)
        ma25_long_extension = candle_close - ma25
        ma25_short_extension = ma25 - candle_close
        ma25_long_extension_ok = 0.0 <= ma25_long_extension <= ma25_extension_limit
        ma25_short_extension_ok = 0.0 <= ma25_short_extension <= ma25_extension_limit

        ma7_ma25_gap_pct = abs(ma7 - ma25) / candle_close if candle_close > 0 else 0.0
        ma7_simple_gap_ok = ma7_ma25_gap_pct >= 0.0008
        adx_trend_ok = adx >= 22.0

        # MA7 谷底轉折向上：當 MA7 勾頭向上、RVOL >= 0.3x 即允許開倉做多
        if (turn_up and slope_confirmed and price_above_ma7 and rsi_bottom_ok
                and ma7_simple_volume_ok and current_rsi < 82.0
                and ma7 >= ma25 and ma25_not_against_long and ma25_long_extension_ok
                and adx_not_spiking and adx_trend_ok and ma7_simple_gap_ok):
            # 規則 1：已超買則不追多
            if current_rsi > MA7_SIMPLE_LONG_RSI_CEIL:
                reason = f"MA7 谷底轉折向上，但 5m RSI={current_rsi:.1f} > {MA7_SIMPLE_LONG_RSI_CEIL:.0f} 偏高，跳過"
                logger.info(f"@@COIN_DEBUG@@ ⏳ {sym} [MA7_Simple] {reason}")
            # 規則 2：15m RSI 多時間框架確認（有資料才檢查）
            elif rsi_15m > 0 and rsi_15m > MTF_RSI_LONG_CEIL:
                reason = f"MA7 谷底轉折，但 15m RSI={rsi_15m:.1f} > {MTF_RSI_LONG_CEIL:.0f} 大週期已超買，跳過"
                logger.info(f"@@COIN_DEBUG@@ ⏳ {sym} [MA7_Simple] {reason}")
            elif rsi_15m > 0 and rsi_15m < MTF_RSI_LONG_FLOOR:
                reason = f"MA7 谷底轉折，但 15m RSI={rsi_15m:.1f} < {MTF_RSI_LONG_FLOOR:.0f} 大週期仍偏空，防逆勢跳過"
                logger.info(f"@@COIN_DEBUG@@ ⏳ {sym} [MA7_Simple] {reason}")
            else:
                side, route = "buy", "MA7_Simple"
                reason = f"MA7 谷底轉折向上 | MA7={ma7:.6f} RVOL={volume_ratio:.2f}x RSI={current_rsi:.1f}" + (f" 15mRSI={rsi_15m:.1f}" if rsi_15m > 0 else "")
                s["ma_signal_candle_ts"] = signal_ts
                logger.info(f"@@COIN_DEBUG@@ ✅ {sym} [MA7_Simple] buy | {reason}")
                volume_adjustment = max(-2.0, min((volume_ratio - 0.8) * 5.0, 5.0))
                strength = 25.0 + volume_adjustment
                return (side, strength, route)
        # MA7 頭部轉折向下：在 MA7 一向下勾且 RVOL >= 0.3x 時即刻開倉做空
        elif (turn_down and ma7_simple_volume_ok and current_rsi > 18.0
                and ma7 <= ma25 and ma25_not_against_short and ma25_short_extension_ok
                and adx_not_spiking and adx_trend_ok and ma7_simple_gap_ok):
            # 規則 1：已在超賣區則不追空（ENAUSDT RSI=40 做空的問題案例）
            if current_rsi < MA7_SIMPLE_SHORT_RSI_FLOOR:
                reason = f"MA7 頭部轉折向下，但 5m RSI={current_rsi:.1f} < {MA7_SIMPLE_SHORT_RSI_FLOOR:.0f} 已偏低，跳過避免超賣區做空"
                logger.info(f"@@COIN_DEBUG@@ ⏳ {sym} [MA7_Simple] {reason}")
            # 規則 2：15m RSI 多時間框架確認（有資料才檢查）
            elif rsi_15m > 0 and rsi_15m < MTF_RSI_SHORT_FLOOR:
                reason = f"MA7 頭部轉折，但 15m RSI={rsi_15m:.1f} < {MTF_RSI_SHORT_FLOOR:.0f} 大週期已超賣，跳過"
                logger.info(f"@@COIN_DEBUG@@ ⏳ {sym} [MA7_Simple] {reason}")
            elif rsi_15m > 0 and rsi_15m > MTF_RSI_SHORT_CEIL:
                reason = f"MA7 頭部轉折，但 15m RSI={rsi_15m:.1f} > {MTF_RSI_SHORT_CEIL:.0f} 大週期仍偏多，防逆勢跳過"
                logger.info(f"@@COIN_DEBUG@@ ⏳ {sym} [MA7_Simple] {reason}")
            else:
                side, route = "sell", "MA7_Simple"
                reason = f"MA7 頭部轉折向下 | MA7={ma7:.6f} RVOL={volume_ratio:.2f}x RSI={current_rsi:.1f}" + (f" 15mRSI={rsi_15m:.1f}" if rsi_15m > 0 else "")
                s["ma_signal_candle_ts"] = signal_ts
                logger.info(f"@@COIN_DEBUG@@ ✅ {sym} [MA7_Simple] sell | {reason}")
                # 空單採對稱評分：弱量仍可觀察，但排序必須低於有量轉折。
                volume_adjustment = max(-2.0, min((volume_ratio - 0.8) * 5.0, 5.0))
                strength = 25.0 + volume_adjustment
                return (side, strength, route)

        if turn_up and not ma25_not_against_long:
            reason = "MA7 雖向上勾，但 MA25 中期趨勢仍下彎，拒絕逆勢做多"
        elif turn_up and ma25_long_extension > ma25_extension_limit:
            reason = (f"MA7 向上勾但價格高於 MA25 {ma25_long_extension/ma25*100:.2f}% "
                      f"> 允許 {ma25_extension_limit/ma25*100:.2f}%，反彈末端不追多")
        elif turn_down and not ma25_not_against_short:
            reason = "MA7 雖向下勾，但 MA25 中期趨勢仍上揚，拒絕逆勢做空"
        elif turn_down and ma25_short_extension > ma25_extension_limit:
            reason = (f"MA7 向下勾但價格低於 MA25 {ma25_short_extension/ma25*100:.2f}% "
                      f"> 允許 {ma25_extension_limit/ma25*100:.2f}%，下跌末端不追空")
        elif turn_up and rsi_15m > 0 and rsi_15m < MTF_RSI_LONG_FLOOR:
            reason = f"MA7 向上勾但 15m RSI={rsi_15m:.1f} < 50，多週期仍偏空不做多"
        elif turn_down and rsi_15m > MTF_RSI_SHORT_CEIL:
            reason = f"MA7 向下勾但 15m RSI={rsi_15m:.1f} > 50，多週期仍偏多不做空"
        elif volume_ratio < base_limit:
            reason = f"量能不足（RVOL={volume_ratio:.2f}x < {base_limit:.2f}x），暫停交易"
        elif (golden_cross or death_cross) and not cross_direction_confirmed:
            reason = (f"MA7／MA25 交叉間距僅 {ma_gap_pct*100:.4f}% < "
                      f"{MA_CROSS_MIN_GAP_PCT*100:.4f}%，方向確認不足")
        elif is_flat_chop and (golden_cross or death_cross):
            reason = "MA7／MA25 平走交織，屬盤整假訊號區"
        elif current_rsi >= 70 and ma7 > ma25:
            reason = f"RSI={current_rsi:.1f} 已達極端值，防超買反轉不追多"
        elif current_rsi <= 30 and ma7 < ma25:
            reason = f"RSI={current_rsi:.1f} 已達極端值，防超賣反轉不追空"
        else:
            reason = "等待 MA7／MA25 收線交叉、MA25 回調或帶量突破"
        s["entry_block_reason"] = reason
        logger.info(f"@@COIN_DEBUG@@ ⏳ {sym} [MA_Strategy] {reason}")
        return (None, 0, None)

    strength = 25.0 + min(max(volume_ratio - 0.8, 0.0) * 5.0, 5.0)
    if route == "MA_Breakout":
        strength += 2.0
    s["ma_signal_candle_ts"] = signal_ts
    logger.info(f"@@COIN_DEBUG@@ ✅ {sym} [{route}] {side} | close={candle_close:.6f}, MA7={ma7:.6f}, MA25={ma25:.6f}, MA99={ma99:.6f}, volume={volume_ratio:.2f}x")
    return (side, strength, route)



# Legacy RSI/MACD/BB entry routes were removed when the MA lifecycle became authoritative.

def _load_disabled_symbols():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return {s.upper().replace(":USDT", "USDT") for s in data.get("disabled", [])}
    except Exception:
        return set()


def _find_horizontal_zones(candles, atr, lookback, min_touches, tolerance_atr,
                           current_price=0.0, min_width_pct=0.0):
    """在已收盤 K 棒中找水平支撐帶與壓力帶。

    演算法：
    1. 收集所有已收盤 K 棒的低點（支撐候選）和高點（壓力候選）。
    2. 以 ATR × tolerance_atr 為群聚半徑，把接近的價位合併為「水平帶」。
    3. 過濾掉觸碰次數 < min_touches 的帶（太少觸碰 = 未確認支撐/壓力）。
    4. 回傳 (最強支撐帶中心, 最強壓力帶中心)，無則回傳 None。

    Args:
        candles: 已收盤 K 棒列表（不含當前未收盤那根）
        atr: 當前 ATR
        lookback: 最多往回看幾根
        min_touches: 確認水平帶的最低觸碰次數
        tolerance_atr: 群聚半徑（ATR 倍數）

    Returns:
        (support_center, resistance_center)，找不到時為 None
    """
    if not candles or atr <= 0:
        return None, None

    recent = candles[-lookback:] if len(candles) >= lookback else candles
    lows  = [float(c[3]) for c in recent]
    highs = [float(c[2]) for c in recent]
    band  = atr * tolerance_atr

    def cluster(prices):
        """把相近價位聚合成帶，回傳 (中心價, 觸碰次數) 的列表。"""
        if not prices:
            return []
        sorted_p = sorted(prices)
        groups = []
        current = [sorted_p[0]]
        for p in sorted_p[1:]:
            if p - current[0] <= band:
                current.append(p)
            else:
                groups.append(current)
                current = [p]
        groups.append(current)
        return [(sum(g) / len(g), len(g)) for g in groups]

    support_clusters    = [(c, t) for c, t in cluster(lows)  if t >= min_touches]
    resistance_clusters = [(c, t) for c, t in cluster(highs) if t >= min_touches]

    # 區間交易要使用「現價下方最近支撐／現價上方最近壓力」。舊版只取觸碰
    # 次數最多，次數相同時會因排序順序拿到最遠價位，甚至把不同區段硬配成一個區間。
    if current_price > 0:
        supports_below = [(c, t) for c, t in support_clusters if c <= current_price]
        resistances_above = [(c, t) for c, t in resistance_clusters if c >= current_price]
        if min_width_pct > 0:
            valid_pairs = [
                (support_row, resistance_row)
                for support_row in supports_below
                for resistance_row in resistances_above
                if support_row[0] > 0
                and (resistance_row[0] - support_row[0]) / support_row[0] >= min_width_pct
            ]
            if not valid_pairs:
                return None, None
            support_row, resistance_row = min(
                valid_pairs,
                key=lambda pair: (
                    (pair[1][0] - pair[0][0]) / pair[0][0],
                    -(pair[0][1] + pair[1][1]),
                    abs(((pair[0][0] + pair[1][0]) / 2.0) - current_price),
                ),
            )
            return support_row[0], resistance_row[0]
        support = max(supports_below, key=lambda x: x[0])[0] if supports_below else None
        resistance = min(resistances_above, key=lambda x: x[0])[0] if resistances_above else None
    else:
        support = max(support_clusters, key=lambda x: x[1])[0] if support_clusters else None
        resistance = max(resistance_clusters, key=lambda x: x[1])[0] if resistance_clusters else None

    return support, resistance


def compute_range_signal(sym):
    """在 ADX 低（區間行情）時，偵測水平支撐/壓力並產生進場訊號。

    條件（全部需滿足）：
    - RANGE_MODE_ENABLED 為 True
    - ADX < RANGE_ADX_THRESHOLD（確認區間行情）
    - 找到有足夠觸碰次數的支撐帶或壓力帶
    - 區間寬度（壓力 - 支撐）> 手續費 + RANGE_MIN_NET_PROFIT_PCT（空間保護）
    - 當前收盤 K 棒確認回彈（做多）或拒絕（做空）：
        做多：低點進入支撐帶誤差帶 且 收盤 > 支撐帶中心（收陽或長下影）
        做空：高點進入壓力帶誤差帶 且 收盤 < 壓力帶中心（收陰或長上影）
    - RSI：做多 < 55，做空 > 45

    Returns:
        (side, strength, route) 或 (None, 0, None)
    """
    from core.config import (
        RANGE_MODE_ENABLED, RANGE_ADX_THRESHOLD, RANGE_LOOKBACK,
        RANGE_TOUCH_COUNT, RANGE_TOUCH_ATR_TOLERANCE,
        RANGE_MIN_NET_PROFIT_PCT, STRICT_ENTRY_SYMBOLS,
        STRICT_RANGE_MIN_NET_PROFIT_PCT, TAKER_FEE_RATE,
    )

    if not RANGE_MODE_ENABLED:
        return (None, 0, None)

    s = ctx.STATES.get(sym)
    if not s:
        return (None, 0, None)

    candles = s.get("ohlcv", [])
    if len(candles) < 22:
        s["entry_block_reason"] = "K 棒資料不足（區間模式）"
        return (None, 0, None)

    atr = float(s.get("current_atr", 0.0) or 0.0)
    adx = float(s.get("adx", 0.0) or 0.0)
    rsi = float(s.get("current_rsi", 50.0) or 50.0)
    prev_rsi = float(s.get("prev_rsi", 50.0) or 50.0)
    vol_ma20 = float(s.get("vol_ma20", 0.0) or 0.0)

    if atr <= 0 or vol_ma20 <= 0:
        s["entry_block_reason"] = (
            f"ATR 或成交量資料不足（區間模式：ATR={atr:.8g}, VolMA20={vol_ma20:.2f}）"
        )
        return (None, 0, None)

    # 1. ADX 確認區間行情；高 ADX 交給 MA 趨勢路線，避免前段產生 Range 訊號、
    # 送單前又被同一個 ADX 門檻取消。
    if adx >= RANGE_ADX_THRESHOLD:
        s["entry_block_reason"] = f"ADX={adx:.1f} ≥ {RANGE_ADX_THRESHOLD}，趨勢明顯，改由 MA 策略評估"
        logger.info(f"⏳ {sym} [Range] ADX={adx:.1f} 過高，略過區間模式")
        return (None, 0, None)

    # 1.5. ATR 波動比例過濾（高於 6.0% 判定為高波動妖幣，不開區間倉以防突破止損）
    close_price = candles[-1][4] if candles else 0.0
    atr_pct = (atr / close_price) if close_price > 0 else 0.0
    if atr_pct > 0.06:
        s["entry_block_reason"] = f"ATR波動佔比={atr_pct*100:.2f}% > 6.0%，波動過大，略過區間模式"
        logger.info(f"@@COIN_DEBUG@@ ⏳ {sym} [Range] ATR波動比例 ({atr_pct*100:.2f}%) 過高，略過區間模式")
        return (None, 0, None)

    # 2. 辨識水平支撐/壓力帶（只用已收盤 K 棒，排除最後一根）
    completed = candles[:-1]
    signal_close = float(completed[-1][4])
    range_min_net_pct = (
        STRICT_RANGE_MIN_NET_PROFIT_PCT
        if sym in STRICT_ENTRY_SYMBOLS else RANGE_MIN_NET_PROFIT_PCT
    )
    min_range_width_pct = TAKER_FEE_RATE * 2 + range_min_net_pct
    support, resistance = _find_horizontal_zones(
        completed, atr, RANGE_LOOKBACK, RANGE_TOUCH_COUNT, RANGE_TOUCH_ATR_TOLERANCE,
        current_price=signal_close, min_width_pct=min_range_width_pct,
    )

    if support is None or resistance is None or support >= resistance:
        s["entry_block_reason"] = "需要同時找到現價下方支撐與上方壓力帶"
        return (None, 0, None)

    # 3. 空間保護：區間寬度必須能覆蓋手續費 + 最低獲利空間
    round_trip_fee = TAKER_FEE_RATE * 2
    min_range_width_pct = round_trip_fee + range_min_net_pct
    if support is not None and resistance is not None:
        range_width_pct = (resistance - support) / support if support > 0 else 0.0
        if range_width_pct < min_range_width_pct:
            s["entry_block_reason"] = (
                f"區間寬度 {range_width_pct*100:.2f}% < 最低需求 {min_range_width_pct*100:.2f}%（手續費+獲利空間）"
            )
            logger.info(f"@@COIN_DEBUG@@ ⏳ {sym} [Range] 區間過窄，略過")
            return (None, 0, None)

    # 4. 訊號 K 棒。ETH/XRP 曾出現只靠一根短暫拒跌 K 棒就逆著 15m
    # 趨勢做多，下一根隨即破底；嚴格幣種因此要用前一根作為觸碰／拒絕，
    # 再由最新已收盤 K 棒確認 higher-low + higher-close（做空反向）。
    strict_reversal_confirmation = sym in STRICT_ENTRY_SYMBOLS
    if strict_reversal_confirmation and len(candles) < 23:
        s["entry_block_reason"] = "等待第二根收線確認區間反轉"
        return (None, 0, None)
    sig = candles[-3] if strict_reversal_confirmation else candles[-2]
    confirmation = candles[-2] if strict_reversal_confirmation else None
    candle_open  = float(sig[1])
    candle_high  = float(sig[2])
    candle_low   = float(sig[3])
    candle_close = float(sig[4])
    candle_vol   = float(sig[5])
    volume_ratio = candle_vol / vol_ma20

    touch_band = atr * RANGE_TOUCH_ATR_TOLERANCE
    body = max(abs(candle_close - candle_open), candle_close * 0.0001)
    lower_wick = min(candle_open, candle_close) - candle_low
    upper_wick = candle_high - max(candle_open, candle_close)
    bullish_rejection = candle_close > candle_open or lower_wick >= body * 1.5
    bearish_rejection = candle_close < candle_open or upper_wick >= body * 1.5

    strict_long_confirmation = True
    strict_short_confirmation = True
    if strict_reversal_confirmation:
        ema20_15m = float(s.get("ema20_15m", 0.0) or 0.0)
        ema50_15m = float(s.get("ema50_15m", 0.0) or 0.0)
        if min(ema20_15m, ema50_15m) <= 0:
            s["entry_block_reason"] = "等待 15m EMA20／EMA50 完成，避免把短暫反彈誤認為反轉"
            return (None, 0, None)

        confirm_open = float(confirmation[1])
        confirm_high = float(confirmation[2])
        confirm_low = float(confirmation[3])
        confirm_close = float(confirmation[4])
        strict_long_confirmation = (
            ema20_15m >= ema50_15m
            and confirm_close > confirm_open
            and confirm_close > candle_close
            and confirm_low > candle_low
        )
        strict_short_confirmation = (
            ema20_15m <= ema50_15m
            and confirm_close < confirm_open
            and confirm_close < candle_close
            and confirm_high < candle_high
        )

    # 實測 ADAUSDT 案例：支撐反彈訊號觸發時 RSI=50.0，看似正常，但短短不到
    # 一分鐘內連續幾輪掃描 RSI 一路殺到 33.3、29.4，代表當下賣壓根本還沒停，
    # 進場後價格直接跌破支撐、從未反彈（峰值 0%）。原本只檢查 RSI 是否低於
    # 55/高於 45 這種靜態門檻，抓不到「動能還在惡化中」的情況。這裡改成同時
    # 要求 RSI 沒有在最近一幾輪掃描間快速朝反方向惡化。
    RANGE_RSI_MOMENTUM_GUARD_PCT = 8.0
    rsi_not_still_falling = (prev_rsi - rsi) <= RANGE_RSI_MOMENTUM_GUARD_PCT
    rsi_not_still_rising = (rsi - prev_rsi) <= RANGE_RSI_MOMENTUM_GUARD_PCT

    # 5. 做多條件：低點碰支撐帶 且 收盤回彈至支撐上方 且 RSI < 50 (原55，收緊防追高)
    long_signal = (
        support is not None
        and candle_low <= support + touch_band      # 低點觸碰支撐帶
        and candle_close >= support                  # 收盤回彈至支撐上方
        and bullish_rejection                        # 收陽或更長下影確認拒跌
        and rsi < 50.0                               # 排除偏高位時的假支撐
        and rsi_not_still_falling                     # 排除賣壓還在惡化中的假支撐
        and strict_long_confirmation                  # ETH/XRP：15m 同向且第二根收線確認反轉
    )

    # 6. 做空條件：高點碰壓力帶 且 收盤回落至壓力下方 且 RSI > 50 (原45，收緊防低位做空)
    short_signal = (
        resistance is not None
        and candle_high >= resistance - touch_band   # 高點觸碰壓力帶
        and candle_close <= resistance               # 收盤回落至壓力下方
        and bearish_rejection                        # 收陰或更長上影確認拒漲
        and rsi > 50.0                               # 排除偏低位時的假壓力
        and rsi_not_still_rising                      # 排除買壓還在惡化中的假壓力
        and strict_short_confirmation                 # ETH/XRP：15m 同向且第二根收線確認反轉
    )

    if not long_signal and not short_signal:
        if strict_reversal_confirmation:
            s["entry_block_reason"] = "ETH/XRP 區間反轉未確認：需 15m 趨勢同向及第二根收線形成更高低點／更低高點"
        else:
            s["entry_block_reason"] = "價格未確認觸碰支撐/壓力後回彈/拒絕（區間模式）"
        return (None, 0, None)

    # 確保兩個訊號不會同時成立（優先支撐做多，壓力做空次之）
    if long_signal and short_signal:
        # 價格更接近支撐就做多，更接近壓力就做空
        if support is not None and resistance is not None:
            mid = (support + resistance) / 2
            long_signal  = candle_close <= mid
            short_signal = not long_signal
        else:
            short_signal = False  # 只有支撐時做多

    side  = "buy"  if long_signal  else "sell"
    route = "Range_Support_Long" if long_signal else "Range_Resistance_Short"

    # 7. 計算訊號強度
    # 基礎分：18（剛好達到 RANGE_MIN_SIGNAL_STRENGTH）
    # 加分：量能比例（最多 +5）、RSI 距中線的距離（最多 +3）、ADX 越低區間越穩（最多 +2）
    base_strength = 18.0
    vol_bonus = min(max(volume_ratio - 0.5, 0.0) * 4.0, 5.0)
    rsi_dist  = abs(rsi - 50.0) / 50.0
    rsi_bonus = rsi_dist * 3.0
    adx_bonus = max(0.0, (RANGE_ADX_THRESHOLD - adx) / RANGE_ADX_THRESHOLD) * 2.0
    strength  = round(base_strength + vol_bonus + rsi_bonus + adx_bonus, 4)

    # 8. 將識別到的支撐/壓力寫入狀態，供進場過濾與出場計算使用
    s["range_support_level"]   = support    if support    is not None else 0.0
    s["range_resistance_level"] = resistance if resistance is not None else 0.0

    logger.info(
        f"@@COIN_DEBUG@@ ✅ {sym} [{route}] {side} | close={candle_close:.6f}, "
        f"support={support}, resistance={resistance}, "
        f"ADX={adx:.1f}, RSI={rsi:.1f}, vol={volume_ratio:.2f}x, strength={strength:.2f}"
    )
    return (side, strength, route)
