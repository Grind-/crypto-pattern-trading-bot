import json
import os
from typing import List, Optional

SIMS_DIR = "/app/data/sims"
SIMS_INDEX = "/app/data/simulations.json"


def save_simulation(entry: dict, full_result: dict = None) -> None:
    os.makedirs(SIMS_DIR, exist_ok=True)
    if full_result:
        with open(f"{SIMS_DIR}/{entry['id']}.json", "w") as f:
            json.dump(full_result, f, indent=2)
    sims = load_simulations()
    sims.insert(0, entry)
    sims = sims[:50]
    os.makedirs(os.path.dirname(SIMS_INDEX), exist_ok=True)
    with open(SIMS_INDEX, "w") as f:
        json.dump(sims, f, indent=2)


def load_simulations() -> List[dict]:
    if not os.path.exists(SIMS_INDEX):
        return []
    try:
        with open(SIMS_INDEX) as f:
            return json.load(f)
    except Exception:
        return []


def load_simulation_detail(sim_id: str) -> Optional[dict]:
    path = f"{SIMS_DIR}/{sim_id}.json"
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None
