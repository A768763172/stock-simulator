@echo off
chcp 65001 >nul 2>&1
title Intraday Scheduler Daemon (stock-simulator)
setlocal
set "ROOT=C:\Users\Administrator\WorkBuddy\2026-09-21-17-21-45\stock-simulator"
set "PY=C:\Users\Administrator\.workbuddy\binaries\python\versions\3.13.12\python.exe"

cd /d "%ROOT%" || (echo [ERR] cannot cd to %ROOT% & pause & exit /b 1)
if not exist "%PY%" (echo [ERR] python not found: %PY% & pause & exit /b 1)
if not exist "%ROOT%\scheduler.py" (echo [ERR] scheduler.py not found & pause & exit /b 1)

echo ============================================================
echo  Intraday Real-time Decision Scheduler  (stock-simulator)
echo  Project : %ROOT%
echo  Log     : %ROOT%\data\scheduler.log
echo  Close this window to STOP the scheduler.
echo ============================================================
"%PY%" scheduler.py
echo.
echo [scheduler exited] see data\scheduler.log for details.
pause
