"""PgStore against a fake connection: the SQL each method sends, the error mapping, and connections always closed.
The real SQL runs in tests/test_store_pg.py against a throwaway Postgres."""
from __future__ import annotations

from types import SimpleNamespace

import psycopg
import pytest

import queries
from models import JobsQuery
from store import Conflict, Forbidden, Invalid, NotFound, PgStore, Unavailable


class FakeDbError(psycopg.DatabaseError):
    """A psycopg error carrying a chosen SQLSTATE and diagnostics, as the server would send them."""
    def __init__(self, sqlstate, primary="", detail=None):
        self._sqlstate, self._diag = sqlstate, SimpleNamespace(message_primary=primary, message_detail=detail)
        super().__init__(primary)

    @property
    def sqlstate(self):
        return self._sqlstate

    @property
    def diag(self):
        return self._diag


class FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class FakeConn:
    def __init__(self, results):
        self.results = list(results)       # one entry per execute: a list of rows, or an exception to raise
        self.calls: list[tuple[str, tuple]] = []
        self.closed = False

    def execute(self, sql, params=()):
        self.calls.append((sql, tuple(params)))
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return FakeCursor(result)

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def store_with(*results):
    conn = FakeConn(results)
    opened = []

    def connect(conninfo, **kw):
        opened.append(kw)
        return conn

    return PgStore("host=x", connect=connect), conn, opened


def test_connections_are_dict_rows_autocommit_with_a_timeout():
    store, conn, opened = store_with([{"s": {"cards": {}}}])
    store.summary()
    assert opened[0]["autocommit"] is True and opened[0]["connect_timeout"] == 5
    assert opened[0]["row_factory"] is not None
    assert conn.closed


def test_summary():
    store, conn, _ = store_with([{"s": {"threshold": 60}}])
    assert store.summary() == {"threshold": 60}
    assert conn.calls == [("SELECT dashboard_summary() AS s", ())]


def test_jobs_counts_then_fetches_the_page():
    q = JobsQuery(tab="all", min_score=60)
    store, conn, _ = store_with([{"total": 2}], [{"id": 1}, {"id": 2}])
    rows, total = store.jobs(q)
    assert total == 2 and [r["id"] for r in rows] == [1, 2]
    assert conn.calls[0] == (queries.jobs_count(q)[0], tuple(queries.jobs_count(q)[1]))
    assert conn.calls[1] == (queries.jobs_page(q)[0], tuple(queries.jobs_page(q)[1]))


def test_jobs_skips_the_page_query_when_nothing_matches():
    store, conn, _ = store_with([{"total": 0}])
    assert store.jobs(JobsQuery()) == ([], 0)
    assert len(conn.calls) == 1


def test_job_adds_its_listings():
    store, conn, _ = store_with([{"id": 7, "title": "x"}], [{"source": "indeed"}, {"source": "lever"}])
    job = store.job(7)
    assert job["sources_detail"] == [{"source": "indeed"}, {"source": "lever"}]
    assert conn.calls[0] == ("SELECT * FROM v_dashboard_job_detail WHERE id = %s", (7,))
    assert "FROM v_dashboard_job_sources WHERE job_id = %s" in conn.calls[1][0] and conn.calls[1][1] == (7,)


def test_a_missing_job_is_not_found():
    store, conn, _ = store_with([])
    with pytest.raises(NotFound):
        store.job(99)
    assert conn.closed


def test_set_status_calls_the_guarded_function():
    store, conn, _ = store_with([{"s": "interview"}])
    assert store.set_status(5, "interview", "applied", "phone screen") == "interview"
    assert conn.calls == [("SELECT dashboard_set_status(%s, %s, %s, %s) AS s", (5, "interview", "applied", "phone screen"))]


def test_application_file_calls_the_path_function():
    store, conn, _ = store_with([{"p": "/output/a/resume.pdf"}])
    assert store.application_file(5, "resume") == "/output/a/resume.pdf"
    assert conn.calls == [("SELECT dashboard_application_file(%s, %s) AS p", (5, "resume"))]


def test_export_rows():
    q = JobsQuery(tab="scored")
    store, conn, _ = store_with([{"id": 1}])
    assert store.export_rows(q) == [{"id": 1}]
    assert conn.calls == [(queries.jobs_export(q)[0], tuple(queries.jobs_export(q)[1]))]


# ---------------------------------------------------------------- error mapping

def test_a_stale_status_is_a_conflict_carrying_the_current_status():
    store, conn, _ = store_with(FakeDbError("JD409", "job 5 changed", "interview"))
    with pytest.raises(Conflict) as e:
        store.set_status(5, "offer", "applied", None)
    assert e.value.current == "interview"
    assert conn.closed


@pytest.mark.parametrize("state, exc", [("JD404", NotFound), ("JD403", Forbidden), ("JD422", Invalid),
                                        ("57014", Unavailable), ("57P01", Unavailable)])
def test_sqlstates_map_to_exceptions(state, exc):
    store, conn, _ = store_with(FakeDbError(state, "message from the server"))
    with pytest.raises(exc):
        store.set_status(5, "offer", "applied", None)
    assert conn.closed


def test_the_server_message_is_kept_for_the_known_errors():
    store, _, _ = store_with(FakeDbError("JD403", "job 5 is scam-blocked"))
    with pytest.raises(Forbidden, match="job 5 is scam-blocked"):
        store.set_status(5, "applied", "scam_blocked", None)


def test_an_unknown_database_error_propagates_unchanged():
    store, conn, _ = store_with(FakeDbError("42P01", "relation does not exist"))
    with pytest.raises(FakeDbError):
        store.summary()
    assert conn.closed


def test_a_lost_connection_is_unavailable():
    store, _, _ = store_with(psycopg.OperationalError("server closed the connection unexpectedly"))
    with pytest.raises(Unavailable):
        store.summary()


def test_a_failed_connect_is_unavailable_and_does_not_leak_details():
    def connect(conninfo, **kw):
        raise psycopg.OperationalError("connection to server at 10.0.0.5 failed: password authentication failed")
    with pytest.raises(Unavailable) as e:
        PgStore("host=x", connect=connect).summary()
    assert "password" not in str(e.value) and "10.0.0.5" not in str(e.value)


def test_a_non_database_error_still_closes_the_connection():
    store, conn, _ = store_with(RuntimeError("boom"))
    with pytest.raises(RuntimeError):
        store.summary()
    assert conn.closed
