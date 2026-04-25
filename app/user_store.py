import hashlib
import secrets
from datetime import datetime, timezone
from typing import Dict, Optional

from sqlalchemy import delete, insert, select, update

from .database import engine, live_states, simulation_details, simulations, users

_SALT = "cpa_salt_bioval_2026"
_DEFAULT_ADMIN_HASH = "700acb2e5e32e2cbdb1cc63418b0842ba87925541d9fe07a7193646bd563aa3a"


def hash_pw(password: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), _SALT.encode(), 200000).hex()


def verify_pw(stored_hash: str, password: str) -> bool:
    return secrets.compare_digest(hash_pw(password), stored_hash)


def _row_to_dict(row) -> Dict:
    return dict(row._mapping)


# ── public API ────────────────────────────────────────────────────────────────

def init_users() -> None:
    """Seed admin user on first start."""
    with engine.connect() as conn:
        exists = conn.execute(
            select(users.c.username).where(users.c.username == "admin")
        ).fetchone()
        if not exists:
            conn.execute(insert(users).values(
                username="admin",
                password_hash=_DEFAULT_ADMIN_HASH,
                role="admin",
                enabled=True,
                created_at=datetime.now(timezone.utc).isoformat(),
                claude_mode="platform",
                claude_api_key=None,
                claude_oauth_token=None,
            ))
            conn.commit()


def list_users() -> Dict:
    with engine.connect() as conn:
        rows = conn.execute(select(users)).fetchall()
    result = {}
    for r in rows:
        d = _row_to_dict(r)
        result[d["username"]] = d
    return result


def get_user(username: str) -> Optional[Dict]:
    with engine.connect() as conn:
        row = conn.execute(
            select(users).where(users.c.username == username)
        ).fetchone()
    return _row_to_dict(row) if row else None


def authenticate(username: str, password: str) -> Optional[Dict]:
    user = get_user(username)
    if not user or not user.get("enabled"):
        return None
    if not verify_pw(user.get("password_hash", ""), password):
        return None
    return user


def create_user(username: str, password: str, role: str = "user",
                claude_mode: str = "api_key") -> bool:
    if not username or not password:
        return False
    with engine.connect() as conn:
        exists = conn.execute(
            select(users.c.username).where(users.c.username == username)
        ).fetchone()
        if exists:
            return False
        conn.execute(insert(users).values(
            username=username,
            password_hash=hash_pw(password),
            role=role,
            enabled=True,
            created_at=datetime.now(timezone.utc).isoformat(),
            claude_mode=claude_mode,
            claude_api_key=None,
            claude_oauth_token=None,
        ))
        conn.commit()
    return True


def delete_user(username: str) -> bool:
    if username == "admin":
        return False
    with engine.connect() as conn:
        conn.execute(delete(live_states).where(live_states.c.username == username))
        to_delete = [
            r._mapping["sim_id"]
            for r in conn.execute(
                select(simulations.c.sim_id).where(simulations.c.username == username)
            ).fetchall()
        ]
        if to_delete:
            conn.execute(delete(simulation_details).where(simulation_details.c.sim_id.in_(to_delete)))
            conn.execute(delete(simulations).where(simulations.c.sim_id.in_(to_delete)))
        result = conn.execute(delete(users).where(users.c.username == username))
        conn.commit()
    return result.rowcount > 0


def set_enabled(username: str, enabled: bool) -> bool:
    if username == "admin":
        return False
    with engine.connect() as conn:
        result = conn.execute(
            update(users).where(users.c.username == username).values(enabled=enabled)
        )
        conn.commit()
    return result.rowcount > 0


def reset_password(username: str, new_password: str) -> bool:
    with engine.connect() as conn:
        result = conn.execute(
            update(users).where(users.c.username == username)
            .values(password_hash=hash_pw(new_password))
        )
        conn.commit()
    return result.rowcount > 0


def update_claude_config(username: str, mode: str,
                         api_key: Optional[str] = None,
                         oauth_token: Optional[str] = None) -> bool:
    vals: dict = {"claude_mode": mode}
    if api_key is not None:
        vals["claude_api_key"] = api_key or None
    if oauth_token is not None:
        vals["claude_oauth_token"] = oauth_token or None
    with engine.connect() as conn:
        result = conn.execute(
            update(users).where(users.c.username == username).values(**vals)
        )
        conn.commit()
    return result.rowcount > 0


def set_platform_access(username: str, allow: bool) -> bool:
    mode = "platform" if allow else "api_key"
    with engine.connect() as conn:
        result = conn.execute(
            update(users).where(users.c.username == username).values(claude_mode=mode)
        )
        conn.commit()
    return result.rowcount > 0


def get_claude_api_key(username: str) -> Optional[str]:
    user = get_user(username)
    if not user:
        return None
    if user.get("claude_mode") in ("platform", "subscription"):
        return None
    return user.get("claude_api_key")


def get_claude_oauth_token(username: str) -> str:
    user = get_user(username)
    if not user:
        return ""
    if user.get("claude_mode") == "subscription":
        return user.get("claude_oauth_token") or ""
    return ""


def uses_platform(username: str) -> bool:
    user = get_user(username)
    return bool(user and user.get("claude_mode") == "platform")


def uses_subscription(username: str) -> bool:
    user = get_user(username)
    return bool(user and user.get("claude_mode") == "subscription")
