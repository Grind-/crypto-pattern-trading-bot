import math
from typing import Dict, List


def calculate_risk_params(candles: List[Dict], capital: float,
                          regime: str, signals_count_green: int,
                          sl_atr_mult: float = 1.5,
                          tp_atr_mult: float = 2.5) -> Dict:
    atr = candles[-1].get("atr") if candles else None
    if not atr or math.isnan(atr):
        entry = candles[-1]["close"] if candles else 1.0
        atr = entry * 0.015  # 1.5% fallback
    entry = candles[-1]["close"] if candles else 1.0
    sl_pct = round(sl_atr_mult * atr / entry * 100, 3)
    tp_pct = round(tp_atr_mult * atr / entry * 100, 3)

    if regime == "HIGH_VOLATILITY" or sl_pct > 5.0:
        size = 0
    elif signals_count_green >= 4 and regime == "BULL_TREND":
        size = 100
    elif signals_count_green >= 3:
        size = 66
    elif signals_count_green >= 2:
        size = 33
    else:
        size = 0

    blocked = size == 0
    risk_usdc = round(capital * size / 100 * sl_pct / 100, 2)
    return {
        "position_size_pct": size,
        "stop_loss_pct": sl_pct,
        "take_profit_pct": tp_pct,
        "atr_value": round(atr, 4),
        "risk_per_trade_usdc": risk_usdc,
        "reasoning": f"ATR={atr:.2f}, SL={sl_atr_mult}×ATR({sl_pct:.2f}%), TP={tp_atr_mult}×ATR({tp_pct:.2f}%), size={size}% ({signals_count_green}/4 grün, regime={regime})",
        "blocked": blocked,
    }
