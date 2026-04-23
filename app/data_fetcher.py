import httpx
from typing import List, Dict

BINANCE_BASE = "https://api.binance.com"

INTERVAL_MINUTES = {
    "1m": 1, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "2h": 120, "4h": 240, "1d": 1440,
}

async def fetch_klines(symbol: str, interval: str, days: int) -> List[Dict]:
    minutes = INTERVAL_MINUTES.get(interval, 240)
    limit = min(int(days * 24 * 60 / minutes), 1000)

    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            f"{BINANCE_BASE}/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
        )
        r.raise_for_status()
        raw = r.json()

    return [
        {
            "timestamp": k[0],
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
        }
        for k in raw
    ]

async def fetch_latest_klines(symbol: str, interval: str, limit: int = 100) -> List[Dict]:
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(
            f"{BINANCE_BASE}/api/v3/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
        )
        r.raise_for_status()
        raw = r.json()

    return [
        {
            "timestamp": k[0],
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
        }
        for k in raw
    ]

async def get_available_symbols() -> List[str]:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(f"{BINANCE_BASE}/api/v3/ticker/price")
        r.raise_for_status()
        data = r.json()
    usdc_pairs = [d["symbol"] for d in data if d["symbol"].endswith("USDC")]
    priority = ["BTCUSDC", "ETHUSDC", "BNBUSDC", "SOLUSDC", "XRPUSDC", "ADAUSDC", "DOTUSDC", "MATICUSDC"]
    others = sorted([s for s in usdc_pairs if s not in priority])
    return priority + others[:50]
