"""HTTP wrapper around job sources so n8n can call them and get normalised JSON back.

Endpoints (all return {"jobs": [...], "errors": [...], "safety_summary": {...}}); every job
carries a "safety" object (risk low|medium|high, flags, hosts) from safety.py:
  POST /search           JobSpy boards (indeed, naukri, linkedin, ...)
  POST /search/linkedin  LinkedIn through Apify actors (credit-aware account rotation)
  POST /fetch/board      one company job board (greenhouse, lever, ashby, workable, smartrecruiters; needs a slug)
                         or one remote-jobs feed (remotive, remoteok, weworkremotely, himalayas, arbeitnow; no slug).
                         A source problem is HTTP 200 with "errors", never 502; a fetch inside the source's minimum
                         interval returns no jobs and meta.skipped = "fetched recently"; a bad request is 422.

Scoring helpers (no model call here; n8n or the CLI calls the model in between):
  POST /score/prepare    {job, profile?}        -> {system, user, max_tokens}
  POST /score/finalize   {model_output, threshold?} -> validated result + computed score/verdict/status

Pipeline stages used by the n8n workflows (database-backed; see pipeline.py):
  GET  /pipeline/plan?batch=N        next slice of scrape requests (rotating cursor); batch defaults to the collect schedule's
  POST /jobs/ingest {jobs}           safety check + dedupe/upsert + free rule filters
  POST /jobs/prepare-batch {limit}   claim unscored jobs and return their scoring prompts
  POST /jobs/{id}/score {model_output}   validate the model reply, compute outcome, store it
  GET  /digest?limit=N               ready-to-send shortlist message;  POST /digest/sent {ids}
  GET  /budget  POST /budget/settings  POST /budget/usage   spend vs caps, change caps, log other paid usage
  GET  /profile  POST /profile       the search profile (terms, sources, company boards, filters), read and saved with the CLI's validation
  POST /boards/find                  which job board a company uses (read-only probes of the five boards' public APIs)
  POST /tailor/run {job_id?, limit?}  tailor one job (or the next eligible ones): draft, audit, render, save, notify
  GET  /tailor/settings  POST /tailor/settings   mode, approval, score band, daily limit, model, cover letter
  POST /telegram/poll                process pending Telegram commands (/approve, /skip, /tailor, /status, ...)
  GET  /features  POST /features    which optional parts are on (tailoring, telegram, digest) and what is configured
  GET  /schedule  POST /schedule/{name}  POST /schedule/{name}/run  POST /schedule/timezone  POST /schedule/tick
                                     when each stage runs and how much it does; the scheduler workflow calls /tick
  GET  /applications  GET /applications/{id}  POST /applications/{id}/review {action, note}  GET /applications/{id}/files/{name}
  POST /maintenance/sweep            retire jobs whose deadline has passed
  POST /maintenance/reassess         re-run the safety rules on scam-blocked jobs; restore any no longer high risk

A board that errors or is blocked is reported in "errors" instead of silently returning
nothing. If every requested board failed and there are no jobs, the response is HTTP 502.

Only reachable from other containers on the compose network (no published port).
"""
import json
import logging
import os
import re
import threading
from typing import Annotated, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from jobspy import scrape_jobs
from pydantic import BaseModel, Field, StringConstraints

import apify_source
import board_probe
import boards
import budget
import features
import pipeline
import profile_tools
import tailor_service
import telegram_bot
import safety
import schedules
import scoring
import sources

log = logging.getLogger("scraper")
logging.basicConfig(level=logging.INFO)
# httpx logs every request URL at INFO, and the Telegram Bot API keeps the bot token in the URL path: keep it out of the logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
app = FastAPI()

# Scrapes run one at a time: gentler on the boards, and lets us attribute log errors.
_scrape_lock = threading.Lock()


# JobSpy loggers have propagate=False and only log errors, so they would never reach a
# normal handler. Attach a capture handler to each board's logger up front.
class _ErrorCapture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.ERROR)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(f"{record.name.split(':')[-1].lower()}: {record.getMessage()}")


_capture = _ErrorCapture()
for _board in ("LinkedIn", "Indeed", "Glassdoor", "ZipRecruiter", "Naukri", "Google",
               "Bayt", "BDJobs", "HelloWork"):
    _lg = logging.getLogger(f"JobSpy:{_board}")
    _lg.setLevel(logging.INFO)
    _lg.propagate = False
    _lg.addHandler(_capture)
    # JobSpy only adds its console handler when a logger has none, so add ours.
    _lg.addHandler(logging.StreamHandler())


class SearchRequest(BaseModel):
    site_names: list[str] = ["indeed"]
    search_term: str
    location: str
    results_wanted: int = 20
    hours_old: int = 72
    country_indeed: str = "india"
    is_remote: Optional[bool] = None
    linkedin_fetch_description: bool = True


