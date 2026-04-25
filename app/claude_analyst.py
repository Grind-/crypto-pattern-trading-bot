import json
import os
from datetime import datetime, timezone
from typing import List, Dict, Optional

import httpx

from .knowledge_store import (
    get_knowledge_context, get_symbol_patterns, update_symbol_patterns,
    append_sim_log, update_global_stats, load_global_insights, save_global_insights,
)

PROXY_URL = os.environ.get("CLAUDE_PROXY_URL", "http://claude-proxy:8081")
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL = "claude-sonnet-4-6"


def _format_data(candles: List[Dict], max_rows: int = 80) -> str:
    step = max(1, len(candles) // max_rows)
    sampled = candles[::step][:max_rows]

    rows = ["idx  | close    | rsi   | macd     | bb_pct | vol_x | ch4h%", "-" * 65]
    for i, entry in enumerate(sampled):
        orig_idx = i * step
        close = entry.get("close", 0)
        rsi   = entry.get("rsi")
        macd  = entry.get("macd")
        bb    = entry.get("bb_pct")
        vol   = entry.get("volume_ratio")
        ch4   = entry.get("change_4")

        def fmt(v, f):
            return format(v, f) if v is not None else "N/A"

        rows.append(
            f"{orig_idx:4d} | {close:8.2f} | {fmt(rsi,'5.1f')} | "
            f"{fmt(macd,'8.3f')} | {fmt(bb,'6.2f')} | "
            f"{fmt(vol,'5.2f')} | {fmt(ch4,'+.2f')}"
        )
    return "\n".join(rows)


def _parse_json(text: str) -> Dict:
    start = text.find("{")
    end   = text.rfind("}") + 1
    if start >= 0 and end > start:
        return json.loads(text[start:end])
    raise ValueError(f"No JSON found in response: {text[:300]}")


# ── Transport ─────────────────────────────────────────────────────────────────

async def _call_proxy(prompt: str, timeout: int = 270,
                      oauth_token: str = "") -> Dict:
    async with httpx.AsyncClient(timeout=timeout + 30) as client:
        r = await client.post(
            f"{PROXY_URL}/analyze",
            json={"system": "", "prompt": prompt, "oauth_token": oauth_token},
        )
        r.raise_for_status()
        data = r.json()

    if "raw_text" in data and len(data) == 1:
        return _parse_json(data["raw_text"])
    return data


async def _call_api(prompt: str, api_key: str, timeout: int = 270) -> Dict:
    async with httpx.AsyncClient(timeout=timeout + 30) as client:
        r = await client.post(
            ANTHROPIC_API_URL,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": CLAUDE_MODEL,
                "max_tokens": 4096,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        r.raise_for_status()
        data = r.json()

    text = data["content"][0]["text"]
    return _parse_json(text)


async def _call_claude(prompt: str, api_key: Optional[str] = None,
                       oauth_token: str = "", timeout: int = 270) -> Dict:
    if api_key:
        return await _call_api(prompt, api_key, timeout)
    return await _call_proxy(prompt, timeout, oauth_token=oauth_token)


# ── Public API ────────────────────────────────────────────────────────────────

async def analyze_with_claude(
    symbol: str,
    interval: str,
    candles: List[Dict],
    feedback: Optional[Dict] = None,
    api_key: Optional[str] = None,
    oauth_token: str = "",
) -> Dict:
    start_price = candles[0]["close"] if candles else 0
    end_price   = candles[-1]["close"] if candles else 0
    period_pct  = ((end_price - start_price) / start_price * 100) if start_price else 0
    data_str    = _format_data(candles, max_rows=80)

    # ── Knowledge injection ──
    knowledge_block = ""
    ctx = get_knowledge_context(symbol, interval)
    if ctx:
        knowledge_block = f"\n{ctx}\n"

    # ── Feedback block ──
    feedback_block = ""
    if feedback:
        sample_trades = ""
        for t in feedback.get("trades", [])[:6]:
            sample_trades += (
                f"  • BUY@{t.get('buy_price','?')} idx {t.get('buy_index','?')} → "
                f"SELL@{t.get('sell_price','?')} idx {t.get('sell_index','?')} → "
                f"P&L: {t.get('pnl_pct','?'):+.2f}%\n"
            )
        feedback_block = f"""
⚠️ PREVIOUS ITERATION RESULT (needs improvement):
- Strategy: {feedback.get('strategy_name', 'Unknown')}
- Total return: {feedback.get('previous_return', 0):.2f}% (NOT YET PROFITABLE)
- Patterns tried: {', '.join(feedback.get('patterns_found', []))}
- Sample trades:
{sample_trades}
→ Analyze why these failed. Try DIFFERENT indicator thresholds, timing, or patterns.
"""

    prompt = f"""You are an expert quantitative cryptocurrency trader. Always respond with valid raw JSON only — no markdown, no code fences, no extra text.

Analyze this {symbol} {interval} market data and generate precise BUY/SELL trading signals.
{knowledge_block}
OVERVIEW:
- Symbol: {symbol} | Interval: {interval} | Candles: {len(candles)} (indices 0–{len(candles)-1})
- Start: ${start_price:.2f} → End: ${end_price:.2f} | Period change: {period_pct:+.2f}%

INDICATOR DATA (sampled, original indices):
{data_str}

INDICATOR GUIDE:
- RSI <30 = oversold (buy opportunity) | RSI >70 = overbought (sell opportunity)
- MACD positive + rising = bullish momentum | negative + falling = bearish
- bb_pct ~0 = price near lower band (oversold) | ~1 = near upper band (overbought)
- vol_x >1.5 = high volume (confirms move) | <0.5 = weak/fake move
- ch4h% = 4-candle momentum
{feedback_block}
RULES FOR YOUR SIGNALS:
1. Use ORIGINAL candle indices (0 to {len(candles)-1})
2. First signal MUST be BUY
3. Strictly alternate: BUY → SELL → BUY → SELL
4. Aim for 3–8 complete round trips
5. BUY when multiple indicators align bullish; SELL at exhaustion signs
6. Avoid trading in the last 10% of candles (insufficient exit data)
7. Apply the KNOWLEDGE BASE above — favour proven patterns, avoid known failures

Respond with ONLY raw JSON (no markdown, no code fences):
{{
  "strategy_name": "Short descriptive name",
  "analysis": "2-3 sentences on market structure and dominant pattern",
  "patterns_found": ["pattern1", "pattern2"],
  "signals": [
    {{"candle_index": 42, "action": "BUY", "reason": "RSI 27 oversold + MACD bullish crossover + high volume"}},
    {{"candle_index": 68, "action": "SELL", "reason": "RSI 74 overbought + price hit upper BB + momentum fading"}}
  ],
  "confidence": 70
}}"""

    return await _call_claude(prompt, api_key=api_key, oauth_token=oauth_token, timeout=270)


async def get_live_signal(
    symbol: str,
    interval: str,
    candles: List[Dict],
    current_position: str,
    signal_history: Optional[List[Dict]] = None,
    strategy_name: str = "",
    strategy_analysis: str = "",
    strategy_patterns: Optional[List[str]] = None,
    api_key: Optional[str] = None,
    oauth_token: str = "",
) -> Dict:
    data_str      = _format_data(candles, max_rows=80)
    current_price = candles[-1]["close"] if candles else 0

    # ── Knowledge injection ──
    knowledge_block = ""
    ctx = get_knowledge_context(symbol, interval)
    if ctx:
        knowledge_block = f"{ctx}\n\n"

    strategy_block = ""
    if strategy_name:
        patterns_str = ", ".join(strategy_patterns) if strategy_patterns else "—"
        strategy_block = f"""
BACKTESTING-STRATEGIE (als Kontext für diese Session):
- Strategie: {strategy_name}
- Analyse: {strategy_analysis}
- Muster: {patterns_str}

"""

    history_block = ""
    if signal_history:
        history_block = "EIGENE SIGNAL-HISTORIE DIESER SESSION (jüngste zuletzt):\n"
        for i, s in enumerate(signal_history):
            price_str = f"${s.get('price', 0):,.2f}" if s.get("price") else "?"
            history_block += (
                f"  {i+1}. {s.get('action','?')} @ {price_str} | "
                f"Konfidenz: {s.get('confidence', 0)}% | {s.get('reason', '')[:80]}\n"
            )
        history_block += "\n"

    prompt = f"""You are a live cryptocurrency trading AI. Respond with valid raw JSON only.

Analyze {symbol} {interval} data and give ONE trading signal.
{knowledge_block}{strategy_block}{history_block}CURRENT PRICE: ${current_price:.2f}
CURRENT POSITION: {current_position} (IN_POSITION = only SELL or HOLD; FLAT = only BUY or HOLD)

RECENT DATA (last {len(candles)} candles):
{data_str}

Respond with ONLY raw JSON:
{{
  "action": "BUY",
  "confidence": 75,
  "reason": "Brief explanation",
  "stop_loss_pct": 2.5,
  "take_profit_pct": 5.0
}}"""

    try:
        return await _call_claude(prompt, api_key=api_key, oauth_token=oauth_token, timeout=60)
    except Exception:
        return {"action": "HOLD", "confidence": 0, "reason": "Claude error",
                "stop_loss_pct": 2, "take_profit_pct": 4}


async def scan_market(symbol_summaries: List[Dict], interval: str,
                      api_key: Optional[str] = None, oauth_token: str = "") -> Dict:
    rows = ["Symbol      | Price       | 24h%   | 7d%    | ATR%  | RSI   | MACD  | Vol",
            "-" * 75]
    for s in symbol_summaries:
        rows.append(
            f"{s['symbol']:<11} | ${s['price']:>10,.2f} | {s['h24']:>+6.1f}% | {s['h7d']:>+6.1f}% | "
            f"{s['atr_pct']:>4.1f}% | {s['rsi']:>5.1f} | {'↑' if s['macd'] > 0 else '↓'}      | {s['vol_ratio']:.1f}x"
        )
    table = "\n".join(rows)

    # Inject historical symbol performance
    g = load_global_insights()
    perf = g.get("symbol_performance", {})
    perf_block = ""
    if len(perf) >= 2:
        ranked = sorted(perf.items(), key=lambda x: x[1].get("avg_return", 0), reverse=True)
        perf_block = "\nHISTORICAL PERFORMANCE (from past simulations):\n"
        for sym, d in ranked[:6]:
            n = d.get("sessions", 0)
            r = d.get("avg_return", 0)
            perf_block += f"  {sym}: avg {r:+.1f}% over {n} sessions\n"

    prompt = f"""You are a crypto market analyst. Identify the best USDC trading pair for live trading RIGHT NOW.

MARKET SNAPSHOT ({interval} candles, last 60 bars):
{table}
{perf_block}
Pick the top symbol based on:
- Clear directional momentum (strong trend, not choppy)
- Sufficient volatility (ATR% > 1.5% for short intervals, >0.5% for daily)
- Healthy volume (VolRatio > 1.0)
- RSI not at reversal extremes (avoid >78 or <22)
- Weight historical performance if available

Respond with ONLY raw JSON:
{{
  "best_symbol": "SOLUSDC",
  "ranking": [
    {{"symbol": "SOLUSDC", "score": 82, "reason": "Strong uptrend with rising volume and RSI in healthy zone"}},
    {{"symbol": "ETHUSDC", "score": 71, "reason": "Bullish momentum but RSI approaching overbought"}}
  ],
  "recommendation": "2 sentences: why best_symbol is the top pick right now and what to watch for."
}}"""

    try:
        return await _call_claude(prompt, api_key=api_key, oauth_token=oauth_token, timeout=90)
    except Exception as e:
        return {"best_symbol": "", "ranking": [], "recommendation": f"Scan fehlgeschlagen: {e}"}


async def test_connection(api_key: Optional[str] = None, oauth_token: str = "") -> bool:
    try:
        result = await _call_claude(
            'Respond with exactly: {"ok": true}',
            api_key=api_key, oauth_token=oauth_token, timeout=30,
        )
        return bool(result)
    except Exception:
        return False


# ── Learning ──────────────────────────────────────────────────────────────────

async def synthesize_learnings(
    symbol: str,
    interval: str,
    sim_entry: dict,
    api_key: Optional[str] = None,
    oauth_token: str = "",
) -> None:
    """
    After a simulation, ask Claude to update the knowledge base.
    Runs fire-and-forget — never raises.
    """
    profitable   = sim_entry.get("profitable", False)
    return_pct   = sim_entry.get("total_return_pct", 0)
    win_rate     = sim_entry.get("win_rate", 0)
    num_trades   = sim_entry.get("num_trades", 0)
    max_dd       = sim_entry.get("max_drawdown", 0)
    strategy     = sim_entry.get("strategy_name", "")
    patterns     = sim_entry.get("strategy_patterns", [])
    analysis     = (sim_entry.get("strategy_analysis", "") or "")[:500]
    iterations   = sim_entry.get("iterations", 1)

    # Always log, regardless of Claude synthesis outcome
    try:
        append_sim_log({
            "timestamp":     datetime.now(timezone.utc).isoformat(),
            "sim_id":        sim_entry.get("id", ""),
            "symbol":        symbol,
            "interval":      interval,
            "strategy_name": strategy,
            "patterns":      patterns,
            "return_pct":    return_pct,
            "win_rate":      win_rate,
            "num_trades":    num_trades,
            "max_drawdown":  max_dd,
            "profitable":    profitable,
            "iterations":    iterations,
        })
        update_global_stats(symbol, return_pct, profitable)
    except Exception:
        pass

    # Ask Claude to update the pattern library
    try:
        current   = get_symbol_patterns(symbol, interval)
        cur_json  = json.dumps(current, indent=2) if current else "{}"
        sc        = current.get("session_count", 0)
        ps        = current.get("profitable_sessions", 0)

        prompt = f"""You are a quantitative trading knowledge curator. Update the pattern library for {symbol} {interval}.

COMPLETED SIMULATION:
- Result: {"✅ PROFITABLE" if profitable else "❌ NOT PROFITABLE"}
- Return: {return_pct:+.2f}% | Win rate: {win_rate:.1f}% | Trades: {num_trades} | Max drawdown: {max_dd:.1f}%
- Strategy: {strategy}
- Patterns identified: {', '.join(patterns) if patterns else '—'}
- Analysis: {analysis}
- Iterations needed: {iterations}

CURRENT KNOWLEDGE BASE (update this):
{cur_json}

INSTRUCTIONS:
1. If PROFITABLE → add/strengthen a winning_pattern entry with this result
2. If NOT PROFITABLE → add/strengthen a losing_pattern entry noting what failed
3. Update running averages: new_avg = (old_avg * n + new_value) / (n + 1)
4. Set session_count = {sc + 1}, profitable_sessions = {ps + (1 if profitable else 0)}
5. Update market_notes if you see a pattern in the data worth noting
6. Keep descriptions ≤ 100 chars. Max {10} winning and {6} losing patterns — drop weakest if exceeded.
7. last_updated must be today's date (ISO-8601)

Respond with ONLY raw JSON (no markdown):
{{
  "session_count": {sc + 1},
  "profitable_sessions": {ps + (1 if profitable else 0)},
  "winning_patterns": [],
  "losing_patterns": [],
  "market_notes": "",
  "last_updated": "{datetime.now(timezone.utc).date().isoformat()}"
}}"""

        result = await _call_claude(prompt, api_key=api_key, oauth_token=oauth_token, timeout=90)

        if isinstance(result, dict) and "session_count" in result:
            result["last_updated"] = datetime.now(timezone.utc).isoformat()
            update_symbol_patterns(symbol, interval, result)

    except Exception:
        pass  # Never block the main flow


async def update_global_rules(
    api_key: Optional[str] = None,
    oauth_token: str = "",
) -> None:
    """
    Periodically distill cross-symbol rules from the sim_log.
    Called after every 10th simulation.
    """
    from .knowledge_store import _load, _SIM_LOG

    try:
        log     = _load(_SIM_LOG, {"entries": []})
        entries = log.get("entries", [])[:50]
        if len(entries) < 5:
            return

        g     = load_global_insights()
        rules = g.get("rules", [])

        summary_lines = []
        for e in entries:
            summary_lines.append(
                f"  {e['symbol']} {e['interval']}: {e['return_pct']:+.1f}% | "
                f"wr={e['win_rate']:.0f}% | patterns={','.join(e.get('patterns',[]))}"
            )
        summary = "\n".join(summary_lines)

        prompt = f"""You are a quantitative trading researcher. Distill cross-symbol patterns from recent simulation results.

RECENT SIMULATIONS (last {len(entries)}):
{summary}

CURRENT GLOBAL RULES:
{json.dumps(rules, indent=2)}

Identify 3–6 reliable rules that appear consistently across multiple simulations.
Update confidence from "seed" → "low"/"medium"/"high" based on sample counts.
Remove rules that appear to be contradicted by the data.

Respond ONLY with a JSON array of rule objects:
[
  {{"rule": "...", "confidence": "medium", "samples": 12}},
  ...
]"""

        result = await _call_claude(prompt, api_key=api_key, oauth_token=oauth_token, timeout=90)

        if isinstance(result, list):
            g["rules"] = result[:8]
            save_global_insights(g)
        elif isinstance(result, dict) and "rules" in result:
            g["rules"] = result["rules"][:8]
            save_global_insights(g)

    except Exception:
        pass
