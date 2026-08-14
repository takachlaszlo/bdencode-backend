@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0windows.ps1"
if errorlevel 1 (
  echo.
  echo A telepites hibat jelzett. A reszletek fent olvashatok.
  pause
)
