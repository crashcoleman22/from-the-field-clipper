#!/usr/bin/env bash
# Stop the background app started by scripts/start.sh.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA="${PODCAST_CLIPPER_DATA:-/home/coder/podcast-clipper-data}"
PIDFILE="$DATA/app.pid"

if [[ ! -f "$PIDFILE" ]]; then
  echo "No PID file at $PIDFILE"
  exit 0
fi
pid="$(cat "$PIDFILE" 2>/dev/null || true)"
if [[ -z "$pid" ]] || ! kill -0 "$pid" 2>/dev/null; then
  echo "Not running."
  rm -f "$PIDFILE"
  exit 0
fi
kill "$pid" 2>/dev/null || true
for _ in $(seq 1 40); do
  if ! kill -0 "$pid" 2>/dev/null; then
    rm -f "$PIDFILE"
    echo "Stopped."
    exit 0
  fi
  sleep 0.25
done
kill -9 "$pid" 2>/dev/null || true
rm -f "$PIDFILE"
echo "Force-stopped."
