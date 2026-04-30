import sys, os, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.simulator import run_simulation


def _mk_candles(n, start_price=100.0, trend=0.01):
    candles = []
    price = start_price
    for i in range(n):
        candles.append({
            "timestamp": 1_700_000_000_000 + i * 60_000,
            "open": price,
            "high": price * 1.002,
            "low": price * 0.998,
            "close": round(price, 4),
            "volume": 1000.0,
        })
        price = round(price * (1 + trend), 4)
    return candles


class TestRunSimulation(unittest.TestCase):

    def test_no_signals_flat_capital(self):
        candles = _mk_candles(20, trend=0.01)
        signals = [{"candle_index": i, "action": "HOLD"} for i in range(20)]
        result = run_simulation(candles, signals, initial_capital=100.0)
        self.assertEqual(result["final_capital"], 100.0)

    def test_single_win_increases_capital(self):
        candles = _mk_candles(20, start_price=100.0, trend=0.01)
        signals = [
            {"candle_index": 5,  "action": "BUY"},
            {"candle_index": 10, "action": "SELL"},
        ]
        result = run_simulation(candles, signals, initial_capital=100.0, fee_pct=0.0)
        self.assertGreater(result["final_capital"], 100.0)
        self.assertGreater(result["total_return_pct"], 0.0)

    def test_trailing_buy_dropped(self):
        candles = _mk_candles(20, trend=0.0)
        # Only a BUY at the end with no following SELL — should be dropped
        signals = [{"candle_index": 15, "action": "BUY"}]
        result = run_simulation(candles, signals, initial_capital=100.0)
        self.assertEqual(result["num_trades"], 0)

    def test_max_drawdown_nonnegative(self):
        candles = _mk_candles(30, start_price=100.0, trend=-0.02)
        signals = [
            {"candle_index": 2,  "action": "BUY"},
            {"candle_index": 20, "action": "SELL"},
        ]
        result = run_simulation(candles, signals, initial_capital=100.0)
        self.assertGreaterEqual(result["max_drawdown"], 0.0)

    def test_compounding_vs_fixed_differ(self):
        # Price goes up then down — compound and fixed should produce different results
        candles = []
        prices = [100, 110, 120, 115, 105, 108, 115, 120, 118, 125]
        for i, p in enumerate(prices):
            candles.append({
                "timestamp": 1_700_000_000_000 + i * 60_000,
                "open": float(p), "high": float(p) + 1,
                "low": float(p) - 1, "close": float(p), "volume": 1000.0,
            })
        signals = [
            {"candle_index": 0, "action": "BUY"},
            {"candle_index": 2, "action": "SELL"},
            {"candle_index": 4, "action": "BUY"},
            {"candle_index": 6, "action": "SELL"},
            {"candle_index": 7, "action": "BUY"},
            {"candle_index": 9, "action": "SELL"},
        ]
        result_compound = run_simulation(candles, signals, initial_capital=100.0,
                                         fee_pct=0.0, compounding_mode="compound")
        result_fixed = run_simulation(candles, signals, initial_capital=100.0,
                                       fee_pct=0.0, compounding_mode="fixed")
        # With compounding the sizes change between trades — results diverge
        self.assertNotAlmostEqual(result_compound["final_capital"],
                                   result_fixed["final_capital"], places=2)


    def test_strict_buy_sell_alternation(self):
        """Double-BUY and double-SELL should be silently ignored.
        BUY at 5, BUY at 7 (ignored), SELL at 10, SELL at 12 (ignored)
        → exactly 1 trade."""
        candles = _mk_candles(20, trend=0.0)
        signals = [
            {"candle_index": 5,  "action": "BUY"},
            {"candle_index": 7,  "action": "BUY"},   # duplicate – ignored
            {"candle_index": 10, "action": "SELL"},
            {"candle_index": 12, "action": "SELL"},  # duplicate – ignored
        ]
        result = run_simulation(candles, signals, initial_capital=100.0)
        self.assertEqual(result["num_trades"], 1)

    def test_open_position_marked_to_market_at_end(self):
        """BUY with no closing SELL on a rising trend → final capital > initial."""
        candles = _mk_candles(20, start_price=100.0, trend=0.05)
        signals = [{"candle_index": 2, "action": "BUY"}]
        result = run_simulation(candles, signals, initial_capital=100.0, fee_pct=0.0)
        # Trailing BUY is mark-to-marked, not dropped when there is no SELL
        # (the simulator closes open positions at last price)
        # Price rose from ~100 to ~100*(1.05^17) ≈ 229, so final > 100
        self.assertGreater(result["final_capital"], 100.0)


if __name__ == "__main__":
    unittest.main()
