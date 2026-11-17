@echo off
chcp 65001 >nul
title Verificar o leitor - Blin Car
cd /d "%~dp0"
".venv\Scripts\python.exe" leitor.py --verificar
pause
