import os
import io
import csv
import json
import math
import random
import datetime
import threading
import time
import numpy as np
import requests
import pytz
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
from typing import List

from services.utils import parse_symbol, paper_key
from services.system_log_service import get_system_logs, add_system_log, clear_system_logs
from services.bot_manager_service import get_bot_status, toggle_bot, set_bot_symbol, set_bot_amount, set_bot_watch_symbols, kill_bot
from services.binance_service import (
    api_key, client, get_price, get_all_prices, get_position, get_trades, get_klines,
    get_all_positions,
    market_buy, market_short, market_sell
)
from services.paper_trade_service import (
    get_paper_balance, get_paper_position, get_paper_trades,
    market_buy as paper_market_buy, 
    market_short as paper_market_short, 
    market_sell as paper_market_sell,
    force_close_all_positions,
    reset_paper_state,
    get_session_start_balance,
)
from services.radar_service import trigger_manual_radar, auto_radar_switch, ATR_ELIGIBLE_SYMBOLS, RADAR_SELECT_COUNT

load_dotenv()

@asynccontextmanager
async def lifespan(app: FastAPI):
    # 啟動 6:00 AM 定時器
    threading.Thread(target=daily_reset_daemon, daemon=True).start()
    # 每 4 小時定期 ATR 雷達重掃
    threading.Thread(target=_periodic_radar_daemon, daemon=True).start()
    # 啟動時跑雷達更新幣池（選最強 RADAR_SELECT_COUNT 隻）再恢復機器人
    threading.Thread(target=_startup_radar_restore, daemon=True).start()
    yield


