import asyncio
import os
import ccxt.pro as ccxtpro  # 使用 CCXT 的 WebSocket 版本
import numpy as np
import sys                  # 用於觸發風控時關閉程式
import argparse             # 用於接收命令列參數
import time                 # 用於計時
from update_paper_state import update_paper_state
from dotenv import load_dotenv
from line_notifier import send_line_alert
from src.execution_engine import ExecutionEngine, OrderStatus

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

# =====================================================================
# 策略參數配置 (Strategy Variables)
# =====================================================================

# 1. 壓力與支撐 (進場天花板/地板)
USE_DYNAMIC_N_DAY_EXTREMES = True  # True: 自動讀取 N 日高低點; False: 使用外部設定字典
N_DAYS_FOR_EXTREMES = 1            # 自動讀取 N 日內的最高/最低點作為天花板與地板

# 若 USE_DYNAMIC_N_DAY_EXTREMES 為 False，則讀取此外部設定字典
# 您可以在此處隨時手動更改不同幣別的天花板 (ceiling) 與地板 (floor)
MANUAL_CEILINGS = {
    "BTC:USDT": {"ceiling": 72000.0, "floor": 65000.0},
    "SOL:USDT": {"ceiling": 200.0, "floor": 120.0},
    "SUI:USDT": {"ceiling": 2.0, "floor": 0.5},
    "LAB:USDT": {"ceiling": 15.0, "floor": 8.0}
}

# 2. 停利與停損動態計算模式
# 模式選項: 'ATR' (依據 ATR 波動度) 或 'RESISTANCE_PCT' (依據進場價距離壓力位的百分比)
TP_SL_MODE = 'ATR' 

# ----------------- 'ATR' 模式參數 -----------------
SL_ATR_MULTIPLIER = 4.0   # 停損為 4 倍 ATR (原 6.0)
TP_ATR_MULTIPLIER = 0.8   # 停利為 0.8 倍 ATR (原 0.5，多吃一點利潤)

# ----------------- 'RESISTANCE_PCT' 模式參數 -----------------
# (相對於壓力/支撐位的百分比)
# 例如：做多時，停利設在「進場價到壓力位距離的 80%」，停損設在「進場價到支撐位距離的 50%」
TP_DISTANCE_PCT = 0.80  # 停利距離百分比
SL_DISTANCE_PCT = 0.50  # 停損距離百分比

# --- 其他系統參數 ---
last_market_condition = "MONKEY"
hard_sl_count = 0
last_hard_sl_reset = time.time()
ATR_PERIOD = 14
ORDER_BOOK_THRESHOLD_USD = 100000.0
current_atr = 0.0
atr_ma20 = 0.0          # ATR 的 20 期移動平均，供僵局動態判斷用
atr_history = []        # 歷史 ATR 值序列
last_exit_time = {}     # 各幣種最後平倉時間，用於開倉冷卻 {"SUI:USDT": timestamp}
RSI_PERIOD = 14
RSI_OVERBOUGHT = 70
current_rsi = 50.0
macro_regime = "猴市 (區間震盪)"
default_amount = 150.0

global_resistance = 0.0
global_support = 0.0

# 大盤趨勢過濾：由 Market_Trend (macro_regime) 控制哪些方向可以下單
ALLOW_LONG = True
ALLOW_SHORT = True

# ================================================================
# 動態攻防與風控全參數 (全變數化，禁止硬編碼價格)
# ================================================================
RESISTANCE_ZONE_THRESHOLD = 0.950       # 壓力區觸發：價格 >= Resistance * 0.950 (原0.975，放寬至5%)
SUPPORT_ZONE_THRESHOLD = 1.050          # 支撐區觸發：價格 <= Support * 1.050 (原1.025，放寬至5%)
VOLUME_BREAKOUT_MULTIPLIER = 1.5       # 爆量破位/突破判定：成交量 >= 均量 * 1.5 (原1.8)
VOLUME_SHRINK_MULTIPLIER = 0.9         # 量縮止穩判定：成交量 < 均量 * 0.9 (原0.7)
MIDPOINT_CLOSE_ENABLED = True          # 中線平倉停利開關
BREAK_EVEN_PROFIT_PCT = 0.008          # 保本鎖觸發利潤 (+0.8%)

SHORT_TAKE_PROFIT_PCT = 0.04                 # 空單固定停利：跌幅達 Entry * (1 - 4%)
LONG_ENTRY_ZONE_TOP_PCT = 0.01              # 多單進場頂部：Support * (1 + 1%)
LONG_STOP_LOSS_PCT = 0.02                   # 多單止損底部：Support * (1 - 2%)
BREAKDOWN_VOLUME_MULTIPLIER = 1.8           # 破位爆量判定：成交量 >= 均量 * 1.8
CONSOLIDATION_VOL_SHRINK = 0.7              # 止跌橫盤量縮：成交量 < 均量 * 0.7

# ================================================================
# 資金費率與滑價防護網參數
# ================================================================
FUNDING_RATE_SHORT_BLOCK = -0.005    # 負費率超過 -0.5% 時禁止開空（極端持倉利息防護）
MAX_SLIPPAGE_PCT = 0.002             # 市價單最大容忍滑價 0.2%

# ================================================================
# 槓桿與強平價安全連動鎖參數
# ================================================================
MIN_LEVERAGE = 3                       # 最低槓桿倍數
MAX_LEVERAGE = 10                      # 最高槓桿倍數（鎖死上限，禁止 > 10x）
MAINTENANCE_MARGIN_RATIO = 0.005       # 維持保證金率 (0.5%，多數幣種第一級)
HARD_STOP_LOSS_PCT = 0.10             # 硬止損百分比（與策略內 -10% 同步）

# 🎯 物理防火牆：每日最大虧損限額設定
INITIAL_BALANCE = 150.0                   # 你的總本金 150 USDT
MAX_DAILY_LOSS_PCT = 0.15                 # 每日最大容忍虧損 5%
BALANCE_STOP_LINE = INITIAL_BALANCE * (1 - MAX_DAILY_LOSS_PCT)

_precisions_cache = None
SYMBOL = symbol

async def get_contract_precision():
    global _precisions_cache
    if _precisions_cache: return _precisions_cache

    try:
        if not exchange.markets:
            await exchange.load_markets()
            
        ccxt_symbol = "1000PEPE/USDT" if SYMBOL == "PEPEUSDT" else SYMBOL.replace("USDT", "/USDT")
        if ccxt_symbol == "PEPE/USDT":
            ccxt_symbol = "1000PEPE/USDT"
            
        market = exchange.market(ccxt_symbol)
        _precisions_cache = {
            'price_prec': market['precision']['price'],
            'qty_prec': market['precision']['amount'],
            'step_size': market['limits']['amount']['min']
        }
        return _precisions_cache
    except Exception as e:
        print(f"⚠️ 無法取得精度: {e}")
        return {'price_prec': 0.001, 'qty_prec': 0.001, 'step_size': 0.001}

async def get_current_price():
    ccxt_symbol = "1000PEPE/USDT" if SYMBOL == "PEPEUSDT" else SYMBOL.replace("USDT", "/USDT")
    if ccxt_symbol == "PEPE/USDT":
        ccxt_symbol = "1000PEPE/USDT"
    ticker = await exchange.fetch_ticker(ccxt_symbol)
    return float(ticker['last'])

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

