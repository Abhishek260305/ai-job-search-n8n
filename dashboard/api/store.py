"""Database access for the dashboard.

The dashboard connects as the jobsearch_dashboard role (migration 014), which can read the three v_dashboard_* views
and run the three dashboard_* functions and nothing else. One connection per request: there is one user, and a
pool would be one more dependency for no gain.

The functions signal problems with their own SQLSTATEs, mapped here to exceptions the routes turn into HTTP codes:
  JD404 -> NotFound   JD403 -> Forbidden   JD409 -> Conflict (with the current status)   JD422 -> Invalid
A database that cannot be reached, or a query that hits the role's 10-second timeout, is Unavailable. Anything else
is a bug and is left to propagate.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

import queries
from models import JobsQuery

log = logging.getLogger("dashboard.store")


class StoreError(Exception):
    pass


class NotFound(StoreError):
    pass


class Forbidden(StoreError):
    pass


class Invalid(StoreError):
    pass


class Unavailable(StoreError):
    pass


class ProfileChanged(StoreError):
    """The search profile was written by someone else (the CLI, another tab) since the page last read it."""


class Conflict(StoreError):
    def __init__(self, message: str, current: str | None):
        super().__init__(message)
        self.current = current


_BY_SQLSTATE = {"JD404": NotFound, "JD403": Forbidden, "JD422": Invalid}
_UNAVAILABLE_SQLSTATES = {"57014", "57P01", "57P02", "57P03"}   # query cancelled (timeout), server shutting down


def _translate(e: psycopg.Error) -> StoreError | None:
    state = getattr(e, "sqlstate", None)
    diag = getattr(e, "diag", None)
    primary = (getattr(diag, "message_primary", None) or str(e)).strip()
    if state == "JD409":
        return Conflict(primary, getattr(diag, "message_detail", None))
    if state in _BY_SQLSTATE:
        return _BY_SQLSTATE[state](primary)
    if isinstance(e, psycopg.DataError):      # a value Postgres cannot store (a NUL byte in text, a number out of range)
        return Invalid("that value cannot be stored")
    if state in _UNAVAILABLE_SQLSTATES or isinstance(e, psycopg.OperationalError):
        log.warning("database unavailable: %s %s", type(e).__name__, state or "")
        return Unavailable("the database is not available right now")
    return None


class PgStore:
    def __init__(self, conninfo: str, connect=psycopg.connect):
        self._conninfo = conninfo
        self._connect = connect

    @contextmanager
    def _conn(self):
        try:
            conn = self._connect(self._conninfo, connect_timeout=5, row_factory=dict_row, autocommit=True)
        except psycopg.Error as e:
            raise (_translate(e) or Unavailable("the database is not available right now")) from None
        try:
            yield conn
        except psycopg.Error as e:
            mapped = _translate(e)
            if mapped is None:
                raise
            raise mapped from None
        finally:
            conn.close()

    def ping(self) -> bool:
        """True when the database answers within about two seconds. Never raises."""
        try:
            with self._connect(self._conninfo, connect_timeout=2, options="-c statement_timeout=2000") as conn:
                conn.execute("SELECT 1")
            return True
        except psycopg.Error as e:
            # the class name and SQLSTATE are enough to diagnose; the message could echo connection details
            log.warning("database not reachable: %s %s", type(e).__name__, getattr(e, "sqlstate", None) or "")
            return False

    def summary(self) -> dict:
        with self._conn() as conn:
            return conn.execute("SELECT dashboard_summary() AS s").fetchone()["s"]

    def jobs(self, q: JobsQuery) -> tuple[list[dict], int]:
        with self._conn() as conn:
            total = conn.execute(*queries.jobs_count(q)).fetchone()["total"]
            rows = conn.execute(*queries.jobs_page(q)).fetchall() if total else []
        return rows, total

    def job(self, job_id: int) -> dict:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM v_dashboard_job_detail WHERE id = %s", (job_id,)).fetchone()
            if row is None:
                raise NotFound(f"no job {job_id}")
            row["sources_detail"] = conn.execute(
                "SELECT source, url, apply_url, first_seen, last_seen FROM v_dashboard_job_sources"
                " WHERE job_id = %s ORDER BY first_seen, source", (job_id,)).fetchall()
        return row

    def set_status(self, job_id: int, status: str, expected: str, note: str | None) -> str:
        with self._conn() as conn:
            return conn.execute("SELECT dashboard_set_status(%s, %s, %s, %s) AS s",
                                (job_id, status, expected, note)).fetchone()["s"]

    def application_file(self, job_id: int, kind: str) -> str | None:
        with self._conn() as conn:
            return conn.execute("SELECT dashboard_application_file(%s, %s) AS p", (job_id, kind)).fetchone()["p"]

    def export_rows(self, q: JobsQuery) -> list[dict]:
        with self._conn() as conn:
            return conn.execute(*queries.jobs_export(q)).fetchall()

    def health(self) -> dict:
        with self._conn() as conn:
            return conn.execute("SELECT dashboard_health() AS h").fetchone()["h"]

    def spend(self) -> dict:
        with self._conn() as conn:
            return conn.execute("SELECT dashboard_spend() AS s").fetchone()["s"]

    def set_pause(self, paused: bool, reason: str | None) -> dict:
        with self._conn() as conn:
            return conn.execute("SELECT dashboard_set_pause(%s, %s) AS p", (paused, reason)).fetchone()["p"]

