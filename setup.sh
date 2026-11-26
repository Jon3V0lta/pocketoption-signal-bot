#!/usr/bin/env bash
# One-shot bootstrap: venv + dependencies + .env template.
# Usage:  ./setup.sh            (venv .venv)
#         NO_VENV=1 ./setup.sh  (install into the current interpreter)
set -euo pipefail

cd "$(dirname "$0")"
PY=${PYTHON:-python3}

echo "→ Python: $($PY -V 2>&1)"

if [[ "${NO_VENV:-0}" != "1" ]]; then
  if [[ ! -d .venv ]]; then
    echo "→ создаю виртуальное окружение .venv"
    "$PY" -m venv .venv
  fi
  # shellcheck disable=SC1091
  source .venv/bin/activate
  PY=python
fi

echo "→ обновляю pip"
"$PY" -m pip install --quiet --upgrade pip

echo "→ устанавливаю зависимости (платформенные пакеты пропускаются автоматически)"
if ! "$PY" -m pip install -r requirements.txt; then
  echo "! Полная установка не удалась — ставлю минимальный набор для paper-режима и дашборда"
  "$PY" -m pip install python-dotenv matplotlib websockets
fi

if [[ ! -f .env ]]; then
  cp .env.example .env
  echo "→ создан .env из .env.example (PO_MODE=paper)"
else
  echo "→ .env уже существует, не трогаю"
fi

echo
echo "Готово. Запуск:"
echo "  paper-режим (терминал):   $( [[ -d .venv ]] && echo 'source .venv/bin/activate && ' )python -m pocket_signal_bot.runner"
echo "  веб-дашборд:              $( [[ -d .venv ]] && echo 'source .venv/bin/activate && ' )python -m pocket_signal_bot.dashboard"
echo "  тесты:                    $( [[ -d .venv ]] && echo 'source .venv/bin/activate && ' )python -m unittest discover -s tests -t ."
echo
echo "Для demo/live дополнительно: PO_SESSION, PO_UID, python -m playwright install chromium"