class LinkedInRequest(BaseModel):
    search_term: str
    location: str
    results_wanted: int = 25
    hours_old: int = 72
    is_remote: Optional[bool] = None
    exclude_keywords: list[str] = []
    exclude_companies: list[str] = []
    only: Optional[str] = None  # pin one actor: curious | cheap | bebity


@app.get("/health")
def health():
    return {"ok": True}


@app.post("/search")
def search(req: SearchRequest):
    kwargs = dict(
        site_name=req.site_names,
        search_term=req.search_term,
        location=req.location,
        results_wanted=req.results_wanted,
        hours_old=req.hours_old,
        country_indeed=req.country_indeed,
        linkedin_fetch_description=req.linkedin_fetch_description,
    )
    if req.is_remote is not None:
        kwargs["is_remote"] = req.is_remote

    with _scrape_lock:
        _capture.messages.clear()
        try:
            df = scrape_jobs(**kwargs)
        except Exception as e:
            log.exception("scrape failed")
            raise HTTPException(status_code=502, detail={"errors": [str(e)]})
        errors = list(_capture.messages)

    rows = json.loads(df.to_json(orient="records", date_format="iso"))  # NaN -> null
    jobs = [normalise(r) for r in rows]
    if not jobs and errors:
        raise HTTPException(status_code=502, detail={"errors": errors})
    return {"jobs": with_safety(jobs), "errors": errors, "safety_summary": safety_summary(jobs)}


@app.post("/search/linkedin")
def search_linkedin(req: LinkedInRequest):
    params = apify_source.SearchParams(
        search_term=req.search_term, location=req.location,
        results_wanted=req.results_wanted, hours_old=req.hours_old,
        is_remote=req.is_remote, exclude_keywords=req.exclude_keywords,
        exclude_companies=req.exclude_companies,
    )
    # Spend guard: look BEFORE starting a paid run. If the budget cannot be read, nothing is spent.
    try:
        st = get_store().apify_budget_status()
    except Exception:
        log.exception("could not read the Apify budget")
        raise HTTPException(status_code=503, detail={"errors": ["cannot read the Apify budget, so no Apify run was started"]})
    left = min(float(st["month_remaining"]), float(st["day_remaining"]))
    if st["blocked_reason"]:    # a skip, not an error: the rest of the run carries on with the free sources
        return {"jobs": [], "errors": [], "safety_summary": safety_summary([]), "skipped": st["blocked_reason"]}
    with _scrape_lock:
        try:
            # the adapter starts a run only if its hard charge limit still fits in what is left of the budget
            out = apify_source.search_linkedin(params, only=req.only, budget_left=left)
        except apify_source.ApifyError as e:
            _log_apify_usage({}, getattr(e, "attempts", []))       # runs that started were probably billed
            raise HTTPException(status_code=502, detail={"errors": [str(e)]})
    if out.get("skipped"):
        return {"jobs": [], "errors": [], "safety_summary": safety_summary([]), "skipped": out["skipped"]}
    out["jobs"] = with_safety(out["jobs"])
    out["safety_summary"] = safety_summary(out["jobs"])
    _log_apify_usage(out.get("meta") or {}, (out.get("meta") or {}).get("failed_attempts") or [])
    return out


_Text = Annotated[str, StringConstraints(max_length=200)]


class BoardFetchRequest(BaseModel):
    source: _Text                              # a board system (needs slug) or a feed (no slug): see sources.py
    slug: Optional[_Text] = None
    name: Optional[_Text] = None               # display name from config/search.json, used for company names
    terms: list[_Text] = Field(min_length=1, max_length=50)
    locations: list[_Text] = Field(max_length=50)
    allow_remote: bool = True
    max_age_days: int = Field(7, ge=1, le=90)
    title_words: list[_Text] = Field([], max_length=50)
    limit: int = Field(25, ge=1, le=100)