app = FastAPI(title="Binance Bot API Backend", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

def is_paper_trading():
    from core.config import PAPER_TRADING
    return PAPER_TRADING

def daily_market_clean_and_reset(is_manual=False):
    """大掃除與即時同步前五名 (模組化)"""
    try:
        trigger_type = "手動強制" if is_manual else "每日/開機"
        add_system_log(f"🔄 [{trigger_type}換班] 啟動排程換班機制！", "warning")
        kill_bot()
        if is_manual:
            force_close_all_positions()
            add_system_log(f"🧹 [{trigger_type}淨空] 系統狀態已重置，舊訂單已撤銷並強制平倉。", "success")
            clear_system_logs()
        else:
            add_system_log(f"🧹 [{trigger_type}換班] 已保留現有持倉部位，將由機器人繼續監控至正常出場。", "success")
        auto_radar_switch(force_start=True)
    except Exception as e:
        add_system_log(f"🚨 [{trigger_type}換班] 發生錯誤: {e}", "danger")

def daily_reset_daemon():
    tz = pytz.timezone('Asia/Taipei')
    while True:
        now = datetime.datetime.now(tz)
        target = now.replace(hour=6, minute=0, second=0, microsecond=0)

        # If today's 6 AM is already past, target tomorrow's 6 AM
        if now >= target:
            target += datetime.timedelta(days=1)

        wait_seconds = (target - now).total_seconds()

        time.sleep(wait_seconds)
        daily_market_clean_and_reset(is_manual=False)


def _periodic_radar_daemon():
    """每 4 小時重新掃描 ATR 排名，更新監控幣池與動態個性參數。
    啟動時跳過第一輪（_startup_radar_restore 已掃過一次）。"""
    INTERVAL = 4 * 3600
    time.sleep(INTERVAL)
    while True:
        try:
            add_system_log("⏰ [定期雷達] 4 小時定期 ATR 重掃，更新監控幣池...", "info")
            auto_radar_switch(force_start=False)
        except Exception as e:
            add_system_log(f"🚨 [定期雷達] 掃描失敗: {e}", "danger")
        time.sleep(INTERVAL)


def _startup_radar_restore():
    """啟動時先跑雷達選出最強幣種，再依儲存狀態決定是否恢復機器人。
    取代 auto_restore_bot_on_startup，確保每次重啟都使用最新的 RADAR_SELECT_COUNT。"""
    time.sleep(3)  # 等 uvicorn 完全就緒
    try:
        from services.bot_manager_service import BOT_STATE_PATH, bot_status
        was_running = False
        if os.path.exists(BOT_STATE_PATH):
            with open(BOT_STATE_PATH, "r") as f:
                saved = json.load(f)
            was_running = saved.get("is_running", False)
            bot_status["trade_amount"] = saved.get("trade_amount", bot_status.get("trade_amount", 150.0))
        auto_radar_switch(force_start=was_running)
    except Exception as e:
        add_system_log(f"⚠️ [啟動雷達] 失敗，改用舊幣池恢復: {e}", "warning")
        from services.bot_manager_service import auto_restore_bot_on_startup
        auto_restore_bot_on_startup()



@app.get("/")
def read_root():
    with open(os.path.join(os.path.dirname(__file__), "..", "web", "index.html"), "r", encoding="utf-8") as f:
        content = f.read()
    response = HTMLResponse(content=content, media_type="text/html; charset=utf-8")
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

@app.post("/api/force-reset")
def api_force_reset():
    try:
        # 手動強制大掃除
        daily_market_clean_and_reset(is_manual=True)
        return {"status": "success", "detail": "大掃除與前五名同步完成"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/bot-status")
def api_get_bot_status():
    status = get_bot_status()
    from core.config import USE_TESTNET
    status["use_testnet"] = USE_TESTNET
    status["environment"] = "testnet" if USE_TESTNET else "live"
    if "entry_diagnosis" not in status:
        status["entry_diagnosis"] = "等待訊號"
    if is_paper_trading():
        status["balance_quote"] = get_paper_balance()
        status["session_start_balance"] = get_session_start_balance()
    else:
        # 實盤餘額的取得可放在 binance_service，為簡化先保留原本邏輯(這部分會用到 binance_service，為快速先這樣)
        pass 
    return status

@app.post("/api/bot-status/toggle")
def api_toggle_bot():
    is_running = toggle_bot()
    return {"status": "success", "is_running": is_running}

@app.post("/api/bot-status/set-symbol/{symbol}")
def api_set_bot_symbol(symbol: str):
    active_symbol = set_bot_symbol(symbol)
    return {"status": "success", "active_symbol": active_symbol}

class WatchSymbolsReq(BaseModel):
    symbols: List[str]

class ActiveSymbolsReq(BaseModel):
    symbols: List[str]

@app.post("/api/bot-status/set-symbols")
def api_set_bot_symbols(req: ActiveSymbolsReq):
    try:
        symbols = set_bot_symbol(req.symbols)
        # Also update watch symbols with the first 5 for backward compatibility if needed
        set_bot_watch_symbols(symbols)
        return {"status": "success", "active_symbols": symbols}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/bot-status/set-active-symbols")
def api_set_active_symbols(req: ActiveSymbolsReq):
    try:
        symbols = set_bot_symbol(req.symbols)
        return {"status": "success", "active_symbols": symbols}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/bot-status/set-amount/{amount}")
def api_set_bot_amount(amount: float):
    try:
        amt = set_bot_amount(amount)
        return {"status": "success", "trade_amount": amt}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/bot-status/reset-realized-pnl")
def api_reset_realized_pnl():
    try:
        from services.binance_service import reset_total_realized_pnl_baseline
        raw_total = reset_total_realized_pnl_baseline()
        return {"status": "success", "previous_total": raw_total, "total_realized_pnl": 0.0}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/logs")
def api_get_logs():
    return get_system_logs()

@app.get("/api/sl-states")
def api_sl_states():
    return get_bot_status().get("sl_states", {})

@app.get("/api/trend-bias")
def api_trend_bias():
    return get_bot_status().get("trend_bias", {})

@app.get("/api/radar/scan")
def api_radar_scan():
    try:
        return trigger_manual_radar()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/radar/atr-rank")
def api_radar_atr_rank():
    try:
        from services.binance_service import get_atr_ranked_coins
        from services.radar_service import BLACKLIST
        scan_pool = [s for s in ATR_ELIGIBLE_SYMBOLS if s not in BLACKLIST]
        selected, full_ranking = get_atr_ranked_coins(scan_pool, limit=RADAR_SELECT_COUNT)
        return {"success": True, "selected": selected, "ranking": full_ranking}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/klines/{symbol}")
def api_get_klines(symbol: str, interval: str = "1m", limit: int = 80):
    try:
        klines = get_klines(symbol.upper(), interval, limit)
        return {"status": "success", "data": klines}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/price/{symbol}")
def api_get_price(symbol: str):
    try:
        return get_price(symbol.upper())
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/prices")
def api_get_all_prices():
    try:
        prices = get_all_prices()
        return {"status": "success", "data": prices}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/positions")
def api_get_all_positions():
    try:
        if is_paper_trading():
            from services.paper_trade_service import get_paper_positions
            return get_paper_positions()
        else:
            from services.binance_service import get_all_positions
            return get_all_positions()
    except Exception as e:
        # 原本這裡吞掉例外直接回傳 {}，前端看起來像「沒有未實現損益」，但實際上是查詢失敗，
        # 而不是真的沒有持倉——留下 log 才能追查到底是什麼原因查詢失敗。
        add_system_log(f"🚨 [持倉查詢失敗] /api/positions: {e}", "danger")
        return {}

@app.get("/api/position/{symbol}")
def api_get_position(symbol: str):
    symbol_upper = symbol.upper()
    base_asset, quote_asset = parse_symbol(symbol_upper)
    try:
        if is_paper_trading():
            pk = paper_key(symbol_upper)
            return get_paper_position(symbol_upper, quote_asset, base_asset, pk)
        else:
            positions = get_all_positions()
            key = symbol_upper.replace("USDT", ":USDT")
            if key in positions:
                return positions[key]
            return {
                "asset": base_asset,
                "quote_asset": quote_asset,
                "qty": 0.0,
                "avg_price": 0.0,
                "current_price": 0.0,
                "pnl": 0.0,
                "pnl_percent": 0.0,
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"獲取持倉狀態失敗: {str(e)}")

@app.get("/api/trades/{symbol}")
def api_get_trades(symbol: str):
    symbol_upper = symbol.upper()
    try:
        if is_paper_trading():
            pk = paper_key(symbol_upper)
            return get_paper_trades(symbol_upper, pk)
        else:
            if symbol_upper != "ALL":
                return get_trades(symbol_upper)

            # 儀表板只需本機已記錄的成交與目前持倉；不再為每個歷史幣種逐一呼叫
            # futures_account_trades，避免單次刷新累積數十個高權重請求。
            trades = _get_real_trades()[-100:]
            open_positions = get_all_positions()
            now_ms = int(time.time() * 1000)
            for key, pos in open_positions.items():
                qty = float(pos.get("qty", 0.0) or 0.0)
                if abs(qty) <= 0.000001:
                    continue
                # 目前持倉的幣種數量最多就 MAX_POSITIONS（通常 <=3），只針對這幾檔
                # 補查真實成交拿正確的手續費，不會像查全部歷史幣種那樣把權重打爆。
                # 原本這裡手續費是寫死 0.0，導致交易列表上「還開著」的部位手續費
                # 永遠顯示 0，即使幣安那邊其實已經扣了手續費。
                real_price = float(pos.get("entryPrice", 0.0) or 0.0)
                real_fee = 0.0
                real_time = int(pos.get("open_time_ms") or now_ms)
                try:
                    raw_sym = str(key).replace(":", "").replace("/", "").upper()
                    fills = client.futures_account_trades(symbol=raw_sym, limit=20)
                    entry_fills = [f for f in fills if float(f.get("realizedPnl", 0.0) or 0.0) == 0.0]
                    if entry_fills:
                        total_qty = sum(float(f["qty"]) for f in entry_fills)
                        total_notional = sum(float(f["qty"]) * float(f["price"]) for f in entry_fills)
                        real_price = total_notional / total_qty if total_qty > 0 else real_price
                        real_fee = sum(float(f.get("commission", 0.0) or 0.0) for f in entry_fills)
                        real_time = max(f.get("time", real_time) for f in entry_fills)
                except Exception:
                    pass
                trades.append({
                    "id": f"pos_{key}",
                    "order_id": None,
                    "symbol": key,
                    "price": real_price,
                    "qty": abs(qty),
                    "time": real_time,
                    "isBuyer": qty > 0,
                    "is_close": False,
                    "realized_pnl": 0.0,
                    "fee": real_fee,
                    "_is_open_position": True,
                })
            return trades
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/order/market-buy/{symbol}")
def api_market_buy(symbol: str, amount: float = 150.0):
    try:
        symbol_upper = symbol.upper()
        if is_paper_trading():
            order = paper_market_buy(symbol_upper, amount)
            return {"status": "success", "order": order}
        else:
            order = market_buy(symbol_upper, amount)
            return {"status": "success", "order": order}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"買入失敗: {str(e)}")

@app.post("/api/order/market-short/{symbol}")
def api_market_short(symbol: str, amount: float = 150.0):
    try:
        symbol_upper = symbol.upper()
        if is_paper_trading():
            order = paper_market_short(symbol_upper, amount)
            return {"status": "success", "order": order}
        else:
            order = market_short(symbol_upper, amount)
            return {"status": "success", "order": order}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"做空失敗: {str(e)}")

