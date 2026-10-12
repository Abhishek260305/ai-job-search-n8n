"""Test helpers shared by the test modules: a valid environment and a fake store."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from store import NotFound, ProfileChanged, Unavailable

PASSWORD = "correct horse battery"
SECRET = "s" * 40
WRITE = {"X-Requested-With": "jobsearch-dashboard"}
NOW = datetime(2026, 10, 12, 9, 30, tzinfo=timezone.utc)


def make_env(tmp_path: Path, **overrides) -> dict:
    """A complete, valid environment; pass NAME=None to remove a variable."""
    env = {
        "DASHBOARD_PASSWORD": PASSWORD,
        "DASHBOARD_SECRET": SECRET,
        "DB_PASSWORD": "db-pass-for-tests",
        "OUTPUT_DIR": str(tmp_path / "output"),
        "STATIC_DIR": str(tmp_path / "static"),
    }
    env.update(overrides)
    return {k: v for k, v in env.items() if v is not None}


def job_row(job_id: int = 1, **overrides) -> dict:
    """A row shaped like v_dashboard_jobs."""
    row = {
        "id": job_id, "title": f"Engineer {job_id}", "company": f"Company {job_id}", "location": "Pune",
        "is_remote": False, "salary_min": None, "salary_max": None, "salary_currency": None, "score": 70,
        "verdict": "good", "scores": {"technical": 70}, "scam_risk": "low", "job_status": "shortlisted",
        "filter_reason": None, "posted_at": NOW, "first_seen": NOW, "last_seen": NOW, "deadline": None,
        "closing_soon": False, "application_status": None, "applied_at": None, "needs_attention": False,
        "has_resume": False, "has_cover_letter": False, "pipeline_status": "shortlisted", "sources": ["indeed"],
        "apply_url": f"https://example.com/jobs/{job_id}",
    }
    row.update(overrides)
    return row


def job_detail(job_id: int = 1, **overrides) -> dict:
    """A row shaped like v_dashboard_job_detail plus sources_detail."""
    row = job_row(job_id) | {
        "description": "about the job", "canonical_url": f"https://example.com/jobs/{job_id}",
        "score_reason": "fits", "strengths": ["python"], "gaps": [], "gates": {}, "yoe_min": 2, "yoe_max": 5,
        "scam_score": 5, "scam_flags": [], "posting_language": "en", "variant": None, "notes": None,
        "review_note": None, "reviewed_at": None, "attention_reason": None, "cost_usd": None, "model": None,
        "sources_detail": [{"source": "indeed", "url": f"https://example.com/jobs/{job_id}", "apply_url": None,
                            "first_seen": NOW, "last_seen": NOW}],
    }
    row.update(overrides)
    return row


SUMMARY = {
    "threshold": 60,
    "cards": {"found": 15, "shortlisted": 2, "new_this_week": 1, "applied": 8, "in_progress": 1, "interview": 1,
              "offer": 1, "rejected": 1, "withdrawn": 2},
    "by_status": {"applied": 1, "in_progress": 1, "interview": 1, "offer": 1, "rejected": 1, "withdrawn": 2,
                  "not_applied": 2},
    "funnel": [{"step": s, "count": c} for s, c in (("found", 15), ("passed_filters", 13), ("scored", 12),
                                                     ("shortlisted", 11), ("applied", 8), ("interview", 2),
                                                     ("offer", 1))],
    "generated_at": NOW,
}


HEALTH = {
    "generated_at": NOW,
    "paused": {"paused": False, "paused_at": None, "reason": None},
    "timezone": "Asia/Kolkata",
    "schedules": [
        {"name": "collect", "enabled": True, "every_minutes": 360, "at_time": None, "batch": 18, "model": None,
         "last_run_at": NOW, "next_run_at": NOW, "overdue": False, "feature_off": False},
        {"name": "digest", "enabled": True, "every_minutes": None, "at_time": "08:00:00", "batch": 10, "model": None,
         "last_run_at": None, "next_run_at": NOW, "overdue": False, "feature_off": True},
    ],
    "queue": {"waiting_to_score": 3, "waiting_over_3h": 0, "last_scored_at": NOW},
    "parked": {"total": 1, "items": [{"stage": "score", "job_id": 9, "title": "Engineer", "company": None,
                                      "claims": 6, "strikes": 0}]},
    "sources": {"tracked": 30, "failing": 1, "items": [
        {"source": "lever", "key": "newco", "last_fetched_at": NOW, "last_ok_at": None, "last_count": None,
         "failures": 3, "last_error": "HTTP 404"}]},
    "alerts": [{"level": "warn", "code": "parked", "message": "1 job(s) ran out of attempts and are skipped until requeued."}],
}

SPEND = {
    "generated_at": NOW,
    "timezone": "Asia/Kolkata",
    "claude": {"month_spent": 0.53, "day_spent": 0.02, "monthly_cap": 10, "daily_cap": 1.5, "month_remaining": 9.47,
               "day_remaining": 1.48, "warn": False, "warn_pct": 80, "blocked": False, "blocked_reason": None,
               "month_start": NOW},
    "apify": {"month_spent": 0.53, "day_spent": 0, "monthly_cap": 15, "daily_cap": 0.5, "month_remaining": 14.47,
              "day_remaining": 0.5, "blocked": False, "blocked_reason": None},
    "daily": [{"day": "2026-10-12", "provider": "anthropic", "purpose": "score", "calls": 40, "cost_usd": 0.0164}],
    "models": [{"provider": "anthropic", "model": "claude-haiku-5-5", "calls": 40, "cost_usd": 0.0164,
                "input_tokens": 90000, "output_tokens": 8000}],
}


EVENTS = [
    {"id": 1, "from_status": None, "to_status": "drafted", "note": None, "source": None, "at": NOW, "undoable": False},
    {"id": 2, "from_status": "drafted", "to_status": "applied", "note": "sent my CV", "source": "dashboard", "at": NOW, "undoable": True},
]


def board_card(job_id: int = 1, **overrides) -> dict:
    row = {"id": job_id, "title": f"Engineer {job_id}", "company": f"Company {job_id}", "location": "Pune", "is_remote": False,
           "score": 80, "pipeline_status": "shortlisted", "applied_at": None, "deadline": None, "closing_soon": False,
           "has_resume": False, "has_cover_letter": False, "needs_attention": False, "follow_up": None,
           "status_since": NOW}
    row.update(overrides)
    return row


BOARD = {
    "generated_at": NOW,
    "columns": [
        {"key": "not_applied", "total": 51, "items": [board_card(1), board_card(2, pipeline_status="drafted", has_resume=True)]},
        {"key": "applied", "total": 1, "items": [board_card(3, pipeline_status="applied", applied_at=NOW)]},
        {"key": "in_progress", "total": 0, "items": []},
        {"key": "interview", "total": 0, "items": []},
        {"key": "offer", "total": 0, "items": []},
        {"key": "rejected", "total": 0, "items": []},
    ],
    "today": "2026-10-12",
    "follow_up_days": 7,
    "follow_ups_total": 3,
    "follow_ups": [
        {"id": 3, "title": "Engineer 3", "company": "Company 3", "location": "Pune", "score": 80, "pipeline_status": "applied",
         "reason": "waiting", "follow_up_on": None, "applied_on": "2026-10-01", "days_waiting": 11,
         "contact_name": "Priya N", "contact_email": "priya@example.com", "referral": None},
    ],
    "closed": {"withdrawn": 2, "skipped": 1},
}

TRACKING = {"notes": "call Priya on Friday", "contact_name": "Priya N", "contact_email": "priya@example.com",
            "referral": "via Sam", "follow_up_on": "2026-10-16", "version": "2026-10-12T09:30:00.123456Z",
            "today": "2026-10-12"}

STATS = {
    "generated_at": NOW, "timezone": "Asia/Kolkata", "applied": 8, "responded": 3, "reached_interview": 2, "reached_offer": 1,
    "avg_days_to_reply": 6.5, "replies_measured": 2,
    "weekly": [{"week_start": f"2026-08-{day:02d}", "found": 10 + day, "shortlisted": 3, "applied": 1} for day in range(10, 22)],
}


class FakeStore:
    """Answers like PgStore from fixed data. Set `fail` to an exception to make the next data call raise it;
    every call is recorded in `calls`."""
    def __init__(self, db_up: bool = True):
        self.db_up = db_up
        self.rows = [job_row(1), job_row(2)]
        self.details = {1: job_detail(1)}
        self.files: dict[tuple[int, str], str | None] = {}
        self.calls: list[tuple] = []
        self.fail: Exception | None = None
        self.event_id: int | None = 42

    def _call(self, *call):
        self.calls.append(call)
        if self.fail is not None:
            raise self.fail

    def ping(self) -> bool:
        return self.db_up

    def summary(self) -> dict:
        self._call("summary")
        return SUMMARY

    def jobs(self, q):
        self._call("jobs", q)
        return self.rows, len(self.rows)

    def job(self, job_id: int) -> dict:
        self._call("job", job_id)
        if job_id not in self.details:
            raise NotFound(f"no job {job_id}")
        return self.details[job_id]

    def move_status(self, job_id, status, expected, note):
        self._call("move_status", job_id, status, expected, note)
        return {"status": status, "event_id": self.event_id}

    def application_file(self, job_id, kind):
        self._call("application_file", job_id, kind)
        return self.files.get((job_id, kind))

    def export_rows(self, q):
        self._call("export_rows", q)
        return self.rows

    def health(self) -> dict:
        self._call("health")
        return HEALTH

    def spend(self) -> dict:
        self._call("spend")
        return SPEND

    def job_events(self, job_id):
        self._call("job_events", job_id)
        if job_id not in self.details:
            raise NotFound(f"no job {job_id}")
        return EVENTS

    def job_tracking(self, job_id):
        self._call("job_tracking", job_id)
        if job_id not in self.details:
            raise NotFound(f"no job {job_id}")
        return TRACKING

    def set_tracking(self, job_id, version, patch):
        self._call("set_tracking", job_id, version, patch)
        if job_id not in self.details:
            raise NotFound(f"no job {job_id}")
        return TRACKING | patch | {"version": "2026-10-12T09:31:00.000001Z"}

    def set_follow_up_days(self, days):
        self._call("set_follow_up_days", days)
        return days

    def undo_status(self, job_id, expected, event_id):
        self._call("undo_status", job_id, expected, event_id)
        if job_id not in self.details:
            raise NotFound(f"no job {job_id}")
        return {"id": job_id, "status": "shortlisted"}

    def board(self) -> dict:
        self._call("board")
        return BOARD

    def stats(self) -> dict:
        self._call("stats")
        return STATS

    def set_pause(self, paused, reason):
        self._call("set_pause", paused, reason)
        return {"paused": paused, "paused_at": NOW if paused else None, "reason": reason if paused else None}


def scraper_schedule() -> dict:
    """GET /schedule of the scraper (scraper/app.py _schedule_report), trimmed."""
    def row(name, every, at, batch, model=None, extra=None):
        return {"name": name, "enabled": True, "every_minutes": every, "at_time": at, "batch": batch, "model": model,
                "run_now": False, "last_run_at": "2026-10-12 09:10:00+05:30", "next_run_at": "2026-10-12 09:40:00+05:30",
                "timezone": "Asia/Kolkata", **(extra or {})}
    return {"timezone": "Asia/Kolkata", "lines": ["ignored"], "paused": False, "paused_reason": None, "schedules": [
        row("collect", 360, None, 18, extra={"board_batch": 35}),
        row("score", 30, None, 25, "claude-haiku-5-5"),
        row("tailor", 15, None, 3),
        row("digest", None, "08:00:00", 10)]}


SCRAPER_FEATURES = {
    "flags": {"tailoring": False, "telegram": True, "digest": True},
    "effective": {"tailoring": {"on": False, "reason": "switched off (jobsearch features set tailoring on)"},
                  "telegram": {"on": True, "reason": ""}, "digest": {"on": True, "reason": ""}},
    "approval": "review", "approval_effective": "review", "lines": ["ignored"],
}
SCRAPER_TAILORING = {"mode": "auto", "min_score": None, "max_score": 80, "daily_limit": 5, "approval": "review",
                     "model": "claude-sonnet-5-5", "cover_letter": True, "max_pages": 2, "verify": True,
                     "verify_model": "claude-haiku-5-5", "paper": "a4", "style_notes": "private notes"}
SCRAPER_BUDGET = {"status": {"ignored": True}, "month": [], "apify": {},
                  "settings": {"monthly_cap_usd": 10.0, "daily_cap_usd": 1.5, "warn_pct": 80, "timezone": "Asia/Kolkata",
                               "apify_monthly_cap_usd": 15.0, "apify_daily_cap_usd": 0.5}}


SCRAPER_PROFILE = {
    "profile": {"search_terms": ["backend engineer"], "locations": ["Pune", "Remote"], "sources": ["indeed", "greenhouse"],
                "allow_remote": True, "min_yoe": 0, "max_yoe": 4, "include_keywords": [], "exclude_keywords": ["intern"],
                "exclude_companies": ["Acme"], "max_age_days": 7, "score_threshold": 60,
                "boards": {"greenhouse": ["stripe", {"slug": "notion", "name": "Notion"}]}, "board_title_words": ["software"]},
    "version": "2026-10-12T09:30:00.123456+00:00", "edited_by": "cli",
    "registry": {"sources": ["indeed", "naukri", "linkedin", "linkedin_apify", "glassdoor", "ziprecruiter", "google", "greenhouse",
                             "lever", "ashby", "workable", "smartrecruiters", "remotive", "remoteok", "weworkremotely", "himalayas", "arbeitnow"],
                 "groups": {"jobspy": ["indeed", "naukri", "linkedin"], "apify": ["linkedin_apify"],
                            "boards": ["greenhouse", "lever", "ashby", "workable", "smartrecruiters"],
                            "feeds": ["remotive", "remoteok", "weworkremotely", "himalayas", "arbeitnow"]},
                 "board_systems": ["greenhouse", "lever", "ashby", "workable", "smartrecruiters"], "paid": ["linkedin_apify"],
                 "descriptions": {"indeed": "Indeed job search through JobSpy; free, no key needed."},
                 "limits": {"max_board_slugs": 300, "max_title_words": 50, "max_board_name": 100}},
}


class FakeScraper:
    """Answers like ScraperClient from fixed reports and records every write. Set `fail` to make the next call raise."""
    def __init__(self):
        self.calls: list[tuple] = []
        self.fail: Exception | None = None
        self.budget_settings = dict(SCRAPER_BUDGET["settings"])
        self.profile_state = {**SCRAPER_PROFILE, "profile": {**SCRAPER_PROFILE["profile"]}}
        self.stale = False                                   # the next save finds the profile changed elsewhere
        self.found = {"name": "x", "tried": ["x"], "matches": []}

    def _call(self, *call):
        self.calls.append(call)
        if self.fail is not None:
            raise self.fail

    def schedule(self):
        self._call("schedule")
        return scraper_schedule()

    def features(self):
        self._call("features")
        return SCRAPER_FEATURES

    def tailoring(self):
        self._call("tailoring")
        return SCRAPER_TAILORING

    def budget(self):
        self._call("budget")
        return {**SCRAPER_BUDGET, "settings": dict(self.budget_settings)}

    def set_schedule(self, name, changes):
        self._call("set_schedule", name, changes)
        return {}

    def run_schedule(self, name):
        self._call("run_schedule", name)
        return {"requested": name, "note": "it starts at the next scheduler tick (within 5 minutes)"}

    def set_features(self, flags):
        self._call("set_features", flags)
        return {}

    def set_tailoring(self, changes):
        self._call("set_tailoring", changes)
        return {}

    def set_budget(self, caps):
        self._call("set_budget", caps)
        self.budget_settings.update(caps)
        return {}

    def profile(self):
        self._call("profile")
        return {**self.profile_state}

    def save_profile(self, profile, version):
        self._call("save_profile", profile, version)
        if self.stale:
            raise ProfileChanged("the search profile was changed somewhere else (the command line, or another tab) since you opened it: reload to see it")
        self.profile_state = {**self.profile_state, "profile": profile, "version": "2026-10-12T09:45:00.000000+00:00", "edited_by": "dashboard"}
        return {}

    def find_board(self, name):
        self._call("find_board", name)
        return {**self.found, "name": name}

    def writes(self):
        return [c for c in self.calls if c[0].startswith(("set_", "run_", "save_"))]

