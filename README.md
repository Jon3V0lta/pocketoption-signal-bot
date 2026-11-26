# Hybrid Trading Bots (MT5 + PocketOption)

Two independent bot paths live in this repo:

- `mt5_signal_bot.py` — MT5 EUR/USD signal bot (Windows only)
- `pocket_signal_bot/` — PocketOption hybrid signal bot (`paper` / `demo` / `live`) **with a web dashboard**

> No strategy guarantees profit. Treat this as research/automation infrastructure and validate on
> `paper` and `demo` first. `PO_MODE=live` trades real money and requires an explicit confirmation flag.

## Быстрый старт (RU)

```bash
./setup.sh                       # venv + зависимости + .env из .env.example
source .venv/bin/activate
python -m pocket_signal_bot.dashboard
```

Откройте `http://127.0.0.1:8000` — дашборд сам запускает бота в **paper**-режиме:
синтетический рынок, сигналы EMA+RSI, сделки, PnL, win rate, PNG-графики и живой журнал событий.

- Кнопка **Стоп / Запустить** — управление движком, настройки внизу страницы.
- Пресеты **60/60** и **15/15** — быстрый A/B-тест таймфрейма и экспирации.
- Símbolo/ставка/риск-лимиты меняются в UI и сохраняются в `data/dashboard_settings.json`.
- Терминальный режим (без UI): `python -m pocket_signal_bot.runner`.

## Quick start (EN)

```bash
./setup.sh                       # venv + deps + .env copy
source .venv/bin/activate
python -m pocket_signal_bot.dashboard      # web UI on :8000, autostarts paper mode
python -m pocket_signal_bot.runner         # headless terminal mode
python -m unittest discover -s tests -t .  # 49 unit tests
```

Only `python-dotenv` + `matplotlib` are needed for paper mode and the dashboard; Playwright and the
unofficial SDK are optional and only used for `demo`/`live`.

## What was assembled

The repo previously contained the strategy, risk manager, adapters and trade storage, but three
things prevented it from actually running as a bot:

1. **Paper mode never traded.** `_paper_candles()` regenerated an almost perfectly flat series, so
   the chop filter (`PO_MIN_RANGE_PCT`) blocked every signal. Replaced with a synthetic market feed
   (`paper_signal_bot/paper_feed.py`) that does a realistic random walk with drift regimes and
   sub-candle ticks.
2. **No visibility.** The loop only printed log lines. `HybridRunner` now publishes a state snapshot
   on every poll (`on_state`), streams events (`on_event`) and supports `request_stop()`, which the
   new dashboard (`pocket_signal_bot/dashboard.py`, stdlib only) uses for live UI + start/stop.
3. **Signals were unexplainable.** `generate_details()` now returns a `reason`
   (`strict_trigger`, `vote_fallback`, `chop_filter`, `rsi_neutral`, `trend_streak`,
   `fallback_disabled`, `insufficient_data`) so the UI can show *why* there is no trade.

Also: dependency markers so `pip install -r requirements.txt` works on Linux/macOS, 49 unit tests,
`setup.sh`, `.env.example` additions, and removed committed `__pycache__` artifacts.

## PocketOption bot architecture

- EMA + RSI signal engine (`CALL` / `PUT` / `NO_TRADE`) with trend-streak, chop and neutral-RSI filters
- Signal post-processing: consecutive-poll confirmation and optional flip cooldown
- Risk controls: daily loss stop, max losses in a row, max trades/day, min payout, stale-signal guard
- Hybrid adapters: unofficial API adapter (primary) + Playwright browser adapter (fallback)
- Trade history JSON + auto-generated PNG charts (`latest_pnl`, `latest_winrate`, `compare_setups`, `dashboard`)
- Web dashboard: live signal, indicators, risk usage, trades, event log, charts, settings

### Files

| Path | Role |
|------|------|
| `pocket_signal_bot/config.py` | `.env` → `BotConfig`, live-mode safety validation |
| `pocket_signal_bot/strategy.py` | EMA/RSI engine + explainable `reason` |
| `pocket_signal_bot/risk.py` | daily stop, streak stop, payout/age gates |
| `pocket_signal_bot/paper_feed.py` | synthetic market for `PO_MODE=paper` |
| `pocket_signal_bot/paper_simulator.py` | binary-contract settlement |
| `pocket_signal_bot/trade_store.py` | sessions/trades JSON persistence |
| `pocket_signal_bot/charts.py` | PNG charts (matplotlib) |
| `pocket_signal_bot/adapters/api_adapter.py` | unofficial `pocket-option` SDK adapter |
| `pocket_signal_bot/adapters/browser_adapter.py` | Playwright/WS-quote browser fallback |
| `pocket_signal_bot/runner.py` | the trading loop (state hooks + graceful stop) |
| `pocket_signal_bot/dashboard.py` | HTTP server + single-page UI |
| `tests/` | `unittest` suite (`python -m unittest discover -s tests -t .`) |

