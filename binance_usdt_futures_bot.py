#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
幣安 U 本位合約機器人 (Binance USDT-Margined Futures Trading Bot)
使用 CCXT 庫連接 Binance USDT Futures API

核心邏輯總結：
1. 多時框對齊：同時檢查 15m (大趨勢) 與 5m (小趨勢)。只有兩者方向一致（例如都是看多）時，才會考慮進場。
2. 實時「往向看」：不再死板地等 K 線收盤。程式會抓取現在這一秒的實時價格 (Ticker)，並與 EMA 均線比較。如果價格現在就在均線上方，就判定趨勢向上。
3. 放寬門檻：
   - RVOL >= 0.60 (降低量能要求)。
   - ADX <= 55.0 (放寬區間模式的趨勢強度限制)。
4. 訊號觸發：在趨勢對齊且量能達標的基礎上，再加上 5m 均線金叉/死叉 以及 RSI 過濾。
"""

import os
import time
import logging
import ccxt
import pandas as pd
import numpy as np
from dotenv import load_dotenv

# 1. 載入環境變數
load_dotenv()

# 設定日誌格式
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("futures_bot_combined.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# 2. 機器人預設參數配置
API_KEY = os.getenv("BINANCE_API_KEY", "YOUR_BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET", "YOUR_BINANCE_API_SECRET")
USE_TESTNET = os.getenv("USE_TESTNET", "true").lower() == "true"

SYMBOL = os.getenv("SYMBOL", "BTC/USDT:USDT")
TIMEFRAME_SHORT = "5m"    # 進場判斷時框
TIMEFRAME_LONG = "15m"    # 大趨勢過濾時框
LEVERAGE = int(os.getenv("LEVERAGE", "5"))
MARGIN_MODE = os.getenv("MARGIN_MODE", "isolated")
TRADE_AMOUNT_USDT = float(os.getenv("TRADE_AMOUNT_USDT", "50.0"))

# --- 風控與策略參數 ---
ATR_PERIOD = 14
ATR_SL_MULT = 1.5
ATR_TP_MULT = 3.0
EMA_FAST = 9
EMA_SLOW = 21
RSI_PERIOD = 14
RSI_BUY_THRESHOLD = 50.0
RSI_SELL_THRESHOLD = 50.0

# --- 放寬參數 ---
MIN_RVOL = 0.60           # 調低 RVOL 門檻
MAX_ADX_FOR_RANGE = 55.0  # 提高 ADX 容忍度


class BinanceFuturesBot:
    def __init__(self, api_key: str, api_secret: str, testnet: bool = True):
        self.testnet = testnet
        self.exchange = ccxt.binance({
            'apiKey': api_key,
            'secret': api_secret,
            'enableRateLimit': True,
            'options': {
                'defaultType': 'future',
                'adjustForTimeDifference': True
            }
        })
        if self.testnet:
            self.exchange.set_sandbox_mode(True)
            logger.info("⚡ 已啟動測試網模式")
        else:
            logger.info("🚀 已啟動實盤交易模式")

    def initialize(self, symbol: str, leverage: int, margin_mode: str):
        try:
            self.exchange.load_markets()
            self.exchange.set_margin_mode(margin_mode.upper(), symbol)
            self.exchange.set_leverage(leverage, symbol)
            logger.info(f"✅ 初始化成功: {symbol} | 槓桿: {leverage}x | 模式: {margin_mode}")
        except Exception as e:
            logger.error(f"❌ 初始化失敗: {e}")
            raise e

    def fetch_ohlcv_dataframe(self, symbol: str, timeframe: str, limit: int = 100) -> pd.DataFrame:
        """獲取數據並計算所有核心指標 (EMA, ATR, RSI, RVOL, ADX)"""
        ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe=timeframe, limit=limit)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')

        # EMA 指標
        df['ema_fast'] = df['close'].ewm(span=EMA_FAST, adjust=False).mean()
        df['ema_slow'] = df['close'].ewm(span=EMA_SLOW, adjust=False).mean()

        # ATR 指標
        high_low = df['high'] - df['low']
        high_close = (df['high'] - df['close'].shift()).abs()
        low_close = (df['low'] - df['close'].shift()).abs()
        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        df['atr'] = tr.rolling(window=ATR_PERIOD).mean()

        # RSI 指標
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=RSI_PERIOD).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=RSI_PERIOD).mean()
        rs = gain / (loss + 1e-10)
        df['rsi'] = 100 - (100 / (1 + rs))

        # RVOL 指標 (當前成交量 / 過去20根平均成交量)
        df['rvol'] = df['volume'] / df['volume'].rolling(window=20).mean()

        # ADX 指標
        df['plus_dm'] = np.where((df['high'].diff() > df['low'].diff()) & (df['high'].diff() > 0), df['high'].diff(), 0)
        df['minus_dm'] = np.where((df['low'].diff() < df['high'].diff()) & (df['low'].diff() < 0), -df['low'].diff(), 0)
        tr_calc = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        sum_plus_dm = df['plus_dm'].rolling(window=14).sum()
        sum_minus_dm = df['minus_dm'].rolling(window=14).sum()
        sum_tr = tr_calc.rolling(window=14).sum()
        df['dx'] = 100 * (sum_plus_dm - sum_minus_dm) / (sum_plus_dm + sum_minus_dm + 1e-10)
        df['adx'] = df['dx'].rolling(window=14).mean()

        return df

    def get_trend_direction(self, symbol: str, timeframe: str) -> str:
        """
        【實作：往向看】
        透過獲取實時 Ticker 價格，並與該時框的 EMA Slow 做比較。
        這確保了判斷是基於「現在這一秒」的動態趨勢。
        """
        df = self.fetch_ohlcv_dataframe(symbol, timeframe, limit=50)
        last_row = df.iloc[-1]

        # 抓取即時價格
        ticker = self.exchange.fetch_ticker(symbol)
        real_time_price = ticker['last']

        if real_time_price > last_row['ema_slow']:
            return 'long'
        elif real_time_price < last_row['ema_slow']:
            return 'short'
        return 'none'

    def get_current_position(self, symbol: str) -> dict:
        positions = self.exchange.fetch_positions([symbol])
        market = self.exchange.market(symbol)
        for pos in positions:
            pos_symbol = pos.get('symbol')
            pos_id = pos.get('info', {}).get('symbol')
            if pos_symbol == symbol or pos_id == market['id']:
                contracts = float(pos['contracts'])
                side = pos['side']
                entry_price = float(pos['entryPrice']) if pos['entryPrice'] else 0.0
                unrealized_pnl = float(pos['unrealizedPnl']) if pos['unrealizedPnl'] else 0.0
                return {'contracts': contracts, 'side': side if contracts > 0 else 'none', 'entry_price': entry_price, 'unrealized_pnl': unrealized_pnl}
        return {'contracts': 0.0, 'side': 'none', 'entry_price': 0.0, 'unrealized_pnl': 0.0}

    def calculate_order_amount(self, symbol: str, usdt_amount: float, current_price: float) -> float:
        notional_value = usdt_amount * LEVERAGE
        # 確保滿足幣安期貨 MIN_NOTIONAL (5.5 USDT) 限制
        if notional_value < 5.5:
            notional_value = 5.5
        raw_quantity = notional_value / current_price
        return float(self.exchange.amount_to_precision(symbol, raw_quantity))

    def execute_trade(self, symbol: str, side: str, amount: float, current_price: float, atr: float):
        logger.info(f"✨ 觸發進場 {side.upper()}，下單數量: {amount}")
        order_side = 'buy' if side == 'long' else 'sell'

        # 1. 執行主單
        try:
            main_order = self.exchange.create_order(symbol=symbol, type='market', side=order_side, amount=amount)
            logger.info(f"✅ 主單成交: {main_order['id']}")
        except Exception as e:
            logger.error(f"❌ 主單開倉失敗！詳細原因: {e}")
            return

        # 2. 計算並掛單 SL/TP (加入重試與詳細日誌機制)
        if side == 'long':
            sl = current_price - (atr * ATR_SL_MULT)
            tp = current_price + (atr * ATR_TP_MULT)
            close_side = 'sell'
        else:
            sl = current_price + (atr * ATR_SL_MULT)
            tp = current_price - (atr * ATR_TP_MULT)
            close_side = 'buy'

        sl_str = self.exchange.price_to_precision(symbol, sl)
        tp_str = self.exchange.price_to_precision(symbol, tp)

        # 重試機制：嘗試掛單最多 3 次
        for i in range(3):
            try:
                self.exchange.create_order(
                    symbol=symbol,
                    type='STOP_MARKET',
                    side=close_side,
                    amount=amount,
                    params={'stopPrice': float(sl_str), 'reduceOnly': True}
                )
                self.exchange.create_order(
                    symbol=symbol,
                    type='TAKE_PROFIT_MARKET',
                    side=close_side,
                    amount=amount,
                    params={'stopPrice': float(tp_str), 'reduceOnly': True}
                )
                logger.info(f"🛡️ 成功掛設止損 {sl_str} / 止盈 {tp_str}")
                break
            except Exception as e:
                logger.warning(f"⚠️ 掛單第 {i+1} 次失敗！詳細原因: {e}")
                time.sleep(1)
        else:
            logger.error("❌ 警告：已嘗試 3 次仍無法掛設止損/止盈單，請檢查精度或最小金額限制！")

    def close_position_market(self, symbol: str, current_side: str, contracts: float):
        if getattr(self, "_is_closing", False):
            logger.warning(f"⚠️ [DuplicateCloseGuard] {symbol} 平倉執行中，忽略重複調用")
            return
        self._is_closing = True
        try:
            try:
                self.exchange.cancel_all_orders(symbol)
            except Exception:
                pass
            close_side = 'sell' if current_side == 'long' else 'buy'
            self.exchange.create_order(symbol=symbol, type='market', side=close_side, amount=contracts, params={'reduceOnly': True})
            logger.info(f"✅ 已平倉 {current_side.upper()}")
        except Exception as e:
            logger.error(f"❌ 市價平倉失敗: {e}")
        finally:
            self._is_closing = False

    def run_strategy(self):
        logger.info(f"🤖 啟動複合策略: {TIMEFRAME_SHORT} 配合 {TIMEFRAME_LONG} (往向看模式)")
        self.initialize(SYMBOL, LEVERAGE, MARGIN_MODE)

        while True:
            try:
                # 1. 多時框趨勢判斷 (實時價格 vs EMA)
                trend_long_15m = self.get_trend_direction(SYMBOL, TIMEFRAME_LONG)
                trend_long_5m = self.get_trend_direction(SYMBOL, TIMEFRAME_SHORT)

                # 2. 獲取 5m 詳細數據用於指標判斷
                df_short = self.fetch_ohlcv_dataframe(SYMBOL, TIMEFRAME_SHORT)
                last_row = df_short.iloc[-1]
                prev_row = df_short.iloc[-2]
                atr = float(last_row['atr'])

                # 獲取實時價格
                ticker = self.exchange.fetch_ticker(SYMBOL)
                real_time_price = ticker['last']

                # 3. 持倉狀態
                pos = self.get_current_position(SYMBOL)

                logger.info(f"📊 [Trend] 15m:{trend_long_15m} | 5m:{trend_long_5m} | Price:{real_time_price} | RVOL:{last_row['rvol']:.2f} | ADX:{last_row['adx']:.2f}")

                # --- 核心策略條件 ---
                if pos['side'] == 'none':
                    # A. 多時框對齊 (15m 和 5m 必須同向)
                    aligned_long = (trend_long_15m == 'long' and trend_long_5m == 'long')
                    aligned_short = (trend_long_15m == 'short' and trend_long_5m == 'short')

                    # B. 放寬參數過濾
                    rvol_ok = last_row['rvol'] >= MIN_RVOL
                    adx_ok = last_row['adx'] <= MAX_ADX_FOR_RANGE

                    # C. 訊號判斷 (金叉/死叉 + RSI)
                    golden_cross = (prev_row['ema_fast'] <= prev_row['ema_slow']) and (last_row['ema_fast'] > last_row['ema_slow'])
                    death_cross = (prev_row['ema_fast'] >= prev_row['ema_slow']) and (last_row['ema_fast'] < last_row['ema_slow'])

                    # D. 防追高/防價差背離 (限制實時價格離 EMA 快線不得超過 0.10%)
                    ema_dev = abs(real_time_price - last_row['ema_fast']) / last_row['ema_fast']
                    deviation_ok = (ema_dev <= 0.0010)

                    # 進場執行
                    if aligned_long and rvol_ok and adx_ok and golden_cross and last_row['rsi'] > RSI_BUY_THRESHOLD:
                        if not deviation_ok:
                            logger.info(f"⏳ 價格離 5m EMA 偏離過大 ({ema_dev*100:.2f}% > 0.10%)，暫停進場以防高點背離")
                        else:
                            logger.info("🟢 條件全達標：多時框對齊 + 量能OK + 金叉 + 價格貼近 EMA")
                            amount = self.calculate_order_amount(SYMBOL, TRADE_AMOUNT_USDT, real_time_price)
                            self.execute_trade(SYMBOL, 'long', amount, real_time_price, atr)

                    elif aligned_short and rvol_ok and adx_ok and death_cross and last_row['rsi'] < RSI_SELL_THRESHOLD:
                        if not deviation_ok:
                            logger.info(f"⏳ 價格離 5m EMA 偏離過大 ({ema_dev*100:.2f}% > 0.10%)，暫停進場以防低點背離")
                        else:
                            logger.info("🔴 條件全達標：多時框對齊 + 量能OK + 死叉 + 價格貼近 EMA")
                            amount = self.calculate_order_amount(SYMBOL, TRADE_AMOUNT_USDT, real_time_price)
                            self.execute_trade(SYMBOL, 'short', amount, real_time_price, atr)

                    elif not rvol_ok:
                        logger.warning(f"⚠️ 跳過：量能不足 (RVOL={last_row['rvol']:.2f})")
                    elif not adx_ok:
                        logger.warning(f"⚠️ 跳過：ADX過高 ({last_row['adx']:.2f})")

                # 4. 平倉判斷
                elif pos['side'] == 'long' and death_cross:
                    logger.info("📉 多單遇死叉，平倉")
                    self.close_position_market(SYMBOL, 'long', pos['contracts'])
                elif pos['side'] == 'short' and golden_cross:
                    logger.info("📈 空單遇金叉，平倉")
                    self.close_position_market(SYMBOL, 'short', pos['contracts'])

            except Exception as e:
                logger.error(f"❌ 運行異常: {e}", exc_info=True)

            time.sleep(10)


if __name__ == "__main__":
    bot = BinanceFuturesBot(API_KEY, API_SECRET, USE_TESTNET)
    bot.run_strategy()