@app.post("/fetch/board")
def fetch_board(req: BoardFetchRequest):
    """One company board or one remote feed, filtered, as normalised jobs. Order: validate (no network, no database),
    claim the politeness window, fetch (no database connection held: this is slow HTTP), record the outcome."""
    source, slug = req.source, req.slug
    is_board = source in sources.BOARD_SYSTEMS
    problems = []
    if not is_board and source not in sources.FEEDS:
        problems.append(f"unknown source '{source[:40]}': expected one of {', '.join(sources.BOARD_SYSTEMS + sources.FEEDS)}")
    elif is_board and not boards.valid_slug(slug):
        problems.append(f"{source}: a company slug is required (letters, digits, '.', '_' and '-'; at most 64 characters)")
    elif not is_board and slug:
        problems.append(f"{source}: this is a feed and takes no company slug")
    if problems:
        raise HTTPException(status_code=422, detail={"errors": problems})

    key = slug if is_board else ""             # fetch_state is kept per company, or per feed
    seconds = boards.MIN_INTERVAL_S["board" if is_board else source]
    if not _db_call(lambda s: s.claim_fetch(source, key, seconds)):
        # a skip, not an error: the run carries on, and the source is not asked again inside its minimum interval
        meta = {"source": source, "slug": slug, "listed": 0, "relevant": 0, "returned": 0,
                "dropped": {"title": 0, "location": 0, "age": 0, "unusable": 0}, "skipped": "fetched recently"}
        return {"jobs": [], "errors": [], "safety_summary": safety_summary([]), "meta": meta}

    try:
        result = boards.fetch(source, slug, boards.BoardRequest(
            terms=req.terms, locations=req.locations, allow_remote=req.allow_remote, max_age_days=req.max_age_days,
            title_words=req.title_words, limit=req.limit, name=req.name))
    except Exception as exc:                   # a bug in an adapter must not become a 500 that fails the whole run
        kind = type(exc).__name__
        log.warning("the %s fetch failed unexpectedly", source, exc_info=True)
        try:
            _db_call(lambda s: s.record_fetch(source, key, False, 0, kind))
        except Exception:
            log.warning("could not record the %s fetch", source, exc_info=True)
        where = f"{source}/{slug}" if is_board else source
        meta = {"source": source, "slug": slug, "listed": 0, "relevant": 0, "returned": 0,
                "dropped": {"title": 0, "location": 0, "age": 0, "unusable": 0}, "skipped": None}
        return {"jobs": [], "errors": [f"{where}: unexpected error ({kind})"], "safety_summary": safety_summary([]),
                "meta": meta}
    try:
        _db_call(lambda s: s.record_fetch(source, key, not result.errors, len(result.jobs),
                                          result.errors[0] if result.errors else None))
    except Exception:                          # the jobs are already in hand: losing a health note must not lose them
        log.warning("could not record the %s fetch", source, exc_info=True)
    jobs = with_safety(result.jobs)
    meta = {"source": source, "slug": slug, "listed": result.listed, "relevant": result.relevant,
            "returned": len(jobs), "dropped": result.dropped, "skipped": None}
    return {"jobs": jobs, "errors": result.errors, "safety_summary": safety_summary(jobs), "meta": meta}


class ScoreJob(BaseModel):
    title: Optional[str] = None
    company: Optional[str] = None
    location: Optional[str] = None
    posted_at: Optional[str] = None
    description: Optional[str] = None


class PrepareRequest(BaseModel):
    job: ScoreJob
    profile: Optional[dict] = None   # defaults to the file at PROFILE_PATH
    max_jd_chars: int = 6000


class FinalizeRequest(BaseModel):
    model_output: str
    threshold: int = 70


@app.post("/score/prepare")
def score_prepare(req: PrepareRequest):
    try:
        profile = req.profile if req.profile is not None else scoring.load_profile(
            os.environ.get("PROFILE_PATH", "/config/profile.json"))
        return scoring.prepare(profile, req.job.model_dump(), max_jd_chars=req.max_jd_chars)
    except scoring.ProfileError as e:
        raise HTTPException(status_code=400, detail={"errors": e.problems})


@app.post("/score/finalize")
def score_finalize(req: FinalizeRequest):
    try:
        return scoring.finalize(req.model_output, threshold=req.threshold)
    except scoring.ScoreError as e:
        raise HTTPException(status_code=422, detail={"errors": e.problems})


# ---------------------------------------------------------------- pipeline (database-backed)

_store = None
_store_lock = threading.Lock()


def get_store():
    global _store
    with _store_lock:
        if _store is None:
            import db  # imported lazily so the scraping endpoints work even without a database driver
            _store = db.PgStore(db.conninfo_from_env())
        return _store


def _db_call(fn):
    """Run a pipeline function, turning expected failures into clear HTTP errors."""
    try:
        return fn(get_store())
    except LookupError as e:
        raise HTTPException(status_code=400, detail={"errors": [str(e)]})
    except scoring.ProfileError as e:
        raise HTTPException(status_code=400, detail={"errors": e.problems})
    except pipeline.ScoreFailure as e:
        raise HTTPException(status_code=422, detail={"errors": e.problems, "attempts": e.attempts})
    except HTTPException:
        raise
    except Exception as e:  # database unreachable, bad SQL, ...
        log.exception("pipeline call failed")
        raise HTTPException(status_code=503, detail={"errors": [f"{type(e).__name__}: {e}"]})


class IngestRequest(BaseModel):
    jobs: list[dict]
    profile: str = "default"


class PrepareBatchRequest(BaseModel):
    limit: int = 10
    max_jd_chars: int = 6000
    model: str = os.environ.get("SCORING_MODEL", "claude-haiku-5-5")   # used to price the worst case