@app.post("/api/order/market-sell/{symbol}")
def api_market_sell(symbol: str):
    try:
        symbol_upper = symbol.upper()
        if is_paper_trading():
            pk = paper_key(symbol_upper)
            msg = paper_market_sell(symbol_upper, pk)
            return {"status": "success", "detail": msg}
        else:
            base_asset, _ = parse_symbol(symbol_upper)
            order = market_sell(symbol_upper, base_asset)
            return {"status": "success", "order": order}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"平倉失敗: {str(e)}")

@app.post("/api/order/close-all")
def api_close_all_orders():
    try:
        force_close_all_positions()
        return {"status": "success", "detail": "已強制平倉所有持有部位"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"一鍵平倉失敗: {str(e)}")


@app.post("/api/paper-state/reset")
def api_reset_paper_state(balance: float = 150.0):
    try:
        reset_paper_state(balance)
        return {"status": "success", "detail": f"紙交易狀態已重置為 {balance} USDT"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"紙交易重置失敗: {str(e)}")


@app.get("/api/exchangerate/usdtwd")
def api_get_usd_twd():
    try:
        response = requests.get("https://open.er-api.com/v6/latest/USD", timeout=5)
        response.raise_for_status()
        data = response.json()
        rates = data.get("rates", {})
        twd_rate = rates.get("TWD")
        if not twd_rate:
            raise Exception("未能獲取到 TWD 匯率")
        return {"base": "USD", "target": "TWD", "rate": twd_rate}
    except Exception as e:
        return {"base": "USD", "target": "TWD", "rate": 32.50, "warning": f"API 獲取失敗，使用預設值。錯誤: {str(e)}"}



