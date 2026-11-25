"""Authentication helpers for the Coder-hosted podcast clipper.

Two layers protect the app:

* The Coder workspace proxy only forwards authenticated users to the port.
* An optional application access code adds a second, app-level login.

Requests whose Host is outside the configured allow-list are rejected. That
prevents DNS-rebinding or accidentally exposing a wildcard bind without the
proxy.
"""
from __future__ import annotations

import base64
import fnmatch
import hashlib
import hmac
import http.cookies
import os
import secrets
import threading
import time
from pathlib import Path

COOKIE = "ftf_session"
SESSION_SECONDS = 7 * 24 * 3600
DEFAULT_HOSTS = (
    "localhost",
    "127.0.0.1",
    "[::1]",
    "*.dollarsperacrecropsolutions.app",
    "*.coder.app",
)


def _env_list(name: str) -> list[str]:
    raw = os.environ.get(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


class Auth:
    def __init__(self, data_root: str | Path):
        self.data_root = Path(data_root)
        self.data_root.mkdir(parents=True, exist_ok=True)
        secret_path = self.data_root / ".session-secret"
        if secret_path.exists():
            self.secret = secret_path.read_bytes()
        else:
            self.secret = secrets.token_bytes(48)
            secret_path.write_bytes(self.secret)
            try:
                secret_path.chmod(0o600)
            except OSError:
                pass
        self.hosts = _env_list("PODCAST_CLIPPER_ALLOWED_HOSTS") or list(DEFAULT_HOSTS)
        self.allow_any_host = os.environ.get("PODCAST_CLIPPER_ALLOW_ANY_HOST") == "1"
        self.code_lock = threading.Lock()
        self.failures: dict[str, list[float]] = {}
        code_file = self.data_root / "access-code.txt"
        self.code_file = code_file
        self.code = os.environ.get("PODCAST_CLIPPER_ACCESS_CODE", "").strip()
        if not self.code and code_file.is_file():
            self.code = code_file.read_text(encoding="utf-8").strip()

    # -- host ---------------------------------------------------------------
    @staticmethod
    def _hostname(host_header: str) -> str:
        host = (host_header or "").strip().lower()
        if host.startswith("["):
            end = host.find("]")
            return host[:end + 1] if end != -1 else host
        return host.split(":", 1)[0]

    def host_allowed(self, host_header: str) -> bool:
        if self.allow_any_host:
            return True
        host = self._hostname(host_header)
        if not host:
            return False
        return any(fnmatch.fnmatchcase(host, pattern.lower()) for pattern in self.hosts)

    # -- sessions -----------------------------------------------------------
    def _sign(self, value: str) -> str:
        return hmac.new(self.secret, value.encode(), hashlib.sha256).hexdigest()

    def _encode_session(self, expires: int) -> str:
        payload = f"{expires}.{secrets.token_hex(8)}"
        token = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=")
        return f"{token}.{self._sign(payload)}"

    def _decode_session(self, token: str) -> bool:
        try:
            encoded, signature = token.rsplit(".", 1)
            padding = "=" * (-len(encoded) % 4)
            payload = base64.urlsafe_b64decode(encoded + padding).decode()
            if not hmac.compare_digest(signature, self._sign(payload)):
                return False
            expires = int(payload.split(".", 1)[0])
            return expires >= int(time.time())
        except (ValueError, TypeError, UnicodeDecodeError):
            return False

    def cookie_from(self, handler) -> str | None:
        raw = handler.headers.get("Cookie")
        if not raw:
            return None
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(raw)
        except http.cookies.CookieError:
            return None
        morsel = jar.get(COOKIE)
        return morsel.value if morsel else None

    def authenticated(self, handler) -> bool:
        if not self.code:
            return True
        token = self.cookie_from(handler)
        return bool(token and self._decode_session(token))

    def session_cookie(self) -> str:
        """Set-Cookie value for a new app session."""
        expires = int(time.time()) + SESSION_SECONDS
        token = self._encode_session(expires)
        return (f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; "
                f"Max-Age={SESSION_SECONDS}")

    def new_session(self) -> tuple[str, str]:
        """Return (Set-Cookie value, session token)."""
        expires = int(time.time()) + SESSION_SECONDS
        token = self._encode_session(expires)
        cookie = (f"{COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict; "
                  f"Max-Age={SESSION_SECONDS}")
        return cookie, token

    def csrf_value(self, token: str | None) -> str:
        return hmac.new(self.secret, ("csrf:" + (token or "anonymous")).encode(),
                        hashlib.sha256).hexdigest()

    def csrf(self, handler) -> str:
        token = self.cookie_from(handler) or "anonymous"
        return self.csrf_value(token)

    def csrf_ok(self, handler) -> bool:
        supplied = handler.headers.get("X-CSRF-Token", "")
        return bool(supplied) and hmac.compare_digest(supplied, self.csrf(handler))

    # -- access code --------------------------------------------------------
    def set_code(self, code: str) -> None:
        code = (code or "").strip()
        self.code = code
        if code:
            self.code_file.write_text(code + "\n", encoding="utf-8")
            try:
                self.code_file.chmod(0o600)
            except OSError:
                pass
        elif self.code_file.exists():
            self.code_file.unlink()

    def client_key(self, handler) -> str:
        return (handler.headers.get("X-Forwarded-For", "").split(",")[0].strip()
                or handler.client_address[0])

    def locked_out(self, key: str) -> bool:
        now = time.time()
        with self.code_lock:
            recent = [stamp for stamp in self.failures.get(key, []) if now - stamp < 600]
            self.failures[key] = recent
            return len(recent) >= 8

    def note_failure(self, key: str) -> None:
        with self.code_lock:
            self.failures.setdefault(key, []).append(time.time())

    def check_code(self, supplied: str) -> bool:
        return bool(self.code) and hmac.compare_digest(supplied or "", self.code)
