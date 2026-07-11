import pandas as pd
import pandas_ta as ta
import logging
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)

class StrategyEngine:
    def __init__(self):
        # Parameters for multi-layer filtering
        self.sma_period = 200
        self.atr_period = 14
        self.macd_fast = 12
        self.macd_slow = 26
        self.macd_signal = 9
        self.atr_volatility_threshold = 0.6  # 60% of moving average ATR

    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Calculate all necessary technical indicators using pandas_ta.
        """
        if df.empty:
            return df

        # 1. Long-term Trend (Gatekeeper)
        df['SMA_200'] = ta.sma(df['close'], length=self.sma_period)
        
        # 2. Momentum Indicators (MACD)
        macd_df = ta.macd(df['close'], fast=self.macd_fast, slow=self.macd_slow, signal=self.macd_signal)
        if macd_df is not None:
            df = pd.concat([df, macd_df], axis=1)
            # pandas_ta 產生的欄位名稱固定是 MACDh_{fast}_{slow}_{signal}（histogram），
            # 原本用 'MACD_12_26_9' in col and 'hist' in col.lower() 找欄位，兩個條件都
            # 不成立：欄位是 'MACDh_12_26_9'（h 緊接在 MACD 後面，不含底線，所以不含
            # 'MACD_12_26_9' 這個子字串），且欄位名稱本身沒有 'hist' 這個字樣，導致
            # list index out of range 直接炸掉、check_signals() 從未成功執行過。
            macd_hist_col = f"MACDh_{self.macd_fast}_{self.macd_slow}_{self.macd_signal}"
            if macd_hist_col in df.columns:
                df['macd_hist'] = df[macd_hist_col]
            else:
                df['macd_hist'] = 0.0

        # 3. Volatility (ATR)
        df['ATR'] = ta.atr(df['high'], df['low'], df['close'], length=self.atr_period)
        
        return df

    def check_signals(self, ohlcv_data: List[List[Any]]) -> Optional[tuple]:
        """
        Core signal determination logic with Multi-Layer Filtering.
        
        ohlcv_data: Raw data from CCXT [[timestamp, open, high, low, close, volume], ...]
        Returns: (side, strength) or None
        """
        # Convert raw list to DataFrame
        df = pd.DataFrame(ohlcv_data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        
        # Ensure we have enough data for the SMA200
        if len(df) < self.sma_period + 1:
            return None

        df = self.calculate_indicators(df)

        # --- IMPORTANT: Close Confirmation Mechanism ---
        # We only analyze the last "closed" candle (second to last row).
        # This filters out "fake signals" caused by live bar price fluctuations.
        last_closed_candle = df.iloc[-2] 
        
        current_price = last_closed_candle['close']
        
        # --- Layer 1: Gatekeeper Filtering ---
        # Condition: Price must be above SMA200 to allow LONG signals
        is_bullish_trend = current_price > last_closed_candle['SMA_200']
        
        # --- Layer 2: Momentum & Volatility Filtering ---
        # Condition A: MACD Histogram must be positive (momentum is up)
        is_momentum_positive = last_closed_candle['macd_hist'] > 0
        
        # Condition B: ATR Filter (ensure market is not in "dead" sideways range)
        # Require current ATR to be higher than 60% of the moving average of the last 10 candles
        avg_atr = df['ATR'].iloc[-10:].mean()
        is_volatile_enough = last_closed_candle['ATR'] > (avg_atr * self.atr_volatility_threshold)

        # --- Layer 3: Final Entry Validation ---
        # Only issue BUY if: Trend is up AND Momentum is up AND Volatility is sufficient
        if is_bullish_trend and is_momentum_positive and is_volatile_enough:
            logger.info(f"Signal Passed Filtering: Trend(T), Momentum(T), Volatility(T) | Price: {current_price}")
            # Return a base strength of 20.0 for a confirmed signal
            return ("BUY", 20.0)

        return None