class JobScoreRequest(BaseModel):
    model_output: str
    threshold: Optional[int] = None
    profile: str = "default"
    model: Optional[str] = None
    usage: Optional[dict] = None   # the provider's token usage for this call, logged for the budget


class BudgetSettingsRequest(BaseModel):
    monthly_cap_usd: Optional[float] = None
    daily_cap_usd: Optional[float] = None
    warn_pct: Optional[int] = None
    timezone: Optional[str] = None
    apify_monthly_cap_usd: Optional[float] = None
    apify_daily_cap_usd: Optional[float] = None


class UsageRequest(BaseModel):
    provider: str = "anthropic"
    purpose: str
    model: Optional[str] = None
    job_id: Optional[int] = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    cost_override_usd: Optional[float] = None
    ok: bool = True
    note: Optional[str] = None


class SentRequest(BaseModel):
    ids: list[int]


@app.get("/pipeline/plan")
def pipeline_plan(batch: Optional[int] = Query(None, ge=1, le=50), profile: str = "default"):
    return _db_call(lambda s: pipeline.plan(s, profile, batch if batch is not None else s.schedule_row("collect")["batch"],
                                            board_batch=s.board_batch()))


@app.post("/jobs/ingest")
def jobs_ingest(req: IngestRequest):
    return _db_call(lambda s: pipeline.ingest(s, req.jobs, s.search_settings(req.profile)))


@app.post("/jobs/prepare-batch")
def jobs_prepare_batch(req: PrepareBatchRequest):
    def run(s):
        profile = scoring.load_profile(os.environ.get("PROFILE_PATH", "/config/profile.json"))
        return pipeline.prepare_batch(s, profile, req.limit, req.max_jd_chars, req.model)
    return _db_call(run)


@app.post("/jobs/{job_id}/release")
def jobs_release(job_id: int):
    """Give back a scoring claim that cost nothing (the model API was down): the job is retried later and
    is not counted against its attempts. A claim that DID cost money is never released."""
    return _db_call(lambda s: {"job_id": job_id, "claims": s.release_score_claim(job_id)})


@app.post("/jobs/{job_id}/score")
def jobs_score(job_id: int, req: JobScoreRequest):
    def run(s):
        threshold = req.threshold if req.threshold is not None else s.search_settings(req.profile)["score_threshold"]
        return pipeline.score_one(s, job_id, req.model_output, threshold, req.usage, req.model)
    return _db_call(run)


@app.get("/digest")
def digest(limit: Optional[int] = Query(None, ge=1, le=50)):
    def run(s):
        eff = _features(s)["digest"]
        if not eff["on"]:     # nothing to send: the workflow's "anything new?" check ends the run quietly
            return {"count": 0, "ids": [], "text": "", "html": "", "alert": "", "disabled": eff["reason"]}
        return pipeline.digest(s, limit if limit is not None else s.schedule_row("digest")["batch"])
    return _db_call(run)


@app.post("/digest/sent")
def digest_sent(req: SentRequest):
    return _db_call(lambda s: {"marked": s.mark_notified(req.ids)})


@app.get("/budget")
def budget_get():
    def run(s):
        return {"status": budget.summarise(s.budget_status()), "settings": s.budget_settings(),
                "month": s.spend_summary(), "apify": s.apify_budget_status()}
    return _db_call(run)


@app.post("/budget/settings")
def budget_settings(req: BudgetSettingsRequest):
    def run(s):
        for name, v in (("monthly_cap_usd", req.monthly_cap_usd), ("daily_cap_usd", req.daily_cap_usd),
                        ("apify_monthly_cap_usd", req.apify_monthly_cap_usd), ("apify_daily_cap_usd", req.apify_daily_cap_usd)):
            if v is not None and not 0 <= v <= 100000:
                raise HTTPException(status_code=422, detail={"errors": [f"{name} must be between 0 and 100000"]})
        if req.warn_pct is not None and not 1 <= req.warn_pct <= 100:
            raise HTTPException(status_code=422, detail={"errors": ["warn_pct must be between 1 and 100"]})
        return s.update_budget_settings(**req.model_dump())
    return _db_call(run)


@app.post("/budget/usage")
def budget_usage(req: UsageRequest):
    """Log paid usage from another workflow (tailoring, ...). Counts toward the caps if provider is anthropic."""
    def run(s):
        u = budget.normalise_usage({"input_tokens": req.input_tokens, "output_tokens": req.output_tokens,
                                    "cache_creation_input_tokens": req.cache_creation_input_tokens,
                                    "cache_read_input_tokens": req.cache_read_input_tokens})
        return {"cost_usd": s.record_usage(req.provider, req.purpose, req.model, req.job_id, u, req.ok, req.note,
                                           req.cost_override_usd)}
    return _db_call(run)


# ---------------------------------------------------------------- tailoring, review, Telegram

_tailor_lock = threading.Lock()    # one tailoring run at a time: no double work, no double spend
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "/output")


