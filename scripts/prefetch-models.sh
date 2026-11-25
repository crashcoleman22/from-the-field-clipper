#!/usr/bin/env bash
# Prefetch the Parakeet ASR model and the face detector used for layout hints.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA="${PODCAST_CLIPPER_DATA:-/home/coder/podcast-clipper-data}"
PY="$ROOT/.venv/bin/python"
mkdir -p "$DATA/models" "$DATA/logs"

export CHOPIFY_MODELS_DIR="$DATA/models"
cd "$ROOT"
"$PY" - <<'PY'
import os
from pathlib import Path
import fast_asr
import render_clips
models = Path(os.environ["CHOPIFY_MODELS_DIR"])
fast_asr.ensure_model() if hasattr(fast_asr, "ensure_model") else None
try:
    render_clips._make_detector(models / "layout")
    print("Face detector ready:", models / "yunet.onnx")
except Exception as exc:
    print("Face detector prefetch skipped:", exc)
print("Models:", models)
PY
