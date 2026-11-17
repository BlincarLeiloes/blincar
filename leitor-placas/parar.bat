@echo off
chcp 65001 >nul
title Parar o leitor - Blin Car
powershell -NoProfile -Command "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*leitor.py*' -and $_.ProcessId -ne $PID } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
echo Leitor desligado. Ele liga de novo quando o Windows reiniciar (ou pelo iniciar.bat).
pause