def _load_resume() -> dict:
    try:
        with open(os.environ.get("RESUME_PATH", "/config/resume.json"), encoding="utf-8") as f:
            return profile_tools.validate_resume(json.load(f))
    except FileNotFoundError:
        raise HTTPException(status_code=400, detail={"errors": ["resume not found at /config/resume.json: run /setup"]})
    except (profile_tools.ProfileToolsError, json.JSONDecodeError) as e:
        raise HTTPException(status_code=400, detail={"errors": getattr(e, "problems", [str(e)])})


def _features(store) -> dict:
    """Effective state of the optional parts: the switches in the database combined with what is configured."""
    return features.effective(store.features(), os.environ)


def _tailor_service(store) -> "tailor_service.TailorService":
    import anthropic_client
    import render
    try:
        ai = anthropic_client.AnthropicClient(os.environ.get("ANTHROPIC_API_KEY", ""))
    except anthropic_client.AnthropicError as e:
        raise HTTPException(status_code=503, detail={"errors": [str(e)]})
    profile = scoring.load_profile(os.environ.get("PROFILE_PATH", "/config/profile.json"))
    pdf = render.GotenbergClient(os.environ.get("GOTENBERG_URL", "http://gotenberg:3000"))
    return tailor_service.TailorService(store, ai, pdf, OUTPUT_DIR, _load_resume(), profile,
                                        telegram_on=_features(store)["telegram"]["on"])


def _bot(store, svc=None):
    """The Telegram handler, or None when Telegram is switched off or not configured."""
    if not _features(store)["telegram"]["on"]:
        return None
    token, chat = os.environ["TELEGRAM_BOT_TOKEN"].strip(), os.environ["TELEGRAM_CHAT_ID"].strip()
    resume = svc.resume if svc else _load_resume()

    def run_tailor(job: dict) -> dict:
        eff = _features(store)["tailoring"]
        if not eff["on"]:
            raise ValueError(f"tailoring is {eff['reason']}")
        with _tailor_lock:
            s = svc or _tailor_service(store)
            return s.run_job(job)
    return telegram_bot.BotHandler(store, telegram_bot.TelegramClient(token), int(chat), resume["basics"], run_tailor)


class TailorRunRequest(BaseModel):
    job_id: Optional[int] = None
    limit: Optional[int] = None       # default: the tailor schedule's batch


class TailorSettingsRequest(BaseModel):
    mode: Optional[str] = None
    min_score: Optional[int] = None
    max_score: Optional[int] = None
    daily_limit: Optional[int] = None
    approval: Optional[str] = None
    model: Optional[str] = None
    cover_letter: Optional[bool] = None
    max_pages: Optional[int] = None
    paper: Optional[str] = None
    style_notes: Optional[str] = None
    clear: list[str] = []          # settings to reset to "none" (min_score, max_score, daily_limit)


class ReviewRequest(BaseModel):
    action: str
    note: Optional[str] = None


@app.post("/tailor/run")
def tailor_run(req: TailorRunRequest):
    def run(s):
        eff = _features(s)["tailoring"]
        if not eff["on"]:
            if req.job_id is not None:      # an explicit request deserves an explicit answer
                raise HTTPException(status_code=409, detail={"errors": [f"tailoring is {eff['reason']}"]})
            return {"results": [], "claimed": 0, "skipped": f"tailoring is {eff['reason']}"}   # the scheduled run is a quiet no-op
        with _tailor_lock:
            svc = _tailor_service(s)
            if req.job_id is not None:
                job = s.get_job(req.job_id)
                if not job:
                    raise HTTPException(status_code=404, detail={"errors": [f"no job {req.job_id}"]})
                if job["status"] in ("scam_blocked", "filtered_out", "expired", "applied") or job.get("score") is None:
                    raise HTTPException(status_code=422, detail={"errors": [f"job {req.job_id} is {job['status']} (score {job.get('score')}); not tailored"]})
                out = {"results": [svc.run_job(job)], "claimed": 1}
            else:
                out = svc.run_next(req.limit if req.limit is not None else s.schedule_row("tailor")["batch"])
            notified = 0
            bot = _bot(s, svc) if features.should_notify(s.tailor_settings()["approval"]) else None
            tg = _features(s)["telegram"]
            if bot:
                for r in out.get("results", []):
                    if r.get("status") == "drafted":      # waiting for a decision (review mode, or flagged for attention)
                        try:
                            bot.notify(r, s.get_job(r["job_id"]))
                            notified += 1
                        except Exception:
                            log.warning("could not send the review message", exc_info=True)
            out["notified"] = notified
            out["telegram"] = "sent" if bot else ("not used: approval is cli" if tg["on"] else tg["reason"])
            return out
    return _db_call(run)


@app.get("/tailor/settings")
def tailor_settings_get():
    return _db_call(lambda s: s.tailor_settings())


