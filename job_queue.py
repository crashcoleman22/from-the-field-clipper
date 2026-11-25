"""Persistent serial job queue with automatic resume after a restart."""
from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import uuid
from pathlib import Path

import storage


class JobQueue:
    def __init__(self, store: storage.Store, runner, autostart: bool = True):
        self.store = store
        self.runner = runner            # callable(job, log_path) -> result dict
        self.path = store.root / "queue.json"
        self.lock = threading.RLock()
        self.wake = threading.Condition(self.lock)
        self.jobs = self._load()
        self.current = None
        self.closing = False
        self.process: subprocess.Popen | None = None
        self.thread = threading.Thread(target=self._loop, name="ftf-queue",
                                       daemon=True)
        self.recovered = self._recover()
        if autostart:
            self.start()

    def start(self) -> None:
        """Start the worker once the owner has finished wiring its callbacks."""
        if not self.thread.is_alive():
            self.thread.start()

    # -- persistence --------------------------------------------------------
    def _load(self) -> list[dict]:
        if not self.path.is_file():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return []
        jobs = data.get("jobs") if isinstance(data, dict) else data
        return jobs if isinstance(jobs, list) else []

    def _save(self) -> None:
        with self.lock:
            storage.atomic_json(self.path, {"jobs": self.jobs})

    def _recover(self) -> int:
        """Jobs that were running when the app stopped go back to pending."""
        recovered = 0
        for job in self.jobs:
            if job.get("status") == "running":
                job["status"] = "pending"
                job["recovered"] = True
                job["error"] = None
                job["started"] = None
                recovered += 1
        if recovered:
            self._save()
        return recovered

    # -- public API ---------------------------------------------------------
    def enqueue(self, episode_id: str, kind: str, meta: dict | None = None,
                force: bool = False) -> dict:
        if kind not in {"prepare", "render", "preview"}:
            raise ValueError("Unknown job kind.")
        with self.lock:
            for job in self.jobs:
                if (job.get("episode_id") == episode_id and job.get("kind") == kind
                        and job.get("status") in {"pending", "running"}):
                    if not force:
                        return job
            job = {
                "id": uuid.uuid4().hex[:12],
                "episode_id": episode_id,
                "kind": kind,
                "status": "pending",
                "created": time.time(),
                "started": None,
                "finished": None,
                "attempts": 0,
                "error": None,
                "meta": meta or {},
                "log": None,
            }
            self.jobs.append(job)
            self._save()
            self.wake.notify_all()
            return job

    def cancel(self, job_id: str) -> bool:
        with self.lock:
            for job in self.jobs:
                if job["id"] == job_id and job["status"] == "pending":
                    job["status"] = "cancelled"
                    job["finished"] = time.time()
                    self._save()
                    return True
        return False

    def retry(self, episode_id: str, kind: str) -> dict:
        with self.lock:
            for job in self.jobs:
                if (job.get("episode_id") == episode_id and job.get("kind") == kind
                        and job.get("status") in {"pending", "running"}):
                    return job
        return self.enqueue(episode_id, kind)

    def status(self) -> dict:
        with self.lock:
            jobs = [dict(job) for job in self.jobs]
            return {
                "jobs": jobs[-80:][::-1],
                "current": dict(self.current) if self.current else None,
                "pending": sum(1 for job in jobs if job["status"] == "pending"),
                "running": sum(1 for job in jobs if job["status"] == "running"),
                "recovered": self.recovered,
            }

    def shutdown(self) -> None:
        with self.lock:
            self.closing = True
            self.wake.notify_all()
            process = self.process
            current = self.current
            if current and current.get("status") == "running":
                current["status"] = "pending"
                current["recovered"] = True
                current["error"] = None
                current["started"] = None
                self._save()
        if process is not None and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass

    # -- worker -------------------------------------------------------------
    def _next_pending(self):
        with self.lock:
            for job in self.jobs:
                if job.get("status") == "pending":
                    return job
        return None

    def _loop(self) -> None:
        while True:
            with self.lock:
                if self.closing:
                    return
                job = self._next_pending()
                if job is None:
                    self.wake.wait(timeout=2.0)
                    continue
                job["status"] = "running"
                job["started"] = time.time()
                job["attempts"] = int(job.get("attempts") or 0) + 1
                job["log"] = str(self.store.projects / job["episode_id"] / "job.log")
                self.current = job
                self._save()
            try:
                result = self.runner(job, Path(job["log"])) or {}
                with self.lock:
                    job = next((item for item in self.jobs if item["id"] == job["id"]), job)
                    job["status"] = "done"
                    job["finished"] = time.time()
                    job["error"] = None
                    job["result"] = result
                    self._save()
            except (Exception, SystemExit) as exc:
                with self.lock:
                    job = next((item for item in self.jobs if item["id"] == job["id"]), job)
                    job["status"] = "failed"
                    job["finished"] = time.time()
                    job["error"] = str(exc)[:2000]
                    self._save()
            finally:
                with self.lock:
                    self.current = None
                    self.process = None
                    if len(self.jobs) > 400:
                        finished = [j for j in self.jobs
                                    if j["status"] in {"done", "failed", "cancelled"}]
                        drop = set(j["id"] for j in finished[:len(self.jobs) - 400])
                        self.jobs = [j for j in self.jobs if j["id"] not in drop]
                        self._save()

    def run_process(self, cmd: list[str], log_path: Path, env: dict | None = None,
                    cwd: str | Path | None = None) -> int:
        """Run a pipeline subprocess, capturing output and allowing shutdown."""
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("w", encoding="utf-8") as log:
            with self.lock:
                if self.closing:
                    raise RuntimeError("The app is shutting down.")
                child = subprocess.Popen(
                    cmd, cwd=str(cwd or self.store.root), env=env,
                    stdout=log, stderr=subprocess.STDOUT,
                    start_new_session=True)
                self.process = child
            try:
                code = child.wait()
            finally:
                with self.lock:
                    self.process = None
        return code
