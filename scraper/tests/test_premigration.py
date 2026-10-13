"""The new scraper image may run before migration 013 is applied (a missed step). Scoring is paid for BEFORE
the settings are read back, so it must behave exactly like the old code on a database without the new columns.

PgStore is exercised with a fake psycopg connection: no database, no network."""
import sys
import types

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("psycopg")
sys.modules.setdefault("jobspy", types.SimpleNamespace(scrape_jobs=lambda **kw: None))

import psycopg  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app as A  # noqa: E402
import db  # noqa: E402

# the search_profile columns that exist BEFORE migration 013 (no boards, no board_title_words)
OLD_ROW = {"name": "default", "search_terms": ["backend engineer"], "locations": ["Pune"], "sources": ["indeed"],
           "allow_remote": True, "min_yoe": None, "max_yoe": None, "include_keywords": [], "exclude_keywords": [],
           "exclude_companies": [], "max_age_days": 7, "score_threshold": 70}


class FakeCursor:
    def __init__(self, conn):
        self.conn, self.sql, self.rowcount = conn, "", 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.sql = sql
        self.conn.statements.append(sql)
        if self.conn.fail_on and self.conn.fail_on[0] in sql:
            raise self.conn.fail_on[1]

    def fetchone(self):
        if "FROM search_profile" in self.sql:
            return dict(OLD_ROW)
        if "record_usage" in self.sql:
            return {"c": 0.0003}
        if "record_score" in self.sql:
            return {"s": "scored"}
        if "note_score_failure" in self.sql:
            return {"n": 1}
        return None


class FakeConn:
    """Like psycopg's connection: leaving the `with` block rolls back on an exception, commits otherwise, closes."""

    def __init__(self, fail_on=None):
        self.statements, self.fail_on, self.closed, self.exit_exc = [], fail_on, False, "not exited"

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.exit_exc, self.closed = exc_type, True
        return False

    def cursor(self):
        return FakeCursor(self)


@pytest.fixture
def conns(monkeypatch):
    """Every connection PgStore opens, plus a way to make one statement fail."""
    state = types.SimpleNamespace(opened=[], fail_on=None)

    def connect(conninfo, **kw):
        c = FakeConn(state.fail_on)
        state.opened.append(c)
        return c

    monkeypatch.setattr(db.psycopg, "connect", connect)
    return state


def test_search_settings_defaults_the_new_keys_when_the_columns_are_missing(conns):
    s = db.PgStore("").search_settings()
    assert s["boards"] == {} and s["board_title_words"] == []
    for k in ("search_terms", "locations", "sources", "allow_remote", "min_yoe", "max_yoe", "include_keywords",
              "exclude_keywords", "exclude_companies", "max_age_days", "score_threshold"):
        assert s[k] == OLD_ROW[k]


def test_search_settings_keeps_the_new_values_when_the_columns_exist(conns, monkeypatch):
    row = dict(OLD_ROW, boards={"lever": ["acme"]}, board_title_words=["backend"])
    monkeypatch.setattr(FakeCursor, "fetchone", lambda self: dict(row))
    s = db.PgStore("").search_settings()
    assert s["boards"] == {"lever": ["acme"]} and s["board_title_words"] == ["backend"]


def test_search_settings_old_keys_stay_strict(conns, monkeypatch):
    row = {k: v for k, v in OLD_ROW.items() if k != "score_threshold"}
    monkeypatch.setattr(FakeCursor, "fetchone", lambda self: dict(row))
    with pytest.raises(KeyError):
        db.PgStore("").search_settings()


def test_score_endpoint_logs_usage_and_returns_200_without_migration_013(conns, monkeypatch):
    monkeypatch.setattr(A, "_store", db.PgStore(""))
    r = TestClient(A.app).post("/jobs/7/score", json={
        "model_output": '{"score": 80}', "model": "claude-haiku-5-5", "usage": {"input_tokens": 1000, "output_tokens": 200}})
    usage_calls = [s for c in conns.opened for s in c.statements if "record_usage" in s]
    assert len(usage_calls) >= 1, "the paid call must be logged"
    # an unusable reply is a normal 422 (the settings were read fine), never a 503 for a missing column
    assert r.status_code == 422, r.text


