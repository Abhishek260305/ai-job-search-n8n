"""Version 3a: the status timeline, the Kanban board and the numbers. Routes against a fake store, the store against a
fake connection, and the response models against the shapes the SQL functions return."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import create_app
from models import Board, JobEvent, Stats, Tracking
from store import HistoryChanged, Invalid, NotFound, TrackingChanged, Unavailable
from tests.support import BOARD, EVENTS, PASSWORD, STATS, TRACKING, WRITE
from tests.test_store import store_with


@pytest.fixture
def client(settings, store):
    c = TestClient(create_app(settings, store), base_url="http://localhost")
    assert c.post("/api/login", json={"password": PASSWORD}).status_code == 204
    return c


def test_the_board(client, store):
    r = client.get("/api/board")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    body = r.json()
    assert [c["key"] for c in body["columns"]] == ["not_applied", "applied", "in_progress", "interview", "offer", "rejected"]
    assert body["columns"][0]["total"] == 51 and len(body["columns"][0]["items"]) == 2
    assert body["columns"][1]["items"][0]["pipeline_status"] == "applied"
    assert body["closed"] == {"withdrawn": 2, "skipped": 1}
    assert store.calls == [("board",)]


def test_the_stats(client, store):
    r = client.get("/api/stats")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["applied"] == 8 and body["avg_days_to_reply"] == 6.5 and len(body["weekly"]) == 12
    assert body["weekly"][0]["week_start"] == "2026-08-10"
    assert store.calls == [("stats",)]


def test_a_job_s_timeline(client, store):
    r = client.get("/api/jobs/1/events")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert [e["to_status"] for e in r.json()] == ["drafted", "applied"]
    assert r.json()[1]["source"] == "dashboard" and r.json()[1]["note"] == "sent my CV"
    assert store.calls == [("job_events", 1)]


def test_the_timeline_of_an_unknown_job_is_404(client):
    assert client.get("/api/jobs/999/events").status_code == 404


@pytest.mark.parametrize("path", ["/api/jobs/0/events", "/api/jobs/-1/events", "/api/jobs/x/events",
                                  f"/api/jobs/{2**63}/events"])
def test_a_bad_job_id_is_422(client, store, path):
    assert client.get(path).status_code == 422
    assert store.calls == []


@pytest.mark.parametrize("path", ["/api/board", "/api/stats", "/api/jobs/1/events"])
def test_the_reads_need_a_session(settings, store, path):
    anonymous = TestClient(create_app(settings, store), base_url="http://localhost")
    assert anonymous.get(path).status_code == 401
    assert store.calls == []


@pytest.mark.parametrize("path", ["/api/board", "/api/stats", "/api/jobs/1/events"])
def test_a_database_outage_is_503(client, store, path):
    store.fail = Unavailable("the database is not available right now")
    assert client.get(path).status_code == 503


@pytest.mark.parametrize("path", ["/api/board", "/api/stats", "/api/jobs/1/events"])
def test_these_routes_only_read(client, path):
    assert client.post(path, headers=WRITE).status_code == 405
    assert client.delete(path, headers=WRITE).status_code == 405


def test_extra_fields_from_the_store_are_not_leaked(client, monkeypatch):
    monkeypatch.setitem(BOARD["columns"][0]["items"][0], "description", "secret posting text")
    assert "secret posting text" not in client.get("/api/board").text


def test_the_models_accept_what_the_functions_return():
    Board.model_validate(BOARD)
    Stats.model_validate(STATS)
    assert [JobEvent.model_validate(e).to_status for e in EVENTS] == ["drafted", "applied"]
    Stats.model_validate(STATS | {"avg_days_to_reply": None, "replies_measured": 0})     # nothing measured yet
    JobEvent.model_validate(EVENTS[0] | {"source": "backfill"})
    with pytest.raises(ValueError):
        JobEvent.model_validate(EVENTS[0] | {"source": "telegram"})


# ---------------------------------------------------------------- the store

def test_store_calls_the_functions_with_parameters():
    store, conn, _ = store_with([{"e": []}], [{"b": {"columns": []}}], [{"s": {"weekly": []}}])
    assert store.job_events(7) == []
    assert store.board() == {"columns": []}
    assert store.stats() == {"weekly": []}
    assert [c[0] for c in conn.calls] == ["SELECT dashboard_job_events(%s) AS e", "SELECT dashboard_board() AS b",
                                          "SELECT dashboard_stats() AS s"]
    assert conn.calls[0][1] == (7,)


def test_the_store_maps_an_unknown_job():
    from tests.test_store import FakeDbError
    store, _, _ = store_with(FakeDbError("JD404", "no job 5"))
    with pytest.raises(NotFound):
        store.job_events(5)


# ---------------------------------------------------------------- version 3b: notes, contacts, follow-ups

def test_the_board_lists_what_is_due(client):
    body = client.get("/api/board").json()
    assert body["follow_up_days"] == 7 and body["follow_ups_total"] == 3 and body["today"] == "2026-10-12"
    assert body["follow_ups"][0]["reason"] == "waiting" and body["follow_ups"][0]["days_waiting"] == 11
    assert body["follow_ups"][0]["contact_email"] == "priya@example.com"
    assert all("follow_up" in c for col in body["columns"] for c in col["items"])


def test_read_a_job_s_tracking(client, store):
    r = client.get("/api/jobs/1/tracking")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert r.json() == TRACKING
    assert store.calls == [("job_tracking", 1)]
    assert client.get("/api/jobs/999/tracking").status_code == 404


def test_save_sends_only_the_fields_given_and_the_version_read(client, store):
    r = client.put("/api/jobs/1/tracking", headers=WRITE, json={"version": "v1", "notes": "moved to Monday", "contact_email": None})
    assert r.status_code == 200 and r.json()["notes"] == "moved to Monday" and r.json()["version"].endswith("0001Z")
    assert store.calls == [("set_tracking", 1, "v1", {"notes": "moved to Monday", "contact_email": None})]


@pytest.mark.parametrize("blank", ["", "   "])
def test_a_blank_date_clears_it_like_the_text_fields(client, store, blank):
    assert client.put("/api/jobs/1/tracking", headers=WRITE, json={"version": None, "follow_up_on": blank}).status_code == 200
    assert store.calls == [("set_tracking", 1, None, {"follow_up_on": None})]


def test_a_date_goes_to_the_database_as_text(client, store):
    assert client.put("/api/jobs/1/tracking", headers=WRITE, json={"version": None, "follow_up_on": "2026-10-20"}).status_code == 200
    assert store.calls == [("set_tracking", 1, None, {"follow_up_on": "2026-10-20"})]
    store.calls.clear()
    assert client.put("/api/jobs/1/tracking", headers=WRITE, json={"version": None, "follow_up_on": None}).status_code == 200
    assert store.calls == [("set_tracking", 1, None, {"follow_up_on": None})]


@pytest.mark.parametrize("body", [
    {}, {"version": "v1"}, {"notes": 5}, {"notes": "x" * 2001}, {"contact_name": "x" * 101}, {"contact_email": "x" * 201},
    {"referral": "x" * 201}, {"follow_up_on": "31/10/2026"}, {"follow_up_on": 20261020}, {"notes": ["a"]}, {"notes": {"a": 1}}, {"follow_up_on": "2026-02-31"}, {"title": "x"}, {"notes": "a\u0000b"},
    {"version": "v" * 65, "notes": "x"},
])
def test_a_bad_save_is_refused_before_the_database(client, store, body):
    assert client.put("/api/jobs/1/tracking", headers=WRITE, json=body).status_code == 422
    assert store.calls == []


def test_a_save_changed_elsewhere_is_409_with_a_plain_message(client, store):
    store.fail = TrackingChanged("tracking of job 1 changed")
    r = client.put("/api/jobs/1/tracking", headers=WRITE, json={"version": "old", "notes": "x"})
    assert r.status_code == 409 and "changed elsewhere" in r.json()["detail"] and "tracking of job" not in r.json()["detail"]


def test_the_database_s_own_refusal_is_a_422_with_its_reason(client, store):
    store.fail = Invalid("that does not look like an email address")
    r = client.put("/api/jobs/1/tracking", headers=WRITE, json={"version": None, "contact_email": "nope"})
    assert r.status_code == 422 and r.json()["detail"] == "that does not look like an email address"


def test_saving_needs_a_session_and_the_write_header(settings, store, client):
    anonymous = TestClient(create_app(settings, store), base_url="http://localhost")
    assert anonymous.put("/api/jobs/1/tracking", headers=WRITE, json={"notes": "x"}).status_code == 401
    assert client.put("/api/jobs/1/tracking", json={"notes": "x"}).status_code == 403
    assert client.put("/api/follow-ups/days", json={"days": 5}).status_code == 403
    assert not [c for c in store.calls if c[0] in ("set_tracking", "set_follow_up_days")]


def test_the_follow_up_days(client, store):
    r = client.put("/api/follow-ups/days", headers=WRITE, json={"days": 10})
    assert r.status_code == 200 and r.json() == {"days": 10}
    assert store.calls == [("set_follow_up_days", 10)]


@pytest.mark.parametrize("body", [{"days": 0}, {"days": 91}, {"days": "7"}, {"days": 7.5}, {"days": True}, {}, {"days": 7, "x": 1}])
def test_bad_follow_up_days_are_refused(client, store, body):
    assert client.put("/api/follow-ups/days", headers=WRITE, json=body).status_code == 422
    assert store.calls == []


def test_the_models_accept_what_the_functions_return():
    Tracking.model_validate(TRACKING)
    Tracking.model_validate({k: (v if k == "today" else None) for k, v in TRACKING.items()})      # nothing saved yet
    Board.model_validate(BOARD)


def test_store_tracking_calls_the_functions():
    store, conn, _ = store_with([{"t": {"notes": None}}], [{"t": {"notes": "x"}}], [{"d": 9}])
    assert store.job_tracking(4) == {"notes": None}
    assert store.set_tracking(4, "v", {"notes": "x"}) == {"notes": "x"}
    assert store.set_follow_up_days(9) == 9
    assert conn.calls[0] == ("SELECT dashboard_job_tracking(%s) AS t", (4,))
    assert conn.calls[1][0] == "SELECT dashboard_set_tracking(%s, %s, %s::jsonb) AS t" and conn.calls[1][1][:2] == (4, "v")
    assert conn.calls[2] == ("SELECT dashboard_set_follow_up_days(%s) AS d", (9,))


def test_the_store_maps_the_changed_elsewhere_state():
    from tests.test_store import FakeDbError
    store, _, _ = store_with(FakeDbError("JD412", "tracking of job 5 changed"))
    with pytest.raises(TrackingChanged):
        store.set_tracking(5, "old", {"notes": "x"})


# ---------------------------------------------------------------- undo

def test_undo_the_newest_change(client, store):
    r = client.post("/api/jobs/1/undo", headers=WRITE, json={"expected_status": "offer"})
    assert r.status_code == 200 and r.json() == {"id": 1, "status": "shortlisted"} and r.headers["cache-control"] == "no-store"
    assert store.calls == [("undo_status", 1, "offer", None)]


def test_undo_an_entry_picked_in_the_timeline_names_it(client, store):
    assert client.post("/api/jobs/1/undo", headers=WRITE, json={"expected_status": "applied", "event_id": 2}).status_code == 200
    assert store.calls == [("undo_status", 1, "applied", 2)]


@pytest.mark.parametrize("body", [{"expected_status": "offer", "event_id": 2**63}, {"expected_status": "offer", "event_id": -1}, {"expected_status": "offer", "event_id": True},
                                  {}, {"event_id": 2}, {"expected_status": "hired"}, {"expected_status": None}, {"expected_status": "offer", "event_id": 0},
                                  {"expected_status": "offer", "event_id": "2"}, {"expected_status": "offer", "x": 1}, {"expected_status": ["offer"]}])
def test_a_bad_undo_is_refused_before_the_database(client, store, body):
    assert client.post("/api/jobs/1/undo", headers=WRITE, json=body).status_code == 422
    assert store.calls == []


def test_undo_needs_a_session_the_write_header_and_a_valid_job(settings, store, client):
    anonymous = TestClient(create_app(settings, store), base_url="http://localhost")
    assert anonymous.post("/api/jobs/1/undo", headers=WRITE, json={"expected_status": "offer"}).status_code == 401
    assert client.post("/api/jobs/1/undo", json={"expected_status": "offer"}).status_code == 403
    assert client.post("/api/jobs/0/undo", headers=WRITE, json={"expected_status": "offer"}).status_code == 422
    assert client.post("/api/jobs/999/undo", headers=WRITE, json={"expected_status": "offer"}).status_code == 404
    assert client.get("/api/jobs/1/undo").status_code in (404, 405)       # a change is never a GET
    assert [c for c in store.calls if c[0] == "undo_status"] == [("undo_status", 999, "offer", None)]     # only the valid id got that far


def test_an_undo_that_is_too_late_is_a_409_with_a_plain_message(client, store):
    store.fail = HistoryChanged("the history of job 1 changed")
    r = client.post("/api/jobs/1/undo", headers=WRITE, json={"expected_status": "offer"})
    assert r.status_code == 409 and "history of this job changed" in r.json()["detail"] and "job 1" not in r.json()["detail"]


def test_an_undo_the_database_refuses_says_why(client, store):
    store.fail = Invalid("only a change made on the dashboard can be undone here")
    r = client.post("/api/jobs/1/undo", headers=WRITE, json={"expected_status": "offer"})
    assert r.status_code == 422 and r.json()["detail"] == "only a change made on the dashboard can be undone here"


def test_the_timeline_says_which_entry_can_be_undone(client):
    events = client.get("/api/jobs/1/events").json()
    assert [e["undoable"] for e in events] == [False, True]


def test_the_store_calls_the_undo_function_and_maps_its_states():
    from tests.test_store import FakeDbError
    store, conn, _ = store_with([{"r": {"id": 4, "status": "applied"}}])
    assert store.undo_status(4, "offer", None) == {"id": 4, "status": "applied"}
    assert conn.calls == [("SELECT dashboard_undo_status(%s, %s, %s) AS r", (4, "offer", None))]
    store, _, _ = store_with(FakeDbError("JD413", "the history of job 4 changed"))
    with pytest.raises(HistoryChanged):
        store.undo_status(4, "offer", 9)


def test_an_old_database_without_undo_gives_a_clear_503_not_a_crash():
    import psycopg
    from tests.test_store import FakeDbError
    class Missing(FakeDbError, psycopg.errors.UndefinedFunction):
        pass
    store, _, _ = store_with(Missing("42883", "function dashboard_undo_status does not exist"))
    with pytest.raises(Unavailable, match="latest migration"):
        store.undo_status(4, "offer", None)


def test_a_timeline_from_a_database_without_undo_still_validates():
    old = [{k: v for k, v in e.items() if k != "undoable"} for e in EVENTS]
    assert [JobEvent.model_validate(e).undoable for e in old] == [False, False]
