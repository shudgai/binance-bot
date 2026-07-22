import pytest
import asyncio
from unittest.mock import patch
from core.config import COIN_PROFILE_CONFIG

def test_major_symbols_are_enabled_in_config():
    assert COIN_PROFILE_CONFIG.get("BTCUSDT", {}).get("disable_entry", False) is False
    assert COIN_PROFILE_CONFIG.get("ETHUSDT", {}).get("disable_entry", False) is False
    assert COIN_PROFILE_CONFIG.get("BNBUSDT", {}).get("disable_entry", False) is False

@patch("services.radar_service.get_bot_status")
@patch("services.radar_service.clean_blacklist")
@patch("services.radar_service.get_atr_ranked_coins")
@patch("services.radar_service.prioritize_entry_ready")
def test_auto_radar_switch_keeps_core_and_fills_dynamic_slots(mock_prioritize, mock_get_atr, mock_clean, mock_status):
    mock_status.return_value = {"is_running": False}
    # Mock return 15 generic coins plus the 3 excluded ones
    mock_ranking = [{"symbol": f"COIN{i}USDT", "price": 10, "atr_pct": 2, "one_h_vol_pct": 1, "change_pct": 0, "entry_direction": "long", "entry_readiness_score": 0.7, "entry_setup": "trend_wait"} for i in range(1, 16)]
    mock_ranking.append({"symbol": "BTCUSDT", "price": 10, "atr_pct": 2, "one_h_vol_pct": 1, "change_pct": 0, "entry_direction": "long", "entry_readiness_score": 0.7, "entry_setup": "trend_wait"})
    mock_ranking.append({"symbol": "ETHUSDT", "price": 10, "atr_pct": 2, "one_h_vol_pct": 1, "change_pct": 0, "entry_direction": "long", "entry_readiness_score": 0.7, "entry_setup": "trend_wait"})
    mock_ranking.append({"symbol": "BNBUSDT", "price": 10, "atr_pct": 2, "one_h_vol_pct": 1, "change_pct": 0, "entry_direction": "long", "entry_readiness_score": 0.7, "entry_setup": "trend_wait"})
    mock_get_atr.return_value = ([], mock_ranking)
    
    # Let prioritized be the same as filtered
    def side_effect(ranking_filtered):
        return ranking_filtered
    mock_prioritize.side_effect = side_effect

    from services.radar_service import auto_radar_switch
    with patch("services.radar_service.save_symbol_config") as mock_save, \
         patch("services.radar_service._save_radar_profiles"), \
         patch("services.radar_service.add_system_log"), \
         patch("services.radar_service.start_bot") as mock_start:
         
        auto_radar_switch(force_start=False)
        
        # Five core symbols stay fixed and the remaining slots come from radar ranking.
        saved_symbols = mock_save.call_args[0][0]
        for symbol in ("BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"):
            assert symbol in saved_symbols
        assert "COIN1USDT" in saved_symbols
        assert len(saved_symbols) == 15

@patch("core.ctx.ALL_SYMBOLS", ["BTCUSDT", "TESTUSDT"])
@patch.dict("core.ctx.STATES", {
    "BTCUSDT": {"status": "ACTIVE", "is_ordering": False, "qty": 0, "ohlcv": []},
    "TESTUSDT": {"status": "ACTIVE", "is_ordering": False, "qty": 0, "ohlcv": []}
}, clear=True)
def test_check_entries_processes_enabled_major_symbols():
    from core.check_entries import check_entries
    import core.ctx as ctx
    
    with patch("core.check_entries.is_daily_loss_halted", return_value=False), \
         patch("core.check_entries.get_open_position_count", return_value=0), \
         patch("core.balance.get_dynamic_max_slots", return_value=5), \
         patch("core.check_entries._load_disabled_symbols", return_value=set()), \
         patch("core.check_entries._is_confirmable_exit_cooldown", return_value=False) as mock_cooldown:
         
         try:
             asyncio.run(check_entries())
         except Exception:
             pass
             
         called_symbols = []
         for call in mock_cooldown.call_args_list:
             state_arg = call[0][0]
             for sym, state in ctx.STATES.items():
                 if state is state_arg:
                     called_symbols.append(sym)
                     
         assert "BTCUSDT" in called_symbols, "BTCUSDT should be processed when entry is enabled"
         assert "TESTUSDT" in called_symbols, "TESTUSDT should have been processed"