def test_score_endpoint_scores_a_valid_reply_without_migration_013(conns, monkeypatch):
    import scoring
    monkeypatch.setattr(scoring, "finalize", lambda out, threshold: {
        "score": 80, "verdict": "apply", "above_threshold": True, "excluded": False, "warnings": []})
    monkeypatch.setattr(A, "_store", db.PgStore(""))
    r = TestClient(A.app).post("/jobs/7/score", json={
        "model_output": "{}", "model": "claude-haiku-5-5", "usage": {"input_tokens": 1000, "output_tokens": 200}})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "scored"
    assert any("record_usage" in s for c in conns.opened for s in c.statements)


def test_board_batch_is_30_when_the_column_does_not_exist_yet(conns):
    conns.fail_on = ("board_batch", psycopg.errors.UndefinedColumn('column "board_batch" does not exist'))
    assert db.PgStore("").board_batch() == 30
    # the failed statement aborted that transaction: its connection was rolled back (exception passed through
    # the `with`) and closed, so nothing leaks
    assert len(conns.opened) == 1
    assert conns.opened[0].closed and conns.opened[0].exit_exc is psycopg.errors.UndefinedColumn


def test_board_batch_other_database_errors_still_propagate(conns):
    conns.fail_on = ("board_batch", psycopg.errors.OperationalError("server closed the connection"))
    with pytest.raises(psycopg.errors.OperationalError):
        db.PgStore("").board_batch()
    assert conns.opened[0].closed


def test_board_batch_reads_the_column_when_present(conns, monkeypatch):
    monkeypatch.setattr(FakeCursor, "fetchone", lambda self: {"board_batch": 12})
    assert db.PgStore("").board_batch() == 12


# ---- migration 021 (the Gmail part): a scraper image that runs before it is applied must not break the others

OLD_FLAGS = {"tailoring": True, "telegram": True, "digest": True}


def test_the_features_read_works_without_the_gmail_column_and_reports_gmail_off(conns, monkeypatch):
    conns.fail_on = ("digest, gmail", psycopg.errors.UndefinedColumn('column "gmail" does not exist'))
    monkeypatch.setattr(FakeCursor, "fetchone", lambda self: dict(OLD_FLAGS))
    assert db.PgStore("").features() == {**OLD_FLAGS, "gmail": False}
    assert conns.opened[0].closed and conns.opened[0].exit_exc is psycopg.errors.UndefinedColumn, "the failed attempt was rolled back and closed"


def test_the_features_read_includes_gmail_when_the_column_exists(conns, monkeypatch):
    monkeypatch.setattr(FakeCursor, "fetchone", lambda self: {**OLD_FLAGS, "gmail": True})
    assert db.PgStore("").features()["gmail"] is True
    assert len(conns.opened) == 1


def test_other_database_errors_while_reading_features_still_propagate(conns):
    conns.fail_on = ("digest, gmail", psycopg.errors.OperationalError("server closed the connection"))
    with pytest.raises(psycopg.errors.OperationalError):
        db.PgStore("").features()


def test_the_mail_overview_of_an_old_database_is_a_clear_message(conns):
    conns.fail_on = ("gmail_overview", psycopg.errors.UndefinedFunction("function gmail_overview() does not exist"))
    with pytest.raises(LookupError, match="scripts/jobsearch migrate"):
        db.PgStore("").gmail_overview()
    assert conns.opened[0].closed


def test_other_errors_while_reading_the_mail_overview_still_propagate(conns):
    conns.fail_on = ("gmail_overview", psycopg.errors.OperationalError("server closed the connection"))
    with pytest.raises(psycopg.errors.OperationalError):
        db.PgStore("").gmail_overview()


def test_the_scheduler_tick_still_works_on_a_database_without_the_gmail_column(conns, monkeypatch):
    conns.fail_on = ("digest, gmail", psycopg.errors.UndefinedColumn('column "gmail" does not exist'))
    seen = {}

    def fetchone(self):
        return dict(OLD_FLAGS)

    def fetchall(self):
        seen["claimed"] = self.sql
        return [{"name": "collect", "batch": 18, "model": None}]
    monkeypatch.setattr(FakeCursor, "fetchone", fetchone)
    monkeypatch.setattr(FakeCursor, "fetchall", fetchall, raising=False)
    monkeypatch.setattr(A, "_store", db.PgStore(""))
    r = TestClient(A.app).post("/schedule/tick")
    assert r.status_code == 200, r.text
    assert "mail" in r.json()["skipped"], "no Gmail column means Gmail is off, so the mail stage is not claimed"


