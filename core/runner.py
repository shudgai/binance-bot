import asyncio
import inspect
import json
import os
import sys
import time
import traceback

import ccxt
import requests

from core import ctx
from core.config import (PAPER_TRADING, MAX_POSITIONS, MAIN_LOOP_INTERVAL_SEC)
from core.exchange_client import exchange_futures, check_binance_weight
from core.state_manager import build_symbol_state, update_states, reset_coin_state
from core.balance import fetch_real_balance
from core.market_data import (update_market_wind, initialize_atr_history, fetch_all_klines,
    fetch_all_sma200, fetch_all_ema50_1h, fetch_all_ema_15m, load_open_positions)
from core.symbol_profile import (filter_valid_symbols, apply_symbol_profile, SYMBOL_PROFILES,
    update_all_dynamic_personalities)
from core.trade_signal import update_trade_signal
from core.check_entries import compute_indicators, check_all_divergence_logic


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


async def watch_symbol_trades(exchange, sym):
    while True:
        try:
            trades = await exchange_futures.fetch_trades(sym, limit=50)
            if isinstance(trades, list):
                for trade in trades:
                    update_trade_signal(sym, trade)
            elif trades:
                update_trade_signal(sym, trades)
        except Exception as e:
            print(f"⚠️ [成交流監聽異常] {sym}: {e}")
        await asyncio.sleep(3)


async def ensure_watch_tasks(exchange):
    desired_symbols = set(ctx.ALL_SYMBOLS)
    current_symbols = set(ctx.WATCH_TASKS.keys())

    for sym in current_symbols - desired_symbols:
        task = ctx.WATCH_TASKS.pop(sym, None)
        if task is not None:
            task.cancel()

    for sym in desired_symbols - current_symbols:
        ctx.WATCH_TASKS[sym] = asyncio.create_task(watch_symbol_trades(exchange, sym))


async def market_wind_loop(exchange):
    while True:
        try:
            await update_market_wind(exchange)
        except Exception as e:
            print(f"⚠️ [大盤風向更新失敗] {e}")
        await asyncio.sleep(60)


async def handle_trading_error(sym):
    """
    處理交易邏輯中的異常：
    1. 增加錯誤計數
    2. 達到閾值時封鎖 (Ban)
    3. 標記為需要校準 (Sync)
    """
    s = ctx.STATES.get(sym)
    if not s:
        return

    s["error_strikes"] = s.get("error_strikes", 0) + 1
    print(f"⚠️ [ERROR_STRIKE] {sym} 發生第 {s['error_strikes']} 次異常")

    if s["error_strikes"] >= 3:
        s["is_banned"] = True
        print(f"🚫 [BANNED] {sym} 因連續報錯被封鎖，將停止監控。")

    s["sync_required"] = True
    reset_coin_state(sym)


async def safe_execute(func, sym, *args):
    """
    安全護盾：隔離單幣種錯誤，確保一個幣種崩潰不會影響全域
    """
    s = ctx.STATES.get(sym)
    if not s or s.get("is_banned"):
        return None

    try:
        if inspect.iscoroutinefunction(func):
            return await func(sym, *args)
        else:
            return func(sym, *args)
    except Exception as e:
        print(f"🚨 [SAFE_SHIELD] {sym} 發生異常在 {func.__name__}: {e}")
        await handle_trading_error(sym)
        return None


