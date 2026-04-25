import json
import os
from typing import List, Optional

_DATA_ROOT = "/app/data/users"


def _sims_dir(username: str) -> str:
    return f"{_DATA_ROOT}/{username}/sims"


def _sims_index(username: str) -> str:
    return f"{_DATA_ROOT}/{username}/simulations.json"


def save_simulation(username: str, entry: dict, full_result: dict = None) -> None:
    sims_dir = _sims_dir(username)
    os.makedirs(sims_dir, exist_ok=True)
    if full_result:
        with open(f"{sims_dir}/{entry['id']}.json", "w") as f:
            json.dump(full_result, f, indent=2)
    sims = load_simulations(username)
    sims.insert(0, entry)
    sims = sims[:50]
    index_path = _sims_index(username)
    os.makedirs(os.path.dirname(index_path), exist_ok=True)
    with open(index_path, "w") as f:
        json.dump(sims, f, indent=2)


def load_simulations(username: str) -> List[dict]:
    path = _sims_index(username)
    if not os.path.exists(path):
        return []
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return []


def load_simulation_detail(username: str, sim_id: str) -> Optional[dict]:
    path = f"{_sims_dir(username)}/{sim_id}.json"
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None
