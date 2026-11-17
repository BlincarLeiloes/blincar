@echo off
chcp 65001 >nul
title Leitor de placas - Blin Car
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Rode o instalar.bat primeiro.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" leitor.py
pause
