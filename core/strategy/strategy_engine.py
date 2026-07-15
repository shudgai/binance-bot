import logging
from typing import Any, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)


class StrategyEngine:
    """Compatibility engine that follows the same closed-candle MA lifecycle."""

    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        if df.empty:
            return df
        result = df.copy()
        for period in (7, 25, 99):
            result[f"MA{period}"] = result["close"].rolling(period).mean()
        result["VOL_MA20"] = result["volume"].rolling(20).mean().shift(1)
        return result

    def check_signals(self, ohlcv_data: List[List[Any]]) -> Optional[tuple]:
        if len(ohlcv_data) < 101:
            return None
        df = pd.DataFrame(
            ohlcv_data,
            columns=["timestamp", "open", "high", "low", "close", "volume"],
        )
        df = self.calculate_indicators(df)
        closed = df.iloc[-2]
        previous = df.iloc[-3]
        required = (closed["MA7"], closed["MA25"], closed["MA99"], closed["VOL_MA20"])
        if any(pd.isna(value) or value <= 0 for value in required):
            return None
        if closed["volume"] < closed["VOL_MA20"]:
            return None

        golden_cross = previous["MA7"] <= previous["MA25"] and closed["MA7"] > closed["MA25"]
        death_cross = previous["MA7"] >= previous["MA25"] and closed["MA7"] < closed["MA25"]
        if golden_cross and closed["close"] > closed["MA99"]:
            logger.info("MA-only compatibility signal: BUY")
            return ("BUY", 25.0)
        if death_cross and closed["close"] < closed["MA99"]:
            logger.info("MA-only compatibility signal: SELL")
            return ("SELL", 25.0)
        return None
