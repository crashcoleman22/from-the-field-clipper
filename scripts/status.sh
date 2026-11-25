#!/usr/bin/env bash
# Print status for the background app.
set -euo pipefail

DATA="${PODCAST_CLIPPER_DATA:-/home/coder/podcast-clipper-data}"
PORT="${PODCAST_CLIPPER_PORT:-8765}"
PIDFILE="$DATA/app.pid"

if [[ -f "$PIDFILE" ]] && pid="$(cat "$PIDFILE")" && kill -0 "$pid" 2>/dev/null; then
  echo "Running (pid $pid), port $PORT"
else
  echo "Not running (no live PID file)."
fi
"$(dirname "$0")/../.venv/bin/python" - "$PORT" <<'PY' || true
import json, sys, urllib.request
port = sys.argv[1]
with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/config", timeout=2) as response:
    data = json.load(response)
print("HTTP OK · access code required:", data["access_code_required"])
PY