# 空單停利後支撐位監控狀態 (0 = 未啟動, >0 = 正在監控此支撐位)
short_tp_support_level = 0.0

# 1h SMA 200 (由 monitor_macro_trend 更新，供開倉濾網使用)
global_sma200_1h = 0.0


# 統一 symbol 格式：BNB/USDT:USDT -> BNB:USDT (與手動下單格式一致)
SYMBOL_KEY = symbol.replace('/', '').replace(':USDT', '').replace('USDT', '') + ':USDT'

# 雷達換倉：追蹤最後活動時間
last_action_time = time.time()

# 移動停利狀態變數
trailing_highest = 0.0
trailing_lowest = float('inf')

# 合約槓桿倍數 (由 detect_regime 根據 1h 偏離度動態調整，鎖死 MAX_LEVERAGE 上限)
LEVERAGE = min(5.0, MAX_LEVERAGE)
current_1h_deviation = 0.0

# 初始化高保真執行引擎與高波動幣種列表
execution_engine = ExecutionEngine()
HIGH_VOL_LIST = ["PEPE/USDT:USDT", "SOL/USDT:USDT", "SYN/USDT:USDT", "ESPORTS/USDT:USDT", "LAB/USDT:USDT"]

async def update_dynamic_leverage():
    global LEVERAGE, current_1h_deviation, macro_regime
    old = LEVERAGE
    if macro_regime == "MONKEY":
        LEVERAGE = 5
    else:
        LEVERAGE = 5
    # 鎖死槓桿在 [MIN_LEVERAGE, MAX_LEVERAGE] 之間，禁止逾越
    LEVERAGE = min(max(LEVERAGE, MIN_LEVERAGE), MAX_LEVERAGE)
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

async def vacuum_zone_check(price, support, resistance):
    """真空區檢查：若價格落在中間地帶，拒絕下單 (已暫時放行以增加開單頻率)"""
    return False

async def check_market_slippage(side, is_market_order=False):
    """檢查市價單滑價是否在可容忍範圍內，超過 MAX_SLIPPAGE_PCT 則拒絕"""
    if PAPER_TRADING or not is_market_order:
        return True
    try:
        ob = await exchange.fetch_order_book(symbol, limit=5)
        bid = float(ob['bids'][0][0])
        ask = float(ob['asks'][0][0])
        last_price = await get_current_price()
        if side == 'buy':
            slippage = (ask - last_price) / last_price
        else:
            slippage = (last_price - bid) / last_price
        if slippage > MAX_SLIPPAGE_PCT:
            print(f"🛑 [滑價防護] 當前{'買' if side=='buy' else '賣'}方滑價 {slippage*100:.3f}% 超過上限 {MAX_SLIPPAGE_PCT*100:.1f}%，拒絕市價單！")
            return False
        return True
    except Exception as e:
        print(f"⚠️ [滑價防護] 檢查失敗: {e}")
        return True  # 保守：檢查失敗則放行

def calculate_liquidation_price(entry_price, side, leverage, mm_ratio=MAINTENANCE_MARGIN_RATIO):
    """計算 Binance 逐倉模式下的預估強制平倉價"""
    if entry_price <= 0 or leverage <= 0:
        return 0.0
    if side == 'buy':  # 多單
        return entry_price * (1 - 1.0 / leverage) / (1 - mm_ratio)
    else:  # 空單
        return entry_price * (1 + 1.0 / leverage) / (1 + mm_ratio)

async def check_liquidation_safety_lock(side, entry_price, stop_loss_price=None):
    """槓桿與強平價安全連動鎖：確保強平價不落在策略止損點之內"""
    liq_price = calculate_liquidation_price(entry_price, side, LEVERAGE)
    if liq_price <= 0:
        return True

    # 若未指定止損價，以硬止損百分比推算
    if stop_loss_price is None:
        if side == 'buy':
            stop_loss_price = entry_price * (1 - HARD_STOP_LOSS_PCT)
        else:
            stop_loss_price = entry_price * (1 + HARD_STOP_LOSS_PCT)

    side_label = "多單" if side == 'buy' else "空單"
    unsafe = False

    if side == 'buy':
        if liq_price >= stop_loss_price:
            unsafe = True
            print(f"🚨 [強平安全鎖] {side_label} 強平價 {liq_price:.6f} >= 止損價 {stop_loss_price:.6f}，強平會在止損前觸發！")
    else:
        if liq_price <= stop_loss_price:
            unsafe = True
            print(f"🚨 [強平安全鎖] {side_label} 強平價 {liq_price:.6f} <= 止損價 {stop_loss_price:.6f}，強平會在止損前觸發！")

    if unsafe:
        suggested = max(LEVERAGE - 2, MIN_LEVERAGE)
        print(f"🛑 [強平安全鎖] 進場價={entry_price:.6f} | 槓桿={LEVERAGE}x | 強平價={liq_price:.6f} | 止損價={stop_loss_price:.6f}")
        print(f"⚠️ [強平安全鎖] 建議降低槓桿至 {suggested}x 以上，或放寬止損比例")
        return False

    print(f"✅ [強平安全鎖] {side_label} 安全 | 進場={entry_price:.6f} 槓桿={LEVERAGE}x 強平={liq_price:.6f} 止損={stop_loss_price:.6f}")
    return True

