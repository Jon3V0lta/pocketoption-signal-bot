from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from pocket_signal_bot import dashboard as dash


class EventTextTests(unittest.TestCase):
    def test_signal_events_are_human_readable(self) -> None:
        level, text = dash.event_text(
            "signal",
            {
                "signal": "CALL",
                "raw_signal": "CALL",
                "can_trade": True,
                "reason": "ok",
                "rsi": 61.2,
                "ema_diff": 0.0001,
            },
        )
        self.assertEqual(level, "trade")
        self.assertIn("CALL", text)
        self.assertIn("61.2", text)

    def test_pending_confirmation_is_explained(self) -> None:
        _, text = dash.event_text(
            "signal",
            {"signal": "NO_TRADE", "raw_signal": "PUT", "confirm_streak": 1, "confirm_need": 2, "rsi": 40},
        )
        self.assertIn("подтверждение 1/2", text)

    def test_order_result_levels(self) -> None:
        win = dash.event_text("order_result", {"won": True, "pnl": 0.8, "session_trades": 3, "win_rate_pct": 66.7})
        loss = dash.event_text("order_result", {"won": False, "pnl": -1.0, "push": False})
        push = dash.event_text("order_result", {"won": True, "pnl": 0.0, "push": True})
        self.assertEqual(win[0], "win")
        self.assertEqual(loss[0], "loss")
        self.assertIn("возврат", push[1])

    def test_unknown_event_does_not_crash(self) -> None:
        level, text = dash.event_text("something_new", {"a": 1})
        self.assertEqual(level, "info")
        self.assertIn("something_new", text)


class ControllerLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self._orig_settings = dash.SETTINGS_PATH
        self._orig_build = dash.build_config
        dash.SETTINGS_PATH = Path(self.tmp.name) / "settings.json"
        root = Path(self.tmp.name)

        def isolated(settings):
            cfg = self._orig_build(settings)
            cfg.trade_data_path = str(root / "history.json")
            cfg.charts_dir = str(root / "charts")
            cfg.po_console_log = False
            return cfg

        dash.build_config = isolated

    def tearDown(self) -> None:
        dash.SETTINGS_PATH = self._orig_settings
        dash.build_config = self._orig_build
        self.tmp.cleanup()

    def test_start_and_stop_paper_bot(self) -> None:
        controller = dash.BotController()
        result = controller.start(
            {
                "mode": "paper",
                "symbol": "EURUSD_otc",
                "timeframe_sec": 60,
                "expiry_sec": 5,
                "poll_seconds": 1,
                "paper_seed": 3,
                "charts_auto": False,
            }
        )
        self.assertTrue(result["ok"], result)
        deadline = time.time() + 10
        while time.time() < deadline and not controller.state_payload()["state"].get("closes"):
            time.sleep(0.2)
        payload = controller.state_payload()
        self.assertTrue(payload["state"]["running"])
        self.assertTrue(payload["state"]["closes"])
        self.assertTrue(payload["events"])

        controller.stop(wait_seconds=20)
        self.assertFalse(controller.running)
        self.assertEqual(controller.state_payload()["state"]["status"], "stopped")

    def test_cannot_start_twice(self) -> None:
        controller = dash.BotController()
        self.assertTrue(controller.start({"mode": "paper", "poll_seconds": 1})["ok"])
        second = controller.start({"mode": "paper"})
        self.assertFalse(second["ok"])
        controller.stop(wait_seconds=20)

    def test_demo_mode_without_credentials_is_refused(self) -> None:
        controller = dash.BotController()
        result = controller.start({"mode": "demo"})
        self.assertFalse(result["ok"])
        self.assertIn("PO_SESSION", result["error"])
        self.assertFalse(controller.running)


if __name__ == "__main__":
    unittest.main()