async def calibrate_with_exchange(exchange):
    """
    與交易所進行實際持倉校準。
    若偵測到本地數據與交易所數據不符，強制覆蓋為交易所數據。
    """
    if PAPER_TRADING:
        print("ℹ️ [CALIBRATION] 紙上交易模式，跳過交易所校準。")
        return

    try:
        positions = await exchange.fetch_positions()
        for pos in positions:
            raw_symbol = pos.get('symbol', '')
            sym = raw_symbol.split(':')[0].replace('/', '')

            real_qty = float(pos.get('contracts', 0.0) or pos.get('info', {}).get('positionAmt', 0.0))
            if abs(real_qty) > 0.000001:
                if sym not in ctx.ALL_SYMBOLS:
                    print(f"⚠️ [發現未監控持倉] 交易所內 {sym} 仍有實盤倉位，自動加回監控清單並在介面顯示！")
                    ctx.ALL_SYMBOLS.append(sym)
                    ctx.STATES[sym] = build_symbol_state(sym)
                    apply_symbol_profile(sym, SYMBOL_PROFILES.get(sym, {}))

            if sym in ctx.STATES:
                current_qty = ctx.STATES[sym].get("qty", 0.0)

                if abs(real_qty - current_qty) > (abs(current_qty) * 0.001) and abs(real_qty) > 0:
                    print(f"⚖️ [CALIBRATION] 校準 {sym}: 內部 {current_qty} -> 交易所 {real_qty}")
                    ctx.STATES[sym]["qty"] = real_qty
                    if current_qty == 0:
                        ctx.STATES[sym]["entry_price"] = float(pos.get('entryPrice', pos.get('avg_price', 0.0)))
                        ctx.STATES[sym]["avg_price"] = ctx.STATES[sym]["entry_price"]
                        print(f"✅ [CALIBRATION] 已恢復 {sym} 的持倉數據。")

    except Exception as e:
        print(f"⚠️ [CALIBRATION_FAIL] 無法連線交易所校準: {e}")


