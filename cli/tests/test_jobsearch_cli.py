import io
import json
import os
import re
import shutil
import stat
import urllib.error
from pathlib import Path

import pytest

import jobsearch_cli as C

REPO = Path(__file__).resolve().parents[2]
CONFIG = REPO / "config"


# ------------------------------------------------------------------ a fake stack

class FakeStack:
    def __init__(self, **over):
        self.ps_rows = over.get("ps_rows", [
            {"Service": "postgres", "State": "running", "Health": "healthy", "Status": "Up 2 hours (healthy)"},
            {"Service": "scraper", "State": "running", "Health": "", "Status": "Up 2 hours"},
            {"Service": "n8n", "State": "running", "Health": "", "Status": "Up 2 hours"}])
        # by default the fake database is fully migrated: derived from the repo, so adding a
        # migration never breaks this test
        self.migrations = over.get("migrations", ["000_baseline"] + sorted(
            p.stem for p in (REPO / "postgres" / "migrations").glob("*.sql")))
        self.apify = over.get("apify", [{"account": "cheap", "remaining_usd": 4.9}])
        self.config = over.get("config", {"profile": "ok", "resume": "ok"})
        self.search = over.get("search", [{"search_terms": ["backend engineer"], "locations": ["Pune"], "score_threshold": 70}])
        self.health = over.get("health", '{"ok":true}')
        self.n8n = over.get("n8n", '{"status":"ok"}')
        self.sql_log = []
        self.jobs = over.get("jobs", {})
        self.budget = over.get("budget", {"month_spent": 1.2, "day_spent": 0.1, "monthly_cap": 10, "daily_cap": 1.5,
                                          "month_remaining": 8.8, "day_remaining": 1.4, "warn": False, "blocked": False,
                                          "blocked_reason": None, "warn_pct": 80})
        self.apify_budget = over.get("apify_budget", {"month_spent": 0.4, "day_spent": 0.1, "monthly_cap": 15, "daily_cap": 0.5,
                                                      "month_remaining": 14.6, "day_remaining": 0.4, "blocked": False, "blocked_reason": None})
        self.parked = over.get("parked", [])
        self.reshortlist = over.get("reshortlist", [])
        self.scoring = over.get("scoring", {"waiting": 2, "hours_since_score": 0.5})
        self.credentials = over.get("credentials", ["Anthropic API key", "Telegram bot"])
        self.queue = over.get("queue", [{"job_id": 24, "score": 82, "variant": "tailored", "title": "Backend Engineer", "company": "Acme",
                                         "needs_attention": True, "attention_reason": "the resume is 2 pages (limit 1)"}])
        self.apps = over.get("apps", [{"files_dir": "/output/applications/24-acme-backend", "status": "drafted",
                                       "needs_attention": False, "attention_reason": None}])
        self.features_body = over.get("features", {
            "flags": {"tailoring": True, "telegram": True, "digest": True},
            "effective": {n: {"on": True, "reason": ""} for n in ("tailoring", "telegram", "digest")},
            "approval": "review", "approval_effective": "review",
            "lines": ["scoring:    always on", "tailoring:  on", "telegram:   on", "digest:     on", "approval:   review"]})
        self.job_rows = over.get("job_rows", [
            {"id": 7, "score": 82, "verdict": "strong", "status": "shortlisted", "title": "Backend Engineer", "company": "Acme",
             "location": "Pune", "url": "https://acme.com/7", "scam_risk": "low", "posted": "2026-10-09", "first_seen": "2026-10-10",
             "application": None, "note": None},
            {"id": 9, "score": None, "verdict": None, "status": "new", "title": "=HYPERLINK(\"x\")", "company": "Globex",
             "location": None, "url": None, "scam_risk": "medium", "posted": None, "first_seen": "2026-10-10",
             "application": "applied", "note": "applied on site"}])
        self.job_detail = over.get("job_detail", [{
            "id": 7, "title": "Backend Engineer", "company": "Acme", "location": "Pune", "url": "https://acme.com/7", "status": "shortlisted",
            "score": 82, "verdict": "strong", "scam_risk": "medium", "scam_flags": [{"code": "free_email"}], "strengths": ["Python"],
            "gaps": ["No Kafka"], "gates": {"eligibility": {"verdict": "UNKNOWN", "note": "visa unclear"}}, "deadline": None,
            "posted": "2026-10-09", "application": None, "variant": None, "applied_on": None, "note": None, "files_dir": None,
            "needs_attention": None, "attention_reason": None}])
        self.schedule_body = over.get("schedule", {
            "timezone": "Asia/Kolkata",
            "schedules": [
                {"name": "collect", "enabled": True, "every_minutes": 360, "at_time": None, "batch": 18, "model": None},
                {"name": "score", "enabled": True, "every_minutes": 30, "at_time": None, "batch": 10, "model": "claude-haiku-5-5"},
                {"name": "tailor", "enabled": False, "every_minutes": 15, "at_time": None, "batch": 3, "model": None},
                {"name": "digest", "enabled": True, "every_minutes": None, "at_time": "08:00:00", "batch": 10, "model": None}],
            "lines": ["collect  on  every 6 hours  18 searches per run", "digest   on  daily at 08:00  10 matches listed per run"]})
        self.mail_body = over.get("mail", {
            "lookback_days": 60, "from_now": False, "search_cursor": None, "token_status": "none", "token_checked_at": None, "last_run_at": None,
            "last_error": None, "messages_seen": 0, "pending": 0, "accepted": 0, "dismissed": 0,
            "enabled": False, "reason": "switched off (jobsearch features set --gmail on)", "configured": False,
            "lines": ["gmail:      off, switched off (jobsearch features set --gmail on)", "login:      not set up: scripts/jobsearch gmail login (docs/gmail.md)",
                      "search:     never run; the first search looks back 60 days",
                      "emails:     0 looked at; suggestions: 0 waiting for you, 0 accepted, 0 dismissed"]})
        self.published = over.get("published", ["js-scheduler", "js-collect", "js-score", "js-tailor", "js-mail", "js-digest", "js-telegram"])
        self.service_status = over.get("service_status", 200)
        self.service_reply = over.get("service_reply", {})
        self.spend = over.get("spend", [{"provider": "anthropic", "purpose": "score", "model": "claude-haiku-5-5", "calls": 40,
                                         "input_tokens": 100000, "output_tokens": 14000, "cache_read_tokens": 52000, "cost_usd": 0.0187}])
        # Phase 7: jobs stored per source (None = the query fails), fetch_state rows with failures >= 5, and the collect
        # board_batch (None = migration 013 is not applied, so the column does not exist)
        self.sources_counts = over.get("sources_counts", {})
        self.fetch_failures = over.get("fetch_failures", [])
        self.board_batch = over.get("board_batch", 30)
        self.psql_raises = over.get("psql_raises", False)
        # the services of the active compose profiles (None = `docker compose config` fails), and what the
        # dashboard's /api/health answers
        self.services = over.get("services", ["postgres", "scraper", "gotenberg", "n8n"])
        self.dashboard_health = over.get("dashboard_health", '{"ok": true, "db": true}')
        self.exec_log = []

    def ps(self):
        return self.ps_rows

    def config_services(self):
        if self.services is None:
            raise C.StackError("permission denied while trying to connect to the Docker daemon")
        return self.services

    def query_json(self, sql, db="jobsearch"):
        self.sql_log.append(sql)
        if "from job_sources" in sql:
            if self.sources_counts is None:
                raise C.StackError("relation \"job_sources\" does not exist")
            return [{"source": k, "n": v} for k, v in self.sources_counts.items()]
        if "from fetch_state" in sql:
            return self.fetch_failures
        if "board_batch from schedule_settings" in sql:
            if self.board_batch is None:
                raise C.StackError('column "board_batch" does not exist')
            return [{"board_batch": self.board_batch}]
        if "apify_budget_status" in sql:
            return [self.apify_budget]
        if "v_parked_jobs" in sql:
            return self.parked if "group by" not in sql and "count(*)::int as n from" not in sql else (
                [{"stage": st, "n": sum(1 for p in self.parked if p["stage"] == st)} for st in sorted({p["stage"] for p in self.parked})]
                if "group by" in sql else [{"n": len(self.parked)}])
        if "hours_since_score" in sql:
            return [self.scoring]
        if "requeue_jobs" in sql:
            return [{"n": 3}]
        if "v_reshortlist" in sql:
            return self.reshortlist
        if "reshortlist()" in sql:
            return [{"n": len(self.reshortlist)}]
        if "FROM jobs j LEFT JOIN applications a" in sql:
            return self.job_detail if "WHERE j.id =" in sql and "ORDER BY" not in sql else self.job_rows
        if "budget_status" in sql:
            return [self.budget]
        if "workflow_entity" in sql:
            return [{"id": i} for i in self.published]
        if "credentials_entity" in sql:
            return [{"name": n} for n in self.credentials]
        if "v_spend_month" in sql:
            return self.spend
        if "v_review_queue" in sql:
            return self.queue
        if "from applications where job_id" in sql:
            return self.apps
        if "schema_migrations" in sql:
            return [{"version": v} for v in self.migrations]
        if "from search_profile where name" in sql:
            return self.search
        if "group by status" in sql and "from jobs" in sql:
            return self.jobs.get("counts", [{"status": "new", "n": 5}, {"status": "shortlisted", "n": 2}])
        if "first_seen >" in sql:
            return [{"n": 3}]
        if "scam_risk is not null" in sql:
            return [{"scam_risk": "low", "n": 6}, {"scam_risk": "medium", "n": 1}]
        if "v_shortlist" in sql:
            return self.jobs.get("top", [
                {"id": 7, "title": "Backend Engineer", "company": "Acme", "location": "Pune", "score": 82,
                 "verdict": "strong", "scam_risk": "low", "deadline": None, "closing_soon": True, "stale": False},
                {"id": 9, "title": "Data Engineer", "company": "Globex", "location": None, "score": 71,
                 "verdict": "good", "scam_risk": "medium", "deadline": None, "closing_soon": False, "stale": True}])
        if "from applications" in sql:
            return self.jobs.get("apps", [])
        return []

    def psql(self, sql, db="jobsearch"):
        self.sql_log.append(sql)
        if self.psql_raises:
            raise C.StackError("connection refused")
        return ""

    def service_body(self, code):
        return self.service_reply

    def scraper_py(self, code, timeout=90):
        self.py_calls = getattr(self, "py_calls", [])
        self.py_calls.append(code)
        if "http://localhost:8000/schedule" in code:
            return json.dumps({"status": self.service_status, "body": self.schedule_body})
        if "http://localhost:8000/features" in code:
            return json.dumps({"status": self.service_status, "body": self.features_body})
        if "http://localhost:8000/mail/" in code:
            return json.dumps({"status": self.service_status, "body": self.mail_body})
        if "http://localhost:8000/tailor" in code:
            return json.dumps({"status": self.service_status, "body": self.service_body(code)})
        if "/health" in code:
            return self.health
        if "apify_source" in code:
            return json.dumps(self.apify)
        if "scoring.load_profile" in code:
            return json.dumps(self.config)
        raise AssertionError("unexpected snippet")

    def exec_out(self, service, args):
        self.exec_log.append((service, args))
        if service == "dashboard":
            if isinstance(self.dashboard_health, Exception):
                raise self.dashboard_health
            return self.dashboard_health
        if service == "n8n":
            return self.n8n
        if service == "postgres":
            return "0 migration(s) applied to jobsearch"
        raise AssertionError(service)


def by_name(checks):
    return {c.name: c for c in checks}


# ------------------------------------------------------------------ Stack command building

def test_local_command_building():
    s = C.Stack(env={})
    assert s.build(["ps"]) == ["docker", "compose", "ps"]
    s = C.Stack(env={"JOBSEARCH_COMPOSE": "sudo docker compose"})
    assert s.build(["ps"]) == ["sudo", "docker", "compose", "ps"]


def test_ssh_command_building_quotes_arguments():
    s = C.Stack(env={"JOBSEARCH_SSH": "ssh -i /k/key ubuntu@host", "JOBSEARCH_REMOTE_DIR": "~/jobsearch",
                     "JOBSEARCH_COMPOSE": "sudo docker compose"})
    cmd = s.build(["exec", "-T", "postgres", "sh", "-c", 'psql -U "$POSTGRES_USER"'])
    assert cmd[:4] == ["ssh", "-i", "/k/key", "ubuntu@host"] and len(cmd) == 5
    assert cmd[4].startswith("cd ~/jobsearch && sudo docker compose exec -T postgres sh -c ")
    assert "'psql -U \"$POSTGRES_USER\"'" in cmd[4]  # one quoted argument for the remote shell


def test_ps_parses_lines_and_arrays(monkeypatch):
    s = C.Stack(env={})

    class R:
        returncode = 0
        stderr = ""
        def __init__(self, out): self.stdout = out

    monkeypatch.setattr(s, "compose", lambda *a, **k: R('{"Service":"a"}\n{"Service":"b"}\n'))
    assert [r["Service"] for r in s.ps()] == ["a", "b"]
    monkeypatch.setattr(s, "compose", lambda *a, **k: R('[{"Service":"a"}]'))
    assert s.ps() == [{"Service": "a"}]
    monkeypatch.setattr(s, "compose", lambda *a, **k: R(""))
    assert s.ps() == []


def test_failed_command_raises_stack_error(monkeypatch):
    s = C.Stack(env={})

    class R:
        returncode = 1
        stdout = ""
        stderr = "no configuration file provided"

    monkeypatch.setattr(s, "compose", lambda *a, **k: R())
    with pytest.raises(C.StackError, match="no configuration file"):
        s.ps()


# ------------------------------------------------------------------ doctor

def test_doctor_all_green():
    checks = C.run_doctor(FakeStack(), REPO)
    assert all(c.status == "ok" for c in checks), [(c.name, c.status, c.detail) for c in checks]
    assert {"database migrations", "scraper service", "n8n", "apify accounts", "config/profile.json"} <= set(by_name(checks))


def test_doctor_stops_early_when_a_container_is_down():
    rows = [{"Service": "postgres", "State": "running", "Health": "healthy"},
            {"Service": "scraper", "State": "exited"}]
    checks = by_name(C.run_doctor(FakeStack(ps_rows=rows), REPO))
    assert checks["container scraper"].status == "fail" and checks["container n8n"].status == "fail"
    assert "database migrations" not in checks


def test_doctor_unhealthy_and_starting():
    rows = [{"Service": "postgres", "State": "running", "Health": "starting"},
            {"Service": "scraper", "State": "running"}, {"Service": "n8n", "State": "running", "Health": "unhealthy"}]
    c = by_name(C.run_doctor(FakeStack(ps_rows=rows), REPO))
    assert c["container postgres"].status == "warn" and c["container n8n"].status == "fail"


def test_doctor_pending_migration_is_a_failure_with_the_fix():
    files = sorted(p.stem for p in (REPO / "postgres" / "migrations").glob("*.sql"))
    m = by_name(C.run_doctor(FakeStack(migrations=["000_baseline"] + files[:-1]), REPO))["database migrations"]
    assert m.status == "fail" and files[-1] in m.detail and "migrate" in m.detail


def test_doctor_warns_about_setup_not_done():
    s = FakeStack(apify=[], config={"profile": ["profile not found at /config/profile.json; ..."], "resume": "missing"},
                  search=[{"search_terms": ["CHANGE ME role title"], "locations": ["CHANGE ME city"], "score_threshold": 70}])
    c = by_name(C.run_doctor(s, REPO))
    assert c["apify accounts"].status == "warn"
    assert c["config/profile.json"].status == "warn" and "/setup" in c["config/profile.json"].detail
    assert c["config/resume.json"].status == "warn"
    assert c["search profile"].status == "warn" and "placeholder" in c["search profile"].detail


def test_doctor_invalid_config_is_a_failure():
    c = by_name(C.run_doctor(FakeStack(config={"profile": ["headline: required non-empty string"], "resume": ["work: required"]}), REPO))
    assert c["config/profile.json"].status == "fail" and "headline" in c["config/profile.json"].detail
    assert c["config/resume.json"].status == "fail"


def test_doctor_low_apify_credit_warns_and_unknown_balance_is_ok():
    c = by_name(C.run_doctor(FakeStack(apify=[{"account": "cheap", "remaining_usd": 0.2}]), REPO))
    assert c["apify accounts"].status == "warn"
    c = by_name(C.run_doctor(FakeStack(apify=[{"account": "cheap", "remaining_usd": None}]), REPO))
    assert c["apify accounts"].status == "ok" and "unknown" in c["apify accounts"].detail


def test_doctor_reports_docker_unreachable():
    class Down(FakeStack):
        def ps(self):
            raise C.StackError("docker: command not found")
    checks = C.run_doctor(Down(), REPO)
    assert len(checks) == 1 and checks[0].status == "fail" and "docker" in checks[0].detail


DASHBOARD_ROW = {"Service": "dashboard", "State": "running", "Health": "healthy", "Status": "Up 5 minutes (healthy)",
                 "Publishers": [{"URL": "127.0.0.1", "TargetPort": 8080, "PublishedPort": 8081, "Protocol": "tcp"}]}
WITH_DASHBOARD = ["postgres", "scraper", "gotenberg", "n8n", "dashboard-db-init", "dashboard"]


def dashboard_line(**over):
    stack = FakeStack(**over)
    return by_name(C.run_doctor(stack, REPO))["dashboard"], stack


def test_doctor_dashboard_not_enabled_is_fine_and_is_not_probed():
    line, stack = dashboard_line()
    assert line.status == "ok" and line.detail == "not enabled (optional, see docs/dashboard.md)"
    assert not [e for e in stack.exec_log if e[0] == "dashboard"]


def test_doctor_dashboard_healthy_names_the_published_port():
    line, stack = dashboard_line(ps_rows=FakeStack().ps_rows + [DASHBOARD_ROW], services=WITH_DASHBOARD)
    assert (line.status, line.detail) == ("ok", "ok on 127.0.0.1:8081")
    service, args = next(e for e in stack.exec_log if e[0] == "dashboard")
    assert args[:2] == ["python", "-c"] and "http://127.0.0.1:8080/api/health" in args[2]


def test_doctor_dashboard_without_database_access_names_the_migration_and_the_role_password():
    line, _ = dashboard_line(ps_rows=FakeStack().ps_rows + [DASHBOARD_ROW], services=WITH_DASHBOARD,
                             dashboard_health='{"ok": true, "db": false}')
    assert line.status == "warn" and "migration 014" in line.detail and "DASHBOARD_DB_PASSWORD" in line.detail


def test_doctor_dashboard_health_that_does_not_answer_is_a_warning():
    for reply in (C.StackError("service \"dashboard\" is not running"), "<html>", '["db"]'):
        line, _ = dashboard_line(ps_rows=FakeStack().ps_rows + [DASHBOARD_ROW], services=WITH_DASHBOARD,
                                 dashboard_health=reply)
        assert line.status == "warn", reply


def test_doctor_dashboard_enabled_before_migration_014_says_to_migrate():
    files = sorted(p.stem for p in (REPO / "postgres" / "migrations").glob("*.sql"))
    before = ["000_baseline"] + [f for f in files if f != "014_dashboard"]
    for rows in (FakeStack().ps_rows, FakeStack().ps_rows + [DASHBOARD_ROW]):
        line, _ = dashboard_line(ps_rows=rows, services=WITH_DASHBOARD, migrations=before)
        assert line.status == "warn" and "migration 014 is not applied" in line.detail and "jobsearch migrate" in line.detail
    line, _ = dashboard_line(migrations=before)
    assert line.status == "ok" and "not enabled" in line.detail


def test_doctor_dashboard_enabled_but_stopped_says_how_to_start_it_and_where_the_logs_are():
    stopped = dict(DASHBOARD_ROW, State="exited", Status="Exited (1) 1 minute ago")
    for rows in (FakeStack().ps_rows, FakeStack().ps_rows + [stopped]):
        line, stack = dashboard_line(ps_rows=rows, services=WITH_DASHBOARD)
        assert line.status == "warn" and "up -d --build" in line.detail and "logs dashboard-db-init dashboard" in line.detail
        assert not [e for e in stack.exec_log if e[0] == "dashboard"]


