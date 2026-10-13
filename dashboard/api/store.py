"""Database access for the dashboard.

The dashboard connects as the jobsearch_dashboard role (migration 014), which can read the three v_dashboard_* views
and run the dashboard_* functions and nothing else. One connection per request: there is one user, and a
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
from psycopg.types.json import Jsonb

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


class TrackingChanged(StoreError):
    """The notes of a job were saved by someone else (another tab, your phone) since the page last read them."""


class HistoryChanged(StoreError):
    """The newest history entry of a job is not the one the page meant (or there is nothing to undo any more)."""


class EmailNotFound(NotFound):
    """No such email suggestion."""


class EmailChanged(StoreError):
    """The suggestion was decided (accepted, dismissed, superseded) since the page last read it."""


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
    if state == "JD413":
        return HistoryChanged(primary)
    if state == "JD412":
        return TrackingChanged(primary)
    if state == "JD409":
        return Conflict(primary, getattr(diag, "message_detail", None))
    if state in _BY_SQLSTATE:
        return _BY_SQLSTATE[state](primary)
    if state in ("40P01", "40001"):           # two changes to one job at the same moment: one of them lost; trying again is safe
        return Unavailable("the database was busy with another change to this job: try again")
    if state == "42883":                      # a function of a newer migration: the image is ahead of the database
        log.warning("database needs its latest migration: %s", state)
        return Unavailable("the database needs its latest migration: run scripts/jobsearch migrate")
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

    def job_events(self, job_id: int) -> list[dict]:
        with self._conn() as conn:
            return conn.execute("SELECT dashboard_job_events(%s) AS e", (job_id,)).fetchone()["e"]

    def job_tracking(self, job_id: int) -> dict:
        with self._conn() as conn:
            return conn.execute("SELECT dashboard_job_tracking(%s) AS t", (job_id,)).fetchone()["t"]

    def set_tracking(self, job_id: int, version: str | None, patch: dict) -> dict:
        with self._conn() as conn:
            return conn.execute("SELECT dashboard_set_tracking(%s, %s, %s::jsonb) AS t",
                                (job_id, version, Jsonb(patch))).fetchone()["t"]

    def set_follow_up_days(self, days: int) -> int:
        with self._conn() as conn:
            return conn.execute("SELECT dashboard_set_follow_up_days(%s) AS d", (days,)).fetchone()["d"]

    def undo_status(self, job_id: int, expected: str, event_id: int | None) -> dict:
        with self._conn() as conn:
            return conn.execute("SELECT dashboard_undo_status(%s, %s, %s) AS r", (job_id, expected, event_id)).fetchone()["r"]

    def board(self) -> dict:
        with self._conn() as conn:
            return conn.execute("SELECT dashboard_board() AS b").fetchone()["b"]

    def stats(self) -> dict:
        with self._conn() as conn:
            return conn.execute("SELECT dashboard_stats() AS s").fetchone()["s"]

    def move_status(self, job_id: int, status: str, expected: str, note: str | None) -> dict:
        """set_status, and also the id of the history entry it wrote (None when nothing was recorded)."""
        with self._conn() as conn:
            return conn.execute("SELECT dashboard_move_status(%s, %s, %s, %s) AS m",
                                (job_id, status, expected, note)).fetchone()["m"]

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

    # ---- version 6f: the email inbox (migration 025). Suggestions are only ever read and decided through these functions; the role
    # has no access to the Gmail tables.
    def email_inbox(self, state: str, limit: int) -> dict:
        with self._conn() as conn:
            return conn.execute("SELECT dashboard_email_inbox(%s, %s) AS i", (state, limit)).fetchone()["i"]

    def email_count(self) -> dict:
        with self._conn() as conn:
            return conn.execute("SELECT dashboard_email_count() AS c").fetchone()["c"]

    def _email_call(self, sql: str, args: tuple) -> dict:
        try:
            with self._conn() as conn:
                return conn.execute(sql, args).fetchone()["r"]
        except NotFound as e:
            if "suggestion" in str(e):
                raise EmailNotFound(str(e)) from None
            raise                                   # the job itself is gone
        except Conflict as e:
            if e.current is None:                   # "already accepted / dismissed", not a status that moved
                raise EmailChanged(str(e)) from None
            raise

    def email_accept(self, suggestion_id: int, expected: str, acknowledged: bool) -> dict:
        """The change, made by the same function as a click on the board: the answer names the history entry (what Undo is tied to)."""
        return self._email_call("SELECT dashboard_email_accept(%s, %s, %s) AS r", (suggestion_id, expected, acknowledged))

    def email_dismiss(self, suggestion_id: int) -> dict:
        return self._email_call("SELECT dashboard_email_dismiss(%s) AS r", (suggestion_id,))

    def email_restore(self, suggestion_id: int) -> dict:
        return self._email_call("SELECT dashboard_email_restore(%s) AS r", (suggestion_id,))
