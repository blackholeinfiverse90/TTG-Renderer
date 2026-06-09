#!/bin/bash
set -e

PORT=${PORT:-8080}

# Start server.py in background
uvicorn server:app --host 0.0.0.0 --port $PORT &
SERVER_PID=$!

# Set connector env vars to point at local server.py
export ATHARVA_HTTP=http://localhost:$PORT
export ATHARVA_WS=ws://localhost:$PORT/ws

# Wait for server.py to boot
echo "[START] Waiting for server.py to boot..."
sleep 5

# Start connector (keeps process alive — Render watches this)
echo "[START] Starting connector..."
node ttg_engine_connector.js

# If connector exits, kill server.py too
kill $SERVER_PID
