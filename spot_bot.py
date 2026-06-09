import asyncio
import os
import ccxt.pro as ccxtpro  # 使用 CCXT 的 WebSocket 版本
import numpy as np
import sys                  # 用於觸發風控時關閉程式
import argparse             # 用於接收命令列參數
import time                 # 用於計時
from update_paper_state import update_paper_state
from dotenv import load_dotenv

# 載入 .env 環境變數
load_dotenv()

# =====================================================================
# 帳號與參數設定區（方案 A：直接交易 BNB 合約）
# =====================================================================
exchange = ccxtpro.binance({
    'apiKey': os.getenv('BINANCE_API_KEY') or None,     # 從 .env 讀取 API Key
    'secret': os.getenv('BINANCE_API_SECRET') or None,   # .env 讀取 Secret Key
    'enableRateLimit': True,
    'options': {
        'defaultType': 'future',  # 強制使用合約交易
    },
})
USE_TESTNET = os.getenv("USE_TESTNET", "True").lower() in ("true", "1", "yes")
PAPER_TRADING = not bool(os.getenv('BINANCE_API_KEY'))

if USE_TESTNET:
    # 繞過 CCXT 的 set_sandbox_mode 棄用限制，手動覆寫合約測試網 URL
    exchange.urls['api']['fapiPublic'] = 'https://testnet.binancefuture.com/fapi/v1'
    exchange.urls['api']['fapiPrivate'] = 'https://testnet.binancefuture.com/fapi/v1'

if PAPER_TRADING:
    print(f"⚠️ [模式設定] 啟動純數據模擬模式 (Paper Trading) | 連線主網: {not USE_TESTNET}")
else:
    print(f"⚠️ [模式設定] 啟動真實交易模式 | 連線主網: {not USE_TESTNET}")

# 接收命令列參數
parser = argparse.ArgumentParser()
parser.add_argument('--symbol', type=str, default='SOL/USDT', help='Trading symbol (e.g. SOL/USDT)')
parser.add_argument('--amount', type=float, default=10.0, help='Trade amount in quote currency (USDT)')
args = parser.parse_args()

# 交易設定
symbol = args.symbol                      # 透過參數決定的交易對
if not symbol.endswith(':USDT') and '/USDT' in symbol:
    symbol = symbol + ':USDT'             # 確保使用合約幣對格式
timeframe = '1m'                          # 1分鐘K線
quote_amount = args.amount                # 下單金額 (USDT)

# --- 交易與風控參數 ---
ATR_PERIOD = 14                       # ATR 計算週期
ORDER_BOOK_THRESHOLD_USD = 100000.0   # 盤口大單追蹤門檻 (10萬美金)
ATR_TP_MULTIPLIER = 0.0       # 取消動態放大，只求最快平倉
ATR_SL_MULTIPLIER = 1.5       # 止損距離 (ATR 倍數)
MIN_TP_PCT = 0.0015     # 全局最低停利標準：0.15%
MIN_SL_PCT = 0.05       # 止損 3%
SWING_TP_PCT = 0.008     # 波段目標 0.8%
current_atr = 0.0                         # 當前 ATR 值（由 K線模組更新）
RSI_PERIOD = 14                           # RSI 計算週期
RSI_OVERBOUGHT = 70                       # RSI 超買門檻（高於此不買入）
current_rsi = 50.0                        # 當前 RSI 值
macro_regime = "猴市 (區間震盪)"          # 全局大趨勢狀態
default_amount = 150.0

# 🎯 物理防火牆：每日最大虧損限額設定
INITIAL_BALANCE = 150.0                   # 你的總本金 150 USDT
MAX_DAILY_LOSS_PCT = 0.15                 # 每日最大容忍虧損 5%
BALANCE_STOP_LINE = INITIAL_BALANCE * (1 - MAX_DAILY_LOSS_PCT)


# =====================================================================
# 安全防護模組：動態檢查當前持倉與帳戶總資產
# =====================================================================
async def check_account_safety():
    """ 檢查帳戶總餘額，若跌破防禦線則立刻終止程式 """
    if PAPER_TRADING:
        return
    try:
        # 獲取帳戶全資產資訊
        balance_info = await exchange.fetch_balance()
        # 取得當前 U本位合約帳戶的總權益 (Total Wallet Balance + Unreallized PnL)
        current_wallet_balance = float(balance_info['total'].get('USDT', 0))
        
        # 判斷是否跌破防禦線
        if current_wallet_balance <= BALANCE_STOP_LINE:
            print(f"\n🚨🚨 [風控斷路器觸發] 🚨🚨")
            print(f"⚠️ 當前帳戶總資產為: {current_wallet_balance:.2f} USDT")
            print(f"⚠️ 已跌破每日防禦底線: {BALANCE_STOP_LINE:.2f} USDT (虧損達 15%)")
            print(f"🛑 為了保護剩餘的 85% 本金，程式現在執行「物理斷電」強制關機！")
            sys.exit(0)  # 徹底關閉 Python 程式
            
    except SystemExit:
        sys.exit(0)
    except Exception as e:
        print(f"⚠️ [風控模組] 讀取資產線失敗: {e}，暫時放行檢查。")

async def get_current_price():
    ticker = await exchange.fetch_ticker(symbol)
    return float(ticker['last'])

# 合約精度資訊快取 (LOT_SIZE stepSize / PRICE_FILTER tickSize)
_contract_precision = None

