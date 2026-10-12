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

from models import Board, JobEvent, JobsQuery, Stats
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
    assert r.status_code == 200 and r.json() == {"id": ids["acme"], "status": "approved", "event_id": r.json()["event_id"]} and r.json()["event_id"]
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


def test_the_numbers_agree_with_each_other_on_awkward_histories(store, ids):
    """Reply figures never exceed the applied figure, whatever order a job went through the stages."""
    _admin("SELECT set_application_status(%s, 'interview')", (ids["beta"],))             # straight to interview
    _admin("SELECT set_application_status(%s, 'applied')", (ids["acme"],))
    _admin("SELECT set_application_status(%s, 'offer')", (ids["acme"],))
    _admin("SELECT set_application_status(%s, 'skipped')", (ids["acme"],))                # applied_at is kept
    stats = Stats.model_validate(store.stats())
    assert stats.applied >= stats.responded >= stats.reached_interview >= stats.reached_offer >= 1
    assert sum(w.applied for w in stats.weekly) == stats.applied, "every submitted job is in exactly one week"


# ---------------------------------------------------------------- version 3b: notes, contacts, follow-ups

def test_tracking_round_trip_with_the_version_check(store, ids):
    from models import Tracking
    from store import TrackingChanged
    job = ids["beta"]
    empty = Tracking.model_validate(store.job_tracking(job))
    assert empty.version is None and empty.notes is None
    from datetime import date, timedelta
    when = (date.today() + timedelta(days=30)).isoformat()
    saved = Tracking.model_validate(store.set_tracking(job, None, {"notes": "  met at the fair ", "contact_name": "Priya N", "follow_up_on": when}))
    assert saved.notes == "met at the fair" and str(saved.follow_up_on) == when and saved.version
    assert saved.today and empty.today == saved.today
    with pytest.raises(TrackingChanged):
        store.set_tracking(job, None, {"notes": "from a stale page"})
    again = Tracking.model_validate(store.set_tracking(job, saved.version, {"referral": "via Sam"}))
    assert again.version != saved.version and again.notes == "met at the fair" and again.referral == "via Sam"
    assert Tracking.model_validate(store.job_tracking(job)) == again
    gone = Tracking.model_validate(store.set_tracking(job, again.version, {"notes": "", "contact_name": None, "referral": None, "follow_up_on": None}))
    assert gone.version is None


def test_tracking_refusals_carry_the_database_s_reason(store, ids):
    with pytest.raises(Invalid, match="email"):
        store.set_tracking(ids["acme"], None, {"contact_email": "nope"})
    with pytest.raises(Invalid):
        store.set_tracking(ids["acme"], None, {"follow_up_on": "2999-01-01"})
    with pytest.raises(NotFound):
        store.set_tracking(10**12, None, {"notes": "x"})
    with pytest.raises(NotFound):
        store.job_tracking(10**12)
    with pytest.raises(Invalid):
        store.set_tracking(ids["acme"], None, {"notes": "a\x00b"})        # the database refuses a NUL even if the API did not


def test_the_api_limits_match_the_database(store, ids):
    """The Pydantic limits and the table's limits are the same numbers: the longest accepted by one is accepted by the other."""
    from models import TrackingSave
    longest = {"notes": "n" * 2000, "contact_name": "c" * 100, "contact_email": "e" * 190 + "@x.example", "referral": "r" * 200}
    TrackingSave(**longest)
    store.set_tracking(ids["acme"], None, TrackingSave(**longest).patch())
    store.set_tracking(ids["acme"], store.job_tracking(ids["acme"])["version"], {"notes": None, "contact_name": None, "contact_email": None, "referral": None})
    for field, size in (("notes", 2001), ("contact_name", 101), ("referral", 201)):
        with pytest.raises(ValueError):
            TrackingSave(**{field: "x" * size})


