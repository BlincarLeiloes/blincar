@echo off
chcp 65001 >nul
title Ler as placas no CMS - Blin Car
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Rode o instalar.bat primeiro.
  pause
  exit /b 1
)
echo Use isto se o procurar-camera.bat nao conseguir o video direto da camera.
echo Antes: abra o CMS of EseeCloud com o video da camera aparecendo (de preferencia maximizado).
echo.
".venv\Scripts\python.exe" leitor.py --tela %*
if errorlevel 1 goto fim
if exist "foto-camera.jpg" start "" "foto-camera.jpg"
:fim
pause
