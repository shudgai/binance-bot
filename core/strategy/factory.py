from core.strategy.base_strategy import BaseStrategy
from core.strategy.trend_strategy import CoreTrendStrategy
from core.strategy.momentum_strategy import HighBetaMomentumStrategy
from core.strategy.speculative_strategy import SpeculativeRiskStrategy
from core.config import COIN_PROFILE_CONFIG

class StrategyFactory:
    @staticmethod
    def create_strategy(symbol: str) -> BaseStrategy:
        profile = COIN_PROFILE_CONFIG.get(symbol, {})
        profile_type = profile.get("profile_type", "Core_Trend")
        
        if profile_type == "Core_Trend":
            return CoreTrendStrategy(symbol)
        elif profile_type == "High_Beta_Momentum":
            return HighBetaMomentumStrategy(symbol)
        elif profile_type == "Speculative_Risk":
            return SpeculativeRiskStrategy(symbol)
        else:
            # Default to Core Trend
            return CoreTrendStrategy(symbol)
