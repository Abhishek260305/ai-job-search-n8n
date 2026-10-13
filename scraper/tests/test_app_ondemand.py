"""The HTTP side of tailoring one job on request: the estimate, the start, the progress, and every refusal.

JobSpy is stubbed, the tailoring service is a fake that records what it was asked: nothing here calls a model."""
import sys
import threading
import types
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("fastapi")
sys.modules.setdefault("jobspy", types.SimpleNamespace(scrape_jobs=lambda **kw: None))

from fastapi.testclient import TestClient  # noqa: E402

import app as A  # noqa: E402
import ondemand  # noqa: E402

PRICING = {"input_per_mtok": 3, "output_per_mtok": 15, "cache_write_per_mtok": 3.75, "cache_read_per_mtok": 0.3}
SETTINGS = {"mode": "manual", "model": "claude-sonnet-5-5", "cover_letter": True, "max_score": 90, "min_score": None, "style_notes": None,
            "approval": "review", "max_pages": 2, "daily_limit": 5, "verify": True, "verify_model": "claude-haiku-5-5", "paper": "a4"}
JOB = {"id": 7, "title": "Backend Engineer", "company": "Acme", "location": "Pune", "description": "Build APIs in Go.",
       "canonical_url": "https://acme.com/7", "score": 80, "verdict": "good", "strengths": ["Go"], "gaps": ["Kafka"], "gates": {},
       "posted_at": None, "status": "shortlisted"}
BUDGET = {"day_remaining": 1.4, "month_remaining": 9.0, "blocked": False, "blocked_reason": None}


class Store:
    def __init__(self, **over):
        self.flags = {"tailoring": True, "telegram": False, "digest": True}
        self.settings = dict(SETTINGS)
        self.job = dict(JOB)
        self.budget = dict(BUDGET)
        self.app = None
        self.typical = 0.047
        self.__dict__.update(over)
        self.calls = []

    def features(self): return dict(self.flags)
    def tailor_settings(self): return dict(self.settings)
    def budget_status(self): return dict(self.budget)
    def pricing(self, model): return PRICING
    def get_job(self, job_id): return dict(self.job, id=job_id) if self.job else None
    def application(self, job_id): return self.app
    def recent_tailor_cost(self): return self.typical
    paused = False
    def schedule_pause(self): return {"paused": self.paused}


class Service:
    """Stands in for TailorService: records the job it was run for and returns one drafted application."""
    resume = {"basics": {"name": "A B"}}

    class Pdf:
        up = True
        def health(self): return self.up

    def __init__(self, result=None, error=None, spent=0.0):
        self.calls, self.result, self.error, self._job_spend, self.tracks, self.pdf = [], result, error, spent, [], Service.Pdf()

    def run_job(self, job, settings=None, *, track=True):
        self.calls.append(job["id"])
        self.tracks.append(track)
        if self.error:
            raise self.error
        return self.result or {"job_id": job["id"], "status": "drafted", "variant": "tailored", "cost_usd": 0.047, "needs_attention": False,
                               "attention": [], "has_cover_letter": True, "pages": 1, "files_dir": "/output/applications/7-x",
                               "requirements": {"matched": 4, "bridged": 1, "gap": 0}}


@pytest.fixture
def env(monkeypatch):
    def setup(store=None, service=None, spawn="now"):
        store = store or Store()
        service = service or Service()
        queue = []
        runs = ondemand.OnDemandRuns(spawn=(lambda fn: fn()) if spawn == "now" else queue.append)
        monkeypatch.setattr(A, "_ondemand", runs)
        monkeypatch.setattr(A, "get_store", lambda: store)
        monkeypatch.setattr(A, "_tailor_service", lambda s: service)
        monkeypatch.setattr(A, "_load_resume", lambda: {"basics": {"name": "A B", "email": "a@b.co"}, "work": [], "education": [], "skills": []})
        monkeypatch.setattr(A.scoring, "load_profile", lambda path: {"name": "x"})
        monkeypatch.setattr(A.tailor, "prepare", lambda *a, **kw: {"system": "s" * 6000, "user": "u" * 9000, "max_tokens": 6000})
        client = TestClient(A.app)
        client.store, client.service, client.runs, client.queue = store, service, runs, queue
        assert not A._tailor_lock.locked()
        return client
    return setup