@app.post("/tailor/settings")
def tailor_settings_set(req: TailorSettingsRequest):
    def run(s):
        fields = {k: v for k, v in req.model_dump(exclude={"clear"}).items() if v is not None}
        for k in req.clear:
            if k not in ("min_score", "max_score", "daily_limit", "style_notes"):
                raise HTTPException(status_code=422, detail={"errors": [f"cannot clear {k}"]})
            fields[k] = None
        try:
            fields = telegram_bot.normalise_settings(fields)
        except ValueError as e:
            raise HTTPException(status_code=422, detail={"errors": [str(e)]})
        try:
            return s.update_tailor_settings(**fields)
        except Exception as e:
            raise HTTPException(status_code=422, detail={"errors": [f"rejected: {str(e).splitlines()[0][:160]}"]})
    return _db_call(run)


class FeaturesRequest(BaseModel):
    tailoring: Optional[bool] = None
    telegram: Optional[bool] = None
    digest: Optional[bool] = None


def _features_report(s) -> dict:
    flags = s.features()
    eff = features.effective(flags, os.environ)
    approval = s.tailor_settings()["approval"]
    return {"flags": flags, "effective": eff, "approval": approval,
            "approval_effective": features.effective_approval(approval, eff["telegram"]["on"]),
            "lines": features.describe(flags, os.environ, approval)}


@app.get("/features")
def features_get():
    return _db_call(_features_report)


@app.post("/features")
def features_set(req: FeaturesRequest):
    def run(s):
        s.update_features(**{k: v for k, v in req.model_dump().items() if v is not None})
        return _features_report(s)
    return _db_call(run)


# ---------------------------------------------------------------- the search profile (dashboard version 2c)
# The CLI writes the profile from config/search.json (`search-profile apply`); the dashboard writes it here, with the
# same validation (profile_tools.validate_search). `edited_by` records who wrote last so the CLI can refuse to overwrite a
# dashboard edit with an older file, and `version` (the row's updated_at) makes a save fail instead of clobbering a change
# made elsewhere in the meantime.

def _registry() -> dict:
    """The sources the profile can switch on, for the settings screen: names, groups, what costs money, what each is."""
    return {"sources": list(sources.ALL), "groups": {k: list(v) for k, v in sources.GROUPS.items()},
            "board_systems": list(sources.BOARD_SYSTEMS), "paid": sorted(sources.PAID),
            "descriptions": dict(sources.DESCRIPTIONS),
            "limits": {"max_board_slugs": profile_tools.MAX_BOARD_SLUGS, "max_title_words": profile_tools.MAX_TITLE_WORDS,
                       "max_board_name": profile_tools.MAX_BOARD_NAME_LEN}}


def _profile_report(s) -> dict:
    row = s.search_profile_row("default")
    if row is None:
        raise HTTPException(status_code=404, detail={"errors": ["there is no search profile yet: run scripts/jobsearch search-profile apply"]})
    return {"profile": profile_tools.settings_from_row(row), "version": row["updated_at"].isoformat(),
            "edited_by": row.get("edited_by", "cli"), "registry": _registry()}


class ProfileRequest(BaseModel):
    profile: dict
    version: str = Field(min_length=1, max_length=64)       # what GET /profile said: the profile as the caller last read it


class FindRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)


@app.get("/profile")
def profile_get():
    return _db_call(_profile_report)


@app.post("/profile")
def profile_set(req: ProfileRequest):
    def run(s):
        try:
            settings = profile_tools.validate_search(req.profile)
        except profile_tools.ProfileToolsError as e:
            raise HTTPException(status_code=422, detail={"errors": e.problems})
        problem = profile_tools.collectable_problem(settings["sources"], settings["boards"])
        if problem:
            raise HTTPException(status_code=422, detail={"errors": [problem]})
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?[+-]\d{2}:\d{2}", req.version):
            raise HTTPException(status_code=422, detail={"errors": ["version must be the value GET /profile returned"]})
        saved = s.save_search_profile("default", settings, req.version, "dashboard")
        if saved is None:
            if s.search_profile_row("default") is None:
                raise HTTPException(status_code=404, detail={"errors": ["there is no search profile yet: run scripts/jobsearch search-profile apply"]})
            raise HTTPException(status_code=409, detail={"errors": ["the search profile was changed somewhere else (the command line, or another tab) since you opened it: reload to see it"]})
        return _profile_report(s)
    return _db_call(run)


@app.post("/boards/find")
def boards_find(req: FindRequest):
    """Which job board a company uses: at most 20 read-only GETs to the five boards' public APIs (sources.PROBE_URLS),
    in parallel and with short timeouts, so the dashboard has an answer within seconds."""
    try:
        matches = board_probe.boards_find(req.name, workers=8, timeout=5)
    except ValueError as e:
        raise HTTPException(status_code=422, detail={"errors": [str(e)]})
    return {"name": req.name, "tried": board_probe.slug_candidates(req.name), "matches": matches}


