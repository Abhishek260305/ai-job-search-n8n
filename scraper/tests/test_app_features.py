"""The HTTP side of the optional parts: switches, the quiet no-ops, and when review messages are sent.

Needs FastAPI (it is in requirements.txt). JobSpy is stubbed: these tests never scrape anything."""
import json
import sys
import types

import pytest

pytest.importorskip("fastapi")
sys.modules.setdefault("jobspy", types.SimpleNamespace(scrape_jobs=lambda **kw: None))

from fastapi.testclient import TestClient  # noqa: E402

import app as A  # noqa: E402
import pipeline  # noqa: E402

TG_ENV = {"TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": "424242"}


class Store:
    def __init__(self, tailoring=True, telegram=True, digest=True, approval="review"):
        self.flags = {"tailoring": tailoring, "telegram": telegram, "digest": digest}
        self.approval = approval

    def features(self): return dict(self.flags)

    def update_features(self, **kw):
        self.flags.update(kw)
        return self.features()

    def tailor_settings(self): return {"approval": self.approval}

    # schedules
    ROWS = {"collect": {"name": "collect", "batch": 18, "model": None}, "score": {"name": "score", "batch": 10, "model": "claude-haiku-5-5"},
            "tailor": {"name": "tailor", "batch": 3, "model": None}, "digest": {"name": "digest", "batch": 10, "model": None}}

    def board_batch(self): return 30

    def schedule_row(self, name):
        if name not in self.ROWS:
            raise LookupError(f"no schedule '{name}'")
        return dict(self.ROWS[name], enabled=True)

    def claim_due_schedules(self, skip):
        self.skipped = skip
        return [dict(r) for n, r in self.ROWS.items() if n not in skip]

    def update_schedule(self, name, **fields): self.updated = (name, fields)
    def request_run(self, name):
        if name not in self.ROWS:
            raise LookupError(f"no schedule '{name}'")
        self.run_requested = name

    def update_budget_settings(self, **kw): self.tz = kw["timezone"]

    # runaway guards
    paused = False

    def schedule_pause(self): return {"paused": self.paused, "paused_at": __import__("datetime").datetime(2026, 10, 11, 9, 0), "reason": getattr(self, "reason", None)}

    def set_schedule_paused(self, paused, reason=None):
        self.paused, self.reason = paused, reason
        return self.schedule_pause()

    def release_score_claim(self, job_id):
        self.released = getattr(self, "released", []) + [job_id]
        return 0
    def valid_timezone(self, tz): return tz in ("Asia/Kolkata", "Europe/London")

    def schedule_status(self):
        return [{"name": n, "enabled": True, "every_minutes": None if n == "digest" else 60, "at_time": "08:00" if n == "digest" else None,
                 "batch": r["batch"], "model": r["model"], "run_now": False, "last_run_at": None, "next_run_at": None, "timezone": "Asia/Kolkata"}
                for n, r in self.ROWS.items()]
    def get_job(self, job_id): return {"id": job_id, "title": "T", "company": "C", "status": "shortlisted", "score": 80}


class Svc:
    """Stands in for the tailoring service: records what was asked and returns one drafted application."""
    resume = {"basics": {"name": "A B"}}

    def __init__(self): self.calls = []

    def run_next(self, limit):
        self.calls.append(("next", limit))
        return {"results": [{"job_id": 7, "status": "drafted"}], "claimed": 1}

    def run_job(self, job):
        self.calls.append(("job", job["id"]))
        return {"job_id": job["id"], "status": "drafted"}


class Bot:
    def __init__(self): self.notified = []
    def notify(self, result, job): self.notified.append(result["job_id"])


@pytest.fixture
def env(monkeypatch):
    def setup(store, telegram_env=True):
        for k in TG_ENV:
            monkeypatch.delenv(k, raising=False)
        if telegram_env:
            for k, v in TG_ENV.items():
                monkeypatch.setenv(k, v)
        monkeypatch.setattr(A, "get_store", lambda: store)
        return TestClient(A.app)
    return setup


# ------------------------------------------------------------------ /features

def test_features_report_shows_flags_effective_state_and_how_approval_behaves(env):
    c = env(Store(), telegram_env=False)
    body = c.get("/features").json()
    assert body["flags"] == {"tailoring": True, "telegram": True, "digest": True}
    assert body["effective"]["telegram"]["on"] is False and "not configured" in body["effective"]["telegram"]["reason"]
    assert body["approval"] == "review" and body["approval_effective"] == "auto", "no Telegram: review behaves as auto"
    assert any("review → behaves as auto" in line for line in body["lines"])


