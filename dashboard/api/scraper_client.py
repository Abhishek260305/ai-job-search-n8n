"""The dashboard's one outbound call: the scraper service, for the settings page.

Every setting the page can change already has a validated endpoint in the scraper (scraper/app.py), which the
CLI and Telegram use too, so a rule such as "collect runs at most every 15 minutes" lives there and only there. This
client calls a fixed list of those endpoints and nothing else: the paths are constants here, and what a request
contributes is only the JSON body. The scraper has no login and is reachable only inside the compose network;
this API is what puts a login in front of it.

Plain urllib: no new dependency, no redirects (the scraper never redirects), no proxy from the environment, a short
timeout and a size cap.
"""
from __future__ import annotations

import http.client
import json
import logging
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from store import Invalid, ProfileChanged, Unavailable

log = logging.getLogger("dashboard.scraper")

TIMEOUT_S = 10.0
FIND_TIMEOUT_S = 30.0            # "find this company" probes five public APIs (in parallel, at most 20 requests)
MAX_BODY = 1_000_000


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def check_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise ValueError("SCRAPER_URL must be an http(s) address such as http://scraper:8000")
    return url.rstrip("/")


def _errors_of(body: bytes) -> str | None:
    """The scraper's refusals are {"detail": {"errors": [...]}}; FastAPI's own are {"detail": [{"msg": ...}]}."""
    try:
        detail = json.loads(body).get("detail")
    except (ValueError, AttributeError):
        return None
    if isinstance(detail, dict) and isinstance(detail.get("errors"), list):
        return "; ".join(str(e) for e in detail["errors"])[:300] or None
    if isinstance(detail, str):
        return detail[:300]
    if isinstance(detail, list):
        msgs = [str(d.get("msg")) for d in detail if isinstance(d, dict) and d.get("msg")]
        return "; ".join(msgs)[:300] or None
    return None


class ScraperClient:
    def __init__(self, base_url: str, timeout: float = TIMEOUT_S, opener=None):
        self._base = check_url(base_url)
        self._timeout = timeout
        self._opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect).open

    def _call(self, method: str, path: str, body: dict | None = None, timeout: float | None = None) -> dict:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self._base + path, data=data, method=method,
                                         headers={"Content-Type": "application/json", "Accept": "application/json"})
        try:
            with self._opener(request, timeout=timeout or self._timeout) as response:
                raw = response.read(MAX_BODY + 1)
        except urllib.error.HTTPError as e:
            if e.code == 422:
                try:
                    detail = _errors_of(e.read(MAX_BODY))
                except (OSError, http.client.HTTPException):
                    detail = None
                raise Invalid(detail or "the scraper refused that value") from None
            if e.code == 409:
                try:
                    detail = _errors_of(e.read(MAX_BODY))
                except (OSError, http.client.HTTPException):
                    detail = None
                raise ProfileChanged(detail or "the search profile was changed somewhere else: reload to see it") from None
            if e.code == 404:       # not a "job not found": the address or the scraper's version is wrong
                raise Unavailable("the scraper does not have that endpoint (is SCRAPER_URL right, and the scraper up to date?)") from None
            log.warning("scraper %s %s answered %s", method, path, e.code)
            raise Unavailable("the scraper answered with an error") from None
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as e:   # incl. a connection cut mid-answer
            log.warning("scraper %s %s unreachable: %s", method, path, type(e).__name__)
            raise Unavailable("the scraper is not reachable right now") from None
        if len(raw) > MAX_BODY:
            raise Unavailable("the scraper answered with too much data")
        try:
            result = json.loads(raw)
        except ValueError:
            raise Unavailable("the scraper gave an answer this page cannot read") from None
        if not isinstance(result, dict):
            raise Unavailable("the scraper gave an answer this page cannot read")
        return result

    # ---- reads (the same reports the CLI prints) --------------------------------------------------------------
    def schedule(self) -> dict:
        return self._call("GET", "/schedule")

    def features(self) -> dict:
        return self._call("GET", "/features")

    def tailoring(self) -> dict:
        return self._call("GET", "/tailor/settings")

    def budget(self) -> dict:
        return self._call("GET", "/budget")

    def profile(self) -> dict:
        return self._call("GET", "/profile")

    # ---- writes: the scraper validates, and refuses with a 422 the page shows as it is ------------------------
    def set_schedule(self, name: str, changes: dict) -> dict:
        return self._call("POST", f"/schedule/{name}", changes)

    def run_schedule(self, name: str) -> dict:
        return self._call("POST", f"/schedule/{name}/run", {})

    def set_features(self, flags: dict) -> dict:
        return self._call("POST", "/features", flags)

    def set_tailoring(self, changes: dict) -> dict:
        return self._call("POST", "/tailor/settings", changes)

    def set_budget(self, caps: dict) -> dict:
        return self._call("POST", "/budget/settings", caps)

    def save_profile(self, profile: dict, version: str) -> dict:
        return self._call("POST", "/profile", {"profile": profile, "version": version})

    def find_board(self, name: str) -> dict:
        return self._call("POST", "/boards/find", {"name": name}, timeout=FIND_TIMEOUT_S)

