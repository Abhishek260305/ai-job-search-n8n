"""Version 2c: the search profile routes against a fake scraper client."""
from __future__ import annotations

import copy

import pytest
from fastapi.testclient import TestClient

from app import create_app
from store import Invalid, Unavailable
from tests.support import PASSWORD, SCRAPER_PROFILE, WRITE, FakeScraper


@pytest.fixture
def scraper():
    return FakeScraper()


@pytest.fixture
def client(settings, store, scraper):
    c = TestClient(create_app(settings, store, scraper=scraper), base_url="http://localhost")
    assert c.post("/api/login", json={"password": PASSWORD}).status_code == 204
    return c


def profile_in(**over):
    """What the page sends: the profile with every company entry as {"slug", "name"}."""
    p = copy.deepcopy(SCRAPER_PROFILE["profile"])
    p["boards"] = {s: [{"slug": e, "name": None} if isinstance(e, str) else e for e in entries] for s, entries in p["boards"].items()}
    p.update(over)
    return p


def save(client, profile=None, version=SCRAPER_PROFILE["version"], headers=WRITE, **kw):
    return client.put("/api/search", headers=headers, json={"profile": profile if profile is not None else profile_in(), "version": version, **kw})


# ---------------------------------------------------------------- read

def test_the_profile_is_returned_with_one_shape_for_every_company(client):
    r = client.get("/api/search")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["version"] == SCRAPER_PROFILE["version"] and body["edited_by"] == "cli"
    assert body["profile"]["boards"]["greenhouse"] == [{"slug": "stripe", "name": None}, {"slug": "notion", "name": "Notion"}]
    assert body["profile"]["sources"] == ["indeed", "greenhouse"] and body["profile"]["score_threshold"] == 60
    assert body["registry"]["board_systems"][0] == "greenhouse" and body["registry"]["paid"] == ["linkedin_apify"]


def test_reading_needs_a_session(settings, store, scraper):
    assert TestClient(create_app(settings, store, scraper=scraper), base_url="http://localhost").get("/api/search").status_code == 401
    assert scraper.calls == []


def test_an_unreachable_scraper_is_a_503(client, scraper):
    scraper.fail = Unavailable("the scraper is not reachable right now")
    assert client.get("/api/search").status_code == 503


# ---------------------------------------------------------------- save

def test_a_save_sends_bare_slugs_and_named_entries_in_the_files_format_with_the_version(client, scraper):
    r = save(client, profile_in(score_threshold=55, boards={"greenhouse": [{"slug": "stripe", "name": None}, {"slug": "notion", "name": "Notion"}],
                                                         "lever": [{"slug": "spotify", "name": ""}]}))
    assert r.status_code == 200
    [(call, profile, version)] = scraper.writes()
    assert call == "save_profile" and version == SCRAPER_PROFILE["version"]
    assert profile["boards"] == {"greenhouse": ["stripe", {"slug": "notion", "name": "Notion"}], "lever": ["spotify"]}
    assert profile["score_threshold"] == 55 and list(profile) == list(SCRAPER_PROFILE["profile"])
    body = r.json()
    assert body["edited_by"] == "dashboard" and body["version"] != SCRAPER_PROFILE["version"]
    assert body["profile"]["boards"]["lever"] == [{"slug": "spotify", "name": None}]


def test_the_scrapers_checks_are_shown_as_they_are(client, scraper):
    scraper.fail = Invalid("search_terms: at least one job title or keyword is required; locations: at least one location is required")
    r = save(client, profile_in(search_terms=[], locations=[]))
    assert r.status_code == 422 and "at least one job title" in r.json()["detail"] and "at least one location" in r.json()["detail"]


def test_a_save_on_an_out_of_date_read_is_a_409_with_the_reason(client, scraper):
    scraper.stale = True
    r = save(client)
    assert r.status_code == 409 and "changed somewhere else" in r.json()["detail"]


def test_a_saved_profile_is_not_reported_as_unsaved_when_only_the_reload_fails(client, scraper):
    original = scraper.profile

    def flaky():
        if scraper.writes():
            raise Unavailable("the scraper is not reachable right now")
        return original()

    scraper.profile = flaky
    r = save(client)
    assert r.status_code == 503 and "was saved" in r.json()["detail"] and "refresh" in r.json()["detail"]


