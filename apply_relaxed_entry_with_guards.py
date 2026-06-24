"""
apply_relaxed_entry_with_guards.py
===================================
進場條件放寬 → 允許更多訊號通過
但每筆交易仍受以下 5 道安全防線保護：
  ✅ ATR-based SL 保護（進場後立即有停損）
  ✅ 1.7% 獲利空間門檻（DUAL_SHOT_MIN_PROFIT_ROOM = 0.017，不變）
  ✅ 盈虧比 RR ≥ 1.2~1.3 確認（不變）
  ✅ 極端RSI防禦 F（RSI>88/12 仍攔截，不變）
  ✅ 每日最大虧損熔斷（新增）

放寬項目：
  - Stage 1 量能硬門檻：0.8/0.6 → 0.4/0.3（避免死水判定過激）
  - MTF 15m 趨勢對齊：從硬攔截改為警告（不 return False）
  - 4H 布林帶壓力位鄰近：0.5*ATR → 0.2*ATR
  - 動能共振 C 門檻：多單 RSI>30→>22；空單 RSI<70→<78
  - 量能真實性 D 門檻：0.2/0.15 → 0.1/0.08
  - 新增：每日最大虧損熔斷（-3% 日虧損封鎖新進場）
"""

with open("multi_coin_bot.py", "r", encoding="utf-8") as f:
    code = f.read()

# -----------------------------------------------------------------
# [PATCH 1] 每日虧損熔斷：在 MAX_POSITIONS 常量後注入全域追蹤變數
# -----------------------------------------------------------------
old_max_pos = """MAX_POSITIONS = 2
COOLDOWN_SEC = 1800"""

new_max_pos = """MAX_POSITIONS = 2
COOLDOWN_SEC = 1800

# -- 每日虧損熔斷 (Daily Loss Circuit Breaker) -------------------
# 當日累計已實現虧損超過 DAILY_LOSS_LIMIT_PCT 時，封鎖所有新進場
DAILY_LOSS_LIMIT_PCT = 0.03        # 3% 帳戶資金上限
_DAILY_REALIZED_LOSS = 0.0        # 當日累計實現虧損 (負數)
_DAILY_LOSS_DATE     = ""          # "YYYY-MM-DD"，跨日自動重置
_DAILY_LOSS_HALTED   = False       # 是否已觸發熔斷

def _reset_daily_loss_if_new_day():
    global _DAILY_REALIZED_LOSS, _DAILY_LOSS_DATE, _DAILY_LOSS_HALTED
    today = time.strftime("%Y-%m-%d")
    if _DAILY_LOSS_DATE != today:
        if _DAILY_LOSS_DATE:
            print(f"[每日熔斷重置] 新的一天 ({today})，清空昨日虧損累計 ({_DAILY_REALIZED_LOSS:.4f})")
        _DAILY_REALIZED_LOSS = 0.0
        _DAILY_LOSS_DATE = today
        _DAILY_LOSS_HALTED = False

def accrue_daily_realized_pnl(profit_pct: float, position_value: float):
    global _DAILY_REALIZED_LOSS, _DAILY_LOSS_HALTED
    _reset_daily_loss_if_new_day()
    if profit_pct < 0:
        _DAILY_REALIZED_LOSS += profit_pct
        if not _DAILY_LOSS_HALTED and abs(_DAILY_REALIZED_LOSS) >= DAILY_LOSS_LIMIT_PCT:
            _DAILY_LOSS_HALTED = True
            print(f"[每日熔斷] 當日累計虧損已達 {_DAILY_REALIZED_LOSS*100:.2f}% (上限: {DAILY_LOSS_LIMIT_PCT*100:.1f}%)，今日封鎖所有新進場！")

def is_daily_loss_halted() -> bool:
    _reset_daily_loss_if_new_day()
    return _DAILY_LOSS_HALTED"""

code = code.replace(old_max_pos, new_max_pos, 1)

