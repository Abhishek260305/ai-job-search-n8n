"""jobsearch command-line tool (stdlib only). Launched by scripts/jobsearch.

It runs wherever this repo is checked out next to the docker-compose stack and talks to the
containers through `docker compose exec`, so nothing extra is exposed on the network. To drive
a stack on another machine, set JOBSEARCH_SSH (for example "ssh -i key ubuntu@host") and
JOBSEARCH_REMOTE_DIR (the compose directory there, default ~/jobsearch). Set JOBSEARCH_COMPOSE
if you need something other than "docker compose" (for example "sudo docker compose").

Commands:
  doctor                  health checks for the whole stack
  status                  what the pipeline has found and what needs attention
  budget [set]            Claude spend vs your caps; `budget set --monthly 10 --daily 1.5` changes them
  tailor [JOB_ID]         tailor one job (or the next eligible ones): draft, honesty checks, PDFs
  review                  applications waiting for your decision
  approve|skip|applied N  record your decision on job N (works with or without tailoring)
  mark N STAGE            set any stage by hand: interview, offer, rejected, ... (no Gmail needed)
  jobs, job N, export     browse the shortlist, one job in detail, or a CSV for a spreadsheet
  files N                 where the PDFs for job N are, and how to copy them
  features [set]          switch the optional parts on or off: tailoring, telegram, digest, gmail
  gmail [status|login|check|lookback|rescan|run]   the optional Gmail part: its state; `gmail login` is the one-time Google sign-in
                          (read-only; saves the login in .env without showing it); `gmail check` tests the saved login;
                          `gmail lookback 30|from-now` sets how the first search starts; `gmail rescan` searches that window again
                          (for mail from before an application could get a reply); `gmail run` asks for a run
  schedule [set|run|timezone|pause|resume]   when each stage runs and how much it does; pause/resume is the emergency brake
                          `schedule set collect --boards N` = how many company boards and feeds one collect run visits
  sources [show|on|off|only] [NAMES...]   which job sources are used: `sources off boards`, `sources only apify`,
                          `sources on remotive greenhouse` (names or groups: boards, feeds, jobspy, apify)
  boards find NAME...     look up a company's job-board slug (Greenhouse, Lever, Ashby, Workable, SmartRecruiters)
  parked, requeue         jobs that ran out of attempts, and putting them back in the queue
  reshortlist             promote scored jobs that are above a threshold you lowered after they were scored
  follow-up [--days N] [--digest on|off]   what is due for a follow-up (applied N days ago with no reply, or a date you set
                          on the dashboard), the days setting, and whether the Telegram digest mentions it
  tailor-settings [set]   mode, approval, score band, daily limit, cover letter, fact check
  migrate                 apply pending database migrations
  validate                validate config/profile.json, resume.json, search.json
  import-linkedin DIR     read a LinkedIn data export into clean JSON (whitelisted columns only)
  conflicts               compare config/resume.json with a LinkedIn import
  push-config             copy profile.json and resume.json to the server when the stack runs on another machine
  search-profile apply|show|pull   write config/search.json into the database / show what is stored / bring the file up
                          to date with the database (after the dashboard edited the profile)
  check                   scam + source check (and optional fit score) for one posting
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scraper"))

import profile_tools  # noqa: E402
import safety  # noqa: E402
import gmail_login  # noqa: E402
import mail  # noqa: E402
import schedules  # noqa: E402
import scoring  # noqa: E402
import sources  # noqa: E402

DEFAULT_MODEL = "claude-haiku-5-5"
ICON = {"ok": "✓", "warn": "⚠", "fail": "✗"}


class StackError(RuntimeError):
    pass


class CheckError(RuntimeError):
    pass


@dataclass
class Check:
    name: str
    status: str  # ok | warn | fail
    detail: str = ""


# ---------------------------------------------------------------- talking to the stack

class Stack:
    """Runs docker compose commands locally, or over SSH when JOBSEARCH_SSH is set."""

    def __init__(self, env=None, repo: Path = REPO):
        env = os.environ if env is None else env
        self.ssh = shlex.split(env.get("JOBSEARCH_SSH", ""))
        self.remote_dir = env.get("JOBSEARCH_REMOTE_DIR", "~/jobsearch")
        self.compose_cmd = shlex.split(env.get("JOBSEARCH_COMPOSE", "docker compose"))
        self.repo = repo

    def build(self, args: list[str]) -> list[str]:
        if self.ssh:
            remote = f"cd {self.remote_dir} && {shlex.join(self.compose_cmd)} {shlex.join(args)}"
            return self.ssh + [remote]
        return self.compose_cmd + args

    def compose(self, args: list[str], input_text: Optional[str] = None, timeout: int = 120):
        try:
            return subprocess.run(self.build(args), input=input_text, capture_output=True, text=True,
                                  timeout=timeout, cwd=None if self.ssh else self.repo)
        except FileNotFoundError as e:
            raise StackError(f"cannot run '{self.build(args)[0]}': {e}")
        except subprocess.TimeoutExpired:
            raise StackError("command timed out")

    def _ok(self, r) -> str:
        if r.returncode != 0:
            raise StackError((r.stderr or r.stdout or "command failed").strip()[:400])
        return r.stdout

    def ps(self) -> list[dict]:
        out = self._ok(self.compose(["ps", "--format", "json"]))
        out = out.strip()
        if not out:
            return []
        if out.startswith("["):
            return json.loads(out)
        return [json.loads(line) for line in out.splitlines() if line.strip().startswith("{")]

    def put_config_file(self, name: str, text: str) -> None:
        """Write config/<name> on the machine that runs the stack (over SSH; owner-only, replaced in one step). The name is
        one of CONFIG_PUSH_FILES, never user input, so it can go into the remote command as it is."""
        if not self.ssh:
            raise StackError("this stack runs on this machine: config/ is already where the containers read it")
        d = self.remote_dir
        tmp = f"{d}/config/.{name}.tmp"
        remote = f"umask 077 && mkdir -p {d}/config && cat > {tmp} && mv {tmp} {d}/config/{name}"
        try:
            r = subprocess.run(self.ssh + [remote], input=text, capture_output=True, text=True, timeout=60)
        except FileNotFoundError as e:
            raise StackError(f"cannot run '{self.ssh[0]}': {e}")
        except subprocess.TimeoutExpired:
            raise StackError("command timed out")
        self._ok(r)

    def run_repo_script(self, args: list[str], input_text: Optional[str] = None, timeout: int = 60):
        """Run `sh scripts/NAME ...` in the repository on the machine that runs the stack (over SSH when JOBSEARCH_SSH is set).
        `args` are fixed script names and option words, never user input; anything secret goes in `input_text`, which is sent on
        standard input, so it is never part of a command line (ps, shell history, the SSH command)."""
        if self.ssh:
            cmd, cwd = self.ssh + [f"cd {self.remote_dir} && sh {shlex.join(args)}"], None
        else:
            cmd, cwd = ["sh"] + args, self.repo
        try:
            return subprocess.run(cmd, input=input_text, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        except FileNotFoundError as e:
            raise StackError(f"cannot run '{cmd[0]}': {e}")
        except subprocess.TimeoutExpired:
            raise StackError("command timed out")

    def psql(self, sql: str, db: str = "jobsearch") -> str:
        cmd = f'psql -v ON_ERROR_STOP=1 -X -q -At -U "$POSTGRES_USER" -d {db}'
        return self._ok(self.compose(["exec", "-T", "postgres", "sh", "-c", cmd], input_text=sql))

    def query_json(self, sql: str, db: str = "jobsearch") -> list[dict]:
        wrapped = f"SELECT coalesce(json_agg(t), '[]'::json) FROM ({sql.rstrip().rstrip(';')}) t;"
        return json.loads(self.psql(wrapped, db).strip() or "[]")

    def scraper_py(self, code: str) -> str:
        return self._ok(self.compose(["exec", "-T", "scraper", "python", "-"], input_text=code, timeout=90))

    def exec_out(self, service: str, args: list[str]) -> str:
        return self._ok(self.compose(["exec", "-T", service] + args))

    def config_services(self) -> list[str]:
        """The services of the active compose profiles (COMPOSE_PROFILES in .env), running or not."""
        return self._ok(self.compose(["config", "--services"])).split()


# ---------------------------------------------------------------- doctor

_APIFY_SNIPPET = """
import json, httpx, apify_source as A
out = []
with httpx.Client(timeout=20) as c:
    for a in A.load_accounts():
        out.append({"account": a.actor.key, "remaining_usd": A.remaining_usd(c, a.token)})
print(json.dumps(out))
"""

_CONFIG_SNIPPET = """
import json, os, scoring, profile_tools
res = {}
path = os.environ.get("PROFILE_PATH", "/config/profile.json")
try:
    scoring.load_profile(path); res["profile"] = "ok"
except scoring.ProfileError as e:
    res["profile"] = e.problems
try:
    with open("/config/resume.json", encoding="utf-8") as f:
        profile_tools.validate_resume(json.load(f)); res["resume"] = "ok"
except FileNotFoundError:
    res["resume"] = "missing"
except (profile_tools.ProfileToolsError, json.JSONDecodeError) as e:
    res["resume"] = getattr(e, "problems", [str(e)])
