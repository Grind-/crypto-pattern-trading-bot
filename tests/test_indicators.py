import sys, os, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.indicators import compute_indicators


def _mk_candles(n, start_price=100.0, trend=0.0):
    """Build synthetic candles. When trend != 0 prices alternate up/down
    so that RSI never divides by zero (always has both gains and losses)."""
    candles = []
    price = start_price
    for i in range(n):
        candles.append({
            "timestamp": 1_700_000_000_000 + i * 60_000,
            "open":   round(price, 4),
            "high":   round(price * 1.005, 4),
            "low":    round(price * 0.995, 4),
            "close":  round(price, 4),
            "volume": float(1000 + i),
        })
        if trend != 0.0:
            # Alternate up-up-down so prices oscillate while drifting with `trend`
            if i % 3 == 2:
                price = round(price * (1 - abs(trend)), 6)
            else:
                price = round(price * (1 + abs(trend) * 2), 6)
        # else: constant price
    return candles


class TestComputeIndicators(unittest.TestCase):

    def test_output_length_matches_input(self):
        candles = _mk_candles(60)
        result = compute_indicators(candles)
        self.assertEqual(len(result), 60)

    def test_required_columns_present(self):
        candles = _mk_candles(60)
        result = compute_indicators(candles)
        last = result[-1]
        for key in ("rsi", "macd", "bb_upper", "bb_lower", "sma20", "atr"):
            self.assertIn(key, last, f"Missing key: {key}")

    def test_rsi_in_range(self):
        candles = _mk_candles(60, trend=0.005)
        result = compute_indicators(candles)
        import math
        rsi_values = [
            r["rsi"] for r in result[-10:]
            if r.get("rsi") is not None and not math.isnan(r["rsi"])
        ]
        self.assertGreater(len(rsi_values), 0, "No valid RSI values found in last 10 candles")
        for v in rsi_values:
            self.assertGreaterEqual(v, 0.0)
            self.assertLessEqual(v, 100.0)

    def test_bollinger_bands_sma_in_envelope(self):
        import math
        candles = _mk_candles(60, trend=0.002)
        result = compute_indicators(candles)
        for r in result:
            bb_upper = r.get("bb_upper")
            bb_lower = r.get("bb_lower")
            sma20    = r.get("sma20")
            if bb_upper is None or bb_lower is None or sma20 is None:
                continue
            if any(math.isnan(x) for x in (bb_upper, bb_lower, sma20)):
                continue
            self.assertLessEqual(bb_lower, sma20 + 1e-9,
                                  f"bb_lower {bb_lower} > sma20 {sma20}")
            self.assertGreaterEqual(bb_upper, sma20 - 1e-9,
                                     f"bb_upper {bb_upper} < sma20 {sma20}")

    def test_volume_ratio_positive(self):
        candles = _mk_candles(60)
        result = compute_indicators(candles)
        last = result[-1]
        vr = last.get("volume_ratio")
        self.assertIsNotNone(vr)
        self.assertGreater(vr, 0.0)


    def test_constant_price_series_rsi_undefined_or_neutral(self):
        """60 candles all at close=100.0 → last RSI is None or 45-55."""
        import math
        candles = _mk_candles(60, trend=0.0)   # constant price (trend=0)
        result = compute_indicators(candles)
        rsi = result[-1].get("rsi")
        # When there are no price changes, delta is always 0.
        # avg_gain = avg_loss = 0, so RS = NaN → RSI = NaN → stored as None.
        # Also accept a neutral value in [45, 55] in case of floating-point drift.
        if rsi is not None:
            import math
            if not math.isnan(float(rsi)):
                self.assertGreaterEqual(float(rsi), 45.0)
                self.assertLessEqual(float(rsi), 55.0)

    def test_divergence_flags_on_synthetic_pattern(self):
        """Bullish divergence: price makes lower low, RSI makes higher low.

        _rsi_divergence uses a lookback of 10 candles:
          bull[i] = closes[i] < closes[i-10] AND rsi[i] > rsi[i-10]

        We build candles so that:
          - candles[i-10] has a *higher* close than candles[i]  (price lower low)
          - candles[i-10] has a *lower* RSI than candles[i]     (RSI higher low)

        Strategy: first 15 candles trend strongly up (high RSI), then 15 candles
        trend strongly down (low RSI), then 20 candles trend strongly up again
        (RSI rises while price is still lower than the first peak).
        """
        import math

        # Phase 1: 15 candles rising strongly → RSI ≫ 50
        # Phase 2: 15 candles falling strongly → RSI ≪ 50, price lower
        # Phase 3: 20 candles rising strongly → RSI rises back up
        # At the transition point (candle ≈ 30), close[30] < close[20] but
        # rsi[30] > rsi[20], so bull_div[30] should be True.

        candles = []
        ts_base = 1_700_000_000_000
        price = 100.0
        for i in range(15):
            price *= 1.03
            candles.append({
                "timestamp": ts_base + i * 60_000,
                "open": round(price * 0.99, 4),
                "high": round(price * 1.01, 4),
                "low":  round(price * 0.99, 4),
                "close": round(price, 4),
                "volume": 1000.0,
            })
        # Phase 2: strong drop
        for i in range(15):
            price *= 0.94
            candles.append({
                "timestamp": ts_base + (15 + i) * 60_000,
                "open": round(price * 1.01, 4),
                "high": round(price * 1.01, 4),
                "low":  round(price * 0.99, 4),
                "close": round(price, 4),
                "volume": 1000.0,
            })
        # Phase 3: strong rise
        for i in range(20):
            price *= 1.03
            candles.append({
                "timestamp": ts_base + (30 + i) * 60_000,
                "open": round(price * 0.99, 4),
                "high": round(price * 1.01, 4),
                "low":  round(price * 0.99, 4),
                "close": round(price, 4),
                "volume": 1000.0,
            })

        result = compute_indicators(candles)

        # Check that at least one candle in phase 3 (index >= 30) shows bull_div
        bull_div_found = any(
            r.get("rsi_bull_div") is True
            for r in result[30:]
        )
        self.assertTrue(bull_div_found,
            "Expected at least one rsi_bull_div=True in phase-3 candles")


if __name__ == "__main__":
    unittest.main()
