@echo off
rem Tar bort Windows-tjansten "PredikanApp". Appens mapp och data finns kvar.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0windows\uninstall-service.ps1"
