@echo off
setlocal
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
title Pipeline Orchestrator V3 Launcher

echo Starting Pipeline Orchestrator V3 in separate windows...
echo (V3 uses different ports to avoid clashing with V1:8000 / V2:8001)

echo [1/2] Starting Backend V3 (Port 8002)...
start "PO_Backend_V3" cmd /c "cd /d "%~dp0backend" && set PYTHONIOENCODING=utf-8 && set PYTHONUTF8=1 && .venv\Scripts\uvicorn.exe main:app --host 0.0.0.0 --port 8002"

echo [2/2] Starting Frontend V3 (Port 3003)...
start "PO_Frontend_V3" cmd /c "cd /d "%~dp0frontend" && npx next dev --port 3003"

echo.
echo V3 startup commands issued.
echo   Frontend : http://localhost:3003
echo   Backend  : http://localhost:8002
echo.
echo If you haven't installed the skill sandbox yet, double-click:
echo   %~dp0sandbox\setup_sandbox.bat
echo.
echo Then toggle "Skill Sandbox" in Settings.
echo.
pause
