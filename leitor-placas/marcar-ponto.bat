@echo off
chcp 65001 >nul
title Marcar onde mostram a placa - Blin Car
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Rode o instalar.bat primeiro.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" leitor.py --ponto
if errorlevel 1 goto fim
if exist "ponto-marcado.jpg" start "" "ponto-marcado.jpg"
:fim
pause
