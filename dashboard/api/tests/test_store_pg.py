"""PgStore against a real, throwaway Postgres with every migration applied, connected as jobsearch_dashboard.

Skipped unless both are set:
  DASHBOARD_TEST_ADMIN_DSN  the owner role, used to wipe and seed the jobs tables
  DASHBOARD_TEST_DSN        the jobsearch_dashboard role (LOGIN and a password set on the throwaway server)
The seed step TRUNCATEs jobs, so both must name a database whose name contains "test"; anything else is refused.
"""
from __future__ import annotations

import os

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from models import JobsQuery
from store import Conflict, Forbidden, Invalid, NotFound, PgStore

ADMIN_DSN = os.environ.get("DASHBOARD_TEST_ADMIN_DSN")
DSN = os.environ.get("DASHBOARD_TEST_DSN")

pytestmark = pytest.mark.skipif(not (ADMIN_DSN and DSN), reason="no throwaway database configured")

SEED = [
    # name,       source,       title,                         company,     remote, status,         score
    ("acme",      "indeed",     "Senior 100% Python Engineer", "Acme",      True,   "shortlisted",  80),
    ("beta",      "lever",      "Data Engineer",               "Beta_Corp", False,  "shortlisted",  65),
    ("interview", "greenhouse", "Backend Engineer",            "Gamma",     False,  "shortlisted",  75),
    ("scam",      "indeed",     "Easy Money From Home",        "Shady",     True,   "scam_blocked", None),
    ("filtered",  "indeed",     "Sales Manager",               "Delta",     False,  "filtered_out", None),
    ("tailored",  "ashby",      "Platform Engineer",           "Epsilon",   True,   "tailored",     90),
]


def _require_test_db(dsn: str) -> None:
    if "test" not in conninfo_to_dict(dsn).get("dbname", ""):
        pytest.fail("refusing to run: the database name must contain 'test'")


@pytest.fixture(scope="module")
def ids():
    _require_test_db(ADMIN_DSN)
    _require_test_db(DSN)
    found = {}
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        conn.execute("TRUNCATE jobs CASCADE")
        for name, source, title, company, remote, status, score in SEED:
            job_id = conn.execute(
                "SELECT job_id FROM upsert_job(%s, %s, %s, NULL, %s, %s, 'Pune', %s, now(), %s,"
                " NULL, NULL, NULL, '{}'::jsonb)",
                (source, f"it-{name}", f"https://example.com/{name}", title, company, remote, f"about {name}"),
            ).fetchone()[0]
            conn.execute("UPDATE jobs SET status = %s, score = %s WHERE id = %s", (status, score, job_id))
            found[name] = job_id
        # the same Acme posting seen on a second board joins the first job
        again = conn.execute(
            "SELECT job_id FROM upsert_job('greenhouse', 'it-acme-2', 'https://boards.example.com/acme', NULL,"
            " 'Senior 100% Python Engineer', 'Acme', 'Pune', true, now(), 'about acme', NULL, NULL, NULL, '{}'::jsonb)"
        ).fetchone()[0]
        assert again == found["acme"]
        conn.execute("INSERT INTO applications (job_id, status, resume_path) VALUES (%s, 'drafted', %s)",
                     (found["tailored"], "/output/applications/tailored/resume.pdf"))
        conn.execute("SELECT set_application_status(%s, 'applied')", (found["interview"],))
        conn.execute("SELECT set_application_status(%s, 'interview')", (found["interview"],))
    return found


@pytest.fixture
def store():
    return PgStore(DSN)


def test_connected_as_the_dashboard_role(ids):
    with psycopg.connect(DSN) as conn:
        assert conn.execute("SELECT current_user").fetchone()[0] == "jobsearch_dashboard"


def test_ping(store):
    assert store.ping() is True


def test_summary(store, ids):
    s = store.summary()
    assert s["cards"]["found"] == 6
    assert s["cards"]["shortlisted"] == 2
    assert s["cards"]["interview"] == 1 and s["cards"]["applied"] == 1
    assert {"threshold", "cards", "by_status", "funnel", "generated_at"} <= s.keys()


def test_jobs_default_tab(store, ids):
    rows, total = store.jobs(JobsQuery())
    assert total == 3
    assert [r["id"] for r in rows] == [ids["tailored"], ids["acme"], ids["beta"]]
    assert rows[0]["pipeline_status"] == "drafted" and rows[0]["has_resume"] is True