async def get_contract_precision():
    global _contract_precision
    if _contract_precision is not None:
        return _contract_precision
    try:
        markets = await exchange.load_markets()
        market = markets.get(symbol)
        if market:
            info = market.get('info', {})
            filters = {f['filterType']: f for f in info.get('filters', [])}
            ls = filters.get('LOT_SIZE', {})
            pf = filters.get('PRICE_FILTER', {})
            _contract_precision = {
                'step_size': float(ls.get('stepSize', 0.001)),
                'min_qty': float(ls.get('minQty', 0.001)),
                'tick_size': float(pf.get('tickSize', 0.001)),
            }
            return _contract_precision
    except Exception as e:
        print(f"⚠️ 讀取合約精度失敗: {e}")
    return {'step_size': 0.001, 'min_qty': 0.001, 'tick_size': 0.001}

def round_step(qty, step_size):
    """將數量取整到交易所要求的 stepSize"""
    precision = int(round(-np.log10(step_size)))
    return round(round(qty / step_size) * step_size, precision)

def round_price(price, tick_size):
    """將價格取整到交易所要求的 tickSize"""
    precision = int(round(-np.log10(tick_size)))
    return round(round(price / tick_size) * tick_size, precision)

async def get_base_amount(amt_usd):
    price = await get_current_price()
    raw_qty = amt_usd / price
    prec = await get_contract_precision()
    return round_step(raw_qty, prec['step_size'])

simulated_base_amt = 0.0
latest_ws_price = 0.0
current_pos_qty = 0.0
current_pos_avg = 0.0
position_open_time = 0.0  # 記錄開倉時間，防止動態平倉秒砍


# 統一 symbol 格式：BNB/USDT:USDT -> BNB:USDT (與手動下單格式一致)
SYMBOL_KEY = symbol.replace('/', '').replace(':USDT', '').replace('USDT', '') + ':USDT'

# 雷達換倉：追蹤最後活動時間
import sys
last_action_time = time.time()

# 移動停利狀態變數
trailing_highest = 0.0
trailing_lowest = float('inf')

# 合約槓桿倍數 (由 detect_regime 根據 1h 偏離度動態調整)
LEVERAGE = 5.0
current_1h_deviation = 0.0

async def update_dynamic_leverage():
    global LEVERAGE, current_1h_deviation, macro_regime
    abs_dev = abs(current_1h_deviation)
    old = LEVERAGE
    if abs_dev > 0.20:
        LEVERAGE = 2
    elif abs_dev > 0.10:
        LEVERAGE = 5
    else:
        LEVERAGE = 10
    if LEVERAGE != old:
        print(f"⚙️ [動態槓桿] 偏離度={current_1h_deviation*100:.1f}% → 調整為 {LEVERAGE}x")
        if not PAPER_TRADING:
            try:
                await exchange.set_leverage(int(LEVERAGE), symbol)
                print(f"   ✅ 已同步更新交易所槓桿為 {int(LEVERAGE)}x")
            except Exception as e:
                print(f"   ⚠️ 同步槓桿失敗: {e}")

async def initialize_simulated_position():
    global simulated_base_amt, position_open_time
    if PAPER_TRADING:
        try:
            import json
            import os
            if os.path.exists("paper_state.json"):
                with open("paper_state.json", "r") as f:
                    state = json.load(f)
                    pos = state.get("positions", {}).get(SYMBOL_KEY, {})
                    simulated_base_amt = float(pos.get("qty", 0.0))
                    if abs(simulated_base_amt) > 0.000001:
                        position_open_time = time.time()
                    print(f"📦 [系統初始化] 從歷史紀錄恢復模擬倉位: {simulated_base_amt} 顆")
        except Exception as e:
            print(f"⚠️ [系統初始化] 無法讀取歷史倉位，預設為 0: {e}")

MAX_POSITION_USD = 150.0

async def has_reached_position_limit():
    """ 檢查當前帳戶持倉是否已達 150 USD 上限 """
    if PAPER_TRADING:
        price = await get_current_price()
        return (abs(simulated_base_amt) * price) >= (MAX_POSITION_USD * 0.95)
    try:
        positions = await exchange.fetch_positions([symbol])
        if positions:
            pos = positions[0]
            notional = abs(float(pos.get('notional', 0)))
            if notional == 0:
                price = await get_current_price()
                notional = abs(float(pos.get('contracts', 0))) * price
            if notional >= (MAX_POSITION_USD * 0.95):
                return True
        return False
    except Exception as e:
        print(f"⚠️ [防護模組] 檢查持倉上限失敗，預設視為已達上限: {e}")
        return True


# =====================================================================
# ③ 下單與風控模組 (Execution & Risk Control)
# =====================================================================
order_lock = asyncio.Lock()
is_ordering = False

