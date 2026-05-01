import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, List, Dict, Optional

import httpx

from .news_fetcher import get_market_context
from .news_analyst import get_news_context_for_trading
from .knowledge_store import (
    MAX_WINNING,
    MAX_LOSING,
    get_knowledge_context,
    get_user_sym_patterns,
    update_user_patterns,
    append_user_sim_log,
    update_user_stats,
    load_all_user_sim_logs,
    aggregate_symbol_performance,
    promote_rules_to_core,
    write_merged_symbol_to_core,
    load_core,
)
from .utils import parse_json as _parse_json

logger = logging.getLogger(__name__)

PROXY_URL = os.environ.get("CLAUDE_PROXY_URL", "http://claude-proxy:8081")
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
CLAUDE_MODEL = "claude-sonnet-4-6"

TRADING_AGENT_SYSTEM = (
    "You are an expert quantitative cryptocurrency trading analyst (Trading Agent). "
    "Your responsibilities: analyse technical indicators and price action to identify "
    "high-probability trading patterns; generate precise BUY/SELL signals with clear "
    "reasoning; design, run, and evaluate backtesting simulations; execute live trades "
    "via the Binance API. "
    "You work alongside a separate News Agent that provides hourly market intelligence. "
    "When NEWS INTELLIGENCE is present in the prompt, treat it as verified real-time "
    "context and weight it alongside technical signals — it reflects catalysts that "
    "price action alone cannot yet show. "
    "Always respond with valid raw JSON only — no markdown, no code fences."
)

REGIME_AGENT_SYSTEM = (
    "You are a market regime classifier (Regime Agent). "
    "Detect the current market mode using ADX, multi-timeframe EMA, Fear & Greed, and price action. "
    "Always respond with valid raw JSON only — no markdown, no code fences."
)