def test_update_features_writes_the_gmail_column_and_ignores_unknown_names(conns, monkeypatch):
    monkeypatch.setattr(FakeCursor, "fetchone", lambda self: {**OLD_FLAGS, "gmail": True})
    db.PgStore("").update_features(gmail=True, bogus=True)
    writes = [s for c in conns.opened for s in c.statements if s.startswith("UPDATE feature_settings")]
    assert len(writes) == 1 and "gmail = %s" in writes[0] and "bogus" not in writes[0]


def test_update_gmail_settings_writes_only_the_two_settings(conns, monkeypatch):
    monkeypatch.setattr(FakeCursor, "fetchone", lambda self: {"o": {"lookback_days": 30}, "c": {"waiting": 0, "parked": 0}})
    out = db.PgStore("").update_gmail_settings(lookback_days=30, from_now=True, token_status="ok", search_cursor="now")
    writes = [s for c in conns.opened for s in c.statements if s.startswith("UPDATE gmail_state")]
    assert len(writes) == 1 and "lookback_days = %s" in writes[0] and "from_now = %s" in writes[0]
    assert "token_status" not in writes[0] and "search_cursor" not in writes[0], "the login state and the cursor are not settings"
    assert out == {"lookback_days": 30, "unclear": {"waiting": 0, "parked": 0}}, "the overview carries the model step's counts (zeros before migration 023)"


def test_updating_the_gmail_settings_on_an_unmigrated_database_is_the_friendly_message(conns):
    conns.fail_on = ("UPDATE gmail_state", psycopg.errors.UndefinedTable('relation "gmail_state" does not exist'))
    with pytest.raises(LookupError, match="scripts/jobsearch migrate"):
        db.PgStore("").update_gmail_settings(lookback_days=30)
    assert conns.opened[0].closed and conns.opened[0].exit_exc is psycopg.errors.UndefinedTable


def test_switching_gmail_on_in_an_unmigrated_database_is_the_friendly_message(conns):
    conns.fail_on = ("UPDATE feature_settings", psycopg.errors.UndefinedColumn('column "gmail" of relation "feature_settings" does not exist'))
    with pytest.raises(LookupError, match="scripts/jobsearch migrate"):
        db.PgStore("").update_features(gmail=True)


def test_other_errors_while_writing_these_settings_still_propagate(conns):
    conns.fail_on = ("UPDATE gmail_state", psycopg.errors.OperationalError("server closed the connection"))
    with pytest.raises(psycopg.errors.OperationalError):
        db.PgStore("").update_gmail_settings(from_now=True)
    conns.fail_on = ("UPDATE feature_settings", psycopg.errors.OperationalError("server closed the connection"))
    with pytest.raises(psycopg.errors.OperationalError):
        db.PgStore("").update_features(gmail=True)


def test_the_other_switches_still_work_on_a_database_without_the_gmail_column(conns, monkeypatch):
    """Turning tailoring off must not need the new column: only a gmail change names it."""
    monkeypatch.setattr(FakeCursor, "fetchone", lambda self: dict(OLD_FLAGS))
    db.PgStore("").update_features(tailoring=False)
    writes = [s for c in conns.opened for s in c.statements if s.startswith("UPDATE feature_settings")]
    assert len(writes) == 1 and "gmail" not in writes[0]


def test_recording_a_login_check_writes_the_status_and_cuts_the_error(conns):
    store = db.PgStore("")
    store.record_gmail_login("error", "x" * 500, ran=True)
    store.record_gmail_login("ok", None)
    writes = [s for c in conns.opened for s in c.statements if s.startswith("UPDATE gmail_state")]
    assert len(writes) == 2 and all("token_status = %s" in w and "token_checked_at = now()" in w for w in writes)


def test_a_bad_login_status_is_refused_before_the_database(conns):
    with pytest.raises(ValueError):
        db.PgStore("").record_gmail_login("great", None)
    assert conns.opened == []


def test_recording_a_login_check_on_an_unmigrated_database_is_the_friendly_message(conns):
    conns.fail_on = ("UPDATE gmail_state", psycopg.errors.UndefinedTable('relation "gmail_state" does not exist'))
    with pytest.raises(LookupError, match="scripts/jobsearch migrate"):
        db.PgStore("").record_gmail_login("ok", None)


