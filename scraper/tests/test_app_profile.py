"""The search profile endpoints (dashboard version 2c): read, save with the CLI's validation, refuse a stale save."""
from __future__ import annotations

import sys
import types
from datetime import datetime, timezone

import pytest

pytest.importorskip("fastapi")
sys.modules.setdefault("jobspy", types.SimpleNamespace(scrape_jobs=lambda **kw: None))   # these tests never scrape

from fastapi.testclient import TestClient  # noqa: E402

import app as A  # noqa: E402
import board_probe  # noqa: E402
import profile_tools  # noqa: E402
import sources  # noqa: E402

NOW = datetime(2026, 10, 12, 9, 30, 0, 123456, tzinfo=timezone.utc)
VERSION = NOW.isoformat()


def row(**over):
    base = {"id": 1, "name": "default", "search_terms": ["backend engineer"], "locations": ["Pune", "Remote"],
            "sources": ["indeed", "greenhouse"], "allow_remote": True, "min_yoe": 0, "max_yoe": 4,
            "include_keywords": [], "exclude_keywords": ["intern"], "exclude_companies": ["Acme"], "max_age_days": 7,
            "score_threshold": 60, "boards": {"greenhouse": ["stripe", {"slug": "notion", "name": "Notion"}]},
            "board_title_words": ["software"], "updated_at": NOW, "edited_by": "cli"}
    base.update(over)
    return base


class Store:
    def __init__(self, current=None):
        self.current = current
        self.saved = None
        self.conflict = False

    def search_profile_row(self, name="default"):
        return self.current

    def save_search_profile(self, name, settings, expected_version, edited_by="dashboard"):
        self.saved = (name, settings, expected_version, edited_by)
        if self.conflict:
            return None
        self.current = {**(self.current or {}), **{k: settings[k] for k in profile_tools.PROFILE_KEYS}, "edited_by": edited_by,
                        "updated_at": datetime(2026, 10, 12, 9, 45, tzinfo=timezone.utc)}
        return self.current


@pytest.fixture
def api(monkeypatch):
    def make(store):
        monkeypatch.setattr(A, "get_store", lambda: store)
        return TestClient(A.app)
    return make


def good(**over):
    p = profile_tools.settings_from_row(row())
    p.update(over)
    return p


# ---------------------------------------------------------------- GET

def test_get_returns_the_profile_a_version_who_wrote_it_and_the_registry(api):
    body = api(Store(row())).get("/profile").json()
    assert body["version"] == VERSION and body["edited_by"] == "cli"
    assert list(body["profile"]) == list(profile_tools.PROFILE_KEYS)
    assert body["profile"]["boards"]["greenhouse"] == ["stripe", {"slug": "notion", "name": "Notion"}]
    reg = body["registry"]
    assert reg["sources"] == list(sources.ALL) and reg["board_systems"] == list(sources.BOARD_SYSTEMS)
    assert reg["groups"]["feeds"] == list(sources.FEEDS) and reg["paid"] == ["linkedin_apify"]
    assert set(reg["descriptions"]) == set(sources.ALL)
    assert reg["limits"]["max_board_slugs"] == profile_tools.MAX_BOARD_SLUGS


def test_a_database_without_migration_017_reads_as_written_by_the_cli(api):
    r = row()
    del r["edited_by"]
    assert api(Store(r)).get("/profile").json()["edited_by"] == "cli"


def test_get_with_no_profile_says_how_to_make_one(api):
    r = api(Store(None)).get("/profile")
    assert r.status_code == 404 and "search-profile apply" in r.json()["detail"]["errors"][0]


# ---------------------------------------------------------------- POST

def test_a_save_is_validated_stored_as_a_dashboard_edit_and_answers_with_the_new_state(api):
    store = Store(row())
    r = api(store).post("/profile", json={"profile": good(search_terms=["  python developer ", "sre"], score_threshold=55), "version": VERSION})
    assert r.status_code == 200
    name, settings, version, by = store.saved
    assert (name, version, by) == ("default", VERSION, "dashboard")
    assert settings["search_terms"] == ["python developer", "sre"] and settings["score_threshold"] == 55   # cleaned like the CLI's file
    body = r.json()
    assert body["edited_by"] == "dashboard" and body["version"] != VERSION and body["profile"]["score_threshold"] == 55


@pytest.mark.parametrize("change, message", [
    ({"search_terms": []}, "at least one job title"),
    ({"locations": []}, "at least one location"),
    ({"sources": ["indeed", "nope"]}, "unknown"),
    ({"score_threshold": 101}, "score_threshold"),
    ({"min_yoe": 9, "max_yoe": 2}, "min_yoe is greater"),
    ({"allow_remote": "yes"}, "allow_remote"),
    ({"boards": {"greenhouse": ["../evil"]}}, "invalid slug"),
    ({"boards": {"workday": ["x"]}}, "unknown system"),
    ({"board_title_words": ["x"] * 51}, "at most 50"),
])
def test_what_the_cli_would_refuse_is_refused_and_nothing_is_saved(api, change, message):
    store = Store(row())
    r = api(store).post("/profile", json={"profile": good(**change), "version": VERSION})
    assert r.status_code == 422 and any(message in e for e in r.json()["detail"]["errors"])
    assert store.saved is None


