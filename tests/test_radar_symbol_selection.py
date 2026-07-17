from unittest.mock import patch
import numpy as np

from core.config import DEFAULT_SYMBOLS, MIN_5M_ATR_PCT_FOR_MA_ENTRY
from core.balance import get_dynamic_max_slots
from services.bot_manager_service import (
    DEFAULT_SYMBOLS as MANAGER_DEFAULT_SYMBOLS,
    TRADE_POOL_SIZE as MANAGER_TRADE_POOL_SIZE,
    _prioritize_trade_pool,
    _restore_truncated_radar_pool,
)
from services.radar_service import (
    ATR_ELIGIBLE_SYMBOLS,
    CORE_SYMBOLS,
    RADAR_SELECT_COUNT,
    TRADE_POOL_SIZE,
    MIN_ATR_PCT_FOR_ENTRY,
    MAX_ATR_PCT_FOR_ENTRY,
    MIN_1H_VOL_PCT_FOR_ENTRY,
    MAX_1H_VOL_PCT_FOR_ENTRY,
    prioritize_entry_ready,
)
from services.binance_service import calculate_entry_readiness


EXPECTED_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT",
    "SOLUSDT", "XRPUSDT", "DOGEUSDT", "ADAUSDT", "NEARUSDT",
    "UNIUSDT", "AAVEUSDT",
    "HYPEUSDT", "WLDUSDT",
]
EXPECTED_ATR_SYMBOLS = EXPECTED_SYMBOLS


def test_atr_sources_use_the_approved_dynamic_pool():
    assert set(MANAGER_DEFAULT_SYMBOLS).issubset(set(DEFAULT_SYMBOLS))
    assert ATR_ELIGIBLE_SYMBOLS == EXPECTED_ATR_SYMBOLS
    assert CORE_SYMBOLS == EXPECTED_ATR_SYMBOLS
    assert RADAR_SELECT_COUNT == 25  # 候選池；實際交易監控仍由 bot manager 截為 15 檔
    assert TRADE_POOL_SIZE == 15
    assert MANAGER_TRADE_POOL_SIZE == 15
    assert MIN_ATR_PCT_FOR_ENTRY == 1.5
    assert MAX_ATR_PCT_FOR_ENTRY == 5.0
    assert MIN_1H_VOL_PCT_FOR_ENTRY == 0.30
    assert MAX_1H_VOL_PCT_FOR_ENTRY == 2.8


def test_entry_slots_follow_capital_tiers():
    assert get_dynamic_max_slots(150) == 3
    assert get_dynamic_max_slots(250) == 5
    assert abs(MIN_5M_ATR_PCT_FOR_MA_ENTRY - 0.0012) < 1e-12


def test_atr_pool_excludes_event_and_unapproved_coins():
    assert "WLFIUSDT" not in ATR_ELIGIBLE_SYMBOLS
    assert "TRUMPUSDT" not in ATR_ELIGIBLE_SYMBOLS
    assert "ONDOUSDT" not in ATR_ELIGIBLE_SYMBOLS


def test_startup_restores_truncated_pool_from_ranked_radar_profiles():
    profiles = {
        f"COIN{i}USDT": {
            "_radar_rank": i,
            "_radar_atr_pct": 3.0,
            "_trade_eligible": i != 15,
        }
        for i in range(1, 16)
    }
    with patch("services.bot_manager_service.load_symbol_profiles", return_value=profiles), \
         patch("services.bot_manager_service.add_system_log"):
        restored = _restore_truncated_radar_pool(["XRPUSDT", "LABUSDT"])

    assert len(restored) == 15
    assert restored[0] == "COIN1USDT"
    assert restored[-1] == "COIN15USDT"


def test_startup_keeps_a_normal_sized_pool_unchanged():
    current = [f"COIN{i}USDT" for i in range(1, 9)]
    with patch("services.bot_manager_service.load_symbol_profiles", return_value={}):
        assert _restore_truncated_radar_pool(current) == current


def test_trade_pool_prioritizes_mature_then_observing_then_watch_only():
    symbols = ["WATCHUSDT", "OBSERVEUSDT", "READYUSDT", "READY2USDT"]
    profiles = {
        "WATCHUSDT": {"_radar_rank": 1},
        "OBSERVEUSDT": {"_radar_strict_eligible": True, "_radar_entry_readiness": 0.9},
        "READYUSDT": {"_trade_eligible": True, "_radar_entry_readiness": 0.6},
        "READY2USDT": {"_trade_eligible": True, "_radar_entry_readiness": 0.8},
    }

    assert _prioritize_trade_pool(symbols, profiles) == [
        "READY2USDT", "READYUSDT", "OBSERVEUSDT", "WATCHUSDT",
    ]


def _make_5m_klines(closes):
    rows = []
    for idx, close in enumerate(closes):
        open_price = close - 0.08 if idx == len(closes) - 2 else close - 0.01
        rows.append([idx, open_price, close + 0.10, close - 0.10, close, 1000, idx + 1, 2_000_000])
    return rows


def test_atr_readiness_prefers_structure_aligned_with_entry_gates():
    # A gentle aligned uptrend near MA25 should rank as an actionable MA long setup.
    closes = [100 + idx * 0.005 + np.sin(idx / 2.0) * 0.10 for idx in range(206)]
    readiness = calculate_entry_readiness(_make_5m_klines(closes))

    assert readiness["direction"] == "long"
    assert readiness["score"] >= 0.75
    assert readiness["setup"] in ("ma25_pullback", "breakout", "trend_wait")
    assert "rsi" not in readiness
    assert "band_position" not in readiness


def test_atr_readiness_rejects_insufficient_history():
    readiness = calculate_entry_readiness(_make_5m_klines([100.0] * 20))

    assert readiness["direction"] == "none"
    assert readiness["score"] == 0.0


def test_atr_selection_prioritizes_entry_ready_rows_without_dropping_waiting_rows():
    rows = [
        {"symbol": "WAITUSDT", "entry_direction": "none", "entry_readiness_score": 0.45},
        {"symbol": "READYUSDT", "entry_direction": "long", "entry_readiness_score": 0.65},
        {"symbol": "BESTUSDT", "entry_direction": "short", "entry_readiness_score": 0.85},
    ]

    prioritized = prioritize_entry_ready(rows)

    assert [row["symbol"] for row in prioritized] == ["BESTUSDT", "READYUSDT", "WAITUSDT"]
