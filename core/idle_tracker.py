import os
import json
import time
from collections import defaultdict
import logging

logger = logging.getLogger(__name__)

from core.config import get_data_file_path
IDLE_STATE_FILE = get_data_file_path("idle_state.json")

class StrategyIdleTracker:
    """
    追蹤各幣種「策略持續無法開倉」的時長。
    當某幣種所有策略都卡住超過閾值，標記為 idle，供動態選幣機制優先替換。
    同時管理換池時的「孤兒倉位」安全出場流程。

    資料會自動持久化到 data/idle_state.json，避免重啟時丟失。
    """

    def __init__(self, idle_threshold_sec: int = 300):
        # 預設 5 分鐘（300 秒）視為卡死太久，可依實際更新頻率調整
        self.idle_threshold_sec = idle_threshold_sec
        self._last_active_ts = {}
        self._block_reasons = {}   # {symbol: {strategy_name: reason}}
        # 換池後仍有持倉、需持續追蹤出場的幣種
        self._orphaned_positions = set()
        self._load_state()

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def _load_state(self):
        try:
            if os.path.exists(IDLE_STATE_FILE):
                with open(IDLE_STATE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._last_active_ts = data.get("last_active_ts", {})
                    self._block_reasons = data.get("block_reasons", {})
                    # 孤兒清單以 list 存檔（JSON 不支援 set），讀回時轉 set
                    self._orphaned_positions = set(data.get("orphaned_positions", []))
        except Exception as e:
            logger.error(f"Failed to load idle state: {e}")

    def _save_state(self):
        try:
            os.makedirs(os.path.dirname(IDLE_STATE_FILE), exist_ok=True)
            with open(IDLE_STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "last_active_ts": self._last_active_ts,
                        "block_reasons": self._block_reasons,
                        "orphaned_positions": list(self._orphaned_positions),
                    },
                    f,
                    indent=2,
                    ensure_ascii=False,
                )
        except Exception as e:
            logger.error(f"Failed to save idle state: {e}")

    # ------------------------------------------------------------------
    # 核心計時
    # ------------------------------------------------------------------

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
        total_strategy_count: 該幣種目前應運行的策略總數（例如 MA_Strategy + Range = 2）
        """
        last_active = self._last_active_ts.get(symbol, time.time())
        elapsed = time.time() - last_active

        blocked_strategies = self._block_reasons.get(symbol, {})
        all_blocked = len(blocked_strategies) >= total_strategy_count
        return all_blocked and elapsed >= self.idle_threshold_sec

    def idle_duration(self, symbol: str) -> float:
        return time.time() - self._last_active_ts.get(symbol, time.time())

    def get_idle_symbols(self, active_symbols: list, total_strategy_count: int = 2) -> list:
        """回傳目前所有處於閒置狀態的幣種清單，供選幣模組使用"""
        return [s for s in active_symbols if self.is_idle(s, total_strategy_count)]

    # ------------------------------------------------------------------
    # 孤兒倉位管理
    # ------------------------------------------------------------------

    def get_removable_symbols(self, idle_symbols: list, position_checker) -> tuple:
        """
        將閒置幣種分成兩類：
        - safe_to_remove: 無持倉，可直接從監控池移除
        - hold_for_exit:  有持倉，暫不移除，轉入孤兒清單持續追蹤直到平倉

        position_checker: 一個函式，傳入 symbol 回傳目前是否有持倉 (bool)。
        建議使用本地 ctx.STATES qty 判斷，避免每輪疊加交易所 API 呼叫。
        """
        safe_to_remove = []
        hold_for_exit = []

        for symbol in idle_symbols:
            if position_checker(symbol):
                hold_for_exit.append(symbol)
                self._orphaned_positions.add(symbol)
            else:
                safe_to_remove.append(symbol)

        if hold_for_exit:
            self._save_state()
            logger.info(
                f"🔒 [IdleTracker] 孤兒倉位新增：{hold_for_exit}，"
                f"孤兒清單現有 {len(self._orphaned_positions)} 檔"
            )

        return safe_to_remove, hold_for_exit

    def confirm_position_closed(self, symbol: str):
        """
        平倉確認後呼叫，把幣種從孤兒清單移除，並清掉閒置紀錄。
        必須在本地狀態（qty 等）真正歸零之後才呼叫（即 reset_coin_state 之後）。
        """
        was_orphan = symbol in self._orphaned_positions
        self._orphaned_positions.discard(symbol)
        self.reset(symbol)   # reset() 內部已呼叫 _save_state
        if was_orphan:
            logger.info(
                f"✅ [IdleTracker] {symbol} 孤兒倉位已平倉，從孤兒清單移除。"
                f"剩餘孤兒：{len(self._orphaned_positions)} 檔"
            )

    def get_orphaned_positions(self) -> set:
        """回傳目前仍需持續監控出場的孤兒倉位"""
        return set(self._orphaned_positions)

    # ------------------------------------------------------------------
    # 清理
    # ------------------------------------------------------------------

    def reset(self, symbol: str):
        """幣種被移出監控池時清除紀錄，避免殘留舊資料"""
        self._last_active_ts.pop(symbol, None)
        self._block_reasons.pop(symbol, None)
        # 孤兒清單不在此清除：孤兒需等 confirm_position_closed 明確確認後才移除
        self._save_state()


# 全域單例
idle_tracker = StrategyIdleTracker(idle_threshold_sec=300)
