"""Tests for the Portfolio Mode feature (feature 20260430-1100).

Tests use source-inspection for structure and runtime tests for logic,
following the same pattern as test_trigger_feature.py.
"""
import asyncio
import sys
import os
import unittest
from unittest.mock import patch, MagicMock

_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)

MAIN_PY = os.path.join(_BASE, "app", "main.py")


def _main_src():
    with open(MAIN_PY, encoding="utf-8") as fh:
        return fh.read()


# ── Source inspection tests ──────────────────────────────────────────────────

class TestPortfolioConstants(unittest.TestCase):
    """Verify constants exist in source."""

    def test_portfolio_max_positions_defined(self):
        src = _main_src()
        self.assertIn("PORTFOLIO_MAX_POSITIONS = 4", src)

    def test_portfolio_min_order_usdc_defined(self):
        src = _main_src()
        self.assertIn("PORTFOLIO_MIN_ORDER_USDC = 10.0", src)

    def test_portfolio_allocation_pct_function(self):
        src = _main_src()
        self.assertIn("def _portfolio_allocation_pct", src)
        self.assertIn("return 0.40", src)
        self.assertIn("return 0.30", src)
        self.assertIn("return 0.20", src)


class TestLiveRequestModel(unittest.TestCase):
    """Verify LiveRequest has mode and max_per_position fields."""

    def test_mode_field_present(self):
        src = _main_src()
        self.assertIn('mode: str = "single"', src)

    def test_max_per_position_field_present(self):
        src = _main_src()
        self.assertIn('max_per_position: float = 0.0', src)


class TestDefaultLiveStatePortfolio(unittest.TestCase):
    """_default_live_state must include portfolio keys."""

    def test_mode_key_in_default_state(self):
        src = _main_src()
        self.assertIn('"mode": "single"', src)

    def test_portfolio_positions_key(self):
        src = _main_src()
        self.assertIn('"portfolio_positions": {}', src)

    def test_max_per_position_key(self):
        src = _main_src()
        self.assertIn('"max_per_position": 0.0', src)


class TestPortfolioLoopExists(unittest.TestCase):
    """_portfolio_loop function must exist and contain required patterns."""

    def test_portfolio_loop_defined(self):
        src = _main_src()
        self.assertIn("async def _portfolio_loop", src)

    def test_portfolio_loop_has_buy_helper(self):
        src = _main_src()
        self.assertIn("async def _portfolio_buy", src)

    def test_portfolio_loop_has_sell_helper(self):
        src = _main_src()
        self.assertIn("async def _portfolio_sell", src)

    def test_portfolio_loop_checks_max_positions(self):
        src = _main_src()
        self.assertIn("PORTFOLIO_MAX_POSITIONS", src)

    def test_portfolio_loop_uses_allocation_pct(self):
        src = _main_src()
        self.assertIn("_portfolio_allocation_pct(confidence)", src)

    def test_portfolio_loop_sl_tp_check(self):
        src = _main_src()
        self.assertIn("SL", src)
        self.assertIn("TP", src)
        self.assertIn("TAKE-PROFIT", src)
        self.assertIn("STOP-LOSS", src)

    def test_portfolio_loop_phase2_scanning(self):
        src = _main_src()
        self.assertIn("scan_market", src)
        self.assertIn("slots_free", src)

    def test_portfolio_loop_confidence_tier_sizing(self):
        src = _main_src()
        # The sizing logic multiplies free_usdc by tier_pct
        self.assertIn("tier_pct = _portfolio_allocation_pct(confidence)", src)
        self.assertIn("sized = round(free_usdc * tier_pct", src)

    def test_portfolio_loop_max_per_position_cap(self):
        src = _main_src()
        self.assertIn("req.max_per_position", src)
        self.assertIn("min(sized, req.max_per_position)", src)

    def test_portfolio_loop_min_order_check(self):
        src = _main_src()
        self.assertIn("PORTFOLIO_MIN_ORDER_USDC", src)

    def test_portfolio_loop_startup_reconcile(self):
        src = _main_src()
        self.assertIn("startup_detected", src)
        self.assertIn("_add_synthetic_buy_if_needed", src)


class TestStartLiveBranching(unittest.TestCase):
    """start_live source must branch on mode."""

    def test_portfolio_branch_in_start_live(self):
        src = _main_src()
        self.assertIn('if mode == "portfolio":', src)
        self.assertIn("_portfolio_loop", src)

    def test_single_branch_in_start_live(self):
        src = _main_src()
        self.assertIn("_live_loop", src)

    def test_strategy_name_persistence(self):
        src = _main_src()
        self.assertIn('"strategy_name": mode', src)


