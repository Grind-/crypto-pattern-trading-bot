"""
Persistent knowledge base — all Claudes read from and write to these files.
Files live in /app/knowledge (git-tracked volume mount).
Atomic writes via temp-file + rename to prevent corruption.
"""
import json
import os
import tempfile
from datetime import datetime, timezone
from typing import Optional

KNOWLEDGE_DIR = "/app/knowledge"
_PATTERNS = f"{KNOWLEDGE_DIR}/patterns.json"
_SIM_LOG  = f"{KNOWLEDGE_DIR}/sim_log.json"
_GLOBAL   = f"{KNOWLEDGE_DIR}/global_insights.json"

MAX_SIM_LOG   = 200
MAX_WINNING   = 10
MAX_LOSING    = 6


# ── I/O helpers ───────────────────────────────────────────────────────────────

def _load(path: str, default) -> dict:
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default if isinstance(default, dict) else {}


def _save(path: str, data) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    dir_ = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(dir=dir_, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        os.unlink(tmp)
        raise


# ── patterns.json ─────────────────────────────────────────────────────────────

def load_patterns() -> dict:
    return _load(_PATTERNS, {"version": 1, "symbols": {}, "updated_at": ""})


def get_symbol_patterns(symbol: str, interval: str) -> dict:
    return load_patterns().get("symbols", {}).get(symbol, {}).get(interval, {})


def update_symbol_patterns(symbol: str, interval: str, new_data: dict) -> None:
    patterns = load_patterns()
    patterns.setdefault("symbols", {}).setdefault(symbol, {})[interval] = new_data
    patterns["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save(_PATTERNS, patterns)


# ── sim_log.json ──────────────────────────────────────────────────────────────

def append_sim_log(entry: dict) -> None:
    log = _load(_SIM_LOG, {"entries": []})
    entries = log.get("entries", [])
    entries.insert(0, entry)
    log["entries"] = entries[:MAX_SIM_LOG]
    _save(_SIM_LOG, log)


# ── global_insights.json ──────────────────────────────────────────────────────

def load_global_insights() -> dict:
    return _load(_GLOBAL, {"rules": [], "symbol_performance": {}, "updated_at": ""})


def save_global_insights(data: dict) -> None:
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    _save(_GLOBAL, data)


def update_global_stats(symbol: str, return_pct: float, profitable: bool) -> None:
    g = load_global_insights()
    perf = g.setdefault("symbol_performance", {})
    s = perf.setdefault(symbol, {"sessions": 0, "profitable": 0, "avg_return": 0.0})
    n = s["sessions"]
    s["avg_return"] = (s["avg_return"] * n + return_pct) / (n + 1)
    s["sessions"] = n + 1
    if profitable:
        s["profitable"] = s.get("profitable", 0) + 1
    g["total_simulations"] = g.get("total_simulations", 0) + 1
    save_global_insights(g)


# ── Context builder ───────────────────────────────────────────────────────────

def get_knowledge_context(symbol: str, interval: str) -> str:
    """Return a compact, prompt-ready knowledge string (≈ 200–400 tokens)."""
    sp = get_symbol_patterns(symbol, interval)
    g  = load_global_insights()

    lines: list[str] = []

    # ── Symbol / interval block ──
    sc = sp.get("session_count", 0)
    if sc > 0:
        ps  = sp.get("profitable_sessions", 0)
        pct = ps / sc * 100
        lines.append(f"══ KNOWLEDGE BASE: {symbol} {interval} — {sc} sessions | {pct:.0f}% profitable ══")

        winning = sp.get("winning_patterns", [])
        if winning:
            lines.append("WINNING PATTERNS:")
            for p in winning[:5]:
                r   = p.get("avg_return_pct", 0)
                wr  = p.get("win_rate", 0)
                n   = p.get("sample_count", 0)
                lines.append(f"  ✓ {p['description']}  →  avg {r:+.1f}%, {wr:.0f}% win (n={n})")

        losing = sp.get("losing_patterns", [])
        if losing:
            lines.append("PATTERNS TO AVOID:")
            for p in losing[:3]:
                lines.append(f"  ✗ {p['description']}")

        notes = sp.get("market_notes", "")
        if notes:
            lines.append(f"MARKET NOTES: {notes}")

    # ── Global rules ──
    rules = g.get("rules", [])
    if rules:
        lines.append("GLOBAL RULES (all symbols):")
        for r in rules[:5]:
            conf = r.get("confidence", "")
            n    = r.get("samples", 0)
            tag  = f" [{conf}, n={n}]" if conf != "seed" else " [seed rule]"
            lines.append(f"  → {r['rule']}{tag}")

    # ── Symbol performance ranking ──
    perf = g.get("symbol_performance", {})
    if len(perf) >= 3:
        ranked = sorted(perf.items(), key=lambda x: x[1].get("avg_return", 0), reverse=True)
        lines.append("SYMBOL PERFORMANCE (avg return across sessions):")
        for sym, d in ranked[:5]:
            n   = d.get("sessions", 0)
            r   = d.get("avg_return", 0)
            p   = d.get("profitable", 0)
            lines.append(f"  {sym}: {r:+.1f}% avg, {p}/{n} profitable")

    # ── Interval notes ──
    inote = g.get("interval_notes", {}).get(interval, "")
    if inote:
        lines.append(f"INTERVAL NOTE ({interval}): {inote}")

    return "\n".join(lines)
