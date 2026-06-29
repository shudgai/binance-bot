from abc import ABC, abstractmethod
import asyncio
import time
import numpy as np

from core import ctx

class BaseStrategy(ABC):
    """
    Base Strategy class.
    All specific profile strategies should inherit from this class.
    """
    def __init__(self, symbol):
        self.symbol = symbol
        self.state = ctx.STATES.get(symbol, {})

    @abstractmethod
    def get_profile_type(self) -> str:
        pass

    async def check_exit(self, sym=None):
        from core.exits import check_exits
        await check_exits(self.symbol)