def test_jobs_search_treats_wildcards_literally(store, ids):
    assert [r["id"] for r in store.jobs(JobsQuery(tab="all", q="100%"))[0]] == [ids["acme"]]
    assert [r["id"] for r in store.jobs(JobsQuery(tab="all", q="beta_corp"))[0]] == [ids["beta"]]
    assert store.jobs(JobsQuery(tab="all", q="n_i"))[1] == 0, "_ must not match any character (as in 'Engineer')"
    assert store.jobs(JobsQuery(tab="all", q="x'); DROP TABLE jobs; --"))[1] == 0


def test_jobs_filters(store, ids):
    assert {r["id"] for r in store.jobs(JobsQuery(tab="all", source=["greenhouse"]))[0]} == {ids["acme"], ids["interview"]}
    assert {r["id"] for r in store.jobs(JobsQuery(tab="all", status=["interview", "drafted"]))[0]} == \
        {ids["interview"], ids["tailored"]}
    assert {r["id"] for r in store.jobs(JobsQuery(tab="all", remote=True, min_score=85))[0]} == {ids["tailored"]}
    assert store.jobs(JobsQuery(tab="all", posted_days=1))[1] == 6


def test_tabs(store, ids):
    def tab(name):
        return {r["id"] for r in store.jobs(JobsQuery(tab=name))[0]}
    assert tab("applied") == {ids["interview"]}
    assert tab("scam") == {ids["scam"]}
    assert tab("filtered") == {ids["filtered"]}
    assert tab("scored") == {ids["acme"], ids["beta"], ids["interview"], ids["tailored"]}
    assert len(tab("all")) == 6


def test_sorting_and_paging(store, ids):
    q = JobsQuery(tab="all", sort="company", order="asc", page_size=25)
    assert [r["company"] for r in store.jobs(q)[0]] == ["Acme", "Beta_Corp", "Delta", "Epsilon", "Gamma", "Shady"]
    rows, total = store.jobs(JobsQuery(tab="all", page=2, page_size=25))
    assert rows == [] and total == 6


def test_job_detail_with_both_listings(store, ids):
    job = store.job(ids["acme"])
    assert job["description"] == "about acme"
    assert [s["source"] for s in job["sources_detail"]] == ["indeed", "greenhouse"]
    assert job["sources"] == ["greenhouse", "indeed"]
    assert "resume_path" not in job and "raw" not in job


def test_missing_job(store, ids):
    with pytest.raises(NotFound):
        store.job(10**12)


def test_set_status_and_conflict(store, ids):
    assert store.set_status(ids["beta"], "applied", "shortlisted", "  sent via site  ") == "applied"
    with pytest.raises(Conflict) as e:
        store.set_status(ids["beta"], "interview", "shortlisted", None)
    assert e.value.current == "applied"
    assert store.set_status(ids["beta"], "interview", "applied", None) == "interview"


def test_set_status_refusals(store, ids):
    with pytest.raises(Forbidden):
        store.set_status(ids["scam"], "applied", "scam_blocked", None)
    with pytest.raises(NotFound):
        store.set_status(10**12, "applied", "shortlisted", None)
    with pytest.raises(Invalid):
        store.set_status(ids["acme"], "drafted", "shortlisted", None)
    with pytest.raises(Invalid):
        store.set_status(ids["acme"], "applied", "shortlisted", "x" * 501)


def test_application_file(store, ids):
    assert store.application_file(ids["tailored"], "resume") == "/output/applications/tailored/resume.pdf"
    assert store.application_file(ids["tailored"], "cover_letter") is None
    assert store.application_file(ids["acme"], "resume") is None
    with pytest.raises(Invalid):
        store.application_file(ids["tailored"], "../etc/passwd")


def test_export_rows(store, ids):
    assert len(store.export_rows(JobsQuery(tab="all", page=3))) == 6


def test_the_api_end_to_end(ids, tmp_path, static_dir):
    """Every route over the real store, so the response models are checked against real column types."""
    from fastapi.testclient import TestClient

    from app import create_app
    from settings import load_settings
    from tests.support import PASSWORD, WRITE, make_env

    c = TestClient(create_app(load_settings(make_env(tmp_path)), PgStore(DSN)), base_url="http://localhost")
    assert c.post("/api/login", json={"password": PASSWORD}).status_code == 204
    assert c.get("/api/health").json() == {"ok": True, "db": True}
    assert c.get("/api/summary").json()["cards"]["found"] == 6
    page = c.get("/api/jobs?tab=all&page_size=25").json()
    assert page["total"] == 6 and len(page["items"]) == 6
    detail = c.get(f"/api/jobs/{ids['acme']}").json()
    assert detail["sources"] == ["greenhouse", "indeed"] and len(detail["sources_detail"]) == 2
    assert c.get("/api/jobs/999999999").status_code == 404
    r = c.patch(f"/api/jobs/{ids['acme']}/status", headers=WRITE,
                json={"status": "approved", "expected_status": "shortlisted"})
    assert r.status_code == 200 and r.json() == {"id": ids["acme"], "status": "approved"}
    r = c.patch(f"/api/jobs/{ids['acme']}/status", headers=WRITE,
                json={"status": "skipped", "expected_status": "shortlisted"})
    assert r.status_code == 409 and r.json()["current_status"] == "approved"
    r = c.patch(f"/api/jobs/{ids['scam']}/status", headers=WRITE,
                json={"status": "applied", "expected_status": "scam_blocked"})
    assert r.status_code == 403
    # the stored path is /output/..., which does not exist here: refused, not an error
    assert c.get(f"/api/jobs/{ids['tailored']}/files/resume").status_code == 404
    csv_text = c.get("/api/export.csv?tab=all").text
    assert csv_text.startswith("\ufeffid,") and csv_text.count("\r\n") == 7


