"""Resumable chunked uploads written directly to the persistent data volume."""
from __future__ import annotations

import os
import re
import time
import uuid
from pathlib import Path

import ingest
import storage

CHUNK_SIZE = int(os.environ.get("PODCAST_CLIPPER_CHUNK_MB", "8")) * 1024 * 1024
MAX_UPLOAD_BYTES = int(os.environ.get("PODCAST_CLIPPER_MAX_UPLOAD_GB", "20")) * 1024**3
ALLOWED_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}


class UploadError(ValueError):
    pass


class UploadManager:
    def __init__(self, store: storage.Store):
        self.store = store
        self.root = store.uploads
        self.root.mkdir(parents=True, exist_ok=True)

    def _dir(self, upload_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{12}", upload_id):
            raise UploadError("Invalid upload ID.")
        return self.root / upload_id

    def _state_path(self, upload_id: str) -> Path:
        return self._dir(upload_id) / "upload.json"

    def part_path(self, upload_id: str) -> Path:
        return self._dir(upload_id) / "source.mp4.part"

    def read(self, upload_id: str) -> dict:
        path = self._state_path(upload_id)
        if not path.is_file():
            raise UploadError("Upload not found.")
        return storage.read_json(path)

    def _save(self, state: dict) -> None:
        state["updated"] = time.time()
        storage.atomic_json(self._state_path(state["id"]), state)

    def create(self, filename: str, size: int, metadata: dict | None = None) -> dict:
        if not isinstance(filename, str) or not filename.strip():
            raise UploadError("Provide a filename.")
        if not isinstance(size, int) or size <= 0:
            raise UploadError("Provide a positive file size.")
        if size > MAX_UPLOAD_BYTES:
            raise UploadError(
                f"File is larger than the {MAX_UPLOAD_BYTES / 1024**3:.0f} GB limit.")
        extension = Path(filename).suffix.lower()
        if extension not in ALLOWED_EXTS:
            raise UploadError("Upload an MP4, MOV, MKV, WebM, or M4V video.")
        self.store.check_free(size)
        upload_id = uuid.uuid4().hex[:12]
        directory = self._dir(upload_id)
        directory.mkdir(parents=True, exist_ok=False)
        state = {
            "id": upload_id,
            "filename": storage.safe_name(filename),
            "original_filename": filename,
            "size": size,
            "received": 0,
            "chunk_size": CHUNK_SIZE,
            "status": "uploading",
            "created": time.time(),
            "updated": time.time(),
            "metadata": metadata or {},
            "source_id": None,
            "error": None,
        }
        self.part_path(upload_id).touch()
        self._save(state)
        return state

    def append(self, upload_id: str, offset: int, data: bytes) -> dict:
        state = self.read(upload_id)
        if state["status"] != "uploading":
            raise UploadError("This upload is not accepting chunks.")
        if not isinstance(offset, int) or offset < 0:
            raise UploadError("Invalid upload offset.")
        part = self.part_path(upload_id)
        current = part.stat().st_size
        if offset != current:
            raise UploadError(f"Offset mismatch. Server has {current} bytes.")
        if current + len(data) > state["size"]:
            raise UploadError("Chunk exceeds the declared file size.")
        if not data:
            raise UploadError("Empty upload chunk.")
        with part.open("ab") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        state["received"] = part.stat().st_size
        self._save(state)
        return state

    def complete(self, upload_id: str) -> dict:
        state = self.read(upload_id)
        if state["status"] == "complete" and state.get("source_id"):
            return state
        part = self.part_path(upload_id)
        actual = part.stat().st_size
        if actual != state["size"]:
            raise UploadError(
                f"Upload incomplete: {actual} of {state['size']} bytes received.")
        self.store.check_free(0)
        try:
            probe = ingest.probe_source(str(part))
        except (OSError, ValueError) as exc:
            raise UploadError(f"Could not read the uploaded video: {exc}") from exc
        if not probe.get("duration"):
            raise UploadError("The uploaded file has no readable duration.")
        if probe.get("has_audio") is False:
            raise UploadError(
                "The uploaded video has no audio track. Podcast episodes need audio.")
        record = self.store.consume_upload(
            part, state["original_filename"],
            {**state.get("metadata", {}), "probe": probe})
        state.update(status="complete", source_id=record["id"], received=actual,
                     probe=probe, completed=time.time())
        self._save(state)
        return state

    def abort(self, upload_id: str) -> None:
        state = self.read(upload_id)
        if state["status"] == "complete":
            raise UploadError("Completed uploads cannot be aborted; delete the source instead.")
        import shutil
        shutil.rmtree(self._dir(upload_id), ignore_errors=True)

    def list(self) -> list[dict]:
        items = []
        for path in sorted(self.root.glob("*/upload.json")):
            try:
                state = storage.read_json(path)
                state["offset"] = (self.part_path(state["id"]).stat().st_size
                                   if self.part_path(state["id"]).exists() else 0)
                items.append(state)
            except (OSError, ValueError):
                continue
        return sorted(items, key=lambda item: item.get("created", 0), reverse=True)
