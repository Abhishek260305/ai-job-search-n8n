"""Version 2a: the health and spend panels and the pause switch. Routes against a fake store, the store against a
fake connection, and the response models against the data shapes the SQL functions return."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import create_app
from models import Health, Spend
from store import Invalid, PgStore, Unavailable
from tests.support import HEALTH, PASSWORD, SPEND, WRITE
from tests.test_store import store_with


@pytest.fixture
def client(settings, store):
    c = TestClient(create_app(settings, store), base_url="http://localhost")
    assert c.post("/api/login", json={"password": PASSWORD}).status_code == 204
    return c


# ---------------------------------------------------------------- reads

def test_pipeline_health(client, store):
    r = client.get("/api/pipeline")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["paused"] == {"paused": False, "paused_at": None, "reason": None}
    assert [s["name"] for s in body["schedules"]] == ["collect", "digest"]
    assert body["schedules"][1]["at_time"] == "08:00:00" and body["schedules"][1]["feature_off"] is True
    assert body["parked"]["items"][0]["job_id"] == 9 and body["sources"]["items"][0]["key"] == "newco"
    assert body["alerts"][0]["code"] == "parked"
    assert store.calls == [("health",)]


def test_spend(client, store):
    r = client.get("/api/spend")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["claude"]["monthly_cap"] == 10 and body["apify"]["daily_cap"] == 0.5
    assert body["daily"][0]["day"] == "2026-10-12" and body["models"][0]["model"] == "claude-haiku-5-5"
    assert store.calls == [("spend",)]


@pytest.mark.parametrize("path", ["/api/pipeline", "/api/spend"])
def test_the_reads_need_a_session(settings, store, path):
    anonymous = TestClient(create_app(settings, store), base_url="http://localhost")
    assert anonymous.get(path).status_code == 401
    assert store.calls == []


@pytest.mark.parametrize("path", ["/api/pipeline", "/api/spend"])
def test_a_database_outage_is_503(client, store, path):
    store.fail = Unavailable("the database is not available right now")
    assert client.get(path).status_code == 503


def test_extra_fields_from_the_store_are_not_leaked(client, store, monkeypatch):
    monkeypatch.setitem(HEALTH, "secret_thing", "x")
    assert "secret_thing" not in client.get("/api/pipeline").json()


# ---------------------------------------------------------------- pause and resume

def test_pause_with_a_reason(client, store):
    r = client.post("/api/pipeline/pause", headers=WRITE, json={"reason": "away for a week"})
    assert r.status_code == 200 and r.json()["paused"] is True and r.json()["reason"] == "away for a week"
    assert store.calls == [("set_pause", True, "away for a week")]


def test_pause_without_a_body(client, store):
    assert client.post("/api/pipeline/pause", headers=WRITE).status_code == 200
    assert store.calls == [("set_pause", True, None)]


def test_resume(client, store):
    r = client.post("/api/pipeline/resume", headers=WRITE)
    assert r.status_code == 200 and r.json() == {"paused": False, "paused_at": None, "reason": None}
    assert store.calls == [("set_pause", False, None)]


@pytest.mark.parametrize("path", ["/api/pipeline/pause", "/api/pipeline/resume"])
def test_pausing_needs_the_write_header_and_a_session(client, settings, store, path):
    assert client.post(path).status_code == 403
    assert client.post(path, headers=WRITE | {"Origin": "http://evil.example"}).status_code == 403
    anonymous = TestClient(create_app(settings, store), base_url="http://localhost")
    assert anonymous.post(path, headers=WRITE).status_code == 401
    assert store.calls == []


@pytest.mark.parametrize("body", [{"reason": "x" * 201}, {"reason": 5}, {"paused": True}, {"extra": 1}, {"reason": "a\u0000b"}])
def test_bad_pause_requests_are_422_before_the_store(client, store, body):
    assert client.post("/api/pipeline/pause", headers=WRITE, json=body).status_code == 422
    assert store.calls == []


def test_a_value_postgres_cannot_store_is_invalid_not_a_500():
    import psycopg
    store, _, _ = store_with(psycopg.errors.DataError("invalid byte sequence"))
    with pytest.raises(Invalid):
        store.set_pause(True, "x")


def test_a_refusal_from_the_database_is_a_422(client, store):
    store.fail = Invalid("paused must be true or false")
    assert client.post("/api/pipeline/pause", headers=WRITE).status_code == 422


# ---------------------------------------------------------------- the store

def test_store_health_calls_the_function():
    store, conn, _ = store_with([{"h": {"alerts": []}}])
    assert store.health() == {"alerts": []}
    assert conn.calls == [("SELECT dashboard_health() AS h", ())] and conn.closed


def test_store_spend_calls_the_function():
    store, conn, _ = store_with([{"s": {"daily": []}}])
    assert store.spend() == {"daily": []}
    assert conn.calls == [("SELECT dashboard_spend() AS s", ())]


def test_store_set_pause_calls_the_function_with_parameters():
    store, conn, _ = store_with([{"p": {"paused": True}}], [{"p": {"paused": False}}])
    assert store.set_pause(True, "x'; DROP TABLE jobs; --") == {"paused": True}
    assert conn.calls == [("SELECT dashboard_set_pause(%s, %s) AS p", (True, "x'; DROP TABLE jobs; --"))]
    store.set_pause(False, None)
    assert conn.calls[1] == ("SELECT dashboard_set_pause(%s, %s) AS p", (False, None))


def test_a_database_refusal_is_invalid():
    from tests.test_store import FakeDbError
    store, _, _ = store_with(FakeDbError("JD422", "paused must be true or false"))
    with pytest.raises(Invalid):
        store.set_pause(True, None)


# ---------------------------------------------------------------- the models

def test_the_models_accept_what_the_functions_return():
    assert Health.model_validate(HEALTH).alerts[0].code == "parked"
    assert Spend.model_validate(SPEND).claude.warn_pct == 80


def test_the_alert_codes_match_the_sql():
    """Migration 016 is the source of the codes; a code the model does not know would turn the whole panel into a 500."""
    import re
    from pathlib import Path
    from typing import get_args
    from models import AlertCode
    sql = (Path(__file__).resolve().parents[3] / "postgres" / "migrations" / "016_dashboard_ops.sql")
    if not sql.is_file():
        pytest.skip("postgres/ is not next to dashboard/")
    in_sql = set(re.findall(r"SELECT \d+, '(?:bad|warn)', '([a-z_]+)'", sql.read_text(encoding="utf-8")))
    assert in_sql == set(get_args(AlertCode))
