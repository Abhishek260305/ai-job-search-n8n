"""Gmail status tracking, step 6a: the settings rules, the report, and the quiet stage. No mailbox is read."""
from datetime import datetime, timedelta, timezone

import pytest

import features
import mail as M

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)


def overview(**kw):
    base = {"lookback_days": 60, "from_now": False, "search_cursor": None, "token_status": "none", "token_checked_at": None,
            "last_run_at": None, "last_error": None, "messages_seen": 0, "pending": 0, "accepted": 0, "dismissed": 0}
    return {**base, **kw}


OFF = {"on": False, "reason": "switched off (jobsearch features set --gmail on)"}
ON = {"on": True, "reason": ""}


# ---------------------------------------------------------------- unclear emails (step 6d)

def test_the_unclear_line_appears_only_when_something_waits_or_is_parked():
    quiet = "\n".join(M.describe(overview(unclear={"waiting": 0, "parked": 0}), ON, configured=True, now=NOW))
    assert "unclear" not in quiet and "unclear" not in "\n".join(M.describe(overview(), ON, configured=True, now=NOW))
    waiting = "\n".join(M.describe(overview(unclear={"waiting": 3, "parked": 0}), ON, configured=True, now=NOW))
    assert "unclear:    3 wait for a closer look by Claude" in waiting and "parked" not in waiting
    both = "\n".join(M.describe(overview(unclear={"waiting": 0, "parked": 2}), ON, configured=True, now=NOW))
    assert "2 parked" in both and "gmail rescan" in both


# ---------------------------------------------------------------- the two settings

def test_valid_settings_are_cleaned():
    assert M.validate_settings({"lookback_days": 30}) == {"lookback_days": 30}
    assert M.validate_settings({"from_now": True}) == {"from_now": True}
    assert M.validate_settings({"lookback_days": 1, "from_now": False}) == {"lookback_days": 1, "from_now": False}
    assert M.validate_settings({"lookback_days": 365}) == {"lookback_days": 365}
    assert M.validate_settings({"lookback_days": None, "from_now": None}) == {}, "unset values are ignored"


@pytest.mark.parametrize("value", [0, -1, 366, 10000, True, False, 2.5, "30", [30]])
def test_a_bad_lookback_is_refused(value):
    with pytest.raises(ValueError, match="from 1 to 365"):
        M.validate_settings({"lookback_days": value})


@pytest.mark.parametrize("value", ["yes", 1, 0, "true", []])
def test_from_now_must_be_a_real_boolean(value):
    with pytest.raises(ValueError, match="true or false"):
        M.validate_settings({"from_now": value})


def test_an_unknown_setting_is_refused():
    with pytest.raises(ValueError, match="unknown Gmail setting 'token_status'"):
        M.validate_settings({"token_status": "ok"})
    with pytest.raises(ValueError, match="unknown Gmail setting"):
        M.validate_settings({"search_cursor": "2026-01-01"})


# ---------------------------------------------------------------- what the CLI and doctor say

def test_a_fresh_install_says_what_will_happen_on_the_first_search():
    text = "\n".join(M.describe(overview(), OFF, configured=False, now=NOW))
    assert "gmail:      off, switched off" in text
    assert "login:      not set up: scripts/jobsearch gmail login (docs/gmail.md)" in text
    assert "never run; the first search looks back 60 days" in text
    assert "0 looked at; suggestions: 0 waiting for you" in text


def test_from_now_changes_what_the_first_search_does():
    text = "\n".join(M.describe(overview(from_now=True), ON, configured=True, now=NOW))
    assert "fetches nothing" in text and "only mail that arrives afterwards" in text and "looks back" not in text


@pytest.mark.parametrize("state,configured,expected", [
    ({"token_status": "none"}, False, "not set up"),
    ({"token_status": "none"}, True, "set in .env, not used yet"),
    ({"token_status": "ok", "token_checked_at": NOW - timedelta(minutes=12)}, True, "works (checked 12 min ago)"),
    ({"token_status": "expired"}, True, "EXPIRED or revoked: sign in again"),
    ({"token_status": "error", "last_error": "Gmail answered 503"}, True, "the last check failed: Gmail answered 503"),
    ({"token_status": "error", "last_error": None}, True, "unknown error")])
