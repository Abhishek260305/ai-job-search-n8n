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
GMAIL_ENV = {"GMAIL_CLIENT_ID": "id.apps.example", "GMAIL_CLIENT_SECRET": "not-a-real-secret", "GMAIL_REFRESH_TOKEN": "not-a-real-token"}


class Store:
    def __init__(self, tailoring=True, telegram=True, digest=True, approval="review", gmail=False):
        self.flags = {"tailoring": tailoring, "telegram": telegram, "digest": digest, "gmail": gmail}
        self.approval = approval
        self.gmail_state = {"lookback_days": 60, "from_now": False, "search_cursor": None, "token_status": "none", "token_checked_at": None,
                            "last_run_at": None, "last_error": None, "messages_seen": 0, "pending": 0, "accepted": 0, "dismissed": 0}

    def features(self): return dict(self.flags)

    def update_features(self, **kw):
        self.flags.update(kw)
        return self.features()

    def tailor_settings(self): return {"approval": self.approval}

    # gmail
    def gmail_overview(self): return dict(self.gmail_state)

    def update_gmail_settings(self, **kw):
        self.gmail_state.update(kw)
        return self.gmail_overview()

    login: list = []

    def record_gmail_login(self, status, error, *, ran=False):
        self.login = [*self.login, (status, error, ran)]
        self.gmail_state.update(token_status=status, last_error=error)

    # schedules
    ROWS = {"collect": {"name": "collect", "batch": 18, "model": None}, "score": {"name": "score", "batch": 10, "model": "claude-haiku-5-5"},
            "tailor": {"name": "tailor", "batch": 3, "model": None}, "digest": {"name": "digest", "batch": 10, "model": None},
            "mail": {"name": "mail", "batch": 20, "model": None}}

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
    def setup(store, telegram_env=True, gmail_env=False):
        for k in (*TG_ENV, *GMAIL_ENV):
            monkeypatch.delenv(k, raising=False)
        if telegram_env:
            for k, v in TG_ENV.items():
                monkeypatch.setenv(k, v)
        if gmail_env:
            for k, v in GMAIL_ENV.items():
                monkeypatch.setenv(k, v)
        monkeypatch.setattr(A, "get_store", lambda: store)
        return TestClient(A.app)
    return setup


# ------------------------------------------------------------------ /features

def test_features_report_shows_flags_effective_state_and_how_approval_behaves(env):
    c = env(Store(), telegram_env=False)
    body = c.get("/features").json()
    assert body["flags"] == {"tailoring": True, "telegram": True, "digest": True, "gmail": False}
    assert body["effective"]["telegram"]["on"] is False and "not configured" in body["effective"]["telegram"]["reason"]
    assert body["approval"] == "review" and body["approval_effective"] == "auto", "no Telegram: review behaves as auto"
    assert any("review → behaves as auto" in line for line in body["lines"])


def test_features_set_changes_only_what_was_given(env):
    store = Store()
    c = env(store)
    body = c.post("/features", json={"tailoring": False}).json()
    assert store.flags == {"tailoring": False, "telegram": True, "digest": True, "gmail": False}
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
    assert r.status_code == 409 and "features set --tailoring on" in r.json()["detail"]["errors"][0]


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
    assert env(Store(telegram=False)).post("/telegram/poll").json() == {"telegram": "switched off (jobsearch features set --telegram on)", "updates": 0}
    body = env(Store(), telegram_env=False).post("/telegram/poll").json()
    assert body["updates"] == 0 and "not configured" in body["telegram"]


# ------------------------------------------------------------------ schedules

def test_the_tick_claims_everything_when_all_parts_are_on(env):
    store = Store(gmail=True)
    body = env(store, gmail_env=True).post("/schedule/tick").json()
    assert [d["name"] for d in body["due"]] == ["collect", "score", "tailor", "digest", "mail"] and body["skipped"] == []
    assert body["due"][1] == {"name": "score", "batch": 10, "model": "claude-haiku-5-5"}
    assert body["due"][4] == {"name": "mail", "batch": 20, "model": None}


