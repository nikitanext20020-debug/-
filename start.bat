@echo off
REM ============================================================
REM  NEURO.CORE - one-click launcher for Windows
REM  First run: creates venv + installs deps. Next runs: instant.
REM ============================================================
setlocal
cd /d "%~dp0"

REM --- 1. Check Python ---
where python >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Python not found in PATH.
    echo Install Python 3.11 or 3.12 from https://www.python.org/downloads/
    echo During install, tick "Add Python to PATH".
    pause
    exit /b 1
)

REM --- 2. Create venv on first run ---
if not exist ".venv\Scripts\python.exe" (
    echo [SETUP] Creating virtual environment...
    python -m venv .venv
    echo [SETUP] Installing dependencies ^(this may take a minute^)...
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
)

REM --- 3. Check config file ---
if not exist "1.envv" (
    echo [WARN] 1.envv not found. Copying template from 1.envv.example ...
    copy "1.envv.example" "1.envv" >nul
    echo [WARN] Open 1.envv and fill in your API keys, then run start.bat again.
    notepad "1.envv"
    pause
    exit /b 0
)

REM --- 4. Launch ---
echo [RUN] Starting NEURO.CORE dashboard...
".venv\Scripts\python.exe" run_dashboard.py

pause
