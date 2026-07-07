import logging
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
from core.config import (
    PAPER_TRADING, MAX_POSITIONS, MAIN_LOOP_INTERVAL_SEC,
    TRADE_POLL_INTERVAL_SEC, TRADE_POLL_LIMIT, API_RATE_LIMIT_COOLDOWN_SEC,
)
from core.exchange_client import exchange_futures, exchange_market_data, check_binance_weight
from core.state_manager import build_symbol_state, update_states, reset_coin_state
from core.balance import fetch_real_balance
from core.market_data import (update_market_wind, initialize_atr_history, fetch_all_klines,
    fetch_all_sma200, fetch_all_ema50_1h, fetch_all_ema_15m, load_open_positions)
from core.symbol_profile import (filter_valid_symbols, apply_symbol_profile, SYMBOL_PROFILES,
    update_all_dynamic_personalities)
from core.trade_signal import update_trade_signal
from core.check_entries import compute_indicators, check_all_divergence_logic

logger = logging.getLogger(__name__)


TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


def send_alert(message):
    """發送緊急告警到 Telegram"""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logger.info(f"⚠️ [通知失敗] 未設定 TELEGRAM_TOKEN 或 TELEGRAM_CHAT_ID，僅輸出到 Log: {message}")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": f"🚨 [機器人警報]\n{message}"}
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        logger.info(f"⚠️ [通知失敗] 無法發送 Telegram 訊息: {e}")


def activate_api_cooldown(seconds=API_RATE_LIMIT_COOLDOWN_SEC):
    ctx.api_cooldown_until = max(ctx.api_cooldown_until, time.time() + max(1.0, seconds))


async def wait_for_api_cooldown():
    remaining = ctx.api_cooldown_until - time.time()
    if remaining > 0:
        await asyncio.sleep(remaining)


async def watch_symbol_trades(exchange, sym, initial_delay=0.0):
    if initial_delay > 0:
        await asyncio.sleep(initial_delay)
    while True:
        try:
            await wait_for_api_cooldown()
            async with ctx.request_semaphore:
                trades = await exchange.fetch_trades(sym, limit=TRADE_POLL_LIMIT)
            if isinstance(trades, list):
                for trade in trades:
                    update_trade_signal(sym, trade)
            elif trades:
                update_trade_signal(sym, trades)
        except (ccxt.DDoSProtection, ccxt.RateLimitExceeded) as e:
            activate_api_cooldown()
            logger.info(f"🚨 [成交流限流] {sym} 暫停所有行情 REST 請求 {API_RATE_LIMIT_COOLDOWN_SEC:.0f} 秒: {e}")
        except Exception as e:
            if "429" in str(e) or "-1003" in str(e):
                activate_api_cooldown()
                logger.info(f"🚨 [成交流限流] {sym} 觸發全域冷卻: {e}")
            else:
                logger.info(f"⚠️ [成交流監聽異常] {sym}: {e}")
        await asyncio.sleep(TRADE_POLL_INTERVAL_SEC)


async def ensure_watch_tasks(exchange):
    desired_symbols = set(ctx.ALL_SYMBOLS)
    current_symbols = set(ctx.WATCH_TASKS.keys())

    for sym in current_symbols - desired_symbols:
        task = ctx.WATCH_TASKS.pop(sym, None)
        if task is not None:
            task.cancel()

    new_symbols = sorted(desired_symbols - current_symbols)
    for index, sym in enumerate(new_symbols):
        # 錯開首次請求，避免啟動瞬間所有幣種同時撞向 REST API。
        ctx.WATCH_TASKS[sym] = asyncio.create_task(
            watch_symbol_trades(exchange, sym, initial_delay=float(index))
        )


async def market_wind_loop(exchange):
    while True:
        try:
            await wait_for_api_cooldown()
            await update_market_wind(exchange)
        except Exception as e:
            logger.info(f"⚠️ [大盤風向更新失敗] {e}")
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
    logger.info(f"⚠️ [ERROR_STRIKE] {sym} 發生第 {s['error_strikes']} 次異常")

    if s["error_strikes"] >= 3:
        s["is_banned"] = True
        logger.info(f"🚫 [BANNED] {sym} 因連續報錯被封鎖，將停止監控。")

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
        logger.info(f"🚨 [SAFE_SHIELD] {sym} 發生異常在 {func.__name__}: {e}")
        await handle_trading_error(sym)
        return None