async def execute_order_and_risk(side, price):
    global simulated_base_amt, simulated_avg_price, current_atr, position_open_time, ALLOW_LONG, ALLOW_SHORT, execution_engine, HIGH_VOL_LIST
    
    # [DCA 設定 - 雙引擎策略]
    ENTRY_BATCH_RATIOS = [0.3, 0.3, 0.4]
    
    # ================================================================
    # 【外層過濾一】Market_Trend 方向控制：ALLOW_LONG / ALLOW_SHORT
    # ================================================================
    if side == 'buy' and not ALLOW_LONG:
        print(f"🛑 [趨勢過濾] 當前大盤為 {macro_regime}，不允許做多 (ALLOW_LONG=False)，跳過")
        return
    if side == 'sell' and not ALLOW_SHORT:
        print(f"🛑 [趨勢過濾] 當前大盤為 {macro_regime}，不允許做空 (ALLOW_SHORT=False)，跳過")
        return

    # ================================================================
    # 【外層過濾二】真空區檢查 (Vacuum Zone Check)
    # ================================================================
    current_p = price if price > 0 else await get_current_price()
    if await vacuum_zone_check(current_p, global_support, global_resistance):
        return

    # ================================================================
    # 【外層過濾三】槓桿與強平價安全連動鎖
    # ================================================================
    if not await check_liquidation_safety_lock(side, current_p):
        print(f"🛑 [強平安全鎖] 槓桿 {LEVERAGE}x 導致強平價侵入止損區，拒絕開單！")
        return

    # ================================================================
    # 【局部鎖】只鎖送單那一段，不影響平倉邏輯
    # ================================================================
    if order_lock.locked():
        print(f"⚠️ [並發防護] 已有訂單在送單中，跳過")
        return

    async with order_lock:
        # 0. 資金費率防護 (Funding Rate Shield)
        if not PAPER_TRADING:
            try:
                funding_info = await exchange.fetch_funding_rate(symbol)
                funding_rate = float(funding_info.get('fundingRate', 0.0))
                if side == 'buy' and funding_rate > 0.002:
                    print(f"🛑 [資金費率防護] 資金費率過高 ({funding_rate*100:.3f}%)，放棄做多避免支付高昂利息！")
                    return
                elif side == 'sell' and funding_rate < -0.002:
                    print(f"🛑 [資金費率防護] 資金費率過低 ({funding_rate*100:.3f}%)，放棄做空避免支付高昂利息！")
                    return
                # 極端費率防護：負費率超過 -0.5% 時禁止任何新空單
                if side == 'sell' and funding_rate < FUNDING_RATE_SHORT_BLOCK:
                    print(f"🛑 [極端費率防護] 資金費率 {funding_rate*100:.3f}% < {FUNDING_RATE_SHORT_BLOCK*100:.1f}%，空單持倉利息過高，強制暫停開空！")
                    return
            except Exception as e:
                print(f"⚠️ [防護攔截] 檢查資金費率失敗: {e}")

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
        dynamic_max_position = current_balance * LEVERAGE
    
        # 計算剩餘可下單額度 (扣除已使用保證金的槓桿部位)
        current_position_usd = abs(simulated_base_amt) * current_p
        available_margin = dynamic_max_position - current_position_usd
    
        if available_margin <= 0:
            print(f"⚠️ [風控攔截] 模擬倉位已達上限 {dynamic_max_position:.2f} USDT，暫停加倉！")
            return
    
        # 0.5 買賣價差滑點防護 (Spread Slippage Protection)
        try:
            ob = await exchange.fetch_order_book(symbol, limit=5)
            bid = float(ob['bids'][0][0])
            ask = float(ob['asks'][0][0])
            spread_pct = (ask - bid) / bid
            if spread_pct > 0.005:
                print(f"🛑 [防護攔截] 買賣價差過大 ({spread_pct*100:.2f}%)，放棄開倉避免嚴重滑點！")
                return
        except Exception as e:
            print(f"⚠️ [防護攔截] 檢查買賣價差失敗: {e}")

        # 依照使用者要求：帳面有多少就下多少單 (複利 All-in)
        # 使用可用餘額的 95% 作為開倉額度，預留 5% 作為緩衝避免因市價滑點而保證金不足
        actual_quote_amount = available_margin * 0.95
        
        if actual_quote_amount < 6.0:
            print(f"🛑 [防護攔截] 剩餘可用額度 {actual_quote_amount:.2f} USDT 低於幣安最低名目價值限制 (5 USDT)，強制放棄避免產生孤兒倉位")
            return
            
        print(f"💰 [{macro_regime}] 下單 {actual_quote_amount:.2f} USDT (設定:{quote_amount:.0f}, 可用:{available_margin:.2f})")
        print(f"@@AMOUNT@@{actual_quote_amount}")
    
        try:
            base_amt = await get_base_amount(actual_quote_amount)
            direction_str = "做多(Long)" if side == 'buy' else "做空(Short)"
            print(f"\n🛒 [下單模組] 💡 訊號觸發！{direction_str} {actual_quote_amount:.2f} USDT -> 數量: {base_amt:.6f}")
            
            if PAPER_TRADING:
                print(f"📋 [模擬盤提醒] 實盤將採用 30%/30%/40% 智能追價與網格掛單。為求簡化，模擬盤全數以市價 ({price}) 立即成交。")
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
                
                # 獲取該幣種的配置 (包含 num_splits, step_percent, coin_type 等)
                coin_config = {
                    "is_simulated": False,
                    "split_threshold": 100.0,
                    "num_splits": 5,
                    "step_percent": 0.001,
                    "coin_type": "HighVolatility" if symbol in HIGH_VOL_LIST else "Normal",
                    "fee_rate": 0.001,
                    "slippage_model": 0.0005
                }

                print(f"🚀 [引擎驅動] 調用 ExecutionEngine 處理 {direction_str} {actual_quote_amount} USDT")
                
                # 調用新引擎執行 (這會處理分批、動態補單、自適應步長)
                executed_orders = await execution_engine.execute_order(
                    symbol=symbol,
                    side=side,
                    total_quantity=base_amt,
                    target_price=price, 
                    config=coin_config
                )

                # 彙整所有分批單的成交結果
                total_filled_qty = 0.0
                weighted_price_sum = 0.0
                
                for order in executed_orders:
                    if order.status in [OrderStatus.FILLED, OrderStatus.PARTIAL]:
                        total_filled_qty += order.filled_quantity
                        weighted_price_sum += (order.avg_price * order.filled_quantity)
                
                if total_filled_qty > 0:
                    final_avg_price = weighted_price_sum / total_filled_qty
                    print(f"✅ [下單模組] 引擎執行完成！實際成交均價: {final_avg_price} | 總成交數量: {total_filled_qty}")
                    
                    # 更新全域狀態
                    avg_price = final_avg_price
                    
                    if side == 'buy':
                        simulated_base_amt += total_filled_qty
                    else:
                        simulated_base_amt -= total_filled_qty
                    
                    simulated_avg_price = avg_price
                    
                    # 記錄單號 (取第一筆成功成交的單號作為參考)
                    open_order = next((o for o in executed_orders if o.status == OrderStatus.FILLED), None)
                    order_id = open_order.order_id if open_order else "N/A"
                    print(f"✅ [下單模組] 開倉成功！實際成交均價: {avg_price} | 單號: {order_id}")
                else:
                    raise Exception("ExecutionEngine 報告：所有分批訂單均未成交。")

                # --- 掛載實體停損與停利單 (保命單) ---
                try:
                    tp_price = round_price(avg_price * 1.002 if side == 'buy' else avg_price * 0.998, prec['tick_size'])
                    sl_price = round_price(avg_price * 0.970 if side == 'buy' else avg_price * 1.030, prec['tick_size'])
                    sl_side = 'sell' if side == 'buy' else 'buy'
                    
                    await asyncio.gather(
                        exchange.create_order(
                            symbol=symbol, type='TAKE_PROFIT_MARKET', side=sl_side,
                            amount=qty_str, price=tp_price,
                            params={'stopPrice': tp_price, 'closePosition': True, 'marginMode': 'isolated'}
                        ),
                        exchange.create_order(
                            symbol=symbol, type='STOP_MARKET', side=sl_side,
                            amount=qty_str, price=sl_price,
                            params={'stopPrice': sl_price, 'closePosition': True, 'marginMode': 'isolated'}
                        ),
                        return_exceptions=True
                    )
                    print(f"🛡️ [保命防護] 已成功掛載實體停利 (+0.2%): {tp_price} 與實體停損 (-3.0%): {sl_price}")
                except Exception as e:
                    print(f"⚠️ [保命防護] 掛載實體停利停損失敗: {e}")
            
            # 更新活動時間
            global last_action_time
            last_action_time = time.time()

            # 重置移動停利狀態，避免舊數值污染新倉位
            reset_trailing_stops()

            # 任何新倉位成功建立後，重置空單停利監控狀態
            global short_tp_support_level
            short_tp_support_level = 0.0

        except Exception as e:
            print(f"🚨 [下單/風控模組嚴重致命錯誤]: {e}")
            if PAPER_TRADING:
                simulated_base_amt = 0.0

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
            await asyncio.sleep(5.0)  # 放慢實盤查詢頻率
        except Exception as e:
            await asyncio.sleep(5.0)  # 放慢實盤查詢頻率

