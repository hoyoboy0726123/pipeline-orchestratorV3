@echo off
setlocal
title Pipeline Orchestrator V3 Launcher
REM 不在這裡設 PYTHONUTF8 / PYTHONIOENCODING — main.py 自己會用 sys.stdout.reconfigure
REM 把 stdout/stderr 強制改 utf-8。env var 鏈式設定容易沾到尾隨空白讓 Python
REM 的 preinitializing 階段直接炸掉（Fatal Python error: invalid PYTHONUTF8）

echo Starting Pipeline Orchestrator V3 in separate windows...
echo (V3 uses different ports to avoid clashing with V1:8000 / V2:8001)

echo [1/2] Starting Backend V3 (Port 8002)...
REM /k instead of /c keeps the window open if uvicorn crashes so you can read the error
REM 顯式清掉可能從父 process 繼承的 PYTHONUTF8（曾經設過沾到尾空白值讓 Python preinit 炸）
start "PO_Backend_V3" cmd /k "cd /d "%~dp0backend" && set "PYTHONUTF8=" && .venv\Scripts\uvicorn.exe main:app --host 0.0.0.0 --port 8002"

echo [2/2] Starting Frontend V3 (Port 3003)...
start "PO_Frontend_V3" cmd /k "cd /d "%~dp0frontend" && npx next dev --port 3003"

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