print(json.dumps(res))
"""


def _fmt_problems(v) -> str:
    return "; ".join(v) if isinstance(v, list) else str(v)


def run_doctor(stack, repo: Path = REPO) -> list[Check]:
    checks: list[Check] = []
    try:
        services = {c.get("Service"): c for c in stack.ps()}
    except (StackError, json.JSONDecodeError) as e:
        return [Check("docker stack", "fail", f"cannot reach docker compose: {e}")]

    for name in ("postgres", "scraper", "n8n"):
        c = services.get(name)
        if not c or c.get("State") != "running":
            checks.append(Check(f"container {name}", "fail", "not running (docker compose up -d)"))
        elif c.get("Health") == "unhealthy":
            checks.append(Check(f"container {name}", "fail", "running but unhealthy"))
        elif c.get("Health") == "starting":
            checks.append(Check(f"container {name}", "warn", "still starting"))
        else:
            checks.append(Check(f"container {name}", "ok", c.get("Status", "running")))
    if any(c.status == "fail" for c in checks):
        return checks

    applied: set = set()
    try:
        applied = {r["version"] for r in stack.query_json("select version from schema_migrations")}
        expected = {"000_baseline"} | {p.stem for p in (repo / "postgres" / "migrations").glob("*.sql")}
        missing = sorted(expected - applied)
        if missing:
            checks.append(Check("database migrations", "fail", f"pending: {', '.join(missing)} (run: scripts/jobsearch migrate)"))
        else:
            checks.append(Check("database migrations", "ok", f"{len(applied)} applied"))
    except (StackError, json.JSONDecodeError, KeyError) as e:
        checks.append(Check("database migrations", "fail", f"cannot read schema_migrations: {e}"))

    try:
        out = stack.scraper_py("import urllib.request;print(urllib.request.urlopen('http://localhost:8000/health',timeout=5).read().decode())")
        checks.append(Check("scraper service", "ok" if '"ok":true' in out.replace(" ", "") else "fail", out.strip()))
    except StackError as e:
        checks.append(Check("scraper service", "fail", str(e)))

    try:
        out = stack.exec_out("n8n", ["wget", "-qO-", "http://localhost:5678/healthz"])
        checks.append(Check("n8n", "ok" if "ok" in out else "fail", out.strip()))
    except StackError as e:
        checks.append(Check("n8n", "fail", str(e)))

    checks.append(_dashboard_check(stack, services, applied))

    try:
        accts = json.loads(stack.scraper_py(_APIFY_SNIPPET).strip() or "[]")
        if not accts:
            checks.append(Check("apify accounts", "warn", "none configured (optional; LinkedIn via JobSpy still works)"))
        else:
            parts = []
            low = False
            for a in accts:
                r = a["remaining_usd"]
                parts.append(f"{a['account']}: " + ("balance unknown" if r is None else f"${r:.2f} left"))
                low |= r is not None and r < 0.5
            checks.append(Check("apify accounts", "warn" if low else "ok", ", ".join(parts)))
    except (StackError, json.JSONDecodeError) as e:
        checks.append(Check("apify accounts", "warn", f"could not check: {e}"))

    try:
        st_, body = _service_call(stack, "GET", "/schedule")
        if st_ == 200:
            summary = ", ".join(
                f"{r['name']} " + (f"daily {str(r['at_time'])[:5]}" if r.get("at_time") else f"every {schedules.format_minutes(r['every_minutes'])}")
                + ("" if r["enabled"] else " (off)") for r in body["schedules"])
            checks.append(Check("schedule", "ok", f"{summary}; times in {body['timezone']}"))
    except (StackError, KeyError, TypeError):
        pass
    try:
        active = {r["id"] for r in stack.query_json("select id from workflow_entity where active", db="n8n")}
        if "js-scheduler" in active:
            checks.append(Check("scheduler workflow", "ok", "published: schedules are running"))
        else:
            checks.append(Check("scheduler workflow", "warn", "'00 Scheduler' is not published, so nothing runs on schedule: "
                                                              "run scripts/n8n-import-workflows.sh --publish"))
    except (StackError, json.JSONDecodeError):
        pass

    try:
        parked = stack.query_json("select stage, count(*)::int as n from v_parked_jobs group by 1")
        if parked:
            checks.append(Check("parked jobs", "warn", ", ".join(f"{p['n']} {p['stage']}" for p in parked)
                                + " ran out of attempts and are skipped: see scripts/jobsearch parked, then requeue"))
        else:
            checks.append(Check("parked jobs", "ok", "none (no job has run out of attempts)"))
    except (StackError, json.JSONDecodeError):
        pass
    try:
        ap = stack.query_json("select * from apify_budget_status()")[0]
        detail = (f"${float(ap['month_spent']):.2f} of ${float(ap['monthly_cap']):.2f} this month, "
                  f"${float(ap['day_spent']):.2f} of ${float(ap['daily_cap']):.2f} today")
        checks.append(Check("apify spend limits", "warn" if ap["blocked"] else "ok",
                            (f"PAUSED ({ap['blocked_reason']}): " if ap["blocked"] else "") + detail))
    except (StackError, json.JSONDecodeError, IndexError, KeyError):
        pass
    try:
        stall = stack.query_json(
            "select count(*) filter (where status = 'new' and first_seen < now() - interval '3 hours')::int as waiting, "
            "coalesce(extract(epoch from now() - max(scored_at)) / 3600, 9999)::numeric(9,1) as hours_since_score from jobs")[0]
        if int(stall["waiting"]) >= 10 and float(stall["hours_since_score"]) >= 3:
            checks.append(Check("scoring progress", "warn", f"{stall['waiting']} jobs have waited over 3 hours and nothing was scored in "
                                                           f"{float(stall['hours_since_score']):.0f} h: check scripts/jobsearch budget (a cap?), "
                                                           "schedule (paused?), and the n8n executions"))
        else:
            checks.append(Check("scoring progress", "ok", "scores are being recorded"))
    except (StackError, json.JSONDecodeError, IndexError, KeyError, ValueError):
        pass
    try:
        st_, body = _service_call(stack, "GET", "/schedule")
        if st_ == 200 and body.get("paused"):
            checks.append(Check("schedules paused", "warn", "everything is paused" + (f" ({body['paused_reason']})" if body.get("paused_reason") else "")
                                + ": run scripts/jobsearch schedule resume"))
    except (StackError, KeyError, TypeError):
        pass

    tailoring_on = True
    try:
        st_, body = _service_call(stack, "GET", "/features")
        if st_ == 200:
            tailoring_on = body["effective"]["tailoring"]["on"]
            parts = [f"{n} " + ("on" if e["on"] else "off") for n, e in body["effective"].items()]
            checks.append(Check("optional parts", "ok", ", ".join(parts) + " (scoring is always on; change with `features set`)"))
            if body["flags"].get("gmail"):
                checks.append(_gmail_check(stack))
    except (StackError, KeyError, TypeError):
        pass

    try:
        res = json.loads(stack.scraper_py(_CONFIG_SNIPPET).strip())
        p = res.get("profile")
        if p == "ok":
            checks.append(Check("config/profile.json", "ok", "valid"))
        elif isinstance(p, list) and any("not found" in x for x in p):
            checks.append(Check("config/profile.json", "warn", "missing: run /setup (scoring needs it)"))
        else:
            checks.append(Check("config/profile.json", "fail", _fmt_problems(p)))
        r = res.get("resume")
        if r == "ok":
            checks.append(Check("config/resume.json", "ok", "valid"))
        elif r == "missing":
            if tailoring_on:
                checks.append(Check("config/resume.json", "warn", "missing: run /setup (tailoring needs it)"))
            else:
                checks.append(Check("config/resume.json", "ok", "not needed: tailoring is off"))
        else:
            checks.append(Check("config/resume.json", "fail", _fmt_problems(r)))
    except (StackError, json.JSONDecodeError) as e:
        checks.append(Check("config files", "warn", f"could not check: {e}"))

    try:
        st = stack.query_json("select * from budget_status()")[0]
        month, cap = float(st["month_spent"]), float(st["monthly_cap"])
        detail = f"${month:.2f} of ${cap:.2f} this month, ${float(st['day_spent']):.2f} of ${float(st['daily_cap']):.2f} today"
        if st["blocked"]:
            checks.append(Check("spend limits", "warn", f"PAUSED ({st['blocked_reason']}): {detail}"))
        elif st["warn"]:
            checks.append(Check("spend limits", "warn", f"over {st['warn_pct']}% of the monthly cap: {detail}"))
        else:
            checks.append(Check("spend limits", "ok", detail))
    except (StackError, json.JSONDecodeError, IndexError, KeyError) as e:
        checks.append(Check("spend limits", "warn", f"could not read the budget (run: scripts/jobsearch migrate): {e}"))

    try:
        names = {r["name"] for r in stack.query_json("select name from credentials_entity", db="n8n")}
        if "Anthropic API key" in names:
            checks.append(Check("n8n credential", "ok", "'Anthropic API key' exists" + ("" if "Telegram bot" in names else " (no 'Telegram bot' credential: fine if you do not use Telegram)")))
        else:
            checks.append(Check("n8n credential", "warn", "'Anthropic API key' not found: scoring cannot run (see docs/workflows.md)"))
    except (StackError, json.JSONDecodeError) as e:
        checks.append(Check("n8n credential", "warn", f"could not check: {e}"))

    try:
        rows = stack.query_json("select search_terms, locations, score_threshold from search_profile where name = 'default'")
        if not rows:
            checks.append(Check("search profile", "warn", "no 'default' row: run /setup"))
        elif any("CHANGE ME" in x for x in rows[0]["search_terms"] + rows[0]["locations"]):
            checks.append(Check("search profile", "warn", "still has placeholder values: run /setup or search-profile apply"))
        else:
            checks.append(Check("search profile", "ok", f"{len(rows[0]['search_terms'])} search terms, threshold {rows[0]['score_threshold']}"))
            if profile_written_by(stack) == "dashboard":      # its own query: before migration 017 there is no such column
                checks.append(Check("search profile file", "warn", "the dashboard edited the profile, so config/search.json is behind it: "
                                                                  "run scripts/jobsearch search-profile pull before editing the file"))
    except (StackError, json.JSONDecodeError) as e:
        checks.append(Check("search profile", "warn", f"could not check: {e}"))

    checks += _source_checks(stack, repo, "013_boards" in applied)
    return checks


_DASHBOARD_HEALTH = ("import urllib.request;"
                     "print(urllib.request.urlopen('http://127.0.0.1:8080/api/health',timeout=5).read().decode())")


def _dashboard_check(stack, services: dict, applied: set) -> Check:
    """The web dashboard is optional: not enabled is fine, enabled but not working is a warning, and a port
    published beyond 127.0.0.1 is a failure. `applied` is empty when the migrations could not be read."""
    c = services.get("dashboard")
    running = bool(c) and c.get("State") == "running"
    if not running:
        try:
            enabled = "dashboard" in stack.config_services()
        except StackError as e:
            return Check("dashboard", "warn", f"could not check: {e}")
        if not enabled:
            return Check("dashboard", "ok", "not enabled (optional, see docs/dashboard.md)")
    if applied and "014_dashboard" not in applied:
        return Check("dashboard", "warn", "enabled, but migration 014 is not applied: scripts/jobsearch migrate, "
                                          "then docker compose up -d")
    if not running:
        return Check("dashboard", "warn", "enabled but not running: docker compose up -d --build "
                                          "(if it stops again: docker compose logs dashboard-db-init dashboard)")
    published = [p for p in c.get("Publishers") or [] if p.get("PublishedPort")]
    exposed = [p for p in published if p.get("URL") not in ("127.0.0.1", "::1")]
    if exposed:
        return Check("dashboard", "fail", f"published on {exposed[0].get('URL')}:{exposed[0]['PublishedPort']}, "
                                          "not on 127.0.0.1 only: fix its ports line in docker-compose.yml")
    if not published:
        return Check("dashboard", "warn", "running, but no port is published, so the tunnel cannot reach it: "
                                          "check its ports line in docker-compose.yml")
    try:
        health = json.loads(stack.exec_out("dashboard", ["python", "-c", _DASHBOARD_HEALTH]))
    except (StackError, json.JSONDecodeError) as e:
        return Check("dashboard", "warn", f"running, but /api/health did not answer: {e}")
    if isinstance(health, dict) and health.get("db") is True:
        return Check("dashboard", "ok", f"ok on 127.0.0.1:{published[0]['PublishedPort']}")
    return Check("dashboard", "warn", "running, but it cannot reach the database as jobsearch_dashboard: is migration "
                                      "014 applied, and did dashboard-db-init run with the current DASHBOARD_DB_PASSWORD? "
                                      "(docker compose up -d --force-recreate dashboard-db-init dashboard)")


def _slugs(n: int) -> str:
    return f"{n} slug" + ("" if n == 1 else "s")


def _source_checks(stack, repo: Path, has_fetch_state: bool) -> list[Check]:
    """What is switched on, whether the switches and the board slugs agree, and which feeds keep failing.
    A config that uses none of the Phase 7 sources gets one ok line and nothing else."""
    out: list[Check] = []
    try:
        cfg = profile_tools.validate_search(_load_json(repo / "config" / "search.json"))
    except FileNotFoundError:
        return out                                          # the search profile check above already says "run /setup"
    except UnicodeDecodeError:                              # a ValueError, but not a JSONDecodeError: say what is wrong
        return [Check("sources", "warn", "cannot read config/search.json (not valid UTF-8)")]
    except (profile_tools.ProfileToolsError, json.JSONDecodeError, OSError) as e:
        return [Check("sources", "warn", f"could not read config/search.json ({e}): see scripts/jobsearch validate")]
    on, boards = cfg["sources"], cfg["boards"]
    problems: list[str] = []
    for system in sources.BOARD_SYSTEMS:
        n = len(boards.get(system) or [])
        if n and system not in on:
            problems.append(f"{system} has {_slugs(n)} but is switched off: scripts/jobsearch sources on {system}")
        elif system in on and not n:
            problems.append(f"{system} is switched on but has no slugs: add companies under \"boards\" in config/search.json "
                            f"(find one: scripts/jobsearch boards find NAME) or switch it off: scripts/jobsearch sources off {system}")
    out.append(Check("sources", "warn" if problems else "ok", "; ".join([f"on: {', '.join(on)}"] + problems)))
    if has_fetch_state:                                     # fetch_state exists from migration 013
        try:
            for r in stack.query_json("select source, key, failures from fetch_state where failures >= 5 order by failures desc"):
                if r["source"] not in on:                   # switched off since: not today's problem
                    continue
                if r.get("key") and r["key"] not in {e if isinstance(e, str) else e.get("slug") for e in boards.get(r["source"]) or []}:
                    continue                                # a slug that is no longer configured
                who = f"{r['source']}/{r['key']}" if r.get("key") else r["source"]
                out.append(Check(f"source {who}", "warn", f"{who} failed {r['failures']} runs in a row: "
                                 + ("check the slug" if r.get("key") else "the feed may be down or have changed")))
        except (StackError, json.JSONDecodeError, KeyError, TypeError):
            pass
    return out


def print_checks(checks: list[Check]) -> int:
    width = max(len(c.name) for c in checks)
    for c in checks:
        print(f" {ICON[c.status]} {c.name.ljust(width)}  {c.detail}")
    fails = sum(c.status == "fail" for c in checks)
    warns = sum(c.status == "warn" for c in checks)
    print(f"\n{len(checks) - fails - warns} ok, {warns} warning(s), {fails} failure(s)")
    return 1 if fails else 0


# ---------------------------------------------------------------- status

def run_status(stack) -> str:
    lines: list[str] = []
    counts = stack.query_json("select status, count(*)::int as n from jobs group by status order by n desc")
    new24 = stack.query_json("select count(*)::int as n from jobs where first_seen > now() - interval '24 hours'")[0]["n"]
    total = sum(c["n"] for c in counts)
    lines.append(f"Jobs: {total} total, {new24} first seen in the last 24h")
    if counts:
        lines.append("  by status: " + ", ".join(f"{c['status']} {c['n']}" for c in counts))
    scam = stack.query_json("select scam_risk, count(*)::int as n from jobs where scam_risk is not null group by 1 order by 1")
    if scam:
        lines.append("  safety:    " + ", ".join(f"{s['scam_risk']} {s['n']}" for s in scam))

    top = stack.query_json(
        "select id, title, company, location, score, verdict, scam_risk, deadline, closing_soon, stale "
        "from v_shortlist order by score desc nulls last, closing_soon desc limit 8")
    lines.append("")
    if top:
        lines.append("Top matches:")
        for j in top:
            marks = ("🔥" if j["closing_soon"] else "") + ("⚠stale" if j["stale"] else "") \
                + ("⚠medium-risk" if j["scam_risk"] == "medium" else "")
            lines.append(f"  #{j['id']:<5} {str(j['score']).rjust(3)} {j['verdict']:<9} {j['title']} | {j['company']} | "
                         f"{j['location'] or '-'} {marks}".rstrip())
    else:
        lines.append("Top matches: none yet (jobs appear here once scored above your threshold)")

    try:
        st = stack.query_json("select * from budget_status()")[0]
        lines.append("")
        lines.append(f"Claude spend: ${float(st['month_spent']):.2f} of ${float(st['monthly_cap']):.2f} this month"
                     f" (${float(st['day_spent']):.2f} of ${float(st['daily_cap']):.2f} today)"
                     + (f"  \u26A0 PAUSED: {st['blocked_reason']}" if st["blocked"] else ""))
    except (StackError, json.JSONDecodeError, IndexError, KeyError):
        pass

    try:
        ap = stack.query_json("select * from apify_budget_status()")[0]
        lines.append(f"Apify spend: ${float(ap['month_spent']):.2f} of ${float(ap['monthly_cap']):.2f} this month (${float(ap['day_spent']):.2f} of ${float(ap['daily_cap']):.2f} today)"
                     + (f"  \u26A0 PAUSED: {ap['blocked_reason']}" if ap["blocked"] else ""))
        parked = stack.query_json("select count(*)::int as n from v_parked_jobs")[0]["n"]
        if parked:
            lines.append(f"\u26A0 {parked} parked job(s) ran out of attempts: scripts/jobsearch parked")
    except (StackError, json.JSONDecodeError, IndexError, KeyError):
        pass

    try:
        st_, body = _service_call(stack, "GET", "/features")
        if st_ == 200:
            lines.append("")
            lines.append("Optional parts: " + ", ".join(f"{n} " + ("on" if e["on"] else "off") for n, e in body["effective"].items())
                         + "   (change: scripts/jobsearch features set ...)")
    except (StackError, KeyError, TypeError):
        pass

    apps = stack.query_json("select status, count(*)::int as n from applications group by status order by n desc")
    lines.append("")
    lines.append("Applications: " + (", ".join(f"{a['status']} {a['n']}" for a in apps) if apps else "none yet"))

    try:
        accts = json.loads(stack.scraper_py(_APIFY_SNIPPET).strip() or "[]")
        if accts:
            lines.append("Apify credit: " + ", ".join(
                f"{a['account']} " + ("?" if a["remaining_usd"] is None else f"${a['remaining_usd']:.2f}") for a in accts))
    except (StackError, json.JSONDecodeError):
        pass
    return "\n".join(lines)


# ---------------------------------------------------------------- budget

def run_budget(stack) -> str:
    st = stack.query_json("select * from budget_status()")[0]
    month, cap, day, dcap = (float(st[k]) for k in ("month_spent", "monthly_cap", "day_spent", "daily_cap"))
    bar_n = 0 if cap <= 0 else min(20, round(20 * month / cap))
    lines = [f"Claude spend this month: ${month:.2f} of ${cap:.2f}  [{'#' * bar_n}{'.' * (20 - bar_n)}]",
             f"Today:                   ${day:.2f} of ${dcap:.2f}",
             f"Remaining:               ${float(st['month_remaining']):.2f} this month, ${float(st['day_remaining']):.2f} today"]
    if st["blocked"]:
        lines.append(f"STATUS: PAUSED, {st['blocked_reason']}. Scoring claims nothing until the window resets or you raise the cap.")
    elif st["warn"]:
        lines.append(f"STATUS: over {st['warn_pct']}% of the monthly cap.")
    else:
        lines.append("STATUS: ok")
    rows = stack.query_json(
        "select provider, purpose, model, sum(calls)::int as calls, sum(input_tokens)::bigint as input_tokens, "
        "sum(output_tokens)::bigint as output_tokens, sum(cache_read_tokens)::bigint as cache_read_tokens, "
        "round(sum(cost_usd), 4) as cost_usd from v_spend_month group by 1, 2, 3 order by 8 desc")
    if rows:
        lines += ["", "This month so far:", f"  {'provider':<10} {'purpose':<8} {'model':<18} {'calls':>6} {'in tok':>9} {'out tok':>8} {'cached':>9} {'cost':>9}"]
        for r in rows:
            lines.append(f"  {r['provider']:<10} {r['purpose']:<8} {str(r['model'] or '-')[:18]:<18} {r['calls']:>6} "
                         f"{r['input_tokens']:>9} {r['output_tokens']:>8} {r['cache_read_tokens']:>9} ${float(r['cost_usd']):>8.4f}")
        lines.append("  (Apify rows count toward the separate Apify caps below, not the Claude caps.)")
    else:
        lines += ["", "No usage logged this month yet."]
    try:
        ap = stack.query_json("select * from apify_budget_status()")[0]
        lines += ["", f"Apify spend this month:   ${float(ap['month_spent']):.2f} of ${float(ap['monthly_cap']):.2f}   today ${float(ap['day_spent']):.2f} of ${float(ap['daily_cap']):.2f}"
                  + (f"   PAUSED: {ap['blocked_reason']}, LinkedIn-via-Apify searches are skipped until the window resets" if ap["blocked"] else "   ok")]
    except (StackError, json.JSONDecodeError, IndexError, KeyError):
        pass
    return "\n".join(lines)


def budget_set_sql(monthly=None, daily=None, warn_pct=None, apify_monthly=None, apify_daily=None) -> str:
    """UPDATE for budget_settings. Values are validated numbers, never raw text."""
    sets = []
    for name, v, lo, hi in (("monthly_cap_usd", monthly, 0, 100000), ("daily_cap_usd", daily, 0, 100000),
                            ("apify_monthly_cap_usd", apify_monthly, 0, 100000), ("apify_daily_cap_usd", apify_daily, 0, 100000)):
        if v is not None:
            if not isinstance(v, (int, float)) or isinstance(v, bool) or not lo <= v <= hi:
                raise ValueError(f"{name} must be a number between {lo} and {hi}")
            sets.append(f"{name} = {float(v)!r}")
    if warn_pct is not None:
        if not isinstance(warn_pct, int) or isinstance(warn_pct, bool) or not 1 <= warn_pct <= 100:
            raise ValueError("warn-pct must be a whole number between 1 and 100")
        sets.append(f"warn_pct = {warn_pct}")
    if not sets:
        raise ValueError("give at least one of --monthly, --daily, --warn-pct, --apify-monthly, --apify-daily")
    return "UPDATE budget_settings SET " + ", ".join(sets) + ", updated_at = now() WHERE id = 1;"


# ---------------------------------------------------------------- tailoring and review

def _service_call(stack, method: str, path: str, body: Optional[dict] = None, timeout: int = 900) -> tuple[int, dict]:
    """Call the scraper service from inside its container (nothing is exposed on the network)."""
    code = ("import json, urllib.request, urllib.error\n"
            f"body = {json.dumps(json.dumps(body))} if {body is not None!r} else None\n"
            f"r = urllib.request.Request('http://localhost:8000{path}', body.encode() if body else None, "
            f"{{'Content-Type': 'application/json'}}, method={method!r})\n"
            "try:\n    resp = urllib.request.urlopen(r, timeout=" + str(timeout) + "); print(json.dumps({'status': resp.status, 'body': json.load(resp)}))\n"
            "except urllib.error.HTTPError as e:\n    print(json.dumps({'status': e.code, 'body': json.load(e)}))\n")
    out = json.loads(stack.scraper_py(code).strip().splitlines()[-1])
    return out["status"], out["body"]


def _errors(body: dict) -> str:
    d = body.get("detail", body)
    return "; ".join(d.get("errors", [str(d)])) if isinstance(d, dict) else str(d)


def run_tailor(stack, job_id: Optional[int], limit: Optional[int]) -> str:
    """limit None means "the tailor schedule's batch size" (scripts/jobsearch schedule)."""
    payload = {k: v for k, v in (("job_id", job_id), ("limit", limit)) if v is not None}
    status, body = _service_call(stack, "POST", "/tailor/run", payload)
    if status != 200:
        raise CheckError(_errors(body))
    if body.get("skipped"):
        return f"Nothing tailored: {body['skipped']}"
    lines = []
    for r in body.get("results", []):
        if r["status"] in ("failed", "api_error", "budget_paused"):
            lines.append(f"job {r['job_id']}: {r['status']}: {r.get('reason')}")
            continue
        c = r.get("requirements") or {}
        lines.append(f"job {r['job_id']}: {r['status']} ({r['variant']}), {c.get('matched', 0)} matched / {c.get('bridged', 0)} bridged / "
                     f"{c.get('gap', 0)} gaps, cost ${r['cost_usd']:.3f}" + ("  \u26A0 needs attention" if r.get("needs_attention") else ""))
        lines += [f"    \u26A0 {a}" for a in r.get("attention", [])]
        lines.append(f"    files: {r['files_dir']}")
    if not lines:
        lines.append("No eligible jobs (shortlisted, above the minimum score, not yet tailored).")
    lines.append(f"Telegram: {body.get('telegram')}" + (f", {body['notified']} review message(s) sent" if body.get("notified") else ""))
    return "\n".join(lines)


def run_review(stack) -> str:
    rows = stack.query_json("select * from v_review_queue order by created_at desc")
    if not rows:
        return "Nothing is waiting for your review."
    lines = [f"{len(rows)} waiting for your decision:"]
    for r in rows:
        lines.append(f"  #{r['job_id']:<5} {r['score']:>3} {r['variant']:<8} {r['title']} @ {r['company']}" + ("  \u26A0 needs attention" if r["needs_attention"] else ""))
        if r.get("attention_reason"):
            lines.append(f"         {r['attention_reason']}")
    lines.append("approve:  scripts/jobsearch approve N     skip:  scripts/jobsearch skip N     files:  scripts/jobsearch files N")
    lines.append("later stages by hand:  scripts/jobsearch mark N interview|offer|rejected|...  (no Gmail needed)")
    return "\n".join(lines)


def review_sql(job_id: int, action: str, note: Optional[str]) -> str:
    """SELECT review_application(...). The id is validated and the note travels in a random dollar-quote."""
    if not isinstance(job_id, int) or isinstance(job_id, bool) or job_id <= 0:
        raise ValueError("job id must be a positive whole number")
    if action not in ("approve", "skip", "applied"):
        raise ValueError("action must be approve, skip or applied")
    if note is None:
        return f"SELECT review_application({job_id}, '{action}');"
    tag = "n" + secrets.token_hex(6)
    while f"${tag}$" in note:
        tag = "n" + secrets.token_hex(6)
    return f"SELECT review_application({job_id}, '{action}', ${tag}${note}${tag}$);"


def run_files(stack, job_id: int) -> str:
    rows = stack.query_json(f"select files_dir, status, needs_attention, attention_reason from applications where job_id = {int(job_id)}")
    if not rows:
        raise CheckError(f"job {job_id} has no application yet (try: scripts/jobsearch tailor {job_id})")
    d = rows[0]["files_dir"]
    if not d:
        raise CheckError(f"job {job_id} has no files: its decision was recorded by hand (tailor it with: scripts/jobsearch tailor {job_id})")
    ssh = getattr(stack, "ssh", [])
    base = stack.remote_dir if ssh else str(getattr(stack, "repo", REPO))     # remote stack, or this checkout
    host = d.replace("/output/", f"{base}/output/", 1) if d and d.startswith("/output/") else d
    lines = [f"status: {rows[0]['status']}" + ("  \u26A0 " + rows[0]["attention_reason"] if rows[0]["needs_attention"] else ""),
             f"folder (on the host): {host}",
             "contents: resume.pdf, cover_letter.pdf, summary.md (coverage, changes, what was removed), patch.json, audit.json, posting.txt"]
    if ssh:
        target = ssh[-1]
        lines.append(f"copy to this machine:  scp -r {target}:{host} .")
    return "\n".join(lines)


def run_tailor_settings(stack) -> str:
    st, body = _service_call(stack, "GET", "/tailor/settings")
    if st != 200:
        raise CheckError(_errors(body))
    b = body
    return "\n".join([
        f"mode:         {b['mode']}   (auto tailors shortlisted jobs by itself; manual waits for `tailor N`)",
        f"approval:     {b['approval']}   (review asks you on Telegram; cli asks only at the command line; auto stores them ready)",
        f"min_score:    {b['min_score'] if b['min_score'] is not None else 'your search threshold'}",
        f"max_score:    {b['max_score'] if b['max_score'] is not None else 'none'}   (at or above it the base resume is used as is)",
        f"daily_limit:  {b['daily_limit'] if b['daily_limit'] is not None else 'none'}",
        f"cover_letter: {'on' if b['cover_letter'] else 'off'}   max_pages: {b['max_pages']}   paper: {b.get('paper', 'a4')}   model: {b['model']}",
        f"style_notes:  {b.get('style_notes') or 'none'}   (tone and wording preferences; never lets the writing add facts)",
        f"fact check:   {'on' if b.get('verify', True) else 'off'} ({b.get('verify_model')})"])


# ---------------------------------------------------------------- optional parts, browsing, manual stages

FEATURE_NAMES = ("tailoring", "telegram", "digest", "gmail")
JOB_STATUSES = ("new", "filtered_out", "scam_blocked", "scored", "shortlisted", "tailored", "approved", "applied", "skipped", "expired")
MARK_STAGES = ("approved", "skipped", "applied", "in_progress", "interview", "offer", "rejected", "withdrawn")


def run_features(stack, changes: Optional[dict] = None) -> str:
    """Show (and optionally change) which optional parts are on. Scoring is the core and is always on."""
    if changes:
        st, body = _service_call(stack, "POST", "/features", changes)
    else:
        st, body = _service_call(stack, "GET", "/features")
    if st != 200:
        raise CheckError(_errors(body))
    return "\n".join(body["lines"])


def run_gmail(stack, action: str = "status", value: Optional[str] = None, **opts) -> str:
    """The optional Gmail part. `status` shows it; `login` is the one-time Google sign-in; `check` tests the saved login;
    `lookback 30` or `lookback from-now` sets how the FIRST search starts; `run` asks for a run at the next scheduler tick
    (refused, with the reason, while the part is not in effect)."""
    if action == "login":
        try:
            return gmail_login.run_login(stack, **opts)
        except gmail_login.LoginError as e:
            raise CheckError(str(e))
    if action == "rescan":
        st, body = _service_call(stack, "POST", "/mail/rescan")
        if st != 200:
            raise CheckError(_errors(body))
        return (f"The next search covers the whole first-search window again ({body['lookback_days']} days), and {body['reopened']} email(s) that were read "
                "but not acted on will be looked at again. Suggestions already made are untouched.\n\n" + "\n".join(body["lines"]))
    if action == "check":
        st, body = _service_call(stack, "POST", "/mail/check")
        if st != 200:
            raise CheckError(_errors(body))
        head = {"ok": f"login: works. Signed in as {body.get('account') or '(unknown)'}.",
                "expired": f"login: EXPIRED or revoked: {body['message']}",
                "error": f"login: the check failed: {body['message']}"}.get(body["status"], f"login: {body['status']}: {body['message']}")
        return "\n".join([head, ""] + body["lines"])
    if action == "lookback":
        if value is None:
            raise ValueError("give a number of days (1 to 365) or from-now, for example: gmail lookback 30")
        if value.lower() in ("from-now", "from_now", "now"):
            changes = {"from_now": True}
        elif re.fullmatch(r"[0-9]{1,4}", value):
            changes = {"lookback_days": int(value), "from_now": False}
        else:
            raise ValueError("lookback is a number of days (1 to 365) or from-now")
        mail.validate_settings(changes)
        st, body = _service_call(stack, "POST", "/mail/settings", changes)
        note = ("\nnote: the first search has already happened, so this changes nothing now: later searches continue from where the last one stopped."
                if st == 200 and body.get("search_cursor") else "")
    elif action == "run":
        st, body = _service_call(stack, "GET", "/mail/status")
        if st == 200 and not body["enabled"]:
            raise CheckError(f"Gmail is not in effect: {body['reason']}")
        if st == 200:
            st, body = _service_call(stack, "POST", "/schedule/mail/run")
            if st == 200:
                return "mail: run requested. It starts at the next scheduler tick (within 5 minutes)."
        note = ""
    else:
        st, body = _service_call(stack, "GET", "/mail/status")
        note = ""
    if st != 200:
        raise CheckError(_errors(body))
    return "\n".join(body["lines"]) + note


def _gmail_check(stack) -> "Check":
    """The doctor's line for the optional Gmail part. Only asked for when the switch is on."""
    st, body = _service_call(stack, "GET", "/mail/status")
    if st != 200:
        return Check("gmail", "warn", _errors(body))
    if not body["enabled"]:
        return Check("gmail", "warn", f"switched on but not in effect: {body['reason']}")
    if body["token_status"] == "expired":
        return Check("gmail", "warn", "the Google login expired or was revoked: sign in again with scripts/jobsearch gmail login")
    if body["token_status"] == "error":
        return Check("gmail", "warn", f"the last mailbox check failed: {body.get('last_error') or 'unknown error'}")
    early = body.get("early_stops") or 0
    if early >= 3:
        summary = body.get("last_summary") or {}
        why = summary.get("stopped") or next((summary[p]["stopped"] for p in ("search", "classify") if isinstance(summary.get(p), dict) and summary[p].get("stopped")), "")
        return Check("gmail", "warn", f"the last {early} mailbox runs ended early" + (f" ({why})" if why else "") + ": check `scripts/jobsearch gmail` and the scraper's log")
    waiting = body["pending"]
    unclear = body.get("unclear") or {}
    parked = f"; {unclear['parked']} email{'s' if unclear['parked'] != 1 else ''} parked (the model could not read them: `gmail rescan` reopens them)" if unclear.get("parked") else ""
    ran = "" if not body.get("last_run_at") else "; last run " + body["last_run_at"][:16].replace("T", " ") + " UTC"
    return Check("gmail", "ok", f"on; {waiting} suggestion{'s' if waiting != 1 else ''} waiting for you, {body['messages_seen']} emails looked at{ran}{parked}")