async def close_entire_position(close_side, actual_close_amt, current_p, pos_avg, force_market=False):
    global simulated_base_amt
    last_exit_time[SYMBOL_KEY] = time.time()  # 紀錄平倉時間，啟動開倉冷卻
    if PAPER_TRADING:
        close_pnl = (current_p - pos_avg) * actual_close_amt if close_side == 'sell' else (pos_avg - current_p) * actual_close_amt
        simulated_base_amt = simulated_base_amt - actual_close_amt if close_side == 'sell' else simulated_base_amt + actual_close_amt
        if abs(simulated_base_amt) < 0.000001:
            simulated_base_amt = 0.0
            print(f"✅ [模擬平倉成功] 全倉已平！盈虧: {close_pnl:.4f} USDT")
        else:
            print(f"✅ [模擬平倉成功] 部分平倉！盈虧: {close_pnl:.4f} USDT，剩餘倉位: {simulated_base_amt:.6f}")
        update_paper_state(SYMBOL_KEY, close_side, current_p, actual_close_amt, is_close=True, pnl=close_pnl)
        reset_trailing_stops()
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
                    if not await check_market_slippage(close_side, is_market_order=True):
                        print(f"🛑 [滑價防護] 平倉盤口異常且滑價過大，放棄市價平倉！")
                        raise Exception("滑價超標，平倉市價單取消")
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
                if not await check_market_slippage(close_side, is_market_order=True):
                    print(f"🛑 [滑價防護] 強制平倉滑價過大，放棄市價單！")
                    raise Exception("滑價超標，強制平倉取消")
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
            # 平倉後重置移動停利狀態
            reset_trailing_stops()
        except Exception as e:
            print(f"🚨 [平倉錯誤]: {e}")
        

import requests
import json
import os

def get_dynamic_stagnation_limit(current_atr, atr_ma20):
    """根據波動率決定僵局時間
    - 波動低於平均 (死魚行情) → 180s (3min)
    - 波動正常或偏高 → 300s (5min)
    """
    if current_atr < atr_ma20:
        return 180
    return 300


def restore_historical_extremes(symbol, pos_qty, pos_avg, current_p):
    global trailing_highest, trailing_lowest, highest_profit_pct_hardlock
    
    if pos_qty == 0:
        trailing_highest = current_p
        trailing_lowest = current_p
        highest_profit_pct_hardlock = 0.0
        return

    try:
        if os.path.exists("paper_state.json"):
            with open("paper_state.json", "r") as f:
                state = json.load(f)
            
            # Find the last open trade for this symbol
            trades = state.get("trades", [])
            open_time = None
            for t in reversed(trades):
                if t["symbol"] == symbol and not t["is_close"]:
                    open_time = t["time"]
                    break
            
            if open_time:
                url = f"https://fapi.binance.com/fapi/v1/klines?symbol={symbol.replace(':', '')}&interval=1m&startTime={open_time}"
                res = requests.get(url).json()
                
                if isinstance(res, list) and len(res) > 0:
                    highs = [float(k[2]) for k in res]
                    lows = [float(k[3]) for k in res]
                    
                    hist_highest = max(highs)
                    hist_lowest = min(lows)
                    
                    trailing_highest = max(current_p, hist_highest)
                    trailing_lowest = min(current_p, hist_lowest)
                    
                    if pos_qty > 0:
                        max_profit_pct = (trailing_highest - pos_avg) / pos_avg
                    else:
                        max_profit_pct = (pos_avg - trailing_lowest) / pos_avg
                        
                    highest_profit_pct_hardlock = max(0.0, max_profit_pct)
                    print(f"🔄 [歷史還原] 成功還原開倉以來的極值！最高價: {trailing_highest}, 最低價: {trailing_lowest}, 最大利潤: {highest_profit_pct_hardlock*100:.2f}%")
                    return
    except Exception as e:
        print(f"⚠️ [歷史還原失敗] {e}")

    # Fallback
    trailing_highest = current_p
    trailing_lowest = current_p
    if pos_qty > 0:
        highest_profit_pct_hardlock = max(0.0, (current_p - pos_avg) / pos_avg)
    else:
        highest_profit_pct_hardlock = max(0.0, (pos_avg - current_p) / pos_avg)


def reset_trailing_stops():
    global trailing_highest, trailing_lowest, has_reached_half_pct_profit
    global highest_profit_pct_hardlock, has_partial_closed_50pct
    trailing_highest = 0.0
    trailing_lowest = float('inf')
    has_reached_half_pct_profit = False
    highest_profit_pct_hardlock = 0.0
    has_partial_closed_50pct = False
def is_trend_reversed(data, side):
    # 此處需根據您現有的 data 變數來判斷
    # 假設您有 macd_hist 判斷，或是其它反轉指標
    # 這裡放您原本判斷反轉的邏輯
    return data.get('is_reversed', False) 

def is_congestion(data):
    # 盤整判斷邏輯
    return data.get('is_congestion', False)

def process_market_state(symbol, market_data, position):
    if is_trend_reversed(market_data, position['side']):
        return "EXIT_REVERSAL"
    if is_congestion(market_data):
        return "MONITOR_STAGNATION"
    return "HOLD_POSITION"
