@echo off
setlocal EnableExtensions
set "PORT=8000"
set "URL=http://127.0.0.1:%PORT%/"
set "APP_DIR=C:\python_projects\agent_market"
set "PY=C:\python_projects\agent_market\venv\Scripts\python.exe"

netstat -ano | findstr ":%PORT%" | findstr /i "LISTENING" >nul 2>&1
if not errorlevel 1 goto open

echo agent_market server is not running. Starting on port %PORT% ...
pushd "%APP_DIR%"
start "agent_market" /min "%PY%" -m uvicorn app.main:app --host 127.0.0.1 --port %PORT%
popd

set /a tries=0
:wait
netstat -ano | findstr ":%PORT%" | findstr /i "LISTENING" >nul 2>&1
if not errorlevel 1 goto open
set /a tries+=1
if %tries% geq 60 goto open
timeout /t 1 /nobreak >nul 2>&1
goto wait

:open
start "" "%URL%"
endlocal