def _lit(text: str) -> str:
    """A SQL string literal for arbitrary text: dollar-quoted with a random tag, so nothing can break out."""
    tag = "q" + secrets.token_hex(6)
    while f"${tag}$" in text:
        tag = "q" + secrets.token_hex(6)
    return f"${tag}${text}${tag}$"


def jobs_sql(status: Optional[str] = "shortlisted", min_score: Optional[int] = None, days: Optional[int] = None,
             search: Optional[str] = None, limit: int = 25) -> str:
    """The query behind `jobs` and `export`. Every value is validated or quoted."""
    conds = []
    if status and status != "all":
        if status not in JOB_STATUSES:
            raise ValueError(f"status must be one of: all, {', '.join(JOB_STATUSES)}")
        conds.append(f"j.status = '{status}'")
    if min_score is not None:
        if not isinstance(min_score, int) or isinstance(min_score, bool) or not 0 <= min_score <= 100:
            raise ValueError("min score must be a whole number from 0 to 100")
        conds.append(f"j.score >= {min_score}")
    if days is not None:
        if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 3650:
            raise ValueError("days must be a whole number from 1 to 3650")
        conds.append(f"j.first_seen >= now() - interval '{days} days'")
    if search:
        conds.append(f"(j.title ILIKE '%' || {_lit(search)} || '%' OR j.company ILIKE '%' || {_lit(search)} || '%')")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 10000:
        raise ValueError("limit must be a whole number from 1 to 10000")
    where = ("WHERE " + " AND ".join(conds)) if conds else ""
    return ("SELECT j.id, j.score, j.verdict, j.status, j.title, j.company, j.location, j.canonical_url AS url, j.scam_risk, "
            "j.posted_at::date AS posted, j.first_seen::date AS first_seen, a.status AS application, a.review_note AS note "
            f"FROM jobs j LEFT JOIN applications a ON a.job_id = j.id {where} "
            f"ORDER BY j.score DESC NULLS LAST, j.id DESC LIMIT {limit}")


