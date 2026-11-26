"""Web dashboard for the PocketOption signal bot.

Runs the exact same :class:`~pocket_signal_bot.runner.HybridRunner` engine in a
background thread and exposes a small JSON API + single-page UI.

Usage::

    python -m pocket_signal_bot.dashboard            # http://127.0.0.1:8000
    PO_DASHBOARD_PORT=9000 python -m pocket_signal_bot.dashboard

Environment:
    PO_DASHBOARD_HOST   bind address (default 0.0.0.0)
    PO_DASHBOARD_PORT   port (default 8000)
    PO_DASHBOARD_AUTOSTART  "true" to start the bot immediately (default true)
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading
import time
from collections import deque
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from pocket_signal_bot.config import BotConfig, validate_config
from pocket_signal_bot.runner import HybridRunner

SETTINGS_PATH = Path(os.getenv("PO_DASHBOARD_SETTINGS", "data/dashboard_settings.json"))
CHART_FILES = {
    "latest_pnl.png",
    "latest_winrate.png",
    "compare_setups.png",
    "dashboard.png",
}
ROOT = Path(__file__).resolve().parent.parent

DEFAULT_SETTINGS: dict[str, Any] = {
    "mode": "paper",
    "symbol": "EURUSD_otc",
    "timeframe_sec": 60,
    "expiry_sec": 60,
    "trade_amount": 1.0,
    "poll_seconds": 2,
    "min_payout_pct": 70.0,
    "max_trades_per_day": 20,
    "max_consecutive_losses": 3,
    "daily_loss_stop_pct": 2.0,
    "ema_fast": 20,
    "ema_slow": 50,
    "rsi_period": 14,
    "buy_rsi_min": 50.0,
    "sell_rsi_max": 50.0,
    "rsi_neutral_band": 1.5,
    "min_range_pct": 0.05,
    "signal_confirm_polls": 2,
    "paper_seed": 0,
    "charts_auto": True,
}

# field -> (type, min, max)
FIELD_SPECS: dict[str, tuple[type, float, float]] = {
    "mode": (str, 0, 0),
    "symbol": (str, 0, 0),
    "timeframe_sec": (int, 5, 3600),
    "expiry_sec": (int, 5, 3600),
    "trade_amount": (float, 0.1, 100000.0),
    "poll_seconds": (int, 1, 60),
    "min_payout_pct": (float, 0.0, 100.0),
    "max_trades_per_day": (int, 1, 1000),
    "max_consecutive_losses": (int, 1, 1000),
    "daily_loss_stop_pct": (float, 0.1, 100.0),
    "ema_fast": (int, 2, 200),
    "ema_slow": (int, 3, 400),
    "rsi_period": (int, 2, 100),
    "buy_rsi_min": (float, 0.0, 100.0),
    "sell_rsi_max": (float, 0.0, 100.0),
    "rsi_neutral_band": (float, 0.0, 20.0),
    "min_range_pct": (float, 0.0, 5.0),
    "signal_confirm_polls": (int, 1, 20),
    "paper_seed": (int, 0, 10**9),
    "charts_auto": (bool, 0, 0),
}


def coerce_settings(raw: dict[str, Any], base: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validate/coerce incoming settings; unknown keys are ignored."""
    out = dict(base or DEFAULT_SETTINGS)
    for key, value in (raw or {}).items():
        spec = FIELD_SPECS.get(key)
        if spec is None or value is None:
            continue
        typ, lo, hi = spec
        try:
            if typ is bool:
                if isinstance(value, str):
                    out[key] = value.strip().lower() in {"1", "true", "yes", "on"}
                else:
                    out[key] = bool(value)
            elif typ is str:
                text = str(value).strip()
                if key == "mode" and text not in ("paper", "demo", "live"):
                    continue
                if key == "symbol":
                    out[key] = text.replace(" ", "")
                    continue
                out[key] = text
            else:
                num = typ(float(value))
                out[key] = max(lo, min(hi, num))
        except (TypeError, ValueError):
            continue
    if out["ema_fast"] >= out["ema_slow"]:
        out["ema_slow"] = out["ema_fast"] + 1
    return out


def load_settings() -> dict[str, Any]:
    saved: dict[str, Any] = {}
    if SETTINGS_PATH.exists():
        try:
            loaded = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                saved = loaded
        except (OSError, json.JSONDecodeError):
            saved = {}
    return coerce_settings(saved)


def save_settings(settings: dict[str, Any]) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    tmp.replace(SETTINGS_PATH)


def build_config(settings: dict[str, Any]) -> BotConfig:
    """BotConfig from .env defaults plus dashboard overrides."""
    cfg = BotConfig()
    cfg.mode = str(settings.get("mode", cfg.mode))
    for key in (
        "symbol",
        "timeframe_sec",
        "expiry_sec",
        "trade_amount",
        "poll_seconds",
        "min_payout_pct",
        "max_trades_per_day",
        "max_consecutive_losses",
        "daily_loss_stop_pct",
        "ema_fast",
        "ema_slow",
        "rsi_period",
        "buy_rsi_min",
        "sell_rsi_max",
        "rsi_neutral_band",
        "min_range_pct",
        "signal_confirm_polls",
        "paper_seed",
        "charts_auto",
    ):
        if key in settings:
            setattr(cfg, key, settings[key])
    return cfg


EVENT_LABELS: dict[str, str] = {
    "startup": "Старт",
    "signal": "Сигнал",
    "order_sent": "Ордер",
    "order_result": "Результат",
    "no_candles": "Нет свечей",
    "data_error": "Ошибка данных",
    "adapter_connect": "Подключение",
    "adapter_disconnect": "Отключение",
    "balance_init": "Баланс",
    "data_failover": "Failover данных",
    "payout_failover": "Failover payout",
    "order_failover": "Failover ордера",
    "order_error": "Ошибка ордера",
    "result_error": "Ошибка результата",
    "api_timeout": "Таймаут API",
    "payout_error": "Ошибка payout",
    "api_candles_disabled": "API свечи отключены",
}


