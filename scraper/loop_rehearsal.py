"""Runaway rehearsal: does anything loop, and is spending always bounded?

Runs the REAL service code and the REAL SQL against a throwaway database, with the stand-in APIs (stubs.py)
misbehaving on purpose: outages, garbage replies, replies that are billed but never recorded, runs that start
and fail, runs that hang forever. Every scenario repeats the work many times (as the scheduler would over days)
and checks that calls and dollars stay inside a hard bound. Nothing real is called, so it costs nothing.

    docker compose run --rm --no-deps -v "$PWD/postgres:/postgres:ro" scraper python loop_rehearsal.py

It creates the database `jobsearch_loop_test`, and drops it at the end. It never touches `jobsearch`.
Exit code 0 only if every scenario stays within its bound.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

import httpx
import psycopg
from psycopg.conninfo import make_conninfo

logging.disable(logging.CRITICAL)      # the scenarios provoke failures on purpose; the report below is what matters

TEST_DB = "jobsearch_loop_test"
POSTGRES_DIR = Path(os.environ.get("POSTGRES_DIR", "/postgres"))
os.environ["DB_NAME"] = TEST_DB          # before anything imports db / app: they must never see the real database


def conninfo(dbname: str) -> str:
    return make_conninfo(host=os.environ.get("DB_HOST", "postgres"), port=os.environ.get("DB_PORT", "5432"), dbname=dbname,
                         user=os.environ.get("DB_USER", "jobs"), password=os.environ.get("DB_PASSWORD", ""))


# ----------------------------------------------------------------------------- the throwaway database

def build_database() -> psycopg.Connection:
    with psycopg.connect(conninfo("postgres"), autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB}")
        admin.execute(f"CREATE DATABASE {TEST_DB}")
    conn = psycopg.connect(conninfo(TEST_DB), autocommit=True)
    conn.execute((POSTGRES_DIR / "schema.sql").read_text())
    for f in sorted((POSTGRES_DIR / "migrations").glob("*.sql")):
        conn.execute(f.read_text())
    return conn


def drop_database() -> None:
    with psycopg.connect(conninfo("postgres"), autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")


JOB_TEXT = "We use Go, Kafka and Kubernetes. 5+ years of experience. Build APIs for 3 million users."


def reset(conn, jobs: int, *, status: str = "new", score: int | None = None) -> list[int]:
    """Empty the data, restore default settings, and add `jobs` distinct jobs."""
    conn.execute("TRUNCATE jobs, job_sources, applications, llm_usage RESTART IDENTITY CASCADE")
    conn.execute("UPDATE budget_settings SET monthly_cap_usd = 10, daily_cap_usd = 1.5, apify_monthly_cap_usd = 15, apify_daily_cap_usd = 0.5")
    conn.execute("UPDATE schedule_settings SET last_run_at = NULL, run_now = false, enabled = true")
    conn.execute("UPDATE schedule_control SET paused = false")
    conn.execute("UPDATE tailor_settings SET verify = false, mode = 'auto', approval = 'review', max_score = NULL, min_score = NULL, daily_limit = NULL")
    ids = []
    for i in range(jobs):
        row = conn.execute("SELECT job_id FROM upsert_job('indeed', %s, 'u', 'x', %s, %s, 'Pune', false, now(), %s, NULL, NULL, NULL, '{}'::jsonb)",
                           (f"R{i}", f"Backend Engineer {i}", f"Company {i} Ltd", JOB_TEXT + f" Role number {i}.")).fetchone()
        ids.append(row[0])
    if status != "new":
        conn.execute("UPDATE jobs SET status = %s, score = %s, verdict = 'good', strengths = '[\"Go\"]', gaps = '[\"Kafka\"]' WHERE id = ANY(%s)",
                     (status, score, ids))
    return ids


def one(conn, sql: str, *args):
    return conn.execute(sql, args).fetchone()[0]


def spend(conn, provider: str) -> float:
    return float(one(conn, "SELECT coalesce(sum(cost_usd), 0) FROM llm_usage WHERE provider = %s", provider))


def parked(conn, stage: str) -> int:
    return one(conn, "SELECT count(*) FROM v_parked_jobs WHERE stage = %s", stage)


def age_leases(conn) -> None:
    """Let the claim leases run out, as they do between scheduled runs."""
    conn.execute("UPDATE jobs SET scoring_claimed_at = scoring_claimed_at - interval '31 minutes', "
                 "tailoring_claimed_at = tailoring_claimed_at - interval '21 minutes'")


# ----------------------------------------------------------------------------- results

class Report:
    def __init__(self):
        self.rows: list[tuple] = []

    def check(self, scenario: str, what: str, observed, bound, ok: bool, note: str = "") -> None:
        self.rows.append((scenario, what, observed, bound, ok, note))

    def print(self) -> bool:
        width = max(len(r[0]) for r in self.rows)
        print()
        for scenario, what, observed, bound, ok, note in self.rows:
            print(f"  {'PASS' if ok else 'FAIL'}  {scenario:<{width}}  {what}: {observed}  (bound: {bound}){'  ' + note if note else ''}")
        bad = [r for r in self.rows if not r[4]]
        print(f"\n{len(self.rows) - len(bad)} of {len(self.rows)} checks within their bounds" + ("" if not bad else f", {len(bad)} FAILED"))
        return not bad


# ----------------------------------------------------------------------------- scoring: emulates workflow 02

def score_run(store, profile, ai_url: str, *, record: bool = True, limit: int = 25) -> None:
    """What workflow 02 does for one scheduled run: claim, ask the model, record the score, or hand the claim
    back when there was no usable reply (the 'Release job (no reply)' node)."""
    import pipeline
    prepared = pipeline.prepare_batch(store, profile, limit, model="claude-haiku-5-5")
    for item in prepared["items"]:
        try:
            r = httpx.post(ai_url, json={"model": "claude-haiku-5-5", "max_tokens": item["max_tokens"],
                                         "messages": [{"role": "user", "content": item["user"]}]}, timeout=10)
        except httpx.HTTPError:
            store.release_score_claim(item["job_id"])
            continue
        if r.status_code != 200:
            store.release_score_claim(item["job_id"])
            continue
        data = r.json()
        if record:
            try:
                pipeline.score_one(store, item["job_id"], data["content"][0]["text"], 70, data.get("usage"), "claude-haiku-5-5")
            except pipeline.ScoreFailure:
                pass


def scoring_scenarios(conn, store, profile, rep: Report) -> None:
    import stubs
    JOBS, TICKS = 40, 60            # 60 runs of 25 = 30 hours of the default schedule

    def run(name, handler, record=True):
        reset(conn, JOBS)
        with stubs.StubAnthropic(handler) as ai:
            for _ in range(TICKS):
                score_run(store, profile, ai.url, record=record)
                age_leases(conn)
            return name, ai

    name, ai = run("score: API down for 30 hours", stubs.always_status(529))
    rep.check(name, "billed calls", ai.billed, 0, ai.billed == 0)
    rep.check(name, "jobs parked by the outage", parked(conn, "score"), 0, parked(conn, "score") == 0, "(refunded, so an outage never uses up attempts)")
    rep.check(name, "claims left on jobs", one(conn, "SELECT coalesce(sum(score_claims), 0) FROM jobs"), 0, one(conn, "SELECT coalesce(sum(score_claims), 0) FROM jobs") == 0)

    name, ai = run("score: replies billed but never recorded", stubs.scoring_ok(), record=False)
    rep.check(name, "billed calls", ai.billed, JOBS * 6, ai.billed <= JOBS * 6, "(6 claims per job, then parked)")
    rep.check(name, "jobs parked", parked(conn, "score"), JOBS, parked(conn, "score") == JOBS)

    name, ai = run("score: model returns garbage", stubs.garbage_reply())
    rep.check(name, "billed calls", ai.billed, JOBS * 3, ai.billed <= JOBS * 3, "(3 bad replies per job, then parked)")
    rep.check(name, "jobs parked", parked(conn, "score"), JOBS, parked(conn, "score") == JOBS)
    rep.check(name, "Claude spend logged", round(spend(conn, "anthropic"), 4), round(JOBS * 3 * 0.0005, 4), spend(conn, "anthropic") <= JOBS * 3 * 0.0005)

    name, ai = run("score: healthy API", stubs.scoring_ok())
    rep.check(name, "billed calls", ai.billed, JOBS, ai.billed == JOBS, "(each job scored once, never again)")
    rep.check(name, "jobs left unscored", one(conn, "SELECT count(*) FROM jobs WHERE status = 'new'"), 0, one(conn, "SELECT count(*) FROM jobs WHERE status = 'new'") == 0)

    # the spending cap stops a healthy run too
    reset(conn, 200)
    conn.execute("UPDATE budget_settings SET daily_cap_usd = 0.01")
    with stubs.StubAnthropic(stubs.scoring_ok()) as ai:
        for _ in range(TICKS):
            score_run(store, profile, ai.url)
            age_leases(conn)
    s = spend(conn, "anthropic")
    rep.check("score: daily cap of $0.01", "Claude spend", round(s, 4), "0.01 + one batch (0.025)", s <= 0.01 + 0.025, f"({ai.billed} calls over {TICKS} runs)")
    rep.check("score: daily cap of $0.01", "jobs still waiting", one(conn, "SELECT count(*) FROM jobs WHERE status = 'new'"), "most of 200", one(conn, "SELECT count(*) FROM jobs WHERE status = 'new'") > 100)


# ----------------------------------------------------------------------------- tailoring

RESUME = {
    "basics": {"name": "Jane Doe", "email": "jane@example.com", "label": "Software Engineer"},
    "work": [{"id": "w1", "name": "Acme", "position": "Software Engineer I", "startDate": "2026-01", "endDate": "",
              "highlights": [{"id": "w1-b1", "group": "Probe Platform (Go, Redis, PostgreSQL)",
                              "text": "Built a multi-tenant orchestration platform across 7+ LLM engines, architected for 50,000+ executions/day across 500+ tenants."},
                             {"id": "w1-b2", "group": "Probe Platform (Go, Redis, PostgreSQL)",
                              "text": "Eliminated duplicate LLM billing on retries via idempotent, credit-safe execution."},
                             {"id": "w1-b3", "group": "Realtime Gateway (WebSockets, JWT)",
                              "text": "Architected a JWT-gated WebSocket gateway with Redis Pub/Sub sustaining 10,000+ concurrent connections per node."}]}],
    "education": [{"institution": "Example University", "studyType": "B.Tech", "area": "Computer Science", "endDate": "2026"}],
    "skills": [{"name": "Languages", "keywords": ["Go", "Python", "SQL"]}, {"name": "Backend", "keywords": ["PostgreSQL", "Redis", "Kubernetes"]}],
}
PROFILE = {"headline": "Backend engineer", "years_experience": 1.25, "target_roles": ["Backend Engineer"], "skills": {"strong": ["Go", "PostgreSQL"]}}
GOOD_PATCH = {
    "headline": "Backend Engineer",
    "summary": "Backend engineer who builds multi-tenant platforms in Go and PostgreSQL.",
    "work": [{"id": "w1", "show": True, "order": ["w1-b3", "w1-b1", "w1-b2"], "rewrite": {}}],
    "skills": {"order": ["Backend", "Languages"], "emphasize": ["go"]},
    "projects": {"include": []}, "additional": {"include": []},
    "requirements": [{"requirement": "Go experience", "status": "matched", "evidence": "w1-b1", "note": ""},
                     {"requirement": "Kafka", "status": "gap", "evidence": None, "note": "not in my resume"}],
    "cover_letter": {"greeting": "Dear Hiring Manager,",
                     "paragraphs": ["I would like to apply for the Backend Engineer role. I build multi-tenant platforms in Go.",
                                    "At Acme I built a platform handling 50,000+ executions/day across 500+ tenants. I also built a JWT-gated gateway.",
                                    "I would welcome the chance to help your team with its Kubernetes services."],
                     "closing": "Sincerely,"},
}


class CrashingPDF:
    def html_to_pdf(self, html):
        raise RuntimeError("the PDF service is down")


def tailoring_scenarios(conn, store, rep: Report) -> None:
    import anthropic_client
    import stubs
    import tailor_service
    JOBS, TICKS = 3, 40             # 40 runs of the 15-minute schedule = 10 hours

    def run(name, handler, pdf=None):
        reset(conn, JOBS, status="shortlisted", score=80)
        with stubs.StubAnthropic(handler) as ai, tempfile.TemporaryDirectory() as out:
            anthropic_client.API_URL = ai.url
            try:
                svc = tailor_service.TailorService(store, anthropic_client.AnthropicClient("stub", client=httpx.Client(timeout=10), sleep=lambda s: None),
                                                   pdf or CrashingPDF(), out, RESUME, PROFILE)
                for _ in range(TICKS):
                    svc.run_next(3)
                    age_leases(conn)
            finally:
                anthropic_client.API_URL = "https://api.anthropic.com/v1/messages"
            return name, ai

    name, ai = run("tailor: API down for 10 hours", stubs.always_status(529))
    rep.check(name, "billed calls", ai.billed, 0, ai.billed == 0)
    rep.check(name, "jobs parked by the outage", parked(conn, "tailor"), 0, parked(conn, "tailor") == 0, "(refunded)")

    flawed = copy.deepcopy(GOOD_PATCH)
    flawed["summary"] = "Backend engineer experienced with Terraform."          # forces a revision call
    name, ai = run("tailor: draft billed, revision hits an outage", stubs.tailor_draft(GOOD_PATCH, then_status=529, flawed=flawed))
    rep.check(name, "billed calls", ai.billed, JOBS * 3, ai.billed <= JOBS * 3, "(3 claims per job, then parked)")
    rep.check(name, "jobs parked", parked(conn, "tailor"), JOBS, parked(conn, "tailor") == JOBS)
    rep.check(name, "Claude spend logged", round(spend(conn, "anthropic"), 3), round(JOBS * 3 * 0.025, 3), spend(conn, "anthropic") <= JOBS * 3 * 0.025)

    name, ai = run("tailor: good draft, then the PDF service is down", stubs.tailor_draft(GOOD_PATCH))
    rep.check(name, "billed calls", ai.billed, JOBS * 2, ai.billed <= JOBS * 2, "(2 failures per job, then parked)")
    rep.check(name, "jobs parked", parked(conn, "tailor"), JOBS, parked(conn, "tailor") == JOBS)


# ----------------------------------------------------------------------------- Apify, through the real endpoint

def apify_scenarios(conn, rep: Report) -> None:
    import apify_source
    import stubs
    from fastapi.testclient import TestClient
    os.environ["APIFY_TOKEN_CURIOUS"] = "tok-a"
    for k in ("APIFY_TOKEN_CHEAP", "APIFY_TOKEN_BEBITY"):
        os.environ.pop(k, None)
    import app
    client = TestClient(app.app)
    SEARCHES = 100                  # four times what the default schedule starts in a whole day

    def run(mode, charge=0.047):
        reset(conn, 0)
        with stubs.StubApify({"tok-a": 80.0}, {"tok-a": mode}, charge=charge) as apify:
            apify_source.API = apify.url
            try:
                codes = [client.post("/search/linkedin", json={"search_term": "backend", "location": "Pune"}).status_code for _ in range(SEARCHES)]
            finally:
                apify_source.API = "https://api.apify.com/v2"
            return apify, codes

    apify, codes = run("ok")
    rep.check("apify: healthy runs, 100 searches", "runs started", apify.starts, 10, apify.starts <= 10, "(daily cap $0.50)")
    rep.check("apify: healthy runs, 100 searches", "real charge", round(apify.charged, 3), 0.5, apify.charged <= 0.5)
    rep.check("apify: healthy runs, 100 searches", "logged spend", round(spend(conn, "apify"), 3), 0.5, spend(conn, "apify") <= 0.5)
    rep.check("apify: healthy runs, 100 searches", "searches skipped, not failed", sum(1 for c in codes if c == 200) - apify.starts, "most", codes.count(502) == 0)

    # your real setup: three accounts configured, different balances (the dearest actor bills a 150-result minimum)
    os.environ.update(APIFY_TOKEN_CHEAP="tok-b", APIFY_TOKEN_BEBITY="tok-c")
    reset(conn, 0)
    with stubs.StubApify({"tok-a": 9.6, "tok-b": 4.9, "tok-c": 4.9}, {}, charge=0.047) as apify:
        apify_source.API = apify.url
        try:
            codes3 = [client.post("/search/linkedin", json={"search_term": "backend", "location": "Pune"}).status_code for _ in range(SEARCHES)]
        finally:
            apify_source.API = "https://api.apify.com/v2"
    rep.check("apify: three accounts, 100 searches", "real charge", round(apify.charged, 3), 0.5, apify.charged <= 0.5)
    rep.check("apify: three accounts, 100 searches", "logged spend", round(spend(conn, "apify"), 3), 0.5, spend(conn, "apify") <= 0.5)
    rep.check("apify: three accounts, 100 searches", "runs that still fit before the cap", apify.starts, "at least 3", apify.starts >= 3,
              "(the guard is not over-conservative: it uses the budget instead of refusing early)")
    for k in ("APIFY_TOKEN_CHEAP", "APIFY_TOKEN_BEBITY"):
        os.environ.pop(k, None)

    apify, codes = run("fail")
    rep.check("apify: every run starts then fails", "runs started", apify.starts, 12, apify.starts <= 12, "(failed runs are counted at 1.5x their estimate)")
    rep.check("apify: every run starts then fails", "real charge", round(apify.charged, 3), 0.5, apify.charged <= 0.5)

    apify, codes = run("start_error")
    rep.check("apify: every start refused (no credit)", "money spent", round(apify.charged, 3), 0, apify.charged == 0 and spend(conn, "apify") == 0)

    reset(conn, 0)
    with stubs.StubApify({"tok-a": 80.0}, {"tok-a": "hang"}) as apify:
        apify_source.API = apify.url
        try:
            try:
                apify_source.search_linkedin(apify_source.SearchParams("backend", "Pune"), env={"APIFY_TOKEN_CURIOUS": "tok-a"}, sleep=lambda s: None)
            except apify_source.ApifyError:
                pass
        finally:
            apify_source.API = "https://api.apify.com/v2"
    rep.check("apify: a run that never finishes", "status polls before giving up", apify.polls, 130, apify.polls <= 130)
    rep.check("apify: a run that never finishes", "runs aborted", apify.aborts, 1, apify.aborts == 1)


# ----------------------------------------------------------------------------- the scheduler

def scheduler_scenarios(conn, rep: Report) -> None:
    reset(conn, 0)
    conn.execute("UPDATE schedule_settings SET at_time = '00:00', every_minutes = NULL WHERE name = 'digest'")
    started = sum(one(conn, "SELECT count(*) FROM claim_due_schedules()") for _ in range(2000))
    rep.check("scheduler: 2000 ticks within the same minute", "stages started", started, 4, started == 4, "(each stage exactly once)")
    conn.execute("UPDATE schedule_control SET paused = true")
    conn.execute("UPDATE schedule_settings SET last_run_at = NULL, run_now = true")
    started = sum(one(conn, "SELECT count(*) FROM claim_due_schedules()") for _ in range(500))
    rep.check("scheduler: paused, even with run-now requests", "stages started", started, 0, started == 0)
    conn.execute("UPDATE schedule_control SET paused = false")


# ----------------------------------------------------------------------------- main

def main() -> int:
    conn = build_database()
    rep = Report()
    try:
        import db
        import scoring
        store = db.PgStore(db.conninfo_from_env())
        profile = scoring.load_profile(os.environ.get("REHEARSAL_PROFILE", "/config/profile.example.json"))
        print("Scoring scenarios ...", flush=True)
        scoring_scenarios(conn, store, profile, rep)
        print("Tailoring scenarios ...", flush=True)
        tailoring_scenarios(conn, store, rep)
        print("Apify scenarios ...", flush=True)
        apify_scenarios(conn, rep)
        print("Scheduler scenarios ...", flush=True)
        scheduler_scenarios(conn, rep)
    finally:
        conn.close()
        drop_database()
    return 0 if rep.print() else 1


if __name__ == "__main__":
    sys.exit(main())
