@echo off
rem Installerar (eller uppdaterar) appen som Windows-tjansten "PredikanApp".
rem Val skickas vidare till windows\install-service.ps1, t.ex.:
rem   Installera.cmd -Port 8080 -ListenOnNetwork
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0windows\install-service.ps1" %*