def test_features_set_changes_only_what_was_given(env):
    store = Store()
    c = env(store)
    body = c.post("/features", json={"tailoring": False}).json()
    assert store.flags == {"tailoring": False, "telegram": True, "digest": True}
    assert body["effective"]["tailoring"]["on"] is False and body["effective"]["telegram"]["on"] is True
    assert c.post("/features", json={}).status_code == 200 and store.flags["tailoring"] is False, "an empty request changes nothing"


# ------------------------------------------------------------------ tailoring switched off = the pipeline stops at scoring

def test_the_scheduled_run_is_a_quiet_noop_when_tailoring_is_off(env, monkeypatch):
    monkeypatch.setattr(A, "_tailor_service", lambda s: pytest.fail("no service (and so no API call) when tailoring is off"))
    r = env(Store(tailoring=False)).post("/tailor/run", json={"limit": 3})
    assert r.status_code == 200, "a failing scheduled run would show up as an error in n8n every 15 minutes"
    assert r.json()["claimed"] == 0 and "switched off" in r.json()["skipped"]


def test_an_explicit_request_gets_an_explicit_refusal_when_tailoring_is_off(env, monkeypatch):
    monkeypatch.setattr(A, "_tailor_service", lambda s: pytest.fail("no service"))
    r = env(Store(tailoring=False)).post("/tailor/run", json={"job_id": 24})
    assert r.status_code == 409 and "features set tailoring on" in r.json()["detail"]["errors"][0]


def test_the_bot_refuses_to_tailor_when_tailoring_is_off(monkeypatch):
    for k, v in TG_ENV.items():
        monkeypatch.setenv(k, v)
    bot = A._bot(Store(tailoring=False), Svc())
    assert bot is not None
    with pytest.raises(ValueError, match="switched off"):
        bot.run_tailor({"id": 1})


# ------------------------------------------------------------------ who gets a review message

def _run_with(env, monkeypatch, store, telegram_env=True):
    svc, bot = Svc(), Bot()
    monkeypatch.setattr(A, "_tailor_service", lambda s: svc)
    monkeypatch.setattr(A, "_bot", lambda s, v=None: bot)
    return env(store, telegram_env).post("/tailor/run", json={"limit": 3}).json(), bot


def test_review_approval_with_telegram_sends_the_message(env, monkeypatch):
    out, bot = _run_with(env, monkeypatch, Store(approval="review"))
    assert bot.notified == [7] and out["notified"] == 1 and out["telegram"] == "sent"


def test_cli_approval_never_pushes_even_when_telegram_is_on(env, monkeypatch):
    out, bot = _run_with(env, monkeypatch, Store(approval="cli"))
    assert bot.notified == [] and out["notified"] == 0 and "approval is cli" in out["telegram"]


def test_no_message_when_telegram_is_switched_off(env, monkeypatch):
    svc = Svc()
    monkeypatch.setattr(A, "_tailor_service", lambda s: svc)
    c = env(Store(telegram=False))
    out = c.post("/tailor/run", json={"job_id": 7}).json()
    assert out["notified"] == 0 and "switched off" in out["telegram"] and svc.calls == [("job", 7)], "the tailoring itself still ran"


def test_no_message_when_telegram_is_not_configured(env, monkeypatch):
    svc = Svc()
    monkeypatch.setattr(A, "_tailor_service", lambda s: svc)
    out = env(Store(), telegram_env=False).post("/tailor/run", json={"limit": 1}).json()
    assert out["notified"] == 0 and "not configured" in out["telegram"] and svc.calls == [("next", 1)]


# ------------------------------------------------------------------ digest and polling

def test_the_digest_is_empty_when_it_cannot_be_sent(env, monkeypatch):
    monkeypatch.setattr(pipeline, "digest", lambda s, limit: pytest.fail("do not even build a digest nobody can receive"))
    for store, telegram_env in ((Store(digest=False), True), (Store(), False), (Store(telegram=False), True)):
        body = env(store, telegram_env).get("/digest").json()
        assert body["text"] == "" and body["ids"] == [] and body["disabled"], "empty text ends the n8n workflow quietly"


def test_the_digest_is_built_when_it_can_be_sent(env, monkeypatch):
    monkeypatch.setattr(pipeline, "digest", lambda s, limit: {"count": 1, "ids": [3], "text": "hi", "html": "hi", "alert": ""})
    assert env(Store()).get("/digest").json()["text"] == "hi"


