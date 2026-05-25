"""
Tests for feature 20260525-1200: risk-reduction changes.
Covers:
  - _SETTINGS_DEFAULTS new keys and values
  - save_user_settings whitelist includes new keys
  - LiveRequest defaults
  - risk_agent.sl_atr_mult default
  - _SCAN_FALLBACK_* pair lists
  - _register_sell_outcome cooldown on pnl_pct < 0 (not only was_sl)
  - BEAR_TREND hard veto in main source
  - vol_x < 0.5 hard veto in main source
  - Portfolio scan BEAR_TREND / vol_x skips in main source
"""
import sys
import os
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.knowledge_store import _SETTINGS_DEFAULTS, save_user_settings, load_user_settings
from app.main import (
    LiveRequest,
    _SCAN_FALLBACK_TOP,
    _SCAN_FALLBACK_UNDERDOGS,
    _SCAN_EXTENDED_TOP,
    _SCAN_EXTENDED_UNDERDOGS,
    _register_sell_outcome,
)
from app import risk_agent
import inspect


# ── Helper ───────────────────────────────────────────────────────────────────

def _live_req(**kwargs):
    defaults = dict(api_key="k", api_secret="s", symbol="BTCUSDC", interval="4h")
    defaults.update(kwargs)
    return LiveRequest(**defaults)


def _live_state_stub():
    return {
        "cooldowns": {},
        "trade_history": [],
        "loss_streak": 0,
    }


# ── 1. knowledge_store._SETTINGS_DEFAULTS ────────────────────────────────────

class TestSettingsDefaults(unittest.TestCase):

    def test_live_min_confidence_default_70(self):
        self.assertEqual(_SETTINGS_DEFAULTS["live_min_confidence"], 70)

    def test_live_sl_mult_default_1_0(self):
        self.assertEqual(_SETTINGS_DEFAULTS["live_sl_mult"], 1.0)

    def test_live_cooldown_candles_default_2(self):
        self.assertEqual(_SETTINGS_DEFAULTS["live_cooldown_candles"], 2)

    def test_live_compounding_mode_default_fixed(self):
        self.assertEqual(_SETTINGS_DEFAULTS["live_compounding_mode"], "fixed")

    def test_save_user_settings_persists_live_min_confidence(self):
        """save_user_settings must not drop live_min_confidence."""
        import tempfile, json
        from unittest.mock import patch
        # We test by calling save_user_settings with the new keys and checking
        # they are written (not silently dropped due to whitelist mismatch).
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_path = os.path.join(tmpdir, "admin", "settings.json")
            os.makedirs(os.path.dirname(fake_path), exist_ok=True)
            with patch("app.knowledge_store._user_settings_path", return_value=fake_path):
                save_user_settings("admin", {
                    "live_min_confidence": 75,
                    "live_sl_mult": 1.0,
                    "live_cooldown_candles": 3,
                    "live_compounding_mode": "fixed",
                })
                with open(fake_path) as f:
                    saved = json.load(f)
        self.assertEqual(saved["live_min_confidence"], 75)
        self.assertEqual(saved["live_sl_mult"], 1.0)
        self.assertEqual(saved["live_cooldown_candles"], 3)
        self.assertEqual(saved["live_compounding_mode"], "fixed")


# ── 2. LiveRequest defaults ───────────────────────────────────────────────────

class TestLiveRequestDefaults(unittest.TestCase):

    def test_min_confidence_default_70(self):
        req = _live_req()
        self.assertEqual(req.min_confidence, 70)

    def test_compounding_mode_default_fixed(self):
        req = _live_req()
        self.assertEqual(req.compounding_mode, "fixed")

    def test_sl_atr_mult_default_1_0(self):
        req = _live_req()
        self.assertEqual(req.sl_atr_mult, 1.0)

    def test_cooldown_candles_default_2(self):
        req = _live_req()
        self.assertEqual(req.cooldown_candles, 2)

    def test_sim_min_confidence_unchanged(self):
        """SimRequest.min_confidence must remain 55."""
        from app.main import SimRequest
        sim = SimRequest()
        self.assertEqual(sim.min_confidence, 55)


