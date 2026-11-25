#!/usr/bin/env bash
# Optional fallback autostart. The preferred method is the Coder template's
# workspace startup command:
#   bash /home/coder/workspace/from-the-field-clipper/scripts/start.sh
# This script adds a guarded idempotent line to ~/.profile for environments
# where login shells start the workspace.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MARKER="# From the Field Clipper autostart"
if grep -qF "$MARKER" "$HOME/.profile" 2>/dev/null; then
  echo "Autostart already installed in ~/.profile"
  exit 0
fi
{
  echo ""
  echo "$MARKER"
  echo "if [ -x \"$ROOT/scripts/start.sh\" ]; then"
  echo "  \"$ROOT/scripts/start.sh\" >/dev/null 2>&1 || true"
  echo "fi"
} >> "$HOME/.profile"
echo "Added guarded autostart to ~/.profile"
echo "For guaranteed startup, configure the Coder template workspace startup command:"
echo "  bash $ROOT/scripts/start.sh"