async def _record_external_position_close(exchange, sym, state):
    """Record a position closed outside the bot before local state is reset."""
    old_qty = float(state.get("qty", 0.0) or 0.0)
    avg_price = float(state.get("avg_price", 0.0) or 0.0)
    if abs(old_qty) <= 0.000001 or avg_price <= 0:
        return False

    try:
        trades = await exchange.fetch_my_trades(sym, limit=50)
    except Exception as exc:
        logger.info(f"⚠️ [ExternalClose] {sym} 無法取得手動平倉成交: {exc}")
        return False
    if not isinstance(trades, list):
        return False

    close_side = "sell" if old_qty > 0 else "buy"
    opened_ms = int(max(
        float(state.get("open_time", 0.0) or 0.0),
        float(state.get("last_entry_time", 0.0) or 0.0),
    ) * 1000)
    candidates = []
    for trade in trades:
        side = str(trade.get("side") or trade.get("info", {}).get("side") or "").lower()
        timestamp = int(trade.get("timestamp") or trade.get("info", {}).get("time") or 0)
        if side != close_side or (opened_ms and timestamp and timestamp < opened_ms - 120000):
            continue
        candidates.append(trade)
    if not candidates:
        logger.info(f"⚠️ [ExternalClose] {sym} 找不到對應的手動平倉成交，僅清理本地狀態")
        return False

    latest = max(candidates, key=lambda item: int(item.get("timestamp") or item.get("info", {}).get("time") or 0))
    order_id = latest.get("order") or latest.get("info", {}).get("orderId") or latest.get("id")
    fills = [
        trade for trade in candidates
        if (trade.get("order") or trade.get("info", {}).get("orderId") or trade.get("id")) == order_id
    ]
    close_qty = sum(abs(float(t.get("amount") or t.get("info", {}).get("qty") or 0.0)) for t in fills)
    if close_qty <= 0:
        close_qty = abs(old_qty)
    notional = sum(
        abs(float(t.get("amount") or t.get("info", {}).get("qty") or 0.0))
        * float(t.get("price") or t.get("info", {}).get("price") or 0.0)
        for t in fills
    )
    exit_price = notional / close_qty if notional > 0 and close_qty > 0 else float(latest.get("price") or 0.0)
    realized_pnl = sum(float(t.get("info", {}).get("realizedPnl") or t.get("realizedPnl") or 0.0) for t in fills)
    fees = sum(float((t.get("fee") or {}).get("cost") or t.get("info", {}).get("commission") or 0.0) for t in fills)
    close_time = max(int(t.get("timestamp") or t.get("info", {}).get("time") or 0) for t in fills)
    history_id = f"{sym}:{order_id}"
    position_value = avg_price * abs(old_qty)
    profit_pct = realized_pnl / position_value if position_value > 0 else 0.0

    from core.orders import record_trade_result
    recorded = record_trade_result(
        symbol=sym,
        entry_reason=state.get("entry_reason", "UNKNOWN"),
        exit_reason="[External_Manual_Close]",
        profit_pct=profit_pct,
        current_atr=state.get("current_atr", 0.0),
        max_profit_reached=state.get("highest_profit_pct", 0.0),
        expected_entry=avg_price,
        expected_exit=exit_price,
        actual_entry=avg_price,
        actual_exit=exit_price,
        fees=fees,
        qty=abs(old_qty),
        exchange_close_id=history_id,
        realized_pnl_usdt=realized_pnl,
        timestamp_ms=close_time,
        entry_timestamp_ms=opened_ms if opened_ms else None,
    )
    if recorded:
        logger.info(f"🧾 [ExternalClose] {sym} 已同步手動平倉：損益 {realized_pnl:.4f} USDT，手續費 {fees:.4f} USDT")
    return bool(recorded)


