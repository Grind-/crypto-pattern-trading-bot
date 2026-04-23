import hashlib
import hmac
import os
import time
from typing import Dict, Optional

import httpx

BINANCE_BASE = "https://api.binance.com"


def _sign(params: dict, secret: str) -> str:
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()


class BinanceTrader:
    def __init__(self, api_key: str, api_secret: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.headers = {"X-MBX-APIKEY": api_key}

    async def get_account(self) -> Dict:
        params = {"timestamp": int(time.time() * 1000), "recvWindow": 5000}
        params["signature"] = _sign(params, self.api_secret)
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{BINANCE_BASE}/api/v3/account", params=params, headers=self.headers)
            r.raise_for_status()
            return r.json()

    async def get_balances(self) -> Dict[str, float]:
        account = await self.get_account()
        return {
            b["asset"]: float(b["free"])
            for b in account.get("balances", [])
            if float(b["free"]) > 0
        }

    async def get_price(self, symbol: str) -> float:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{BINANCE_BASE}/api/v3/ticker/price", params={"symbol": symbol})
            r.raise_for_status()
            return float(r.json()["price"])

    async def place_market_order(self, symbol: str, side: str, quantity: Optional[float] = None, quote_quantity: Optional[float] = None) -> Dict:
        params = {
            "symbol": symbol,
            "side": side.upper(),
            "type": "MARKET",
            "timestamp": int(time.time() * 1000),
            "recvWindow": 5000,
        }
        if quote_quantity is not None:
            params["quoteOrderQty"] = f"{quote_quantity:.2f}"
        elif quantity is not None:
            params["quantity"] = f"{quantity:.6f}"
        else:
            raise ValueError("Either quantity or quote_quantity must be specified")

        params["signature"] = _sign(params, self.api_secret)
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(f"{BINANCE_BASE}/api/v3/order", params=params, headers=self.headers)
            r.raise_for_status()
            return r.json()

    async def get_open_orders(self, symbol: str) -> list:
        params = {"symbol": symbol, "timestamp": int(time.time() * 1000)}
        params["signature"] = _sign(params, self.api_secret)
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{BINANCE_BASE}/api/v3/openOrders", params=params, headers=self.headers)
            r.raise_for_status()
            return r.json()

    async def validate_keys(self) -> bool:
        try:
            await self.get_account()
            return True
        except Exception:
            return False
