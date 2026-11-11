@echo off
chcp 65001 >nul 2>&1
setlocal
title Intraday Stack (headless autostart)
set "ROOT=C:\Users\Administrator\WorkBuddy\2026-09-21-17-21-45\stock-simulator"
set "PY=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe"
set "VENV_PY=C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
set "DASH_PORT=8501"
cd /d "%ROOT%" || (echo [ERR] cannot cd to %ROOT% & exit /b 1)
if not exist "%PY%" (echo [ERR] python not found: %PY% & exit /b 1)
if not exist "%VENV_PY%" (echo [ERR] venv python not found: %VENV_PY% & exit /b 1)
if not exist "%ROOT%\scheduler.py" (echo [ERR] scheduler.py not found & exit /b 1)
if not exist "%ROOT%\dashboard.py" (echo [ERR] dashboard.py not found & exit /b 1)

REM 1) Dashboard (streamlit): 单实例保护，端口占用则跳过，避免双 streamlit 同绑 8501。
set DASH_SKIP=0
powershell -NoProfile -Command "$p=Get-NetTCPConnection -LocalPort %DASH_PORT% -ErrorAction SilentlyContinue; if($p){Write-Host '[WARN] 端口 %DASH_PORT% 已被占用，dashboard 可能已在运行，跳过。'; exit 2}"
if errorlevel 2 set DASH_SKIP=1
if "%DASH_SKIP%"=="0" (
    start "Intraday Dashboard" "%VENV_PY%" -m streamlit run dashboard.py --server.port %DASH_PORT% --server.headless true
) else (
    echo [INFO] 未重复启动 dashboard（端口 %DASH_PORT% 已被占用）。
)

REM 2) Scheduler: 自带 8502 单实例锁，已在运行则立即退出（不卡）。
REM    主循环异常自恢复；非交易日自动休眠；live_run 子进程 1h 超时保护。
REM    用 start "" 脱离当前控制台，bat 立即返回，定时任务不会卡在 pause。
start "" "%PY%" scheduler.py
exit /b 0
