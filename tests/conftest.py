import pytest

import core.peak_store as peak_store
import core.orders as orders
import core.entry_reason_store as entry_reason_store
import core.entry_time_store as entry_time_store


@pytest.fixture(autouse=True)
def _isolate_peak_store(tmp_path, monkeypatch):
    """Tests must never touch data/position_peaks.json — it holds live trading
    risk state (e.g. the highest-profit peak MA_Peak_Lock trails from). Without
    this, tests using real symbol names (e.g. test_realtime_trailing.py's
    LINKUSDT fixture with highest_profit_pct=0.015) write straight into the
    production file every run. Confirmed in the wild: LINKUSDT was stuck
    reporting a fictitious 1.5% peak across three unrelated live trades over
    three days because the suite kept overwriting it back in."""
    monkeypatch.setattr(peak_store, "PEAK_STORE_FILE", tmp_path / "position_peaks.json")


@pytest.fixture(autouse=True)
def _isolate_live_trading_side_effects(tmp_path, monkeypatch):
    """A unit test must never place an order on the configured exchange.

    The test process inherits the bot testnet credentials. A test that calls
    check_exits without mocking close_position would otherwise submit a real
    reduce-only order against a live position with the same symbol. Keep tests
    in paper mode by default and redirect their trade history. Tests that cover
    the live-order branch opt in explicitly with a mocked exchange client.
    """
    monkeypatch.setattr(orders, "PAPER_TRADING", True)
    monkeypatch.setattr(orders, "TRADE_HISTORY_FILE", str(tmp_path / "trade_history.json"))
    monkeypatch.setattr(entry_reason_store, "ENTRY_REASON_STORE_FILE", tmp_path / "position_entry_reasons.json")
    monkeypatch.setattr(entry_time_store, "ENTRY_TIME_STORE_FILE", tmp_path / "position_entry_times.json")