# ------------------------------------------------------------------ the estimate

def test_the_estimate_says_what_it_costs_and_that_it_can_run(env):
    c = env()
    body = c.get("/tailor/estimate?job_id=7").json()
    assert body["can_run"] is True and body["reason"] is None and body["uses_model"] is True and body["variant"] == "tailored"
    assert body["model"] == "claude-sonnet-5-5" and body["cover_letter"] is True and body["mode"] == "manual"
    assert body["typical_usd"] == 0.047 and body["at_most_usd"] > body["typical_usd"] > 0
    assert body["budget"] == {"day_remaining": 1.4, "month_remaining": 9.0, "blocked": False, "blocked_reason": None}
    assert body["existing"] is None and "code" not in body and "blocker" not in body


def test_it_works_in_manual_mode_which_is_the_point(env):
    assert env().get("/tailor/estimate?job_id=7").json()["can_run"] is True        # SETTINGS mode is manual
    s = Store(); s.settings["mode"] = "auto"
    assert env(s).get("/tailor/estimate?job_id=7").json()["can_run"] is True


def test_with_no_history_the_typical_cost_is_a_default_and_never_above_the_ceiling(env):
    body = env(Store(typical=None)).get("/tailor/estimate?job_id=7").json()
    assert body["typical_usd"] == 0.05
    body = env(Store(typical=99.0)).get("/tailor/estimate?job_id=7").json()
    assert body["typical_usd"] == body["at_most_usd"]


def test_a_job_that_scores_above_the_band_with_no_cover_letter_uses_no_model_at_all(env):
    s = Store(); s.settings.update(cover_letter=False, max_score=70)
    body = env(s).get("/tailor/estimate?job_id=7").json()
    assert body["variant"] == "base" and body["uses_model"] is False and body["at_most_usd"] == 0 and body["can_run"] is True


def test_it_shows_what_would_be_replaced(env):
    s = Store(app={"status": "drafted", "resume_path": "/output/x/resume.pdf", "cover_letter_path": None})
    assert env(s).get("/tailor/estimate?job_id=7").json()["existing"] == {"status": "drafted", "has_resume": True, "has_cover_letter": False}


@pytest.mark.parametrize("status", ["scam_blocked", "filtered_out", "expired", "applied"])
def test_jobs_that_must_not_be_tailored_are_refused_with_the_reason(env, status):
    s = Store(); s.job["status"] = status
    body = env(s).get("/tailor/estimate?job_id=7").json()
    assert body["can_run"] is False and status in body["reason"]


def test_a_job_that_was_never_scored_is_refused(env):
    s = Store(); s.job["score"] = None
    assert env(s).get("/tailor/estimate?job_id=7").json()["can_run"] is False


def test_an_unknown_job_is_a_clear_no(env):
    body = env(Store(job=None)).get("/tailor/estimate?job_id=7").json()
    assert body["can_run"] is False and "no job 7" in body["reason"]


def test_switched_off_tailoring_says_how_to_turn_it_on(env):
    s = Store(); s.flags["tailoring"] = False
    body = env(s).get("/tailor/estimate?job_id=7").json()
    assert body["can_run"] is False and "switched off" in body["reason"]


def test_a_reached_cap_blocks_the_estimate(env):
    s = Store(); s.budget.update(blocked=True, blocked_reason="daily cap reached")
    body = env(s).get("/tailor/estimate?job_id=7").json()
    assert body["can_run"] is False and "daily cap reached" in body["reason"]


def test_not_enough_budget_left_for_the_worst_case_blocks_it_even_if_the_cap_is_not_reached(env):
    s = Store(); s.budget.update(day_remaining=0.01)
    body = env(s).get("/tailor/estimate?job_id=7").json()
    assert body["can_run"] is False and "not enough budget" in body["reason"]


@pytest.mark.parametrize("query", ["", "?job_id=0", "?job_id=-1", "?job_id=x", f"?job_id={2**63}"])
def test_a_bad_job_id_is_a_422(env, query):
    assert env().get("/tailor/estimate" + query).status_code == 422


# ------------------------------------------------------------------ the start