async def calibrate_with_exchange(exchange):
    """
    與交易所進行實際持倉校準。
    若偵測到本地數據與交易所數據不符，強制覆蓋為交易所數據。
    """
    if PAPER_TRADING:
        logger.info("ℹ️ [CALIBRATION] 紙上交易模式，跳過交易所校準。")
        return

    try:
        positions = await exchange.fetch_positions()
        live_position_symbols = set()
        for pos in positions:
            raw_symbol = pos.get('symbol', '')
            sym = raw_symbol.split(':')[0].replace('/', '')

            # ccxt 統一格式的 contracts 欄位永遠是正數（不含方向），只有交易所原始的
            # info.positionAmt 才會正確帶正負號（空單是負的）。原本寫成
            # `contracts or info.positionAmt`，contracts 只要不是 0 就一定被優先選中，
            # 導致空單的真實持倉方向被校準成多單，內部損益判斷完全顛倒。改成優先讀取
            # 帶正負號的 positionAmt，讀不到才退回沒有方向資訊的 contracts。
            raw_amt = pos.get('info', {}).get('positionAmt')
            real_qty = float(raw_amt) if raw_amt is not None else float(pos.get('contracts', 0.0) or 0.0)
            if abs(real_qty) > 0.000001:
                live_position_symbols.add(sym)
                if sym not in ctx.ALL_SYMBOLS:
                    logger.info(f"⚠️ [發現未監控持倉] 交易所內 {sym} 仍有實盤倉位，自動加回監控清單並在介面顯示！")
                    ctx.ALL_SYMBOLS.append(sym)
                    ctx.STATES[sym] = build_symbol_state(sym)
                    apply_symbol_profile(sym, SYMBOL_PROFILES.get(sym, {}))
                    # 這裡只更新了 main.py 這個進程自己記憶體裡的 ALL_SYMBOLS，
                    # 但網頁「監控幣種」清單是 API 那個獨立進程從 bot_symbols.json
                    # 讀出來的，兩個進程不共用記憶體——不寫回檔案，介面永遠看不到
                    # 這個剛救回來的幣種，即使 log 訊息說「並在介面顯示」也不成立。
                    try:
                        from core.symbol_profile import save_symbol_pool
                        save_symbol_pool(ctx.ALL_SYMBOLS)
                    except Exception as se:
                        logger.info(f"⚠️ [持倉救回寫檔失敗] {sym}: {se}")

            if sym in ctx.STATES:
                current_qty = ctx.STATES[sym].get("qty", 0.0)

                if abs(real_qty - current_qty) > (abs(current_qty) * 0.001) and abs(real_qty) > 0:
                    logger.info(f"⚖️ [CALIBRATION] 校準 {sym}: 內部 {current_qty} -> 交易所 {real_qty}")
                    ctx.STATES[sym]["qty"] = real_qty
                    if current_qty == 0:
                        ctx.STATES[sym]["entry_price"] = float(pos.get('entryPrice', pos.get('avg_price', 0.0)))
                        ctx.STATES[sym]["avg_price"] = ctx.STATES[sym]["entry_price"]
                        # 恢復 open_time
                        ctx.STATES[sym]["open_time"] = time.time()
                        if ctx.STATES[sym].get("entry_count", 0) == 0:
                            ctx.STATES[sym]["entry_count"] = 1


                        
                        # ── 重啟峰值保護 ──
                        # 讀取當前交易所的未實現損益，用來預設最高獲利率最高點。
                        # 避免重啟後最高獲利峰值歸零，導致回吐時無法平倉的漏洞。
                        try:
                            _raw_pnl = float(pos.get('unRealizedProfit') or pos.get('info', {}).get('unRealizedProfit', 0.0))
                            _entry_val = abs(real_qty) * ctx.STATES[sym]["entry_price"]
                            if _entry_val > 0:
                                # 計算當前無槓桿的實際利潤率
                                _cur_pct = _raw_pnl / _entry_val
                                ctx.STATES[sym]["highest_profit_pct"] = max(0.0, _cur_pct)
                                logger.info(f"💾 [重啟峰值保護] {sym} 已根據當前未實現損益還原最高獲利峰值: {max(0.0, _cur_pct)*100:.3f}%")
                        except Exception as e_pnl:
                            logger.info(f"⚠️ [重啟峰值保護] {sym} 還原盈虧峰值失敗: {e_pnl}")

                        logger.info(f"✅ [CALIBRATION] 已恢復 {sym} 的持倉數據。")


        from core.orders import _ensure_exchange_exit_orders, _cancel_exchange_exit_order_id
        for sym in live_position_symbols:
            if sym not in ctx.STATES:
                continue
            try:
                await _ensure_exchange_exit_orders(sym)
            except Exception as exit_order_error:
                logger.info(f"🚨 [CALIBRATION] {sym} 交易所退出單修復失敗: {exit_order_error}")



        for sym, state in list(ctx.STATES.items()):
            if abs(state.get("qty", 0.0)) <= 0.000001 or sym in live_position_symbols:
                continue
            await _record_external_position_close(exchange, sym, state)
            logger.info(f"🔄 [CALIBRATION] {sym} 本地仍有持倉 {state.get('qty', 0.0):.4f}，但交易所已無倉位；清理本地狀態與交易所退出單追蹤")
            for key, label in (("exchange_stop_order_id", "止損"), ("exchange_take_profit_order_id", "停利")):
                order_id = state.get(key)
                if not order_id:
                    continue
                try:
                    await _cancel_exchange_exit_order_id(sym, order_id, f"校準殘留{label}")
                except Exception as ce:
                    logger.info(f"⚠️ [CALIBRATION] 撤銷 {sym} 殘留交易所{label}單失敗: {ce}")
                finally:
                    state[key] = None
            reset_coin_state(sym)

    except Exception as e:
        logger.info(f"⚠️ [CALIBRATION_FAIL] 無法連線交易所校準: {e}")


