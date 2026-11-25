"""Persistent storage layout for the From the Field clipper.

All large artifacts live outside the Git checkout. Sources are immutable and
content-addressed; projects reference them instead of copying them.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
from pathlib import Path

DEFAULT_DATA_DIR = Path(
    os.environ.get("PODCAST_CLIPPER_DATA")
    or os.environ.get("CHOPIFY_DATA_DIR")
    or "/home/coder/podcast-clipper-data"
).expanduser()
MIN_FREE_BYTES = int(os.environ.get("PODCAST_CLIPPER_MIN_FREE_GB", "30")) * 1024**3
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(chunk_size), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path: Path, data) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False),
                    encoding="utf-8")
    temp.replace(path)


def read_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def safe_name(name: str, fallback: str = "episode") -> str:
    stem = Path(name or fallback).name
    stem = re.sub(r"[^A-Za-z0-9._ -]+", "-", stem).strip(" .-")
    stem = re.sub(r"\s+", "-", stem)
    if not stem:
        stem = fallback
    if not Path(stem).suffix:
        stem += ".mp4"
    return stem[:140]


class Store:
    """Data root with upload, source, project, brand, and export paths."""

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root or DEFAULT_DATA_DIR).expanduser().resolve()
        self.sources = self.root / "sources"
        self.uploads = self.root / "uploads"
        self.projects = self.root / "projects"
        self.brand = self.root / "brand"
        self.models = self.root / "models"
        self.tmp = self.root / "tmp"
        self.exports = self.root / "exports"
        self.db_path = self.root / "app.db"
        self._ensure()

    def _ensure(self) -> None:
        for path in (self.root, self.sources, self.uploads, self.projects,
                     self.brand, self.models, self.tmp, self.exports):
            path.mkdir(parents=True, exist_ok=True)

    # -- disk ---------------------------------------------------------------
    def disk(self) -> dict:
        usage = shutil.disk_usage(self.root)
        return {
            "total": usage.total,
            "used": usage.used,
            "free": usage.free,
            "min_free": MIN_FREE_BYTES,
            "ok": usage.free >= MIN_FREE_BYTES,
        }

    def check_free(self, incoming_bytes: int = 0) -> None:
        free = self.disk()["free"]
        required = max(MIN_FREE_BYTES, int(incoming_bytes * 2.2) + 2 * 1024**3)
        if free < required:
            raise ValueError(
                f"Not enough free disk space. {free / 1024**3:.1f} GB free; "
                f"need at least {required / 1024**3:.1f} GB including the "
                f"{MIN_FREE_BYTES / 1024**3:.0f} GB safety reserve.")

    # -- sources ------------------------------------------------------------
    def source_dir(self, source_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{64}", source_id):
            raise ValueError("Invalid source ID.")
        return self.sources / source_id

    def source_record_path(self, source_id: str) -> Path:
        return self.source_dir(source_id) / "source.json"

    def read_source(self, source_id: str) -> dict:
        return read_json(self.source_record_path(source_id))

    def list_sources(self) -> list[dict]:
        records = []
        for path in sorted(self.sources.glob("*/source.json")):
            try:
                record = read_json(path)
                record["exists"] = Path(record["path"]).is_file()
                record["size_bytes"] = (Path(record["path"]).stat().st_size
                                        if record["exists"] else 0)
                records.append(record)
            except (OSError, ValueError, KeyError):
                continue
        return sorted(records, key=lambda item: item.get("created", 0), reverse=True)

    def consume_upload(self, part_path: Path, original_name: str,
                       metadata: dict | None = None) -> dict:
        """Hash, dedupe, and move an upload into the immutable source store."""
        part_path = Path(part_path)
        size = part_path.stat().st_size
        digest = sha256_file(part_path)
        directory = self.source_dir(digest)
        directory.mkdir(parents=True, exist_ok=True)
        record_path = directory / "source.json"
        filename = safe_name(original_name)
        target = directory / filename
        if record_path.exists() and target.exists():
            # Same bytes already stored: drop the duplicate upload.
            try:
                part_path.unlink()
            except OSError:
                pass
            record = read_json(record_path)
            record["deduplicated"] = True
            return record
        os.replace(part_path, target)
        try:
            target.chmod(0o444)
        except OSError:
            pass
        record = {
            "id": digest,
            "sha256": digest,
            "path": str(target),
            "original_name": original_name,
            "filename": filename,
            "size": size,
            "created": time.time(),
            "metadata": metadata or {},
        }
        atomic_json(record_path, record)
        return record

    def delete_source(self, source_id: str, delete_projects: bool = False) -> dict:
        record = self.read_source(source_id)
        source = Path(record["path"])
        try:
            source.chmod(0o644)
            source.unlink()
        except OSError:
            pass
        shutil.rmtree(self.source_dir(source_id), ignore_errors=True)
        removed_projects = []
        if delete_projects:
            for project in self.projects.glob("*/project.json"):
                try:
                    state = read_json(project)
                except (OSError, ValueError):
                    continue
                if state.get("source_sha256") == source_id or state.get("source") == str(source):
                    removed_projects.append(project.parent.name)
                    shutil.rmtree(project.parent, ignore_errors=True)
        return {"removed": source_id, "projects": removed_projects}

    # -- projects -----------------------------------------------------------
    def project_dir(self, project_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{12}", project_id):
            raise ValueError("Invalid project ID.")
        return self.projects / project_id

    def list_projects(self) -> list[dict]:
        result = []
        for path in self.projects.glob("*/project.json"):
            try:
                result.append(read_json(path))
            except (OSError, ValueError):
                continue
        return sorted(result, key=lambda p: p.get("created", 0), reverse=True)

    def usage(self) -> dict:
        def directory_size(path: Path) -> int:
            total = 0
            for item in path.rglob("*"):
                try:
                    if item.is_file():
                        total += item.stat().st_size
                except OSError:
                    pass
            return total

        projects = []
        for project in self.list_projects():
            directory = self.project_dir(project["id"])
            projects.append({
                "id": project["id"],
                "source": project.get("source"),
                "title": (project.get("meta") or {}).get("episode_title")
                         or Path(str(project.get("source", ""))).name,
                "status": project.get("status"),
                "bytes": directory_size(directory),
            })
        return {
            "disk": self.disk(),
            "sources": self.list_sources(),
            "projects": projects,
            "source_bytes": directory_size(self.sources),
            "project_bytes": directory_size(self.projects),
            "export_bytes": directory_size(self.exports),
            "model_bytes": directory_size(self.models),
            "upload_bytes": directory_size(self.uploads),
        }

    def cleanup(self) -> dict:
        """Remove stale upload parts and render scratch directories."""
        removed = []
        cutoff = time.time() - 24 * 3600
        for part in self.uploads.glob("*/*.part"):
            try:
                if part.stat().st_mtime < cutoff:
                    part.unlink()
                    removed.append(str(part))
            except OSError:
                pass
        for pattern in (".work-*", "audio_parts", "visual_sections"):
            for path in self.projects.glob(f"*/{pattern}"):
                try:
                    if path.is_dir():
                        shutil.rmtree(path, ignore_errors=True)
                        removed.append(str(path))
                except OSError:
                    pass
        return {"removed": removed}
