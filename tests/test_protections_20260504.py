"""
Tests for feature 20260504-1445: Freqtrade-style protections.
Tests LiveRequest new fields, _default_live_state new keys,
and all three helper functions.
"""
import sys
import os
import time
import unittest

# Ensure the app module is importable
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.main import (
    LiveRequest,
    _default_live_state,
    _count_recent_consecutive_losses,
    _is_buy_blocked_by_protections,
    _register_sell_outcome,
)


# ─── Minimal req stub ─────────────────────────────────────────────────────────

def _req(**kwargs):
    """Build a LiveRequest with safe defaults, overridable via kwargs."""
    defaults = dict(
        api_key="k", api_secret="s", symbol="BTCUSDC", interval="4h",
        trailing_stop=False, trailing_activate_pct=1.0,
        cooldown_candles=0, max_consecutive_losses=0, halt_candles=4,
        min_hold_candles=0,
    )
    defaults.update(kwargs)
    return LiveRequest(**defaults)


# ─── AC: LiveRequest fields ────────────────────────────────────────────────────

class TestLiveRequestFields(unittest.TestCase):

    def test_trailing_stop_default_false(self):
        req = _req()
        self.assertFalse(req.trailing_stop)
        self.assertIsInstance(req.trailing_stop, bool)

    def test_trailing_activate_pct_default(self):
        req = _req()
        self.assertEqual(req.trailing_activate_pct, 1.0)
        self.assertIsInstance(req.trailing_activate_pct, float)

    def test_cooldown_candles_default_zero(self):
        req = _req()
        self.assertEqual(req.cooldown_candles, 0)
        self.assertIsInstance(req.cooldown_candles, int)

    def test_max_consecutive_losses_default_zero(self):
        req = _req()
        self.assertEqual(req.max_consecutive_losses, 0)
        self.assertIsInstance(req.max_consecutive_losses, int)

    def test_halt_candles_default_four(self):
        req = _req()
        self.assertEqual(req.halt_candles, 4)
        self.assertIsInstance(req.halt_candles, int)

    def test_min_hold_candles_default_zero(self):
        req = _req()
        self.assertEqual(req.min_hold_candles, 0)
        self.assertIsInstance(req.min_hold_candles, int)

    def test_all_new_fields_accept_nondefault_values(self):
        req = _req(
            trailing_stop=True,
            trailing_activate_pct=2.5,
            cooldown_candles=3,
            max_consecutive_losses=5,
            halt_candles=8,
            min_hold_candles=2,
        )
        self.assertTrue(req.trailing_stop)
        self.assertEqual(req.trailing_activate_pct, 2.5)
        self.assertEqual(req.cooldown_candles, 3)
        self.assertEqual(req.max_consecutive_losses, 5)
        self.assertEqual(req.halt_candles, 8)
        self.assertEqual(req.min_hold_candles, 2)


# ─── AC: _default_live_state new keys ─────────────────────────────────────────

class TestDefaultLiveState(unittest.TestCase):

    def setUp(self):
        self.state = _default_live_state()

    def test_cooldowns_present_and_empty_dict(self):
        self.assertIn("cooldowns", self.state)
        self.assertEqual(self.state["cooldowns"], {})

    def test_trading_halted_until_ts_present_none(self):
        self.assertIn("trading_halted_until_ts", self.state)
        self.assertIsNone(self.state["trading_halted_until_ts"])

    def test_loss_streak_present_zero(self):
        self.assertIn("loss_streak", self.state)
        self.assertEqual(self.state["loss_streak"], 0)

    def test_sl_price_present_none(self):
        self.assertIn("sl_price", self.state)
        self.assertIsNone(self.state["sl_price"])

    def test_entry_candle_count_present_none(self):
        self.assertIn("entry_candle_count", self.state)
        self.assertIsNone(self.state["entry_candle_count"])

    def test_no_key_error_on_cold_start(self):
        """Access every new key without raising KeyError."""
        _ = self.state["cooldowns"]
        _ = self.state["trading_halted_until_ts"]
        _ = self.state["loss_streak"]
        _ = self.state["sl_price"]
        _ = self.state["entry_candle_count"]


# ─── AC: _count_recent_consecutive_losses ─────────────────────────────────────

