"""Version 6f: the email inbox. Routes against a fake store, the store against a fake connection (including how it maps the
database's refusals), and the response models against the shape the SQL function returns."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import create_app
from models import EmailAccepted, EmailCount, EmailInbox
from store import Conflict, EmailChanged, EmailNotFound, Forbidden, Invalid, NotFound, Unavailable
from tests.support import EMAIL_INBOX, PASSWORD, WRITE, email_item
from tests.test_store import store_with


@pytest.fixture
def client(settings, store):
    c = TestClient(create_app(settings, store), base_url="http://localhost")
    assert c.post("/api/login", json={"password": PASSWORD}).status_code == 204
    return c


# ---------------------------------------------------------------- reading

def test_the_inbox_defaults_to_the_pending_suggestions(client, store):
    r = client.get("/api/email")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["enabled"] is True and body["counts"] == {"pending": 2, "accepted": 1, "dismissed": 0} and [i["id"] for i in body["items"]] == [1, 2]
    assert body["items"][1]["needs_ack"] is True and body["items"][1]["flags"] == ["phishing_risk"] and body["items"][0]["can_accept"] is True
    assert store.calls == [("email_inbox", "pending", 50)]


@pytest.mark.parametrize("state", ["pending", "accepted", "dismissed"])
def test_each_state_can_be_asked_for(client, store, state):
    assert client.get(f"/api/email?state={state}&limit=10").status_code == 200
    assert store.calls == [("email_inbox", state, 10)]


@pytest.mark.parametrize("query", ["state=superseded", "state=", "limit=0", "limit=101", "limit=x", "foo=1", "state=pending&state=accepted&x=1"])
def test_a_bad_inbox_query_is_422_and_reaches_no_database(client, store, query):
    assert client.get(f"/api/email?{query}").status_code == 422
    assert store.calls == []


def test_the_count_for_the_menu_badge(client, store):
    r = client.get("/api/email/count")
    assert r.status_code == 200 and r.json() == {"enabled": True, "pending": 2} and r.headers["cache-control"] == "no-store"


def test_the_inbox_needs_a_login(settings, store):
    anonymous = TestClient(create_app(settings, store), base_url="http://localhost")
    for path in ("/api/email", "/api/email/count"):
        assert anonymous.get(path).status_code == 401
    assert anonymous.post("/api/email/1/dismiss", headers=WRITE).status_code == 401
    assert store.calls == []


def test_the_response_models_accept_what_the_sql_function_returns():
    EmailInbox.model_validate(EMAIL_INBOX)
    EmailCount.model_validate({"enabled": False, "pending": 0})
    EmailAccepted.model_validate({"id": 1, "job_id": 3, "status": "interview", "event_id": None})
    assert "sort_at" not in EmailInbox.model_validate(EMAIL_INBOX).items[0].model_dump(), "an internal column is not passed on"


@pytest.mark.parametrize("bad", [{"signal": "hired"}, {"proposed_status": "withdrawn"}, {"proposed_status": "offer declined"}, {"confidence": "certain"},
                                 {"source": "human"}, {"state": "superseded"}])
def test_the_models_refuse_anything_an_email_may_not_propose(bad):
    with pytest.raises(Exception):
        EmailInbox.model_validate({**EMAIL_INBOX, "items": [email_item(1, **bad)]})


# ---------------------------------------------------------------- deciding

def test_accept_makes_the_change_and_names_the_history_entry_undo_is_tied_to(client, store):
    r = client.post("/api/email/7/accept", json={"expected_status": "applied"}, headers=WRITE)
    assert r.status_code == 200 and r.json() == {"id": 7, "job_id": 3, "status": "interview", "event_id": 42}
    assert store.calls == [("email_accept", 7, "applied", False)]


def test_accept_passes_the_second_look_on(client, store):
    client.post("/api/email/7/accept", json={"expected_status": "applied", "acknowledged": True}, headers=WRITE)
    assert store.calls == [("email_accept", 7, "applied", True)]


@pytest.mark.parametrize("body", [{}, {"expected_status": "banana"}, {"expected_status": "applied", "acknowledged": "yes"}, {"expected_status": "applied", "acknowledged": 1},
                                  {"expected_status": "applied", "status": "offer"}, {"expected_status": "applied", "note": "x"}])
def test_accept_validates_its_body_and_takes_no_status_from_the_page(client, store, body):
    assert client.post("/api/email/7/accept", json=body, headers=WRITE).status_code == 422
    assert store.calls == []


@pytest.mark.parametrize("path", ["/api/email/0/accept", "/api/email/-1/dismiss", "/api/email/x/restore", f"/api/email/{2**63}/accept"])
def test_a_bad_suggestion_id_is_422(client, store, path):
    assert client.post(path, json={"expected_status": "applied"}, headers=WRITE).status_code == 422
    assert store.calls == []


def test_dismiss_and_restore(client, store):
    assert client.post("/api/email/7/dismiss", headers=WRITE).json() == {"id": 7, "state": "dismissed"}
    assert client.post("/api/email/7/restore", headers=WRITE).json() == {"id": 7, "state": "pending"}
    assert store.calls == [("email_dismiss", 7), ("email_restore", 7)]


@pytest.mark.parametrize("path,body", [("/api/email/7/accept", {"expected_status": "applied"}), ("/api/email/7/dismiss", None), ("/api/email/7/restore", None)])
def test_every_decision_needs_the_csrf_header(client, store, path, body):
    r = client.post(path, json=body) if body else client.post(path)
    assert r.status_code == 403 and store.calls == []


@pytest.mark.parametrize("error,status,words", [
    (EmailNotFound("no suggestion 7"), 404, "suggestion not found"),
    (EmailChanged("suggestion 7 was already accepted"), 409, "already accepted"),
    (EmailChanged("another suggestion for this job and status is already waiting"), 409, "Another suggestion for this job and status is already waiting"),
    (Conflict("the job changed since this email was read", "rejected"), 409, "changed elsewhere"),
    (Forbidden("job 3 is scam-blocked"), 403, "scam-blocked"),
    (Invalid("this suggestion needs a second look: check the sender, then accept it again"), 422, "second look"),
    (NotFound("no job 3"), 404, "job not found"),
    (Unavailable("the database is not available right now"), 503, "not available")])
def test_the_refusals_become_plain_answers(client, store, error, status, words):
    store.fail = error
    r = client.post("/api/email/7/accept", json={"expected_status": "applied"}, headers=WRITE)
    assert r.status_code == status and words in r.json()["detail"]
    if isinstance(error, Conflict):
        assert r.json()["current_status"] == "rejected", "the page can show what the job is now"


# ---------------------------------------------------------------- the store

def test_the_store_calls_only_the_inbox_functions():
    store, conn, _ = store_with([{"i": {"items": []}}])
    assert store.email_inbox("pending", 50) == {"items": []}
    assert conn.calls == [("SELECT dashboard_email_inbox(%s, %s) AS i", ("pending", 50))]
    store, conn, _ = store_with([{"r": {"id": 7}}])
    store.email_accept(7, "applied", True)
    assert conn.calls == [("SELECT dashboard_email_accept(%s, %s, %s) AS r", (7, "applied", True))]
    store, conn, _ = store_with([{"r": {"id": 7}}])
    store.email_dismiss(7)
    assert conn.calls[0][0] == "SELECT dashboard_email_dismiss(%s) AS r"
    store, conn, _ = store_with([{"r": {"id": 7}}])
    store.email_restore(7)
    assert conn.calls[0][0] == "SELECT dashboard_email_restore(%s) AS r"
    store, conn, _ = store_with([{"c": {"enabled": True, "pending": 1}}])
    assert store.email_count() == {"enabled": True, "pending": 1}


def test_the_stores_connection_is_closed_after_a_decision():
    store, conn, _ = store_with([{"r": {"id": 7}}])
    store.email_dismiss(7)
    assert conn.closed


# ---- how the store maps the database's own refusals (SQLSTATEs JD404, JD409, JD403, JD422)

from tests.test_store import FakeDbError  # noqa: E402


def test_a_missing_suggestion_is_told_apart_from_a_missing_job():
    store, _, _ = store_with(FakeDbError("JD404", "no suggestion 7"))
    with pytest.raises(EmailNotFound):
        store.email_accept(7, "applied", False)
    store, _, _ = store_with(FakeDbError("JD404", "no job 3"))
    with pytest.raises(NotFound) as e:
        store.email_accept(7, "applied", False)
    assert not isinstance(e.value, EmailNotFound)


def test_an_already_decided_suggestion_is_told_apart_from_a_job_that_moved():
    store, _, _ = store_with(FakeDbError("JD409", "suggestion 7 was already accepted", None))
    with pytest.raises(EmailChanged):
        store.email_accept(7, "applied", False)
    store, _, _ = store_with(FakeDbError("JD409", "the job changed since this email was read", "rejected"))
    with pytest.raises(Conflict) as e:
        store.email_accept(7, "applied", False)
    assert e.value.current == "rejected"


@pytest.mark.parametrize("state,exc", [("JD403", Forbidden), ("JD422", Invalid)])
def test_a_scam_blocked_job_and_a_missing_second_look_are_refusals(state, exc):
    store, _, _ = store_with(FakeDbError(state, "refused"))
    with pytest.raises(exc):
        store.email_accept(7, "applied", False)


def test_dismiss_and_restore_map_the_same_way():
    store, _, _ = store_with(FakeDbError("JD409", "suggestion 7 was already dismissed", None))
    with pytest.raises(EmailChanged):
        store.email_dismiss(7)
    store, _, _ = store_with(FakeDbError("JD409", "another suggestion for this job and status is already waiting", None))
    with pytest.raises(EmailChanged):
        store.email_restore(7)
    store, _, _ = store_with(FakeDbError("JD404", "no suggestion 7"))
    with pytest.raises(EmailNotFound):
        store.email_restore(7)


def test_an_image_ahead_of_the_database_says_to_migrate():
    store, _, _ = store_with(FakeDbError("42883", "function dashboard_email_inbox does not exist"))
    with pytest.raises(Unavailable, match="migrate"):
        store.email_inbox("pending", 50)


def test_two_changes_to_one_job_at_the_same_moment_are_a_try_again_not_a_crash():
    for state in ("40P01", "40001"):
        store, _, _ = store_with(FakeDbError(state, "deadlock detected"))
        with pytest.raises(Unavailable, match="try again"):
            store.email_accept(7, "applied", False)