@app.get("/api/chart/predict/{symbol}")
def api_chart_predict(symbol: str):
    """回傳 Chart.js 格式的未來 5 分鐘價格預測折線圖"""
    symbol_upper = symbol.upper()
    try:
        klines = get_klines(symbol_upper, "1m", limit=20)
        if not klines or len(klines) < 5:
            return {"status": "error", "detail": "K 線資料不足"}

        closes_raw = [k["close"] for k in klines]
        closes = np.array(closes_raw)
        times = [k["open_time"] for k in klines]

        # 計算歷史標籤 (HH:mm)
        hist_labels = []
        for t in times:
            dt = datetime.datetime.fromtimestamp(t)
            hist_labels.append(dt.strftime("%H:%M"))

        # 用最近 5 筆變化計算平均變動率與波動度
        returns = np.diff(closes[-10:]) / closes[-10:-1]
        mean_return = float(np.mean(returns))
        std_return = float(np.std(returns)) if float(np.std(returns)) > 0 else abs(mean_return) * 0.5
        if std_return < 0.0001:
            std_return = 0.0001

        # 模擬未來 5 分鐘 (隨機漫步 + 動量)
        last_price = float(closes[-1])
        pred_prices = [last_price]
        momentum = mean_return
        for i in range(5):
            shock = random.gauss(0, std_return)
            momentum = 0.7 * momentum + 0.3 * shock
            next_price = pred_prices[-1] * (1 + momentum)
            pred_prices.append(round(next_price, 8))

        # 預測標籤
        last_dt = datetime.datetime.fromtimestamp(klines[-1]["open_time"])
        pred_labels = []
        for i in range(1, 6):
            dt = last_dt + datetime.timedelta(minutes=i)
            pred_labels.append(dt.strftime("%H:%M"))

        pred_min = min(pred_prices)
        pred_max = max(pred_prices)
        padding = (pred_max - pred_min) * 0.2 if pred_max > pred_min else last_price * 0.001

        chart_config = {
            "type": "line",
            "data": {
                "labels": hist_labels + pred_labels,
                "datasets": [
                    {
                        "label": "歷史價格",
                        "data": closes_raw + [None] * 5,
                        "borderColor": "#0ecb81",
                        "backgroundColor": "rgba(14,203,129,0.1)",
                        "borderWidth": 2,
                        "pointRadius": 0,
                        "fill": False,
                        "spanGaps": False,
                    },
                    {
                        "label": "預測價格",
                        "data": [None] * len(closes_raw) + pred_prices,
                        "borderColor": "#f0b90b",
                        "backgroundColor": "rgba(240,185,11,0.15)",
                        "borderWidth": 2,
                        "borderDash": [5, 5],
                        "pointRadius": 3,
                        "pointBackgroundColor": "#f0b90b",
                        "fill": True,
                        "spanGaps": True,
                    },
                ],
            },
            "options": {
                "responsive": True,
                "plugins": {
                    "title": {
                        "display": True,
                        "text": f"{symbol_upper} 未來 5 分鐘價格預測",
                        "color": "#1a1a2e",
                        "font": {"size": 13},
                    },
                    "legend": {
                        "labels": {"color": "#6b7280", "font": {"size": 10}},
                    },
                },
                "scales": {
                    "x": {
                        "ticks": {"color": "#6b7280", "font": {"size": 9}, "maxTicksLimit": 10},
                        "grid": {"color": "rgba(0,0,0,0.06)"},
                    },
                    "y": {
                        "ticks": {"color": "#6b7280", "font": {"size": 9}},
                        "grid": {"color": "rgba(0,0,0,0.06)"},
                        "min": pred_min - padding,
                        "max": pred_max + padding,
                    },
                },
            },
        }

        return {"status": "success", "chart": chart_config}
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


