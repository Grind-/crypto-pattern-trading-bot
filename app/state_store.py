import json
import os
from typing import Optional

STATE_FILE = "/app/data/live_state.json"


def save_live_state(config: dict) -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(config, f, indent=2)


def load_live_state() -> Optional[dict]:
    if not os.path.exists(STATE_FILE):
        return None
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return None


def clear_live_state() -> None:
    if os.path.exists(STATE_FILE):
        os.remove(STATE_FILE)


def update_position(position: str, symbol: str = None) -> None:
    state = load_live_state()
    if state:
        state["position"] = position
        if symbol:
            state["symbol"] = symbol
        save_live_state(state)
