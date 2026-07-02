from core.strategy.base_strategy import BaseStrategy

class CoreTrendStrategy(BaseStrategy):
    def get_profile_type(self) -> str:
        return "Core_Trend"

    async def check_exit(self, sym=None):
        # Specific overrides can be placed here, otherwise inherit default
        await super().check_exit(sym)