async def execute_order_and_risk(side, price):
    global simulated_base_amt, simulated_avg_price, current_atr, is_ordering, position_open_time
    
    if side == 'sell' and simulated_base_amt <= 0:
        print('現貨模式不支援做空，忽略此訊號')
        return
    
    # 防止並發開倉：同時間只允許一筆訂單執行
    if is_ordering:
        print(f"⚠️ [並發防護] 已有訂單在執行，跳過")
        return
    is_ordering = True
    try:
    
        # 1. 每次準備下單前，先檢查今天是不是虧太多了
        await check_account_safety()
        
        # 2. 安全防護：檢查是否有持倉上限
        current_p = await get_current_price()
        current_balance = 150.0
        if PAPER_TRADING:
            try:
                import json
                with open("paper_state.json", "r") as f:
                    state = json.load(f)
                    current_balance = float(state.get("balance_usdt", 150.0))
            except:
                pass
        
        # 動態持倉上限 = 當前總資金 (實現複利滾存) * 槓桿倍數
        dynamic_max_position = current_balance
    
        # 計算剩餘可下單額度 (扣除已使用保證金的槓桿部位)
        current_position_usd = abs(simulated_base_amt) * current_p
        available_margin = dynamic_max_position - current_position_usd
    
        if available_margin <= 0:
            print(f"⚠️ [風控攔截] 模擬倉位已達上限 {dynamic_max_position:.2f} USDT，暫停加倉！")
            return
    
        # 依照使用者要求：帳面有多少就下多少單 (複利 All-in)
        # 使用可用餘額的 95% 作為開倉額度，預留 5% 作為緩衝避免因市價滑點而保證金不足
        actual_quote_amount = available_margin * 0.95
        
        if actual_quote_amount < 1.0:
            print(f"⚠️ [額度限制] 剩餘可用額度 {actual_quote_amount:.2f} USDT 過低，不再加倉")
            return
            
        print(f"💰 [{macro_regime}] 下單 {actual_quote_amount:.2f} USDT (設定:{quote_amount:.0f}, 可用:{available_margin:.2f})")
        print(f"@@AMOUNT@@{actual_quote_amount}")
    
        try:
            base_amt = await get_base_amount(actual_quote_amount)
            direction_str = "做多(Long)" if side == 'buy' else "做空(Short)"
            print(f"\n🛒 [下單模組] 💡 訊號觸發！{direction_str} {actual_quote_amount:.2f} USDT -> 數量: {base_amt:.6f}")
            
            if PAPER_TRADING:
                avg_price = price
                actual_received_amt = base_amt
                if side == 'buy':
                    simulated_base_amt += actual_received_amt
                else:
                    simulated_base_amt -= actual_received_amt
                simulated_avg_price = avg_price
                print(f"✅ [模擬開倉成功] {direction_str} | 成交均價: {avg_price} | 總倉位: {simulated_base_amt:.6f}")
                position_open_time = time.time()
                update_paper_state(SYMBOL_KEY, side, avg_price, actual_received_amt)
            else:
                prec = await get_contract_precision()
                qty_str = round_step(base_amt, prec['step_size'])
                ob = await exchange.fetch_order_book(symbol, limit=5)
                bid, ask = ob['bids'][0][0], ob['asks'][0][0]
                spread = ask - bid
                if spread <= 0 or bid <= 0:
                    print(f"⚠️ 盤口異常，市價單保底")
                    open_order = await exchange.create_order(
                        symbol=symbol, type='market', side=side,
                        amount=qty_str, params={'marginMode': 'isolated'}
                    )
                else:
                    splits = 4
                    per_qty = round_step(base_amt / splits, prec['step_size'])
                    if per_qty < prec['min_qty']:
                        splits = 1
                        per_qty = qty_str
                    tasks = []
                    placed_qty = 0.0
                    for i in range(splits):
                        q = round_step(qty_str - placed_qty, prec['step_size']) if i == splits - 1 else per_qty
                        if q < prec['min_qty']:
                            continue
                        ratio = i / (splits - 1) if splits > 1 else 0
                        p = round_price(bid + spread * ratio, prec['tick_size']) if side == 'buy' else round_price(ask - spread * ratio, prec['tick_size'])
                        tasks.append(exchange.create_order(
                            symbol=symbol, type='limit', side=side,
                            amount=q, price=p,
                            params={'marginMode': 'isolated'}
                        ))
                        placed_qty += q
                    if not tasks:
                        raise Exception("所有拆分訂單數量不足")
                    print(f"📊 [分批排隊] {direction_str} 分{splits}單 maker費率 | 範圍 {bid:.6f}~{ask:.6f}")
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    for r in results:
                        if isinstance(r, Exception):
                            print(f"⚠️ 拆分訂單失敗: {r}")
                            continue
                        open_order = r
                        break
                    else:
                        raise Exception("所有拆分訂單均失敗")
                avg_price = open_order.get('average') or price
                print(f"✅ [下單模組] {direction_str} 開倉成功！實際成交均價: {avg_price} | 單號: {open_order['id']}")
            
            # 更新活動時間
            global last_action_time
            last_action_time = time.time()
                
        except Exception as e:
            print(f"🚨 [下單/風控模組嚴重致命錯誤]: {e}")
            if PAPER_TRADING:
                simulated_base_amt = 0.0
    finally:
        is_ordering = False

async def update_position_info():
    global current_pos_qty, current_pos_avg
    while True:
        try:
            if PAPER_TRADING:
                import json
                try:
                    with open("paper_state.json", "r") as f:
                        state = json.load(f)
                        pos = state.get("positions", {}).get(SYMBOL_KEY, {})
                        current_pos_qty = float(pos.get("qty", 0.0))
                        current_pos_avg = float(pos.get("avg_price", 0.0))
                except:
                    pass
            else:
                positions = await exchange.fetch_positions([symbol])
                if positions:
                    p = positions[0]
                    current_pos_qty = float(p.get('info', {}).get('positionAmt', 0.0))
                    current_pos_avg = float(p.get('entryPrice', 0.0))
            await asyncio.sleep(1.0)
        except Exception as e:
            await asyncio.sleep(1.0)