def _format_data(candles: List[Dict], max_rows: int = 80) -> str:
    n = len(candles)
    step = max(1, n // max_rows)
    # Sample evenly then keep the LAST max_rows entries so the most recent
    # candles are always included and rolling-indicator NaN warmup rows
    # (first ~20) are dropped from the visible window.
    all_sampled = [(i, candles[i]) for i in range(0, n, step)]
    sampled_pairs = all_sampled[-max_rows:]

    rows = ["idx  | close    | rsi   | macd     | bb_pct | vol_x | ch4h%", "-" * 65]
    for orig_idx, entry in sampled_pairs:
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


# ── Regime Agent ──────────────────────────────────────────────────────────────

async def get_regime(
    symbol: str, interval: str,
    candles_1h: List[Dict], candles_4h: List[Dict],
    fear_greed: Optional[Dict] = None,
    api_key: Optional[str] = None, oauth_token: str = "",
) -> Dict:
    """Regime Agent — classify market mode. Never raises; returns fallback on any error."""
    _FALLBACK = {
        "regime": "RANGING", "strength": 50,
        "recommended_strategy": "mean_revert",
        "signal_weight_technical": 70, "signal_weight_news": 30,
    }
    try:
        c1h = candles_1h[-1] if candles_1h else {}
        c4h = candles_4h[-1] if candles_4h else {}

        ema_1h = "bullish" if (c1h.get("ema12") or 0) > (c1h.get("ema26") or 0) else "bearish"
        ema_4h = "bullish" if (c4h.get("ema12") or 0) > (c4h.get("ema26") or 0) else "bearish"

        fng_text = ""
        if isinstance(fear_greed, dict):
            fng_text = f"Fear & Greed: {fear_greed.get('value', '?')}/100 ({fear_greed.get('label', '?')})"

        def _f(v):
            return f"{v:.4f}" if v is not None else "N/A"

        prompt = (
            f"Classify the market regime for {symbol} ({interval}).\n\n"
            f"1h last candle: close={_f(c1h.get('close'))}, adx={_f(c1h.get('adx'))}, "
            f"ema12={_f(c1h.get('ema12'))}, ema26={_f(c1h.get('ema26'))}, "
            f"rsi={_f(c1h.get('rsi'))}, atr={_f(c1h.get('atr'))}, "
            f"rsi_bull_div={c1h.get('rsi_bull_div', False)}, rsi_bear_div={c1h.get('rsi_bear_div', False)}\n"
            f"4h last candle: close={_f(c4h.get('close'))}, ema12={_f(c4h.get('ema12'))}, "
            f"ema26={_f(c4h.get('ema26'))}, adx={_f(c4h.get('adx'))}\n\n"
            f"ADX interpretation: <25=no-trend, 25-50=trending, >50=strong\n"
            f"1h EMA: {ema_1h} (ema12 {'>' if ema_1h == 'bullish' else '<'} ema26)\n"
            f"4h EMA: {ema_4h} (ema12 {'>' if ema_4h == 'bullish' else '<'} ema26)\n"
            f"{fng_text}\n\n"
            'Respond ONLY with raw JSON:\n'
            '{"regime":"BULL_TREND|BEAR_TREND|RANGING|HIGH_VOLATILITY",'
            '"strength":0-100,"recommended_strategy":"trend_follow|mean_revert|stay_flat",'
            '"signal_weight_technical":70,"signal_weight_news":30}'
        )
        result = await _call_claude(
            prompt, api_key=api_key, oauth_token=oauth_token,
            timeout=45, system=REGIME_AGENT_SYSTEM,
        )
        if not isinstance(result, dict) or "regime" not in result:
            return _FALLBACK
        return result
    except Exception:
        return _FALLBACK


# ── Transport ─────────────────────────────────────────────────────────────────

async def _call_proxy(prompt: str, timeout: int = 270,
                      oauth_token: str = "",
                      system: str = TRADING_AGENT_SYSTEM) -> Dict:
    async with httpx.AsyncClient(timeout=timeout + 30) as client:
        r = await client.post(
            f"{PROXY_URL}/analyze",
            json={"system": system, "prompt": prompt, "oauth_token": oauth_token},
        )
        r.raise_for_status()
        data = r.json()

    if "raw_text" in data and len(data) == 1:
        return _parse_json(data["raw_text"])
    return data


async def _call_api(prompt: str, api_key: str, timeout: int = 270,
                    system: str = TRADING_AGENT_SYSTEM) -> Dict:
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
                "system": system,
                "messages": [{"role": "user", "content": prompt}],
            },
        )
        r.raise_for_status()
        data = r.json()

    text = data["content"][0]["text"]
    return _parse_json(text)


async def _call_claude(prompt: str, api_key: Optional[str] = None,
                       oauth_token: str = "", timeout: int = 270,
                       system: str = TRADING_AGENT_SYSTEM) -> Dict:
    if api_key:
        return await _call_api(prompt, api_key, timeout, system=system)
    return await _call_proxy(prompt, timeout, oauth_token=oauth_token, system=system)


# ── Public API ────────────────────────────────────────────────────────────────

