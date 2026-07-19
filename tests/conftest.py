import pytest

import core.peak_store as peak_store


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