def test_the_mail_stage_is_not_claimed_until_gmail_is_switched_on_and_configured(env):
    store = Store()                                                  # the default: Gmail off
    body = env(store, gmail_env=True).post("/schedule/tick").json()
    assert store.skipped == ["mail"] and "mail" not in [d["name"] for d in body["due"]]
    store = Store(gmail=True)                                         # switched on but no Google login in .env
    env(store, gmail_env=False).post("/schedule/tick")
    assert store.skipped == ["mail"], "unconfigured behaves like switched off, so it fires promptly once it is set up"
    store = Store(gmail=True)
    env(store, gmail_env=True).post("/schedule/tick")
    assert store.skipped == []


def test_the_tick_leaves_switched_off_stages_alone(env):
    store = Store(tailoring=False, gmail=True)
    body = env(store, gmail_env=True).post("/schedule/tick").json()
    assert store.skipped == ["tailor"] and "tailor" not in [d["name"] for d in body["due"]]
    store = Store(telegram=False, gmail=True)
    env(store, gmail_env=True).post("/schedule/tick")
    assert store.skipped == ["digest"], "no Telegram: the digest is not even claimed, so it fires when Telegram is back"
    store = Store(tailoring=False, digest=False, gmail=True)
    env(store, gmail_env=True).post("/schedule/tick")
    assert sorted(store.skipped) == ["digest", "tailor"]
    env(Store(gmail=True), telegram_env=False, gmail_env=True).post("/schedule/tick")           # unconfigured behaves like switched off
    store = Store(gmail=True)
    env(store, telegram_env=False, gmail_env=True).post("/schedule/tick")
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
    assert r.json()["timezone"] == "Asia/Kolkata" and len(r.json()["schedules"]) == 5 and len(r.json()["lines"]) == 5
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



# ------------------------------------------------------------------ the optional Gmail part (Phase 6, step 6a)

def test_the_gmail_switch_is_in_the_features_report_and_off_by_default(env):
    body = env(Store(), gmail_env=True).get("/features").json()
    assert body["flags"]["gmail"] is False
    assert body["effective"]["gmail"] == {"on": False, "reason": "switched off (jobsearch features set --gmail on)"}
    assert any(line.startswith("gmail:") and "off" in line for line in body["lines"])


def test_switching_gmail_on_needs_the_google_login_to_be_in_effect(env):
    store = Store()
    c = env(store, gmail_env=False)
    body = c.post("/features", json={"gmail": True}).json()
    assert store.flags["gmail"] is True and body["flags"]["gmail"] is True
    assert body["effective"]["gmail"]["on"] is False and "not configured" in body["effective"]["gmail"]["reason"]
    store = Store()
    body = env(store, gmail_env=True).post("/features", json={"gmail": True}).json()
    assert body["effective"]["gmail"] == {"on": True, "reason": ""}
    assert c.post("/features", json={"gmail": "perhaps"}).status_code == 422


def test_switching_gmail_does_not_touch_the_other_parts(env):
    store = Store()
    env(store).post("/features", json={"gmail": True})
    assert store.flags == {"tailoring": True, "telegram": True, "digest": True, "gmail": True}


def test_the_mail_status_of_a_fresh_install(env):
    body = env(Store(), gmail_env=False).get("/mail/status").json()
    assert body["enabled"] is False and body["configured"] is False and "switched off" in body["reason"]
    assert body["lookback_days"] == 60 and body["from_now"] is False and body["search_cursor"] is None
    assert body["pending"] == 0 and body["messages_seen"] == 0
    assert any("the first search looks back 60 days" in line for line in body["lines"])
    assert any("not set up" in line for line in body["lines"])


def test_the_mail_status_when_it_is_on_and_configured(env):
    body = env(Store(gmail=True), gmail_env=True).get("/mail/status").json()
    assert body["enabled"] is True and body["configured"] is True and body["reason"] == ""
    assert any("set in .env, not used yet" in line for line in body["lines"])


def test_the_mail_status_never_returns_the_login_values(env):
    text = env(Store(gmail=True), gmail_env=True).get("/mail/status").text
    for value in GMAIL_ENV.values():
        assert value not in text


def test_mail_settings_are_changed_one_at_a_time_and_validated(env):
    store = Store()
    c = env(store)
    body = c.post("/mail/settings", json={"lookback_days": 30}).json()
    assert store.gmail_state["lookback_days"] == 30 and body["lookback_days"] == 30
    assert any("looks back 30 days" in line for line in body["lines"])
    body = c.post("/mail/settings", json={"from_now": True}).json()
    assert store.gmail_state["from_now"] is True and store.gmail_state["lookback_days"] == 30, "the other setting is kept"
    assert any("fetches nothing" in line for line in body["lines"])


