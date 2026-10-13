"""The scheduled `mail` stage (Phase 6, step 6e): the login check, the search, the model step, and what is recorded. The search and the
model step have their own tests; here they are replaced by scripted answers, so what is checked is the stage's own job: the order, the
counts it keeps (never an email's text), when a run counts as ended early, and what each failure does."""
import pytest

import features
import gmail_client as G
import mail as M
import mail_ai
import mail_search
import stubs

GMAIL_ENV = {"GMAIL_CLIENT_ID": "id.apps.example", "GMAIL_CLIENT_SECRET": "not-a-real-secret", "GMAIL_REFRESH_TOKEN": "not-a-real-token", "ANTHROPIC_API_KEY": "sk-test"}
ON = {"on": True, "reason": ""}
OFF = {"on": False, "reason": "switched off (jobsearch features set --gmail on)"}

SEARCH_OK = {"opened": 12, "suggested": 2, "noise": 3, "unmatched": 5, "unclear": 2, "failed": 0, "listed": 14, "complete": True, "stopped": "", "names_skipped": 0}
CLASSIFY_OK = {"claimed": 2, "asked": 2, "suggested": 1, "noise": 1, "failed": 0, "bad_answers": 0, "skipped": 0, "gone": 0, "released": 0, "spent": 0.00034,
               "stopped": "", "capped": False}


class Store:
    def __init__(self):
        self.login, self.runs = [], []

    def record_gmail_login(self, status, error, *, ran=False):
        self.login.append((status, error, ran))

    def gmail_record_run(self, summary, early):
        self.runs.append((summary, early))
        return True


class Client:
    closed = 0

    def profile(self):
        return {"email": "jane.doe@gmail.com"}

    def close(self):
        Client.closed += 1


@pytest.fixture
def script(monkeypatch):
    """Scripted search and model step. `calls` records what the stage handed them."""
    state = {"search": dict(SEARCH_OK), "classify": dict(CLASSIFY_OK), "calls": []}

    def run_search(client, store, **kw):
        state["calls"].append(("search", kw))
        if isinstance(state["search"], Exception):
            raise state["search"]
        return state["search"]

    def run_classify(client, ai, store, **kw):
        state["calls"].append(("classify", kw))
        if isinstance(state["classify"], Exception):
            raise state["classify"]
        return state["classify"]

    monkeypatch.setattr(mail_search, "run_search", run_search)
    monkeypatch.setattr(mail_ai, "run_classify", run_classify)
    Client.closed = 0
    return state


def stage(store, script, *, eff=ON, env=GMAIL_ENV, ai=object(), **kw):
    return M.run_stage(eff, store, env, make_client=lambda e: Client(), make_ai=lambda e: ai, **kw)


def test_a_clean_run_searches_then_asks_the_model_and_records_counts_only(script):
    store = Store()
    out = stage(store, script, batch=20)
    assert [c[0] for c in script["calls"]] == ["search", "classify"] and script["calls"][0][1]["batch"] == 20
    assert out["checked"] == 12 and out["suggested"] == 3 and out["login"] == "ok" and out["early"] is False and "error" not in out
    assert store.login == [("ok", None, True)]
    (summary, early), = store.runs
    assert early is False and summary["search"]["opened"] == 12 and summary["classify"]["asked"] == 2 and summary["classify"]["spent"] == 0.00034
    assert Client.closed == 1


def test_what_is_recorded_has_numbers_flags_and_short_reasons_only(script):
    script["search"] = {**SEARCH_OK, "stopped": "Gmail is limiting requests"}
    store = Store()
    stage(store, script)
    summary = store.runs[0][0]
    def walk(x):
        if isinstance(x, dict):
            for v in x.values():
                yield from walk(v)
        else:
            yield x
    assert all(isinstance(v, (int, float, bool, str)) for v in walk(summary)) and all(len(v) <= 120 for v in walk(summary) if isinstance(v, str))
    assert set(summary["search"]) <= set(M.SEARCH_KEYS) | {"complete", "stopped"} and set(summary["classify"]) <= set(M.CLASSIFY_KEYS) | {"spent", "stopped", "note"}


def test_the_part_being_off_does_nothing_at_all(script):
    made = []
    out = M.run_stage(OFF, Store(), GMAIL_ENV, make_client=lambda e: made.append(1))
    assert "switched off" in out["skipped"] and made == [] and script["calls"] == []


def test_a_missing_google_login_is_said_not_raised(script):
    eff = features.effective({"gmail": True}, {})["gmail"]
    out = M.run_stage(eff, Store(), {})
    assert "not configured" in out["skipped"] or "switched" in out["skipped"]
    out = M.run_stage(ON, Store(), {})
    assert out["login"] == "none" and script["calls"] == []


