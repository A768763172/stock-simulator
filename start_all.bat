@echo off
chcp 936 >nul 2>&1
title 模拟交易 一键入口
setlocal
set "ROOT=C:\Users\Administrator\WorkBuddy\2026-09-21-17-21-45\stock-simulator"
set "PY=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe"
set "VENV_PY=C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
set "DASH_PORT=8501"
set "DASH_URL=http://localhost:%DASH_PORT%"

cd /d "%ROOT%" || (echo [错误] 无法进入 %ROOT% & pause & exit /b 1)
if not exist "%PY%" (echo [错误] 未找到 python & pause & exit /b 1)
if not exist "%VENV_PY%" (echo [错误] 未找到 venv python & pause & exit /b 1)

REM 1) 调度器单实例 端口 8502
set SCHED_SKIP=0
powershell -NoProfile -Command "if(Get-NetTCPConnection -LocalPort 8502 -ErrorAction SilentlyContinue){exit 2}" >nul 2>&1
if errorlevel 2 set SCHED_SKIP=1
if "%SCHED_SKIP%"=="0" (
    echo [启动] 调度器 后台
    start "" "%PY%" "%ROOT%\scheduler.py"
) else (
    echo [跳过] 调度器已在运行
)

REM 2) 看板单实例 端口 8501
set DASH_SKIP=0
powershell -NoProfile -Command "if(Get-NetTCPConnection -LocalPort %DASH_PORT% -ErrorAction SilentlyContinue){exit 2}" >nul 2>&1
if errorlevel 2 set DASH_SKIP=1
if "%DASH_SKIP%"=="0" (
    echo [启动] 看板 后台
    start "Intraday Dashboard" "%VENV_PY%" -m streamlit run "%ROOT%\dashboard.py" --server.port %DASH_PORT% --server.headless true
) else (
    echo [跳过] 看板已在运行
)

REM 3) 无论如何都打开看板浏览器，让用户看到界面
echo [完成] 正在打开看板 %DASH_URL%
ping -n 4 127.0.0.1 >nul
start "" "%DASH_URL%"

echo 系统已就绪，可关闭此窗口。
ping -n 3 127.0.0.1 >nul
exit /b 0
