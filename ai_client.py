"""Small OpenAI-compatible client for the workspace LiteLLM gateway.

The app never requires an LLM. If the gateway is missing, slow, or returns
malformed data, editorial.py falls back to deterministic scoring.
"""
from __future__ import annotations

import json
import os
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_MODEL = "sync/opencode-go/deepseek-v4.1-flash"
DEFAULT_TIMEOUT = 90


def _config_file(data_root: str | Path | None) -> dict:
    if not data_root:
        return {}
    path = Path(data_root) / "ai.json"
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def config(data_root: str | Path | None = None) -> dict:
    saved = _config_file(data_root)
    base = (os.environ.get("PODCAST_CLIPPER_AI_BASE_URL")
            or saved.get("base_url")
            or os.environ.get("AI_DEFAULT_BASE_URL")
            or os.environ.get("OPENAI_BASE_URL")
            or "").rstrip("/")
    key = (os.environ.get("PODCAST_CLIPPER_AI_API_KEY")
           or saved.get("api_key")
           or os.environ.get("AI_DEFAULT_API_KEY")
           or os.environ.get("OPENAI_API_KEY")
           or "")
    model = (os.environ.get("PODCAST_CLIPPER_AI_MODEL")
             or saved.get("model")
             or DEFAULT_MODEL)
    disabled = (os.environ.get("PODCAST_CLIPPER_AI_DISABLED") == "1"
                or saved.get("enabled") is False)
    try:
        timeout = int(os.environ.get("PODCAST_CLIPPER_AI_TIMEOUT", DEFAULT_TIMEOUT))
    except ValueError:
        timeout = DEFAULT_TIMEOUT
    return {
        "base_url": base,
        "api_key": key,
        "model": model,
        "timeout": max(5, min(timeout, 300)),
        "enabled": bool(base and key and not disabled),
    }


def public_status(data_root: str | Path | None = None) -> dict:
    cfg = config(data_root)
    base = cfg["base_url"]
    host = ""
    if base:
        try:
            from urllib.parse import urlsplit
            host = urlsplit(base).netloc
        except ValueError:
            host = ""
    return {
        "enabled": cfg["enabled"],
        "model": cfg["model"],
        "gateway": host,
        "fallback": "Deterministic scoring runs automatically when the model is unavailable.",
    }


class AIError(RuntimeError):
    pass


def _extract_json(text: str):
    text = (text or "").strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except ValueError:
                continue
    raise AIError("The model did not return valid JSON.")


def chat(messages, data_root=None, max_tokens=2400, temperature=0.2) -> dict:
    cfg = config(data_root)
    if not cfg["enabled"]:
        raise AIError("AI gateway is not configured.")
    body = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    request = urllib.request.Request(
        cfg["base_url"] + "/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {cfg['api_key']}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    started = time.time()
    last_error = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(request, timeout=cfg["timeout"]) as response:
                payload = json.loads(response.read().decode("utf-8"))
            choices = payload.get("choices") or []
            if not choices:
                raise AIError("The model returned no choices.")
            message = choices[0].get("message") or {}
            content = message.get("content")
            if isinstance(content, list):
                content = "".join(str(part.get("text", "")) for part in content
                                  if isinstance(part, dict))
            parsed = _extract_json(str(content or ""))
            usage = payload.get("usage") or {}
            return {
                "data": parsed,
                "model": payload.get("model") or cfg["model"],
                "usage": usage,
                "seconds": round(time.time() - started, 2),
                "cost": usage.get("cost"),
            }
        except (urllib.error.URLError, socket.timeout, TimeoutError, ValueError,
                AIError) as exc:
            last_error = exc
            if attempt == 0:
                time.sleep(1.5)
    raise AIError(f"AI request failed: {last_error}")
