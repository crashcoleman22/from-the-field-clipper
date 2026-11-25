"""Editorial analysis and platform metadata for agronomy podcast clips.

The deterministic scorer is always the baseline. When the workspace LiteLLM
gateway is available it refines the scores, explanation, title, description,
and platform recommendation. Timestamps and transcript text always come from
the word-level transcript, never from the model.
"""
from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path

import ai_client

PLATFORMS = ("youtube_shorts", "instagram_reels", "facebook")
PLATFORM_LABELS = {
    "youtube_shorts": "YouTube Shorts",
    "instagram_reels": "Instagram Reels",
    "facebook": "Facebook",
}

AG_TERMS = {
    "agronomy", "agronomic", "soil", "soils", "corn", "soybean", "soybeans",
    "wheat", "cotton", "rice", "alfalfa", "hay", "forage", "cover crop",
    "cover crops", "crop", "crops", "yield", "yields", "bushel", "bushels",
    "nitrogen", "phosphorus", "potassium", "sulfur", "boron", "zinc",
    "ph", "lime", "fertilizer", "fertility", "nutrient", "nutrients",
    "seed", "seeding", "planting", "planter", "population", "germination",
    "tillage", "no-till", "notill", "residue", "compaction", "drainage",
    "drain", "irrigation", "water", "moisture", "drought", "flood",
    "weed", "weeds", "herbicide", "pesticide", "fungicide", "insecticide",
    "disease", "fungus", "nematode", "pest", "pests", "insect", "aphid",
    "rootworm", "cutworm", "rust", "blight", "mold", "vomitoxin",
    "hybrid", "variety", "cultivar", "trait", "gene", "gmo",
    "scouting", "scout", "tissue test", "soil test", "grid sample",
    "application", "sidedress", "side-dress", "topdress", "spray",
    "harvest", "combine", "storage", "grain", "silage", "livestock",
    "cattle", "pasture", "grazing", "rotation", "crop rotation",
    "precision ag", "variable rate", "zone", "field", "acre", "acres",
    "grower", "growers", "farmer", "farmers", "producer", "producers",
    "extension", "research", "trial", "trials", "plot", "plots",
    "cover", "tile", "terrace", "erosion", "organic", "carbon",
}
RESEARCH_TERMS = {
    "research", "trial", "trials", "study", "studies", "data", "dataset",
    "plot", "plots", "experiment", "measured", "measurement", "result",
    "results", "found", "showed", "shows", "compared", "comparison",
    "yield", "bushels", "tested", "field trial", "university", "extension",
}
ECONOMICS_TERMS = {
    "cost", "costs", "dollar", "dollars", "price", "prices", "roi",
    "return", "returns", "profit", "profitable", "margin", "margins",
    "input", "inputs", "budget", "economics", "economic", "expensive",
    "cheap", "savings", "value", "break-even", "break even", "per acre",
}
ACTION_TERMS = {
    "apply", "applying", "application", "scout", "scouting", "plant",
    "planting", "spray", "spraying", "sample", "sampling", "test",
    "testing", "rotate", "rotation", "manage", "management", "control",
    "adjust", "check", "monitor", "avoid", "choose", "select", "increase",
    "reduce", "split", "sidedress", "side-dress", "topdress", "drain",
    "irrigate", "harvest", "storage", "calibrate", "treat", "seed",
}
MISTAKE_TERMS = {
    "mistake", "mistakes", "wrong", "avoid", "problem", "problems",
    "failed", "failure", "error", "instead", "careful", "caution",
    "risk", "risky", "lose", "loss", "losses", "waste", "wasting",
}
SURPRISE_TERMS = {
    "surprising", "surprised", "unexpected", "unexpectedly", "actually",
    "contrary", "myth", "truth", "discovered", "we found", "turns out",
    "not what", "counterintuitive", "challenging", "controversial",
    "debate", "disagree", "wrong about", "changed my mind",
}
EDUCATION_TERMS = {
    "because", "means", "meaning", "reason", "reasons", "how", "why",
    "understand", "explain", "explains", "important", "key", "depends",
    "difference", "differences", "lesson", "learn", "learned", "example",
    "step", "steps", "first", "second", "third", "so that", "which means",
}
SEASON_TERMS = {
    "spring", "summer", "fall", "autumn", "winter", "planting season",
    "growing season", "harvest", "harvest season", "this week", "this month",
    "right now", "timely", "deadline", "window", "early season",
    "late season", "midseason", "post-emerge", "pre-emerge",
}
MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
    "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}
STOP_TITLE = re.compile(r"\s+")


