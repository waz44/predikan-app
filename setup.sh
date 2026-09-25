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
# OBS: lokal Whisper installeras via faster-whisper (1.2.1), som har färdiga
# paket även för Python 3.14. Skulle installationen ändå fallera på din
# Python-version, kör med --no-whisper och använd OpenAI Whisper API i stället
# (sätts i inställningsguiden) - eller installera en äldre Python (3.11-3.13).
#
# Skriptet går att köra flera gånger: en befintlig venv och .env återanvänds,
# och pip installerar bara om det som ändrats. Kör det igen efter "git pull"
# för att få nya paket som en ny version av appen behöver.

# -e: avbryt vid första fel. -u: fel om en odefinierad variabel används.
# -o pipefail: ett fel i en del av en pipe (a | b) räknas som fel.
set -euo pipefail
# Kör alltid från mappen där skriptet ligger, oavsett var det startades
# ifrån - alla sökvägar nedan är relativa till projektroten.
cd "$(dirname "$0")"

# Läs flaggan --no-whisper. "${1:-}" ger en tom sträng om inget argument
# skickades (annars skulle "set -u" stoppa skriptet).
NO_WHISPER=0
if [ "${1:-}" = "--no-whisper" ]; then NO_WHISPER=1; fi

# Steg 1: en virtuell miljö (venv) i projektmappen. Den håller appens paket
# åtskilda från resten av datorns Python-installation. Finns den redan
# återanvänds den ("[ -d venv ] ||" = skapa bara om mappen saknas).
echo "==> Skapar virtuell miljö (venv/)..."
[ -d venv ] || python3 -m venv venv
# All installation nedan görs med venv:ens egen Python, så att paketen
# hamnar i venv:en - utan att venv:en behöver "aktiveras" först.
PY="./venv/bin/python"

# Steg 2: en aktuell pip klarar nyare paketformat och hittar färdigbyggda
# paket i större utsträckning (slipper kompilera).
echo "==> Uppdaterar pip..."
"$PY" -m pip install --upgrade pip

# Steg 3: installera appen. "-e" (redigerbart läge) betyder att appen körs
# direkt från projektmappen - ändringar och "git pull" gäller utan ny
# installation, och uploads/, processed/ m.m. hamnar i projektmappen.
if [ "$NO_WHISPER" -eq 1 ]; then
  echo "==> Installerar appen (utan lokal Whisper)..."
  "$PY" -m pip install -e .
else
  # ".[faster-whisper]" tar med extrapaketen för lokal transkribering (se
  # [project.optional-dependencies] i pyproject.toml) - även KB-Whisper.
  echo "==> Installerar appen med lokal Whisper (faster-whisper)..."
  "$PY" -m pip install -e ".[faster-whisper]"
fi

# Steg 4: skapa .env från mallen första gången. En befintlig .env skrivs
# aldrig över - den innehåller dina nycklar och inställningar.
if [ ! -f .env ]; then
  echo "==> Skapar .env från .env-example..."
  cp .env-example .env
else
  echo "==> .env finns redan - lämnar den orörd."
fi

# Klart - tala om hur appen startas. venv/bin/predikan skapades av pip i
# steg 3 (se [project.scripts] i pyproject.toml).
echo ""
echo "Klart! Starta appen med:"
echo "    ./venv/bin/predikan"
echo "Öppna sedan http://127.0.0.1:8000 och fyll i resten under fliken '⚙️ Inställningar'."