async def monitor_position_tp_sl():
    """ 獨立監控持倉：波段止盈/止損 """
    global highest_profit_pct_hardlock, current_atr, atr_ma20, position_open_time, has_partial_closed_50pct
    while True:
        try:
            await asyncio.sleep(0.5)
            pos_qty = 0.0
            pos_avg = 0.0
            # 使用全域變數 current_pos_qty 以節省 API 次數
            pos_qty = current_pos_qty
            pos_avg = current_pos_avg

            if abs(pos_qty) <= 0.000001 or pos_avg <= 0:
                continue

            # ⏱️ 開倉緩衝保護：前 10 秒完全不出場，10~120 秒放寬停損
            hold_sec = time.time() - position_open_time if position_open_time > 0 else 9999
            if hold_sec < 10:
                continue
            sl_multiplier = SL_ATR_MULTIPLIER * 2 if hold_sec < 120 else SL_ATR_MULTIPLIER

            ticker = await exchange.fetch_ticker(symbol)
            current_p = ticker['last']

            # 計算動態停利與停損價格
            if TP_SL_MODE == 'ATR':
                atr_val = current_atr if current_atr > 0 else (current_p * 0.01)
                if pos_qty > 0:
                    tp = pos_avg + max((atr_val * TP_ATR_MULTIPLIER), pos_avg * 0.003)
                    sl = pos_avg - (atr_val * sl_multiplier)
                else:
                    tp = pos_avg - max((atr_val * TP_ATR_MULTIPLIER), pos_avg * 0.003)
                    sl = pos_avg + (atr_val * sl_multiplier)
            else:
                # 'RESISTANCE_PCT' 模式
                if global_resistance <= 0 or global_support <= 0:
                    # 避免 0 值導致計算錯誤，給予預設值
                    tp = pos_avg * 1.02 if pos_qty > 0 else pos_avg * 0.98
                    sl = pos_avg * 0.97 if pos_qty > 0 else pos_avg * 1.03
                else:
                    if pos_qty > 0:
                        tp = global_resistance * 0.99  # 停利在壓力位下方 1%
                        sl = global_support * 0.985    # 停損在支撐位下方 1.5%
                        
                        # 保底檢查：避免進場點離目標太近
                        if tp < pos_avg * 1.01: tp = pos_avg * 1.01
                        if sl > pos_avg * 0.99: sl = pos_avg * 0.99
                        
                        # 盈虧比動態對齊：確保預期毛利潤 >= 實際停損距離
                        sl_dist = pos_avg - sl
                        if (tp - pos_avg) < sl_dist:
                            tp = pos_avg + sl_dist
                    else:
                        tp = global_support * 1.01     # 停利在支撐位上方 1%
                        sl = global_resistance * 1.015 # 停損在壓力位上方 1.5%
                        
                        # 保底檢查
                        if tp > pos_avg * 0.99: tp = pos_avg * 0.99
                        if sl < pos_avg * 1.01: sl = pos_avg * 1.01
                        
                        # 盈虧比動態對齊：確保預期毛利潤 >= 實際停損距離
                        sl_dist = sl - pos_avg
                        if (pos_avg - tp) < sl_dist:
                            tp = pos_avg - sl_dist

            tp_pct_val = abs(tp - pos_avg) / pos_avg * 100
            sl_pct_val = abs(sl - pos_avg) / pos_avg * 100

            # ================================================================
            # 🛡️ 鋼鐵防回吐防線 (Ironclad Profit Defense)
            # ================================================================
            if 'highest_profit_pct_hardlock' not in globals():
                highest_profit_pct_hardlock = 0.0

            if pos_qty > 0:
                profit_pct = (current_p - pos_avg) / pos_avg
            else:
                profit_pct = (pos_avg - current_p) / pos_avg

            if profit_pct > highest_profit_pct_hardlock:
                highest_profit_pct_hardlock = profit_pct

            # ================================================================
            # 初始化歷史極值 (僅在重啟或新開倉時執行一次)
            # ================================================================
            global _has_restored_history
            if '_has_restored_history' not in globals():
                _has_restored_history = False
            
            if pos_qty != 0 and not _has_restored_history:
                restore_historical_extremes(SYMBOL_KEY, pos_qty, pos_avg, current_p)
                _has_restored_history = True
            elif pos_qty == 0:
                _has_restored_history = False


            # ================================================================
            # 量化數學移動停利/停損 (基於 ATR)
            # ================================================================
            global trailing_highest, trailing_lowest
            if 'trailing_highest' not in globals():
                trailing_highest = current_p
            if 'trailing_lowest' not in globals():
                trailing_lowest = current_p

            if current_p > trailing_highest:
                trailing_highest = current_p
            if current_p < trailing_lowest:
                trailing_lowest = current_p

            is_long = pos_qty > 0
            
            # ================================================================
            # 趨勢動態停利邏輯 (使用者指定)
            # ================================================================
            global current_coin_trend
            
            # ⏳ 時間/僵局停利邏輯 (Time-Stuck Take Profit) - 兩階段 (動態 ATR 版)
            stagnation_limit = get_dynamic_stagnation_limit(current_atr, atr_ma20)
            # 階段一 (動態 3~5 分鐘)：利潤 0.2%~0.5%，平 50% 倉位
            if position_open_time > 0 and (time.time() - position_open_time) > stagnation_limit:
                if 0.002 <= profit_pct < 0.005:
                    half_qty = abs(pos_qty) * 0.5
                    close_side = 'sell' if is_long else 'buy'
                    print(f"⏳ [僵局一階] 持倉3分鐘利潤僅 {profit_pct*100:.2f}%，平50%釋放資金")
                    await close_entire_position(close_side, half_qty, current_p, pos_avg, force_market=True)
                    has_partial_closed_50pct = True
                    continue
            # 階段二 (8分鐘)：剩餘 50% 仍未突破 1%，全平
            if has_partial_closed_50pct and position_open_time > 0 and (time.time() - position_open_time) > 480:
                if profit_pct < 0.01:
                    close_side = 'sell' if is_long else 'buy'
                    print(f"⏳ [僵局二階] 剩餘50%持倉8分鐘仍未突破1%，全平")
                    await close_entire_position(close_side, abs(pos_qty), current_p, pos_avg, force_market=True)
                    highest_profit_pct_hardlock = 0.0
                    has_partial_closed_50pct = False
                    continue

            # 若當下該幣種趨勢與持倉方向相反 (弱勢 / 逆風)，見好就收：利潤若有 0.5% 就直接出倉
            is_strong = False
            if 'current_coin_trend' in globals():
                is_strong = (is_long and current_coin_trend == "UP") or (not is_long and current_coin_trend == "DOWN")
            
            if not is_strong:
                if highest_profit_pct_hardlock >= 0.005:
                    print(f"🎯 [快速停利] 該幣當前為弱勢 (逆風)，利潤達 0.5%，直接入袋！")
                    close_side = 'sell' if is_long else 'buy'
                    await close_entire_position(close_side, abs(pos_qty), current_p, pos_avg, force_market=True)
                    highest_profit_pct_hardlock = 0.0
                    continue
            else:
                # 若該幣種趨勢與持倉方向相同 (強勢 / 順風)，動態停利：在高點回檔 0.5% 就出倉
                if highest_profit_pct_hardlock >= 0.01:
                    if is_long and current_p <= trailing_highest * 0.995:
                        print(f"🏃 [動態停利] 多單高點回撤 0.5%，強勢趨勢保護出場！")
                        await close_entire_position('sell', abs(pos_qty), current_p, pos_avg, force_market=True)
                        highest_profit_pct_hardlock = 0.0
                        continue
                    elif not is_long and current_p >= trailing_lowest * 1.005:
                        print(f"🏃 [動態停利] 空單低點反彈 0.5%，強勢趨勢保護出場！")
                        await close_entire_position('buy', abs(pos_qty), current_p, pos_avg, force_market=True)
                        highest_profit_pct_hardlock = 0.0
                        continue

            if pos_qty > 0:
                if current_p >= tp:
                    print(f"🎯 [動態停利] 多單均價 {pos_avg:.4f}，現價 {current_p:.4f} >= {tp:.4f} (+{tp_pct_val:.1f}%)，全平！")
                    await close_entire_position('sell', abs(pos_qty), current_p, pos_avg, force_market=True)
                elif current_p <= sl:
                    print(f"🛑 [動態止損] 多單均價 {pos_avg:.4f}，現價 {current_p:.4f} <= {sl:.4f} (-{sl_pct_val:.1f}%)，全平！")
                    await close_entire_position('sell', abs(pos_qty), current_p, pos_avg, force_market=True)
                    _handle_hard_sl()
            else:
                if current_p <= tp:
                    print(f"🎯 [動態停利] 空單均價 {pos_avg:.4f}，現價 {current_p:.4f} <= {tp:.4f} (+{tp_pct_val:.1f}%)，全平！")
                    await close_entire_position('buy', abs(pos_qty), current_p, pos_avg, force_market=True)
                elif current_p >= sl:
                    print(f"🛑 [動態止損] 空單均價 {pos_avg:.4f}，現價 {current_p:.4f} >= {sl:.4f} (-{sl_pct_val:.1f}%)，全平！")
                    await close_entire_position('buy', abs(pos_qty), current_p, pos_avg, force_market=True)
                    _handle_hard_sl()

        except Exception as e:
            print(f"⚠️ [監控模組錯誤] {e}")