def test_the_login_line(state, configured, expected):
    assert expected in M.login_line(overview(**state), configured, NOW)


def test_a_search_in_progress_history_is_described():
    text = "\n".join(M.describe(overview(search_cursor=NOW - timedelta(minutes=20), last_run_at=NOW - timedelta(minutes=19), messages_seen=14,
                                          pending=3, accepted=2, dismissed=1), ON, configured=True, now=NOW))
    assert "last started 20 min ago" in text and "finished 19 min ago" in text
    assert "14 looked at; suggestions: 3 waiting for you, 2 accepted, 1 dismissed" in text


def test_the_report_shows_only_what_it_means_to_show():
    """Whatever else a state row ever carries (a later step may add columns), the lines are built from named fields only."""
    state = overview(subject="Offer: wire $500 now", excerpt="send us your bank details", refresh_token="1//not-a-real-token",
                     client_secret="not-a-real-secret", from_domain="evil.example", token_status="ok", token_checked_at=NOW - timedelta(minutes=3))
    text = "\n".join(M.describe(state, ON, configured=True, now=NOW))
    for leaked in ("wire $500", "bank details", "not-a-real", "evil.example"):
        assert leaked not in text
    assert "works (checked 3 min ago)" in text


def test_an_error_message_is_shown_as_it_was_stored_and_nothing_more():
    assert "Gmail answered 503" in M.login_line(overview(token_status="error", last_error="Gmail answered 503"), True, NOW)


# ---------------------------------------------------------------- the login check and the scheduled stage

import gmail_client   # noqa: E402
import stubs          # noqa: E402

GMAIL_ENV = {"GMAIL_CLIENT_ID": "id.apps.example", "GMAIL_CLIENT_SECRET": "not-a-real-secret", "GMAIL_REFRESH_TOKEN": "not-a-real-token"}


class LoginStore:
    """Records what the check writes: (status, error, ran)."""

    def __init__(self):
        self.recorded = []

    def record_gmail_login(self, status, error, *, ran=False):
        self.recorded.append((status, error, ran))


@pytest.fixture
def stub(monkeypatch):
    monkeypatch.setattr(gmail_client, "RETRY_DELAY_S", 0)
    with stubs.StubGmail(account="jane.doe@gmail.com", refresh_token=GMAIL_ENV["GMAIL_REFRESH_TOKEN"], client_id=GMAIL_ENV["GMAIL_CLIENT_ID"],
                         client_secret=GMAIL_ENV["GMAIL_CLIENT_SECRET"]) as s:
        monkeypatch.setenv("GMAIL_API_URL", s.url)
        yield s


def never_made(env):
    raise AssertionError("a client must not be made")


def test_a_working_login_reports_the_masked_account_and_is_recorded_without_the_stamp(stub):
    store = LoginStore()
    out = M.check(store, GMAIL_ENV)
    assert out == {"status": "ok", "account": "j***@gmail.com", "message": "the login works"}
    assert store.recorded == [("ok", None, False)], "a plain check does not claim a scheduled run happened"
    assert "jane.doe" not in str(out)


@pytest.mark.parametrize("mode,status,words", [("invalid_grant", "expired", "sign in again"), ("invalid_client", "expired", "client id or secret"),
                                               ("overbroad", "error", "read-only"), ("server_error", "error", "tried twice"),
                                               ("rate_limited", "error", "limiting"), ("garbage", "error", "not JSON")])
def test_a_login_that_does_not_work_is_recorded_with_a_message_that_helps(stub, mode, status, words):
    stub.modes["token"] = mode
    store = LoginStore()
    out = M.check(store, GMAIL_ENV)
    assert out["status"] == status and words in out["message"] and out["account"] == ""
    if mode in ("server_error", "rate_limited"):
        assert out["transient"] is True and store.recorded == [], "a rate limit or an outage is shown, never stored as a bad login"
    else:
        assert not out.get("transient") and store.recorded == [(status, out["message"], False)]
    for secret in GMAIL_ENV.values():
        assert secret not in out["message"]


