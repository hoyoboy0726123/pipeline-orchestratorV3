@echo off
REM Pipeline Orchestrator V3 — 沙盒一鍵安裝（Windows 端入口）
REM
REM 做的事：
REM   1. 確認 WSL 安裝（沒有就提示使用者執行 `wsl --install` 並重啟）
REM   2. 把當前專案路徑轉成 WSL 內的 /mnt/c/... 格式
REM   3. 呼叫 sandbox/setup.sh 進到 WSL 內跑真正的安裝
REM
REM 用法：
REM   setup_sandbox.bat              - 一般安裝（首次 clone 用）
REM   setup_sandbox.bat --rebuild    - 強制 rebuild image
REM                                    （改了 Dockerfile / requirements.txt 之後用）
SETLOCAL
SET "EXTRA_ARGS=%*"

echo ==================================================
echo  Pipeline Orchestrator V3 - Sandbox Setup
echo ==================================================
echo.

REM ── 1. 檢查 WSL 是否安裝
wsl --status >NUL 2>&1
IF %ERRORLEVEL% NEQ 0 (
    echo [X] WSL not detected.
    echo.
    echo Please run the following in an Administrator PowerShell:
    echo.
    echo     wsl --install
    echo.
    echo Then reboot, and run this script again.
    echo.
    pause
    exit /b 1
)

REM ── 2. 檢查 WSL 內有可用的 default distro（Ubuntu 等）
wsl -e bash -c "echo OK" >NUL 2>&1
IF %ERRORLEVEL% NEQ 0 (
    echo [X] No usable WSL distro.
    echo Please install Ubuntu via: wsl --install -d Ubuntu
    pause
    exit /b 1
)

REM ── 3. 轉換專案目錄到 WSL 路徑格式
SET "WIN_PROJECT=%~dp0.."
FOR /F "usebackq tokens=*" %%F IN (`wsl wslpath -a "%WIN_PROJECT%"`) DO SET "WSL_PROJECT=%%F"

echo Windows project : %WIN_PROJECT%
echo WSL project     : %WSL_PROJECT%
echo.

REM ── 4. 執行 WSL 內的安裝腳本
echo === Running setup inside WSL ===
echo (If this is the first time, you may be prompted for your WSL password for sudo)
echo.
wsl bash "%WSL_PROJECT%/sandbox/setup.sh" "%WSL_PROJECT%" %EXTRA_ARGS%

IF %ERRORLEVEL% NEQ 0 (
    echo.
    echo !! Setup FAILED. See messages above for details.
    pause
    exit /b %ERRORLEVEL%
)

echo.
echo ==================================================
echo  All done. Sandbox is ready.
echo ==================================================
echo.
echo Next: start V3 normally (launch_full_project.bat or
echo uvicorn + npm dev). The backend will auto-detect
echo the sandbox and route skill code through it when the
echo "Sandbox execution" toggle is on in Settings.
echo.
pause
