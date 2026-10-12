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
