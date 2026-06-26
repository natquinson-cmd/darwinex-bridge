@echo off
chcp 65001 >nul
title Pont IG - Darwinex Zero
cd /d "%~dp0"
echo ============================================
echo   PONT IG -^> DARWINEX ZERO  (Ctrl+C = stop)
echo ============================================
:loop
python bridge_ig_mt5.py
if errorlevel 3 (
  echo.
  echo Un autre pont tourne deja - ce lanceur s'arrete pour eviter les doublons.
  timeout /t 8 /nobreak >nul
  goto :eof
)
echo.
echo Le pont s'est arrete - redemarrage automatique dans 30 s (Ctrl+C pour annuler)...
timeout /t 30 /nobreak >nul
goto loop
