from core.ctx import init_states
from core.strategy.factory import StrategyFactory
init_states()
strategy = StrategyFactory.create_strategy("WLFIUSDT")
print(type(strategy))
