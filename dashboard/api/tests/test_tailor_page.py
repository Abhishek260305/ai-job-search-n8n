"""Version 3c: tailor one job on request. Routes against a fake scraper client; the client against a fake connection."""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app import create_app
from models import TailorEstimate, TailorState
from store import Invalid, Unavailable
from tests.support import PASSWORD, TAILOR_DONE, TAILOR_ESTIMATE, WRITE, FakeScraper


@pytest.fixture
def scraper():
    return FakeScraper()


@pytest.fixture
def client(settings, store, scraper):
    c = TestClient(create_app(settings, store, scraper=scraper), base_url="http://localhost")
    assert c.post("/api/login", json={"password": PASSWORD}).status_code == 204
    return c


def test_the_estimate_is_passed_on_as_the_scraper_gave_it(client, scraper):
    r = client.get("/api/jobs/4/tailor/estimate")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["job_id"] == 4 and body["can_run"] is True and body["at_most_usd"] == 0.31 and body["typical_usd"] == 0.047
    assert body["budget"]["day_remaining"] == 1.4 and body["existing"] is None and body["variant"] == "tailored"
    assert scraper.calls == [("tailor_estimate", 4)]


def test_a_refused_estimate_carries_the_reason(client, scraper, monkeypatch):
    monkeypatch.setitem(TAILOR_ESTIMATE, "can_run", False)
    monkeypatch.setitem(TAILOR_ESTIMATE, "reason", "tailoring is switched off (jobsearch features set tailoring on)")
    body = client.get("/api/jobs/4/tailor/estimate").json()
    assert body["can_run"] is False and "switched off" in body["reason"]


def test_start_sends_the_job_and_the_agreed_cost(client, scraper):
    r = client.post("/api/jobs/4/tailor", headers=WRITE, json={"max_cost_usd": 0.31})
    assert r.status_code == 200 and r.json()["state"] == "running"
    assert scraper.calls == [("tailor_start", 4, 0.31)]


def test_start_without_the_agreed_cost_is_refused_because_the_page_never_starts_one_blind(client, scraper):
    assert client.post("/api/jobs/4/tailor", headers=WRITE).status_code == 422
    assert client.post("/api/jobs/4/tailor", headers=WRITE, json={}).status_code == 422
    assert scraper.calls == []


@pytest.mark.parametrize("body", [{}, {"max_cost_usd": -1}, {"max_cost_usd": 1000}, {"max_cost_usd": "a lot"}, {"max_cost_usd": True},
                                  {"job_id": 5}, {"max_cost_usd": [1]}])
def test_a_bad_start_is_refused_before_the_scraper(client, scraper, body):
    assert client.post("/api/jobs/4/tailor", headers=WRITE, json=body).status_code == 422
    assert scraper.calls == []


def test_starting_needs_a_session_and_the_write_header(settings, store, scraper, client):
    anonymous = TestClient(create_app(settings, store, scraper=scraper), base_url="http://localhost")
    assert anonymous.post("/api/jobs/4/tailor", headers=WRITE, json={"max_cost_usd": 1}).status_code == 401
    assert client.post("/api/jobs/4/tailor", json={"max_cost_usd": 1}).status_code == 403
    assert client.post("/api/jobs/0/tailor", headers=WRITE, json={"max_cost_usd": 1}).status_code == 422
    assert scraper.calls == []


def test_a_refusal_from_the_scraper_is_a_422_with_its_words(client, scraper):
    scraper.fail = Invalid("another job is being tailored right now: wait for it to finish")
    r = client.post("/api/jobs/4/tailor", headers=WRITE, json={"max_cost_usd": 0.31})
    assert r.status_code == 422 and r.json()["detail"] == "another job is being tailored right now: wait for it to finish"


def test_an_unreachable_scraper_is_a_503_for_every_call(client, scraper):
    scraper.fail = Unavailable("the scraper is not reachable right now")
    assert client.get("/api/jobs/4/tailor/estimate").status_code == 503
    assert client.get("/api/jobs/4/tailor").status_code == 503
    assert client.post("/api/jobs/4/tailor", headers=WRITE, json={"max_cost_usd": 0.31}).status_code == 503


