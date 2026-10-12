"""Pipeline stages behind the n8n workflows. All logic lives here so it can be unit-tested;
n8n only schedules, loops, calls the model, and sends the digest.

  plan()           which scrape requests this run should make (rotating slice)
  ingest()         safety-check, dedupe/upsert, then free rule filters on a batch of scraped jobs
  prepare_batch()  claim unscored jobs and build their scoring prompts
  score_one()      validate a model reply, compute the outcome, store it
  digest()         the shortlist message for the day
  sweep()          retire jobs whose deadline has passed

The `store` argument is anything with the methods used below (see db.PgStore); tests use a fake.
"""
from __future__ import annotations

import html as html_lib
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import budget
import filters
import planner
import safety
import scoring

MAX_DIGEST_CHARS = 3800  # Telegram's limit is 4096


class ScoreFailure(Exception):
    def __init__(self, problems: list[str], attempts: int):
        self.problems, self.attempts = problems, attempts
        super().__init__("; ".join(problems))


# ---------------------------------------------------------------- plan

def plan(store, profile: str = "default", batch: int = 6, board_batch: int = 30) -> dict:
    """Search requests (rotating slice, as always), then, only when the user switched on company boards or remote
    feeds, a slice of /fetch/board requests on their OWN cursor (`{profile}:boards`, `board_batch` a run), so the
    two never take slots from each other. With no board requests the result is exactly the old one."""
    settings = store.search_settings(profile)
    combos = planner.build_combos(settings)
    board_reqs = planner.build_board_requests(settings)
    total = len(combos)
    if total == 0 and not board_reqs:
        return {"profile": profile, "total": 0, "start": 0, "requests": [],
                "note": "no usable (title, location, source) combinations: check search settings"}
    start = 0
    requests: list[dict] = []
    if total:                                   # no search combinations: the search cursor must not move
        start = store.next_slice(profile, total, batch)
        requests = [planner.to_request(c, settings) for c in planner.slice_combos(combos, start, batch)]
    out = {"profile": profile, "total": total, "start": start, "requests": requests}
    if board_reqs:
        board_start = store.next_slice(f"{profile}:boards", len(board_reqs), board_batch)
        out["requests"] = requests + planner.slice_combos(board_reqs, board_start, board_batch)
        out["board_total"] = len(board_reqs)
        out["board_start"] = board_start
    return out


# ---------------------------------------------------------------- ingest

def ingest(store, jobs: list[dict], settings: dict, now: Optional[datetime] = None) -> dict:
    """Upsert scraped jobs and apply the safety and rule filters. Safety is recomputed here
    rather than trusted from the caller."""
    counts: Counter = Counter()
    reasons: Counter = Counter()
    counts["received"] = len(jobs)
    for j in jobs:
        if not (j.get("source") and j.get("source_job_id") and (j.get("title") or "").strip()
                and (j.get("company") or "").strip()):
            counts["invalid"] += 1
            continue
        assessment = safety.assess(j)
        job_id, is_new = store.upsert(j, assessment)
        counts["new" if is_new else "seen"] += 1
        status = store.status_of(job_id)
        if status == "scam_blocked":
            counts["scam_blocked"] += 1
            continue
        if status != "new":
            continue
        d = filters.decide(j, settings, now)
        if d.keep:
            store.set_yoe(job_id, d.yoe_min, d.yoe_max)
            counts["kept"] += 1
        else:
            store.mark_filtered(job_id, d.reason, d.yoe_min, d.yoe_max)
            counts["filtered_out"] += 1
            reasons[d.reason.split(":")[0].split("(")[0].strip()] += 1
    out = {k: counts.get(k, 0) for k in ("received", "new", "seen", "kept", "filtered_out", "scam_blocked", "invalid")}
    out["top_filter_reasons"] = dict(reasons.most_common(5))
    return out


# ---------------------------------------------------------------- scoring

