#!/usr/bin/env bash
# ============================================================
#  NEURO.CORE - one-click launcher for macOS / Linux
#  First run: creates venv + installs deps. Next runs: instant.
# ============================================================

set -euo pipefail

# Change to script directory regardless of where it's called from
cd "$(dirname "$0")"

# --- 1. Check Python ---
if ! command -v python3 &>/dev/null; then
    echo "[ERROR] Python3 not found in PATH."
    echo "Install Python 3.11 or 3.12: https://www.python.org/downloads/"
    exit 1
fi

PYTHON=python3

# --- 2. Create venv on first run ---
if [ ! -f ".venv/bin/python" ]; then
    echo "[SETUP] Creating virtual environment..."
    $PYTHON -m venv .venv
    echo "[SETUP] Installing dependencies (this may take a minute)..."
    .venv/bin/python -m pip install --upgrade pip
    .venv/bin/python -m pip install -r requirements.txt
fi

# --- 3. Check config file ---
if [ ! -f "1.envv" ]; then
    echo "[WARN] 1.envv not found. Copying template from 1.envv.example ..."
    cp "1.envv.example" "1.envv"
    echo "[WARN] Open 1.envv and fill in your API keys, then run run.sh again."
    # Try to open with default editor; fall back to nano
    if command -v open &>/dev/null; then
        open "1.envv"          # macOS
    elif command -v xdg-open &>/dev/null; then
        xdg-open "1.envv"     # Linux desktop
    else
        nano "1.envv"
    fi
    exit 0
fi

# --- 4. Launch ---
echo "[RUN] Starting NEURO.CORE dashboard..."
.venv/bin/python run_dashboard.py