def test_doctor_dashboard_published_beyond_localhost_is_a_failure():
    for url in ("0.0.0.0", "::", "10.0.0.5"):
        row = dict(DASHBOARD_ROW, Publishers=[{"URL": url, "TargetPort": 8080, "PublishedPort": 8080, "Protocol": "tcp"}])
        line, _ = dashboard_line(ps_rows=FakeStack().ps_rows + [row], services=WITH_DASHBOARD)
        assert line.status == "fail" and url in line.detail and "127.0.0.1" in line.detail
    unpublished = dict(DASHBOARD_ROW, Publishers=[{"URL": "", "TargetPort": 8080, "PublishedPort": 0, "Protocol": "tcp"}])
    for row in (unpublished, {k: v for k, v in DASHBOARD_ROW.items() if k != "Publishers"}):
        line, _ = dashboard_line(ps_rows=FakeStack().ps_rows + [row], services=WITH_DASHBOARD)
        assert line.status == "warn" and "no port is published" in line.detail


def test_doctor_dashboard_unknown_profiles_is_a_warning_not_a_failure():
    line, _ = dashboard_line(services=None)
    assert line.status == "warn" and "could not check" in line.detail


def test_stack_config_services_lists_the_active_profiles(monkeypatch):
    s = C.Stack(env={})
    seen = []

    class R:
        returncode = 0
        stderr = ""
        stdout = "postgres\nn8n\ndashboard\n"

    monkeypatch.setattr(s, "compose", lambda args, **k: seen.append(args) or R())
    assert s.config_services() == ["postgres", "n8n", "dashboard"]
    assert seen == [["config", "--services"]]


def test_print_checks_exit_code(capsys):
    assert C.print_checks([C.Check("a", "ok"), C.Check("b", "warn", "meh")]) == 0
    assert C.print_checks([C.Check("a", "fail", "boom")]) == 1
    assert "1 failure" in capsys.readouterr().out


# ------------------------------------------------------------------ status

def test_status_summary():
    out = C.run_status(FakeStack())
    assert "Jobs: 7 total, 3 first seen in the last 24h" in out
    assert "new 5" in out and "low 6" in out
    assert "#7" in out and "🔥" in out and "stale" in out and "medium-risk" in out
    assert "Applications: none yet" in out and "Apify credit: cheap $4.90" in out


def test_status_with_nothing_yet():
    out = C.run_status(FakeStack(jobs={"counts": [], "top": []}))
    assert "Jobs: 0 total" in out and "Top matches: none yet" in out


# ------------------------------------------------------------------ search profile SQL

SETTINGS = {"search_terms": ["backend engineer"], "locations": ["Pune"], "sources": ["indeed"], "allow_remote": True,
            "min_yoe": 0, "max_yoe": 5, "include_keywords": [], "exclude_keywords": ["intern"],
            "exclude_companies": [], "max_age_days": 7, "score_threshold": 70}


def test_search_sql_structure():
    sql = C.search_profile_sql(SETTINGS)
    assert "ON CONFLICT (name) DO UPDATE" in sql and "jsonb_array_elements_text(j->'search_terms')" in sql
    assert "'default'" in sql


def test_search_sql_payload_cannot_break_out():
    nasty = dict(SETTINGS, search_terms=["x'); DROP TABLE jobs; --", "$cfg$ evil $cfg$", "back\\slash"])
    sql = C.search_profile_sql(nasty)
    tags = {t for t in __import__("re").findall(r"\$(cfg[0-9a-f]{12})\$", sql)}
    assert len(tags) == 1  # one random tag opens and closes the literal
    tag = tags.pop()
    body = sql.split(f"${tag}$")[1]
    assert json.loads(body)["search_terms"][0] == "x'); DROP TABLE jobs; --"  # inert inside the literal


@pytest.mark.parametrize("name", ["bad name", "x'; --", "", "a" * 41, "ünï"])
def test_search_sql_rejects_bad_profile_names(name):
    with pytest.raises(ValueError):
        C.search_profile_sql(SETTINGS, name)


# ------------------------------------------------------------------ call_claude + check

class FakeResp(io.BytesIO):
    pass


def fake_opener(reply_text, capture=None):
    def opener(req, timeout=0):
        if capture is not None:
            capture["req"] = req
        return FakeResp(json.dumps({"content": [{"type": "text", "text": reply_text}]}).encode())
    return opener


GOOD_REPLY = json.dumps({
    "scores": {"technical": 80, "experience": 70, "behavioral": 60, "career": 90},
    "gates": {"eligibility": {"verdict": "PASS", "note": ""}, "language": {"verdict": "PASS", "note": ""},
              "location": {"verdict": "PASS", "note": ""}},
    "deadline": None, "posting_language": "English", "yoe": {"min": 3, "max": 6},
    "strengths": ["Python match"], "gaps": ["no Kafka"], "reason": "Strong match."})

CLEAN_TEXT = "Backend Engineer. Build APIs in Python and Postgres. 3-6 years. Hybrid in Pune."


def test_call_claude_builds_the_request():
    cap = {}
    out = C.call_claude("SYS", "USER", api_key="sk-test", model="m1", max_tokens=123, opener=fake_opener("hi", cap))
    req = cap["req"]
    assert out == "hi" and req.full_url == "https://api.anthropic.com/v1/messages"
    assert req.get_header("X-api-key") == "sk-test" and req.get_header("Anthropic-version") == "2023-06-01"
    body = json.loads(req.data)
    assert body["model"] == "m1" and body["max_tokens"] == 123 and body["system"] == "SYS"
    assert body["messages"] == [{"role": "user", "content": "USER"}]


def test_call_claude_http_error_becomes_check_error():
    def opener(req, timeout=0):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b'{"error":"invalid x-api-key"}'))
    with pytest.raises(C.CheckError, match="HTTP 401"):
        C.call_claude("s", "u", api_key="bad", model="m", opener=opener)


def test_check_without_scoring_is_local_only():
    res = C.check_posting(url="https://boards.greenhouse.io/acme/jobs/1", text=CLEAN_TEXT, company="Acme")
    assert res["safety"]["risk"] == "low" and res["score"] is None
    assert res["safety"]["hosts"]["url"]["category"] == "known_ats"


def test_check_flags_a_scam_without_any_model_call():
    res = C.check_posting(url="https://greenhouse.io@evil.com/x", text="Pay a refundable registration fee of Rs 500.")
    assert res["safety"]["risk"] == "high"


def test_check_refuses_to_score_high_risk_unless_forced():
    text = "Pay a refundable registration fee of Rs 500."
    with pytest.raises(C.CheckError, match="HIGH"):
        C.check_posting(text=text, score=True, api_key="k", profile_path=str(CONFIG / "profile.example.json"),
                        opener=fake_opener(GOOD_REPLY))
    res = C.check_posting(text=text, score=True, force=True, api_key="k",
                          profile_path=str(CONFIG / "profile.example.json"), opener=fake_opener(GOOD_REPLY))
    assert res["score"]["score"] == 78


def test_check_score_requires_key_text_and_profile(tmp_path):
    prof = str(CONFIG / "profile.example.json")
    with pytest.raises(C.CheckError, match="ANTHROPIC_API_KEY"):
        C.check_posting(text=CLEAN_TEXT, score=True, api_key=None, profile_path=prof)
    with pytest.raises(C.CheckError, match="posting text"):
        C.check_posting(url="https://x.com", text="", score=True, api_key="k", profile_path=prof)
    with pytest.raises(C.CheckError, match="profile problem"):
        C.check_posting(text=CLEAN_TEXT, score=True, api_key="k", profile_path=str(tmp_path / "none.json"))


def test_check_scores_through_the_validated_pipeline():
    cap = {}
    res = C.check_posting(text=CLEAN_TEXT, company="Acme", title="Backend Engineer", score=True, api_key="k",
                          profile_path=str(CONFIG / "profile.example.json"), opener=fake_opener(GOOD_REPLY, cap))
    assert res["score"]["verdict"] == "strong" and res["score"]["above_threshold"] is True
    sent = json.loads(cap["req"].data)
    assert "untrusted_job_posting_" in sent["messages"][0]["content"]  # posting is fenced
    assert "CANDIDATE PROFILE" in sent["system"]


def test_check_rejects_an_invalid_model_reply():
    with pytest.raises(C.CheckError, match="not valid"):
        C.check_posting(text=CLEAN_TEXT, score=True, api_key="k", profile_path=str(CONFIG / "profile.example.json"),
                        opener=fake_opener("I cannot do that"))


def test_render_check_mentions_flags_and_fit():
    res = C.check_posting(text=CLEAN_TEXT + " Contact hr@gmail.com", score=True, api_key="k",
                          profile_path=str(CONFIG / "profile.example.json"), opener=fake_opener(GOOD_REPLY))
    out = C.render_check(res)
    assert "Safety: MEDIUM" in out and "FREE_EMAIL_CONTACT" in out and "Fit: 78/100, strong" in out


# ------------------------------------------------------------------ validate + main

def test_validate_config_reports_each_file(tmp_path):
    for name in ("profile.example.json", "resume.example.json", "search.example.json"):
        shutil.copy(CONFIG / name, tmp_path / name.replace(".example", ""))
    lines, ok = C.validate_config(tmp_path)
    assert ok and all("valid" in l for l in lines)

    bad = json.loads((tmp_path / "resume.json").read_text())
    bad["basics"]["email"] = "nope"
    (tmp_path / "resume.json").write_text(json.dumps(bad))
    (tmp_path / "search.json").write_text("{not json")
    lines, ok = C.validate_config(tmp_path)
    text = "\n".join(lines)
    assert not ok and "basics.email" in text and "not valid JSON" in text


def test_validate_config_missing_files_are_warnings_not_failures(tmp_path):
    lines, ok = C.validate_config(tmp_path)
    assert ok and all("not found" in l for l in lines)


def test_main_validate_and_exit_codes(tmp_path, capsys):
    assert C.main(["validate", "--config-dir", str(tmp_path)], stack=FakeStack()) == 0
    (tmp_path / "profile.json").write_text("{}")
    assert C.main(["validate", "--config-dir", str(tmp_path)], stack=FakeStack()) == 1


def test_main_check_exit_codes_and_input(tmp_path, capsys):
    f = tmp_path / "jd.txt"
    f.write_text(CLEAN_TEXT)
    assert C.main(["check", "--text", str(f)], stack=FakeStack()) == 0
    f.write_text("Pay a refundable registration fee of Rs 500.")
    assert C.main(["check", "--text", str(f)], stack=FakeStack()) == 1
    assert C.main(["check", "--text", "-"], stack=FakeStack(), stdin=io.StringIO(CLEAN_TEXT)) == 0
    assert C.main(["check"], stack=FakeStack()) == 2
    out = capsys.readouterr()
    assert "HIGH" in out.out and "give --url" in out.err


def test_main_check_json_output(tmp_path, capsys):
    C.main(["check", "--url", "https://www.linkedin.com/jobs/view/1", "--json"], stack=FakeStack())
    data = json.loads(capsys.readouterr().out)
    assert data["safety"]["risk"] == "low"


def test_main_import_linkedin_and_conflicts(tmp_path, capsys):
    (tmp_path / "Positions.csv").write_text(
        "Company Name,Title,Description,Location,Started On,Finished On\nAcme,Engineer,,Pune,Jan 2024,\n", encoding="utf-8")
    out = tmp_path / "li.json"
    assert C.main(["import-linkedin", str(tmp_path), "-o", str(out)], stack=FakeStack()) == 0
    assert json.loads(out.read_text())["positions"][0]["start"] == "2024-01"

    resume = json.loads((CONFIG / "resume.example.json").read_text())
    resume["work"][0]["name"] = "Acme"
    resume["work"][0]["position"] = "Senior Engineer"
    resume["work"][0]["startDate"] = "2022-01"
    rp = tmp_path / "resume.json"
    rp.write_text(json.dumps(resume))
    capsys.readouterr()
    assert C.main(["conflicts", "--resume", str(rp), "--linkedin-json", str(out)], stack=FakeStack()) == 0
    text = capsys.readouterr().out
    assert "title_differs" in text and "start_differs" in text


def test_main_search_profile_apply_runs_sql(tmp_path):
    f = tmp_path / "search.json"
    f.write_text(json.dumps(SETTINGS))
    stack = FakeStack()
    assert C.main(["search-profile", "apply", "--file", str(f)], stack=stack) == 0
    assert any("INSERT INTO search_profile" in s for s in stack.sql_log)
    f.write_text(json.dumps(dict(SETTINGS, score_threshold=500)))
    assert C.main(["search-profile", "apply", "--file", str(f)], stack=stack) == 2


def test_main_reports_stack_errors_cleanly(capsys):
    class Down(FakeStack):
        def exec_out(self, service, args):
            raise C.StackError("postgres is not running")
    assert C.main(["migrate"], stack=Down()) == 2
    assert "postgres is not running" in capsys.readouterr().err


# ------------------------------------------------------------------ budget + credential checks

def test_doctor_reports_spend_and_credential():
    c = by_name(C.run_doctor(FakeStack(), REPO))
    assert c["spend limits"].status == "ok" and "$1.20 of $10.00" in c["spend limits"].detail
    assert c["n8n credential"].status == "ok"


def test_doctor_warns_when_spend_is_high_or_paused():
    hi = dict(FakeStack().budget, warn=True, month_spent=8.6)
    assert by_name(C.run_doctor(FakeStack(budget=hi), REPO))["spend limits"].status == "warn"
    paused = dict(FakeStack().budget, warn=True, blocked=True, blocked_reason="daily cap reached")
    d = by_name(C.run_doctor(FakeStack(budget=paused), REPO))["spend limits"]
    assert d.status == "warn" and "PAUSED" in d.detail and "daily cap reached" in d.detail


def test_doctor_catches_a_missing_or_misnamed_anthropic_credential():
    c = by_name(C.run_doctor(FakeStack(credentials=["Header Auth account"]), REPO))["n8n credential"]
    assert c.status == "warn" and "Anthropic API key" in c.detail
    c = by_name(C.run_doctor(FakeStack(credentials=["Anthropic API key"]), REPO))["n8n credential"]
    assert c.status == "ok" and "Telegram" in c.detail


def test_status_shows_spend_and_pause():
    assert "Claude spend: $1.20 of $10.00 this month ($0.10 of $1.50 today)" in C.run_status(FakeStack())
    paused = dict(FakeStack().budget, blocked=True, blocked_reason="monthly cap reached")
    assert "PAUSED: monthly cap reached" in C.run_status(FakeStack(budget=paused))


def test_budget_report():
    out = C.run_budget(FakeStack())
    assert "$1.20 of $10.00" in out and "STATUS: ok" in out and "claude-haiku-5-5" in out and "$  0.0187" in out
    assert "separate Apify caps" in out and "Apify spend this month:   $0.40 of $15.00" in out
    paused = dict(FakeStack().budget, blocked=True, blocked_reason="daily cap reached")
    assert "PAUSED, daily cap reached" in C.run_budget(FakeStack(budget=paused))
    assert "No usage logged" in C.run_budget(FakeStack(spend=[]))


@pytest.mark.parametrize("kwargs,expected", [
    ({"monthly": 10}, "monthly_cap_usd = 10.0"),
    ({"daily": 1.5}, "daily_cap_usd = 1.5"),
    ({"monthly": 10, "daily": 2, "warn_pct": 75}, "monthly_cap_usd = 10.0, daily_cap_usd = 2.0, warn_pct = 75"),
])
def test_budget_set_sql(kwargs, expected):
    sql = C.budget_set_sql(**kwargs)
    assert sql.startswith("UPDATE budget_settings SET " + expected) and sql.endswith("WHERE id = 1;")


@pytest.mark.parametrize("kwargs", [{}, {"monthly": -1}, {"monthly": 1e9}, {"daily": "ten"}, {"warn_pct": 0}, {"warn_pct": 101},
                                     {"warn_pct": 5.5}, {"monthly": True}])
def test_budget_set_rejects_bad_values(kwargs):
    with pytest.raises(ValueError):
        C.budget_set_sql(**kwargs)


def test_main_budget_show_and_set(capsys):
    stack = FakeStack()
    assert C.main(["budget"], stack=stack) == 0
    assert "Claude spend this month" in capsys.readouterr().out
    assert C.main(["budget", "set", "--monthly", "12", "--daily", "2"], stack=stack) == 0
    out = capsys.readouterr().out
    assert "budget updated" in out and any("monthly_cap_usd = 12.0" in q for q in stack.sql_log)
    assert C.main(["budget", "set"], stack=stack) == 2                     # nothing to set
    assert C.main(["budget", "set", "--monthly", "-5"], stack=stack) == 2   # invalid
    assert "give at least one" in capsys.readouterr().err


# ------------------------------------------------------------------ tailoring and review commands

def extract_call(code):
    """Pull (method, path, body) out of the generated in-container snippet."""
    import re
    m = re.search(r"body = (\".*\") if (True|False) else None", code)
    body = json.loads(json.loads(m.group(1))) if m and m.group(2) == "True" else None
    path = re.search(r"http://localhost:8000(/[^']*)'", code).group(1)
    method = re.search(r"method='([A-Z]+)'", code).group(1)
    return method, path, body


def test_the_generated_service_snippet_really_runs(monkeypatch):
    import urllib.request

    class Resp:
        status = 200
        def read(self): return b'{"ok": true}'
        def __enter__(self): return self
        def __exit__(self, *a): return False

    seen = {}

    def fake_urlopen(req, timeout=0):
        seen.update(url=req.full_url, method=req.get_method(), data=req.data, timeout=timeout)
        return Resp()
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    class Capture(FakeStack):
        def scraper_py(self, code, timeout=90):
            import contextlib, io
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                exec(code, {})
            return buf.getvalue()
    out = C._service_call(Capture(), "POST", "/tailor/run", {"job_id": 24, "limit": 3})
    assert out == (200, {"ok": True})
    assert seen["url"] == "http://localhost:8000/tailor/run" and seen["method"] == "POST" and json.loads(seen["data"]) == {"job_id": 24, "limit": 3}
    seen.clear()
    C._service_call(Capture(), "GET", "/tailor/settings")
    assert seen["method"] == "GET" and seen["data"] is None


def test_tailor_one_job_prints_the_outcome():
    reply = {"results": [{"job_id": 24, "status": "drafted", "variant": "tailored", "needs_attention": True,
                          "attention": ["the resume is 2 pages (limit 1)"], "cost_usd": 0.0481, "files_dir": "/output/applications/24-x",
                          "requirements": {"matched": 2, "bridged": 1, "gap": 3}}], "telegram": "sent", "notified": 1}
    stack = FakeStack(service_reply=reply)
    out = C.run_tailor(stack, 24, 3)
    assert "job 24: drafted (tailored), 2 matched / 1 bridged / 3 gaps, cost $0.048" in out and "needs attention" in out
    assert "the resume is 2 pages" in out and "files: /output/applications/24-x" in out and "1 review message(s) sent" in out
    method, path, body = extract_call(stack.py_calls[-1])
    assert (method, path, body) == ("POST", "/tailor/run", {"job_id": 24, "limit": 3})


def test_tailor_without_an_id_runs_the_next_batch_and_reports_skips():
    stack = FakeStack(service_reply={"skipped": "manual mode: nothing is tailored until you ask (/tailor N)", "results": []})
    assert "Nothing tailored: manual mode" in C.run_tailor(stack, None, 2)
    assert extract_call(stack.py_calls[-1])[2] == {"limit": 2}
    assert "No eligible jobs" in C.run_tailor(FakeStack(service_reply={"results": [], "telegram": "not configured"}), None, 3)


def test_tailor_failures_and_http_errors_are_readable():
    out = C.run_tailor(FakeStack(service_reply={"results": [{"job_id": 7, "status": "budget_paused", "reason": "not enough budget left"}]}), 7, 3)
    assert "job 7: budget_paused: not enough budget left" in out
    with pytest.raises(C.CheckError, match="job 99 is scam_blocked"):
        C.run_tailor(FakeStack(service_status=422, service_reply={"detail": {"errors": ["job 99 is scam_blocked (score 90); not tailored"]}}), 99, 3)


