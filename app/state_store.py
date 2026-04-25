import json
import os
from typing import Optional

_DATA_ROOT = "/app/data/users"


def _state_file(username: str) -> str:
    return f"{_DATA_ROOT}/{username}/live_state.json"


def save_live_state(username: str, config: dict) -> None:
    path = _state_file(username)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(config, f, indent=2)


def load_live_state(username: str) -> Optional[dict]:
    path = _state_file(username)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def clear_live_state(username: str) -> None:
    path = _state_file(username)
    if os.path.exists(path):
        os.remove(path)


def update_position(username: str, position: str, symbol: str = None) -> None:
    state = load_live_state(username)
    if state:
        state["position"] = position
        if symbol:
            state["symbol"] = symbol
        save_live_state(username, state)