async def main_loop(exchange):
    from core.orders import check_stale_limit_orders, check_total_equity_protection, execute_panic_sell_all_positions
    from core.orders import check_paper_pending_order
    from core.exits import check_exits
    from core.check_entries import check_entries

    asyncio.create_task(market_wind_loop(exchange_market_data))
    """初始化後進入主交易循環"""

    try:
        await asyncio.wait_for(exchange_futures.load_markets(), timeout=15)
        if exchange_market_data is not exchange_futures:
            await asyncio.wait_for(exchange_market_data.load_markets(), timeout=15)
    except Exception as e:
        logger.info(f"⚠️ load_markets 失敗 ({e})，使用預設市場清單")

    ctx.ALL_SYMBOLS = filter_valid_symbols(exchange, ctx.ALL_SYMBOLS)
    from core.symbol_profile import save_symbol_pool
    save_symbol_pool(ctx.ALL_SYMBOLS)

    logger.info(f"📋 監控幣種: {', '.join(ctx.ALL_SYMBOLS)}")
    try:
        await asyncio.wait_for(initialize_atr_history(exchange_market_data), timeout=60)
    except (asyncio.TimeoutError, Exception) as e:
        logger.info(f"⏳ [初始化] ATR 歷史預熱超時或失敗 ({e})，將在運行中慢慢加熱")

    try:
        from core.check_entries import load_pending_signals
        load_pending_signals()
    except Exception as e:
        logger.info(f"⚠️ [Pending快取] 還原失敗: {e}")

    logger.info("🔍 [INIT] 正在啟動時校準倉位...")
    await calibrate_with_exchange(exchange)
    await fetch_real_balance()
    await load_open_positions()
    await fetch_all_sma200(exchange_market_data)
    await fetch_all_ema50_1h(exchange_market_data)
    await fetch_all_ema_15m(exchange_market_data)

    last_balance_update = time.time()

    while True:
        try:
            loop_start = time.time()
            await ensure_watch_tasks(exchange_market_data)
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
                    logger.info("🛑 [全局冷卻] 機器人進入 1 小時強制休眠，防禦連續虧損！")
                    setattr(sys.modules[__name__], 'GLOBAL_MELTDOWN_COOLING', True)
                    setattr(sys.modules[__name__], 'MELTDOWN_TIME', time.time())

            if getattr(sys.modules[__name__], 'GLOBAL_MELTDOWN_COOLING', False):
                if time.time() - getattr(sys.modules[__name__], 'MELTDOWN_TIME', 0) > 3600:
                    logger.info("✅ [全局冷卻結束] 1小時防禦期滿，恢復正常運行。")
                    setattr(sys.modules[__name__], 'GLOBAL_MELTDOWN_COOLING', False)
                else:
                    await asyncio.sleep(60)
                    continue

            for sym in ctx.ALL_SYMBOLS:
                if ctx.STATES[sym].get("sync_required"):
                    logger.info(f"🔄 [SYNC_REQUIRED] 正在重新校準 {sym}...")
                    await load_open_positions()
                    ctx.STATES[sym]["sync_required"] = False

            for sym in ctx.ALL_SYMBOLS:
                ctx.STATES[sym]["adjusted_this_tick"] = False

            print_multi_status()
            await fetch_all_klines(exchange_market_data)
            for sym in ctx.ALL_SYMBOLS:
                if ctx.STATES[sym].get("status") == "COOLDOWN":
                    if time.time() < ctx.STATES[sym].get("next_status_time", 0):
                        continue
                    else:
                        ctx.STATES[sym]["status"] = "ACTIVE"
                        logger.info(f"✅ [冷卻結束] {sym} 恢復 ACTIVE 狀態")

                await safe_execute(compute_indicators, sym)

            # --- 背離自動掃描 ---
            if time.time() % 300 < MAIN_LOOP_INTERVAL_SEC:
                div_list = check_all_divergence_logic()
                for msg in div_list:
                    logger.info(f"🌟 [自動背離掃描] {msg}")

            # --- 狀態更新區塊 ---
            try:
                update_states()
                update_all_dynamic_personalities()
            except Exception as e:
                logger.info(f"⚠️ [狀態更新異常]: {e}")

            # --- AI 大腦診斷（已停用）---
            # 停用原因：1) 沒有設定 OPENAI_API_KEY，每次呼叫都收到 401 静默失敗，
            # 完全沒有實際作用；2) 就算補上 key，這是全自動套用（信心分數過門檻就
            # 直接寫入 bot_symbols.json 生效），跟目前每次調整風控參數都要先分析
            # 數據、跟使用者確認過的做法互相矛盾，背景自動改參數的風險比效益大。
            # try:
            #     from services.ai_manager import ai_engine
            #     if time.time() % 1800 < 6:
            #         asyncio.create_task(ai_engine.run_ai_diagnosis_cycle())
            # except ImportError:
            #     pass

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
                logger.info(f"⚠️ [進場檢查異常]: {e}")
                traceback.print_exc()

            # 成功執行，重置連續錯誤計數器
            ctx.CONSECUTIVE_ERRORS = 0

            weight_sleep = check_binance_weight()

            elapsed = time.time() - loop_start
            sleep_time = max(1.5, MAIN_LOOP_INTERVAL_SEC - elapsed) + weight_sleep

            # ── 持倉間歇快速出場檢查 ──
            # 主迴圈 25s 一輪，但 1-秒內的利潤高點根本看不到
            # 有持倉時：每 5s 抓一次 ticker 最新價 + 跑出場判斷，縮短反應窗口
            _mini_iv = 5.0
            _remaining = sleep_time
            from core.strategy.factory import StrategyFactory
            while _remaining > _mini_iv:
                if ctx.api_cooldown_until > time.time():
                    await wait_for_api_cooldown()
                    _remaining = 0
                    break
                await asyncio.sleep(_mini_iv)
                _remaining -= _mini_iv
                _open_syms = [s for s in ctx.ALL_SYMBOLS
                              if abs(ctx.STATES[s].get("qty", 0)) > 0.000001]
                if not _open_syms:
                    continue
                try:
                    for _sym in _open_syms:
                        try:
                            _tk = await exchange_market_data.fetch_ticker(_sym)
                            if _tk and _tk.get("last"):
                                ctx.STATES[_sym]["close_price"] = float(_tk["last"])
                        except Exception:
                            pass
                    for _sym in _open_syms:
                        if ctx.STATES[_sym].get("status") == "ACTIVE":
                            _strat = StrategyFactory.create_strategy(_sym)
                            await safe_execute(_strat.check_exit, _sym)
                except Exception as _e:
                    logger.debug(f"[快速出場] 例外: {_e}")
            if _remaining > 0:
                await asyncio.sleep(_remaining)
        except ccxt.DDoSProtection as e:
            activate_api_cooldown()
            logger.info(f"🚨 [API限流 429] DDoSProtection，啟動全域冷卻 {API_RATE_LIMIT_COOLDOWN_SEC:.0f} 秒: {e}")
            await wait_for_api_cooldown()
        except ccxt.RateLimitExceeded as e:
            activate_api_cooldown()
            logger.info(f"🚨 [API限流 429] RateLimitExceeded，啟動全域冷卻 {API_RATE_LIMIT_COOLDOWN_SEC:.0f} 秒: {e}")
            await wait_for_api_cooldown()
        except Exception as e:
            if "429" in str(e) or "-1003" in str(e):
                activate_api_cooldown()
                logger.info(f"🚨 [API限流] 啟動全域冷卻 {API_RATE_LIMIT_COOLDOWN_SEC:.0f} 秒: {e}")
                await wait_for_api_cooldown()
                continue
            error_msg = f"發生未預期的錯誤：\n{str(e)}\n{traceback.format_exc()}"
            logger.info(f"❌ [系統錯誤] {error_msg}")

            try:
                await load_open_positions()
                logger.info("♻️ 已重新載入真實部位完成")
            except Exception as e2:
                logger.info(f"⚠️ 重新載入部位失敗: {e2}")

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
                logger.info(f"🚨 [連續API錯誤風控] 已連續錯誤 {ctx.CONSECUTIVE_ERRORS} 次，觸發風控冷卻，暫停 {cooldown} 秒...")
                await asyncio.sleep(cooldown)
            else:
                await asyncio.sleep(5)