def _handle_hard_sl():
    """ 處理單幣熔斷計數器 """
    global hard_sl_count, last_hard_sl_reset
    now = time.time()
    if now - last_hard_sl_reset > 24 * 3600:
        hard_sl_count = 0
        last_hard_sl_reset = now
    
    hard_sl_count += 1
    if hard_sl_count >= 2:
        print(f"🔥 [單幣熔斷停牌] 該幣種在 24 小時內連續觸發 2 次硬停損，啟動熔斷停牌程序！")
        sys.exit(4)

# =====================================================================
# ① 行情接收模組 & ② 策略邏輯模組
# =====================================================================
async def monitor_macro_trend():
    """ 週期性檢查 1H 級別的 20T 均線，並更新全局壓力/支撐位 """
    global macro_regime, current_pos_qty, current_1h_deviation
    global global_resistance, global_support
    global ALLOW_LONG, ALLOW_SHORT
    global global_sma200_1h
    while True:
        try:
            # 1. 更新壓力與支撐位
            if USE_DYNAMIC_N_DAY_EXTREMES:
                try:
                    global is_narrow_range_mode
                    is_narrow = 'is_narrow_range_mode' in globals() and is_narrow_range_mode
                    # 抓取 N 日 K 線 (1d) 或 降維為 1h
                    tf_sr = '1h' if is_narrow else '1d'
                    limit_sr = 24 if is_narrow else N_DAYS_FOR_EXTREMES
                    daily_ohlcv = await exchange.fetch_ohlcv(symbol, timeframe=tf_sr, limit=limit_sr)
                    if daily_ohlcv:
                        highs = [x[2] for x in daily_ohlcv]
                        lows = [x[3] for x in daily_ohlcv]
                        global_resistance = max(highs)
                        global_support = min(lows)
                except Exception as e:
                    print(f"⚠️ 無法獲取極值: {e}")
            else:
                manual_cfg = MANUAL_CEILINGS.get(symbol, {"ceiling": 0.0, "floor": 0.0})
                global_resistance = manual_cfg.get("ceiling", 0.0)
                global_support = manual_cfg.get("floor", 0.0)

            # 2. 抓取 BTC 大盤 (降維為 15m 或維持 1h)
            is_narrow = 'is_narrow_range_mode' in globals() and is_narrow_range_mode
            timeframe_macro = '15m' if is_narrow else '1h'
            btc_ohlcv = await exchange.fetch_ohlcv('BTCUSDT', timeframe=timeframe_macro, limit=210)
            if len(btc_ohlcv) >= 200:
                btc_closes = np.array([x[4] for x in btc_ohlcv])
                btc_sma = np.mean(btc_closes[-200:])
                btc_deviation = (btc_closes[-1] - btc_sma) / btc_sma
            else:
                btc_deviation = 0.0

            # 抓取當前交易幣種
            ohlcv = await exchange.fetch_ohlcv(symbol, timeframe=timeframe_macro, limit=210)
            if len(ohlcv) >= 200:
                closes = np.array([x[4] for x in ohlcv])
                current_price = closes[-1]
                sma_200 = np.mean(closes[-200:])
                global_sma200_1h = sma_200
                
                # 計算幣種偏離度
                coin_deviation = (current_price - sma_200) / sma_200
                
                old_regime = macro_regime
                old_dev = current_1h_deviation
                
                # 邏輯：以個別幣種本身的走勢為最高判斷標準 (回應使用者需求：大盤跌不代表每個幣都在跌)
                if coin_deviation > 0.02:
                    macro_regime = "BULL"
                elif coin_deviation < -0.02:
                    macro_regime = "BEAR"
                else:
                    macro_regime = "MONKEY"
                print_dev = coin_deviation
                
                current_1h_deviation = print_dev

                # 恢復趨勢過濾：牛市只做多，熊市只做空，猴市雙向皆可
                ALLOW_LONG = (macro_regime in ["BULL", "MONKEY"])
                ALLOW_SHORT = (macro_regime in ["BEAR", "MONKEY"])

                await update_dynamic_leverage()
                
                if old_regime != macro_regime or abs(old_dev - current_1h_deviation) > 0.03:
                    try:
                        print(f"@@REGIME@@{macro_regime} ({current_1h_deviation*100:.1f}%)")
                        print(f"@@COIN_REGIME@@{symbol}@@{macro_regime}")
                        print(f"@@LEVERAGE@@{LEVERAGE}")
                        sys.stdout.flush()
                    except:
                        pass
                    print(f"🌍 [環境感知] {macro_regime} | 偏離: {print_dev*100:.2f}% | 槓桿: {LEVERAGE}x")

        except Exception as e:
            print(f"⚠️ [環境感知] 無法獲取 1H 趨勢: {e}")
        
        # 每 5 分鐘檢查一次
        await asyncio.sleep(300)
        print(f"💓 [心跳] bot 運行中 | 持倉: {current_pos_qty:.4f} | 狀態: {macro_regime}")

# =====================================================================
# 數學量化指標計算模組
# =====================================================================
def calculate_ema(prices, period):
    if len(prices) == 0: return np.array([])
    ema = np.zeros(len(prices))
    ema[0] = prices[0]
    alpha = 2 / (period + 1)
    for i in range(1, len(prices)):
        ema[i] = (prices[i] - ema[i-1]) * alpha + ema[i-1]
    return ema

def calculate_macd(prices, fast_period=12, slow_period=26, signal_period=9):
    if len(prices) < slow_period:
        return 0, 0, 0
    fast_ema = calculate_ema(prices, fast_period)
    slow_ema = calculate_ema(prices, slow_period)
    macd_line = fast_ema - slow_ema
    signal_line = calculate_ema(macd_line, signal_period)
    macd_hist = macd_line - signal_line
    return macd_line[-1], signal_line[-1], macd_hist[-1], macd_line[-2], signal_line[-2]

def calculate_bollinger_bands(prices, period=20, std_dev=2.0):
    if len(prices) < period:
        return 0, 0, 0
    sma = np.mean(prices[-period:])
    std = np.std(prices[-period:])
    upper_band = sma + (std_dev * std)
    lower_band = sma - (std_dev * std)
    return upper_band, sma, lower_band