def test_review_queue_listing():
    out = C.run_review(FakeStack())
    assert "1 waiting for your decision" in out and "#24" in out and "Backend Engineer @ Acme" in out and "needs attention" in out
    assert "the resume is 2 pages (limit 1)" in out and "approve:  scripts/jobsearch approve N" in out
    assert C.run_review(FakeStack(queue=[])) == "Nothing is waiting for your review."


def test_review_sql_validates_and_quotes():
    assert C.review_sql(24, "approve", None) == "SELECT review_application(24, 'approve');"
    sql = C.review_sql(24, "skip", "it's \"odd\"; DROP TABLE jobs; --")
    import re
    tag = re.search(r"\$(n[0-9a-f]{12})\$", sql).group(1)
    assert sql.startswith("SELECT review_application(24, 'skip', $") and sql.count(f"${tag}$") == 2
    for bad in (0, -3, True, "24; DROP", 1.5):
        with pytest.raises(ValueError):
            C.review_sql(bad, "approve", None)
    with pytest.raises(ValueError):
        C.review_sql(24, "delete", None)


def test_approve_skip_applied_commands_run_the_sql(capsys):
    stack = FakeStack()
    for cmd in ("approve", "skip", "applied"):
        assert C.main([cmd, "24", "--note", "fine"], stack=stack) == 0
    assert [("approve" in q, "skip" in q, "applied" in q) for q in stack.sql_log if "review_application" in q] == [(True, False, False), (False, True, False), (False, False, True)]
    assert "job 24: approve recorded" in capsys.readouterr().out
    assert C.main(["approve", "0"], stack=stack) == 2


def test_files_maps_container_paths_to_the_host(monkeypatch):
    stack = FakeStack()
    out = C.run_files(stack, 24)
    assert f"{C.REPO}/output/applications/24-acme-backend" in out, "a local stack keeps its files in this checkout"
    assert "status: drafted" in out and "resume.pdf, cover_letter.pdf" in out and "scp" not in out
    remote = C.Stack(env={"JOBSEARCH_SSH": "ssh -i /k ubuntu@host"})
    remote.query_json = stack.query_json
    assert "scp -r ubuntu@host:~/jobsearch/output/applications/24-acme-backend ." in C.run_files(remote, 24)
    with pytest.raises(C.CheckError, match="no application yet"):
        C.run_files(FakeStack(apps=[]), 5)


SETTINGS_REPLY = {"mode": "auto", "approval": "review", "min_score": None, "max_score": 80, "daily_limit": None, "model": "claude-sonnet-5-5",
                  "cover_letter": True, "max_pages": 1, "verify": True, "verify_model": "claude-haiku-5-5"}


def test_tailor_settings_show():
    out = C.run_tailor_settings(FakeStack(service_reply=SETTINGS_REPLY))
    assert "mode:         auto" in out and "your search threshold" in out and "max_score:    80" in out and "fact check:   on (claude-haiku-5-5)" in out


def test_tailor_settings_set_translates_options(capsys):
    stack = FakeStack(service_reply=SETTINGS_REPLY)
    rc = C.main(["tailor-settings", "set", "--mode", "manual", "--approval", "auto", "--min-score", "65", "--max-score", "none",
                 "--daily-limit", "5", "--cover-letter", "off", "--max-pages", "2", "--verify", "off", "--model", "claude-haiku-5-5"], stack=stack)
    assert rc == 0
    method, path, body = extract_call(next(c for c in stack.py_calls if "'POST'" in c))
    assert (method, path) == ("POST", "/tailor/settings")
    assert body == {"mode": "manual", "approval": "auto", "min_score": 65, "daily_limit": 5, "cover_letter": False, "max_pages": 2,
                    "verify": False, "model": "claude-haiku-5-5", "clear": ["max_score"]}
    assert "tailoring settings updated" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [["tailor-settings", "set"], ["tailor-settings", "set", "--min-score", "lots"], ["tailor-settings", "set", "--daily-limit", "-3"]])
def test_tailor_settings_set_rejects_bad_input(argv, capsys):
    assert C.main(argv, stack=FakeStack(service_reply=SETTINGS_REPLY)) == 2
    assert capsys.readouterr().err.startswith("error:")


def test_server_side_rejection_is_reported(capsys):
    stack = FakeStack(service_status=422, service_reply={"detail": {"errors": ["mode must be auto or manual"]}})
    assert C.main(["tailor-settings", "set", "--mode", "auto"], stack=stack) == 2
    assert "mode must be auto or manual" in capsys.readouterr().err


# ------------------------------------------------------------------ optional parts (features), browsing, manual stages

def test_features_show_and_set(capsys):
    st = FakeStack()
    assert C.main(["features"], stack=st) == 0
    out = capsys.readouterr().out
    assert "scoring:" in out and "tailoring:" in out
    assert C.main(["features", "set", "--tailoring", "off", "--telegram", "off"], stack=st) == 0
    method, path, body = extract_call(next(c for c in st.py_calls if "'POST'" in c))
    assert (method, path) == ("POST", "/features") and body == {"tailoring": False, "telegram": False}, "unspecified parts are left alone"


def test_features_set_needs_a_choice_and_reports_server_errors(capsys):
    assert C.main(["features", "set"], stack=FakeStack()) == 2
    assert "at least one" in capsys.readouterr().err
    assert C.main(["features", "set"], stack=FakeStack()) == 2 and "--gmail" in capsys.readouterr().err, "the message names every switch"
    st = FakeStack(service_status=503, features={"detail": {"errors": ["database down"]}})
    assert C.main(["features"], stack=st) == 2
    assert "database down" in capsys.readouterr().err


def test_doctor_lists_optional_parts_and_stops_asking_for_a_resume_when_tailoring_is_off():
    off = {"flags": {}, "approval": "auto", "approval_effective": "auto", "lines": [],
           "effective": {"tailoring": {"on": False, "reason": "switched off"}, "telegram": {"on": False, "reason": "not configured"},
                         "digest": {"on": False, "reason": "needs Telegram"}}}
    checks = by_name(C.run_doctor(FakeStack(features=off, config={"profile": "ok", "resume": "missing"}), REPO))
    assert checks["optional parts"].status == "ok" and "tailoring off" in checks["optional parts"].detail
    assert checks["config/resume.json"].status == "ok" and "not needed" in checks["config/resume.json"].detail
    on = by_name(C.run_doctor(FakeStack(config={"profile": "ok", "resume": "missing"}), REPO))
    assert on["config/resume.json"].status == "warn", "with tailoring on a missing resume is still a warning"


def test_doctor_does_not_nag_about_a_missing_telegram_credential():
    checks = by_name(C.run_doctor(FakeStack(credentials=["Anthropic API key"]), REPO))
    assert checks["n8n credential"].status == "ok" and "fine if you do not use Telegram" in checks["n8n credential"].detail


def test_status_shows_the_optional_parts():
    assert "Optional parts: tailoring on, telegram on, digest on" in C.run_status(FakeStack())


def test_jobs_sql_defaults_filters_and_validation():
    sql = C.jobs_sql()
    assert "j.status = 'shortlisted'" in sql and "LIMIT 25" in sql and "ORDER BY j.score DESC NULLS LAST" in sql
    assert "j.status =" not in C.jobs_sql(status="all"), "status=all adds no status condition"
    full = C.jobs_sql(status="applied", min_score=70, days=7, limit=5)
    assert "j.status = 'applied'" in full and "j.score >= 70" in full and "interval '7 days'" in full and "LIMIT 5" in full
    for bad in (dict(status="x'; drop table jobs;--"), dict(min_score=101), dict(min_score=-1), dict(days=0), dict(limit=0), dict(limit=10001)):
        with pytest.raises(ValueError):
            C.jobs_sql(**bad)


def test_jobs_sql_quotes_hostile_search_text():
    evil = "x'); DROP TABLE jobs; --"
    sql = C.jobs_sql(search=evil)
    assert sql.count(evil) == 2 and re.search(r"\$q[0-9a-f]{12}\$" + re.escape(evil) + r"\$q[0-9a-f]{12}\$", sql)
    tricky = "has $q1$ inside"
    assert tricky in C.jobs_sql(search=tricky)


def test_jobs_listing_and_empty_message():
    out = C.run_jobs(FakeStack())
    assert "#7" in out and "Backend Engineer | Acme | Pune" in out and "https://acme.com/7" in out
    assert "medium risk" in out and "[applied]" in out and "mark N interview" in out
    assert "No jobs match" in C.run_jobs(FakeStack(job_rows=[]))


def test_job_detail_with_and_without_a_decision():
    out = C.run_job(FakeStack(), 7)
    assert "Backend Engineer @ Acme" in out and "score 82 strong" in out and "free_email" in out
    assert "eligibility UNKNOWN: visa unclear" in out and "+ Python" in out and "- No Kafka" in out and "decision:  none yet" in out
    decided = FakeStack(job_detail=[dict(FakeStack().job_detail[0], application="interview", variant="base", applied_on="2026-10-11",
                                         note="phone screen")])
    assert "decision:  interview (base resume), applied 2026-10-11; note: phone screen" in C.run_job(decided, 7)
    with pytest.raises(C.CheckError):
        C.run_job(FakeStack(job_detail=[]), 99999)
    with pytest.raises(ValueError):
        C.run_job(FakeStack(), 0)


def test_mark_sql_validates_and_quotes():
    assert C.mark_sql(24, "interview", None) == "SELECT set_application_status(24, 'interview');"
    assert C.mark_sql(24, "In-Progress", None).startswith("SELECT set_application_status(24, 'in_progress'")
    sql = C.mark_sql(24, "offer", "it's \"great\"; DROP TABLE jobs")
    assert "DROP TABLE jobs" in sql and sql.count("$q") >= 2
    for args in ((0, "offer", None), (24, "hired", None), (True, "offer", None)):
        with pytest.raises(ValueError):
            C.mark_sql(*args)


def test_mark_command_runs_the_sql_and_rejects_unknown_stages(capsys):
    st = FakeStack()
    assert C.main(["mark", "24", "rejected", "--note", "no thanks"], stack=st) == 0
    assert "set_application_status(24, 'rejected'" in st.sql_log[-1] and "no thanks" in st.sql_log[-1]
    assert "marked rejected" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        C.main(["mark", "24", "hired"], stack=st)


def test_export_csv_neutralises_spreadsheet_formulas(tmp_path, capsys):
    out = C.export_csv(FakeStack())
    lines = out.splitlines()
    assert lines[0].startswith("id,score,verdict,status,application,title,company")
    assert "'=HYPERLINK" in out and "\n=" not in out and ",=HYPERLINK" not in out
    target = tmp_path / "jobs.csv"
    assert C.main(["export", "-o", str(target), "--status", "all"], stack=FakeStack()) == 0
    assert target.read_text().splitlines()[0].startswith("id,score") and "wrote" in capsys.readouterr().out


def test_files_for_a_hand_recorded_decision_explains_there_are_no_files():
    st = FakeStack(apps=[{"files_dir": None, "status": "applied", "needs_attention": False, "attention_reason": None}])
    with pytest.raises(C.CheckError, match="no files"):
        C.run_files(st, 7)


def test_tailor_settings_accepts_the_cli_only_approval(capsys):
    st = FakeStack(service_reply=SETTINGS_REPLY)
    assert C.main(["tailor-settings", "set", "--approval", "cli"], stack=st) == 0
    method, path, body = extract_call(next(c for c in st.py_calls if "'POST'" in c))
    assert (method, path, body) == ("POST", "/tailor/settings", {"approval": "cli", "clear": []})
    with pytest.raises(SystemExit):
        C.main(["tailor-settings", "set", "--approval", "maybe"], stack=st)


# ------------------------------------------------------------------ schedules

def _post(st):
    return extract_call(next(c for c in st.py_calls if "'POST'" in c))


def test_schedule_show(capsys):
    assert C.main(["schedule"], stack=FakeStack()) == 0
    out = capsys.readouterr().out
    assert "collect  on  every 6 hours" in out and "times are in Asia/Kolkata" in out and "schedule run NAME" in out


def test_schedule_set_translates_friendly_values(capsys):
    st = FakeStack()
    assert C.main(["schedule", "set", "collect", "--every", "6h", "--batch", "18"], stack=st) == 0
    assert _post(st) == ("POST", "/schedule/collect", {"every_minutes": 360, "batch": 18})
    assert "applies from the next scheduler tick" in capsys.readouterr().out
    st = FakeStack()
    assert C.main(["schedule", "set", "digest", "--at", "7:30", "--off"], stack=st) == 0
    assert _post(st) == ("POST", "/schedule/digest", {"at_time": "07:30", "enabled": False})
    st = FakeStack()
    assert C.main(["schedule", "set", "digest", "--every", "6h"], stack=st) == 0
    assert _post(st) == ("POST", "/schedule/digest", {"every_minutes": 360}), "the service clears the daily time itself"
    st = FakeStack()
    assert C.main(["schedule", "set", "score", "--every", "90m", "--model", "claude-haiku-5-5", "--on"], stack=st) == 0
    assert _post(st)[2] == {"every_minutes": 90, "model": "claude-haiku-5-5", "enabled": True}


@pytest.mark.parametrize("argv,message", [
    (["schedule", "set"], "name the schedule"),
    (["schedule", "set", "collect"], "give something to change"),
    (["schedule", "set", "mystery", "--every", "1h"], "name the schedule"),
    (["schedule", "set", "collect", "--every", "10m"], "15 minutes"),
    (["schedule", "set", "collect", "--every", "soon"], "not a duration"),
    (["schedule", "set", "digest", "--every", "30m"], "1 hour"),
    (["schedule", "set", "score", "--at", "08:00"], "not at a time of day"),
    (["schedule", "set", "digest", "--at", "25:00"], "not a time"),
    (["schedule", "set", "collect", "--batch", "0"], "from 1 to 50"),
    (["schedule", "set", "collect", "--model", "claude-haiku-5-5"], "only the score schedule"),
    (["schedule", "run"], "name the schedule"),
    (["schedule", "timezone"], "give a timezone")])
def test_schedule_rejects_bad_input_before_calling_the_service(argv, message, capsys):
    st = FakeStack()
    assert C.main(argv, stack=st) == 2
    assert message in capsys.readouterr().err and not any("'POST'" in c for c in getattr(st, "py_calls", []))


def test_schedule_run_and_timezone(capsys):
    st = FakeStack()
    assert C.main(["schedule", "run", "collect"], stack=st) == 0
    assert _post(st)[:2] == ("POST", "/schedule/collect/run") and "within 5 minutes" in capsys.readouterr().out
    st = FakeStack()
    assert C.main(["schedule", "timezone", "Europe/London"], stack=st) == 0
    assert _post(st) == ("POST", "/schedule/timezone", {"timezone": "Europe/London"})
    assert "spending-cap day" in capsys.readouterr().out


def test_schedule_server_errors_are_readable(capsys):
    st = FakeStack(service_status=422, schedule={"detail": {"errors": ["unknown timezone 'Mars/Base'"]}})
    assert C.main(["schedule", "timezone", "Mars/Base"], stack=st) == 2
    assert "unknown timezone" in capsys.readouterr().err


def test_doctor_shows_the_schedule_and_checks_the_scheduler_is_published():
    ok = by_name(C.run_doctor(FakeStack(), REPO))
    assert ok["schedule"].status == "ok" and "collect every 6 hours" in ok["schedule"].detail and "tailor every 15 minutes (off)" in ok["schedule"].detail
    assert "digest daily 08:00" in ok["schedule"].detail and "Asia/Kolkata" in ok["schedule"].detail
    assert ok["scheduler workflow"].status == "ok"
    bad = by_name(C.run_doctor(FakeStack(published=["js-collect"]), REPO))
    assert bad["scheduler workflow"].status == "warn" and "n8n-import-workflows.sh --publish" in bad["scheduler workflow"].detail


def test_tailor_without_a_limit_uses_the_schedule_batch():
    st = FakeStack(service_reply={"results": [], "claimed": 0, "telegram": "off"})
    C.main(["tailor"], stack=st)
    assert _post(st) == ("POST", "/tailor/run", {}), "no limit sent: the service uses the tailor schedule's batch"
    st = FakeStack(service_reply={"results": [], "claimed": 0, "telegram": "off"})
    C.main(["tailor", "--limit", "2"], stack=st)
    assert _post(st)[2] == {"limit": 2}


def test_tailor_settings_paper_and_style_notes(capsys):
    st = FakeStack(service_reply=SETTINGS_REPLY)
    assert C.main(["tailor-settings", "set", "--paper", "letter", "--style-notes", "  Warm   but formal. "], stack=st) == 0
    assert _post(st)[2] == {"paper": "letter", "style_notes": "Warm but formal.", "clear": []}
    st = FakeStack(service_reply=SETTINGS_REPLY)
    assert C.main(["tailor-settings", "set", "--style-notes", "none"], stack=st) == 0
    assert _post(st)[2] == {"clear": ["style_notes"]}
    assert C.main(["tailor-settings", "set", "--style-notes", "x" * 401], stack=FakeStack(service_reply=SETTINGS_REPLY)) == 2
    assert "400 characters" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        C.main(["tailor-settings", "set", "--paper", "a5"], stack=FakeStack())


# ------------------------------------------------------------------ runaway guards

def test_apify_caps_in_budget_set():
    sql = C.budget_set_sql(apify_monthly=12, apify_daily=0.4)
    assert "apify_monthly_cap_usd = 12.0" in sql and "apify_daily_cap_usd = 0.4" in sql
    for bad in (dict(apify_monthly=-1), dict(apify_daily=True), dict(apify_monthly="lots")):
        with pytest.raises(ValueError):
            C.budget_set_sql(**bad)
    with pytest.raises(ValueError, match="--apify-monthly"):
        C.budget_set_sql()


def test_budget_set_command_passes_the_apify_caps(capsys):
    st = FakeStack()
    assert C.main(["budget", "set", "--apify-daily", "0.25"], stack=st) == 0
    assert "apify_daily_cap_usd = 0.25" in st.sql_log[0]


def test_a_paused_apify_budget_is_stated_plainly():
    paused = {"month_spent": 15, "day_spent": 0.5, "monthly_cap": 15, "daily_cap": 0.5, "month_remaining": 0, "day_remaining": 0,
              "blocked": True, "blocked_reason": "Apify monthly cap reached"}
    out = C.run_budget(FakeStack(apify_budget=paused))
    assert "PAUSED: Apify monthly cap reached" in out and "skipped" in out


def test_schedule_pause_and_resume(capsys):
    st = FakeStack()
    assert C.main(["schedule", "pause", "--reason", "checking the bill"], stack=st) == 0
    assert _post(st) == ("POST", "/schedule/pause", {"reason": "checking the bill"})
    assert "ALL SCHEDULES PAUSED" in capsys.readouterr().out
    st = FakeStack()
    assert C.main(["schedule", "pause"], stack=st) == 0 and _post(st)[2] == {"reason": None}
    st = FakeStack()
    assert C.main(["schedule", "resume"], stack=st) == 0
    assert _post(st)[:2] == ("POST", "/schedule/resume") and "resumed" in capsys.readouterr().out


def test_parked_listing_and_requeue(capsys):
    assert "No parked jobs" in C.run_parked(FakeStack())
    parked = [{"stage": "score", "job_id": 9, "title": "Engineer", "company": "Acme", "claims": 6, "strikes": 0}]
    out = C.run_parked(FakeStack(parked=parked))
    assert "1 parked job" in out and "score" in out and "#9" in out and "requeue" in out
    assert C.requeue_sql("score", None) == "SELECT requeue_jobs('score') AS n;"
    assert C.requeue_sql("tailor", 24) == "SELECT requeue_jobs('tailor', 24) AS n;"
    for bad in (("mystery", None), ("score", 0), ("score", True)):
        with pytest.raises(ValueError):
            C.requeue_sql(*bad)
    st = FakeStack()
    assert C.main(["requeue", "score", "9"], stack=st) == 0 and "requeue_jobs('score', 9)" in st.sql_log[-1]
    assert "3 job(s) put back" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        C.main(["requeue", "mystery"], stack=FakeStack())


