@echo off
chcp 65001 >nul 2>&1
title Intraday Stack Launcher (scheduler + dashboard)
setlocal
set "ROOT=C:\Users\Administrator\WorkBuddy\2026-09-21-17-21-45\stock-simulator"
set "PY=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe"
set "VENV_PY=C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
set "DASH_PORT=8501"

cd /d "%ROOT%" || (echo [ERR] cannot cd to %ROOT% & pause & exit /b 1)
if not exist "%PY%" (echo [ERR] python not found: %PY% & pause & exit /b 1)
if not exist "%VENV_PY%" (echo [ERR] venv python not found: %VENV_PY% & pause & exit /b 1)
if not exist "%ROOT%\scheduler.py" (echo [ERR] scheduler.py not found & pause & exit /b 1)
if not exist "%ROOT%\dashboard.py" (echo [ERR] dashboard.py not found & pause & exit /b 1)

echo ============================================================
echo  Starting FULL intraday stack: scheduler + dashboard
echo  Project       : %ROOT%
echo  Scheduler log : %ROOT%\data\scheduler.log
echo  Dashboard URL : http://localhost:%DASH_PORT%
echo  - Dashboard launches in its OWN window (title: Intraday Dashboard)
echo  - Close THIS window to stop the scheduler.
echo  - Close the Dashboard window to stop the dashboard.
echo  NOTE: if port %DASH_PORT% is busy, stop the old dashboard first.
echo ============================================================

REM launch dashboard (streamlit) in a separate window
REM 单实例保护：若 8501 已被占用，说明 dashboard 已在运行，跳过重复启动，
REM 避免同端口起两个 streamlit 互相干扰（曾出现两个 streamlit 同绑 8501）
powershell -NoProfile -Command "$p=Get-NetTCPConnection -LocalPort %DASH_PORT% -ErrorAction SilentlyContinue; if($p){Write-Host '[WARN] 端口 %DASH_PORT% 已被占用，dashboard 可能已在运行，跳过重复启动。'; exit 2}"
set DASH_SKIP=0
if errorlevel 2 set DASH_SKIP=1
if "%DASH_SKIP%"=="0" (
    start "Intraday Dashboard" "%VENV_PY%" -m streamlit run dashboard.py --server.port %DASH_PORT% --server.headless true
) else (
    echo [INFO] 未重复启动 dashboard（端口 %DASH_PORT% 已被占用）。
)

REM run scheduler in this window (foreground)
REM 单实例保护：若 8502 已被占用，说明调度器已在运行（如登录任务已拉起），
REM 跳过前台启动，避免重复进程与末尾 pause 阻塞。
set SCHED_SKIP=0
powershell -NoProfile -Command "$p=Get-NetTCPConnection -LocalPort 8502 -ErrorAction SilentlyContinue; if($p){Write-Host '[WARN] 端口 8502 已被占用，调度器可能已在运行，跳过启动。'; exit 2}"
if errorlevel 2 set SCHED_SKIP=1
if "%SCHED_SKIP%"=="0" (
    "%PY%" scheduler.py
    echo.
    echo [scheduler exited] see data\scheduler.log for details.
    pause
) else (
    echo [INFO] 调度器已在运行（登录任务或此前已启动），本窗口无关紧要，可直接关闭。
)
