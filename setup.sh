#!/usr/bin/env bash
# Enkel uppsättning för macOS/Linux (bash).
#
# Kör från projektroten:   ./setup.sh
# Hoppa över lokal Whisper: ./setup.sh --no-whisper
#
# Skapar en venv, installerar appen (som paket, så kommandot `predikan`
# blir tillgängligt), kopierar .env-example -> .env om den saknas och
# skriver ut nästa steg. Rör aldrig en befintlig .env.
#
# OBS: lokal Whisper (torch/ctranslate2) har färdiga paket för Python
# 3.11-3.13. Kör du en nyare Python och installationen av Whisper fallerar,
# skapa venv med en 3.11-3.13-tolk eller kör med --no-whisper och använd
# OpenAI Whisper API i stället (sätts i inställningsguiden).

set -euo pipefail
cd "$(dirname "$0")"

NO_WHISPER=0
if [ "${1:-}" = "--no-whisper" ]; then NO_WHISPER=1; fi

echo "==> Skapar virtuell miljö (venv/)..."
[ -d venv ] || python3 -m venv venv
PY="./venv/bin/python"

echo "==> Uppdaterar pip..."
"$PY" -m pip install --upgrade pip

if [ "$NO_WHISPER" -eq 1 ]; then
  echo "==> Installerar appen (utan lokal Whisper)..."
  "$PY" -m pip install -e .
else
  echo "==> Installerar appen med lokal Whisper (faster-whisper)..."
  "$PY" -m pip install -e ".[faster-whisper]"
fi

if [ ! -f .env ]; then
  echo "==> Skapar .env från .env-example..."
  cp .env-example .env
else
  echo "==> .env finns redan - lämnar den orörd."
fi

echo ""
echo "Klart! Starta appen med:"
echo "    ./venv/bin/predikan"
echo "Öppna sedan http://127.0.0.1:8000 och fyll i resten under fliken '⚙️ Inställningar'."