@pytest.mark.parametrize("body,message", [
    ({"lookback_days": 0}, "from 1 to 365"), ({"lookback_days": 366}, "from 1 to 365"), ({}, "nothing to change"),
    ({"token_status": "ok"}, "nothing to change"), ({"search_cursor": "2026-01-01"}, "nothing to change")])
def test_a_bad_mail_settings_change_is_refused_and_changes_nothing(env, body, message):
    store = Store()
    before = dict(store.gmail_state)
    r = env(store).post("/mail/settings", json=body)
    assert r.status_code == 422 and message in r.json()["detail"]["errors"][0]
    assert store.gmail_state == before


@pytest.mark.parametrize("value", ["soon", True, 2.5, [1]])
def test_mail_settings_types_are_checked(env, value):
    store = Store()
    assert env(store).post("/mail/settings", json={"lookback_days": value}).status_code == 422
    assert store.gmail_state["lookback_days"] == 60


def test_the_mail_stage_is_a_quiet_noop_with_a_reason_when_off(env):
    store = Store()
    calls = []
    store.gmail_overview = lambda: calls.append("read") or {}
    body = env(store).post("/mail/run").json()
    assert body == {"skipped": "switched off (jobsearch features set --gmail on)", "checked": 0} and calls == [], "nothing is read while it is off"


@pytest.fixture
def google(monkeypatch):
    """A stand-in Google login and Gmail API whose credentials are the ones in GMAIL_ENV."""
    import gmail_client
    import stubs
    monkeypatch.setattr(gmail_client, "RETRY_DELAY_S", 0)
    with stubs.StubGmail(refresh_token=GMAIL_ENV["GMAIL_REFRESH_TOKEN"], client_id=GMAIL_ENV["GMAIL_CLIENT_ID"], client_secret=GMAIL_ENV["GMAIL_CLIENT_SECRET"]) as s:
        monkeypatch.setenv("GMAIL_API_URL", s.url)
        yield s


def test_the_mail_stage_runs_with_the_mail_schedules_batch_and_model_when_on(env, google, monkeypatch):
    store = Store(gmail=True)
    calls = []
    monkeypatch.setattr(A.mail, "run_stage", lambda eff, s, e, **kw: calls.append((eff["on"], kw)) or {"checked": 7, "suggested": 1, "login": "ok"})
    body = env(store, gmail_env=True).post("/mail/run").json()
    assert body == {"checked": 7, "suggested": 1, "login": "ok"} and len(calls) == 1
    assert calls[0][0] is True and calls[0][1]["batch"] == store.schedule_row("mail")["batch"] and "model" in calls[0][1]


def test_a_second_mail_run_while_one_is_going_is_refused_not_queued(env, monkeypatch):
    store = Store(gmail=True)
    client = env(store, gmail_env=True)
    inner = []
    def slow_stage(eff, s, e, **kw):
        inner.append(client.post("/mail/run").json())          # a second request arrives while the first is still running
        return {"checked": 0, "login": "ok"}
    monkeypatch.setattr(A.mail, "run_stage", slow_stage)
    outer = client.post("/mail/run").json()
    assert outer == {"checked": 0, "login": "ok"} and inner == [{"skipped": "a mail run is already in progress", "checked": 0}]
    assert client.post("/mail/run").status_code == 200, "the lock is released afterwards"


def test_the_mail_stage_says_what_is_missing_when_on_but_not_configured(env):
    body = env(Store(gmail=True), gmail_env=False).post("/mail/run").json()
    assert body["checked"] == 0 and "not configured" in body["skipped"]


def test_the_mail_stage_makes_no_google_call_while_the_part_is_off(env, google):
    body = env(Store(), gmail_env=True).post("/mail/run").json()
    assert "switched off" in body["skipped"] and google.token_requests == [] and google.api_log == []


def test_the_mail_stage_reports_an_expired_login(env, google):
    google.modes["token"] = "invalid_grant"
    store = Store(gmail=True)
    body = env(store, gmail_env=True).post("/mail/run").json()
    assert body["login"] == "expired" and "sign in again" in body["skipped"] and store.login[0][0] == "expired"


