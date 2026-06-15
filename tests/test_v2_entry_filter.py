import sys
import types


def _install_stubs():
    class DummyExchange:
        def __init__(self, *args, **kwargs):
            self.urls = {"api": {"fapiPublic": "", "fapiPrivate": ""}}

    ccxt_module = types.ModuleType("ccxt")
    ccxt_module.binance = lambda *args, **kwargs: DummyExchange()
    sys.modules.setdefault("ccxt", ccxt_module)

    ccxt_pro_module = types.ModuleType("ccxt.pro")
    ccxt_pro_module.binance = lambda *args, **kwargs: DummyExchange()
    sys.modules.setdefault("ccxt.pro", ccxt_pro_module)

    ai_signal_module = types.ModuleType("ai_signal")
    ai_signal_module.build_ai_context = lambda *args, **kwargs: {}
    ai_signal_module.fetch_ai_signals = lambda *args, **kwargs: {}
    ai_signal_module.AI_UPDATE_INTERVAL = 60
    sys.modules.setdefault("ai_signal", ai_signal_module)


_install_stubs()

import multi_coin_bot_v2 as m


def test_strong_signal_can_bypass_hugging_filter():
    assert not m.should_block_for_hugging(14.11, 0.11, 0.10, True)


def test_weak_signal_still_blocks_hugging_filter():
    assert m.should_block_for_hugging(8.0, 0.11, 0.10, True)