def test_reshortlist_lists_first_and_only_changes_things_with_apply(capsys):
    assert "Nothing to do" in C.run_reshortlist(FakeStack())
    rows = [{"id": 54, "score": 69, "title": "AI Agent Developer", "company": "Skysecure"},
            {"id": 238, "score": 60, "title": "Backend Engineer", "company": "Wise"}]
    st = FakeStack(reshortlist=rows)
    out = C.run_reshortlist(st)
    assert "2 job(s)" in out and "#54" in out and "69" in out and "Wise" in out and "--apply" in out
    assert not any("reshortlist()" in q for q in st.sql_log), "listing never changes anything"
    assert C.main(["reshortlist"], stack=st) == 0
    assert not any("reshortlist()" in q for q in st.sql_log)
    assert C.main(["reshortlist", "--apply"], stack=st) == 0
    assert any("SELECT reshortlist()" in q for q in st.sql_log)
    assert "2 job(s) shortlisted" in capsys.readouterr().out


def test_doctor_reports_parked_jobs_apify_limits_and_a_stalled_scorer():
    ok = by_name(C.run_doctor(FakeStack(), REPO))
    assert ok["parked jobs"].status == "ok" and ok["apify spend limits"].status == "ok" and ok["scoring progress"].status == "ok"
    parked = [{"stage": "score", "job_id": 9, "title": "E", "company": "A", "claims": 6, "strikes": 0}]
    bad = by_name(C.run_doctor(FakeStack(parked=parked), REPO))
    assert bad["parked jobs"].status == "warn" and "1 score" in bad["parked jobs"].detail and "requeue" in bad["parked jobs"].detail
    blocked = dict(FakeStack().apify_budget, blocked=True, blocked_reason="Apify daily cap reached")
    assert by_name(C.run_doctor(FakeStack(apify_budget=blocked), REPO))["apify spend limits"].status == "warn"
    stalled = by_name(C.run_doctor(FakeStack(scoring={"waiting": 40, "hours_since_score": 9.0}), REPO))
    assert stalled["scoring progress"].status == "warn" and "40 jobs" in stalled["scoring progress"].detail
    quiet = by_name(C.run_doctor(FakeStack(scoring={"waiting": 3, "hours_since_score": 9.0}), REPO))
    assert quiet["scoring progress"].status == "ok", "a few waiting jobs is not a stall"


def test_doctor_warns_when_everything_is_paused():
    paused = dict(FakeStack().schedule_body, paused=True, paused_reason="bill check")
    checks = by_name(C.run_doctor(FakeStack(schedule=paused), REPO))
    assert checks["schedules paused"].status == "warn" and "bill check" in checks["schedules paused"].detail and "resume" in checks["schedules paused"].detail
    assert "schedules paused" not in by_name(C.run_doctor(FakeStack(), REPO))


def test_status_shows_apify_spend_and_parked_jobs():
    parked = [{"stage": "score", "job_id": 9, "title": "E", "company": "A", "claims": 6, "strikes": 0}]
    out = C.run_status(FakeStack(parked=parked))
    assert "Apify spend: $0.40 of $15.00 this month" in out and "1 parked job(s)" in out
    assert "parked" not in C.run_status(FakeStack())


# ------------------------------------------------------------------ search profile SQL: boards (Phase 7)

def _validated(**over):
    import profile_tools
    s = json.loads((CONFIG / "search.example.json").read_text(encoding="utf-8"))
    s.update(over)
    return profile_tools.validate_search(s)


def test_old_example_config_still_applies_with_empty_board_defaults():
    settings = _validated()
    assert settings["boards"] == {} and settings["board_title_words"] == []
    sql = C.search_profile_sql(settings)
    assert "coalesce(j->'boards', '{}'::jsonb)" in sql
    assert "jsonb_array_elements_text(j->'board_title_words')" in sql


def test_search_sql_writes_both_new_columns_on_insert_and_update():
    sql = C.search_profile_sql(_validated(boards={"greenhouse": ["stripe"]}, board_title_words=["sde"]))
    insert_cols = sql.split("INSERT INTO search_profile (")[1].split(")")[0]
    assert "boards" in insert_cols and "board_title_words" in insert_cols
    update = sql.split("DO UPDATE SET")[1]
    assert "boards = EXCLUDED.boards" in update and "board_title_words = EXCLUDED.board_title_words" in update
    assert re.search(r"\$(cfg[0-9a-f]{12})\$", sql)                  # random-tag quoting still there


def test_search_sql_old_settings_without_the_new_keys_still_work():
    sql = C.search_profile_sql(SETTINGS)                              # a dict from before Phase 7
    assert "coalesce(j->'boards', '{}'::jsonb)" in sql


def test_search_sql_board_name_cannot_break_out_of_the_literal(monkeypatch):
    nasty = _validated(boards={"ashby": [{"slug": "acme", "name": "Acme $cfg1$ '); DROP TABLE jobs; -- $"}]},
                       board_title_words=["$cfg1$"])
    sql = C.search_profile_sql(nasty)
    tags = set(re.findall(r"\$(cfg[0-9a-f]{12})\$", sql))
    assert len(tags) == 1
    tag = tags.pop()
    parts = sql.split(f"${tag}$")
    assert len(parts) == 3                                            # opens once, closes once
    assert f"${tag}$" not in parts[1]
    assert json.loads(parts[1])["boards"]["ashby"][0]["name"].startswith("Acme $cfg1$")


def test_search_sql_picks_a_new_tag_when_boards_content_contains_the_first_one(monkeypatch):
    calls = iter(["aaaaaaaaaaaa", "bbbbbbbbbbbb"])
    monkeypatch.setattr(C.secrets, "token_hex", lambda n: next(calls))
    settings = _validated(boards={"ashby": [{"slug": "acme", "name": "x $cfgaaaaaaaaaaaa$ y"}]})
    sql = C.search_profile_sql(settings)
    assert "$cfgbbbbbbbbbbbb$" in sql and sql.count("$cfgaaaaaaaaaaaa$") == 1  # only inside the payload


# ================================================================== Phase 7: sources, boards find, doctor

import sources as SRC  # noqa: E402  (scraper/ is on sys.path through jobsearch_cli)

JOBSPY_GROUP = list(SRC.GROUPS["jobspy"])
ALL_GROUPS = [s for s in SRC.ALL if s in SRC.JOBSPY + SRC.APIFY + SRC.BOARD_SYSTEMS + SRC.FEEDS]   # "everything" the four groups name


def search_cfg(**over):
    """A valid config/search.json as a dict (with a _comment key, like config/search.example.json)."""
    cfg = {"_comment": "keep me", "search_terms": ["backend engineer"], "locations": ["Zürich", "Remote"],
           "sources": ["indeed", "naukri", "linkedin"], "allow_remote": True, "min_yoe": 0, "max_yoe": 5,
           "include_keywords": [], "exclude_keywords": ["intern"], "exclude_companies": [], "max_age_days": 7,
           "score_threshold": 70}
    cfg.update(over)
    return cfg


def make_repo(tmp_path, cfg=None, raw=None):
    """A throwaway repo: config/search.json (a dict, or raw text) and a copy of the real migrations folder."""
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    if cfg is not None or raw is not None:
        text = raw if raw is not None else json.dumps(cfg, indent=2, ensure_ascii=False) + "\n"
        (tmp_path / "config" / "search.json").write_text(text, encoding="utf-8")
    if not (tmp_path / "postgres").exists():
        shutil.copytree(REPO / "postgres" / "migrations", tmp_path / "postgres" / "migrations")
    return tmp_path


def cfg_path(repo):
    return repo / "config" / "search.json"


def applied_sources(sql_log):
    """The `sources` array inside the single search_profile statement the CLI sent to the database."""
    stmts = [q for q in sql_log if "INSERT INTO search_profile" in q]
    assert len(stmts) == 1, f"expected one search_profile statement, got {len(stmts)}"
    m = re.search(r"\$(cfg[0-9a-f]{12})\$(.*?)\$\1\$", stmts[0], re.S)
    return json.loads(m.group(2))["sources"]


# ------------------------------------------------------------------ change_sources (pure)

def test_change_sources_off_boards_keeps_the_rest():
    cur = ["indeed", "linkedin", "greenhouse", "lever", "remotive"]
    assert C.change_sources(cur, "off", ["boards"]) == ["indeed", "linkedin", "remotive"]


def test_change_sources_only_apify():
    assert C.change_sources(list(SRC.ALL), "only", ["apify"]) == ["linkedin_apify"]


def test_change_sources_on_adds_without_duplicates():
    out = C.change_sources(["remotive", "indeed"], "on", ["feeds", "indeed", "remotive"])
    assert out == ["indeed"] + list(SRC.FEEDS)                      # no duplicates, ordered by sources.ALL, not by typing order
    assert C.change_sources(["indeed"], "on", ["indeed"]) == ["indeed"]


def test_change_sources_result_follows_the_registry_order():
    assert C.change_sources([], "on", ["remotive", "greenhouse", "linkedin_apify", "indeed"]) == \
        ["indeed", "linkedin_apify", "greenhouse", "remotive"]


def test_change_sources_refuses_zero_sources():
    with pytest.raises(ValueError, match=r"at least one source must stay on \(a collect run with no sources does nothing\)"):
        C.change_sources(["indeed"], "off", ["indeed"])
    with pytest.raises(ValueError, match="at least one source must stay on"):
        C.change_sources(list(SRC.ALL), "off", ["jobspy", "apify", "boards", "feeds", "glassdoor", "ziprecruiter", "google"])


def test_change_sources_unknown_name_lists_valid_choices():
    with pytest.raises(ValueError) as e:
        C.change_sources(["indeed"], "on", ["remotiv"])
    msg = str(e.value)
    assert "remotiv" in msg and "remotive" in msg and "greenhouse" in msg and "boards" in msg and "feeds" in msg and "apify" in msg


def test_change_sources_off_a_source_that_is_already_off_is_a_noop():
    assert C.change_sources(["indeed", "linkedin"], "off", ["remotive"]) == ["indeed", "linkedin"]
    assert C.change_sources(["indeed"], "off", ["boards", "feeds"]) == ["indeed"]
    with pytest.raises(ValueError, match="at least one source must stay on"):        # ...but never into an empty list
        C.change_sources(["remotive"], "off", ["feeds"])


def test_names_are_case_and_space_insensitive():
    assert C.change_sources(["indeed"], "on", ["Boards", " Feeds ", "LINKEDIN_APIFY"]) == \
        ["indeed", "linkedin_apify"] + list(SRC.BOARD_SYSTEMS) + list(SRC.FEEDS)
    assert C.change_sources(list(SRC.ALL), "only", ["  Apify"]) == ["linkedin_apify"]


@pytest.mark.parametrize("action", ["on", "off", "only"])
def test_a_missing_name_is_refused_with_the_choices(action):
    for names in ([], ["", "  "]):
        with pytest.raises(ValueError, match=r"name at least one source or group \(boards, feeds, jobspy, apify\)"):
            C.change_sources(["indeed", "remotive"], action, names)


def test_only_with_no_names_is_refused():
    with pytest.raises(ValueError, match="name at least one source or group"):
        C.change_sources(["indeed"], "only", [])


def test_change_sources_rejects_an_unknown_action():
    with pytest.raises(ValueError, match="on, off or only"):
        C.change_sources(["indeed"], "flip", ["feeds"])


# ------------------------------------------------------------------ the combinations the user asked for, through main

def _run(tmp_path, monkeypatch, start, argv, **stack_kw):
    repo = make_repo(tmp_path, search_cfg(sources=start))
    monkeypatch.setattr(C, "REPO", repo)
    st = FakeStack(**stack_kw)
    code = C.main(argv, stack=st)
    return repo, st, code


def test_only_apify(tmp_path, monkeypatch, capsys):
    repo, st, code = _run(tmp_path, monkeypatch, ALL_GROUPS, ["sources", "only", "apify"])
    assert code == 0
    assert json.loads(cfg_path(repo).read_text(encoding="utf-8"))["sources"] == ["linkedin_apify"]
    assert applied_sources(st.sql_log) == ["linkedin_apify"]
    assert "takes effect on the next collect run" in capsys.readouterr().out


def test_apify_plus_feeds_without_boards(tmp_path, monkeypatch):
    expected = ["linkedin_apify"] + list(SRC.FEEDS)
    repo, st, code = _run(tmp_path, monkeypatch, ALL_GROUPS, ["sources", "only", "apify", "feeds"])
    assert code == 0 and json.loads(cfg_path(repo).read_text(encoding="utf-8"))["sources"] == expected
    assert applied_sources(st.sql_log) == expected
    # the same result reached the other way round: everything on, then the boards and the free scrapers off
    repo2, st2, code2 = _run(tmp_path / "b", monkeypatch, ALL_GROUPS, ["sources", "off", "boards", "jobspy"])
    assert code2 == 0 and applied_sources(st2.sql_log) == expected


def test_everything_except_boards(tmp_path, monkeypatch):
    expected = [s for s in ALL_GROUPS if s not in SRC.BOARD_SYSTEMS]
    repo, st, code = _run(tmp_path, monkeypatch, ALL_GROUPS, ["sources", "off", "boards"])
    assert code == 0 and json.loads(cfg_path(repo).read_text(encoding="utf-8"))["sources"] == expected
    assert applied_sources(st.sql_log) == expected
    assert not set(expected) & set(SRC.BOARD_SYSTEMS) and set(SRC.FEEDS) <= set(expected) and "linkedin_apify" in expected


def test_all_on(tmp_path, monkeypatch):
    repo, st, code = _run(tmp_path, monkeypatch, ["indeed"], ["sources", "on", "jobspy", "apify", "boards", "feeds"])
    assert code == 0 and json.loads(cfg_path(repo).read_text(encoding="utf-8"))["sources"] == ALL_GROUPS
    assert applied_sources(st.sql_log) == ALL_GROUPS
    # and naming every single source (the three legacy ones are not in any group) gives the whole registry
    repo2, st2, code2 = _run(tmp_path / "all", monkeypatch, ["indeed"], ["sources", "on"] + list(SRC.ALL))
    assert code2 == 0 and applied_sources(st2.sql_log) == list(SRC.ALL)


def test_sources_command_accepts_mixed_case_names_and_any_action_case(tmp_path, monkeypatch):
    repo, st, code = _run(tmp_path, monkeypatch, ["indeed"], ["sources", "ON", "Boards", " Feeds "])
    assert code == 0 and applied_sources(st.sql_log) == ["indeed"] + list(SRC.BOARD_SYSTEMS) + list(SRC.FEEDS)


# ------------------------------------------------------------------ run_sources

def test_run_sources_writes_file_and_database_together(tmp_path):
    repo = make_repo(tmp_path, search_cfg(boards={"greenhouse": ["stripe"]}))
    st = FakeStack()
    out = C.run_sources(st, repo, "on", ["remotive"])
    on_disk = json.loads(cfg_path(repo).read_text(encoding="utf-8"))
    assert on_disk["sources"] == ["indeed", "naukri", "linkedin", "remotive"] == applied_sources(st.sql_log)
    assert on_disk["_comment"] == "keep me" and on_disk["boards"] == {"greenhouse": ["stripe"]}
    assert "remotive" in out and "on   " in out
    assert [p.name for p in (repo / "config").iterdir()] == ["search.json"], "no temp file is left behind"


def test_run_sources_missing_config_explains_setup(tmp_path):
    repo = make_repo(tmp_path)
    st = FakeStack()
    with pytest.raises(C.CheckError, match="/setup"):
        C.run_sources(st, repo, "on", ["feeds"])
    with pytest.raises(C.CheckError, match="/setup"):
        C.run_sources(st, repo)                                   # show needs the file too
    assert not any("INSERT" in q for q in st.sql_log)


def test_run_sources_invalid_json_is_a_clear_error(tmp_path):
    repo = make_repo(tmp_path, raw="{ not json")
    before = cfg_path(repo).read_bytes()
    with pytest.raises(C.CheckError, match="not valid JSON"):
        C.run_sources(FakeStack(), repo, "on", ["feeds"])
    assert cfg_path(repo).read_bytes() == before


def test_run_sources_show_changes_nothing(tmp_path):
    repo = make_repo(tmp_path, search_cfg(boards={"lever": ["spotify"]}, sources=["indeed", "lever"]))
    before = cfg_path(repo).read_bytes()
    st = FakeStack(sources_counts={"indeed": 12, "lever": 40})
    out = C.run_sources(st, repo)
    assert cfg_path(repo).read_bytes() == before
    assert not any("INSERT" in q or "UPDATE" in q for q in st.sql_log)
    assert "12 jobs" in out and "(1 company)" in out and "boards per run: 30" in out
    assert [p.name for p in (repo / "config").iterdir()] == ["search.json"]


def test_off_everything_is_refused_and_writes_nothing(tmp_path):
    repo = make_repo(tmp_path, search_cfg(sources=["indeed"]))
    before = cfg_path(repo).read_bytes()
    st = FakeStack()
    with pytest.raises(ValueError, match="at least one source must stay on"):
        C.run_sources(st, repo, "off", ["indeed"])
    assert cfg_path(repo).read_bytes() == before
    assert st.sql_log == []
    assert [p.name for p in (repo / "config").iterdir()] == ["search.json"]


def test_off_everything_through_main_prints_the_error(tmp_path, monkeypatch, capsys):
    repo, st, code = _run(tmp_path, monkeypatch, ["indeed"], ["sources", "off", "jobspy"])
    assert code == 2 and "at least one source must stay on" in capsys.readouterr().err
    assert json.loads(cfg_path(repo).read_text(encoding="utf-8"))["sources"] == ["indeed"] and st.sql_log == []


def test_database_failure_leaves_the_config_file_untouched(tmp_path):
    repo = make_repo(tmp_path, search_cfg())
    before = cfg_path(repo).read_bytes()
    st = FakeStack(psql_raises=True)
    with pytest.raises(C.CheckError) as e:
        C.run_sources(st, repo, "on", ["feeds"])
    assert "config/search.json was not changed" in str(e.value) and "connection refused" in str(e.value)
    assert cfg_path(repo).read_bytes() == before
    assert [p.name for p in (repo / "config").iterdir()] == ["search.json"], "the temp file is removed"


def test_database_failure_through_main_exits_2(tmp_path, monkeypatch, capsys):
    repo, st, code = _run(tmp_path, monkeypatch, ["indeed"], ["sources", "on", "feeds"], psql_raises=True)
    assert code == 2 and "was not changed" in capsys.readouterr().err
    assert json.loads(cfg_path(repo).read_text(encoding="utf-8"))["sources"] == ["indeed"]


def test_config_with_comment_key_keeps_it_and_its_order(tmp_path):
    cfg = search_cfg(boards={"ashby": [{"slug": "openai", "name": "OpenAI"}]}, board_title_words=["sde"])
    repo = make_repo(tmp_path, cfg)
    C.run_sources(FakeStack(), repo, "off", ["linkedin"])
    text = cfg_path(repo).read_text(encoding="utf-8")
    expected = dict(cfg, sources=["indeed", "naukri"])
    assert list(json.loads(text)) == list(cfg), "same keys in the same order, _comment first"
    assert text == json.dumps(expected, indent=2, ensure_ascii=False) + "\n"
    assert "Zürich" in text, "non-ASCII text is written as is"


def test_a_config_without_a_sources_key_gets_one_at_the_end(tmp_path):
    cfg = search_cfg()
    del cfg["sources"]                                           # the validator defaults this to indeed + naukri
    repo = make_repo(tmp_path, cfg)
    C.run_sources(FakeStack(), repo, "on", ["feeds"])
    on_disk = json.loads(cfg_path(repo).read_text(encoding="utf-8"))
    assert on_disk["sources"] == ["indeed", "naukri"] + list(SRC.FEEDS) and list(on_disk)[-1] == "sources"