def test_start_runs_the_job_in_the_background_and_the_status_reports_it(env):
    c = env(spawn="later")
    r = c.post("/tailor/start", json={"job_id": 7})
    assert r.status_code == 200 and r.json()["state"] == "running" and c.service.calls == [], "nothing runs before the background thread does"
    assert c.get("/tailor/status?job_id=7").json()["state"] == "running"
    c.queue.pop()()
    done = c.get("/tailor/status?job_id=7").json()
    assert done["state"] == "done" and c.service.calls == [7]
    assert done["result"]["status"] == "drafted" and done["result"]["cost_usd"] == 0.047 and "files_dir" not in str(done)


def test_a_second_start_while_running_is_refused_and_spends_nothing(env):
    c = env(spawn="later")
    assert c.post("/tailor/start", json={"job_id": 7}).status_code == 200
    r = c.post("/tailor/start", json={"job_id": 7})
    assert r.status_code == 409 and "being tailored right now" in r.json()["detail"]["errors"][0]
    r = c.post("/tailor/start", json={"job_id": 8})
    assert r.status_code == 409 and "another job" in r.json()["detail"]["errors"][0]
    assert len(c.queue) == 1
    c.queue.pop()()
    assert c.service.calls == [7]


def test_a_job_just_done_cannot_be_started_again_at_once(env):
    c = env()
    assert c.post("/tailor/start", json={"job_id": 7}).status_code == 200
    r = c.post("/tailor/start", json={"job_id": 7})
    assert r.status_code == 409 and "a moment ago" in r.json()["detail"]["errors"][0] and c.service.calls == [7]
    assert c.get("/tailor/estimate?job_id=7").json()["can_run"] is False


@pytest.mark.parametrize("mutate, code, text", [
    (lambda s: s.flags.update(tailoring=False), 409, "switched off"),
    (lambda s: s.job.update(status="scam_blocked"), 422, "scam_blocked"),
    (lambda s: s.job.update(score=None), 422, "not tailored"),
    (lambda s: s.budget.update(blocked=True, blocked_reason="monthly cap reached"), 409, "monthly cap reached"),
    (lambda s: s.budget.update(month_remaining=0.001), 409, "not enough budget"),
])
def test_every_refusal_of_the_estimate_is_a_refusal_of_the_start_and_nothing_is_run(env, mutate, code, text):
    s = Store(); mutate(s)
    c = env(s)
    r = c.post("/tailor/start", json={"job_id": 7})
    assert r.status_code == code and text in r.json()["detail"]["errors"][0]
    assert c.service.calls == [] and c.get("/tailor/status?job_id=7").json() == {"state": "idle"}


def test_an_unknown_job_is_a_422_not_a_404_so_the_dashboard_can_tell_it_from_a_missing_endpoint(env):
    assert env(Store(job=None)).post("/tailor/start", json={"job_id": 7}).status_code == 422


def test_the_cost_the_user_agreed_to_is_enforced(env):
    c = env()
    at_most = c.get("/tailor/estimate?job_id=7").json()["at_most_usd"]
    r = c.post("/tailor/start", json={"job_id": 7, "max_cost_usd": at_most / 2})
    assert r.status_code == 409 and "more than the" in r.json()["detail"]["errors"][0] and c.service.calls == []
    assert c.post("/tailor/start", json={"job_id": 7, "max_cost_usd": at_most}).status_code == 200 and c.service.calls == [7]


@pytest.mark.parametrize("body", [{}, {"job_id": 0}, {"job_id": "7"}, {"job_id": 7, "max_cost_usd": -1}, {"job_id": 7, "max_cost_usd": 1000},
                                  {"job_id": 7, "max_cost_usd": "a lot"}, {"job_id": 2**63}])
def test_a_bad_start_is_a_422(env, body):
    c = env()
    assert c.post("/tailor/start", json=body).status_code == 422 and c.service.calls == []


def test_a_failed_run_is_reported_and_the_job_waits_out_the_cooldown(env):
    c = env(service=Service(error=RuntimeError("the PDF service is down")))
    assert c.post("/tailor/start", json={"job_id": 7}).status_code == 200
    s = c.get("/tailor/status?job_id=7").json()
    assert s["state"] == "failed" and "PDF service is down" in s["error"] and "Traceback" not in s["error"]
    assert c.post("/tailor/start", json={"job_id": 7}).status_code == 409


