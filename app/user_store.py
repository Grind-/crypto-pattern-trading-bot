import hashlib
import json
import os
import secrets
from datetime import datetime, timezone
from typing import Dict, Optional

USERS_FILE = "/app/data/users.json"
_SALT = "cpa_salt_bioval_2026"

# admin password hash: same as before (password set separately in init)
_DEFAULT_ADMIN_HASH = "700acb2e5e32e2cbdb1cc63418b0842ba87925541d9fe07a7193646bd563aa3a"


def hash_pw(password: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), _SALT.encode(), 200000).hex()


def verify_pw(stored_hash: str, password: str) -> bool:
    return secrets.compare_digest(hash_pw(password), stored_hash)


def _load() -> Dict:
    if not os.path.exists(USERS_FILE):
        return {}
    try:
        with open(USERS_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save(users: Dict) -> None:
    os.makedirs(os.path.dirname(USERS_FILE), exist_ok=True)
    with open(USERS_FILE, "w") as f:
        json.dump(users, f, indent=2)


def init_users() -> None:
    """Create users.json with admin if it doesn't exist yet."""
    if os.path.exists(USERS_FILE):
        return
    _save({
        "admin": {
            "password_hash": _DEFAULT_ADMIN_HASH,
            "role": "admin",
            "enabled": True,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "claude_mode": "platform",
            "claude_api_key": None,
        }
    })
    os.makedirs(f"/app/data/users/admin/sims", exist_ok=True)


def list_users() -> Dict:
    return _load()


def get_user(username: str) -> Optional[Dict]:
    return _load().get(username)


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
    users = _load()
    if username in users:
        return False
    users[username] = {
        "password_hash": hash_pw(password),
        "role": role,
        "enabled": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "claude_mode": claude_mode,
        "claude_api_key": None,
    }
    _save(users)
    os.makedirs(f"/app/data/users/{username}/sims", exist_ok=True)
    return True


def delete_user(username: str) -> bool:
    if username == "admin":
        return False
    users = _load()
    if username not in users:
        return False
    del users[username]
    _save(users)
    return True


def set_enabled(username: str, enabled: bool) -> bool:
    if username == "admin":
        return False
    users = _load()
    if username not in users:
        return False
    users[username]["enabled"] = enabled
    _save(users)
    return True


def reset_password(username: str, new_password: str) -> bool:
    users = _load()
    if username not in users:
        return False
    users[username]["password_hash"] = hash_pw(new_password)
    _save(users)
    return True


def update_claude_config(username: str, mode: str,
                         api_key: Optional[str] = None) -> bool:
    users = _load()
    if username not in users:
        return False
    users[username]["claude_mode"] = mode
    users[username]["claude_api_key"] = api_key or None
    _save(users)
    return True


def set_platform_access(username: str, allow: bool) -> bool:
    """Admin can grant/revoke platform (proxy) access for a user."""
    users = _load()
    if username not in users:
        return False
    if allow:
        users[username]["claude_mode"] = "platform"
    else:
        users[username]["claude_mode"] = "api_key"
    _save(users)
    return True


def get_claude_api_key(username: str) -> Optional[str]:
    user = get_user(username)
    if not user:
        return None
    if user.get("claude_mode") == "platform":
        return None  # proxy mode, no key needed
    return user.get("claude_api_key")


def uses_platform(username: str) -> bool:
    user = get_user(username)
    return bool(user and user.get("claude_mode") == "platform")