# ---- migration 022 (the Gmail search): friendly messages on an old database, and what the store sends

def test_the_candidates_of_an_unmigrated_database_are_a_clear_message(conns):
    conns.fail_on = ("gmail_candidates", psycopg.errors.UndefinedFunction("function gmail_candidates(integer) does not exist"))
    with pytest.raises(LookupError, match="scripts/jobsearch migrate"):
        db.PgStore("").gmail_candidates()
    assert conns.opened[0].closed


def test_recording_a_result_on_an_unmigrated_database_is_a_clear_message(conns):
    conns.fail_on = ("gmail_record_result", psycopg.errors.UndefinedFunction("function does not exist"))
    with pytest.raises(LookupError, match="scripts/jobsearch migrate"):
        db.PgStore("").record_gmail_result("m1", "t1", "noise", None)


def test_other_errors_while_recording_still_propagate(conns):
    conns.fail_on = ("gmail_record_result", psycopg.errors.CheckViolation("new row violates check constraint"))
    with pytest.raises(psycopg.errors.CheckViolation):
        db.PgStore("").record_gmail_result("m1", "t1", "suggested", 1, {"signal": "interview"})


def test_the_state_row_of_an_unmigrated_database_is_a_clear_message(conns):
    conns.fail_on = ("FROM gmail_state", psycopg.errors.UndefinedTable('relation "gmail_state" does not exist'))
    with pytest.raises(LookupError, match="scripts/jobsearch migrate"):
        db.PgStore("").gmail_state_row()


def test_asking_which_ids_are_new_for_no_ids_does_not_touch_the_database(conns):
    assert db.PgStore("").gmail_unseen([]) == [] and conns.opened == []


def test_unseen_ids_keep_their_order_and_drop_the_seen_ones(conns, monkeypatch):
    monkeypatch.setattr(FakeCursor, "fetchall", lambda self: [{"message_id": "b"}], raising=False)
    assert db.PgStore("").gmail_unseen(["a", "b", "c"]) == ["a", "c"]


def test_a_search_that_was_not_complete_leaves_the_cursor_alone_in_the_statement(conns):
    db.PgStore("").record_gmail_search(None)
    sql = [s for c in conns.opened for s in c.statements if s.startswith("UPDATE gmail_state")][0]
    assert "coalesce(%s, search_cursor)" in sql and "last_run_at" in sql


def test_a_rescan_of_an_unmigrated_database_is_a_clear_message(conns):
    conns.fail_on = ("gmail_rescan", psycopg.errors.UndefinedFunction("function gmail_rescan() does not exist"))
    with pytest.raises(LookupError, match="scripts/jobsearch migrate"):
        db.PgStore("").gmail_rescan()


def test_a_rescan_returns_how_many_rows_were_reopened(conns, monkeypatch):
    monkeypatch.setattr(FakeCursor, "fetchone", lambda self: {"n": 12})
    assert db.PgStore("").gmail_rescan() == 12


# ---- migration 024 (the stage's record and the digest): an old database must not crash the new image

def test_a_working_login_re_arms_the_digests_login_alert_and_other_statuses_do_not(conns):
    store = db.PgStore("")
    store.record_gmail_login("ok", None)
    store.record_gmail_login("expired", "x")
    writes = [s for c in conns.opened for s in c.statements if s.startswith("UPDATE gmail_state")]
    assert "login_alerted_at = NULL" in writes[0] and "login_alerted_at" not in writes[1]


def test_recording_a_login_check_before_migration_024_falls_back_to_the_old_statement(conns):
    conns.fail_on = ("login_alerted_at", psycopg.errors.UndefinedColumn('column "login_alerted_at" of relation "gmail_state" does not exist'))
    db.PgStore("").record_gmail_login("ok", None, ran=True)
    writes = [s for c in conns.opened for s in c.statements if s.startswith("UPDATE gmail_state")]
    assert len(writes) == 2 and "login_alerted_at" in writes[0] and "login_alerted_at" not in writes[1], "the new statement was refused, then the old one ran"


def test_the_stages_record_and_the_digests_mail_are_quiet_before_migration_024(conns):
    conns.fail_on = ("gmail_", psycopg.errors.UndefinedFunction("function gmail_record_run does not exist"))
    store = db.PgStore("")
    assert store.gmail_record_run({"search": {}}, False) is False
    assert store.gmail_digest(5) is None
    assert store.gmail_digest_sent([1, 2], True) == 0