def run_jobs(stack, **filters) -> str:
    rows = stack.query_json(jobs_sql(**filters))
    if not rows:
        return "No jobs match. (Jobs appear here once they pass the filters and are scored; try `jobs --status all`.)"
    lines = [f"{len(rows)} job(s):"]
    for r in rows:
        score = "-" if r["score"] is None else str(r["score"])
        marks = ("  ⚠ " + r["scam_risk"] + " risk" if r.get("scam_risk") in ("medium", "high") else "") \
            + (f"  [{r['application']}]" if r.get("application") else "")
        lines.append(f"  #{r['id']:<5} {score:>3} {(r.get('verdict') or ''):<9} {r['title']} | {r['company']} | {r.get('location') or '-'}{marks}")
        if r.get("url"):
            lines.append(f"         {r['url']}")
    lines.append("details: scripts/jobsearch job N      decide: approve N | skip N | applied N | mark N interview")
    return "\n".join(lines)


def run_job(stack, job_id: int) -> str:
    if not isinstance(job_id, int) or isinstance(job_id, bool) or job_id <= 0:
        raise ValueError("job id must be a positive whole number")
    rows = stack.query_json(
        "SELECT j.id, j.title, j.company, j.location, j.canonical_url AS url, j.status, j.score, j.verdict, j.scam_risk, "
        "j.scam_flags, j.strengths, j.gaps, j.gates, j.deadline, j.posted_at::date AS posted, a.status AS application, "
        "a.variant, a.applied_at::date AS applied_on, a.review_note AS note, a.files_dir, a.needs_attention, a.attention_reason "
        f"FROM jobs j LEFT JOIN applications a ON a.job_id = j.id WHERE j.id = {job_id}")
    if not rows:
        raise CheckError(f"no job {job_id}")
    r = rows[0]
    lines = [f"#{r['id']}  {r['title']} @ {r['company']}  ({r.get('location') or 'no location'})",
             f"link:      {r.get('url') or '-'}",
             f"status:    {r['status']}" + (f"   score {r['score']} {r.get('verdict') or ''}" if r.get("score") is not None else "   (not scored)")
             + (f"   posted {r['posted']}" if r.get("posted") else "") + (f"   closes {r['deadline']}" if r.get("deadline") else "")]
    risk = r.get("scam_risk")
    if risk:
        flags = ", ".join(f.get("code", "?") for f in (r.get("scam_flags") or []) if isinstance(f, dict))
        lines.append(f"safety:    {risk}" + (f" ({flags})" if flags else ""))
    for g, v in (r.get("gates") or {}).items():
        if isinstance(v, dict) and v.get("verdict") not in (None, "PASS"):
            lines.append(f"gate:      {g} {v['verdict']}" + (f": {v['note']}" if v.get("note") else ""))
    lines += [f"  + {x}" for x in (r.get("strengths") or [])[:4]] + [f"  - {x}" for x in (r.get("gaps") or [])[:4]]
    if r.get("application"):
        lines.append(f"decision:  {r['application']} ({r['variant']} resume)" + (f", applied {r['applied_on']}" if r.get("applied_on") else "")
                     + (f"; note: {r['note']}" if r.get("note") else ""))
        if r.get("needs_attention"):
            lines.append(f"           ⚠ needs attention: {r.get('attention_reason')}")
        if r.get("files_dir"):
            lines.append(f"files:     scripts/jobsearch files {job_id}")
    else:
        lines.append("decision:  none yet   (approve | skip | applied | mark N interview)")
    return "\n".join(lines)


def mark_sql(job_id: int, stage: str, note: Optional[str]) -> str:
    """SELECT set_application_status(...): the by-hand way to track an application (no Gmail needed)."""
    if not isinstance(job_id, int) or isinstance(job_id, bool) or job_id <= 0:
        raise ValueError("job id must be a positive whole number")
    stage = stage.lower().replace("-", "_")
    if stage not in MARK_STAGES:
        raise ValueError(f"stage must be one of: {', '.join(MARK_STAGES)}")
    return f"SELECT set_application_status({job_id}, '{stage}'" + (f", {_lit(note)}" if note else "") + ");"


def export_csv(stack, **filters) -> str:
    import csv
    import io
    rows = stack.query_json(jobs_sql(**filters))
    cols = ["id", "score", "verdict", "status", "application", "title", "company", "location", "url", "scam_risk", "posted", "first_seen", "note"]
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(cols)
    for r in rows:
        # a spreadsheet would run a cell starting with = + - @ as a formula; job titles come from the internet
        w.writerow([("'" + str(v)) if isinstance(v, str) and v[:1] in ("=", "+", "-", "@") else ("" if v is None else v)
                    for v in (r.get(c) for c in cols)])
    return buf.getvalue()


