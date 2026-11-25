#!/usr/bin/env bash
# Start the From the Field clipper in the background (idempotent).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA="${PODCAST_CLIPPER_DATA:-/home/coder/podcast-clipper-data}"
HOST="${PODCAST_CLIPPER_HOST:-0.0.0.0}"
PORT="${PODCAST_CLIPPER_PORT:-8765}"
PY="$ROOT/.venv/bin/python"
PIDFILE="$DATA/app.pid"
LOG="$DATA/logs/app.log"

mkdir -p "$DATA/logs"
if [[ ! -x "$PY" ]]; then
  echo "Missing virtualenv: $PY" >&2
  echo "Create it with: uv venv --python 3.12 .venv && uv pip install -r requirements.txt -r requirements-dev.txt" >&2
  exit 1
fi

if [[ -f "$PIDFILE" ]]; then
  pid="$(cat "$PIDFILE" 2>/dev/null || true)"
  if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
    echo "Already running (pid $pid) on $HOST:$PORT"
    exit 0
  fi
  rm -f "$PIDFILE"
fi

export PODCAST_CLIPPER_DATA="$DATA"
export CHOPIFY_MODELS_DIR="$DATA/models"
export CHOPIFY_ASR_PARTS="${CHOPIFY_ASR_PARTS:-2}"
export CHOPIFY_RENDER_WORKERS="${CHOPIFY_RENDER_WORKERS:-2}"
export CHOPIFY_SCAN_WORKERS="${CHOPIFY_SCAN_WORKERS:-2}"
export CHOPIFY_SECTION_WORKERS="${CHOPIFY_SECTION_WORKERS:-2}"
export PYTHONUNBUFFERED=1

cd "$ROOT"
setsid "$PY" app.py --data-dir "$DATA" --host "$HOST" --port "$PORT" \
  >> "$LOG" 2>&1 < /dev/null &
pid=$!
echo "$pid" > "$PIDFILE"

for _ in $(seq 1 40); do
  if "$PY" - "$PORT" <<'PY' >/dev/null 2>&1
import json, sys, urllib.request
port = sys.argv[1]
with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1) as response:
    assert json.load(response)["ok"]
PY
  then
    echo "From the Field Clipper running: http://$HOST:$PORT (pid $pid)"
    echo "Data: $DATA"
    echo "Log: $LOG"
    exit 0
  fi
  if ! kill -0 "$pid" 2>/dev/null; then
    echo "App exited during startup. Recent log:" >&2
    tail -40 "$LOG" >&2 || true
    rm -f "$PIDFILE"
    exit 1
  fi
  sleep 0.25
done

echo "App started but health check did not answer yet (pid $pid). See $LOG" >&2
exit 0
