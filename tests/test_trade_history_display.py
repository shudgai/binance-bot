import json
from pathlib import Path

from services import api


def test_real_trade_history_has_unique_stable_row_ids(tmp_path, monkeypatch):
    history_path = tmp_path / "trade_history.json"
    history_path.write_text(json.dumps([{
        "timestamp": "2026-07-18 12:34:24",
        "entry_timestamp_ms": 1784373400523,
        "symbol": "XRPUSDT",
        "profit_pct": -0.001,
        "gross_profit_pct": 0.0001,
        "side": "buy",
        "actual_entry": 1.0871,
        "actual_exit": 1.0872,
        "qty": 102.4,
        "fees": 0.0668,
        "realized_pnl_usdt": -0.01024,
        "exchange_close_id": "XRPUSDT:2746951952",
    }]), encoding="utf-8")
    monkeypatch.setattr("core.config.TRADE_HISTORY_FILE", str(history_path))

    trades = api._get_real_trades()

    assert [trade["time"] for trade in trades] == [1784373400523, 1784378064000]
    assert [trade["id"] for trade in trades] == [
        "history:XRPUSDT:2746951952:entry",
        "history:XRPUSDT:2746951952:exit",
    ]
    assert len({trade["id"] for trade in trades}) == 2
    assert trades[0]["isBuyer"] is True
    assert trades[1]["isBuyer"] is False
    paired = api._attach_round_trip_fees(trades)
    assert round(paired[1]["net_pnl"], 5) == -0.07704


def test_trade_table_uses_composite_key_and_taipei_timezone():
    html = (Path(__file__).parents[1] / "web" / "index.html").read_text(encoding="utf-8")

    assert ':key="tradeRowKey(t)"' in html
    assert "const tradeRowKey = (trade) =>" in html
    assert "timeZone: 'Asia/Taipei'" in html
    assert ':key="t.id"' not in html