def test_a_run_that_could_not_afford_itself_is_reported_as_done_with_that_outcome(env):
    c = env(service=Service(result={"job_id": 7, "status": "budget_paused", "reason": "not enough budget left for this job"}))
    c.post("/tailor/start", json={"job_id": 7})
    s = c.get("/tailor/status?job_id=7").json()
    assert s["state"] == "done" and s["result"]["status"] == "budget_paused" and s["result"]["reason"].startswith("not enough budget")


def test_the_status_of_a_job_never_started_is_idle_and_a_bad_id_is_a_422(env):
    c = env()
    assert c.get("/tailor/status?job_id=9").json() == {"state": "idle"}
    assert c.get("/tailor/status?job_id=0").status_code == 422


def test_no_telegram_message_is_sent_for_a_run_you_started_yourself(env, monkeypatch):
    monkeypatch.setattr(A, "_bot", lambda *a, **kw: pytest.fail("a run started from the dashboard does not message Telegram"))
    c = env()
    c.post("/tailor/start", json={"job_id": 7})
    assert c.service.calls == [7]


# ------------------------------------------------------------------ what the review found

@pytest.mark.parametrize("status", ["applied", "in_progress", "interview", "offer", "rejected", "withdrawn"])
def test_an_application_that_was_submitted_is_never_tailored_again_even_if_the_job_still_says_scored(env, status):
    s = Store(app={"status": status, "resume_path": "/o/r.pdf", "cover_letter_path": None, "variant": "tailored"})
    s.job["status"] = "scored"                        # the job row did not follow the application
    c = env(s)
    body = c.get("/tailor/estimate?job_id=7").json()
    assert body["can_run"] is False and "already" in body["reason"] and body["existing"]["status"] == status
    assert c.post("/tailor/start", json={"job_id": 7}).status_code == 422 and c.service.calls == []


@pytest.mark.parametrize("status", ["drafted", "approved", "skipped"])
def test_an_application_still_in_review_may_be_tailored_again(env, status):
    assert env(Store(app={"status": status, "resume_path": "/o/r.pdf", "cover_letter_path": None, "variant": "tailored"})).get("/tailor/estimate?job_id=7").json()["can_run"] is True


def test_the_emergency_pause_stops_this_too(env):
    s = Store(); s.paused = True
    c = env(s)
    assert "paused" in c.get("/tailor/estimate?job_id=7").json()["reason"]
    assert c.post("/tailor/start", json={"job_id": 7}).status_code == 409 and c.service.calls == []


def test_a_job_tailored_a_moment_ago_waits_even_after_the_scraper_restarted(env):
    just_now = datetime.now(timezone.utc) - timedelta(seconds=30)
    s = Store(app={"status": "drafted", "resume_path": "/o/r.pdf", "cover_letter_path": None, "variant": "tailored", "updated_at": just_now})
    body = env(s).get("/tailor/estimate?job_id=7").json()
    assert body["can_run"] is False and "a moment ago" in body["reason"]
    long_ago = datetime.now(timezone.utc) - timedelta(seconds=ondemand.COOLDOWN_S + 5)
    s = Store(app={"status": "drafted", "resume_path": "/o/r.pdf", "cover_letter_path": None, "variant": "tailored", "updated_at": long_ago})
    assert env(s).get("/tailor/estimate?job_id=7").json()["can_run"] is True


def test_a_start_is_refused_at_once_when_a_scheduled_run_holds_the_lock(env):
    c = env()
    with A._tailor_lock:
        r = c.post("/tailor/start", json={"job_id": 7})
    assert r.status_code == 409 and "scheduled or Telegram" in r.json()["detail"]["errors"][0] and c.service.calls == []
    assert c.get("/tailor/status?job_id=7").json() == {"state": "idle"}


def test_a_run_that_finds_the_lock_taken_stops_instead_of_waiting_and_spends_nothing(env):
    c = env(spawn="later")
    assert c.post("/tailor/start", json={"job_id": 7}).status_code == 200
    work = c.queue.pop()
    A._tailor_lock.acquire()                            # the schedule takes over between the start and the run
    try:
        t = threading.Thread(target=work)
        t.start(); t.join(3)
        assert not t.is_alive(), "it must not wait for the lock"
    finally:
        A._tailor_lock.release()
    s = c.get("/tailor/status?job_id=7").json()
    assert s["state"] == "failed" and "took over" in s["error"] and "nothing was spent" in s["error"] and c.service.calls == []


