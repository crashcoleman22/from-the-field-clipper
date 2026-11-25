"""Episode lifecycle: prepare, review, approve, export, and brand storage."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import ai_client
import branding
import editorial
import projects
import score_clips
import storage

HERE = Path(__file__).resolve().parent
PODCAST_NAME = "From the Field"
APPROVED = "approved"
REJECTED = "rejected"
PENDING = "pending"
DECISIONS = {APPROVED, REJECTED, PENDING}
PLATFORM_KEYS = set(editorial.PLATFORMS)

DEFAULT_CAPTION = {
    "font": "DejaVu Sans",
    "font_size": 74,
    "color": "#FFFFFF",
    "highlight": "#FFE14D",
    "outline_color": "#000000",
    "outline": 5,
    "shadow": 2,
    "position": "bottom",
    "words_per_cue": 3,
    "uppercase": False,
    "background": "none",
    "background_color": "#000000",
    "background_opacity": 70,
    "animation": "none",
    "highlight_mode": "word",
    "emoji": False,
    "emphasis": True,
}

DEFAULT_BRAND = {
    "podcast_name": PODCAST_NAME,
    "logo": None,
    "logo_width": 180,
    "primary_color": "#2D5016",
    "secondary_color": "#F4F1E8",
    "accent_color": "#FFE14D",
    "caption": DEFAULT_CAPTION,
    "speaker_names": [],
    "intro": None,
    "outro": None,
    "vocabulary": [],
    "platform_safe": {"top": 220, "bottom": 420, "left": 80, "right": 80},
    "updated": None,
}


# -- brand ------------------------------------------------------------------
def _split_color(value, field):
    if not isinstance(value, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        raise ValueError(f"{field} must use #RRGGBB.")
    return value.upper()


def load_brand(store: storage.Store) -> dict:
    path = store.brand / "brand.json"
    if not path.is_file():
        return {**DEFAULT_BRAND, "caption": dict(DEFAULT_CAPTION),
                "platform_safe": dict(DEFAULT_BRAND["platform_safe"])}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        data = {}
    brand = {**DEFAULT_BRAND, **(data or {})}
    brand["caption"] = {**DEFAULT_CAPTION, **(data.get("caption") or {})}
    brand["platform_safe"] = {**DEFAULT_BRAND["platform_safe"],
                              **(data.get("platform_safe") or {})}
    return brand


def validate_brand(body: dict) -> dict:
    if not isinstance(body, dict):
        raise ValueError("Brand settings must be an object.")
    allowed = set(DEFAULT_BRAND)
    unknown = set(body) - allowed
    if unknown:
        raise ValueError("Unknown brand fields: " + ", ".join(sorted(unknown)))
    name = body.get("podcast_name", PODCAST_NAME)
    if not isinstance(name, str) or not name.strip() or len(name) > 120:
        raise ValueError("Podcast name must be 1-120 characters.")
    brand = {**DEFAULT_BRAND, **body, "podcast_name": name.strip()}
    brand["primary_color"] = _split_color(brand["primary_color"], "Primary color")
    brand["secondary_color"] = _split_color(brand["secondary_color"], "Secondary color")
    brand["accent_color"] = _split_color(brand["accent_color"], "Accent color")
    caption = body.get("caption") or {}
    if not isinstance(caption, dict):
        raise ValueError("Caption settings must be an object.")
    clean_caption = branding.validate_caption(
        {key: value for key, value in caption.items()
         if key in branding.CAPTION_FIELDS and value is not None})
    brand["caption"] = {**DEFAULT_CAPTION, **clean_caption}

    speakers = body.get("speaker_names", [])
    if not isinstance(speakers, list) or len(speakers) > 8:
        raise ValueError("Provide up to eight speaker names.")
    brand["speaker_names"] = [str(item).strip()[:80] for item in speakers
                              if str(item).strip()]

    vocab = body.get("vocabulary", [])
    if not isinstance(vocab, list) or len(vocab) > 500:
        raise ValueError("Vocabulary must be a list of up to 500 pairs.")
    clean_vocab = []
    for item in vocab:
        if (not isinstance(item, (list, tuple)) or len(item) != 2
                or not isinstance(item[0], str) or not isinstance(item[1], str)):
            raise ValueError("Each vocabulary entry needs a spoken form and a correction.")
        wrong, right = item[0].strip(), item[1].strip()
        if wrong and right and len(wrong) <= 120 and len(right) <= 120:
            clean_vocab.append([wrong, right])
    brand["vocabulary"] = clean_vocab

    safe = body.get("platform_safe", {})
    if not isinstance(safe, dict):
        raise ValueError("Platform-safe margins must be an object.")
    clean_safe = {}
    for key in ("top", "bottom", "left", "right"):
        value = safe.get(key, DEFAULT_BRAND["platform_safe"][key])
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 900:
            raise ValueError("Platform-safe margins must be integers from 0 to 900.")
        clean_safe[key] = value
    brand["platform_safe"] = clean_safe
    brand["updated"] = time.time()
    return brand


def save_brand(store: storage.Store, body: dict) -> dict:
    current = load_brand(store)
    for key in ("logo", "intro", "outro"):
        body.setdefault(key, current.get(key))
    brand = validate_brand(body)
    storage.atomic_json(store.brand / "brand.json", brand)
    write_caption_brand(store, brand)
    write_vocabulary(store, brand)
    return brand


def write_caption_brand(store: storage.Store, brand: dict | None = None) -> Path:
    brand = brand or load_brand(store)
    caption = brand["caption"]
    fields = {
        "font": caption.get("font"),
        "font_size": caption.get("font_size"),
        "margin": None,
        "logo": brand.get("logo"),
        "logo_width": brand.get("logo_width", 180),
        "color": caption.get("color"),
        "highlight": caption.get("highlight"),
        "emphasis_color": caption.get("highlight"),
        "outline_color": caption.get("outline_color"),
        "uppercase": caption.get("uppercase"),
        "words_per_cue": caption.get("words_per_cue"),
        "position": caption.get("position"),
        "outline": caption.get("outline"),
        "shadow": caption.get("shadow"),
        "background": caption.get("background"),
        "background_color": caption.get("background_color"),
        "background_opacity": caption.get("background_opacity"),
        "animation": caption.get("animation"),
        "highlight_mode": caption.get("highlight_mode"),
        "emoji": caption.get("emoji"),
        "emphasis": caption.get("emphasis"),
    }
    clean = branding.validate_brand(
        {key: value for key, value in fields.items() if value is not None})
    path = store.brand / "chopify-brand.json"
    storage.atomic_json(path, clean)
    return path


def write_vocabulary(store: storage.Store, brand: dict | None = None) -> Path | None:
    brand = brand or load_brand(store)
    mapping = {}
    for wrong, right in brand.get("vocabulary", []):
        mapping[wrong] = right
    path = store.brand / "vocabulary.json"
    if not mapping:
        try:
            path.unlink()
        except OSError:
            pass
        return None
    storage.atomic_json(path, mapping)
    return path


def brand_file(store: storage.Store) -> Path | None:
    path = store.brand / "chopify-brand.json"
    if path.is_file():
        return path
    try:
        return write_caption_brand(store)
    except (ValueError, OSError):
        return None


# -- layout -----------------------------------------------------------------
def detect_layout(source: str | Path, duration: float | None = None,
                  model_dir: str | Path | None = None) -> dict:
    """Count faces in sampled frames to suggest a vertical composition."""
    result = {
        "speakers": None,
        "suggested_layout": "single",
        "suggested_fit": "cover",
        "confidence": 0.0,
        "centers": [0.25, 0.75],
        "method": "faces",
        "sampled_frames": 0,
    }
    path = Path(source)
    if not path.is_file():
        result["method"] = "unavailable"
        return result
    try:
        import cv2
    except Exception:
        result["method"] = "opencv-unavailable"
        return result
    kind = None
    detector = None
    if model_dir:
        try:
            import render_clips
            kind, detector = render_clips._make_detector(Path(model_dir) / "layout")
        except Exception:
            kind, detector = None, None
    if detector is None:
        cascade_path = Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml"
        if not cascade_path.is_file():
            result["method"] = "no-face-model"
            return result
        kind, detector = "haar", cv2.CascadeClassifier(str(cascade_path))
    result["method"] = kind
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        cap.release()
        result["method"] = "unreadable"
        return result
    try:
        frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0) or 30.0
        if duration:
            frames = max(frames, int(float(duration) * fps))
        if frames <= 0:
            result["method"] = "no-frames"
            return result
        positions = [0.08, 0.22, 0.36, 0.5, 0.64, 0.78, 0.92]
        counts, centers, sampled = [], [], 0
        for fraction in positions:
            index = int(frames * fraction)
            cap.set(cv2.CAP_PROP_POS_FRAMES, index)
            ok, frame = cap.read()
            if not ok or frame is None:
                continue
            sampled += 1
            height, width = frame.shape[:2]
            if width > 720:
                scale = 720.0 / width
                frame = cv2.resize(frame, (720, max(1, int(height * scale))))
                height, width = frame.shape[:2]
            if kind == "yunet":
                detector.setInputSize((width, height))
                _retval, faces = detector.detect(frame)
                boxes = []
                if faces is not None:
                    for face in faces:
                        x, y, w, h = [float(value) for value in face[:4]]
                        boxes.append((x, y, w, h))
            else:
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                found = detector.detectMultiScale(gray, scaleFactor=1.1,
                                                  minNeighbors=5,
                                                  minSize=(36, 36))
                boxes = [tuple(float(value) for value in box) for box in found]
            counts.append(len(boxes))
            centers.extend([(x + w / 2.0) / width for x, y, w, h in boxes])
        result["sampled_frames"] = sampled
        if not sampled:
            result["method"] = "no-frames"
            return result
        counts.sort()
        median = counts[len(counts) // 2] if counts else 0
        peak = max(counts) if counts else 0
        speakers = median or peak
        result["speakers"] = int(speakers) if speakers else 0
        result["confidence"] = round(min(1.0, sampled / 7.0), 2)
        if speakers >= 3:
            result.update(suggested_layout="single", suggested_fit="contain")
        elif speakers == 2:
            xs = sorted(centers)
            if len(xs) >= 2:
                mid = len(xs) // 2
                left = sum(xs[:mid]) / max(1, mid)
                right = sum(xs[mid:]) / max(1, len(xs) - mid)
                result["centers"] = [round(max(0.0, min(1.0, left)), 3),
                                     round(max(0.0, min(1.0, right)), 3)]
            result.update(suggested_layout="split", suggested_fit="cover")
        else:
            result.update(suggested_layout="single", suggested_fit="cover")
            if centers:
                result["centers"] = [round(max(0.0, min(1.0, sum(centers) / len(centers))), 3),
                                     round(max(0.0, min(1.0, sum(centers) / len(centers))), 3)]
    finally:
        cap.release()
    return result


# -- episode helpers --------------------------------------------------------
def _read(path: Path, default=None):
    try:
        return storage.read_json(path)
    except (OSError, ValueError):
        return default


def episode_path(store: storage.Store, episode_id: str) -> Path:
    return store.project_dir(episode_id)


def read_episode(store: storage.Store, episode_id: str) -> dict:
    path = episode_path(store, episode_id)
    data = _read(path / "episode.json")
    if not isinstance(data, dict):
        raise ValueError("Episode not found.")
    state = _read(path / "project.json", {}) or {}
    data["pipeline"] = state
    return data


def save_episode(store: storage.Store, episode_id: str, episode: dict) -> None:
    storage.atomic_json(episode_path(store, episode_id) / "episode.json", episode)


def create_episode(store: storage.Store, source: dict, title: str | None = None,
                   notes: str = "") -> tuple[dict, bool]:
    """Create a project for an uploaded source. Reuse an existing episode."""
    for path in store.projects.glob("*/episode.json"):
        existing = _read(path)
        if isinstance(existing, dict) and existing.get("source_id") == source["id"]:
            return existing, True
    brand = load_brand(store)
    options = _base_options(store, brand)
    state = projects.create_project(store.projects, source["path"], options)
    project_dir = projects.project_path(store.projects, state["id"])
    state = projects.read_project(project_dir)
    state["source_sha256"] = source["id"]
    state["source_original_name"] = source.get("original_name")
    state["source_size"] = source.get("size")
    projects.save_project(project_dir, state)
    episode = {
        "id": state["id"],
        "source_id": source["id"],
        "source_path": source["path"],
        "original_name": source.get("original_name"),
        "title": (title or Path(source.get("original_name") or "Episode").stem)[:200],
        "notes": notes or "",
        "created": time.time(),
        "updated": time.time(),
        "status": "queued",
        "stage": "Waiting for the queue",
        "error": None,
        "layout": None,
        "layout_analysis": None,
        "layout_manual": False,
        "counts": {"clips": 0, "approved": 0, "rejected": 0, "exported": 0},
    }
    save_episode(store, state["id"], episode)
    return episode, False


def _base_options(store: storage.Store, brand: dict) -> dict:
    options = dict(projects.DEFAULT_OPTIONS)
    options.update({
        "aspect": "9:16",
        "fit": "cover",
        "style": "podcast",
        "layout": "single",
        "video_type": "podcast",
        "format": "podcast",
        "min_len": 30,
        "max_len": 180,
        "preferred_len": 60,
        "min_score": 3.5,
        "max_clips": 12,
        "asr": "parakeet",
        "device": "cpu",
        "tighten": True,
        "loudnorm": True,
        "brand": str(brand_file(store)) if brand_file(store) else None,
    })
    return projects.validate_options(options)


def update_counts(store: storage.Store, episode_id: str) -> dict:
    path = episode_path(store, episode_id)
    episode = read_episode(store, episode_id)
    review = _read(path / "review.json", {}) or {}
    clips = review.get("clips") or {}
    approved = sum(1 for item in clips.values() if item.get("decision") == APPROVED)
    rejected = sum(1 for item in clips.values() if item.get("decision") == REJECTED)
    exported = sum(1 for item in clips.values()
                   if item.get("decision") == APPROVED and item.get("export"))
    episode["counts"] = {
        "clips": len(clips),
        "approved": approved,
        "rejected": rejected,
        "exported": exported,
    }
    save_episode(store, episode_id, episode)
    return episode


def assign_clip_ids(store: storage.Store, episode_id: str) -> list[dict]:
    path = episode_path(store, episode_id)
    segments = _read(path / "segments.json", []) or []
    for index, segment in enumerate(segments, 1):
        segment["clip_id"] = f"c{index:03d}"
    storage.atomic_json(path / "segments.json", segments)
    return segments


def editorial_clips(store: storage.Store, episode_id: str) -> list[dict]:
    data = _read(episode_path(store, episode_id) / "editorial.json", {}) or {}
    clips = data.get("clips") or []
    return clips if isinstance(clips, list) else []


def export_urls(episode_id: str, clip_id: str) -> dict:
    base = f"/media/export/{episode_id}/{clip_id}"
    return {
        "master": base + "/master.mp4",
        "captions": base + "/captions.srt",
        "poster": base + "/poster.png",
        "metadata": base + "/metadata.json",
    }


def merged_clips(store: storage.Store, episode_id: str) -> list[dict]:
    path = episode_path(store, episode_id)
    review = _read(path / "review.json", {}) or {}
    review_clips = review.get("clips") or {}
    result = []
    for clip in editorial_clips(store, episode_id):
        clip_id = clip.get("clip_id")
        saved = review_clips.get(clip_id) or {}
        item = dict(clip)
        item["decision"] = saved.get("decision", PENDING)
        item["title"] = saved.get("title") or clip.get("title") or ""
        item["description"] = saved.get("description") or clip.get("description") or ""
        item["platforms"] = saved.get("platforms") or clip.get("platforms") or []
        item["notes"] = saved.get("notes") or ""
        item["edited"] = bool(saved.get("edited"))
        item["export"] = saved.get("export")
        item["render_error"] = saved.get("render_error")
        export = item.get("export") or {}
        if export.get("master") and Path(export["master"]).is_file():
            export = {**export, "urls": export_urls(episode_id, clip_id)}
        metadata_path = export.get("metadata")
        if metadata_path and Path(metadata_path).is_file():
            try:
                meta = storage.read_json(Path(metadata_path))
                export = {**export,
                          "platforms": meta.get("platforms"),
                          "hashtags": meta.get("hashtags")}
            except (OSError, ValueError):
                pass
        item["export"] = export or None
        result.append(item)
    return result


def initialize_review(store: storage.Store, episode_id: str) -> dict:
    path = episode_path(store, episode_id)
    old = _read(path / "review.json", {}) or {}
    old_clips = old.get("clips") or {}
    clips = {}
    for clip in editorial_clips(store, episode_id):
        clip_id = clip["clip_id"]
        previous = old_clips.get(clip_id) or {}
        same_range = (previous.get("start") == clip.get("start")
                      and previous.get("end") == clip.get("end"))
        clips[clip_id] = {
            "start": clip.get("start"),
            "end": clip.get("end"),
            "decision": previous.get("decision", PENDING) if same_range else PENDING,
            "title": previous.get("title") if same_range else clip.get("title"),
            "description": previous.get("description") if same_range else clip.get("description"),
            "platforms": previous.get("platforms") if same_range else clip.get("platforms"),
            "notes": previous.get("notes", "") if same_range else "",
            "edited": previous.get("edited", False) if same_range else False,
            "export": previous.get("export") if same_range else None,
            "updated": previous.get("updated"),
        }
    review = {"clips": clips, "updated": time.time()}
    storage.atomic_json(path / "review.json", review)
    return review


def verify_source(store: storage.Store, episode: dict) -> None:
    source_id = episode.get("source_id")
    if not source_id:
        return
    try:
        record = store.read_source(source_id)
    except (OSError, ValueError):
        return
    path = Path(record["path"])
    if not path.is_file():
        raise ValueError("The original source video is missing from storage.")
    if path.stat().st_size != int(record.get("size") or 0):
        raise ValueError("The original source video changed size; refusing to process it.")


def _pipeline_env(store: storage.Store) -> dict:
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env["CHOPIFY_MODELS_DIR"] = str(store.models)
    env.setdefault("CHOPIFY_ASR_PARTS", "2")
    env.setdefault("CHOPIFY_RENDER_WORKERS", "2")
    env.setdefault("CHOPIFY_SCAN_WORKERS", "2")
    env.setdefault("CHOPIFY_SECTION_WORKERS", "2")
    vocab = store.brand / "vocabulary.json"
    if vocab.is_file():
        env["PODCAST_CLIPPER_VOCAB"] = str(vocab)
    return env


def _log_tail(path: Path, limit: int = 4000) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(max(0, path.stat().st_size - limit))
            return handle.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


# -- prepare ----------------------------------------------------------------
def prepare_episode(store: storage.Store, queue, episode_id: str) -> dict:
    episode = read_episode(store, episode_id)
    path = episode_path(store, episode_id)
    episode.update(status="processing", stage="Preparing transcript and clip candidates",
                   error=None, updated=time.time())
    save_episode(store, episode_id, episode)
    verify_source(store, episode)
    store.check_free(0)
    state = projects.read_project(path)
    brand = load_brand(store)
    options = dict(state.get("options") or {})
    baseline = _base_options(store, brand)
    options.update({
        "aspect": "9:16",
        "video_type": "podcast",
        "format": "podcast",
        "asr": "parakeet",
        "device": "cpu",
        "brand": str(brand_file(store)) if brand_file(store) else None,
        "min_len": 30,
        "max_len": 180,
        "preferred_len": 60,
        "min_score": 3.5,
        "max_clips": 12,
    })
    if not episode.get("layout_manual"):
        options["layout"] = "single"
        options["fit"] = "cover"
    options = projects.validate_options({**baseline, **options})
    state["options"] = options
    state["source_sha256"] = episode.get("source_id")
    state["status"] = "queued"
    projects.save_project(path, state)

    log = path / "job.log"

    def run_pipeline():
        cmd = [sys.executable, str(HERE / "chopify.py"), "--project", str(path),
               "--action", "prepare"]
        code = queue.run_process(cmd, log, env=_pipeline_env(store), cwd=HERE)
        return code, projects.read_project(path)

    code, state = run_pipeline()
    if code != 0 or state.get("status") == "failed":
        message = state.get("error") or f"Pipeline exited with code {code}."
        tail = _log_tail(log)
        episode.update(status="failed", stage="Processing failed",
                       error=(message + ("\n\n" + tail[-1200:] if tail else ""))[:4000],
                       updated=time.time())
        save_episode(store, episode_id, episode)
        raise RuntimeError(message)

    segments = assign_clip_ids(store, episode_id)
    if not segments:
        # Short episodes may not contain a 30-second complete thought. Retry once
        # with a smaller window so the review step still has usable material.
        options["min_len"] = 15
        options["max_len"] = 90
        options["preferred_len"] = 30
        options["min_score"] = 0
        state = projects.read_project(path)
        state["options"] = projects.validate_options(options)
        state["status"] = "queued"
        projects.save_project(path, state)
        code, state = run_pipeline()
        segments = assign_clip_ids(store, episode_id)
        if code != 0 or state.get("status") == "failed":
            message = state.get("error") or f"Pipeline exited with code {code}."
            episode.update(status="failed", stage="Processing failed",
                           error=message[:4000], updated=time.time())
            save_episode(store, episode_id, episode)
            raise RuntimeError(message)
    if not segments:
        episode.update(status="failed", stage="No clips found",
                       error=("No clips passed the selector. The episode may be too short, "
                              "or the minimum score may be too high."),
                       updated=time.time())
        save_episode(store, episode_id, episode)
        raise RuntimeError("No clips were selected.")

    layout = detect_layout(state.get("video") or episode.get("source_path"),
                           (_read(path / "transcript.json", {}) or {}).get("duration"),
                           model_dir=store.models)
    episode["layout_analysis"] = layout
    if not episode.get("layout_manual"):
        options["layout"] = layout["suggested_layout"]
        options["fit"] = layout["suggested_fit"]
        options["centers"] = layout.get("centers") or options.get("centers")
        state = projects.read_project(path)
        state["options"] = projects.validate_options(options)
        projects.save_project(path, state)
    episode["layout"] = {"layout": options["layout"], "fit": options["fit"],
                         "centers": options.get("centers")}

    transcript = _read(path / "transcript.json", {}) or {}
    analysis = editorial.analyze(transcript, segments, brand["podcast_name"],
                                 data_root=store.root)
    storage.atomic_json(path / "editorial.json", analysis)
    initialize_review(store, episode_id)
    episode.update(status="clips_awaiting_review", stage="Clips ready for review",
                   error=None, updated=time.time())
    save_episode(store, episode_id, episode)
    update_counts(store, episode_id)
    return {"clips": len(segments), "editorial": analysis.get("generated_by"),
            "ai_model": analysis.get("model"), "layout": layout}


# -- preview ----------------------------------------------------------------
def preview_episode(store: storage.Store, queue, episode_id: str) -> dict:
    episode = read_episode(store, episode_id)
    path = episode_path(store, episode_id)
    episode.update(status="rendering_previews", stage="Rendering previews",
                   error=None, updated=time.time())
    save_episode(store, episode_id, episode)
    log = path / "job.log"
    cmd = [sys.executable, str(HERE / "chopify.py"), "--project", str(path),
           "--action", "render", "--preview", "--out", str(path / "previews")]
    code = queue.run_process(cmd, log, env=_pipeline_env(store), cwd=HERE)
    state = projects.read_project(path)
    if code != 0 and state.get("status") == "failed":
        episode.update(status="clips_awaiting_review", stage="Preview failed",
                       error=(state.get("error") or "Preview render failed.")[:2000],
                       updated=time.time())
        save_episode(store, episode_id, episode)
        raise RuntimeError(state.get("error") or "Preview render failed.")
    episode.update(status="clips_awaiting_review", stage="Previews ready",
                   error=None, updated=time.time())
    save_episode(store, episode_id, episode)
    return {"previews": len(list((path / "previews").glob("*.mp4")))}


# -- render/export ----------------------------------------------------------
def _clip_metadata(episode: dict, clip: dict, brand: dict) -> dict:
    platforms = [p for p in clip.get("platforms") or []
                 if p in PLATFORM_KEYS] or ["youtube_shorts"]
    hashtags = clip.get("hashtags") or ["#agronomy", "#farming"]
    title = clip.get("title") or ""
    description = clip.get("description") or ""
    tags = " ".join(hashtags)
    platform_meta = {
        "youtube_shorts": {
            "title": (title + " #Shorts")[:100],
            "description": ((description + "\n\n" + tags).strip())[:4900],
            "visibility": "public",
        },
        "instagram_reels": {
            "caption": ((title + "\n\n" + description + "\n\n" + tags).strip())[:2200],
            "share_to_feed": True,
        },
        "facebook": {
            "message": ((title + "\n\n" + description + "\n\n" + tags).strip())[:63000],
        },
    }
    return {
        "clip_id": clip["clip_id"],
        "episode_id": episode["id"],
        "episode_title": episode.get("title"),
        "podcast": brand["podcast_name"],
        "title": title,
        "description": description,
        "hashtags": hashtags,
        "platforms_recommended": platforms,
        "platforms": {key: platform_meta[key] for key in platforms},
        "scores": clip.get("scores"),
        "overall": clip.get("overall"),
        "selection_explanation": clip.get("selection_explanation"),
        "seasonal_relevance": clip.get("seasonal_relevance"),
        "expires": clip.get("expires"),
        "start": clip.get("start"),
        "end": clip.get("end"),
        "duration": clip.get("duration"),
        "transcript": clip.get("transcript"),
        "needs_review": clip.get("needs_review", False),
        "warnings": clip.get("warnings", []),
        "source_sha256": episode.get("source_id"),
        "master": "master.mp4",
        "captions": "captions.srt" if (clip.get("export") or {}).get("captions") else None,
        "generated": time.time(),
    }


def _find_master(path: Path, index: int) -> Path | None:
    candidates = sorted(path.glob(f"{index:03d}-*.mp4"))
    return candidates[0] if candidates else None


def render_approved(store: storage.Store, queue, episode_id: str,
                    clip_ids: list[str] | None = None) -> dict:
    episode = read_episode(store, episode_id)
    path = episode_path(store, episode_id)
    clips = merged_clips(store, episode_id)
    by_id = {clip["clip_id"]: clip for clip in clips}
    selected = clip_ids or [clip["clip_id"] for clip in clips
                            if clip["decision"] == APPROVED]
    selected = [clip_id for clip_id in selected
                if by_id.get(clip_id, {}).get("decision") == APPROVED]
    if not selected:
        raise ValueError("Approve at least one clip before exporting.")
    indices = sorted({int(by_id[clip_id]["index"]) for clip_id in selected})
    episode.update(status="rendering", stage="Rendering approved vertical clips",
                   error=None, updated=time.time())
    save_episode(store, episode_id, episode)
    log = path / "job.log"
    cmd = [sys.executable, str(HERE / "chopify.py"), "--project", str(path),
           "--action", "render", "--out", str(path / "clips"),
           "--clips", ",".join(str(index) for index in indices)]
    code = queue.run_process(cmd, log, env=_pipeline_env(store), cwd=HERE)
    state = projects.read_project(path)
    if code != 0 and not any(path.glob("clips/*.mp4")):
        message = state.get("error") or f"Render exited with code {code}."
        episode.update(status="clips_awaiting_review", stage="Export failed",
                       error=(message + "\n\n" + _log_tail(log))[:4000],
                       updated=time.time())
        save_episode(store, episode_id, episode)
        raise RuntimeError(message)

    brand = load_brand(store)
    review = _read(path / "review.json", {}) or {}
    review_clips = review.get("clips") or {}
    exported = []
    missing = []
    for clip_id in selected:
        clip = by_id[clip_id]
        master = _find_master(path / "clips", int(clip["index"]))
        if master is None:
            missing.append(clip_id)
            review_clips.setdefault(clip_id, {})["render_error"] = "Render produced no file."
            continue
        folder = store.exports / episode_id / clip_id
        if folder.exists():
            shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir(parents=True, exist_ok=True)
        master_copy = folder / "master.mp4"
        shutil.copy2(master, master_copy)
        captions = master.with_suffix(".srt")
        if captions.is_file():
            shutil.copy2(captions, folder / "captions.srt")
        poster = master.with_suffix(".png")
        if poster.is_file():
            shutil.copy2(poster, folder / "poster.png")
        clip["export"] = {
            "master": str(master_copy),
            "captions": str(folder / "captions.srt") if captions.is_file() else None,
            "poster": str(folder / "poster.png") if poster.is_file() else None,
            "metadata": str(folder / "metadata.json"),
            "exported": time.time(),
        }
        storage.atomic_json(folder / "metadata.json", _clip_metadata(episode, clip, brand))
        review_clips.setdefault(clip_id, {}).update({
            "export": clip["export"], "render_error": None,
            "decision": APPROVED, "updated": time.time()})
        exported.append(clip_id)
    review["clips"] = review_clips
    review["updated"] = time.time()
    storage.atomic_json(path / "review.json", review)
    if missing and not exported:
        episode.update(status="clips_awaiting_review", stage="Export failed",
                       error="The renderer produced no files for the approved clips.",
                       updated=time.time())
        save_episode(store, episode_id, episode)
        raise RuntimeError("The renderer produced no files for the approved clips.")
    episode.update(status="approved" if exported else "clips_awaiting_review",
                   stage="Approved clips are ready to download" if exported else "Render incomplete",
                   error=(f"{len(missing)} clip(s) did not render." if missing else None),
                   updated=time.time())
    save_episode(store, episode_id, episode)
    update_counts(store, episode_id)
    return {"exported": exported, "missing": missing}


def update_clip(store: storage.Store, episode_id: str, clip_id: str,
                body: dict) -> dict:
    path = episode_path(store, episode_id)
    review = _read(path / "review.json", {}) or {}
    clips = review.get("clips") or {}
    if clip_id not in clips:
        raise ValueError("Unknown clip.")
    item = clips[clip_id]
    if "decision" in body:
        decision = body["decision"]
        if decision not in DECISIONS:
            raise ValueError("Decision must be pending, approved, or rejected.")
        item["decision"] = decision
    if "title" in body:
        title = body["title"]
        if not isinstance(title, str) or not title.strip() or len(title) > 120:
            raise ValueError("Title must be 1-120 characters.")
        item["title"] = title.strip()
        item["edited"] = True
    if "description" in body:
        description = body["description"]
        if not isinstance(description, str) or len(description) > 5000:
            raise ValueError("Description must be 5,000 characters or fewer.")
        item["description"] = description.strip()
        item["edited"] = True
    if "notes" in body:
        notes = body["notes"]
        if not isinstance(notes, str) or len(notes) > 1000:
            raise ValueError("Notes must be 1,000 characters or fewer.")
        item["notes"] = notes.strip()
    if "platforms" in body:
        platforms = body["platforms"]
        if not isinstance(platforms, list) or any(p not in PLATFORM_KEYS for p in platforms):
            raise ValueError("Choose YouTube Shorts, Instagram Reels, or Facebook.")
        item["platforms"] = [p for p in editorial.PLATFORMS if p in platforms]
    item["updated"] = time.time()
    clips[clip_id] = item
    review["clips"] = clips
    review["updated"] = time.time()
    storage.atomic_json(path / "review.json", review)
    update_counts(store, episode_id)
    return item


def set_layout(store: storage.Store, episode_id: str, layout: str, fit: str,
               centers: list[float] | None = None) -> dict:
    if layout not in {"single", "split", "stacked"}:
        raise ValueError("Layout must be single, split, or stacked.")
    if fit not in {"cover", "contain"}:
        raise ValueError("Fit must be cover or contain.")
    if layout in {"split", "stacked"} and fit == "contain" and layout == "stacked":
        raise ValueError("Stacked layout always uses cover.")
    path = episode_path(store, episode_id)
    state = projects.read_project(path)
    options = dict(state.get("options") or {})
    options.update(layout=layout, fit=fit)
    if centers is not None:
        if (not isinstance(centers, list) or len(centers) != 2
                or any(isinstance(v, bool) or not isinstance(v, (int, float))
                       or not 0 <= v <= 1 for v in centers)):
            raise ValueError("Speaker centers must be two numbers from 0 to 1.")
        options["centers"] = centers
    state["options"] = projects.validate_options(options)
    projects.save_project(path, state)
    episode = read_episode(store, episode_id)
    episode["layout"] = {"layout": layout, "fit": fit,
                         "centers": state["options"].get("centers")}
    episode["layout_manual"] = True
    episode["updated"] = time.time()
    save_episode(store, episode_id, episode)
    return episode["layout"]


def update_episode(store: storage.Store, episode_id: str, body: dict) -> dict:
    episode = read_episode(store, episode_id)
    if "title" in body:
        title = body["title"]
        if not isinstance(title, str) or not title.strip() or len(title) > 200:
            raise ValueError("Episode title must be 1-200 characters.")
        episode["title"] = title.strip()
    if "notes" in body:
        notes = body["notes"]
        if not isinstance(notes, str) or len(notes) > 5000:
            raise ValueError("Notes must be 5,000 characters or fewer.")
        episode["notes"] = notes.strip()
    episode["updated"] = time.time()
    save_episode(store, episode_id, episode)
    return episode


def manifest(store: storage.Store, episode_id: str) -> dict:
    episode = read_episode(store, episode_id)
    path = episode_path(store, episode_id)
    data = {
        "episode": episode,
        "clips": merged_clips(store, episode_id),
        "editorial": _read(path / "editorial.json", {}) or {},
        "review": _read(path / "review.json", {}) or {},
        "files": [],
        "previews": [],
        "state": episode.get("pipeline") or {},
    }
    for directory in ("clips", "previews"):
        for file in sorted((path / directory).glob("*")):
            if file.suffix in {".mp4", ".srt", ".png"}:
                data["files" if directory == "clips" else "previews"].append({
                    "name": file.name,
                    "url": f"/media/project/{episode_id}/{directory}/{file.name}",
                })
    for clip_id in (data["review"].get("clips") or {}):
        item = data["review"]["clips"][clip_id]
        export = item.get("export") or {}
        if export.get("master") and Path(export["master"]).is_file():
            item["export"]["urls"] = export_urls(episode_id, clip_id)
    return data
