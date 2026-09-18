#!/bin/bash
# =============================================================================
#  Foreground startup script for The AI Conclave Switchboard (macOS/Linux).
#  Windows equivalent: start.bat
#
#  What it does:
#    1. Resolves the Python interpreter (prefers .venv, falls back to PATH).
#    2. Asks the app's own config loader for the configured host/port
#       (falls back to 127.0.0.1:8787 if config can't be resolved yet).
#    3. Kills whatever process is already bound to that port.
#    4. Runs uvicorn in the foreground so you see startup + request logs.
#
#  This is a plain dev/ops console launcher. For a double-click launcher
#  with no Terminal window and an auto-opened browser tab, use the
#  "The AI Conclave.app" bundle built by tools/install-desktop-app.sh
#  instead — that one checks /api/health first rather than killing the
#  port, since it expects the service to already be running.
#
#  Double-click this file in Finder to run it (Terminal opens automatically
#  for .command files), or run `bash start.command` / `./start.command`
#  from a terminal.
# =============================================================================

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)" || exit 1

# --- Resolve the Python interpreter: prefer the project's .venv, fall back to PATH ---
if [ -x ".venv/bin/python" ]; then
    PYEXE=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
    PYEXE="python3"
elif command -v python >/dev/null 2>&1; then
    PYEXE="python"
else
    echo "ERROR: No Python interpreter found."
    echo "Create .venv (python3 -m venv .venv) or install Python 3.13+ on PATH."
    read -n 1 -s -r -p "Press any key to close..."
    echo ""
    exit 1
fi

# --- Resolve host/port from config.yaml via the app's own config loader ---
HOST="127.0.0.1"
PORT="8787"
RESOLVED=$("$PYEXE" -c "from app.config import get_config; c=get_config(); print(c.server.host, c.server.port)" 2>/dev/null)
if [ -n "$RESOLVED" ]; then
    read -r HOST PORT <<< "$RESOLVED"
fi

echo ""
echo "Target: ${HOST}:${PORT}"

# --- Kill any process already bound to that port ---
PIDS=$(lsof -ti tcp:"$PORT" 2>/dev/null)
if [ -n "$PIDS" ]; then
    for PID in $PIDS; do
        NAME=$(ps -p "$PID" -o comm= 2>/dev/null)
        NAME=${NAME:-unknown}
        echo "Killing process $PID ($NAME) currently using port $PORT ..."
        kill -9 "$PID" 2>/dev/null
    done
else
    echo "Port $PORT was free."
fi

echo ""
echo "Starting The AI Conclave Switchboard on ${HOST}:${PORT} ..."
echo "Press Ctrl+C to stop."
echo ""
"$PYEXE" -m uvicorn app.main:app --host "$HOST" --port "$PORT"

echo ""
echo "Service stopped."
read -n 1 -s -r -p "Press any key to close..."
echo ""
