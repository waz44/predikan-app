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
# OBS: lokal Whisper (torch/ctranslate2) har färdiga paket för Python
# 3.11-3.13. Kör du en nyare Python och installationen av Whisper fallerar,
# skapa venv med en 3.11-3.13-tolk eller kör med -NoWhisper och använd
# OpenAI Whisper API i stället (sätts i inställningsguiden).

param([switch]$NoWhisper)

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

Write-Host "==> Skapar virtuell miljö (venv/)..." -ForegroundColor Cyan
if (-not (Test-Path venv)) { python -m venv venv }
$py = ".\venv\Scripts\python.exe"

Write-Host "==> Uppdaterar pip..." -ForegroundColor Cyan
& $py -m pip install --upgrade pip

if ($NoWhisper) {
  Write-Host "==> Installerar appen (utan lokal Whisper)..." -ForegroundColor Cyan
  & $py -m pip install -e .
} else {
  Write-Host "==> Installerar appen med lokal Whisper (faster-whisper)..." -ForegroundColor Cyan
  & $py -m pip install -e ".[faster-whisper]"
}

if (-not (Test-Path .env)) {
  Write-Host "==> Skapar .env från .env-example..." -ForegroundColor Cyan
  Copy-Item .env-example .env
} else {
  Write-Host "==> .env finns redan - lämnar den orörd." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Klart! Starta appen med:" -ForegroundColor Green
Write-Host "    .\venv\Scripts\predikan.exe"
Write-Host "Öppna sedan http://127.0.0.1:8000 och fyll i resten under fliken '⚙️ Inställningar'."