class TestAutoResumeBranching(unittest.TestCase):
    """_auto_resume_all must read saved_mode and branch."""

    def test_saved_mode_read(self):
        src = _main_src()
        self.assertIn('saved_mode = (saved.get("strategy_name") or "single")', src)

    def test_resume_portfolio_branch(self):
        src = _main_src()
        self.assertIn('if saved_mode == "portfolio":', src)


class TestStatusEndpointPortfolio(unittest.TestCase):
    """status endpoint must expose portfolio fields when mode=portfolio."""

    def test_portfolio_total_value_in_status(self):
        src = _main_src()
        self.assertIn('"portfolio_total_value"', src)

    def test_portfolio_free_usdc_in_status(self):
        src = _main_src()
        self.assertIn('"portfolio_free_usdc"', src)

    def test_portfolio_open_count_in_status(self):
        src = _main_src()
        self.assertIn('"portfolio_open_count"', src)

    def test_portfolio_max_positions_in_status(self):
        src = _main_src()
        self.assertIn('"portfolio_max_positions"', src)


class TestResetPositionPortfolio(unittest.TestCase):
    """reset-position must handle portfolio mode."""

    def test_portfolio_branch_in_reset(self):
        src = _main_src()
        self.assertIn('live_state.get("mode") == "portfolio"', src)

    def test_cleared_count_in_response(self):
        src = _main_src()
        self.assertIn('"cleared": n', src)


class TestPerformanceEndpointPortfolio(unittest.TestCase):
    """performance endpoint must handle portfolio aggregation."""

    def test_is_portfolio_flag(self):
        src = _main_src()
        self.assertIn('is_portfolio = live_state.get("mode") == "portfolio"', src)

    def test_agg_value_calculation(self):
        src = _main_src()
        self.assertIn("agg_value", src)

    def test_free_usdc_in_performance(self):
        src = _main_src()
        self.assertIn("portfolio_free_usdc", src)


# ── Runtime tests ─────────────────────────────────────────────────────────────

def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestPortfolioAllocationPct(unittest.TestCase):
    """Unit test for _portfolio_allocation_pct."""

    def test_85_returns_40pct(self):
        from app.main import _portfolio_allocation_pct
        self.assertAlmostEqual(_portfolio_allocation_pct(85), 0.40)

    def test_84_returns_30pct(self):
        from app.main import _portfolio_allocation_pct
        self.assertAlmostEqual(_portfolio_allocation_pct(84), 0.30)

    def test_70_returns_30pct(self):
        from app.main import _portfolio_allocation_pct
        self.assertAlmostEqual(_portfolio_allocation_pct(70), 0.30)

    def test_69_returns_20pct(self):
        from app.main import _portfolio_allocation_pct
        self.assertAlmostEqual(_portfolio_allocation_pct(69), 0.20)

    def test_55_returns_20pct(self):
        from app.main import _portfolio_allocation_pct
        self.assertAlmostEqual(_portfolio_allocation_pct(55), 0.20)

    def test_54_returns_zero(self):
        from app.main import _portfolio_allocation_pct
        self.assertAlmostEqual(_portfolio_allocation_pct(54), 0.0)

    def test_100_returns_40pct(self):
        from app.main import _portfolio_allocation_pct
        self.assertAlmostEqual(_portfolio_allocation_pct(100), 0.40)


class TestDefaultLiveStateRuntime(unittest.TestCase):
    """Runtime check: _default_live_state includes portfolio keys."""

    def test_portfolio_keys_present(self):
        from app.main import _default_live_state
        state = _default_live_state()
        self.assertIn("mode", state)
        self.assertEqual(state["mode"], "single")
        self.assertIn("portfolio_positions", state)
        self.assertEqual(state["portfolio_positions"], {})
        self.assertIn("max_per_position", state)
        self.assertEqual(state["max_per_position"], 0.0)


class TestLiveRequestDefaultMode(unittest.TestCase):
    """LiveRequest defaults to single mode."""

    def test_default_mode(self):
        from app.main import LiveRequest
        req = LiveRequest()
        self.assertEqual(req.mode, "single")
        self.assertEqual(req.max_per_position, 0.0)

    def test_portfolio_mode(self):
        from app.main import LiveRequest
        req = LiveRequest(mode="portfolio", max_per_position=100.0)
        self.assertEqual(req.mode, "portfolio")
        self.assertEqual(req.max_per_position, 100.0)