def test_checking_the_login_works_before_the_part_is_switched_on(env, google):
    store = Store()                                                       # Gmail is off
    body = env(store, gmail_env=True).post("/mail/check").json()
    assert body["status"] == "ok" and body["account"] == "j***@gmail.com"
    assert body["enabled"] is False, "the part is still switched off: checking the login does not switch anything on"
    assert store.login == [("ok", None, False)]
    assert any("never run" in l or "last started" in l for l in body["lines"])


def test_checking_a_login_that_is_not_set_up_is_a_409_that_names_the_missing_values(env, google):
    r = env(Store(), gmail_env=False).post("/mail/check")
    assert r.status_code == 409 and "GMAIL_CLIENT_ID" in r.json()["detail"]["errors"][0] and google.token_requests == []


def test_checking_a_bad_login_reports_it_and_records_it(env, google):
    google.modes["token"] = "overbroad"
    store = Store()
    body = env(store, gmail_env=True).post("/mail/check").json()
    assert body["status"] == "error" and "read-only" in body["message"] and store.login[0][0] == "error"


def test_no_credential_or_full_address_is_in_any_mail_response(env, google):
    c = env(Store(gmail=True), gmail_env=True)
    text = c.post("/mail/check").text + c.post("/mail/run").text + c.get("/mail/status").text
    for value in (*GMAIL_ENV.values(), "jane.doe@gmail.com", google.issued[0]):
        assert value not in text


def test_an_old_database_gets_a_clear_message_not_a_crash(env):
    """The store raises LookupError for a database without the Gmail tables (tests/test_premigration.py checks the real store
    does, for each of these calls); the endpoints turn it into a 400 that says what to do."""
    def missing(*a, **k):
        raise LookupError("the Gmail tables are not there yet: run scripts/jobsearch migrate")
    cases = [("gmail_overview", lambda c: c.get("/mail/status")),
             ("gmail_overview", lambda c: c.post("/mail/settings", json={"lookback_days": 5})),     # it reports back after the change
             ("update_gmail_settings", lambda c: c.post("/mail/settings", json={"lookback_days": 5}))]
    for method, call in cases:
        store = Store()
        setattr(store, method, missing)
        r = call(env(store))
        assert r.status_code == 400 and "scripts/jobsearch migrate" in r.json()["detail"]["errors"][0], method


def test_switching_gmail_on_for_a_database_without_the_switch_is_a_clear_400(env):
    store = Store()
    def refuse(**kw):
        raise LookupError("this database has no Gmail switch yet: run scripts/jobsearch migrate")
    store.update_features = refuse
    r = env(store).post("/features", json={"gmail": True})
    assert r.status_code == 400 and "scripts/jobsearch migrate" in r.json()["detail"]["errors"][0]


def test_the_status_report_works_with_the_timestamps_the_database_really_returns(env):
    """gmail_overview() is a jsonb report: its timestamps are ISO strings. This was a 503 (datetime - str)."""
    store = Store(gmail=True)
    store.gmail_state.update(search_cursor="2026-10-12T11:40:00.5+00:00", last_run_at="2026-10-12T11:41:00+00:00",
                             token_status="ok", token_checked_at="2026-10-12T11:41:00.123456+00:00")
    r = env(store, gmail_env=True).get("/mail/status")
    assert r.status_code == 200 and any("last started" in line for line in r.json()["lines"])
    assert any("works (checked" in line for line in r.json()["lines"])


def test_the_mail_schedule_can_be_changed_like_the_others(env):
    store = Store()
    c = env(store)
    assert c.post("/schedule/mail", json={"every_minutes": 60, "batch": 10}).status_code == 200 and store.updated == ("mail", {"every_minutes": 60, "batch": 10})
    r = c.post("/schedule/mail", json={"every_minutes": 5})
    assert r.status_code == 422 and "kept gentle on Gmail" in r.json()["detail"]["errors"][0]
    assert c.post("/schedule/mail/run").status_code == 200 and store.run_requested == "mail"


def test_a_rescan_reopens_old_mail_and_reports_the_state(env):
    store = Store()
    store.gmail_rescan = lambda: 7
    body = env(store).post("/mail/rescan").json()
    assert body["reopened"] == 7 and body["lookback_days"] == 60 and any("search" in line for line in body["lines"])


