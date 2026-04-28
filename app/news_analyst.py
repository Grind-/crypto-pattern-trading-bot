"""
News Agent — central shared Claude instance for market intelligence.

Runs ONCE per hour as a background task, independent of how many users
are active. Results are shared across ALL users' Trading Agent calls.
No user credentials involved — always uses the platform proxy.

Data pipeline (all fetched in parallel):
  Phase 1 — structured APIs:
    Fear & Greed Index, RSS feeds (4 sources), CoinGecko trending
  Phase 2 — real internet research:
    Google News RSS search (3-5 targeted queries), Reddit r/CryptoCurrency

Output: knowledge/news/intelligence.json  (read by every Trading Agent call)

NOT responsible for:
  Technical indicator analysis, trade execution, simulation, per-user patterns.
  That is the Trading Agent's job.
"""
import asyncio
import json
import logging
import os
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import quote_plus

import httpx
from .utils import parse_json as _parse_json_util

logger = logging.getLogger(__name__)

INTELLIGENCE_FILE = "/app/knowledge/news/intelligence.json"
_PROXY_URL = os.environ.get("CLAUDE_PROXY_URL", "http://claude-proxy:8081")

_HEADERS = {"User-Agent": "CryptoPatternAI/2.0 (market research bot)"}

NEWS_AGENT_SYSTEM = (
    "You are a cryptocurrency market intelligence analyst (News Agent). "
    "Your sole responsibility is to analyse news, social sentiment, and market data "
    "to identify emerging opportunities and risks across Binance USDC trading pairs. "
    "You do NOT execute trades, run simulations, or evaluate technical indicators — "
    "that is the Trading Agent's job. "
    "Your output is read by the Trading Agent as real-time market context, so keep it "
    "concise, actionable, and focused on near-term (4–48h) catalysts."
)

_KNOWN_PAIRS = [
    "BTCUSDC", "ETHUSDC", "SOLUSDC", "BNBUSDC", "XRPUSDC",
    "DOGEUSDC", "ADAUSDC", "AVAXUSDC", "DOTUSDC", "LINKUSDC",
    "MATICUSDC", "LTCUSDC", "UNIUSDC", "ATOMUSDC", "NEARUSDC",
    "AAVEUSDC", "APTUSDC", "SUIUSDC", "PEPEUSDC", "SHIBUSDC",
]

_last_run: float = 0.0


# ── Internet research ─────────────────────────────────────────────────────────

async def _search_google_news(query: str, max_results: int = 8) -> list[str]:
    """Search Google News RSS — real internet results indexed by Google."""
    url = (
        f"https://news.google.com/rss/search"
        f"?q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"
    )
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True,
                                     headers=_HEADERS) as client:
            r = await client.get(url)
            r.raise_for_status()
        root = ET.fromstring(r.content)
        return [
            item.findtext("title", "").strip()
            for item in root.iter("item")
            if item.findtext("title", "").strip()
        ][:max_results]
    except Exception:
        return []


async def _fetch_reddit_crypto(limit: int = 20) -> list[str]:
    """Fetch hot posts from r/CryptoCurrency for community sentiment."""
    url = f"https://www.reddit.com/r/CryptoCurrency/hot.json?limit={limit}"
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True,
                                     headers=_HEADERS) as client:
            r = await client.get(url)
            r.raise_for_status()
        children = r.json()["data"]["children"]
        return [
            f"{c['data']['title']} [↑{c['data']['score']}]"
            for c in children
            if not c["data"].get("stickied")
        ][:15]
    except Exception:
        return []