def calculate_adx(highs, lows, closes, period=14):
    if len(closes) < period + 1:
        return 0.0
    tr_list = []
    plus_dm_list = []
    minus_dm_list = []
    for i in range(1, len(closes)):
        high_diff = highs[i] - highs[i-1]
        low_diff = lows[i-1] - lows[i]
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        tr_list.append(tr)
        plus_dm = high_diff if high_diff > low_diff and high_diff > 0 else 0
        minus_dm = low_diff if low_diff > high_diff and low_diff > 0 else 0
        plus_dm_list.append(plus_dm)
        minus_dm_list.append(minus_dm)
    if len(tr_list) < period:
        return 0.0
    atr = np.mean(tr_list[:period])
    plus_di = 100 * np.mean(plus_dm_list[:period]) / atr if atr > 0 else 0
    minus_di = 100 * np.mean(minus_dm_list[:period]) / atr if atr > 0 else 0
    dx_list = []
    for i in range(period, len(tr_list)):
        atr = (atr * (period - 1) + tr_list[i]) / period
        if atr > 0:
            plus_di = (plus_di * (period - 1) + 100 * plus_dm_list[i] / atr) / period
            minus_di = (minus_di * (period - 1) + 100 * minus_dm_list[i] / atr) / period
        di_sum = plus_di + minus_di
        dx = 100 * abs(plus_di - minus_di) / di_sum if di_sum > 0 else 0
        dx_list.append(dx)
    adx = np.mean(dx_list[-period:]) if len(dx_list) >= period else np.mean(dx_list) if dx_list else 0
    return adx

async def watch_kline_and_strategy():
    """ 透過 WebSocket 監聽 1分K，並用 NumPy 計算布林插針策略 """
    print("🚀 [行情模組一] 開始監聽 WebSocket K線數據 (正在預載歷史數據...)")
    global current_atr, atr_history, atr_ma20, current_rsi, current_pos_qty, short_tp_support_level
    global last_order_signal_time, is_narrow_range_mode
    global global_sma200_1h
    if 'last_order_signal_time' not in globals():
        last_order_signal_time = time.time()
    if 'is_narrow_range_mode' not in globals():
        is_narrow_range_mode = False

    prev_close = None
    tr_list = []
    last_buy_time = 0
    wait_bar_counter = 0
    last_kline_ts = 0
    
    # 二次確認機制變數
    pending_signal_side = None
    pending_signal_time = 0
    pending_confirm_high = 0
    pending_confirm_low = 0
    
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
    
    global last_market_condition
    
    while True:
        Current_Status = ""
        try:
            # === [跨市況轉訊號即時跟進與舊單清理模組] ===
            if macro_regime != last_market_condition:
                print(f"🚨🚨 [大盤變天] 訊號由 {last_market_condition} 轉為 {macro_regime}！")
                # 無情清倉
                if current_pos_qty > 0:
                    print(f"🧹 舊市況殘留多單強制退場：{symbol}")
                    fallback_price = closes_history[-1] if len(closes_history) > 0 else (ohlcv_buffer[-1][4] if ohlcv_buffer else 0)
                    await close_entire_position('sell', abs(current_pos_qty), current_p if 'current_p' in locals() else fallback_price, current_pos_avg, force_market=True)
                elif current_pos_qty < 0:
                    print(f"🧹 舊市況殘留空單強制退場：{symbol}")
                    fallback_price = closes_history[-1] if len(closes_history) > 0 else (ohlcv_buffer[-1][4] if ohlcv_buffer else 0)
                    await close_entire_position('buy', abs(current_pos_qty), current_p if 'current_p' in locals() else fallback_price, current_pos_avg, force_market=True)
                
                # 清除掛單 (若有)
                try:
                    if not PAPER_TRADING:
                        await exchange.cancel_all_orders(symbol)
                except Exception as ex:
                    print(f"⚠️ 清除掛單失敗: {ex}")
                
                last_market_condition = macro_regime
                print(f"🚀 [順勢跟進] 策略已全自動切換為 {macro_regime} 模式，全速進擊！")
            
            try:
                new_ohlcv = await asyncio.wait_for(
                    exchange.watch_ohlcv(symbol, timeframe), timeout=10.0
                )
            except asyncio.TimeoutError:
                # WebSocket 超時：使用上一次緩衝繼續執行策略，不卡死主循環
                new_ohlcv = []
            
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
                atr_history.append(current_atr)
                if len(atr_history) > 20:
                    atr_history = atr_history[-20:]
                if len(atr_history) >= 20:
                    atr_ma20 = float(np.mean(atr_history))
                else:
                    atr_ma20 = current_atr

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

            # 🚀 [計算全局指標與短線趨勢]
            global current_coin_trend
            macd_line, macd_signal, macd_hist, prev_macd_line, prev_macd_signal = calculate_macd(closes, 12, 26, 9)
            bb_up, bb_mid, bb_low = calculate_bollinger_bands(closes, 20, 2.0)
            prev_macd_hist = prev_macd_line - prev_macd_signal
            
            if close_price > bb_mid:
                current_coin_trend = "UP"
            else:
                current_coin_trend = "DOWN"

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

                global has_reached_half_pct_profit, has_partial_closed_50pct
                if 'has_reached_half_pct_profit' not in globals():
                    has_reached_half_pct_profit = False
                if 'has_partial_closed_50pct' not in globals():
                    has_partial_closed_50pct = False
                    
                
                # ================================================================
                # 量化數學策略：MACD + RSI + BB + ATR
                # ================================================================
                # (指標已在迴圈前端全局計算)
                
                # 改為 10% 絕對停損，不使用 ATR (因為太容易被震掉)
                hard_sl_distance = current_pos_avg * 0.10
                tp_distance = current_atr * 3.0

                if is_long and close_price <= current_pos_avg - hard_sl_distance:
                    close_signal = True; close_reason = "⛔ [10%停損] 多單觸及停損線"
                elif not is_long and close_price >= current_pos_avg + hard_sl_distance:
                    close_signal = True; close_reason = "⛔ [10%停損] 空單觸及停損線"
                elif is_long and close_price >= current_pos_avg + tp_distance:
                    close_signal = True; close_reason = "🎯 [ATR停利] 多單達到目標"
                elif not is_long and close_price <= current_pos_avg - tp_distance:
                    close_signal = True; close_reason = "🎯 [ATR停利] 空單達到目標"

                # 🚀 [智能防禦搶救機制] 提早動態停損
                # 1. 指標反轉停損：如果做多但隨後出現「死叉+超買回落」，且已經處於虧損狀態，不等 10% 提早跑路
                if not close_signal and is_long and profit_pct < -0.01 and current_rsi < 50 and (prev_macd_line > prev_macd_signal and macd_line < macd_signal):
                    close_signal = True; close_reason = "📉 [反轉搶救] 多單趨勢轉弱(MACD死叉)，提早認賠！"
                if not close_signal and not is_long and profit_pct < -0.01 and current_rsi > 50 and (prev_macd_line < prev_macd_signal and macd_line > macd_signal):
                    close_signal = True; close_reason = "📈 [反轉搶救] 空單趨勢轉強(MACD金叉)，提早認賠！"

                # 2. 時間停損：持倉超過 15 分鐘 (900 秒) 且依然處於 >1% 的虧損，代表動能耗盡，果斷平倉
                if not close_signal and (time.time() - position_open_time > 900) and profit_pct < -0.01:
                    close_signal = True; close_reason = f"⏱️ [時間停損] 持倉已達 {(time.time()-position_open_time)/60:.1f} 分鐘仍虧損，解除僵局！"

                if close_signal:
                    print(f"⚠️ [量化平倉] {close_reason}，執行市價平倉！")
                    close_side = 'sell' if is_long else 'buy'
                    await close_entire_position(close_side, abs(current_pos_qty), close_price, current_pos_avg)
                    current_pos_qty = 0.0
                    global last_action_time
                    last_action_time = time.time()
                    continue

            current_time = time.time()
            if abs(current_pos_qty) < 0.000001 and current_time - last_buy_time > 5:
                # (指標已在迴圈前端全局計算)

                # ── 三層濾網檢查 ────────────────────────────────────
                adx_val = calculate_adx(
                    [x[2] for x in ohlcv],
                    [x[3] for x in ohlcv],
                    [x[4] for x in ohlcv]
                )

                def is_entry_allowed(side, is_counter_trend=False):
                    if is_counter_trend:
                        return True, "反轉搶短，無視趨勢濾網"
                    if global_sma200_1h > 0:
                        if side == 'buy' and close_price <= global_sma200_1h:
                            return False, f"SMA200={global_sma200_1h:.4f} 之上才允許多，當前={close_price:.4f}"
                        if side == 'sell' and close_price >= global_sma200_1h:
                            return False, f"SMA200={global_sma200_1h:.4f} 之下才允許空，當前={close_price:.4f}"
                    if adx_val < 15:  # 放寬至 15
                        return False, f"ADX={adx_val:.1f} < 15，盤整不開倉"
                    if current_vol < vol_ma20 * 0.7:  # 放寬至 70%
                        return False, f"量能不足 {current_vol:.0f} < {vol_ma20*0.7:.0f}(均量70%)"
                    return True, ""

                def is_symbol_tradable(sym_key):
                    now = time.time()
                    if sym_key in last_exit_time and (now - last_exit_time[sym_key]) < 300:
                        return False
                    return True

                # ── 確認 K 線 (MACD 金叉/死叉專用) ─────────────────
                macd_triggered = (prev_macd_line <= prev_macd_signal and macd_line > macd_signal) or \
                                 (prev_macd_line >= prev_macd_signal and macd_line < macd_signal)
                if macd_triggered and len(ohlcv) >= 2:
                    # 存下確認價格水準，供整個 pending 期間使用
                    pending_confirm_high = ohlcv[-2][2]
                    pending_confirm_low = ohlcv[-2][3]
                elif pending_signal_side is None:
                    pending_confirm_high = pending_confirm_low = 0

