@echo off
chcp 65001 >nul
title Procurar a camera - Blin Car
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Rode o instalar.bat primeiro.
  pause
  exit /b 1
)
".venv\Scripts\python.exe" leitor.py --procurar %1
if errorlevel 4 goto fim
if errorlevel 3 goto senha
goto fim
:senha
echo.
set /p "SENHA=Digite a senha da camera e aperte Enter: "
".venv\Scripts\python.exe" leitor.py --procurar "%SENHA%"
if errorlevel 4 goto fim
if errorlevel 3 goto senha
:fim
if exist "foto-camera.jpg" start "" "foto-camera.jpg"
pause
