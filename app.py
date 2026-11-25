"""From the Field podcast clipper: Coder-hosted review and export app."""
from __future__ import annotations

import argparse
import html
import json
import mimetypes
import os
import re
import secrets
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

import ai_client
import editorial
import episodes
import podcast_auth
import projects
import job_queue
import storage
import uploads

HERE = Path(__file__).resolve().parent
STATIC = HERE / "assets" / "app"
PIPELINE = ("Upload", "Transcribe", "Identify", "Rank", "Captions & metadata",
            "Human review", "Approve", "Export", "Publish (coming soon)",
            "Analyze (coming soon)")
SECTIONS = [
    {"id": "overview", "label": "Overview", "available": True},
    {"id": "upload", "label": "Upload Episodes", "available": True},
    {"id": "queue", "label": "Processing Queue", "available": True},
    {"id": "review", "label": "Clips Awaiting Review", "available": True},
    {"id": "approved", "label": "Approved Clips", "available": True},
    {"id": "calendar", "label": "Content Calendar", "available": False},
    {"id": "published", "label": "Published Content", "available": False},
    {"id": "analytics", "label": "Analytics", "available": False},
    {"id": "brand", "label": "Brand Settings", "available": True},
    {"id": "storage", "label": "Storage Management", "available": True},
]
MAX_JSON = 2 * 1024 * 1024
MAX_CHUNK = 32 * 1024 * 1024


def now() -> float:
    return time.time()


