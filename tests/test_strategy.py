from __future__ import annotations

import unittest

from pocket_signal_bot.strategy import EmaRsiStrategy, StrategyConfig, ema, rsi


def cfg(**kwargs) -> StrategyConfig:
    base = dict(
        ema_fast=5,
        ema_slow=10,
        rsi_period=5,
        buy_rsi_min=50.0,
        sell_rsi_max=50.0,
        rsi_neutral_band=0.0,
        min_ema_gap=0.0,
        require_momentum_confirm=False,
        min_abs_ema_diff=0.0,
        allow_fallback_vote=True,
        min_trend_streak=1,
        chop_lookback=5,
        min_range_pct=0.0,
    )
    base.update(kwargs)
    return StrategyConfig(**base)


class IndicatorTests(unittest.TestCase):
    def test_ema_shape_and_seed(self) -> None:
        values = [float(i) for i in range(20)]
        out = ema(values, 5)
        self.assertEqual(len(out), len(values))
        self.assertTrue(all(v is None for v in out[:4]))
        self.assertAlmostEqual(out[4], sum(values[:5]) / 5)

    def test_ema_requires_full_period(self) -> None:
        self.assertEqual(ema([1.0, 2.0], 5), [None, None])

    def test_rsi_in_range_and_extremes(self) -> None:
        up = [1.0 + i for i in range(30)]
        down = [30.0 - i for i in range(30)]
        self.assertAlmostEqual(max(v for v in rsi(up, 14) if v is not None), 100.0)
        self.assertAlmostEqual(min(v for v in rsi(down, 14) if v is not None), 0.0)
        side = [1.0, 1.1, 1.05, 1.15, 1.12, 1.2, 1.18, 1.25, 1.22, 1.3] * 4
        for value in rsi(side, 14):
            if value is not None:
                self.assertGreaterEqual(value, 0.0)
                self.assertLessEqual(value, 100.0)


class StrategySignalTests(unittest.TestCase):
    def test_insufficient_data(self) -> None:
        details = EmaRsiStrategy(cfg()).generate_details([1.0, 1.1])
        self.assertEqual(details["signal"], "NO_TRADE")
        self.assertEqual(details["reason"], "insufficient_data")

    def test_uptrend_call(self) -> None:
        prices = [1.0 + i * 0.001 for i in range(60)]
        details = EmaRsiStrategy(cfg()).generate_details(prices)
        self.assertEqual(details["signal"], "CALL")
        self.assertEqual(details["reason"], "strict_trigger")
        self.assertGreater(details["ema_diff"], 0)

    def test_downtrend_put(self) -> None:
        prices = [1.0 - i * 0.001 for i in range(60)]
        details = EmaRsiStrategy(cfg()).generate_details(prices)
        self.assertEqual(details["signal"], "PUT")
        self.assertLess(details["ema_diff"], 0)

    def test_chop_filter_blocks_flat_market(self) -> None:
        prices = [1.08 + (0.000001 if i % 2 else -0.000001) for i in range(60)]
        details = EmaRsiStrategy(cfg(min_range_pct=0.5)).generate_details(prices)
        self.assertEqual(details["signal"], "NO_TRADE")
        self.assertEqual(details["reason"], "chop_filter")

    def test_neutral_rsi_zone(self) -> None:
        prices = [1.08 + (0.0002 if i % 2 else -0.0002) for i in range(60)]
        details = EmaRsiStrategy(cfg(rsi_neutral_band=5.0, min_range_pct=0.0)).generate_details(prices)
        if details["reason"] == "rsi_neutral":
            self.assertEqual(details["signal"], "NO_TRADE")
        else:
            self.assertIn(details["reason"], {"trend_streak", "vote_fallback", "strict_trigger"})

    def test_trend_streak_blocks_reversal(self) -> None:
        prices = [1.0 + i * 0.001 for i in range(55)] + [1.055 - i * 0.002 for i in range(5)]
        details = EmaRsiStrategy(cfg(min_trend_streak=4)).generate_details(prices)
        self.assertEqual(details["signal"], "NO_TRADE")

    def test_vote_fallback_and_disabled_fallback(self) -> None:
        # Rising trend (EMA fast > slow) with a short pullback: RSI < 50 and a
        # negative 3-bar momentum, so no strict trigger fires.
        prices = [1.08 + i * 0.00006 for i in range(80)] + [1.08 + 79 * 0.00006 - 0.00008 * i for i in range(4)]
        voting = EmaRsiStrategy(cfg(allow_fallback_vote=True)).generate_details(prices)
        self.assertEqual(voting["reason"], "vote_fallback")
        self.assertEqual(voting["signal"], "PUT")
        strict = EmaRsiStrategy(cfg(allow_fallback_vote=False)).generate_details(prices)
        self.assertEqual(strict["signal"], "NO_TRADE")
        self.assertEqual(strict["reason"], "fallback_disabled")

    def test_signal_matches_generate(self) -> None:
        prices = [1.0 + i * 0.001 for i in range(60)]
        strategy = EmaRsiStrategy(cfg())
        self.assertEqual(strategy.generate(prices), strategy.generate_details(prices)["signal"])

    def test_details_always_include_reason_and_indicators(self) -> None:
        strategy = EmaRsiStrategy(cfg())
        for prices in ([], [1.0], [1.0, 1.1, 1.2], [1.0 + i * 0.001 for i in range(80)]):
            details = strategy.generate_details(prices)
            self.assertIn("signal", details)
            self.assertIn("reason", details)
            self.assertIn("ema_diff", details)
            self.assertIn("rsi", details)
            self.assertIn("momentum", details)


if __name__ == "__main__":
    unittest.main()
