import pandas as pd
import numpy as np
from typing import List, Dict


def compute_indicators(candles: List[Dict]) -> List[Dict]:
    df = pd.DataFrame(candles)
    closes = df["close"]
    highs = df["high"]
    lows = df["low"]
    volumes = df["volume"]

    # RSI
    df["rsi"] = _rsi(closes, 14)

    # MACD
    ema12 = closes.ewm(span=12, adjust=False).mean()
    ema26 = closes.ewm(span=26, adjust=False).mean()
    df["macd"] = ema12 - ema26
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["macd_hist"] = df["macd"] - df["macd_signal"]

    # Bollinger Bands
    sma20 = closes.rolling(20).mean()
    std20 = closes.rolling(20).std()
    df["bb_upper"] = sma20 + 2 * std20
    df["bb_lower"] = sma20 - 2 * std20
    bb_range = df["bb_upper"] - df["bb_lower"]
    df["bb_pct"] = (closes - df["bb_lower"]) / bb_range.replace(0, np.nan)

    # Moving averages
    df["sma20"] = sma20
    df["sma50"] = closes.rolling(50).mean()
    df["ema12"] = ema12
    df["ema26"] = ema26

    # Volume
    vol_sma = volumes.rolling(20).mean()
    df["volume_ratio"] = volumes / vol_sma.replace(0, np.nan)

    # Price changes
    df["change_1"] = closes.pct_change(1) * 100
    df["change_4"] = closes.pct_change(4) * 100
    df["change_12"] = closes.pct_change(12) * 100

    # ATR
    tr = pd.concat([
        highs - lows,
        (highs - closes.shift(1)).abs(),
        (lows - closes.shift(1)).abs(),
    ], axis=1).max(axis=1)
    df["atr"] = tr.rolling(14).mean()

    # Stochastic RSI
    rsi = df["rsi"]
    rsi_min = rsi.rolling(14).min()
    rsi_max = rsi.rolling(14).max()
    rsi_range = (rsi_max - rsi_min).replace(0, np.nan)
    df["stoch_rsi"] = (rsi - rsi_min) / rsi_range

    df = df.where(pd.notna(df), None)
    return df.to_dict("records")


def _rsi(prices: pd.Series, period: int = 14) -> pd.Series:
    delta = prices.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(com=period - 1, adjust=True).mean()
    avg_loss = loss.ewm(com=period - 1, adjust=True).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))
