from core.config import DEFAULT_SYMBOLS
from services.bot_manager_service import DEFAULT_SYMBOLS as MANAGER_DEFAULT_SYMBOLS
from services.radar_service import (
    ATR_ELIGIBLE_SYMBOLS,
    CORE_SYMBOLS,
    RADAR_SELECT_COUNT,
)


EXPECTED_SYMBOLS = [
    "ETHUSDT", "SOLUSDT", "XRPUSDT", "BNBUSDT", "DOGEUSDT",
    "SUIUSDT", "LINKUSDT", "AVAXUSDT", "XLMUSDT", "ADAUSDT",
]


def test_all_default_symbol_sources_use_the_approved_ten():
    assert DEFAULT_SYMBOLS == EXPECTED_SYMBOLS
    assert MANAGER_DEFAULT_SYMBOLS == EXPECTED_SYMBOLS
    assert ATR_ELIGIBLE_SYMBOLS == EXPECTED_SYMBOLS
    assert CORE_SYMBOLS == EXPECTED_SYMBOLS
    assert RADAR_SELECT_COUNT == 10


def test_atr_pool_excludes_event_and_unapproved_coins():
    assert "WLFIUSDT" not in ATR_ELIGIBLE_SYMBOLS
    assert "TRUMPUSDT" not in ATR_ELIGIBLE_SYMBOLS
    assert "ONDOUSDT" not in ATR_ELIGIBLE_SYMBOLS