class TestStatusEndpointPortfolioRuntime(unittest.TestCase):
    """live_status returns portfolio fields in portfolio mode."""

    def test_portfolio_fields_in_status(self):
        from app import main as m

        state = m._default_live_state()
        state["mode"] = "portfolio"
        state["portfolio_positions"] = {
            "BTCUSDC": {
                "symbol": "BTCUSDC",
                "position_qty": 0.001,
                "buy_price": 60000.0,
                "current_price": 61000.0,
            }
        }
        state["portfolio_free_usdc"] = 200.0
        m.live_states["__pm_status_test__"] = state

        class FakeReq:
            pass

        with patch("app.main._get_current_user",
                   return_value={"username": "__pm_status_test__"}):
            result = _run(m.live_status(FakeReq()))

        self.assertIn("portfolio_total_value", result)
        self.assertIn("portfolio_free_usdc", result)
        self.assertIn("portfolio_open_count", result)
        self.assertIn("portfolio_max_positions", result)
        self.assertEqual(result["portfolio_open_count"], 1)
        self.assertEqual(result["portfolio_max_positions"], 4)
        self.assertAlmostEqual(result["portfolio_free_usdc"], 200.0)
        # total_value = 0.001 * 61000 = 61
        self.assertAlmostEqual(result["portfolio_total_value"], 61.0, places=1)

    def test_single_mode_no_portfolio_fields(self):
        from app import main as m

        state = m._default_live_state()
        state["mode"] = "single"
        m.live_states["__pm_single_test__"] = state

        class FakeReq:
            pass

        with patch("app.main._get_current_user",
                   return_value={"username": "__pm_single_test__"}):
            result = _run(m.live_status(FakeReq()))

        # Single mode should NOT include portfolio_ aggregates
        self.assertNotIn("portfolio_total_value", result)
        self.assertNotIn("portfolio_open_count", result)


class TestResetPositionPortfolioRuntime(unittest.TestCase):
    """reset-position behaves correctly in portfolio mode."""

    def test_portfolio_reset_clears_positions(self):
        from app import main as m
        from fastapi import HTTPException

        state = m._default_live_state()
        state["running"] = True
        state["mode"] = "portfolio"
        state["portfolio_positions"] = {
            "BTCUSDC": {"symbol": "BTCUSDC", "position_qty": 0.001},
            "ETHUSDC": {"symbol": "ETHUSDC", "position_qty": 0.01},
        }
        m.live_states["__pm_reset_test__"] = state

        class FakeReq:
            pass

        with patch("app.main._get_current_user",
                   return_value={"username": "__pm_reset_test__"}), \
             patch("app.main.update_position"), \
             patch("app.main._log"):
            result = _run(m.reset_live_position(FakeReq()))

        self.assertTrue(result["ok"])
        self.assertEqual(result["cleared"], 2)
        self.assertEqual(result["position"], "FLAT")
        self.assertEqual(len(state["portfolio_positions"]), 0)

    def test_portfolio_reset_requires_running(self):
        from app import main as m
        from fastapi import HTTPException

        state = m._default_live_state()
        state["running"] = False
        state["mode"] = "portfolio"
        m.live_states["__pm_reset_notrun__"] = state

        class FakeReq:
            pass

        with patch("app.main._get_current_user",
                   return_value={"username": "__pm_reset_notrun__"}):
            with self.assertRaises(HTTPException) as ctx:
                _run(m.reset_live_position(FakeReq()))
        self.assertEqual(ctx.exception.status_code, 400)

    def test_single_mode_reset_unaffected(self):
        """Single-pair reset should work unchanged when mode=single."""
        from app import main as m

        state = m._default_live_state()
        state["running"] = True
        state["mode"] = "single"
        state["position"] = "IN_POSITION"
        state["position_qty"] = 0.01
        state["buy_price"] = 50000.0
        state["symbol"] = "BTCUSDC"
        m.live_states["__pm_single_reset__"] = state

        class FakeReq:
            pass

        with patch("app.main._get_current_user",
                   return_value={"username": "__pm_single_reset__"}), \
             patch("app.main.update_position"), \
             patch("app.main._log"), \
             patch("app.main.save_live_state_snapshot"):
            result = _run(m.reset_live_position(FakeReq()))

        self.assertTrue(result["ok"])
        self.assertEqual(result["position"], "FLAT")
        # No "cleared" key for single mode
        self.assertNotIn("cleared", result)


class TestPortfolioLoopFunction(unittest.TestCase):
    """Verify _portfolio_loop is importable and has the right signature."""

    def test_portfolio_loop_importable(self):
        from app.main import _portfolio_loop
        import inspect
        self.assertTrue(inspect.iscoroutinefunction(_portfolio_loop))

    def test_portfolio_loop_signature(self):
        from app.main import _portfolio_loop
        import inspect
        sig = inspect.signature(_portfolio_loop)
        params = list(sig.parameters.keys())
        self.assertIn("req", params)
        self.assertIn("username", params)
        self.assertIn("session_token", params)


