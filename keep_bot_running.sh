#!/usr/bin/env bash
# keep_bot_running.sh - Watchdog script to keep textbox_bot.py running 24/7 locally
# Automatically restarts the bot if it crashes or disconnects.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON_BIN="$SCRIPT_DIR/.venv/bin/python"
if [ ! -f "$PYTHON_BIN" ]; then
    PYTHON_BIN="python3"
fi

echo "=========================================="
echo "Starting Textbox Bot Keep-Alive Supervisor"
echo "Python binary: $PYTHON_BIN"
echo "Working directory: $SCRIPT_DIR"
echo "=========================================="

while true; do
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Starting textbox_bot.py..."
    "$PYTHON_BIN" textbox_bot.py
    EXIT_CODE=$?
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] textbox_bot.py exited with code $EXIT_CODE. Restarting in 5 seconds..."
    sleep 5
done