@pytest.mark.parametrize("change", [
    {"extra": 1}, {"search_terms": "backend"}, {"search_terms": None}, {"allow_remote": "yes"}, {"allow_remote": 1},
    {"min_yoe": "2"}, {"min_yoe": 2.5}, {"max_age_days": True}, {"score_threshold": "70"},
    {"boards": {"workday": [{"slug": "x", "name": None}]}}, {"boards": {"greenhouse": ["stripe"]}},
    {"boards": {"greenhouse": [{"slug": "x", "name": None, "extra": 1}]}}, {"boards": {"greenhouse": [{"name": "no slug"}]}},
    {"search_terms": ["x"] * 101}, {"search_terms": ["x" * 201]}, {"sources": ["x"] * 41}, {"board_title_words": ["x" * 101]},
])
def test_a_profile_with_the_wrong_shape_is_a_422_before_the_scraper(client, scraper, change):
    r = save(client, profile_in(**change)) if "extra" not in change else save(client, {**profile_in(), **change})
    assert r.status_code == 422
    assert scraper.writes() == []


def test_a_missing_field_is_refused(client, scraper):
    p = profile_in()
    del p["boards"]
    assert save(client, p).status_code == 422 and scraper.writes() == []


@pytest.mark.parametrize("extra", [{"version": ""}, {"version": "x" * 65}])
def test_a_missing_or_long_version_is_refused(client, scraper, extra):
    r = client.put("/api/search", headers=WRITE, json={"profile": profile_in(), **extra})
    assert r.status_code == 422 and scraper.writes() == []
    assert client.put("/api/search", headers=WRITE, json={"profile": profile_in()}).status_code == 422


def test_a_save_needs_a_session_the_header_and_a_local_origin(client, settings, store, scraper):
    assert save(client, headers={}).status_code == 403
    assert save(client, headers=WRITE | {"Origin": "http://evil.example"}).status_code == 403
    anonymous = TestClient(create_app(settings, store, scraper=scraper), base_url="http://localhost")
    assert save(anonymous).status_code == 401
    assert scraper.writes() == []


def test_a_422_does_not_echo_what_was_sent(client):
    r = save(client, profile_in(search_terms="hunter2-secret"))
    assert r.status_code == 422 and "hunter2-secret" not in r.text


# ---------------------------------------------------------------- find a company

def test_find_forwards_the_name_and_returns_the_matches(client, scraper):
    scraper.found = {"name": "x", "tried": ["stripe", "Stripe"], "matches": [{"system": "greenhouse", "slug": "stripe", "jobs": 12}]}
    r = client.post("/api/search/boards/find", headers=WRITE, json={"name": "Stripe"})
    assert r.status_code == 200 and r.json() == {"name": "Stripe", "tried": ["stripe", "Stripe"],
                                                 "matches": [{"system": "greenhouse", "slug": "stripe", "jobs": 12}]}
    assert scraper.calls[-1] == ("find_board", "Stripe")


@pytest.mark.parametrize("body", [{}, {"name": ""}, {"name": "x" * 101}, {"name": 5}, {"name": "a", "extra": 1}])
def test_a_bad_find_request_is_refused_before_the_scraper(client, scraper, body):
    assert client.post("/api/search/boards/find", headers=WRITE, json=body).status_code == 422
    assert scraper.calls == []


def test_find_needs_the_session_and_the_header(client, settings, store, scraper):
    assert client.post("/api/search/boards/find", json={"name": "x"}).status_code == 403
    anonymous = TestClient(create_app(settings, store, scraper=scraper), base_url="http://localhost")
    assert anonymous.post("/api/search/boards/find", headers=WRITE, json={"name": "x"}).status_code == 401
    assert scraper.calls == []


def test_a_name_the_scraper_cannot_use_is_shown_as_its_message(client, scraper):
    scraper.fail = Invalid("that company name does not make a usable slug: give a shorter name, for example: boards find Stripe")
    r = client.post("/api/search/boards/find", headers=WRITE, json={"name": "!!!"})
    assert r.status_code == 422 and "usable slug" in r.json()["detail"]
