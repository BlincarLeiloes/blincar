@echo off
chcp 65001 >nul
title Instalar o leitor de placas - Blin Car
cd /d "%~dp0"
echo ==================================================
echo    Leitor de placas da Blin Car - instalacao
echo ==================================================
echo.

rem Python 3.10 a 3.13 (a leitura de placas ainda nao funciona em versao mais nova).
set "PY="
for %%v in (3.12 3.13 3.11 3.10) do (
  if not defined PY ( py -%%v -c "1" >nul 2>nul && set "PY=py -%%v" )
)
if not defined PY (
  python -c "import sys; sys.exit(0 if (3,10) <= sys.version_info[:2] <= (3,13) else 1)" >nul 2>nul && set "PY=python"
)
if not defined PY goto sempython

echo [1/5] Preparando o Python desta pasta...
if not exist ".venv\Scripts\python.exe" %PY% -m venv .venv || goto erro

echo [2/5] Instalando a leitura de placas (pode levar alguns minutos)...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q --upgrade pip
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt || goto erro

echo [3/5] Baixando os arquivos da leitura de placas...
".venv\Scripts\python.exe" leitor.py --preparar || goto erro

echo [4/5] Ligando o leitor junto com o Windows...
set "INICIO=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Leitor de placas Blin Car.vbs"
> "%INICIO%" echo CreateObject("WScript.Shell").Run """%~dp0iniciar-escondido.vbs""", 0, False

echo.
choice /c SN /m "[5/5] Deixar o computador sem dormir enquanto estiver na tomada (recomendado)"
if errorlevel 2 goto depoisenergia
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0 >nul 2>nul
powercfg /setactive SCHEME_CURRENT >nul 2>nul
echo Pronto: na tomada ele nao dorme, e fechar a tampa nao desliga.
:depoisenergia

echo.
echo Procurando a camera na rede...
call "%~dp0procurar-camera.bat"
echo.
if not exist "config-blincar.json" if not exist "%USERPROFILE%\Downloads\config-blincar*.json" (
  echo Falta baixar a configuracao no painel da Blin Car:
  echo aba Camera, "Ligar a camera". Ela vai para a pasta Downloads
  echo e o leitor pega de la sozinho.
  echo.
)
echo Tudo instalado. Agora abra o marcar-ponto.bat e marque o lugar onde voces vao
echo mostrar a placa na mao para a camera.
echo Depois, para ligar o leitor, abra o iniciar.bat.
echo Nas proximas vezes ele liga sozinho quando o Windows ligar.
pause
exit /b 0

:sempython
echo O Python (programa que o leitor usa) nao esta instalado. Vou instalar agora...
winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
if errorlevel 1 (
  echo.
  echo Nao consegui instalar sozinho. Vou abrir o site do Python:
  echo baixe o "Python 3.12" para Windows e, na instalacao, marque "Add python.exe to PATH".
  start "" "https://www.python.org/downloads/windows/"
)
echo.
echo Quando o Python terminar de instalar, feche esta janela e abra o instalar.bat de novo.
pause
exit /b 1

:erro
echo.
echo Deu erro na instalacao. Tire um print desta janela e mande para o Arthur.
pause
exit /b 1
