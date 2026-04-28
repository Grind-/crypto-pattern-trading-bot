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
COMMUNITY_DIR  = f"{KNOWLEDGE_DIR}/community"
_CORE_PATTERNS = f"{CORE_DIR}/patterns.json"

MAX_SIM_LOG      = 100
MAX_WINNING      = 10
MAX_LOSING       = 6
MAX_GLOBAL_RULES = 8
MAX_TRADE_LOG    = 10_000
MIN_USERS_FOR_COMMUNITY = 2


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


def _user_trade_log_path(username: str, symbol: str) -> str:
    return f"{_user_dir(username)}/trades_{symbol}.json"


def _user_live_state_snapshot_path(username: str, symbol: str) -> str:
    return f"{_user_dir(username)}/live_state_{symbol}.json"


def _user_live_log_path(username: str) -> str:
    return f"{_user_dir(username)}/live_log.txt"


def append_live_log(username: str, line: str) -> None:
    """Append one log line to the persistent live log file."""
    try:
        os.makedirs(_user_dir(username), exist_ok=True)
        with open(_user_live_log_path(username), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_live_log(username: str, limit: int = 300) -> list:
    """Return the last `limit` lines from the persisted live log."""
    try:
        with open(_user_live_log_path(username), "r", encoding="utf-8") as f:
            lines = f.readlines()
        return [l.rstrip("\n") for l in lines[-limit:]]
    except Exception:
        return []


def trim_live_log(username: str, keep: int = 1000) -> None:
    """Truncate the log file to the most recent `keep` lines."""
    try:
        path = _user_live_log_path(username)
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) > keep:
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(lines[-keep:])
    except Exception:
        pass


def _community_path(symbol: str, interval: str) -> str:
    return f"{COMMUNITY_DIR}/{symbol}_{interval}.json"


def _user_settings_path(username: str) -> str:
    return f"{_user_dir(username)}/settings.json"


_SETTINGS_DEFAULTS: dict = {
    "live_interval": "4h",
    "live_amount": 50,
    "live_compounding_mode": "compound",
    "live_analysis_weight": 30,
    "sim_symbol": "BTCUSDC",
    "sim_interval": "4h",
    "sim_days": 30,
    "sim_capital": 1000,
    "sim_fee_tier": "standard",
    "sim_compounding_mode": "compound",
    "sim_analysis_weight": 30,
}


def load_user_settings(username: str) -> dict:
    saved = _load(_user_settings_path(username), {})
    return {**_SETTINGS_DEFAULTS, **saved}


def save_user_settings(username: str, settings: dict) -> None:
    allowed = set(_SETTINGS_DEFAULTS.keys())
    clean = {k: v for k, v in settings.items() if k in allowed}
    _save(_user_settings_path(username), clean)


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


_SNAPSHOT_EXCLUDE = frozenset({
    "signals", "log", "live_candles",
    "api_key", "api_secret",
    "_session_token", "_sell_fail_count",
    "pending_symbol_switch",
})


def append_trade_log(username: str, symbol: str, trade_dict: dict) -> None:
    """Append one trade entry to the persistent per-symbol log."""
    if not symbol:
        return
    path = _user_trade_log_path(username, symbol)
    log = _load(path, {"entries": []})
    entries = log.get("entries", [])
    entry = {**trade_dict, "recorded_at": datetime.now(timezone.utc).isoformat()}
    entries.append(entry)
    if len(entries) > MAX_TRADE_LOG:
        entries = entries[-MAX_TRADE_LOG:]
    log["entries"] = entries
    _save(path, log)


def load_trade_log(username: str, symbol: str, limit: int = 20, offset: int = 0) -> dict:
    """Return paginated trade history newest-first."""
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    path = _user_trade_log_path(username, symbol)
    log = _load(path, {"entries": []})
    entries = list(reversed(log.get("entries", [])))
    page = entries[offset:offset + limit]
    return {
        "trades": page,
        "total": len(entries),
        "limit": limit,
        "offset": offset,
        "has_more": (offset + limit) < len(entries),
    }


def save_live_state_snapshot(username: str, symbol: str, state: dict) -> None:
    """Persist a stripped live_state snapshot for crash recovery."""
    if not symbol:
        return
    snapshot = {k: v for k, v in state.items() if k not in _SNAPSHOT_EXCLUDE}
    snapshot["_snapshot_at"] = datetime.now(timezone.utc).isoformat()
    _save(_user_live_state_snapshot_path(username, symbol), snapshot)