def test_the_progress_of_a_run(client, scraper):
    assert client.get("/api/jobs/4/tailor").json() == {"state": "idle", "started_at": None, "finished_at": None, "result": None, "error": None, "cost_usd": None}
    scraper.tailor_state = TAILOR_DONE
    body = client.get("/api/jobs/4/tailor").json()
    assert body["state"] == "done" and body["result"]["status"] == "drafted" and body["result"]["cost_usd"] == 0.047
    assert body["result"]["attention"] == ["the resume is 2 pages (limit 1)"] and body["result"]["requirements"]["matched"] == 4
    assert client.get("/api/jobs/4/tailor", headers={}).headers["cache-control"] == "no-store"


def test_a_failed_run_and_a_run_that_could_not_afford_itself(client, scraper):
    scraper.tailor_state = {"state": "failed", "started_at": 1.0, "finished_at": 2.0, "error": "the PDF service is down", "cost_usd": 0.0184}
    body = client.get("/api/jobs/4/tailor").json()
    assert body["error"] == "the PDF service is down" and body["cost_usd"] == 0.0184
    scraper.tailor_state = {"state": "done", "started_at": 1.0, "finished_at": 2.0, "result": {"job_id": 4, "status": "budget_paused", "reason": "not enough budget left for this job"}}
    body = client.get("/api/jobs/4/tailor").json()
    assert body["result"]["status"] == "budget_paused" and body["result"]["attention"] == [] and body["result"]["cost_usd"] is None


def test_nothing_unknown_from_the_scraper_reaches_the_page(client, scraper):
    scraper.tailor_state = {**TAILOR_DONE, "result": {**TAILOR_DONE["result"], "files_dir": "/output/applications/1-x", "audit": {"hard": ["x"]}}}
    text = client.get("/api/jobs/4/tailor").text
    assert "files_dir" not in text and "/output" not in text and "audit" not in text


def test_the_models_accept_what_the_scraper_returns():
    TailorEstimate.model_validate(TAILOR_ESTIMATE)
    TailorEstimate.model_validate({**TAILOR_ESTIMATE, "variant": "base", "uses_model": False, "at_most_usd": 0.0,
                                   "existing": {"status": "drafted", "has_resume": True, "has_cover_letter": False}})
    TailorState.model_validate(TAILOR_DONE)
    TailorState.model_validate({"state": "idle"})
    with pytest.raises(ValueError):
        TailorState.model_validate({"state": "paused"})


# ---------------------------------------------------------------- the client (fixed paths; a 409 is the scraper's own refusal)

def test_the_client_uses_fixed_paths_and_a_409_is_the_scrapers_refusal_not_a_profile_change():
    from tests.test_scraper_client import client_with, http_error
    c, seen = client_with(b'{"state": "idle"}')
    c.tailor_estimate(4), c.tailor_start(4, 0.31), c.tailor_start(5, None), c.tailor_status(4)
    assert [(m, u) for m, u, *_ in seen] == [
        ("GET", "http://scraper:8000/tailor/estimate?job_id=4"), ("POST", "http://scraper:8000/tailor/start"),
        ("POST", "http://scraper:8000/tailor/start"), ("GET", "http://scraper:8000/tailor/status?job_id=4")]
    assert json.loads(seen[1][2]) == {"job_id": 4, "max_cost_usd": 0.31} and json.loads(seen[2][2]) == {"job_id": 5}
    c2, _ = client_with(http_error(409, b'{"detail": {"errors": ["this job was tailored a moment ago: wait about 90 seconds before doing it again"]}}'))
    with pytest.raises(Invalid, match="a moment ago"):
        c2.tailor_start(4, None)
    c3, _ = client_with(http_error(422, b'{"detail": {"errors": ["job 4 is scam_blocked (score 3); not tailored"]}}'))
    with pytest.raises(Invalid, match="scam_blocked"):
        c3.tailor_start(4, None)


def test_the_scrapers_400_for_a_missing_resume_reaches_the_page_in_its_own_words():
    from tests.test_scraper_client import client_with, http_error
    c, _ = client_with(http_error(400, b'{"detail": {"errors": ["resume not found at /config/resume.json: run /setup"]}}'))
    with pytest.raises(Invalid, match="run /setup"):
        c.tailor_estimate(4)


def test_a_scraper_without_the_endpoints_is_a_503_not_a_job_not_found():
    from tests.test_scraper_client import client_with, http_error
    c, _ = client_with(http_error(404, b'{"detail": "Not Found"}'))
    with pytest.raises(Unavailable, match="does not have that endpoint"):
        c.tailor_estimate(4)
