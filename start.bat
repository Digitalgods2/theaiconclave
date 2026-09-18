@echo off
setlocal EnableDelayedExpansion
title The AI Conclave Switchboard
cd /d "%~dp0"

rem =============================================================================
rem  Foreground startup script for The AI Conclave Switchboard (Windows).
rem
rem  What it does:
rem    1. Resolves the Python interpreter (prefers .venv, falls back to PATH).
rem    2. Asks the app's own config loader for the configured host/port
rem       (falls back to 127.0.0.1:8787 if config can't be resolved yet).
rem    3. Kills whatever process is already bound to that port.
rem    4. Runs uvicorn in the foreground so you see startup + request logs.
rem
rem  This is a plain dev/ops console launcher. For a double-click launcher
rem  with no console window and an auto-opened browser tab, use launch.pyw
rem  (installed as a desktop shortcut by tools\install-desktop-shortcut.ps1)
rem  instead — that one checks /api/health first rather than killing the
rem  port, since it expects the service to already be running.
rem
rem  macOS equivalent: start.command
rem =============================================================================

rem --- Resolve the Python interpreter: prefer the project's .venv, fall back to PATH ---
set "PYEXE=%~dp0.venv\Scripts\python.exe"
if not exist "%PYEXE%" (
    where python >nul 2>nul
    if errorlevel 1 (
        echo ERROR: No Python interpreter found.
        echo Create .venv ^(python -m venv .venv^) or install Python 3.13+ on PATH.
        pause
        exit /b 1
    )
    set "PYEXE=python"
)

rem --- Resolve host/port from config.yaml via the app's own config loader ---
set "HOST=127.0.0.1"
set "PORT=8787"
for /f "usebackq tokens=1,2 delims= " %%A in (`"%PYEXE%" -c "from app.config import get_config; c=get_config(); print(c.server.host, c.server.port)" 2^>nul`) do (
    set "HOST=%%A"
    set "PORT=%%B"
)

echo.
echo Target: %HOST%:%PORT%

rem --- Kill any process already bound to that port ---
set "KILLED=0"
for /f "tokens=5" %%P in ('netstat -ano ^| findstr LISTENING ^| findstr /C:":%PORT%"') do (
    set "PROC=unknown"
    for /f "tokens=1 delims=," %%N in ('tasklist /FI "PID eq %%P" /FO CSV /NH 2^>nul') do set "PROC=%%~N"
    echo Killing process %%P ^(!PROC!^) currently using port %PORT% ...
    taskkill /F /PID %%P >nul 2>&1
    set "KILLED=1"
)
if "!KILLED!"=="0" echo Port %PORT% was free.

echo.
echo Starting The AI Conclave Switchboard on %HOST%:%PORT% ...
echo Press Ctrl+C to stop.
echo.
"%PYEXE%" -m uvicorn app.main:app --host %HOST% --port %PORT%

echo.
echo Service stopped.
pause