def test_switching_a_board_system_off_keeps_its_slugs(tmp_path):
    boards = {"greenhouse": ["cloudflare", "stripe"], "ashby": [{"slug": "openai", "name": "OpenAI"}], "lever": []}
    repo = make_repo(tmp_path, search_cfg(sources=["indeed", "greenhouse", "ashby"], boards=boards))
    st = FakeStack()
    C.run_sources(st, repo, "off", ["boards"])
    on_disk = json.loads(cfg_path(repo).read_text(encoding="utf-8"))
    assert on_disk["sources"] == ["indeed"] and on_disk["boards"] == boards
    stmt = next(q for q in st.sql_log if "INSERT INTO search_profile" in q)
    sent = json.loads(re.search(r"\$(cfg[0-9a-f]{12})\$(.*?)\$\1\$", stmt, re.S).group(2))
    assert sent["boards"]["greenhouse"] == ["cloudflare", "stripe"] and sent["boards"]["ashby"] == [{"slug": "openai", "name": "OpenAI"}]


def test_run_sources_rejects_an_invalid_config_without_writing(tmp_path):
    repo = make_repo(tmp_path, search_cfg(boards={"greenhouse": ["bad slug!"]}))
    before = cfg_path(repo).read_bytes()
    st = FakeStack()
    with pytest.raises(ValueError, match="invalid slug"):
        C.run_sources(st, repo, "on", ["feeds"])
    assert cfg_path(repo).read_bytes() == before and st.sql_log == []
    with pytest.raises(ValueError, match="invalid slug"):         # show refuses a broken file too: it cannot describe it reliably
        C.run_sources(FakeStack(), repo)


def test_an_unknown_source_name_writes_nothing(tmp_path):
    repo = make_repo(tmp_path, search_cfg())
    before = cfg_path(repo).read_bytes()
    st = FakeStack()
    with pytest.raises(ValueError, match="unknown source 'remotiv'"):
        C.run_sources(st, repo, "on", ["remotiv"])
    assert cfg_path(repo).read_bytes() == before and st.sql_log == []


def test_a_change_that_changes_nothing_touches_neither_file_nor_database(tmp_path):
    repo = make_repo(tmp_path, raw=json.dumps(search_cfg(), indent=4) + "\n")       # hand-formatted on purpose
    before = cfg_path(repo).read_bytes()
    st = FakeStack()
    out = C.run_sources(st, repo, "off", ["feeds"])
    assert "no change" in out and cfg_path(repo).read_bytes() == before
    assert not any("INSERT" in q for q in st.sql_log)


def test_the_file_mode_is_kept(tmp_path):
    import os
    import stat
    repo = make_repo(tmp_path, search_cfg())
    os.chmod(cfg_path(repo), 0o640)
    C.run_sources(FakeStack(), repo, "on", ["feeds"])
    assert stat.S_IMODE(cfg_path(repo).stat().st_mode) == 0o640


def test_run_sources_warns_about_a_paid_source_and_a_board_system_without_slugs(tmp_path):
    repo = make_repo(tmp_path, search_cfg())
    out = C.run_sources(FakeStack(), repo, "on", ["apify", "greenhouse"])
    assert "linkedin_apify uses your Apify credit" in out
    assert 'greenhouse is on but lists no companies' in out and 'boards find' in out


# ------------------------------------------------------------------ sources_report

def test_sources_report_lines():
    enabled = ["indeed", "linkedin_apify", "greenhouse", "remotive"]
    boards = {"greenhouse": ["cloudflare", {"slug": "stripe", "name": "Stripe"}], "lever": ["spotify"]}
    lines = C.sources_report(enabled, boards, {"indeed": 12, "greenhouse": 140, "lever": 3, "linkedin": 9}, board_batch=30)
    assert len(lines) == len(SRC.ALL) + 1
    by = {l.split()[1]: l for l in lines[:-1]}
    assert list(by) == list(SRC.ALL), "one line per source, in registry order"
    assert by["indeed"] == "on   indeed                               12 jobs   Indeed job search through JobSpy; free, no key needed."
    assert by["greenhouse"] == ("on   greenhouse       (2 companies)      140 jobs   "
                                "Jobs from company boards hosted on Greenhouse; needs the company slugs you list.")
    assert by["lever"] == ("off  lever            (1 company)          3 jobs   "
                           "Jobs from company boards hosted on Lever; needs the company slugs you list.")
    assert by["ashby"] == ("off  ashby            (0 companies)        0 jobs   "
                           "Jobs from company boards hosted on Ashby; needs the company slugs you list.")
    assert by["remotive"] == "on   remotive                              0 jobs   Remotive's public remote-jobs feed; needs nothing."
    assert by["linkedin"] == ("off  linkedin                              9 jobs   "
                              "LinkedIn job search through JobSpy; free, no key needed, but it can be rate limited.")
    assert by["linkedin_apify"] == ("on   linkedin_apify                   in linkedin   (paid: uses Apify credit)   "
                                    "LinkedIn job search through Apify; it uses your Apify credit.")
    assert lines[-1] == "boards per run: 30"


def test_sources_report_marks_the_paid_source_and_shows_counts():
    lines = C.sources_report(["linkedin_apify"], {}, {"linkedin": 50})
    paid = [l for l in lines if " linkedin_apify " in l][0]
    assert paid.startswith("on ") and "(paid: uses Apify credit)" in paid
    assert sum("paid" in l for l in lines) == 1, "only linkedin_apify is paid"
    assert not any(l.startswith("boards per run") for l in lines), "no boards-per-run line when the setting is unknown"
    assert " 50 jobs" in [l for l in lines if " linkedin " in l][0]


def test_sources_report_apify_jobs_are_counted_where_they_are_stored():
    """Apify results are stored with source 'linkedin' (apify_source._job), so the apify line points there instead of a wrong 0."""
    lines = C.sources_report(["linkedin_apify"], {}, {"linkedin": 50})
    apify = [l for l in lines if " linkedin_apify " in l][0]
    assert "in linkedin" in apify and " 0 jobs" not in apify
    lines = C.sources_report(["linkedin_apify"], {}, {"linkedin": 50, "linkedin_apify": 7})      # if a row ever exists under its own name
    assert " 7 jobs" in [l for l in lines if " linkedin_apify " in l][0]


def test_sources_report_through_run_sources_reads_counts_and_the_board_batch(tmp_path):
    repo = make_repo(tmp_path, search_cfg(sources=["indeed", "greenhouse"], boards={"greenhouse": ["stripe"]}))
    out = C.run_sources(FakeStack(sources_counts={"indeed": 5, "greenhouse": 21}, board_batch=45), repo)
    assert "boards per run: 45" in out and "21 jobs" in out and "(1 company)" in out
    assert "scripts/jobsearch sources off boards" in out and "only apify" in out, "the report shows how to change things"


def test_sources_report_survives_a_failing_count_query_and_a_missing_board_batch_column(tmp_path):
    repo = make_repo(tmp_path, search_cfg())
    out = C.run_sources(FakeStack(sources_counts=None, board_batch=None), repo)        # table missing / migration 013 not applied
    assert "0 jobs" in out and "indeed" in out and "boards per run" not in out
    out = C.run_sources(FakeStack(sources_counts={}), repo)                             # table empty
    assert "0 jobs" in out and "boards per run: 30" in out


def test_sources_command_prints_the_report(tmp_path, monkeypatch, capsys):
    repo = make_repo(tmp_path, search_cfg())
    monkeypatch.setattr(C, "REPO", repo)
    assert C.main(["sources"], stack=FakeStack()) == 0
    out = capsys.readouterr().out
    assert "indeed" in out and "linkedin_apify" in out and "paid" in out
    assert C.main(["sources", "show"], stack=FakeStack()) == 0


def test_sources_command_error_for_unknown_action_and_names(tmp_path, monkeypatch, capsys):
    repo = make_repo(tmp_path, search_cfg())
    monkeypatch.setattr(C, "REPO", repo)
    assert C.main(["sources", "on", "nonsense"], stack=FakeStack()) == 2
    assert "valid sources" in capsys.readouterr().err
    assert C.main(["sources", "only"], stack=FakeStack()) == 2
    assert "name at least one source or group" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        C.main(["sources", "toggle", "feeds"], stack=FakeStack())


# ------------------------------------------------------------------ schedule set collect --boards

def test_schedule_set_boards_translates_to_board_batch(capsys):
    st = FakeStack()
    assert C.main(["schedule", "set", "collect", "--boards", "45"], stack=st) == 0
    assert _post(st) == ("POST", "/schedule/collect", {"board_batch": 45})
    st = FakeStack()
    assert C.main(["schedule", "set", "collect", "--every", "6h", "--batch", "18", "--boards", "12"], stack=st) == 0
    assert _post(st)[2] == {"every_minutes": 360, "batch": 18, "board_batch": 12}


def test_schedule_boards_only_for_collect(capsys):
    st = FakeStack()
    assert C.main(["schedule", "set", "score", "--boards", "5"], stack=st) == 2
    assert "only the collect schedule" in capsys.readouterr().err and not hasattr(st, "py_calls")


@pytest.mark.parametrize("value", ["0", "201", "-4"])
def test_schedule_boards_range(value, capsys):
    st = FakeStack()
    assert C.main(["schedule", "set", "collect", "--boards", value], stack=st) == 2
    assert "from 1 to 200" in capsys.readouterr().err and not hasattr(st, "py_calls")


def test_schedule_changes_maps_boards():
    class A:
        every = at = batch = model = None
        enable = disable = False
        boards = 30
    assert C.schedule_changes(A) == {"board_batch": 30}


# ------------------------------------------------------------------ boards find

class ProbeResp(io.BytesIO):
    status = 200


def probe_opener(routes, log=None):
    """A stand-in for urlopen: `routes` maps a probe URL to the JSON it returns; every other URL is a 404."""
    def opener(req, timeout=0):
        if log is not None:
            log.append({"url": req.full_url, "timeout": timeout, "ua": req.get_header("User-agent")})
        if req.full_url in routes:
            return ProbeResp(json.dumps(routes[req.full_url]).encode())
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, io.BytesIO(b'{"error": "not found"}'))
    return opener


def probe_url(system, slug):
    return SRC.PROBE_URLS[system].format(slug=slug)


def test_slug_candidates():
    assert C.slug_candidates("stripe inc") == ["stripeinc", "stripe-inc", "stripe"]        # the original case adds nothing here
    assert C.slug_candidates("Stripe Inc") == ["stripeinc", "stripe-inc", "stripe", "StripeInc"]
    assert C.slug_candidates("Bosch Group") == ["boschgroup", "bosch-group", "bosch", "BoschGroup"]
    assert C.slug_candidates("Stripe") == ["stripe", "Stripe"]
    assert C.slug_candidates("stripe") == ["stripe"]
    assert C.slug_candidates("Foo_Bar Baz") == ["foo_barbaz", "foo_bar-baz", "foobar", "Foo_BarBaz"]


def test_slug_candidates_are_at_most_four_unique_and_valid():
    for name in ("a b c d e f g h", "Stripe, Inc.", "monday.com", "  Acme   Corp  ", "x" * 80, "Über Café", "a-b c"):
        try:
            got = C.slug_candidates(name)
        except ValueError:
            continue
        assert 1 <= len(got) <= 4 and len(set(got)) == len(got)
        assert all(SRC.valid_slug(s) for s in got), got


@pytest.mark.parametrize("name", ["", "   ", "!!!", "x" * 200])
def test_slug_candidates_refuse_a_name_with_no_usable_slug(name):
    with pytest.raises(ValueError, match="company name"):
        C.slug_candidates(name)


def test_boards_find_reports_the_matching_system():
    routes = {probe_url("greenhouse", "stripe"): {"jobs": [{"id": 1}, {"id": 2}, {"id": 3}]}}
    assert C.boards_find("stripe inc", probe_opener(routes)) == [{"system": "greenhouse", "slug": "stripe", "jobs": 3}]


def test_boards_find_understands_each_systems_response_shape():
    routes = {probe_url("greenhouse", "acme"): {"jobs": [{}] * 2},
              probe_url("lever", "acme"): [{}] * 5,
              probe_url("ashby", "acme"): {"jobs": [{}] * 4, "apiVersion": "1"},
              probe_url("workable", "acme"): {"name": "Acme", "jobs": []},
              probe_url("smartrecruiters", "acme"): {"totalFound": 7, "content": []}}
    found = {m["system"]: m["jobs"] for m in C.boards_find("Acme", probe_opener(routes))}
    assert found == {"greenhouse": 2, "lever": 5, "ashby": 4, "workable": 0, "smartrecruiters": 7}


def test_boards_find_ignores_a_response_of_the_wrong_shape():
    routes = {probe_url("greenhouse", "acme"): "<html>nope</html>",             # a JSON string, not the object
              probe_url("lever", "acme"): {"ok": False, "error": "Document not found"},     # an object where a list belongs
              probe_url("ashby", "acme"): {"jobs": "many"},
              probe_url("workable", "acme"): [{}],
              probe_url("smartrecruiters", "acme"): {"totalFound": True}}
    assert C.boards_find("acme", probe_opener(routes)) == []


def test_boards_find_smartrecruiters_answers_200_for_unknown_companies_so_zero_is_no_match():
    assert C.boards_find("acme", probe_opener({probe_url("smartrecruiters", "acme"): {"totalFound": 0, "content": []}})) == []


def test_boards_find_no_match():
    log = []
    assert C.boards_find("Nonexistent Startup", probe_opener({}, log)) == []
    assert len(log) > 0


def test_boards_find_a_body_that_is_not_json_is_no_match():
    class Html(io.BytesIO):
        status = 200
    def opener(req, timeout=0):
        return Html(b"<html><body>Welcome</body></html>")
    assert C.boards_find("acme", opener) == []


def test_boards_find_non_200_is_no_match():
    class NoContent(io.BytesIO):
        status = 204
    def opener(req, timeout=0):
        return NoContent(json.dumps({"jobs": [{}]}).encode())
    assert C.boards_find("acme", opener) == []


def test_boards_find_makes_at_most_twenty_requests():
    log = []
    C.boards_find("Acme Corp", probe_opener({}, log))                          # three lower-case candidates x five systems + the original case on smartrecruiters
    assert len(log) == 16
    log.clear()
    C.boards_find("a b c d e f g h i j", probe_opener({}, log))                # a long name still means four candidates
    assert len(log) <= 20
    log.clear()
    C.boards_find("Acme", probe_opener({}, log))
    assert len(log) == 6                                                       # acme on five systems, Acme on smartrecruiters only


def test_boards_find_only_probes_the_five_fixed_hosts_with_a_timeout_and_an_honest_user_agent():
    from urllib.parse import urlparse
    hosts = {urlparse(u).netloc for u in SRC.PROBE_URLS.values()}
    log = []
    C.boards_find("Acme Corp", probe_opener({}, log))
    assert {urlparse(r["url"]).netloc for r in log} == hosts
    assert all(urlparse(r["url"]).scheme == "https" for r in log)
    assert all(r["timeout"] == 10 for r in log)
    assert all(r["ua"] == "ai-job-search-n8n/1.0 (personal job search)" for r in log)


def test_boards_find_tolerates_every_kind_of_opener_failure():
    errors = [urllib.error.URLError("no route"), TimeoutError("slow"), ConnectionResetError("reset"), OSError("boom"),
              ValueError("weird"), json.JSONDecodeError("x", "y", 0)]
    calls = []
    def opener(req, timeout=0):
        calls.append(req.full_url)
        raise errors[len(calls) % len(errors)]
    assert C.boards_find("Acme", opener) == []
    assert len(calls) == 6                                                     # it kept going after each failure


def test_boards_find_does_not_follow_a_redirect():
    handler = C._NoRedirect()
    req = urllib.request.Request("https://boards-api.greenhouse.io/v1/boards/x/jobs")
    assert handler.redirect_request(req, io.BytesIO(b""), 301, "Moved", {}, "https://evil.example/x") is None


def test_boards_find_reads_a_bounded_amount():
    class Endless(io.BytesIO):
        status = 200
        asked = []
        def read(self, n=-1):
            Endless.asked.append(n)
            return b"[" + b" " * 10
    def opener(req, timeout=0):
        return Endless()
    C.boards_find("acme", opener)
    assert Endless.asked and all(isinstance(n, int) and 0 < n <= 40 * 1024 * 1024 for n in Endless.asked)


def test_run_boards_find_prints_the_match_and_the_exact_lines_to_add(monkeypatch):
    routes = {probe_url("greenhouse", "stripe"): {"jobs": [{}] * 140}}
    out = C.run_boards_find("Stripe Inc", probe_opener(routes))
    assert "greenhouse" in out and "stripe" in out and "140" in out
    assert '"boards": {"greenhouse": ["stripe"]}' in out
    assert "scripts/jobsearch sources on greenhouse" in out
    assert "config/search.json" in out


def test_run_boards_find_with_no_match_explains_where_to_look():
    out = C.run_boards_find("Nonexistent Startup", probe_opener({}))
    assert "no match" in out.lower()
    for hint in ("boards.greenhouse.io/<slug>", "jobs.lever.co/<slug>", "jobs.ashbyhq.com/<slug>", "Workday", "careers page"):
        assert hint in out, hint
    assert "own career site" in out


def test_boards_find_through_main(monkeypatch, capsys):
    routes = {probe_url("lever", "spotify"): [{}] * 9}
    monkeypatch.setattr(C, "_probe_open", probe_opener(routes))
    assert C.main(["boards", "find", "Spotify"], stack=FakeStack()) == 0
    out = capsys.readouterr().out
    assert '"lever": ["spotify"]' in out and "sources on lever" in out
    monkeypatch.setattr(C, "_probe_open", probe_opener({probe_url("greenhouse", "stripeinc"): {"jobs": []}}))
    assert C.main(["boards", "find", "stripe", "inc"], stack=FakeStack()) == 0, "the words after `find` are joined into one name"
    assert '"greenhouse": ["stripeinc"]' in capsys.readouterr().out
    assert C.main(["boards", "find", "!!!"], stack=FakeStack()) == 2
    assert "company name" in capsys.readouterr().err


# ------------------------------------------------------------------ doctor: sources

def _doctor(tmp_path, cfg=None, **stack_kw):
    repo = make_repo(tmp_path, cfg)
    return by_name(C.run_doctor(FakeStack(**stack_kw), repo))


def test_doctor_lists_what_is_switched_on(tmp_path):
    c = _doctor(tmp_path, search_cfg(sources=["indeed", "linkedin_apify", "remotive"]))
    assert c["sources"].status == "ok" and c["sources"].detail == "on: indeed, linkedin_apify, remotive"


def test_doctor_warns_when_slugs_are_listed_for_a_system_that_is_switched_off(tmp_path):
    c = _doctor(tmp_path, search_cfg(boards={"greenhouse": ["cloudflare", "stripe"]}))
    assert c["sources"].status == "warn"
    assert "greenhouse has 2 slugs but is switched off: scripts/jobsearch sources on greenhouse" in c["sources"].detail
    assert "on: indeed, naukri, linkedin" in c["sources"].detail
    c = _doctor(tmp_path / "x", search_cfg(boards={"lever": ["spotify"]}))
    assert "lever has 1 slug but is switched off" in c["sources"].detail


def test_doctor_warns_when_a_board_system_is_on_without_slugs(tmp_path):
    c = _doctor(tmp_path, search_cfg(sources=["indeed", "greenhouse"], boards={"lever": []}))
    assert c["sources"].status == "warn"
    assert "greenhouse is switched on but has no slugs" in c["sources"].detail
    assert "lever" not in c["sources"].detail, "an empty list for a system that is off is fine"


def test_doctor_warns_for_a_slug_that_keeps_failing(tmp_path):
    rows = [{"source": "greenhouse", "key": "foo", "failures": 7}, {"source": "remotive", "key": "", "failures": 5}]
    c = _doctor(tmp_path, search_cfg(sources=["indeed", "greenhouse", "remotive"], boards={"greenhouse": ["foo"]}), fetch_failures=rows)
    assert c["source greenhouse/foo"].status == "warn"
    assert c["source greenhouse/foo"].detail == "greenhouse/foo failed 7 runs in a row: check the slug"
    assert c["source remotive"].status == "warn" and "remotive failed 5 runs in a row" in c["source remotive"].detail
    assert c["sources"].status == "ok", "the slugs and the switches agree"


def test_doctor_asks_only_for_rows_with_five_or_more_failures(tmp_path):
    st = FakeStack()
    C.run_doctor(st, make_repo(tmp_path, search_cfg()))
    q = next(q for q in st.sql_log if "from fetch_state" in q)
    assert q == "select source, key, failures from fetch_state where failures >= 5 order by failures desc"