class App:
    def __init__(self, data_root=None, host="0.0.0.0", port=8765):
        self.store = storage.Store(data_root)
        self.auth = podcast_auth.Auth(self.store.root)
        self.uploads = None
        import uploads as upload_module
        self.uploads = upload_module.UploadManager(self.store)
        self.queue = job_queue.JobQueue(self.store, self._run_job)
        self.host = host
        self.port = port
        self.started = now()
        self.recovered = self._recover_episodes()

    def _run_job(self, job: dict, log_path: Path) -> dict:
        episode_id = job["episode_id"]
        kind = job["kind"]
        try:
            if kind == "prepare":
                return episodes.prepare_episode(self.store, self.queue, episode_id)
            if kind == "preview":
                return episodes.preview_episode(self.store, self.queue, episode_id)
            if kind == "render":
                clip_ids = (job.get("meta") or {}).get("clip_ids")
                return episodes.render_approved(self.store, self.queue, episode_id,
                                                clip_ids)
            raise ValueError("Unknown job kind.")
        except (Exception, SystemExit) as exc:
            try:
                episode = episodes.read_episode(self.store, episode_id)
                if episode.get("status") not in {"approved"}:
                    episode.update(status="failed", stage="Job failed",
                                   error=str(exc)[:4000], updated=now())
                    episodes.save_episode(self.store, episode_id, episode)
            except (OSError, ValueError):
                pass
            raise

    def _recover_episodes(self) -> int:
        """Restart interrupted work automatically after an app restart."""
        active = {(job.get("episode_id"), job.get("kind"))
                  for job in self.queue.status()["jobs"]
                  if job.get("status") in {"pending", "running"}}
        recovered = 0
        for path in self.store.projects.glob("*/episode.json"):
            try:
                episode = storage.read_json(path)
            except (OSError, ValueError):
                continue
            episode_id = episode.get("id")
            status = episode.get("status")
            if not episode_id or (episode_id, "prepare") in active:
                continue
            if status in {"queued", "processing"}:
                self.queue.enqueue(episode_id, "prepare", force=True)
                recovered += 1
            elif status in {"rendering", "rendering_previews"}:
                kind = "preview" if status == "rendering_previews" else "render"
                approved = [clip["clip_id"] for clip in episodes.merged_clips(self.store, episode_id)
                            if clip.get("decision") == episodes.APPROVED]
                if kind == "render" and approved:
                    self.queue.enqueue(episode_id, kind,
                                       meta={"clip_ids": approved}, force=True)
                elif kind == "preview":
                    self.queue.enqueue(episode_id, kind, force=True)
                recovered += 1
            elif not (path.parent / "editorial.json").is_file():
                state = storage.read_json(path.parent / "project.json") \
                    if (path.parent / "project.json").is_file() else {}
                if state.get("status") == "review":
                    self.queue.enqueue(episode_id, "prepare", force=True)
                    recovered += 1
        return recovered

    def shutdown(self) -> None:
        self.queue.shutdown()

    # -- summaries ----------------------------------------------------------
    def episode_summary(self, episode: dict) -> dict:
        path = episodes.episode_path(self.store, episode["id"])
        state = {}
        try:
            state = projects.read_project(path)
        except (OSError, ValueError):
            pass
        episode = dict(episode)
        episode["pipeline_status"] = state.get("status")
        episode["pipeline_stage"] = state.get("stage")
        episode["progress"] = state.get("progress")
        episode["source_url"] = f"/media/source/{episode['id']}"
        episode["review_url"] = f"/api/episodes/{episode['id']}"
        return episode

    def episodes_list(self) -> list[dict]:
        items = []
        for path in self.store.projects.glob("*/episode.json"):
            try:
                episode = storage.read_json(path)
            except (OSError, ValueError):
                continue
            items.append(self.episode_summary(episode))
        return sorted(items, key=lambda item: item.get("created", 0), reverse=True)

    def overview(self) -> dict:
        summaries = self.episodes_list()
        counts = {"episodes": len(summaries), "processing": 0, "review": 0,
                  "approved_clips": 0, "awaiting_review": 0, "failed": 0,
                  "rejected": 0}
        for item in summaries:
            status = item.get("status")
            if status in {"queued", "processing", "rendering", "rendering_previews"}:
                counts["processing"] += 1
            if status == "clips_awaiting_review":
                counts["review"] += 1
            if status == "approved":
                counts["approved_clips"] += item.get("counts", {}).get("exported", 0)
            if status == "failed":
                counts["failed"] += 1
            counts["awaiting_review"] += item.get("counts", {}).get("clips", 0) - \
                item.get("counts", {}).get("approved", 0) - \
                item.get("counts", {}).get("rejected", 0)
            counts["rejected"] += item.get("counts", {}).get("rejected", 0)
        counts["awaiting_review"] = max(0, counts["awaiting_review"])
        brands = episodes.load_brand(self.store)
        return {
            "counts": counts,
            "episodes": summaries[:8],
            "queue": self.queue.status(),
            "disk": self.store.disk(),
            "brand": {"podcast_name": brands["podcast_name"],
                      "logo": bool(brands.get("logo"))},
            "ai": ai_client.public_status(self.store.root),
            "recovered": self.recovered,
            "pipeline": PIPELINE,
        }