async def analyze_with_claude(
    symbol: str,
    interval: str,
    candles: List[Dict],
    username: str = "",
    analysis_weight: int = 70,
    api_key: Optional[str] = None,
    oauth_token: str = "",
) -> Dict:
    analysis_weight = max(0, min(100, int(analysis_weight)))
    kb_weight = 100 - analysis_weight

    start_price = candles[0]["close"] if candles else 0
    end_price   = candles[-1]["close"] if candles else 0
    period_pct  = ((end_price - start_price) / start_price * 100) if start_price else 0
    data_str    = _format_data(candles, max_rows=80)

    knowledge_ctx, news_ctx = await asyncio.gather(
        asyncio.to_thread(get_knowledge_context, symbol, interval, username),
        get_market_context(symbol),
        return_exceptions=True,
    )
    news_intel = get_news_context_for_trading(symbol)
    knowledge_block = f"\n{knowledge_ctx}\n" if isinstance(knowledge_ctx, str) and knowledge_ctx else ""
    news_block = ""
    if isinstance(news_ctx, str) and news_ctx:
        news_block += f"\n{news_ctx}\n"
    if news_intel:
        news_block += f"\n{news_intel}\n"

    if kb_weight >= 80:
        mode_instruction = (
            "DECISION MODE: Knowledge-Base-Led (strict). "
            "The KNOWLEDGE BASE above is your primary signal source. "
            "Only generate signals that align with proven KB patterns. "
            "Market indicators serve as confirmation only — do not override KB guidance."
        )
    elif kb_weight >= 50:
        mode_instruction = (
            "DECISION MODE: Balanced. "
            "Weight Knowledge-Base patterns and current market indicators equally. "
            "KB patterns set the framework; indicator confluence refines entry/exit timing."
        )
    elif kb_weight >= 20:
        mode_instruction = (
            "DECISION MODE: Market-Led. "
            "Base signals primarily on current indicator analysis. "
            "Use KB patterns as confirmation context, not primary drivers."
        )
    else:
        mode_instruction = (
            "DECISION MODE: Pure Market Analysis. "
            "Base signals entirely on current technical indicators and price action. "
            "Knowledge Base is background context only."
        )

    prompt = f"""You are an expert quantitative cryptocurrency trader. Always respond with valid raw JSON only — no markdown, no code fences, no extra text.

Analyze this {symbol} {interval} market data and generate precise BUY/SELL trading signals.
{knowledge_block}{news_block}
{mode_instruction}

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

RULES FOR YOUR SIGNALS:
1. Use ORIGINAL candle indices (0 to {len(candles)-1})
2. First signal MUST be BUY
3. Strictly alternate: BUY → SELL → BUY → SELL
4. Aim for 3–8 complete round trips
5. BUY when multiple indicators align bullish; SELL at exhaustion signs
6. Avoid trading in the last 10% of candles (insufficient exit data)

Respond with ONLY raw JSON (no markdown, no code fences):
{{
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
    username: str = "",
    signal_history: Optional[List[Dict]] = None,
    trade_history: Optional[List[Dict]] = None,
    analysis_weight: int = 70,
    api_key: Optional[str] = None,
    oauth_token: str = "",
    regime: Optional[Dict] = None,
    news_score: Optional[Dict] = None,
    portfolio_context: Optional[str] = None,
    min_confidence: int = 55,
) -> Dict:
    analysis_weight = max(0, min(100, int(analysis_weight)))

    data_str      = _format_data(candles, max_rows=80)
    current_price = candles[-1]["close"] if candles else 0

    knowledge_ctx, news_ctx = await asyncio.gather(
        asyncio.to_thread(get_knowledge_context, symbol, interval, username),
        get_market_context(symbol),
        return_exceptions=True,
    )
    news_intel = get_news_context_for_trading(symbol)
    knowledge_block = f"{knowledge_ctx}\n\n" if isinstance(knowledge_ctx, str) and knowledge_ctx else ""
    news_block = ""
    if isinstance(news_ctx, str) and news_ctx:
        news_block += f"{news_ctx}\n\n"
    if news_intel:
        news_block += f"{news_intel}\n\n"

    # Weighting instruction based on knowledge base vs live market analysis
    kb_weight = 100 - analysis_weight
    if kb_weight >= 80:
        mode_instruction = (
            f"DECISION MODE: Knowledge-Base-Led ({kb_weight}% KB / {analysis_weight}% market analysis). "
            "Follow the patterns proven in your knowledge base strictly. "
            "Use current market analysis ONLY to veto in extreme risk situations."
        )
    elif kb_weight >= 50:
        mode_instruction = (
            f"DECISION MODE: Balanced ({kb_weight}% KB / {analysis_weight}% market analysis). "
            "Use the knowledge base as primary framework. "
            "Allow current market conditions to adjust timing or skip signals when clearly unfavorable."
        )
    elif kb_weight >= 20:
        mode_instruction = (
            f"DECISION MODE: Market-Led ({kb_weight}% KB / {analysis_weight}% market analysis). "
            "Base decisions primarily on current indicators. "
            "Use the knowledge base only to confirm — not to trigger — signals."
        )
    else:
        mode_instruction = (
            "DECISION MODE: Pure Market Analysis. "
            "Decide entirely based on current indicators, price action, and market conditions."
        )
    strategy_block = f"\n{mode_instruction}\n\n"

    regime_block = ""
    if regime:
        mc = max(50, min(95, int(min_confidence)))
        thresholds = {
            "BULL_TREND": f"BUY≥{mc}%, SELL≥{mc + 10}%",
            "RANGING": f"BUY≥{mc}%, SELL≥{mc + 5}%",
            "BEAR_TREND": f"BUY≥{mc + 15}%, SELL≥{mc}%",
            "HIGH_VOLATILITY": "No new BUY",
        }
        regime_block = (
            f"MARKT-REGIME: {regime.get('regime')} "
            f"(Stärke {regime.get('strength')}/100) | "
            f"Strategie: {regime.get('recommended_strategy')} | "
            f"Schwellen: {thresholds.get(regime.get('regime', 'RANGING'), f'BUY≥{mc}%,SELL≥{mc + 5}%')}\n\n"
        )

    news_extra = ""
    if news_score:
        sc = news_score.get("sentiment_score", 50)
        mod = news_score.get("signal_modifier", 0)
        veto = news_score.get("veto", False)
        news_extra = f"NEWS SENTIMENT: {sc}/100 (Threshold-Modifier: {mod:+d}%)"
        if veto:
            news_extra += " | *** VETO: Kein BUY ***"
        news_extra += "\n\n"

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

    trade_block = ""
    if trade_history:
        import time as _time
        now_ms = int(_time.time() * 1000)
        completed = [(b, s) for b, s in zip(trade_history, trade_history[1:])
                     if b.get("type") == "BUY" and s.get("type") == "SELL"]
        # rebuild correct pairs regardless of ordering
        pairs = []
        pending_buy = None
        for t in sorted(trade_history, key=lambda x: x.get("timestamp", 0)):
            if t.get("type") == "BUY":
                pending_buy = t
            elif t.get("type") == "SELL" and pending_buy:
                pairs.append((pending_buy, t))
                pending_buy = None

        if pairs or pending_buy:
            trade_block = "ABGESCHLOSSENE TRADES DIESER SESSION:\n"
            for buy, sell in pairs[-5:]:  # last 5 completed trades
                pnl = sell.get("pnl_pct")
                pnl_str = f"{pnl:+.2f}%" if pnl is not None else "?"
                trade_block += (
                    f"  BUY {buy.get('symbol','?')} @ ${buy.get('price',0):,.4f} → "
                    f"SELL @ ${sell.get('price',0):,.4f} | P&L: {pnl_str}\n"
                )
            if pending_buy:
                buy_price = pending_buy.get("price", 0)
                buy_sym   = pending_buy.get("symbol", symbol)
                hold_ms   = now_ms - pending_buy.get("timestamp", now_ms)
                hold_h    = round(hold_ms / 3_600_000, 1)
                unreal_pct = ((current_price / buy_price) - 1) * 100 if buy_price else 0
                trade_block += (
                    f"  OFFENE POSITION: {buy_sym} @ ${buy_price:,.4f} | "
                    f"Haltedauer: {hold_h}h | Unrealisiert: {unreal_pct:+.2f}%\n"
                )
            trade_block += "\n"

    portfolio_rebalancing_block = ""
    if portfolio_context:
        portfolio_rebalancing_block = f"PORTFOLIO REBALANCING:\n{portfolio_context}\n\n"

    prompt = f"""You are a live cryptocurrency trading AI. Respond with valid raw JSON only.

