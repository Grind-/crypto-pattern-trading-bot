"""
Market sentiment + news context for Claude prompts.
  - Fear & Greed Index (alternative.me) — no API key
  - RSS headlines (CoinTelegraph, CoinDesk)  — no API key
Both cached for 15 minutes so live-loop iterations don't hammer external services.
"""
import asyncio
import time
import xml.etree.ElementTree as ET
from typing import Optional

import httpx

_CACHE: dict = {}
_TTL = 900  # 15 minutes

_WHALE_ALERT_URL = "https://api.whale-alert.io/v1/transactions?api_key=demo&min_value=1000000&limit=20"
_MESSARI_RSS_URL = "https://messari.io/rss"

_SYMBOL_KEYWORDS: dict[str, list[str]] = {
    "BTC":  ["Bitcoin", "BTC"],
    "ETH":  ["Ethereum", "ETH"],
    "BNB":  ["BNB", "Binance"],
    "SOL":  ["Solana", "SOL"],
    "XRP":  ["XRP", "Ripple"],
    "ADA":  ["Cardano", "ADA"],
    "AVAX": ["Avalanche", "AVAX"],
    "DOGE": ["Dogecoin", "DOGE"],
    "DOT":  ["Polkadot", "DOT"],
    "LINK": ["Chainlink", "LINK"],
}

_CRYPTO_TERMS = {
    "crypto", "bitcoin", "ethereum", "blockchain", "defi",
    "altcoin", "market", "bull", "bear", "token", "coin",
    "exchange", "wallet", "halving", "regulation",
}

_RSS_FEEDS = [
    "https://cointelegraph.com/rss",
    "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "https://decrypt.co/feed",
    "https://cryptopotato.com/feed/",
    "https://messari.io/rss",
]


def _cached(key: str):
    e = _CACHE.get(key)
    if e and time.time() - e["ts"] < _TTL:
        return e["v"]
    return None


def _set_cache(key: str, value) -> None:
    _CACHE[key] = {"ts": time.time(), "v": value}


def _base(symbol: str) -> str:
    """'BTCUSDC' → 'BTC'"""
    for base in _SYMBOL_KEYWORDS:
        if symbol.startswith(base):
            return base
    return symbol[:3]


# ── Fetchers ──────────────────────────────────────────────────────────────────

async def _fetch_whale_alert() -> list:
    cached = _cached("whale")
    if cached is not None:
        return cached
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(_WHALE_ALERT_URL)
            r.raise_for_status()
        txns = r.json().get("transactions", [])
        result = []
        for tx in txns:
            try:
                headline = (
                    f"🐋 {tx['blockchain']} {tx['amount']:.0f} {tx['symbol']}"
                    f" (~${tx['amount_usd']/1e6:.1f}M)"
                    f" — {tx['from']['owner_type']} → {tx['to']['owner_type']}"
                )
                result.append({"headline": headline, **tx})
            except Exception:
                continue
        _set_cache("whale", result)
        return result
    except Exception:
        return []


async def _fetch_fear_greed() -> Optional[dict]:
    cached = _cached("fng")
    if cached is not None:
        return cached
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get("https://api.alternative.me/fng/")
            r.raise_for_status()
        entry = r.json()["data"][0]
        result = {"value": int(entry["value"]), "label": entry["value_classification"]}
        _set_cache("fng", result)
        return result
    except Exception:
        return None


async def _fetch_rss(url: str) -> list[str]:
    try:
        async with httpx.AsyncClient(timeout=8, follow_redirects=True,
                                     headers={"User-Agent": "CryptoPatternAI/1.0"}) as client:
            r = await client.get(url)
            r.raise_for_status()
        root = ET.fromstring(r.text)
        return [
            item.findtext("title", "").strip()
            for item in root.iter("item")
            if item.findtext("title", "").strip()
        ][:40]
    except Exception:
        return []


async def _all_headlines() -> list[str]:
    cached = _cached("rss")
    if cached is not None:
        return cached
    results = await asyncio.gather(*[_fetch_rss(u) for u in _RSS_FEEDS], return_exceptions=True)
    headlines: list[str] = []
    for r in results:
        if isinstance(r, list):
            headlines.extend(r)
    _set_cache("rss", headlines)
    return headlines


# ── Context builder ───────────────────────────────────────────────────────────

async def get_market_context(symbol: str) -> str:
    """
    Returns a compact, prompt-ready block with Fear & Greed + news.
    Never raises — returns "" on complete failure.
    Runs Fear & Greed and RSS fetch in parallel (cached after first call).
    """
    try:
        fng, headlines = await asyncio.gather(
            _fetch_fear_greed(),
            _all_headlines(),
            return_exceptions=True,
        )
    except Exception:
        return ""

    lines: list[str] = ["══ MARKET CONTEXT ══"]

    # Fear & Greed
    if isinstance(fng, dict):
        v, label = fng["value"], fng["label"]
        bar   = "█" * (v // 10) + "░" * (10 - v // 10)
        emoji = "🟢" if v >= 60 else "🔴" if v <= 30 else "🟡"
        lines.append(f"Fear & Greed: {v}/100 [{bar}] {label} {emoji}")
        if v >= 78:
            lines.append("  ⚠ Extreme Greed — elevated mean-reversion risk, avoid chasing breakouts")
        elif v <= 22:
            lines.append("  ⚠ Extreme Fear — potential capitulation zone, watch for reversal setups")

    # Filter headlines
    if isinstance(headlines, list) and headlines:
        base     = _base(symbol)
        keywords = _SYMBOL_KEYWORDS.get(base, [base])
        relevant, general = [], []
        for h in headlines:
            hl = h.lower()
            if any(kw.lower() in hl for kw in keywords):
                relevant.append(h)
            elif any(t in hl for t in _CRYPTO_TERMS):
                general.append(h)

        if relevant:
            lines.append(f"{base} NEWS:")
            for h in relevant[:4]:
                lines.append(f"  • {h}")
        if general:
            lines.append("CRYPTO MARKET NEWS:")
            for h in general[:3]:
                lines.append(f"  • {h}")

    if len(lines) == 1:  # only the header, nothing fetched
        return ""

    return "\n".join(lines)


# ── Public aliases for News Analyst ──────────────────────────────────────────

async def fetch_fear_greed() -> Optional[dict]:
    return await _fetch_fear_greed()


async def fetch_all_headlines() -> list[str]:
    return await _all_headlines()


async def fetch_whale_headlines() -> list[str]:
    txns = await _fetch_whale_alert()
    return [t["headline"] for t in txns if isinstance(t, dict) and "headline" in t]