class TestFrontendSourcePortfolio(unittest.TestCase):
    """Verify frontend files contain the required portfolio mode changes."""

    _HTML_PATH = os.path.join(_BASE, "frontend", "index.html")
    _JS_PATH   = os.path.join(_BASE, "frontend", "app.js")
    _CSS_PATH  = os.path.join(_BASE, "frontend", "style.css")

    def _html(self):
        with open(self._HTML_PATH, encoding="utf-8") as f:
            return f.read()

    def _js(self):
        with open(self._JS_PATH, encoding="utf-8") as f:
            return f.read()

    def _css(self):
        with open(self._CSS_PATH, encoding="utf-8") as f:
            return f.read()

    # ── HTML tests ──

    def test_mode_switcher_present(self):
        html = self._html()
        self.assertIn("live-mode-switcher", html)
        self.assertIn("mode-switcher-btn", html)

    def test_single_pair_button(self):
        html = self._html()
        self.assertIn("Single Pair", html)

    def test_portfolio_button(self):
        html = self._html()
        self.assertIn("Portfolio", html)

    def test_portfolio_summary_card(self):
        html = self._html()
        self.assertIn("portfolio-summary-card", html)
        self.assertIn("portfolio-total-value", html)
        self.assertIn("portfolio-total-meta", html)

    def test_portfolio_positions_card(self):
        html = self._html()
        self.assertIn("portfolio-positions-card", html)
        self.assertIn("portfolio-positions-count", html)
        self.assertIn("portfolio-positions-empty", html)
        self.assertIn("portfolio-positions", html)

    def test_portfolio_info_hint(self):
        html = self._html()
        self.assertIn("portfolio-info-hint", html)

    def test_version_bumped_css(self):
        html = self._html()
        self.assertIn("style.css?v=17", html)

    def test_version_bumped_js(self):
        html = self._html()
        self.assertIn("app.js?v=17", html)

    def test_start_button_has_label_span(self):
        html = self._html()
        self.assertIn('id="btn-live-start-label"', html)

    def test_live_amount_label_id(self):
        html = self._html()
        self.assertIn('id="live-amount-label"', html)

    def test_live_amount_hint_id(self):
        html = self._html()
        self.assertIn('id="live-amount-hint"', html)

    # ── JS tests ──

    def test_live_mode_variable(self):
        js = self._js()
        self.assertIn("let _liveMode = 'single'", js)

    def test_set_live_mode_function(self):
        js = self._js()
        self.assertIn("function setLiveMode(mode)", js)

    def test_set_mode_switcher_disabled(self):
        js = self._js()
        self.assertIn("function _setModeSwitcherDisabled", js)

    def test_start_live_sends_mode(self):
        js = self._js()
        self.assertIn("mode: _liveMode", js)
        self.assertIn("max_per_position:", js)

    def test_render_portfolio_function(self):
        js = self._js()
        self.assertIn("function renderPortfolio(state)", js)

    def test_portfolio_mode_branching_in_poll(self):
        js = self._js()
        self.assertIn("portfolioMode", js)
        self.assertIn("renderPortfolio(state)", js)

    def test_fetch_holdings_skips_portfolio(self):
        js = self._js()
        self.assertIn("if (_liveMode === 'portfolio') return", js)

    def test_save_settings_has_live_mode(self):
        js = self._js()
        self.assertIn("live_mode:", js)

    def test_load_settings_restores_mode(self):
        js = self._js()
        self.assertIn("s.live_mode", js)
        self.assertIn("setLiveMode(s.live_mode)", js)

    def test_stop_live_re_enables_switcher(self):
        js = self._js()
        # After stopLive, _setModeSwitcherDisabled(false) must be called
        self.assertIn("_setModeSwitcherDisabled(false)", js)

    def test_init_page_portfolio_resume(self):
        js = self._js()
        self.assertIn("state.mode === 'portfolio'", js)

    # ── CSS tests ──

    def test_mode_switcher_class(self):
        css = self._css()
        self.assertIn(".mode-switcher", css)

    def test_mode_switcher_btn_class(self):
        css = self._css()
        self.assertIn(".mode-switcher-btn", css)

    def test_mode_switcher_btn_active(self):
        css = self._css()
        self.assertIn(".mode-switcher-btn.active", css)

    def test_pos_card_class(self):
        css = self._css()
        self.assertIn(".pos-card", css)

    def test_pos_card_positive(self):
        css = self._css()
        self.assertIn(".pos-card--positive", css)

    def test_pos_card_negative(self):
        css = self._css()
        self.assertIn(".pos-card--negative", css)

    def test_portfolio_positions_grid(self):
        css = self._css()
        self.assertIn(".portfolio-positions", css)

    def test_pos_card_bar(self):
        css = self._css()
        self.assertIn(".pos-card-bar", css)

    def test_portfolio_total_amount(self):
        css = self._css()
        self.assertIn(".portfolio-total-amount", css)

    def test_responsive_grid(self):
        css = self._css()
        self.assertIn("min-width: 600px", css)
        self.assertIn("grid-template-columns: 1fr 1fr", css)


if __name__ == "__main__":
    unittest.main()
