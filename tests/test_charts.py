from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

try:
    import matplotlib  # noqa: F401

    HAVE_MPL = True
except ImportError:  # pragma: no cover
    HAVE_MPL = False

from pocket_signal_bot.charts import generate_charts
from pocket_signal_bot.trade_store import TradeStore


@unittest.skipUnless(HAVE_MPL, "matplotlib is not installed")
class ChartTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data = self.root / "trading_history.json"
        self.out = self.root / "charts"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_no_data_returns_nothing(self) -> None:
        self.assertEqual(generate_charts(self.data, self.out), [])

    def test_charts_are_written_for_a_finished_session(self) -> None:
        store = TradeStore(str(self.data))
        store.begin_session({"time_pair": "15/15", "timeframe_sec": 15, "expiry_sec": 15})
        for pnl in (0.8, -1.0, 0.8, 0.0):
            store.record_trade({"pnl": pnl, "signal": "CALL" if pnl >= 0 else "PUT"})
        store.end_session()

        paths = generate_charts(self.data, self.out)
        names = {p.name for p in paths}
        self.assertIn("latest_pnl.png", names)
        self.assertIn("latest_winrate.png", names)
        self.assertIn("compare_setups.png", names)
        self.assertIn("dashboard.png", names)
        for path in paths:
            self.assertTrue(path.exists())
            self.assertGreater(path.stat().st_size, 1000)
            self.assertEqual(path.read_bytes()[:4], b"\x89PNG")


if __name__ == "__main__":
    unittest.main()