async def main_loop(exchange):
    from core.orders import check_stale_limit_orders, check_total_equity_protection, execute_panic_sell_all_positions
    from core.orders import check_paper_pending_order
    from core.exits import check_exits
    from core.check_entries import check_entries

    asyncio.create_task(market_wind_loop(exchange))
    """初始化後進入主交易循環"""

    try:
        await asyncio.wait_for(exchange_futures.load_markets(), timeout=15)
    except Exception as e:
        print(f"⚠️ load_markets 失敗 ({e})，使用預設市場清單")

    ctx.ALL_SYMBOLS = filter_valid_symbols(exchange, ctx.ALL_SYMBOLS)
    from core.symbol_profile import save_symbol_pool
    save_symbol_pool(ctx.ALL_SYMBOLS)

    print(f"📋 監控幣種: {', '.join(ctx.ALL_SYMBOLS)}")
    try:
        await asyncio.wait_for(initialize_atr_history(exchange), timeout=60)
    except (asyncio.TimeoutError, Exception) as e:
        print(f"⏳ [初始化] ATR 歷史預熱超時或失敗 ({e})，將在運行中慢慢加熱")

    print("🔍 [INIT] 正在啟動時校準倉位...")
    await calibrate_with_exchange(exchange)
    await fetch_real_balance()
    await load_open_positions()
    await fetch_all_sma200(exchange)
    await fetch_all_ema50_1h(exchange)
    await fetch_all_ema_15m(exchange)

    last_balance_update = time.time()

    while True:
        try:
            loop_start = time.time()
            if not PAPER_TRADING and loop_start - last_balance_update > 30:
                await fetch_real_balance()
                last_balance_update = loop_start

            open_syms = [sym for sym in ctx.ALL_SYMBOLS if abs(ctx.STATES[sym]["qty"]) > 0.000001]
            closed_syms = [sym for sym in ctx.ALL_SYMBOLS if abs(ctx.STATES[sym]["qty"]) <= 0.000001]
            ctx.ALL_SYMBOLS = closed_syms + open_syms

            # ====== 總資金水位審查 ======
            if not getattr(sys.modules[__name__], 'GLOBAL_MELTDOWN_COOLING', False):
                is_equity_safe = check_total_equity_protection()
                if not is_equity_safe:
                    await execute_panic_sell_all_positions()
                    print("🛑 [全局冷卻] 機器人進入 1 小時強制休眠，防禦連續虧損！")
                    setattr(sys.modules[__name__], 'GLOBAL_MELTDOWN_COOLING', True)
                    setattr(sys.modules[__name__], 'MELTDOWN_TIME', time.time())

            if getattr(sys.modules[__name__], 'GLOBAL_MELTDOWN_COOLING', False):
                if time.time() - getattr(sys.modules[__name__], 'MELTDOWN_TIME', 0) > 3600:
                    print("✅ [全局冷卻結束] 1小時防禦期滿，恢復正常運行。")
                    setattr(sys.modules[__name__], 'GLOBAL_MELTDOWN_COOLING', False)
                else:
                    await asyncio.sleep(60)
                    continue

            for sym in ctx.ALL_SYMBOLS:
                if ctx.STATES[sym].get("sync_required"):
                    print(f"🔄 [SYNC_REQUIRED] 正在重新校準 {sym}...")
                    await load_open_positions()
                    ctx.STATES[sym]["sync_required"] = False

            for sym in ctx.ALL_SYMBOLS:
                ctx.STATES[sym]["adjusted_this_tick"] = False

            print_multi_status()
            await fetch_all_klines(exchange)
            for sym in ctx.ALL_SYMBOLS:
                if ctx.STATES[sym].get("status") == "COOLDOWN":
                    if time.time() < ctx.STATES[sym].get("next_status_time", 0):
                        continue
                    else:
                        ctx.STATES[sym]["status"] = "ACTIVE"
                        print(f"✅ [冷卻結束] {sym} 恢復 ACTIVE 狀態")

                await safe_execute(compute_indicators, sym)

            # --- 背離自動掃描 ---
            if time.time() % 300 < MAIN_LOOP_INTERVAL_SEC:
                div_list = check_all_divergence_logic()
                for msg in div_list:
                    print(f"🌟 [自動背離掃描] {msg}")

            # --- 狀態更新區塊 ---
            try:
                update_states()
                update_all_dynamic_personalities()
            except Exception as e:
                print(f"⚠️ [狀態更新異常]: {e}")

            # --- AI 大腦診斷 ---
            try:
                from services.ai_manager import ai_engine
                if time.time() % 1800 < 6:
                    asyncio.create_task(ai_engine.run_ai_diagnosis_cycle())
            except ImportError:
                pass

            # --- 出場檢查區塊 (最關鍵的防禦) ---
            from core.strategy.factory import StrategyFactory
            for sym in ctx.ALL_SYMBOLS:
                if ctx.STATES[sym].get("status") != "ACTIVE":
                    continue
                if PAPER_TRADING:
                    await check_paper_pending_order(sym)
                strategy = StrategyFactory.create_strategy(sym)
                await safe_execute(strategy.check_exit, sym) # Actually safe_execute expects a function and sym. 

            # --- 進場檢查區塊 ---
            try:
                await check_entries() # Check entries evaluates all at once currently. Let's keep it global for ranking, or wrap it in a PortfolioManager later.
            except Exception as e:
                print(f"⚠️ [進場檢查異常]: {e}")
                traceback.print_exc()

            # 成功執行，重置連續錯誤計數器
            ctx.CONSECUTIVE_ERRORS = 0

            weight_sleep = check_binance_weight()

            elapsed = time.time() - loop_start
            sleep_time = max(1.5, MAIN_LOOP_INTERVAL_SEC - elapsed) + weight_sleep
            await asyncio.sleep(sleep_time)
        except ccxt.DDoSProtection as e:
            print(f"🚨 [API限流 429] 檢測到 DDoSProtection 限流，冷卻 10 秒: {e}")
            await asyncio.sleep(10)
        except ccxt.RateLimitExceeded as e:
            print(f"🚨 [API限流 429] 檢測到 RateLimitExceeded 限流，冷卻 10 秒: {e}")
            await asyncio.sleep(10)
        except Exception as e:
            if "429" in str(e):
                print(f"🚨 [API限流 429] 檢測到 429 錯誤，冷卻 10 秒: {e}")
                await asyncio.sleep(10)
                continue
            error_msg = f"發生未預期的錯誤：\n{str(e)}\n{traceback.format_exc()}"
            print(f"❌ [系統錯誤] {error_msg}")

            try:
                await load_open_positions()
                print("♻️ 已重新載入真實部位完成")
            except Exception as e2:
                print(f"⚠️ 重新載入部位失敗: {e2}")

            try:
                send_alert(error_msg)
            except NameError:
                pass

            ctx.CONSECUTIVE_ERRORS += 1
            if ctx.CONSECUTIVE_ERRORS >= 3:
                try:
                    send_alert("⚠️ [嚴重警告] 機器人連續報錯 3 次以上，請立即檢查系統狀態！")
                except NameError:
                    pass
                cooldown = min(120, 15 * (ctx.CONSECUTIVE_ERRORS - 2))
                print(f"🚨 [連續API錯誤風控] 已連續錯誤 {ctx.CONSECUTIVE_ERRORS} 次，觸發風控冷卻，暫停 {cooldown} 秒...")
                await asyncio.sleep(cooldown)
            else:
                await asyncio.sleep(5)