# ---------------------------------------------------------------- schedules

class ScheduleRequest(BaseModel):
    enabled: Optional[bool] = None
    every_minutes: Optional[int] = None
    at_time: Optional[str] = None
    batch: Optional[int] = None
    board_batch: Optional[int] = None          # collect only: company boards and feeds visited per run
    model: Optional[str] = None


class TimezoneRequest(BaseModel):
    timezone: str


def _schedule_report(s) -> dict:
    from datetime import datetime, timezone as tzinfo
    rows = s.schedule_status()
    lines = schedules.describe(rows, datetime.now(tzinfo.utc))
    plain = [{k: (str(v) if k in ("at_time", "last_run_at", "next_run_at") and v is not None else v) for k, v in r.items()} for r in rows]
    for r in plain:
        if r["name"] == "collect":      # the one stage with a second batch size (company boards and feeds per run)
            r["board_batch"] = s.board_batch()
    paused = s.schedule_pause()
    if paused["paused"]:
        lines = [f"PAUSED since {paused['paused_at']:%Y-%m-%d %H:%M} UTC" + (f": {paused['reason']}" if paused["reason"] else "")
                 + "  (nothing runs until: scripts/jobsearch schedule resume)"] + lines
    return {"timezone": rows[0]["timezone"] if rows else None, "schedules": plain, "lines": lines,
            "paused": bool(paused["paused"]), "paused_reason": paused["reason"]}


@app.get("/schedule")
def schedule_get():
    return _db_call(_schedule_report)


@app.post("/schedule/tick")
def schedule_tick():
    """Called by the scheduler workflow every few minutes: claim what is due and say what to start.
    A stage whose feature is switched off (tailoring, digest) is left alone, so it fires promptly when switched back on."""
    def run(s):
        eff = _features(s)
        skip = [n for n, feat in (("tailor", "tailoring"), ("digest", "digest")) if not eff[feat]["on"]]
        return {"due": s.claim_due_schedules(skip), "skipped": skip}
    return _db_call(run)


class PauseRequest(BaseModel):
    reason: Optional[str] = None


@app.post("/schedule/pause")
def schedule_pause(req: PauseRequest):
    """The emergency brake: nothing scheduled starts until /schedule/resume (per-stage settings are kept)."""
    def run(s):
        s.set_schedule_paused(True, (req.reason or "")[:200] or None)
        return _schedule_report(s)
    return _db_call(run)


@app.post("/schedule/resume")
def schedule_resume():
    def run(s):
        s.set_schedule_paused(False)
        return _schedule_report(s)
    return _db_call(run)


@app.post("/schedule/timezone")
def schedule_timezone(req: TimezoneRequest):
    def run(s):
        if not re.fullmatch(r"[A-Za-z0-9_+/-]{1,64}", req.timezone) or not s.valid_timezone(req.timezone):
            raise HTTPException(status_code=422, detail={"errors": [f"unknown timezone '{req.timezone[:40]}': use a name such as Asia/Kolkata or Europe/London"]})
        s.update_budget_settings(timezone=req.timezone)
        return _schedule_report(s)
    return _db_call(run)


@app.get("/schedule/{name}/settings")
def schedule_settings_get(name: str):
    """What a workflow needs for its run (the score workflow reads its model and batch size here)."""
    def run(s):
        r = s.schedule_row(name)
        return {"name": r["name"], "enabled": r["enabled"], "batch_size": r["batch"], "model": r["model"]}
    return _db_call(run)


@app.post("/schedule/{name}/run")
def schedule_run(name: str):
    def run(s):
        s.request_run(name)
        return {"requested": name, "note": "it starts at the next scheduler tick (within 5 minutes)"}
    return _db_call(run)


@app.post("/schedule/{name}")
def schedule_set(name: str, req: ScheduleRequest):
    def run(s):
        try:
            clean = schedules.validate_update(name, {k: v for k, v in req.model_dump().items() if v is not None})
        except ValueError as e:
            raise HTTPException(status_code=422, detail={"errors": [str(e)]})
        if not clean:
            raise HTTPException(status_code=422, detail={"errors": ["nothing to change"]})
        s.update_schedule(name, **clean)
        return _schedule_report(s)
    return _db_call(run)


_poll_lock = threading.Lock()


@app.post("/telegram/poll")
def telegram_poll():
    # One poll at a time: a slow command (/tailor can take a minute) must not overlap the next scheduled poll.
    if not _poll_lock.acquire(blocking=False):
        return {"busy": True, "updates": 0}

    def run(s):
        bot = _bot(s)
        if not bot:
            return {"telegram": _features(s)["telegram"]["reason"], "updates": 0}
        try:
            return bot.poll()
        except telegram_bot.BotError as e:
            raise HTTPException(status_code=502, detail={"errors": [str(e)]})
    try:
        return _db_call(run)
    finally:
        _poll_lock.release()


