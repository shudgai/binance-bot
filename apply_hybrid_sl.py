import re

with open("multi_coin_bot.py", "r") as f:
    code = f.read()

# 1. Volume-linked anti-whipsaw
old_cooldown = """    if hold_sec < cooldown_limit:
        return"""
new_cooldown = """    if hold_sec < cooldown_limit:
        # 防插針量能檢查
        current_vol = s.get("current_vol", 0.0)
        vol_ma20 = s.get("vol_ma20", 1.0)
        vol_ratio = current_vol / vol_ma20 if vol_ma20 > 0 else 1.0
        
        if vol_ratio > 2.5:
            print(f"⚠️ [防插針豁免] {sym} 瞬時爆發量 (Ratio: {vol_ratio:.2f}x)，視為真崩盤，取消盲區保護！")
        else:
            return"""
code = code.replace(old_cooldown, new_cooldown)

# 2. Hard max qty check on ATR
old_entry_atr = 's["entry_atr"] = s.get("current_atr", 0.0)'
new_entry_atr = 's["entry_atr"] = max(s.get("current_atr", 0.0), fill_price * 0.005 if "fill_price" in locals() else price * 0.005)'
code = code.replace(old_entry_atr, new_entry_atr)

# 3. Hybrid Stop Loss in execute_order
old_exec = """            s["last_flip_time"] = now
        except Exception as e:
            print(f"🚨 [開倉錯誤] {sym}: {e}")"""
new_exec = """            s["last_flip_time"] = now
            
            # --- 混合停損: 交易所掛單 (Stop Market) ---
            try:
                stop_side = 'sell' if s["qty"] > 0 else 'buy'
                hard_sl_pct = s.get("hard_stop_loss_pct", 0.02)
                stop_price = s["avg_price"] * (1 - hard_sl_pct) if s["qty"] > 0 else s["avg_price"] * (1 + hard_sl_pct)
                prec = await get_contract_precision(sym)
                stop_price = round_step(stop_price, prec['tick_size'])
                
                if s.get("exchange_stop_order_id"):
                    try:
                        await exchange_futures.cancel_order(s["exchange_stop_order_id"], sym)
                    except Exception as ce:
                        print(f"⚠️ [取消舊止損單失敗] {sym}: {ce}")
                
                stop_order = await exchange_futures.create_order(
                    sym, type='STOP_MARKET', side=stop_side, amount=abs(s["qty"]),
                    params={'stopPrice': stop_price, 'reduceOnly': True}
                )
                s["exchange_stop_order_id"] = stop_order['id']
                print(f"🛡️ [交易所掛單] {sym} 成功掛出 Stop Market 止損單 @ {stop_price} (數量: {abs(s['qty'])})")
            except Exception as se:
                print(f"🚨 [交易所止損掛單失敗] {sym}: {se}")
            # ----------------------------------------
            
        except Exception as e:
            print(f"🚨 [開倉錯誤] {sym}: {e}")"""
code = code.replace(old_exec, new_exec)

# 4. Hybrid Stop Loss cancellation in close_position (Full Close)
old_full_close = """        mark_exit(sym, is_stop_loss=is_stop_loss, reason=full_reason)
        reset_coin_state(sym)"""
new_full_close = """        if s.get("exchange_stop_order_id") and not PAPER_TRADING:
            try:
                await exchange_futures.cancel_order(s["exchange_stop_order_id"], sym)
                print(f"✅ [止損單取消] {sym} 部位已全平，撤銷交易所止損單")
            except Exception as ce:
                print(f"⚠️ [取消止損單失敗] {sym}: {ce}")
                
        mark_exit(sym, is_stop_loss=is_stop_loss, reason=full_reason)
        reset_coin_state(sym)"""
code = code.replace(old_full_close, new_full_close)

# 5. Hybrid Stop Loss update in close_position (Partial Close)
old_partial_close = """        s["qty"] = round_step(raw_qty, prec['step_size'])
        print(f"✅ [部分平] {sym} 平{qty} 剩{abs(s['qty']):.4f} {full_reason}")"""
new_partial_close = """        s["qty"] = round_step(raw_qty, prec['step_size'])
        print(f"✅ [部分平] {sym} 平{qty} 剩{abs(s['qty']):.4f} {full_reason}")
        
        if s.get("exchange_stop_order_id") and not PAPER_TRADING:
            try:
                await exchange_futures.cancel_order(s["exchange_stop_order_id"], sym)
                stop_side = 'sell' if s["qty"] > 0 else 'buy'
                hard_sl_pct = s.get("hard_stop_loss_pct", 0.02)
                stop_price = s["avg_price"] * (1 - hard_sl_pct) if s["qty"] > 0 else s["avg_price"] * (1 + hard_sl_pct)
                stop_price = round_step(stop_price, prec['tick_size'])
                new_stop = await exchange_futures.create_order(
                    sym, type='STOP_MARKET', side=stop_side, amount=abs(s["qty"]),
                    params={'stopPrice': stop_price, 'reduceOnly': True}
                )
                s["exchange_stop_order_id"] = new_stop['id']
                print(f"🛡️ [止損單更新] {sym} 部分平倉後更新止損單 @ {stop_price} (數量: {abs(s['qty'])})")
            except Exception as ce:
                print(f"⚠️ [更新止損單失敗] {sym}: {ce}")"""
code = code.replace(old_partial_close, new_partial_close)


with open("multi_coin_bot.py", "w") as f:
    f.write(code)

print("Applied Hybrid SL")
