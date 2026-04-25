import json
from typing import List, Optional

from sqlalchemy import delete, insert, select

from .database import engine, simulation_details, simulations

# Columns that are stored as JSON strings
_JSON_COLS = ("strategy_patterns",)

# Metadata columns that map 1:1 to the simulations table
_META_COLS = (
    "sim_id", "username", "created_at", "symbol", "interval", "days",
    "capital", "fee_tier", "total_return_pct", "win_rate", "num_trades",
    "max_drawdown", "total_fees_usdt", "fee_drag_pct",
    "strategy_name", "strategy_analysis", "strategy_patterns",
    "profitable", "iterations",
)


def _entry_to_row(username: str, entry: dict) -> dict:
    row = {"username": username}
    for col in _META_COLS:
        if col == "username":
            continue
        val = entry.get(col) or entry.get("id" if col == "sim_id" else col)
        if col in _JSON_COLS and not isinstance(val, str):
            val = json.dumps(val) if val is not None else "[]"
        row[col] = val
    return row


def _row_to_entry(row) -> dict:
    d = dict(row._mapping)
    for col in _JSON_COLS:
        v = d.get(col)
        if isinstance(v, str):
            try:
                d[col] = json.loads(v)
            except Exception:
                d[col] = []
    # expose as "id" for backward compat
    d["id"] = d.get("sim_id")
    return d


# ── public API ────────────────────────────────────────────────────────────────

def save_simulation(username: str, entry: dict, full_result: dict = None) -> None:
    row = _entry_to_row(username, entry)
    sim_id = row["sim_id"]

    with engine.connect() as conn:
        # upsert: delete old entry if exists, then insert
        conn.execute(delete(simulations).where(simulations.c.sim_id == sim_id))
        conn.execute(insert(simulations).values(**row))

        if full_result is not None:
            conn.execute(delete(simulation_details).where(simulation_details.c.sim_id == sim_id))
            conn.execute(insert(simulation_details).values(
                sim_id=sim_id,
                username=username,
                full_data=json.dumps(full_result),
            ))

        # enforce max 50 sims per user — delete oldest beyond limit
        all_ids = conn.execute(
            select(simulations.c.sim_id, simulations.c.created_at)
            .where(simulations.c.username == username)
            .order_by(simulations.c.created_at.desc())
        ).fetchall()
        if len(all_ids) > 50:
            to_delete = [r._mapping["sim_id"] for r in all_ids[50:]]
            conn.execute(delete(simulations).where(simulations.c.sim_id.in_(to_delete)))
            conn.execute(delete(simulation_details).where(simulation_details.c.sim_id.in_(to_delete)))

        conn.commit()


def load_simulations(username: str) -> List[dict]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(simulations)
            .where(simulations.c.username == username)
            .order_by(simulations.c.created_at.desc())
        ).fetchall()
    return [_row_to_entry(r) for r in rows]


def load_simulation_detail(username: str, sim_id: str) -> Optional[dict]:
    with engine.connect() as conn:
        row = conn.execute(
            select(simulation_details)
            .where(
                simulation_details.c.sim_id == sim_id,
                simulation_details.c.username == username,
            )
        ).fetchone()
    if not row:
        return None
    try:
        return json.loads(row._mapping["full_data"])
    except Exception:
        return None
