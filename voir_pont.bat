@echo off
chcp 65001 >nul
title Journal du pont (Ctrl+C pour fermer)
powershell -NoProfile -Command "Get-Content -Path '%~dp0bridge.log' -Encoding UTF8 -Wait -Tail 25"
