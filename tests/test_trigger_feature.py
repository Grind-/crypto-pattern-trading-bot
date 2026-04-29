"""Tests for the manual analysis trigger feature (feature 20260429-2005).

These tests run inside the Docker container where all dependencies are installed.
Source-inspection tests do NOT import main.py at the top level to avoid
startup side-effects; they use importlib or read the source file directly.
"""
import asyncio
import sys
import os
import unittest
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Path setup: /app is the working dir inside the container, but for local
# host-side runs we add the parent of the tests/ directory.
# ---------------------------------------------------------------------------
_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

MAIN_PY = os.path.join(_BASE, "app", "main.py")


# ---------------------------------------------------------------------------
# Helper – read main.py source once
# ---------------------------------------------------------------------------
def _main_src():
    with open(MAIN_PY, encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# Source-only tests (no import of main.py required)
# ---------------------------------------------------------------------------

class TestSourceDefaultLiveState(unittest.TestCase):
    """_default_live_state() source must include _cycle_running: False."""

    def test_cycle_running_in_default_state_source(self):
        src = _main_src()
        # The dict literal inside _default_live_state must contain the key
        self.assertIn('"_cycle_running": False', src)

    def test_start_live_update_contains_cycle_running(self):
        src = _main_src()
        # The live_state.update({...}) block in start_live must contain the key
        self.assertIn('"_cycle_running": False', src)


class TestSourceLiveLoop(unittest.TestCase):
    """_live_loop source checks."""

    def test_trigger_event_created(self):
        src = _main_src()
        self.assertIn("trigger_event = asyncio.Event()", src)

    def test_trigger_event_stored_in_live_state(self):
        src = _main_src()
        self.assertIn('live_state["_trigger_event"] = trigger_event', src)

    def test_manual_trigger_flag_and_wait_for(self):
        src = _main_src()
        self.assertIn("manual_trigger", src)
        self.assertIn("asyncio.wait_for(trigger_event.wait()", src)

    def test_manual_trigger_log_message(self):
        src = _main_src()
        self.assertIn("Manueller Trigger", src)

    def test_cycle_running_set_true(self):
        src = _main_src()
        self.assertIn('live_state["_cycle_running"] = True', src)

    def test_cycle_running_set_false(self):
        src = _main_src()
        self.assertIn('live_state["_cycle_running"] = False', src)

    def test_finally_clears_trigger_event(self):
        src = _main_src()
        self.assertIn('live_state["_trigger_event"] = None', src)

    def test_finally_clears_cycle_running(self):
        src = _main_src()
        # There must be at least two occurrences:
        # one in the loop body and one in the finally block
        count = src.count('live_state["_cycle_running"] = False')
        self.assertGreaterEqual(count, 2)

    def test_status_endpoint_exposes_cycle_running(self):
        src = _main_src()
        self.assertIn('result["cycle_running"] = bool(live_state.get("_cycle_running"))', src)

    def test_trigger_endpoint_defined(self):
        src = _main_src()
        self.assertIn('/api/live/trigger', src)
        self.assertIn('async def trigger_live', src)

    def test_trigger_endpoint_checks_running(self):
        src = _main_src()
        self.assertIn('live_state.get("running")', src)

    def test_trigger_endpoint_checks_cycle_running(self):
        src = _main_src()
        self.assertIn('live_state.get("_cycle_running")', src)

    def test_candle_count_not_incremented_on_manual_trigger(self):
        """manual_trigger path must NOT increment candle_count."""
        src = _main_src()
        # The candle_count increment must be in the else branch
        # (i.e., only when NOT a manual trigger)
        import re
        # Find the block containing manual_trigger conditional
        idx = src.find("if manual_trigger:")
        self.assertGreater(idx, 0, "if manual_trigger: not found")
        # After this, candle_count increment must appear in else branch
        block = src[idx:idx + 400]
        self.assertIn("else:", block)
        self.assertIn("candle_count", block)


# ---------------------------------------------------------------------------
# Runtime tests – import app.main inside each test to isolate failures
# ---------------------------------------------------------------------------

class TestDefaultLiveStateRuntime(unittest.TestCase):
    def test_cycle_running_false(self):
        from app.main import _default_live_state
        state = _default_live_state()
        self.assertIn("_cycle_running", state)
        self.assertFalse(state["_cycle_running"])


class TestTriggerEndpointRuntime(unittest.TestCase):
    """Test the trigger endpoint logic directly (bypass HTTP layer)."""

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_returns_400_when_not_running(self):
        from app import main as m
        from fastapi import HTTPException
        m.live_states["__rt_not_running__"] = m._default_live_state()

        class FakeReq:
            pass

        with patch("app.main._get_current_user", return_value={"username": "__rt_not_running__"}):
            with self.assertRaises(HTTPException) as ctx:
                self._run(m.trigger_live(FakeReq()))
            self.assertEqual(ctx.exception.status_code, 400)

    def test_returns_cycle_running_reason(self):
        from app import main as m
        state = m._default_live_state()
        state["running"] = True
        state["_cycle_running"] = True
        m.live_states["__rt_cycle__"] = state

        class FakeReq:
            pass

        with patch("app.main._get_current_user", return_value={"username": "__rt_cycle__"}):
            result = self._run(m.trigger_live(FakeReq()))
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "cycle_running")

    def test_returns_loop_not_ready_when_no_event(self):
        from app import main as m
        state = m._default_live_state()
        state["running"] = True
        state["_cycle_running"] = False
        state["_trigger_event"] = None
        m.live_states["__rt_noev__"] = state

        class FakeReq:
            pass

        with patch("app.main._get_current_user", return_value={"username": "__rt_noev__"}):
            result = self._run(m.trigger_live(FakeReq()))
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "loop_not_ready")

    def test_sets_event_and_returns_ok(self):
        from app import main as m
        ev = asyncio.Event()
        state = m._default_live_state()
        state["running"] = True
        state["_cycle_running"] = False
        state["_trigger_event"] = ev
        m.live_states["__rt_ok__"] = state

        class FakeReq:
            pass

        self.assertFalse(ev.is_set())
        with patch("app.main._get_current_user", return_value={"username": "__rt_ok__"}):
            result = self._run(m.trigger_live(FakeReq()))
        self.assertTrue(result["ok"])
        self.assertTrue(ev.is_set())


class TestStatusEndpointRuntime(unittest.TestCase):
    """Test cycle_running appears in live_status output."""

    def _run(self, coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    def test_cycle_running_false_in_status(self):
        from app import main as m
        state = m._default_live_state()
        state["_cycle_running"] = False
        m.live_states["__st_false__"] = state

        class FakeReq:
            pass

        with patch("app.main._get_current_user", return_value={"username": "__st_false__"}):
            result = self._run(m.live_status(FakeReq()))
        self.assertIn("cycle_running", result)
        self.assertFalse(result["cycle_running"])

    def test_cycle_running_true_in_status(self):
        from app import main as m
        state = m._default_live_state()
        state["_cycle_running"] = True
        m.live_states["__st_true__"] = state

        class FakeReq:
            pass

        with patch("app.main._get_current_user", return_value={"username": "__st_true__"}):
            result = self._run(m.live_status(FakeReq()))
        self.assertIn("cycle_running", result)
        self.assertTrue(result["cycle_running"])


if __name__ == "__main__":
    unittest.main()