async def close_entire_position(close_side, actual_close_amt, current_p, pos_avg, force_market=False):
    global simulated_base_amt
    if PAPER_TRADING:
        close_pnl = (current_p - pos_avg) * actual_close_amt if close_side == 'sell' else (pos_avg - current_p) * actual_close_amt
        simulated_base_amt = 0.0
        print(f"✅ [模擬平倉成功] 全倉已平！盈虧: {close_pnl:.4f} USDT")
        update_paper_state(SYMBOL_KEY, close_side, current_p, actual_close_amt, is_close=True, pnl=close_pnl)
    else:
        close_action = "賣出平多" if close_side == 'sell' else "買入平空"
        try:
            prec = await get_contract_precision()
            qty_str = round_step(actual_close_amt, prec['step_size'])
            if not force_market:
                ob = await exchange.fetch_order_book(symbol, limit=5)
                bid, ask = ob['bids'][0][0], ob['asks'][0][0]
                spread = ask - bid
                if spread <= 0 or bid <= 0:
                    print(f"⚠️ 盤口異常，市價單保底")
                    close_order = await exchange.create_order(
                        symbol=symbol, type='market', side=close_side,
                        amount=qty_str, params={'reduceOnly': True, 'marginMode': 'isolated'}
                    )
                else:
                    splits = 4
                    per_qty = round_step(actual_close_amt / splits, prec['step_size'])
                    if per_qty < prec['min_qty']:
                        splits = 1
                        per_qty = qty_str
                    tasks = []
                    placed_qty = 0.0
                    for i in range(splits):
                        q = round_step(qty_str - placed_qty, prec['step_size']) if i == splits - 1 else per_qty
                        if q < prec['min_qty']:
                            continue
                        ratio = i / (splits - 1) if splits > 1 else 0
                        p = round_price(bid + spread * ratio, prec['tick_size']) if close_side == 'buy' else round_price(ask - spread * ratio, prec['tick_size'])
                        tasks.append(exchange.create_order(
                            symbol=symbol, type='limit', side=close_side,
                            amount=q, price=p,
                            params={'reduceOnly': True, 'marginMode': 'isolated'}
                        ))
                        placed_qty += q
                    if not tasks:
                        raise Exception("所有拆分訂單數量不足")
                    print(f"📊 [分批平倉] {close_action} 分{splits}單 maker費率 | 範圍 {bid:.6f}~{ask:.6f}")
                    results = await asyncio.gather(*tasks, return_exceptions=True)
                    for r in results:
                        if isinstance(r, Exception):
                            print(f"⚠️ 拆分訂單失敗: {r}")
                            continue
                        close_order = r
                        break
                    else:
                        raise Exception("所有拆分訂單均失敗")
            else:
                close_order = await exchange.create_order(
                    symbol=symbol,
                    type='market',
                    side=close_side,
                    amount=qty_str,
                    params={'reduceOnly': True, 'marginMode': 'isolated'}
                )
            print(f"✅ [全局平倉成功] 已成功{close_action}！數量: {actual_close_amt:.6f}")
            # 更新活動時間
            global last_action_time
            last_action_time = time.time()
        except Exception as e:
            print(f"🚨 [平倉錯誤]: {e}")
        
def reset_trailing_stops():
    global trailing_highest, trailing_lowest
    trailing_highest = 0.0
    trailing_lowest = float('inf')

async def monitor_position_tp_sl():
    """ 獨立監控持倉：波段止盈/止損 """
    while True:
        try:
            await asyncio.sleep(0.5)

            pos_qty = 0.0
            pos_avg = 0.0
            if PAPER_TRADING:
                import json
                try:
                    with open("paper_state.json", "r") as f:
                        state = json.load(f)
                        pos = state.get("positions", {}).get(SYMBOL_KEY, {})
                        pos_qty = float(pos.get("qty", 0.0))
                        pos_avg = float(pos.get("avg_price", 0.0))
                except:
                    pass
            else:
                positions = await exchange.fetch_positions([symbol])
                if positions:
                    p = positions[0]
                    pos_qty = float(p.get('info', {}).get('positionAmt', 0.0))
                    pos_avg = float(p.get('entryPrice', 0.0))

            if abs(pos_qty) <= 0.000001 or pos_avg <= 0:
                continue

            ticker = await exchange.fetch_ticker(symbol)
            current_p = ticker['last']

            # 不使用固定止盈，一律靠動態反轉訊號出場；SL: 固定 3%
            sl_pct = 0.03

            if pos_qty > 0:
                sl = pos_avg * (1 - sl_pct)
                if current_p <= sl:
                    print(f"🛑 [止損] 多單均價 {pos_avg:.4f}，現價 {current_p:.4f} <= {sl:.4f} (-{sl_pct*100:.1f}%)，全平！")
                    await close_entire_position('sell', abs(pos_qty), current_p, pos_avg, force_market=True)
            else:
                sl = pos_avg * (1 + sl_pct)
                if current_p >= sl:
                    print(f"🛑 [止損] 空單均價 {pos_avg:.4f}，現價 {current_p:.4f} >= {sl:.4f} (+{sl_pct*100:.1f}%)，全平！")
                    await close_entire_position('buy', abs(pos_qty), current_p, pos_avg, force_market=True)

        except Exception as e:
            pass

