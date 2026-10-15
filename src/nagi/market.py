"""Market data for the regime gate: Binance public klines (no API key)."""

from __future__ import annotations

import httpx
import pandas as pd

from nagi.indicators import klines_to_df

KLINES_URL = "https://api.binance.com/api/v3/klines"


async def fetch_klines(symbol: str = "BNBUSDT", interval: str = "1h", limit: int = 100) -> pd.DataFrame:
    async with httpx.AsyncClient(timeout=10) as http:
        resp = await http.get(KLINES_URL, params={"symbol": symbol, "interval": interval, "limit": limit})
        resp.raise_for_status()
        return klines_to_df(resp.json())
