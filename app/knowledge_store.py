"""
3-tier knowledge store.

  knowledge/
    core/
      patterns.json          read-only (admin-promoted): global rules + curated symbol patterns
    users/
      {username}/
        patterns.json        per-user symbol/interval patterns (Claude writes here)
        sim_log.json         per-user simulation log (last 100 entries)

Read:  all layers are readable by any Claude call
Write: Claude writes only to users/{username}/
       admin promote endpoint merges selected user patterns into core/
"""
import json
import os
import tempfile
from datetime import datetime, timezone

KNOWLEDGE_DIR  = "/app/knowledge"
CORE_DIR       = f"{KNOWLEDGE_DIR}/core"
USERS_DIR      = f"{KNOWLEDGE_DIR}/users"
_CORE_PATTERNS = f"{CORE_DIR}/patterns.json"

MAX_SIM_LOG      = 100
MAX_WINNING      = 10
MAX_LOSING       = 6
MAX_GLOBAL_RULES = 8


# ── I/O helpers ───────────────────────────────────────────────────────────────

def _load(path: str, default) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default if isinstance(default, dict) else {}


def _save(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        os.unlink(tmp)
        raise


# ── Core layer (read-only for Claude) ────────────────────────────────────────

def load_core() -> dict:
    return _load(_CORE_PATTERNS, {
        "version": 1, "updated_at": "",
        "global_rules": [], "interval_notes": {}, "symbol_patterns": {},
    })


def _core_sym_patterns(symbol: str, interval: str) -> dict:
    return load_core().get("symbol_patterns", {}).get(symbol, {}).get(interval, {})


# ── User layer (Claude writes here) ──────────────────────────────────────────

def _user_dir(username: str) -> str:
    return f"{USERS_DIR}/{username}"


def _user_patterns_path(username: str) -> str:
    return f"{_user_dir(username)}/patterns.json"


def _user_sim_log_path(username: str) -> str:
    return f"{_user_dir(username)}/sim_log.json"


def load_user_patterns(username: str) -> dict:
    return _load(_user_patterns_path(username), {
        "version": 1, "username": username,
        "symbols": {}, "symbol_performance": {},
    })


def get_user_sym_patterns(username: str, symbol: str, interval: str) -> dict:
    return load_user_patterns(username).get("symbols", {}).get(symbol, {}).get(interval, {})


def update_user_patterns(username: str, symbol: str, interval: str, data: dict) -> None:
    p = load_user_patterns(username)
    p.setdefault("symbols", {}).setdefault(symbol, {})[interval] = data
    p["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save(_user_patterns_path(username), p)


def append_user_sim_log(username: str, entry: dict) -> None:
    path = _user_sim_log_path(username)
    log = _load(path, {"entries": []})
    entries = log.get("entries", [])
    entries.insert(0, entry)
    log["entries"] = entries[:MAX_SIM_LOG]
    _save(path, log)


def update_user_stats(username: str, symbol: str, return_pct: float, profitable: bool) -> None:
    p = load_user_patterns(username)
    perf = p.setdefault("symbol_performance", {})
    s = perf.setdefault(symbol, {"sessions": 0, "profitable": 0, "avg_return": 0.0})
    n = s["sessions"]
    s["avg_return"] = (s["avg_return"] * n + return_pct) / (n + 1)
    s["sessions"] = n + 1
    if profitable:
        s["profitable"] += 1
    p["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save(_user_patterns_path(username), p)


def load_all_user_sim_logs(limit: int = 60) -> list:
    """Aggregate sim logs from all users — used for cross-user rule distillation."""
    entries = []
    try:
        for uname in os.listdir(USERS_DIR):
            path = _user_sim_log_path(uname)
            log = _load(path, {"entries": []})
            for e in log.get("entries", []):
                entries.append({**e, "_user": uname})
    except Exception:
        pass
    entries.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
    return entries[:limit]


def aggregate_symbol_performance() -> dict:
    """Merge symbol_performance from all users into a single ranking."""
    merged: dict = {}
    try:
        for uname in os.listdir(USERS_DIR):
            p = load_user_patterns(uname)
            for sym, d in p.get("symbol_performance", {}).items():
                if sym not in merged:
                    merged[sym] = {"sessions": 0, "profitable": 0, "avg_return": 0.0}
                m = merged[sym]
                n_old, n_new = m["sessions"], d.get("sessions", 0)
                if n_new > 0:
                    m["avg_return"] = (m["avg_return"] * n_old + d.get("avg_return", 0) * n_new) / (n_old + n_new)
                    m["sessions"]   = n_old + n_new
                    m["profitable"] += d.get("profitable", 0)
    except Exception:
        pass
    return merged


# ── Promotion (admin-only writes to core) ─────────────────────────────────────

def promote_symbol_to_core(username: str, symbol: str, interval: str) -> bool:
    """Copy user's symbol/interval patterns into core/patterns.json."""
    data = get_user_sym_patterns(username, symbol, interval)
    if not data:
        return False
    core = load_core()
    core.setdefault("symbol_patterns", {}).setdefault(symbol, {})[interval] = data
    core["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save(_CORE_PATTERNS, core)
    return True


def promote_rules_to_core(rules: list) -> None:
    """Replace global_rules in core with a Claude-distilled list."""
    core = load_core()
    core["global_rules"] = rules[:MAX_GLOBAL_RULES]
    core["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save(_CORE_PATTERNS, core)


def write_merged_symbol_to_core(symbol: str, interval: str, data: dict) -> None:
    """Write Claude-merged patterns into core. Called from admin promote endpoint."""
    core = load_core()
    core.setdefault("symbol_patterns", {}).setdefault(symbol, {})[interval] = data
    core["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save(_CORE_PATTERNS, core)


# ── Context builder (injected into Claude prompts) ────────────────────────────

def get_knowledge_context(symbol: str, interval: str, username: str = "") -> str:
    """Return a compact, prompt-ready knowledge string (~200–400 tokens)."""
    core    = load_core()
    user_p  = load_user_patterns(username) if username else {}
    user_sp = user_p.get("symbols", {}).get(symbol, {}).get(interval, {}) if username else {}
    core_sp = _core_sym_patterns(symbol, interval)

    lines: list[str] = []

    # User's own accumulated patterns (highest priority)
    sc = user_sp.get("session_count", 0)
    if sc > 0 and username:
        ps = user_sp.get("profitable_sessions", 0)
        lines.append(f"══ YOUR PATTERNS: {symbol} {interval} — {sc} sessions | {ps}/{sc} profitable ══")
        for p in user_sp.get("winning_patterns", [])[:5]:
            r  = p.get("avg_return_pct", 0)
            wr = p.get("win_rate", 0)
            n  = p.get("sample_count", 0)
            lines.append(f"  ✓ {p['description']}  → avg {r:+.1f}%, {wr:.0f}% win (n={n})")
        for p in user_sp.get("losing_patterns", [])[:3]:
            lines.append(f"  ✗ {p['description']}")
        notes = user_sp.get("market_notes", "")
        if notes:
            lines.append(f"  NOTE: {notes}")

    # Core promoted symbol patterns (community-verified)
    csc = core_sp.get("session_count", 0)
    if csc > 0:
        lines.append(f"══ CORE PATTERNS: {symbol} {interval} — {csc} sessions promoted ══")
        for p in core_sp.get("winning_patterns", [])[:4]:
            lines.append(f"  ✓ {p['description']}  → avg {p.get('avg_return_pct',0):+.1f}%")
        for p in core_sp.get("losing_patterns", [])[:2]:
            lines.append(f"  ✗ {p['description']}")

    # Core global rules
    rules = core.get("global_rules", [])
    if rules:
        lines.append("GLOBAL RULES:")
        for r in rules[:5]:
            conf = r.get("confidence", "")
            tag  = " [seed]" if conf == "seed" else f" [{conf}]"
            lines.append(f"  → {r['rule']}{tag}")

    # Interval notes
    inote = core.get("interval_notes", {}).get(interval, "")
    if inote:
        lines.append(f"INTERVAL NOTE ({interval}): {inote}")

    # User's symbol performance ranking
    if username:
        perf = user_p.get("symbol_performance", {})
        if len(perf) >= 2:
            ranked = sorted(perf.items(), key=lambda x: x[1].get("avg_return", 0), reverse=True)
            lines.append("YOUR SYMBOL PERFORMANCE:")
            for sym, d in ranked[:4]:
                n = d.get("sessions", 0)
                r = d.get("avg_return", 0)
                p = d.get("profitable", 0)
                lines.append(f"  {sym}: {r:+.1f}% avg, {p}/{n} profitable")

    return "\n".join(lines)


# ── Admin status ──────────────────────────────────────────────────────────────

def get_knowledge_status() -> dict:
    core = load_core()
    users: dict = {}
    try:
        for uname in os.listdir(USERS_DIR):
            p   = load_user_patterns(uname)
            log = _load(_user_sim_log_path(uname), {"entries": []})
            sym_summary = {}
            for sym, ivs in p.get("symbols", {}).items():
                sym_summary[sym] = {}
                for iv, data in ivs.items():
                    sym_summary[sym][iv] = {
                        "session_count":      data.get("session_count", 0),
                        "profitable_sessions": data.get("profitable_sessions", 0),
                        "winning_patterns":   len(data.get("winning_patterns", [])),
                        "losing_patterns":    len(data.get("losing_patterns", [])),
                        "last_updated":       data.get("last_updated", ""),
                    }
            users[uname] = {
                "sim_count":          len(log.get("entries", [])),
                "symbol_performance": p.get("symbol_performance", {}),
                "symbols":            sym_summary,
                "updated_at":         p.get("updated_at", ""),
            }
    except Exception:
        pass

    return {
        "core": {
            "global_rules_count": len(core.get("global_rules", [])),
            "global_rules":       core.get("global_rules", []),
            "symbol_patterns": {
                sym: list(ivs.keys())
                for sym, ivs in core.get("symbol_patterns", {}).items()
            },
            "updated_at": core.get("updated_at", ""),
        },
        "users": users,
    }