# -----------------------------------------------------------------
# [PATCH 2] 平倉後累計當日 PnL（注入在 record_trade_result 呼叫之後）
# -----------------------------------------------------------------
old_record_call = """    record_trade_result(
        symbol=sym,
        entry_reason=s.get("entry_reason", "UNKNOWN"),
        exit_reason=full_reason,
        profit_pct=profit_pct,
        current_atr=s.get("current_atr", 0.0),
        max_profit_reached=s.get("max_profit", 0.0),
        expected_entry=real_avg,
        expected_exit=price,
        actual_entry=real_avg,
        actual_exit=price,
        fees=0.0,
        qty=qty
    )"""

new_record_call = """    record_trade_result(
        symbol=sym,
        entry_reason=s.get("entry_reason", "UNKNOWN"),
        exit_reason=full_reason,
        profit_pct=profit_pct,
        current_atr=s.get("current_atr", 0.0),
        max_profit_reached=s.get("max_profit", 0.0),
        expected_entry=real_avg,
        expected_exit=price,
        actual_entry=real_avg,
        actual_exit=price,
        fees=0.0,
        qty=qty
    )
    # [每日熔斷] 累計當日已實現損益
    try:
        accrue_daily_realized_pnl(profit_pct, real_avg * qty)
        if profit_pct < 0:
            print(f"[每日熔斷追蹤] {sym} 虧損 {profit_pct*100:.2f}% | 今日累計: {_DAILY_REALIZED_LOSS*100:.2f}% / {DAILY_LOSS_LIMIT_PCT*100:.1f}%")
    except Exception as _e:
        print(f"[每日熔斷追蹤失敗] {_e}")"""

code = code.replace(old_record_call, new_record_call, 1)

# -----------------------------------------------------------------
# [PATCH 3] check_entries 開頭注入每日熔斷門衛
# -----------------------------------------------------------------
old_check_entries_head = """async def check_entries():
    open_count = get_open_position_count()
    remaining_slots = MAX_POSITIONS - open_count"""

new_check_entries_head = """async def check_entries():
    # [每日熔斷] 先確認是否已觸發當日封鎖
    if is_daily_loss_halted():
        print(f"[每日熔斷] 今日累計虧損已超上限 ({abs(_DAILY_REALIZED_LOSS)*100:.2f}% >= {DAILY_LOSS_LIMIT_PCT*100:.1f}%)，跳過所有新進場！")
        return

    open_count = get_open_position_count()
    remaining_slots = MAX_POSITIONS - open_count"""

code = code.replace(old_check_entries_head, new_check_entries_head, 1)

# -----------------------------------------------------------------
# [PATCH 4] Stage 1 量能硬門檻放寬：0.8→0.4，0.6→0.3
# -----------------------------------------------------------------
old_vol_gate = """    vol_multiplier = 0.6 if is_low_vol_mode else 0.8  # 軟性放寬：高波動模式從 100% 降至 80%，避免低量時段完全停擺
    dynamic_vol_threshold = volume_ma20 * vol_multiplier
    if current_volume <= dynamic_vol_threshold:
        mode_label = "低波動放寬模式 60%" if is_low_vol_mode else "高波動軟性放寬 80%"
        # 【Extreme_Reversal 豁免】反轉訊號本身需要市場已活躍，低量才是問題；
        # 但此處是全局硬門檻，Extreme_Reversal 不應被「死水」邏輯攔截。
        if route == "Extreme_Reversal":
            print(f"⚡ [ALLOW] [Filter:Volume] {sym} Extreme_Reversal 路由豁免死水量能攔截 (當前: {current_volume:.1f} | 門檻: {dynamic_vol_threshold:.1f} | {mode_label})")
        else:
            print(f"🛑 [REJECT] [Filter:Volume] {sym} 量能未達標 (當前: {current_volume:.1f} <= 門檻: {dynamic_vol_threshold:.1f} | {mode_label})，判定為死水行情。")
            return False"""