# ── 3. risk_agent.sl_atr_mult default ─────────────────────────────────────────

class TestRiskAgentDefault(unittest.TestCase):

    def test_sl_atr_mult_signature_default_1_0(self):
        sig = inspect.signature(risk_agent.calculate_risk_params)
        param = sig.parameters["sl_atr_mult"]
        self.assertEqual(param.default, 1.0)


# ── 4. Pairs lists ───────────────────────────────────────────────────────────

class TestScanPairs(unittest.TestCase):

    def test_fallback_top_8_approved(self):
        expected = ["BTCUSDC", "ETHUSDC", "SOLUSDC", "BNBUSDC",
                    "XRPUSDC", "LINKUSDC", "AVAXUSDC", "ADAUSDC"]
        self.assertEqual(_SCAN_FALLBACK_TOP, expected)

    def test_fallback_underdogs_empty(self):
        self.assertEqual(_SCAN_FALLBACK_UNDERDOGS, [])

    def test_extended_top_empty(self):
        self.assertEqual(_SCAN_EXTENDED_TOP, [])

    def test_extended_underdogs_empty(self):
        self.assertEqual(_SCAN_EXTENDED_UNDERDOGS, [])


# ── 5. _register_sell_outcome cooldown on pnl_pct < 0 ────────────────────────

class TestRegisterSellOutcome(unittest.TestCase):

    def _run(self, pnl_pct, was_sl=False, cooldown_candles=2):
        req = _live_req(cooldown_candles=cooldown_candles, max_consecutive_losses=0)
        state = _live_state_stub()
        _register_sell_outcome(
            state, "BTCUSDC", pnl_pct, was_sl, 14400, req
        )
        return state

    def test_negative_pnl_triggers_cooldown_even_without_sl(self):
        """Loss that is NOT a stop-loss should still trigger cooldown."""
        state = self._run(pnl_pct=-2.5, was_sl=False, cooldown_candles=2)
        self.assertIn("BTCUSDC", state["cooldowns"])
        self.assertGreater(state["cooldowns"]["BTCUSDC"], time.time())

    def test_positive_pnl_no_cooldown(self):
        """Winning trade must NOT trigger cooldown."""
        state = self._run(pnl_pct=3.0, was_sl=False, cooldown_candles=2)
        self.assertNotIn("BTCUSDC", state["cooldowns"])

    def test_sl_hit_also_triggers_cooldown(self):
        """SL hit is a loss — cooldown must still fire (pnl_pct < 0)."""
        state = self._run(pnl_pct=-1.5, was_sl=True, cooldown_candles=2)
        self.assertIn("BTCUSDC", state["cooldowns"])

    def test_zero_cooldown_candles_no_cooldown_even_on_loss(self):
        """cooldown_candles=0 means cooldown disabled."""
        state = self._run(pnl_pct=-5.0, was_sl=True, cooldown_candles=0)
        self.assertNotIn("BTCUSDC", state["cooldowns"])


# ── 6. Veto blocks in main.py source ─────────────────────────────────────────

def _main_src():
    import app.main as m
    return inspect.getsource(m)


class TestVetoBlocksInSource(unittest.TestCase):

    def test_bear_trend_hard_veto_single_mode(self):
        src = _main_src()
        self.assertIn('regime_str == "BEAR_TREND" and action == "BUY"', src)
        self.assertIn("BUY blockiert: BEAR_TREND-Regime", src)

    def test_vol_x_hard_veto_single_mode(self):
        src = _main_src()
        self.assertIn("volume_ratio", src)
        self.assertIn("vol_x", src)
        self.assertIn("< 0.5", src)
        self.assertIn("BUY blockiert: vol_x=", src)

    def test_bear_trend_skip_portfolio_scan(self):
        src = _main_src()
        self.assertIn('regime.get("regime") == "BEAR_TREND"', src)
        self.assertIn("BEAR_TREND — kein neuer Kauf", src)

    def test_vol_x_skip_portfolio_scan(self):
        src = _main_src()
        self.assertIn("_vol_x_p", src)
        self.assertIn("Volumen zu niedrig, kein Kauf", src)


if __name__ == "__main__":
    unittest.main()