Analyze {symbol} {interval} data and give ONE trading signal.
{knowledge_block}{news_block}{strategy_block}{regime_block}{news_extra}{history_block}{trade_block}{portfolio_rebalancing_block}CURRENT PRICE: ${current_price:.2f}
CURRENT POSITION: {current_position} (IN_POSITION = SELL, PARTIAL_SELL or HOLD; FLAT = only BUY or HOLD)

RECENT DATA (last {len(candles)} candles):
{data_str}

INDICATOR GUIDE:
- RSI <30 = oversold (buy opportunity) | RSI >70 = overbought (sell opportunity)
- MACD positive + rising = bullish momentum | negative + falling = bearish
- bb_pct ~0 = price near lower band (oversold) | ~1 = near upper band (overbought)
- vol_x >1.5 = high volume (confirms move) | <0.5 = weak/fake move
- rsi_bull_div=True → bullische Divergenz (starkes BUY-Muster)
- rsi_bear_div=True → bärische Divergenz (starkes SELL-Muster)
- adx: <25=kein Trend, 25-50=Trend, >50=starker Trend

ACTIONS GUIDE:
- BUY: open new position (only when FLAT)
- SELL: close full position (only when IN_POSITION)
- PARTIAL_SELL: reduce position by sell_fraction (0.05–0.95); use to free capital for a stronger opportunity or to take partial profits (only when IN_POSITION)
- HOLD: no action

