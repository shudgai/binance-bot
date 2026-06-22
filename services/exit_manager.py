import time

class ExitManager:
    def __init__(self, coin_profile_config: dict):
        """
        初始化 ExitManager，傳入全域的幣種配置字典 (COIN_PROFILE_CONFIG)
        """
        self.config = coin_profile_config

        # 定義預設值，確保即使 config 中沒寫也有安全底線
        self.default_profile = {
            "atr_multiplier": 2.0,        # 預設動態 ATR 止損乘數
            "mmp": 0.005,                 # 預設 MMP (0.5% 最小獲利門檻)
            "volume_threshold_factor": 1.2,
            "stalemate_threshold": 0.002, # 預設僵局防禦獲利門檻 (0.2%)
            "stalemate_time_sec": 3600    # 預設陷入僵局的時間認定 (例如 3600 秒 = 1小時)
        }

    def _get_symbol_profile(self, symbol: str) -> dict:
        """
        讀取特定幣種的個性化配置，並與預設值合併
        """
        symbol_config = self.config.get(symbol, {})
        profile = self.default_profile.copy()
        
        if "sl_atr_multiplier" in symbol_config:
            profile["atr_multiplier"] = symbol_config["sl_atr_multiplier"]
            
        for key, value in symbol_config.items():
            if key in profile or key in ["mmp", "stalemate_threshold", "stalemate_time_sec"]:
                profile[key] = value
                
        return profile

    def check_exit_conditions(self, symbol: str, position: dict, market_data: dict) -> dict:
        """
        檢查是否滿足平倉條件
        """
        qty = position.get("qty", 0)
        if qty == 0:
            return {"should_exit": False, "exit_type": "NONE", "reason": ""}

        is_long = qty > 0
        avg_price = position.get("avg_price", 0.0)
        open_time = position.get("open_time", time.time())
        current_price = market_data.get("current_price", 0.0)
        current_atr = market_data.get("current_atr", 0.0)
        
        profile = self._get_symbol_profile(symbol)
        atr_multiplier = profile.get("atr_multiplier")
        mmp = profile.get("mmp")
        stalemate_threshold = profile.get("stalemate_threshold")
        stalemate_time_sec = profile.get("stalemate_time_sec")

        if is_long:
            profit_pct = (current_price - avg_price) / avg_price
            stop_loss_price = avg_price - (current_atr * atr_multiplier)
            is_stop_loss = current_price <= stop_loss_price
        else:
            profit_pct = (avg_price - current_price) / avg_price
            stop_loss_price = avg_price + (current_atr * atr_multiplier)
            is_stop_loss = current_price >= stop_loss_price

        # 1. 動態 ATR 止損 (最優先防護)
        if is_stop_loss:
            return {
                "should_exit": True,
                "exit_type": "FULL",
                "reason": f"ATR_STOP_LOSS ({atr_multiplier}x)"
            }

        # 2. 僵局防禦 (Stalemate Defense)
        held_time = time.time() - open_time
        if held_time > stalemate_time_sec and profit_pct < stalemate_threshold:
            return {
                "should_exit": True,
                "exit_type": "PARTIAL_50", 
                "reason": f"STALEMATE_DEFENSE (Held {int(held_time/60)}m)"
            }

        # 3. MMP (最小意義獲利門檻) 攔截機制
        if profit_pct < mmp:
            return {
                "should_exit": False,
                "exit_type": "NONE",
                "reason": f"BELOW_MMP (Profit: {profit_pct*100:.2f}%)"
            }

        # 4. 常規獲利了結訊號
        trend_reversed = market_data.get("trend_reversed", False)
        if trend_reversed:
            return {
                "should_exit": True,
                "exit_type": "FULL",
                "reason": "TREND_REVERSAL_TAKE_PROFIT"
            }

        return {
            "should_exit": False,
            "exit_type": "NONE",
            "reason": ""
        }