def load_live_state_snapshot(username: str, symbol: str) -> dict | None:
    """Load snapshot; returns None on any error."""
    if not symbol:
        return None
    try:
        with open(_user_live_state_snapshot_path(username, symbol)) as f:
            return json.load(f)
    except Exception:
        return None


def get_all_user_data_for_symbol(symbol: str, interval: str) -> list[dict]:
    """Aggregate sim logs + live trade performance per user for a given symbol/interval.
    Returns list of per-user summaries with enough data for community synthesis."""
    results = []
    try:
        for uname in os.listdir(USERS_DIR):
            # Sim history for this symbol+interval
            sim_path = _user_sim_log_path(uname)
            sim_log  = _load(sim_path, {"entries": []})
            sessions = [
                e for e in sim_log.get("entries", [])
                if e.get("symbol") == symbol and e.get("interval") == interval
            ]
            if not sessions:
                continue

            # Live trade P&L for this symbol (SELL entries only, where pnl_pct is set)
            trade_path = _user_trade_log_path(uname, symbol)
            trade_log  = _load(trade_path, {"entries": []})
            live_sells = [
                e for e in trade_log.get("entries", [])
                if e.get("type") == "SELL" and e.get("pnl_pct") is not None
            ]
            live_count      = len(live_sells)
            live_profitable = sum(1 for t in live_sells if t["pnl_pct"] > 0)
            live_avg_pnl    = (
                sum(t["pnl_pct"] for t in live_sells) / live_count
                if live_count > 0 else None
            )

            # Pattern summary from user's patterns.json
            user_patterns = get_user_sym_patterns(uname, symbol, interval)
            winning = [p.get("description", "") for p in user_patterns.get("winning_patterns", [])[:5]]
            losing  = [p.get("description", "") for p in user_patterns.get("losing_patterns", [])[:3]]

            returns      = [e.get("return_pct", 0) for e in sessions]
            win_rates    = [e.get("win_rate", 0) for e in sessions]
            n_sessions   = len(sessions)
            n_profitable = sum(1 for e in sessions if e.get("profitable"))
            avg_return   = sum(returns) / n_sessions if n_sessions else 0
            avg_win_rate = sum(win_rates) / n_sessions if n_sessions else 0

            results.append({
                "username":       uname,
                "sim_sessions":   n_sessions,
                "sim_profitable": n_profitable,
                "avg_return_pct": round(avg_return, 2),
                "avg_win_rate":   round(avg_win_rate, 1),
                "winning_patterns": winning,
                "losing_patterns":  losing,
                "live_trades":    live_count,
                "live_profitable": live_profitable,
                "live_avg_pnl":   round(live_avg_pnl, 3) if live_avg_pnl is not None else None,
                "market_notes":   user_patterns.get("market_notes", ""),
            })
    except Exception:
        pass
    return results


def load_community_patterns(symbol: str, interval: str) -> dict:
    return _load(_community_path(symbol, interval), {})


def save_community_patterns(symbol: str, interval: str, data: dict) -> None:
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save(_community_path(symbol, interval), data)


def append_live_regime_log(username: str, entry: dict) -> None:
    path = f"{USERS_DIR}/{username}/live_regime_log.json"
    log = _load(path, {"entries": []})
    log["entries"] = ([entry] + log["entries"])[:200]
    _save(path, log)


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

    # Community patterns (cross-user consensus, anonymised)
    community = load_community_patterns(symbol, interval)
    n_users   = community.get("contributing_users", 0)
    if n_users >= MIN_USERS_FOR_COMMUNITY:
        total_s = community.get("total_sessions", 0)
        prof_s  = community.get("profitable_sessions", 0)
        lines.append(
            f"══ COMMUNITY PATTERNS: {symbol} {interval} — "
            f"{n_users} Trader · {total_s} Sessions · {prof_s}/{total_s} profitabel ══"
        )
        for p in community.get("consensus_patterns", [])[:4]:
            r   = p.get("avg_return_pct", 0)
            cnt = p.get("user_count", 0)
            lines.append(f"  ✓ {p['description']}  → avg {r:+.1f}% (bestätigt von {cnt} Tradern)")
        for p in community.get("consensus_avoid", [])[:3]:
            cnt = p.get("user_count", 0)
            lines.append(f"  ✗ {p['description']}  (gemieden von {cnt} Tradern)")
        comm_note = community.get("community_notes", "")
        if comm_note:
            lines.append(f"  COMMUNITY NOTE: {comm_note}")

    # Core promoted symbol patterns (admin-curated)
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