def test_polling_does_nothing_and_says_why_when_telegram_is_off(env):
    assert env(Store(telegram=False)).post("/telegram/poll").json() == {"telegram": "switched off (jobsearch features set telegram on)", "updates": 0}
    body = env(Store(), telegram_env=False).post("/telegram/poll").json()
    assert body["updates"] == 0 and "not configured" in body["telegram"]


# ------------------------------------------------------------------ schedules

def test_the_tick_claims_everything_when_all_parts_are_on(env):
    store = Store()
    body = env(store).post("/schedule/tick").json()
    assert [d["name"] for d in body["due"]] == ["collect", "score", "tailor", "digest"] and body["skipped"] == []
    assert body["due"][1] == {"name": "score", "batch": 10, "model": "claude-haiku-5-5"}


def test_the_tick_leaves_switched_off_stages_alone(env):
    store = Store(tailoring=False)
    body = env(store).post("/schedule/tick").json()
    assert store.skipped == ["tailor"] and "tailor" not in [d["name"] for d in body["due"]]
    store = Store(telegram=False)
    env(store).post("/schedule/tick")
    assert store.skipped == ["digest"], "no Telegram: the digest is not even claimed, so it fires when Telegram is back"
    store = Store(tailoring=False, digest=False)
    env(store).post("/schedule/tick")
    assert sorted(store.skipped) == ["digest", "tailor"]
    env(Store(), telegram_env=False).post("/schedule/tick")           # unconfigured behaves like switched off
    store = Store()
    env(store, telegram_env=False).post("/schedule/tick")
    assert store.skipped == ["digest"]


def test_workflows_default_their_batch_to_the_schedule_settings(env, monkeypatch):
    seen = {}
    monkeypatch.setattr(pipeline, "plan", lambda s, profile, batch, board_batch=30: seen.setdefault("plan", batch) or {})
    monkeypatch.setattr(pipeline, "digest", lambda s, limit: seen.setdefault("digest", limit) and {"count": 0, "ids": [], "text": "", "html": "", "alert": ""})
    c = env(Store())
    c.get("/pipeline/plan")
    assert seen["plan"] == 18, "collect batch from the schedule, no longer hard-coded in the workflow"
    c.get("/digest")
    assert seen["digest"] == 10
    seen.clear()
    c.get("/pipeline/plan?batch=4")
    assert seen["plan"] == 4, "an explicit batch still wins"
    c.get("/digest?limit=2")
    assert seen["digest"] == 2


def test_tailor_run_defaults_to_the_tailor_batch(env, monkeypatch):
    calls = []

    class S2(Svc):
        def run_next(self, limit):
            calls.append(limit)
            return {"results": [], "claimed": 0}
    monkeypatch.setattr(A, "_tailor_service", lambda s: S2())
    c = env(Store(approval="cli"))
    c.post("/tailor/run", json={})
    c.post("/tailor/run", json={"limit": 1})
    assert calls == [3, 1]


def test_the_score_workflow_reads_its_model_and_batch_here(env):
    body = env(Store()).get("/schedule/score/settings").json()
    assert body == {"name": "score", "enabled": True, "batch_size": 10, "model": "claude-haiku-5-5"}
    assert env(Store()).get("/schedule/mystery/settings").status_code == 400


def test_changing_a_schedule(env):
    store = Store()
    c = env(store)
    r = c.post("/schedule/collect", json={"every_minutes": 360, "batch": 18})
    assert r.status_code == 200 and store.updated == ("collect", {"every_minutes": 360, "batch": 18})
    assert r.json()["timezone"] == "Asia/Kolkata" and len(r.json()["schedules"]) == 4 and len(r.json()["lines"]) == 4
    assert c.post("/schedule/digest", json={"at_time": "7:30"}).status_code == 200 and store.updated == ("digest", {"at_time": "07:30", "every_minutes": None})
    assert c.post("/schedule/digest", json={"every_minutes": 360}).status_code == 200 and store.updated == ("digest", {"every_minutes": 360, "at_time": None})


@pytest.mark.parametrize("name,body,message", [
    ("collect", {"every_minutes": 10}, "15 minutes"), ("digest", {"every_minutes": 30}, "1 hour"),
    ("score", {"batch": 99}, "1 to 50"), ("collect", {"model": "claude-haiku-5-5"}, "only the score schedule"),
    ("mystery", {"batch": 1}, "unknown schedule"), ("collect", {}, "nothing to change")])
def test_bad_schedule_changes_are_refused_and_change_nothing(env, name, body, message):
    store = Store()
    r = env(store).post(f"/schedule/{name}", json=body)
    assert r.status_code == 422 and message in r.json()["detail"]["errors"][0] and not hasattr(store, "updated")