async def _run_web_research(trending_coin_names: list[str]) -> dict:
    """
    Run multiple Google News searches and Reddit in parallel.
    Searches: broad market, BTC, ETH, plus up to 3 trending coins.
    Returns dict with deduplicated results per category.
    """
    search_queries = [
        ("market",   "crypto market news today"),
        ("bitcoin",  "bitcoin price news"),
        ("ethereum", "ethereum news"),
    ]
    for name in trending_coin_names[:3]:
        base = name.split("(")[0].strip()
        search_queries.append((base.lower(), f"{base} crypto news"))

    tasks = [_search_google_news(q, max_results=6) for _, q in search_queries]
    tasks.append(_fetch_reddit_crypto())

    results = await asyncio.gather(*tasks, return_exceptions=True)

    research: dict = {}
    seen: set[str] = set()

    for i, (label, _) in enumerate(search_queries):
        hits = results[i] if isinstance(results[i], list) else []
        unique = []
        for h in hits:
            key = h[:60].lower()
            if key not in seen:
                seen.add(key)
                unique.append(h)
        if unique:
            research[label] = unique

    reddit = results[-1]
    if isinstance(reddit, list) and reddit:
        research["reddit_hot"] = reddit

    return research


# ── CoinGecko trending ────────────────────────────────────────────────────────

async def _fetch_coingecko_trending() -> list[str]:
    try:
        async with httpx.AsyncClient(timeout=10, headers=_HEADERS) as client:
            r = await client.get("https://api.coingecko.com/api/v3/search/trending")
            r.raise_for_status()
        coins = r.json().get("coins", [])
        return [f"{c['item']['name']} ({c['item']['symbol'].upper()})" for c in coins[:10]]
    except Exception:
        return []


# ── Intelligence file I/O ─────────────────────────────────────────────────────

def _save_intelligence(data: dict) -> None:
    os.makedirs(os.path.dirname(INTELLIGENCE_FILE), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(INTELLIGENCE_FILE), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, INTELLIGENCE_FILE)
    except Exception:
        try:
            os.unlink(tmp)
        except Exception:
            pass
        raise


