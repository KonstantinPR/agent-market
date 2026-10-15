@echo off
rem One-click restart of the agent_market dev server.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0restart-app.ps1"
