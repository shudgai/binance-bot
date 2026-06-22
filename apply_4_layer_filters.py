import sys

with open("multi_coin_bot.py", "r", encoding="utf-8") as f:
    code = f.read()

# 1. Update Market Wind (Layer 1)
old_market_wind = """    try:
        # 抓取 BTC 和 ETH
        btc_ohlcv = await exchange.fetch_ohlcv("BTC/USDT", TIMEFRAME, limit=100)
        eth_ohlcv = await exchange.fetch_ohlcv("ETH/USDT", TIMEFRAME, limit=100)
        
        MARKET_WIND["allow_long"] = True
        MARKET_WIND["allow_short"] = True
        
        if len(btc_ohlcv) >= 20:"""

new_market_wind = """    try:
        # 抓取 BTC 和 ETH
        btc_ohlcv = await exchange.fetch_ohlcv("BTC/USDT", TIMEFRAME, limit=100)
        eth_ohlcv = await exchange.fetch_ohlcv("ETH/USDT", TIMEFRAME, limit=100)
        btc_ohlcv_4h = await exchange.fetch_ohlcv("BTC/USDT", '4h', limit=50)
        
        MARKET_WIND["allow_long"] = True
        MARKET_WIND["allow_short"] = True
        
        if len(btc_ohlcv_4h) >= 20:
            btc_closes_4h = [x[4] for x in btc_ohlcv_4h]
            # Simple EMA20 for 4H
            alpha = 2 / 21
            ema = btc_closes_4h[0]
            for val in btc_closes_4h[1:]: ema = alpha * val + (1 - alpha) * ema
            btc_price_4h = btc_closes_4h[-1]
            MARKET_WIND["btc_trend_4h"] = "BULL" if btc_price_4h > ema else "BEAR"
        else:
            MARKET_WIND["btc_trend_4h"] = "NEUTRAL"

        if len(btc_ohlcv) >= 20:"""

code = code.replace(old_market_wind, new_market_wind)

old_market_wind_crash = """        # 1. 瀑布防護 (15m內跌超過1.2%暫停多單，漲超過1.2%暫停空單)
        if btc_change_15m < -0.012 or eth_change_15m < -0.015:
            MARKET_WIND["allow_long"] = False
            print(f"⚠️ [大盤瀑布風控] BTC 15m變動 {btc_change_15m*100:.2f}% | ETH 15m變動 {eth_change_15m*100:.2f}% | 🚫 暫停所有小幣多單開倉！")
        elif btc_change_15m > 0.012 or eth_change_15m > 0.015:
            MARKET_WIND["allow_short"] = False
            print(f"⚠️ [大盤暴漲風控] BTC 15m變動 {btc_change_15m*100:.2f}% | ETH 15m變動 {eth_change_15m*100:.2f}% | 🚫 暫停所有小幣空單開倉！")"""

new_market_wind_crash = """        # 1. 瀑布防護 (極端風暴：2% 震幅)
        if btc_change_15m < -0.02 or eth_change_15m < -0.02:
            MARKET_WIND["allow_long"] = False
            print(f"⚠️ [大盤瀑布風控] BTC 15m變動 {btc_change_15m*100:.2f}% | ETH 15m變動 {eth_change_15m*100:.2f}% | 🚫 暫停所有小幣多單開倉！")
        elif btc_change_15m > 0.02 or eth_change_15m > 0.02:
            MARKET_WIND["allow_short"] = False
            print(f"⚠️ [大盤暴漲風控] BTC 15m變動 {btc_change_15m*100:.2f}% | ETH 15m變動 {eth_change_15m*100:.2f}% | 🚫 暫停所有小幣空單開倉！")"""

code = code.replace(old_market_wind_crash, new_market_wind_crash)

# 2. MTF & Market Wind Filter in check_entries (around line 2736)
old_ai_action_3 = """        # [AI Action 3] 環境過濾：大盤多頭時禁止做空山寨幣
        if side == "sell" and MARKET_WIND.get("btc_trend") == "BULL":
            print(f"🛑 [大盤過濾] {sym} 訊號為空，但 BTC 處於上漲趨勢 (BULL)，禁止逆勢做空！")
            continue"""