def test_run_now_and_timezone(env):
    store = Store()
    c = env(store)
    assert c.post("/schedule/collect/run").json()["requested"] == "collect" and store.run_requested == "collect"
    assert c.post("/schedule/mystery/run").status_code == 400
    assert c.post("/schedule/timezone", json={"timezone": "Europe/London"}).status_code == 200 and store.tz == "Europe/London"
    r = c.post("/schedule/timezone", json={"timezone": "Mars/Base"})
    assert r.status_code == 422 and "unknown timezone" in r.json()["detail"]["errors"][0]
    assert c.post("/schedule/timezone", json={"timezone": "x'; drop table jobs;--"}).status_code == 422


# ------------------------------------------------------------------ emergency brake and refunds

def test_pause_and_resume(env):
    store = Store()
    c = env(store)
    r = c.post("/schedule/pause", json={"reason": "checking the bill"})
    assert r.status_code == 200 and store.paused and store.reason == "checking the bill"
    assert r.json()["paused"] and r.json()["lines"][0].startswith("PAUSED") and "checking the bill" in r.json()["lines"][0]
    assert "schedule resume" in r.json()["lines"][0]
    r = c.post("/schedule/resume")
    assert r.status_code == 200 and not store.paused and not r.json()["paused"] and not r.json()["lines"][0].startswith("PAUSED")
    c.post("/schedule/pause", json={})
    assert store.reason is None


def test_a_scoring_claim_that_cost_nothing_can_be_released(env):
    store = Store()
    r = env(store).post("/jobs/77/release")
    assert r.status_code == 200 and r.json() == {"job_id": 77, "claims": 0} and store.released == [77]


# ------------------------------------------------------------------ the Apify spend guard

class ApifyStore(Store):
    def __init__(self, **kw):
        super().__init__()
        self.status = dict({"month_remaining": 10.0, "day_remaining": 0.5, "blocked": False, "blocked_reason": None}, **kw)
        self.usage = []

    def apify_budget_status(self):
        if self.status.get("boom"):
            raise RuntimeError("database down")
        return self.status

    def record_usage(self, provider, purpose, model, job_id, usage, ok=True, note=None, cost=None):
        self.usage.append({"provider": provider, "model": model, "ok": ok, "cost": cost, "note": note})
        return cost


GOOD_RUN = {"jobs": [], "errors": [], "meta": {"account": "curious", "actor": "curious_coder/x", "estimated_cost_usd": 0.035,
                                               "actual_cost_usd": 0.047, "failed_attempts": []}}
SEARCH = {"search_term": "backend", "location": "Pune"}


@pytest.fixture
def apify(monkeypatch):
    monkeypatch.setenv("APIFY_TOKEN_CURIOUS", "tok")
    calls = []

    def fake(params, only=None, budget_left=None):
        calls.append((params, budget_left))
        return json.loads(json.dumps(GOOD_RUN))
    monkeypatch.setattr(A.apify_source, "search_linkedin", fake)
    return calls


def test_a_blocked_apify_budget_skips_the_search_without_calling_apify(env, apify):
    store = ApifyStore(blocked=True, blocked_reason="Apify daily cap reached", day_remaining=0)
    r = env(store).post("/search/linkedin", json=SEARCH)
    assert r.status_code == 200 and r.json()["skipped"] == "Apify daily cap reached" and r.json()["jobs"] == [] and r.json()["errors"] == []
    assert apify == [] and store.usage == [], "no paid run was started, and nothing is logged as spent"


def test_the_remaining_budget_is_handed_to_the_adapter_which_decides_per_run(env, apify):
    store = ApifyStore(month_remaining=5, day_remaining=0.2)
    assert not env(store).post("/search/linkedin", json=SEARCH).json().get("skipped")
    assert apify[0][1] == 0.2, "the lower of the monthly and daily remainder is the limit for this search"
    apify.clear()
    env(ApifyStore(month_remaining=0.07, day_remaining=0.5)).post("/search/linkedin", json=SEARCH)
    assert apify[0][1] == 0.07


def test_a_skip_decided_by_the_adapter_is_reported_as_a_skip_not_an_error(env, monkeypatch):
    monkeypatch.setenv("APIFY_TOKEN_CURIOUS", "tok")
    monkeypatch.setattr(A.apify_source, "search_linkedin",
                        lambda p, only=None, budget_left=None: {"jobs": [], "errors": [], "meta": None, "skipped": "not enough Apify budget left for this search ($0.01 left)"})
    store = ApifyStore(day_remaining=0.01)
    r = env(store).post("/search/linkedin", json=SEARCH)
    assert r.status_code == 200 and "not enough Apify budget" in r.json()["skipped"] and r.json()["jobs"] == [] and store.usage == []