def test_doctor_adds_only_one_line_for_an_old_config(tmp_path):
    """A config that uses nothing from Phase 7 gets exactly one new line, and it is ok."""
    base = by_name(C.run_doctor(FakeStack(), make_repo(tmp_path / "none")))          # no search.json: nothing to add
    old = by_name(C.run_doctor(FakeStack(), make_repo(tmp_path / "old", search_cfg())))
    assert set(old) - set(base) == {"sources"} and set(base) - set(old) == set()
    assert all(c.status == "ok" for c in old.values()), [(c.name, c.detail) for c in old.values() if c.status != "ok"]


def test_doctor_is_quiet_when_nothing_new_is_configured(tmp_path):
    c = _doctor(tmp_path, search_cfg(sources=["indeed", "naukri", "linkedin", "linkedin_apify"]))
    assert c["sources"].status == "ok" and [n for n in c if n.startswith("source ")] == []


def test_doctor_skips_the_fetch_state_check_when_013_is_not_applied(tmp_path):
    files = sorted(p.stem for p in (REPO / "postgres" / "migrations").glob("*.sql"))
    old_db = ["000_baseline"] + [f for f in files if f != "013_boards"]
    st = FakeStack(migrations=old_db, fetch_failures=[{"source": "greenhouse", "key": "foo", "failures": 9}])
    checks = by_name(C.run_doctor(st, make_repo(tmp_path, search_cfg())))
    assert not any("fetch_state" in q for q in st.sql_log)
    assert "source greenhouse/foo" not in checks and checks["sources"].status == "ok"
    assert checks["database migrations"].status == "fail" and "013_boards" in checks["database migrations"].detail


def test_doctor_without_a_search_json_adds_no_sources_line(tmp_path):
    assert "sources" not in _doctor(tmp_path)


def test_doctor_with_a_broken_search_json_warns_instead_of_crashing(tmp_path):
    repo = make_repo(tmp_path, raw="{ nope")
    checks = by_name(C.run_doctor(FakeStack(), repo))
    assert checks["sources"].status == "warn" and "search.json" in checks["sources"].detail
    repo = make_repo(tmp_path / "inv", search_cfg(boards={"greenhouse": ["bad slug!"]}))
    checks = by_name(C.run_doctor(FakeStack(), repo))
    assert checks["sources"].status == "warn" and "invalid slug" in checks["sources"].detail


def test_doctor_fetch_state_failure_to_read_is_quiet(tmp_path):
    class Broken(FakeStack):
        def query_json(self, sql, db="jobsearch"):
            if "from fetch_state" in sql:
                raise C.StackError("relation does not exist")
            return super().query_json(sql, db)
    checks = by_name(C.run_doctor(Broken(), make_repo(tmp_path, search_cfg())))
    assert checks["sources"].status == "ok" and not any(n.startswith("source ") for n in checks)


def test_the_module_docstring_lists_the_new_commands():
    doc = C.__doc__
    assert "sources [show|on|off|only]" in doc and "boards find" in doc and "--boards" in doc


# ================================================================== follow-up batch (Task F)

def test_doctor_with_a_non_utf8_search_json_warns_instead_of_crashing(tmp_path):
    repo = make_repo(tmp_path)
    cfg_path(repo).write_bytes(b'{"x": "\xff"}')
    checks = by_name(C.run_doctor(FakeStack(), repo))
    assert checks["sources"].status == "warn"
    assert checks["sources"].detail == "cannot read config/search.json (not valid UTF-8)"      # printed as `sources  cannot read ...`


def test_run_boards_find_prints_one_combined_boards_fragment_for_several_systems():
    routes = {probe_url("greenhouse", "stripe"): {"jobs": [{}] * 3}, probe_url("lever", "stripe"): [{}] * 2}
    out = C.run_boards_find("Stripe", probe_opener(routes))
    frags = [l.strip() for l in out.splitlines() if l.strip().startswith('"boards":')]
    assert frags == ['"boards": {"greenhouse": ["stripe"], "lever": ["stripe"]}'], "ONE fragment, valid to paste as a single key"
    assert json.loads("{" + frags[0] + "}") == {"boards": {"greenhouse": ["stripe"], "lever": ["stripe"]}}
    cmds = [l.strip() for l in out.splitlines() if l.strip().startswith("scripts/jobsearch sources on")]
    assert cmds == ["scripts/jobsearch sources on greenhouse lever"], "one command listing every matched system"


def test_run_boards_find_combines_several_slugs_of_one_system_into_one_list():
    routes = {probe_url("greenhouse", "stripeinc"): {"jobs": []}, probe_url("greenhouse", "stripe"): {"jobs": [{}]}}
    out = C.run_boards_find("Stripe Inc", probe_opener(routes))
    assert '"boards": {"greenhouse": ["stripeinc", "stripe"]}' in out and out.count('"boards":') == 1


def test_sources_show_with_names_is_an_error_and_writes_nothing(tmp_path, monkeypatch, capsys):
    repo = make_repo(tmp_path, search_cfg())
    monkeypatch.setattr(C, "REPO", repo)
    before = cfg_path(repo).read_bytes()
    st = FakeStack()
    assert C.main(["sources", "show", "feeds"], stack=st) == 2
    assert "show takes no names: use on, off or only to change sources" in capsys.readouterr().err
    assert cfg_path(repo).read_bytes() == before and st.sql_log == []
    with pytest.raises(ValueError, match="show takes no names"):
        C.run_sources(st, repo, "show", ["remotive"])
    assert st.sql_log == []


def test_a_successful_change_says_the_whole_file_was_applied(tmp_path):
    repo = make_repo(tmp_path, search_cfg())
    out = C.run_sources(FakeStack(), repo, "on", ["remotive"])
    line = "applied config/search.json to the database (the whole file, not only sources)"
    assert out.splitlines().count(line) == 1
    # not for a show, and not for a change that changes nothing
    assert line not in C.run_sources(FakeStack(), repo)
    assert line not in C.run_sources(FakeStack(), repo, "on", ["remotive"])


def test_run_sources_docstring_documents_the_symlink_caveat():
    assert "symlink" in C.run_sources.__doc__


def test_a_failed_file_swap_says_the_database_already_has_the_new_sources(tmp_path, monkeypatch):
    repo = make_repo(tmp_path, search_cfg())
    before = cfg_path(repo).read_bytes()
    st = FakeStack()

    def refuse(src, dst):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(C.os, "replace", refuse)
    with pytest.raises(C.CheckError) as e:
        C.run_sources(st, repo, "on", ["feeds"])
    msg = str(e.value)
    assert ("the database already has the new sources; make config/search.json writable "
            "and run the same `sources` command again") in msg and "Permission denied" in msg
    assert cfg_path(repo).read_bytes() == before
    assert len([q for q in st.sql_log if "INSERT INTO search_profile" in q]) == 1
    assert [p.name for p in (repo / "config").iterdir()] == ["search.json"], "the temp file is removed"


def test_a_read_only_config_dir_is_reported_and_no_sql_is_sent(tmp_path, monkeypatch):
    repo = make_repo(tmp_path, search_cfg())
    before = cfg_path(repo).read_bytes()
    st = FakeStack()

    def deny(*a, **kw):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(C.tempfile, "mkstemp", deny)
    with pytest.raises(C.CheckError) as e:
        C.run_sources(st, repo, "on", ["feeds"])
    assert str(e.value) == "cannot write in config/ (Permission denied); nothing was changed"
    assert st.sql_log == [] and cfg_path(repo).read_bytes() == before


# ------------------------------------------------------------------ boards find: candidates (Task F, F6)

def case_insensitive_opener(routes, log=None):
    """Like probe_opener, but a host that ignores the case of the slug (every system except SmartRecruiters)."""
    lowered = {u.lower(): v for u, v in routes.items()}

    def opener(req, timeout=0):
        if log is not None:
            log.append(req.full_url)
        if req.full_url.lower() in lowered:
            return ProbeResp(json.dumps(lowered[req.full_url.lower()]).encode())
        raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, io.BytesIO(b"{}"))
    return opener


def test_boards_find_the_original_case_is_probed_only_on_smartrecruiters():
    log = []
    C.boards_find("Acme Corp", probe_opener({}, log))
    mixed = [r["url"] for r in log if "AcmeCorp" in r["url"]]
    assert mixed == [probe_url("smartrecruiters", "AcmeCorp")]
    for system, template in SRC.PROBE_URLS.items():
        probed = [r["url"] for r in log if r["url"] in {template.format(slug=s) for s in ("acmecorp", "acme-corp", "acme", "AcmeCorp")}]
        want = 4 if system == "smartrecruiters" else 3
        assert len(probed) == want, system
    assert len(log) == 16


def test_slug_candidates_strip_edge_dots_commas_and_underscores():
    assert C.slug_candidates("Stripe, Inc.")[:3] == ["stripeinc", "stripe-inc", "stripe"]
    for name in ("Stripe, Inc.", "_acme_ corp.", "Acme.", "..acme.."):
        for slug in C.slug_candidates(name):
            assert slug[0] not in "._," and slug[-1] not in "._,", (name, slug)
    assert C.slug_candidates("monday.com") == ["monday.com"]                    # inner dots stay
    assert C.slug_candidates("Foo_Bar Baz") == ["foo_barbaz", "foo_bar-baz", "foobar", "Foo_BarBaz"]   # inner underscores stay


def test_boards_find_a_case_insensitive_host_is_reported_once():
    routes = {probe_url("greenhouse", "stripe"): {"jobs": [{}] * 3}}
    found = C.boards_find("Stripe", case_insensitive_opener(routes))
    assert [(m["system"], m["slug"]) for m in found] == [("greenhouse", "stripe")]
    # SmartRecruiters is case-sensitive: both spellings are different companies there, so both can be reported
    sr = {probe_url("smartrecruiters", "stripe"): {"totalFound": 4}, probe_url("smartrecruiters", "Stripe"): {"totalFound": 1}}
    found = C.boards_find("Stripe", probe_opener(sr))
    assert [(m["system"], m["slug"]) for m in found] == [("smartrecruiters", "stripe"), ("smartrecruiters", "Stripe")]


def test_boards_find_never_reports_the_same_case_insensitive_slug_twice(monkeypatch):
    monkeypatch.setattr(C, "slug_candidates", lambda name: ["stripe", "stripe"])   # even a repeated candidate
    routes = {probe_url("lever", "stripe"): [{}] * 2}
    found = C.boards_find("Stripe", case_insensitive_opener(routes))
    assert [(m["system"], m["slug"]) for m in found] == [("lever", "stripe")]


def test_boards_find_total_requests_stay_within_twenty():
    for name in ("Acme", "Acme Corp", "Stripe, Inc.", "a b c d e f g h i j"):
        log = []
        C.boards_find(name, probe_opener({}, log))
        assert len(log) <= 20, name


# ------------------------------------------------------------------ doctor: no stale fetch_state noise (Task F, F8)

def test_doctor_fetch_state_warns_only_for_what_is_still_configured(tmp_path):
    rows = [{"source": "greenhouse", "key": "kept", "failures": 6},
            {"source": "greenhouse", "key": "removed", "failures": 9},        # slug no longer in config/search.json
            {"source": "lever", "key": "spotify", "failures": 8},             # the whole system is switched off
            {"source": "remotive", "key": "", "failures": 5},                 # feed switched on
            {"source": "remoteok", "key": "", "failures": 7}]                 # feed switched off
    cfg = search_cfg(sources=["indeed", "greenhouse", "remotive"],
                     boards={"greenhouse": ["kept", {"slug": "other", "name": "Other"}], "lever": ["spotify"]})
    c = _doctor(tmp_path, cfg, fetch_failures=rows)
    assert {n for n in c if n.startswith("source ")} == {"source greenhouse/kept", "source remotive"}
    assert c["source greenhouse/kept"].status == "warn" and c["source remotive"].status == "warn"


def test_doctor_fetch_state_object_form_slug_counts_as_configured(tmp_path):
    rows = [{"source": "ashby", "key": "openai", "failures": 5}]
    c = _doctor(tmp_path, search_cfg(sources=["indeed", "ashby"], boards={"ashby": [{"slug": "openai", "name": "OpenAI"}]}),
                fetch_failures=rows)
    assert c["source ashby/openai"].status == "warn"


def test_doctor_fetch_state_failing_rows_for_a_removed_slug_or_a_switched_off_feed_add_nothing(tmp_path):
    rows = [{"source": "greenhouse", "key": "gone", "failures": 9}, {"source": "arbeitnow", "key": "", "failures": 6}]
    c = _doctor(tmp_path, search_cfg(sources=["indeed", "greenhouse"], boards={"greenhouse": ["stripe"]}), fetch_failures=rows)
    assert [n for n in c if n.startswith("source ")] == []


# ------------------------------------------------------------------ final review: sources that could never collect anything

NO_SLUGS_MESSAGE = ("boards has no company slugs yet: add some under \"boards\" in config/search.json, "
                    "or use `boards find`")


def test_only_boards_without_any_slug_is_refused_and_writes_nothing(tmp_path, monkeypatch, capsys):
    repo, st, code = _run(tmp_path, monkeypatch, ["indeed"], ["sources", "only", "boards"])
    assert code == 2 and NO_SLUGS_MESSAGE in capsys.readouterr().err
    assert json.loads(cfg_path(repo).read_text(encoding="utf-8"))["sources"] == ["indeed"]
    assert st.sql_log == [] and [p.name for p in (repo / "config").iterdir()] == ["search.json"]


def test_on_lever_with_only_board_systems_and_no_slugs_is_refused(tmp_path):
    repo = make_repo(tmp_path, search_cfg(sources=["greenhouse"]))
    before = cfg_path(repo).read_bytes()
    st = FakeStack()
    with pytest.raises(ValueError) as e:
        C.run_sources(st, repo, "on", ["lever"])
    assert str(e.value) == NO_SLUGS_MESSAGE
    assert cfg_path(repo).read_bytes() == before and st.sql_log == []


def test_boards_without_slugs_stay_allowed_when_another_source_remains(tmp_path):
    repo = make_repo(tmp_path, search_cfg(sources=["indeed"]))
    out = C.run_sources(FakeStack(), repo, "on", ["lever"])          # indeed stays on: the existing note, no refusal
    assert json.loads(cfg_path(repo).read_text(encoding="utf-8"))["sources"] == ["indeed", "lever"]
    assert "lever is on but lists no companies yet" in out
    repo2 = make_repo(tmp_path / "feeds", search_cfg(sources=["indeed"]))
    C.run_sources(FakeStack(), repo2, "only", ["boards", "remotive"])  # a feed needs no slugs, so it counts as another source
    assert json.loads(cfg_path(repo2).read_text(encoding="utf-8"))["sources"] == list(SRC.BOARD_SYSTEMS) + ["remotive"]


def test_only_boards_with_slugs_is_allowed(tmp_path):
    repo = make_repo(tmp_path, search_cfg(sources=["indeed"], boards={"greenhouse": ["stripe"]}))
    C.run_sources(FakeStack(), repo, "only", ["boards"])
    assert json.loads(cfg_path(repo).read_text(encoding="utf-8"))["sources"] == list(SRC.BOARD_SYSTEMS)


def test_a_change_that_changes_nothing_is_not_refused(tmp_path):
    repo = make_repo(tmp_path, search_cfg(sources=["greenhouse"]))
    out = C.run_sources(FakeStack(), repo, "only", ["greenhouse"])
    assert "no change" in out