def event_text(kind: str, payload: dict[str, Any]) -> tuple[str, str]:
    """(level, human text) for an engine event."""
    if kind == "startup":
        return "info", f"Запуск · режим {payload.get('effective_mode', '?')}"
    if kind == "signal":
        can = payload.get("can_trade")
        sig = payload.get("signal")
        raw = payload.get("raw_signal")
        if sig == "NO_TRADE" and raw in ("CALL", "PUT"):
            return (
                "info",
                f"Жду подтверждение {payload.get('confirm_streak')}/{payload.get('confirm_need')}: {raw}"
                f" · RSI {payload.get('rsi')}",
            )
        return (
            "trade" if can and sig in ("CALL", "PUT") else "info",
            (
                f"{sig} · стратегия: {raw}"
                f" · RSI {payload.get('rsi')} · EMA Δ {payload.get('ema_diff')}"
                f" · сделка {'разрешена' if can else 'запрещена'} ({payload.get('reason')})"
            ),
        )
    if kind == "order_sent":
        return (
            "trade",
            f"Ордер {payload.get('signal')} на ${payload.get('amount')} → {payload.get('adapter')} "
            f"id={payload.get('order_id')}",
        )
    if kind == "order_result":
        pnl = payload.get("pnl")
        pnl_s = f"{float(pnl):+.2f}" if isinstance(pnl, (int, float)) else "—"
        outcome = "выигрыш" if payload.get("won") else "убыток"
        if payload.get("push"):
            outcome = "возврат"
        trades = payload.get("session_trades")
        wr = payload.get("win_rate_pct")
        extra = f" · сессия: {pnl_s} ({trades} сделок" + (f", winrate {wr}%)" if wr is not None else ")")
        return (
            "win" if payload.get("won") and not payload.get("push") else "loss" if not payload.get("push") else "info",
            f"{outcome} · PnL {pnl_s}{extra}",
        )
    if kind in ("adapter_connect",):
        ok = payload.get("ok")
        return (
            "info" if ok else "warn",
            f"{payload.get('adapter')}: {'подключено' if ok else 'ошибка — ' + str(payload.get('error'))[:140]}",
        )
    if kind == "adapter_disconnect":
        return "info", f"{payload.get('adapter')}: отключено"
    if kind == "balance_init":
        return "info", f"Баланс {payload.get('adapter')}: {payload.get('balance')}"
    if kind in ("data_error", "order_error", "result_error", "payout_error", "api_timeout"):
        return "warn", f"{EVENT_LABELS.get(kind, kind)}: {str(payload.get('error') or payload.get('what') or '')[:160]}"
    if kind == "no_candles":
        return "warn", f"Нет свечей от {payload.get('adapter')} — жду данные"
    if kind.endswith("failover"):
        return "warn", f"{EVENT_LABELS.get(kind, kind)}: {payload.get('from_adapter')} → {payload.get('to_adapter')}"
    if kind == "api_candles_disabled":
        return "warn", "API-источник свечей отключён после серии ошибок"
    return "info", f"{EVENT_LABELS.get(kind, kind)}: {json.dumps(payload, ensure_ascii=False)[:180]}"