def test_an_unreadable_apify_budget_means_nothing_is_spent(env, apify):
    r = env(ApifyStore(boom=True)).post("/search/linkedin", json=SEARCH)
    assert r.status_code == 503 and "no Apify run was started" in r.json()["detail"]["errors"][0] and apify == []


def test_a_finished_run_is_logged_at_the_larger_of_estimate_and_real_cost(env, apify):
    store = ApifyStore()
    env(store).post("/search/linkedin", json=SEARCH)
    assert [(u["provider"], u["cost"], u["ok"]) for u in store.usage] == [("apify", 0.047, True)]
    assert "measured from the balance" in store.usage[0]["note"]


@pytest.mark.parametrize("meta,expected", [
    ({"estimated_cost_usd": 0.035, "actual_cost_usd": 0.047}, 0.047),
    ({"estimated_cost_usd": 0.035, "actual_cost_usd": 0.010}, 0.035),     # never below the estimate
    ({"estimated_cost_usd": 0.035, "actual_cost_usd": None}, 0.0525),     # unknown balance: the estimate was low by a third in a real run
    ({"estimated_cost_usd": 0.035, "actual_cost_usd": 0.0}, 0.0525),     # a real run read 0.0 straight away: the balance lags, so 0 means "not known yet"
    ({}, None)])
def test_apify_cost_to_log(meta, expected):
    assert A.apify_cost_to_log(meta) == expected


def test_failed_runs_that_started_are_logged_too(env, monkeypatch):
    monkeypatch.setenv("APIFY_TOKEN_CURIOUS", "tok")
    ok = dict(GOOD_RUN, meta=dict(GOOD_RUN["meta"], failed_attempts=[{"account": "cheap", "estimated_cost_usd": 0.125}]))
    monkeypatch.setattr(A.apify_source, "search_linkedin", lambda p, only=None, budget_left=None: ok)
    store = ApifyStore()
    env(store).post("/search/linkedin", json=SEARCH)
    assert [(u["model"], u["cost"], u["ok"]) for u in store.usage] == [("curious_coder/x", 0.047, True), ("cheap", 0.1875, False)]


def test_a_search_that_fails_after_starting_still_counts_its_cost(env, monkeypatch):
    monkeypatch.setenv("APIFY_TOKEN_CURIOUS", "tok")

    def boom(params, only=None, budget_left=None):
        e = A.apify_source.ApifyError("all accounts failed: x", started=True)
        e.attempts = [{"account": "curious", "estimated_cost_usd": 0.035}, {"account": "cheap", "estimated_cost_usd": 0.125}]
        raise e
    monkeypatch.setattr(A.apify_source, "search_linkedin", boom)
    store = ApifyStore()
    r = env(store).post("/search/linkedin", json=SEARCH)
    assert r.status_code == 502 and [(u["model"], u["ok"]) for u in store.usage] == [("curious", False), ("cheap", False)]
    assert round(sum(u["cost"] for u in store.usage), 4) == round((0.035 + 0.125) * 1.5, 4)


# ---- board_batch travels through the schedule endpoint (Phase 7, Task 13)

def test_the_schedule_endpoint_accepts_board_batch_for_collect(env):
    store = Store()
    r = env(store).post("/schedule/collect", json={"board_batch": 45})
    assert r.status_code == 200 and store.updated == ("collect", {"board_batch": 45})
    r = env(store).post("/schedule/collect", json={"every_minutes": 360, "board_batch": 12})
    assert r.status_code == 200 and store.updated == ("collect", {"every_minutes": 360, "board_batch": 12})


@pytest.mark.parametrize("name,value", [("score", 5), ("tailor", 5), ("digest", 5), ("collect", 0), ("collect", 201)])
def test_the_schedule_endpoint_refuses_a_bad_board_batch(env, name, value):
    store = Store()
    r = env(store).post(f"/schedule/{name}", json={"board_batch": value})
    assert r.status_code == 422 and "board_batch" in r.json()["detail"]["errors"][0] and not hasattr(store, "updated")


def test_the_schedule_report_tells_how_many_boards_collect_visits_per_run(env):
    rows = {r["name"]: r for r in env(Store()).get("/schedule").json()["schedules"]}
    assert rows["collect"]["board_batch"] == 30
    assert all("board_batch" not in r for n, r in rows.items() if n != "collect")

