"""The dashboard's HTTP side: a small JSON API under /api and the built React app everywhere else.

Published on 127.0.0.1 on the server and opened through an SSH tunnel (docs/dashboard.md). Every response carries
security headers, and a request whose Host is not an allowed name is refused before any route runs, so a web page
cannot reach this service through a DNS-rebinding trick even though it listens on localhost.

Routes (see auth.py for how login works):
  GET  /api/health        liveness, and whether the database answers (no login)
  GET  /api/openapi.json  the API schema; the frontend's TypeScript types are generated from it (no login)
  POST /api/login         {password} -> 204 and the session cookie; 401 wrong password; 429 locked out
  POST /api/logout        ends every session and clears the cookie (needs the X-Requested-With header)
  GET  /api/me            the current session's expiry
  GET  /api/summary       the cards, the donut and the funnel
  GET  /api/jobs          one page of the jobs table (tab, filters, sort, page: models.JobsQuery)
  GET  /api/jobs/{id}     one job with every listing, for the details panel
  GET  /api/jobs/{id}/events   its timeline: every status change, with the note and who made it, and which one can be undone
  GET  /api/jobs/{id}/tailor/estimate   what tailoring this job would cost, and whether it may start (from the scraper)
  POST /api/jobs/{id}/tailor {max_cost_usd?}  start tailoring it in the background (the scraper may refuse); GET /api/jobs/{id}/tailor  progress
  POST /api/jobs/{id}/undo   {expected_status, event_id?} takes back the newest change if it was made here; 409 if it moved on
  GET/PUT /api/jobs/{id}/tracking   notes, contact, referral and follow-up date of a job; a save needs the version it read (409 if changed)
  PUT  /api/follow-ups/days   after how many days with no reply an application is due for a follow-up
  GET  /api/board         the Kanban board: the cards of every column, the true totals and what is due for a follow-up
  GET  /api/email?state=pending|accepted|dismissed   the email inbox: suggestions from replies to your applications (Gmail part)
  GET  /api/email/count   how many wait, and whether the Gmail part is on
  POST /api/email/{id}/accept {expected_status, acknowledged?}   make the change (the same function as a board move: history, Undo)
  POST /api/email/{id}/dismiss  /restore   close a suggestion, or bring a dismissed one back
  GET  /api/stats         response rate, days to a first reply and the last 12 weeks
  PATCH /api/jobs/{id}/status   {status, expected_status, note?}; 409 with current_status if it changed elsewhere; the answer
                          names the history entry it wrote (event_id), which is what Undo is tied to
  GET  /api/jobs/{id}/files/{resume|cover_letter}   the tailored PDF, shown in the browser
  GET  /api/export.csv    the jobs table with the same filters, every page (up to 5,000 rows)
  GET  /api/pipeline      the health panel: pause state, schedules, scoring queue, parked jobs, failing sources, alerts
  GET  /api/spend         the spend panel: Claude and Apify against their caps, 30 days by purpose, this month by model
  POST /api/pipeline/pause   {reason?} stops every scheduled stage (the same switch as `schedule pause`)
  POST /api/pipeline/resume  starts them again
  GET  /api/settings      the settings page: schedules, feature switches, tailoring, budget caps (from the scraper)
  PATCH /api/settings/schedules/{name}   change one stage; POST .../run asks for a run at the next tick
  PATCH /api/settings/features, /tailoring, /budget (caps can only be lowered)
  GET/PUT /api/search     the search profile (terms, locations, sources, company boards, filters); POST /api/search/boards/find
  Every route from /api/me on sits on the `protected` router: no valid session is 401, and a change without the
  X-Requested-With header (or from a foreign Origin) is 403.
  GET /assets/...        the built app's hashed files (cached for a year)
  GET /anything-else     a file at the root of the built app (favicon, ...) or index.html, so client-side routes work

Run with:  uvicorn app:build --factory
"""
from __future__ import annotations

