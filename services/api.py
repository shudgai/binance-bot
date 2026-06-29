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
from services.radar_service import trigger_manual_radar, auto_radar_switch, CORE_SYMBOLS, RADAR_SELECT_COUNT

load_dotenv()

app = FastAPI(title="Binance Bot API Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

def is_paper_trading():
    return not api_key or api_key == "your_api_key_here"

def daily_market_clean_and_reset(is_manual=False):
    """大掃除與即時同步前五名 (模組化)"""
    try:
        trigger_type = "手動強制" if is_manual else "每日/開機"
        add_system_log(f"🔄 [{trigger_type}換班] 啟動排程換班機制！", "warning")
        kill_bot()
        if is_manual:
            force_close_all_positions()
            add_system_log(f"🧹 [{trigger_type}淨空] 系統狀態已重置，舊訂單已撤銷並強制平倉。", "success")
        else:
            add_system_log(f"🧹 [{trigger_type}換班] 已保留現有持倉部位，將由機器人繼續監控至正常出場。", "success")
        clear_system_logs()
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

@app.on_event("startup")
async def startup_event():
    # 啟動 6:00 AM 定時器
    threading.Thread(target=daily_reset_daemon, daemon=True).start()
    # 後端重啟後自動恢復機器人
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
        scan_pool = [s for s in CORE_SYMBOLS if s not in BLACKLIST]
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
            return {} # For real trading, not implemented yet
    except Exception as e:
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
            return get_position(symbol_upper, quote_asset, base_asset)
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
            return get_trades(symbol_upper)
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

@app.get("/api/history/summary")
def api_history_summary():
    try:
        ps_path = os.path.join(os.path.dirname(__file__), "..", "data", "paper_state.json")
        if not os.path.exists(ps_path):
            return {"summaries": []}
        tz = pytz.timezone('Asia/Taipei')
        with open(ps_path, "r") as f:
            state = json.load(f)
        trades = state.get("trades", [])
        daily = {}
        for t in trades:
            dt = datetime.datetime.fromtimestamp(t["time"] / 1000, tz=tz)
            date_key = dt.strftime("%Y-%m-%d")
            entry = daily.setdefault(date_key, {"trades": 0, "pnl": 0.0, "fee": 0.0})
            entry["trades"] += 1
            if t.get("is_close") and t.get("realized_pnl"):
                entry["pnl"] += t["realized_pnl"]
            
            # 手續費加總 (支援相容舊紀錄)
            fee = t.get("fee", (t["price"] * abs(t["qty"])) * 0.0005)
            entry["fee"] += fee

        # 將 fee 也回傳，並將 pnl 扣除 fee
        summaries = [{"date": k, "trades": v["trades"], "fee": round(v["fee"], 4), "pnl": round(v["pnl"] - v["fee"], 4)} for k, v in sorted(daily.items(), reverse=True)]
        return {"summaries": summaries}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/history/download/{date}")
def api_history_download(date: str):
    try:
        ps_path = os.path.join(os.path.dirname(__file__), "..", "data", "paper_state.json")
        if not os.path.exists(ps_path):
            raise HTTPException(status_code=404, detail="無交易紀錄")
        with open(ps_path, "r") as f:
            state = json.load(f)
        trades = state.get("trades", [])
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
            # 將單筆的 realized_pnl 扣除手續費，確保整欄加總等於總淨利潤
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
        # Add UTF-8 BOM (\ufeff) so Excel correctly recognizes the encoding for Chinese characters
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

@app.post("/api/coin/{symbol}/toggle")
def api_toggle_coin(symbol: str):
    from services.bot_manager_service import toggle_coin_disabled
    return toggle_coin_disabled(symbol)


@app.get("/api/coin-profiles")
def api_get_coin_profiles():
    try:
        import ast, re
        src_path = os.path.join(os.path.dirname(__file__), "core", "config.py")
        with open(src_path, "r", encoding="utf-8") as f:
            src = f.read()
        # 找 COIN_PROFILE_CONFIG = { ... \n} 區塊，支援尾隨逗號
        m = re.search(r'COIN_PROFILE_CONFIG\s*=\s*\{(.*?)\n\}', src, re.DOTALL)
        if m:
            # 用 ast.literal_eval 解析，補上括號
            config = ast.literal_eval('{' + m.group(1) + '}')
            return config
        return {}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/open-orders")
def get_open_orders(symbol: str):
    try:
        orders = client.futures_get_open_orders(symbol=symbol)
        return {"status": "success", "data": orders}
    except Exception as e:
        return {"status": "error", "message": str(e)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8005)

