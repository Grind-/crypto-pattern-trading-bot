import sys, os, unittest
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.main import _build_capital_series

NOW = 1_700_000_000_000
START = NOW - 10 * 3_600_000

def _sell(ts, pnl): return {"type": "SELL", "timestamp": ts, "pnl_pct": pnl}
def _buy(ts):       return {"type": "BUY",  "timestamp": ts}


class TestBuildCapitalSeries(unittest.TestCase):

    def _assert_monotonic(self, raw):
        for i in range(1, len(raw)):
            self.assertLess(raw[i-1][0], raw[i][0],
                            f"Non-monotonic at index {i}: {raw[i-1][0]} >= {raw[i][0]}")

    def test_no_trades_flat_series(self):
        raw, chk = _build_capital_series([], 100.0, "compound", "FLAT", None, 100.0, [], NOW, 100.0, START)
        self.assertGreaterEqual(len(raw), 2)
        self.assertEqual(raw[0], (START, 100.0))
        self.assertEqual(raw[-1][0], NOW)
        self._assert_monotonic(raw)

    def test_single_winning_sell_compound(self):
        sell_ts = START + 3_600_000
        trades = [_buy(START + 1000), _sell(sell_ts, 10.0)]
        raw, chk = _build_capital_series(trades, 100.0, "compound", "FLAT", None, 100.0, [], NOW, 110.0, START)
        self.assertIn(sell_ts, chk)
        self.assertAlmostEqual(chk[sell_ts], 110.0, places=1)
        self._assert_monotonic(raw)

    def test_compound_wins_caps_at_trade_amount_on_loss(self):
        sell_ts = START + 3_600_000
        trades = [_buy(START + 1000), _sell(sell_ts, -5.0)]
        raw, chk = _build_capital_series(trades, 100.0, "compound_wins", "FLAT", None, 100.0, [], NOW, 100.0, START)
        self.assertAlmostEqual(chk[sell_ts], 100.0, places=1)

    def test_fixed_mode_resets_each_trade(self):
        t1 = START + 1_000_000
        t2 = START + 2_000_000
        trades = [_buy(START+1), _sell(t1, 20.0), _buy(t1+1), _sell(t2, 10.0)]
        raw, chk = _build_capital_series(trades, 100.0, "fixed", "FLAT", None, 100.0, [], NOW, 100.0, START)
        # In fixed mode running_cap is always reset to trade_amount (not compounded),
        # so each checkpoint records exactly trade_amount.
        self.assertAlmostEqual(chk[t1], 100.0, places=1)
        self.assertAlmostEqual(chk[t2], 100.0, places=1)

    def test_in_position_adds_candle_mtm_points(self):
        sell_ts = START + 1_000_000
        candle_ts = sell_ts + 500_000
        candles = [
            {"timestamp": sell_ts - 100, "close": 105.0},
            {"timestamp": candle_ts,     "close": 110.0},
        ]
        trades = [_buy(START+1), _sell(sell_ts, 0.0)]
        raw, _ = _build_capital_series(trades, 100.0, "compound", "IN_POSITION", 100.0, 100.0, candles, NOW, 110.0, START)
        ts_list = [t for t, _ in raw]
        self.assertIn(candle_ts, ts_list)
        self.assertNotIn(sell_ts - 100, ts_list)
        self._assert_monotonic(raw)

    def test_dedup_collapses_same_timestamp(self):
        collide_ts = NOW
        candles = [{"timestamp": collide_ts, "close": 105.0}]
        trades = [_buy(START+1), _sell(START + 1_000_000, 0.0)]
        raw, _ = _build_capital_series(trades, 100.0, "compound", "IN_POSITION", 100.0, 100.0, candles, NOW, 110.0, START)
        ts_counts = {}
        for ts, _ in raw:
            ts_counts[ts] = ts_counts.get(ts, 0) + 1
        self.assertTrue(all(v == 1 for v in ts_counts.values()))

    def test_series_is_strictly_monotonic_in_ts(self):
        t1 = START + 1_000_000
        t2 = START + 2_000_000
        candle_ts = t2 + 500_000
        candles = [{"timestamp": candle_ts, "close": 108.0}]
        trades = [_buy(START+1), _sell(t1, 5.0), _buy(t1+1), _sell(t2, -3.0)]
        raw, _ = _build_capital_series(trades, 100.0, "compound", "FLAT", None, 100.0, candles, NOW, 102.0, START)
        self._assert_monotonic(raw)

    def test_sell_checkpoints_returned_correctly(self):
        t1 = START + 1_000_000
        t2 = START + 3_000_000
        trades = [_buy(START+1), _sell(t1, 10.0), _buy(t1+1), _sell(t2, 5.0)]
        _, chk = _build_capital_series(trades, 100.0, "compound", "FLAT", None, 100.0, [], NOW, 115.5, START)
        self.assertIn(t1, chk)
        self.assertIn(t2, chk)
        self.assertEqual(len(chk), 2)


if __name__ == "__main__":
    unittest.main()
