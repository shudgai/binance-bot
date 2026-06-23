import re

with open("multi_coin_bot.py", "r") as f:
    content = f.read()

# Add the panic sell function
panic_code = """
async def execute_panic_sell_all_positions():
    print("🚨🚨 [緊急清倉] 開始強制市價平掉所有倉位！")
    for sym in ALL_SYMBOLS:
        s = STATES[sym]
        if abs(s["qty"]) > 0.000001:
            is_long = s["qty"] > 0
            cs = 'sell' if is_long else 'buy'
            p = s.get("close_price", s["avg_price"])
            print(f"🚨 [緊急清倉] 正在平倉 {sym}...")
            try:
                await close_position(sym, cs, abs(s["qty"]), p, s["avg_price"], reason="[GLOBAL_MELTDOWN]", is_stop_loss=True)
            except Exception as e:
                print(f"⚠️ [緊急清倉失敗] {sym}: {e}")

def get_total_wallet_balance():
    if PAPER_TRADING:
        # Paper trading assumption: basic capital + sum of all PnL
        try:
            with open(PAPER_STATE_FILE, 'r') as f:
                st = json.load(f)
                realized = sum(v.get('realized_pnl', 0.0) for v in st.get('positions', {}).values())
                return 1500.0 + realized # Assuming 1500 base paper capital
        except:
            return 1500.0
    else:
        # Live balance is not tracked locally as a single float reliably in states, 
        # but we can fallback to a fixed estimation or fetch it.
        # Assuming we have REAL_WALLET_BALANCE if we fetched it, or we use a fixed 1500
        return 1500.0 # Modify as per actual logic if available

def check_total_equity_protection():
    total_unrealized_pnl = 0.0
    has_positions = False
    
    for sym in ALL_SYMBOLS:
        s = STATES[sym]
        qty = s.get("qty", 0.0)
        if abs(qty) > 0.000001:
            has_positions = True
            p = s.get("close_price", s.get("avg_price", 0.0))
            avg = s.get("avg_price", 0.0)
            if qty > 0:
                pnl = (p - avg) * abs(qty)
            else:
                pnl = (avg - p) * abs(qty)
            total_unrealized_pnl += pnl

    if not has_positions:
        return True

    total_balance = get_total_wallet_balance()
    if total_balance <= 0:
        return True
        
    loss_percentage = (total_unrealized_pnl / total_balance) * 100
    GLOBAL_LOSS_THRESHOLD = -4.0 

    if loss_percentage <= GLOBAL_LOSS_THRESHOLD:
        print(f"\\n🚨🚨🚨 [全局風控熔斷] 警告！當前總未實現虧損已達 {loss_percentage:.2f}%")
        print(f"🛑 超過安全防線 {GLOBAL_LOSS_THRESHOLD}%！觸發系統緊急黑天鵝熔斷機制...")
        return False
    return True
"""

content = content.replace("async def check_exits(sym):", panic_code + "\nasync def check_exits(sym):")

# Now inject it into the main loop
loop_target = """            for sym in ALL_SYMBOLS:
                if STATES[sym].get("sync_required"):"""

loop_inject = """            # ====== 第二點：總資金水位審查 ======
            if not getattr(sys.modules[__name__], 'GLOBAL_MELTDOWN_COOLING', False):
                is_equity_safe = check_total_equity_protection()
                if not is_equity_safe:
                    await execute_panic_sell_all_positions()
                    # 激活全局冷卻時間 (1小時)
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

            for sym in ALL_SYMBOLS:
                if STATES[sym].get("sync_required"):"""

content = content.replace(loop_target, loop_inject)

with open("multi_coin_bot.py", "w") as f:
    f.write(content)