Respond with ONLY raw JSON:
{{
  "action": "BUY",
  "confidence": 75,
  "reason": "Brief explanation",
  "stop_loss_pct": 2.5,
  "take_profit_pct": 5.0,
  "sell_fraction": 0.0
}}"""

    try:
        return await _call_claude(prompt, api_key=api_key, oauth_token=oauth_token, timeout=60)
    except Exception:
        return {"action": "HOLD", "confidence": 0, "reason": "Claude error",
                "stop_loss_pct": 2, "take_profit_pct": 4, "sell_fraction": 0}


async def scan_market(symbol_summaries: List[Dict], interval: str,
                      username: str = "",
                      api_key: Optional[str] = None, oauth_token: str = "",
                      underdog_symbols: Optional[List[str]] = None) -> Dict:
    underdog_set = set(underdog_symbols or [])
    rows = ["Symbol      | Price       | 24h%   | 7d%    | ATR%  | RSI   | MACD  | Vol",
            "-" * 75]
    for s in symbol_summaries:
        tag = " *" if s["symbol"] in underdog_set else ""
        rows.append(
            f"{s['symbol']:<11} | ${s['price']:>10,.2f} | {s['h24']:>+6.1f}% | {s['h7d']:>+6.1f}% | "
            f"{s['atr_pct']:>4.1f}% | {s['rsi']:>5.1f} | {'↑' if s['macd'] > 0 else '↓'}      | {s['vol_ratio']:.1f}x{tag}"
        )
    table = "\n".join(rows)

    news_ctx = await get_market_context("BTC")  # global sentiment for scan
    news_intel = get_news_context_for_trading("BTC")
    news_block = ""
    if news_ctx:
        news_block += f"{news_ctx}\n\n"
    if news_intel:
        news_block += f"{news_intel}\n\n"

    perf = aggregate_symbol_performance()
    perf_block = ""
    if len(perf) >= 2:
        ranked = sorted(perf.items(), key=lambda x: x[1].get("avg_return", 0), reverse=True)
        perf_block = "\nHISTORICAL PERFORMANCE (aggregated across all users):\n"
        for sym, d in ranked[:6]:
            n = d.get("sessions", 0)
            r = d.get("avg_return", 0)
            perf_block += f"  {sym}: avg {r:+.1f}% over {n} sessions\n"

    underdog_note = ""
    if underdog_set:
        underdog_note = (
            f"\nUNDERDOG PICKS (marked * in table): {', '.join(sorted(underdog_set))} — "
            "these are less-followed pairs included as dark-horse candidates. "
            "Score them on indicators alone, without penalising them for lower market cap.\n"
        )

    prompt = f"""You are a crypto market analyst. Identify the best USDC trading pair for live trading RIGHT NOW.

{news_block}MARKET SNAPSHOT ({interval} candles, last 60 bars):
{table}
{underdog_note}{perf_block}
Pick the top symbol based on:
- Clear directional momentum (strong trend, not choppy)
- Sufficient volatility (ATR% > 1.5% for short intervals, >0.5% for daily)
- Healthy volume (VolRatio > 1.0)
- RSI not at reversal extremes (avoid >78 or <22)
- Weight historical performance if available
- Underdog picks (*) deserve equal consideration if their indicators are strong

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


