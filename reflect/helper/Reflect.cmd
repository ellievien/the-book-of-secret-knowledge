@echo off
rem Double-click to start Reflect on Windows (runs run.ps1 without changing your PowerShell policy).
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
if errorlevel 1 pause