class ChatMessage(BaseModel):
    message: str

@app.post("/api/chat")
def api_chat(chat_msg: ChatMessage):
    try:
        status = get_bot_status()
        if is_paper_trading():
            status["balance_quote"] = get_paper_balance()
            
        # Get current price and klines
        symbol = status.get("active_symbol", "BTCUSDT")
        try:
            price_data = get_price(symbol)
            status["current_price"] = price_data.get("price", "未知")
        except:
            status["current_price"] = "未知"
            
        try:
            klines = get_klines(symbol, "1m", limit=30)
            if klines:
                # Format: [Close, Volume] to save tokens
                kline_str = ", ".join([f"{k['close']}(vol:{int(k['volume'])})" for k in klines])
                status["klines"] = kline_str
                # 傳入陣列格式，方便 AI 畫圖用
                status["prices"] = [k["close"] for k in klines]
                status["klines_raw"] = klines
            else:
                status["klines"] = "無 K 線資料"
                status["prices"] = []
        except:
            status["klines"] = "獲取 K 線失敗"
            status["prices"] = []
        
        reply = f"收到您的訊息: {chat_msg.message}"
        return {"status": "success", "reply": reply}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

def _get_real_trades():
    import json
    from core.config import TRADE_HISTORY_FILE
    import datetime
    import pytz
    if not os.path.exists(TRADE_HISTORY_FILE):
        return []
    try:
        with open(TRADE_HISTORY_FILE, "r", encoding="utf-8") as f:
            history = json.load(f)
    except Exception:
        return []
    
    trades = []
    tz = pytz.timezone('Asia/Taipei')
    for t in history:
        try:
            timestamp_str = t.get("timestamp")
            if not timestamp_str:
                continue
            # trade_history.json 的 timestamp 是 record_trade_result() 用
            # time.strftime() 寫入的，這支伺服器系統時區是 UTC，所以存進去的其實
            # 是 UTC 時間字串。原本這裡直接 tz.localize(dt) 把它當成台北時間標記，
            # 等於把 UTC 的數字原封不動當台北時間顯示，導致歷史筆記本的時間、以及
            # 用來分天的日期都跟真正的台北時間差了 8 小時。改成先標記為 UTC 再轉換
            # 成台北時間，這樣後面算出來的 exit_time_ms 才是真正對應的時刻，任何
            # 用 tz=Asia/Taipei 顯示出來的時間才會是正確的台北時間。
            dt = datetime.datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
            dt = pytz.utc.localize(dt).astimezone(tz)
            exit_time_ms = int(dt.timestamp() * 1000)
            entry_time_ms = int(t.get("entry_timestamp_ms") or 0)
            if entry_time_ms <= 0:
                entry_time_ms = exit_time_ms - 600000  # 舊紀錄沒有開倉時間時才保留估算
            
            ae = float(t.get("actual_entry") or 0.0)
            ax = float(t.get("actual_exit") or 0.0)
            qty = float(t.get("qty") or 0.0)
            profit_pct = float(t.get("profit_pct") or 0.0)
            fees = float(t.get("fees") or 0.0)
            sym = str(t.get("symbol", "")).replace("USDT", ":USDT")
            
            # 判斷多空方向
            if profit_pct >= 0:
                is_long = (ax > ae)
            else:
                is_long = (ax < ae)
                
            pnl = (
                float(t["realized_pnl_usdt"])
                if t.get("realized_pnl_usdt") is not None
                else ((ax - ae) * qty if is_long else (ae - ax) * qty)
            )
            
            # 入場 trade 紀錄
            trades.append({
                "symbol": sym,
                "price": ae,
                "qty": qty,
                "time": entry_time_ms,
                "isBuyer": is_long,
                "is_close": False,
                "realized_pnl": 0.0,
                "fee": fees / 2.0
            })
            
            # 出場 trade 紀錄
            trades.append({
                "symbol": sym,
                "price": ax,
                "qty": qty,
                "time": exit_time_ms,
                "isBuyer": not is_long,
                "is_close": True,
                "realized_pnl": pnl,
                "fee": fees / 2.0
            })
        except Exception:
            continue
    return trades