new_vol_gate = """    # [放寬] 量能硬門檻：高波動 0.8→0.4，低波動 0.6→0.3
    # 策略：僅攔截真正死水行情，讓更多訊號通過，由後端 RR/ATR/利潤 守門
    vol_multiplier = 0.3 if is_low_vol_mode else 0.4
    dynamic_vol_threshold = volume_ma20 * vol_multiplier
    if current_volume <= dynamic_vol_threshold:
        mode_label = "低波動放寬模式 30%" if is_low_vol_mode else "高波動放寬 40%"
        if route in ("Extreme_Reversal", "Exhaustion_Entry"):
            print(f"⚡ [ALLOW] [Filter:Volume] {sym} {route} 路由豁免死水量能攔截 (當前: {current_volume:.1f} | 門檻: {dynamic_vol_threshold:.1f} | {mode_label})")
        else:
            print(f"🛑 [REJECT] [Filter:Volume] {sym} 量能嚴重不足 (當前: {current_volume:.1f} <= 門檻: {dynamic_vol_threshold:.1f} | {mode_label})，判定為死水行情。")
            return False"""

code = code.replace(old_vol_gate, new_vol_gate, 1)

# -----------------------------------------------------------------
# [PATCH 5] MTF 15m 趨勢對齊：硬攔截 → 警告
# -----------------------------------------------------------------
old_mtf_15m = """    if ema20_15m > 0 and ema50_15m > 0 and route != "Extreme_Reversal":
        if side == 'sell' and ema20_15m > ema50_15m:
            print(f"🛑 [REJECT] [Filter:MTF_Trend] {sym} 15m 大趨勢向上 (EMA20: {ema20_15m:.4f} > EMA50: {ema50_15m:.4f})，拒絕 5m 短線逆勢做空。")
            return False
        elif side == 'buy' and ema20_15m < ema50_15m:
            print(f"🛑 [REJECT] [Filter:MTF_Trend] {sym} 15m 大趨勢向下 (EMA20: {ema20_15m:.4f} < EMA50: {ema50_15m:.4f})，拒絕 5m 短線逆勢做多。")
            return False"""

new_mtf_15m = """    # [放寬] MTF 15m 趨勢對齊：改為警告而非硬攔截
    # 理由：讓 RR/ATR/利潤門檻取代趨勢強制攔截，允許逆勢高品質訊號通過
    if ema20_15m > 0 and ema50_15m > 0 and route not in ("Extreme_Reversal", "Exhaustion_Entry"):
        if side == 'sell' and ema20_15m > ema50_15m:
            print(f"⚠️ [WARN] [Filter:MTF_Trend] {sym} 15m 大趨勢向上，逆勢做空 — 由 RR/利潤門檻把關")
        elif side == 'buy' and ema20_15m < ema50_15m:
            print(f"⚠️ [WARN] [Filter:MTF_Trend] {sym} 15m 大趨勢向下，逆勢做多 — 由 RR/利潤門檻把關")"""

code = code.replace(old_mtf_15m, new_mtf_15m, 1)

# -----------------------------------------------------------------
# [PATCH 6] 4H 布林帶鄰近壓力位：0.5*ATR → 0.2*ATR
# -----------------------------------------------------------------
old_4h_bb = """    if upper_4h is not None and lower_4h is not None and atr > 0:
        if side == 'buy' and (upper_4h - cp) < atr * 0.5:
            print(f"🛑 觸發 [MTF 4H 強壓力位] {sym} 現價 {cp} 距離 4H 布林上軌 {upper_4h:.4f} 過近，禁止多單開倉防接刀")
            return False
        if side == 'sell' and (cp - lower_4h) < atr * 0.5:
            print(f"🛑 觸發 [MTF 4H 強壓力位] {sym} 現價 {cp} 距離 4H 布林下軌 {lower_4h:.4f} 過近，禁止空單開倉防地板空")
            return False"""

