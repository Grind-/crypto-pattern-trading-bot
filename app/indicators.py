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

    df["adx"] = _adx(df["high"], df["low"], df["close"], 14)
    bull_div, bear_div = _rsi_divergence(df["close"], df["rsi"], 10)
    df["rsi_bull_div"] = bull_div
    df["rsi_bear_div"] = bear_div

    df = df.where(pd.notna(df), None)
    return df.to_dict("records")


def _adx(highs, lows, closes, period=14):
    tr1 = highs - lows
    tr2 = (highs - closes.shift(1)).abs()
    tr3 = (lows - closes.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    plus_dm = (highs - highs.shift(1)).clip(lower=0)
    minus_dm = (lows.shift(1) - lows).clip(lower=0)
    mask = plus_dm >= minus_dm
    plus_dm = plus_dm.where(mask, 0.0)
    minus_dm = minus_dm.where(~mask, 0.0)
    tr_s = tr.ewm(com=period - 1, adjust=False).mean()
    pdm_s = plus_dm.ewm(com=period - 1, adjust=False).mean()
    mdm_s = minus_dm.ewm(com=period - 1, adjust=False).mean()
    pdi = 100 * pdm_s / tr_s.replace(0, float('nan'))
    mdi = 100 * mdm_s / tr_s.replace(0, float('nan'))
    dx = (abs(pdi - mdi) / (pdi + mdi).replace(0, float('nan'))) * 100
    return dx.ewm(com=period - 1, adjust=False).mean()


def _rsi_divergence(closes, rsi, lookback=10):
    bull = pd.Series(False, index=closes.index)
    bear = pd.Series(False, index=closes.index)
    for i in range(lookback, len(closes)):
        bull.iloc[i] = closes.iloc[i] < closes.iloc[i - lookback] and rsi.iloc[i] > rsi.iloc[i - lookback]
        bear.iloc[i] = closes.iloc[i] > closes.iloc[i - lookback] and rsi.iloc[i] < rsi.iloc[i - lookback]
    return bull, bear


def _rsi(prices: pd.Series, period: int = 14) -> pd.Series:
    delta = prices.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(com=period - 1, adjust=True).mean()
    avg_loss = loss.ewm(com=period - 1, adjust=True).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))