@app.get("/api/history/summary")
def api_history_summary():
    try:
        if is_paper_trading():
            ps_path = os.path.join(os.path.dirname(__file__), "..", "data", "paper_state.json")
            if not os.path.exists(ps_path):
                return {"summaries": []}
            with open(ps_path, "r") as f:
                state = json.load(f)
            trades = state.get("trades", [])
        else:
            trades = _get_real_trades()

        tz = pytz.timezone('Asia/Taipei')
        daily = {}
        for t in trades:
            dt = datetime.datetime.fromtimestamp(t["time"] / 1000, tz=tz)
            date_key = dt.strftime("%Y-%m-%d")
            entry = daily.setdefault(date_key, {"trades": 0, "pnl": 0.0, "fee": 0.0})
            entry["trades"] += 1
            if t.get("is_close") and t.get("realized_pnl"):
                entry["pnl"] += t["realized_pnl"]

            fee = t.get("fee", (t.get("price", 0) * abs(t.get("qty", 0))) * 0.0005)
            entry["fee"] += fee

        summaries = [{"date": k, "trades": v["trades"], "fee": round(v["fee"], 4), "pnl": round(v["pnl"] - v["fee"], 4)} for k, v in sorted(daily.items(), reverse=True)]
        return {"summaries": summaries}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/history/download/{date}")