new_layer_1_2 = """        # [Layer 1] 大盤過濾 (4H BTC Trend)
        if side == "buy" and MARKET_WIND.get("btc_trend_4h") != "BULL":
            print(f"🛑 [大盤過濾] {sym} 訊號為多，但 BTC 4H 趨勢為 {MARKET_WIND.get('btc_trend_4h')}，禁止做多！")
            continue
        if side == "sell" and MARKET_WIND.get("btc_trend_4h") != "BEAR":
            print(f"🛑 [大盤過濾] {sym} 訊號為空，但 BTC 4H 趨勢為 {MARKET_WIND.get('btc_trend_4h')}，禁止做空！")
            continue
            
        # [Layer 2] MTF 中線與大趨勢過濾
        cp = s["close_price"]
        ema50_1h = s.get("ema50_1h", 0)
        sma200_15m = s.get("sma200_15m", 0)
        
        if side == "buy":
            if ema50_1h > 0 and cp < ema50_1h:
                print(f"🛑 [MTF過濾] {sym} 多單被攔截：價格低於 1H EMA50 ({ema50_1h:.4f})")
                continue
            if sma200_15m > 0 and cp < sma200_15m:
                print(f"🛑 [MTF過濾] {sym} 多單被攔截：價格低於 15m SMA200 ({sma200_15m:.4f})")
                continue
        else: # sell
            if ema50_1h > 0 and cp > ema50_1h:
                print(f"🛑 [MTF過濾] {sym} 空單被攔截：價格高於 1H EMA50 ({ema50_1h:.4f})")
                continue
            if sma200_15m > 0 and cp > sma200_15m:
                print(f"🛑 [MTF過濾] {sym} 空單被攔截：價格高於 15m SMA200 ({sma200_15m:.4f})")
                continue"""

code = code.replace(old_ai_action_3, new_layer_1_2)

# 3. Layer 3: K線結構
old_candle = """                if s["pending_side"] == "buy":
                    # 嚴格要求：確認K線必須是實體綠K(收盤>開盤)，且不能留太長的上影線(上影線 < 實體長度 * 1.5)
                    body = prev_close - prev_open
                    upper_shadow = prev_candle[2] - prev_close
                    if body > 0 and upper_shadow < body * 1.5:
                        is_valid = True
                elif s["pending_side"] == "sell":
                    # 嚴格要求：確認K線必須是實體紅K(開盤>收盤)，且不能留太長的下影線(下影線 < 實體長度 * 1.5)
                    body = prev_open - prev_close
                    lower_shadow = prev_close - prev_candle[3]
                    if body > 0 and lower_shadow < body * 1.5:
                        is_valid = True"""

new_candle = """                if s["pending_side"] == "buy":
                    # [Layer 3] 嚴格K線：實體綠K且上影線 < 實體的 50%
                    body = prev_close - prev_open
                    upper_shadow = prev_candle[2] - prev_close
                    if body > 0 and upper_shadow < body * 0.5:
                        is_valid = True
                elif s["pending_side"] == "sell":
                    # [Layer 3] 嚴格K線：實體紅K且下影線 < 實體的 50%
                    body = prev_open - prev_close
                    lower_shadow = prev_close - prev_candle[3]
                    if body > 0 and lower_shadow < body * 0.5:
                        is_valid = True"""
code = code.replace(old_candle, new_candle)

# Layer 3: Volume Spike confirmation
old_vol = """    # 動態量能門檻：放寬模式 (由 1.1/1.2 調降至 1.0)
    vol_factor = s.get("volume_threshold_factor", 1.0)
    if side == 'sell':
        vol_factor = 1.0"""
new_vol = """    # [Layer 3] 動態量能門檻：嚴格爆發 (至少 1.5 倍)
    vol_factor = max(1.5, s.get("volume_threshold_factor", 1.5))"""
code = code.replace(old_vol, new_vol)

# 4. Layer 4: R:R & Spatial Filter
old_rr = """                    expected_rr = tp_dist / sl_dist if sl_dist > 0 else 0
                    if expected_rr < 1.49:
                        print(f"⚠️ [盈虧比過濾] {sym} 確認階段預期盈虧比 {expected_rr:.2f} < 1.5，放棄")
                        continue"""

new_rr = """                    expected_rr = tp_dist / sl_dist if sl_dist > 0 else 0
                    # 從配置取得盈虧比門檻，若未設定則使用 1.7 作為預設值
                    rr_threshold = COIN_PROFILE_CONFIG.get(sym, {}).get('rr_threshold', 1.7)
                    if expected_rr < rr_threshold:
                        print(f"⚠️ [盈虧比過濾] {sym} 確認階段預期盈虧比 {expected_rr:.2f} < {rr_threshold:.2f}，放棄")
                        continue
                        
                    # [Layer 4] 布林帶空間過濾
                    if side == "buy" and s.get("bb_up", 0) > 0:
                        space = s["bb_up"] - p
                        if space < sl_dist * 0.5:
                            print(f"⚠️ [空間過濾] {sym} 做多距布林上軌僅 {space:.4f} < 0.5*SL({sl_dist*0.5:.4f})，拒絕進場")
                            continue
                    if side == "sell" and s.get("bb_low", 0) > 0:
                        space = p - s["bb_low"]
                        if space < sl_dist * 0.5:
                            print(f"⚠️ [空間過濾] {sym} 做空距布林下軌僅 {space:.4f} < 0.5*SL({sl_dist*0.5:.4f})，拒絕進場")
                            continue"""
code = code.replace(old_rr, new_rr)

with open("multi_coin_bot.py", "w", encoding="utf-8") as f:
    f.write(code)

print("Applied 4-layer multi-confluence entry filter!")
