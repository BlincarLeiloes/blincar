@echo off
chcp 65001 >nul
title Testar uma foto ou video - Blin Car
cd /d "%~dp0"
if "%~1"=="" (
  echo Arraste uma foto ou um video de um carro em cima deste arquivo.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" leitor.py --testar "%~1"
if exist "teste-resultado.jpg" start "" "teste-resultado.jpg"
pause