class TestCountRecentConsecutiveLosses(unittest.TestCase):

    def test_empty_history_returns_zero(self):
        self.assertEqual(_count_recent_consecutive_losses([]), 0)

    def test_all_wins_returns_zero(self):
        hist = [
            {"type": "SELL", "pnl_pct": 5.0},
            {"type": "SELL", "pnl_pct": 2.0},
        ]
        self.assertEqual(_count_recent_consecutive_losses(hist), 0)

    def test_trailing_losses_stops_at_profit(self):
        hist = [
            {"type": "SELL", "pnl_pct": 5.0},   # profit — streak stops here
            {"type": "SELL", "pnl_pct": -1.0},
            {"type": "SELL", "pnl_pct": -2.0},
        ]
        # Reversed: [-2, -1, +5] → count 2 then stop
        self.assertEqual(_count_recent_consecutive_losses(hist), 2)

    def test_skips_non_sell_entries(self):
        hist = [
            {"type": "BUY", "pnl_pct": None},
            {"type": "SELL", "pnl_pct": -3.0},
        ]
        self.assertEqual(_count_recent_consecutive_losses(hist), 1)

    def test_symbol_scoped_ignores_other_symbol(self):
        hist = [
            {"type": "SELL", "symbol": "BTCUSDT", "pnl_pct": -1.0},
            {"type": "SELL", "symbol": "ETHUSDT", "pnl_pct": -1.0},
        ]
        # When scoped to BTCUSDT, only that one counts
        self.assertEqual(_count_recent_consecutive_losses(hist, symbol="BTCUSDT"), 1)

    def test_symbol_scoped_unscoped_counts_all(self):
        hist = [
            {"type": "SELL", "symbol": "BTCUSDT", "pnl_pct": -1.0},
            {"type": "SELL", "symbol": "ETHUSDT", "pnl_pct": -1.0},
        ]
        self.assertEqual(_count_recent_consecutive_losses(hist), 2)

    def test_win_after_losses_resets_streak(self):
        # trade_history is chronological: last entry = most recent
        hist = [
            {"type": "SELL", "pnl_pct": -2.0},  # oldest
            {"type": "SELL", "pnl_pct": -3.0},  # middle
            {"type": "SELL", "pnl_pct": 1.0},   # most recent = win
        ]
        # Most recent is win → streak = 0
        self.assertEqual(_count_recent_consecutive_losses(hist), 0)

    def test_pnl_none_entries_skipped(self):
        """Entries with pnl_pct=None in SELL type should be skipped."""
        hist = [
            {"type": "SELL", "pnl_pct": None},
            {"type": "SELL", "pnl_pct": -1.0},
        ]
        # None entry is skipped; -1 counts
        self.assertEqual(_count_recent_consecutive_losses(hist), 1)


# ─── AC: _is_buy_blocked_by_protections ───────────────────────────────────────

class TestIsBuyBlockedByProtections(unittest.TestCase):

    def _state(self):
        s = _default_live_state()
        return s

    def test_no_halt_no_cooldown_not_blocked(self):
        state = self._state()
        req = _req()
        blocked, why = _is_buy_blocked_by_protections(state, "BTCUSDC", time.time(), req)
        self.assertFalse(blocked)
        self.assertEqual(why, "")

    def test_active_halt_blocks(self):
        state = self._state()
        state["trading_halted_until_ts"] = time.time() + 9999
        req = _req()
        blocked, why = _is_buy_blocked_by_protections(state, "BTCUSDC", time.time(), req)
        self.assertTrue(blocked)
        self.assertIn("Halt", why)

    def test_expired_halt_does_not_block(self):
        state = self._state()
        state["trading_halted_until_ts"] = time.time() - 1
        req = _req()
        blocked, _ = _is_buy_blocked_by_protections(state, "BTCUSDC", time.time(), req)
        self.assertFalse(blocked)

    def test_cooldown_active_for_symbol_blocks(self):
        state = self._state()
        state["cooldowns"] = {"BTCUSDC": time.time() + 9999}
        req = _req()
        blocked, why = _is_buy_blocked_by_protections(state, "BTCUSDC", time.time(), req)
        self.assertTrue(blocked)
        self.assertIn("Cooldown", why)

    def test_cooldown_different_symbol_does_not_block(self):
        state = self._state()
        state["cooldowns"] = {"ETHUSDC": time.time() + 9999}
        req = _req()
        blocked, _ = _is_buy_blocked_by_protections(state, "BTCUSDC", time.time(), req)
        self.assertFalse(blocked)

    def test_expired_cooldown_does_not_block(self):
        state = self._state()
        state["cooldowns"] = {"BTCUSDC": time.time() - 1}
        req = _req()
        blocked, _ = _is_buy_blocked_by_protections(state, "BTCUSDC", time.time(), req)
        self.assertFalse(blocked)

    def test_halt_takes_priority_over_cooldown(self):
        state = self._state()
        state["trading_halted_until_ts"] = time.time() + 9999
        state["cooldowns"] = {"BTCUSDC": time.time() + 9999}
        req = _req()
        blocked, why = _is_buy_blocked_by_protections(state, "BTCUSDC", time.time(), req)
        self.assertTrue(blocked)
        self.assertIn("Halt", why)


