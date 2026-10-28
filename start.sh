#!/bin/sh
# Start Processing Agent at http://localhost:8000.
# The graceful-shutdown timeout keeps reloads from hanging on open browser connections.
cd "$(dirname "$0")" && exec .venv/bin/uvicorn app.server:app --reload --port 8000 --timeout-graceful-shutdown 2 "$@"