def test_a_login_that_is_not_set_up_is_none_and_nothing_is_recorded():
    store = LoginStore()
    out = M.check(store, {"GMAIL_CLIENT_ID": "x"})
    assert out["status"] == "none" and "GMAIL_CLIENT_SECRET" in out["message"] and "GMAIL_CLIENT_ID" not in out["message"]
    assert store.recorded == []


def test_a_message_that_would_contain_a_credential_is_redacted_before_it_is_recorded():
    class Leaky:
        def profile(self):
            raise gmail_client.GmailError("Google said ya29.a0AfH6SMBxabcdefghijk and refresh_token=1//0gAbCdEfGhIjKlMnOp")
    out = M.check_login(Leaky())
    assert "ya29" not in out["message"] and "1//0g" not in out["message"] and "[removed]" in out["message"]


def test_the_stage_is_quiet_and_says_why_when_the_part_is_off():
    store = LoginStore()
    assert M.run_stage(OFF, store, GMAIL_ENV, make_client=never_made) == {"skipped": "switched off (jobsearch features set --gmail on)", "checked": 0}
    assert store.recorded == []


def test_the_stage_reports_an_expired_login_instead_of_pretending(stub):
    stub.modes["token"] = "invalid_grant"
    store = LoginStore()
    out = M.run_stage(ON, store, GMAIL_ENV)
    assert out["login"] == "expired" and "sign in again" in out["skipped"] and out["checked"] == 0
    assert store.recorded[0][0] == "expired" and store.recorded[0][2] is True


def test_the_stage_reason_comes_from_the_feature_rules():
    eff = features.effective({"gmail": True}, {})["gmail"]
    assert "not configured" in M.run_stage(eff, LoginStore(), {}, make_client=never_made)["skipped"]


# ---------------------------------------------------------------- timestamps arrive as the database's JSON strings

@pytest.mark.parametrize("text,expected", [
    ("2026-10-12T12:00:00+00:00", datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)),
    ("2026-10-12T12:00:00Z", datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)),
    ("2026-10-12 12:00:00+00", datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)),
    ("2026-10-12T17:30:00+05:30", datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)),
    ("2026-10-12T17:30:00+0530", datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)),
    ("2026-10-12T12:00:00", datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)),           # no zone: UTC
    ("2026-10-12T12:00:00.5+00:00", datetime(2026, 10, 12, 12, 0, 0, 500000, tzinfo=timezone.utc)),
    ("2026-10-12T12:00:00.1234+00:00", datetime(2026, 10, 12, 12, 0, 0, 123400, tzinfo=timezone.utc)),   # Postgres drops trailing zeros
    ("2026-10-12T12:00:00.123456789+00:00", datetime(2026, 10, 12, 12, 0, 0, 123456, tzinfo=timezone.utc)),
    (" 2026-10-12T12:00:00+00:00 ", datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc))])
def test_database_timestamps_are_parsed(text, expected):
    assert M.to_datetime(text) == expected


def test_a_datetime_is_accepted_and_made_aware():
    aware = datetime(2026, 10, 12, 12, 0, tzinfo=timezone.utc)
    assert M.to_datetime(aware) is aware
    assert M.to_datetime(datetime(2026, 10, 12, 12, 0)) == aware


@pytest.mark.parametrize("junk", [None, "", "yesterday", "2026-13-45T00:00:00+00:00", "2026-10-12", 5, 1.5, [], {}, b"2026-10-12T12:00:00+00:00"])
def test_junk_is_none_never_an_exception(junk):
    assert M.to_datetime(junk) is None


def test_the_report_describes_a_real_overview_whose_timestamps_are_strings():
    """The shape gmail_overview() really returns (a jsonb object): this crashed with datetime - str before."""
    real = overview(search_cursor="2026-10-12T11:40:00.5+00:00", last_run_at="2026-10-12T11:41:00+00:00", token_status="ok",
                    token_checked_at="2026-10-12T11:41:00.123+00:00", messages_seen=3, pending=1)
    text = "\n".join(M.describe(real, ON, configured=True, now=NOW))
    # 12:00 now: 11:40:00.5 is 19 minutes (and a half) ago, 11:41:00 is 19, 11:41:00.123 is 18 (and a bit)
    assert "last started 19 min ago" in text and "finished 19 min ago" in text and "works (checked 18 min ago)" in text