@app.get("/applications")
def applications_list(status: str = "drafted"):
    def run(s):
        if status == "drafted":
            return {"applications": s.review_queue()}
        raise HTTPException(status_code=422, detail={"errors": ["only status=drafted is listed here"]})
    return _db_call(run)


@app.get("/applications/{job_id}")
def application_get(job_id: int):
    def run(s):
        a = s.application(job_id)
        if not a:
            raise HTTPException(status_code=404, detail={"errors": [f"job {job_id} has no application"]})
        return a
    return _db_call(run)


@app.post("/applications/{job_id}/review")
def application_review(job_id: int, req: ReviewRequest):
    def run(s):
        if req.action not in ("approve", "skip", "applied"):
            raise HTTPException(status_code=422, detail={"errors": ["action must be approve, skip or applied"]})
        try:
            return {"job_id": job_id, "status": s.review_application(job_id, req.action, req.note)}
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=409, detail={"errors": [str(e).splitlines()[0][:200]]})
    return _db_call(run)


@app.get("/applications/{job_id}/files/{name}")
def application_file(job_id: int, name: str):
    if name not in ("resume.pdf", "cover_letter.pdf", "summary.md", "tailored_resume.json", "patch.json", "audit.json", "posting.txt"):
        raise HTTPException(status_code=404, detail={"errors": ["unknown file"]})

    def run(s):
        a = s.application(job_id)
        if not a or not a.get("files_dir"):
            raise HTTPException(status_code=404, detail={"errors": [f"job {job_id} has no files"]})
        path = os.path.realpath(os.path.join(a["files_dir"], name))
        root = os.path.realpath(OUTPUT_DIR)
        if not path.startswith(root + os.sep) or not os.path.isfile(path):
            raise HTTPException(status_code=404, detail={"errors": ["file not found"]})
        return FileResponse(path)
    return _db_call(run)


@app.post("/maintenance/sweep")
def maintenance_sweep():
    return _db_call(pipeline.sweep)


@app.post("/maintenance/reassess")
def maintenance_reassess(profile: str = "default"):
    return _db_call(lambda s: pipeline.reassess_blocked(s, s.search_settings(profile)))


def apify_cost_to_log(meta: dict) -> Optional[float]:
    """What a finished run is counted as against the Apify caps: the larger of the actor's price estimate and the
    measured change in the account balance. Apify reports that change late, and a real run's cost was both above
    (25 results: $0.047 against a $0.035 estimate) and below (5 results: $0.010 against $0.015) the estimate, so when
    no positive measurement is available the run is counted at 1.5x its estimate: a cap that trips early is the
    safe direction."""
    est = meta.get("estimated_cost_usd")
    if est is None:
        return None
    actual = meta.get("actual_cost_usd")
    return round(max(est, actual), 4) if actual else round(est * 1.5, 4)


def _log_apify_usage(meta: dict, failed_attempts: list) -> None:
    """Record Apify spend (best effort: never fails a search). Runs that failed after starting are counted at
    their estimate times 1.5, because a failed run may still be billed for what it used."""
    try:
        store = get_store()
        cost = apify_cost_to_log(meta)
        if cost is not None:
            note = ("measured from the balance" if meta.get("actual_cost_usd") else "no measurement yet: counted at 1.5x the estimate") + f"; account {meta.get('account')}"
            store.record_usage("apify", "search", meta.get("actor"), None, {}, True, note, cost)
        for a in failed_attempts:
            store.record_usage("apify", "search", a.get("account"), None, {}, False,
                               "failed run, counted at its estimate x1.5", round(float(a.get("estimated_cost_usd") or 0) * 1.5, 4))
    except Exception:
        log.warning("could not log apify usage", exc_info=True)


def with_safety(jobs: list[dict]) -> list[dict]:
    for j in jobs:
        j["safety"] = safety.assess(j)
    return jobs


def safety_summary(jobs: list[dict]) -> dict:
    summary = {"low": 0, "medium": 0, "high": 0}
    for j in jobs:
        summary[j["safety"]["risk"]] += 1
    return summary


def normalise(r: dict) -> dict:
    return {
        "source": r.get("site"),
        "source_job_id": str(r.get("id")) if r.get("id") is not None else r.get("job_url"),
        "url": r.get("job_url"),
        "apply_url": r.get("job_url_direct"),
        "title": r.get("title"),
        "company": r.get("company"),
        "location": r.get("location"),
        "is_remote": r.get("is_remote"),
        "posted_at": r.get("date_posted"),
        "description": r.get("description"),
        "salary_min": r.get("min_amount"),
        "salary_max": r.get("max_amount"),
        "salary_currency": r.get("currency"),
        "raw": r,
    }
