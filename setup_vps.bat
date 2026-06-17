@echo off
chcp 65001 >nul
title Installation du pont sur le VPS
cd /d "%~dp0"
echo ============================================================
echo   INSTALLATION DU PONT IG -^> DARWINEX ZERO  (sur le VPS)
echo ============================================================
echo.

echo [1/4] Verification de Python...
where python >nul 2>&1
if errorlevel 1 (
  echo   ERREUR : Python introuvable.
  echo   Installez Python 3.12 depuis https://www.python.org/downloads/windows/
  echo   en COCHANT "Add python.exe to PATH", puis relancez ce script.
  pause
  exit /b 1
)
python --version

echo.
echo [2/4] Installation de la dependance MetaTrader5...
python -m pip install --upgrade pip >nul 2>&1
python -m pip install MetaTrader5
if errorlevel 1 ( echo   ERREUR pip. & pause & exit /b 1 )

echo.
echo [3/4] Verification de la configuration...
if not exist "config.json" (
  echo   ERREUR : config.json manquant. Copiez tout le dossier Darwinex_Bridge
  echo   depuis votre PC (il contient vos identifiants) avant de lancer ce script.
  pause
  exit /b 1
)
echo   config.json present.

echo.
echo [4/4] Creation de la tache planifiee (demarrage auto, instance unique)...
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_vps.ps1"

echo.
echo ============================================================
echo   PRE-REQUIS AVANT DE DEMARRER :
echo   1. MetaTrader 5 (Darwinex) installe et connecte au compte
echo      4000093713, case "Conserver le mot de passe" cochee.
echo   2. Bouton "Algo Trading" du terminal active (vert).
echo.
echo   Puis lancez :  Start-ScheduledTask -TaskName PontDarwinex
echo   (ou deconnectez/reconnectez votre session RDP)
echo ============================================================
pause