# ─── AC: _register_sell_outcome ───────────────────────────────────────────────

class TestRegisterSellOutcome(unittest.TestCase):

    def _state_with_history(self, trade_history):
        s = _default_live_state()
        s["trade_history"] = trade_history
        return s

    def test_sl_sell_with_cooldown_candles_sets_cooldown(self):
        state = self._state_with_history([
            {"type": "SELL", "pnl_pct": -1.0}
        ])
        req = _req(cooldown_candles=2, max_consecutive_losses=0)
        before = time.time()
        _register_sell_outcome(state, "BTCUSDC", -1.0, True, 300, req)
        cooldown_ts = state["cooldowns"].get("BTCUSDC")
        self.assertIsNotNone(cooldown_ts)
        # Should be approximately now + 2*300 = now + 600
        self.assertGreaterEqual(cooldown_ts, before + 595)

    def test_non_sl_sell_does_not_set_cooldown(self):
        state = self._state_with_history([
            {"type": "SELL", "pnl_pct": 2.0}
        ])
        req = _req(cooldown_candles=2, max_consecutive_losses=0)
        _register_sell_outcome(state, "BTCUSDC", 2.0, False, 300, req)
        self.assertNotIn("BTCUSDC", state["cooldowns"])

    def test_cooldown_candles_zero_never_writes_cooldown(self):
        """Default cooldown_candles=0: SL exit must not pollute cooldowns dict."""
        state = self._state_with_history([
            {"type": "SELL", "pnl_pct": -1.0}
        ])
        req = _req(cooldown_candles=0)
        _register_sell_outcome(state, "BTCUSDC", -1.0, True, 300, req)
        self.assertNotIn("BTCUSDC", state["cooldowns"])

    def test_three_consecutive_losses_triggers_halt(self):
        state = self._state_with_history([
            {"type": "SELL", "pnl_pct": -1.0},
            {"type": "SELL", "pnl_pct": -2.0},
            {"type": "SELL", "pnl_pct": -3.0},
        ])
        req = _req(max_consecutive_losses=3, halt_candles=4)
        before = time.time()
        _register_sell_outcome(state, "BTCUSDC", -3.0, False, 300, req)
        halted_until = state.get("trading_halted_until_ts")
        self.assertIsNotNone(halted_until)
        self.assertGreaterEqual(halted_until, before + 4 * 300 - 5)

    def test_two_losses_when_max_is_three_no_halt(self):
        state = self._state_with_history([
            {"type": "SELL", "pnl_pct": -1.0},
            {"type": "SELL", "pnl_pct": -2.0},
        ])
        req = _req(max_consecutive_losses=3, halt_candles=4)
        _register_sell_outcome(state, "BTCUSDC", -2.0, False, 300, req)
        self.assertIsNone(state.get("trading_halted_until_ts"))

    def test_max_consecutive_losses_zero_never_triggers_halt(self):
        """max_consecutive_losses=0 is the off-switch; 100 losses should not halt."""
        trade_history = [{"type": "SELL", "pnl_pct": -1.0}] * 10
        state = self._state_with_history(trade_history)
        req = _req(max_consecutive_losses=0)
        _register_sell_outcome(state, "BTCUSDC", -1.0, False, 300, req)
        self.assertIsNone(state.get("trading_halted_until_ts"))

    def test_log_fn_called_on_halt(self):
        """log_fn should be called when halt is triggered."""
        state = self._state_with_history([
            {"type": "SELL", "pnl_pct": -1.0},
            {"type": "SELL", "pnl_pct": -2.0},
            {"type": "SELL", "pnl_pct": -3.0},
        ])
        req = _req(max_consecutive_losses=3, halt_candles=4)
        log_calls = []
        def fake_log(s, msg):
            log_calls.append(msg)
        _register_sell_outcome(state, "BTCUSDC", -3.0, False, 300, req, log_fn=fake_log)
        self.assertTrue(len(log_calls) > 0)
        self.assertTrue(any("Halt" in m or "halt" in m or "Verluste" in m for m in log_calls))

    def test_log_fn_none_no_crash_on_halt(self):
        """log_fn=None must not crash even when halt triggers."""
        state = self._state_with_history([
            {"type": "SELL", "pnl_pct": -1.0},
            {"type": "SELL", "pnl_pct": -2.0},
            {"type": "SELL", "pnl_pct": -3.0},
        ])
        req = _req(max_consecutive_losses=3, halt_candles=4)
        # Should not raise
        _register_sell_outcome(state, "BTCUSDC", -3.0, False, 300, req, log_fn=None)

    def test_cooldown_uses_seconds_not_milliseconds(self):
        """Cooldown timestamp: must be around now + candles * interval_seconds (not *1000)."""
        state = self._state_with_history([{"type": "SELL", "pnl_pct": -1.0}])
        req = _req(cooldown_candles=1)
        interval_s = 300
        before = time.time()
        _register_sell_outcome(state, "BTCUSDC", -1.0, True, interval_s, req)
        ts = state["cooldowns"]["BTCUSDC"]
        # Should be ~now+300, absolutely NOT now+300000
        self.assertLess(ts, before + interval_s * 10)
        self.assertGreater(ts, before + interval_s * 0.9)


