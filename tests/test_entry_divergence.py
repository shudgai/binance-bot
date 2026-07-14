from core import ctx
from core.check_entries import compute_indicators, has_near_extreme_momentum_divergence
from core.ctx import init_states
from core.state_manager import reset_coin_state


def _state(closes, rsis, current_rsi):
    return {
        "ohlcv": [[i, c, c, c, c, 1000] for i, c in enumerate(closes)]
        + [[len(closes), closes[-1], closes[-1], closes[-1], closes[-1], 0]],
        "rsi_history": rsis,
        "current_rsi": current_rsi,
    }


def test_rsi_history_records_each_closed_candle_once():
    sym = "RSITESTUSDT"
    init_states([sym])
    reset_coin_state(sym)
    state = ctx.STATES[sym]
    state["ohlcv"] = [
        [i, 100 + i * 0.1, 100.2 + i * 0.1, 99.8 + i * 0.1, 100 + i * 0.1, 1000]
        for i in range(20)
    ]

    compute_indicators(sym)
    first_history = list(state["rsi_history"])
    state["ohlcv"][-1][4] += 0.5
    compute_indicators(sym)
    assert state["rsi_history"] == first_history

    state["ohlcv"].append([20, 102.0, 102.2, 101.8, 102.1, 100])
    compute_indicators(sym)
    assert len(state["rsi_history"]) == len(first_history) + 1


def test_blocks_long_near_high_when_rsi_has_fallen_sharply():
    state = _state(
        [0.7440, 0.7460, 0.7480, 0.7496, 0.7494],
        [55.0, 62.0, 68.0, 70.5, 60.7],
        60.7,
    )
    assert has_near_extreme_momentum_divergence(state, "buy", 0.7494)


def test_allows_long_near_high_when_rsi_remains_strong():
    state = _state(
        [0.7440, 0.7460, 0.7480, 0.7496, 0.7494],
        [55.0, 62.0, 68.0, 70.5, 67.0],
        67.0,
    )
    assert not has_near_extreme_momentum_divergence(state, "buy", 0.7494)


def test_does_not_block_when_price_has_already_pulled_back_from_high():
    state = _state(
        [0.7440, 0.7460, 0.7480, 0.7500, 0.7470],
        [55.0, 62.0, 68.0, 71.0, 60.0],
        60.0,
    )
    assert not has_near_extreme_momentum_divergence(state, "buy", 0.7470)
