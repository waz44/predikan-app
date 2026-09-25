# Enkel uppsättning för Windows (PowerShell).
#
# Kör från projektroten:   .\setup.ps1
# Hoppa över lokal Whisper: .\setup.ps1 -NoWhisper
#
# Skapar en venv, installerar appen (som paket, så kommandot `predikan`
# blir tillgängligt), kopierar .env-example -> .env om den saknas och
# skriver ut nästa steg. Rör aldrig en befintlig .env.
#
# Om execution policy blockerar skriptet, kör en gång i detta fönster:
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#
# OBS: lokal Whisper installeras via faster-whisper (1.2.1), som har färdiga
# paket även för Python 3.14. Skulle installationen ändå fallera på din
# Python-version, kör med -NoWhisper och använd OpenAI Whisper API i stället
# (sätts i inställningsguiden) - eller installera en äldre Python (3.11-3.13).
#
# Skriptet går att köra flera gånger: en befintlig venv och .env återanvänds,
# och pip installerar bara om det som ändrats. Kör det igen efter "git pull"
# för att få nya paket som en ny version av appen behöver.

# -NoWhisper är en växel (switch): finns den med på kommandoraden blir
# $NoWhisper sann, annars falsk.
param([switch]$NoWhisper)

# Avbryt direkt vid första fel, i stället för att fortsätta med nästa steg
# i ett halvt installerat läge.
$ErrorActionPreference = "Stop"
# Kör alltid från mappen där skriptet ligger, oavsett var det startades
# ifrån - alla sökvägar nedan är relativa till projektroten.
Set-Location -Path $PSScriptRoot

# Steg 1: en virtuell miljö (venv) i projektmappen. Den håller appens paket
# åtskilda från resten av datorns Python-installation. Finns den redan
# återanvänds den.
Write-Host "==> Skapar virtuell miljö (venv/)..." -ForegroundColor Cyan
if (-not (Test-Path venv)) { python -m venv venv }
# All installation nedan görs med venv:ens egen Python, så att paketen
# hamnar i venv:en - utan att venv:en behöver "aktiveras" först.
$py = ".\venv\Scripts\python.exe"

# Steg 2: en aktuell pip klarar nyare paketformat och hittar färdigbyggda
# paket i större utsträckning (slipper kompilera).
Write-Host "==> Uppdaterar pip..." -ForegroundColor Cyan
& $py -m pip install --upgrade pip

# Steg 3: installera appen. "-e" (redigerbart läge) betyder att appen körs
# direkt från projektmappen - ändringar och "git pull" gäller utan ny
# installation, och uploads/, processed/ m.m. hamnar i projektmappen.
if ($NoWhisper) {
  Write-Host "==> Installerar appen (utan lokal Whisper)..." -ForegroundColor Cyan
  & $py -m pip install -e .
} else {
  # ".[faster-whisper]" tar med extrapaketen för lokal transkribering (se
  # [project.optional-dependencies] i pyproject.toml) - även KB-Whisper.
  Write-Host "==> Installerar appen med lokal Whisper (faster-whisper)..." -ForegroundColor Cyan
  & $py -m pip install -e ".[faster-whisper]"
}

# Steg 4: skapa .env från mallen första gången. En befintlig .env skrivs
# aldrig över - den innehåller dina nycklar och inställningar.
if (-not (Test-Path .env)) {
  Write-Host "==> Skapar .env från .env-example..." -ForegroundColor Cyan
  Copy-Item .env-example .env
} else {
  Write-Host "==> .env finns redan - lämnar den orörd." -ForegroundColor Yellow
}

# Klart - tala om hur appen startas. predikan.exe skapades av pip i steg 3
# (se [project.scripts] i pyproject.toml).
Write-Host ""
Write-Host "Klart! Starta appen med:" -ForegroundColor Green
Write-Host "    .\venv\Scripts\predikan.exe"
Write-Host "Öppna sedan http://127.0.0.1:8000 och fyll i resten under fliken '⚙️ Inställningar'."