def prepare_batch(store, profile: dict, limit: int = 10, max_jd_chars: int = 6000,
                  model: str = "claude-haiku-5-5") -> dict:
    """Claim unscored jobs and build their prompts, but only as many as the budget can pay for
    in the worst case (see budget.py). At a cap, nothing is claimed."""
    status = store.budget_status()
    pricing = store.pricing(model)
    sample = scoring.prepare(profile, {"title": "", "company": "", "description": ""}, max_jd_chars=max_jd_chars)
    per_item = budget.worst_case_item_cost(pricing, len(sample["system"]) + max_jd_chars + 600, sample["max_tokens"])
    n = budget.allowed_items(status, limit, per_item)
    if n <= 0:
        return {"claimed": 0, "items": [], "budget": budget.summarise(status, per_item, 0)}
    rows = store.claim(n)
    items = []
    for r in rows:
        job = {**r, "posted_at": r["posted_at"].isoformat() if hasattr(r.get("posted_at"), "isoformat") else r.get("posted_at")}
        p = scoring.prepare(profile, job, max_jd_chars=max_jd_chars)
        items.append({"job_id": r["id"], "title": r["title"], "company": r["company"], **p})
    return {"claimed": len(items), "items": items, "budget": budget.summarise(status, per_item, n)}


def score_one(store, job_id: int, model_output, threshold: int, usage: Optional[dict] = None,
              model: Optional[str] = None) -> dict:
    """Validate a model reply, log what the call cost (even when the reply is unusable, because
    the tokens were spent), then store the computed outcome."""
    u = budget.normalise_usage(usage)

    def log(ok: bool, note: Optional[str] = None) -> None:
        store.record_usage("anthropic", "score", model, job_id, u, ok, note)

    try:
        result = scoring.finalize(model_output, threshold=threshold)
    except scoring.ScoreError as e:
        log(False, "unusable reply")
        raise ScoreFailure(e.problems, store.note_failure(job_id))
    log(True)
    status = store.record_score(job_id, result)
    return {"job_id": job_id, "status": status, "score": result["score"], "verdict": result["verdict"],
            "above_threshold": result["above_threshold"], "excluded": result["excluded"],
            "warnings": result["warnings"]}


# ---------------------------------------------------------------- digest

def _marks(row: dict, today: date) -> list[str]:
    marks = []
    dl = row.get("deadline")
    if isinstance(dl, str):
        try:
            dl = date.fromisoformat(dl)
        except ValueError:
            dl = None
    if dl and today <= dl <= today + timedelta(days=7):
        marks.append(f"\U0001F525 deadline {dl.isoformat()}")
    if row.get("scam_risk") == "medium":
        flags = row.get("scam_flags") or []
        why = flags[0]["code"] if flags else "check"
        marks.append(f"⚠ safety: medium ({why}), verify before applying")
    for gate, g in (row.get("gates") or {}).items():
        if isinstance(g, dict) and g.get("verdict") in ("FLAG", "UNKNOWN"):
            note = f": {g['note']}" if g.get("note") else ""
            marks.append(f"⚠ {gate} {g['verdict'].lower()}{note}")
    posted = row.get("posted_at")
    if posted and hasattr(posted, "date"):
        if (datetime.now(timezone.utc) - posted).days > 30:
            marks.append("⚠ posted over 30 days ago")
    return marks


FOLLOWUPS_SHOWN = 8
_CUT = {"title": 60, "company": 40, "contact_name": 40}     # a long title must not push the message over Telegram's limit