# ---------------------------------------------------------------- parked jobs

def run_parked(stack) -> str:
    rows = stack.query_json("select stage, job_id, title, company, claims, strikes from v_parked_jobs order by stage, job_id")
    if not rows:
        return "No parked jobs: nothing has run out of attempts."
    lines = [f"{len(rows)} parked job(s) (they ran out of attempts and are skipped until you put them back):"]
    for r in rows:
        lines.append(f"  {r['stage']:<7} #{r['job_id']:<5} {r['title']} @ {r['company']}   (attempts {r['claims']}, bad replies {r['strikes']})")
    lines.append("put them back:  scripts/jobsearch requeue score|tailor [JOB_ID]")
    return "\n".join(lines)


def requeue_sql(stage: str, job_id: Optional[int]) -> str:
    if stage not in ("score", "tailor"):
        raise ValueError("stage must be score or tailor")
    if job_id is not None and (not isinstance(job_id, int) or isinstance(job_id, bool) or job_id <= 0):
        raise ValueError("job id must be a positive whole number")
    return f"SELECT requeue_jobs('{stage}'" + (f", {job_id}" if job_id is not None else "") + ") AS n;"


# ---------------------------------------------------------------- re-shortlisting

def run_reshortlist(stack, apply: bool = False) -> str:
    """Jobs scored before the threshold was lowered stay 'scored' for good. List them; with apply, shortlist them."""
    rows = stack.query_json("select id, score, title, company from v_reshortlist order by score desc, id")
    if not rows:
        return "Nothing to do: no scored job is at or above the current threshold without being on the shortlist."
    lines = [f"{len(rows)} job(s) scored at or above the current threshold but not on the shortlist:"]
    for r in rows:
        lines.append(f"  #{r['id']:<5} {r['score']:>3}  {r['title']} @ {r['company']}")
    if not apply:
        lines.append("shortlist them (the next digest sends them):  scripts/jobsearch reshortlist --apply")
        return "\n".join(lines)
    done = stack.query_json("SELECT reshortlist() AS n;")
    lines.append(f"{done[0]['n'] if done else 0} job(s) shortlisted.")
    return "\n".join(lines)


# ---------------------------------------------------------------- follow-ups (dashboard version 3b)

def follow_up_sql(days: Optional[int], digest: Optional[bool]) -> str:
    """The one UPDATE behind `follow-up --days N --digest on|off`. The values are checked here and never pasted as text."""
    sets = []
    if days is not None:
        if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 90:
            raise ValueError("days must be a whole number from 1 to 90")
        sets.append(f"days = {days}")
    if digest is not None:
        if not isinstance(digest, bool):
            raise ValueError("digest must be on or off")
        sets.append(f"digest = {'true' if digest else 'false'}")
    if not sets:
        raise ValueError("give --days N and/or --digest on|off")
    return "UPDATE follow_up_settings SET " + ", ".join(sets) + ", updated_at = now() WHERE id = 1;"


def run_follow_up(stack, days: Optional[int] = None, digest: Optional[bool] = None) -> str:
    """Show the reminders and their two settings; optionally change them."""
    prefix = ""
    try:
        if days is not None or digest is not None:
            stack.psql(follow_up_sql(days, digest))
            prefix = "saved.\n\n"
        settings = stack.query_json("select days, digest from follow_up_settings where id = 1")
        due = stack.query_json(
            "select j.id, j.title, j.company, d.reason, d.follow_up_on, d.days_waiting, t.contact_name "
            "from follow_ups_due() d join jobs j on j.id = d.job_id left join job_tracking t on t.job_id = d.job_id "
            "order by (d.reason = 'date') desc, d.follow_up_on, d.applied_on, j.id")
    except StackError as e:
        # only a missing follow-up table or function means "migration 019 not applied" (not a missing database or role)
        if re.search(r'(relation|function) "?(follow_up_settings|follow_ups_due|job_tracking)', str(e)) and "does not exist" in str(e):
            saved = "nothing was changed. " if (days is not None or digest is not None) else ""
            return f"follow-up reminders need database migration 019, {saved}run  scripts/jobsearch migrate"
        raise
    if not settings:
        return "follow-up settings are missing from the database: run  scripts/jobsearch migrate"
    n, on = settings[0]["days"], bool(settings[0]["digest"])
    lines = [prefix + f"An application with no reply is due for a follow-up after {n} day{'s' if n != 1 else ''} "
             f"(a date you set on the dashboard wins over this).",
             f"The daily Telegram message {'lists' if on else 'does not list'} what is due.",
             "change:  scripts/jobsearch follow-up --days N | --digest on|off", ""]
    if not due:
        lines.append("Nothing is due for a follow-up today.")
    else:
        lines.append(f"{len(due)} due:")
        for r in due:
            why = (f"follow-up date {r['follow_up_on']}" if r["reason"] == "date"
                   else f"applied {r['days_waiting']} day{'s' if r['days_waiting'] != 1 else ''} ago, no reply")
            who = f"  ({r['contact_name']})" if r.get("contact_name") else ""
            lines.append(f"  #{r['id']:<5} {r['title']} @ {r['company']}: {why}{who}")
    return "\n".join(lines)


# ---------------------------------------------------------------- schedules

def run_schedule(stack, name: Optional[str] = None, changes: Optional[dict] = None, run: bool = False,
                 timezone: Optional[str] = None, pause: Optional[str] = None, resume: bool = False) -> str:
    """Show the schedules; optionally change one, request a run, change the timezone, or pause/resume everything."""
    prefix = ""
    if pause is not None:
        st, body = _service_call(stack, "POST", "/schedule/pause", {"reason": pause or None})
        prefix = "ALL SCHEDULES PAUSED: nothing runs on its own until you run: scripts/jobsearch schedule resume\n\n"
    elif resume:
        st, body = _service_call(stack, "POST", "/schedule/resume")
        prefix = "resumed: each stage runs again at its next turn.\n\n"
    elif timezone:
        st, body = _service_call(stack, "POST", "/schedule/timezone", {"timezone": timezone})
        prefix = f"timezone set to {timezone} (this also moves the spending-cap day boundaries).\n\n"
    elif run:
        st, body = _service_call(stack, "POST", f"/schedule/{name}/run")
        if st != 200:
            raise CheckError(_errors(body))
        prefix = f"{name}: run requested. It starts at the next scheduler tick (within 5 minutes).\n\n"
        st, body = _service_call(stack, "GET", "/schedule")
    elif changes:
        st, body = _service_call(stack, "POST", f"/schedule/{name}", changes)
        prefix = f"{name} updated. It applies from the next scheduler tick (within 5 minutes).\n\n"
    else:
        st, body = _service_call(stack, "GET", "/schedule")
    if st != 200:
        raise CheckError(_errors(body))
    lines = body["lines"]
    return prefix + "\n".join(lines + ["", f"times are in {body['timezone']}.  change: schedule set NAME --every 6h | --at 08:00 | --batch N | --off | --on (collect also: --boards N);  run now: schedule run NAME"])


def schedule_changes(args) -> dict:
    """Turn `schedule set` options into the request body (validated again by the service)."""
    changes: dict = {}
    if args.every is not None:
        changes["every_minutes"] = schedules.parse_duration(args.every)
    if args.at is not None:
        changes["at_time"] = schedules.parse_time(args.at)
    if args.batch is not None:
        changes["batch"] = args.batch
    if getattr(args, "boards", None) is not None:
        changes["board_batch"] = args.boards
    if args.model is not None:
        changes["model"] = args.model
    if args.enable:
        changes["enabled"] = True
    if args.disable:
        changes["enabled"] = False
    return changes


# ---------------------------------------------------------------- config files -> the machine that runs the stack

# The two personal files the scraper container reads from its ./config mount. search.json is not here: it goes into the
# database with `search-profile apply` (and the dashboard's Search page), the container never reads the file.
CONFIG_PUSH_FILES = ("profile.json", "resume.json")


def run_push_config(stack, repo: Path = REPO, dry_run: bool = False) -> str:
    """Copy config/profile.json and config/resume.json to the machine that runs the stack, for when you edit them on one
    computer and the stack runs on another (a server). Each is checked first, so a broken file is never copied."""
    if not getattr(stack, "ssh", None):
        return "The stack runs on this machine, so config/ is already where the containers read it: nothing to copy."
    lines: list[str] = []
    todo: list[tuple[str, str]] = []
    for name in CONFIG_PUSH_FILES:
        path = repo / "config" / name
        if not path.is_file():
            lines.append(f"skipped {name}: {path} does not exist yet (run /setup, or copy config/{name.replace('.json', '.example.json')})")
            continue
        text = path.read_text(encoding="utf-8")
        try:
            data = json.loads(text)
            (profile_tools.validate_resume if name == "resume.json" else scoring.validate_profile)(data)
        except (ValueError, profile_tools.ProfileToolsError, scoring.ProfileError) as e:
            raise CheckError(f"{name} does not pass the checks, so nothing was copied: {e} (run `scripts/jobsearch validate`)")
        todo.append((name, text))
    if not todo:
        raise CheckError("nothing to copy: neither config/profile.json nor config/resume.json exists yet")
    where = f"{stack.ssh[-1]}:{stack.remote_dir}/config"
    for name, text in todo:
        if dry_run:
            lines.append(f"would copy {name} to {where}/{name}")
        else:
            stack.put_config_file(name, text)
            lines.append(f"copied {name} to {where}/{name} (readable by its owner only)")
    if not dry_run:
        lines.append("The scraper reads them on its next scoring or tailoring run: no restart is needed.")
    return "\n".join(lines)


# ---------------------------------------------------------------- search profile -> database

def search_profile_sql(settings: dict, name: str = "default", guard: bool = False) -> str:
    """UPDATE-or-INSERT SQL for search_profile. The JSON travels as one dollar-quoted literal
    with a random tag, so no value in it can break out of the statement."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", name):
        raise ValueError("profile name may only contain letters, digits, '_' and '-'")
    payload = json.dumps(settings, ensure_ascii=False)
    tag = "cfg" + secrets.token_hex(6)
    while f"${tag}$" in payload:
        tag = "cfg" + secrets.token_hex(6)
    arr = lambda key: f"ARRAY(SELECT jsonb_array_elements_text(j->'{key}'))"  # noqa: E731
    # `guard`: never overwrite a profile the dashboard wrote, even if it did so after the check that came before this
    # statement (the statement then changes nothing, and the caller finds out by reading edited_by again)
    where = "\n  WHERE search_profile.edited_by <> 'dashboard'" if guard else ""
    return f"""WITH c AS (SELECT ${tag}${payload}${tag}$::jsonb AS j)
INSERT INTO search_profile (name, search_terms, locations, sources, allow_remote, min_yoe, max_yoe,
                            include_keywords, exclude_keywords, exclude_companies, max_age_days,
                            score_threshold, boards, board_title_words, edited_by, updated_at)
SELECT '{name}', {arr('search_terms')}, {arr('locations')}, {arr('sources')},
       (j->>'allow_remote')::boolean, (j->>'min_yoe')::int, (j->>'max_yoe')::int,
       {arr('include_keywords')}, {arr('exclude_keywords')}, {arr('exclude_companies')},
       (j->>'max_age_days')::int, (j->>'score_threshold')::int,
       coalesce(j->'boards', '{{}}'::jsonb), {arr('board_title_words')}, 'cli', now()
FROM c
ON CONFLICT (name) DO UPDATE SET
  search_terms = EXCLUDED.search_terms, locations = EXCLUDED.locations, sources = EXCLUDED.sources,
  allow_remote = EXCLUDED.allow_remote, min_yoe = EXCLUDED.min_yoe, max_yoe = EXCLUDED.max_yoe,
  include_keywords = EXCLUDED.include_keywords, exclude_keywords = EXCLUDED.exclude_keywords,
  exclude_companies = EXCLUDED.exclude_companies, max_age_days = EXCLUDED.max_age_days,
  score_threshold = EXCLUDED.score_threshold, boards = EXCLUDED.boards,
  board_title_words = EXCLUDED.board_title_words, edited_by = 'cli', updated_at = now(){where};
"""


# The search profile can also be edited in the dashboard (version 2c), which writes the database and cannot write
# config/search.json. `edited_by` (migration 017) says who wrote last. Applying the file over a dashboard edit would
# silently discard it, so the commands that write the whole file refuse until `search-profile pull` has brought it up to date.

def profile_written_by(stack, name: str = "default") -> Optional[str]:
    """'cli' or 'dashboard'; None when there is no such profile or the column does not exist yet (migration 017)."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", name):
        return None
    try:
        rows = stack.query_json(f"select edited_by from search_profile where name = '{name}'")
        return rows[0].get("edited_by") if rows else None
    except (StackError, json.JSONDecodeError, IndexError, KeyError, TypeError, AttributeError):
        return None