async def periodic_htf_update(exchange):
    while True:
        await asyncio.sleep(900)
        await wait_for_api_cooldown()
        await fetch_all_sma200(exchange)
        await fetch_all_ema50_1h(exchange)
        await fetch_all_ema_15m(exchange)
        logger.info("🔄 [HTF] 已更新所有幣種 15m SMA200 與 1H EMA50 以及 15m EMA20 & EMA50")


async def periodic_momentum_swap():
    """
    每 5 分鐘掃描目前監控幣種的即時動能（ATR% 與 1h 波動度）。
    若某幣無持倉且動能不足，自動呼叫 replace_dead_coin() 換成池中動能最強的替補。
    這樣確保幣種池隨時保持活躍，不讓「死水幣」佔住槽位卻毫無進場機會。
    """
    # 首次執行稍微延遲，等主迴圈完成初始化
    await asyncio.sleep(60)
    while True:
        try:
            from services.radar_service import check_momentum_and_swap, FOLLOW_SYMBOLS_FROM
            # 跟隨模式不自行換幣（換幣權交給來源部署）
            if not FOLLOW_SYMBOLS_FROM:
                await asyncio.get_event_loop().run_in_executor(None, check_momentum_and_swap)
        except Exception as e:
            logger.info(f"⚠️ [動能自動換幣] 執行失敗: {e}")
        await asyncio.sleep(300)  # 每 5 分鐘檢查一次