# =====================================================================
# ① 行情接收模組 & ② 策略邏輯模組
# =====================================================================
async def monitor_macro_trend():
    """ 週期性檢查 1H 級別的 20T 均線，判斷大趨勢 (牛/熊/猴) """
    global macro_regime, current_pos_qty, current_1h_deviation
    while True:
        try:
            # 先抓取 BTC 大盤 1 小時 K 線
            btc_ohlcv = await exchange.fetch_ohlcv('BTCUSDT', timeframe='1h', limit=30)
            if len(btc_ohlcv) >= 20:
                btc_closes = np.array([x[4] for x in btc_ohlcv])
                btc_sma = np.mean(btc_closes[-20:])
                btc_deviation = (btc_closes[-1] - btc_sma) / btc_sma
            else:
                btc_deviation = 0.0

            # 抓取當前交易幣種 1 小時 K 線
            ohlcv = await exchange.fetch_ohlcv(symbol, timeframe='1h', limit=30)
            if len(ohlcv) >= 20:
                closes = np.array([x[4] for x in ohlcv])
                current_price = closes[-1]
                sma_20 = np.mean(closes[-20:])
                
                # 計算幣種偏離度
                coin_deviation = (current_price - sma_20) / sma_20
                
                old_regime = macro_regime
                old_dev = current_1h_deviation
                
                # 邏輯：BTC 大盤擁有最高決策權
                if btc_deviation > 0.02:
                    macro_regime = "牛市 (大盤BTC帶飛)"
                    print_dev = btc_deviation
                elif btc_deviation < -0.02:
                    macro_regime = "熊市 (大盤BTC帶崩)"
                    print_dev = btc_deviation
                else:
                    # 大盤震盪時，才看個別幣種
                    if coin_deviation > 0.02:
                        macro_regime = "牛市 (獨立走強)"
                    elif coin_deviation < -0.02:
                        macro_regime = "熊市 (獨立走弱)"
                    else:
                        macro_regime = "猴市 (區間震盪)"
                    print_dev = coin_deviation
                
                current_1h_deviation = print_dev
                await update_dynamic_leverage()
                
                if old_regime != macro_regime or abs(old_dev - current_1h_deviation) > 0.03:
                    amount = 50.0
                    print(f"@@REGIME@@{macro_regime}")
                    print(f"@@AMOUNT@@{amount}")
                    print(f"🌍 [環境感知] {macro_regime} | 偏離: {print_dev*100:.2f}% | 槓桿: {LEVERAGE}x")
        except Exception as e:
            print(f"⚠️ [環境感知] 無法獲取 1H 趨勢: {e}")
        
        # 每 5 分鐘檢查一次
        await asyncio.sleep(300)
        print(f"💓 [心跳] bot 運行中 | 持倉: {current_pos_qty:.4f} | 狀態: {macro_regime}")