new_4h_bb = """    # [放寬] 4H BB 壓力位鄰近：0.5*ATR → 0.2*ATR（只攔截貼壓最極端情況）
    if upper_4h is not None and lower_4h is not None and atr > 0:
        if side == 'buy' and (upper_4h - cp) < atr * 0.2:
            print(f"🛑 觸發 [MTF 4H 強壓力位] {sym} 現價 {cp} 貼近 4H 布林上軌 {upper_4h:.4f} (<0.2*ATR)，禁止多單追高")
            return False
        if side == 'sell' and (cp - lower_4h) < atr * 0.2:
            print(f"🛑 觸發 [MTF 4H 強壓力位] {sym} 現價 {cp} 貼近 4H 布林下軌 {lower_4h:.4f} (<0.2*ATR)，禁止空單地板空")
            return False"""

code = code.replace(old_4h_bb, new_4h_bb, 1)

# -----------------------------------------------------------------
# [PATCH 7] 動能共振 C 塊放寬：RSI 多單>30→>22；空單<70→<78
# -----------------------------------------------------------------
old_confluence_c = """        if route not in ["Exhaustion_Entry", "Extreme_Reversal"]:
            # --- 趨勢過濾已由 compute_signal_strength 的 trend_score 扣分機制取代 ---
            # 這裡移除 SMA200/EMA50 的硬性攔截，讓分數(強度)決定一切
            
            # C. 動能共振過濾 (Momentum Confluence) - 已放寬
            if side == "buy":
                # 做多要求放寬：RSI > 30 (原本 35) 且 MACD 柱狀圖為正
                if rsi <= 30 or macd_hist <= 0:
                    print(f"🛑 [CONFLUENCE_FAIL] {sym}: 動能不共振 (RSI {rsi:.1f} <= 30 或 MACD {macd_hist:.6f} <= 0)")
                    continue
            else: # sell
                # 做空要求放寬：RSI < 70 (原本 65) 且 MACD 柱狀圖為負
                if rsi >= 70 or macd_hist >= 0:
                    print(f"🛑 [CONFLUENCE_FAIL] {sym}: 動能不共振 (RSI {rsi:.1f} >= 70 或 MACD {macd_hist:.6f} >= 0)")
                    continue"""

new_confluence_c = """        if route not in ["Exhaustion_Entry", "Extreme_Reversal"]:
            # --- 趨勢過濾已由 compute_signal_strength 的 trend_score 扣分機制取代 ---
            # 這裡移除 SMA200/EMA50 的硬性攔截，讓分數(強度)決定一切

            # C. [放寬] 動能共振過濾：RSI 多單>22；空單<78；MACD 允許剛轉向
            _macd_tiny = 1e-8
            if side == "buy":
                # 多單：RSI > 22 (原 > 30)；MACD 若偏低才強制要正
                if rsi <= 22:
                    print(f"🛑 [CONFLUENCE_FAIL] {sym}: 多單 RSI 極端超賣 ({rsi:.1f} <= 22)，防接刀")
                    continue
                if macd_hist < -_macd_tiny and rsi < 35:
                    print(f"🛑 [CONFLUENCE_FAIL] {sym}: 多單 RSI 低 ({rsi:.1f}) 且 MACD 仍負 ({macd_hist:.6f})")
                    continue
            else:  # sell
                # 空單：RSI < 78 (原 < 70)；MACD 若偏高才強制要負
                if rsi >= 78:
                    print(f"🛑 [CONFLUENCE_FAIL] {sym}: 空單 RSI 極端超買 ({rsi:.1f} >= 78)，防追高")
                    continue
                if macd_hist > _macd_tiny and rsi > 65:
                    print(f"🛑 [CONFLUENCE_FAIL] {sym}: 空單 RSI 高 ({rsi:.1f}) 且 MACD 仍正 ({macd_hist:.6f})")
                    continue"""