def _clean_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def clip_transcript(transcript: dict, start: float, end: float) -> str:
    words = transcript.get("words") or []
    picked = []
    for word in words:
        try:
            w_start = float(word.get("start", 0))
            w_end = float(word.get("end", w_start))
        except (TypeError, ValueError):
            continue
        if w_end <= start or w_start >= end:
            continue
        picked.append(str(word.get("word", "")))
    if picked:
        text = " ".join(part.lstrip() for part in picked)
        return _clean_text(text)
    pieces = []
    for segment in transcript.get("segments") or []:
        try:
            if float(segment.get("end", 0)) <= start:
                continue
            if float(segment.get("start", 0)) >= end:
                continue
        except (TypeError, ValueError):
            continue
        pieces.append(str(segment.get("text", "")))
    return _clean_text(" ".join(pieces))


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9][a-z0-9'\-]*", text.lower())


def _count_phrases(text: str, phrases: set[str]) -> int:
    lower = " " + re.sub(r"[^a-z0-9 ]+", " ", text.lower()) + " "
    total = 0
    for phrase in phrases:
        needle = " " + phrase + " "
        total += lower.count(needle)
    return total


def _scale(value: float, low: float = 0.0, high: float = 1.0) -> int:
    if high <= low:
        return 50
    ratio = max(0.0, min(1.0, (value - low) / (high - low)))
    return int(round(8 + 92 * ratio))


def _hook_strength(text: str) -> int:
    first = re.split(r"(?<=[.!?])\s+", text.strip(), maxsplit=1)[0]
    tokens = _tokens(first)
    score = 38
    if first.endswith("?"):
        score += 22
    if re.search(r"\d", first):
        score += 14
    if re.search(r"[%$]", first):
        score += 8
    if any(term in first.lower() for term in
           ("biggest", "most", "best", "worst", "never", "always", "wrong",
            "truth", "secret", "mistake", "surprising", "actually", "why",
            "how", "what happens")):
        score += 12
    if 6 <= len(tokens) <= 24:
        score += 8
    if text.count("?") >= 1:
        score += 4
    return max(5, min(98, score))


def _seasonal(text: str, now: time.struct_time | None = None) -> tuple[str, str | None, int]:
    now = now or time.localtime()
    lower = text.lower()
    found = None
    for name, month in MONTHS.items():
        if re.search(r"\b" + re.escape(name) + r"\b", lower):
            found = month
            break
    season_hits = _count_phrases(text, SEASON_TERMS)
    timeliness = 38 + min(32, season_hits * 10)
    if found is not None:
        year = now.tm_year
        if found < now.tm_mon:
            year += 1
        try:
            expires = time.strftime("%Y-%m-%d", time.strptime(f"{year}-{found:02d}-28", "%Y-%m-%d"))
        except ValueError:
            expires = None
        timeliness += 24
        return (f"References {found:02d}/{year}; seasonal guidance can expire.",
                expires, min(98, timeliness))
    if season_hits:
        return ("Contains seasonal timing language; review the expiration date.",
                None, min(96, timeliness))
    return ("No explicit seasonal deadline found; treat as an evergreen topic.",
            None, timeliness)


def _numeric_warnings(text: str, clip_text: str) -> list[str]:
    """Flag numbers the model added that are not present in the transcript."""
    allowed = set(re.findall(r"\d+(?:\.\d+)?", clip_text.lower()))
    warned = []
    for number in re.findall(r"\d+(?:\.\d+)?", text.lower()):
        if number not in allowed:
            warned.append(number)
    return warned[:5]


def _fallback_clip(clip: dict, index: int, podcast: str) -> dict:
    text = clip["transcript"]
    tokens = _tokens(text)
    word_count = max(1, len(tokens))
    duration = float(clip["end"]) - float(clip["start"])
    base = float(clip.get("overall") or 5.0) * 10

    ag_hits = _count_phrases(text, AG_TERMS)
    research_hits = _count_phrases(text, RESEARCH_TERMS)
    econ_hits = _count_phrases(text, ECONOMICS_TERMS)
    action_hits = _count_phrases(text, ACTION_TERMS)
    mistake_hits = _count_phrases(text, MISTAKE_TERMS)
    surprise_hits = _count_phrases(text, SURPRISE_TERMS)
    education_hits = _count_phrases(text, EDUCATION_TERMS)

    density = ag_hits / max(8.0, word_count / 10.0)
    agronomic = _scale(density, 0.2, 2.8)
    if ag_hits >= 6:
        agronomic = max(agronomic, 72)
    educational = _scale((education_hits + action_hits * 0.8 + research_hits) /
                         max(8.0, word_count / 10.0), 0.25, 3.0)
    if research_hits:
        educational = max(educational, 68)
    hook = _hook_strength(text)
    seasonal_text, expires, timeliness = _seasonal(text)

    engagement = 0.55 * base + 0.45 * (
        hook * 0.45 + min(100, 55 + surprise_hits * 14 + econ_hits * 5
                          + mistake_hits * 8 + (10 if re.search(r"\d", text) else 0)) * 0.55)
    engagement += min(8, action_hits)
    engagement = max(5, min(99, round(engagement)))

    reasons = []
    if ag_hits:
        reasons.append("directly discusses agronomic management")
    if research_hits:
        reasons.append("cites trial, research, or measured results")
    if econ_hits:
        reasons.append("connects the decision to economics or ROI")
    if action_hits:
        reasons.append("gives growers an actionable next step")
    if mistake_hits:
        reasons.append("names a common mistake or risk")
    if surprise_hits:
        reasons.append("contains a surprising or counterintuitive claim")
    if hook >= 65:
        reasons.append("opens with a strong, concrete hook")
    if duration < 20:
        reasons.append("is short enough for a fast social cut")
    elif duration > 180:
        reasons.append("runs long and needs trimming before export")
    if not reasons:
        reasons.append("is a self-contained moment from the episode")

    plain = re.sub(r"\s+", " ", text).strip()
    title_seed = re.split(r"(?<=[.!?])\s+", plain, maxsplit=1)[0]
    title_seed = title_seed.strip(" -–—:;,")
    if len(title_seed) > 72:
        title_seed = title_seed[:72].rsplit(" ", 1)[0].rstrip(" ,;:-")
    if not title_seed:
        title_seed = f"Field note from {podcast}"
    title = title_seed[0].upper() + title_seed[1:] if title_seed else title_seed
    excerpt = plain[:260].rstrip()
    description = (f"{excerpt}{'…' if len(plain) > 260 else ''}\n\n"
                   f"From the {podcast} podcast.")

    platforms = ["youtube_shorts"]
    if duration <= 95 and hook >= 55:
        platforms.append("instagram_reels")
    if educational >= 65 or timeliness >= 65:
        platforms.append("facebook")

    score = round(0.30 * agronomic + 0.24 * educational + 0.18 * hook
                  + 0.16 * timeliness + 0.12 * engagement)
    return {
        "clip_id": clip.get("clip_id") or f"c{index:03d}",
        "index": index,
        "start": round(float(clip["start"]), 3),
        "end": round(float(clip["end"]), 3),
        "duration": round(duration, 2),
        "transcript": plain,
        "scores": {
            "engagement": int(engagement),
            "agronomic_relevance": int(agronomic),
            "educational_value": int(educational),
            "timeliness": int(timeliness),
            "hook_strength": int(hook),
        },
        "overall": int(max(0, min(100, score))),
        "selection_explanation": "This clip " + ", ".join(reasons) + ".",
        "title": title,
        "description": description,
        "platforms": platforms,
        "seasonal_relevance": seasonal_text,
        "expires": expires,
        "hashtags": _hashtags(text, podcast),
        "needs_review": False,
        "warnings": [],
        "generated_by": "deterministic",
        "ai": False,
    }


def _hashtags(text: str, podcast: str) -> list[str]:
    lower = text.lower()
    tags = ["#agronomy", "#farming"]
    for tag, words in (
        ("#corn", ("corn",)), ("#soybeans", ("soybean", "soybeans")),
        ("#soilhealth", ("soil health", "cover crop", "cover crops")),
        ("#nitrogen", ("nitrogen",)), ("#agriculture", ("grower", "farmer")),
        ("#agronomy", ("agronomy", "agronomic")),
    ):
        if any(word in lower for word in words) and tag not in tags:
            tags.append(tag)
    slug = re.sub(r"[^a-z0-9]+", "", podcast.lower())[:24]
    if slug:
        tags.append("#" + slug)
    return tags[:6]


SYSTEM_PROMPT = """You are the editorial producer for "From the Field", an
agronomy podcast for farmers and crop advisers. Score and package short clips.

Prioritize: actionable agronomy, research findings, surprising or
controversial results, ROI and economics, common grower mistakes, timely
seasonal advice, reasoned strong opinions, compelling Q&A, and standalone
educational moments. Do not reward mere animation, shouting, or emotion.

Never invent facts. Do not add quotes, research findings, yield advantages,
product claims, numbers, dates, or recommendations that are not in the
provided transcript. Titles may be tight and compelling but must not promise
anything the clip does not say. Return JSON only."""


def _ai_batch(clips: list[dict], podcast: str, data_root) -> tuple[dict, dict]:
    payload = {
        "podcast": podcast,
        "clips": [
            {
                "id": clip["clip_id"],
                "start": clip["start"],
                "end": clip["end"],
                "duration": clip["duration"],
                "transcript": clip["transcript"][:2200],
            }
            for clip in clips
        ],
        "required_output": {
            "clips": [
                {
                    "id": "same id as input",
                    "engagement": "0-100 integer",
                    "agronomic_relevance": "0-100 integer",
                    "educational_value": "0-100 integer",
                    "timeliness": "0-100 integer",
                    "hook_strength": "0-100 integer",
                    "selection_explanation": "1-2 sentences grounded in the transcript",
                    "title": "max 90 characters, no fabricated facts",
                    "description": "max 500 characters, grounded in the transcript",
                    "platforms": ["youtube_shorts", "instagram_reels", "facebook"],
                    "seasonal_relevance": "one sentence",
                    "expires": "YYYY-MM-DD or null",
                }
            ]
        },
    }
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    result = ai_client.chat(messages, data_root=data_root, max_tokens=2600)
    data = result["data"]
    if isinstance(data, list):
        rows = data
    elif isinstance(data, dict):
        rows = data.get("clips") or data.get("results") or []
    else:
        rows = []
    cleaned = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        clip_id = str(row.get("id") or row.get("clip_id") or "")
        if clip_id:
            cleaned[clip_id] = row
    return cleaned, result


def _apply_ai(base: dict, row: dict, warnings: list[str]) -> dict:
    out = dict(base)
    scores = dict(base["scores"])
    for key in ("engagement", "agronomic_relevance", "educational_value",
                "timeliness", "hook_strength"):
        try:
            value = int(round(float(row.get(key))))
            if math.isfinite(value) and 0 <= value <= 100:
                scores[key] = value
        except (TypeError, ValueError):
            pass
    out["scores"] = scores
    weights = {"agronomic_relevance": 0.30, "educational_value": 0.24,
               "hook_strength": 0.18, "timeliness": 0.16, "engagement": 0.12}
    out["overall"] = int(round(sum(scores[key] * weight
                                   for key, weight in weights.items())))

    explanation = _clean_text(row.get("selection_explanation"))
    if explanation:
        out["selection_explanation"] = explanation[:600]

    title = _clean_text(row.get("title"))
    if title:
        out["title"] = title[:120]
    description = _clean_text(row.get("description"))
    if description:
        out["description"] = description[:900]

    platforms = row.get("platforms")
    if isinstance(platforms, list):
        chosen = [p for p in PLATFORMS if p in platforms]
        if chosen:
            out["platforms"] = chosen

    seasonal = _clean_text(row.get("seasonal_relevance"))
    if seasonal:
        out["seasonal_relevance"] = seasonal[:300]
    expires = row.get("expires")
    if isinstance(expires, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", expires):
        out["expires"] = expires
    elif expires in (None, "", "null"):
        out["expires"] = None

    added = _numeric_warnings(out["title"] + " " + out["description"],
                              base["transcript"])
    if added:
        message = ("Check model-added numbers not found in the transcript: "
                   + ", ".join(added))
        out["warnings"] = [message]
        out["needs_review"] = True
        warnings.append(f"{base['clip_id']}: {message}")
    out["generated_by"] = "ai"
    out["ai"] = True
    return out


def analyze(transcript: dict, segments: list[dict], podcast: str = "From the Field",
            data_root=None) -> dict:
    clips = []
    for index, segment in enumerate(segments, 1):
        start = float(segment["start"])
        end = float(segment["end"])
        clip_id = str(segment.get("clip_id") or f"c{index:03d}")
        clips.append({
            "clip_id": clip_id,
            "index": index,
            "start": start,
            "end": end,
            "overall": segment.get("overall"),
            "transcript": clip_transcript(transcript, start, end),
            "hook": segment.get("hook"),
        })
    bases = [_fallback_clip(clip, index, podcast)
             for index, clip in enumerate(clips, 1)]
    status = ai_client.public_status(data_root)
    generated_by = "deterministic"
    model = None
    warnings: list[str] = []
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0}
    if status["enabled"] and bases:
        merged = {}
        model = status["model"]
        for start in range(0, len(bases), 6):
            batch = bases[start:start + 6]
            try:
                rows, result = _ai_batch(batch, podcast, data_root)
                merged.update(rows)
                model = result.get("model") or model
                tokens = result.get("usage") or {}
                usage["prompt_tokens"] += int(tokens.get("prompt_tokens") or 0)
                usage["completion_tokens"] += int(tokens.get("completion_tokens") or 0)
                try:
                    usage["cost"] += float(tokens.get("cost") or 0)
                except (TypeError, ValueError):
                    pass
            except (ai_client.AIError, KeyError, TypeError, ValueError) as exc:
                warnings.append(f"AI batch failed; deterministic scores kept: {exc}")
        if merged:
            generated_by = "ai+deterministic"
            bases = [_apply_ai(base, merged.get(base["clip_id"], {}), warnings)
                     if merged.get(base["clip_id"]) else base for base in bases]
        else:
            warnings.append("AI returned no usable clip scores; deterministic fallback used.")
    return {
        "version": 1,
        "generated": time.time(),
        "generated_by": generated_by,
        "model": model,
        "podcast": podcast,
        "usage": usage,
        "warnings": warnings,
        "clips": bases,
    }
