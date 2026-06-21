POST_CLOSE_COOLDOWN_SEC = 60
trading_locks = {}
POST_CLOSE_COOLDOWN_SEC = 60
trading_locks = {}
POST_CLOSE_COOLDOWN_SEC = 60
trading_locks = {}
POST_CLOSE_COOLDOWN_SEC = 60
trading_locks = {}
import asyncio
import ccxt
import ccxt.pro as ccxtpro
import numpy as np
import json
import os
import signal
import time
import sys
import uuid
import fcntl
import math
import requests
import traceback
from dotenv import load_dotenv
from services.utils import paper_key
from update_paper_state import update_paper_state
import csv
from services.exit_manager import ExitManager
from src.execution_engine import ExecutionEngine

load_dotenv()

# --- 網絡併發限制 ---
_sem = None

def get_network_sem():
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(5)
    return _sem

async def fetch_ohlcv_with_sem(exchange, *args, **kwargs):
    sem = get_network_sem()
    async with sem:
        return await exchange.fetch_ohlcv(*args, **kwargs)


# --- 通知設定 ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

def send_alert(message):
    """發送緊急告警到 Telegram"""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"⚠️ [通知失敗] 未設定 TELEGRAM_TOKEN 或 TELEGRAM_CHAT_ID，僅輸出到 Log: {message}")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": f"🚨 [機器人警報]\n{message}"}
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"⚠️ [通知失敗] 無法發送 Telegram 訊息: {e}")

LOCK_FILE = f"/tmp/binance_bot_single_instance_{os.path.basename(os.path.dirname(os.path.abspath(__file__)))}.lock"
lock_file_handle = None


def _process_exists(pid):
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _terminate_process(pid):
    try:
        os.kill(pid, signal.SIGTERM)
        time.sleep(0.2)
        if _process_exists(pid):
            os.kill(pid, signal.SIGKILL)
        return True
    except Exception:
        return False