def test_a_leftover_search_config_temp_file_is_git_ignored(tmp_path):
    """`sources` writes the whole search config to config/.search.<random>.tmp before swapping it in; a crash can leave it behind."""
    import fnmatch
    import os
    import tempfile
    fd, name = tempfile.mkstemp(dir=str(tmp_path), prefix=".search.", suffix=".tmp")      # the same call run_sources makes
    os.close(fd)
    patterns = [ln.strip() for ln in (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()]
    assert "config/.search.*.tmp" in patterns
    assert fnmatch.fnmatch(Path(name).name, Path("config/.search.*.tmp").name)


def test_the_docs_say_what_the_final_review_asked_them_to_say():
    import boards as boards_mod                                   # scraper/ is on sys.path; the doc's "one minute" is its deadline
    setup = (REPO / "docs" / "setup.md").read_text(encoding="utf-8")
    assert "docker compose build --pull scraper" in setup
    assert "python:3.12-slim" in setup and "rebuild with `--pull`" in setup
    assert "scoring step can fail after the model call was already paid for" in setup
    assert setup.index("scripts/jobsearch migrate") < setup.index("docker compose build --pull scraper")   # migrate first
    sources_doc = (REPO / "docs" / "sources.md").read_text(encoding="utf-8")
    assert "a slow source can hold one request up to a minute" in sources_doc.lower()
    assert str(int(boards_mod.REQUEST_DEADLINE_S)) == "60" and "one minute" in sources_doc
    assert "not the database" in sources_doc and "search-profile apply" in sources_doc


# ------------------------------------------------------------------ the dashboard edits the search profile (version 2c)

def _row(**over):
    base = {"name": "default", "search_terms": ["backend engineer"], "locations": ["Pune"], "sources": ["indeed", "greenhouse"],
            "allow_remote": True, "min_yoe": 0, "max_yoe": 4, "include_keywords": [], "exclude_keywords": ["intern"],
            "exclude_companies": ["Acme"], "max_age_days": 7, "score_threshold": 60,
            "boards": {"greenhouse": ["stripe", {"slug": "notion", "name": "Notion"}]}, "board_title_words": ["software"],
            "edited_by": "dashboard"}
    base.update(over)
    return base


def _writes(stack):
    """The statements that change the database (the guard's own SELECT is not one)."""
    return [q for q in stack.sql_log if q.lstrip().upper().startswith(("INSERT", "UPDATE", "WITH"))]


def test_every_cli_write_marks_the_profile_as_written_by_the_cli():
    sql = C.search_profile_sql(SETTINGS)
    assert "edited_by, updated_at)" in sql and "'cli', now()" in sql and "edited_by = 'cli', updated_at = now()" in sql


def test_who_wrote_the_profile_last():
    assert C.profile_written_by(FakeStack(search=[{"edited_by": "dashboard"}])) == "dashboard"
    assert C.profile_written_by(FakeStack(search=[{"edited_by": "cli"}])) == "cli"
    assert C.profile_written_by(FakeStack(search=[])) is None                       # no such profile
    assert C.profile_written_by(FakeStack(search=[{"search_terms": []}])) is None   # migration 017 not applied: no column
    assert C.profile_written_by(FakeStack(), "bad name!") is None


def test_apply_refuses_to_overwrite_a_dashboard_edit_and_sends_nothing(tmp_path, capsys):
    f = tmp_path / "search.json"
    f.write_text(json.dumps(SETTINGS))
    stack = FakeStack(search=[{"edited_by": "dashboard"}])
    assert C.main(["search-profile", "apply", "--file", str(f)], stack=stack) == 2
    err = capsys.readouterr().err
    assert "edited in the dashboard" in err and "search-profile pull" in err and "--force" in err
    assert _writes(stack) == []


def test_apply_with_force_discards_the_dashboard_edit(tmp_path):
    f = tmp_path / "search.json"
    f.write_text(json.dumps(SETTINGS))
    stack = FakeStack(search=[{"edited_by": "dashboard"}])
    assert C.main(["search-profile", "apply", "--file", str(f), "--force"], stack=stack) == 0
    assert any("INSERT INTO search_profile" in q for q in stack.sql_log)


def test_apply_is_unchanged_when_the_cli_wrote_last_or_there_is_no_profile_yet(tmp_path):
    f = tmp_path / "search.json"
    f.write_text(json.dumps(SETTINGS))
    for search in ([{"edited_by": "cli"}], [], [{"search_terms": []}]):
        stack = FakeStack(search=search)
        assert C.main(["search-profile", "apply", "--file", str(f)], stack=stack) == 0
        assert any("INSERT INTO search_profile" in q for q in stack.sql_log)


def test_sources_change_refuses_to_write_the_file_over_a_dashboard_edit(tmp_path):
    repo = make_repo(tmp_path, _validated(sources=["indeed", "remotive"]))
    before = cfg_path(repo).read_text(encoding="utf-8")
    stack = FakeStack(search=[{"edited_by": "dashboard"}])
    with pytest.raises(C.CheckError, match="search-profile pull"):
        C.run_sources(stack, repo, "off", ["indeed"])
    assert cfg_path(repo).read_text(encoding="utf-8") == before and _writes(stack) == []
    with pytest.raises(C.CheckError) as e:
        C.run_sources(stack, repo, "off", ["indeed"])
    assert "--force" not in str(e.value), "`sources` has no --force: the way out is pull"


def test_sources_show_says_when_the_file_is_behind_the_dashboard(tmp_path):
    repo = make_repo(tmp_path, _validated(sources=["indeed", "remotive"]))
    out = C.run_sources(FakeStack(search=[{"edited_by": "dashboard"}]), repo, "show")
    assert "the dashboard edited the search profile" in out and "search-profile pull" in out
    assert "dashboard edited" not in C.run_sources(FakeStack(search=[{"edited_by": "cli"}]), repo, "show")


def test_pull_writes_the_database_profile_into_the_file_and_marks_it_as_the_clis(tmp_path):
    repo = make_repo(tmp_path, {"_comment": "my note", **_validated(sources=["indeed"], score_threshold=70)})
    path = cfg_path(repo)
    os.chmod(path, 0o600)
    stack = FakeStack(search=[_row()])
    assert C.main(["search-profile", "pull", "--file", str(path)], stack=stack) == 0
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["_comment"] == "my note", "keys the file has besides the profile are kept"
    assert written["sources"] == ["indeed", "greenhouse"] and written["score_threshold"] == 60 and written["exclude_companies"] == ["Acme"]
    assert written["boards"] == {"greenhouse": ["stripe", {"slug": "notion", "name": "Notion"}]}
    assert list(written)[0] == "_comment" and set(written) >= {"search_terms", "board_title_words", "boards"}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(path.parent.glob(".search.*.tmp"))
    assert any("UPDATE search_profile SET edited_by = 'cli' WHERE name = 'default' AND edited_by = 'dashboard'" in q for q in stack.sql_log)
    import profile_tools
    profile_tools.validate_search(written)                      # what it wrote is a valid file


def test_pull_keeps_what_the_file_said_when_it_differs_from_the_database(tmp_path):
    repo = make_repo(tmp_path, {"_comment": "mine", **_validated(sources=["indeed"], score_threshold=70)})
    path = cfg_path(repo)
    old = path.read_text(encoding="utf-8")
    out = C.run_profile_pull(FakeStore_for_pull(), repo)
    backup = path.with_name("search.json.before-pull")
    assert backup.read_text(encoding="utf-8") == old and "search.json.before-pull" in out
    assert json.loads(path.read_text(encoding="utf-8"))["score_threshold"] == 60


def test_pull_makes_no_backup_when_the_file_already_matches_the_database(tmp_path):
    import profile_tools
    repo = make_repo(tmp_path)
    C.run_profile_pull(FakeStore_for_pull(), repo)                       # the file now equals the database
    out = C.run_profile_pull(FakeStore_for_pull(), repo)
    assert not (repo / "config" / "search.json.before-pull").exists() and "before-pull" not in out
    profile_tools.validate_search(json.loads(cfg_path(repo).read_text(encoding="utf-8")))


def test_pull_backs_up_a_file_that_is_not_valid_settings_too(tmp_path):
    repo = make_repo(tmp_path, {"search_terms": "not a list"})
    C.run_profile_pull(FakeStore_for_pull(), repo)
    assert "not a list" in (repo / "config" / "search.json.before-pull").read_text(encoding="utf-8")


def FakeStore_for_pull():
    return FakeStack(search=[_row()])


def test_pull_creates_the_file_when_there_is_none(tmp_path):
    repo = make_repo(tmp_path)
    stack = FakeStack(search=[_row()])
    out = C.run_profile_pull(stack, repo)
    assert cfg_path(repo).is_file() and "2 source(s)" in out and "2 company board(s)" in out


def test_pull_replaces_a_file_that_is_not_json(tmp_path):
    repo = make_repo(tmp_path, raw="{ this is not json")
    C.run_profile_pull(FakeStack(search=[_row()]), repo)
    assert json.loads(cfg_path(repo).read_text(encoding="utf-8"))["search_terms"] == ["backend engineer"]


def test_pull_refuses_a_stored_profile_that_fails_the_checks_and_leaves_the_file(tmp_path):
    repo = make_repo(tmp_path, _validated())
    before = cfg_path(repo).read_text(encoding="utf-8")
    with pytest.raises(C.CheckError, match="does not pass the checks"):
        C.run_profile_pull(FakeStack(search=[_row(search_terms=[])]), repo)
    assert cfg_path(repo).read_text(encoding="utf-8") == before


def test_pull_with_no_profile_in_the_database_says_what_to_do(tmp_path):
    repo = make_repo(tmp_path)
    with pytest.raises(C.CheckError, match="search-profile apply"):
        C.run_profile_pull(FakeStack(search=[]), repo)
    assert not cfg_path(repo).exists()


def test_pull_refuses_a_bad_profile_name(tmp_path):
    with pytest.raises(ValueError):
        C.run_profile_pull(FakeStack(), make_repo(tmp_path), name="x'; drop table jobs;--")


def test_doctor_says_when_the_file_is_behind_the_dashboard(tmp_path):
    ok = by_name(C.run_doctor(FakeStack(search=[{"search_terms": ["backend"], "locations": ["Pune"], "score_threshold": 60, "edited_by": "cli"}]), REPO))
    assert "search profile file" not in ok
    behind = by_name(C.run_doctor(FakeStack(search=[{"search_terms": ["backend"], "locations": ["Pune"], "score_threshold": 60, "edited_by": "dashboard"}]), REPO))
    assert behind["search profile file"].status == "warn" and "search-profile pull" in behind["search profile file"].detail
    assert behind["search profile"].status == "ok"


# ------------------------------------------------------------------ version 2c: what the review found

@pytest.mark.parametrize("content", ['{"search_terms": ["x"],}', "", "[1, 2]", "not json at all"])
def test_pull_never_replaces_a_broken_file_without_keeping_it(tmp_path, content):
    repo = make_repo(tmp_path, raw=content)
    C.run_profile_pull(FakeStack(search=[_row()]), repo)
    assert (repo / "config" / "search.json.before-pull").read_text(encoding="utf-8") == content
    assert json.loads(cfg_path(repo).read_text(encoding="utf-8"))["search_terms"] == ["backend engineer"]


def test_pull_never_replaces_an_older_backup(tmp_path):
    repo = make_repo(tmp_path, raw="first edit")
    C.run_profile_pull(FakeStack(search=[_row()]), repo)
    cfg_path(repo).write_text("second edit", encoding="utf-8")
    C.run_profile_pull(FakeStack(search=[_row()]), repo)
    config = repo / "config"
    assert (config / "search.json.before-pull").read_text(encoding="utf-8") == "first edit"
    assert (config / "search.json.before-pull.2").read_text(encoding="utf-8") == "second edit"


def test_pull_marks_only_the_version_it_wrote_and_says_if_the_dashboard_saved_again(tmp_path):
    repo = make_repo(tmp_path)
    ts = "2026-10-12T09:30:00.123456+05:30"
    stack = FakeStack(search=[_row(updated_at=ts)])
    out = C.run_profile_pull(stack, repo)
    update = [q for q in stack.sql_log if q.startswith("UPDATE search_profile SET edited_by = 'cli'")][0]
    assert f"AND updated_at = '{ts}'::timestamptz" in update
    assert "saved again" in out, "the fake still says 'dashboard' afterwards, as it would if a newer save had landed"


def test_pull_ignores_a_version_that_is_not_a_timestamp(tmp_path):
    repo = make_repo(tmp_path)
    stack = FakeStack(search=[_row(updated_at="x'; drop table jobs; --")])
    C.run_profile_pull(stack, repo)
    update = [q for q in stack.sql_log if q.startswith("UPDATE search_profile")][0]
    assert "updated_at =" not in update and "drop table" not in update


def test_the_guarded_statement_never_updates_a_row_the_dashboard_wrote():
    plain = C.search_profile_sql(SETTINGS)
    guarded = C.search_profile_sql(SETTINGS, guard=True)
    assert "edited_by <> 'dashboard'" not in plain
    assert guarded.rstrip().endswith("WHERE search_profile.edited_by <> 'dashboard';")


class _RacingStack(FakeStack):
    """Says the profile is the CLI's at the guard check, and the dashboard's after the write."""
    def __init__(self, **kw):
        super().__init__(**kw)
        self.written = False

    def psql(self, sql, db="jobsearch"):
        if "INSERT INTO search_profile" in sql:
            self.written = True
            assert "edited_by <> 'dashboard'" in sql, "the write that can race must carry the guard"
        return super().psql(sql, db)

    def query_json(self, sql, db="jobsearch"):
        if "select edited_by from search_profile" in sql:
            return [{"edited_by": "dashboard" if self.written else "cli"}]
        return super().query_json(sql, db)


def test_apply_notices_a_dashboard_save_that_landed_after_the_check(tmp_path, capsys):
    f = tmp_path / "search.json"
    f.write_text(json.dumps(SETTINGS))
    assert C.main(["search-profile", "apply", "--file", str(f)], stack=_RacingStack()) == 2
    assert "nothing was overwritten" in capsys.readouterr().err


def test_sources_leaves_the_file_alone_when_the_dashboard_saved_in_between(tmp_path):
    repo = make_repo(tmp_path, _validated(sources=["indeed", "remotive"]))
    before = cfg_path(repo).read_text(encoding="utf-8")
    with pytest.raises(C.CheckError, match="nothing was overwritten"):
        C.run_sources(_RacingStack(), repo, "off", ["indeed"])
    assert cfg_path(repo).read_text(encoding="utf-8") == before
    assert not list((repo / "config").glob(".search.*.tmp"))


def test_force_apply_carries_no_guard(tmp_path):
    f = tmp_path / "search.json"
    f.write_text(json.dumps(SETTINGS))
    stack = FakeStack(search=[{"edited_by": "dashboard"}])
    assert C.main(["search-profile", "apply", "--file", str(f), "--force"], stack=stack) == 0
    assert not any("edited_by <> 'dashboard'" in q for q in stack.sql_log)


def test_the_doctor_still_checks_the_profile_before_migration_017():
    class NoColumn(FakeStack):
        def query_json(self, sql, db="jobsearch"):
            if "edited_by" in sql:
                raise C.StackError('column "edited_by" does not exist')
            return super().query_json(sql, db)
    ok = by_name(C.run_doctor(NoColumn(search=[{"search_terms": ["backend"], "locations": ["Pune"], "score_threshold": 60}]), REPO))
    assert ok["search profile"].status == "ok" and "search profile file" not in ok


# ------------------------------------------------------------------ push-config: a stack on another machine

class RemoteStack:
    """A stack on a server: records the files that would be written there."""
    ssh = ["ssh", "-i", "/k/key", "ubuntu@vm"]
    remote_dir = "~/jobsearch"

    def __init__(self):
        self.pushed = {}

    def put_config_file(self, name, text):
        self.pushed[name] = text


def _config_repo(tmp_path, profile=True, resume=True):
    import shutil as _sh
    cfg = tmp_path / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    if profile:
        _sh.copy(CONFIG / "profile.example.json", cfg / "profile.json")
    if resume:
        _sh.copy(CONFIG / "resume.example.json", cfg / "resume.json")
    return tmp_path


def test_push_config_on_the_machine_that_runs_the_stack_does_nothing():
    out = C.run_push_config(FakeStack(), REPO)
    assert "already where the containers read it" in out


def test_push_config_copies_both_files_after_checking_them(tmp_path):
    repo = _config_repo(tmp_path)
    stack = RemoteStack()
    out = C.run_push_config(stack, repo)
    assert set(stack.pushed) == {"profile.json", "resume.json"}
    assert json.loads(stack.pushed["resume.json"]) == json.loads((repo / "config" / "resume.json").read_text(encoding="utf-8"))
    assert "copied profile.json to ubuntu@vm:~/jobsearch/config/profile.json" in out and "no restart is needed" in out


def test_push_config_dry_run_copies_nothing(tmp_path):
    stack = RemoteStack()
    out = C.run_push_config(stack, _config_repo(tmp_path), dry_run=True)
    assert stack.pushed == {} and "would copy resume.json" in out and "no restart" not in out


def test_push_config_never_copies_a_file_that_fails_the_checks(tmp_path):
    repo = _config_repo(tmp_path)
    (repo / "config" / "resume.json").write_text("{ not json", encoding="utf-8")
    stack = RemoteStack()
    with pytest.raises(C.CheckError, match="resume.json does not pass the checks, so nothing was copied"):
        C.run_push_config(stack, repo)
    assert stack.pushed == {}, "not even the valid profile.json is copied when the other file is broken"


def test_push_config_says_which_file_is_missing_and_copies_the_other(tmp_path):
    stack = RemoteStack()
    out = C.run_push_config(stack, _config_repo(tmp_path, resume=False))
    assert set(stack.pushed) == {"profile.json"} and "skipped resume.json" in out


def test_push_config_with_neither_file_says_what_to_do(tmp_path):
    with pytest.raises(C.CheckError, match="neither config/profile.json nor config/resume.json exists"):
        C.run_push_config(RemoteStack(), _config_repo(tmp_path, profile=False, resume=False))


def test_the_remote_write_is_owner_only_atomic_and_sends_the_text_on_stdin(monkeypatch):
    seen = {}

    def fake_run(cmd, input=None, capture_output=None, text=None, timeout=None, **kw):
        seen.update(cmd=cmd, input=input)
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(C.subprocess, "run", fake_run)
    s = C.Stack(env={"JOBSEARCH_SSH": "ssh -i /k/key ubuntu@vm", "JOBSEARCH_REMOTE_DIR": "~/jobsearch"})
    s.put_config_file("profile.json", '{"a": "secret résumé text"}')
    assert seen["cmd"][:4] == ["ssh", "-i", "/k/key", "ubuntu@vm"] and len(seen["cmd"]) == 5
    remote = seen["cmd"][4]
    assert remote.startswith("umask 077 && mkdir -p ~/jobsearch/config") and "mv ~/jobsearch/config/.profile.json.tmp ~/jobsearch/config/profile.json" in remote
    assert seen["input"] == '{"a": "secret résumé text"}', "the text travels on stdin, never on the command line"
    assert "secret" not in remote


def test_a_failed_remote_write_is_a_clean_error(monkeypatch):
    monkeypatch.setattr(C.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 255, "stdout": "", "stderr": "Permission denied (publickey)."})())
    s = C.Stack(env={"JOBSEARCH_SSH": "ssh ubuntu@vm"})
    with pytest.raises(C.StackError, match="Permission denied"):
        s.put_config_file("profile.json", "{}")


def test_a_local_stack_refuses_to_push():
    with pytest.raises(C.StackError, match="runs on this machine"):
        C.Stack(env={}).put_config_file("profile.json", "{}")


# ================================================================== follow-ups (dashboard version 3b)

class FollowUpStack(FakeStack):
    def __init__(self, settings=None, due=None, missing=False, **over):
        super().__init__(**over)
        self.fu_settings = settings if settings is not None else [{"days": 7, "digest": False}]
        self.due = due if due is not None else []
        self.missing = missing

    def psql(self, sql, db="jobsearch"):
        if "follow_up_settings" in sql:
            self.sql_log.append(sql)
            if self.missing:
                raise C.StackError('relation "follow_up_settings" does not exist')
            return ""
        return super().psql(sql, db)

    def query_json(self, sql, db="jobsearch"):
        if "from follow_up_settings" in sql:
            self.sql_log.append(sql)
            if self.missing:
                raise C.StackError('relation "follow_up_settings" does not exist')
            return self.fu_settings
        if "follow_ups_due()" in sql:
            self.sql_log.append(sql)
            return self.due
        return super().query_json(sql, db)


def test_follow_up_sql_checks_its_values():
    assert C.follow_up_sql(10, None) == "UPDATE follow_up_settings SET days = 10, updated_at = now() WHERE id = 1;"
    assert C.follow_up_sql(None, True) == "UPDATE follow_up_settings SET digest = true, updated_at = now() WHERE id = 1;"
    assert "days = 3, digest = false" in C.follow_up_sql(3, False)
    for bad in (0, 91, -1, True, "7", 7.5):
        with pytest.raises(ValueError):
            C.follow_up_sql(bad, None)
    with pytest.raises(ValueError):
        C.follow_up_sql(None, "on")
    with pytest.raises(ValueError):
        C.follow_up_sql(None, None)


def test_follow_up_with_nothing_due_says_so_and_shows_the_settings():
    out = C.run_follow_up(FollowUpStack())
    assert "after 7 days" in out and "does not list what is due" in out and "Nothing is due for a follow-up today." in out
    assert "scripts/jobsearch follow-up --days N | --digest on|off" in out


def test_follow_up_lists_what_is_due_with_the_reason():
    due = [{"id": 5, "title": "Data Engineer", "company": "Beta", "reason": "date", "follow_up_on": "2026-10-10", "days_waiting": None, "contact_name": "Priya N"},
           {"id": 9, "title": "Backend", "company": "Acme", "reason": "waiting", "follow_up_on": None, "days_waiting": 1, "contact_name": None}]
    out = C.run_follow_up(FollowUpStack(due=due, settings=[{"days": 1, "digest": True}]))
    assert "after 1 day " in out and "lists what is due" in out and "2 due:" in out
    assert "#5     Data Engineer @ Beta: follow-up date 2026-10-10  (Priya N)" in out
    assert "#9     Backend @ Acme: applied 1 day ago, no reply" in out


def test_follow_up_changes_the_settings_and_then_shows_them():
    stack = FollowUpStack()
    out = C.run_follow_up(stack, days=14, digest=True)
    assert out.startswith("saved.") and any("UPDATE follow_up_settings SET days = 14, digest = true" in q for q in stack.sql_log)


def test_follow_up_refuses_a_bad_value_before_any_sql():
    stack = FollowUpStack()
    with pytest.raises(ValueError):
        C.run_follow_up(stack, days=0)
    assert not any("UPDATE" in q for q in stack.sql_log)


def test_follow_up_before_migration_019_says_what_to_do():
    out = C.run_follow_up(FollowUpStack(missing=True))
    assert "migration 019" in out and "scripts/jobsearch migrate" in out


def test_follow_up_says_nothing_was_changed_when_it_could_not_save():
    out = C.run_follow_up(FollowUpStack(missing=True), days=10)
    assert "migration 019" in out and "nothing was changed" in out


def test_follow_up_does_not_blame_the_migration_for_another_missing_thing():
    class NoDb(FollowUpStack):
        def query_json(self, sql, db="jobsearch"):
            raise C.StackError('database "jobsearch" does not exist')
    with pytest.raises(C.StackError):
        C.run_follow_up(NoDb())


def test_follow_up_is_a_command(tmp_path, capsys):
    stack = FollowUpStack()
    import unittest.mock as mock
    with mock.patch.object(C, "Stack", return_value=stack):
        assert C.main(["follow-up"]) == 0
        assert "Nothing is due" in capsys.readouterr().out
        assert C.main(["follow-up", "--days", "10", "--digest", "off"]) == 0
        assert any("days = 10, digest = false" in q for q in stack.sql_log)
        with pytest.raises(SystemExit):
            C.main(["follow-up", "--digest", "maybe"])


def test_the_module_docstring_lists_follow_up():
    assert "follow-up [--days N] [--digest on|off]" in C.__doc__


# ------------------------------------------------------------------ the optional Gmail part (Phase 6, step 6a)

def _gmail_on(**kw):
    body = {"lookback_days": 60, "from_now": False, "search_cursor": None, "token_status": "ok", "token_checked_at": None, "last_run_at": None,
            "last_error": None, "messages_seen": 14, "pending": 3, "accepted": 2, "dismissed": 1, "enabled": True, "reason": "", "configured": True,
            "lines": ["gmail:      on"]}
    return {**body, **kw}


