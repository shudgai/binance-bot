import asyncio
from unittest.mock import AsyncMock, patch

from core.ctx import STATES, init_states
from core.state_manager import reset_coin_state
from core.trade_signal import update_trade_signal


def test_realtime_trade_crossing_trailing_stop_closes_immediately():
    sym = "XRPUSDT"
    init_states([sym])
    reset_coin_state(sym)
    state = STATES[sym]
    state.update({
        "qty": 1.0,
        "avg_price": 100.0,
        "current_atr": 0.1,
        "highest_profit_pct": 0.005,
        "trailing_highest": 100.5,
        "trailing_stop_price": 100.3,
        "stop_loss": 100.3,
        "trade_price_history": [100.4],
        "trade_qty_history": [1.0],
    })

    with patch("core.orders.close_position", AsyncMock()) as close:
        asyncio.run(update_trade_signal(sym, {"price": 100.29, "amount": 1.0}))

    close.assert_awaited_once()
    assert close.await_args.kwargs["reason"] == "[Dynamic_Trailing]"
    assert close.await_args.kwargs["is_stop_loss"] is False


def test_ma_wave_position_uses_dedicated_realtime_peak_lock():
    sym = "LINKUSDT"
    init_states([sym])
    reset_coin_state(sym)
    state = STATES[sym]
    state.update({
        "qty": 1.0, "avg_price": 100.0, "entry_reason": "MA_Breakout",
        "current_atr": 0.1, "highest_profit_pct": 0.015,
        "trailing_highest": 101.5, "trailing_stop_price": 100.5,
        "stop_loss": 100.5, "trade_price_history": [100.6],
        "trade_qty_history": [1.0],
    })

    with patch("core.orders.close_position", AsyncMock()) as close:
        asyncio.run(update_trade_signal(sym, {"price": 100.4, "amount": 1.0}))

    close.assert_awaited_once()
    assert close.await_args.kwargs["reason"] == "[MA_Peak_Lock]"
    assert state["ma_peak_lock_armed"] is True


def test_realtime_trade_does_not_close_before_soft_activation():
    sym = "XRPUSDT"
    init_states([sym])
    reset_coin_state(sym)
    state = STATES[sym]
    state.update({
        "qty": 1.0,
        "avg_price": 100.0,
        "current_atr": 0.1,
        "highest_profit_pct": 0.002,
        "trailing_highest": 100.2,
        "trailing_stop_price": 100.1,
        "stop_loss": 100.1,
        "trade_price_history": [100.15],
        "trade_qty_history": [1.0],
    })

    with patch("core.orders.close_position", AsyncMock()) as close:
        asyncio.run(update_trade_signal(sym, {"price": 100.05, "amount": 1.0}))

    close.assert_not_awaited()


def test_realtime_soft_trailing_gap_closes_immediately_instead_of_growing_loss():
    sym = "XRPUSDT"
    init_states([sym])
    reset_coin_state(sym)
    state = STATES[sym]
    state.update({
        "qty": 1.0,
        "avg_price": 100.0,
        "current_atr": 0.1,
        "highest_profit_pct": 0.0035,
        "trailing_highest": 100.35,
        "trailing_stop_price": 100.15,
        "stop_loss": 100.15,
        "trade_price_history": [100.20],
        "trade_qty_history": [1.0],
    })

    with patch("core.orders.close_position", AsyncMock()) as close:
        asyncio.run(update_trade_signal(sym, {"price": 99.95, "amount": 1.0}))

    close.assert_awaited_once()
    assert close.await_args.kwargs["reason"] == "[Dynamic_Trailing]"
    assert close.await_args.kwargs["is_stop_loss"] is True


def test_realtime_short_soft_trailing_gap_closes_immediately():
    sym = "XLMUSDT"
    init_states([sym])
    reset_coin_state(sym)
    state = STATES[sym]
    state.update({
        "qty": -559.0,
        "avg_price": 0.18320,
        "current_atr": 0.00046,
        "highest_profit_pct": 0.0037,
        "trailing_lowest": 0.18252,
        "trailing_stop_price": 0.18289,
        "stop_loss": 0.18289,
        "trade_price_history": [0.18280],
        "trade_qty_history": [559.0],
    })

    with patch("core.orders.close_position", AsyncMock()) as close:
        asyncio.run(update_trade_signal(sym, {"price": 0.18322, "amount": 559.0}))

    close.assert_awaited_once()
    assert close.await_args.kwargs["reason"] == "[Dynamic_Trailing]"
    assert close.await_args.kwargs["is_stop_loss"] is True


def test_single_spike_needs_confirmation_before_trailing():
    sym = "HYPEUSDT"
    init_states([sym])
    reset_coin_state(sym)
    state = STATES[sym]
    state.update({
        "qty": 2.43, "avg_price": 65.413, "open_time": 1000.0,
        "current_atr": 0.168, "trailing_highest": 65.413,
        "trailing_stop_price": 0.0, "stop_loss": 0.0,
        "last_market_trade_time": 0.0,
        "trade_price_history": [65.40], "trade_qty_history": [1.0],
    })

    with patch("core.orders.close_position", AsyncMock()) as close:
        asyncio.run(update_trade_signal(sym, {"price": 65.851, "amount": 1.0, "timestamp": 1001_000}))
        asyncio.run(update_trade_signal(sym, {"price": 65.367, "amount": 1.0, "timestamp": 1001_060}))

    close.assert_not_awaited()
    assert state["highest_profit_pct"] == 0.0
    assert state["trailing_stop_price"] == 0.0
    assert state["realtime_peak_candidate_profit"] == 0.0


def test_two_nearby_ticks_confirm_peak_and_keep_fast_trailing():
    sym = "HYPEUSDT"
    init_states([sym])
    reset_coin_state(sym)
    state = STATES[sym]
    state.update({
        "qty": 2.43, "avg_price": 65.413, "open_time": 2000.0,
        "current_atr": 0.168, "trailing_highest": 65.413,
        "trailing_stop_price": 0.0, "stop_loss": 0.0,
        "last_market_trade_time": 0.0,
        "trade_price_history": [65.40], "trade_qty_history": [1.0],
    })

    with patch("core.orders.close_position", AsyncMock()) as close:
        asyncio.run(update_trade_signal(sym, {"price": 65.851, "amount": 1.0, "timestamp": 2001_000}))
        asyncio.run(update_trade_signal(sym, {"price": 65.849, "amount": 1.0, "timestamp": 2001_050}))
        asyncio.run(update_trade_signal(sym, {"price": 65.367, "amount": 1.0, "timestamp": 2001_100}))

    close.assert_awaited_once()
    assert state["highest_profit_pct"] > 0.006
    assert close.await_args.kwargs["reason"] == "[Dynamic_Trailing]"


def test_out_of_order_trade_is_ignored_before_state_mutation():
    sym = "XRPUSDT"
    init_states([sym])
    reset_coin_state(sym)
    state = STATES[sym]
    state.update({
        "qty": 1.0, "avg_price": 100.0, "open_time": 3000.0,
        "last_market_trade_time": 3002.0, "last_trade_price": 100.1,
        "trade_price_history": [100.1], "trade_qty_history": [1.0],
    })

    asyncio.run(update_trade_signal(sym, {"price": 101.0, "amount": 1.0, "timestamp": 3001_000}))

    assert state["last_trade_price"] == 100.1
    assert state["trade_price_history"] == [100.1]
    assert state["highest_profit_pct"] == 0.0