# ── Learning (writes to users/{username}/ only) ───────────────────────────────

async def synthesize_learnings(
    symbol: str,
    interval: str,
    sim_entry: dict,
    username: str,
    api_key: Optional[str] = None,
    oauth_token: str = "",
) -> tuple[bool, str]:
    """Update user's knowledge base after a simulation.
    Returns (success, message) — never raises."""
    profitable = sim_entry.get("profitable", False)
    return_pct = sim_entry.get("total_return_pct", 0)
    win_rate   = sim_entry.get("win_rate", 0)
    num_trades = sim_entry.get("num_trades", 0)
    max_dd     = sim_entry.get("max_drawdown", 0)
    patterns   = sim_entry.get("patterns_found", sim_entry.get("strategy_patterns", []))
    analysis   = (sim_entry.get("analysis", sim_entry.get("strategy_analysis", "")) or "")[:500]

    try:
        append_user_sim_log(username, {
            "timestamp":  datetime.now(timezone.utc).isoformat(),
            "sim_id":     sim_entry.get("id", ""),
            "symbol":     symbol,
            "interval":   interval,
            "patterns":   patterns,
            "return_pct": return_pct,
            "win_rate":   win_rate,
            "num_trades": num_trades,
            "max_drawdown": max_dd,
            "profitable": profitable,
        })
        update_user_stats(username, symbol, return_pct, profitable)
    except Exception as e:
        return False, f"Sim-Log Fehler: {e}"

    current  = get_user_sym_patterns(username, symbol, interval)
    cur_json = json.dumps(current, indent=2) if current else "{}"
    sc       = current.get("session_count", 0)
    ps       = current.get("profitable_sessions", 0)

    prompt = f"""You are a quantitative trading knowledge curator. Update the pattern library for {symbol} {interval}.

COMPLETED SIMULATION:
- Result: {"✅ PROFITABLE" if profitable else "❌ NOT PROFITABLE"}
- Return: {return_pct:+.2f}% | Win rate: {win_rate:.1f}% | Trades: {num_trades} | Max drawdown: {max_dd:.1f}%
- Patterns identified: {', '.join(patterns) if patterns else '—'}
- Analysis: {analysis}

CURRENT KNOWLEDGE BASE (update this):
{cur_json}

INSTRUCTIONS:
1. If PROFITABLE → add/strengthen a winning_pattern entry with this result
2. If NOT PROFITABLE → add/strengthen a losing_pattern entry noting what failed
3. Update running averages: new_avg = (old_avg * n + new_value) / (n + 1)
4. Set session_count = {sc + 1}, profitable_sessions = {ps + (1 if profitable else 0)}
5. Update market_notes if you see a pattern worth noting
6. Keep descriptions ≤ 100 chars. Max {MAX_WINNING} winning and {MAX_LOSING} losing patterns — drop weakest if exceeded.
7. last_updated must be today's ISO-8601 date

Respond with ONLY raw JSON (no markdown):
{{
  "session_count": {sc + 1},
  "profitable_sessions": {ps + (1 if profitable else 0)},
  "winning_patterns": [],
  "losing_patterns": [],
  "market_notes": "",
  "last_updated": "{datetime.now(timezone.utc).date().isoformat()}"
}}"""

    for attempt in range(2):
        try:
            result = await _call_claude(prompt, api_key=api_key, oauth_token=oauth_token, timeout=90)
            if isinstance(result, dict) and "session_count" in result:
                result["last_updated"] = datetime.now(timezone.utc).isoformat()
                update_user_patterns(username, symbol, interval, result)
                total = result["session_count"]
                wins  = result.get("profitable_sessions", 0)
                wp    = len(result.get("winning_patterns", []))
                lp    = len(result.get("losing_patterns", []))
                return True, (
                    f"Wissensbasis aktualisiert — {symbol} {interval}: "
                    f"{total} Sessions ({wins} profitabel), "
                    f"{wp} Gewinnmuster, {lp} Verlustmuster"
                )
            return False, "Claude-Antwort hatte unerwartetes Format — Wissensbasis nicht aktualisiert"
        except Exception as e:
            if attempt == 0:
                await asyncio.sleep(5)
                continue
            return False, f"Claude-Fehler nach 2 Versuchen: {e}"

    return False, "Unbekannter Fehler"