@pytest.mark.parametrize("modes,status", [({"token": "invalid_grant"}, "expired"), ({"token": "overbroad"}, "error")])
def test_a_login_that_does_not_work_stops_the_run_before_any_search(script, monkeypatch, modes, status):
    monkeypatch.setattr(G, "RETRY_DELAY_S", 0)
    store = Store()
    with stubs.StubGmail(modes=modes, refresh_token=GMAIL_ENV["GMAIL_REFRESH_TOKEN"], client_id=GMAIL_ENV["GMAIL_CLIENT_ID"], client_secret=GMAIL_ENV["GMAIL_CLIENT_SECRET"]) as g:
        monkeypatch.setenv("GMAIL_API_URL", g.url)
        out = M.run_stage(ON, store, GMAIL_ENV, make_ai=lambda e: object())
    assert out["login"] == status and out["checked"] == 0 and script["calls"] == [] and store.login[0][0] == status and store.runs == []


def test_a_login_that_expires_in_the_middle_of_a_run_is_recorded_and_ends_it(script):
    script["search"] = G.LoginExpired("Google no longer accepts the saved login: sign in again with scripts/jobsearch gmail login")
    store = Store()
    out = stage(store, script)
    assert out["login"] == "expired" and "sign in again" in out["skipped"] and len(store.runs) == 1 and store.runs[0][1] is True
    assert store.login == [("ok", None, True), ("expired", out["skipped"], False)] and Client.closed == 1


def test_an_overbroad_login_in_the_middle_of_a_run_is_recorded_as_an_error(script):
    script["classify"] = G.Overbroad("the login grants more than read-only access")
    store = Store()
    out = stage(store, script)
    assert out["login"] == "error" and store.login[-1][0] == "error" and len(store.runs) == 1


def test_a_search_that_ran_out_of_time_counts_as_an_early_run_and_the_model_step_still_gets_its_turn(script):
    script["search"] = {**SEARCH_OK, "complete": False, "stopped": "ran out of time: the rest waits for the next run"}
    store = Store()
    out = stage(store, script)
    assert out["early"] is True and "ran out of time" in out["error"] and store.runs[0][1] is True
    assert [c[0] for c in script["calls"]] == ["search", "classify"]


def test_when_gmail_is_limiting_requests_the_model_step_waits_because_it_would_only_ask_gmail_again(script):
    script["search"] = {**SEARCH_OK, "complete": False, "stopped": "Gmail is limiting requests: it will try again at the next run"}
    store = Store()
    out = stage(store, script)
    assert out["early"] is True and [c[0] for c in script["calls"]] == ["search"]
    assert "limiting" in store.runs[0][0]["classify"]["note"]


def test_a_model_step_that_failed_is_early_but_one_that_hit_a_cap_is_not(script):
    script["classify"] = {**CLASSIFY_OK, "stopped": "the Claude API did not answer (HTTP 529): the rest waits for the next run", "capped": False}
    store = Store()
    assert stage(store, script)["early"] is True
    script["classify"] = {**CLASSIFY_OK, "stopped": "the daily limit of model calls for emails is used up", "capped": True}
    store = Store()
    out = stage(store, script)
    assert out["early"] is False and store.runs[0][1] is False and "error" not in out


def test_without_the_claude_key_the_model_step_is_skipped_and_the_run_is_clean(script):
    store = Store()
    out = M.run_stage(ON, store, {k: v for k, v in GMAIL_ENV.items() if k != "ANTHROPIC_API_KEY"}, make_client=lambda e: Client())
    assert [c[0] for c in script["calls"]] == ["search"] and out["early"] is False
    assert "ANTHROPIC_API_KEY" in store.runs[0][0]["classify"]["note"]


def test_the_mail_schedules_model_is_passed_on_only_when_set(script):
    stage(Store(), script)
    assert "model" not in script["calls"][1][1]
    script["calls"].clear()
    stage(Store(), script, model="claude-haiku-5-5")
    assert script["calls"][1][1]["model"] == "claude-haiku-5-5"


def test_the_stage_fits_the_workflows_five_minute_timeout(script):
    stage(Store(), script)
    search_kw, classify_kw = script["calls"][0][1], script["calls"][1][1]
    assert search_kw["deadline_s"] + classify_kw["deadline_s"] < 300


def test_a_nonsense_batch_never_reaches_the_search_as_zero_or_negative(script):
    stage(Store(), script, batch=0)
    assert script["calls"][0][1]["batch"] == 1


def test_the_client_is_closed_even_when_the_search_blows_up(script):
    script["search"] = RuntimeError("the database is unwell")
    with pytest.raises(RuntimeError):
        stage(Store(), script)
    assert Client.closed == 1


def test_the_real_default_ai_is_none_without_a_key_and_a_client_with_one(monkeypatch):
    assert M._default_ai({}) is None
    assert M._default_ai({"ANTHROPIC_API_KEY": "sk-test"}) is not None


