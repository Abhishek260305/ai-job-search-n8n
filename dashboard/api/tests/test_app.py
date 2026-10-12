"""The HTTP shell: Host check, security headers, health, static files and the client-side-route fallback."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import SECURITY_HEADERS, create_app, host_without_port
from settings import load_settings
from store import PgStore
from tests.support import FakeStore, make_env


# ---------------------------------------------------------------- Host check (DNS rebinding)

@pytest.mark.parametrize("host", ["localhost", "localhost:8080", "127.0.0.1", "127.0.0.1:18080", "LOCALHOST:8080"])
def test_local_host_names_are_accepted(client, host):
    assert client.get("/api/health", headers={"host": host}).status_code == 200


@pytest.mark.parametrize("host", ["evil.example", "evil.example:8080", "localhost.evil.example", "192.0.2.10:8080",
                                  "", "[::1]:8080", "127.0.0.1.nip.io"])
def test_any_other_host_is_refused_before_routing(client, host):
    r = client.get("/api/health", headers={"host": host})
    assert r.status_code == 400
    assert r.json() == {"detail": "unknown host"}
    assert r.headers["X-Frame-Options"] == "DENY", "refusals carry the security headers too"


def test_the_refusal_covers_every_path(client):
    for path in ("/", "/assets/index-abc123.js", "/api/openapi.json", "/favicon.svg"):
        assert client.get(path, headers={"host": "evil.example"}).status_code == 400, path


def test_an_extra_allowed_host_works(tmp_path, static_dir):
    s = load_settings(make_env(tmp_path, DASHBOARD_ALLOWED_HOSTS="myvm.tailnet.ts.net"))
    c = TestClient(create_app(s, FakeStore()), base_url="http://localhost")
    assert c.get("/api/health", headers={"host": "myvm.tailnet.ts.net:8080"}).status_code == 200
    assert c.get("/api/health", headers={"host": "othervm.tailnet.ts.net"}).status_code == 400


@pytest.mark.parametrize("raw, expected", [
    ("localhost:8080", "localhost"), ("LocalHost", "localhost"), ("127.0.0.1:80", "127.0.0.1"),
    ("[::1]:8080", "::1"), ("[::1", ""), ("::1", "::1"), ("", ""),
])
def test_host_without_port(raw, expected):
    assert host_without_port(raw) == expected


# ---------------------------------------------------------------- security and cache headers

@pytest.mark.parametrize("path", ["/", "/api/health", "/assets/index-abc123.js", "/jobs", "/api/nope"])
def test_every_response_carries_the_security_headers(client, path):
    r = client.get(path)
    for name, value in SECURITY_HEADERS.items():
        assert r.headers[name] == value, f"{name} on {path}"


def test_the_policy_allows_only_same_origin_scripts(client):
    csp = client.get("/").headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert "script-src" not in csp, "scripts fall back to default-src 'self': no inline or remote scripts"
    assert "unsafe-eval" not in csp


def test_api_responses_are_never_cached(client):
    assert client.get("/api/health").headers["Cache-Control"] == "no-store"
    assert client.get("/api/nope").headers["Cache-Control"] == "no-store"


def test_hashed_assets_are_cached_and_the_index_is_revalidated(client):
    asset = client.get("/assets/index-abc123.js")
    assert asset.status_code == 200 and asset.text == "console.log('app')"
    assert asset.headers["Cache-Control"] == "public, max-age=31536000, immutable"
    assert client.get("/").headers["Cache-Control"] == "no-cache", "a new deploy must be picked up"
    missing = client.get("/assets/nope.js")
    assert missing.status_code == 404 and "immutable" not in missing.headers.get("Cache-Control", "")


# ---------------------------------------------------------------- health

def test_health_reports_the_database(client):
    assert client.get("/api/health").json() == {"ok": True, "db": True}


def test_health_is_200_even_when_the_database_is_down(settings):
    c = TestClient(create_app(settings, FakeStore(db_up=False)), base_url="http://localhost")
    r = c.get("/api/health")
    assert r.status_code == 200 and r.json() == {"ok": True, "db": False}


def test_pg_store_ping_is_false_not_an_exception_when_nothing_listens():
    # port 1 on this machine: refused at once, and the no-network guard allows loopback
    assert PgStore("host=127.0.0.1 port=1 dbname=x user=y password=z connect_timeout=2").ping() is False


# ---------------------------------------------------------------- the built app and client-side routes

@pytest.mark.parametrize("path", ["/", "/jobs", "/jobs/953", "/login?next=/jobs", "/settings/schedules"])
def test_client_side_routes_get_the_index(client, path):
    r = client.get(path)
    assert r.status_code == 200 and "dashboard index" in r.text
    assert r.headers["content-type"].startswith("text/html")


@pytest.mark.parametrize("path", ["/%00", "/jobs%00.js", "/" + "a" * 5000])
def test_a_nul_byte_or_an_absurd_name_is_just_another_route(client, path):
    """A NUL byte made Path.resolve() raise, which was a 500 instead of the app's index."""
    r = client.get(path)
    assert r.status_code == 200 and "dashboard index" in r.text