def print_multi_status():
    """
    優化後的狀態輸出：將進行中的持倉置頂，並增加視覺分隔。
    """
    from datetime import datetime
    now = datetime.now().strftime("%H:%M:%S")

    active_positions = []
    for sym, s in ctx.STATES.items():
        qty = s.get('qty', 0)
        if abs(qty) > 0.000001:
            avg_price = s.get('avg_price', 0)
            close_price = s.get('close_price', 0) or avg_price  # 重啟後 close_price 還沒抓到報價前，先當作 0 損益，避免顯示假的 100%
            direction = "多" if qty > 0 else "空"
            if avg_price > 0:
                pnl_val = (close_price - avg_price) / avg_price if qty > 0 else (avg_price - close_price) / avg_price
                pnl = round(pnl_val * 100, 2)
            else:
                pnl = 0.0
            active_positions.append(f"  🔥 [持倉] {sym} | 方向:{direction} | 入場:{avg_price} | 獲利:{pnl}%")

    logger.info(f"[{now}] [__multi__] 📊 [現況]")

    if active_positions:
        for pos in active_positions:
            logger.info(pos)
    else:
        logger.info("  ✨ [持倉] 目前無持倉")

    total_monitored = len(ctx.STATES)
    active_count = len(active_positions)
    cooldown_count = sum(1 for s in ctx.STATES.values() if s.get('status') == 'COOLDOWN')
    banned_count = sum(1 for s in ctx.STATES.values() if s.get('status') == 'BANNED')

    logger.info(f"  📊 [統計] 監控池={total_monitored} | 冷卻={cooldown_count} | 禁賽={banned_count} | 持倉數:{active_count}/{MAX_POSITIONS}")
    logger.info("-" * 60)


