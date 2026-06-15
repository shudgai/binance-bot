import pandas as pd
import numpy as np
import os
import itertools
from datetime import datetime

# ==========================================
# 1. Indicator Calculations
# ==========================================
def calculate_rsi(series, period=14):
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.rolling(window=period, min_periods=period).mean()
    avg_loss = loss.rolling(window=period, min_periods=period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calculate_macd(series, fast=12, slow=26, signal=9):
    exp1 = series.ewm(span=fast, adjust=False).mean()
    exp2 = series.ewm(span=slow, adjust=False).mean()
    macd = exp1 - exp2
    sig = macd.ewm(span=signal, adjust=False).mean()
    return macd, sig

def calculate_bb(series, period=20, std=2):
    ma = series.rolling(window=period).mean()
    sd = series.rolling(window=period).std()
    return ma + (sd * std), ma - (sd * std)

def calculate_atr(df, period=14):
    high_low = df['high'] - df['low']
    high_close = np.abs(df['high'] - df['close'].shift())
    low_close = np.abs(df['low'] - df['close'].shift())
    ranges = pd.concat([high_low, high_close, low_close], axis=1)
    true_range = np.max(ranges, axis=1)
    return true_range.rolling(period).mean()

# ==========================================
# 2. Copied Bot Logic (compute_signal_strength & helpers)
# ==========================================
class MockState:
    def __init__(self):
        pass

MARKET_WIND = {}
STATES = {}

STRATEGY_CONF = {
    "ATR_SPIKE_MULTIPLIER": 1.5,
    "FAST_PATH_SCORE_LOW_VOL": 14.0,
    "FAST_PATH_SCORE_HIGH_VOL": 16.0,
    "FAST_PATH_REQUIRE_4H": False
}

class DummyLogger:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass

logger = DummyLogger()

def check_rsi_divergence(closes, rsis, window=60):
    if len(closes) < window or len(rsis) < window:
        return False, False
        
    recent_closes = closes[-window:]
    recent_rsis = rsis[-window:]
    
    # 看漲背離 (Bullish Divergence)
    bullish_div = False
    current_close = recent_closes[-1]
    current_rsi = recent_rsis[-1]
    
    if len(recent_closes[:-5]) > 0:
        lowest_idx = np.argmin(recent_closes[:-5])
        prev_low_close = recent_closes[lowest_idx]
        prev_low_rsi = recent_rsis[lowest_idx]
        
        if current_close < prev_low_close * 0.998 and current_rsi > prev_low_rsi + 5.0 and current_rsi < 45.0:
            bullish_div = True
            
    # 看跌背離 (Bearish Divergence)
    bearish_div = False
    if len(recent_closes[:-5]) > 0:
        highest_idx = np.argmax(recent_closes[:-5])
        prev_high_close = recent_closes[highest_idx]
        prev_high_rsi = recent_rsis[highest_idx]
        
        if current_close > prev_high_close * 1.002 and current_rsi < prev_high_rsi - 5.0 and current_rsi > 55.0:
            bearish_div = True
            
    return bullish_div, bearish_div

def compute_signal_strength(sym):
    s = STATES[sym]
    ohlcv = getattr(s, "ohlcv", None) or []
    if len(s.closes) > 0:
        closes = np.array(s.closes, dtype=float)
    elif ohlcv:
        closes = np.array([x[4] for x in ohlcv], dtype=float)
    else:
        closes = np.array([], dtype=float)
        
    if len(closes) < 20 and not ohlcv and s.close_price == 0.0 and (s.prev_close is None or s.prev_close == 0.0):
        return (None, 0, None, False)
        
    if len(closes) >= 20:
        s.closes = closes

    rsi = s.current_rsi
    rsis = s.rsis
    if len(rsis) == 0:
        rsis = [rsi]
        
    close = s.close_price
    prev_close = s.prev_close if s.prev_close is not None else close
    ema20 = s.ema20
    ema50 = s.ema50

    trend_long = ema20 > 0 and close > ema20
    trend_short = ema20 > 0 and close < ema20

    LONG_RSI_NORMAL = 42.0
    SHORT_RSI_NORMAL = 58.0
    LONG_RSI_HIGH_VOL = 38.0
    SHORT_RSI_HIGH_VOL = 62.0

    atr_24h_avg = getattr(s, "atr_24h_avg", 0.0)
    current_atr = s.current_atr

    if current_atr > atr_24h_avg * STRATEGY_CONF["ATR_SPIKE_MULTIPLIER"] and atr_24h_avg > 0:
        long_rsi_threshold = LONG_RSI_HIGH_VOL
        short_rsi_threshold = SHORT_RSI_HIGH_VOL
        vol_mode = "高波動模式 (High Vol)"
    else:
        long_rsi_threshold = LONG_RSI_NORMAL
        short_rsi_threshold = SHORT_RSI_NORMAL
        vol_mode = "低波動模式 (Low Vol)"
    s.vol_mode = vol_mode

    is_in_bb_zone_long = close <= s.bb_low * 1.005
    is_in_bb_zone_short = close >= s.bb_up * 0.995
    
    macd_line = s.macd_line
    macd_signal = s.macd_signal
    prev_macd_line = s.prev_macd_line
    prev_macd_signal = s.prev_macd_signal
    
    macd_hist = macd_line - macd_signal
    prev_macd_hist = prev_macd_line - prev_macd_signal
    
    long_macd_cross = prev_macd_line <= prev_macd_signal and macd_line > macd_signal
    short_macd_cross = prev_macd_line >= prev_macd_signal and macd_line < macd_signal
    
    long_macd_hist_aligned = macd_hist > prev_macd_hist
    short_macd_hist_aligned = macd_hist < prev_macd_hist
    
    long_macd_ok = long_macd_cross or long_macd_hist_aligned
    short_macd_ok = short_macd_cross or short_macd_hist_aligned

    last_candle_confirmed_long = (
        len(s.ohlcv) >= 3 and 
        s.ohlcv[-1][4] > s.ohlcv[-2][4] and
        s.ohlcv[-2][4] > s.ohlcv[-3][4]
    )
    last_candle_confirmed_short = (
        len(s.ohlcv) >= 3 and 
        s.ohlcv[-1][4] < s.ohlcv[-2][4] and
        s.ohlcv[-2][4] < s.ohlcv[-3][4]
    )

    ema50 = s.ema50
    trend_confluence_long = ema50 == 0.0 or close > ema50
    trend_confluence_short = ema50 == 0.0 or close < ema50

    is_above_sma200 = s.sma200_15m > 0 and close > s.sma200_15m * 0.999
    is_below_sma200 = s.sma200_15m > 0 and close < s.sma200_15m * 1.001

    rsi_rising = len(rsis) >= 2 and rsis[-1] > rsis[-2]
    rsi_falling = len(rsis) >= 2 and rsis[-1] < rsis[-2]

    route_a_long = (is_above_sma200 or trend_long) and (long_macd_cross or long_macd_hist_aligned) and last_candle_confirmed_long
    route_a_short = (is_below_sma200 or trend_short) and (short_macd_cross or short_macd_hist_aligned) and last_candle_confirmed_short

    route_b_long = rsi < 30.0 and is_in_bb_zone_long
    route_b_short = rsi > 70.0 and is_in_bb_zone_short

    momentum_long = close > prev_close * 1.001 and (s.current_vol >= max(1000.0, getattr(s, "vol_ma20", 0) * 1.2) or getattr(s, "trade_signal_strength", 0) > 0.2)
    momentum_short = close < prev_close * 0.999 and (s.current_vol >= max(1000.0, getattr(s, "vol_ma20", 0) * 1.2) or getattr(s, "trade_signal_strength", 0) > 0.2)

    route_c_long = rsi <= 20.0 and last_candle_confirmed_long
    route_c_short = rsi >= 80.0 and last_candle_confirmed_short

    bullish_div, bearish_div = check_rsi_divergence(s.closes, s.rsis, window=60)
    route_s_long = bullish_div and last_candle_confirmed_long
    route_s_short = bearish_div and last_candle_confirmed_short

    left_side_positions = 0 

    right_side_long = route_a_long or (momentum_long and last_candle_confirmed_long and (long_macd_cross or long_macd_hist_aligned) and trend_long)
    right_side_short = route_a_short or (momentum_short and last_candle_confirmed_short and (short_macd_cross or short_macd_hist_aligned) and trend_short)

    is_rsi_safe_long = rsi < 80.0
    is_rsi_safe_short = rsi > 20.0
    
    if right_side_long and not is_rsi_safe_long: right_side_long = False
    if right_side_short and not is_rsi_safe_short: right_side_short = False

    left_side_long = route_s_long or route_b_long or route_c_long
    left_side_short = route_s_short or route_b_short or route_c_short

    long_base_ok = right_side_long or left_side_long
    short_base_ok = right_side_short or left_side_short

    long_score = 0.0
    short_score = 0.0
    long_route = None
    short_route = None
    long_details = {}
    short_details = {}

    if long_base_ok:
        long_route = "s" if route_s_long else "c" if route_c_long else "b" if route_b_long else "a"
        if long_route == "a":
            base_score = 4.0 + ((close - ema20) / max(ema20, 1e-8) * 100) + 10.0
            long_score = base_score
        else:
            rsi_bonus = max(0.0, long_rsi_threshold - rsi)
            if getattr(s, "vol_mode", "") == "低波動模式 (Low Vol)":
                rsi_bonus *= 0.5
            long_score = rsi_bonus + 4.0
        if momentum_long: long_score += 3.0
        if long_macd_cross: long_score += 5.0

    if short_base_ok:
        short_route = "s" if route_s_short else "c" if route_c_short else "b" if route_b_short else "a"
        if short_route == "a":
            base_score = 4.0 + ((ema20 - close) / max(ema20, 1e-8) * 100) + 10.0
            short_score = base_score
        else:
            rsi_bonus = max(0.0, rsi - short_rsi_threshold)
            if getattr(s, "vol_mode", "") == "低波動模式 (Low Vol)":
                rsi_bonus *= 0.5
            short_score = rsi_bonus + 4.0
        if momentum_short: short_score += 3.0
        if short_macd_cross: short_score += 5.0

    if long_score == 0 and short_score == 0:
        return (None, 0.0, None, False)

    current_atr = getattr(s, "current_atr", 0.0)
    atr_ma20 = getattr(s, "atr_ma20", current_atr)
    if atr_ma20 > 0:
        atr_modifier = 0.0
        if current_atr > atr_ma20:
            atr_modifier = 3.0
        elif current_atr < atr_ma20 * 0.8:
            atr_modifier = -5.0
            
        if long_score > 0: long_score = max(0.0, long_score + atr_modifier)
        if short_score > 0: short_score = max(0.0, short_score + atr_modifier)

    if long_score == 0 and short_score == 0:
        if long_base_ok and not short_base_ok:
            side, strength, route = "buy", 0.0, long_route
        elif short_base_ok and not long_base_ok:
            side, strength, route = "sell", 0.0, short_route
        else:
            return (None, 0.0, None, False)
    else:
        if short_score > long_score:
            side, strength, route = "sell", short_score, short_route
        else:
            side, strength, route = "buy", long_score, long_route

    fng = MARKET_WIND.get("fng_value", 50)
    if fng > 75:
        offset = 1.0 if side == 'sell' else -1.0
        strength += offset
    elif fng < 25:
        offset = 1.0 if side == 'buy' else -1.0
        strength += offset

    if getattr(s, "vol_mode", "") == "低波動模式 (Low Vol)":
        strength -= 2.0
        
    if getattr(s, "atr_ma20", 0) > 0 and getattr(s, "current_atr", 0) > getattr(s, "atr_ma20", 0) * 1.2:
        strength += 3.0

    fast_path_threshold = STRATEGY_CONF["FAST_PATH_SCORE_LOW_VOL"] if getattr(s, "vol_mode", "") == "低波動模式 (Low Vol)" else STRATEGY_CONF["FAST_PATH_SCORE_HIGH_VOL"]
    expected_trend = "long" if side == "buy" else "short"
    
    btc_change = MARKET_WIND.get("btc_change_15m", 0.0)
    if btc_change > 0.005 and side == "buy":
        return (side, max(strength, fast_path_threshold), route, False)
    elif btc_change < -0.005 and side == "sell":
        return (side, max(strength, fast_path_threshold), route, False)

    if getattr(s, "avg_vol_24h_1m", 0) > 0 and s.current_vol > getattr(s, "avg_vol_24h_1m", 0) * 3.0:
        if len(s.closes) >= 2:
            c_change = abs(s.closes[-1] - s.closes[-2]) / max(s.closes[-2], 1e-8)
            if c_change > 0.002:
                return (side, max(strength, fast_path_threshold), route, False)

    if s.vwap and s.vwap > 0:
        if side == "buy" and s.close_price < s.vwap:
            strength -= 1.0
        elif side == "sell" and s.close_price > s.vwap:
            strength -= 1.0

    if side == "buy" and not trend_confluence_long:
        strength -= 1.5
    elif side == "sell" and not trend_confluence_short:
        strength -= 1.5

    return (side, strength, route, False)

# ==========================================
# 3. Backtest Engine Loop
# ==========================================
def run_backtest(df_dict, btc_df, eth_df, params):
    prev_vol_multi = params['prev_vol_multi']
    waterfall_btc_thresh = params['waterfall_btc']
    waterfall_eth_thresh = params['waterfall_eth']
    
    total_trades = 0
    total_wins = 0
    total_pnl = 0.0
    max_drawdown = 0.0
    peak_pnl = 0.0
    
    btc_ts = btc_df['timestamp'].values
    btc_close = btc_df['close'].values
    eth_ts = eth_df['timestamp'].values
    eth_close = eth_df['close'].values
    
    for sym, df in df_dict.items():
        ts = df['timestamp'].values
        close_arr = df['close'].values
        vol_arr = df['volume'].values
        rsi_arr = df['rsi'].values
        macd_arr = df['macd'].values
        macd_sig_arr = df['macd_sig'].values
        bb_upper_arr = df['bb_upper'].values
        bb_lower_arr = df['bb_lower'].values
        vol_ma20_arr = df['vol_ma20'].values
        ema20_arr = df['ema20'].values
        ema50_arr = df['ema50'].values
        atr_arr = df['atr'].values
        atr_ma20_arr = df['atr_ma20'].values
        sma200_15m_arr = df['sma600'].values # Approximated SMA200 of 15m
        avg_vol_24h_arr = df['volume'].rolling(288).mean().values # 288 * 5m = 24h
        
        # We need an array of ohlcv tuples to mock s.ohlcv
        ohlcv_arr = df[['timestamp', 'open', 'high', 'low', 'close', 'volume']].values
        
        in_position = False
        entry_price = 0.0
        entry_side = None
        candles_held = 0
        
        s = MockState()
        s.vwap = 0.0 # Disabled for simplification
        STATES[sym] = s
        
        for i in range(100, len(df)):
            if in_position:
                candles_held += 1
                if candles_held >= 10:
                    exit_price = close_arr[i]
                    pnl_pct = (exit_price - entry_price) / entry_price if entry_side == 'long' else (entry_price - exit_price) / entry_price
                    pnl_pct -= 0.001
                    total_pnl += pnl_pct
                    total_trades += 1
                    if pnl_pct > 0:
                        total_wins += 1
                    
                    if total_pnl > peak_pnl:
                        peak_pnl = total_pnl
                    drawdown = peak_pnl - total_pnl
                    if drawdown > max_drawdown:
                        max_drawdown = drawdown
                        
                    in_position = False
                continue
            
            timestamp = ts[i]
            btc_idx = np.searchsorted(btc_ts, timestamp, side='right') - 1
            eth_idx = np.searchsorted(eth_ts, timestamp, side='right') - 1
            
            btc_waterfall = False
            btc_change_15m = 0.0
            if btc_idx >= 15:
                btc_c_0 = btc_close[btc_idx]
                btc_c_1 = btc_close[btc_idx - 1]
                btc_change_15m = (btc_c_0 - btc_c_1) / btc_c_1
                if btc_change_15m < waterfall_btc_thresh:
                    btc_waterfall = True
            
            eth_waterfall = False
            if eth_idx >= 15:
                eth_c_0 = eth_close[eth_idx]
                eth_c_1 = eth_close[eth_idx - 1]
                if (eth_c_0 - eth_c_1) / eth_c_1 < waterfall_eth_thresh:
                    eth_waterfall = True
            
            MARKET_WIND["btc_change_15m"] = btc_change_15m
            MARKET_WIND["fng_value"] = 50 # Neutral FNG

            # Setup the mocked state for this K-line
            s.ohlcv = ohlcv_arr[i-5:i+1].tolist()
            s.closes = close_arr[i-100:i+1].tolist()
            s.rsis = rsi_arr[i-100:i+1].tolist()
            s.close_price = close_arr[i]
            s.prev_close = close_arr[i-1]
            s.ema20 = ema20_arr[i]
            s.ema50 = ema50_arr[i]
            s.current_rsi = rsi_arr[i]
            s.current_atr = atr_arr[i]
            s.atr_24h_avg = atr_arr[i-288:i].mean() if i >= 288 else atr_arr[i]
            s.atr_ma20 = atr_ma20_arr[i]
            s.bb_low = bb_lower_arr[i]
            s.bb_up = bb_upper_arr[i]
            s.macd_line = macd_arr[i]
            s.macd_signal = macd_sig_arr[i]
            s.prev_macd_line = macd_arr[i-1]
            s.prev_macd_signal = macd_sig_arr[i-1]
            s.sma200_15m = sma200_15m_arr[i]
            s.current_vol = vol_arr[i]
            s.vol_ma20 = vol_ma20_arr[i]
            s.avg_vol_24h_1m = avg_vol_24h_arr[i] / 5.0 # Approx 1m vol
            
            side, strength, route, _ = compute_signal_strength(sym)
            
            if side == "buy" and strength >= 7.5:
                # 瀑布防護：多單攔截
                if btc_waterfall or eth_waterfall:
                    continue
                
                # PrevVol 過濾器
                vol_multi = prev_vol_multi if s.vol_mode == "高波動模式 (High Vol)" else (prev_vol_multi + 0.5)
                vol_req = s.vol_ma20 * vol_multi
                prev_vol = vol_arr[i-1]
                if prev_vol < vol_req:
                    continue
                
                # ENTER LONG
                in_position = True
                entry_price = close_arr[i]
                entry_side = 'long'
                candles_held = 0

            elif side == "sell" and strength >= 7.5:
                # 暫時不跑空單回測，或者如果想一起測可以開放
                pass
                        
    win_rate = (total_wins / total_trades * 100) if total_trades > 0 else 0
    return {
        'PrevVol Multi': f"{prev_vol_multi}x",
        'Waterfall': f"{waterfall_btc_thresh*100:.1f}%",
        'Trades': total_trades,
        'Win Rate (%)': f"{win_rate:.1f}%",
        'Total PnL (%)': f"{total_pnl*100:.2f}%",
        'Max DD (%)': f"{max_drawdown*100:.2f}%"
    }

def main():
    print("Loading CRASH data...")
    btc_df = pd.read_csv('crash_data/BTCUSDT_15m.csv', parse_dates=['timestamp'])
    eth_df = pd.read_csv('crash_data/ETHUSDT_15m.csv', parse_dates=['timestamp'])
    
    btc_df = btc_df.sort_values('timestamp').reset_index(drop=True)
    eth_df = eth_df.sort_values('timestamp').reset_index(drop=True)
    
    altcoins = ['WLDUSDT', 'SOLUSDT', '1000PEPEUSDT']
    df_dict = {}
    
    for sym in altcoins:
        df = pd.read_csv(f'crash_data/{sym}_5m.csv', parse_dates=['timestamp'])
        df = df.sort_values('timestamp').reset_index(drop=True)
        
        # Calculate Indicators
        df['rsi'] = calculate_rsi(df['close'], 14)
        df['macd'], df['macd_sig'] = calculate_macd(df['close'])
        df['bb_upper'], df['bb_lower'] = calculate_bb(df['close'], 20, 2)
        df['vol_ma20'] = df['volume'].rolling(20).mean()
        df['ema20'] = df['close'].ewm(span=20, adjust=False).mean()
        df['ema50'] = df['close'].ewm(span=50, adjust=False).mean()
        df['sma600'] = df['close'].rolling(600).mean() # SMA200 of 15m approx
        df['atr'] = calculate_atr(df, 14)
        df['atr_ma20'] = df['atr'].rolling(20).mean()
        
        # Drop initial NaNs to avoid errors
        df = df.dropna().reset_index(drop=True)
        df_dict[sym] = df
        
    print("Running backtest for CRASH period (Fixed PrevVol=2.0x)...")
    
    vol_multis = [2.0]
    waterfall_threshs = [-0.003, -0.005, -0.008, -0.010]
    
    results = []
    
    for vol, wf in itertools.product(vol_multis, waterfall_threshs):
        params = {
            'prev_vol_multi': vol,
            'waterfall_btc': wf,
            'waterfall_eth': wf - 0.001
        }
        res = run_backtest(df_dict, btc_df, eth_df, params)
        results.append(res)
        
    # Formatting Results
    res_df = pd.DataFrame(results)
    
    pd.set_option('display.max_columns', None)
    pd.set_option('display.width', 1000)
    print("\n--- Crash Period Backtest Table ---")
    print(res_df.to_string(index=False))
    
    res_df.to_csv('crash_backtest_results.csv', index=False)
    print("\nResults saved to 'crash_backtest_results.csv'")

if __name__ == '__main__':
    main()
