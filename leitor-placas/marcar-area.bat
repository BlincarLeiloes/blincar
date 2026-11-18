@echo off
chcp 65001 >nul
title Marcar a area dos carros - Blin Car
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Rode o instalar.bat primeiro.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" leitor.py --area
if errorlevel 1 goto fim
if exist "area-marcada.jpg" start "" "area-marcada.jpg"
:fim
pause
