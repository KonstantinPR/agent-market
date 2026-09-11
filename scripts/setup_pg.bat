@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_pg.ps1"
echo.
echo Press any key to close this window...
pause >nul