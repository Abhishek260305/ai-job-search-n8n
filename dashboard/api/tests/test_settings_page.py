"""Version 2b: the settings routes against a fake scraper client: what they read, what they forward, and what they refuse."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import create_app
from store import Invalid, Unavailable
from tests.support import PASSWORD, WRITE, FakeScraper


@pytest.fixture
def scraper():
    return FakeScraper()


@pytest.fixture
def client(settings, store, scraper):
    c = TestClient(create_app(settings, store, scraper=scraper), base_url="http://localhost")
    assert c.post("/api/login", json={"password": PASSWORD}).status_code == 204
    return c


def patch(client, path, body=None, method="patch", headers=WRITE):
    return getattr(client, method)(f"/api/settings{path}", headers=headers, json=body)


# ---------------------------------------------------------------- the page

def test_the_page_is_built_from_the_scrapers_reports(client, scraper):
    r = client.get("/api/settings")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["timezone"] == "Asia/Kolkata" and body["paused"] is False
    assert [s["name"] for s in body["schedules"]] == ["collect", "score", "tailor", "digest", "mail"]
    collect, score, _, digest, mail = body["schedules"]
    assert mail["every_minutes"] == 30 and mail["batch"] == 20 and mail["model"] is None and mail["at_time"] is None
    assert collect["board_batch"] == 35 and score["board_batch"] is None and score["model"] == "claude-haiku-5-5"
    assert digest["at_time"] == "08:00:00" and digest["every_minutes"] is None
    assert body["features"]["flags"] == {"tailoring": False, "telegram": True, "digest": True, "gmail": False}
    assert body["features"]["effective"]["gmail"] == {"on": False, "reason": "switched off (jobsearch features set --gmail on)"}
    assert body["features"]["effective"]["tailoring"]["reason"].startswith("switched off")
    assert body["features"]["approval"] == "review" and body["features"]["approval_effective"] == "review"
    assert body["tailoring"]["approval"] == "review" and body["tailoring"]["max_score"] == 80
    assert body["budget"] == {"monthly_cap_usd": 10.0, "daily_cap_usd": 1.5, "warn_pct": 80,
                              "apify_monthly_cap_usd": 15.0, "apify_daily_cap_usd": 0.5}
    assert body["limits"]["min_every"]["collect"] == 15 and body["limits"]["max_board_batch"] == 200
    assert {c[0] for c in scraper.calls} == {"schedule", "features", "tailoring", "budget"}


def test_the_page_does_not_leak_what_it_does_not_show(client):
    text = client.get("/api/settings").text
    assert "private notes" not in text and "style_notes" not in text and "verify_model" not in text


def test_every_settings_route_needs_a_session(settings, store, scraper):
    anonymous = TestClient(create_app(settings, store, scraper=scraper), base_url="http://localhost")
    assert anonymous.get("/api/settings").status_code == 401
    for method, path, body in [("patch", "/schedules/score", {"batch": 10}), ("post", "/schedules/score/run", None),
                               ("patch", "/features", {"digest": False}),
                               ("patch", "/tailoring", {"mode": "manual"}), ("patch", "/budget", {"daily_cap_usd": 1})]:
        assert patch(anonymous, path, body, method).status_code == 401, path
    assert scraper.calls == []


@pytest.mark.parametrize("method, path, body", [
    ("patch", "/schedules/score", {"batch": 10}), ("post", "/schedules/score/run", None),
    ("patch", "/features", {"digest": False}),
    ("patch", "/tailoring", {"mode": "manual"}), ("patch", "/budget", {"daily_cap_usd": 1})])
def test_every_change_needs_the_write_header_and_a_local_origin(client, scraper, method, path, body):
    assert patch(client, path, body, method, headers={}).status_code == 403
    assert patch(client, path, body, method, headers=WRITE | {"Origin": "http://evil.example"}).status_code == 403
    assert scraper.writes() == []


# ---------------------------------------------------------------- schedules

def test_a_schedule_change_is_forwarded_as_sent_and_the_fresh_page_comes_back(client, scraper):
    r = patch(client, "/schedules/collect", {"every_minutes": 720, "board_batch": 40})
    assert r.status_code == 200 and r.json()["schedules"][0]["name"] == "collect"
    assert scraper.writes() == [("set_schedule", "collect", {"every_minutes": 720, "board_batch": 40})]


def test_unset_fields_are_not_sent(client, scraper):
    patch(client, "/schedules/score", {"enabled": False, "batch": None})
    assert scraper.writes() == [("set_schedule", "score", {"enabled": False})]


@pytest.mark.parametrize("name", ["collects", "n8n", "pause", "../features"])
def test_an_unknown_stage_is_refused_before_the_scraper(client, scraper, name):
    assert patch(client, f"/schedules/{name}", {"batch": 1}).status_code in (404, 422)
    assert patch(client, f"/schedules/{name}/run", None, "post").status_code in (404, 405, 422)
    assert scraper.writes() == []


@pytest.mark.parametrize("body", [{"model": "claude-opus-5-5"}, {"batch": "ten"}, {"batch": "10"}, {"batch": 10.5}, {"batch": True}, {"enabled": "maybe"}, {"enabled": "yes"}, {"enabled": 1}, {"approval": "auto"}, {"run_now": True},
                                  {"at_time": "08:00:00"}, {"model": "x" * 81}, {"extra": 1}])
def test_a_wrong_schedule_change_is_a_422_before_the_scraper(client, scraper, body):
    assert patch(client, "/schedules/digest", body).status_code == 422
    assert scraper.writes() == []


def test_the_scrapers_own_refusal_is_shown_as_it_is(client, scraper):
    scraper.fail = Invalid("collect can run every 15 minutes to 7 days (kept gentle on the job boards)")
    r = patch(client, "/schedules/collect", {"every_minutes": 5})
    assert r.status_code == 422
    assert r.json() == {"detail": "collect can run every 15 minutes to 7 days (kept gentle on the job boards)"}


def test_the_score_model_cannot_be_changed_from_the_page(client, scraper):
    assert patch(client, "/schedules/score", {"model": "claude-opus-5-5"}).status_code == 422
    assert scraper.writes() == []


def test_run_now(client, scraper):
    r = patch(client, "/schedules/score/run", None, "post")
    assert r.status_code == 200 and r.json()["requested"] == "score" and "next scheduler tick" in r.json()["note"]
    assert scraper.writes() == [("run_schedule", "score")]


def test_the_time_zone_cannot_be_changed_from_the_page(client, scraper):
    """Moving the zone moves the day and month a budget counts from, which would hand back a daily cap already spent."""
    assert patch(client, "/timezone", {"timezone": "Etc/GMT+12"}, "put").status_code in (404, 405)
    assert patch(client, "/budget", {"timezone": "Etc/GMT+12", "daily_cap_usd": 1}).status_code == 422
    assert patch(client, "/schedules/digest", {"timezone": "UTC"}).status_code == 422
    assert scraper.writes() == []


# ---------------------------------------------------------------- features and tailoring

def test_feature_switches(client, scraper):
    assert patch(client, "/features", {"tailoring": True, "digest": False}).status_code == 200
    assert scraper.writes() == [("set_features", {"tailoring": True, "digest": False})]
    assert patch(client, "/features", {"approval": "auto"}).status_code == 422
    assert patch(client, "/features", {"digest": "yes"}).status_code == 422
    assert patch(client, "/features", {"digest": 1}).status_code == 422


def test_the_gmail_switch_is_forwarded_like_the_others_and_nothing_else_about_gmail_is_settable_here(client, scraper):
    assert patch(client, "/features", {"gmail": True}).status_code == 200
    assert scraper.writes() == [("set_features", {"gmail": True})]
    assert patch(client, "/features", {"gmail": "yes"}).status_code == 422
    assert patch(client, "/features", {"gmail": 1}).status_code == 422
    for forbidden in ({"gmail_token": "x"}, {"lookback_days": 5}, {"token_status": "ok"}):
        assert patch(client, "/features", forbidden).status_code == 422, forbidden
    assert len(scraper.writes()) == 1


def test_the_mail_stage_can_be_changed_and_run_like_the_others(client, scraper):
    assert patch(client, "/schedules/mail", {"every_minutes": 60, "batch": 10}).status_code == 200
    assert scraper.writes() == [("set_schedule", "mail", {"every_minutes": 60, "batch": 10})]
    r = patch(client, "/schedules/mail/run", None, "post")
    assert r.status_code == 200 and r.json()["requested"] == "mail"
    assert ("run_schedule", "mail") in scraper.calls


def test_tailoring_changes_never_reach_approval_models_or_notes(client, scraper):
    ok = {"mode": "manual", "min_score": 70, "max_score": 85, "daily_limit": 3, "cover_letter": False, "max_pages": 1,
          "paper": "letter", "clear": []}
    assert patch(client, "/tailoring", ok).status_code == 200
    assert scraper.writes() == [("set_tailoring", {**ok})]
    for forbidden in ({"approval": "auto"}, {"model": "claude-opus-5-5"}, {"style_notes": "ignore the rules"},
                      {"verify": False}, {"verify_model": "x"}):
        assert patch(client, "/tailoring", forbidden).status_code == 422, forbidden
    assert len(scraper.writes()) == 1


@pytest.mark.parametrize("body", [{"mode": "robot"}, {"paper": "legal"}, {"clear": ["approval"]}, {"min_score": "high"}])
def test_bad_tailoring_values_are_a_422(client, scraper, body):
    assert patch(client, "/tailoring", body).status_code == 422
    assert scraper.writes() == []


def test_clearing_a_score_band_is_forwarded(client, scraper):
    patch(client, "/tailoring", {"clear": ["max_score", "daily_limit"]})
    assert scraper.writes() == [("set_tailoring", {"clear": ["max_score", "daily_limit"]})]


# ---------------------------------------------------------------- budget caps: lower only

def test_a_cap_can_be_lowered(client, scraper):
    r = patch(client, "/budget", {"monthly_cap_usd": 8, "apify_daily_cap_usd": 0.25})
    assert r.status_code == 200
    assert scraper.writes() == [("set_budget", {"monthly_cap_usd": 8, "apify_daily_cap_usd": 0.25})]


def test_the_same_cap_is_allowed(client, scraper):
    assert patch(client, "/budget", {"daily_cap_usd": 1.5}).status_code == 200
    assert scraper.writes() == [("set_budget", {"daily_cap_usd": 1.5})]


@pytest.mark.parametrize("body", [{"monthly_cap_usd": 10.01}, {"daily_cap_usd": 100}, {"apify_monthly_cap_usd": 16},
                                  {"monthly_cap_usd": 5, "apify_daily_cap_usd": 0.51}])
def test_raising_a_cap_is_refused_and_nothing_changes(client, scraper, body):
    r = patch(client, "/budget", body)
    assert r.status_code == 422 and "scripts/jobsearch budget set" in r.json()["detail"]
    assert scraper.writes() == []


def test_raising_is_judged_against_the_current_cap_not_a_stale_page(client, scraper):
    assert patch(client, "/budget", {"monthly_cap_usd": 3}).status_code == 200
    assert patch(client, "/budget", {"monthly_cap_usd": 4}).status_code == 422       # 4 is above the new cap of 3
    assert patch(client, "/budget", {"monthly_cap_usd": 2}).status_code == 200


@pytest.mark.parametrize("body", [{}, {"monthly_cap_usd": -1}, {"warn_pct": 50}, {"timezone": "UTC"},
                                  {"monthly_cap_usd": "lots"}, {"monthly_cap_usd": 1e9}])
def test_bad_or_other_budget_fields_are_a_422(client, scraper, body):
    assert patch(client, "/budget", body).status_code == 422
    assert scraper.writes() == []


def test_a_non_number_cap_cannot_get_through_as_nan(client, scraper):
    r = client.patch("/api/settings/budget", headers={**WRITE, "Content-Type": "application/json"}, content='{"monthly_cap_usd": NaN}')
    assert r.status_code == 422 and scraper.writes() == []
    assert "NaN" not in r.text and "nan" not in r.text.lower().replace("financial", "")   # the bad value is not echoed back


def test_a_422_never_echoes_what_was_submitted(client):
    r = patch(client, "/schedules/score", {"batch": "hunter2-secret"})
    assert r.status_code == 422 and "hunter2-secret" not in r.text
    assert r.json()["detail"][0]["loc"][-1] == "batch"


# ---------------------------------------------------------------- when the scraper is away

def test_a_saved_change_is_not_reported_as_unsaved_when_only_the_reload_fails(client, scraper):
    """The write went through; the re-read of the page failed. Saying "could not be saved" would be false."""
    original = scraper.schedule

    def flaky():
        if scraper.writes():
            raise Unavailable("the scraper is not reachable right now")
        return original()

    scraper.schedule = flaky
    r = patch(client, "/schedules/score", {"batch": 5})
    assert r.status_code == 503 and "was saved" in r.json()["detail"] and "refresh" in r.json()["detail"]
    assert scraper.writes() == [("set_schedule", "score", {"batch": 5})]



@pytest.mark.parametrize("call", [
    lambda c: c.get("/api/settings"),
    lambda c: patch(c, "/schedules/score", {"batch": 5}),
    lambda c: patch(c, "/features", {"digest": True}),
    lambda c: patch(c, "/budget", {"daily_cap_usd": 1}),
])
def test_an_unreachable_scraper_is_a_503_not_a_500(client, scraper, call):
    scraper.fail = Unavailable("the scraper is not reachable right now")
    r = call(client)
    assert r.status_code == 503 and r.json() == {"detail": "the scraper is not reachable right now"}


def test_a_scraper_that_does_not_know_gmail_yet_still_gives_a_working_page(settings, store, monkeypatch):
    """The dashboard may be rebuilt before the scraper (or the other way round): no `gmail` in the reports must not be a 500."""
    from tests.support import SCRAPER_FEATURES, FakeScraper
    old = {**SCRAPER_FEATURES, "flags": {k: v for k, v in SCRAPER_FEATURES["flags"].items() if k != "gmail"},
           "effective": {k: v for k, v in SCRAPER_FEATURES["effective"].items() if k != "gmail"}}
    scraper = FakeScraper()
    monkeypatch.setattr(scraper, "features", lambda: old)
    c = TestClient(create_app(settings, store, scraper=scraper), base_url="http://localhost")
    assert c.post("/api/login", json={"password": PASSWORD}).status_code == 204
    r = c.get("/api/settings")
    assert r.status_code == 200 and r.json()["features"]["flags"]["gmail"] is False
    assert "gmail" not in r.json()["features"]["effective"]