def _features_with_gmail(flag=True):
    return {"flags": {"tailoring": True, "telegram": True, "digest": True, "gmail": flag},
            "effective": {n: {"on": True, "reason": ""} for n in ("tailoring", "telegram", "digest", "gmail")},
            "approval": "review", "approval_effective": "review", "lines": []}


def test_features_set_gmail(capsys):
    st = FakeStack()
    assert C.main(["features", "set", "--gmail", "on"], stack=st) == 0
    assert _post(st) == ("POST", "/features", {"gmail": True}), "only what was asked for is sent"
    st = FakeStack()
    assert C.main(["features", "set", "--gmail", "off", "--digest", "on"], stack=st) == 0
    assert _post(st)[2] == {"gmail": False, "digest": True}


def test_gmail_status_prints_the_report(capsys):
    st = FakeStack()
    assert C.main(["gmail"], stack=st) == 0
    out = capsys.readouterr().out
    assert "gmail:      off" in out and "the first search looks back 60 days" in out
    assert extract_call(st.py_calls[-1])[:2] == ("GET", "/mail/status")
    assert C.main(["gmail", "status"], stack=FakeStack()) == 0


@pytest.mark.parametrize("value,sent", [("30", {"lookback_days": 30, "from_now": False}), ("1", {"lookback_days": 1, "from_now": False}),
                                        ("365", {"lookback_days": 365, "from_now": False}), ("from-now", {"from_now": True}),
                                        ("FROM-NOW", {"from_now": True})])
def test_gmail_lookback_sets_how_the_first_search_starts(value, sent, capsys):
    st = FakeStack()
    assert C.main(["gmail", "lookback", value], stack=st) == 0
    assert _post(st) == ("POST", "/mail/settings", sent), "choosing a number also leaves the from-now mode"
    assert "gmail:" in capsys.readouterr().out


@pytest.mark.parametrize("argv,message", [
    (["gmail", "lookback"], "give a number of days"),
    (["gmail", "lookback", "soon"], "number of days"),
    (["gmail", "lookback", "0"], "from 1 to 365"),
    (["gmail", "lookback", "366"], "from 1 to 365"),
    (["gmail", "lookback", "12345"], "number of days"),
    (["gmail", "lookback", "\u0663\u0660"], "number of days"),          # Arabic-Indic digits are not a number here
    (["gmail", "lookback", "-5"], "number of days")])
def test_a_bad_lookback_is_refused_before_calling_the_service(argv, message, capsys):
    st = FakeStack()
    assert C.main(argv, stack=st) == 2
    assert message in capsys.readouterr().err and not any("'POST'" in c for c in getattr(st, "py_calls", []))


def test_a_lookback_change_after_the_first_search_says_it_changes_nothing_now(capsys):
    st = FakeStack(mail=_gmail_on(search_cursor="2026-10-12T10:00:00+00:00", lines=["gmail:      on"]))
    assert C.main(["gmail", "lookback", "30"], stack=st) == 0
    assert "the first search has already happened" in capsys.readouterr().out
    st = FakeStack()
    assert C.main(["gmail", "lookback", "30"], stack=st) == 0
    assert "already happened" not in capsys.readouterr().out


def test_gmail_run_is_refused_with_the_reason_while_the_part_is_not_in_effect(capsys):
    st = FakeStack()
    assert C.main(["gmail", "run"], stack=st) == 2
    assert "Gmail is not in effect: switched off" in capsys.readouterr().err
    assert not any("/schedule/mail/run" in c for c in st.py_calls), "nothing was requested"


def test_gmail_run_asks_for_a_run_at_the_next_tick_when_it_is_in_effect(capsys):
    st = FakeStack(mail=_gmail_on())
    assert C.main(["gmail", "run"], stack=st) == 0
    assert _post(st)[:2] == ("POST", "/schedule/mail/run") and "next scheduler tick" in capsys.readouterr().out


def test_gmail_errors_from_the_service_are_readable(capsys):
    st = FakeStack(service_status=400, mail={"detail": {"errors": ["the Gmail tables are not there yet: run scripts/jobsearch migrate"]}})
    assert C.main(["gmail"], stack=st) == 2
    assert "scripts/jobsearch migrate" in capsys.readouterr().err


def test_doctor_says_nothing_about_gmail_until_it_is_switched_on():
    checks = by_name(C.run_doctor(FakeStack(features=_features_with_gmail(False)), REPO))
    assert "gmail" not in checks and "gmail" in checks["optional parts"].detail
    old = _features_with_gmail()
    del old["flags"]["gmail"], old["effective"]["gmail"]
    assert "gmail" not in by_name(C.run_doctor(FakeStack(features=old), REPO)), "an older scraper without the part is not nagged"


@pytest.mark.parametrize("mail,status,text", [
    (_gmail_on(), "ok", "3 suggestions waiting for you, 14 emails looked at"),
    (_gmail_on(pending=1), "ok", "1 suggestion waiting for you"),
    (_gmail_on(enabled=False, reason="not configured: add GMAIL_CLIENT_ID to .env"), "warn", "switched on but not in effect: not configured"),
    (_gmail_on(token_status="expired"), "warn", "expired or was revoked: sign in again"),
    (_gmail_on(token_status="error", last_error="Gmail answered 503"), "warn", "the last mailbox check failed: Gmail answered 503"),
    (_gmail_on(early_stops=3, last_summary={"search": {"stopped": "Gmail is limiting requests"}}), "warn", "the last 3 mailbox runs ended early (Gmail is limiting requests)"),
    (_gmail_on(early_stops=4, last_summary={"stopped": "Gmail is unavailable"}), "warn", "the last 4 mailbox runs ended early (Gmail is unavailable)"),
    (_gmail_on(early_stops=2), "ok", "3 suggestions waiting for you"),
    (_gmail_on(unclear={"waiting": 0, "parked": 2}), "ok", "2 emails parked"),
    (_gmail_on(unclear={"waiting": 4, "parked": 1}), "ok", "1 email parked"),
    (_gmail_on(last_run_at="2026-10-13T09:30:00.123456+00:00"), "ok", "last run 2026-10-13 09:30 UTC")])
def test_doctor_checks_gmail_when_it_is_on(mail, status, text):
    checks = by_name(C.run_doctor(FakeStack(features=_features_with_gmail(), mail=mail), REPO))
    assert checks["gmail"].status == status and text in checks["gmail"].detail


def test_doctor_reports_a_gmail_status_error_as_a_warning_not_a_crash():
    st = FakeStack(features=_features_with_gmail(), mail={"detail": {"errors": ["the Gmail tables are not there yet"]}})
    st.service_status = 200      # features answer fine; only the mail call is made to fail below
    orig = st.scraper_py

    def scraper_py(code):
        if "/mail/" in code:
            return json.dumps({"status": 400, "body": st.mail_body})
        return orig(code)
    st.scraper_py = scraper_py
    checks = by_name(C.run_doctor(st, REPO))
    assert checks["gmail"].status == "warn" and "tables are not there yet" in checks["gmail"].detail


def test_the_cli_and_the_service_agree_on_the_schedule_names():
    assert "mail" in C.schedules.NAMES and C.FEATURE_NAMES[-1] == "gmail"


def _check_body(**kw):
    body = {"status": "ok", "account": "j***@gmail.com", "message": "the login works", "enabled": False, "reason": "switched off",
            "lines": ["gmail:      off, switched off", "login:      works (checked just now)"]}
    return {**body, **kw}


def test_gmail_check_says_which_account_works(capsys):
    st = FakeStack(mail=_check_body())
    assert C.main(["gmail", "check"], stack=st) == 0
    out = capsys.readouterr().out
    assert "login: works. Signed in as j***@gmail.com." in out and "gmail:      off" in out
    assert extract_call(st.py_calls[-1])[:2] == ("POST", "/mail/check")


@pytest.mark.parametrize("body,text", [
    (_check_body(status="expired", account="", message="Google no longer accepts the saved login: sign in again"), "login: EXPIRED or revoked: Google no longer accepts"),
    (_check_body(status="error", account="", message="Gmail answered HTTP 503"), "login: the check failed: Gmail answered HTTP 503")])
def test_gmail_check_reports_a_login_that_does_not_work(body, text, capsys):
    assert C.main(["gmail", "check"], stack=FakeStack(mail=body)) == 0
    assert text in capsys.readouterr().out


def test_gmail_check_without_a_login_says_what_is_missing(capsys):
    st = FakeStack(service_status=409, mail={"detail": {"errors": ["the Google login is not set up: GMAIL_CLIENT_ID missing from .env (docs/gmail.md)"]}})
    assert C.main(["gmail", "check"], stack=st) == 2
    assert "GMAIL_CLIENT_ID missing" in capsys.readouterr().err


def test_gmail_rescan_says_what_it_does_and_what_it_leaves_alone(capsys):
    body = {"lookback_days": 60, "reopened": 12, "lines": ["gmail:      on", "search:     never run; the first search looks back 60 days"], "enabled": True}
    st = FakeStack(mail=body)
    assert C.main(["gmail", "rescan"], stack=st) == 0
    out = capsys.readouterr().out
    assert "whole first-search window again (60 days)" in out and "12 email(s)" in out and "Suggestions already made are untouched" in out
    assert extract_call(st.py_calls[-1])[:2] == ("POST", "/mail/rescan")


def test_gmail_rescan_errors_are_readable(capsys):
    st = FakeStack(service_status=400, mail={"detail": {"errors": ["this database has no Gmail search yet: run scripts/jobsearch migrate"]}})
    assert C.main(["gmail", "rescan"], stack=st) == 2
    assert "scripts/jobsearch migrate" in capsys.readouterr().err


# ------------------------------------------------------------------ tailor-compare: try the tailoring on a job without saving it

COMPARE_OK = {"job_id": 24, "folder": "/output/compare/24-acme-backend", "compare_md": "/output/compare/24-acme-backend/compare.md", "cost_usd": 0.05,
              "runs": {"v2": {"status": "compared", "cost_usd": 0.05, "requirements": {"matched": 4, "bridged": 2, "gap": 1},
                              "audit": {"rejected": [{"x": 1}, {"x": 2}], "soft": []}, "needs_attention": True}}}


def test_tailor_compare_prints_the_result_and_where_to_read():
    stack = FakeStack(service_reply=COMPARE_OK)
    out = C.run_tailor_compare(stack, [24])
    assert "Trying the tailoring on 1 job(s)" in out and "saves nothing as an application" in out
    assert "job 24: v2: $0.050, 4 matched / 2 bridged / 1 gaps, 2 removed, 0 to check, needs attention" in out
    assert "read: " in out and "spent: $0.050" in out
    method, path, body = extract_call(stack.py_calls[-1])
    assert (method, path, body) == ("POST", "/tailor/compare", {"job_id": 24, "engines": ["v2"]})


def test_tailor_compare_waits_long_enough_for_a_model_run():
    seen = {}

    class Stack(FakeStack):
        def scraper_py(self, code, timeout=90):
            seen["timeout"] = timeout
            return super().scraper_py(code)
    C.run_tailor_compare(Stack(service_reply=COMPARE_OK), [24])
    assert seen["timeout"] >= 600


def test_tailor_compare_maps_the_folder_to_the_host_for_a_remote_stack():
    stack = FakeStack(service_reply=COMPARE_OK)
    stack.ssh, stack.remote_dir = ["ssh", "ubuntu@host"], "/home/ubuntu/jobsearch"
    out = C.run_tailor_compare(stack, [24])
    assert "read: /home/ubuntu/jobsearch/output/compare/24-acme-backend/compare.md" in out
    assert "scp -r ubuntu@host:/home/ubuntu/jobsearch/output/compare ." in out


def test_tailor_compare_reports_a_refusal_per_job_and_carries_on():
    calls = []

    class Stack(FakeStack):
        def scraper_py(self, code, timeout=90):
            calls.append(code)
            if len(calls) == 1:
                return json.dumps({"status": 409, "body": {"detail": {"errors": ["a tailoring run is going right now: try again in a minute (nothing was spent)"]}}})
            return json.dumps({"status": 200, "body": COMPARE_OK})
    out = C.run_tailor_compare(Stack(), [7, 24])
    assert "job 7: not compared: a tailoring run is going right now" in out and "job 24: v2: $0.050" in out and len(calls) == 2


def test_tailor_compare_shows_a_run_that_did_not_finish():
    reply = {"cost_usd": 0.0, "compare_md": None, "runs": {"v2": {"status": "budget_paused", "reason": "not enough budget left for this job"}}}
    out = C.run_tailor_compare(FakeStack(service_reply=reply), [24])
    assert "v2: budget_paused: not enough budget left for this job" in out and "read:" not in out


@pytest.mark.parametrize("ids,engines,msg", [([], None, "at least one job id"), (list(range(1, 12)), None, "at most 10 jobs"),
                                             ([1], ["v1"], "v1 was retired"), ([1], ["v3"], "v1 was retired")])
def test_tailor_compare_refuses_bad_input_before_calling_anything(ids, engines, msg):
    stack = FakeStack(service_reply=COMPARE_OK)
    with pytest.raises(C.CheckError, match=msg):
        C.run_tailor_compare(stack, ids, engines)
    assert getattr(stack, "py_calls", []) == []


def test_tailor_compare_is_a_real_subcommand_and_in_the_help_and_has_no_engine_choice():
    assert "tailor-compare N [N..]" in C.__doc__
    ns = C.build_parser().parse_args(["tailor-compare", "5", "9"])
    assert ns.cmd == "tailor-compare" and ns.job_ids == [5, 9] and not hasattr(ns, "engines")
    with pytest.raises(SystemExit):
        C.build_parser().parse_args(["tailor-compare", "5", "--engines", "v1"])


def test_tailor_compare_that_times_out_keeps_the_earlier_jobs_and_stops():
    calls = []

    class Stack(FakeStack):
        def scraper_py(self, code, timeout=90):
            calls.append(code)
            if len(calls) == 2:
                raise C.StackError("command timed out")
            return json.dumps({"status": 200, "body": COMPARE_OK})
    out = C.run_tailor_compare(Stack(), [24, 31, 40])
    assert "job 24: v2: $0.050" in out and "job 31: no answer (command timed out)" in out and "tailoring lock" in out
    assert "job 40" not in out and len(calls) == 2 and "spent: $0.050" in out                  # the third would only queue behind the second


def test_tailor_compare_does_not_pay_for_the_same_job_twice():
    stack = FakeStack(service_reply=COMPARE_OK)
    out = C.run_tailor_compare(stack, [24, 24, 24])
    assert "on 1 job(s)" in out and len(stack.py_calls) == 1 and extract_call(stack.py_calls[0])[2] == {"job_id": 24, "engines": ["v2"]}


def test_tailor_compare_gives_the_service_less_time_than_the_command_so_the_answer_comes_back():
    seen = {}

    class Stack(FakeStack):
        def scraper_py(self, code, timeout=90):
            seen["outer"], seen["inner"] = timeout, int(re.search(r"urlopen\(r, timeout=(\d+)\)", code).group(1))
            return super().scraper_py(code)
    C.run_tailor_compare(Stack(service_reply=COMPARE_OK), [24])
    assert seen["inner"] < seen["outer"] == 600


# ------------------------------------------------------------------ tailoring v2: settings flags, validate and push-config for proof_points.json

def test_tailor_settings_flags_for_v2_are_sent_and_the_display_shows_them():
    stack = FakeStack(service_reply=dict(SETTINGS_REPLY, engine="v2", gap_policy="omit", cover_style="evidence", tone="direct", reviewer=True,
                                         reviewer_model="claude-haiku-5-5", company_research=False))
    out = C.run_tailor_settings(stack)
    assert "engine:       v2" in out and "gap_policy omit, cover_style evidence, tone direct, reviewer on (claude-haiku-5-5), company_research off" in out
    old = C.run_tailor_settings(FakeStack(service_reply=SETTINGS_REPLY))                      # a reply without the new keys still prints
    assert "engine:       v2" in old and "reviewer on" in old


def test_the_parser_knows_the_v2_flags():
    ns = C.build_parser().parse_args(["tailor-settings", "set", "--engine", "v2", "--gap-policy", "bridge", "--cover-style", "classic", "--tone", "formal",
                                      "--reviewer", "off", "--reviewer-model", "claude-haiku-5-5", "--company-research", "on"])
    assert (ns.engine, ns.gap_policy, ns.cover_style, ns.tone, ns.reviewer, ns.reviewer_model, ns.company_research) == ("v2", "bridge", "classic", "formal", "off", "claude-haiku-5-5", "on")
    for bad in (["--engine", "v3"], ["--engine", "v1"], ["--tone", "rude"], ["--reviewer", "maybe"]):
        with pytest.raises(SystemExit):
            C.build_parser().parse_args(["tailor-settings", "set", *bad])


def test_setting_the_engine_posts_it_with_the_booleans_as_booleans(monkeypatch, capsys):
    stack = FakeStack(service_reply=SETTINGS_REPLY)
    assert C.main(["tailor-settings", "set", "--engine", "v2", "--reviewer", "off", "--company-research", "on"], stack=stack) == 0
    method, path, body = extract_call(next(c for c in stack.py_calls if "POST" in c and "/tailor/settings" in c))
    assert (method, path) == ("POST", "/tailor/settings") and body["engine"] == "v2" and body["reviewer"] is False and body["company_research"] is True


def test_validate_checks_proof_points_when_the_file_exists_and_stays_quiet_when_it_does_not(tmp_path):
    cfg = tmp_path
    (cfg / "resume.json").write_text(json.dumps({"basics": {"name": "A", "email": "a@b.co"}, "work": [{"id": "w1", "name": "X", "position": "Dev", "startDate": "2024-01",
                                                                                                         "highlights": [{"id": "w1-b1", "text": "Built X."}]}],
                                                 "education": [], "skills": []}))
    lines, ok = C.validate_config(cfg)
    assert not any("proof_points" in l for l in lines)
    good = {"id": "pp-1", "bullet": "w1-b1", "project": "P", "role": "built", "what": "Did X.", "metric": "", "tech": [], "evidence": ""}
    (cfg / "proof_points.json").write_text(json.dumps({"proof_points": [good]}))
    lines, _ = C.validate_config(cfg)
    assert " ✓ proof_points.json: valid (1 proof point(s))" in lines
    (cfg / "proof_points.json").write_text(json.dumps({"proof_points": [dict(good, bullet="nope"), dict(good, id="pp-2", metric="40% faster", evidence="")]}))
    lines, ok = C.validate_config(cfg)
    assert not ok and " ✗ proof_points.json:" in lines and any("must be the id of a bullet" in l for l in lines) and any("needs evidence" in l for l in lines)
    (cfg / "proof_points.json").write_text("{nope")
    lines, ok = C.validate_config(cfg)
    assert not ok and any("proof_points.json: could not be read" in l for l in lines)


def test_push_config_copies_proof_points_when_valid_and_refuses_a_broken_one():
    import tempfile, pathlib
    repo = pathlib.Path(tempfile.mkdtemp())
    (repo / "config").mkdir()
    shutil.copy(REPO / "config" / "profile.example.json", repo / "config" / "profile.json")
    shutil.copy(REPO / "config" / "resume.example.json", repo / "config" / "resume.json")
    resume = json.loads((repo / "config" / "resume.json").read_text())
    bullet = resume["work"][0]["highlights"][0]["id"]
    (repo / "config" / "proof_points.json").write_text(json.dumps({"proof_points": [{"id": "pp-1", "bullet": bullet, "project": "P", "role": "built", "what": "Did X.", "metric": "", "tech": [], "evidence": ""}]}))
    stack = FakeStack()
    stack.ssh, stack.remote_dir, stack.put = ["ssh", "u@h"], "/home/u/jobsearch", []
    stack.put_config_file = lambda name, text: stack.put.append(name)
    out = C.run_push_config(stack, repo=repo)
    assert "copied proof_points.json" in out and set(stack.put) == {"profile.json", "resume.json", "proof_points.json"}
    (repo / "config" / "proof_points.json").write_text(json.dumps({"proof_points": [{"id": "pp-1", "bullet": "nope"}]}))
    stack.put.clear()
    with pytest.raises(C.CheckError, match="proof_points.json does not pass the checks"):
        C.run_push_config(stack, repo=repo)
    assert stack.put == []
