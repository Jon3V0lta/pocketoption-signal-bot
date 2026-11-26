from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pocket_signal_bot.paper_simulator import PocketPaperSimulator
from pocket_signal_bot.risk import RiskConfig, RiskManager
from pocket_signal_bot.trade_store import TradeStore, format_time_pair


class PaperSimulatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sim = PocketPaperSimulator()

    def test_call_win_and_loss(self) -> None:
        win = self.sim.settle("CALL", 1.0, 80.0, 1.0800, 1.0810)
        loss = self.sim.settle("CALL", 1.0, 80.0, 1.0800, 1.0790)
        self.assertTrue(win.won)
        self.assertAlmostEqual(win.pnl, 0.80)
        self.assertFalse(loss.won)
        self.assertAlmostEqual(loss.pnl, -1.0)

    def test_put_win(self) -> None:
        result = self.sim.settle("PUT", 2.0, 75.0, 1.0800, 1.0780)
        self.assertTrue(result.won)
        self.assertAlmostEqual(result.pnl, 1.5)

    def test_tie_returns_stake(self) -> None:
        result = self.sim.settle("PUT", 1.0, 80.0, 1.0800, 1.0800)
        self.assertAlmostEqual(result.pnl, 0.0)
        self.assertTrue(result.won)  # never counts as a loss streak


class RiskManagerTests(unittest.TestCase):
    def make(self, **kwargs) -> RiskManager:
        base = dict(
            min_payout_pct=70.0,
            max_signal_age_ms=1500,
            max_consecutive_losses=3,
            max_trades_per_day=5,
            daily_loss_stop_pct=2.0,
        )
        base.update(kwargs)
        return RiskManager(RiskConfig(**base), start_balance=1000.0)

    def test_allows_normal_trade(self) -> None:
        ok, reason = self.make().can_trade(80.0, 100, 1000.0)
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")

    def test_blocks_low_payout_and_stale_signal(self) -> None:
        manager = self.make()
        self.assertEqual(manager.can_trade(50.0, 100, 1000.0), (False, "payout_below_min"))
        self.assertEqual(manager.can_trade(80.0, 5000, 1000.0), (False, "signal_too_old"))

    def test_stops_after_consecutive_losses(self) -> None:
        manager = self.make(max_consecutive_losses=2)
        manager.register_result(False)
        manager.register_result(False)
        ok, reason = manager.can_trade(80.0, 100, 1000.0)
        self.assertFalse(ok)
        self.assertEqual(reason, "max_consecutive_losses")
        manager.register_result(True)
        self.assertTrue(manager.can_trade(80.0, 100, 1000.0)[0])

    def test_daily_loss_stop(self) -> None:
        manager = self.make(daily_loss_stop_pct=2.0)
        ok, reason = manager.can_trade(80.0, 100, 975.0)
        self.assertFalse(ok)
        self.assertEqual(reason, "daily_loss_stop")

    def test_max_trades_per_day(self) -> None:
        manager = self.make(max_trades_per_day=2)
        manager.register_result(True)
        manager.register_result(True)
        self.assertEqual(manager.can_trade(80.0, 100, 1000.0), (False, "max_trades_per_day"))


class TradeStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "history.json"
        self.store = TradeStore(str(self.path))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_requires_session(self) -> None:
        with self.assertRaises(RuntimeError):
            self.store.record_trade({"pnl": 1.0})

    def test_session_lifecycle_and_summary(self) -> None:
        session_id = self.store.begin_session({"time_pair": "60/60"})
        self.assertTrue(session_id)
        row = self.store.record_trade({"pnl": 0.8, "signal": "CALL"})
        self.assertEqual(row["trade_num"], 1)
        self.assertAlmostEqual(row["cumulative_pnl"], 0.8)
        self.store.record_trade({"pnl": -1.0, "signal": "PUT"})
        self.store.record_trade({"pnl": 0.0, "signal": "CALL"})
        active = self.store.active_session()
        self.assertIsNotNone(active)
        summary = active["summary"]
        self.assertEqual(summary["trades"], 3)
        self.assertEqual(summary["wins"], 1)
        self.assertEqual(summary["losses"], 1)
        self.assertEqual(summary["pushes"], 1)
        self.assertAlmostEqual(summary["total_pnl"], -0.2)
        self.assertAlmostEqual(summary["win_rate_pct"], 50.0)

        ended = self.store.end_session()
        self.assertIsNotNone(ended)
        self.assertIsNone(self.store.active_session())
        self.assertEqual(len(self.store.all_sessions()), 1)

    def test_persisted_between_instances(self) -> None:
        self.store.begin_session({"time_pair": "15/15"})
        self.store.record_trade({"pnl": 1.2})
        self.store.end_session()
        reloaded = TradeStore(str(self.path))
        sessions = reloaded.all_sessions()
        self.assertEqual(len(sessions), 1)
        self.assertAlmostEqual(sessions[0]["summary"]["total_pnl"], 1.2)

    def test_corrupt_file_is_ignored(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not json", encoding="utf-8")
        store = TradeStore(str(self.path))
        self.assertEqual(store.all_sessions(), [])

    def test_format_time_pair(self) -> None:
        self.assertEqual(format_time_pair(15, 15), "15/15")
        self.assertEqual(format_time_pair(60, 60), "M1/M1")
        self.assertEqual(format_time_pair(300, 180), "M5/M3")
        self.assertEqual(format_time_pair(90, 60), "90s/M1")


if __name__ == "__main__":
    unittest.main()
