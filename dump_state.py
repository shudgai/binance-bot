import json
import sys
import os

try:
    with open("data/state.json", "r") as f:
        data = json.load(f)
        for sym, state in data.get("states", {}).items():
            if state.get("is_ordering") or abs(state.get("qty", 0.0)) > 0.000001:
                print(f"{sym}: is_ordering={state.get('is_ordering')}, qty={state.get('qty')}, pending_side={state.get('pending_side')}")
except Exception as e:
    print(e)
