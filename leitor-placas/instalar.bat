@echo off
chcp 65001 >nul
title Instalar o leitor de placas - Blin Car
cd /d "%~dp0"
echo ==================================================
echo    Leitor de placas da Blin Car - instalacao
echo ==================================================
echo.

if /i "%PROCESSOR_ARCHITECTURE%"=="x86" if not defined PROCESSOR_ARCHITEW6432 (
  echo Este Windows e de 32 bits. O leitor de placas precisa de Windows 64 bits.
  pause
  exit /b 1
)

rem Python de 64 bits, do 3.10 ao 3.14 (a leitura de placas nao existe para Python de 32 bits):
rem pelo "py", pelo "python" ou na pasta onde o instalador do site coloca.
set "CONF=import sys; sys.exit(0 if sys.maxsize > 2**32 and (3,10) <= sys.version_info[:2] <= (3,14) else 1)"
set "PY="
call :acharpython
if not defined PY goto sempython

:tempython

echo [1/5] Preparando o Python desta pasta...
rem Pasta .venv feita com um Python errado (32 bits ou versao que nao serve): apaga e faz de novo.
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" -c "%CONF%" >nul 2>nul || rmdir /s /q ".venv"
)
if not exist ".venv\Scripts\python.exe" %PY% -m venv .venv || goto erro

echo [2/5] Instalando a leitura de placas (pode levar alguns minutos)...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q --upgrade pip
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt || goto erro
rem A leitura de placas (onnxruntime) precisa do Microsoft Visual C++. Computador que nunca teve: instala.
".venv\Scripts\python.exe" -c "import onnxruntime" >nul 2>nul || call :vcredist
".venv\Scripts\python.exe" -c "import onnxruntime" >nul 2>nul || goto semvc

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

:acharpython
rem O novo py (Python install manager) pode perguntar se instala a versao: o echo n responde que nao.
for %%v in (3.12 3.13 3.11 3.10 3.14) do (
  if not defined PY ( echo n| py -%%v -c "%CONF%" >nul 2>nul && set "PY=py -%%v" )
)
if not defined PY (
  python -c "%CONF%" >nul 2>nul && set "PY=python"
)
for %%d in (Python312 Python313 Python311 Python310 Python314) do (
  if not defined PY if exist "%LOCALAPPDATA%\Programs\Python\%%d\python.exe" "%LOCALAPPDATA%\Programs\Python\%%d\python.exe" -c "%CONF%" >nul 2>nul && set PY="%LOCALAPPDATA%\Programs\Python\%%d\python.exe"
  if not defined PY if exist "%ProgramFiles%\%%d\python.exe" "%ProgramFiles%\%%d\python.exe" -c "%CONF%" >nul 2>nul && set PY="%ProgramFiles%\%%d\python.exe"
)
exit /b 0

:sempython
echo Nao achei o Python de 64 bits (programa que o leitor usa). O de 32 bits nao serve.
echo Vou baixar o certo do site oficial e instalar sozinho (uns 30 MB, 1 ou 2 minutos)...
set "INST=%TEMP%\python-blincar.exe"
del "%INST%" >nul 2>nul
powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol='Tls12'; Invoke-WebRequest -UseBasicParsing -Uri 'https://www.python.org/ftp/python/3.14.10/python-3.14.10-amd64.exe' -OutFile $env:TEMP\python-blincar.exe"
if exist "%INST%" (
  echo Instalando o Python...
  start "" /wait "%INST%" /quiet InstallAllUsers=0 InstallLauncherAllUsers=0 PrependPath=1 Include_launcher=1 Include_test=0
  del "%INST%" >nul 2>nul
)
call :acharpython
if defined PY (
  echo Python instalado.
  goto tempython
)
winget install -e --id Python.Python.3.13 --accept-package-agreements --accept-source-agreements >nul 2>nul
call :acharpython
if defined PY goto tempython
echo.
echo Nao consegui instalar sozinho. Vou abrir o site do Python:
echo baixe o "Windows installer (64-bit)" do Python 3.14 (ou 3.13), NAO o de 32-bit, e na instalacao
echo marque "Add python.exe to PATH". Depois abra o instalar.bat de novo.
start "" "https://www.python.org/downloads/windows/"
pause
exit /b 1

:vcredist
echo.
echo Falta uma peca do Windows: o Microsoft Visual C++. Vou baixar e instalar.
echo Se o Windows perguntar se pode fazer alteracoes, clique em Sim.
set "VC=%TEMP%\vc_redist.x64.exe"
del "%VC%" >nul 2>nul
powershell -NoProfile -ExecutionPolicy Bypass -Command "[Net.ServicePointManager]::SecurityProtocol='Tls12'; Invoke-WebRequest -UseBasicParsing -Uri 'https://aka.ms/vs/17/release/vc_redist.x64.exe' -OutFile $env:TEMP\vc_redist.x64.exe"
if exist "%VC%" start "" /wait "%VC%" /install /passive /norestart
del "%VC%" >nul 2>nul
exit /b 0

:semvc
echo.
echo Nao consegui instalar o Microsoft Visual C++ sozinho. Vou abrir o link da Microsoft:
echo baixe, instale e depois abra o instalar.bat de novo.
echo https://aka.ms/vs/17/release/vc_redist.x64.exe
start "" "https://aka.ms/vs/17/release/vc_redist.x64.exe"
pause
exit /b 1

:erro
echo.
echo Deu erro na instalacao. Tire um print desta janela e mande para o Arthur.
pause
exit /b 1