def test_the_board_lists_what_is_due_and_the_setting_changes_it(store, ids):
    from models import Board
    _admin("SELECT set_application_status(%s, 'applied')", (ids["tailored"],))
    _admin("UPDATE applications SET applied_at = now() - interval '20 days' WHERE job_id = %s", (ids["tailored"],))
    try:
        board = Board.model_validate(store.board())
        due = {f.id: f for f in board.follow_ups}
        assert board.today and due[ids["tailored"]].reason == "waiting" and due[ids["tailored"]].days_waiting >= 20
        card = next(c for col in board.columns for c in col.items if c.id == ids["tailored"])
        assert card.follow_up == "waiting"
        assert store.set_follow_up_days(60) == 60
        assert ids["tailored"] not in {f.id for f in Board.model_validate(store.board()).follow_ups}
        with pytest.raises(Invalid):
            store.set_follow_up_days(0)
    finally:
        _admin("UPDATE follow_up_settings SET days = 7")


def test_the_role_cannot_touch_the_tracking_tables(ids):
    with psycopg.connect(DSN, autocommit=True) as conn:
        for sql in ("SELECT 1 FROM job_tracking", "SELECT 1 FROM follow_up_settings", "DELETE FROM job_tracking",
                    "SELECT 1 FROM follow_ups_due()"):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(sql)


# ---------------------------------------------------------------- undo

def test_undo_takes_back_a_mistaken_move_so_the_numbers_never_see_it(store, ids):
    from models import JobEvent, Stats
    from store import HistoryChanged
    job = _admin("SELECT job_id FROM upsert_job('indeed', 'undo-1', 'https://example.com/undo-1', NULL, 'Undo Role', 'UndoCo', 'Pune',"
                 " false, now(), 'about undo', NULL, NULL, NULL, '{}'::jsonb)")[0][0]
    _admin("UPDATE jobs SET status = 'shortlisted', score = 80 WHERE id = %s", (job,))
    before = Stats.model_validate(store.stats())
    store.set_status(job, "applied", "shortlisted", None)
    store.set_status(job, "offer", "applied", "dropped on the wrong column")
    assert Stats.model_validate(store.stats()).reached_offer == before.reached_offer + 1
    events = [JobEvent.model_validate(e) for e in store.job_events(job)]
    assert [e.undoable for e in events] == [False, True]
    with pytest.raises(HistoryChanged):
        store.undo_status(job, "offer", events[0].id)                        # not the newest
    assert store.undo_status(job, "offer", events[1].id) == {"id": job, "status": "applied"}
    assert Stats.model_validate(store.stats()).reached_offer == before.reached_offer
    assert [e["to_status"] for e in store.job_events(job)] == ["applied"]
    with pytest.raises(HistoryChanged):
        store.undo_status(job, "offer", None)                                 # that move is gone
    assert store.undo_status(job, "applied", None) == {"id": job, "status": "shortlisted"}
    assert store.job_events(job) == []


def test_undo_refusals_reach_the_api_layer_as_the_right_errors(store, ids):
    with pytest.raises(Forbidden):
        store.undo_status(ids["scam"], "applied", None)
    with pytest.raises(NotFound):
        store.undo_status(10**12, "applied", None)
    with pytest.raises(Invalid, match="dashboard"):
        store.undo_status(ids["interview"], "interview", None)               # made through the CLI in the seed


def test_a_move_names_its_entry_and_a_no_op_names_none(store, ids):
    job = _admin("SELECT job_id FROM upsert_job('indeed', 'undo-2', 'https://example.com/undo-2', NULL, 'Undo Role 2', 'UndoCo2', 'Pune',"
                 " false, now(), 'about undo', NULL, NULL, NULL, '{}'::jsonb)")[0][0]
    _admin("UPDATE jobs SET status = 'shortlisted', score = 80 WHERE id = %s", (job,))
    first = store.move_status(job, "applied", "shortlisted", None)
    newest = store.job_events(job)[-1]["id"]
    assert first == {"status": "applied", "event_id": newest}
    assert store.move_status(job, "applied", "applied", "") == {"status": "applied", "event_id": None}
    assert len(store.job_events(job)) == 1, "the no-op wrote nothing, so there is nothing for an Undo to take back"
    second = store.move_status(job, "interview", "applied", "phone screen")
    assert second["event_id"] and second["event_id"] != newest
    from store import HistoryChanged
    with pytest.raises(HistoryChanged):
        store.undo_status(job, "interview", newest)                              # the older entry, by its id
    assert store.undo_status(job, "interview", second["event_id"]) == {"id": job, "status": "applied"}
