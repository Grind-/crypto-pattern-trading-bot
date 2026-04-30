"""Tests for the /api/live/performance endpoint response shape (feature 20260430-1000).

Follows the event-loop + patch pattern from test_trigger_feature.py.
fetch_latest_klines is patched to return [] to avoid network calls.
"""
import asyncio
import sys
import os
import unittest
from unittest.mock import patch

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_user_and_state(username):
    """Create a minimal live state with no trades and patch auth to return username."""
    from app import main as m
    state = m._default_live_state()
    state["trade_amount"] = 100.0
    state["current_capital"] = 100.0
    m.live_states[username] = state
    return state


class TestPerformanceEndpointShape(unittest.TestCase):

    def _call(self, username="__perf_test__"):
        from app import main as m

        class FakeReq:
            pass

        with patch("app.main._get_current_user", return_value={"username": username}):
            with patch("app.main.fetch_latest_klines", return_value=[]):
                return _run(m.live_performance(FakeReq()))

    def setUp(self):
        _make_user_and_state("__perf_test__")

    def test_returns_required_top_level_keys(self):
        result = self._call()
        for key in ("capital_series", "bot_pct_series", "btc_pct_series",
                    "trade_pnl", "trade_pairs", "summary"):
            self.assertIn(key, result, f"Missing top-level key: {key}")

    def test_summary_has_required_fields(self):
        result = self._call()
        summary = result["summary"]
        for key in ("start_capital", "current_capital", "bot_pct", "num_sells"):
            self.assertIn(key, summary, f"Missing summary key: {key}")

    def test_capital_series_entries_have_ts_and_usdc(self):
        result = self._call()
        capital_series = result["capital_series"]
        self.assertGreater(len(capital_series), 0)
        for item in capital_series:
            self.assertIn("ts", item)
            self.assertIn("usdc", item)
            self.assertIsInstance(item["ts"], int)
            self.assertIsInstance(item["usdc"], float)

    def test_capital_series_ts_strictly_increasing(self):
        """Regression: timestamps in capital_series must be strictly increasing."""
        result = self._call()
        series = result["capital_series"]
        for i in range(1, len(series)):
            self.assertLess(
                series[i - 1]["ts"], series[i]["ts"],
                f"Non-increasing ts at index {i}: {series[i-1]['ts']} >= {series[i]['ts']}"
            )

    def test_empty_history_returns_two_point_baseline(self):
        """No trades → capital_series should have exactly 2 entries (start + now)."""
        result = self._call()
        capital_series = result["capital_series"]
        self.assertEqual(len(capital_series), 2,
            f"Expected 2 baseline entries, got {len(capital_series)}")


if __name__ == "__main__":
    unittest.main()
