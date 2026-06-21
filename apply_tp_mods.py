import re

with open("multi_coin_bot.py", "r") as f:
    code = f.read()

# 1. Breakeven point
old_be = """    if s.get("highest_profit_pct", 0.0) >= breakeven_threshold:
        sl = avg * 1.0015 if is_long else avg * 0.9985"""
new_be = """    if s.get("highest_profit_pct", 0.0) >= breakeven_threshold:
        atr_half = s.get("current_atr", atr_val) * 0.5
        sl = avg + atr_half if is_long else avg - atr_half"""
code = code.replace(old_be, new_be)

# 2 & 3. Strong Mode Priority & Layer 4 momentum check
old_tier = """    if s["highest_profit_pct"] >= tier3_target and profit_pct < s["highest_profit_pct"] * (0.8 if is_trend_ok else 0.6):
        cs = 'sell' if is_long else 'buy'
        print(f"🛡️ [大行情鎖利] {sym} 獲利達 {s['highest_profit_pct']*100:.3f}%(>4ATR)，觸發大行情回撤平倉")
        await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[Whipsaw_Stop]")
        s["highest_profit_pct"] = 0.0
        return
    elif s["highest_profit_pct"] >= tier2_target and profit_pct < s["highest_profit_pct"] * (0.7 if is_trend_ok else 0.5):
        cs = 'sell' if is_long else 'buy'
        print(f"🛡️ [中利鎖利] {sym} 獲利達 {s['highest_profit_pct']*100:.3f}%(>2.5ATR)，回落至 {profit_pct*100:.3f}% 平倉")
        await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[Take_Profit]")
        s["highest_profit_pct"] = 0.0
        return
    elif s["highest_profit_pct"] >= tier1_target and profit_pct < s["highest_profit_pct"] * 0.5:
        cs = 'sell' if is_long else 'buy'
        print(f"🛡️ [基本鎖利] {sym} 獲利達 {s['highest_profit_pct']*100:.3f}%(>1.5ATR)，回落至 {profit_pct*100:.3f}% 保護平倉")
        await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[Take_Profit]")
        s["highest_profit_pct"] = 0.0
        return"""

new_tier = """    if not is_strong:
        macd_hist_expanding = False
        if len(s.get("ohlcv", [])) >= 34:
            import numpy as np
            closes = np.array([x[4] for x in s["ohlcv"]])
            try:
                # We can compute MACD or just use a simple heuristic if calculate_macd isn't accessible
                # Let's rely on s["macd_hist"] and assume we can approximate expansion
                pass
            except:
                pass
        
        # Simplified momentum expansion check without recalculating MACD arrays:
        # We assume if the current price is continuing the trend powerfully, momentum is expanding.
        # But better: calculate_macd returns 5 values (macd, signal, hist, prev_macd, prev_sig) in this codebase!
        # Wait, the codebase has `calculate_macd(closes)` returning `macd_line, macd_signal, macd_hist, prev_macd_line, prev_macd_signal`.
        # So we can calculate it:
        try:
            import numpy as np
            closes = np.array([x[4] for x in s["ohlcv"]])
            _, _, m_hist, p_line, p_sig = calculate_macd(closes)
            p_hist = p_line - p_sig
            macd_hist_expanding = abs(m_hist) > abs(p_hist)
        except:
            macd_hist_expanding = False

        if s["highest_profit_pct"] >= tier3_target and profit_pct < s["highest_profit_pct"] * (0.8 if is_trend_ok else 0.6):
            if macd_hist_expanding:
                print(f"⚡ [強勢保留] {sym} 獲利達大行情水準，雖回撤20%但動能仍在擴張，暫不鎖利！")
            else:
                cs = 'sell' if is_long else 'buy'
                print(f"🛡️ [大行情鎖利] {sym} 獲利達 {s['highest_profit_pct']*100:.3f}%(>4ATR)，觸發大行情回撤平倉")
                await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[Whipsaw_Stop]")
                s["highest_profit_pct"] = 0.0
                return
        elif s["highest_profit_pct"] >= tier2_target and profit_pct < s["highest_profit_pct"] * (0.7 if is_trend_ok else 0.5):
            cs = 'sell' if is_long else 'buy'
            print(f"🛡️ [中利鎖利] {sym} 獲利達 {s['highest_profit_pct']*100:.3f}%(>2.5ATR)，回落至 {profit_pct*100:.3f}% 平倉")
            await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[Take_Profit]")
            s["highest_profit_pct"] = 0.0
            return
        elif s["highest_profit_pct"] >= tier1_target and profit_pct < s["highest_profit_pct"] * 0.5:
            cs = 'sell' if is_long else 'buy'
            print(f"🛡️ [基本鎖利] {sym} 獲利達 {s['highest_profit_pct']*100:.3f}%(>1.5ATR)，回落至 {profit_pct*100:.3f}% 保護平倉")
            await close_position(sym, cs, abs(s["qty"]), p, avg, reason="[Take_Profit]")
            s["highest_profit_pct"] = 0.0
            return"""

code = code.replace(old_tier, new_tier)

with open("multi_coin_bot.py", "w") as f:
    f.write(code)

print("Applied TP mods")