def test_only_company_boards_with_no_company_is_refused(api):
    store = Store(row())
    r = api(store).post("/profile", json={"profile": good(sources=["greenhouse", "lever"], boards={}), "version": VERSION})
    assert r.status_code == 422 and "collect run would find nothing" in r.json()["detail"]["errors"][0]
    ok = api(store).post("/profile", json={"profile": good(sources=["remotive", "greenhouse"], boards={}), "version": VERSION})
    assert ok.status_code == 200, "a feed needs no company, so boards may be listed but empty"


def test_a_stale_version_is_a_409_and_changes_nothing(api):
    store = Store(row())
    store.conflict = True
    r = api(store).post("/profile", json={"profile": good(), "version": "2026-10-01T00:00:00+00:00"})
    assert r.status_code == 409 and "changed somewhere else" in r.json()["detail"]["errors"][0]


def test_saving_when_there_is_no_profile_is_a_404_not_a_conflict(api):
    store = Store(None)
    store.conflict = True
    assert api(store).post("/profile", json={"profile": good(), "version": VERSION}).status_code == 404


@pytest.mark.parametrize("version", ["", "yesterday", "x" * 65])
def test_a_version_that_is_not_a_timestamp_is_refused(api, version):
    store = Store(row())
    assert api(store).post("/profile", json={"profile": good(), "version": version}).status_code == 422
    assert store.saved is None


def test_the_request_must_have_a_profile_and_a_version(api):
    store = Store(row())
    assert api(store).post("/profile", json={"profile": good()}).status_code == 422
    assert api(store).post("/profile", json={"version": VERSION}).status_code == 422
    assert api(store).post("/profile", json={"profile": "x", "version": VERSION}).status_code == 422


# ---------------------------------------------------------------- boards find

def test_find_returns_the_matches_and_what_it_tried(api, monkeypatch):
    seen = {}

    def fake(name, opener=None, *, timeout, workers):
        seen.update(name=name, timeout=timeout, workers=workers)
        return [{"system": "greenhouse", "slug": "stripe", "jobs": 12}]

    monkeypatch.setattr(board_probe, "boards_find", fake)
    r = api(Store()).post("/boards/find", json={"name": "Stripe Inc"})
    assert r.status_code == 200
    assert r.json() == {"name": "Stripe Inc", "tried": ["stripeinc", "stripe-inc", "stripe", "StripeInc"],
                        "matches": [{"system": "greenhouse", "slug": "stripe", "jobs": 12}]}
    assert seen == {"name": "Stripe Inc", "timeout": 5, "workers": 8}


@pytest.mark.parametrize("name", ["!!!", "   ", "...", "_-_"])
def test_a_name_that_makes_no_slug_is_refused_before_any_request(api, monkeypatch, name):
    monkeypatch.setattr(board_probe, "_probe_open", lambda *a, **k: pytest.fail("no request may be made"))
    r = api(Store()).post("/boards/find", json={"name": name})
    assert r.status_code == 422 and "company name" in r.json()["detail"]["errors"][0]


@pytest.mark.parametrize("body", [{}, {"name": ""}, {"name": "x" * 101}, {"name": 5}])
def test_a_bad_find_request_is_a_422(api, body):
    assert api(Store()).post("/boards/find", json=body).status_code == 422


# ---------------------------------------------------------------- what the review found

@pytest.mark.parametrize("field", ["search_terms", "locations", "include_keywords", "exclude_keywords", "exclude_companies", "board_title_words"])
@pytest.mark.parametrize("bad", ["a\x00b", "line\nbreak", "tab\there", "bell\x07"])
def test_control_characters_in_a_list_are_a_422_not_a_database_error(api, field, bad):
    store = Store(row())
    r = api(store).post("/profile", json={"profile": good(**{field: [bad]}), "version": VERSION})
    assert r.status_code == 422 and any("control characters" in e for e in r.json()["detail"]["errors"])
    assert store.saved is None


@pytest.mark.parametrize("version", ["2026-W01-1", "2026-10-12", "2026-10-12T09:30:00", "2026-10-12 09:30:00+00:00", "now", "2026-10-12T09:30:00.1234567+00:00"])
def test_a_version_must_be_the_exact_form_get_profile_returns(api, version):
    store = Store(row())
    assert api(store).post("/profile", json={"profile": good(), "version": version}).status_code == 422
    assert store.saved is None


def test_the_real_version_format_is_accepted(api):
    for v in ("2026-10-12T09:30:00.123456+00:00", "2026-10-12T09:30:00+05:30", "2026-10-12T09:30:00.5-07:00"):
        assert api(Store(row())).post("/profile", json={"profile": good(), "version": v}).status_code == 200
