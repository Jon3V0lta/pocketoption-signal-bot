"""Synthetic market feed used by ``PO_MODE=paper``.

The original paper mode returned an almost flat, frozen series, so the
strategy never produced a CALL/PUT and the chop filter always blocked
trading.  This feed generates a realistic random walk with slowly
switching drift regimes, so the EMA/RSI engine sees a market that
actually moves (and therefore trades).
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

# A few plausible starting prices so the chart looks sane for common symbols.
_DEFAULT_START_PRICES: dict[str, float] = {
    "EURUSD": 1.0850,
    "GBPUSD": 1.2700,
    "AUDUSD": 0.6600,
    "NZDUSD": 0.6000,
    "USDCAD": 1.3600,
    "USDCHF": 0.8800,
    "USDJPY": 150.00,
    "EURJPY": 162.00,
    "GBPJPY": 190.00,
    "AUDCAD": 0.9000,
    "EURGBP": 0.8550,
    "XAUUSD": 2350.00,
    "BTCUSD": 65000.00,
}


def pip_size(price: float) -> float:
    """Forex-style pip size for ~1.x quotes, larger tick for high-priced assets."""
    if price >= 20.0:
        return 0.01
    return 0.0001


def default_start_price(symbol: str) -> float:
    key = (symbol or "").upper().replace("_OTC", "").replace("-OTC", "").replace("/", "")
    for name, price in _DEFAULT_START_PRICES.items():
        if key.startswith(name):
            return price
    return 1.0850


@dataclass
class PaperFeedConfig:
    symbol: str = "EURUSD_otc"
    timeframe_sec: int = 60
    candle_count: int = 300
    # Std-dev of one candle in pips (scaled by sqrt(timeframe/60)).
    volatility_pips: float = 1.3
    # Average number of candles a drift regime lasts.
    regime_candles: int = 26
    # Max regime drift in pips per candle.
    max_drift_pips: float = 0.45
    start_price: float | None = None
    seed: int | None = None


class SyntheticMarketFeed:
    """Generates a rolling synthetic candle series (one close per candle)."""

    def __init__(self, cfg: PaperFeedConfig):
        self.cfg = cfg
        self.start_price = float(cfg.start_price or default_start_price(cfg.symbol))
        self._pip = pip_size(self.start_price)
        self._rng = random.Random(cfg.seed)
        self._price = self.start_price
        self._drift_pips = 0.0
        self._regime_left = 0
        self._count = max(3, int(cfg.candle_count))
        self._acc_sec = 0.0
        self._history: list[float] = []
        for _ in range(self._count):
            self._history.append(self._next_price())

    # ── internals ────────────────────────────────────────────────────────────
    def _roll_regime(self) -> None:
        self._regime_left = max(4, int(self._rng.gauss(self.cfg.regime_candles, 6)))
        self._drift_pips = self._rng.uniform(-self.cfg.max_drift_pips, self.cfg.max_drift_pips)

    def _next_price(self) -> float:
        if self._regime_left <= 0:
            self._roll_regime()
        self._regime_left -= 1

        scale = math.sqrt(max(1, int(self.cfg.timeframe_sec)) / 60.0)
        shock = self._rng.gauss(0.0, max(0.05, float(self.cfg.volatility_pips)) * scale)
        step = (self._drift_pips + shock) * self._pip
        self._price = max(self._pip * 10, self._price + step)
        return self._price

    # ── public API ───────────────────────────────────────────────────────────
    @property
    def last_price(self) -> float:
        return self._history[-1]

    def advance(self, candles: int = 1) -> None:
        for _ in range(max(1, int(candles))):
            self._history.append(self._next_price())
            if len(self._history) > self._count:
                del self._history[0]

    def tick(self, dt_sec: float) -> None:
        """Move the current (forming) candle's close by a sub-candle shock."""
        if dt_sec <= 0:
            return
        scale = math.sqrt(max(1.0, float(dt_sec)) / 60.0)
        shock = self._rng.gauss(0.0, max(0.02, float(self.cfg.volatility_pips)) * scale)
        price = max(self._pip * 10, self._history[-1] + shock * self._pip)
        self._history[-1] = price

    def update(self, dt_sec: float) -> None:
        """Advance the feed by wall-clock seconds.

        New candles appear once per ``timeframe_sec``; in between the current
        candle is ticked, exactly like a live feed delivering quotes.
        """
        if dt_sec <= 0:
            return
        self._acc_sec += min(float(dt_sec), 300.0)
        step = max(1, int(self.cfg.timeframe_sec))
        whole = int(self._acc_sec // step)
        if whole > 0:
            self._acc_sec -= whole * step
            self.advance(whole)
        else:
            self.tick(dt_sec)

    def snapshot(self, count: int | None = None) -> list[dict[str, Any]]:
        """Return the series in the shape the adapters use: ``[{"close", "time"}]``."""
        n = min(len(self._history), int(count or self._count))
        closes = self._history[-n:]
        now = datetime.now(timezone.utc)
        step = max(1, int(self.cfg.timeframe_sec))
        return [
            {
                "close": round(close, 5),
                "time": (now - timedelta(seconds=(n - 1 - i) * step)).isoformat(),
            }
            for i, close in enumerate(closes)
        ]