async def watch_kline_and_strategy():
    """ 透過 WebSocket 監聽 1分K，並用 NumPy 計算布林插針策略 """
    print("🚀 [行情模組一] 開始監聽 WebSocket K線數據 (正在預載歷史數據...)")
    global current_atr, current_rsi, current_pos_qty
    prev_close = None
    tr_list = []
    last_buy_time = 0
    
    # 預載歷史 K 線以防啟動時需空等 20 分鐘
    try:
        historical_ohlcv = await exchange.fetch_ohlcv(symbol, timeframe, limit=30)
        closes_history = [x[4] for x in historical_ohlcv]
        volumes_history = [x[5] for x in historical_ohlcv]
    except Exception as e:
        print(f"⚠️ 無法預載歷史 K 線: {e}")
        historical_ohlcv = []
        closes_history = []
        volumes_history = []
    
    ohlcv_buffer = list(historical_ohlcv)  # 自己維護的 OHLCV 緩衝區
    
    while True:
        try:
            new_ohlcv = await exchange.watch_ohlcv(symbol, timeframe)
            
            # 將新數據合併進緩衝區 (避免 watch_ohlcv 只回傳遞增數據)
            if new_ohlcv:
                ohlcv_buffer.extend(new_ohlcv)
                # 去重：保留最後 60 筆 (足夠 30 筆計算)
                seen = {}
                unique = []
                for x in ohlcv_buffer:
                    ts = x[0]
                    if ts not in seen:
                        seen[ts] = True
                        unique.append(x)
                ohlcv_buffer = unique[-60:]
            
            ohlcv = ohlcv_buffer[-30:]
            
            # 只取最後 30 筆即可
            closes = np.array([x[4] for x in ohlcv])
            volumes = np.array([x[5] for x in ohlcv])
            highs = np.array([x[2] for x in ohlcv])
            opens = np.array([x[1] for x in ohlcv])
            
            if len(closes) < 20:
                continue
                
            # 計算成交量均線 (20MA)
            vol_ma20 = np.mean(volumes[-20:])
            current_vol = volumes[-1]

            # 計算 ATR
            for i in range(len(ohlcv)):
                h, l, c = ohlcv[i][2], ohlcv[i][3], ohlcv[i][4]
                if i == 0 and prev_close is not None:
                    tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
                elif i > 0:
                    tr = max(h - l, abs(h - ohlcv[i-1][4]), abs(l - ohlcv[i-1][4]))
                else:
                    tr = h - l
                tr_list.append(tr)
            prev_close = ohlcv[-1][4]
            if len(tr_list) > ATR_PERIOD * 3:
                tr_list = tr_list[-(ATR_PERIOD * 3):]
            if len(tr_list) >= ATR_PERIOD:
                current_atr = float(np.mean(tr_list[-ATR_PERIOD:]))

            # 計算 RSI
            if len(closes) > RSI_PERIOD:
                deltas = np.diff(closes[-RSI_PERIOD-1:])
                gains = deltas[deltas > 0].mean() if np.any(deltas > 0) else 1e-10
                losses = -deltas[deltas < 0].mean() if np.any(deltas < 0) else 1e-10
                rs = gains / losses
                current_rsi = 100.0 - (100.0 / (1.0 + rs))
                
            middle_band = np.mean(closes[-20:])
            close_price = closes[-1]
            deviation = (close_price - middle_band) / middle_band
            
            # 每 60 秒印一次狀態
            if not hasattr(watch_kline_and_strategy, '_status_log') or time.time() - watch_kline_and_strategy._status_log > 60:
                watch_kline_and_strategy._status_log = time.time()
                print(f"📊 [狀態] RSI={current_rsi:.1f} 偏離={deviation*100:.2f}% 持倉={current_pos_qty:.4f}  regime={macro_regime}")
            
            # 🔥 新增近期 RSI 歷史紀錄，用於右側確認 (抄底/摸頂)
            if not hasattr(watch_kline_and_strategy, 'recent_rsis'):
                watch_kline_and_strategy.recent_rsis = []
            watch_kline_and_strategy.recent_rsis.append(current_rsi)
            if len(watch_kline_and_strategy.recent_rsis) > 180: # 紀錄過去 3 分鐘 (180秒) 的變化
                watch_kline_and_strategy.recent_rsis.pop(0)
            
            min_recent_rsi = min(watch_kline_and_strategy.recent_rsis)
            max_recent_rsi = max(watch_kline_and_strategy.recent_rsis)
            
            # 🔥 高溫煞車系統：極端 RSI 時避免左側直接進場，改為右側確認
            rsi_extreme = current_rsi > 75.0 or current_rsi < 20.0
            if rsi_extreme:
                if not hasattr(watch_kline_and_strategy, '_rsi_warned') or time.time() - watch_kline_and_strategy._rsi_warned > 60:
                    watch_kline_and_strategy._rsi_warned = time.time()
                    print(f"🌡️ [RSI 防護] RSI={current_rsi:.1f}，左側高溫區，等待右側確認...")

            # 🚀 [動態轉折平倉邏輯]
            if abs(current_pos_qty) > 0.000001:
                is_long = current_pos_qty > 0
                recent_highs = [x[2] for x in ohlcv[-30:-1]]
                recent_lows = [x[3] for x in ohlcv[-30:-1]]
                resistance = max(recent_highs) if recent_highs else 999999
                support = min(recent_lows) if recent_lows else 0
                range_height = resistance - support
                current_open = opens[-1]
                
                close_signal = False
                close_reason = ""

                # ⏱️ 最少持倉 120 秒，避免手動開倉後被秒砍
                if time.time() - position_open_time < 120:
                    if not hasattr(watch_kline_and_strategy, '_hold_warned') or time.time() - watch_kline_and_strategy._hold_warned > 30:
                        watch_kline_and_strategy._hold_warned = time.time()
                        print(f"⏱️ [持倉保護] 開倉僅 {time.time()-position_open_time:.0f}s，120 秒內不觸發動態平倉")
                    continue

                if is_long:
                    profit_pct = (close_price - current_pos_avg) / current_pos_avg
                else:
                    profit_pct = (current_pos_avg - close_price) / current_pos_avg

                if profit_pct <= -0.10:
                    close_signal = True
                    close_reason = f"⛔ 硬止損：虧損超過 10% ({profit_pct*100:.1f}%)"
                    
                # 【階梯式追蹤停利 (現貨版)】
                global trailing_highest

                # 現貨只有做多 (is_long = True)
                if not close_signal:
                    # 啟動門檻：0.25%
                    tp_threshold = 0.0025
                    # 追蹤回落距離：0.05% (0.25 -> 0.20, 0.30 -> 0.25)
                    trailing_distance = 0.0005
                    
                    if close_price > trailing_highest:
                        trailing_highest = close_price
                        
                    highest_profit_pct = (trailing_highest - current_pos_avg) / current_pos_avg
                    
                    if highest_profit_pct >= tp_threshold:
                        stop_line = highest_profit_pct - trailing_distance
                        if profit_pct <= stop_line:
                            close_signal = True
                            close_reason = f"🛡️ 階梯追蹤停利：最高 {highest_profit_pct*100:.2f}% 回落至 {stop_line*100:.2f}%"
                            reset_trailing_stops()
                
                if close_signal:
                    # 計算當前損益（含來回手續費 0.1%）
                    if is_long:
                        pnl = (close_price - current_pos_avg) * current_pos_qty
                    else:
                        pnl = (current_pos_avg - close_price) * abs(current_pos_qty)
                    fee_cost = close_price * abs(current_pos_qty) * 0.001
                    if pnl <= fee_cost and "停利" not in close_reason and "止損" not in close_reason:
                        print(f"💤 [利潤不足] 損益 {pnl:.4f} ≤ 手續費 {fee_cost:.4f}，等待更好價格再平")
                        continue
                        
                    print(f"⚠️ [動態平倉] 偵測到平倉信號！原因: {close_reason}，執行市價平倉！")
                    close_side = 'sell' if is_long else 'buy'
                    
                    # 等待平倉完成，確保資金釋放
                    await close_entire_position(close_side, abs(current_pos_qty), close_price, current_pos_avg)
                    current_pos_qty = 0.0
                    
                    # 【無縫反手接軌】若為停利出場，立刻反向開單
                    if "停利" in close_reason:
                        next_side = 'sell' if is_long else 'buy'
                        if next_side == 'sell' and current_rsi < 30:
                            print(f"🛑 [反手防護] RSI = {current_rsi:.1f} (超賣到底)，取消無縫做空，避免在阿呆谷被套！")
                        elif next_side == 'buy' and current_rsi > 70:
                            print(f"🛑 [反手防護] RSI = {current_rsi:.1f} (超買到頂)，取消無縫做多，避免在天花板被套！")
                        else:
                            print(f"🔥 [無縫接軌] 停利出場，判斷安全，立刻反手做 {next_side.upper()}！")
                            asyncio.create_task(execute_order_and_risk(side=next_side, price=close_price))
                        
                    continue # 結束本回合，不再執行後續開倉判斷

            # 動態大腦：根據大環境切換雙刀流策略 (Regime-Switching)
            # 方案 B 取消直接擋單，改由右側確認決定
            current_time = time.time()
            
            # 【全自動雷達換倉機制】閒置超過 30 分鐘 (1800秒) 且無倉位時，強制登出並觸發雷達
            global last_action_time
            if current_time - last_action_time > 1800 and abs(current_pos_qty) < 0.000001:
                print(f"📡 [雷達換倉] 該幣種已超過 30 分鐘無明顯波動，啟動自動切換機制！")
                sys.exit(2)
                
            if current_time - last_buy_time > 30: # 全局共用 30 秒冷卻
                # 🐒 統一為【全天候區間雙向操作】不管牛熊，皆可多空雙開
                recent_highs = [x[2] for x in ohlcv[-30:-1]] # 過去 29 根 K 線的高點
                recent_lows = [x[3] for x in ohlcv[-30:-1]]  # 過去 29 根 K 線的低點
                
                if recent_highs and recent_lows:
                    resistance = max(recent_highs)
                    support = min(recent_lows)
                    range_height = resistance - support
                    range_pct = (range_height / support * 100) if support > 0 else 0
                    
                    if not hasattr(watch_kline_and_strategy, '_range_log') or time.time() - watch_kline_and_strategy._range_log > 30:
                        watch_kline_and_strategy._range_log = time.time()
                        pct_in_range = ((close_price - support) / range_height * 100) if range_height > 0 else 0
                        print(f"📐 [全天候區間] 幅度={range_pct:.3f}% 支撐={support:.6f} 壓力={resistance:.6f} 當前={close_price:.6f} 位置={pct_in_range:.0f}%")
                    
                    # 確保箱子夠大 (至少 0.1% 震幅)
                    if range_pct >= 0.1:
                        # 【全天候無差別雙向區間策略】
                        # 不管牛市還是熊市，衝到頂部(天花板)就做空，跌到底部(地板)就做多
                        
                        # 1. 接近或突破壓力位 (天花板) -> 摸頂做空
                        if close_price >= resistance - (range_height * 0.40):
                            # 動態 RSI 門檻：熊市因為很難到 70，所以下調門檻到 60/50
                            short_peak_rsi = 60 if "熊市" in macro_regime else 70
                            short_confirm_rsi = 50 if "熊市" in macro_regime else 60
                            
                            # 右側確認：曾經頂破門檻，現在回落才做空
                            if max_recent_rsi > short_peak_rsi and current_rsi <= short_confirm_rsi:
                                last_buy_time = current_time
                                print(f"⚠️ [全天候: 摸頂] 右側確認！箱頂:{resistance:.4f} (最高RSI={max_recent_rsi:.1f} 回落至={current_rsi:.1f})，觸發做空(Short)")
                                if simulated_base_amt > 0: asyncio.create_task(execute_close_all())
                            elif "牛市" in macro_regime:
                                pass # 牛市仍稍微防護，或交由右側確認處理，這裡已由右側確認取代，所以暫不限制
                            
                        # 2. 接近或跌破支撐位 (地板) -> 抄底做多
                        elif close_price <= support + (range_height * 0.40):
                            # 動態 RSI 門檻：牛市因為很難跌破 30，所以上調門檻到 40/50
                            long_dip_rsi = 40 if "牛市" in macro_regime else 30
                            long_confirm_rsi = 50 if "牛市" in macro_regime else 40
                            
                            # 右側確認：曾經跌破門檻，現在反彈才做多
                            if min_recent_rsi < long_dip_rsi and current_rsi >= long_confirm_rsi:
                                last_buy_time = current_time
                                print(f"⚠️ [全天候: 抄底] 右側確認！箱底:{support:.4f} (最低RSI={min_recent_rsi:.1f} 反彈至={current_rsi:.1f})，觸發做多(Long)")
                                asyncio.create_task(execute_order_and_risk(side='buy', price=close_price))
                
        except Exception as e:
            import traceback
            print(f"❌ [K線模組發生波動]: {e}")
            traceback.print_exc()
            print("1秒後自動重連...")
            await asyncio.sleep(1)