def test_head_requests_get_the_same_headers_as_get(client):
    """curl -I is how the headers get checked by hand."""
    r = client.head("/jobs")
    assert r.status_code == 200 and r.content == b""
    assert r.headers["cache-control"] == "no-cache"
    assert r.headers["content-security-policy"] == client.get("/jobs").headers["content-security-policy"]


def test_files_at_the_root_of_the_build_are_served(client):
    r = client.get("/favicon.svg")
    assert r.status_code == 200 and r.text == "<svg/>"


@pytest.mark.parametrize("path", ["/api", "/api/", "/api/nope", "/api/jobs/1/whatever"])
def test_unknown_api_paths_are_json_404s_not_the_index(client, path):
    r = client.get(path)
    assert r.status_code == 404
    assert r.headers["content-type"].startswith("application/json")
    assert "dashboard index" not in r.text


def test_paths_cannot_climb_out_of_the_build(client, tmp_path):
    (tmp_path / "outside-secret.txt").write_text("TOP SECRET", encoding="utf-8")
    for path in ("/../outside-secret.txt", "/%2e%2e/outside-secret.txt", "/..%2Foutside-secret.txt",
                 "/assets/../../outside-secret.txt", "/assets/%2e%2e/%2e%2e/outside-secret.txt"):
        r = client.get(path)
        assert "TOP SECRET" not in r.text, path


def test_a_missing_build_is_explained_not_a_crash(tmp_path):
    s = load_settings(make_env(tmp_path, STATIC_DIR=str(tmp_path / "not-built")))
    c = TestClient(create_app(s, FakeStore()), base_url="http://localhost")
    r = c.get("/")
    assert r.status_code == 503 and "not built" in r.text
    assert c.get("/api/health").status_code == 200, "the API works without the UI"


def test_writes_to_unknown_paths_are_refused(client):
    assert client.post("/jobs").status_code == 405


# ---------------------------------------------------------------- the API schema

def test_the_openapi_schema_is_published_under_api(client):
    r = client.get("/api/openapi.json")
    assert r.status_code == 200 and "/api/health" in r.json()["paths"]
    assert "/{full_path}" not in r.json()["paths"], "the fallback route is not part of the API"


def test_no_interactive_api_docs(client):
    for path in ("/docs", "/redoc"):
        assert "swagger" not in client.get(path).text.lower() and "redoc" not in client.get(path).text.lower()


# ---------------------------------------------------------------- startup (what the container runs)

def test_build_refuses_to_start_with_a_clear_message(monkeypatch, capsys):
    import app
    for name in ("DASHBOARD_PASSWORD", "DASHBOARD_SECRET", "DB_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit) as e:
        app.build()
    assert e.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("dashboard: cannot start:") and "DASHBOARD_PASSWORD" in err


def test_build_makes_the_app_without_touching_the_database(monkeypatch, tmp_path, static_dir):
    import app
    for name, value in make_env(tmp_path).items():
        monkeypatch.setenv(name, value)
    built = app.build()
    assert isinstance(built.state.store, PgStore)
    assert TestClient(built, base_url="http://localhost").get("/").status_code == 200


@pytest.mark.parametrize("args, kept", [
    (("127.0.0.1:41234", "GET", "/api/health", "1.1", 200), False),
    (("172.17.0.1:5000", "GET", "/api/health", "1.1", 200), True),
    (("127.0.0.1:41234", "GET", "/api/summary", "1.1", 200), True),
    (("127.0.0.1:41234", "GET", "/api/health?x=1", "1.1", 200), True),
    ((), True),
])
def test_only_the_containers_own_health_checks_are_left_out_of_the_access_log(args, kept):
    import logging

    from app import QuietHealthChecks
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d', args or None, None)
    assert QuietHealthChecks().filter(record) is kept