def get_news_intelligence() -> dict:
    """Return latest News Agent findings, or {} if not yet available."""
    try:
        with open(INTELLIGENCE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


# ── Claude caller (News Agent) ────────────────────────────────────────────────

def _parse_json(text: str):
    return _parse_json_util(text)


async def _call_news_claude(prompt: str, timeout: int = 120) -> dict:
    """Call Claude via the platform proxy with the News Agent system prompt."""
    async with httpx.AsyncClient(timeout=timeout + 30) as client:
        r = await client.post(
            f"{_PROXY_URL}/analyze",
            json={"system": NEWS_AGENT_SYSTEM, "prompt": prompt, "oauth_token": ""},
        )
        r.raise_for_status()
        data = r.json()
        text = data.get("raw_text", json.dumps(data))
    return _parse_json(text)


# ── Main cycle ────────────────────────────────────────────────────────────────

def _fmt_research(research: dict) -> str:
    if not research:
        return "No internet research available."
    lines = []
    labels = {
        "market":   "MARKET (Google News)",
        "bitcoin":  "BITCOIN (Google News)",
        "ethereum": "ETHEREUM (Google News)",
        "reddit_hot": "REDDIT r/CryptoCurrency (hot)",
    }
    for key, items in research.items():
        label = labels.get(key, f"{key.upper()} (Google News)")
        lines.append(f"{label}:")
        for item in items[:5]:
            lines.append(f"  • {item}")
    return "\n".join(lines)


async def run_news_cycle() -> dict:
    """
    Central News Agent cycle — runs hourly, shared across all users.
      1. Parallel fetch: Fear & Greed + RSS feeds + CoinGecko trending
      2. Internet research: Google News searches + Reddit hot posts
      3. Claude (News Agent) via platform proxy → structured intelligence
      4. Persist to knowledge/news/intelligence.json
    Returns the intelligence dict, or {} on failure. Never raises.
    """
    global _last_run

    if time.time() - _last_run < 2700:  # 45-min guard
        return get_news_intelligence()

    try:
        from .news_fetcher import _fetch_fear_greed, _all_headlines, fetch_whale_headlines

        # Phase 1: fast structured data
        fng, headlines, trending, whale = await asyncio.gather(
            _fetch_fear_greed(),
            _all_headlines(),
            _fetch_coingecko_trending(),
            fetch_whale_headlines(),
            return_exceptions=True,
        )

        # Phase 2: internet research (uses trending coin names for targeted searches)
        trending_names = trending if isinstance(trending, list) else []
        research = await _run_web_research(trending_names)

        # Format sections
        fng_text = ""
        if isinstance(fng, dict):
            fng_text = f"Fear & Greed Index: {fng.get('value', '?')}/100 ({fng.get('label', '?')})\n"

        headlines_text = "No RSS headlines."
        if isinstance(headlines, list) and headlines:
            headlines_text = "\n".join(f"• {h}" for h in headlines[:15])

        trending_text = "No trending data."
        if trending_names:
            trending_text = ", ".join(trending_names)

        research_text = _fmt_research(research)

        whale_headlines = whale if isinstance(whale, list) else []
        whale_text = (
            "\n".join(f"• {h}" for h in whale_headlines[:10])
            if whale_headlines
            else "No major whale transactions detected."
        )

        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        pairs_list = ", ".join(_KNOWN_PAIRS)

        prompt = f"""Analyze the current crypto market using the research below and identify actionable trading opportunities.

TIME: {now_str}
AVAILABLE BINANCE USDC PAIRS: {pairs_list}

── STRUCTURED DATA ──────────────────────────────
{fng_text}TRENDING ON COINGECKO (last 24h): {trending_text}

RSS FEED HEADLINES:
{headlines_text}

── ON-CHAIN DATA ──
{whale_text}

── INTERNET RESEARCH ────────────────────────────
{research_text}

Your task:
1. Synthesize ALL sources above — structured data AND internet research carry equal weight
2. Identify 2–5 USDC pairs with a concrete near-term catalyst (4–48h)
3. For EVERY significant news item (8–12 total), explicitly evaluate:
   - weight: "high" (breaks market direction), "medium" (relevant context), "low" (background noise)
   - signal: "bullish", "bearish", or "neutral"
   - affects_symbols: which USDC pairs are directly affected ([] if market-wide)
   - decision_impact: one concrete sentence — e.g. "Stärkt BUY-Signal für XRPUSDC", "Erhöht Hold-Druck bei offenen Long-Positionen", "Kein direkter Einfluss auf Spot-Trading"
   - reasoning: 1-2 sentences WHY this news shifts a trading signal (mechanism, not just restatement)
   - flows_into_decision: true if this item is actively passed to the Trading Agent, false if filtered out as noise
4. List key risks and warnings
5. Confidence ≥ 60% only; pairs from AVAILABLE BINANCE USDC PAIRS only

Respond with ONLY raw JSON:
{{
  "market_sentiment": "bullish",
  "fear_greed_value": 72,
  "fear_greed_label": "Greed",
  "top_opportunities": [
    {{
      "symbol": "SOLUSDC",
      "catalyst": "Short description of why this coin right now",
      "confidence": 75,
      "timeframe": "24h",
      "direction": "long",
      "source": "Google News / Reddit / RSS"
    }}
  ],
  "weighted_news": [
    {{
      "headline": "Exact headline or concise summary of the news item",
      "source": "RSS / Google News / Reddit",
      "weight": "high",
      "signal": "bullish",
      "affects_symbols": ["XRPUSDC"],
      "decision_impact": "Stärkt BUY-Signal für XRPUSDC aufgrund von Supply-Squeeze",
      "reasoning": "Exchange outflows historically precede price appreciation as circulating supply tightens. Combined with regulatory tailwind, this shifts the BUY threshold lower.",
      "flows_into_decision": true
    }}
  ],
  "market_sentiment_score": 55,
  "symbol_scores": {{
    "BTCUSDC": {{"sentiment_score": 72, "signal_modifier": 5, "veto": false, "reasoning": "..."}},
    "SOLUSDC": {{"sentiment_score": 30, "signal_modifier": -10, "veto": false, "reasoning": "..."}}
  }},
  "warnings": ["warning1", "warning2"],
  "key_headlines": ["most relevant headline 1", "most relevant headline 2", "most relevant headline 3"],
  "analysis": "2-3 sentence synthesis of market conditions based on all sources"
}}"""

        result = await _call_news_claude(prompt)
        if not isinstance(result, dict):
            return {}

        result["timestamp"] = datetime.now(timezone.utc).isoformat()
        result["trending_coins"] = trending_names
        result["sources_used"] = list(research.keys())
        _save_intelligence(result)
        _last_run = time.time()
        return result

    except Exception:
        return {}


# ── Context for Trading Agent ─────────────────────────────────────────────────

def get_news_context_for_trading(symbol: str) -> str:
    """
    Format News Agent intelligence as a compact block for Trading Agent prompts.
    Returns "" if no intelligence is available or it is stale (>6h).
    """
    intel = get_news_intelligence()
    if not intel:
        return ""

    ts = intel.get("timestamp", "")
    age_h = 0.0
    if ts:
        try:
            age_h = (datetime.now(timezone.utc) - datetime.fromisoformat(ts)).total_seconds() / 3600
        except Exception:
            pass
    if age_h > 6:
        return ""

    age_note = f" [{age_h:.0f}h ago]" if age_h >= 1 else ""
    sources = intel.get("sources_used", [])
    src_note = f" via {','.join(sources)}" if sources else ""
    lines = [f"══ NEWS INTELLIGENCE{age_note}{src_note} ══"]

    sentiment = intel.get("market_sentiment", "")
    fgv = intel.get("fear_greed_value", "")
    fgl = intel.get("fear_greed_label", "")
    if sentiment:
        lines.append(f"Market: {sentiment.upper()} | Fear&Greed: {fgv}/100 ({fgl})")

    opps = intel.get("top_opportunities", [])
    sym_opps = [o for o in opps if o.get("symbol", "").upper() == symbol.upper()]
    other_opps = [o for o in opps if o.get("symbol", "").upper() != symbol.upper()]

    for o in sym_opps:
        src = f" [{o.get('source', '')}]" if o.get("source") else ""
        lines.append(
            f"  ★ {symbol}: {o.get('catalyst', '')} "
            f"[{o.get('confidence', 0)}% conf, {o.get('timeframe', '')}]{src}"
        )

    if other_opps:
        lines.append(
            "Other opportunities: "
            + " | ".join(
                f"{o['symbol']} ({o.get('confidence', 0)}%)" for o in other_opps[:3]
            )
        )

    analysis = intel.get("analysis", "")
    if analysis:
        lines.append(f"Summary: {analysis}")

    # Weighted news that flows into decisions — symbol-specific first, then market-wide
    weighted = intel.get("weighted_news", [])
    active = [n for n in weighted if n.get("flows_into_decision") and n.get("weight") in ("high", "medium")]
    sym_news = [n for n in active if symbol.upper() in [s.upper() for s in n.get("affects_symbols", [])]]
    market_news = [n for n in active if not n.get("affects_symbols")]
    for n in (sym_news + market_news)[:5]:
        sig = {"bullish": "↑", "bearish": "↓", "neutral": "→"}.get(n.get("signal", ""), "")
        lines.append(f"  {sig}[{n.get('weight','').upper()}] {n.get('decision_impact', '')} — {n.get('reasoning', '')}")

    for w in intel.get("warnings", [])[:2]:
        lines.append(f"  ⚠ {w}")

    headlines = intel.get("key_headlines", [])
    if headlines:
        lines.append("Headlines: " + " | ".join(headlines[:3]))

    return "\n".join(lines)


def get_news_score_for_symbol(symbol: str) -> dict:
    """Return per-symbol sentiment score from cached news intelligence. No network I/O."""
    intel = get_news_intelligence()
    if not intel or not intel.get("symbol_scores"):
        return {"sentiment_score": 50, "signal_modifier": 0, "veto": False}
    scores = intel.get("symbol_scores", {})
    score = scores.get(symbol.upper(), scores.get(symbol, {}))
    if not score:
        return {"sentiment_score": 50, "signal_modifier": 0, "veto": False}
    return {
        "sentiment_score": int(score.get("sentiment_score", 50)),
        "signal_modifier": int(score.get("signal_modifier", 0)),
        "veto": bool(score.get("veto", False)),
    }