class Handler(BaseHTTPRequestHandler):
    server_version = "FromTheFieldClipper/1.0"

    def log_message(self, fmt, *args):
        pass

    @property
    def app(self) -> App:
        return self.server.app

    # -- response plumbing --------------------------------------------------
    def end_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self'; script-src 'self'; "
            "media-src 'self' blob:; img-src 'self' data:; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.send_header("Cache-Control", "no-store")
        if getattr(self, "_set_cookie", None):
            self.send_header("Set-Cookie", self._set_cookie)
        super().end_headers()

    def send_json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, body: str, status=200):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, location: str):
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def send_error_json(self, message, status=400):
        self.send_json({"error": str(message)}, status)

    def send_file(self, path: Path, download: bool = False):
        if not path.is_file():
            raise FileNotFoundError(path)
        size = path.stat().st_size
        start, end, status = 0, size - 1, 200
        requested = self.headers.get("Range")
        if requested:
            match = re.fullmatch(r"bytes=(\d+)-(\d*)", requested)
            if not match:
                return self.send_error_json("Unsupported byte range.", 416)
            start = int(match[1])
            end = min(int(match[2]) if match[2] else end, end)
            if start > end or start >= size:
                return self.send_error_json("Range outside file.", 416)
            status = 206
        self.send_response(status)
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_header("Content-Type", mime)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(max(0, end - start + 1)))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        if download:
            self.send_header("Content-Disposition",
                             f'attachment; filename="{path.name}"')
        self.end_headers()
        try:
            with path.open("rb") as handle:
                handle.seek(start)
                remaining = end - start + 1
                while remaining > 0:
                    chunk = handle.read(min(65536, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    # -- request helpers ----------------------------------------------------
    def _parts(self) -> list[str]:
        return [part for part in unquote(urlsplit(self.path).path).strip("/").split("/")
                if part]

    def _query(self) -> dict:
        return {key: values[-1] for key, values in
                parse_qs(urlsplit(self.path).query).items()}

    def _json_body(self, limit=MAX_JSON) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("Invalid Content-Length.")
        if not 0 < length <= limit:
            raise ValueError("Request body is empty or too large.")
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw)
        except ValueError as exc:
            raise ValueError(f"Invalid JSON: {exc}") from exc
        if not isinstance(body, dict):
            raise ValueError("Expected a JSON object.")
        return body

    def _raw_body(self, limit=MAX_CHUNK) -> bytes:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("Invalid Content-Length.")
        if not 0 < length <= limit:
            raise ValueError("Upload chunk is empty or too large.")
        return self.rfile.read(length)

    def _session(self):
        token = self.app.auth.cookie_from(self)
        if token is None:
            cookie, token = self.app.auth.new_session()
            self._set_cookie = cookie
        return token

    def _gate(self, parts: list[str], method: str) -> bool:
        if not self.app.auth.host_allowed(self.headers.get("Host", "")):
            self.send_error_json(
                "This app only accepts requests from its Coder proxy address.", 421)
            return False
        if parts == ["healthz"]:
            return True
        if method == "GET" and parts and parts[0] == "static":
            return True
        if parts == ["login"]:
            if method == "GET":
                return True
            if method == "POST":
                return True
        if not self.app.auth.authenticated(self):
            if parts[:1] == ["api"]:
                self.send_error_json("Sign in to continue.", 401)
            else:
                self.redirect("/login")
            return False
        self._session()
        return True

    def _csrf(self) -> bool:
        return self.app.auth.csrf_ok(self)

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        try:
            origin_host = urlsplit(origin).netloc.lower()
        except ValueError:
            return False
        return origin_host == (self.headers.get("Host", "").lower())

    # -- dispatch -----------------------------------------------------------
    def do_GET(self):
        parts = self._parts()
        if not self._gate(parts, "GET"):
            return
        try:
            self.route_get(parts)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except FileNotFoundError:
            self.send_error_json("Not found.", 404)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            self.send_error_json(str(exc), 400)

    def do_POST(self):
        parts = self._parts()
        if not self._gate(parts, "POST"):
            return
        try:
            if parts == ["login"]:
                return self.login_post()
            if not self._origin_ok():
                return self.send_error_json("Cross-origin request rejected.", 403)
            if not self._csrf():
                return self.send_error_json("Reload the page and retry.", 403)
            self.route_post(parts)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except FileNotFoundError:
            self.send_error_json("Not found.", 404)
        except (ValueError, KeyError, TypeError, OSError) as exc:
            self.send_error_json(str(exc), 400)

    def do_DELETE(self):
        self.do_POST()

    def do_PATCH(self):
        self.do_POST()

    def do_PUT(self):
        self.do_POST()

    # -- GET routes ---------------------------------------------------------
    def route_get(self, parts: list[str]):
        if parts == ["healthz"]:
            return self.send_json({"ok": True, "service": "from-the-field-clipper"})
        if parts == ["login"]:
            if not self.app.auth.code:
                return self.redirect("/")
            return self.send_html(login_page())
        if parts == ["logout"]:
            self._set_cookie = ("ftf_session=; Path=/; HttpOnly; SameSite=Strict; "
                               "Max-Age=0")
            return self.redirect("/login" if self.app.auth.code else "/")
        if not parts:
            return self.send_file(STATIC / "index.html")
        if parts[0] == "static" and len(parts) == 2 and parts[1] in {
                "app.js", "style.css", "index.html"}:
            return self.send_file(STATIC / parts[1])
        if parts == ["api", "config"]:
            return self.send_json(self.config_payload())
        if parts == ["api", "overview"]:
            return self.send_json(self.app.overview())
        if parts == ["api", "queue"]:
            return self.send_json(self.app.queue.status())
        if parts == ["api", "uploads"]:
            return self.send_json({"uploads": self.app.uploads.list()})
        if parts == ["api", "episodes"]:
            return self.send_json({"episodes": self.app.episodes_list()})
        if len(parts) == 3 and parts[:2] == ["api", "episodes"]:
            return self.send_json(self.episode_detail(parts[2]))
        if len(parts) == 4 and parts[:2] == ["api", "episodes"] and parts[3] == "log":
            return self.send_json(self.episode_log(parts[2]))
        if parts == ["api", "clips"]:
            decision = self._query().get("decision", "approved")
            return self.send_json({"clips": self.clips(decision)})
        if parts == ["api", "brand"]:
            brand = episodes.load_brand(self.app.store)
            brand["logo_url"] = ("/media/brand/logo?t=" + str(int(brand.get("updated") or 0))
                                 if brand.get("logo") else None)
            brand["intro_url"] = "/media/brand/intro" if brand.get("intro") else None
            brand["outro_url"] = "/media/brand/outro" if brand.get("outro") else None
            return self.send_json(brand)
        if parts == ["api", "storage"]:
            return self.send_json(self.storage_payload())
        if parts == ["api", "ai"]:
            return self.send_json(ai_client.public_status(self.app.store.root))
        if parts[:2] == ["media", "source"] and len(parts) == 3:
            return self.send_file(self.source_file(parts[2]))
        if parts[:2] == ["media", "project"] and len(parts) == 5:
            return self.project_media(parts[2], parts[3], parts[4])
        if parts[:2] == ["media", "export"] and len(parts) == 5:
            return self.export_media(parts[2], parts[3], parts[4])
        if parts == ["media", "brand", "logo"]:
            brand = episodes.load_brand(self.app.store)
            if not brand.get("logo"):
                raise FileNotFoundError("No logo set.")
            return self.send_file(Path(brand["logo"]))
        if len(parts) == 3 and parts[:2] == ["media", "brand"] and parts[2] in {"intro", "outro"}:
            brand = episodes.load_brand(self.app.store)
            path = brand.get(parts[2])
            if not path:
                raise FileNotFoundError("No file set.")
            return self.send_file(Path(path))
        self.send_error_json("Not found.", 404)

    def config_payload(self) -> dict:
        token = self.app.auth.cookie_from(self)
        if token is None:
            token = self._session()
        return {
            "csrf": self.app.auth.csrf_value(token),
            "access_code_required": bool(self.app.auth.code),
            "sections": SECTIONS,
            "pipeline": PIPELINE,
            "brand": {"podcast_name": episodes.load_brand(self.app.store)["podcast_name"]},
            "ai": ai_client.public_status(self.app.store.root),
            "limits": {
                "chunk_size": uploads.CHUNK_SIZE,
                "max_upload_gb": uploads.MAX_UPLOAD_BYTES / 1024**3,
                "min_free_gb": storage.MIN_FREE_BYTES / 1024**3,
            },
            "storage": self.app.store.disk(),
            "platforms": [{"id": key, "label": label}
                          for key, label in editorial.PLATFORM_LABELS.items()],
            "episode_options": {
                "min_len": 30, "max_len": 180, "preferred_len": 60,
                "target": "30-90s preferred, up to 180s only to keep a complete thought",
                "master": "1080x1920",
            },
        }

    def episode_detail(self, episode_id: str) -> dict:
        data = episodes.manifest(self.app.store, episode_id)
        data["source_url"] = f"/media/source/{episode_id}"
        status = self.app.queue.status()
        data["job"] = next((job for job in status["jobs"]
                            if job.get("episode_id") == episode_id), None)
        data["brand"] = {"podcast_name": episodes.load_brand(self.app.store)["podcast_name"]}
        return data

    def episode_log(self, episode_id: str) -> dict:
        path = episodes.episode_path(self.app.store, episode_id) / "job.log"
        if not path.is_file():
            return {"log": ""}
        with path.open("rb") as handle:
            handle.seek(max(0, path.stat().st_size - 20000))
            return {"log": handle.read().decode("utf-8", errors="replace")}

    def clips(self, decision: str) -> list[dict]:
        if decision not in {"approved", "rejected", "pending", "all"}:
            raise ValueError("Unknown clip filter.")
        result = []
        for episode in self.app.episodes_list():
            for clip in episodes.merged_clips(self.app.store, episode["id"]):
                if decision != "all" and clip.get("decision") != decision:
                    continue
                item = dict(clip)
                item["episode_title"] = episode.get("title")
                item["source_url"] = f"/media/source/{episode['id']}"
                item["export_urls"] = (clip.get("export") or {}).get("urls")
                result.append(item)
        return result

    def storage_payload(self) -> dict:
        usage = self.app.store.usage()
        episodes_by_source: dict[str, list] = {}
        for episode in self.app.episodes_list():
            episodes_by_source.setdefault(episode.get("source_id"), []).append({
                "id": episode["id"], "title": episode.get("title"),
                "status": episode.get("status"),
            })
        for source in usage["sources"]:
            source["episodes"] = episodes_by_source.get(source["id"], [])
        usage["ai"] = ai_client.public_status(self.app.store.root)
        usage["uploads"] = self.app.uploads.list()[:20]
        return usage

    def source_file(self, episode_id: str) -> Path:
        path = episodes.episode_path(self.app.store, episode_id)
        transcript = storage.read_json(path / "transcript.json") \
            if (path / "transcript.json").is_file() else {}
        source = transcript.get("video")
        if not source:
            episode = episodes.read_episode(self.app.store, episode_id)
            source = episode.get("source_path")
        path = Path(source or "").resolve()
        root = self.app.store.sources.resolve()
        if root not in path.parents and path != root:
            raise ValueError("Source is outside managed storage.")
        if path.suffix.lower() not in storage.VIDEO_EXTS | {".mp3", ".wav", ".m4a"}:
            raise ValueError("Unsupported source media type.")
        return path

    def project_media(self, episode_id: str, directory: str, filename: str) -> None:
        if directory not in {"clips", "previews"}:
            raise ValueError("Unknown media directory.")
        base = (episodes.episode_path(self.app.store, episode_id) / directory).resolve()
        file = (base / filename).resolve()
        if file.parent != base or file.suffix.lower() not in {".mp4", ".srt", ".png"}:
            raise ValueError("Invalid media path.")
        return self.send_file(file)

    def export_media(self, episode_id: str, clip_id: str, filename: str) -> None:
        if not re.fullmatch(r"[a-f0-9]{12}", episode_id) or not re.fullmatch(r"c\d{3}", clip_id):
            raise ValueError("Invalid export path.")
        base = (self.app.store.exports / episode_id / clip_id).resolve()
        file = (base / filename).resolve()
        if file.parent != base or file.suffix.lower() not in {
                ".mp4", ".srt", ".png", ".json"}:
            raise ValueError("Invalid export path.")
        return self.send_file(file, download=filename.endswith(".json"))

    # -- POST routes --------------------------------------------------------
    def route_post(self, parts: list[str]):
        if parts == ["api", "uploads"]:
            body = self._json_body()
            state = self.app.uploads.create(body.get("filename"),
                                            body.get("size"),
                                            body.get("metadata") or {})
            return self.send_json(state, 201)
        if len(parts) == 4 and parts[:2] == ["api", "uploads"]:
            upload_id, action = parts[2], parts[3]
            if action == "chunk":
                offset = self._query().get("offset")
                if offset is None:
                    offset = self.headers.get("X-Upload-Offset")
                try:
                    offset = int(offset)
                except (TypeError, ValueError):
                    raise ValueError("Provide a numeric upload offset.")
                data = self._raw_body()
                state = self.app.uploads.append(upload_id, offset, data)
                return self.send_json(state)
            if action == "complete":
                state = self.app.uploads.complete(upload_id)
                source = self.app.store.read_source(state["source_id"])
                episode, duplicate = episodes.create_episode(
                    self.app.store, source,
                    title=(state.get("metadata") or {}).get("title"))
                if not duplicate:
                    job = self.app.queue.enqueue(episode["id"], "prepare")
                else:
                    job = None
                episode = episodes.read_episode(self.app.store, episode["id"])
                return self.send_json({"upload": state, "episode": episode,
                                       "duplicate": duplicate,
                                       "job": job}, 201)
            if action == "abort":
                self.app.uploads.abort(upload_id)
                return self.send_json({"ok": True})
        if len(parts) == 4 and parts[:2] == ["api", "episodes"] and parts[3] == "prepare":
            self._require_episode(parts[2])
            job = self.app.queue.enqueue(parts[2], "prepare", force=True)
            self._stage(parts[2], "queued", "Queued for processing")
            return self.send_json({"job": job}, 201)
        if len(parts) == 4 and parts[:2] == ["api", "episodes"] and parts[3] == "preview":
            self._require_episode(parts[2])
            job = self.app.queue.enqueue(parts[2], "preview", force=True)
            self._stage(parts[2], "rendering_previews", "Queued to render previews")
            return self.send_json({"job": job}, 201)
        if len(parts) == 4 and parts[:2] == ["api", "episodes"] and parts[3] == "render":
            body = self._json_body()
            clip_ids = body.get("clip_ids")
            if clip_ids is not None and (not isinstance(clip_ids, list)
                                         or any(not isinstance(c, str) for c in clip_ids)):
                raise ValueError("clip_ids must be a list of clip IDs.")
            clips = episodes.merged_clips(self.app.store, parts[2])
            approved = [clip["clip_id"] for clip in clips
                        if clip.get("decision") == episodes.APPROVED]
            selected = clip_ids or approved
            if not selected:
                raise ValueError("Approve at least one clip first.")
            job = self.app.queue.enqueue(parts[2], "render",
                                         meta={"clip_ids": selected}, force=True)
            self._stage(parts[2], "rendering", "Queued approved clips for export")
            return self.send_json({"job": job}, 201)
        if len(parts) == 4 and parts[:2] == ["api", "episodes"] and parts[3] == "approve":
            body = self._json_body()
            clip_ids = body.get("clip_ids")
            if not isinstance(clip_ids, list) or not clip_ids:
                raise ValueError("Select at least one clip to approve.")
            for clip_id in clip_ids:
                episodes.update_clip(self.app.store, parts[2], clip_id,
                                     {"decision": episodes.APPROVED})
            job = self.app.queue.enqueue(parts[2], "render",
                                         meta={"clip_ids": clip_ids}, force=True)
            self._stage(parts[2], "rendering", "Queued approved clips for export")
            return self.send_json({"ok": True, "job": job})
        if len(parts) == 4 and parts[:2] == ["api", "episodes"] and parts[3] == "reject":
            body = self._json_body()
            clip_ids = body.get("clip_ids")
            if not isinstance(clip_ids, list) or not clip_ids:
                raise ValueError("Select at least one clip to reject.")
            for clip_id in clip_ids:
                episodes.update_clip(self.app.store, parts[2], clip_id,
                                     {"decision": episodes.REJECTED})
            return self.send_json({"ok": True})
        if len(parts) == 4 and parts[:2] == ["api", "episodes"] and parts[3] == "layout":
            body = self._json_body()
            layout = episodes.set_layout(self.app.store, parts[2],
                                         body.get("layout"), body.get("fit"),
                                         body.get("centers"))
            return self.send_json({"layout": layout})
        if len(parts) == 4 and parts[:2] == ["api", "episodes"] and parts[3] == "delete":
            body = self._json_body()
            if body.get("confirm") is not True:
                raise ValueError("Send confirm=true to delete this episode's derived files.")
            return self.send_json(self.delete_episode(parts[2]))
        if len(parts) == 5 and parts[:2] == ["api", "episodes"] and parts[3] == "clips":
            body = self._json_body()
            item = episodes.update_clip(self.app.store, parts[2], parts[4], body)
            return self.send_json({"clip": item})
        if len(parts) == 3 and parts[:2] == ["api", "episodes"]:
            body = self._json_body()
            episode = episodes.update_episode(self.app.store, parts[2], body)
            return self.send_json({"episode": episode})
        if parts == ["api", "brand"]:
            body = self._json_body()
            brand = episodes.save_brand(self.app.store, body)
            brand["logo_url"] = ("/media/brand/logo?t=" + str(int(brand.get("updated") or 0))
                                 if brand.get("logo") else None)
            return self.send_json(brand)
        if len(parts) == 3 and parts[:2] == ["api", "brand"] and parts[2] in {
                "logo", "intro", "outro"}:
            return self.upload_brand_file(parts[2])
        if parts == ["api", "storage", "cleanup"]:
            result = self.app.store.cleanup()
            return self.send_json(result)
        if len(parts) == 5 and parts[:2] == ["api", "storage"] and parts[2] == "sources":
            source_id = parts[3]
            action = parts[4]
            if action == "verify":
                source = self.app.store.read_source(source_id)
                digest = storage.sha256_file(Path(source["path"]))
                return self.send_json({"match": digest == source["sha256"],
                                       "sha256": digest})
            if action == "delete":
                body = self._json_body()
                if body.get("confirm") is not True:
                    raise ValueError("Send confirm=true to delete the original source.")
                return self.send_json(self.delete_source(source_id,
                                                         bool(body.get("delete_projects"))))
        if len(parts) == 4 and parts[:2] == ["api", "queue"] and parts[3] == "cancel":
            return self.send_json({"cancelled": self.app.queue.cancel(parts[2])})
        self.send_error_json("Not found.", 404)

    def _require_episode(self, episode_id: str) -> None:
        episodes.read_episode(self.app.store, episode_id)

    def _stage(self, episode_id: str, status: str, stage: str) -> None:
        episode = episodes.read_episode(self.app.store, episode_id)
        episode.update(status=status, stage=stage, error=None, updated=now())
        episodes.save_episode(self.app.store, episode_id, episode)

    def delete_episode(self, episode_id: str) -> dict:
        path = episodes.episode_path(self.app.store, episode_id)
        jobs = self.app.queue.status()["jobs"]
        if any(job.get("episode_id") == episode_id
               and job.get("status") in {"pending", "running"} for job in jobs):
            raise ValueError("Wait for this episode's job to finish before deleting it.")
        import shutil
        shutil.rmtree(path, ignore_errors=True)
        shutil.rmtree(self.app.store.exports / episode_id, ignore_errors=True)
        return {"deleted": episode_id}

    def delete_source(self, source_id: str, delete_projects: bool) -> dict:
        episodes_for_source = [episode for episode in self.app.episodes_list()
                               if episode.get("source_id") == source_id]
        jobs = self.app.queue.status()["jobs"]
        busy = {job.get("episode_id") for job in jobs
                if job.get("status") in {"pending", "running"}}
        if any(episode["id"] in busy for episode in episodes_for_source):
            raise ValueError("Wait for processing to finish before deleting this source.")
        result = self.app.store.delete_source(source_id, delete_projects=False)
        if delete_projects:
            import shutil
            for episode in episodes_for_source:
                shutil.rmtree(episodes.episode_path(self.app.store, episode["id"]),
                              ignore_errors=True)
                shutil.rmtree(self.app.store.exports / episode["id"], ignore_errors=True)
                result.setdefault("projects", []).append(episode["id"])
        return result

    def upload_brand_file(self, kind: str) -> None:
        filename = self.headers.get("X-Filename", "")
        size = int(self.headers.get("Content-Length", "0"))
        limit = 6 * 1024**2 if kind == "logo" else 300 * 1024**2
        if not 0 < size <= limit:
            raise ValueError("File is empty or larger than the limit.")
        allowed = ({".png", ".jpg", ".jpeg", ".webp"} if kind == "logo"
                   else {".mp4", ".mov", ".m4v", ".webm", ".mp3", ".wav", ".m4a"})
        suffix = Path(filename or "").suffix.lower()
        if suffix not in allowed:
            raise ValueError("Unsupported file type.")
        data = self.rfile.read(size)
        if len(data) != size:
            raise ValueError("Upload was interrupted; try again.")
        folder = self.app.store.brand
        target = folder / f"{kind}-{int(time.time())}{suffix}"
        target.write_bytes(data)
        brand = episodes.load_brand(self.app.store)
        old = brand.get(kind)
        brand[kind] = str(target)
        if kind == "logo":
            try:
                formatted = episodes.validate_brand(brand)
                episodes.save_brand(self.app.store, formatted)
            except ValueError:
                target.unlink(missing_ok=True)
                raise
        else:
            updated = dict(brand)
            updated["updated"] = now()
            storage.atomic_json(folder / "brand.json", updated)
        if old and Path(old) != target:
            try:
                Path(old).unlink()
            except OSError:
                pass
        return self.send_json({"ok": True, "path": f"/media/brand/{kind}",
                               "applied_to_exports": kind == "logo"})

    def login_post(self):
        if not self._origin_ok():
            return self.send_html(login_page("Cross-origin request rejected."), 403)
        key = self.app.auth.client_key(self)
        if self.app.auth.locked_out(key):
            return self.send_html(login_page("Too many attempts. Wait ten minutes."), 429)
        Content_Type = self.headers.get("Content-Type", "")
        if "application/json" in Content_Type:
            body = self._json_body(64 * 1024)
            supplied = str(body.get("code") or "")
        else:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 64 * 1024:
                return self.send_html(login_page("Provide the access code."), 400)
            fields = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
            supplied = (fields.get("code") or [""])[0]
        if not self.app.auth.check_code(supplied):
            self.app.auth.note_failure(key)
            time.sleep(0.6)
            return self.send_html(login_page("That access code was not accepted."), 401)
        cookie, _token = self.app.auth.new_session()
        self._set_cookie = cookie
        self.redirect("/")


def login_page(error: str = "") -> str:
    message = (f'<p class="login-error">{html.escape(error)}</p>' if error else "")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>Sign in · From the Field Clipper</title>
<link rel="icon" href="data:,">
<link rel="stylesheet" href="/static/style.css">
</head><body class="login-body">
<main class="login-card">
  <span class="eyebrow">FROM THE FIELD · AGRONOMY PODCAST</span>
  <h1>Clipper access</h1>
  <p>Enter the application access code. Coder authentication is required before this page loads.</p>
  {message}
  <form method="post" action="/login">
    <label for="code">Access code</label>
    <input id="code" name="code" type="password" autocomplete="current-password"
      autofocus required>
    <button type="submit">Unlock clipper</button>
  </form>
  <p class="fine">Codes and sessions stay on this workspace. Failed attempts are rate limited.</p>
</main></body></html>"""


def make_server(data_root=None, host="0.0.0.0", port=8765):
    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    server.app = App(data_root, host, port)
    return server


def main():
    parser = argparse.ArgumentParser(description="From the Field podcast clipper")
    parser.add_argument("--data-dir", default=None)
    parser.add_argument("--host", default=os.environ.get("PODCAST_CLIPPER_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("PODCAST_CLIPPER_PORT", "8765")))
    args = parser.parse_args()
    server = make_server(args.data_dir, args.host, args.port)
    app = server.app
    print(f"From the Field Clipper: http://{args.host}:{server.server_port}", flush=True)
    print(f"Data: {app.store.root}", flush=True)
    print(f"Access code required: {bool(app.auth.code)}", flush=True)
    print(f"Auto-resumed {app.recovered} episode(s); recovered "
          f"{app.queue.recovered} queued job(s).", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