def test_an_unreadable_timestamp_reads_as_never_instead_of_crashing_the_report():
    text = "\n".join(M.describe(overview(search_cursor="garbage", token_status="ok", token_checked_at="garbage"), ON, configured=True, now=NOW))
    assert "works (checked never)" in text


def test_the_check_closes_the_client_it_used_even_when_the_login_fails():
    class Client:
        closed = 0

        def profile(self):
            raise gmail_client.LoginExpired("sign in again")

        def close(self):
            Client.closed += 1
    out = M.check(LoginStore(), GMAIL_ENV, make_client=lambda env: Client())
    assert out["status"] == "expired" and Client.closed == 1


# ---------------------------------------------------------------- the last run and runs that keep ending early (step 6e)

SUMMARY = {"search": {"opened": 12, "suggested": 2, "unclear": 2, "complete": True, "stopped": ""},
           "classify": {"asked": 2, "suggested": 1, "bad_answers": 0, "stopped": ""}}


def test_the_last_run_is_described_in_counts():
    text = "\n".join(M.describe(overview(last_summary=SUMMARY), ON, configured=True, now=NOW))
    assert "last run:   opened 12 emails (2 suggested by the rules, 2 unclear); the model looked at 2 (1 suggested)" in text
    assert "problem:" not in text and "more waiting" not in text


def test_an_incomplete_search_unusable_answers_and_a_stop_reason_are_said():
    summary = {"search": {"opened": 1, "suggested": 0, "unclear": 0, "complete": False, "stopped": "Gmail is limiting requests"},
               "classify": {"asked": 3, "suggested": 0, "bad_answers": 2, "stopped": ""}}
    text = "\n".join(M.describe(overview(last_summary=summary), ON, configured=True, now=NOW))
    assert "opened 1 email (" in text and "more waiting" in text and "2 unusable" in text and "stopped: Gmail is limiting requests" in text


def test_a_skipped_model_step_is_said():
    summary = {"search": {"opened": 0, "complete": True}, "classify": {"note": "ANTHROPIC_API_KEY is not set: unclear emails wait"}}
    assert "model step skipped: ANTHROPIC_API_KEY is not set" in "\n".join(M.describe(overview(last_summary=summary), ON, configured=True, now=NOW))


def test_a_problem_line_appears_from_the_second_early_run_with_the_reason():
    summary = {"search": {"stopped": "Gmail is limiting requests"}}
    one = "\n".join(M.describe(overview(early_stops=1, last_summary=summary), ON, configured=True, now=NOW))
    many = "\n".join(M.describe(overview(early_stops=7, last_summary=summary), ON, configured=True, now=NOW))
    assert "problem:" not in one and "problem:    the last 7 runs ended early (Gmail is limiting requests)" in many


@pytest.mark.parametrize("junk", [None, "text", 5, [], {"search": "x"}, {"search": {"opened": "many"}}])
def test_a_malformed_summary_never_takes_the_status_command_down(junk):
    M.describe(overview(last_summary=junk, early_stops=junk), ON, configured=True, now=NOW)


def test_a_count_named_skipped_is_not_mistaken_for_a_skipped_model_step():
    summary = {"search": {"opened": 3, "complete": True}, "classify": {"asked": 2, "suggested": 1, "skipped": 2}}
    text = M.describe_summary(summary)
    assert "the model looked at 2 (1 suggested)" in text and "skipped:" not in text


def test_a_run_that_could_not_check_the_login_is_described_as_ended_early_not_as_a_search():
    assert M.describe_summary({"stopped": "Google is limiting requests (HTTP 429)"}) == "ended early: Google is limiting requests (HTTP 429)"
    text = "\n".join(M.describe(overview(last_summary={"stopped": "Gmail is unavailable"}, early_stops=4), ON, configured=True, now=NOW))
    assert "opened" not in text and "problem:    the last 4 runs ended early (Gmail is unavailable)" in text
