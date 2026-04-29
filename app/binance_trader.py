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

    async def get_asset_balance(self, asset: str) -> float:
        balances = await self.get_balances()
        return balances.get(asset.upper(), 0.0)

    async def get_price(self, symbol: str) -> float:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{BINANCE_BASE}/api/v3/ticker/price", params={"symbol": symbol})
            r.raise_for_status()
            return float(r.json()["price"])

    async def symbol_exists(self, symbol: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(f"{BINANCE_BASE}/api/v3/ticker/price", params={"symbol": symbol})
                return r.is_success
        except Exception:
            return False

    async def place_market_order(self, symbol: str, side: str, quantity: Optional[float] = None,
                                  quote_quantity: Optional[float] = None, qty_precision: int = 6) -> Dict:
        params = {
            "symbol": symbol,
            "side": side.upper(),
            "type": "MARKET",
            "timestamp": int(time.time() * 1000),
            "recvWindow": 5000,
        }
        if quote_quantity is not None:
            params["quoteOrderQty"] = f"{quote_quantity:.8f}".rstrip("0").rstrip(".")
        elif quantity is not None:
            params["quantity"] = f"{quantity:.{qty_precision}f}"
        else:
            raise ValueError("Either quantity or quote_quantity must be specified")

        params["signature"] = _sign(params, self.api_secret)
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(f"{BINANCE_BASE}/api/v3/order", params=params, headers=self.headers)
            if not r.is_success:
                try:
                    err_body = r.json()
                    raise ValueError(f"Binance {r.status_code}: code={err_body.get('code')} msg={err_body.get('msg')}")
                except (ValueError, KeyError):
                    raise ValueError(f"Binance {r.status_code}: {r.text[:200]}")
            return r.json()

    async def get_open_orders(self, symbol: str) -> list:
        params = {"symbol": symbol, "timestamp": int(time.time() * 1000)}
        params["signature"] = _sign(params, self.api_secret)
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{BINANCE_BASE}/api/v3/openOrders", params=params, headers=self.headers)
            r.raise_for_status()
            return r.json()

    async def get_lot_step(self, symbol: str) -> float:
        """Return the LOT_SIZE stepSize for a symbol (e.g. 0.00001 for BTC)."""
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{BINANCE_BASE}/api/v3/exchangeInfo", params={"symbol": symbol})
            r.raise_for_status()
            info = r.json()
        for s in info.get("symbols", []):
            if s["symbol"] == symbol:
                for f in s.get("filters", []):
                    if f["filterType"] == "LOT_SIZE":
                        return float(f["stepSize"])
        return 1e-8  # safe fallback

    async def get_my_trades(self, symbol: str, limit: int = 10) -> list:
        params = {"symbol": symbol, "limit": limit, "timestamp": int(time.time() * 1000)}
        params["signature"] = _sign(params, self.api_secret)
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{BINANCE_BASE}/api/v3/myTrades", params=params, headers=self.headers)
            r.raise_for_status()
            return r.json()

    async def validate_keys(self) -> bool:
        try:
            await self.get_account()
            return True
        except Exception:
            return False
