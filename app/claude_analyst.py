import json
import os
from typing import List, Dict, Optional

import httpx

PROXY_URL = os.environ.get("CLAUDE_PROXY_URL", "http://claude-proxy:8081")
SYSTEM_TRADER = (
    "You are an expert quantitative cryptocurrency trader. "
    "Always respond with valid raw JSON only — no markdown, no code fences, no extra text."
)


def _format_data(candles: List[Dict], max_rows: int = 160) -> str:
    step = max(1, len(candles) // max_rows)
    sampled = candles[::step][:max_rows]

    rows = ["idx  | close    | rsi   | macd     | bb_pct | vol_x | ch4h%", "-" * 65]
    for i, entry in enumerate(sampled):
        orig_idx = i * step
        close = entry.get("close", 0)
        rsi = entry.get("rsi")
        macd = entry.get("macd")
        bb = entry.get("bb_pct")
        vol = entry.get("volume_ratio")
        ch4 = entry.get("change_4")

        def fmt(v, f):
            return format(v, f) if v is not None else "N/A"

        rows.append(
            f"{orig_idx:4d} | {close:8.2f} | {fmt(rsi, '5.1f')} | "
            f"{fmt(macd, '8.3f')} | {fmt(bb, '6.2f')} | "
            f"{fmt(vol, '5.2f')} | {fmt(ch4, '+.2f')}"
        )

    return "\n".join(rows)


async def _call_proxy(system: str, prompt: str, timeout: int = 270) -> Dict:
    async with httpx.AsyncClient(timeout=timeout + 30) as client:
        r = await client.post(
            f"{PROXY_URL}/analyze",
            json={"system": system, "prompt": prompt},
        )
        r.raise_for_status()
        data = r.json()

    # If proxy returned raw_text, try to extract JSON from it
    if "raw_text" in data and len(data) == 1:
        raw = data["raw_text"]
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start >= 0 and end > start:
            return json.loads(raw[start:end])
        raise ValueError(f"Unexpected proxy response: {raw[:300]}")

    return data


async def analyze_with_claude(
    symbol: str,
    interval: str,
    candles: List[Dict],
    feedback: Optional[Dict] = None,
) -> Dict:
    start_price = candles[0]["close"] if candles else 0
    end_price = candles[-1]["close"] if candles else 0
    period_pct = ((end_price - start_price) / start_price * 100) if start_price else 0

    data_str = _format_data(candles, max_rows=80)

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

    return await _call_proxy("", prompt, timeout=270)


async def get_live_signal(
    symbol: str,
    interval: str,
    candles: List[Dict],
    current_position: str,
    signal_history: Optional[List[Dict]] = None,
    strategy_name: str = "",
    strategy_analysis: str = "",
    strategy_patterns: Optional[List[str]] = None,
) -> Dict:
    data_str = _format_data(candles, max_rows=80)
    current_price = candles[-1]["close"] if candles else 0

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
{strategy_block}{history_block}CURRENT PRICE: ${current_price:.2f}
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
        return await _call_proxy("", prompt, timeout=60)
    except Exception:
        return {"action": "HOLD", "confidence": 0, "reason": "Proxy error", "stop_loss_pct": 2, "take_profit_pct": 4}