async def periodic_htf_update(exchange):
    while True:
        await asyncio.sleep(900)
        await fetch_all_sma200(exchange)
        await fetch_all_ema50_1h(exchange)
        await fetch_all_ema_15m(exchange)
        print("🔄 [HTF] 已更新所有幣種 15m SMA200 與 1H EMA50 以及 15m EMA20 & EMA50")


def print_multi_status():
    """
    優化後的狀態輸出：將進行中的持倉置頂，並增加視覺分隔。
    """
    from datetime import datetime
    now = datetime.now().strftime("%H:%M:%S")

    active_positions = []
    for sym, s in ctx.STATES.items():
        if abs(s.get('qty', 0)) > 0.000001:
            pnl = round(s.get('pnl_pct', 0.0), 2)
            direction = "多" if s.get('qty', 0) > 0 else "空"
            avg_price = s.get('avg_price', 0)
            active_positions.append(f"  🔥 持倉] {sym} | 方向:{direction} | 入場:{avg_price} | 獲利:{pnl}%")

    print(f"[{now}] [__multi__] 📊 [現況]")

    if active_positions:
        for pos in active_positions:
            print(pos)
    else:
        print("  ✨ 持倉] 目前無持倉")

    total_monitored = len(ctx.STATES)
    active_count = len(active_positions)
    cooldown_count = sum(1 for s in ctx.STATES.values() if s.get('status') == 'COOLDOWN')
    banned_count = sum(1 for s in ctx.STATES.values() if s.get('status') == 'BANNED')

    print(f"  📊 統計] 監控池={total_monitored} | 冷卻={cooldown_count} | 禁賽={banned_count} | 持倉數:{active_count}/{MAX_POSITIONS}")
    print("-" * 60)


async def periodic_status_log():
    while True:
        await asyncio.sleep(60)
        # 狀態列印已移至 main_loop 的 print_multi_status
        # 保留 periodic_status_log 來定時儲存快取
        try:
            cache_data = {}
            for sym in ctx.STATES:
                cache_data[sym] = ctx.STATES[sym]["atr_history"][-1000:]
            with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "atr_history_cache.json"), "w") as f:
                json.dump(cache_data, f)
        except Exception:
            pass


async def sync_paper_state():
    from services.utils import paper_key
    while True:
        await asyncio.sleep(1)
        if not PAPER_TRADING:
            continue
        try:
            with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "paper_state.json"), "r") as f:
                state = json.load(f)
            for sym in ctx.ALL_SYMBOLS:
                pk = paper_key(sym)
                pos = state.get("positions", {}).get(pk, {})
                qty = float(pos.get("qty", 0.0))
                ctx.STATES[sym]["qty"] = qty
                ctx.STATES[sym]["avg_price"] = float(pos.get("avg_price", 0.0))
        except:
            pass


async def main():
    from core.orders import check_stale_limit_orders
    asyncio.create_task(sync_paper_state())
    asyncio.create_task(periodic_htf_update(exchange_futures))
    asyncio.create_task(periodic_status_log())
    asyncio.create_task(check_stale_limit_orders())

    try:
        while True:
            try:
                await main_loop(exchange_futures)
            except Exception as e:
                print(f"🚨 [致命錯誤] main_loop 崩潰: {e}")
                traceback.print_exc()
                print("⏳ 將在 10 秒後由內部自動重啟主程序...")
                await asyncio.sleep(10)
    finally:
        # 在同一個 event loop 內關閉 ccxt 連線，避免跨 loop 的資源殘留
        try:
            await exchange_futures.close()
        except Exception:
            pass


def check_direction_safety(sym, side):
    s = ctx.STATES.get(sym, {})
    cp = s.get("close_price", 0.0)
    if cp <= 0 or len(s.get("ohlcv", [])) < 2:
        return True
    prev_close = s["ohlcv"][-2][4]
    ema50 = s.get("ema50", 0.0)
    if side == "buy" and cp <= prev_close and ema50 > 0 and cp < ema50:
        return False
    if side == "sell" and ema50 > 0 and cp > ema50:
        return False
    return True
