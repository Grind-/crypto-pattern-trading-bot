import json
import os
from typing import List

SIMS_FILE = "/app/data/simulations.json"


def save_simulation(entry: dict) -> None:
    sims = load_simulations()
    sims.insert(0, entry)
    sims = sims[:50]
    os.makedirs(os.path.dirname(SIMS_FILE), exist_ok=True)
    with open(SIMS_FILE, "w") as f:
        json.dump(sims, f, indent=2)


def load_simulations() -> List[dict]:
    if not os.path.exists(SIMS_FILE):
        return []
    try:
        with open(SIMS_FILE) as f:
            return json.load(f)
    except Exception:
        return []