# --- [加權計分制進場 + 反轉搶短] ---
                is_counter_trend_long = current_rsi < 25
                is_counter_trend_short = current_rsi > 75

                # 多單加權計分 (5 項指標各 1 分，满足 ≥3 分即可開倉)
                score_long = 0
                if macd_line > macd_signal:          score_long += 1
                if close_price > global_sma200_1h:   score_long += 1
                if current_rsi < 60:                 score_long += 1
                if current_vol > vol_ma20 * 0.7:     score_long += 1
                if macro_regime != "BEAR":            score_long += 1

                # 空單加權計分
                score_short = 0
                if macd_line < macd_signal:          score_short += 1
                if close_price < global_sma200_1h:   score_short += 1
                if current_rsi > 40:                 score_short += 1
                if current_vol > vol_ma20 * 0.7:     score_short += 1
                if macro_regime != "BULL":            score_short += 1

                long_cond  = (score_long  >= 3) or (current_rsi < 40 and close_price <= bb_low * 1.005) or is_counter_trend_long
                short_cond = (score_short >= 3) or (current_rsi > 60 and close_price >= bb_up  * 0.995) or is_counter_trend_short
                # ----------------------------------------

                if long_cond:
                    allowed, reason = is_entry_allowed('buy', is_counter_trend_long)
                    if not allowed:
                        if pending_signal_side is not None:
                            pending_signal_side = None
                        continue
                    if not is_symbol_tradable(SYMBOL_KEY):
                        if pending_signal_side is not None:
                            pending_signal_side = None
                        print(f"⏳ [冷卻] {SYMBOL_KEY} 剛平倉未滿 5 分鐘，跳過")
                        continue
                    if pending_signal_side != 'buy':
                        print(f"⏳ [二次確認-多] RSI={current_rsi:.1f}，等待 3 秒確認...")
                        pending_signal_side = 'buy'
                        pending_signal_time = current_time
                    elif current_time - pending_signal_time >= 3:
                        if pending_confirm_high > 0 and close_price <= pending_confirm_high:
                            print(f"⏳ [確認K線-多] MACD金叉尚未突破前高 {pending_confirm_high:.4f}，繼續等待")
                            continue
                        print(f"🟢 [量化做多] 加權計分層過濾通過！score={score_long}/5 | RSI={current_rsi:.1f}, ADX={adx_val:.1f}, SMA200={global_sma200_1h:.4f}")
                        last_buy_time = current_time
                        asyncio.create_task(execute_order_and_risk(side='buy', price=close_price))
                        pending_signal_side = None
                elif short_cond:
                    allowed, reason = is_entry_allowed('sell', is_counter_trend_short)
                    if not allowed:
                        if pending_signal_side is not None:
                            pending_signal_side = None
                        continue
                    if not is_symbol_tradable(SYMBOL_KEY):
                        if pending_signal_side is not None:
                            pending_signal_side = None
                        print(f"⏳ [冷卻] {SYMBOL_KEY} 剛平倉未滿 5 分鐘，跳過")
                        continue
                    if pending_signal_side != 'sell':
                        print(f"⏳ [二次確認-空] RSI={current_rsi:.1f}，等待 3 秒確認...")
                        pending_signal_side = 'sell'
                        pending_signal_time = current_time
                    elif current_time - pending_signal_time >= 3:
                        if pending_confirm_low > 0 and close_price >= pending_confirm_low:
                            print(f"⏳ [確認K線-空] MACD死叉尚未跌破前低 {pending_confirm_low:.4f}，繼續等待")
                            continue
                        print(f"🔴 [量化做空] 加權計分層過濾通過！score={score_short}/5 | RSI={current_rsi:.1f}, ADX={adx_val:.1f}, SMA200={global_sma200_1h:.4f}")
                        last_buy_time = current_time
                        asyncio.create_task(execute_order_and_risk(side='sell', price=close_price))
                        pending_signal_side = None
                else:
                    if pending_signal_side is not None:
                        pending_signal_side = None

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
            try:
                await exchange.fapiPrivate_post_positionside_dual({'dualSidePosition': 'false'})
                print("✅ [防護設定] 已確認或強制切換為「單向持倉模式」")
            except Exception as e:
                if 'No need to change position side' in str(e):
                    print("✅ [防護設定] 當前已是「單向持倉模式」")
                else:
                    print(f"⚠️ [防護設定] 強制單向持倉失敗 (可能因為已有持倉): {e}")

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
    print(f"@@COIN_REGIME@@{symbol}@@{macro_regime}") # 初始化專屬幣種狀態
    print(f"@@AMOUNT@@{default_amount}") # 初始化發送金額
    print(f"@@LEVERAGE@@{LEVERAGE}") # 初始化發送槓桿
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
