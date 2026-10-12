"""Login, sessions, the lockout and the write protection."""
from __future__ import annotations

import base64
import json

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from app import PUBLIC_API, create_app
from auth import COOKIE, WRITE_HEADER, WRITE_VALUE, LoginLimiter, Sessions, password_ok, require_auth
from tests.support import PASSWORD, SECRET, FakeStore

WRITE = {WRITE_HEADER: WRITE_VALUE}


class Clock:
    def __init__(self, t: float = 1_800_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def app(settings, clock):
    a = create_app(settings, FakeStore(), sessions=Sessions(SECRET, 7, clock=clock), limiter=LoginLimiter(clock=clock))

    # a stand-in for the routes later tasks add to the protected router
    @a.post("/api/test-write", dependencies=[Depends(require_auth)])
    def test_write():
        return {"ok": True}

    return a


@pytest.fixture
def client(app):
    return TestClient(app, base_url="http://localhost")


def login(client, password=PASSWORD):
    return client.post("/api/login", json={"password": password})


# ---------------------------------------------------------------- the password

def test_password_comparison():
    assert password_ok(PASSWORD, PASSWORD)
    assert not password_ok(PASSWORD + " ", PASSWORD)
    assert not password_ok("", PASSWORD)
    assert password_ok("pässwörd-ünïcode", "pässwörd-ünïcode")


def test_right_password_sets_a_strict_http_only_cookie(client):
    r = login(client)
    assert r.status_code == 204
    cookie = r.headers["set-cookie"]
    assert cookie.startswith(f"{COOKIE}=")
    low = cookie.lower()
    assert "httponly" in low and "samesite=strict" in low and "path=/" in low
    assert "max-age=604800" in low, "7 days"
    assert client.get("/api/me").status_code == 200


def test_wrong_password_is_401_and_reveals_nothing(client):
    for wrong in ("x", "wrong password of another length entirely", PASSWORD[:-1]):
        r = login(client, wrong)
        assert r.status_code == 401
        assert r.json() == {"detail": "wrong password"}
        assert "set-cookie" not in r.headers
    assert client.get("/api/me").status_code == 401


def test_a_malformed_login_body_is_rejected(client):
    assert client.post("/api/login", json={}).status_code == 422
    assert client.post("/api/login", json={"password": "x" * 1025}).status_code == 422
    assert client.post("/api/login", content="password=x").status_code == 422


# ---------------------------------------------------------------- /api/me and the protected router

def test_me_reports_the_expiry(client, clock):
    login(client)
    body = client.get("/api/me").json()
    assert body["authenticated"] is True
    assert body["expires_at"].startswith("2027-01-")     # 1_800_000_000 is 2027-01-15; plus 7 days


def test_every_protected_api_route_needs_a_session(app):
    """Walks every API route (from the OpenAPI schema, which also lists routes on included routers): a route added
    later without the dependency would fail here."""
    anonymous = TestClient(app, base_url="http://localhost")
    seen = set()
    for path, operations in app.openapi()["paths"].items():
        if not path.startswith("/api") or path in PUBLIC_API:
            continue
        concrete = path.replace("{", "").replace("}", "")   # /api/jobs/{job_id} -> /api/jobs/job_id
        for method in operations:
            r = anonymous.request(method.upper(), concrete, headers=WRITE)
            assert r.status_code == 401, f"{method.upper()} {path} answered {r.status_code} without a session"
            seen.add(f"{method.upper()} {path}")
    assert {"GET /api/me", "POST /api/test-write", "GET /api/summary", "GET /api/jobs", "GET /api/jobs/{job_id}",
            "PATCH /api/jobs/{job_id}/status", "GET /api/jobs/{job_id}/files/{kind}", "GET /api/export.csv"} <= seen, seen


def test_the_public_api_is_only_what_is_meant_to_be_public(app):
    paths = {p for p in app.openapi()["paths"] if p.startswith("/api")}
    assert paths & PUBLIC_API == {"/api/health", "/api/login", "/api/logout"}


# ---------------------------------------------------------------- the cookie itself

def test_a_tampered_payload_is_rejected(client):
    login(client)
    payload, sig = client.cookies[COOKIE].split(".")
    body = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    body["exp"] += 10 * 365 * 86400
    forged = base64.urlsafe_b64encode(json.dumps(body).encode()).rstrip(b"=").decode()
    client.cookies.set(COOKIE, f"{forged}.{sig}")
    assert client.get("/api/me").status_code == 401


def test_a_tampered_signature_is_rejected(client):
    login(client)
    payload, sig = client.cookies[COOKIE].split(".")
    client.cookies.set(COOKIE, f"{payload}.{sig[:-2]}{'AA' if sig[-2:] != 'AA' else 'BB'}")
    assert client.get("/api/me").status_code == 401


def test_a_cookie_signed_with_another_secret_is_rejected(clock):
    mine, theirs = Sessions(SECRET, 7, clock=clock), Sessions("t" * 40, 7, clock=clock)
    token, _ = theirs.issue()
    assert mine.verify(token) is None
    assert theirs.verify(token) is not None


def test_a_session_expires(client, clock):
    login(client)
    clock.advance(7 * 86400 - 1)
    assert client.get("/api/me").status_code == 200
    clock.advance(1)
    assert client.get("/api/me").status_code == 401


@pytest.mark.parametrize("junk", ["", "abc", "a.b.c", "a.b", "é.ü", "x" * 600 + "." + "y", ".", "e30.", ".sig"])
def test_junk_cookies_are_rejected_without_errors(clock, junk):
    assert Sessions(SECRET, 7, clock=clock).verify(junk) is None


def test_a_validly_signed_but_wrong_shaped_payload_is_rejected(clock):
    s = Sessions(SECRET, 7, clock=clock)
    for body in (b"[1,2]", b'"text"', b"7", b'{"v":2,"iat":1,"exp":9999999999}', b'{"v":1,"exp":9999999999}', b"not json"):
        payload = base64.urlsafe_b64encode(body).rstrip(b"=").decode()
        assert s.verify(f"{payload}.{s._sign(payload)}") is None, body


# ---------------------------------------------------------------- logout

def test_logout_ends_every_session_issued_so_far(app, clock):
    laptop, phone = TestClient(app, base_url="http://localhost"), TestClient(app, base_url="http://localhost")
    login(laptop), login(phone)
    stolen = laptop.cookies[COOKIE]
    r = laptop.post("/api/logout", headers=WRITE)
    assert r.status_code == 204
    assert f'{COOKIE}=""' in r.headers["set-cookie"] or "max-age=0" in r.headers["set-cookie"].lower()
    assert laptop.get("/api/me").status_code == 401
    assert phone.get("/api/me").status_code == 401, "every session issued before the logout ends"
    thief = TestClient(app, base_url="http://localhost")
    thief.cookies.set(COOKIE, stolen)
    assert thief.get("/api/me").status_code == 401, "a copied cookie dies with the logout"
    clock.advance(0.002)
    login(laptop)
    assert laptop.get("/api/me").status_code == 200, "logging in again works at once"


def test_logout_without_a_session_still_clears_the_cookie(client):
    assert client.post("/api/logout", headers=WRITE).status_code == 204


def test_logout_needs_the_header(client):
    login(client)
    assert client.post("/api/logout").status_code == 403
    assert client.get("/api/me").status_code == 200, "a cross-site request cannot log you out"


def test_an_anonymous_logout_does_not_end_real_sessions(app):
    user, stranger = TestClient(app, base_url="http://localhost"), TestClient(app, base_url="http://localhost")
    login(user)
    stranger.post("/api/logout", headers=WRITE)
    assert user.get("/api/me").status_code == 200


# ---------------------------------------------------------------- the lockout

def test_ten_failures_lock_every_login_for_fifteen_minutes(client, clock):
    for _ in range(10):
        assert login(client, "nope").status_code == 401
    r = login(client)
    assert r.status_code == 429, "even the right password is refused while locked"
    assert r.json() == {"detail": "too many attempts"}
    assert 1 <= int(r.headers["Retry-After"]) <= 900
    clock.advance(899)
    assert login(client).status_code == 429
    clock.advance(2)
    assert login(client).status_code == 204, "the lock lifts after 15 minutes"


def test_failures_spread_over_more_than_the_window_do_not_lock(client, clock):
    for _ in range(9):
        login(client, "nope")
    clock.advance(901)
    login(client, "nope")
    assert login(client).status_code == 204


def test_a_success_resets_the_count(client):
    for _ in range(9):
        login(client, "nope")
    assert login(client).status_code == 204
    for _ in range(9):
        login(client, "nope")
    assert login(client).status_code == 204


def test_the_limiter_reports_whole_seconds(clock):
    lim = LoginLimiter(max_failures=1, window=60, lockout=30, clock=clock)
    assert lim.retry_after() == 0
    lim.failure()
    assert lim.retry_after() == 30
    clock.advance(29.5)
    assert lim.retry_after() == 1
    clock.advance(0.5)
    assert lim.retry_after() == 0


# ---------------------------------------------------------------- writes

def test_a_write_needs_the_session_first(client):
    assert client.post("/api/test-write", headers=WRITE).status_code == 401


def test_a_write_needs_the_header(client):
    login(client)
    assert client.post("/api/test-write").status_code == 403
    assert client.post("/api/test-write", headers={WRITE_HEADER: "XMLHttpRequest"}).status_code == 403
    assert client.post("/api/test-write", headers=WRITE).json() == {"ok": True}


@pytest.mark.parametrize("origin, ok", [
    ("http://localhost:8080", True), ("http://127.0.0.1:18080", True), ("https://evil.example", False),
    ("http://localhost.evil.example", False), ("null", False),
])
def test_a_write_from_a_foreign_origin_is_refused(client, origin, ok):
    login(client)
    r = client.post("/api/test-write", headers={**WRITE, "Origin": origin})
    assert (r.status_code == 200) is ok, (origin, r.status_code)


def test_reads_do_not_need_the_header(client):
    login(client)
    assert client.get("/api/me").status_code == 200


def test_no_cors_headers_are_ever_sent(client):
    r = client.options("/api/test-write", headers={"Origin": "https://evil.example",
                                                   "Access-Control-Request-Method": "POST",
                                                   "Access-Control-Request-Headers": WRITE_HEADER})
    assert "access-control-allow-origin" not in r.headers
    assert "access-control-allow-headers" not in r.headers