@pytest.mark.parametrize("change, text", [
    (lambda s: s.flags.update(tailoring=False), "switched off"),
    (lambda s: setattr(s, "app", {"status": "applied", "resume_path": "/o/r.pdf", "cover_letter_path": None, "variant": "tailored"}), "already applied"),
    (lambda s: s.job.update(status="scam_blocked"), "scam_blocked"),
    (lambda s: s.budget.update(day_remaining=0.001), "not enough budget"),
    (lambda s: setattr(s, "paused", True), "paused"),
])
def test_everything_is_checked_again_inside_the_lock_because_the_world_may_have_changed(env, change, text):
    c = env(spawn="later")
    assert c.post("/tailor/start", json={"job_id": 7, "max_cost_usd": 5}).status_code == 200
    change(c.store)                                     # while the start waited its turn
    c.queue.pop()()
    s = c.get("/tailor/status?job_id=7").json()
    assert s["state"] == "failed" and text in s["error"] and "nothing was spent" in s["error"] and c.service.calls == []


def test_a_model_switched_to_a_dearer_one_after_the_user_agreed_is_not_run(env):
    c = env(spawn="later")
    agreed = c.get("/tailor/estimate?job_id=7").json()["at_most_usd"]
    assert c.post("/tailor/start", json={"job_id": 7, "max_cost_usd": agreed}).status_code == 200
    c.store.settings["max_pages"] = 2
    c.store.settings["cover_letter"] = True
    c.store.settings["model"] = "claude-opus-5-5"
    A.s_pricing = None
    c.store.pricing = lambda model: {"input_per_mtok": 15, "output_per_mtok": 75, "cache_write_per_mtok": 18.75, "cache_read_per_mtok": 1.5}
    c.queue.pop()()
    s = c.get("/tailor/status?job_id=7").json()
    assert s["state"] == "failed" and "more than the" in s["error"] and "you agreed to" in s["error"] and c.service.calls == []


def test_a_pdf_service_that_is_down_is_found_before_anything_is_spent(env):
    svc = Service()
    svc.pdf.up = False
    c = env(service=svc)
    c.post("/tailor/start", json={"job_id": 7})
    s = c.get("/tailor/status?job_id=7").json()
    assert s["state"] == "failed" and "PDF service is not answering" in s["error"] and "nothing was spent" in s["error"]
    assert c.service.calls == []


def test_the_pdf_check_is_skipped_when_no_model_call_will_be_made(env):
    s = Store(); s.settings.update(cover_letter=False, max_score=70)         # nothing is sent to Claude: nothing is lost by trying
    svc = Service()
    svc.pdf.up = False
    c = env(s, svc)
    c.post("/tailor/start", json={"job_id": 7})
    assert c.get("/tailor/status?job_id=7").json()["state"] == "done" and svc.calls == [7]


def test_a_run_that_failed_after_spending_reports_what_it_cost(env):
    c = env(service=Service(error=RuntimeError("could not reach the PDF service: ConnectError"), spent=0.0184))
    c.post("/tailor/start", json={"job_id": 7})
    s = c.get("/tailor/status?job_id=7").json()
    assert s["state"] == "failed" and s["cost_usd"] == 0.0184 and "PDF service" in s["error"]


def test_a_run_you_start_never_touches_the_schedules_attempt_counters(env):
    c = env()
    c.post("/tailor/start", json={"job_id": 7})
    assert c.service.tracks == [False]


def test_the_lock_is_free_again_after_every_kind_of_end(env):
    for svc in (Service(), Service(error=RuntimeError("boom"))):
        c = env(service=svc)
        c.post("/tailor/start", json={"job_id": 7})
        assert not A._tailor_lock.locked()