class BotController:
    """Owns the runner thread, the latest state snapshot and the event feed."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._settings = load_settings()
        self._runner: HybridRunner | None = None
        self._thread: threading.Thread | None = None
        self._state: dict[str, Any] = {}
        self._events: deque[dict[str, Any]] = deque(maxlen=250)
        self._boot_error = ""
        self._last_start_error = ""

    # ── callbacks from the runner thread ────────────────────────────────────
    def _on_state(self, state: dict[str, Any]) -> None:
        with self._lock:
            self._state = state

    def _on_event(self, kind: str, payload: dict[str, Any]) -> None:
        level, text = event_text(kind, payload)
        with self._lock:
            self._events.append(
                {
                    "ts": datetime.now(timezone.utc).strftime("%H:%M:%S"),
                    "kind": kind,
                    "level": level,
                    "text": text,
                }
            )

    # ── lifecycle ───────────────────────────────────────────────────────────
    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self, raw_settings: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._lock:
            if self.running:
                return {"ok": False, "error": "Бот уже запущен — сначала остановите его."}
            candidate = coerce_settings(raw_settings or self._settings, base=self._settings)
            cfg = build_config(candidate)
            try:
                validate_config(cfg)
            except SystemExit as exc:  # config module raises SystemExit with a message
                self._last_start_error = (
                    "Некорректная конфигурация (код %s). Для demo/live нужны PO_SESSION и PO_UID в .env, "
                    "для live — ещё и PO_LIVE_CONFIRMED=true." % exc.code
                )
                self._on_event("start_error", {"error": self._last_start_error})
                return {"ok": False, "error": self._last_start_error}
            self._settings = candidate
            try:
                save_settings(self._settings)
            except OSError:
                pass
            runner = HybridRunner(cfg, on_state=self._on_state, on_event=self._on_event)
            self._runner = runner
            self._last_start_error = ""
            thread = threading.Thread(target=self._run_thread, args=(runner,), daemon=True, name="bot-runner")
            self._thread = thread
            self._state = {
                "running": True,
                "status": "starting",
                "mode": cfg.effective_mode,
                "symbol": cfg.symbol,
                "timeframe_sec": cfg.timeframe_sec,
                "expiry_sec": cfg.expiry_sec,
                "trade_amount": cfg.trade_amount,
                "balance": 0.0,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "signal": {"signal": "NO_TRADE", "reason": "starting"},
                "indicators": {"ema_diff": 0.0, "rsi": 50.0, "momentum": 0.0},
                "closes": [],
                "session": {"pnl": 0, "trades": 0, "wins": 0, "losses": 0, "pushes": 0, "win_rate_pct": None},
                "risk": {},
                "adapters": {"api": False, "browser": False, "candles": "—", "payout": "—"},
                "trades": [],
                "last_error": "",
            }
            thread.start()
            return {"ok": True}

    def _run_thread(self, runner: HybridRunner) -> None:
        import asyncio

        try:
            asyncio.run(runner.run())
        except Exception as exc:  # keep the dashboard alive on engine crashes
            self._last_start_error = f"Движок остановлен с ошибкой: {exc}"
            self._on_event("engine_error", {"error": str(exc)})
            with self._lock:
                if self._state:
                    self._state["status"] = "error"
                    self._state["last_error"] = str(exc)
                    self._state["running"] = False

    def stop(self, wait_seconds: float = 0.0) -> dict[str, Any]:
        with self._lock:
            runner = self._runner
            thread = self._thread
        if runner is None or thread is None or not thread.is_alive():
            return {"ok": True, "message": "Бот не запущен"}
        runner.request_stop()
        if wait_seconds > 0:
            thread.join(wait_seconds)
        return {"ok": True, "message": "Останавливаю…"}

    def restart(self, raw_settings: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            target = coerce_settings(raw_settings or self._settings, base=self._settings)
        self.stop()

        def _worker() -> None:
            deadline = time.time() + 30
            while self.running and time.time() < deadline:
                time.sleep(0.2)
            self.start(target)

        threading.Thread(target=_worker, daemon=True, name="bot-restart").start()
        return {"ok": True, "message": "Перезапуск…"}

    # ── views ───────────────────────────────────────────────────────────────
    def state_payload(self) -> dict[str, Any]:
        with self._lock:
            state = dict(self._state)
            events = list(self._events)
            settings = dict(self._settings)
            running = self.running
            start_error = self._last_start_error
        state.setdefault("status", "idle")
        if not running and state.get("status") in ("starting", "connecting", "running", "settling"):
            state["status"] = "stopped"
        state["running"] = running and state.get("status") not in ("stopped", "error")
        return {
            "state": state,
            "events": events,
            "settings": settings,
            "start_error": start_error,
            "environment": environment_info(),
            "server_time": datetime.now(timezone.utc).isoformat(),
        }

    def settings(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._settings)

    def update_settings(self, raw: dict[str, Any]) -> dict[str, Any]:
        settings = coerce_settings(raw, base=self.settings())
        save_settings(settings)
        with self._lock:
            self._settings = settings
        return settings

    def charts_dir(self) -> Path:
        cfg = build_config(self.settings())
        return Path(cfg.charts_dir)

    def history_path(self) -> Path:
        cfg = build_config(self.settings())
        return Path(cfg.trade_data_path)


def environment_info() -> dict[str, Any]:
    cfg = BotConfig()
    return {
        "has_session": bool(cfg.po_session),
        "has_uid": bool(cfg.po_uid),
        "live_confirmed": bool(cfg.po_live_confirmed),
        "playwright": importlib.util.find_spec("playwright") is not None,
        "sdk": importlib.util.find_spec("pocket_option") is not None,
        "python": sys.version.split()[0],
        "env_file": (ROOT / ".env").exists(),
    }


CONTROLLER = BotController()


class Handler(BaseHTTPRequestHandler):
    server_version = "PocketSignalBot/1.0"
    protocol_version = "HTTP/1.1"
    _head_only = False

    # ── helpers ─────────────────────────────────────────────────────────────
    def _send(self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self._head_only:
            return
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, payload: Any, status: int = 200) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        raw = self.rfile.read(min(length, 1 << 20))
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}

    def log_message(self, fmt: str, *args: Any) -> None:  # keep terminal clean
        return

    # ── routes ──────────────────────────────────────────────────────────────
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            self._send(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/state":
            self._json(CONTROLLER.state_payload())
            return
        if path == "/favicon.ico":
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/api/health":
            self._json({"ok": True, "running": CONTROLLER.running})
            return
        if path == "/api/chart":
            name = (parse_qs(parsed.query).get("name") or [""])[0]
            if name not in CHART_FILES:
                self._json({"error": "unknown chart"}, 404)
                return
            path_file = CONTROLLER.charts_dir() / name
            if not path_file.exists():
                self._json({"error": "chart not generated yet"}, 404)
                return
            try:
                data = path_file.read_bytes()
            except OSError as exc:
                self._json({"error": str(exc)}, 500)
                return
            self._send(200, data, "image/png")
            return
        if path == "/api/history":
            file_path = CONTROLLER.history_path()
            if not file_path.exists():
                self._json({"error": "no history yet"}, 404)
                return
            self._send(
                200,
                file_path.read_bytes(),
                "application/json; charset=utf-8",
                {"Content-Disposition": 'attachment; filename="trading_history.json"'},
            )
            return
        self._json({"error": "not found"}, 404)

    def do_HEAD(self) -> None:  # noqa: N802
        self._head_only = True
        try:
            self.do_GET()
        finally:
            self._head_only = False

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        payload = self._read_json()
        if path == "/api/start":
            self._json(CONTROLLER.start(payload.get("settings") or payload))
            return
        if path == "/api/stop":
            self._json(CONTROLLER.stop())
            return
        if path == "/api/restart":
            self._json(CONTROLLER.restart(payload.get("settings") or payload))
            return
        if path == "/api/settings":
            settings = CONTROLLER.update_settings(payload.get("settings") or payload)
            self._json({"ok": True, "settings": settings})
            return
        self._json({"error": "not found"}, 404)


def run_server(host: str, port: int, *, autostart: bool) -> None:
    if autostart and not CONTROLLER.running:
        result = CONTROLLER.start(CONTROLLER.settings())
        if not result.get("ok"):
            print(f"[dashboard] автозапуск не удался: {result.get('error')}", flush=True)
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    print(f"[dashboard] http://{host}:{port}  (Ctrl+C — остановить)", flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\n[dashboard] остановка…", flush=True)
    finally:
        CONTROLLER.stop(wait_seconds=20)
        server.server_close()


def main() -> None:
    host = os.getenv("PO_DASHBOARD_HOST", "0.0.0.0")
    port = int(os.getenv("PO_DASHBOARD_PORT", os.getenv("PORT", "8000")))
    autostart = os.getenv("PO_DASHBOARD_AUTOSTART", "true").strip().lower() in {"1", "true", "yes", "on"}
    run_server(host, port, autostart=autostart)


INDEX_HTML = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PocketOption Signal Bot</title>
<style>
  :root{
    --bg:#0b0f16; --panel:#131a24; --panel2:#182131; --line:#223047;
    --text:#e6edf7; --muted:#8fa3bf; --green:#2ecc71; --red:#ef5350;
    --blue:#4aa3ff; --amber:#f5a524; --gray:#7b8a9f;
  }
  *{box-sizing:border-box}
  body{margin:0;background:radial-gradient(1200px 600px at 80% -10%,#16243a 0%,var(--bg) 55%);
       color:var(--text);font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Ubuntu,sans-serif;}
  header{display:flex;flex-wrap:wrap;gap:12px;align-items:center;justify-content:space-between;
         padding:16px 20px;border-bottom:1px solid var(--line);background:rgba(11,15,22,.85);
         position:sticky;top:0;z-index:20;backdrop-filter:blur(8px)}
  .brand{display:flex;gap:10px;align-items:center;font-weight:700;letter-spacing:.2px}
  .logo{width:28px;height:28px;border-radius:8px;background:linear-gradient(135deg,#4aa3ff,#2ecc71);
        display:grid;place-items:center;font-size:15px}
  .pill{display:inline-flex;gap:7px;align-items:center;padding:5px 11px;border-radius:999px;
        border:1px solid var(--line);background:var(--panel);font-size:12px;color:var(--muted)}
  .dot{width:8px;height:8px;border-radius:50%;background:var(--gray)}
  .dot.on{background:var(--green);box-shadow:0 0 10px var(--green)}
  .dot.err{background:var(--red)}
  .dot.wait{background:var(--amber)}
  button{font:inherit;border-radius:10px;border:1px solid var(--line);padding:9px 16px;cursor:pointer;
         background:var(--panel2);color:var(--text);transition:.15s}
  button:hover{border-color:#365a86;transform:translateY(-1px)}
  button.primary{background:linear-gradient(135deg,#2f9e63,#2ecc71);border:none;color:#04210f;font-weight:700}
  button.danger{background:linear-gradient(135deg,#b7443f,#ef5350);border:none;color:#fff;font-weight:600}
  button:disabled{opacity:.45;cursor:not-allowed;transform:none}
  main{max-width:1320px;margin:0 auto;padding:20px;display:grid;gap:16px}
  .grid{display:grid;gap:16px}
  .kpis{grid-template-columns:repeat(auto-fit,minmax(160px,1fr))}
  .cols{grid-template-columns:minmax(0,1.55fr) minmax(0,1fr)}
  @media(max-width:980px){.cols{grid-template-columns:1fr}}
  .card{background:linear-gradient(180deg,rgba(24,33,49,.9),rgba(19,26,36,.9));border:1px solid var(--line);
        border-radius:16px;padding:16px}
  .card h2{margin:0 0 12px;font-size:12px;text-transform:uppercase;letter-spacing:.12em;color:var(--muted)}
  .kpi .label{font-size:11px;text-transform:uppercase;letter-spacing:.1em;color:var(--muted)}
  .kpi .value{font-size:24px;font-weight:700;margin-top:6px;font-variant-numeric:tabular-nums}
  .kpi .sub{font-size:12px;color:var(--muted);margin-top:2px}
  .pos{color:var(--green)} .neg{color:var(--red)} .neutral{color:var(--muted)}
  .signal{display:flex;flex-direction:column;gap:10px}
  .signal-row{display:flex;align-items:center;gap:14px;flex-wrap:wrap}
  .badge{font-size:34px;font-weight:800;letter-spacing:.06em;padding:10px 22px;border-radius:14px;
         background:#1c2739;color:var(--gray);min-width:190px;text-align:center;transition:.25s}
  .badge.call{background:rgba(46,204,113,.14);color:var(--green);box-shadow:inset 0 0 0 1px rgba(46,204,113,.4)}
  .badge.put{background:rgba(239,83,80,.14);color:var(--red);box-shadow:inset 0 0 0 1px rgba(239,83,80,.4)}
  .badge.wait{background:rgba(143,163,191,.1);color:var(--muted);box-shadow:inset 0 0 0 1px rgba(143,163,191,.25)}
  .signal-meta{display:flex;flex-direction:column;gap:3px;min-width:180px}
  .signal-meta b{font-size:15px}
  .tag{display:inline-block;padding:2px 8px;border-radius:8px;background:#1c2739;font-size:11px;color:var(--muted)}
  .bars{display:grid;gap:10px}
  .bar-row{display:grid;grid-template-columns:96px 1fr auto;gap:10px;align-items:center;font-size:12px;color:var(--muted)}
  .track{height:8px;border-radius:999px;background:#0e1520;overflow:hidden}
  .fill{height:100%;width:0%;border-radius:999px;background:linear-gradient(90deg,#4aa3ff,#2ecc71);transition:width .3s}
  .fill.warn{background:linear-gradient(90deg,#f5a524,#ef5350)}
  canvas{width:100%;height:260px;display:block}
  table{width:100%;border-collapse:collapse;font-size:12.5px}
  th,td{text-align:left;padding:7px 8px;border-bottom:1px solid rgba(34,48,71,.6);white-space:nowrap}
  th{color:var(--muted);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.08em}
  tbody tr:hover{background:rgba(74,163,255,.06)}
  .log{height:230px;overflow:auto;font:12px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace;
       display:flex;flex-direction:column-reverse;gap:2px}
  .log div{color:var(--muted);border-left:2px solid transparent;padding-left:8px}
  .log .l-trade{border-color:var(--blue);color:#cfe3ff}
  .log .l-win{border-color:var(--green);color:#c6f6d9}
  .log .l-loss{border-color:var(--red);color:#ffd6d4}
  .log .l-warn{border-color:var(--amber);color:#ffe6b8}
  .log .l-info{border-color:var(--line)}
  form.settings{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
  label{display:flex;flex-direction:column;gap:4px;font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted)}
  input,select{font:inherit;background:#0e1520;border:1px solid var(--line);color:var(--text);
               border-radius:9px;padding:8px 10px;width:100%}
  input:focus,select:focus{outline:none;border-color:#4aa3ff}
  .row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
  .hint{font-size:12px;color:var(--muted)}
  .banner{border:1px solid rgba(245,165,36,.35);background:rgba(245,165,36,.08);color:#ffe0a8;
          border-radius:12px;padding:10px 14px;font-size:13px}
  .banner.err{border-color:rgba(239,83,80,.4);background:rgba(239,83,80,.1);color:#ffc9c7}
  .chartwrap{position:relative}
  .chart-legend{position:absolute;top:6px;right:8px;font-size:11px;color:var(--muted);display:flex;gap:10px}
  .swatch{display:inline-block;width:10px;height:3px;border-radius:2px;margin-right:5px;vertical-align:middle}
  .charts img{width:100%;border-radius:12px;border:1px solid var(--line);background:#0e1520}
  .subtabs{display:flex;gap:6px;margin-bottom:10px;flex-wrap:wrap}
  .subtabs button{padding:6px 10px;font-size:12px;border-radius:8px;background:#0e1520}
  .subtabs button.active{border-color:#4aa3ff;color:#cfe3ff}
  .foot{color:var(--muted);font-size:12px;text-align:center;padding:8px 0 22px}
</style>
</head>
<body>
<header>
  <div class="brand"><div class="logo">◆</div><div>PocketOption Signal Bot<div style="font-size:11px;color:var(--muted);font-weight:400">EMA + RSI · risk-managed</div></div></div>
  <div class="row">
    <span class="pill"><span id="statusDot" class="dot"></span><span id="statusText">остановлен</span></span>
    <span class="pill" id="modePill">PAPER</span>
    <span class="pill" id="pairPill">—</span>
    <span class="pill" id="uptimePill">—</span>
    <button id="btnStart" class="primary">▶ Запустить</button>
    <button id="btnStop" class="danger">■ Стоп</button>
  </div>
</header>

<main>
  <div id="banner" class="banner" style="display:none"></div>

  <div class="grid kpis">
    <div class="card kpi"><div class="label">Баланс</div><div class="value" id="kBalance">—</div><div class="sub" id="kBalanceSub">paper</div></div>
    <div class="card kpi"><div class="label">PnL сессии</div><div class="value" id="kPnl">—</div><div class="sub" id="kPnlSub">0 сделок</div></div>
    <div class="card kpi"><div class="label">Win rate</div><div class="value" id="kWin">—</div><div class="sub" id="kWinSub">W/L/P = 0/0/0</div></div>
    <div class="card kpi"><div class="label">Сделок сегодня</div><div class="value" id="kTrades">—</div><div class="sub" id="kTradesSub">лимит —</div></div>
    <div class="card kpi"><div class="label">Payout</div><div class="value" id="kPayout">—</div><div class="sub" id="kPayoutSub">мин. —</div></div>
  </div>

  <div class="grid cols">
    <div class="card">
      <h2>Текущий сигнал</h2>
      <div class="signal">
        <div class="signal-row">
          <div id="signalBadge" class="badge wait">—</div>
          <div class="signal-meta">
            <b id="signalReason">ожидание данных</b>
            <span class="tag" id="signalConfirm">подтверждение —</span>
            <span class="tag" id="signalPrice">цена —</span>
          </div>
        </div>
        <div class="hint" id="signalHint"></div>
        <div class="chartwrap">
          <div class="chart-legend">
            <span><span class="swatch" style="background:#4aa3ff"></span>цена</span>
            <span><span class="swatch" style="background:#f5a524"></span>EMA fast</span>
            <span><span class="swatch" style="background:#8b5cf6"></span>EMA slow</span>
          </div>
          <canvas id="chart"></canvas>
        </div>
      </div>
    </div>

    <div class="card">
      <h2>Индикаторы и риск</h2>
      <div class="bars">
        <div class="bar-row"><span>RSI</span><div class="track"><div class="fill" id="rsiFill"></div></div><span id="rsiVal">—</span></div>
        <div class="bar-row"><span>EMA Δ</span><div class="track"><div class="fill" id="emaFill"></div></div><span id="emaVal">—</span></div>
        <div class="bar-row"><span>Momentum</span><div class="track"><div class="fill" id="momFill"></div></div><span id="momVal">—</span></div>
        <div class="bar-row"><span>Лимит сделок</span><div class="track"><div class="fill" id="tradeFill"></div></div><span id="tradeVal">—</span></div>
        <div class="bar-row"><span>Серия убытков</span><div class="track"><div class="fill warn" id="lossFill"></div></div><span id="lossVal">—</span></div>
      </div>
      <div class="hint" id="riskHint" style="margin-top:12px"></div>
      <div class="hint" id="adapterHint" style="margin-top:6px"></div>
    </div>
  </div>

  <div class="grid cols">
    <div class="card">
      <h2>Сделки сессии</h2>
      <div style="max-height:280px;overflow:auto">
        <table>
          <thead><tr><th>#</th><th>Время</th><th>Сигнал</th><th>Ставка</th><th>Payout</th><th>PnL</th><th>Cum</th><th>Источник</th></tr></thead>
          <tbody id="tradesBody"><tr><td colspan="8" class="hint">Сделок пока нет</td></tr></tbody>
        </table>
      </div>
    </div>
    <div class="card">
      <h2>Журнал событий</h2>
      <div class="log" id="log"></div>
    </div>
  </div>

  <div class="card charts">
    <h2>Графики</h2>
    <div class="subtabs">
      <button data-chart="latest_pnl.png" class="active">PnL</button>
      <button data-chart="latest_winrate.png">Win rate</button>
      <button data-chart="compare_setups.png">Сравнение</button>
      <button data-chart="dashboard.png">Дашборд</button>
    </div>
    <img id="chartImg" alt="chart" src="/api/chart?name=latest_pnl.png">
  </div>

  <div class="card">
    <h2>Настройки бота</h2>
    <form class="settings" id="settingsForm">
      <label>Режим
        <select name="mode">
          <option value="paper">paper — симуляция</option>
          <option value="demo">demo — счёт PocketOption</option>
          <option value="live">live — реальные деньги</option>
        </select>
      </label>
      <label>Инструмент<input name="symbol" placeholder="EURUSD_otc"></label>
      <label>Свеча, сек<input name="timeframe_sec" type="number" min="5" max="3600"></label>
      <label>Экспирация, сек<input name="expiry_sec" type="number" min="5" max="3600"></label>
      <label>Ставка, $<input name="trade_amount" type="number" step="0.1" min="0.1"></label>
      <label>Опрос, сек<input name="poll_seconds" type="number" min="1" max="60"></label>
      <label>EMA fast<input name="ema_fast" type="number" min="2"></label>
      <label>EMA slow<input name="ema_slow" type="number" min="3"></label>
      <label>RSI период<input name="rsi_period" type="number" min="2"></label>
      <label>RSI покупка ≥<input name="buy_rsi_min" type="number" step="0.1"></label>
      <label>RSI продажа ≤<input name="sell_rsi_max" type="number" step="0.1"></label>
      <label>Мин. диапазон, %<input name="min_range_pct" type="number" step="0.01"></label>
      <label>Подтверждений<input name="signal_confirm_polls" type="number" min="1"></label>
      <label>Мин. payout, %<input name="min_payout_pct" type="number" step="1"></label>
      <label>Сделок в день<input name="max_trades_per_day" type="number" min="1"></label>
      <label>Стоп-серия убытков<input name="max_consecutive_losses" type="number" min="1"></label>
      <label>Дневной стоп, %<input name="daily_loss_stop_pct" type="number" step="0.1"></label>
      <label>Seed симуляции<input name="paper_seed" type="number" min="0"></label>
    </form>
    <div class="row" style="margin-top:12px">
      <button id="btnSave" class="primary">Сохранить и перезапустить</button>
      <button id="btnPreset60">Пресет 60/60</button>
      <button id="btnPreset15">Пресет 15/15</button>
      <a class="pill" href="/api/history" download>Скачать историю JSON</a>
    </div>
    <div class="hint" id="envHint" style="margin-top:10px"></div>
  </div>

  <div class="foot">Бот не гарантирует прибыль. Проверяйте стратегию на demo и paper перед реальными деньгами.</div>
</main>

<script>
const $ = (id) => document.getElementById(id);
const fmt = (v, d = 2) => (v === null || v === undefined || Number.isNaN(v)) ? '—' : Number(v).toFixed(d);
const money = (v) => (v > 0 ? '+' : '') + fmt(v, 2);
const cls = (v) => v > 0 ? 'pos' : v < 0 ? 'neg' : 'neutral';

const REASON_RU = {
  strict_trigger: 'Строгий сигнал EMA + RSI',
  vote_fallback: 'Сигнал по голосованию индикаторов',
  chop_filter: 'Флэт: диапазон рынка слишком узкий',
  trend_streak: 'Нет устойчивого тренда EMA',
  rsi_neutral: 'RSI в нейтральной зоне',
  weak_trend: 'Слабый тренд EMA',
  fallback_disabled: 'Fallback выключен — строгий режим',
  insufficient_data: 'Мало данных для расчёта',
  no_data: 'Нет данных от источника',
  starting: 'Запуск…',
};
const RISK_RU = {
  ok: 'риск в норме', daily_loss_stop: 'дневной стоп по убытку',
  max_trades_per_day: 'достигнут лимит сделок в день',
  max_consecutive_losses: 'стоп по серии убытков',
  payout_below_min: 'payout ниже минимума', signal_too_old: 'сигнал устарел',
  no_candles: 'нет свечей',
};
const STATUS_RU = {
  idle: 'остановлен', starting: 'запуск…', connecting: 'подключение…', running: 'работает',
  settling: 'ожидание экспирации…', stopping: 'остановка…', stopped: 'остановлен', error: 'ошибка',
};

let lastChartKey = '';
let activeChart = 'latest_pnl.png';
let settingsLoaded = false;
let renderedTrades = 0;

function applyState(payload) {
  const s = payload.state || {};
  const env = payload.environment || {};
  const st = s.status || 'idle';
  const running = !!s.running;

  $('statusText').textContent = STATUS_RU[st] || st;
  const dot = $('statusDot');
  dot.className = 'dot ' + (st === 'error' ? 'err' : running ? (st === 'settling' ? 'wait' : 'on') : '');
  $('modePill').textContent = (s.mode || 'paper').toUpperCase();
  $('pairPill').textContent = `${s.symbol || '—'} · ${s.timeframe_sec || '—'}/${s.expiry_sec || '—'}`;
  if (s.started_at && running) {
    const sec = Math.max(0, (Date.now() - new Date(s.started_at).getTime()) / 1000);
    const m = Math.floor(sec / 60), h = Math.floor(m / 60);
    $('uptimePill').textContent = '⏱ ' + (h ? `${h}ч ${m % 60}м` : `${m}м ${Math.floor(sec % 60)}с`);
  } else { $('uptimePill').textContent = '—'; }

  $('btnStart').disabled = running;
  $('btnStop').disabled = !running;

  const banner = $('banner');
  if (payload.start_error) { banner.style.display = ''; banner.className = 'banner err'; banner.textContent = payload.start_error; }
  else if (s.last_error) { banner.style.display = ''; banner.className = 'banner'; banner.textContent = 'Последняя ошибка: ' + s.last_error; }
  else banner.style.display = 'none';

  const sess = s.session || {};
  const pnl = sess.pnl || 0;
  $('kBalance').textContent = '$' + fmt(s.balance, 2);
  $('kBalanceSub').textContent = (s.mode === 'paper') ? 'виртуальный баланс' : 'баланс счёта';
  $('kPnl').textContent = money(pnl) + ' $';
  $('kPnl').className = 'value ' + cls(pnl);
  $('kPnlSub').textContent = `${sess.trades || 0} сделок, ${sess.pushes || 0} возвратов`;
  $('kWin').textContent = sess.win_rate_pct === null || sess.win_rate_pct === undefined ? '—' : fmt(sess.win_rate_pct, 1) + '%';
  $('kWin').className = 'value ' + (sess.win_rate_pct >= 50 ? 'pos' : sess.win_rate_pct === null || sess.win_rate_pct === undefined ? '' : 'neg');
  $('kWinSub').textContent = `W/L/P = ${sess.wins || 0}/${sess.losses || 0}/${sess.pushes || 0}`;
  const risk = s.risk || {};
  $('kTrades').textContent = `${risk.trades_today || 0}`;
  $('kTradesSub').textContent = `лимит ${risk.max_trades_per_day || '—'} · стоп ${risk.max_consecutive_losses || '—'} подряд`;
  const payout = s.payout_pct || 0;
  $('kPayout').textContent = payout ? fmt(payout, 1) + '%' : '—';
  $('kPayoutSub').textContent = `мин. ${payload.settings?.min_payout_pct ?? '—'}% · ${s.adapters?.payout || '—'}`;

  const sig = s.signal || {};
  const badge = $('signalBadge');
  const side = sig.signal || 'NO_TRADE';
  badge.textContent = side === 'NO_TRADE' ? 'НЕТ ВХОДА' : side;
  badge.className = 'badge ' + (side === 'CALL' ? 'call' : side === 'PUT' ? 'put' : 'wait');
  $('signalReason').textContent = REASON_RU[sig.reason] || sig.reason || '—';
  const raw = sig.raw_signal && sig.raw_signal !== side ? ` · сырой: ${sig.raw_signal}` : '';
  const streak = Math.min(sig.confirm_streak ?? 0, sig.confirm_need ?? 0);
  $('signalConfirm').textContent = `подтверждение ${streak}/${sig.confirm_need ?? '—'}${raw}`;
  $('signalPrice').textContent = sig.last_close ? `цена ${fmt(sig.last_close, 5)}` : 'цена —';
  const can = sig.can_trade;
  $('signalHint').textContent = sig.signal_age_ms === undefined ? '' :
    `Сделка ${can ? 'разрешена' : 'запрещена'}${can ? '' : ' — ' + (RISK_RU[sig.risk_reason] || sig.risk_reason)} · свечи: ${s.adapters?.candles || '—'} · возраст сигнала ${sig.signal_age_ms} мс`;

  const ind = s.indicators || {};
  const rsi = ind.rsi ?? 50;
  setBar('rsiFill', rsi, Math.abs(rsi - 50) < 1.5);
  $('rsiVal').textContent = fmt(rsi, 1);
  const emaAbs = Math.abs(ind.ema_diff || 0);
  setBar('emaFill', Math.min(100, emaAbs * 3500), false, ind.ema_diff >= 0 ? '#2ecc71' : '#ef5350');
  $('emaVal').textContent = (ind.ema_diff >= 0 ? '+' : '') + (ind.ema_diff ?? 0).toFixed(6);
  setBar('momFill', Math.min(100, Math.abs(ind.momentum || 0) * 25000), false, ind.momentum >= 0 ? '#2ecc71' : '#ef5350');
  $('momVal').textContent = (ind.momentum >= 0 ? '+' : '') + (ind.momentum ?? 0).toFixed(6);
  setBar('tradeFill', ((risk.trades_today || 0) / Math.max(1, risk.max_trades_per_day || 1)) * 100, (risk.trades_today || 0) >= (risk.max_trades_per_day || 1));
  $('tradeVal').textContent = `${risk.trades_today || 0}/${risk.max_trades_per_day || '—'}`;
  setBar('lossFill', ((risk.consecutive_losses || 0) / Math.max(1, risk.max_consecutive_losses || 1)) * 100, (risk.consecutive_losses || 0) >= (risk.max_consecutive_losses || 1));
  $('lossVal').textContent = `${risk.consecutive_losses || 0}/${risk.max_consecutive_losses || '—'}`;

  $('riskHint').textContent = risk.halted_reason
    ? '⛔ Торговля остановлена риском: ' + (RISK_RU[risk.halted_reason] || risk.halted_reason)
    : `Старт дня: $${fmt(risk.day_start_balance, 2)} · дневной стоп ${risk.daily_loss_stop_pct ?? '—'}%`;
  const ad = s.adapters || {};
  $('adapterHint').textContent = `Источники — свечи: ${ad.candles || '—'}, payout: ${ad.payout || '—'}` +
    (s.mode === 'paper' ? ' · синтетический рынок' : ` · API ${ad.api ? '✓' : '✗'}, браузер ${ad.browser ? '✓' : '✗'}`);

  renderTrades(s.trades || []);
  renderLog(payload.events || []);
  drawChart(s.closes || [], s);
  refreshChartImage();

  $('envHint').innerHTML = envHint(env, s);
  if (!settingsLoaded && payload.settings) { fillSettings(payload.settings); settingsLoaded = true; }
}

function envHint(env, s) {
  const parts = [];
  parts.push(`Python ${env.python || ''}`);
  parts.push(env.env_file ? '.env найден' : '.env не найден (значения по умолчанию)');
  parts.push(env.has_session && env.has_uid ? 'ключи PocketOption есть' : 'PO_SESSION/PO_UID не заданы');
  parts.push(env.playwright ? 'Playwright установлен' : 'Playwright не установлен');
  if (s.mode === 'live' && !env.live_confirmed) parts.push('⚠ live заблокирован: PO_LIVE_CONFIRMED!=true');
  return parts.join(' · ');
}

function setBar(id, pct, warn, color) {
  const el = $(id);
  el.style.width = Math.max(0, Math.min(100, pct || 0)) + '%';
  el.classList.toggle('warn', !!warn);
  if (color) { el.style.background = color; } else { el.style.background = ''; }
}

function renderTrades(trades) {
  const body = $('tradesBody');
  if (!trades.length) { body.innerHTML = '<tr><td colspan="8" class="hint">Сделок пока нет</td></tr>'; renderedTrades = 0; return; }
  const rows = trades.slice().reverse().map(t => {
    const p = Number(t.pnl || 0);
    const time = (t.ts || '').replace('T', ' ').slice(11, 19);
    return `<tr>
      <td>${t.trade_num ?? '—'}</td>
      <td>${time}</td>
      <td><span class="tag" style="color:${t.signal === 'CALL' ? 'var(--green)' : 'var(--red)'}">${t.signal || '—'}</span></td>
      <td>$${fmt(t.amount, 2)}</td>
      <td>${fmt(t.payout_pct, 0)}%</td>
      <td class="${cls(p)}">${money(p)}</td>
      <td class="${cls(t.cumulative_pnl)}">${money(t.cumulative_pnl)}</td>
      <td>${t.adapter || '—'}</td>
    </tr>`;
  }).join('');
  body.innerHTML = rows;
  renderedTrades = trades.length;
}

let lastLogFirst = '';
function renderLog(events) {
  const log = $('log');
  if (!events.length) { log.innerHTML = '<div class="l-info">Журнал пуст</div>'; return; }
  const first = events[0].ts + events[0].text;
  if (first === lastLogFirst) return;
  lastLogFirst = first;
  log.innerHTML = events.slice(-120).map(e =>
    `<div class="l-${e.level}"><span style="opacity:.6">${e.ts}</span> ${escapeHtml(e.text)}</div>`
  ).join('');
}

function escapeHtml(s) { return String(s).replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

function ema(values, period) {
  if (values.length < period) return [];
  const k = 2 / (period + 1);
  let prev = values.slice(0, period).reduce((a, b) => a + b, 0) / period;
  const out = [prev];
  for (let i = period; i < values.length; i++) { prev = values[i] * k + prev * (1 - k); out.push(prev); }
  return out;
}

function drawChart(closes, s) {
  const canvas = $('chart');
  const dpr = window.devicePixelRatio || 1;
  const w = canvas.clientWidth, h = canvas.clientHeight;
  if (canvas.width !== w * dpr || canvas.height !== h * dpr) {
    canvas.width = w * dpr; canvas.height = h * dpr;
  }
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, h);
  if (!closes.length) {
    ctx.fillStyle = '#5b6d85'; ctx.font = '13px sans-serif'; ctx.textAlign = 'center';
    ctx.fillText('Нет данных — запустите бота', w / 2, h / 2);
    return;
  }
  const n = closes.length;
  const fast = ema(closes, s.timeframe_sec ? (Number(s.ema_fast) || 20) : 20);
  const slow = ema(closes, Number(s.ema_slow) || 50);
  const all = closes.concat(fast, slow).filter(v => Number.isFinite(v));
  let min = Math.min(...all), max = Math.max(...all);
  if (max - min < 1e-9) { max += 1e-5; min -= 1e-5; }
  const pad = (max - min) * 0.12; min -= pad; max += pad;
  const X = (i) => 34 + (i / Math.max(1, n - 1)) * (w - 44);
  const Y = (v) => 12 + (1 - (v - min) / (max - min)) * (h - 34);

  // grid
  ctx.strokeStyle = 'rgba(34,48,71,.7)'; ctx.lineWidth = 1;
  ctx.fillStyle = '#6f8199'; ctx.font = '10px ui-monospace, monospace'; ctx.textAlign = 'left';
  for (let g = 0; g <= 4; g++) {
    const v = min + (max - min) * (g / 4), y = Y(v);
    ctx.beginPath(); ctx.moveTo(34, y); ctx.lineTo(w - 8, y); ctx.stroke();
    ctx.fillText(v.toFixed(5), 2, y - 3);
  }

  // price area
  ctx.beginPath();
  closes.forEach((v, i) => i ? ctx.lineTo(X(i), Y(v)) : ctx.moveTo(X(i), Y(v)));
  const grad = ctx.createLinearGradient(0, 0, 0, h);
  grad.addColorStop(0, 'rgba(74,163,255,.28)'); grad.addColorStop(1, 'rgba(74,163,255,0)');
  ctx.save();
  ctx.lineWidth = 1.8; ctx.strokeStyle = '#4aa3ff';
  ctx.beginPath(); closes.forEach((v, i) => i ? ctx.lineTo(X(i), Y(v)) : ctx.moveTo(X(i), Y(v))); ctx.stroke();
  ctx.lineTo(X(n - 1), h - 22); ctx.lineTo(X(0), h - 22); ctx.closePath();
  ctx.fillStyle = grad; ctx.fill(); ctx.restore();

  // EMAs
  const drawEma = (arr, color) => {
    if (!arr.length) return;
    ctx.beginPath(); ctx.lineWidth = 1.4; ctx.strokeStyle = color;
    arr.forEach((v, i) => {
      const idx = n - arr.length + i;
      idx ? ctx.lineTo(X(idx), Y(v)) : ctx.moveTo(X(idx), Y(v));
    });
    ctx.stroke();
  };
  drawEma(fast, '#f5a524'); drawEma(slow, '#8b5cf6');

  // last price label
  const last = closes[n - 1];
  const y = Y(last);
  ctx.fillStyle = '#4aa3ff'; ctx.beginPath(); ctx.arc(X(n - 1), y, 3.2, 0, Math.PI * 2); ctx.fill();
  ctx.fillStyle = '#0b0f16'; ctx.font = 'bold 11px ui-monospace, monospace'; ctx.textAlign = 'right';
  const label = last.toFixed(5);
  const tw = ctx.measureText(label).width + 10;
  ctx.fillStyle = '#4aa3ff';
  ctx.fillRect(w - tw - 4, y - 17, tw, 15);
  ctx.fillStyle = '#04121f'; ctx.fillText(label, w - 8, y - 6);
}

let chartTick = 0;
function refreshChartImage() {
  if (++chartTick % 5 !== 0) return;             // refresh the PNG every ~5 s
  const img = $('chartImg');
  img.style.display = '';
  img.src = `/api/chart?name=${encodeURIComponent(activeChart)}&t=${Date.now()}`;
  img.onerror = () => {
    img.style.display = 'none';
    let note = document.getElementById('chartNote');
    if (!note) {
      note = document.createElement('div');
      note.id = 'chartNote';
      note.className = 'hint';
      note.style.padding = '28px 0';
      note.style.textAlign = 'center';
      img.parentNode.appendChild(note);
    }
    note.textContent = 'График появится после первой закрытой сделки.';
  };
  img.onload = () => {
    const note = document.getElementById('chartNote');
    if (note) note.remove();
  };
}

function fillSettings(st) {
  const form = $('settingsForm');
  Object.entries(st).forEach(([k, v]) => {
    const el = form.elements[k];
    if (el) el.value = v;
  });
}

function collectSettings() {
  const form = $('settingsForm');
  const out = {};
  Object.keys(form.elements).forEach((k) => {
    const el = form.elements[k];
    if (!el || !el.name) return;
    out[el.name] = el.type === 'number' ? Number(el.value) : el.value;
  });
  return out;
}

async function post(path, body) {
  const res = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}) });
  return res.json().catch(() => ({}));
}

async function poll() {
  try {
    const res = await fetch('/api/state', { cache: 'no-store' });
    applyState(await res.json());
  } catch (e) {
    $('statusText').textContent = 'нет связи с ботом';
    $('statusDot').className = 'dot err';
  }
}

$('btnStart').onclick = async () => { await post('/api/start', { settings: collectSettings() }); poll(); };
$('btnStop').onclick = async () => { await post('/api/stop'); poll(); };
$('btnSave').onclick = async () => {
  const st = collectSettings();
  if (st.mode === 'live' && !confirm('Режим LIVE использует реальные деньги. Продолжить?')) return;
  await post('/api/restart', { settings: st });
  poll();
};
$('btnPreset60').onclick = () => { fillSettings({ timeframe_sec: 60, expiry_sec: 60, min_range_pct: 0.05, signal_confirm_polls: 2 }); };
$('btnPreset15').onclick = () => { fillSettings({ timeframe_sec: 15, expiry_sec: 15, min_range_pct: 0.02, signal_confirm_polls: 2 }); };

document.querySelectorAll('.subtabs button').forEach((btn) => {
  btn.onclick = () => {
    document.querySelectorAll('.subtabs button').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    activeChart = btn.dataset.chart;
    chartTick = 0;
    refreshChartImage();
    poll();
  };
});

window.addEventListener('resize', () => { lastChartKey = ''; });
poll();
setInterval(poll, 1000);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