# ─── Code-inspection tests (grep-based) ───────────────────────────────────────

class TestCodeInspection(unittest.TestCase):
    """Grep main.py to verify structural code patterns are present."""

    @classmethod
    def setUpClass(cls):
        main_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "app", "main.py"
        )
        with open(main_path) as f:
            cls.src = f.read()

    def test_trailing_sl_ratchet_in_live_loop(self):
        """Trailing SL update block must be present in single-mode loop."""
        self.assertIn("req.trailing_stop", self.src)
        self.assertIn("req.trailing_activate_pct", self.src)
        # new_sl ratchet guard
        self.assertIn("new_sl > live_state", self.src)

    def test_sl_price_seeded_after_buy_single(self):
        """sl_price must be set after BUY in single mode."""
        self.assertIn('live_state["sl_price"] = round', self.src)

    def test_sl_price_cleared_on_sell_single(self):
        """sl_price must be cleared on sell in single mode."""
        self.assertIn('live_state["sl_price"] = None', self.src)

    def test_trailing_sl_ratchet_guard_never_down(self):
        """Trailing SL must only ratchet up (new_sl > current)."""
        self.assertIn("new_sl > live_state", self.src)

    def test_portfolio_trailing_sl_uses_slot(self):
        """Portfolio trailing SL must use slot dict."""
        self.assertIn("slot[\"sl_price\"]", self.src)
        self.assertIn("new_sl_p > slot[\"sl_price\"]", self.src)

    def test_is_buy_blocked_called_in_single_loop(self):
        """_is_buy_blocked_by_protections must appear at least twice (single + portfolio)."""
        count = self.src.count("_is_buy_blocked_by_protections")
        self.assertGreaterEqual(count, 2)

    def test_register_sell_outcome_called_after_sell(self):
        """_register_sell_outcome must appear in both loops."""
        count = self.src.count("_register_sell_outcome")
        self.assertGreaterEqual(count, 2)

    def test_confidence_scaling_min_in_single_loop(self):
        """Protection D: min(sized_capital, ..._portfolio_allocation_pct...) must exist."""
        self.assertIn("_portfolio_allocation_pct(confidence)", self.src)
        # Must appear inside a min() call context - check min+_portfolio_allocation_pct
        self.assertIn("min(sized_capital", self.src)

    def test_entry_candle_count_set_after_buy_single(self):
        """entry_candle_count must be set after BUY in single mode."""
        self.assertIn('live_state["entry_candle_count"] = live_state.get("candle_count", 0)', self.src)

    def test_min_hold_candles_gate_single(self):
        """SELL gate by min_hold_candles must exist in single loop."""
        self.assertIn("req.min_hold_candles > 0", self.src)
        self.assertIn("held_c < req.min_hold_candles", self.src)

    def test_force_sell_bypasses_min_hold(self):
        """min_hold gate must be guarded by 'not force_sell'."""
        self.assertIn("not force_sell and req.min_hold_candles", self.src)

    def test_portfolio_entry_candle_count(self):
        """Portfolio slot must get entry_candle_count at buy time."""
        self.assertIn('"entry_candle_count": live_state.get("candle_count", 0)', self.src)

    def test_partial_sell_not_in_min_hold_scope(self):
        """PARTIAL_SELL action should not be subject to min_hold_candles gate.
        The gate code explicitly checks action == 'SELL' via if action == 'SELL':."""
        # The gate wraps around: 'if not force_sell and req.min_hold_candles > 0'
        # followed by 'if action == "SELL":' to execute _do_sell
        self.assertIn('if action == "SELL":', self.src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
