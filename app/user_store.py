import hashlib
import logging
import secrets
import shutil
from datetime import datetime, timezone
from typing import Dict, Optional

from sqlalchemy import delete, insert, select, update

from .database import engine, live_states, simulation_details, simulations, users

logger = logging.getLogger(__name__)

# Legacy global salt — used only to verify passwords of users who have not yet
# been migrated to per-user salts (salt column is NULL).  Never used for new users.
_LEGACY_SALT = "cpa_salt_bioval_2026"

_KNOWLEDGE_USERS_DIR = "/app/knowledge/users"


def _hash_pw(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200000).hex()


def verify_pw(stored_hash: str, salt: Optional[str], password: str) -> bool:
    effective_salt = salt if salt else _LEGACY_SALT
    return secrets.compare_digest(_hash_pw(password, effective_salt), stored_hash)


def hash_pw(password: str, salt: Optional[str] = None) -> str:
    """Hash a password.  If no salt is provided a new random one is generated."""
    if salt is None:
        salt = secrets.token_hex(16)
    return _hash_pw(password, salt)


def _row_to_dict(row) -> Dict:
    return dict(row._mapping)


# ── public API ────────────────────────────────────────────────────────────────

def init_users() -> None:
    """Seed admin user on first start with a random password printed to logs."""
    with engine.connect() as conn:
        exists = conn.execute(
            select(users.c.username).where(users.c.username == "admin")
        ).fetchone()
        if not exists:
            new_password = secrets.token_urlsafe(16)
            new_salt = secrets.token_hex(16)
            print(
                f"\n{'='*60}\n"
                f"  ADMIN PASSWORD (shown only once): {new_password}\n"
                f"{'='*60}\n",
                flush=True,
            )
            logger.warning("Admin account created — see stdout for the initial password")
            conn.execute(insert(users).values(
                username="admin",
                password_hash=_hash_pw(new_password, new_salt),
                salt=new_salt,
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
    if not verify_pw(user.get("password_hash", ""), user.get("salt"), password):
        return None
    # Transparently migrate legacy global-salt users to per-user salt on first login
    if not user.get("salt"):
        new_salt = secrets.token_hex(16)
        with engine.connect() as conn:
            conn.execute(
                update(users).where(users.c.username == username)
                .values(salt=new_salt, password_hash=_hash_pw(password, new_salt))
            )
            conn.commit()
        logger.info("Migrated user '%s' to per-user salt", username)
    return user


def create_user(username: str, password: str, role: str = "user",
                claude_mode: str = "api_key", owner: Optional[str] = None,
                email: Optional[str] = None) -> bool:
    if not username or not password:
        return False
    new_salt = secrets.token_hex(16)
    with engine.connect() as conn:
        exists = conn.execute(
            select(users.c.username).where(users.c.username == username)
        ).fetchone()
        if exists:
            return False
        conn.execute(insert(users).values(
            username=username,
            password_hash=_hash_pw(password, new_salt),
            salt=new_salt,
            role=role,
            enabled=True,
            created_at=datetime.now(timezone.utc).isoformat(),
            claude_mode=claude_mode,
            claude_api_key=None,
            claude_oauth_token=None,
            owner=owner,
            email=email,
        ))
        conn.commit()
    return True


def email_main_user(email: str) -> Optional[str]:
    """Return the username of the main user (owner=NULL) who owns this email, or None."""
    if not email:
        return None
    with engine.connect() as conn:
        row = conn.execute(
            select(users.c.username).where(
                users.c.email == email,
                users.c.owner.is_(None),
            )
        ).fetchone()
    return row[0] if row else None


def set_email(username: str, email: str) -> tuple[bool, Optional[str]]:
    """
    Set email for a user.
    Returns (True, None) on success.
    Returns (False, conflicting_username) if another main user already owns this email.
    Sub-accounts (owner != NULL) are exempt from the uniqueness check.
    """
    norm = (email or "").strip().lower() or None
    if norm:
        user = get_user(username)
        # Only main users (no owner) must be unique per email
        if not user or user.get("owner") is None:
            existing = email_main_user(norm)
            if existing and existing != username:
                return False, existing
    with engine.connect() as conn:
        result = conn.execute(
            update(users).where(users.c.username == username).values(email=norm)
        )
        conn.commit()
    return result.rowcount > 0, None


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
    shutil.rmtree(f"{_KNOWLEDGE_USERS_DIR}/{username}", ignore_errors=True)
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
    new_salt = secrets.token_hex(16)
    with engine.connect() as conn:
        result = conn.execute(
            update(users).where(users.c.username == username)
            .values(password_hash=_hash_pw(new_password, new_salt), salt=new_salt)
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


def save_binance_keys(username: str, api_key: str, api_secret: str) -> None:
    with engine.connect() as conn:
        conn.execute(
            update(users).where(users.c.username == username)
            .values(binance_api_key=api_key, binance_api_secret=api_secret)
        )
        conn.commit()


def get_binance_keys(username: str) -> tuple:
    user = get_user(username)
    if not user:
        return ("", "")
    return (user.get("binance_api_key") or "", user.get("binance_api_secret") or "")


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