def test_a_rescan_on_an_old_database_is_a_clear_message(env):
    store = Store()
    def missing():
        raise LookupError("this database has no Gmail search yet: run scripts/jobsearch migrate")
    store.gmail_rescan = missing
    r = env(store).post("/mail/rescan")
    assert r.status_code == 400 and "scripts/jobsearch migrate" in r.json()["detail"]["errors"][0]



# ------------------------------------------------------------------ /digest/sent marks what the message told you (step 6e)

def test_digest_sent_marks_the_jobs_and_the_mail_suggestions_it_announced(env):
    store = Store()
    store.marked, store.mail_marked = [], []
    store.mark_notified = lambda ids: store.marked.append(ids) or len(ids)
    store.gmail_digest_sent = lambda ids, login=False: store.mail_marked.append((list(ids), login)) or len(ids)
    body = env(store).post("/digest/sent", json={"ids": [1, 2], "suggestion_ids": [7, 8, 9], "login_alert": True}).json()
    assert body == {"marked": 2, "mail_marked": 3} and store.marked == [[1, 2]] and store.mail_marked == [([7, 8, 9], True)]


def test_digest_sent_from_the_old_workflow_sends_only_ids_and_touches_no_mail(env):
    store = Store()
    store.mail_marked = []
    store.mark_notified = lambda ids: len(ids)
    store.gmail_digest_sent = lambda ids, login=False: store.mail_marked.append((ids, login)) or 0
    body = env(store).post("/digest/sent", json={"ids": [4]}).json()
    assert body == {"marked": 1, "mail_marked": 0} and store.mail_marked == []


# ---- tailoring v2 settings (migration 026) and proof points

def test_the_v2_settings_are_parsed_like_the_others():
    import telegram_bot as TB
    assert TB.normalise_settings({"engine": "v2", "gap_policy": "bridge", "cover_style": "classic", "tone": "conversational",
                                  "reviewer": False, "company_research": True, "reviewer_model": "claude-haiku-5-5"}) == {
        "engine": "v2", "gap_policy": "bridge", "cover_style": "classic", "tone": "conversational", "reviewer": False,
        "company_research": True, "reviewer_model": "claude-haiku-5-5"}
    for bad in ({"engine": "v3"}, {"gap_policy": "hide"}, {"cover_style": "fancy"}, {"tone": "rude"}, {"reviewer": "maybe"}, {"reviewer_model": "x"}, {"company_research": "perhaps"}):
        with pytest.raises(ValueError):
            TB.normalise_settings({k: (v if not isinstance(v, bool) else v) for k, v in bad.items()})
    assert "engine: v1" in TB.settings_text({"mode": "auto", "approval": "review", "min_score": None, "max_score": None, "daily_limit": None,
                                             "cover_letter": True, "max_pages": 1, "model": "m"})
    assert "engine: v2" in TB.settings_text({"mode": "auto", "approval": "review", "min_score": None, "max_score": None, "daily_limit": None,
                                             "cover_letter": True, "max_pages": 1, "model": "m", "engine": "v2"})


def test_proof_points_are_loaded_validated_and_a_bad_file_never_stops_tailoring(tmp_path, monkeypatch):
    resume = {"work": [{"id": "w1", "highlights": [{"id": "w1-b1", "text": "Built X."}]}]}
    path = tmp_path / "proof_points.json"
    monkeypatch.setenv("PROOF_POINTS_PATH", str(path))
    assert A._load_proof_points(resume) == []                                                          # no file: none
    path.write_text("{not json")
    assert A._load_proof_points(resume) == []                                                          # unreadable: none, not an error
    good = {"id": "pp-1", "bullet": "w1-b1", "project": "P", "role": "built", "what": "Did X.", "metric": "", "tech": [], "evidence": ""}
    path.write_text(json.dumps({"proof_points": [good, dict(good, id="pp-2", bullet="w9-b9")]}))
    assert [p["id"] for p in A._load_proof_points(resume)] == ["pp-1"]                                  # the valid one is kept, the other is logged and skipped


def test_a_proof_points_file_that_is_not_utf8_never_stops_tailoring(tmp_path, monkeypatch):
    path = tmp_path / "proof_points.json"
    path.write_bytes(b"{\xff\xfe not utf-8")
    monkeypatch.setenv("PROOF_POINTS_PATH", str(path))
    assert A._load_proof_points({"work": []}) == []
