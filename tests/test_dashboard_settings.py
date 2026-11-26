from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pocket_signal_bot.config import BotConfig, validate_config
from pocket_signal_bot.dashboard import coerce_settings, build_config, DEFAULT_SETTINGS
from pocket_signal_bot.runner import HybridRunner


class SettingsTests(unittest.TestCase):
    def test_unknown_keys_are_ignored(self) -> None:
        settings = coerce_settings({"nonsense": 5, "mode": "paper"})
        self.assertNotIn("nonsense", settings)
        self.assertEqual(settings["mode"], "paper")

    def test_invalid_mode_is_rejected(self) -> None:
        self.assertEqual(coerce_settings({"mode": "gambling"})["mode"], DEFAULT_SETTINGS["mode"])

    def test_numbers_are_clamped_and_typed(self) -> None:
        settings = coerce_settings({"trade_amount": "2.5", "timeframe_sec": 1, "poll_seconds": 999})
        self.assertAlmostEqual(settings["trade_amount"], 2.5)
        self.assertEqual(settings["timeframe_sec"], 5)
        self.assertEqual(settings["poll_seconds"], 60)
        self.assertIsInstance(settings["trade_amount"], float)

    def test_ema_fast_must_be_below_slow(self) -> None:
        settings = coerce_settings({"ema_fast": 100, "ema_slow": 20})
        self.assertLess(settings["ema_fast"], settings["ema_slow"])

    def test_bool_coercion(self) -> None:
        self.assertTrue(coerce_settings({"charts_auto": "false"})["charts_auto"] is False)
        self.assertTrue(coerce_settings({"charts_auto": "yes"})["charts_auto"] is True)

    def test_symbol_keeps_case(self) -> None:
        self.assertEqual(coerce_settings({"symbol": "eurusd_otc"})["symbol"], "eurusd_otc")

    def test_build_config_maps_settings(self) -> None:
        settings = coerce_settings({"mode": "paper", "timeframe_sec": 15, "expiry_sec": 15, "trade_amount": 3.0})
        cfg = build_config(settings)
        self.assertIsInstance(cfg, BotConfig)
        self.assertEqual(cfg.effective_mode, "paper")
        self.assertEqual(cfg.timeframe_sec, 15)
        self.assertEqual(cfg.expiry_sec, 15)
        self.assertAlmostEqual(cfg.trade_amount, 3.0)
        self.assertEqual(cfg.time_pair_label, "15/15")


class RunnerConstructionTests(unittest.TestCase):
    def test_paper_runner_builds_without_broker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = build_config(coerce_settings({"mode": "paper", "symbol": "EURUSD_otc"}))
            cfg.trade_data_path = str(Path(tmp) / "history.json")
            cfg.charts_dir = str(Path(tmp) / "charts")
            cfg.charts_auto = False
            cfg.po_console_log = False
            runner = HybridRunner(cfg)
            self.assertEqual(runner.status, "idle")
            self.assertFalse(runner.cfg.requires_broker)
            self.assertIsNotNone(runner._paper_feed)
            runner.trade_store.end_session()

    def test_live_requires_confirmation(self) -> None:
        cfg = build_config(coerce_settings({"mode": "live"}))
        cfg.po_session = "session"
        cfg.po_uid = "123"
        cfg.po_live_confirmed = False
        with self.assertRaises(SystemExit):
            validate_config(cfg)


if __name__ == "__main__":
    unittest.main()
