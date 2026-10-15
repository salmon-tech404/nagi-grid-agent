"""Technical indicators on pandas OHLCV frames.

Copied from vibe-trading backend/src/services/market_analysis/indicators.py
(only the parts Nagi needs), not imported across repos.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def klines_to_df(raw_klines: list) -> pd.DataFrame:
    """Binance kline arrays -> DataFrame with float open/high/low/close/volume and UTC open_time."""
    df = pd.DataFrame(
        [row[:6] for row in raw_klines],
        columns=["open_time", "open", "high", "low", "close", "volume"],
    )
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = df[col].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"].astype("int64"), unit="ms", utc=True)
    return df.reset_index(drop=True)


def _wilder(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(alpha=1.0 / period, adjust=False).mean()


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift()
    return pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    return _wilder(true_range(df), period)


def efficiency_ratio(close: pd.Series, period: int = 10) -> pd.Series:
    """Kaufman efficiency ratio: net move / path length over `period` bars (0 = noise, 1 = straight line)."""
    change = (close - close.shift(period)).abs()
    path = close.diff().abs().rolling(period).sum()
    return (change / path.replace(0.0, np.nan)).fillna(0.0).clip(0.0, 1.0)