async def synthesize_community_patterns(
    symbol: str,
    interval: str,
    api_key: Optional[str] = None,
    oauth_token: str = "",
) -> tuple[bool, str]:
    """Build community knowledge from all users' data for symbol+interval.
    Returns (success, message) — never raises."""
    from .knowledge_store import (get_all_user_data_for_symbol,
                                   save_community_patterns, MIN_USERS_FOR_COMMUNITY)

    user_data = get_all_user_data_for_symbol(symbol, interval)
    if len(user_data) < MIN_USERS_FOR_COMMUNITY:
        return False, f"Community-Update übersprungen: nur {len(user_data)} User mit Daten (Minimum {MIN_USERS_FOR_COMMUNITY})"

    n = len(user_data)
    total_sessions  = sum(u["sim_sessions"] for u in user_data)
    total_profitable = sum(u["sim_profitable"] for u in user_data)
    live_total      = sum(u["live_trades"] for u in user_data)

    user_lines = []
    for u in user_data:
        line = (
            f"  Trader (anonym): {u['sim_sessions']} Sim-Sessions, "
            f"avg Return {u['avg_return_pct']:+.1f}%, "
            f"avg Win-Rate {u['avg_win_rate']:.0f}%"
        )
        if u["winning_patterns"]:
            line += f"\n    ✓ Patterns: {', '.join(u['winning_patterns'][:3])}"
        if u["losing_patterns"]:
            line += f"\n    ✗ Vermeiden: {', '.join(u['losing_patterns'][:2])}"
        if u["live_avg_pnl"] is not None:
            line += f"\n    Live-Trades: {u['live_trades']} ({u['live_profitable']} profitabel, avg {u['live_avg_pnl']:+.2f}%)"
        if u["market_notes"]:
            line += f"\n    Notiz: {u['market_notes'][:120]}"
        user_lines.append(line)

    prompt = f"""You are a quantitative trading research analyst. Synthesize anonymized trading data from {n} independent traders for {symbol} {interval} into community consensus patterns.

TRADER DATA (anonymized — {n} traders, {total_sessions} simulation sessions, {live_total} live trades):
{chr(10).join(user_lines)}

TASK: Find patterns that appear confirmed by MULTIPLE traders independently.
- consensus_patterns: Strategies/conditions that multiple traders found profitable
- consensus_avoid: Conditions that multiple traders found unprofitable or risky
- community_notes: 1-2 sentences summarizing the overall community edge for this symbol/interval

Rules:
- Only include a pattern in consensus_patterns if confirmed by ≥2 traders
- Keep descriptions ≤ 100 chars
- Max 5 consensus_patterns, max 3 consensus_avoid
- contributing_traders reflects how many traders confirmed each pattern

Respond ONLY with raw JSON:
{{
  "contributing_users": {n},
  "total_sessions": {total_sessions},
  "profitable_sessions": {total_profitable},
  "consensus_patterns": [
    {{"description": "...", "contributing_traders": 2, "avg_return_pct": 0.0}}
  ],
  "consensus_avoid": [
    {{"description": "...", "contributing_traders": 2}}
  ],
  "community_notes": "..."
}}"""

    for attempt in range(2):
        try:
            result = await _call_claude(prompt, api_key=api_key, oauth_token=oauth_token, timeout=90)
            if isinstance(result, dict) and "contributing_users" in result:
                result["contributing_users"] = n
                result["total_sessions"]     = total_sessions
                result["profitable_sessions"] = total_profitable
                save_community_patterns(symbol, interval, result)
                cp = len(result.get("consensus_patterns", []))
                ca = len(result.get("consensus_avoid", []))
                return True, (
                    f"Community-Wissensbasis aktualisiert — {symbol} {interval}: "
                    f"{n} Trader, {total_sessions} Sessions, "
                    f"{cp} Konsens-Muster, {ca} Vermeiden-Muster"
                )
            return False, "Community-Synthese: unerwartetes Antwortformat"
        except Exception as e:
            if attempt == 0:
                await asyncio.sleep(5)
                continue
            return False, f"Community-Synthese fehlgeschlagen nach 2 Versuchen: {e}"

    return False, "Unbekannter Fehler bei Community-Synthese"


