from core.check_entries import has_near_extreme_momentum_divergence


def _state(closes, rsis, current_rsi):
    return {
        "ohlcv": [[i, c, c, c, c, 1000] for i, c in enumerate(closes)],
        "rsi_history": rsis,
        "current_rsi": current_rsi,
    }


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
