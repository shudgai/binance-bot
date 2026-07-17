import os
import json
import time
from collections import defaultdict
import logging

logger = logging.getLogger(__name__)

IDLE_STATE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "idle_state.json"
)

class StrategyIdleTracker:
    """
    追蹤各幣種「策略持續無法開倉」的時長。
    資料會自動持久化到 data/idle_state.json，避免重啟時丟失。
    """
    def __init__(self, idle_threshold_sec: int = 300):
        self.idle_threshold_sec = idle_threshold_sec
        self._last_active_ts = {}
        self._block_reasons = {}  # {symbol: {strategy_name: reason}}
        self._load_state()

    def _load_state(self):
        try:
            if os.path.exists(IDLE_STATE_FILE):
                with open(IDLE_STATE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._last_active_ts = data.get("last_active_ts", {})
                    self._block_reasons = data.get("block_reasons", {})
        except Exception as e:
            logger.error(f"Failed to load idle state: {e}")

    def _save_state(self):
        try:
            os.makedirs(os.path.dirname(IDLE_STATE_FILE), exist_ok=True)
            with open(IDLE_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump({
                    "last_active_ts": self._last_active_ts,
                    "block_reasons": self._block_reasons
                }, f, indent=2, ensure_ascii=False)
        except Exception as e:
            logger.error(f"Failed to save idle state: {e}")

    def mark_blocked(self, symbol: str, strategy_name: str, reason: str):
        """策略回報本輪未開倉時呼叫"""
        if symbol not in self._last_active_ts:
            self._last_active_ts[symbol] = time.time()
        
        if symbol not in self._block_reasons:
            self._block_reasons[symbol] = {}
        
        self._block_reasons[symbol][strategy_name] = reason
        self._save_state()

    def mark_active(self, symbol: str, strategy_name: str):
        """策略成功產生開倉信號時呼叫，重置該幣種的閒置計時"""
        self._last_active_ts[symbol] = time.time()
        if symbol in self._block_reasons:
            self._block_reasons[symbol].pop(strategy_name, None)
        self._save_state()

    def is_idle(self, symbol: str, total_strategy_count: int) -> bool:
        """
        判斷該幣種是否所有策略都卡住，且超過閒置閾值。
        """
        last_active = self._last_active_ts.get(symbol, time.time())
        elapsed = time.time() - last_active
        
        blocked_strategies = self._block_reasons.get(symbol, {})
        all_blocked = len(blocked_strategies) >= total_strategy_count
        return all_blocked and elapsed >= self.idle_threshold_sec

    def idle_duration(self, symbol: str) -> float:
        return time.time() - self._last_active_ts.get(symbol, time.time())

    def get_idle_symbols(self, active_symbols: list, total_strategy_count: int = 2) -> list:
        """回傳目前所有處於閒置狀態的幣種清單"""
        return [s for s in active_symbols if self.is_idle(s, total_strategy_count)]

    def reset(self, symbol: str):
        """幣種被移出監控池時清除紀錄"""
        self._last_active_ts.pop(symbol, None)
        self._block_reasons.pop(symbol, None)
        self._save_state()

# 全域單例
idle_tracker = StrategyIdleTracker(idle_threshold_sec=300)