async def distill_and_promote_rules(
    api_key: Optional[str] = None,
    oauth_token: str = "",
) -> list:
    """
    Admin-triggered: Claude reads all users' sim logs, distills global rules,
    and writes them to core/patterns.json.
    Returns the new rule list, or [] on failure.
    """
    try:
        entries = load_all_user_sim_logs(limit=60)
        if len(entries) < 5:
            return []

        core  = load_core()
        rules = core.get("global_rules", [])

        summary_lines = []
        for e in entries:
            summary_lines.append(
                f"  [{e.get('_user','?')}] {e['symbol']} {e['interval']}: "
                f"{e['return_pct']:+.1f}% | wr={e['win_rate']:.0f}% | "
                f"patterns={','.join(e.get('patterns', []))}"
            )
        summary = "\n".join(summary_lines)

        prompt = f"""You are a quantitative trading researcher. Distill cross-symbol, cross-user patterns from recent simulation results.

RECENT SIMULATIONS (last {len(entries)}):
{summary}

CURRENT GLOBAL RULES:
{json.dumps(rules, indent=2)}

Identify 3–6 reliable rules that appear consistently across multiple simulations and users.
Update confidence from "seed" → "low"/"medium"/"high" based on sample counts.
Remove rules that appear contradicted by the data.

Respond ONLY with a JSON array of rule objects:
[
  {{"rule": "...", "confidence": "medium", "samples": 12}},
  ...
]"""

        result = await _call_claude(prompt, api_key=api_key, oauth_token=oauth_token, timeout=90)

        new_rules: list = []
        if isinstance(result, list):
            new_rules = result
        elif isinstance(result, dict) and "rules" in result:
            new_rules = result["rules"]

        if new_rules:
            promote_rules_to_core(new_rules[:8])
            return new_rules
        return []
    except Exception:
        return []


async def promote_symbol_patterns_via_claude(
    username: str,
    symbol: str,
    interval: str,
    api_key: Optional[str] = None,
    oauth_token: str = "",
) -> bool:
    """
    Admin-triggered: Claude merges user's symbol patterns with existing core patterns,
    then writes the result to core/patterns.json.
    Returns True on success.
    """
    try:
        user_data = get_user_sym_patterns(username, symbol, interval)
        if not user_data:
            return False

        core = load_core()
        core_data = core.get("symbol_patterns", {}).get(symbol, {}).get(interval, {})

        prompt = f"""You are a quantitative trading knowledge curator. Merge user-contributed patterns into the core knowledge base.

USER PATTERNS (to be promoted — verified through simulations):
{json.dumps(user_data, indent=2)}

EXISTING CORE PATTERNS (community-verified):
{json.dumps(core_data, indent=2) if core_data else "{}"}

TASK: Merge these patterns intelligently:
1. Keep winning patterns from both; drop weakest if >10 total
2. Keep losing patterns from both; drop weakest if >6 total
3. Update session_count and profitable_sessions by summing both
4. Recalculate avg_return_pct as weighted average
5. Write a concise market_notes combining key insights
6. Set last_updated to today's date

Respond with ONLY raw JSON — the merged pattern object:
{{
  "session_count": 0,
  "profitable_sessions": 0,
  "winning_patterns": [],
  "losing_patterns": [],
  "market_notes": "",
  "last_updated": "{datetime.now(timezone.utc).date().isoformat()}"
}}"""

        result = await _call_claude(prompt, api_key=api_key, oauth_token=oauth_token, timeout=90)
        if not isinstance(result, dict) or "session_count" not in result:
            return False

        result["last_updated"] = datetime.now(timezone.utc).isoformat()
        write_merged_symbol_to_core(symbol, interval, result)
        return True
    except Exception:
        return False