async def periodic_status_log():
    _data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
    while True:
        await asyncio.sleep(60)
        try:
            cache_data = {}
            for sym in ctx.STATES:
                cache_data[sym] = ctx.STATES[sym]["atr_history"][-1000:]
            with open(os.path.join(_data_dir, "atr_history_cache.json"), "w") as f:
                json.dump(cache_data, f)
        except Exception:
            pass


async def push_paper_live_state():
    """每 2 秒即時更新 paper_state.json 的未實現損益與現價"""
    from services.utils import paper_key
    from services.update_paper_state import mutate_paper_state
    while True:
        await asyncio.sleep(2)
        if not PAPER_TRADING:
            continue
        try:
            def _mutate(state):
                total_unrealized = 0.0
                positions = state.get("positions", {})
                for sym in ctx.ALL_SYMBOLS:
                    pk = paper_key(sym)
                    s = ctx.STATES.get(sym, {})
                    pos = positions.get(pk, {})
                    qty = float(pos.get("qty", 0.0))
                    avg = float(pos.get("avg_price", 0.0))
                    cur = float(s.get("close_price", 0.0))
                    if abs(qty) > 0.000001 and avg > 0 and cur > 0:
                        upnl = (cur - avg) * abs(qty) if qty > 0 else (avg - cur) * abs(qty)
                        pos["unrealized_pnl"] = round(upnl, 6)
                        pos["current_price"] = cur
                        total_unrealized += upnl
                    else:
                        pos.pop("unrealized_pnl", None)
                        pos.pop("current_price", None)
                state["total_unrealized_pnl"] = round(total_unrealized, 6)
                state["last_updated"] = int(time.time())

            mutate_paper_state(_mutate)
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
                if abs(qty) > 0.000001:
                    entries = pos.get("entries", [])
                    if entries:
                        ctx.STATES[sym]["open_time"] = float(entries[0].get("time", time.time() * 1000)) / 1000.0
                    elif ctx.STATES[sym].get("open_time", 0) <= 0:
                        ctx.STATES[sym]["open_time"] = time.time()
                else:
                    ctx.STATES[sym]["open_time"] = 0.0
        except:
            pass


async def main():
    from core.orders import check_stale_limit_orders
    asyncio.create_task(sync_paper_state())
    asyncio.create_task(push_paper_live_state())
    asyncio.create_task(periodic_htf_update(exchange_market_data))
    asyncio.create_task(periodic_status_log())
    asyncio.create_task(check_stale_limit_orders())
    asyncio.create_task(periodic_momentum_swap())  # 每 5 分鐘自動偵測並汰換動能不足幣種

    try:
        while True:
            try:
                await main_loop(exchange_futures)
            except Exception as e:
                logger.info(f"🚨 [致命錯誤] main_loop 崩潰: {e}")
                traceback.print_exc()
                logger.info("⏳ 將在 10 秒後由內部自動重啟主程序...")
                await asyncio.sleep(10)
    finally:
        # 在同一個 event loop 內關閉 ccxt 連線，避免跨 loop 的資源殘留
        try:
            await exchange_futures.close()
        except Exception:
            pass
        if exchange_market_data is not exchange_futures:
            try:
                await exchange_market_data.close()
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