## Install

```bash
pip install -r requirements.txt
```

`MetaTrader5` is installed only on Windows, `pocket-option` only on Python ≥ 3.13 (see the
environment markers in `requirements.txt`). If you use the browser fallback:

```bash
python -m playwright install chromium
```

## Dashboard

```bash
python -m pocket_signal_bot.dashboard
# http://127.0.0.1:8000  (PO_DASHBOARD_HOST/PORT to change)
```

| Endpoint | Purpose |
|----------|---------|
| `GET /api/state` | status, signal, indicators, candles, session stats, trades, events |
| `POST /api/start` / `POST /api/stop` | start / stop the engine |
| `POST /api/restart` | stop, save settings, start again |
| `GET /api/chart?name=latest_pnl.png` | generated PNG chart |
| `GET /api/history` | download `data/trading_history.json` |

`PO_DASHBOARD_AUTOSTART=true` (default) starts the bot in paper mode right away.

## Environment file

```bash
cp .env.example .env
```

`pocket_signal_bot/config.py` loads `.env` automatically when `python-dotenv` is installed.

### PocketOption credentials (demo first)

- `PO_SESSION` — session token from the PocketOption web auth flow
- `PO_UID` — your account uid
- `PO_IS_DEMO=true`
- `PO_MODE=paper|demo|live` (live needs `PO_LIVE_CONFIRMED=true`)
- Optional: `PO_SYMBOL=EURUSD_otc`, `PO_TIMEFRAME_SEC=60`, `PO_EXPIRY_SEC=60`

### Demo/live run (terminal)

```bash
# demo
PO_MODE=demo PO_SESSION=... PO_UID=... python -m pocket_signal_bot.runner

# live (real money — refused unless confirmed)
PO_MODE=live PO_LIVE_CONFIRMED=true PO_IS_DEMO=false PO_SESSION=... PO_UID=... \
  python -m pocket_signal_bot.runner
```

The same modes are selectable in the dashboard. Without Playwright + valid cookies the bot logs
connection errors and keeps retrying; paper mode needs neither.

### Demo mode: fix “Could not read current price from UI”

1. Set `PO_HEADLESS=false` and `PO_BROWSER_STARTUP_WAIT_SEC=15` (or higher on slow PCs).
2. Run the bot; a Chromium window opens on the quick-trading URL.
3. **Log in** and wait until the live quote is visible.
4. In normal Chrome: **F12 → Elements → select → click the price → Copy → Copy selector**.
5. Put it in `.env` as `PO_PRICE_SELECTOR=...` (use `|` between several selectors).
6. Re-run. Browser candles work once the selector matches.

The bot logs `data_error` and retries instead of exiting when a quote read fails.

If the chart price is drawn on a **canvas**, keep `PO_USE_WS_QUOTES=true` (default): the adapter reads
page WebSocket `update_quotes` payloads for `PO_SYMBOL`, no selector needed.

## Trade history & PNG charts

Each run stores trades in `data/trading_history.json`. After every settled trade (with
`PO_CHARTS_AUTO=true`) PNGs are written to `data/charts/`:

| File | Content |
|------|---------|
| `latest_pnl.png` | cumulative PnL + per-trade bars (current session) |
| `latest_winrate.png` | win rate over trade number |
| `compare_setups.png` | compare **15/15**, **30/30**, **60/60**, **M5/M3** sessions |
| `dashboard.png` | all sessions’ PnL curves overlaid |

**A/B workflow** — change the setup in the UI (or `.env`), run demo, stop:

```env
PO_TIMEFRAME_SEC=15
PO_EXPIRY_SEC=15
```

Regenerate charts anytime:

```bash
python -m pocket_signal_bot.charts
```

`PO_EXPERIMENT_LABEL=15/15` overrides the auto label.

## Paper mode notes

The synthetic feed uses a random walk with slowly switching drift regimes, volatility
`PO_PAPER_VOLATILITY_PIPS` (1.3 pips per 60 s candle, scaled by `sqrt(timeframe/60)`), sub-candle
ticks every poll, and a new candle per `timeframe_sec`. `PO_PAPER_SEED>0` makes a run reproducible.

Short timeframes are noisier and their 20-candle range is smaller — if the UI shows
*«Флэт: диапазон рынка слишком узкий»*, lower **Мин. диапазон, %** (e.g. `0.02`) or use a larger
timeframe. This is the same chop filter that protects demo/live trading.

## Readiness checklist before live

- At least 200+ demo trades logged
- Positive expectancy with realistic payout conditions
- No recurring adapter disconnect loop
- Latency and failover behavior verified in logs
- Risk limits tested (daily stop, max losses, max trades)
