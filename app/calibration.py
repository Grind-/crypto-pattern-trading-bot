"""
Adaptive threshold calibration.

After each completed trade cycle (BUY → SELL) the bot logs the voting score
and regime that led to the BUY decision.  Once enough paired trades exist,
this module finds the per-regime total_score threshold that maximises
win_rate × trade_coverage — i.e. the cutoff above which trades tend to be
profitable while still allowing enough trades through.
"""

from collections import defaultdict
from typing import Optional

_DEFAULTS: dict[str, float] = {
    "BULL_TREND":      0.8,
    "RANGING":         1.0,
    "BEAR_TREND":      1.2,
    "HIGH_VOLATILITY": 999.0,
}

# Absolute bounds — calibrated thresholds are clamped to this range
_MIN_THRESHOLD = 0.3
_MAX_THRESHOLD = 2.5

# Minimum completed trades (per regime) before the default is overridden
MIN_SAMPLES_PER_REGIME = 5

# Minimum total completed trades before any calibration happens at all
MIN_TOTAL_SAMPLES = 8


def calibrate_thresholds(trade_history: list) -> dict:
    """
    Returns a dict {regime: threshold} for regimes that have enough data.
    Regimes with fewer than MIN_SAMPLES_PER_REGIME completed trades are
    omitted — the caller falls back to _DEFAULTS for those.

    trade_history entries look like:
      BUY:  {"type":"BUY",  "voting_score": float, "voting_regime": str, ...}
      SELL: {"type":"SELL", "pnl_pct": float, ...}
    """
    paired = _pair_trades(trade_history)
    if len(paired) < MIN_TOTAL_SAMPLES:
        return {}

    by_regime: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for p in paired:
        by_regime[p["regime"]].append((p["score"], p["pnl"]))

    result = {}
    for regime, samples in by_regime.items():
        if len(samples) < MIN_SAMPLES_PER_REGIME:
            continue
        default = _DEFAULTS.get(regime, 1.0)
        result[regime] = _find_threshold(samples, default)

    return result


def calibration_meta(trade_history: list) -> dict:
    """
    Returns metadata for the UI:
      {
        "paired_trades": int,          # total completed BUY→SELL pairs with score data
        "by_regime": {                 # per-regime breakdown
          "<regime>": {"samples": int, "win_rate": float, "threshold": float}
        },
        "active": bool,               # True when calibration is applied
        "defaults": dict,             # _DEFAULTS reference
      }
    """
    paired = _pair_trades(trade_history)
    by_regime: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for p in paired:
        by_regime[p["regime"]].append((p["score"], p["pnl"]))

    regime_info = {}
    active = False
    for regime, samples in by_regime.items():
        wins = sum(1 for _, pnl in samples if pnl > 0)
        wr = round(wins / len(samples) * 100, 1) if samples else 0.0
        if len(samples) >= MIN_SAMPLES_PER_REGIME:
            default = _DEFAULTS.get(regime, 1.0)
            threshold = _find_threshold(samples, default)
            active = True
        else:
            threshold = None
        regime_info[regime] = {
            "samples":   len(samples),
            "win_rate":  wr,
            "threshold": threshold,
        }

    return {
        "paired_trades": len(paired),
        "by_regime":     regime_info,
        "active":        active,
        "defaults":      dict(_DEFAULTS),
        "min_samples":   MIN_SAMPLES_PER_REGIME,
        "min_total":     MIN_TOTAL_SAMPLES,
    }


# ── internal ──────────────────────────────────────────────────────────────────

def _pair_trades(trade_history: list) -> list[dict]:
    """
    Pair each BUY (that has voting_score) with its subsequent SELL (that has
    pnl_pct).  Returns list of {regime, score, pnl} dicts.
    """
    sorted_trades = sorted(trade_history, key=lambda x: x.get("timestamp", 0))
    pairs = []
    pending: Optional[dict] = None

    for t in sorted_trades:
        typ = t.get("type", "").upper()
        if typ == "BUY" and t.get("voting_score") is not None:
            pending = t
        elif typ == "SELL" and t.get("pnl_pct") is not None and pending is not None:
            pairs.append({
                "regime": pending.get("voting_regime", "RANGING"),
                "score":  pending["voting_score"],
                "pnl":    t["pnl_pct"],
            })
            pending = None

    return pairs


def _find_threshold(samples: list[tuple[float, float]], default: float) -> float:
    """
    Scan all distinct score values as candidate thresholds.
    For each threshold t: compute win_rate among trades with score >= t,
    weighted by coverage (fraction of all trades still included).
    Pick the t that maximises win_rate × coverage, then clamp to bounds.
    """
    all_scores = sorted(set(s for s, _ in samples))
    best_metric = -1.0
    best_t = default
    n = len(samples)

    for t in all_scores:
        above = [(s, p) for s, p in samples if s >= t]
        if len(above) < 2:
            continue
        wins = sum(1 for _, p in above if p > 0)
        win_rate = wins / len(above)
        coverage = len(above) / n
        metric = win_rate * coverage
        if metric > best_metric:
            best_metric = metric
            best_t = t

    return round(max(_MIN_THRESHOLD, min(_MAX_THRESHOLD, best_t)), 3)
