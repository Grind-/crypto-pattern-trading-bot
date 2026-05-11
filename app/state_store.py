import json
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import delete, insert, select, update

from .database import engine, live_states


_JSON_LIST_FIELDS = ("strategy_patterns", "trade_history")
_JSON_DICT_FIELDS = ("calibrated_thresholds", "portfolio_positions")


def _serialize(config: dict) -> dict:
    """Convert list/dict fields to JSON strings for storage."""
    row = dict(config)
    for key in _JSON_LIST_FIELDS:
        if key in row and not isinstance(row[key], str):
            row[key] = json.dumps(row[key])
    for key in _JSON_DICT_FIELDS:
        if key in row and not isinstance(row[key], str):
            row[key] = json.dumps(row[key]) if row[key] is not None else None
    return row


def _deserialize(row: dict) -> dict:
    """Convert JSON string fields back to Python objects."""
    result = dict(row)
    for key in _JSON_LIST_FIELDS:
        val = result.get(key)
        if isinstance(val, str):
            try:
                result[key] = json.loads(val)
            except Exception:
                result[key] = []
        elif val is None:
            result[key] = []
    for key in _JSON_DICT_FIELDS:
        val = result.get(key)
        if isinstance(val, str):
            try:
                result[key] = json.loads(val)
            except Exception:
                result[key] = {}
        elif val is None:
            result[key] = {}
    return result


# ── public API ────────────────────────────────────────────────────────────────

def save_live_state(username: str, config: dict) -> None:
    row = _serialize(config)
    row["username"] = username
    row["updated_at"] = datetime.now(timezone.utc).isoformat()
    # keep only columns that exist in the table
    cols = {c.name for c in live_states.c}
    row = {k: v for k, v in row.items() if k in cols}

    with engine.connect() as conn:
        exists = conn.execute(
            select(live_states.c.username).where(live_states.c.username == username)
        ).fetchone()
        if exists:
            conn.execute(
                update(live_states).where(live_states.c.username == username).values(**row)
            )
        else:
            conn.execute(insert(live_states).values(**row))
        conn.commit()


def load_live_state(username: str) -> Optional[dict]:
    with engine.connect() as conn:
        row = conn.execute(
            select(live_states).where(live_states.c.username == username)
        ).fetchone()
    if not row:
        return None
    return _deserialize(dict(row._mapping))


def clear_live_state(username: str) -> None:
    with engine.connect() as conn:
        conn.execute(
            delete(live_states).where(live_states.c.username == username)
        )
        conn.commit()


def deactivate_live_state(username: str) -> None:
    """Mark live state as stopped while preserving session data (position, history)."""
    with engine.connect() as conn:
        conn.execute(
            update(live_states).where(live_states.c.username == username)
            .values(was_running=False, updated_at=datetime.now(timezone.utc).isoformat())
        )
        conn.commit()


def update_position(username: str, position: str, symbol: str = None) -> None:
    vals = {"position": position}
    if symbol:
        vals["symbol"] = symbol
    vals["updated_at"] = datetime.now(timezone.utc).isoformat()
    with engine.connect() as conn:
        conn.execute(
            update(live_states).where(live_states.c.username == username).values(**vals)
        )
        conn.commit()