async def watch_trades_and_order_book():
    """ 透過 WebSocket 監聽逐筆成交，執行「盤口大單追蹤」 """
    print("🚀 [行情模組二] 開始監聽 WebSocket 逐筆成交數據...")
    while True:
        try:
            trades = await exchange.watch_trades(symbol)
            price = await get_current_price()
            threshold_qty = ORDER_BOOK_THRESHOLD_USD / price
            for trade in trades:
                trade_volume = trade['amount']
                trade_price = trade['price']
                trade_side = trade['side']
                
                if trade_volume >= threshold_qty:
                    print(f"🔥 [策略訊號] 盤口湧入特大單！方向: {trade_side}, 數量: {trade_volume:.2f}, USD約: ${trade_volume*price:.0f}, 價格: {trade_price}")
                    
                    if trade_side == 'buy':
                        print("👉 [策略動態] 主力強勢吃單 (僅監控，不追高買入以保護成本)")
                        # asyncio.create_task(execute_order_and_risk(side='buy', price=trade_price))
                    elif trade_side == 'sell':
                        print("👉 [策略動態] 主力強勢賣出 (僅監控，不追跌)")
                        # asyncio.create_task(execute_order_and_risk(side='sell', price=trade_price))
                        
        except Exception as e:
            print(f"❌ [逐筆成交模組發生波動]: {e}，1秒後自動重連...")
            await asyncio.sleep(1)


# =====================================================================
# 主程式入口
# =====================================================================



last_stable_buy_price = 0.99990


last_stable_buy_price = 0.99990

