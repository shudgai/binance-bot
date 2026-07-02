from core.strategy.base_strategy import BaseStrategy

class HighBetaMomentumStrategy(BaseStrategy):
    def get_profile_type(self) -> str:
        return "High_Beta_Momentum"

    async def check_exit(self, sym=None):
        await super().check_exit(sym)