def ensure_single_instance():
    global lock_file_handle
    lock_file_handle = open(LOCK_FILE, "a+")
    try:
        fcntl.flock(lock_file_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_file_handle.seek(0)
        lock_file_handle.truncate()
        lock_file_handle.write(str(os.getpid()))
        lock_file_handle.flush()
        return
    except IOError:
        lock_file_handle.seek(0)
        pid_text = lock_file_handle.read().strip()
        stale_pid = None
        try:
            stale_pid = int(pid_text)
        except Exception:
            stale_pid = None

        if stale_pid and stale_pid != os.getpid() and _process_exists(stale_pid):
            print(f"⚠️ 偵測到系統中已有另一個機器人正在執行 (PID={stale_pid})，將自動終止舊進程並繼續啟動...")
            _terminate_process(stale_pid)
            try:
                lock_file_handle.close()
            except Exception:
                pass
            try:
                os.remove(LOCK_FILE)
            except FileNotFoundError:
                pass
            except Exception:
                pass
            lock_file_handle = open(LOCK_FILE, "a+")
            try:
                fcntl.flock(lock_file_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                lock_file_handle.seek(0)
                lock_file_handle.truncate()
                lock_file_handle.write(str(os.getpid()))
                lock_file_handle.flush()
                return
            except IOError:
                pass
        elif stale_pid and stale_pid != os.getpid() and not _process_exists(stale_pid):
            print(f"⚠️ 偵測到鎖定進程 PID={stale_pid} 已不存在，清理過期鎖檔並繼續啟動...")
            try:
                lock_file_handle.close()
            except Exception:
                pass
            try:
                os.remove(LOCK_FILE)
            except FileNotFoundError:
                pass
            except Exception:
                pass
            lock_file_handle = open(LOCK_FILE, "a+")
            try:
                fcntl.flock(lock_file_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                lock_file_handle.seek(0)
                lock_file_handle.truncate()
                lock_file_handle.write(str(os.getpid()))
                lock_file_handle.flush()
                return
            except IOError:
                pass

        print("🚨 錯誤: 偵測到系統中已有另一個機器人正在執行！")
        print("💡 為了避免重複下單與邏輯衝突，本次啟動已自動攔截並退出。")
        print("💡 提示: 若是意外關閉舊程式，請先刪除過期的鎖定檔 /tmp/binance_bot_single_instance.lock，再重新啟動。")
        sys.exit(1)

if __name__ == "__main__":
    ensure_single_instance()

exchange_futures = ccxtpro.binance({
    'apiKey': os.getenv('BINANCE_API_KEY') or None,
    'secret': os.getenv('BINANCE_API_SECRET') or None,
    'enableRateLimit': True,
    'rateLimit': 1000,
    'options': {
        'defaultType': 'future',
        'watchOrderBookSnapshot': True,
    },
})

exchange_spot = ccxtpro.binance({
    'apiKey': os.getenv('BINANCE_API_KEY') or None,
    'secret': os.getenv('BINANCE_API_SECRET') or None,
    'enableRateLimit': True,
    'rateLimit': 1000,
    'options': {
        'defaultType': 'spot',
        'watchOrderBookSnapshot': True,
    },
})

USE_TESTNET = os.getenv("USE_TESTNET", "True").lower() in ("true", "1", "yes")
PAPER_TRADING = True
TIMEFRAME = '1m'
MAX_GLOBAL_DRAWDOWN_PCT = 0.10  # 總帳戶浮動損益虧損超過 10% 時觸發

# --- 相關性群組設定 ---
CORRELATION_GROUPS = {
    "Mainstream": ["BTC", "ETH", "SOL", "BNB", "XRP"],  # 主流/大市值幣種
    "Metaverse_Gaming": ["AXS", "ALICE", "SAND"],      # 元宇宙與鏈遊板塊
    "AI_Tech": ["TAO", "FET", "NEAR", "RENDER"]        # AI 與基礎設施板塊
}

# 設定每個群組允許的最大同時持倉數
MAX_POSITIONS_PER_GROUP = 2  # 每個群組最多同時持有 2 個相關幣種

# --- 動態倉位管理設定 ---
RISK_PER_TRADE_PCT = 0.01   # 每筆交易允許損失的最大比例（1% 總餘額）
MAX_NOTIONAL_PCT   = 0.20   # 單筆名義部位上限（不超過總餘額 20% × 槓桿）
# ----------------------

# --- 追蹤止損加速設定 ---
TRAILING_ACCEL_ENABLED = True  # 啟用動態追蹤加速：獲利越多，追蹤距離越緊
# 追蹤倍數階梯：[利潤門檻, ATR 追蹤倍數]
TRAILING_ACCEL_TIERS = [
    (0.05,  0.8),   # 獲利 >= 5%  → 0.8 ATR (極緊，防大回吐)
    (0.03,  1.0),   # 獲利 >= 3%  → 1.0 ATR (收緊)
    (0.015, 1.2),   # 獲利 >= 1.5% → 1.2 ATR (適中)
    (0.0,   1.5),   # 其他         → 1.5 ATR (原有寬鬆值)
]
# ----------------------

TRADE_HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trade_history.json")

def record_trade_result(symbol, entry_reason, exit_reason, profit_pct, current_atr, max_profit_reached=0.0,
                        expected_entry=0.0, expected_exit=0.0, actual_entry=0.0, actual_exit=0.0, fees=0.0, qty=0.0):
    """
    將每筆交易的結果記錄到 trade_history.json 中，供 AI 後續分析。
    包含預期價格、實際價格與摩擦損耗（Friction Rate）。
    """
    history_file = TRADE_HISTORY_FILE
    
    # 計算進場與出場的滑價 (Slippage)
    entry_slippage = abs(actual_entry - expected_entry) if expected_entry > 0 else 0.0
    exit_slippage = abs(actual_exit - expected_exit) if expected_exit > 0 else 0.0
    total_slippage = entry_slippage + exit_slippage
    
    # 計算總摩擦力 (Total Friction = 總滑價金額 + 手續費)
    # 滑價金額 = (進場滑價 * 數量) + (出場滑價 * 數量)
    slippage_cost = total_slippage * qty if qty > 0 else 0.0
    total_friction = slippage_cost + fees
    
    # 計算摩擦力佔比 (Friction Rate) - 佔總交易金額 (進場價值) 的百分比
    total_value = actual_entry * qty if (actual_entry > 0 and qty > 0) else 1.0
    friction_rate = (total_friction / total_value) * 100 if total_value > 0 else 0.0

    # 計算理想狀態下的獲利與實際獲利落差
    # 理想獲利 = (預期出場 - 預期進場) * 數量 (買多為正，做空相反)
    # 實際獲利百分比 profit_pct 已經由外部傳入
    
    # 準備要記錄的數據
    trade_data = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "symbol": symbol,
        "entry_reason": entry_reason or "UNKNOWN",      # 例如: "Route_A", "Exhaustion_Entry"
        "exit_reason": exit_reason,        # 例如: "MMP_Blocked", "ATR_Stop", "Layer_1_Divergence"
        "profit_pct": round(profit_pct, 4), # 實質獲利百分比
        "max_profit_reached": round(max_profit_reached, 4), # 最高觸及獲利
        "atr_at_exit": round(current_atr, 6),
        "market_mode": "High_Vol" if current_atr > 0.005 else "Low_Vol", # 自動標記市場環境
        "expected_entry": round(expected_entry, 6),
        "expected_exit": round(expected_exit, 6),
        "actual_entry": round(actual_entry, 6),
        "actual_exit": round(actual_exit, 6),
        "fees": round(fees, 4),
        "qty": round(qty, 4),
        "slippage": round(total_slippage, 6),
        "friction_rate": round(friction_rate, 4), # 摩擦力佔比 %
        "theoretical_profit": round((expected_exit - expected_entry)/expected_entry if expected_entry > 0 else 0.0, 4)
    }

    # 讀取現有紀錄
    if os.path.exists(history_file):
        with open(history_file, 'r', encoding='utf-8') as f:
            try:
                history = json.load(f)
                if not isinstance(history, list):
                    history = []
            except Exception:
                history = []
    else:
        history = []

    # 加入新紀錄
    history.append(trade_data)

    # 寫回檔案
    try:
        with open(history_file, 'w', encoding='utf-8') as f:
            json.dump(history, f, indent=4, ensure_ascii=False)
        print(f"📝 [Memory] 已記錄 {symbol} 的交易結果 (Friction Rate: {friction_rate:.2f}%)。")
    except Exception as e:
        print(f"⚠️ [Memory] 紀錄 {symbol} 失敗: {e}")

MAX_GLOBAL_CONCURRENT_TRADES = 3
DEFAULT_LEVERAGE = 5

import json

DEFAULT_CONFIG = {
    # --- 第一類：核心趨勢層 (Core Trend) - 穩健趨勢，較高槓桿 ---
    "SOLUSDT": {"sl_atr_multiplier": 3.0, "tp_atr_multiplier": 6.0, "volume_threshold_factor": 1.2, "breakeven_trigger": 0.5, "min_flip_time": 300, "mtf_filter": True, "profile_type": "Core_Trend", "leverage": 8, "k_factor": 2.5},
    "LINKUSDT": {"sl_atr_multiplier": 2.0, "tp_atr_multiplier": 4.0, "volume_threshold_factor": 1.1, "breakeven_trigger": 0.4, "min_flip_time": 180, "mtf_filter": True, "profile_type": "Core_Trend", "leverage": 8, "k_factor": 2.5},
    "TRXUSDT": {"sl_atr_multiplier": 2.5, "tp_atr_multiplier": 5.0, "volume_threshold_factor": 1.3, "breakeven_trigger": 0.5, "min_flip_time": 240, "mtf_filter": True, "profile_type": "Core_Trend", "leverage": 8, "k_factor": 2.5},

    # --- 第二類：高彈性動能層 (High-Beta Momentum) - 快速爆發，中等槓桿 ---
    "RENDERUSDT": {"sl_atr_multiplier": 2.0, "tp_atr_multiplier": 4.0, "volume_threshold_factor": 1.5, "breakeven_trigger": 0.6, "min_flip_time": 120, "mtf_filter": True, "profile_type": "High_Beta_Momentum", "leverage": 4, "k_factor": 4.5},
    "SUIUSDT": {"sl_atr_multiplier": 1.8, "tp_atr_multiplier": 3.6, "volume_threshold_factor": 1.8, "breakeven_trigger": 0.7, "min_flip_time": 90, "mtf_filter": True, "profile_type": "High_Beta_Momentum", "leverage": 4, "k_factor": 4.5},
    "INJUSDT": {"sl_atr_multiplier": 2.2, "tp_atr_multiplier": 4.4, "volume_threshold_factor": 1.5, "breakeven_trigger": 0.6, "min_flip_time": 120, "mtf_filter": True, "profile_type": "High_Beta_Momentum", "leverage": 4, "k_factor": 4.5},
    "NEARUSDT": {"sl_atr_multiplier": 2.3, "tp_atr_multiplier": 4.6, "volume_threshold_factor": 1.3, "breakeven_trigger": 0.5, "min_flip_time": 180, "mtf_filter": True, "profile_type": "High_Beta_Momentum", "leverage": 4, "k_factor": 4.5},
    "VELVETUSDT": {"sl_atr_multiplier": 2.0, "tp_atr_multiplier": 4.0, "volume_threshold_factor": 1.6, "breakeven_trigger": 0.6, "min_flip_time": 120, "mtf_filter": True, "profile_type": "High_Beta_Momentum", "leverage": 4, "k_factor": 4.5},
    "LABUSDT": {"sl_atr_multiplier": 2.0, "tp_atr_multiplier": 4.0, "volume_threshold_factor": 1.6, "breakeven_trigger": 0.6, "min_flip_time": 120, "mtf_filter": True, "profile_type": "High_Beta_Momentum", "leverage": 4, "k_factor": 4.5},

    # --- 第三類：投機與特定風險層 (Speculative_Risk) - 極端防禦，低槓桿 ---
    "AVAXUSDT": {"sl_atr_multiplier": 2.5, "tp_atr_multiplier": 5.0, "volume_threshold_factor": 1.3, "breakeven_trigger": 0.5, "min_flip_time": 240, "mtf_filter": True, "profile_type": "Speculative_Risk", "leverage": 2, "k_factor": 6.0},
    "DOGEUSDT": {"sl_atr_multiplier": 3.5, "tp_atr_multiplier": 7.0, "volume_threshold_factor": 2.0, "breakeven_trigger": 0.8, "min_flip_time": 600, "mtf_filter": True, "profile_type": "Speculative_Risk", "leverage": 2, "k_factor": 6.0},
    "PEPEUSDT": {"sl_atr_multiplier": 4.0, "tp_atr_multiplier": 8.0, "volume_threshold_factor": 2.0, "breakeven_trigger": 0.8, "min_flip_time": 600, "mtf_filter": True, "profile_type": "Speculative_Risk", "leverage": 2, "k_factor": 6.0},
    
    # --- 新增分析調校幣種 ---
    "ESPORTSUSDT": {"sl_atr_multiplier": 3.0, "tp_atr_multiplier": 6.0, "volume_threshold_factor": 1.5, "breakeven_trigger": 0.8, "min_flip_time": 300, "mtf_filter": True, "profile_type": "Speculative_Risk", "leverage": 2, "k_factor": 6.0, "mmp": 0.005},
    "HEIUSDT": {"sl_atr_multiplier": 3.0, "tp_atr_multiplier": 6.0, "volume_threshold_factor": 1.5, "breakeven_trigger": 0.8, "min_flip_time": 300, "mtf_filter": True, "profile_type": "Speculative_Risk", "leverage": 2, "k_factor": 6.0, "mmp": 0.005},
    "BSBUSDT": {"sl_atr_multiplier": 4.5, "tp_atr_multiplier": 8.0, "volume_threshold_factor": 2.0, "breakeven_trigger": 0.8, "min_flip_time": 600, "mtf_filter": True, "profile_type": "Speculative_Risk", "leverage": 2, "k_factor": 6.0},
    "BELUSDT": {"sl_atr_multiplier": 2.5, "tp_atr_multiplier": 5.0, "volume_threshold_factor": 1.5, "breakeven_trigger": 0.6, "min_flip_time": 300, "mtf_filter": True, "profile_type": "High_Beta_Momentum", "leverage": 4, "k_factor": 4.5},
    "LABUSDT": {"sl_atr_multiplier": 3.0, "tp_atr_multiplier": 6.0, "volume_threshold_factor": 1.5, "breakeven_trigger": 0.8, "min_flip_time": 300, "mtf_filter": True, "profile_type": "Speculative_Risk", "leverage": 2, "k_factor": 6.0, "stalemate_time_sec": 1800, "stalemate_threshold": 0.01},
    "HUSDT": {"sl_atr_multiplier": 4.0, "tp_atr_multiplier": 8.0, "volume_threshold_factor": 2.5, "breakeven_trigger": 0.8, "min_flip_time": 600, "mtf_filter": True, "profile_type": "Wild", "leverage": 2, "k_factor": 6.0, "mmp": 0.01, "volatility_circuit_breaker": True}
}

def load_coin_profiles():
    # 取得目前腳本所在的絕對路徑
    base_path = os.path.dirname(os.path.abspath(__file__))
    
    # 這裡請根據您剛才 ls 指令查到的位置來調整
    # 如果檔案在 config 資料夾裡，就用：
    config_path = os.path.join(base_path, "config", "coin_profiles.json")
    
    # 如果檔案就在根目錄，就改用：
    # config_path = os.path.join(base_path, "coin_profiles.json")

    print(f"🔍 [系統診斷] 正在嘗試讀取配置路徑: {config_path}")

    if os.path.exists(config_path):
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config = json.load(f)
                print(f"✅ [系統診斷] 配置讀取成功！已載入 {len(config)} 個幣種的個性化參數。")
                return config
        except Exception as e:
            print(f"❌ [系統診斷] 讀取檔案時發生錯誤: {e}")
            return DEFAULT_CONFIG
    else:
        print(f"❌ [系統診斷] 警告：在 {config_path} 找不到檔案！請確認檔案名稱與路徑。")
        return DEFAULT_CONFIG

COIN_PROFILE_CONFIG = load_coin_profiles()

LAST_CONFIG_MTIME = 0

def check_and_reload_config():
    global COIN_PROFILE_CONFIG, exit_mgr, LAST_CONFIG_MTIME
    base_path = os.path.dirname(os.path.abspath(__file__))
    config_path = os.path.join(base_path, "config", "coin_profiles.json")
    if os.path.exists(config_path):
        try:
            mtime = os.path.getmtime(config_path)
            if LAST_CONFIG_MTIME == 0:
                LAST_CONFIG_MTIME = mtime
                return
            if mtime > LAST_CONFIG_MTIME:
                LAST_CONFIG_MTIME = mtime
                new_config = load_coin_profiles()
                if new_config:
                    COIN_PROFILE_CONFIG.clear()
                    COIN_PROFILE_CONFIG.update(new_config)
                    # exit_mgr updates automatically because it shares the same dict reference
                    print("🔄 [熱載入] 偵測到配置更新！已動態重新載入最新個性化交易參數 (MMP、止損比率等已同步)。")
        except Exception as e:
            print(f"⚠️ [熱載入] 檢查配置更新失敗: {e}")

exit_mgr = ExitManager(COIN_PROFILE_CONFIG)

ALL_SYMBOLS = list(COIN_PROFILE_CONFIG.keys())

LEVERAGE_TIERS = {
    "custom_leverage": {
        "coins": {},  # 預留：若未來想針對某些幣種調低槓桿可填入，例如 {"DOGEUSDT"}
        "leverage": 3
    }
}

def get_symbol_leverage(sym):
    conf = COIN_PROFILE_CONFIG.get(sym, {})
    if "leverage" in conf:
        return int(conf["leverage"])
    return DEFAULT_LEVERAGE
RSI_PERIOD = 9
VOLUME_RATIO_THRESHOLD = 0.7
ATR_WARMUP_BATCH_SIZE = 2
ATR_WARMUP_SYMBOL_COUNT = 12
ATR_WARMUP_LIMIT = 1000
ATR_WARMUP_PAUSE_SEC = 0.4
TIME_STOP_MINUTES = 30

if USE_TESTNET:
    exchange_futures.urls['api']['fapiPublic'] = 'https://testnet.binancefuture.com/fapi/v1'
    exchange_futures.urls['api']['fapiPrivate'] = 'https://testnet.binancefuture.com/fapi/v1'
    exchange_spot.urls['api']['public'] = 'https://testnet.binance.vision/api/v3'
    exchange_spot.urls['api']['private'] = 'https://testnet.binance.vision/api/v3'

DEFAULT_SYMBOLS = [
    "XRPUSDT", "DOGEUSDT", "ADAUSDT", "LINKUSDT", "AVAXUSDT",
    "DOTUSDT", "UNIUSDT", "NEARUSDT", "FETUSDT", "SUIUSDT"
]
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "bot_symbols.json")

PERSONALITY_TEMPLATES = {
    "calm": {
        "personality": "calm",
        "risk_multiplier": 0.7,
        "volume_multiplier": 0.8,
        "entry_cooldown_sec": 180,
        "max_additional_entries": 1,
        "entry_size_pct": 0.3,
        "add_entry_pct": 0.15,
        "sl_atr_multiplier": 1.5,
        "tp_atr_multiplier": 3.0,
        "hard_stop_loss_pct": 0.03,
    },
    "balanced": {
        "personality": "balanced",
        "risk_multiplier": 1.0,
        "volume_multiplier": 1.0,
        "entry_cooldown_sec": 90,
        "max_additional_entries": 2,
        "entry_size_pct": 0.5,
        "add_entry_pct": 0.25,
        "sl_atr_multiplier": 1.2,
        "tp_atr_multiplier": 2.4,
        "hard_stop_loss_pct": 0.02,
    },
    "aggressive": {
        "personality": "aggressive",
        "risk_multiplier": 1.2,
        "volume_multiplier": 1.2,
        "entry_cooldown_sec": 60,
        "max_additional_entries": 3,
        "entry_size_pct": 0.7,
        "add_entry_pct": 0.4,
        "sl_atr_multiplier": 1.0,
        "tp_atr_multiplier": 2.0,
        "hard_stop_loss_pct": 0.015,
    },
    "adaptive": {
        "personality": "adaptive",
        "risk_multiplier": 1.0,
        "volume_multiplier": 1.0,
        "entry_cooldown_sec": 90,
        "max_additional_entries": 2,
        "entry_size_pct": 0.5,
        "add_entry_pct": 0.25,
        "sl_atr_multiplier": 1.2,
        "tp_atr_multiplier": 2.4,
        "hard_stop_loss_pct": 0.02,
    },
}

SYMBOL_EXIT_OVERRIDES = {
    "XRPUSDT": {
        "tp_atr_multiplier": 3.0,
        "sl_atr_multiplier": 1.5,
    },
    "LINKUSDT": {
        "tp_atr_multiplier": 3.0,
        "sl_atr_multiplier": 1.5,
    },
}

DEFAULT_REVERSAL_SETTINGS = {
    "trade_signal_threshold": 1.8,
    "volume_multiplier": 3.0,
    "price_jump_pct": 0.01,
    "min_reverse_pct": 0.008,
}

SYMBOL_REVERSAL_SETTINGS = {
    "XRPUSDT": {
        "trade_signal_threshold": 2.5,
        "volume_multiplier": 3.5,
        "price_jump_pct": 0.012,
        "min_reverse_pct": 0.01,
    },
}

_PRECISION_CACHE = {}


def convert_to_ccxt_symbol(symbol: str) -> str:
    symbol = str(symbol).upper().strip()
    if symbol == "PEPEUSDT":
        return "1000PEPE/USDT"
    if symbol.endswith("USDT"):
        return f"{symbol[:-4]}/USDT"
    return symbol


async def get_contract_precision(sym: str):
    if sym in _PRECISION_CACHE:
        return _PRECISION_CACHE[sym]

    ccxt_symbol = convert_to_ccxt_symbol(sym)
    if not exchange_futures.markets:
        try:
            await exchange_futures.load_markets()
        except Exception:
            pass

    try:
        market = exchange_futures.market(ccxt_symbol)
        amount_limits = market.get('limits', {}).get('amount', {})
        step_size = float(amount_limits.get('min', 0.001) or 0.001)
        min_qty = float(amount_limits.get('min', step_size) or step_size)
        precision = int(round(-math.log10(step_size))) if step_size > 0 else 8
        _PRECISION_CACHE[sym] = {
            'step_size': step_size,
            'min_qty': min_qty,
            'qty_prec': market.get('precision', {}).get('amount', precision),
            'price_prec': market.get('precision', {}).get('price', precision)
        }
    except Exception:
        _PRECISION_CACHE[sym] = {
            'step_size': 0.001,
            'min_qty': 0.001,
            'qty_prec': 3,
            'price_prec': 3
        }

    return _PRECISION_CACHE[sym]


def round_step(qty, step_size):
    if qty <= 0 or step_size <= 0:
        return 0.0
    precision = int(round(-math.log10(step_size)))
    rounded = round(qty / step_size) * step_size
    return round(rounded, precision)


async def sanitize_order_qty(sym: str, qty: float):
    prec = await get_contract_precision(sym)
    qty = round_step(qty, prec['step_size'])
    if qty < prec['min_qty']:
        return 0.0
    return qty


def normalize_symbol(sym):
    if sym is None:
        return ""
    sym = str(sym).strip().upper()
    if not sym:
        return ""
    if sym in ("PEPE", "PEPEUSDT"):
        return "1000PEPEUSDT"
    if not sym.endswith("USDT"):
        sym = f"{sym}USDT"
    return sym


def normalize_symbol_list(symbols, max_count=20):
    if isinstance(symbols, str):
        symbols = [symbols]
    if not symbols:
        return list(DEFAULT_SYMBOLS[:max_count])

    seen = []
    for item in symbols:
        sym = normalize_symbol(item)
        if sym and sym not in seen:
            seen.append(sym)

    return seen[:max_count]


def load_symbol_config():
    global SYMBOL_EXIT_OVERRIDES, SYMBOL_REVERSAL_SETTINGS
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        symbols = []
        profiles = {}
        exit_overrides = {}
        reversal_settings = {}
        if isinstance(data, dict):
            symbols = normalize_symbol_list(data.get("symbols", []))
            raw_profiles = data.get("profiles", {})
            if isinstance(raw_profiles, dict):
                for sym, profile in raw_profiles.items():
                    normalized = normalize_symbol(sym)
                    if not normalized or not isinstance(profile, dict):
                        continue
                    profile_copy = dict(profile)
                    overrides = profile_copy.get("exit_overrides")
                    if isinstance(overrides, dict):
                        exit_overrides[normalized] = overrides
                    settings = profile_copy.get("reversal_settings")
                    if isinstance(settings, dict):
                        reversal_settings[normalized] = settings
                    profiles[normalized] = profile_copy
        SYMBOL_EXIT_OVERRIDES = exit_overrides
        SYMBOL_REVERSAL_SETTINGS = reversal_settings
        return symbols or list(DEFAULT_SYMBOLS), profiles
    except FileNotFoundError:
        return list(DEFAULT_SYMBOLS), {}
    except Exception as e:
        print(f"⚠️ 讀取幣種清單失敗: {e}")
        return list(DEFAULT_SYMBOLS), {}


    # 讀取優先權清單
    try:
        with open(os.path.join(os.path.dirname(__file__), "strategy_config.json"), "r") as f:
            config = json.load(f)
            priority_list = config.get("priority_symbols", [])
    except Exception:
        priority_list = []

    # 組合清單：優先幣種在前，其餘在後（去重）
    combined = []
    for s in priority_list:
        norm = normalize_symbol(s)
        if norm and norm not in combined:
            combined.append(norm)
    
    for s in symbols:
        norm = normalize_symbol(s)
        if norm and norm not in combined:
            combined.append(norm)
            
    return combined


def load_symbol_profiles():
    _, profiles = load_symbol_config()
    return profiles


def load_symbol_pool():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return normalize_symbol_list(data.get("symbols", []))
        return normalize_symbol_list(data)
    except FileNotFoundError:
        return list(DEFAULT_SYMBOLS)
    except Exception as e:
        print(f"⚠️ 讀取幣種清單失敗: {e}")
        return list(DEFAULT_SYMBOLS)

def save_symbol_pool(symbols):
    normalized = normalize_symbol_list(symbols)
    profiles = load_symbol_profiles()
    payload = {"symbols": normalized}
    if profiles:
        payload["profiles"] = profiles
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return normalized


def save_symbol_profiles(profiles):
    symbols = load_symbol_pool()
    normalized_profiles = {}
    for sym, profile in (profiles or {}).items():
        normalized = normalize_symbol(sym)
        if normalized and isinstance(profile, dict):
            normalized_profiles[normalized] = profile
    payload = {"symbols": symbols, "profiles": normalized_profiles}
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return normalized_profiles


def clean_for_serialization(val):
    if isinstance(val, dict):
        return {k: clean_for_serialization(v) for k, v in val.items()}
    elif isinstance(val, (list, tuple)):
        return [clean_for_serialization(x) for x in val]
    elif hasattr(val, "tolist"): # numpy array or scalar
        return val.tolist()
    elif isinstance(val, (np.integer, np.int64, np.int32, np.int16, np.int8)):
        return int(val)
    elif isinstance(val, (np.floating, np.float64, np.float32, np.float16)):
        if np.isinf(val):
            return "inf" if val > 0 else "-inf"
        elif np.isnan(val):
            return "nan"
        return float(val)
    elif isinstance(val, np.bool_):
        return bool(val)
    elif isinstance(val, float):
        if np.isinf(val):
            return "inf" if val > 0 else "-inf"
        elif np.isnan(val):
            return "nan"
        return val
    return val

def restore_deserialized_value(val):
    if isinstance(val, dict):
        return {k: restore_deserialized_value(v) for k, v in val.items()}
    elif isinstance(val, list):
        return [restore_deserialized_value(x) for x in val]
    elif val == "inf":
        return float('inf')
    elif val == "-inf":
        return float('-inf')
    elif val == "nan":
        return float('nan')
    return val

def save_current_states():
    """將目前的交易狀態持久化到檔案"""
    try:
        exclude_keys = {
            "adjusted_this_tick", "last_trade_price", "last_trade_qty",
            "ohlcv", "closes", "tr_list", "atr_history", "trade_qty_history",
            "trade_price_history", "pnl_history", "current_atr", "atr_ma20",
            "current_rsi", "ema20", "ema50", "macd_line", "macd_signal",
            "macd_hist", "prev_macd_line", "prev_macd_signal", "bb_up",
            "bb_mid", "bb_low", "vol_ma10", "vol_ma20", "current_vol",
            "prev_close"
        }
        save_data = {}
        for sym, s in STATES.items():
            symbol_data = {}
            for k, v in s.items():
                if k in exclude_keys:
                    continue
                # 遞迴清理 numpy 型別以避免 JSON 序列化失敗
                symbol_data[k] = clean_for_serialization(v)
            save_data[sym] = symbol_data
        
        if not save_data:
            return

        temp_file = "current_states.json.tmp"
        try:
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(save_data, f, indent=4, ensure_ascii=False)
            os.replace(temp_file, "current_states.json")
        except OSError as e:
            print(f"⚠️ [狀態持久化寫入失敗] 可能是權限不足或磁碟空間已滿: {e}")
    except Exception as e:
        print(f"⚠️ [狀態持久化失敗] {e}")

def load_saved_states():
    """從檔案讀取之前的交易狀態"""
    if os.path.exists("current_states.json"):
        try:
            with open("current_states.json", "r", encoding="utf-8") as f:
                saved_data = json.load(f)
                for sym, data in saved_data.items():
                    if sym in STATES:
                        # 遞迴恢復特殊浮點數
                        restored_data = restore_deserialized_value(data)
                        # 更新現有的狀態資料
                        STATES[sym].update(restored_data)
                        print(f"🔄 [狀態恢復] 已從檔案恢復 {sym} 的狀態。")
            return True
        except Exception as e:
            print(f"⚠️ [狀態恢復失敗] {e}")
    return False


async def close_all_positions_emergency():
    """緊急狀況：平倉所有持倉"""
    print("🚨 [緊急防護] 觸發全域止損，正在執行全平所有持倉...")
    open_syms = [sym for sym in ALL_SYMBOLS if abs(STATES[sym]["qty"]) > 0.000001]
    
    for sym in open_syms:
        s = STATES[sym]
        side = 'sell' if s["qty"] > 0 else 'buy'
        # 使用市價平倉 (呼叫 close_position)
        # 傳入 is_stop_loss=True 繞過 MMP 獲利門檻限制，強制平倉
        await close_position(sym, side, abs(s["qty"]), s["close_price"], s["avg_price"], 
                              reason="GLOBAL_EMERGENCY_STOP", is_stop_loss=True)
    print("✅ [緊急防護] 所有持倉已執行平倉。")



def infer_symbol_personality(sym):
    if sym in ("BTCUSDT", "ETHUSDT"):
        return "calm"
    aggressive_coins = {"DOGEUSDT", "PEPEUSDT", "SHIBUSDT"}
    balanced_coins = {"ADAUSDT", "SOLUSDT", "LINKUSDT", "AVAXUSDT", "NEARUSDT", "SUIUSDT", "INJUSDT", "RENDERUSDT"}
    if sym in aggressive_coins:
        return "aggressive"
    if sym in balanced_coins:
        return "balanced"
    return "adaptive"


def get_personality_template(personality):
    if not personality:
        return PERSONALITY_TEMPLATES["balanced"]
    return PERSONALITY_TEMPLATES.get(personality.lower(), PERSONALITY_TEMPLATES["balanced"])


def get_symbol_exit_override(sym):
    return SYMBOL_EXIT_OVERRIDES.get(sym, {})


def should_require_strong_exit(overrides):
    return bool(
        overrides.get("require_strong_momentum")
        or overrides.get("volume_threshold") is not None
        or overrides.get("momentum_threshold") is not None
    )


def is_strong_exit_condition(sym, is_long):
    overrides = get_symbol_exit_override(sym)
    if not overrides:
        return False
    if overrides.get("require_strong_momentum"):
        return has_strong_momentum(sym, is_long)
    volume_threshold = overrides.get("volume_threshold")
    momentum_threshold = overrides.get("momentum_threshold")
    s = STATES[sym]
    if s.get("vol_ma20", 0.0) <= 0 or len(s.get("closes", [])) < 4:
        return False
    volume_ratio = s["current_vol"] / max(s["vol_ma20"], 1e-8)
    recent_return = abs((s["closes"][-1] - s["closes"][-4]) / max(abs(s["closes"][-4]), 1e-8))
    if volume_threshold is not None and volume_ratio < float(volume_threshold):
        return False
    if momentum_threshold is not None and recent_return < float(momentum_threshold):
        return False
    return True


def get_effective_exit_setting(sym, key, base_value, is_long):
    # 1. 優先從 SYMBOL_PROFILES 地圖中讀取個性化設定
    profile = SYMBOL_PROFILES.get(sym)
    if profile and key in profile:
        return profile[key]
    
    # 2. 如果地圖沒寫，再從策略配置檔讀取
    overrides = get_symbol_exit_override(sym)
    if not overrides:
        return base_value
    value = overrides.get(key)
    if not isinstance(value, (int, float)):
        return base_value
    if key == "tp_atr_multiplier":
        if should_require_strong_exit(overrides) and not is_strong_exit_condition(sym, is_long):
            return base_value
        return value
    if key in ("sl_atr_multiplier", "hard_stop_loss_pct"):
        if value > base_value:
            return base_value
    return value


def apply_symbol_profile(sym, profile):
    if sym not in STATES:
        return
    state = STATES[sym]
    if isinstance(profile, str):
        profile = {"personality": profile}
    personality = profile.get("personality") or state.get("personality") or infer_symbol_personality(sym)
    personality_source = "manual" if profile.get("personality") else state.get("personality_source", "infer")
    template = get_personality_template(personality)
    state.update(template)
    for key in [
        "risk_multiplier", "volume_multiplier", "entry_cooldown_sec",
        "max_additional_entries", "entry_size_pct", "add_entry_pct",
        "sl_atr_multiplier", "tp_atr_multiplier", "hard_stop_loss_pct",
        "max_loss_usdt", "trailing_activation", "trailing_distance_atr",
        "volume_threshold_factor", "min_flip_time", 
        "breakeven_trigger", "profile_type", "leverage", "mtf_filter", "sector"
    ]:
        if key in profile:
            state[key] = profile[key]
    state["personality"] = personality
    state["personality_source"] = personality_source
    if personality_source == "manual":
        state["last_personality_update"] = time.time()


def apply_all_symbol_profiles():
    for sym in ALL_SYMBOLS:
        json_profile = SYMBOL_PROFILES.get(sym, {})
        py_profile = COIN_PROFILE_CONFIG.get(sym, {})
        merged_profile = {**json_profile, **py_profile}
        apply_symbol_profile(sym, merged_profile)


def has_manual_personality(sym):
    profile = SYMBOL_PROFILES.get(sym, {})
    return isinstance(profile, dict) and "personality" in profile


def evaluate_dynamic_personality(sym):
    s = STATES[sym]
    if s["current_atr"] <= 0 or s["vol_ma20"] <= 0 or len(s["ohlcv"]) < 20:
        return s.get("personality", "balanced")

    close = s["close_price"]
    atr_pct = s["current_atr"] / max(close, 1e-8)
    volume_ratio = s["current_vol"] / max(s["vol_ma20"], 1e-8)
    recent_candles = s["ohlcv"][-20:]
    highs = np.array([x[2] for x in recent_candles])
    lows = np.array([x[3] for x in recent_candles])
    recent_high = float(np.max(highs))
    recent_low = float(np.min(lows))
    range_width_pct = (recent_high - recent_low) / max(recent_low, 1e-8)
    rsi = s.get("current_rsi", 50.0)
    macd_hist = s.get("macd_hist", 0.0)

    quiet_market = volume_ratio < 1.15 and atr_pct < 0.008 and range_width_pct < 0.02
    high_volatility = volume_ratio > 1.9 or atr_pct > 0.02 or range_width_pct > 0.04
    strong_trend = abs(rsi - 50.0) > 12.0 or abs(macd_hist) > close * 0.0006

    if quiet_market:
        return "calm"
    if high_volatility or strong_trend:
        return "aggressive"
    if range_width_pct >= 0.03 or abs(rsi - 50.0) > 8.0:
        return "balanced"
    return "adaptive"


def measure_personality_traits(sym):
    s = STATES[sym]
    close = max(s.get("close_price", 0.0), 1e-8)
    atr_pct = s.get("current_atr", 0.0) / close
    volume_ratio = s.get("current_vol", 0.0) / max(s.get("vol_ma20", 1e-8), 1e-8)
    recent_candles = s.get("ohlcv", [])[-20:]
    highs = np.array([x[2] for x in recent_candles]) if recent_candles else np.array([close])
    lows = np.array([x[3] for x in recent_candles]) if recent_candles else np.array([close])
    recent_high = float(np.max(highs))
    recent_low = float(np.min(lows))
    range_width_pct = (recent_high - recent_low) / max(recent_low, 1e-8)
    rsi = s.get("current_rsi", 50.0)
    return volume_ratio, atr_pct, rsi, range_width_pct


def update_dynamic_personality(sym):
    if has_manual_personality(sym):
        return False
    s = STATES[sym]
    new_personality = evaluate_dynamic_personality(sym)
    old_personality = s.get("personality", "balanced")
    if new_personality == old_personality and s.get("personality_source") == "dynamic":
        s["last_personality_update"] = time.time()
        return False
    if new_personality != old_personality:
        s.update(get_personality_template(new_personality))
        s["personality"] = new_personality
        s["personality_source"] = "dynamic"
        s["last_personality_update"] = time.time()
        volume_ratio, atr_pct, rsi, range_width_pct = measure_personality_traits(sym)
        print(f"🔧 [動態個性] {sym} 由 {old_personality} 變更為 {new_personality} | vol={volume_ratio:.2f} atr_pct={atr_pct:.4f} rsi={rsi:.1f} range={range_width_pct:.3f}")
        return True
    return False


def update_all_dynamic_personalities():
    now = time.time()
    for sym in ALL_SYMBOLS:
        s = STATES[sym]
        if has_manual_personality(sym):
            continue
        if now - s.get("last_personality_update", 0.0) < 300:
            continue
        update_dynamic_personality(sym)


ALL_SYMBOLS, SYMBOL_PROFILES = load_symbol_config()

MAX_POSITIONS = 3
COOLDOWN_SEC = 1200
MAIN_LOOP_INTERVAL_SEC = 6
PENDING_CONFIRM_SEC = 2
BAN_WINDOW = 3600
BAN_DURATION = 86400
MAX_STOPS_IN_WINDOW = 3
SL_ATR_MULTIPLIER = 1.5
TP_ATR_MULTIPLIER = 3.0
HARD_STOP_LOSS_PCT = 0.02

def build_symbol_state(sym):
    conf = COIN_PROFILE_CONFIG.get(sym, {})
    return {
        "status": "ACTIVE",
        "status_reason": "",
        "entry_reason": "UNKNOWN",
        "next_status_time": 0,
        "stop_count": 0,
        "first_stop_time": 0,
        "qty": 0.0,
        "avg_price": 0.0,
        "open_time": 0.0,
        "current_atr": 0.0,
        "atr_history": [],
        "atr_ma20": 0.0,
        "current_rsi": 50.0,
        "ema20": 0.0,
        "ema50": 0.0,
        "macd_line": 0.0,
        "macd_signal": 0.0,
        "macd_hist": 0.0,
        "prev_macd_line": 0.0,
        "prev_macd_signal": 0.0,
        "bb_up": 0.0,
        "bb_mid": 0.0,
        "bb_low": 0.0,
        "vol_ma10": 0.0,
        "vol_ma20": 0.0,
        "current_vol": 0.0,
        "trailing_highest": 0.0,
        "trailing_lowest": float('inf'),
        "trailing_stop_price": 0.0,
        "highest_profit_pct": 0.0,
        "has_partial_closed": False,
        "trade_status": "NORMAL",
        "pending_stop_loss": False,
        "stop_loss_price": 0.0,
        "ohlcv": [],
        "closes": [],
        "tr_list": [],
        "prev_close": None,
        "last_trade_price": 0.0,
        "last_trade_qty": 0.0,
        "last_trade_side": "",
        "last_trade_time": 0.0,
        "trade_qty_history": [],
        "trade_price_history": [],
        "trade_signal_strength": 0.0,
        "trade_signal_reason": "",
        "pending_side": None,
        "pending_time": 0,
        "pending_confirm_high": 0,
        "pending_confirm_low": 0,
        "close_price": 0.0,
        "last_buy_time": 0,
        "signal_strength": 0.0,
        "pnl_history": [],
        "has_been_negative": False,
        "trail_tp_price": 0.0,
        "entry_count": 0,
        "avg_entry_price": 0.0,
        "max_additional_entries": 2,
        "entry_cooldown_sec": conf.get("entry_cooldown_sec", 90),
        "min_flip_time": conf.get("min_flip_time", 300),
        "profile_type": conf.get("profile_type", "Core_Trend"),
        "entry_size_pct": 0.5,
        "add_entry_pct": 0.25,
        "risk_multiplier": 1.0,
        "volume_threshold_factor": conf.get("volume_threshold_factor", 0.8),
        "k_factor": conf.get("k_factor", 3.0),
        "volume_multiplier": conf.get("volume_multiplier", 1.0),
        "sl_atr_multiplier": conf.get("sl_atr_multiplier", 1.5),
        "tp_atr_multiplier": conf.get("tp_atr_multiplier", 2.5),
        "hard_stop_loss_pct": HARD_STOP_LOSS_PCT,
        "max_loss_usdt": conf.get("max_loss_usdt", 10.0), # 預設每單最大虧損金額上限 (對應150u資金)
        "trailing_activation": conf.get("trailing_activation", 0.03), # 獲利達幾%啟動 (預設 3%)
        "trailing_distance_atr": conf.get("trailing_distance_atr", 1.2), # 回撤多少ATR平倉 (預設 1.2倍)
        "sector": conf.get("sector", "Speculative"), # 預設賽道標籤
        "expected_entry_price": 0.0, # 記錄觸發委託時的預期進場價格
        "personality": "balanced",
        "personality_source": "infer",
        "last_personality_update": 0.0,
        "last_entry_time": 0.0,
        "last_close_time": 0.0,   # 記錄最後一次平倉完成的時間戳（Unix 秒）
    }

STATES = {sym: build_symbol_state(sym) for sym in ALL_SYMBOLS}
if "XRPUSDT" not in STATES:
    STATES["XRPUSDT"] = build_symbol_state("XRPUSDT")
apply_all_symbol_profiles()
WATCH_TASKS = {}
EXECUTION_ENGINE = None

def init_global_engine(exchange):
    global EXECUTION_ENGINE
    if EXECUTION_ENGINE is None:
        EXECUTION_ENGINE = ExecutionEngine(exchange)

# ── 指標計算函數 ──────────────────────────────────────────────

def calculate_ema(prices, period):
    if len(prices) < period:
        return np.mean(prices)
    multiplier = 2.0 / (period + 1)
    ema = np.mean(prices[:period])
    for p in prices[period:]:
        ema = (p - ema) * multiplier + ema
    return ema

def calculate_macd(prices, fast=12, slow=26, signal=9):
    if len(prices) < slow + signal:
        return 0, 0, 0, 0, 0
    ema_fast = np.array([calculate_ema(prices[:i+1], fast) for i in range(fast-1, len(prices))])
    ema_slow = np.array([calculate_ema(prices[:i+1], slow) for i in range(slow-1, len(prices))])
    macd_line = ema_fast[-1] - ema_slow[-1]
    prev_macd_line = ema_fast[-2] - ema_slow[-2] if len(ema_fast) >= 2 and len(ema_slow) >= 2 else macd_line
    macd_vals = ema_fast[-signal*2:] - ema_slow[-signal*2:]
    signal_vals = np.array([calculate_ema(macd_vals[:i+1], signal) for i in range(signal-1, len(macd_vals))])
    macd_signal = signal_vals[-1] if len(signal_vals) > 0 else 0
    prev_macd_signal = signal_vals[-2] if len(signal_vals) >= 2 else macd_signal
    macd_hist = macd_line - macd_signal
    return macd_line, macd_signal, macd_hist, prev_macd_line, prev_macd_signal

def calculate_bollinger_bands(prices, period=20, std_dev=2.0):
    if len(prices) < period:
        return 0, 0, 0
    sma = np.mean(prices[-period:])
    std = np.std(prices[-period:])
    return sma + std_dev * std, sma, sma - std_dev * std

def calculate_adx(highs, lows, closes, period=14):
    if len(highs) < period + 1:
        return 0
    tr_list, plus_dm_list, minus_dm_list = [], [], []
    for i in range(1, len(highs)):
        tr = max(highs[i] - lows[i], abs(highs[i] - closes[i-1]), abs(lows[i] - closes[i-1]))
        up_move = highs[i] - highs[i-1]
        down_move = lows[i-1] - lows[i]
        plus_dm = up_move if up_move > down_move and up_move > 0 else 0
        minus_dm = down_move if down_move > up_move and down_move > 0 else 0
        tr_list.append(tr)
        plus_dm_list.append(plus_dm)
        minus_dm_list.append(minus_dm)
    if len(tr_list) < period:
        return 0
    atr = np.mean(tr_list[-period:])
    if atr < 1e-10:
        return 0
    plus_di = 100 * np.mean(plus_dm_list[-period:]) / atr
    minus_di = 100 * np.mean(minus_dm_list[-period:]) / atr
    dx = abs(plus_di - minus_di) / (plus_di + minus_di) * 100 if (plus_di + minus_di) > 1e-10 else 0
    return dx

def get_dynamic_stagnation_limit(current_atr, atr_ma20):
    if current_atr < atr_ma20:
        return 180
    return 300

def check_binance_weight():
    try:
        headers = getattr(exchange_futures, 'last_response_headers', {})
        weight = None
        for k, v in headers.items():
            if k.lower() == 'x-mbx-used-weight-1m':
                weight = int(v)
                break
        if weight is not None:
            if weight > 900:
                print(f"⚠️ [API限流警報] 幣安目前權重已達 {weight}/1200，觸發重度防護，冷卻 10 秒")
                return 10.0
            elif weight > 700:
                print(f"⚠️ [API限流警報] 幣安目前權重已達 {weight}/1200，觸發輕度防護，冷卻 3 秒")
                return 3.0
    except Exception as e:
        print(f"⚠️ [API權重讀取失敗] {e}")
    return 0.0

CONSECUTIVE_ERRORS = 0

# --- 平倉後冷卻機制 ---
POST_CLOSE_COOLDOWN_SEC = 60  # 平倉後至少等待 60 秒再允許同幣種重新開倉

# --- 異步鎖定機制（防止同一幣種同時觸發多個下單請求）---
trading_locks: dict = {}  # {sym: bool}，True 表示正在執行下單中

# ── 狀態管理 ──────────────────────────────────────────────────

def get_active_count():
    return sum(1 for s in STATES.values() if s["status"] == "ACTIVE")

def get_open_position_count():
    return sum(1 for s in STATES.values() if abs(s["qty"]) > 0.000001)

def get_open_symbols():
    return [sym for sym in ALL_SYMBOLS if sym in STATES and abs(STATES[sym]["qty"]) > 0.000001]


def is_symbol_locked(sym):
    s = STATES.get(sym)
    if not s:
        return False
    return abs(s["qty"]) > 0.000001 or s["entry_count"] > 0 or s["open_time"] > 0 or s["status"] in ("COOLDOWN", "BANNED")


def filter_valid_symbols(exchange, symbols):
    if not exchange_futures.markets:
        return list(symbols)
    valid = []
    for sym in symbols:
        found = False
        for m in exchange_futures.markets.values():
            if m['id'] == sym or m['symbol'] == sym:
                found = True
                break
        if found:
            valid.append(sym)
        else:
            print(f"⚠️ [過濾無效幣種] 交易所目前不支援/已下架此幣種，已自動移出監聽清單: {sym}")
    return valid


def apply_symbol_pool_change(requested_symbols):
    global ALL_SYMBOLS
    desired = filter_valid_symbols(exchange_futures, normalize_symbol_list(requested_symbols))
    locked_symbols = [sym for sym in ALL_SYMBOLS if is_symbol_locked(sym)]

    new_symbols = []
    used = set()
    target_count = min(20, max(len(desired), len(ALL_SYMBOLS)))

    for sym in locked_symbols:
        if sym not in used:
            new_symbols.append(sym)
            used.add(sym)
    for sym in desired:
        if sym in used or len(new_symbols) >= target_count:
            continue
        new_symbols.append(sym)
        used.add(sym)
    for sym in ALL_SYMBOLS:
        if sym in used or len(new_symbols) >= target_count:
            continue
        new_symbols.append(sym)
        used.add(sym)
    for sym in DEFAULT_SYMBOLS:
        if sym in used or len(new_symbols) >= target_count:
            continue
        new_symbols.append(sym)
        used.add(sym)

    ALL_SYMBOLS = new_symbols[:target_count]
    for sym in ALL_SYMBOLS:
        STATES.setdefault(sym, build_symbol_state(sym))
    apply_all_symbol_profiles()
    save_symbol_pool(ALL_SYMBOLS)
    return list(ALL_SYMBOLS)


def update_trade_signal(sym, trade):
    s = STATES[sym]
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


REAL_BALANCE = 150.0

async def fetch_real_balance():
    global REAL_BALANCE
    if PAPER_TRADING:
        return
    try:
        balance_info = await exchange_futures.fetch_balance()
        usdt_balance = float(balance_info.get('USDT', {}).get('total', 150.0))
        REAL_BALANCE = usdt_balance
    except Exception as e:
        print(f"⚠️ [餘額獲取失敗] {e}")

def get_balance():
    if not PAPER_TRADING:
        return REAL_BALANCE
    try:
        with open("paper_state.json", "r") as f:
            state = json.load(f)
            return float(state.get("balance_usdt", 150.0))
    except:
        return 150.0

# --- 賽道手動分配權重定義 (第二層：動態權重分配) ---
SECTOR_WEIGHTS = {
    "AI": 0.50,            # AI 賽道分配 50% 資金額度
    "Layer2": 0.30,        # L2 賽道分配 30% 資金額度
    "Gaming": 0.20,        # Gaming 賽道分配 20% 資金額度
    "Speculative": 0.15,   # 高投機賽道分配 15% 資金額度
    "Layer1_Layer2": 0.30  # L1/L2 基礎賽道分配 30% 資金額度
}

def compute_per_coin_margin(sym=None):
    balance = get_balance()
    if balance <= 0 or not sym:
        return 0

    s = STATES.get(sym, {})
    sector = s.get("sector", "Speculative")
    # 根據幣種所屬賽道動態取得分配權重，若無定義則預設 0.20
    weight = SECTOR_WEIGHTS.get(sector, 0.20)
    
    # 實際配置金額 = 總餘額 * 賽道權重 * 風控保留係數 (95%)
    return balance * weight * 0.95

def calculate_dynamic_qty(sym, price, side):
    """
    根據 ATR 止損距離與風險比例動態計算首次開倉數量。
    公式：qty = (balance × RISK_PER_TRADE_PCT) / (ATR × sl_multiplier)
    上限：qty × price ≤ balance × MAX_NOTIONAL_PCT × leverage

    優點：
    - 高波動幣 (ATR大) → 自動縮減數量 → 固定風險金額
    - 低波動幣 (ATR小) → 自動增加數量 → 資金效率最大化
    - 無論市況，每筆最大損失都控制在 RISK_PER_TRADE_PCT 以內
    """
    try:
        balance = get_balance()
        if balance <= 0 or price <= 0:
            return 0.0

        s = STATES.get(sym, {})
        lev = get_symbol_leverage(sym)
        is_long = (side == "buy")

        # --- 取得止損距離 (與實際止損邏輯一致) ---
        atr_val = s.get("current_atr", 0.0)
        if atr_val <= 0:
            atr_val = price * 0.01   # 預設 1% 止損距離作為保底

        sl_multiplier = get_effective_exit_setting(
            sym, "sl_atr_multiplier", s.get("sl_atr_multiplier", SL_ATR_MULTIPLIER), is_long
        )
        sl_distance = max(atr_val * sl_multiplier, price * 0.005)

        # --- 計算基準數量 (風險單位) ---
        risk_dollar = balance * RISK_PER_TRADE_PCT
        qty = risk_dollar / sl_distance

        # --- 名義部位上限 (防止止損極小時數量爆炸) ---
        max_notional = balance * MAX_NOTIONAL_PCT * lev
        if qty * price > max_notional:
            qty = max_notional / price
            
        # --- 絕對名義部位上限 (最大1000 USDT) ---
        if qty * price > 1000.0:
            qty = 1000.0 / price

        # --- 最小名義價值保護 (幣安合約最低約 6 USDT) ---
        if qty * price < 6.0:
            qty = 6.0 / price

        print(f"📐 [動態倉位] {sym} | 餘額:{balance:.2f}U | ATR:{atr_val:.6f} | SL距離:{sl_distance:.6f} | 風險:{risk_dollar:.2f}U | 計算數量:{qty:.4f} (名義:{qty*price:.2f}U)")
        return qty
    except Exception as e:
        print(f"⚠️ [計算數量錯誤] {sym}: {e}")
        return 0.0

# ── 幣種狀態更新 ──────────────────────────────────────────────

def update_states():
    now = time.time()
    for sym in ALL_SYMBOLS:
        s = STATES[sym]
        if s["status"] == "COOLDOWN" and now >= s["next_status_time"]:
            s["status"] = "ACTIVE"
            s["status_reason"] = ""
            print(f"🔄 [狀態] {sym} 冷卻結束 → ACTIVE")
        if s["status"] == "BANNED" and now >= s["next_status_time"]:
            s["status"] = "ACTIVE"
            s["status_reason"] = ""
            s["stop_count"] = 0
            s["first_stop_time"] = 0
            print(f"🔄 [狀態] {sym} 封禁解除 → ACTIVE")

def mark_exit(sym, is_stop_loss=False, reason=""):
    s = STATES[sym]
    now = time.time()
    s["status"] = "COOLDOWN"
    s["next_status_time"] = now + COOLDOWN_SEC
    s["status_reason"] = f"冷卻中 (20分鐘) - {reason}"
    print(f"⏳ [狀態] {sym} 平倉 ({reason}) → COOLDOWN 20分鐘")
    if is_stop_loss:
        s["stop_count"] += 1
        if s["stop_count"] == 1:
            s["first_stop_time"] = now
        if s["stop_count"] >= MAX_STOPS_IN_WINDOW and (now - s["first_stop_time"]) <= BAN_WINDOW:
            s["status"] = "BANNED"
            s["next_status_time"] = now + BAN_DURATION
            s["status_reason"] = f"封禁中 (24h，{MAX_STOPS_IN_WINDOW}次停損)"
            print(f"🚫 [狀態] {sym} 1h內{MAX_STOPS_IN_WINDOW}次停損 → BANNED 24h")
        elif s["stop_count"] >= MAX_STOPS_IN_WINDOW:
            s["stop_count"] = 1
            s["first_stop_time"] = now

def reset_coin_state(sym):
    s = STATES[sym]
    s["qty"] = 0.0
    s["avg_price"] = 0.0
    s["entry_reason"] = "UNKNOWN"
    s["open_time"] = 0.0
    s["trailing_highest"] = 0.0
    s["trailing_lowest"] = float('inf')
    s["trailing_stop_price"] = 0.0
    s["highest_profit_pct"] = 0.0
    s["has_partial_closed"] = False
    s["trailing_stop_multiplier"] = 2.0
    s["trade_status"] = "NORMAL"
    s["pending_side"] = None
    s["pending_time"] = 0
    s["pending_confirm_high"] = 0
    s["pending_confirm_low"] = 0
    s["has_been_negative"] = False
    s["trail_tp_price"] = 0.0
    s["adjusted_this_tick"] = False
    s["entry_count"] = 0
    s["avg_entry_price"] = 0.0
    s["max_additional_entries"] = 2
    s["entry_cooldown_sec"] = 90
    s["entry_size_pct"] = 0.5
    s["add_entry_pct"] = 0.25
    s["risk_multiplier"] = 1.0
    s["volume_multiplier"] = 1.0
    s["sl_atr_multiplier"] = 1.5
    s["tp_atr_multiplier"] = 2.5
    s["hard_stop_loss_pct"] = 0.02
    s["personality"] = "balanced"
    s["personality_source"] = "infer"
    s["expected_entry_price"] = 0.0
    s["last_personality_update"] = 0.0
    s["last_entry_time"] = 0.0
    s["last_flip_time"] = 0.0
    # 注意：不重置 last_close_time，讓冷卻機制在 reset 後仍然有效

# ── 大盤與風向監控 (BTC & ETH Filter) ─────────────────────────

MARKET_WIND = {
    "btc_trend": "NEUTRAL",  # "BULL" or "BEAR"
    "allow_long": True,
    "allow_short": True,
    "btc_change_15m": 0.0,
    "eth_change_15m": 0.0
}

async def update_market_wind(exchange):
    global MARKET_WIND
    try:
        # 抓取 BTC 和 ETH
        btc_ohlcv = await fetch_ohlcv_with_sem(exchange, "BTC/USDT", TIMEFRAME, limit=100)
        eth_ohlcv = await fetch_ohlcv_with_sem(exchange, "ETH/USDT", TIMEFRAME, limit=100)
        
        MARKET_WIND["allow_long"] = True
        MARKET_WIND["allow_short"] = True
        
        if len(btc_ohlcv) >= 20:
            btc_closes = np.array([x[4] for x in btc_ohlcv])
            btc_ema20 = calculate_ema(btc_closes, 20)
            btc_price = btc_closes[-1]
            btc_change_15m = (btc_price - btc_closes[-15]) / btc_closes[-15]
            
            MARKET_WIND["btc_trend"] = "BULL" if btc_price > btc_ema20 else "BEAR"
            MARKET_WIND["btc_change_15m"] = btc_change_15m
        else:
            btc_change_15m = 0.0
            
        if len(eth_ohlcv) >= 20:
            eth_closes = np.array([x[4] for x in eth_ohlcv])
            eth_price = eth_closes[-1]
            eth_change_15m = (eth_price - eth_closes[-15]) / eth_closes[-15]
            MARKET_WIND["eth_change_15m"] = eth_change_15m
        else:
            eth_change_15m = 0.0
            
        # 1. 瀑布防護 (15m內跌超過1.2%暫停多單，漲超過1.2%暫停空單)
        if btc_change_15m < -0.012 or eth_change_15m < -0.015:
            MARKET_WIND["allow_long"] = False
            print(f"⚠️ [大盤瀑布風控] BTC 15m變動 {btc_change_15m*100:.2f}% | ETH 15m變動 {eth_change_15m*100:.2f}% | 🚫 暫停所有小幣多單開倉！")
        elif btc_change_15m > 0.012 or eth_change_15m > 0.015:
            MARKET_WIND["allow_short"] = False
            print(f"⚠️ [大盤暴漲風控] BTC 15m變動 {btc_change_15m*100:.2f}% | ETH 15m變動 {eth_change_15m*100:.2f}% | 🚫 暫停所有小幣空單開倉！")
            
    except Exception as e:
        print(f"⚠️ [更新大盤風向失敗]: {e}")

# ── 資料獲取 ──────────────────────────────────────────────────

async def initialize_atr_history(exchange, batch_size: int = ATR_WARMUP_BATCH_SIZE, limit: int = ATR_WARMUP_LIMIT, pause_sec: float = ATR_WARMUP_PAUSE_SEC):
    target_symbols = ALL_SYMBOLS[:ATR_WARMUP_SYMBOL_COUNT]
    print(f"⏳ [初始化] 開始分批獲取 {limit} 根 1m K線，以預熱前 {len(target_symbols)} 個主攻幣種的 ATR 歷史，下次批次間隔 {pause_sec}s...")
    total = len(target_symbols)
    if total == 0:
        print("⚠️ [初始化] 監控幣種清單為空，跳過 ATR 歷史預熱")
        return

    for batch_index in range(0, total, batch_size):
        batch = target_symbols[batch_index:batch_index + batch_size]
        print(f"⏳ [初始化] 進行第 {batch_index // batch_size + 1} 批：{len(batch)} 個幣種")
        tasks = [fetch_ohlcv_with_sem(exchange, sym, '1m', limit=limit) for sym in batch]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for sym, result in zip(batch, results):
            if not isinstance(result, Exception) and result:
                ohlcv = result
                tr_list = []
                for j in range(1, len(ohlcv)):
                    h = ohlcv[j][2]
                    l = ohlcv[j][3]
                    pc = ohlcv[j-1][4]
                    tr = max(h - l, abs(h - pc), abs(l - pc))
                    tr_list.append(tr)
                    if len(tr_list) >= 14:
                        atr = float(np.mean(tr_list[-14:]))
                        STATES[sym]["atr_history"].append(atr)
                print(f"✅ [初始化] {sym} 歷史 ATR 預熱完成，載入 {len(STATES[sym]['atr_history'])} 筆數據")
            else:
                print(f"⚠️ [初始化] {sym} 歷史 ATR 預熱失敗: {result}")

        if batch_index + batch_size < total:
            await asyncio.sleep(pause_sec)

async def fetch_all_klines(exchange):
    tasks = {}
    for sym in ALL_SYMBOLS:
        tasks[sym] = fetch_ohlcv_with_sem(exchange, sym, TIMEFRAME, limit=100)
    results = await asyncio.gather(*[tasks[sym] for sym in ALL_SYMBOLS], return_exceptions=True)
    for i, sym in enumerate(ALL_SYMBOLS):
        if not isinstance(results[i], Exception):
            STATES[sym]["ohlcv"] = results[i]
            STATES[sym]["close_price"] = results[i][-1][4]
        else:
            print(f"⚠️ [K線獲取失敗] {sym}: {results[i]}")

async def fetch_sma200_15m(exchange, sym):
    try:
        ohlcv = await fetch_ohlcv_with_sem(exchange, sym, '15m', limit=200)
        closes = np.array([x[4] for x in ohlcv])
        return float(np.mean(closes))
    except Exception as e:
        print(f"⚠️ [SMA200獲取失敗] {sym}: {e}")
        return 0.0

async def fetch_all_sma200(exchange):
    tasks = [fetch_sma200_15m(exchange, sym) for sym in ALL_SYMBOLS]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for i, sym in enumerate(ALL_SYMBOLS):
        if not isinstance(results[i], Exception):
            STATES[sym]["sma200_15m"] = results[i]

async def fetch_ema50_1h(exchange, sym):
    try:
        ohlcv = await fetch_ohlcv_with_sem(exchange, sym, '1h', limit=100)
        if not ohlcv or len(ohlcv) == 0:
            return 0.0
        closes = np.array([x[4] for x in ohlcv])
        ema50 = calculate_ema(closes, 50)
        return float(ema50)
    except Exception as e:
        print(f"⚠️ [1H EMA50獲取失敗] {sym}: {e}")
        return 0.0

async def fetch_all_ema50_1h(exchange):
    tasks = [fetch_ema50_1h(exchange, sym) for sym in ALL_SYMBOLS]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for i, sym in enumerate(ALL_SYMBOLS):
        if not isinstance(results[i], Exception):
            STATES[sym]["ema50_1h"] = results[i]


async def load_open_positions():
    if not PAPER_TRADING:
        return
    try:
        if not os.path.exists("paper_state.json"):
            with open("paper_state.json", "w") as f:
                json.dump({"balance_usdt": 150.0, "session_start_balance": 150.0, "positions": {}, "trades": []}, f, indent=4)
        with open("paper_state.json", "r") as f:
            state = json.load(f)
            
        current_time = time.time()
        for sym in ALL_SYMBOLS:
            pk = paper_key(sym)
            pos = state.get("positions", {}).get(pk, {})
            qty = float(pos.get("qty", 0.0))
            if abs(qty) > 0.000001:
                STATES[sym]["qty"] = qty
                STATES[sym]["avg_price"] = float(pos.get("avg_price", 0.0))
                
                # Restore open_time by searching in trades history
                open_time_val = 0.0
                for t in reversed(state.get("trades", [])):
                    if t.get("symbol") == pk and not t.get("is_close"):
                        open_time_val = float(t.get("time", 0)) / 1000.0
                        break
                if open_time_val > 0.0:
                    STATES[sym]["open_time"] = open_time_val
                else:
                    STATES[sym]["open_time"] = current_time

        # 檢查最近的平倉紀錄，加上冷卻時間，防止剛平倉完馬上又自動開倉
        trades = state.get("trades", [])
        for t in reversed(trades):
            if t.get("is_close"):
                # 將 "BTC:USDT" 還原為 "BTCUSDT" 以匹配 STATES 的鍵
                sym = t.get("symbol", "").replace(":USDT", "USDT")
                if sym in STATES:
                    trade_time_sec = t.get("time", 0) / 1000.0
                    # 如果這筆平倉是在最近 10 分鐘內發生的，且當前沒有持倉
                    if current_time - trade_time_sec < 600 and STATES[sym]["qty"] == 0:
                        if STATES[sym]["status"] != "COOLDOWN":
                            STATES[sym]["status"] = "COOLDOWN"
                            STATES[sym]["next_status_time"] = trade_time_sec + 600
    except Exception as e:
        print(f"⚠️ [讀取持倉失敗] {e}")

# ── 指標計算 ──────────────────────────────────────────────────

def compute_indicators(sym):
    s = STATES[sym]
    ohlcv = s["ohlcv"]
    if len(ohlcv) < 20:
        return
    closes = np.array([x[4] for x in ohlcv])
    highs = np.array([x[2] for x in ohlcv])
    lows = np.array([x[3] for x in ohlcv])
    volumes = np.array([x[5] for x in ohlcv])
    s["closes"] = closes
    prev = s["prev_close"]
    for i in range(len(ohlcv)):
        h, l, c = ohlcv[i][2], ohlcv[i][3], ohlcv[i][4]
        if i == 0 and prev is not None:
            tr = max(h - l, abs(h - prev), abs(l - prev))
        elif i > 0:
            tr = max(h - l, abs(h - ohlcv[i-1][4]), abs(l - ohlcv[i-1][4]))
        else:
            tr = h - l
        s["tr_list"].append(tr)
    s["prev_close"] = ohlcv[-1][4]
    if len(s["tr_list"]) > 42:
        s["tr_list"] = s["tr_list"][-42:]
    if len(s["tr_list"]) >= 14:
        s["current_atr"] = float(np.mean(s["tr_list"][-14:]))
        s["atr_history"].append(s["current_atr"])
        if len(s["atr_history"]) > 1440:
            s["atr_history"] = s["atr_history"][-1440:]
        s["atr_ma20"] = float(np.mean(s["atr_history"][-20:])) if len(s["atr_history"]) >= 20 else s["current_atr"]
    if len(closes) > RSI_PERIOD:
        deltas = np.diff(closes[-(RSI_PERIOD + 1):])
        gains = deltas[deltas > 0].mean() if np.any(deltas > 0) else 1e-10
        losses = -deltas[deltas < 0].mean() if np.any(deltas < 0) else 1e-10
        rs = gains / losses
        s["current_rsi"] = 100.0 - (100.0 / (1.0 + rs))
    s["vol_ma10"] = float(np.mean(volumes[-10:])) if len(volumes) >= 10 else float(np.mean(volumes))
    s["vol_ma20"] = float(np.mean(volumes[-20:])) if len(volumes) >= 20 else float(np.mean(volumes))
    s["current_vol"] = float(volumes[-1])
    if len(closes) >= 20:
        s["ema20"] = calculate_ema(closes, 20)
    if len(closes) >= 50:
        s["ema50"] = calculate_ema(closes, 50)
    if len(closes) >= 26:
        m_line, m_sig, m_hist, p_line, p_sig = calculate_macd(closes)
        s["macd_line"] = m_line
        s["macd_signal"] = m_sig
        s["macd_hist"] = m_hist
        s["prev_macd_line"] = p_line
        s["prev_macd_signal"] = p_sig
    if len(closes) >= 20:
        up, mid, low = calculate_bollinger_bands(closes)
        s["bb_up"] = up
        s["bb_mid"] = mid
        s["bb_low"] = low

# ── 出場邏輯 ──────────────────────────────────────────────────

def update_trailing_stop(sym, current_price, is_long):
    """
    實作非對稱移動停損 (Asymmetric Trailing Stop)
    當價格創新高/新低時，上移停損點，且加入保本緩衝區防止被雜訊洗出場。
    """
    s = STATES[sym]
    atr_val = s.get("current_atr", 0.0)
    if atr_val <= 0:
        return False, s["trailing_stop_price"]

    trailing_multiplier = s.get("trailing_stop_multiplier", 2.0)
    liq_price = s["avg_price"] * (1 - s["sl_atr_multiplier"] * (s.get("current_atr", 0.0) / max(s["close_price"], 1e-8)))
    
    if is_long:
        if current_price > s.get("trailing_highest", 0.0):
            s["trailing_highest"] = current_price
            # 修改：使用最高點而非當前價來計算停損，確保停損點只會上移
            trail_sl = s["trailing_highest"] - (atr_val * trailing_multiplier)
            
            # --- 保本邏輯 ---
            trigger_mult = s.get("breakeven_trigger")
            if trigger_mult is None:
                trigger_mult = s.get("sl_atr_multiplier", 1.5)
            sl_dist_atr = trigger_mult * atr_val
            breakeven_trigger = s["avg_price"] + sl_dist_atr
            # 關鍵修正：使用最高價判斷是否已達保本，且保本價包含手續費
            if s.get("trailing_highest", current_price) >= breakeven_trigger:
                breakeven_sl = s["avg_price"] * 1.0025 # 加上 0.25% 確保至少不虧手續費
                trail_sl = max(trail_sl, breakeven_sl)
            
            safe_min_sl = liq_price * 1.2
            new_sl = max(s["trailing_stop_price"], trail_sl) # 確保只往有利方向移動
            if new_sl < safe_min_sl:
                new_sl = safe_min_sl
            
            if new_sl > s["trailing_stop_price"]:
                s["trailing_stop_price"] = new_sl
    else:
        if current_price < s.get("trailing_lowest", float('inf')):
            s["trailing_lowest"] = current_price
            # 修改：使用最低點而非當前價來計算停損，確保停損點只會下移
            trail_sl = s["trailing_lowest"] + (atr_val * trailing_multiplier)
            
            # --- 保本邏輯 ---
            trigger_mult = s.get("breakeven_trigger")
            if trigger_mult is None:
                trigger_mult = s.get("sl_atr_multiplier", 1.5)
            sl_dist_atr = trigger_mult * atr_val
            breakeven_trigger = s["avg_price"] - sl_dist_atr
            # 關鍵修正：使用最低價判斷是否已達保本，且保本價包含手續費
            if s.get("trailing_lowest", current_price) <= breakeven_trigger:
                breakeven_sl = s["avg_price"] * 0.9975 # 減去 0.25% 確保至少不虧手續費
                trail_sl = min(trail_sl, breakeven_sl)
            
            safe_max_sl = liq_price * 0.8
            new_sl = min(s["trailing_stop_price"], trail_sl)
            if new_sl > safe_max_sl:
                new_sl = safe_max_sl
            
            if s["trailing_stop_price"] == 0.0 or new_sl < s["trailing_stop_price"]:
                s["trailing_stop_price"] = new_sl
                    
    return False, s["trailing_stop_price"]
    
def detect_market_regime(sym, current_price, avg_price, is_long):
    s = STATES[sym]
    if len(s["ohlcv"]) < 20 or avg_price <= 0:
        return "HOLD", "資料不足"

    recent_candles = s["ohlcv"][-20:]
    highs = np.array([x[2] for x in recent_candles])
    lows = np.array([x[3] for x in recent_candles])
    closes = np.array([x[4] for x in recent_candles])
    recent_high = float(np.max(highs))
    recent_low = float(np.min(lows))
    range_width_pct = (recent_high - recent_low) / recent_low if recent_low > 0 else 0

    atr_val = s["current_atr"] if s["current_atr"] > 0 else (current_price * 0.01)
    atr_pct = atr_val / current_price if current_price > 0 else 0

    # 1) 即時成交流監聽：大額異常成交 + 明顯逆向價格跳動才判定為突破反轉
    reversal_settings = DEFAULT_REVERSAL_SETTINGS.copy()
    reversal_settings.update(SYMBOL_REVERSAL_SETTINGS.get(sym, {}))
    trade_signal = s.get("trade_signal_strength", 0.0)
    reversal_threshold = reversal_settings["trade_signal_threshold"]
    prev_close = s.get("prev_close")
    if trade_signal >= reversal_threshold and prev_close:
        price_move_pct = (current_price - prev_close) / max(prev_close, 1e-8)
        if (is_long and price_move_pct < -max(reversal_settings["min_reverse_pct"], atr_pct * 1.2)) or \
           (not is_long and price_move_pct > max(reversal_settings["min_reverse_pct"], atr_pct * 1.2)):
            return "BREAKOUT_REVERSAL", f"即時大額成交異常 {s['trade_signal_reason']}"

    # 2) 簡化的大單/突發行情判斷：必須是與持倉方向相反的急速價格跳動
    reversal_settings = DEFAULT_REVERSAL_SETTINGS.copy()
    reversal_settings.update(SYMBOL_REVERSAL_SETTINGS.get(sym, {}))
    volume_surge = s["current_vol"] > s["vol_ma20"] * reversal_settings["volume_multiplier"]
    if prev_close:
        price_jump = (prev_close - current_price) / max(prev_close, 1e-8) > max(reversal_settings["price_jump_pct"], atr_pct * 1.2) if is_long else \
                     (current_price - prev_close) / max(prev_close, 1e-8) > max(reversal_settings["price_jump_pct"], atr_pct * 1.2)
    else:
        price_jump = False
    if volume_surge and price_jump:
        return "BREAKOUT_REVERSAL", "放量突發且價格急速變動"

    # 2) 盤整市場：價格被壓縮在狹窄區間內，且 ATR 也偏小
    is_ranging = range_width_pct < 0.025 and atr_pct < 0.015
    if is_ranging:
        profit_pct = (current_price - avg_price) / avg_price if is_long else (avg_price - current_price) / avg_price
        if profit_pct >= 0.005:
            return "RANGE_PROFIT_TAKE", f"盤整區間內已獲利 {profit_pct * 100:.2f}%"

    return "HOLD", "未達出場條件"


def has_strong_momentum(sym, is_long):
    s = STATES[sym]
    if s.get("vol_ma20", 0.0) <= 0 or len(s.get("closes", [])) < 4:
        return False
    volume_ratio = s["current_vol"] / max(s["vol_ma20"], 1e-8)
    recent_return = (s["closes"][-1] - s["closes"][-4]) / max(abs(s["closes"][-4]), 1e-8)
    if is_long:
        return volume_ratio > 1.2 and s["close_price"] > s.get("bb_mid", 0.0) and s["current_rsi"] > 52 and s.get("macd_hist", 0.0) > 0 and recent_return > 0.005
    return volume_ratio > 1.2 and s["close_price"] < s.get("bb_mid", 0.0) and s["current_rsi"] < 48 and s.get("macd_hist", 0.0) < 0 and recent_return < -0.005

async def close_position(sym, close_side, qty, price, avg_price, reason="", is_stop_loss=False):
    s = STATES[sym]
    s["adjusted_this_tick"] = True
    success = False
    try:
        if abs(s["qty"]) < 0.000001:
            return
        pk = paper_key(sym)
        qty = min(abs(qty), abs(s["qty"]))
        if qty < 0.000001:
            return

        # 动态产生损益标签 (Reason_Tag)
        real_avg = s["avg_price"] if s["avg_price"] > 0 else avg_price
        profit_pct = (price - real_avg) / real_avg if s["qty"] > 0 else (real_avg - price) / real_avg
        
        # --- 实施「最小意义获利门槛 (MMP)」 ---
        is_divergence = "Layer_1_Volume_Divergence" in reason
        if not is_stop_loss and not is_divergence and profit_pct < 0.0015:
            print(f"🛡️ [MMP过滤] {sym} 获利 {profit_pct*100:.2f}% < 0.15% 且非止损/量缩背离，拦截无效平仓 ({reason})")
            return
            
        atr_val = s.get("entry_atr", s.get("current_atr", price * 0.01))
        sl_mult = s.get("sl_atr_multiplier", 1.5)
        initial_risk_pct = (sl_mult * atr_val) / real_avg if real_avg > 0 else 0.01
        
        if profit_pct > 0 and initial_risk_pct > 0 and (profit_pct / initial_risk_pct) >= 2.0:
            pnl_tag = "[Big_Win]"
        elif profit_pct > 0.01:
            pnl_tag = "[大赚]"
        elif profit_pct > 0.002:
            pnl_tag = "[微利]"
        elif profit_pct > -0.002:
            pnl_tag = "[打平]"
        elif profit_pct > -0.01:
            pnl_tag = "[小亏]"
        else:
            pnl_tag = "[大亏]"
            
        full_reason = f"{pnl_tag} {reason}".strip()

        sanitized_qty = await sanitize_order_qty(sym, qty)
        if sanitized_qty <= 0.0:
            print(f"⚠️ [平仓风控] {sym} 无法取得有效数量 ({qty:.6f})")
            return
        # 直接使用处理过交易所精度的数量，避免因为 min() 带回浮点数微小误差
        qty = sanitized_qty

        if PAPER_TRADING:
            real_avg = s["avg_price"] if s["avg_price"] > 0 else avg_price
            if s["qty"] > 0:
                pnl = (price - real_avg) * qty
            else:
                pnl = (real_avg - price) * qty
            update_paper_state(pk, close_side, price, qty, is_close=True, pnl=pnl)
        else:
            global EXECUTION_ENGINE
            if EXECUTION_ENGINE is None:
                EXECUTION_ENGINE = ExecutionEngine(exchange_futures)
            engine = EXECUTION_ENGINE
            config = {
                "is_simulated": False,
                "split_threshold": 100.0,
                "coin_type": s.get("profile_type", "Normal"),
                "num_splits": COIN_PROFILE_CONFIG.get(sym, {}).get("num_splits", 5),
                "step_percent": COIN_PROFILE_CONFIG.get(sym, {}).get("step_percent", 0.001),
                "fee_rate": 0.001,
                "slippage_model": 0.0005
            }
            # Execute closing splits
            await engine.execute_order(sym, close_side, qty, price, config)
            
            # Re-fill until filled or max attempts reached
            max_attempts = 3
            while engine.remaining_quantity > 0.0001 and engine.refill_attempts < max_attempts:
                try:
                    ticker = await exchange_futures.fetch_ticker(sym)
                    current_price = ticker.get('last') or price
                except Exception:
                    current_price = price
                await engine.re_fill_orders(sym, close_side, current_price, config)

            if engine.total_units_filled <= 0:
                print(f"🛑 [平仓失败] {sym} {close_side} | 所有分批限价平仓单均未成交。")
                return

            qty = engine.total_units_filled  # Update qty with actually closed amount

    except Exception as e:
        print(f'🚨 [平倉錯誤] {sym}: {e}')
        return
    remaining = abs(s["qty"]) - qty
    if remaining < 0.01:
        if remaining > 0.000001:
            print(f"🧹 [尘埃清理] {sym} 剩余 {remaining:.6f} 视为已清")
            
        # 计算手续费 (买入与卖出双边手续费，预设 0.04% 币安合约 VIP0 费率或个性化)
        # 用于实体摩擦力计算
        trade_value = (s.get("avg_price", price) + price) * qty
        approx_fees = trade_value * 0.0004
        
        record_trade_result(
            symbol=sym,
            entry_reason=s.get("entry_reason", "UNKNOWN"),
            exit_reason=full_reason,
            profit_pct=profit_pct,
            current_atr=s.get("current_atr", 0.0),
            max_profit_reached=s.get("highest_profit_pct", 0.0),
            expected_entry=s.get("expected_entry_price", s.get("avg_price", price)),
            expected_exit=price, # 触发平仓时的现价视为预期出场价
            actual_entry=s.get("avg_price", price),
            actual_exit=price if PAPER_TRADING else (engine.final_avg_fill_price if 'engine' in locals() else price),
            fees=approx_fees,
            qty=qty
        )
        mark_exit(sym, is_stop_loss=is_stop_loss, reason=full_reason)
        # ★ 立即清零持倉狀態，防止下一個事件循環在 reset 前仍判定有倉位
        s["qty"] = 0.0
        s["avg_price"] = 0.0
        # ★ 記錄平倉時間，供 POST_CLOSE_COOLDOWN_SEC 使用
    else:
        prec = await get_contract_precision(sym)
        raw_qty = (abs(s["qty"]) - qty) * (1 if s["qty"] > 0 else -1)
        s["qty"] = round_step(raw_qty, prec["step_size"])
        print(f"✅ [部分平] {sym} 平{qty} 剩{abs(s['qty']):.4f} {full_reason}")

    save_current_states()
    print(f"💾 [狀態已備份] {sym} 平倉完成。")

async def check_exits(sym):
    s = STATES[sym]
    if s.get("adjusted_this_tick", False):
        return
    if abs(s["qty"]) < 0.000001 or s["avg_price"] <= 0:
        return
        
    hold_sec = time.time() - s["open_time"] if s["open_time"] > 0 else 9999
    atr_history = s.get("atr_history", [])
    atr_24h_avg = float(sum(atr_history)/len(atr_history)) if len(atr_history) > 0 else 0.0
    current_atr = s.get("current_atr", 0.0)
    cooldown_limit = 20.0 if (current_atr > atr_24h_avg and atr_24h_avg > 0) else 60.0
    if hold_sec < cooldown_limit:
        return

    p = s["close_price"]
    avg = s["avg_price"]
    is_long = s["qty"] > 0
    profit_pct = (p - avg) / avg if is_long else (avg - p) / avg
    cs = 'sell' if is_long else 'buy'

    # --- Slippage Compensation (淨利潤扣除 0.15% 摩擦成本) ---
    net_profit_pct = profit_pct - 0.0015

    # ── 初始化量化指標與階梯停利目標 ──
    atr_val = s["current_atr"] if s.get("current_atr", 0.0) > 0 else (p * 0.01)
    atr_pct = (s.get("entry_atr", atr_val) / avg) if avg > 0 else 0.002
    tier1_target = max(atr_pct * 1.5, 0.006)
    tier2_target = max(atr_pct * 2.5, 0.008)
    tier3_target = max(atr_pct * 4.0, 0.012)

    if profit_pct > s["highest_profit_pct"]:
        s["highest_profit_pct"] = profit_pct
    if profit_pct < 0:
        s["has_been_negative"] = True
    if p > s["trailing_highest"]:
        s["trailing_highest"] = p
    if p < s["trailing_lowest"]:
        s["trailing_lowest"] = p

    # --- State Machine Transitions ---
    # [優化1] 降低 TRAILING 啟動門檻 0.01 → 0.006，讓更多交易進入追蹤模式，抓住更多波段
    if s.get("trade_status", "NORMAL") == "NORMAL" and s["highest_profit_pct"] >= 0.006:
        s["trade_status"] = "TRAILING"
        print(f"🔄 [狀態切換] {sym} 獲利達 0.6%，進入 TRAILING 極限追蹤模式！")

    # ==========================================
    # ── ExitManager 底層防線 (MMP與硬停損) ──
    # ==========================================
    macd_is_down = s.get("macd_line", 0) < s.get("macd_signal", 0)
    macd_is_up = s.get("macd_line", 0) > s.get("macd_signal", 0)
    trend_reversed = (is_long and macd_is_down) or (not is_long and macd_is_up)
    
    position_data = {
        "qty": s["qty"],
        "avg_price": s["avg_price"],
        "open_time": s["open_time"]
    }
    market_data = {
        "current_price": p,
        "current_atr": s.get("current_atr", 0.0),
        "trend_reversed": trend_reversed
    }
    
    decision = exit_mgr.check_exit_conditions(sym, position_data, market_data)
    
    if decision["should_exit"]:
        is_stop_loss = "STOP_LOSS" in decision["reason"]
        qty_to_close = abs(s["qty"])
        if decision["exit_type"] == "PARTIAL_50":
            qty_to_close *= 0.5
            s["trade_status"] = "PARTIAL_EXIT"
            
        print(f"🛑 [ExitManager] {sym} 觸發平倉: {decision['reason']} ({decision['exit_type']})")
        await close_position(sym, cs, qty_to_close, p, avg, reason=decision['reason'], is_stop_loss=is_stop_loss)
        return
        
    if "BELOW_MMP" in decision["reason"]:
        # 未達最小意義獲利門檻，且未觸發硬停損或僵局，攔截後續進階邏輯
        return

    # ==========================================
    # Waterfall Logic (Layer 1-4 Defense System)
    # ==========================================

    # ── Layer 1: 量價背離與破位強制止損 (Nuclear Option) ──
    if len(s["ohlcv"]) >= 3:
        # 1.1 放量破位
        prev_k = s["ohlcv"][-2]
        vol_ma20 = s.get("vol_ma20", 0.0)
        current_vol = s.get("current_vol", 0.0)
        if vol_ma20 > 0 and (current_vol / vol_ma20) > 2.0:
            if is_long and p < prev_k[3]:
                print(f"🚨 [Layer_1] {sym} 多單放量跌破前低，緊急強制止損！")
                await close_position(sym, cs, abs(s["qty"]), p, avg, reason="Layer_1_Volume_Breakout", is_stop_loss=True)
                s["highest_profit_pct"] = 0.0
                return
            if not is_long and p > prev_k[2]:
                print(f"🚨 [Layer_1] {sym} 空單放量突破前高，緊急強制止損！")
                await close_position(sym, cs, abs(s["qty"]), p, avg, reason="Layer_1_Volume_Breakout", is_stop_loss=True)
                s["highest_profit_pct"] = 0.0
                return
                
        # 1.2 量價背離 (逃頂)
        c1 = s["ohlcv"][-2]
        # 計算近5根收盤K線的平均量
        recent_vols = [k[5] for k in s["ohlcv"][-7:-2]] if len(s["ohlcv"]) >= 7 else [c1[5]]
        vol_ma_5 = sum(recent_vols) / len(recent_vols)
        
        # 動態利潤門檻：使用 0.4 倍 ATR 或保底 0.1% 來定義「創高/低逃頂」
        atr_pct = (s.get("entry_atr", atr_val) / avg) if avg > 0 else 0.002
        min_divergence_profit = max(atr_pct * 0.4, 0.001)
        
        is_new_high = (is_long and p >= s["trailing_highest"] and profit_pct >= min_divergence_profit)
        is_new_low = (not is_long and p <= s["trailing_lowest"] and profit_pct >= min_divergence_profit)
        
        divergence_exit = False
        if (is_new_high or is_new_low) and c1[5] < (vol_ma_5 * 0.70):
            divergence_exit = True
            
        if divergence_exit:
            print(f"📉 [Layer_1] {sym} 價格創高/低且獲利 > {min_divergence_profit*100:.2f}% 但量能萎縮 (<70%)，量價背離收網！")
            await close_position(sym, cs, abs(s["qty"]), p, avg, reason="Layer_1_Volume_Divergence")
            s["highest_profit_pct"] = 0.0
            return

    # ── Layer 2: 極限追蹤 (Extreme Trailing) ──
    if s.get("trade_status", "NORMAL") == "TRAILING":
        # [優化2] 動態收緊追蹤回撤：獲利越高，追蹤越緊，避免利潤大幅回吐
        atr_pct = (s.get("entry_atr", atr_val) / avg) if avg > 0 else 0.002
        hp = s["highest_profit_pct"]
        if hp >= 0.03:   # 獲利 >= 3%：收最緊，不讓大行情溜走
            dynamic_trailing = max(0.003, atr_pct * 0.2)
        elif hp >= 0.02: # 獲利 >= 2%：收緊
            dynamic_trailing = max(0.004, atr_pct * 0.25)
        else:            # 獲利 0.6%~2%：維持適度寬鬆，讓趨勢繼續跑
            dynamic_trailing = max(0.005, atr_pct * 0.3)
        
        if (is_long and p <= s["trailing_highest"] * (1 - dynamic_trailing)) or (not is_long and p >= s["trailing_lowest"] * (1 + dynamic_trailing)):
            print(f"🏃 [Layer_2] {sym} 極限追蹤觸發，從最高點回撤 {dynamic_trailing*100:.2f}%（最高:{hp*100:.2f}%），獲利了結")
            await close_position(sym, cs, abs(s["qty"]), p, avg, reason="Layer_2_Max_Trailing_Stop")
            s["highest_profit_pct"] = 0.0
            return

    # ── Layer 3: 技術反轉 (Technical Reversal) ──
    macd_is_down = s.get("macd_line", 0) < s.get("macd_signal", 0)
    macd_is_up = s.get("macd_line", 0) > s.get("macd_signal", 0)
    sl_pct = s.get("hard_stop_loss_pct", 0.02)
    early_exit_limit = -(sl_pct * 0.5)
    
    # [優化3] Layer 3 MACD 出場門檻從 1.5% 降至 0.6%（與 tier1_target 對齊）
    # 當獲利已超過 Tier1 且 MACD 反向，代表動能已終結，應立即出場而非繼續等待
    macd_profit_trigger = max(tier1_target, 0.006)  # 與 tier1 掛鉤，動態響應 ATR
    if ((is_long and macd_is_down) or (not is_long and macd_is_up)) and (net_profit_pct < early_exit_limit or net_profit_pct > macd_profit_trigger):
        is_sl = net_profit_pct < 0.0
        print(f"📉 [Layer_3] {sym} MACD狀態反向，趨勢終結立即平倉 (淨利: {net_profit_pct*100:.2f}%, 觸發線:{macd_profit_trigger*100:.2f}%)")
        await close_position(sym, cs, abs(s["qty"]), p, avg, reason="Layer_3_MACD_Reversal", is_stop_loss=is_sl)
        return

    # ── Layer 4: 階梯式動態鎖利 (Dynamic Profit Lock) ──
    
    is_trend_ok = (is_long and s.get("macd_line", 0) > s.get("macd_signal", 0)) or (not is_long and s.get("macd_line", 0) < s.get("macd_signal", 0))
    
    # 建立「最小意義獲利門檻 (MMP)」與「手續費緩衝區 (Fee Buffer)」
    # 要求平倉時，實質拿到的淨利潤必須大於 0.15% (0.0015) 才有意義，否則死扛到底或讓底層防護接手
    if net_profit_pct >= 0.0015:
        if (s["highest_profit_pct"] - 0.0015) >= tier3_target and net_profit_pct < (s["highest_profit_pct"] - 0.0015) * (0.6 if is_trend_ok else 0.4):
            print(f"🛡️ [Layer_4] {sym} 觸發大行情鎖利 (回吐 40%)")
            await close_position(sym, cs, abs(s["qty"]), p, avg, reason="Layer_4_Tier3_Trailing")
            s["highest_profit_pct"] = 0.0
            return
        elif (s["highest_profit_pct"] - 0.0015) >= tier2_target and net_profit_pct < (s["highest_profit_pct"] - 0.0015) * (0.5 if is_trend_ok else 0.3):
            print(f"🛡️ [Layer_4] {sym} 觸發中波段鎖利 (回吐 50%)")
            await close_position(sym, cs, abs(s["qty"]), p, avg, reason="Layer_4_Tier2_Trailing")
            s["highest_profit_pct"] = 0.0
            return
        # [優化4] Tier1 保本線從 0.2% 拉高至 0.3%（atr_pct * 0.6），確保出場時實拿利潤有意義
        elif (s["highest_profit_pct"] - 0.0015) >= tier1_target and net_profit_pct < (max(atr_pct * 0.6, 0.003)):
            print(f"🛡️ [Layer_4] {sym} 觸發基本保本鎖利 (保本線:{max(atr_pct * 0.6, 0.003)*100:.2f}%)")
            await close_position(sym, cs, abs(s["qty"]), p, avg, reason="Layer_4_Tier1_Trailing")
            s["highest_profit_pct"] = 0.0
            return

    # ── Layer 5: 時間防禦與分批平倉 (Time Defense & Partial Exit) ──
    trade_status = s.get("trade_status", "NORMAL")
    if trade_status == "NORMAL":
        # 5.1 50% 分批停利 (Partial Take Profit)
        if net_profit_pct >= tier2_target:
            half_qty = abs(s["qty"]) * 0.5
            print(f"�� [Layer_5] {sym} 淨利達標 (>=2.5ATR)，市價平倉 50% 落袋為安！")
            await close_position(sym, cs, half_qty, p, avg, reason="Layer_5_Partial_TP")
            s["trade_status"] = "PARTIAL_EXIT"
            return
            
        # 5.2 盤整時間防禦 (Stagnation)
        stagnation_limit = get_dynamic_stagnation_limit(s.get("current_atr", atr_val), s.get("atr_ma20", current_atr))
        # 強化果斷性：如果持倉超過 5 分鐘 (300 秒) 或動態上限
        actual_stagnation_limit = min(stagnation_limit, 300)
        
        if hold_sec > actual_stagnation_limit:
            if net_profit_pct < 0.001: 
                print(f"⏳ [Layer_5] {sym} 僵局盤整過久且無法獲利，無效波動直接斬倉")
                await close_position(sym, cs, abs(s["qty"]), p, avg, reason="Layer_5_Stagnation_Kill")
                s["highest_profit_pct"] = 0.0
                return
            elif 0.001 <= net_profit_pct <= 0.005:
                # 在 0.1% ~ 0.5% 之間直接全平，不分批了！
                print(f"⏳ [Layer_5] {sym} 僵局盤整超過 5 分鐘，微利 ({net_profit_pct*100:.2f}%) 直接全平釋放資金")
                await close_position(sym, cs, abs(s["qty"]), p, avg, reason="Layer_5_Stagnation_Full_MicroProfit")
                s["highest_profit_pct"] = 0.0
                return
            elif net_profit_pct > 0.005:
                print(f"⏳ [Layer_5] {sym} 僵局盤整過久，獲利尚可，直接全平落袋")
                await close_position(sym, cs, abs(s["qty"]), p, avg, reason="Layer_5_Stagnation_Full")
                s["highest_profit_pct"] = 0.0
                return

    elif trade_status == "PARTIAL_EXIT":
        # 已經平過 50%，如果卡了超過 8 分鐘且獲利不佳，全跑
        if hold_sec > 480 and net_profit_pct < 0.01:
            print(f"⏳ [Layer_5] {sym} 剩餘倉位盤整過久，全數平倉")
            await close_position(sym, cs, abs(s["qty"]), p, avg, reason="Layer_5_Stagnation_Remaining")
            s["highest_profit_pct"] = 0.0
            return

async def check_position_exits(exchange, sym):
    s = STATES[sym]
    if s.get("adjusted_this_tick", False):
        return
    if abs(s["qty"]) < 0.000001:
        return
    p = s["close_price"]
    avg = s["avg_price"]
    is_long = s["qty"] > 0
    profit_pct = (p - avg) / avg if is_long else (avg - p) / avg
    hold_sec = time.time() - s["open_time"] if s["open_time"] > 0 else 9999

    if hold_sec < 120:
        return

    # 1. 取得 ATR 停利停損倍數
    sl_multiplier = get_effective_exit_setting(sym, "sl_atr_multiplier", s.get("sl_atr_multiplier", SL_ATR_MULTIPLIER), is_long)
    tp_multiplier = get_effective_exit_setting(sym, "tp_atr_multiplier", s.get("tp_atr_multiplier", TP_ATR_MULTIPLIER), is_long)
    atr_val = s["current_atr"] if s.get("current_atr", 0.0) > 0 else (p * 0.01)

    # 初始的距離 (加入最小停損與停利距離保護)
    sl_dist = max(atr_val * sl_multiplier, p * 0.005)
    tp_dist = max(atr_val * tp_multiplier, p * 0.015)

    # 定義初始 TP/SL 價格
    initial_tp = avg + tp_dist if is_long else avg - tp_dist
    initial_sl = avg - sl_dist if is_long else avg + sl_dist

    # 初始化 s["dynamic_sl"] 如果還沒有
    if s.get("dynamic_sl", 0.0) == 0.0:
        s["dynamic_sl"] = initial_sl

    # 2. 保本與移動停損邏輯
    # 判斷是否達到 1:1 盈虧比 (利潤超過初始風險距離)
    profit_dist = (p - avg) if is_long else (avg - p)
    
    if profit_dist >= sl_dist:
        # 達到 1:1，首先確保停損位移到保本點 (含 0.25% 手續費與滑價緩衝)
        breakeven_sl = avg * 1.0025 if is_long else avg * 0.9975

        # --- TSL 加速：追蹤倍數根據當前利潤動態收緊 ---
        if TRAILING_ACCEL_ENABLED:
            tsl_mult = TRAILING_ACCEL_TIERS[-1][1]  # 預設最寬鬆值
            for profit_threshold, mult in TRAILING_ACCEL_TIERS:
                if profit_pct >= profit_threshold:
                    tsl_mult = mult
                    break
        else:
            tsl_mult = 1.5  # 原有固定值
        trail_dist = atr_val * tsl_mult
        trail_sl = p - trail_dist if is_long else p + trail_dist

        # 決定最終的動態停損位 (只會往有利方向移動)
        if is_long:
            new_sl = max(breakeven_sl, trail_sl)
            if new_sl > s.get("dynamic_sl", 0.0):
                s["dynamic_sl"] = new_sl
                print(f"🛡️ [動態停損] {sym} 移至 {new_sl:.6f} (保本/追蹤 {tsl_mult}x ATR, 獲利:{profit_pct*100:.2f}%)")
        else:
            new_sl = min(breakeven_sl, trail_sl)
            current_dyn_sl = s.get("dynamic_sl", float('inf'))
            if current_dyn_sl == 0.0 or new_sl < current_dyn_sl:
                s["dynamic_sl"] = new_sl
                print(f"🛡️ [動態停損] {sym} 移至 {new_sl:.6f} (保本/追蹤 {tsl_mult}x ATR, 獲利:{profit_pct*100:.2f}%)")

    # --- 整合 update_trailing_stop() 的追蹤價格至 dynamic_sl ---
    # （此函數計算了「只往有利方向移動」的非對稱追蹤停損，現在與 dynamic_sl 合併取最優）
    _, tsp = update_trailing_stop(sym, p, is_long)
    if is_long and tsp > 0 and tsp > s.get("dynamic_sl", 0.0):
        s["dynamic_sl"] = tsp
    elif not is_long and tsp > 0:
        current_dyn_sl = s.get("dynamic_sl", 0.0)
        if current_dyn_sl == 0.0 or tsp < current_dyn_sl:
            s["dynamic_sl"] = tsp


    # --- 新增動態追蹤止盈平倉檢測 (ExecutionEngine Trailing Stop) ---
    global EXECUTION_ENGINE
    if not PAPER_TRADING and EXECUTION_ENGINE is not None:
        trailing_act = s.get("trailing_activation", 0.03)
        trailing_dist = s.get("trailing_distance_atr", 1.2)
        triggered = await EXECUTION_ENGINE.check_and_apply_trailing_stops(
            symbol=sym,
            current_price=p,
            atr_val=atr_val,
            trailing_activation_pct=trailing_act,
            trailing_distance_atr=trailing_dist,
            close_position_func=close_position
        )
        if triggered:
            return
            
    # 3. 執行停利或停損
    cs = 'sell' if is_long else 'buy'
    
    # --- 新增安全氣囊：硬性 USDT 虧損上限保護 (Max Loss per Trade) ---
    max_loss_usdt = s.get("max_loss_usdt", 10.0)
    current_loss_usdt = 0.0
    if is_long and p < avg:
        current_loss_usdt = (avg - p) * abs(s["qty"])
    elif not is_long and p > avg:
        current_loss_usdt = (p - avg) * abs(s["qty"])

    if current_loss_usdt >= max_loss_usdt:
        reason = "[Hard_Loss_USDT]"
        print(f"🚨🚨🚨 [{reason}] {sym} 虧損額已達限制上限 {current_loss_usdt:.2f} USDT >= {max_loss_usdt:.2f} USDT，觸發最終保命平倉！")
        await close_position(sym, cs, abs(s["qty"]), p, avg, reason=reason, is_stop_loss=True)
        s["sl_trigger_time"] = 0
        return
        
    # 檢查是否觸發停損 (Dynamic SL or Hard Stop)
    hard_stop_loss_pct = get_effective_exit_setting(sym, "hard_stop_loss_pct", s.get("hard_stop_loss_pct", HARD_STOP_LOSS_PCT), is_long)
    hard_sl = avg * (1 - hard_stop_loss_pct) if is_long else avg * (1 + hard_stop_loss_pct)
    
    active_sl = max(s["dynamic_sl"], hard_sl) if is_long else min(s["dynamic_sl"], hard_sl)
    
    if (is_long and p <= active_sl) or (not is_long and p >= active_sl):
        is_high_volume = s.get("current_vol", 0) > s.get("vol_ma20", 0) * 1.5
        
        if is_high_volume:
            reason = "[Trend_Follow]"
            print(f"🛑 [{reason}] {sym} 觸發價格 {active_sl:.6f} (現價:{p:.6f})")
            await close_position(sym, cs, abs(s["qty"]), p, avg, reason=reason, is_stop_loss=(profit_pct < 0))
            s["sl_trigger_time"] = 0
            return
        else:
            if s.get("sl_trigger_time", 0) == 0:
                s["sl_trigger_time"] = time.time()
                print(f"⚠️ [防插針觀察] {sym} 觸發停損 {active_sl:.6f} 但量能小 ({s.get('current_vol',0):.2f} < {s.get('vol_ma20',0)*1.5:.2f})，進入 2 秒觀察期...")
                return
            elif time.time() - s["sl_trigger_time"] >= 2.0:
                reason = "[Trend_Follow]"
                print(f"🛑 [{reason}] {sym} 持續觸發停損超過 2 秒，確認執行！")
                await close_position(sym, cs, abs(s["qty"]), p, avg, reason=reason, is_stop_loss=(profit_pct < 0))
                s["sl_trigger_time"] = 0
                return
            else:
                return
    else:
        s["sl_trigger_time"] = 0

    # 檢查是否觸發分批停利 (Partial Close at 1.5 ATR or 0.8%)
    partial_tp_dist = max(atr_val * 1.5, p * 0.008)
    partial_tp_price = avg + partial_tp_dist if is_long else avg - partial_tp_dist
    if not s.get("has_partial_closed", False) and ((is_long and p >= partial_tp_price) or (not is_long and p <= partial_tp_price)):
        half_qty = abs(s["qty"]) * 0.5
        if half_qty >= (s.get("min_qty", 0.001) if "min_qty" in s else 0.0):
            print(f"🎯 [分批停利] {sym} 觸發 1.5 ATR 或 0.8% 利潤，先平倉 50% 落袋為安")
            await close_position(sym, cs, half_qty, p, avg, reason="分批停利 50%")
            s["has_partial_closed"] = True
            
            # 關鍵修正：平倉 50% 後，剩下的 50% 立刻切換到「極限追蹤模式」(緊密跟隨 0.8 ATR)
            s["trailing_stop_multiplier"] = 0.8
            # 同時上調保本點或更新動態停損
            if is_long:
                s["trailing_stop_price"] = max(s.get("trailing_stop_price", 0.0), p - atr_val * 0.8)
            else:
                if s.get("trailing_stop_price", 0.0) == 0.0:
                    s["trailing_stop_price"] = p + atr_val * 0.8
                else:
                    s["trailing_stop_price"] = min(s.get("trailing_stop_price", 0.0), p + atr_val * 0.8)
            # 不 return，讓剩餘倉位繼續走下面的追蹤邏輯

    # 檢查是否觸發最終停利 (TP)
    if (is_long and p >= initial_tp) or (not is_long and p <= initial_tp):
        # 強勢行情不摸頂 (Let Profits Run)
        rsi = s.get("current_rsi", 50.0)
        if (is_long and rsi > 75) or (not is_long and rsi < 25):
            print(f"🚀 [強勢行情] {sym} 觸發停利點，但 RSI 極度強勢 ({rsi:.1f})，不全平倉，改為極限追蹤停損！")
            trail_extreme = p - atr_val * 0.5 if is_long else p + atr_val * 0.5
            s["dynamic_sl"] = trail_extreme
        else:
            print(f"🎯 [ATR停利] {sym} 觸發價格 {initial_tp:.6f} (現價:{p:.6f})")
            await close_position(sym, cs, abs(s["qty"]), p, avg, reason="ATR停利")
            return

    # 多層次時間停損
    if hold_sec > 3600 and profit_pct < 0.0:
        print(f"⏱️ [超時停損] {sym} 持倉過久 ({hold_sec/60:.1f}分) 且未獲利，釋放資金")
        await close_position(sym, cs, abs(s["qty"]), p, avg, reason="超時無利潤出場", is_stop_loss=True)
        return
    elif hold_sec > 900 and profit_pct < -0.005:
        print(f"⏱️ [時間停損] {sym} {hold_sec/60:.1f}分仍顯著虧損")
        await close_position(sym, cs, abs(s["qty"]), p, avg, reason="時間停損", is_stop_loss=True)
        return


# ── 進場邏輯 ──────────────────────────────────────────────────




async def execute_order(sym, side, price, route="UNKNOWN"):
    # --- 1. 防止異步競爭鎖定 ---
    if trading_locks.get(sym):
        print(f"⏳ [鎖定中] {sym} 正在處理另一個下單動作，拒絕重複觸發")
        return

    trading_locks[sym] = True # 獲取鎖
    try:
        s = STATES[sym]
        now_time = time.time()
        
        # --- 2. 平倉後冷卻檢查 ---
        if s.get("entry_count", 0) == 0:
            last_close = s.get("last_close_time", 0)
            if now_time - last_close < POST_CLOSE_COOLDOWN_SEC:
                print(f"❄️ [冷卻中] {sym} 距離上次平倉不足 {POST_CLOSE_COOLDOWN_SEC} 秒，拒絕在高位重複開倉")
                return

        # --- 3. 槓桿與價格檢查 ---
        pk = paper_key(sym)
        lev = get_symbol_leverage(sym)
        s["leverage"] = lev
        print(f"@@LEVERAGE@@{lev}")
        if not PAPER_TRADING:
            try:
                await exchange_futures.set_leverage(lev, convert_to_ccxt_symbol(sym))
            except Exception as e:
                print(f"⚠️ [槓桿設定失敗] {sym}: {e}")
        
        margin = compute_per_coin_margin(sym)
        if margin <= 0:
            print(f"⚠️ [風控] {sym} 無可用保證金")
            return

        try:
            ticker = await exchange_futures.fetch_ticker(sym)
            market_price = ticker.get('last')
            if market_price and market_price > 0:
                deviation = abs(price - market_price) / market_price
                if deviation > 0.05:
                    print(f"🚨 [風控] {sym} 訂單價格 {price} 嚴重偏離市價 {market_price} (偏離 {deviation*100:.2f}%)，拒絕執行！")
                    return
        except Exception as e:
            print(f"⚠️ [價格偏離檢查失敗] {e}")

        # --- 4. 倉位大小計算 (保留您的 ATR 動態風控法) ---
        if s["entry_count"] == 0:
            base_amt = calculate_dynamic_qty(sym, price, side)
            base_amt = await sanitize_order_qty(sym, base_amt)
        else:
            if now_time - s["last_entry_time"] < s["entry_cooldown_sec"]:
                print(f"⏳ [加倉冷卻] {sym} 距離上次加倉不足 {s['entry_cooldown_sec']} 秒")
                return
            if s["entry_count"] >= s["max_additional_entries"]:
                print(f"⚠️ [加倉上限] {sym} 已達最大加倉次數")
                return
            if s["avg_price"] > 0 and s["close_price"] > 0:
                profit_pct = (s["close_price"] - s["avg_price"]) / s["avg_price"] if side == 'buy' else (s["avg_price"] - s["close_price"]) / s["avg_price"]
                if profit_pct < 0.001:
                    print(f"🛑 [加倉風控] {sym} 目前尚未回到保本線以上，不加倉 (利潤: {profit_pct*100:.2f}%)")
                    return

        balance = get_balance()
        if s["entry_count"] == 0:
            base_amt = calculate_dynamic_qty(sym, price, side)
            base_amt = await sanitize_order_qty(sym, base_amt)
        else:
            target_notional = margin * lev
            allocation_pct  = s["add_entry_pct"]
            base_notional   = target_notional * allocation_pct
            if base_notional > 1000.0:
                base_notional = 1000.0
            required_margin = base_notional / lev
            if required_margin > balance * 0.98:
                base_notional = (balance * 0.98) * lev
            base_amt = base_notional / price
            base_amt = await sanitize_order_qty(sym, base_amt)

        if base_amt <= 0.0:
            print(f"🚨 [開倉錯誤] {sym} 計算並經精度修剪後的下單量為 0，停止下單流程！")
            return

        actual_notional = base_amt * price
        if actual_notional < 6.0 and actual_notional > 0:
            min_qty = 6.0 / price
            min_qty = await sanitize_order_qty(sym, min_qty)
            if (min_qty * price) / lev > balance * 0.98:
                print(f"⚠️ [風控] {sym} 資金不足以達到最小開倉額度 6 USDT (餘額: {balance:.2f})")
                return
            base_amt = min_qty
            actual_notional = base_amt * price

        if base_amt <= 0.0:
            print(f"⚠️ [風控] {sym} 計算後開倉數量為 0")
            return

        # --- 5. 下單執行邏輯 (保留原有的 ExecutionEngine) ---
        if PAPER_TRADING:
            update_paper_state(pk, side, price, base_amt)
            if side == 'buy':
                s["qty"] += base_amt
            else:
                s["qty"] -= base_amt
            if s["avg_price"] <= 0:
                s["avg_price"] = price
                s["entry_atr"] = s.get("current_atr", 0.0)
            else:
                old_abs_qty = abs(s["qty"]) - base_amt
                if old_abs_qty > 0:
                    s["avg_price"] = ((s["avg_price"] * old_abs_qty) + (price * base_amt)) / abs(s["qty"])
            s["open_time"] = now_time
            s["last_buy_time"] = now_time
            s["last_entry_time"] = now_time
            s["entry_count"] += 1
            s["expected_entry_price"] = price
            direction = "做多" if side == 'buy' else "做空"
            print(f"🟢 [{direction}] {sym} {base_amt:.4f} @ {price} (保證金:{margin:.2f} USDT)")
        else:
            global EXECUTION_ENGINE
            if EXECUTION_ENGINE is None:
                EXECUTION_ENGINE = ExecutionEngine(exchange_futures)
            await engine.sync_balance(fetch_real_balance)
            
            config = {
                "is_simulated": False,
                "split_threshold": 100.0,
                "coin_type": s.get("profile_type", "Normal"),
                "num_splits": COIN_PROFILE_CONFIG.get(sym, {}).get("num_splits", 5),
                "step_percent": COIN_PROFILE_CONFIG.get(sym, {}).get("step_percent", 0.001),
                "fee_rate": 0.001,
                "slippage_model": 0.0005
            }
            await engine.execute_order(sym, side, base_amt, price, config)
            
            max_attempts = 3
            while engine.remaining_quantity > 0.0001 and engine.refill_attempts < max_attempts:
                try:
                    ticker = await exchange_futures.fetch_ticker(sym)
                    current_price = ticker.get('last') or price
                except Exception:
                    current_price = price
                await engine.re_fill_orders(sym, side, current_price, config)

            if engine.total_units_filled <= 0:
                print(f"🛑 [實盤開倉失敗] {sym} {side} | 所有分批限價開倉單均未成交。")
                return

            fill_price = engine.final_avg_fill_price
            actual_filled_qty = engine.total_units_filled
            slippage = (fill_price - price) / price if price > 0 else 0
            if side == 'sell':
                slippage = (price - fill_price) / price if price > 0 else 0
                
            print(f"✅ [實盤開倉成功] {sym} {side} | 預期: {price:.6f} | 實際: {fill_price:.6f} | 實質成交: {actual_filled_qty:.4f} | 滑價: {slippage*100:.3f}%")
            
            old_qty = s["qty"]
            if side == 'buy':
                s["qty"] += actual_filled_qty
            else:
                s["qty"] -= actual_filled_qty
                
            if s["avg_price"] <= 0:
                s["avg_price"] = fill_price
                s["entry_atr"] = s.get("current_atr", 0.0)
            else:
                s["avg_price"] = ((s["avg_price"] * abs(old_qty)) + (fill_price * actual_filled_qty)) / abs(s["qty"])
                
            s["open_time"] = now_time
            s["last_buy_time"] = now_time
            s["last_entry_time"] = now_time
            s["entry_count"] += 1
            s["expected_entry_price"] = price
            s["last_flip_time"] = now_time

        save_current_states()
        print(f"💾 [狀態已備份] {sym} 開倉成功。")

    except Exception as e:
        print(f"🚨 [開倉錯誤] {sym}: {e}")
    finally:
        trading_locks[sym] = False # 釋放鎖
        save_current_states()
def should_recover_from_reversal(sym, is_long):
    s = STATES[sym]
    if abs(s["qty"]) < 0.000001:
        return False
    macd_reversal = (is_long and s["prev_macd_line"] > s["prev_macd_signal"] and s["macd_line"] < s["macd_signal"]) or \
                    (not is_long and s["prev_macd_line"] < s["prev_macd_signal"] and s["macd_line"] > s["macd_signal"])
    if not macd_reversal or not s.get("prev_close") or len(s["ohlcv"]) < 2:
        return False
    current_price = s["close_price"]
    atr_val = s["current_atr"] if s["current_atr"] > 0 else (current_price * 0.01)
    prev_bar_high = s["ohlcv"][-2][2]
    prev_bar_low = s["ohlcv"][-2][3]
    breakout_confirmed = False
    if is_long:
        breakout_confirmed = current_price < prev_bar_low and prev_bar_low - current_price > max(atr_val * 0.25, 0.001)
    else:
        breakout_confirmed = current_price > prev_bar_high and current_price - prev_bar_high > max(atr_val * 0.25, 0.001)
    reversal_settings = DEFAULT_REVERSAL_SETTINGS.copy()
    reversal_settings.update(SYMBOL_REVERSAL_SETTINGS.get(sym, {}))
    volume_confirmed = s["current_vol"] > s["vol_ma20"] * reversal_settings["volume_multiplier"]
    trade_signal = s.get("trade_signal_strength", 0.0)
    trade_confirmed = trade_signal >= reversal_settings["trade_signal_threshold"]
    if macd_reversal and breakout_confirmed and volume_confirmed and trade_confirmed:
        return True
    return False

async def main():
    global ALL_SYMBOLS
    try:
        print("🔍 [系統初始化] 正在向交易所加載市場資訊以驗證交易對上架狀態...")
        await exchange_futures.load_markets()
        
        valid_symbols = []
        for sym in ALL_SYMBOLS:
            ccxt_symbol = convert_to_ccxt_symbol(sym)
            if ccxt_symbol in exchange_futures.markets:
                market_info = exchange_futures.market(ccxt_symbol)
                if market_info.get('active', True):
                    valid_symbols.append(sym)
                else:
                    print(f"⚠️ [上架檢查] {sym} 已暫停交易或下架，自動剔除。")
            else:
                print(f"🚨 [上架檢查] {sym} 在幣安合約市場中不存在，自動剔除。")
                
        if len(valid_symbols) != len(ALL_SYMBOLS):
            print(f"✅ [上架檢查] 已完成過濾。有效幣種數量: {len(valid_symbols)} / {len(ALL_SYMBOLS)}")
            ALL_SYMBOLS = valid_symbols
        else:
            print(f"✅ [上架檢查] 所有監控幣種皆正常上架且可交易！")
    except Exception as e:
        print(f"⚠️ [上架檢查] 無法與交易所連線進行幣種核對: {e}，將使用預設清單。")

    asyncio.create_task(periodic_htf_update(exchange_futures))
    asyncio.create_task(periodic_status_log())
    
    while True:
        try:
            await main_loop(exchange_futures)
        except Exception as e:
            import traceback
            print(f"🚨 [致命錯誤] main_loop 崩潰: {e}")
            traceback.print_exc()
            print("⏳ 將在 10 秒後由內部自動重啟主程序...")
            await asyncio.sleep(10)
        finally:
            # 如果因為某種原因跳出，確保資源有被釋放或嘗試重新連接
            pass

def wait_for_network(timeout=60):
    """確保網路連線正常才開始啟動機器人"""
    import time
    import requests
    print("⏳ 正在等待網路連線...")
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            # 嘗試連線到 Google 或 幣安 API
            requests.get("https://google.com", timeout=3)
            print("✅ 網路已就緒，開始初始化機器人...")
            return True
        except Exception:
            time.sleep(5)
    print("❌ 網路連線超時，請檢查網路設定。")
    return False

if __name__ == "__main__":
    if wait_for_network():
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            print("🛑 程式已被手動終止")
        finally:
            # 在退出前關閉交易所連接
            async def cleanup():
                await exchange_futures.close()
                await exchange_spot.close()
            asyncio.run(cleanup())
