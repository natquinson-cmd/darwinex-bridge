@echo off
chcp 65001 >nul
title Mise a jour du pont IG -^> Darwinex
cd /d "%~dp0"
set "REPO=https://raw.githubusercontent.com/natquinson-cmd/darwinex-bridge/main"
echo ============================================================
echo   MISE A JOUR DU PONT  (depuis GitHub)
echo ============================================================
echo.
echo [1/3] Telechargement des derniers fichiers...
powershell -NoProfile -Command "$repo='%REPO%'; foreach ($f in 'bridge_ig_mt5.py','bridge_ig_metaapi.py','voir_pont.bat','setup_vps.ps1','setup_vps.bat','start_pont.bat'){ try { Invoke-WebRequest -Uri ($repo + '/' + $f) -OutFile $f -UseBasicParsing; Write-Host ('  OK   ' + $f) } catch { Write-Host ('  ECHEC ' + $f + ' : ' + $_.Exception.Message) } }"
echo.
echo [2/3] Redemarrage du pont...
schtasks /End /TN PontDarwinex >nul 2>&1
timeout /t 5 /nobreak >nul
schtasks /Run /TN PontDarwinex
echo.
echo [3/3] Termine. Ouvrez voir_pont.bat pour verifier.
echo       (config.json n'a PAS ete touche - vos identifiants sont intacts)
echo.
pause