async def stablecoin_scalper_loop():
    global last_stable_buy_price
    print("🚀 [穩定幣專屬策略] 啟動純市價跟蹤動態插隊造市策略 (Pure Dynamic, 防自我競價版)...")
    while True:
        try:
            if PAPER_TRADING:
                print("⚠️ 模擬模式暫不支援穩定幣限價單自動搓合與動態插隊。")
                await asyncio.sleep(60)
                continue
            
            # 獲取當前最佳買賣價
            order_book = await exchange.fetch_order_book(symbol, limit=5)
            best_bid = float(order_book['bids'][0][0]) if order_book['bids'] else 0.99990
            best_ask = float(order_book['asks'][0][0]) if order_book['asks'] else 1.00000

            # 獲取當前開放訂單
            open_orders = await exchange.fetch_open_orders(symbol)
            buy_orders = [o for o in open_orders if o['side'] == 'buy']
            sell_orders = [o for o in open_orders if o['side'] == 'sell']
            
            # 獲取餘額
            balance = await exchange.fetch_balance()
            usdc_free = float(balance.get('USDC', {}).get('free', 0.0))
            usdt_free = float(balance.get('USDT', {}).get('free', 0.0))
            
            # 1. 動態買單插隊邏輯
            needs_new_buy = False
            target_buy_price = round(best_bid + 0.00001, 5)

            if usdt_free > 5.0:
                buy_amount = min(usdt_free, default_amount)
                
                # 跌市(熊市)防護
                global macro_regime
                is_bear_market = "熊市" in macro_regime
                
                if buy_amount > 1 and not is_bear_market:
                    if buy_orders:
                        current_buy_order = buy_orders[0]
                        current_buy_price = float(current_buy_order['price'])
                        # 防自我競價：如果我們的買單已經是全場最高（或平手），保持不動！
                        if current_buy_price >= best_bid:
                            needs_new_buy = False
                        else:
                            needs_new_buy = True
                            print(f"🔄 [買單調整] 被人超過了！(他:{best_bid:.5f} 我:{current_buy_price:.5f})，執行撤單...")
                            try:
                                await exchange.cancel_order(current_buy_order['id'], symbol)
                                await asyncio.sleep(0.5)
                            except Exception as e:
                                print(f"撤單失敗: {e}")
                    else:
                        needs_new_buy = True
                elif is_bear_market and buy_amount > 1:
                    print(f"🛑 [環境防護] 目前為 {macro_regime}，暫停掛買單。")
                        
                if needs_new_buy:
                    print(f"🛒 [穩定幣動態掛單] 搶佔第一！掛買單: {buy_amount:.2f} USDC @ {target_buy_price:.5f}")
                    try:
                        await exchange.create_limit_buy_order(symbol, buy_amount / target_buy_price, target_buy_price)
                        last_stable_buy_price = target_buy_price
                    except Exception as e:
                        print(f"下買單失敗: {e}")

            # 2. 動態賣單插隊邏輯
            needs_new_sell = False
            target_sell_price = round(best_ask - 0.00001, 5)
            # 保本防呆：確保賣出價比最後買入價高至少 0.00001
            minimum_sell_price = round(last_stable_buy_price + 0.00001, 5)
            
            if target_sell_price < minimum_sell_price:
                target_sell_price = minimum_sell_price
                
            if usdc_free > 5.0:
                sell_amount = min(usdc_free, default_amount)
                if sell_amount > 1:
                    if sell_orders:
                        current_sell_order = sell_orders[0]
                        current_sell_price = float(current_sell_order['price'])
                        # 防自我競價：如果我們的賣價已經是全場最低（或平手），保持不動！
                        if current_sell_price <= best_ask:
                            needs_new_sell = False
                        else:
                            needs_new_sell = True
                            print(f"🔄 [賣單調整] 被人搶低了！(他:{best_ask:.5f} 我:{current_sell_price:.5f})，執行撤單...")
                            try:
                                await exchange.cancel_order(current_sell_order['id'], symbol)
                                await asyncio.sleep(0.5)
                            except Exception as e:
                                print(f"撤單失敗: {e}")
                    else:
                        needs_new_sell = True
                            
                    if needs_new_sell:
                        print(f"💰 [穩定幣動態掛單] 搶佔第一！掛賣單: {sell_amount:.2f} USDC @ {target_sell_price:.5f} (成本: {last_stable_buy_price:.5f})")
                        try:
                            await exchange.create_limit_sell_order(symbol, sell_amount, target_sell_price)
                        except Exception as e:
                            print(f"下賣單失敗: {e}")
                        
        except Exception as e:
            print(f"❌ [穩定幣動態策略錯誤]: {e}")
        
        # 1 秒極速造市模式
        await asyncio.sleep(1)

async def main():
    try:
        if not PAPER_TRADING:
            # 設定合約槓桿與保證金模式 (逐倉)
            try:
                await exchange.set_margin_mode('isolated', symbol)
                print(f"🔧 [系統初始化] 設定逐倉模式成功")
            except Exception as e:
                print(f"⚠️ [設定逐倉] {e}")
            try:
                await exchange.set_leverage(int(LEVERAGE), symbol)
                print(f"🔧 [系統初始化] 設定槓桿 {int(LEVERAGE)}x 成功")
            except Exception as e:
                print(f"⚠️ [設定槓桿] {e}")
            print(f"🔧 [系統初始化] 實盤模式: 合約交易啟動")
    except Exception as e:
        print(f"⚠️ [安全保護警告] 設定逐倉/槓桿失敗，可能該幣種不支援或已有持倉: {e}")
            
    # 啟動時先做第一次帳戶安全檢查與歷史倉位載入
    await check_account_safety()
    await initialize_simulated_position()
    print("🚀 啟動防爆倉模組 & WebSocket 即時監聽...")
    print(f"@@REGIME@@{macro_regime}") # 初始化發送狀態
    print(f"@@AMOUNT@@{default_amount}") # 初始化發送金額
    # 同時併發運行兩大行情模組與全局監控
    if symbol == 'USDC/USDT':
        await asyncio.gather(
            update_position_info(),
            stablecoin_scalper_loop()
        )
    else:
        await asyncio.gather(
            check_account_safety(),
            update_position_info(),
            watch_kline_and_strategy(),
            watch_trades_and_order_book(),
            monitor_macro_trend(),
            monitor_position_tp_sl()
        )
if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 機器人已被手動關閉，安全退出。")