def test_the_role_cannot_touch_tables(ids):
    with psycopg.connect(DSN, autocommit=True) as conn:
        for sql in ("SELECT 1 FROM jobs LIMIT 1", "SELECT 1 FROM applications LIMIT 1",
                    "UPDATE jobs SET score = 0", "SELECT set_application_status(1, 'applied')"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(sql)


# ---------------------------------------------------------------- version 2a: health, spend, pause

def _admin(sql, params=()):
    with psycopg.connect(ADMIN_DSN, autocommit=True) as conn:
        return conn.execute(sql, params).fetchall() if sql.lstrip().upper().startswith("SELECT") else conn.execute(sql, params)


def test_health_has_the_shape_the_screen_reads(store, ids):
    from models import Health
    _admin("UPDATE schedule_control SET paused = false, paused_at = NULL, reason = NULL")
    _admin("DELETE FROM fetch_state")
    _admin("SELECT record_fetch('lever', 'newco', false, NULL, 'HTTP 404')")
    try:
        health = Health.model_validate(store.health())
    finally:
        _admin("DELETE FROM fetch_state")
    assert [s.name for s in health.schedules] == ["collect", "score", "tailor", "digest"]
    assert health.paused.paused is False and health.timezone
    assert health.sources.failing == 1 and health.sources.items[0].key == "newco"
    assert health.queue.waiting_to_score == 0
    assert all(a.code and a.message for a in health.alerts)


def test_spend_has_the_shape_the_screen_reads(store, ids):
    from models import Spend
    _admin("DELETE FROM llm_usage")
    _admin("INSERT INTO llm_usage (provider, purpose, model, input_tokens, output_tokens, cost_usd) VALUES"
           " ('anthropic', 'score', 'claude-haiku-5-5', 1000, 100, 0.25), ('apify', 'search', NULL, 0, 0, 0.10)")
    try:
        spend = Spend.model_validate(store.spend())
    finally:
        _admin("DELETE FROM llm_usage")
    assert spend.claude.day_spent == pytest.approx(0.25) and spend.apify.day_spent == pytest.approx(0.10)
    assert {(d.provider, d.purpose) for d in spend.daily} == {("anthropic", "score"), ("apify", "search")}
    assert {m.model for m in spend.models} == {"claude-haiku-5-5", "(none)"}


def test_an_empty_usage_table_is_zeros_and_empty_lists(store, ids):
    from models import Spend
    _admin("DELETE FROM llm_usage")
    spend = Spend.model_validate(store.spend())
    assert spend.claude.month_spent == 0 and spend.daily == [] and spend.models == []


def test_pause_and_resume_change_the_shared_switch(store, ids):
    try:
        state = store.set_pause(True, "  away  ")
        assert state["paused"] is True and state["reason"] == "away" and state["paused_at"]
        assert _admin("SELECT paused, reason FROM schedule_control WHERE id = 1")[0] == (True, "away")
        assert [a["code"] for a in store.health()["alerts"]][:1] == ["paused"]
        again = store.set_pause(False, "ignored")
        assert again == {"paused": False, "paused_at": None, "reason": None}
    finally:
        _admin("UPDATE schedule_control SET paused = false, paused_at = NULL, reason = NULL")


def test_the_dashboard_role_cannot_touch_the_tables_behind_the_panels(ids):
    with psycopg.connect(DSN, autocommit=True) as conn:
        for sql in ("SELECT 1 FROM schedule_control", "SELECT 1 FROM fetch_state", "SELECT 1 FROM llm_usage",
                    "UPDATE schedule_control SET paused = true", "SELECT requeue_jobs('score')", "SELECT schedule_status()"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(sql)
