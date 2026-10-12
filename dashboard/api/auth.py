"""Login for the dashboard: one password, a signed session cookie, a lockout after repeated failures.

- The password is compared as SHA-256 digests in constant time, so neither its content nor its length leaks
  through timing.
- The session cookie is `payload.signature`: the payload is base64url JSON {"v", "iat" (issued, in milliseconds),
  "exp" (seconds), "n" (random)}, the signature an
  HMAC-SHA256 under a key derived from DASHBOARD_SECRET. Nothing is stored on the server. Changing the secret ends
  every session for good; logging out ends every session issued so far (until the service restarts).
- After 10 failed logins within 15 minutes, every login is refused for 15 minutes, even with the right password.
  The limit is global, not per address: through the SSH tunnel every request comes from the same Docker gateway.
- Every protected request needs a valid cookie. One that changes something (anything but GET/HEAD/OPTIONS) also
  needs the X-Requested-With header, which another website cannot add without a CORS preflight this service never
  allows, and must not come from a foreign Origin. The cookie is SameSite=Strict on top of that.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import secrets
import threading
import time
from collections import deque
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

COOKIE = "jd_session"
WRITE_HEADER = "X-Requested-With"
WRITE_VALUE = "jobsearch-dashboard"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
MAX_TOKEN = 512


def password_ok(given: str, expected: str) -> bool:
    return hmac.compare_digest(hashlib.sha256(given.encode("utf-8")).digest(),
                               hashlib.sha256(expected.encode("utf-8")).digest())


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Sessions:
    def __init__(self, secret: str, days: int, clock=time.time):
        self._key = hashlib.sha256(b"jobsearch-dashboard session v1|" + secret.encode("utf-8")).digest()
        self._ttl = days * 86400
        self._clock = clock
        self._revoked_at_ms = -1

    @property
    def max_age(self) -> int:
        return self._ttl

    def _sign(self, payload: str) -> str:
        return _b64(hmac.new(self._key, payload.encode("ascii"), hashlib.sha256).digest())

    def issue(self) -> tuple[str, int]:
        now = self._clock()
        body = {"v": 1, "iat": int(now * 1000), "exp": int(now) + self._ttl, "n": secrets.token_urlsafe(8)}
        payload = _b64(json.dumps(body, separators=(",", ":")).encode("utf-8"))
        return f"{payload}.{self._sign(payload)}", body["exp"]

    def verify(self, token: str | None) -> int | None:
        """The expiry (unix seconds) of a valid session, or None."""
        if not token or len(token) > MAX_TOKEN or not token.isascii() or token.count(".") != 1:
            return None
        payload, signature = token.split(".")
        if not hmac.compare_digest(signature, self._sign(payload)):
            return None
        try:
            body = json.loads(_unb64(payload))
            iat, exp = int(body["iat"]), int(body["exp"])
        except (ValueError, KeyError, TypeError):
            return None
        if body.get("v") != 1 or exp <= self._clock() or iat <= self._revoked_at_ms:
            return None
        return exp

    def revoke_all(self) -> None:
        # milliseconds: everything issued up to now ends, and a login a moment later is valid again
        self._revoked_at_ms = int(self._clock() * 1000)


class LoginLimiter:
    def __init__(self, max_failures: int = 10, window: int = 900, lockout: int = 900, clock=time.monotonic):
        self._max, self._window, self._lockout, self._clock = max_failures, window, lockout, clock
        self._failures: deque[float] = deque()
        self._locked_until = 0.0
        self._lock = threading.Lock()

    def retry_after(self) -> int:
        """Seconds until logins are accepted again; 0 when they are accepted now."""
        with self._lock:
            remaining = self._locked_until - self._clock()
            return math.ceil(remaining) if remaining > 0 else 0

    def failure(self) -> None:
        with self._lock:
            now = self._clock()
            self._failures.append(now)
            while self._failures and self._failures[0] <= now - self._window:
                self._failures.popleft()
            if len(self._failures) >= self._max:
                self._locked_until = now + self._lockout
                self._failures.clear()

    def success(self) -> None:
        with self._lock:
            self._failures.clear()


def _foreign_origin(request: Request, allowed_hosts: frozenset[str]) -> bool:
    origin = request.headers.get("origin")
    if origin is None:
        return False
    host = (urlsplit(origin).hostname or "").lower()
    return host not in allowed_hosts


def check_write(request: Request) -> None:
    if request.headers.get(WRITE_HEADER) != WRITE_VALUE:
        raise HTTPException(status_code=403, detail="missing request header")
    if _foreign_origin(request, request.app.state.settings.allowed_hosts):
        raise HTTPException(status_code=403, detail="foreign origin")


def require_auth(request: Request) -> int:
    """Dependency for every protected route. Returns the session's expiry."""
    exp = request.app.state.sessions.verify(request.cookies.get(COOKIE))
    if exp is None:
        raise HTTPException(status_code=401, detail="not logged in")
    if request.method not in SAFE_METHODS:
        check_write(request)
    return exp