def api_history_download(date: str):
    try:
        if is_paper_trading():
            ps_path = os.path.join(os.path.dirname(__file__), "..", "data", "paper_state.json")
            if not os.path.exists(ps_path):
                raise HTTPException(status_code=404, detail="無交易紀錄")
            with open(ps_path, "r") as f:
                state = json.load(f)
            trades = state.get("trades", [])
        else:
            trades = _get_real_trades()

        tz = pytz.timezone('Asia/Taipei')
        filtered = [t for t in trades if datetime.datetime.fromtimestamp(t["time"] / 1000, tz=tz).strftime("%Y-%m-%d") == date]
        if not filtered:
            raise HTTPException(status_code=404, detail=f"日期 {date} 無交易紀錄")

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["時間", "幣種", "方向", "價格", "數量", "手續費", "已實現損益", "平倉"])
        for t in filtered:
            ts = datetime.datetime.fromtimestamp(t["time"] / 1000, tz=tz).strftime("%Y-%m-%d %H:%M:%S")
            side = "買入(多)" if t.get("isBuyer") and not t.get("is_close") else \
                   "賣出(平多)" if not t.get("isBuyer") and t.get("is_close") else \
                   "賣出(空)" if not t.get("isBuyer") and not t.get("is_close") else \
                   "買入(平空)"

            fee = t.get("fee", (t.get("price", 0) * abs(t.get("qty", 0))) * 0.0005)
            net_pnl = t.get("realized_pnl", 0) - fee

            writer.writerow([
                ts,
                t.get("symbol", "").replace(":USDT", ""),
                side,
                t.get("price", ""),
                t.get("qty", ""),
                round(fee, 6),
                round(net_pnl, 6),
                "是" if t.get("is_close") else "否"
            ])

        from fastapi.responses import StreamingResponse
        csv_content = "\ufeff" + output.getvalue()
        return StreamingResponse(
            iter([csv_content]),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename=trades_{date}.csv"}
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/history/delete/{date}")
def api_history_delete(date: str):
    try:
        if is_paper_trading():
            ps_path = os.path.join(os.path.dirname(__file__), "..", "data", "paper_state.json")
            if os.path.exists(ps_path):
                with open(ps_path, "r", encoding="utf-8") as f:
                    state = json.load(f)
                tz = pytz.timezone('Asia/Taipei')
                trades = state.get("trades", [])
                new_trades = []
                for t in trades:
                    t_date = datetime.datetime.fromtimestamp(t["time"] / 1000, tz=tz).strftime("%Y-%m-%d")
                    if t_date != date:
                        new_trades.append(t)
                state["trades"] = new_trades
                with open(ps_path, "w", encoding="utf-8") as f:
                    json.dump(state, f, indent=4)
        else:
            from core.config import TRADE_HISTORY_FILE
            if os.path.exists(TRADE_HISTORY_FILE):
                with open(TRADE_HISTORY_FILE, "r", encoding="utf-8") as f:
                    history = json.load(f)
                new_history = []
                tz = pytz.timezone('Asia/Taipei')
                for t in history:
                    timestamp_str = t.get("timestamp")
                    if timestamp_str:
                        # timestamp 存的是 UTC 時間字串（見 _get_real_trades 的說明），
                        # 直接切字串取日期會跟摘要/下載頁面顯示的台北日期對不起來，
                        # 一樣要先轉成台北時間才能正確判斷屬於哪一天。
                        try:
                            dt = datetime.datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
                            t_date = pytz.utc.localize(dt).astimezone(tz).strftime("%Y-%m-%d")
                        except Exception:
                            t_date = timestamp_str.split(" ")[0]
                        if t_date != date:
                            new_history.append(t)
                    else:
                        new_history.append(t)
                with open(TRADE_HISTORY_FILE, "w", encoding="utf-8") as f:
                    json.dump(new_history, f, indent=4)
        return {"status": "success", "detail": f"已成功刪除 {date} 的交易紀錄"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/history/add/{date}")
def api_history_add(date: str):
    try:
        try:
            datetime.datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=400, detail="日期格式錯誤，必須為 YYYY-MM-DD")

        if is_paper_trading():
            ps_path = os.path.join(os.path.dirname(__file__), "..", "data", "paper_state.json")
            if os.path.exists(ps_path):
                with open(ps_path, "r", encoding="utf-8") as f:
                    state = json.load(f)
                tz = pytz.timezone('Asia/Taipei')
                dt = datetime.datetime.strptime(date + " 12:00:00", "%Y-%m-%d %H:%M:%S")
                dt = tz.localize(dt)
                t_ms = int(dt.timestamp() * 1000)
                
                dummy_trade = {
                    "symbol": "DUMMY:USDT",
                    "price": 0.0,
                    "qty": 0.0,
                    "time": t_ms,
                    "isBuyer": True,
                    "realized_pnl": 0.0,
                    "fee": 0.0,
                    "is_close": True
                }
                state.setdefault("trades", []).append(dummy_trade)
                with open(ps_path, "w", encoding="utf-8") as f:
                    json.dump(state, f, indent=4)
        else:
            from core.config import TRADE_HISTORY_FILE
            if os.path.exists(TRADE_HISTORY_FILE):
                with open(TRADE_HISTORY_FILE, "r", encoding="utf-8") as f:
                    history = json.load(f)
                
                dummy_record = {
                    "timestamp": f"{date} 00:00:00",
                    "symbol": "DUMMYUSDT",
                    "entry_reason": "manual",
                    "exit_reason": "manual",
                    "profit_pct": 0.0,
                    "max_profit_reached": 0.0,
                    "atr_at_exit": 0.0,
                    "market_mode": "Neutral",
                    "expected_entry": 0.0,
                    "expected_exit": 0.0,
                    "actual_entry": 0.0,
                    "actual_exit": 0.0,
                    "fees": 0.0,
                    "qty": 0.0,
                    "slippage": 0.0,
                    "friction_rate": 0.0,
                    "theoretical_profit": 0.0,
                    "ai_summary": "手動新增空白日期。"
                }
                history.append(dummy_record)
                with open(TRADE_HISTORY_FILE, "w", encoding="utf-8") as f:
                    json.dump(history, f, indent=4)
        return {"status": "success", "detail": f"已成功新增 {date} 的空白歷史占位符"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/coin/{symbol}/toggle")
def api_toggle_coin(symbol: str):
    from services.bot_manager_service import toggle_coin_disabled
    return toggle_coin_disabled(symbol)


@app.get("/api/coin-profiles")
def api_get_coin_profiles():
    try:
        from core.config import COIN_PROFILE_CONFIG
        return COIN_PROFILE_CONFIG
    except Exception as e:
        return {"error": str(e)}


_open_orders_cache = {}


@app.get("/api/open-orders")
def get_open_orders(symbol: str):
    now = time.time()
    cached = _open_orders_cache.get(symbol)
    if cached and now - cached[0] < 10:
        return {"status": "success", "data": cached[1]}
    try:
        orders = client.futures_get_open_orders(symbol=symbol)
        algo_orders = client.futures_get_open_algo_orders(symbol=symbol)
        combined_orders = list(orders or []) + list(algo_orders or [])
        _open_orders_cache[symbol] = (now, combined_orders)
        return {"status": "success", "data": combined_orders}
    except Exception as e:
        if cached:
            return {"status": "success", "data": cached[1]}
        return {"status": "error", "message": str(e)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8005)

