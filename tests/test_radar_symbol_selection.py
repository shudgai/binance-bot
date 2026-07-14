from unittest.mock import patch
import numpy as np

from core.config import DEFAULT_SYMBOLS
from services.bot_manager_service import (
    DEFAULT_SYMBOLS as MANAGER_DEFAULT_SYMBOLS,
    _restore_truncated_radar_pool,
)
from services.radar_service import (
    ATR_ELIGIBLE_SYMBOLS,
    CORE_SYMBOLS,
    RADAR_SELECT_COUNT,
    MIN_ATR_PCT_FOR_ENTRY,
    MAX_ATR_PCT_FOR_ENTRY,
    MIN_1H_VOL_PCT_FOR_ENTRY,
    MAX_1H_VOL_PCT_FOR_ENTRY,
    prioritize_entry_ready,
)
from services.binance_service import calculate_entry_readiness


EXPECTED_SYMBOLS = [
    "XRPUSDT", "ADAUSDT", "LINKUSDT", "DOTUSDT", "LTCUSDT",
    "BCHUSDT", "UNIUSDT", "ETCUSDT", "AAVEUSDT", "ATOMUSDT",
    "HBARUSDT", "XLMUSDT", "AVAXUSDT", "NEARUSDT", "APTUSDT",
    "SUIUSDT", "INJUSDT", "RENDERUSDT",
]
EXPECTED_ATR_SYMBOLS = EXPECTED_SYMBOLS + ["DOGEUSDT", "SOLUSDT"]


def test_atr_sources_use_the_approved_dynamic_pool():
    assert set(MANAGER_DEFAULT_SYMBOLS).issubset(set(DEFAULT_SYMBOLS))
    assert ATR_ELIGIBLE_SYMBOLS == EXPECTED_ATR_SYMBOLS
    assert CORE_SYMBOLS == EXPECTED_ATR_SYMBOLS
    assert RADAR_SELECT_COUNT == 12
    assert MIN_ATR_PCT_FOR_ENTRY == 2.0
    assert MAX_ATR_PCT_FOR_ENTRY == 6.0
    assert MIN_1H_VOL_PCT_FOR_ENTRY == 0.30
    assert MAX_1H_VOL_PCT_FOR_ENTRY == 2.8


def test_atr_pool_excludes_event_and_unapproved_coins():
    assert "WLFIUSDT" not in ATR_ELIGIBLE_SYMBOLS
    assert "TRUMPUSDT" not in ATR_ELIGIBLE_SYMBOLS
    assert "ONDOUSDT" not in ATR_ELIGIBLE_SYMBOLS


def test_startup_restores_truncated_pool_from_ranked_radar_profiles():
    profiles = {
        f"COIN{i}USDT": {
            "_radar_rank": i,
            "_radar_atr_pct": 3.0,
            "_trade_eligible": i != 12,
        }
        for i in range(1, 13)
    }
    with patch("services.bot_manager_service.load_symbol_profiles", return_value=profiles), \
         patch("services.bot_manager_service.add_system_log"):
        restored = _restore_truncated_radar_pool(["XRPUSDT", "LABUSDT"])

    assert len(restored) == 12
    assert restored[0] == "COIN1USDT"
    assert restored[-1] == "COIN12USDT"


def test_startup_keeps_a_normal_sized_pool_unchanged():
    current = [f"COIN{i}USDT" for i in range(1, 9)]
    with patch("services.bot_manager_service.load_symbol_profiles", return_value={}):
        assert _restore_truncated_radar_pool(current) == current


def _make_15m_klines(closes):
    rows = []
    for idx, close in enumerate(closes):
        open_price = close - 0.08 if idx == len(closes) - 2 else close - 0.01
        rows.append([idx, open_price, close + 0.10, close - 0.10, close, 1000, idx + 1, 2_000_000])
    return rows


def test_atr_readiness_prefers_structure_aligned_with_entry_gates():
    # Gentle uptrend with pullbacks keeps RSI out of the extreme zone while EMA/MACD
    # and the completed candle remain suitable for a Route-A long.
    closes = [100 + idx * 0.005 + np.sin(idx / 2.0) * 0.10 for idx in range(206)]
    readiness = calculate_entry_readiness(_make_15m_klines(closes))

    assert readiness["direction"] == "long"
    assert readiness["score"] >= 0.75


def test_atr_readiness_rejects_insufficient_history():
    readiness = calculate_entry_readiness(_make_15m_klines([100.0] * 20))

    assert readiness["direction"] == "none"
    assert readiness["score"] == 0.0


def test_atr_selection_prioritizes_entry_ready_rows_without_dropping_waiting_rows():
    rows = [
        {"symbol": "WAITUSDT", "entry_direction": "none", "entry_readiness_score": 0.45},
        {"symbol": "READYUSDT", "entry_direction": "long", "entry_readiness_score": 0.65},
    ]

    prioritized = prioritize_entry_ready(rows)

    assert [row["symbol"] for row in prioritized] == ["READYUSDT", "WAITUSDT"]