def _cut(text: object, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def format_followups(rows: list[dict], today: Optional[date] = None) -> str:
    """The follow-up reminders for the daily message (dashboard version 3b), or "" when nothing is due. Only sent when the
    user switched it on (`scripts/jobsearch follow-up --digest on`); the rows come from follow_ups_due(). Each row may carry
    `total` (how many are due in all) and `today` (the date in the configured time zone, which the wording is relative to)."""
    if not rows:
        return ""
    today = today or rows[0].get("today") or date.today()
    total = max(len(rows), rows[0].get("total") or 0)
    lines = [f"⏰ Follow up ({total}):"]
    for r in rows[:FOLLOWUPS_SHOWN]:
        if r.get("reason") == "date":
            due = r.get("follow_up_on")
            when = "follow-up date is today" if due == today else f"follow-up date was {due}"
        else:
            days = r.get("days_waiting")
            when = f"applied {days} day{'s' if days != 1 else ''} ago, no reply"
        who = f" ({_cut(r['contact_name'], _CUT['contact_name'])})" if r.get("contact_name") else ""
        lines.append(f"- {_cut(r['title'], _CUT['title'])} @ {_cut(r['company'], _CUT['company'])}: {when}{who}")
    if total > FOLLOWUPS_SHOWN:
        lines.append(f"... and {total - FOLLOWUPS_SHOWN} more (the dashboard's Board lists them)")
    return "\n".join(lines)


def format_digest(rows: list[dict], today: Optional[date] = None, spend: Optional[dict] = None,
                  followups: Optional[list[dict]] = None) -> dict:
    """The shortlist message. `spend` (a budget status) adds a footer, and an alert line that is sent
    on its own even when there are no new matches (so a paused pipeline is never silent). `followups` (only when the
    user asked for them) adds a reminder section, which is also sent on its own when there are no new matches."""
    today = today or date.today()
    alert = budget.alert_text(spend) if spend else ""
    follow = format_followups(followups or [], today)
    if not rows:
        text = "\n\n".join(part for part in (follow, alert) if part)
        return {"count": 0, "ids": [], "text": text, "html": html_lib.escape(text, quote=False), "alert": alert}
    lines = [f"\U0001F3AF {len(rows)} new match{'es' if len(rows) != 1 else ''} above your threshold", ""]
    shown: list[int] = []
    for i, r in enumerate(rows, 1):
        block = [f"{i}. {r['score']} {r['verdict']} | {r['title']} @ {r['company']}"
                 + (f" ({r['location']})" if r.get("location") else "")]
        block += ["   " + m for m in _marks(r, today)]
        if r.get("strengths"):
            block.append(f"   + {r['strengths'][0]}")
        if r.get("gaps"):
            block.append(f"   - {r['gaps'][0]}")
        if r.get("canonical_url"):
            block.append(f"   {r['canonical_url']}")
        block.append("")
        candidate = "\n".join(lines + block)
        if len(candidate) > MAX_DIGEST_CHARS - (len(follow) + 2 if follow else 0):
            lines.append(f"... and {len(rows) - len(shown)} more (run /status)")
            break
        lines += block
        shown.append(r["id"])
    text = "\n".join(lines).strip()
    if follow:
        text += "\n\n" + follow
    if spend:
        text += "\n\n" + (alert + "\n" if alert else "") + budget.footer_text(spend)
    # Telegram's HTML mode needs &, < and > escaped; titles and URLs come from third-party postings.
    return {"count": len(shown), "ids": shown, "text": text, "html": html_lib.escape(text, quote=False), "alert": alert}


def digest(store, limit: int = 10, today: Optional[date] = None) -> dict:
    return format_digest(store.digest_rows(limit), today, store.budget_status(), store.follow_ups_for_digest())


def sweep(store) -> dict:
    return {"expired": store.sweep()}


def reassess_blocked(store, settings: dict, now: Optional[datetime] = None) -> dict:
    """Re-run the safety rules on every scam-blocked job. Rules improve over time, so a job blocked by
    an older rule set is restored (and then passed through the normal filters) if it is no longer
    high risk. Jobs that are still high risk stay blocked."""
    counts: Counter = Counter()
    for r in store.blocked_rows():
        job = {"title": r["title"], "company": r["company"], "location": r.get("location"),
               "description": r.get("description"), "url": r.get("url"), "apply_url": None,
               "posted_at": r.get("posted_at"), "is_remote": r.get("is_remote")}
        a = safety.assess(job)
        if a["risk"] == "high":
            store.set_safety(r["id"], a, unblock=False)
            counts["still_blocked"] += 1
            continue
        store.set_safety(r["id"], a, unblock=True)
        d = filters.decide(job, settings, now)
        if d.keep:
            store.set_yoe(r["id"], d.yoe_min, d.yoe_max)
            counts["restored"] += 1
        else:
            store.mark_filtered(r["id"], d.reason, d.yoe_min, d.yoe_max)
            counts["restored_then_filtered"] += 1
    return {k: counts.get(k, 0) for k in ("still_blocked", "restored", "restored_then_filtered")}