def test_the_start_that_the_registry_refuses_late_is_a_409_not_a_crash(env, monkeypatch):
    c = env(spawn="later")
    c.post("/tailor/start", json={"job_id": 7})
    monkeypatch.setattr(c.runs, "refusal", lambda job_id: None)          # the plan sees nothing wrong, the registry still does
    r = c.post("/tailor/start", json={"job_id": 7})
    assert r.status_code == 409 and "being tailored right now" in r.json()["detail"]["errors"][0]


# ------------------------------------------------------------------ what it says it costs

def test_the_estimate_uses_the_model_when_the_cover_letter_alone_needs_one_and_when_the_resume_does(env, monkeypatch):
    seen = []
    s = Store(); s.settings.update(cover_letter=False)                 # tailored, no cover letter: still a call
    c = env(s)
    monkeypatch.setattr(A.tailor, "prepare", lambda *a, **kw: (seen.append(kw["mode"]), {"system": "s" * 6000, "user": "u" * 9000, "max_tokens": 6000})[1])
    body = c.get("/tailor/estimate?job_id=7").json()
    assert body["uses_model"] is True and body["at_most_usd"] > 0 and seen == ["full"]
    seen.clear()
    s2 = Store(); s2.settings.update(cover_letter=True, max_score=70)  # above the band, with a cover letter: a call for the letter only
    c2 = env(s2)
    monkeypatch.setattr(A.tailor, "prepare", lambda *a, **kw: (seen.append(kw["mode"]), {"system": "s" * 6000, "user": "u" * 9000, "max_tokens": 6000})[1])
    body = c2.get("/tailor/estimate?job_id=7").json()
    assert body["uses_model"] is True and body["variant"] == "base" and seen == ["cover_only"]


def test_the_most_it_can_cost_is_what_the_guard_in_the_run_holds_back_so_the_two_agree_at_the_boundary(env):
    import budget
    s = Store(); s.settings["verify"] = False
    worst = budget.worst_case_item_cost(PRICING, 6000 + 9000 * 2 + 9000, 6000)
    body = env(s).get("/tailor/estimate?job_id=7").json()
    assert body["at_most_usd"] == round(worst * 2, 4)
    below = Store(); below.settings["verify"] = False; below.budget.update(day_remaining=worst * 2 - 0.0005)
    assert env(below).get("/tailor/estimate?job_id=7").json()["can_run"] is False
    enough = Store(); enough.settings["verify"] = False; enough.budget.update(day_remaining=worst * 2 + 0.0005)
    assert env(enough).get("/tailor/estimate?job_id=7").json()["can_run"] is True


def test_the_fact_check_call_is_part_of_the_most_it_can_cost_and_a_dear_checker_shows(env):
    a = Store(); a.settings["verify"] = False
    b = Store()                                        # verify on, the cheap default checker
    c = Store(); c.settings["verify_model"] = "claude-opus-5-5"
    c.pricing = lambda model: PRICING if model != "claude-opus-5-5" else {"input_per_mtok": 15, "output_per_mtok": 75, "cache_write_per_mtok": 18.75, "cache_read_per_mtok": 1.5}
    base = env(a).get("/tailor/estimate?job_id=7").json()["at_most_usd"]
    cheap = env(b).get("/tailor/estimate?job_id=7").json()["at_most_usd"]
    dear = env(c).get("/tailor/estimate?job_id=7").json()["at_most_usd"]
    assert base < cheap < dear


# ---- the "at most" the user agrees to counts the v2 engine's extra calls

def test_the_estimate_for_the_v2_engine_includes_the_reviewer_and_the_company_note(env):
    base = env().get("/tailor/estimate?job_id=7").json()["at_most_usd"]
    s2 = Store(); s2.settings.update(engine="v2", reviewer=True, company_research=True)
    with_v2 = env(s2).get("/tailor/estimate?job_id=7").json()["at_most_usd"]
    s3 = Store(); s3.settings.update(engine="v2", reviewer=False, company_research=False)
    bare = env(s3).get("/tailor/estimate?job_id=7").json()["at_most_usd"]
    assert with_v2 > bare >= base > 0


def test_an_invalid_engine_in_the_settings_is_a_clear_refusal_not_a_crash(env):
    s = Store(); s.settings["engine"] = "v9"
    body = env(s).get("/tailor/estimate?job_id=7").json()
    assert body["can_run"] is False and "tailoring settings are not valid" in body["reason"]
