@echo off
setlocal
title BDEncode Windows telepito
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0windows.ps1"
set "BDENCODE_EXIT=%ERRORLEVEL%"
if not "%BDENCODE_EXIT%"=="0" (
  echo.
  echo A telepites hibat jelzett. A reszletek a masik ablakban
  echo es a %%LOCALAPPDATA%%\BDEncode\install.log fajlban olvashatok.
  pause
)
exit /b %BDENCODE_EXIT%