code = code.replace(old_confluence_c, new_confluence_c, 1)

# -----------------------------------------------------------------
# [PATCH 8] 量能真實性 D 門檻：0.2/0.15 → 0.1/0.08
# -----------------------------------------------------------------
old_vol_d = """        _d_multiplier = 0.15 if _is_low_vol_ce else 0.2
        if volume < (vol_ma20 * _d_multiplier):
            print(f"🛑 [CONFLUENCE_FAIL] {sym}: 量能不足 (當前量 {volume:.0f} < 均量 {vol_ma20:.0f} * {_d_multiplier})")
            continue"""

new_vol_d = """        # [放寬] 量能真實性 D 門檻：0.15/0.2 → 0.08/0.1
        _d_multiplier = 0.08 if _is_low_vol_ce else 0.1
        if volume < (vol_ma20 * _d_multiplier):
            print(f"🛑 [CONFLUENCE_FAIL] {sym}: 量能極度不足 (當前量 {volume:.0f} < 均量 {vol_ma20:.0f} * {_d_multiplier})")
            continue"""

code = code.replace(old_vol_d, new_vol_d, 1)

# -----------------------------------------------------------------
# [PATCH 9] RVOL 參與度 E 門檻：0.15/0.2 → 0.08/0.1
# -----------------------------------------------------------------
old_rvol = """            # 1. RVOL 檢查 (爆發力) - 動態門檻 (高波動放寬至 20%，與 CONFLUENCE 前置門檻對齊)
            _rvol_multiplier = 0.15 if _is_low_vol_ce else 0.2
            rvol_check = current_vol > (vol_ma20 * _rvol_multiplier)"""

new_rvol = """            # 1. [放寬] RVOL 門檻與 D 塊對齊：0.15/0.2 → 0.08/0.1
            _rvol_multiplier = 0.08 if _is_low_vol_ce else 0.1
            rvol_check = current_vol > (vol_ma20 * _rvol_multiplier)"""

code = code.replace(old_rvol, new_rvol, 1)

# -----------------------------------------------------------------
# 寫回
# -----------------------------------------------------------------
with open("multi_coin_bot.py", "w", encoding="utf-8") as f:
    f.write(code)

print("=" * 65)
print("✅ 進場條件放寬 + 每日熔斷 補丁已成功套用！")
print("=" * 65)
print()
print("變更摘要：")
print("  [PATCH 1] 新增每日虧損熔斷全域變數與函數")
print("  [PATCH 2] 平倉後自動累計當日已實現損益")
print("  [PATCH 3] check_entries 開頭：每日熔斷觸發則直接 return")
print("  [PATCH 4] Stage 1 量能硬門檻: 0.8→0.4 / 0.6→0.3")
print("  [PATCH 5] MTF 15m 對齊: 硬攔截 → 警告（不 return False）")
print("  [PATCH 6] 4H BB 鄰近保護: 0.5*ATR → 0.2*ATR")
print("  [PATCH 7] 動能共振 C: 多單 RSI>30→>22；空單 RSI<70→<78")
print("  [PATCH 8] 量能 D 門檻: 0.2/0.15 → 0.1/0.08")
print("  [PATCH 9] RVOL E 門檻: 0.2/0.15 → 0.1/0.08")
print()
print("保持不變的 5 道安全防線：")
print("  ✅ ATR-based SL（SL_ATR_MULTIPLIER = 2.5，進場即有停損）")
print("  ✅ 1.7% 獲利空間門檻（DUAL_SHOT_MIN_PROFIT_ROOM = 0.017）")
print("  ✅ 盈虧比 RR >= 1.2~1.3（根據訊號強度動態）")
print("  ✅ 極端RSI防禦 F（RSI>88/RSI<12 強勢訊號仍被攔截）")
print("  ✅ 每日最大虧損熔斷（日虧損 >= 3% 封鎖所有新進場）")