def guard_dashboard_edits(stack, name: str = "default", force: bool = False, can_force: bool = True) -> None:
    if force or profile_written_by(stack, name) != "dashboard":
        return
    raise CheckError(
        "the search profile was last edited in the dashboard, so config/search.json is behind it and writing the file "
        "would discard those edits. Run `scripts/jobsearch search-profile pull` first: it brings the file up to date, "
        "and then you can edit and apply as usual" + (" (or add --force to discard the dashboard's edits)." if can_force else "."))


def assert_not_overwritten(stack, name: str = "default") -> None:
    """After a guarded write: if the dashboard saved between the check and the write, the write changed nothing."""
    if profile_written_by(stack, name) == "dashboard":
        raise CheckError("the dashboard saved the search profile while this ran, so nothing was overwritten. "
                         "Run `scripts/jobsearch search-profile pull` and try again.")


def run_profile_pull(stack, repo: Path = REPO, name: str = "default", file: Optional[str] = None) -> str:
    """Write the database's search profile into config/search.json, so the file matches what the dashboard saved. Keys the
    file has besides the profile (its "_comment") and the order of its keys are kept. The file is replaced in one step, and
    only after that is the profile marked as written by the CLI again, so a failure in between can only be repeated."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", name):
        raise ValueError("profile name may only contain letters, digits, '_' and '-'")
    rows = stack.query_json(f"select * from search_profile where name = '{name}'")
    if not rows:
        raise CheckError(f"there is no search profile '{name}' in the database yet: run `search-profile apply` first")
    try:
        settings = profile_tools.validate_search(profile_tools.settings_from_row(rows[0]))
    except profile_tools.ProfileToolsError as e:
        raise CheckError(f"the stored profile does not pass the checks, so nothing was written: {e}")
    path = Path(file) if file else repo / "config" / "search.json"
    existing: dict = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            existing = loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError):
            existing = {}                                   # an unreadable file is replaced: its content is what we are fixing
    merged = {**existing, **settings}                       # the file's own order first, new keys last
    new_text = json.dumps(merged, indent=2, ensure_ascii=False) + "\n"
    # Whatever the file holds now, if it is not what is about to be written, is kept first: it may have been edited after the
    # dashboard saved (a broken file, a half-finished edit, an empty file), and replacing it would lose that without a trace.
    backup: Optional[Path] = None
    if path.is_file() and path.read_bytes() != new_text.encode("utf-8"):
        backup = path.with_name(path.name + ".before-pull")
        n = 1
        while backup.exists():                              # never replace an older backup
            n += 1
            backup = path.with_name(f"{path.name}.before-pull.{n}")
        backup.write_bytes(path.read_bytes())
    try:
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".search.", suffix=".tmp")
    except OSError as e:
        raise CheckError(f"cannot write in {path.parent} ({e.strerror or e}); nothing was changed")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new_text)
        if path.is_file():
            shutil.copymode(path, tmp)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()
    # Only the version that was just written into the file is marked as pulled: a dashboard save that landed after the
    # read above has a newer updated_at, stays 'dashboard', and the next `apply` still refuses to overwrite it.
    version = rows[0].get("updated_at")
    same_version = f" AND updated_at = '{version}'::timestamptz" if isinstance(version, str) and re.fullmatch(r"[0-9T:.+\- Z]{20,40}", version) else ""
    stack.psql(f"UPDATE search_profile SET edited_by = 'cli' WHERE name = '{name}' AND edited_by = 'dashboard'{same_version};")
    summary = (f"{path.name} now matches the database: {len(settings['search_terms'])} term(s), {len(settings['locations'])} location(s), "
               f"{len(settings['sources'])} source(s), {sum(len(v) for v in settings['boards'].values())} company board(s), "
               f"threshold {settings['score_threshold']}.")
    if backup is not None:
        summary += f"\nThe file said something different before: it is kept as {backup.name}, in case you edited it after the dashboard saved."
    if same_version and profile_written_by(stack, name) == "dashboard":
        summary += "\nThe dashboard saved again while this ran: run `search-profile pull` once more."
    return summary


# ---------------------------------------------------------------- sources: which places jobs come from

# Apify results are stored with source 'linkedin' (apify_source._job), the same value JobSpy's LinkedIn uses, so job_sources
# has no 'linkedin_apify' rows and the two cannot be told apart by that column.
APIFY_STORED_AS = "linkedin"
SOURCE_ACTIONS = ("show", "on", "off", "only")
_NAME_HINT = "name at least one source or group (boards, feeds, jobspy, apify)"


def change_sources(current: list[str], action: str, names: list[str]) -> list[str]:
    """The new `sources` list. `on` adds, `off` removes only what is named, `only` keeps exactly what is named. Names are
    stripped and lower-cased and may be groups (boards, feeds, jobspy, apify). Result: no duplicates, in sources.ALL order.
    Raises ValueError for an unknown name or action, for no names, and for a result with nothing on."""
    if action not in ("on", "off", "only"):
        raise ValueError(f"action must be on, off or only (not '{action}')")
    wanted_names = [n.strip().lower() for n in names if n and n.strip()]
    if not wanted_names:
        raise ValueError(_NAME_HINT)
    wanted = set(sources.expand(wanted_names))
    have = set(current)
    result = have | wanted if action == "on" else have - wanted if action == "off" else wanted
    ordered = [name for name in sources.ALL if name in result]
    if not ordered:
        raise ValueError("at least one source must stay on (a collect run with no sources does nothing)")
    return ordered


def sources_report(enabled: list[str], boards: dict, counts: dict, board_batch: Optional[int] = None) -> list[str]:
    """One line per source in registry order: on/off, name, company count (board systems), jobs stored, cost note, what it is.
    `board_batch` (when known) adds a last line with how many boards and feeds one collect run visits."""
    width = max(len(n) for n in sources.ALL)
    lines = []
    for name in sources.ALL:
        companies = ""
        if name in sources.BOARD_SYSTEMS:
            n = len(boards.get(name) or [])
            companies = f"({n} {'company' if n == 1 else 'companies'})"
        if name == "linkedin_apify" and name not in counts:
            jobs = f"{'in ' + APIFY_STORED_AS:>11}"                  # stored under 'linkedin': see APIFY_STORED_AS
        else:
            jobs = f"{int(counts.get(name, 0)):>6} jobs"
        paid = "(paid: uses Apify credit)   " if name in sources.PAID else ""
        lines.append(f"{'on' if name in enabled else 'off':<3}  {name:<{width}}  {companies:<14}  {jobs}   {paid}{sources.DESCRIPTIONS[name]}")
    if board_batch is not None:
        lines.append(f"boards per run: {board_batch}")
    return lines


def _read_search_config(repo: Path) -> tuple[Path, dict]:
    path = repo / "config" / "search.json"
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise CheckError("config/search.json does not exist yet: run /setup first (or copy config/search.example.json to config/search.json)")
    try:
        cfg = json.loads(raw)
    except json.JSONDecodeError as e:
        raise CheckError(f"config/search.json is not valid JSON ({e}): fix it, then try again")
    if not isinstance(cfg, dict):
        raise CheckError("config/search.json must be a JSON object")
    return path, cfg


def _source_counts(stack) -> dict:
    """Jobs stored per source. A failing query or an empty table means zeros, never an error."""
    try:
        return {str(r["source"]): int(r["n"]) for r in stack.query_json("select source, count(*) as n from job_sources group by source")}
    except (StackError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return {}


def _board_batch(stack) -> Optional[int]:
    """The collect schedule's board_batch, or None when it cannot be read (migration 013 not applied yet)."""
    try:
        return int(stack.query_json("select board_batch from schedule_settings where name = 'collect'")[0]["board_batch"])
    except (StackError, json.JSONDecodeError, IndexError, KeyError, TypeError, ValueError):
        return None