def test_the_digest_marks_nothing_it_was_not_given(monkeypatch, conns):
    monkeypatch.setattr(FakeCursor, "fetchone", lambda self: {"n": 0})
    db.PgStore("").gmail_digest_sent([], False)
    sent = [s for c in conns.opened for s in c.statements if "gmail_digest_sent" in s]
    assert len(sent) == 1


# ---- migration 026 (tailoring v2): an image that runs before it is applied reads v1 settings and no company notes

OLD_TAILOR = {"mode": "auto", "min_score": None, "max_score": None, "daily_limit": None, "approval": "review", "model": "claude-sonnet-5-5",
              "cover_letter": True, "max_pages": 1, "verify": True, "verify_model": "claude-haiku-5-5", "paper": "a4", "style_notes": None}


def test_tailor_settings_of_an_old_database_say_engine_v2_and_the_defaults(conns, monkeypatch):
    conns.fail_on = ("engine, gap_policy", psycopg.errors.UndefinedColumn('column "engine" does not exist'))
    monkeypatch.setattr(FakeCursor, "fetchone", lambda self: dict(OLD_TAILOR))
    s = db.PgStore("").tailor_settings()
    assert s["engine"] == "v2" and s["gap_policy"] == "omit" and s["cover_style"] == "evidence" and s["tone"] == "direct"
    assert s["reviewer"] is True and s["reviewer_model"] == "claude-haiku-5-5" and s["company_research"] is True and s["mode"] == "auto"
    assert conns.opened[0].exit_exc is psycopg.errors.UndefinedColumn and conns.opened[0].closed        # the failed attempt was rolled back


def test_tailor_settings_of_a_migrated_database_are_read_in_one_go(conns, monkeypatch):
    monkeypatch.setattr(FakeCursor, "fetchone", lambda self: dict(OLD_TAILOR, engine="v2", gap_policy="bridge"))
    s = db.PgStore("").tailor_settings()
    assert s["engine"] == "v2" and s["gap_policy"] == "bridge" and len(conns.opened) == 1


def test_changing_an_old_setting_does_not_need_the_new_columns_but_changing_the_engine_says_to_migrate(conns, monkeypatch):
    monkeypatch.setattr(FakeCursor, "fetchone", lambda self: dict(OLD_TAILOR))
    db.PgStore("").update_tailor_settings(mode="manual")
    writes = [s for c in conns.opened for s in c.statements if s.startswith("UPDATE tailor_settings")]
    assert len(writes) == 1 and "engine" not in writes[0]
    conns.fail_on = ("UPDATE tailor_settings SET engine", psycopg.errors.UndefinedColumn('column "engine" does not exist'))
    with pytest.raises(LookupError, match="run scripts/jobsearch migrate"):
        db.PgStore("").update_tailor_settings(engine="v2")


def test_company_notes_before_the_migration_are_nothing_and_saving_them_is_a_quiet_no_op(conns):
    conns.fail_on = ("company_notes", psycopg.errors.UndefinedTable('relation "company_notes" does not exist'))
    store = db.PgStore("")
    assert store.company_notes_get("acme", 30) is None
    store.company_notes_save("acme", ["a fact"], "https://acme.com/")                     # must not raise


def test_company_notes_are_cut_to_three_facts_and_a_short_source(conns, monkeypatch):
    seen = []
    monkeypatch.setattr(FakeCursor, "execute", lambda self, sql, params=None: seen.append((sql, params)))
    db.PgStore("").company_notes_save("acme", ["a", "b", "c", "d"], "https://acme.com/" + "x" * 400)
    sql, params = seen[0]
    assert "ON CONFLICT (company_key) DO UPDATE" in sql and params[0] == "acme" and len(params[2]) == 300
    assert len(params[1].obj) == 3


def test_the_links_of_a_job_are_its_own_source_and_apply_links_only(conns, monkeypatch):
    monkeypatch.setattr(FakeCursor, "fetchall", lambda self: [{"url": "https://a.com/1", "apply_url": "https://a.com/apply"}, {"url": "https://b.com/2", "apply_url": None}], raising=False)
    assert db.PgStore("").job_links(7) == ["https://a.com/1", "https://a.com/apply", "https://b.com/2"]