import logging
import sys
from datetime import date, datetime, timezone
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Path, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import csv_export
import files
from auth import COOKIE, LoginLimiter, Sessions, check_write, password_ok, require_auth
import settings_service
from models import (BudgetChange, ErrorBody, FeaturesChange, FindBoard, FindResult, Health, JobDetail, JobPage, JobsQuery, PauseRequest, PauseState,
                    RunRequested, ScheduleChange, ScheduleName, SearchPage, SearchSave, SettingsPage, Spend, StatusChange,
                    StatusResult, Summary, TailoringChange)
from models import (EmailAccept, EmailAccepted, EmailCount, EmailDecided, EmailInbox, EmailQuery)
from models import (Board, FollowUpDays, JobEvent, Stats, TailorEstimate, TailorStart, TailorState, Tracking, TrackingSave, UndoRequest,
                    UndoResult)
from scraper_client import ScraperClient
from settings import Settings, SettingsError, load_settings
from store import Conflict, EmailChanged, EmailNotFound, Forbidden, Invalid, NotFound, HistoryChanged, PgStore, ProfileChanged, StoreError, TrackingChanged, Unavailable

PUBLIC_API = frozenset({"/api/health", "/api/openapi.json", "/api/login", "/api/logout"})
SECURITY_HEADERS = {
    # Recharts sets inline style attributes, hence 'unsafe-inline' for styles only; scripts are same-origin only.
    "Content-Security-Policy": ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
                                "frame-ancestors 'none'; base-uri 'none'; form-action 'self'"),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}
ASSET_CACHE = "public, max-age=31536000, immutable"   # Vite puts a content hash in every asset name

JobId = Annotated[int, Path(ge=1, le=2**63 - 1)]       # a bigint; anything larger would be a database error
EmailId = Annotated[int, Path(ge=1, le=2**63 - 1)]
# 422 keeps FastAPI's schema (a list of field errors); the database's own 422 has a plain detail string.
ERRORS = {code: {"model": ErrorBody} for code in (403, 404, 409, 503)}
STORE_ERRORS = {NotFound: 404, EmailNotFound: 404, EmailChanged: 409, Forbidden: 403, Invalid: 422, Conflict: 409, ProfileChanged: 409, TrackingChanged: 409, HistoryChanged: 409, Unavailable: 503}


def host_without_port(host: str) -> str:
    host = host.strip().lower()
    if host.startswith("["):                          # [::1]:8080
        return host[1:host.find("]")] if "]" in host else ""
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


class LoginBody(BaseModel):
    password: str = Field(max_length=1024)


class Me(BaseModel):
    authenticated: bool
    expires_at: datetime


