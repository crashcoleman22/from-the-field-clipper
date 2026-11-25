"""Focused tests for the Coder-hosted From the Field app layer."""
import hashlib
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
import http.cookiejar
from pathlib import Path

import pytest

import ai_client
import editorial
import episodes
import podcast_auth
import storage
import uploads


@pytest.fixture()
def store(tmp_path):
    return storage.Store(tmp_path / "data")


def make_video(path: Path, seconds=1):
    subprocess.run([
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", f"color=c=0x2d5016:s=320x180:r=15:d={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
        "-shortest", "-c:v", "libx264", "-preset", "ultrafast",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-f", "mp4", str(path),
    ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return path


def test_store_consumes_upload_and_preserves_source(tmp_path, store):
    part = tmp_path / "upload.part"
    make_video(part, 1)
    digest = hashlib.sha256(part.read_bytes()).hexdigest()
    record = store.consume_upload(part, "My Episode.mp4", {"title": "Test"})
    assert record["sha256"] == digest
    assert Path(record["path"]).is_file()
    assert Path(record["path"]).name == "My-Episode.mp4"
    assert storage.sha256_file(Path(record["path"])) == digest
    # A duplicate upload is deduplicated without changing the stored source.
    duplicate = tmp_path / "again.part"
    duplicate.write_bytes(Path(record["path"]).read_bytes())
    second = store.consume_upload(duplicate, "My Episode.mp4")
    assert second["id"] == record["id"]
    assert second.get("deduplicated") is True
    assert storage.sha256_file(Path(record["path"])) == digest


def test_upload_manager_resumes_by_offset(tmp_path, store):
    manager = uploads.UploadManager(store)
    source = make_video(tmp_path / "source.mp4", 1)
    state = manager.create("source.mp4", source.stat().st_size)
    with source.open("rb") as handle:
        first = handle.read(100)
        second = handle.read()
    manager.append(state["id"], 0, first)
    with pytest.raises(uploads.UploadError):
        manager.append(state["id"], 0, b"duplicate")
    manager.append(state["id"], 100, second)
    done = manager.complete(state["id"])
    assert done["status"] == "complete"
    assert store.read_source(done["source_id"])["size"] == source.stat().st_size


def test_upload_rejects_non_video(tmp_path, store):
    manager = uploads.UploadManager(store)
    with pytest.raises(uploads.UploadError):
        manager.create("notes.txt", 10)


def test_auth_hosts_sessions_and_csrf(tmp_path):
    auth = podcast_auth.Auth(tmp_path)
    assert auth.host_allowed("127.0.0.1:8765")
    assert auth.host_allowed("8765--main--x.dollarsperacrecropsolutions.app")
    assert not auth.host_allowed("evil.example.com")
    cookie, token = auth.new_session()
    assert auth._decode_session(token)
    assert auth.csrf_value(token) == auth.csrf_value(token)
    assert auth.csrf_value(token) != auth.csrf_value(token + "x")
    auth.set_code("field-code")
    assert auth.code == "field-code"
    assert auth.check_code("field-code")
    assert not auth.check_code("wrong")
    auth.set_code("")
    assert not auth.code


def test_deterministic_editorial_is_grounded(monkeypatch):
    monkeypatch.setenv("PODCAST_CLIPPER_AI_DISABLED", "1")
    transcript = {
        "words": [
            {"word": "Our", "start": 0, "end": 0.2},
            {"word": "trial", "start": 0.2, "end": 0.4},
            {"word": "showed", "start": 0.4, "end": 0.6},
            {"word": "eight", "start": 0.6, "end": 0.8},
            {"word": "bushels", "start": 0.8, "end": 1.0},
            {"word": "with", "start": 1.0, "end": 1.2},
            {"word": "split", "start": 1.2, "end": 1.4},
            {"word": "nitrogen.", "start": 1.4, "end": 1.6},
        ],
        "segments": [],
    }
    segments = [{"start": 0, "end": 1.6, "overall": 7.5, "clip_id": "c001"}]
    result = editorial.analyze(transcript, segments, "From the Field", data_root=None)
    clip = result["clips"][0]
    assert result["generated_by"] == "deterministic"
    assert clip["transcript"] == "Our trial showed eight bushels with split nitrogen."
    assert 0 <= clip["overall"] <= 100
    assert set(clip["scores"]) == {
        "engagement", "agronomic_relevance", "educational_value", "timeliness",
        "hook_strength"}
    assert clip["title"]
    assert "eight" in clip["title"].lower() or "trial" in clip["title"].lower()
    assert clip["platforms"]
    assert clip["ai"] is False


def test_numeric_guard_flags_model_invented_numbers():
    base = {
        "transcript": "We saw a yield bump.",
        "clip_id": "c001",
        "scores": {"engagement": 50, "agronomic_relevance": 50,
                   "educational_value": 50, "timeliness": 50, "hook_strength": 50},
        "title": "A yield bump", "description": "No numbers here.", "platforms": [],
    }
    warnings = []
    result = editorial._apply_ai(base, {
        "title": "Growers saw 42 bushels more",
        "description": "A 42 bushel advantage.",
    }, warnings)
    assert result["needs_review"] is True
    assert result["warnings"]
    assert warnings


def test_brand_roundtrip(store):
    brand = episodes.save_brand(store, {
        "podcast_name": "From the Field",
        "primary_color": "#2D5016",
        "secondary_color": "#F4F1E8",
        "accent_color": "#FFE14D",
        "caption": {"font": "DejaVu Sans", "font_size": 74,
                    "color": "#FFFFFF", "highlight": "#FFE14D"},
        "speaker_names": ["Cole", "Megan"],
        "vocabulary": [["side dress", "sidedress"]],
    })
    loaded = episodes.load_brand(store)
    assert loaded["podcast_name"] == "From the Field"
    assert loaded["speaker_names"] == ["Cole", "Megan"]
    assert (store.brand / "chopify-brand.json").is_file()
    assert json.loads((store.brand / "vocabulary.json").read_text()) == {
        "side dress": "sidedress"}
    # The validated brand is accepted by the renderer's brand loader.
    import branding
    assert branding.load_brand(store.brand / "chopify-brand.json")["font_size"] == 74


def test_brand_rejects_bad_colors(store):
    with pytest.raises(ValueError):
        episodes.save_brand(store, {"primary_color": "red"})


def test_api_config_and_auth_gate(tmp_path):
    import app as app_module
    server = app_module.make_server(tmp_path / "data", "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_port
    base = f"http://127.0.0.1:{port}"
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    try:
        with opener.open(base + "/api/config", timeout=10) as response:
            config = json.loads(response.read())
        assert config["csrf"]
        assert config["access_code_required"] is False
        assert config["limits"]["chunk_size"] > 0
        assert any(section["id"] == "review" for section in config["sections"])
        # POST without the CSRF header is rejected.
        request = urllib.request.Request(base + "/api/brand", data=b"{}",
                                         headers={"Content-Type": "application/json"},
                                         method="POST")
        with pytest.raises(urllib.error.HTTPError) as exc:
            opener.open(request, timeout=10)
        assert exc.value.code == 403
        # A bad Host header is rejected before authentication.
        request = urllib.request.Request(base + "/api/config",
                                         headers={"Host": "evil.example.com"})
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(request, timeout=10)
        assert exc.value.code == 421
    finally:
        server.shutdown()
        server.app.shutdown()
        server.server_close()


def test_access_code_login_flow(tmp_path, monkeypatch):
    import app as app_module
    monkeypatch.setenv("PODCAST_CLIPPER_ACCESS_CODE", "field-test-code")
    server = app_module.make_server(tmp_path / "data", "127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_port
    base = f"http://127.0.0.1:{port}"
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            opener.open(base + "/api/config", timeout=10)
        assert exc.value.code == 401
        # Static CSS is available to style the login page before a session exists.
        with opener.open(base + "/static/style.css", timeout=10) as response:
            assert response.status == 200
            assert b"login-body" in response.read()
        # A wrong code is rejected and does not establish a session.
        request = urllib.request.Request(
            base + "/login", data=b"code=wrong",
            headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
        with pytest.raises(urllib.error.HTTPError) as exc:
            opener.open(request, timeout=10)
        assert exc.value.code == 401
        request = urllib.request.Request(
            base + "/login", data=b"code=field-test-code",
            headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST")
        with opener.open(request, timeout=10) as response:
            assert response.status == 200
        with opener.open(base + "/api/config", timeout=10) as response:
            config = json.loads(response.read())
        assert config["access_code_required"] is True
    finally:
        server.shutdown()
        server.app.shutdown()
        server.server_close()


def test_ai_status_never_exposes_key(monkeypatch):
    monkeypatch.setenv("AI_DEFAULT_BASE_URL", "http://example.invalid:4000")
    monkeypatch.setenv("AI_DEFAULT_API_KEY", "super-secret")
    status = ai_client.public_status(None)
    assert "secret" not in json.dumps(status)
    assert status["enabled"] is True


def test_queue_recovers_and_resumes_running_job(store):
    import job_queue
    storage.atomic_json(store.root / "queue.json", {"jobs": [{
        "id": "abc123abc123", "episode_id": "aabbccddeeff", "kind": "prepare",
        "status": "running", "created": time.time(), "started": time.time(),
        "finished": None, "attempts": 1, "error": None, "meta": {}, "log": None,
    }]})
    finished = threading.Event()
    seen = {}

    def runner(job, log_path):
        seen.update(job)
        finished.set()
        return {"ok": True}

    queue = job_queue.JobQueue(store, runner)
    try:
        assert finished.wait(5), "recovered job did not run"
        assert queue.recovered == 1
        assert seen["episode_id"] == "aabbccddeeff"
        status = queue.status()
        assert status["jobs"][0]["status"] == "done"
    finally:
        queue.shutdown()