def run_sources(stack, repo: Path = REPO, action: str = "show", names=()) -> str:
    """Show which sources are on, or change them (on / off / only). A change goes to the database and to config/search.json
    together, in an order that cannot leave them disagreeing: validate everything, write the new file next to the old one,
    update the database, and only then swap the file in. If the database step fails the old file is untouched.
    The database gets the WHOLE config/search.json (the search profile), not only the sources. A symlinked
    config/search.json is replaced by a regular file when the new one is swapped in (the link itself is not followed)."""
    if action == "show" and names:
        raise ValueError("show takes no names: use on, off or only to change sources")
    path, cfg = _read_search_config(repo)
    try:
        current = profile_tools.validate_search(cfg)
    except profile_tools.ProfileToolsError as e:
        raise ValueError(f"config/search.json has problems, so sources cannot be changed safely until they are fixed: {e}")
    notes: list[str] = []
    if action != "show":
        new_sources = change_sources(current["sources"], action, list(names))
        if set(new_sources) == set(current["sources"]):
            notes.append("no change: those sources are already " + ("on" if action == "on" else "off" if action == "off" else "the only ones on") + ".")
        else:
            if (all(name in sources.BOARD_SYSTEMS for name in new_sources)
                    and not any(current["boards"].get(system) for system in sources.BOARD_SYSTEMS)):
                # only company boards would be on and none lists a company: every collect run would do nothing (feeds and the
                # job-site scrapers need no slugs, so they count as "another source")
                raise ValueError("boards has no company slugs yet: add some under \"boards\" in config/search.json, "
                                 "or use `boards find`")
            new_cfg = dict(cfg)
            new_cfg["sources"] = new_sources                      # keeps the key's place; a new key goes last
            try:
                settings = profile_tools.validate_search(new_cfg)
            except profile_tools.ProfileToolsError as e:
                raise ValueError(f"config/search.json would be invalid after this change, so nothing was written: {e}")
            sql = search_profile_sql(settings, guard=True)
            try:
                fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".search.", suffix=".tmp")
            except OSError as e:                                  # a read-only config directory: nothing has been sent anywhere yet
                raise CheckError(f"cannot write in config/ ({e.strerror or e}); nothing was changed")
            tmp = Path(tmp_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(json.dumps(new_cfg, indent=2, ensure_ascii=False) + "\n")
                shutil.copymode(path, tmp)
                guard_dashboard_edits(stack, can_force=False)      # the file's whole profile is about to be written: not over a dashboard edit
                try:
                    stack.psql(sql)
                except Exception as e:                            # the database is the step that can fail: keep the file as it was
                    raise CheckError(f"the database update failed, so config/search.json was not changed: {e}")
                assert_not_overwritten(stack)                     # a dashboard save in between: nothing was written, the file stays
                try:
                    os.replace(tmp, path)
                except OSError as e:
                    raise CheckError(f"config/search.json could not be written ({e}), but the database already has the new sources; "
                                     "make config/search.json writable and run the same `sources` command again")
            finally:
                if tmp.exists():
                    tmp.unlink()
            was_on, cfg, current = current["sources"], new_cfg, settings
            notes.append("sources updated in the database and in config/search.json: " + ", ".join(new_sources)
                         + "\ntakes effect on the next collect run (no restart needed).")
            notes.append("applied config/search.json to the database (the whole file, not only sources)")
            if "linkedin_apify" in new_sources and "linkedin_apify" not in was_on:
                notes.append("linkedin_apify uses your Apify credit (spend caps apply: scripts/jobsearch budget).")
            for system in sources.BOARD_SYSTEMS:
                if system in new_sources and not current["boards"].get(system):
                    notes.append(f"{system} is on but lists no companies yet: add slugs under \"boards\" in config/search.json "
                                 f"(find one with: scripts/jobsearch boards find NAME).")
    if action == "show" and profile_written_by(stack) == "dashboard":
        notes.append("note: the dashboard edited the search profile since config/search.json was written, so the list below is "
                     "the file's, not the database's. Run `scripts/jobsearch search-profile pull` to bring the file up to date.")
    lines = sources_report(current["sources"], current["boards"], _source_counts(stack), _board_batch(stack))
    help_ = ["", f"(Apify jobs are stored as '{APIFY_STORED_AS}', so they are counted on the {APIFY_STORED_AS} line.)",
             "change: scripts/jobsearch sources off boards | off feeds | only apify | on remotive greenhouse   (groups: boards, feeds, jobspy, apify)",
             "how many boards and feeds one run visits: scripts/jobsearch schedule set collect --boards N"]
    return "\n".join(notes + ([""] if notes else []) + lines + help_)


# ---------------------------------------------------------------- boards find: which job board does a company use?

# the probing itself lives in scraper/board_probe.py (shared with the dashboard's "find this company")
import board_probe  # noqa: E402
from board_probe import (  # noqa: E402,F401
    PROBE_MAX_BYTES, PROBE_TIMEOUT, PROBE_USER_AGENT, _NoRedirect, _probe_jobs, _probe_open, slug_candidates,
)


def boards_find(name: str, opener=None) -> list[dict]:
    """The shared probe, with this module's `_probe_open` as the default opener (looked up at call time)."""
    return board_probe.boards_find(name, opener or _probe_open)


def run_boards_find(name: str, opener=None) -> str:
    matches = boards_find(name, opener)
    if not matches:
        return "\n".join([
            f"No match for '{name}' on Greenhouse, Lever, Ashby, Workable or SmartRecruiters (tried: {', '.join(slug_candidates(name))}).",
            "",
            "The slug is the company's name in its job-board address. Open the company's careers page, click a job, and look at the address:",
            "  boards.greenhouse.io/<slug>   or   job-boards.greenhouse.io/<slug>",
            "  jobs.lever.co/<slug>",
            "  jobs.ashbyhq.com/<slug>",
            "  apply.workable.com/<slug>",
            "  careers.smartrecruiters.com/<slug>   (the slug is case-sensitive)",
            "then run this again with the slug as the name, or add it to config/search.json by hand.",
            "Big companies with their own career site, or on Workday, are not covered: there is no free open feed for those."])
    lines = [f"Found {len(matches)} possible match(es) for '{name}':"]
    lines += [f"  {m['system']:<16} {m['slug']:<24} {m['jobs']} open job(s)" for m in matches]
    lines += ["", "Check that it is the right company (open its careers page), then add it to config/search.json:"]
    systems: dict = {}
    for m in matches:
        systems.setdefault(m["system"], []).append(m["slug"])
    lines.append(f'  "boards": {json.dumps(systems)}')                # ONE key: several lines would not paste as valid JSON
    lines += ["(if config/search.json already has a \"boards\" section, add the slug to that system's list instead)", "",
              "and make sure the system is switched on:"]
    lines.append(f"  scripts/jobsearch sources on {' '.join(systems)}")
    return "\n".join(lines)


# ---------------------------------------------------------------- check one posting

def call_claude(system: str, user: str, *, api_key: str, model: str, max_tokens: int = 900,
                opener=urllib.request.urlopen, timeout: int = 60) -> str:
    # thinking is off: scoring is a short structured task, and some models think by default (extra cost)
    body = json.dumps({"model": model, "max_tokens": max_tokens, "system": system, "thinking": {"type": "disabled"},
                       "messages": [{"role": "user", "content": user}]}).encode()
    req = urllib.request.Request("https://api.anthropic.com/v1/messages", body, {
        "content-type": "application/json", "x-api-key": api_key, "anthropic-version": "2023-06-01"})
    try:
        data = json.load(opener(req, timeout=timeout))
    except urllib.error.HTTPError as e:
        raise CheckError(f"Anthropic API returned HTTP {e.code}: {e.read()[:300].decode('utf-8', 'replace')}")
    except urllib.error.URLError as e:
        raise CheckError(f"could not reach the Anthropic API: {e.reason}")
    return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")


def check_posting(*, url: Optional[str] = None, text: str = "", company: str = "", title: str = "",
                  score: bool = False, force: bool = False, profile_path: Optional[str] = None,
                  api_key: Optional[str] = None, model: str = DEFAULT_MODEL, opener=urllib.request.urlopen) -> dict:
    """Safety-check a posting. The URL is only classified, never fetched."""
    job = {"title": title, "company": company, "url": url, "apply_url": None, "description": text}
    result: dict = {"safety": safety.assess(job, require_company=bool(company)), "score": None}
    if not score:
        return result
    if result["safety"]["risk"] == "high" and not force:
        raise CheckError("risk is HIGH, so no model call was made (use --force to score anyway)")
    if not api_key:
        raise CheckError("set ANTHROPIC_API_KEY in your environment to use --score")
    if not text.strip():
        raise CheckError("--score needs the posting text (--text FILE or --text -)")
    try:
        profile = scoring.load_profile(profile_path or str(REPO / "config" / "profile.json"))
    except scoring.ProfileError as e:
        raise CheckError(f"profile problem: {e}")
    prompt = scoring.prepare(profile, job)
    reply = call_claude(prompt["system"], prompt["user"], api_key=api_key, model=model,
                        max_tokens=prompt["max_tokens"], opener=opener)
    try:
        result["score"] = scoring.finalize(reply)
    except scoring.ScoreError as e:
        raise CheckError("the model's reply was not valid: " + "; ".join(e.problems))
    return result


def render_check(res: dict) -> str:
    s = res["safety"]
    icon = {"low": "✓", "medium": "⚠", "high": "✗"}[s["risk"]]
    lines = [f"{icon} Safety: {s['risk'].upper()} (score {s['score']})"]
    for h, info in s["hosts"].items():
        if info.get("host"):
            lines.append(f"    {h}: {info['host']}: {info['category']} ({info['reason']})")
    for f in s["flags"]:
        lines.append(f"    [{f['severity']}] {f['code']}: {f['detail']}")
    if not s["flags"]:
        lines.append("    no red flags found (this is a heuristic, not a guarantee)")
    r = res.get("score")
    if r:
        lines.append("")
        lines.append(f"Fit: {r['score']}/100, {r['verdict']}"
                     + (" (above your threshold)" if r["above_threshold"] else ""))
        lines.append("    " + ", ".join(f"{k} {v}" for k, v in r["scores"].items()))
        for g, v in r["gates"].items():
            lines.append(f"    gate {g}: {v['verdict']}" + (f" ({v['note']})" if v["note"] else ""))
        for label, key in (("Strengths", "strengths"), ("Gaps", "gaps")):
            for item in r[key]:
                lines.append(f"    {label[:-1] if label.endswith('s') else label}: {item}")
        lines.append(f"    {r['score_reason']}")
    return "\n".join(lines)


# ---------------------------------------------------------------- validate

def validate_config(config_dir: Path) -> tuple[list[str], bool]:
    """Validate whichever of profile/resume/search exist. Returns (report lines, all_valid)."""
    lines, ok = [], True
    for fname, fn in (("profile.json", scoring.validate_profile), ("resume.json", profile_tools.validate_resume),
                      ("search.json", profile_tools.validate_search)):
        path = config_dir / fname
        if not path.is_file():
            lines.append(f" ⚠ {fname}: not found (run /setup)")
            continue
        try:
            with open(path, encoding="utf-8") as f:
                fn(json.load(f))
            lines.append(f" ✓ {fname}: valid")
        except json.JSONDecodeError as e:
            ok = False
            lines.append(f" ✗ {fname}: not valid JSON ({e})")
        except (scoring.ProfileError, profile_tools.ProfileToolsError) as e:
            ok = False
            lines.append(f" ✗ {fname}:")
            lines += [f"      - {p}" for p in e.problems]
    return lines, ok


# ---------------------------------------------------------------- CLI

def _load_json(path: Path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="jobsearch", description="Manage and inspect your job-search pipeline.")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("doctor", help="health checks for the whole stack")
    sub.add_parser("status", help="what the pipeline has found and what needs attention")
    sub.add_parser("migrate", help="apply pending database migrations")

    b = sub.add_parser("budget", help="Claude spend vs your caps (and `budget set` to change them)")
    b.add_argument("action", nargs="?", choices=["show", "set"], default="show")
    b.add_argument("--monthly", type=float, help="monthly cap in USD")
    b.add_argument("--daily", type=float, help="daily cap in USD")
    b.add_argument("--warn-pct", type=int, help="warn when this percent of the monthly cap is used")
    b.add_argument("--apify-monthly", type=float, help="monthly cap on Apify spend in USD (default 15)")
    b.add_argument("--apify-daily", type=float, help="daily cap on Apify spend in USD (default 0.50)")

    v = sub.add_parser("validate", help="validate config/profile.json, resume.json, search.json")
    v.add_argument("--config-dir", default=str(REPO / "config"))

    li = sub.add_parser("import-linkedin", help="read a LinkedIn data export folder into JSON")
    li.add_argument("folder")
    li.add_argument("-o", "--output")

    c = sub.add_parser("conflicts", help="compare resume.json with a LinkedIn import")
    c.add_argument("--resume", default=str(REPO / "config" / "resume.json"))
    g = c.add_mutually_exclusive_group(required=True)
    g.add_argument("--linkedin-json")
    g.add_argument("--linkedin-folder")

    t = sub.add_parser("tailor", help="tailor one job, or the next eligible ones")
    t.add_argument("job_id", nargs="?", type=int)
    t.add_argument("--limit", type=int, help="how many jobs when no id is given (default: the tailor schedule's batch)")

    sub.add_parser("review", help="applications waiting for your decision")
    for name in ("approve", "skip", "applied"):
        a = sub.add_parser(name, help=f"{name} an application")
        a.add_argument("job_id", type=int)
        a.add_argument("--note")
    mk = sub.add_parser("mark", help="set an application's stage by hand (no Gmail needed)")
    mk.add_argument("job_id", type=int)
    mk.add_argument("stage", choices=list(MARK_STAGES), help="the stage to record")
    mk.add_argument("--note", help="a note to keep with it, for example 'phone screen Tuesday'")

    def _job_filters(sp, default_limit):
        sp.add_argument("--status", default="shortlisted", help="a job status, or all (default: shortlisted)")
        sp.add_argument("--min-score", type=int)
        sp.add_argument("--days", type=int, help="only jobs first seen in the last N days")
        sp.add_argument("--search", help="text in the title or company")
        sp.add_argument("--limit", type=int, default=default_limit)

    jb = sub.add_parser("jobs", help="list jobs from the database (default: your shortlist)")
    _job_filters(jb, 25)
    jd = sub.add_parser("job", help="one job in detail")
    jd.add_argument("job_id", type=int)
    ex = sub.add_parser("export", help="write jobs as CSV for a spreadsheet")
    _job_filters(ex, 1000)
    ex.add_argument("-o", "--output", help="file to write (default: print)")

    ft = sub.add_parser("features", help="show or change which optional parts are on (tailoring, telegram, digest, gmail)")
    ft.add_argument("action", nargs="?", choices=["show", "set"], default="show")
    for name in FEATURE_NAMES:
        ft.add_argument(f"--{name}", choices=["on", "off"])

    gm = sub.add_parser("gmail", help="the optional Gmail part: state, how far back the first search looks, run now")
    gm.add_argument("action", nargs="?", choices=["status", "login", "check", "lookback", "rescan", "run"], default="status")
    gm.add_argument("value", nargs="?", help="for lookback: a number of days (1 to 365), or from-now")
    gm.add_argument("--no-browser", action="store_true", help="login: only print the link, do not try to open a browser")
    gm.add_argument("--restart", action="store_true", help="login: restart the scraper afterwards so it sees the saved login")

    sub.add_parser("parked", help="jobs that ran out of attempts (skipped until requeued)")
    rq = sub.add_parser("requeue", help="put parked jobs back in the queue")
    rq.add_argument("stage", choices=["score", "tailor"])
    rq.add_argument("job_id", nargs="?", type=int, help="one job; omit for every parked job of the stage")

    fu = sub.add_parser("follow-up", help="what is due for a follow-up, and the reminder settings")
    fu.add_argument("--days", type=int, help="days an application waits for a reply before it is due (1 to 90)")
    fu.add_argument("--digest", choices=["on", "off"], help="list what is due in the daily Telegram message")

    rs = sub.add_parser("reshortlist", help="shortlist scored jobs that are above a threshold lowered after they were scored")
    rs.add_argument("--apply", action="store_true", help="do it (without this, only list them)")

    sc = sub.add_parser("schedule", help="when each stage runs and how much it does")
    sc.add_argument("action", nargs="?", choices=["show", "set", "run", "timezone", "pause", "resume"], default="show")
    sc.add_argument("target", nargs="?", help="collect|score|tailor|digest|mail (for set and run), or a timezone name")
    sc.add_argument("--every", help="how often, for example 30m, 6h or 1d (collect, score, tailor)")
    sc.add_argument("--at", help="time of day, 24-hour HH:MM (digest)")
    sc.add_argument("--batch", type=int, help="how much one run does (searches, jobs, applications, matches or emails)")
    sc.add_argument("--boards", type=int, help="company boards and feeds one collect run visits, 1 to 200 (collect only)")
    sc.add_argument("--model", help="the scoring model (score only)")
    sc.add_argument("--reason", help="why you are pausing (shown while paused)")
    g = sc.add_mutually_exclusive_group()
    g.add_argument("--enable", "--on", dest="enable", action="store_true", help="turn the schedule on")
    g.add_argument("--disable", "--off", dest="disable", action="store_true", help="turn the schedule off")

    so = sub.add_parser("sources", help="show or change which job sources are used (on, off, only; names or groups)",
                        description="Show which job sources are on, or switch some on or off. Names: any source, or a group: "
                                    "boards (the five company-board systems), feeds (the five remote feeds), jobspy "
                                    "(indeed, naukri, linkedin), apify (linkedin_apify, which uses your Apify credit). "
                                    "At least one source must stay on.",
                        epilog="examples: sources off boards | sources only apify | sources on remotive greenhouse")
    so.add_argument("action", nargs="?", type=str.lower, choices=list(SOURCE_ACTIONS), default="show",
                    help="show (default), on (add), off (remove what is named), only (keep exactly what is named)")
    so.add_argument("names", nargs="*", help="sources or groups: boards, feeds, jobspy, apify, or a single source such as remotive")

    bd = sub.add_parser("boards", help="find a company's job-board slug (`boards find Stripe`)")
    bd.add_argument("action", choices=["find"])
    bd.add_argument("name", nargs="+", help="the company name")

    fl = sub.add_parser("files", help="where the files for a job are")
    fl.add_argument("job_id", type=int)

    ts = sub.add_parser("tailor-settings", help="show or change the tailoring settings")
    ts.add_argument("action", nargs="?", choices=["show", "set"], default="show")
    ts.add_argument("--mode", choices=["auto", "manual"])
    ts.add_argument("--approval", choices=["review", "cli", "auto"],
                    help="review: ask on Telegram; cli: ask only at the command line; auto: store ready")
    ts.add_argument("--min-score", help="a number, or none (= your search threshold)")
    ts.add_argument("--max-score", help="a number, or none (at or above it the base resume is used as is)")
    ts.add_argument("--daily-limit", help="a number, or none")
    ts.add_argument("--cover-letter", choices=["on", "off"])
    ts.add_argument("--max-pages", choices=["1", "2"])
    ts.add_argument("--verify", choices=["on", "off"], help="second-layer fact check of the generated text")
    ts.add_argument("--model", help="model for the resume patch and cover letter")
    ts.add_argument("--paper", choices=["a4", "letter"], help="page size of the PDFs")
    ts.add_argument("--style-notes", help="tone and wording preferences, at most 400 characters, or none to clear")

    sub.add_parser("push-config", help="copy config/profile.json and resume.json to the server that runs the stack (when you edit them elsewhere)").add_argument(
        "--dry-run", action="store_true", help="only say what would be copied")

    sp = sub.add_parser("search-profile", help="apply or show the search settings in the database, or pull them into the file")
    sp.add_argument("action", choices=["apply", "show", "pull"])
    sp.add_argument("--file", default=str(REPO / "config" / "search.json"))
    sp.add_argument("--name", default="default")
    sp.add_argument("--force", action="store_true", help="apply: overwrite what the dashboard edited since the file was written")

    ck = sub.add_parser("check", help="scam + source check (and optional fit score) for one posting")
    ck.add_argument("--url", help="posting or apply link (classified, never fetched)")
    ck.add_argument("--text", help="file with the posting text, or - for stdin")
    ck.add_argument("--company", default="")
    ck.add_argument("--title", default="")
    ck.add_argument("--score", action="store_true", help="also score fit (needs ANTHROPIC_API_KEY and config/profile.json)")
    ck.add_argument("--force", action="store_true", help="score even when safety risk is high")
    ck.add_argument("--model", default=os.environ.get("JOBSEARCH_MODEL", DEFAULT_MODEL))
    ck.add_argument("--json", action="store_true", help="machine-readable output")
    return p


def main(argv=None, stack=None, stdin=None) -> int:
    args = build_parser().parse_args(argv)
    stack = stack or Stack()
    try:
        if args.cmd == "doctor":
            return print_checks(run_doctor(stack))
        if args.cmd == "status":
            print(run_status(stack))
            return 0
        if args.cmd == "budget":
            if args.action == "set":
                stack.psql(budget_set_sql(args.monthly, args.daily, args.warn_pct, args.apify_monthly, args.apify_daily))
                print("budget updated.\n")
            print(run_budget(stack))
            return 0
        if args.cmd == "tailor":
            print(run_tailor(stack, args.job_id, args.limit))
            return 0
        if args.cmd == "review":
            print(run_review(stack))
            return 0
        if args.cmd in ("approve", "skip", "applied"):
            stack.psql(review_sql(args.job_id, args.cmd, args.note))
            print(f"job {args.job_id}: {args.cmd} recorded.")
            return 0
        if args.cmd == "files":
            print(run_files(stack, args.job_id))
            return 0
        if args.cmd == "mark":
            stack.psql(mark_sql(args.job_id, args.stage, args.note))
            print(f"job {args.job_id}: marked {args.stage}.")
            return 0
        if args.cmd == "jobs":
            print(run_jobs(stack, status=args.status, min_score=args.min_score, days=args.days, search=args.search, limit=args.limit))
            return 0
        if args.cmd == "job":
            print(run_job(stack, args.job_id))
            return 0
        if args.cmd == "export":
            out = export_csv(stack, status=args.status, min_score=args.min_score, days=args.days, search=args.search, limit=args.limit)
            if args.output:
                Path(args.output).write_text(out, encoding="utf-8")
                print(f"wrote {args.output} ({out.count(chr(10)) - 1} row(s))")
            else:
                print(out, end="")
            return 0
        if args.cmd == "parked":
            print(run_parked(stack))
            return 0
        if args.cmd == "requeue":
            rows = stack.query_json(requeue_sql(args.stage, args.job_id))
            print(f"{rows[0]['n'] if rows else 0} job(s) put back in the {args.stage} queue.")
            return 0
        if args.cmd == "follow-up":
            print(run_follow_up(stack, args.days, None if args.digest is None else args.digest == "on"))
            return 0
        if args.cmd == "reshortlist":
            print(run_reshortlist(stack, apply=args.apply))
            return 0
        if args.cmd == "schedule":
            if args.action == "pause":
                print(run_schedule(stack, pause=args.reason or ""))
                return 0
            if args.action == "resume":
                print(run_schedule(stack, resume=True))
                return 0
            if args.action == "timezone":
                if not args.target:
                    raise ValueError("give a timezone, for example: schedule timezone Asia/Kolkata")
                print(run_schedule(stack, timezone=args.target))
                return 0
            if args.action in ("set", "run"):
                if args.target not in schedules.NAMES:
                    raise ValueError(f"name the schedule: one of {', '.join(schedules.NAMES)}")
            if args.action == "set":
                changes = schedule_changes(args)
                if not changes:
                    raise ValueError("give something to change: --every, --at, --batch, --boards, --model, --on or --off")
                schedules.validate_update(args.target, {k: v for k, v in changes.items()})
                print(run_schedule(stack, args.target, changes))
            elif args.action == "run":
                print(run_schedule(stack, args.target, run=True))
            else:
                print(run_schedule(stack))
            return 0
        if args.cmd == "sources":
            print(run_sources(stack, REPO, args.action, args.names))
            return 0
        if args.cmd == "boards":
            print(run_boards_find(" ".join(args.name)))
            return 0
        if args.cmd == "features":
            changes = {n: getattr(args, n) == "on" for n in FEATURE_NAMES if getattr(args, n) is not None}
            if args.action == "set" and not changes:
                raise ValueError("give at least one of --tailoring, --telegram, --digest, --gmail (on or off)")
            print(run_features(stack, changes if args.action == "set" else None))
            return 0
        if args.cmd == "gmail":
            opts = {"no_browser": args.no_browser, "restart": args.restart} if args.action == "login" else {}
            if args.action != "login" and (args.no_browser or args.restart):
                raise ValueError("--no-browser and --restart belong to gmail login")
            print(run_gmail(stack, args.action, args.value, **opts))
            return 0
        if args.cmd == "tailor-settings":
            if args.action == "set":
                fields: dict = {}
                clear: list[str] = []
                for key, raw in (("min_score", args.min_score), ("max_score", args.max_score), ("daily_limit", args.daily_limit)):
                    if raw is None:
                        continue
                    if raw.lower() in ("none", "null", "off", "-"):
                        clear.append(key)
                    elif re.fullmatch(r"\d{1,4}", raw):
                        fields[key] = int(raw)
                    else:
                        raise ValueError(f"{key} must be a whole number or none")
                if args.style_notes is not None:
                    if args.style_notes.strip().lower() in ("none", "off", "-", ""):
                        clear.append("style_notes")
                    elif len(args.style_notes) > 400:
                        raise ValueError("style notes can be at most 400 characters")
                    else:
                        fields["style_notes"] = " ".join(args.style_notes.split())
                for key, val in (("mode", args.mode), ("approval", args.approval), ("max_pages", args.max_pages and int(args.max_pages)),
                                 ("model", args.model), ("paper", args.paper)):
                    if val is not None:
                        fields[key] = val
                if args.cover_letter is not None:
                    fields["cover_letter"] = args.cover_letter == "on"
                if args.verify is not None:
                    fields["verify"] = args.verify == "on"
                if not fields and not clear:
                    raise ValueError("give at least one setting to change (see --help)")
                st, body = _service_call(stack, "POST", "/tailor/settings", dict(fields, clear=clear))
                if st != 200:
                    raise CheckError(_errors(body))
                print("tailoring settings updated.\n")
            print(run_tailor_settings(stack))
            return 0
        if args.cmd == "migrate":
            print(stack.exec_out("postgres", ["sh", "/migrate.sh"]).strip())
            return 0
        if args.cmd == "push-config":
            print(run_push_config(stack, dry_run=args.dry_run))
            return 0
        if args.cmd == "validate":
            lines, ok = validate_config(Path(args.config_dir))
            print("\n".join(lines))
            return 0 if ok else 1
        if args.cmd == "import-linkedin":
            data = profile_tools.parse_linkedin_export(args.folder)
            out = json.dumps(data, indent=2, ensure_ascii=False)
            if args.output:
                Path(args.output).write_text(out + "\n", encoding="utf-8")
                print(f"wrote {args.output} ({len(data['positions'])} positions, {len(data['skills'])} skills, "
                      f"{len(data['education'])} education, {len(data['projects'])} projects; "
                      f"{data['files_ignored']} other file(s) ignored)")
            else:
                print(out)
            return 0
        if args.cmd == "conflicts":
            resume = profile_tools.validate_resume(_load_json(Path(args.resume)))
            li = _load_json(Path(args.linkedin_json)) if args.linkedin_json \
                else profile_tools.parse_linkedin_export(args.linkedin_folder)
            found = profile_tools.find_conflicts(resume, li)
            if not found:
                print("No conflicts between the resume and LinkedIn.")
                return 0
            for i, c in enumerate(found, 1):
                print(f"{i}. [{c['kind']}] {c['message']}")
                if c["resume"]:
                    print(f"     resume:   {c['resume']}")
                for l in ([c["linkedin"]] if isinstance(c["linkedin"], str) else (c["linkedin"] or [])):
                    print(f"     linkedin: {l}")
            return 0
        if args.cmd == "search-profile":
            if args.action == "show":
                rows = stack.query_json("select * from search_profile order by id")
                print(json.dumps(rows, indent=2, default=str, ensure_ascii=False))
                return 0
            if args.action == "pull":
                print(run_profile_pull(stack, name=args.name, file=args.file))
                return 0
            settings = profile_tools.validate_search(_load_json(Path(args.file)))
            guard_dashboard_edits(stack, args.name, force=args.force)
            stack.psql(search_profile_sql(settings, args.name, guard=not args.force))
            if not args.force:
                assert_not_overwritten(stack, args.name)
            print(f"search profile '{args.name}' updated: {len(settings['search_terms'])} term(s), "
                  f"{len(settings['locations'])} location(s), threshold {settings['score_threshold']}")
            return 0
        if args.cmd == "check":
            text = ""
            if args.text:
                text = (stdin or sys.stdin).read() if args.text == "-" else Path(args.text).read_text(encoding="utf-8")
            if not (args.url or text):
                print("give --url and/or --text", file=sys.stderr)
                return 2
            res = check_posting(url=args.url, text=text, company=args.company, title=args.title,
                                score=args.score, force=args.force, api_key=os.environ.get("ANTHROPIC_API_KEY"),
                                model=args.model)
            print(json.dumps(res, indent=2, ensure_ascii=False) if args.json else render_check(res))
            return 1 if res["safety"]["risk"] == "high" else 0
    except (StackError, CheckError, profile_tools.ProfileToolsError, scoring.ProfileError, OSError,
            json.JSONDecodeError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    return 0