def create_app(settings: Settings, store, sessions: Sessions | None = None,
               limiter: LoginLimiter | None = None, scraper: ScraperClient | None = None) -> FastAPI:
    app = FastAPI(title="jobsearch dashboard", docs_url=None, redoc_url=None, openapi_url="/api/openapi.json")
    app.state.settings = settings
    app.state.store = store
    app.state.sessions = sessions = sessions or Sessions(settings.secret, settings.session_days)
    app.state.limiter = limiter = limiter or LoginLimiter()
    app.state.scraper = scraper = scraper or ScraperClient(settings.scraper_url)
    static_dir = settings.static_dir.resolve()

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if host_without_port(request.headers.get("host", "")) not in settings.allowed_hosts:
            response = JSONResponse({"detail": "unknown host"}, status_code=400)
        else:
            response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        path = request.url.path
        if path.startswith("/api"):
            response.headers["Cache-Control"] = "no-store"
        elif path.startswith("/assets/") and response.status_code == 200:
            response.headers["Cache-Control"] = ASSET_CACHE
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, e: RequestValidationError):
        # FastAPI's own answer echoes the submitted value, and a NaN in it cannot be written as JSON (a 500). The
        # location, the message and the kind are all a screen needs.
        errors = [{"loc": [str(p) for p in err.get("loc", ())], "msg": str(err.get("msg", "invalid")),
                   "type": str(err.get("type", ""))} for err in e.errors()]
        return JSONResponse({"detail": errors}, status_code=422)

    @app.exception_handler(StoreError)
    async def store_error(request: Request, e: StoreError):
        status = STORE_ERRORS.get(type(e), 500)
        body = ErrorBody(detail=str(e) or "error")
        if isinstance(e, EmailNotFound):
            body.detail = "suggestion not found"
        elif isinstance(e, EmailChanged):
            body.detail = (str(e) or "This suggestion changed.").strip().capitalize() + "."
        elif isinstance(e, NotFound):
            body.detail = "job not found"
        elif isinstance(e, Conflict):
            body = ErrorBody(detail="the status was changed elsewhere; reload to see it", current_status=e.current)
        elif isinstance(e, HistoryChanged):
            body.detail = "The history of this job changed since you looked (or there is nothing left to undo). Reload to see it."
        elif isinstance(e, TrackingChanged):
            body.detail = "These notes were changed elsewhere (another tab or device). Reload to see the latest version."
        return JSONResponse(body.model_dump(exclude_none=True), status_code=status)

    @app.get("/api/health")
    def health():
        return {"ok": True, "db": bool(store.ping())}

    @app.post("/api/login", status_code=204, responses={401: {"model": ErrorBody}, 429: {"model": ErrorBody}})
    def login(body: LoginBody):
        wait = limiter.retry_after()
        if wait:
            return JSONResponse({"detail": "too many attempts"}, status_code=429, headers={"Retry-After": str(wait)})
        if not password_ok(body.password, settings.password):
            limiter.failure()
            return JSONResponse({"detail": "wrong password"}, status_code=401)
        limiter.success()
        token, _ = sessions.issue()
        response = Response(status_code=204)
        # No Secure flag: the tunnel delivers the page as plain http://localhost, where browsers would drop it.
        response.set_cookie(COOKIE, token, max_age=sessions.max_age, path="/", httponly=True, samesite="strict")
        return response

    @app.post("/api/logout", status_code=204)
    def logout(request: Request):
        check_write(request)
        if sessions.verify(request.cookies.get(COOKIE)) is not None:
            sessions.revoke_all()
        response = Response(status_code=204)
        response.delete_cookie(COOKIE, path="/", httponly=True, samesite="strict")
        return response

    protected = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])

    @protected.get("/me", response_model=Me)
    def me(expires: int = Depends(require_auth)):
        return Me(authenticated=True, expires_at=datetime.fromtimestamp(expires, tz=timezone.utc))

    # Plain `def` routes: the store is synchronous, so FastAPI runs them in its thread pool.
    @protected.get("/summary", response_model=Summary, responses=ERRORS)
    def summary():
        return store.summary()

    @protected.get("/jobs", response_model=JobPage, responses=ERRORS)
    def jobs(q: Annotated[JobsQuery, Query()]):
        rows, total = store.jobs(q)
        return {"items": rows, "total": total, "page": q.page, "page_size": q.page_size}

    @protected.get("/jobs/{job_id}", response_model=JobDetail, responses=ERRORS)
    def job(job_id: JobId):
        return store.job(job_id)

    @protected.patch("/jobs/{job_id}/status", response_model=StatusResult, responses=ERRORS)
    def set_status(job_id: JobId, change: StatusChange):
        moved = store.move_status(job_id, change.status, change.expected_status, change.note)
        return StatusResult(id=job_id, status=moved["status"], event_id=moved["event_id"])

    @protected.get("/jobs/{job_id}/events", response_model=list[JobEvent], responses=ERRORS)
    def job_events(job_id: JobId):
        return store.job_events(job_id)

    # ---- version 3c: tailor this job. The scraper holds every rule (the switch, eligibility, one run at a time, a cooldown per
    # job, the spending caps); this only carries the page's request and the agreed cost, and shows the answers.
    @protected.get("/jobs/{job_id}/tailor/estimate", response_model=TailorEstimate, responses=ERRORS)
    def tailor_estimate(job_id: JobId):
        return scraper.tailor_estimate(job_id)

    @protected.get("/jobs/{job_id}/tailor", response_model=TailorState, responses=ERRORS)
    def tailor_status(job_id: JobId):
        return scraper.tailor_status(job_id)

    @protected.post("/jobs/{job_id}/tailor", response_model=TailorState, responses=ERRORS)
    def tailor_start(job_id: JobId, start: TailorStart):
        return scraper.tailor_start(job_id, start.max_cost_usd)

    @protected.post("/jobs/{job_id}/undo", response_model=UndoResult, responses=ERRORS)
    def undo_status(job_id: JobId, undo: UndoRequest):
        return store.undo_status(job_id, undo.expected_status, undo.event_id)

    @protected.get("/jobs/{job_id}/tracking", response_model=Tracking, responses=ERRORS)
    def job_tracking(job_id: JobId):
        return store.job_tracking(job_id)

    @protected.put("/jobs/{job_id}/tracking", response_model=Tracking, responses=ERRORS)
    def save_tracking(job_id: JobId, save: TrackingSave):
        patch = save.patch()
        if not patch:
            raise Invalid("nothing to change")
        return store.set_tracking(job_id, save.version, patch)

    @protected.get("/jobs/{job_id}/files/{kind}", response_class=FileResponse,
                   responses={200: {"content": {"application/pdf": {}}}, **ERRORS})
    def application_file(job_id: JobId, kind: Literal["resume", "cover_letter"]):
        path = files.resolve(store.application_file(job_id, kind), settings.output_dir, job_id)
        if path is None:
            raise NotFound("no such file")
        # the download name is ours; nothing from the stored path reaches the header
        return FileResponse(path, media_type="application/pdf", headers={
            "Content-Disposition": f'inline; filename="{job_id}-{kind}.pdf"',
            "Content-Security-Policy": files.PDF_CSP,
        })

    @protected.get("/export.csv", response_class=Response,
                   responses={200: {"content": {"text/csv": {}}}, **ERRORS})
    def export_csv(q: Annotated[JobsQuery, Query()]):
        return Response(csv_export.to_csv(store.export_rows(q)), media_type="text/csv; charset=utf-8", headers={
            "Content-Disposition": f'attachment; filename="jobs-{date.today().isoformat()}.csv"'})

    # ---- version 3a: the board (a move is the same PATCH /jobs/{id}/status as the table's menu) and the numbers.
    @protected.get("/board", response_model=Board, responses=ERRORS)
    def board():
        return store.board()

    @protected.put("/follow-ups/days", response_model=FollowUpDays, responses=ERRORS)
    def follow_up_days(body: FollowUpDays):
        return FollowUpDays(days=store.set_follow_up_days(body.days))

    # ---- version 6f: the email inbox. Reading and deciding go through the database's own functions (migration 025); accepting is
    # the same function as a click on the board, so it is in the history and Undo works.
    @protected.get("/email", response_model=EmailInbox, responses=ERRORS)
    def email_inbox(q: Annotated[EmailQuery, Query()]):
        return store.email_inbox(q.state, q.limit)

    @protected.get("/email/count", response_model=EmailCount, responses=ERRORS)
    def email_count():
        return store.email_count()

    @protected.post("/email/{suggestion_id}/accept", response_model=EmailAccepted, responses=ERRORS)
    def email_accept(suggestion_id: EmailId, body: EmailAccept):
        return store.email_accept(suggestion_id, body.expected_status, body.acknowledged)

    @protected.post("/email/{suggestion_id}/dismiss", response_model=EmailDecided, responses=ERRORS)
    def email_dismiss(suggestion_id: EmailId):
        return store.email_dismiss(suggestion_id)

    @protected.post("/email/{suggestion_id}/restore", response_model=EmailDecided, responses=ERRORS)
    def email_restore(suggestion_id: EmailId):
        return store.email_restore(suggestion_id)

    @protected.get("/stats", response_model=Stats, responses=ERRORS)
    def stats():
        return store.stats()

    @protected.get("/pipeline", response_model=Health, responses=ERRORS)
    def pipeline():
        return store.health()

    @protected.get("/spend", response_model=Spend, responses=ERRORS)
    def spend():
        return store.spend()

    # Pausing only ever stops spending; resuming starts the schedules again, still inside every budget cap.
    @protected.post("/pipeline/pause", response_model=PauseState, responses=ERRORS)
    def pause(body: PauseRequest | None = None):
        return store.set_pause(True, body.reason if body else None)

    @protected.post("/pipeline/resume", response_model=PauseState, responses=ERRORS)
    def resume():
        return store.set_pause(False, None)

    # ---- version 2b: settings. Reads and writes go to the scraper's own endpoints (settings_service.py), which hold
    # every rule; the page gets the fresh settings back after each change. Fields that stay a CLI action (raising a
    # cap, the approval mode, models) are not in the request models, so they are refused with a 422.
    def page_after_change():
        # The change is already saved when this runs. If the re-read fails, say so: "could not be saved" would be false.
        try:
            return settings_service.page(scraper)
        except Unavailable:
            raise Unavailable("Your change was saved, but the settings could not be reloaded: refresh the page.") from None

    @protected.get("/settings", response_model=SettingsPage, responses=ERRORS)
    def get_settings():
        return settings_service.page(scraper)

    @protected.patch("/settings/schedules/{name}", response_model=SettingsPage, responses=ERRORS)
    def change_schedule(name: ScheduleName, change: ScheduleChange):
        scraper.set_schedule(name, change.model_dump(exclude_none=True))
        return page_after_change()

    @protected.post("/settings/schedules/{name}/run", response_model=RunRequested, responses=ERRORS)
    def run_schedule(name: ScheduleName):
        return scraper.run_schedule(name)

    @protected.patch("/settings/features", response_model=SettingsPage, responses=ERRORS)
    def change_features(change: FeaturesChange):
        scraper.set_features(change.model_dump(exclude_none=True))
        return page_after_change()

    @protected.patch("/settings/tailoring", response_model=SettingsPage, responses=ERRORS)
    def change_tailoring(change: TailoringChange):
        scraper.set_tailoring(change.model_dump(exclude_none=True))
        return page_after_change()

    @protected.patch("/settings/budget", response_model=SettingsPage, responses=ERRORS)
    def lower_budget(change: BudgetChange):
        settings_service.lower_only(scraper, change.model_dump(exclude_none=True))
        return page_after_change()

    # ---- version 2c: the search profile (terms, sources, company boards, filters). The scraper validates it with the
    # CLI's checks and refuses a save made on an out-of-date read; this API only carries it and shapes the entries.
    @protected.get("/search", response_model=SearchPage, responses=ERRORS)
    def get_search():
        return settings_service.search_page(scraper)

    @protected.put("/search", response_model=SearchPage, responses=ERRORS)
    def save_search(save: SearchSave):
        profile = settings_service.to_scraper_profile(save.profile.model_dump())
        scraper.save_profile(profile, save.version)
        try:
            return settings_service.search_page(scraper)
        except Unavailable:
            raise Unavailable("Your change was saved, but the profile could not be reloaded: refresh the page.") from None

    @protected.post("/search/boards/find", response_model=FindResult, responses=ERRORS)
    def find_board(find: FindBoard):
        return scraper.find_board(find.name)

    app.include_router(protected)

    app.mount("/assets", StaticFiles(directory=static_dir / "assets", check_dir=False), name="assets")

    @app.api_route("/{full_path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    def spa(full_path: str):
        if full_path == "api" or full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not Found")
        if full_path:
            try:
                candidate = (static_dir / full_path).resolve()
                if candidate.is_relative_to(static_dir) and candidate.is_file():
                    return FileResponse(candidate)
            except (ValueError, OSError):      # a NUL byte (%00) or an over-long name: not a file, so the app's index
                pass
        index = static_dir / "index.html"
        if not index.is_file():
            return PlainTextResponse("The dashboard UI is not built. Rebuild the image: docker compose build dashboard",
                                     status_code=503)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    return app


class QuietHealthChecks(logging.Filter):
    """Drops the access-log line of the image's own health check (every 30 s, from inside the container); requests
    through the tunnel come from the Docker gateway and are still logged."""
    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args if isinstance(record.args, tuple) else ()
        return not (len(args) >= 3 and args[2] == "/api/health" and str(args[0]).startswith("127.0.0.1:"))


def build() -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s: %(message)s")
    logging.getLogger("uvicorn.access").addFilter(QuietHealthChecks())
    try:
        settings = load_settings()
    except SettingsError as e:
        print(f"dashboard: cannot start: {e}", file=sys.stderr)
        raise SystemExit(1) from None
    return create_app(settings, PgStore(settings.conninfo()))
