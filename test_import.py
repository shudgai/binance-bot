import core.symbol_profile as sp
sp.SYMBOL_PROFILES = {"a": 1}

def test():
    from core.symbol_profile import SYMBOL_PROFILES
    print(SYMBOL_PROFILES)

test()
