@echo off
chcp 65001 >nul
title Escolher o modo do leitor - Blin Car
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Rode o instalar.bat primeiro.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" leitor.py --modo
pause
