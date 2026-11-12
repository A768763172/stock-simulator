@echo off
chcp 65001 >nul 2>&1
setlocal
title Intraday Stack (headless autostart)
set "ROOT=C:\Users\Administrator\WorkBuddy\2026-09-21-17-21-45\stock-simulator"
set "PY=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe"
set "VENV_PY=C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe"
set "DASH_PORT=8501"
set "LOG=%ROOT%\data\autostart.log"

REM ===== 全流程日志（供开机/未登录 S4U 会话排障，cmd 无窗口时唯一可见线索）=====
echo ==== autostart %DATE% %TIME% ==== >> "%LOG%" 2>&1
echo [1] cd /d "%ROOT%" >> "%LOG%" 2>&1
cd /d "%ROOT%" || (echo [ERR] cannot cd to %ROOT% >> "%LOG%" 2>&1 & exit /b 1)
echo [2] check python files >> "%LOG%" 2>&1
if not exist "%PY%" (echo [ERR] python not found: %PY% >> "%LOG%" 2>&1 & exit /b 1)
if not exist "%VENV_PY%" (echo [ERR] venv python not found: %VENV_PY% >> "%LOG%" 2>&1 & exit /b 1)
if not exist "%ROOT%\scheduler.py" (echo [ERR] scheduler.py not found >> "%LOG%" 2>&1 & exit /b 1)
if not exist "%ROOT%\dashboard.py" (echo [ERR] dashboard.py not found >> "%LOG%" 2>&1 & exit /b 1)
echo [3] files OK >> "%LOG%" 2>&1

REM 1) Dashboard (streamlit): 单实例保护，端口占用则跳过，避免双 streamlit 同绑 8501。
REM    S4U / Session0 下 Get-NetTCPConnection 偶发不可用：失败一律按「未占用」处理，
REM    由 streamlit 自身端口冲突退出来兜底，绝不让 PowerShell 报错阻断启动。
set DASH_SKIP=0
powershell -NoProfile -Command "$p=Get-NetTCPConnection -LocalPort %DASH_PORT% -ErrorAction SilentlyContinue; if($p){exit 2}else{exit 0}" >> "%LOG%" 2>&1
if errorlevel 2 set DASH_SKIP=1
echo [4] DASH_SKIP=%DASH_SKIP% (powershell errorlevel=%ERRORLEVEL%) >> "%LOG%" 2>&1
if "%DASH_SKIP%"=="0" (
    echo [5] start dashboard >> "%LOG%" 2>&1
    start "Intraday Dashboard" "%VENV_PY%" -m streamlit run dashboard.py --server.port %DASH_PORT% --server.headless true
) else (
    echo [5] skip dashboard (port occupied) >> "%LOG%" 2>&1
)

REM 2) Scheduler: 自带 8502 单实例锁，已在运行则立即退出（不卡）。
REM    主循环异常自恢复；非交易日自动休眠；盘前 09:00 检索；live_run 子进程 1h 超时保护。
echo [6] start scheduler >> "%LOG%" 2>&1
start "" "%PY%" scheduler.py
echo [7] done, exiting >> "%LOG%" 2>&1
exit /b 0
