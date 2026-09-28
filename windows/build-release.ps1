# Bygger releasepaketet för Windows: en zip med appen, redo att packas upp
# och installeras som tjänst med Installera.cmd.
#
# Kör från projektroten:   .\windows\build-release.ps1
# En bestämd version:      .\windows\build-release.ps1 -Ref v1.3.0
#
# Innehållet tas från git (git archive), så bara incheckade filer kommer
# med - aldrig .env, databasen, ljudfiler, venv eller annat lokalt. Tester,
# CI och Docker-filerna utelämnas; de behövs inte för att köra appen.
#
# Resultat: dist\predikan-app-<version>-windows.zip, med allt i mappen
# PredikanApp\ (packas upp till t.ex. C:\PredikanApp).
param([string]$Ref = "HEAD")

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

# Versionen läses ur pyproject.toml i den version som paketeras.
$pyproject = git show "${Ref}:pyproject.toml"
if ($LASTEXITCODE -ne 0) { throw "Hittar inte $Ref i git." }
$version = ($pyproject | Select-String '^version\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value

if ($Ref -eq "HEAD" -and (git status --porcelain)) {
  Write-Host "OBS: det finns ändringar som inte är incheckade - de kommer INTE med i paketet." -ForegroundColor Yellow
}

New-Item -ItemType Directory -Force (Join-Path $root "dist") | Out-Null
$zip = Join-Path $root "dist\predikan-app-$version-windows.zip"
git archive --format=zip --prefix=PredikanApp/ -o $zip $Ref -- . `
  ":(exclude)tests" ":(exclude).github" ":(exclude)Dockerfile" ":(exclude)docker-compose.yml" `
  ":(exclude).dockerignore" ":(exclude)setup.sh" ":(exclude)requirements-dev.txt"
if ($LASTEXITCODE -ne 0) { throw "git archive misslyckades." }

$size = (Get-Item $zip).Length / 1KB
Write-Host ("Klart: {0} ({1:N0} kB, version {2})" -f $zip, $size, $version) -ForegroundColor Green
