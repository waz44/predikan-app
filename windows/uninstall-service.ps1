# Tar bort Windows-tjänsten för Predikan -> Podcast (och brandväggsregeln,
# om en sådan skapades). Enklast: dubbelklicka på Avinstallera.cmd.
#
# Appens mapp lämnas orörd - .env, databasen, ljudfilerna och arkivet finns
# kvar. Ta bort mappen själv om du inte vill spara något.
param([switch]$PauseAtEnd)

$ErrorActionPreference = "Stop"
$ServiceName = "PredikanApp"

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  Start-Process powershell.exe -Verb RunAs -ArgumentList @(
    "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"", "-PauseAtEnd"
  )
  exit
}

try {
  $service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
  if ($service) {
    if ($service.Status -ne "Stopped") {
      Write-Host "==> Stoppar tjänsten..." -ForegroundColor Cyan
      Stop-Service -Name $ServiceName -Force
      $service.WaitForStatus("Stopped", [TimeSpan]::FromSeconds(60))
    }
    Write-Host "==> Tar bort tjänsten..." -ForegroundColor Cyan
    sc.exe delete $ServiceName | Out-Null
  } else {
    Write-Host "Tjänsten $ServiceName finns inte." -ForegroundColor Yellow
  }
  Get-NetFirewallRule -DisplayName "Predikan-appen" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
  Write-Host ""
  Write-Host "Klart. Appens mapp med dina inställningar och filer finns kvar: $(Split-Path -Parent $PSScriptRoot)" -ForegroundColor Green
} catch {
  Write-Host "Avinstallationen misslyckades: $($_.Exception.Message)" -ForegroundColor Red
} finally {
  if ($PauseAtEnd) { Read-Host "Tryck Enter för att stänga" | Out-Null }
}
