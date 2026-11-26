from __future__ import annotations

import unittest

from pocket_signal_bot.paper_feed import PaperFeedConfig, SyntheticMarketFeed, default_start_price, pip_size


class PaperFeedTests(unittest.TestCase):
    def test_snapshot_shape(self) -> None:
        feed = SyntheticMarketFeed(PaperFeedConfig(symbol="EURUSD_otc", timeframe_sec=60, candle_count=120, seed=3))
        candles = feed.snapshot()
        self.assertEqual(len(candles), 120)
        for candle in candles[:3] + candles[-3:]:
            self.assertIn("close", candle)
            self.assertIn("time", candle)
            self.assertIsInstance(candle["close"], float)

    def test_market_moves_enough_for_strategy_filters(self) -> None:
        feed = SyntheticMarketFeed(PaperFeedConfig(timeframe_sec=60, candle_count=300, seed=11))
        for _ in range(240):
            feed.update(60)
        closes = [c["close"] for c in feed.snapshot()]
        window = closes[-20:]
        range_pct = (max(window) - min(window)) / window[-1] * 100
        # default chop filter needs >= 0.05 % over 20 candles
        self.assertGreater(range_pct, 0.05)

    def test_update_advances_candles_by_timeframe(self) -> None:
        feed = SyntheticMarketFeed(PaperFeedConfig(timeframe_sec=60, candle_count=60, seed=5))
        before = feed.snapshot()
        feed.update(60)
        after = feed.snapshot()
        self.assertNotEqual(before[-1]["close"], after[-1]["close"])
        self.assertEqual(len(after), 60)

    def test_sub_candle_update_ticks_instead_of_new_candle(self) -> None:
        feed = SyntheticMarketFeed(PaperFeedConfig(timeframe_sec=60, candle_count=60, seed=5))
        before = [c["close"] for c in feed.snapshot()]
        before_price = feed.last_price
        for _ in range(5):
            feed.update(2)  # 10 s total: still inside the same candle
        after = [c["close"] for c in feed.snapshot()]
        self.assertEqual(before[:-1], after[:-1])  # closed history untouched
        self.assertNotEqual(before_price, feed.last_price)  # forming candle moved

    def test_deterministic_with_seed(self) -> None:
        a = SyntheticMarketFeed(PaperFeedConfig(seed=42)).snapshot()
        b = SyntheticMarketFeed(PaperFeedConfig(seed=42)).snapshot()
        self.assertEqual([c["close"] for c in a], [c["close"] for c in b])

    def test_helpers(self) -> None:
        self.assertEqual(pip_size(1.08), 0.0001)
        self.assertEqual(pip_size(2350.0), 0.01)
        self.assertAlmostEqual(default_start_price("EURUSD_otc"), 1.0850)
        self.assertAlmostEqual(default_start_price("XAUUSD"), 2350.0)
        self.assertAlmostEqual(default_start_price("UNKNOWNPAIR"), 1.0850)


if __name__ == "__main__":
    unittest.main()