@pytest.mark.parametrize("mode", ["rate_limited", "server_error"])
def test_a_rate_limit_or_an_outage_on_the_login_check_is_an_early_run_not_a_login_that_stopped(script, monkeypatch, mode):
    monkeypatch.setattr(G, "RETRY_DELAY_S", 0)
    store = Store()
    with stubs.StubGmail(modes={"api": mode}, refresh_token=GMAIL_ENV["GMAIL_REFRESH_TOKEN"], client_id=GMAIL_ENV["GMAIL_CLIENT_ID"], client_secret=GMAIL_ENV["GMAIL_CLIENT_SECRET"]) as g:
        monkeypatch.setenv("GMAIL_API_URL", g.url)
        out = M.run_stage(ON, store, GMAIL_ENV, make_ai=lambda e: object())
    assert out["login"] == "unchecked" and out["early"] is True and out["checked"] == 0 and script["calls"] == []
    assert store.login == [], "the stored login status is untouched: nothing says the login stopped working"
    assert len(store.runs) == 1 and store.runs[0][1] is True and store.runs[0][0]["stopped"] and "search" not in store.runs[0][0]


def test_check_login_marks_which_errors_are_transient(monkeypatch):
    class C:
        def __init__(self, exc):
            self.exc = exc
        def profile(self):
            raise self.exc
    assert M.check_login(C(G.RateLimited("rate limited")))["transient"] is True
    assert M.check_login(C(G.Unavailable("Gmail is unavailable")))["transient"] is True
    assert M.check_login(C(G.GmailError("the Gmail API is not enabled for this Google project")))["transient"] is False
    assert M.check_login(C(G.Overbroad("more than read-only")))["transient"] is False
    assert M.check_login(C(G.LoginExpired("expired")))["status"] == "expired"


def test_an_unexpected_failure_is_recorded_as_an_early_run_with_what_was_done_so_far_and_then_raised(script):
    script["classify"] = KeyError("pricing")
    store = Store()
    with pytest.raises(KeyError):
        stage(store, script)
    (summary, early), = store.runs
    assert early is True and summary["search"]["opened"] == 12 and "KeyError" in summary["stopped"] and "pricing" not in summary["stopped"]


def test_a_failure_to_record_a_failure_never_hides_the_real_one(script):
    class Broken(Store):
        def gmail_record_run(self, summary, early):
            raise RuntimeError("the database is gone")
    script["search"] = ValueError("the real problem")
    with pytest.raises(ValueError, match="the real problem"):
        stage(Broken(), script)


def test_a_login_that_expires_mid_run_keeps_the_counts_of_what_the_run_did_before(script):
    script["classify"] = G.LoginExpired("Google no longer accepts the saved login")
    store = Store()
    out = stage(store, script)
    assert out["login"] == "expired"
    (summary, early), = store.runs
    assert early is True and summary["search"]["opened"] == 12 and "no longer accepts" in summary["stopped"]


def test_a_manual_check_during_an_outage_is_shown_but_never_stored_as_a_bad_login(monkeypatch):
    monkeypatch.setattr(G, "RETRY_DELAY_S", 0)
    store = Store()
    with stubs.StubGmail(modes={"api": "server_error"}, refresh_token=GMAIL_ENV["GMAIL_REFRESH_TOKEN"], client_id=GMAIL_ENV["GMAIL_CLIENT_ID"], client_secret=GMAIL_ENV["GMAIL_CLIENT_SECRET"]) as g:
        monkeypatch.setenv("GMAIL_API_URL", g.url)
        result = M.check(store, GMAIL_ENV)
    assert result["status"] == "error" and result["transient"] is True and store.login == []


@pytest.mark.parametrize("mode,transient", [("server_error", True), ("rate_limited", True), ("api_disabled", False), ("garbage", False)])
def test_only_a_rate_limit_or_an_outage_is_transient_a_refusal_is_not(monkeypatch, mode, transient):
    monkeypatch.setattr(G, "RETRY_DELAY_S", 0)
    store = Store()
    with stubs.StubGmail(modes={"api": mode}, refresh_token=GMAIL_ENV["GMAIL_REFRESH_TOKEN"], client_id=GMAIL_ENV["GMAIL_CLIENT_ID"], client_secret=GMAIL_ENV["GMAIL_CLIENT_SECRET"]) as g:
        monkeypatch.setenv("GMAIL_API_URL", g.url)
        result = M.check(store, GMAIL_ENV)
    assert result["status"] == "error" and result["transient"] is transient and bool(store.login) is (not transient)


def test_the_claude_client_the_stage_builds_cannot_hang_for_minutes():
    ai = M._default_ai({"ANTHROPIC_API_KEY": "sk-test"})
    assert ai.retries == 1 and ai.client.timeout.read <= M.CLAUDE_TIMEOUT_S


def test_the_models_share_of_the_stage_is_what_is_left_of_the_budget(script, monkeypatch):
    clock = iter([0.0, 200.0])
    monkeypatch.setattr(M.time, "monotonic", lambda: next(clock))
    stage(Store(), script)
    assert script["calls"][1][1]["deadline_s"] == max(M.MIN_CLASSIFY_S, min(M.CLASSIFY_DEADLINE_S, M.STAGE_BUDGET_S - 200.0))
