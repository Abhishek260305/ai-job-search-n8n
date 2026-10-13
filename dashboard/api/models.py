"""Request and response models. They validate input before any SQL is built, and they are the OpenAPI schema
the frontend's TypeScript types are generated from (web/src/api/schema.d.ts)."""
from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, field_validator

# Every value the combined status can take: jobs.status and applications.status together (migration 014).
PipelineStatus = Literal[
    "new", "filtered_out", "scam_blocked", "scored", "shortlisted", "tailored", "drafted", "approved",
    "applied", "in_progress", "interview", "offer", "rejected", "withdrawn", "skipped", "expired",
]
# What set_application_status() accepts, so what the dropdown may offer. "drafted" only comes from tailoring.
EditableStatus = Literal["approved", "skipped", "applied", "in_progress", "interview", "offer", "rejected", "withdrawn"]
# scraper/sources.py ALL; tests/test_models.py fails if the two drift apart.
SourceName = Literal[
    "indeed", "naukri", "linkedin", "linkedin_apify", "glassdoor", "ziprecruiter", "google",
    "greenhouse", "lever", "ashby", "workable", "smartrecruiters",
    "remotive", "remoteok", "weworkremotely", "himalayas", "arbeitnow",
]
Tab = Literal["shortlisted", "applied", "scored", "filtered", "scam", "all"]
SortKey = Literal["score", "posted", "first_seen", "company", "title", "status"]
PAGE_SIZES = (25, 50, 100)


def _page_size(value: int) -> int:
    if value not in PAGE_SIZES:
        raise ValueError(f"page_size must be one of {', '.join(map(str, PAGE_SIZES))}")
    return value


class JobsQuery(BaseModel):
    """The jobs table's query string. Unknown parameters are refused, so a typo is a 422, not a silent no-op."""
    model_config = ConfigDict(extra="forbid", frozen=True)

    tab: Tab = "shortlisted"
    q: str | None = Field(default=None, max_length=100)
    status: list[PipelineStatus] = Field(default_factory=list, max_length=16)
    source: list[SourceName] = Field(default_factory=list, max_length=17)
    min_score: int | None = Field(default=None, ge=0, le=100)
    remote: bool = False
    posted_days: int | None = Field(default=None, ge=1, le=365)
    sort: SortKey = "score"
    order: Literal["asc", "desc"] = "desc"
    page: int = Field(default=1, ge=1, le=10_000)
    page_size: Annotated[int, AfterValidator(_page_size)] = 50


class JobRow(BaseModel):
    """One row of v_dashboard_jobs."""
    id: int
    title: str
    company: str | None
    location: str | None
    is_remote: bool | None
    salary_min: float | None
    salary_max: float | None
    salary_currency: str | None
    score: int | None
    verdict: str | None
    scores: dict[str, Any] | None
    scam_risk: str | None
    job_status: PipelineStatus
    filter_reason: str | None
    posted_at: datetime | None
    first_seen: datetime
    last_seen: datetime | None
    deadline: date | None
    closing_soon: bool
    application_status: PipelineStatus | None
    applied_at: datetime | None
    needs_attention: bool
    has_resume: bool
    has_cover_letter: bool
    pipeline_status: PipelineStatus
    sources: list[str]
    apply_url: str | None


class JobPage(BaseModel):
    items: list[JobRow]
    total: int
    page: int
    page_size: int


class JobSource(BaseModel):
    source: str
    url: str | None
    apply_url: str | None
    first_seen: datetime
    last_seen: datetime | None


class JobDetail(JobRow):
    """v_dashboard_job_detail plus every listing of the job."""
    description: str | None
    canonical_url: str | None
    score_reason: str | None
    strengths: list[Any] | None
    gaps: list[Any] | None
    gates: dict[str, Any] | None
    yoe_min: int | None
    yoe_max: int | None
    scam_score: int | None
    scam_flags: list[Any] | None
    posting_language: str | None
    variant: str | None
    notes: str | None
    review_note: str | None
    reviewed_at: datetime | None
    attention_reason: str | None
    cost_usd: float | None
    model: str | None
    sources_detail: list[JobSource]


class Cards(BaseModel):
    found: int
    shortlisted: int
    new_this_week: int
    applied: int
    in_progress: int
    interview: int
    offer: int
    rejected: int
    withdrawn: int


class ByStatus(BaseModel):
    """The donut: the current status of everything submitted, plus the shortlist as `not_applied`."""
    applied: int
    in_progress: int
    interview: int
    offer: int
    rejected: int
    withdrawn: int
    not_applied: int


class FunnelStep(BaseModel):
    step: str
    count: int


class Summary(BaseModel):
    threshold: int | None
    cards: Cards
    by_status: ByStatus
    funnel: list[FunnelStep]
    generated_at: datetime


class StatusChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: EditableStatus
    expected_status: PipelineStatus      # what the browser showed; a mismatch is a 409
    note: str | None = Field(default=None, max_length=500)


class StatusResult(BaseModel):
    id: int
    status: EditableStatus
    event_id: int | None = None          # the history entry this change wrote (what "Undo" is tied to); none if nothing was recorded


class ErrorBody(BaseModel):
    detail: str
    current_status: PipelineStatus | None = None


# ---------------------------------------------------------------- version 3a: history, board, stats
# One dict each from dashboard_job_events(), dashboard_board() and dashboard_stats() (migration 018); the shapes are
# checked against the real functions in tests/test_store_pg.py.

class JobEvent(BaseModel):
    id: int
    from_status: str | None              # none for the first event of an application
    to_status: str                       # equal to from_status for a note recorded without a change
    note: str | None
    source: Literal["dashboard", "backfill"] | None   # none: the CLI, Telegram or the pipeline made the change
    at: datetime
    undoable: bool = False               # only the newest entry, made on the dashboard, and only while the job is still as it left it


class BoardCard(BaseModel):
    id: int
    title: str
    company: str | None
    location: str | None
    is_remote: bool | None
    score: int | None
    pipeline_status: PipelineStatus
    applied_at: datetime | None
    deadline: date | None
    closing_soon: bool
    has_resume: bool
    has_cover_letter: bool
    needs_attention: bool
    follow_up: Literal["date", "waiting"] | None   # why it is due for a follow-up today, if it is
    status_since: datetime               # when it last changed status (not yet acted on: when it was scored)


BoardColumnKey = Literal["not_applied", "applied", "in_progress", "interview", "offer", "rejected"]


class BoardColumn(BaseModel):
    key: BoardColumnKey
    total: int                           # every job in the column
    items: list[BoardCard]               # at most 40


class BoardClosed(BaseModel):
    withdrawn: int
    skipped: int


class FollowUp(BaseModel):
    """One job that is due for a follow-up today (version 3b)."""
    id: int
    title: str
    company: str | None
    location: str | None
    score: int | None
    pipeline_status: PipelineStatus
    reason: Literal["date", "waiting"]   # your follow-up date has come, or it has waited the set number of days with no reply
    follow_up_on: date | None
    applied_on: date | None
    days_waiting: int | None
    contact_name: str | None
    contact_email: str | None
    referral: str | None


class Board(BaseModel):
    generated_at: datetime
    columns: list[BoardColumn]
    today: date                          # today in the configured time zone: the page counts reminders from it
    follow_up_days: int                  # days an application waits before it is due (the setting)
    follow_ups_total: int
    follow_ups: list[FollowUp]           # the first 20: dates first, then the longest waiting
    closed: BoardClosed


# ---------------------------------------------------------------- version 3b: notes, contacts, follow-up dates
# dashboard_job_tracking() and dashboard_set_tracking() (migration 019). The limits repeat the table's; the database
# is what enforces them, and tests/test_store_pg.py checks the two agree.

def _no_nul(value: str | None) -> str | None:
    if value is not None and "\x00" in value:       # Postgres text cannot hold it; better a 422 than a 500
        raise ValueError("a value cannot contain a NUL character")
    return value


class Tracking(BaseModel):
    notes: str | None
    contact_name: str | None
    contact_email: str | None
    referral: str | None
    follow_up_on: date | None
    version: str | None                  # opaque: send it back with the next save; none until something is saved
    today: date                          # today in the configured time zone (what "in 3 days" counts from)


class TrackingSave(BaseModel):
    """Only the fields sent change; null or an empty string clears one. `version` is what the last read said."""
    model_config = ConfigDict(extra="forbid")

    version: str | None = Field(default=None, max_length=64)
    notes: Annotated[str | None, Field(max_length=2000)] = None
    contact_name: Annotated[str | None, Field(max_length=100)] = None
    contact_email: Annotated[str | None, Field(max_length=200)] = None
    referral: Annotated[str | None, Field(max_length=200)] = None
    follow_up_on: date | None = None

    _nul = field_validator("version", "notes", "contact_name", "contact_email", "referral")(_no_nul)

    @field_validator("follow_up_on", mode="before")
    @classmethod
    def _blank_date_clears(cls, value):
        # an empty or blank string clears the date, like it does for the text fields
        return None if isinstance(value, str) and not value.strip() else value

    def patch(self) -> dict[str, str | None]:
        """The fields that were sent (even as null), ready for the database function."""
        out: dict[str, str | None] = {}
        for name in ("notes", "contact_name", "contact_email", "referral"):
            if name in self.model_fields_set:
                out[name] = getattr(self, name)
        if "follow_up_on" in self.model_fields_set:
            out["follow_up_on"] = self.follow_up_on.isoformat() if self.follow_up_on else None
        return out


class UndoRequest(BaseModel):
    """Take back the newest change of a job. `expected_status` is the status that change set (what the page showed after it);
    `event_id` is given when the user picked an entry in the timeline, and must then still be the newest."""
    model_config = ConfigDict(extra="forbid")

    expected_status: PipelineStatus
    event_id: StrictInt | None = Field(default=None, ge=1, le=2**63 - 1)


class UndoResult(BaseModel):
    id: int
    status: PipelineStatus               # what the job is after the undo


# ---------------------------------------------------------------- version 3c: tailor this job
# The scraper's own reports (scraper/app.py /tailor/estimate, /tailor/start, /tailor/status), shaped for the page. The scraper
# decides everything; the page only shows it. Nothing here carries a file path.

class TailorBudget(BaseModel):
    day_remaining: float
    month_remaining: float
    blocked: bool
    blocked_reason: str | None


class TailorExisting(BaseModel):
    status: str                          # what is there now: running it again replaces it
    has_resume: bool
    has_cover_letter: bool


class TailorEstimate(BaseModel):
    job_id: int
    can_run: bool
    reason: str | None                   # why not, in words, when can_run is false
    mode: str                            # the tailoring mode (auto or manual): the button works in both
    model: str
    cover_letter: bool
    uses_model: bool                     # false when nothing is sent to Claude (a job above the score band with no cover letter)
    variant: Literal["tailored", "base"] | None
    typical_usd: float                   # what it has cost lately
    at_most_usd: float                   # the most the guard allows it to cost: what the user agrees to
    existing: TailorExisting | None
    budget: TailorBudget


class TailorOutcome(BaseModel):
    job_id: int | None = None
    status: str                          # drafted, approved, budget_paused, failed, api_error ...
    variant: str | None = None
    needs_attention: bool | None = None
    attention: list[str] = Field(default_factory=list)
    cost_usd: float | None = None
    has_cover_letter: bool | None = None
    pages: int | None = None
    reason: str | None = None
    requirements: dict[str, int] | None = None


class TailorState(BaseModel):
    state: Literal["idle", "running", "done", "failed"]
    started_at: float | None = None
    finished_at: float | None = None
    result: TailorOutcome | None = None
    error: str | None = None
    cost_usd: float | None = None        # what a run that failed had cost before it stopped


class TailorStart(BaseModel):
    """`max_cost_usd` is the "at most" the user was shown; the scraper refuses if it would now be more."""
    model_config = ConfigDict(extra="forbid")

    max_cost_usd: StrictFloat | StrictInt = Field(ge=0, le=100)     # required: the page never starts a run without agreeing a cost


class FollowUpDays(BaseModel):
    model_config = ConfigDict(extra="forbid")

    days: StrictInt = Field(ge=1, le=90)


class WeekStats(BaseModel):
    week_start: date                     # a Monday, in the configured time zone
    found: int
    shortlisted: int
    applied: int


class Stats(BaseModel):
    generated_at: datetime
    timezone: str
    applied: int                         # every application ever submitted
    responded: int                       # of those, moved to in progress, interview, offer or rejected
    reached_interview: int
    reached_offer: int
    avg_days_to_reply: float | None      # from real history only; none until at least one reply was measured
    replies_measured: int
    weekly: list[WeekStats]              # the last 12 weeks, oldest first


# ---------------------------------------------------------------- version 2a: health, spend, pause
# One dict each from dashboard_health() and dashboard_spend() (migration 016); the shapes are checked against the
# real functions in tests/test_store_pg.py.

class PauseState(BaseModel):
    paused: bool
    paused_at: datetime | None
    reason: str | None


class PauseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=200)

    @field_validator("reason")
    @classmethod
    def _no_nul(cls, value: str | None) -> str | None:
        if value is not None and "\x00" in value:      # Postgres text cannot hold it; better a 422 than a 500
            raise ValueError("the reason cannot contain a NUL character")
        return value


class ScheduleRow(BaseModel):
    name: str
    enabled: bool
    every_minutes: int | None
    at_time: str | None                  # "08:00:00" for a daily schedule
    batch: int | None
    model: str | None
    last_run_at: datetime | None
    next_run_at: datetime | None         # none while the schedule is off
    overdue: bool                        # only ever true for collect and score (see migration 016)
    feature_off: bool                    # tailor with tailoring off, digest with the digest or Telegram off, mail with Gmail off: the scheduler skips it


class Queue(BaseModel):
    waiting_to_score: int
    waiting_over_3h: int
    last_scored_at: datetime | None


class ParkedItem(BaseModel):
    stage: Literal["score", "tailor"]
    job_id: int
    title: str
    company: str | None
    claims: int
    strikes: int


class Parked(BaseModel):
    total: int
    items: list[ParkedItem]              # the first 20


class SourceFailure(BaseModel):
    source: str
    key: str                             # a board's company token; empty for a feed
    last_fetched_at: datetime | None
    last_ok_at: datetime | None
    last_count: int | None
    failures: int                        # in a row
    last_error: str | None


class Sources(BaseModel):
    tracked: int
    failing: int
    items: list[SourceFailure]           # the failing ones, worst first, at most 50


AlertCode = Literal["paused", "claude_blocked", "claude_warn", "apify_blocked", "scoring_stalled", "parked",
                    "sources_failing", "schedule_overdue", "gmail_login", "gmail_stalled"]


class Alert(BaseModel):
    level: Literal["bad", "warn"]
    code: AlertCode
    message: str


class Health(BaseModel):
    generated_at: datetime
    paused: PauseState
    timezone: str
    schedules: list[ScheduleRow]
    queue: Queue
    parked: Parked
    sources: Sources
    alerts: list[Alert]


class ClaudeSpend(BaseModel):
    month_spent: float
    day_spent: float
    monthly_cap: float
    daily_cap: float
    month_remaining: float
    day_remaining: float
    warn: bool
    warn_pct: int
    blocked: bool
    blocked_reason: str | None
    month_start: datetime


class ApifySpend(BaseModel):
    month_spent: float
    day_spent: float
    monthly_cap: float
    daily_cap: float
    month_remaining: float
    day_remaining: float
    blocked: bool
    blocked_reason: str | None


class DailySpend(BaseModel):
    day: date
    provider: str                        # anthropic | apify
    purpose: str                         # score | tailor | verify | search ...
    calls: int
    cost_usd: float


class ModelSpend(BaseModel):
    provider: str
    model: str
    calls: int
    cost_usd: float
    input_tokens: int
    output_tokens: int


class Spend(BaseModel):
    generated_at: datetime
    timezone: str
    claude: ClaudeSpend
    apify: ApifySpend
    daily: list[DailySpend]              # the last 30 days
    models: list[ModelSpend]             # this month



# ---------------------------------------------------------------- version 2b: the settings page
# What the page reads comes from the scraper's own reports and what it changes goes to the scraper's own validated
# endpoints (scraper_client.py). The checks below only reject what could not be a value at all (a wrong type, an
# unknown field); the real rules (ranges, "collect at most every 15 minutes") are the scraper's, and its message is
# shown as it is. Fields that stay a CLI or .env action are not in these models on purpose, so the API refuses them:
# raising a budget cap, the approval mode (`auto` would approve applications for you), the models (the score schedule's
# too), the style notes and the time zone (it moves the day and month a budget counts from, which would hand back a
# daily cap already spent).

ScheduleName = Literal["collect", "score", "tailor", "digest", "mail"]
TailorMode = Literal["auto", "manual"]
Paper = Literal["a4", "letter"]


class ScheduleSetting(BaseModel):
    name: ScheduleName
    enabled: bool
    every_minutes: int | None
    at_time: str | None                  # "08:00" for a daily schedule (the digest)
    batch: int
    model: str | None                    # the score schedule's model
    board_batch: int | None              # collect only: company boards and feeds visited per run
    run_now: bool                        # a run was requested and waits for the next tick
    last_run_at: datetime | None
    next_run_at: datetime | None


class FeatureStatus(BaseModel):
    on: bool                             # what is actually in effect
    reason: str                          # empty when on, else what would turn it on


class FeatureFlags(BaseModel):
    tailoring: bool
    telegram: bool
    digest: bool
    gmail: bool = False                  # off until asked for (it also needs a Google login in .env); an older scraper does not report it


class FeatureSettings(BaseModel):
    flags: FeatureFlags                  # the switches
    effective: dict[Literal["tailoring", "telegram", "digest", "gmail"], FeatureStatus]   # switch AND configured (.env)
    approval: str                        # the approval mode as set
    approval_effective: str              # what it behaves as now: "review" becomes "auto" without Telegram


class TailoringSettings(BaseModel):
    mode: TailorMode
    min_score: int | None
    max_score: int | None
    daily_limit: int | None
    approval: str                        # shown, not editable here
    cover_letter: bool
    max_pages: int
    paper: str
    model: str                           # shown, not editable here


class BudgetCaps(BaseModel):
    monthly_cap_usd: float
    daily_cap_usd: float
    warn_pct: int
    apify_monthly_cap_usd: float
    apify_daily_cap_usd: float


class SettingsLimits(BaseModel):
    """The scraper's limits, for hints next to the fields (scraper/schedules.py). The scraper enforces them."""
    min_every: dict[str, int]
    max_every: int
    max_batch: dict[str, int]
    max_board_batch: int


class SettingsPage(BaseModel):
    timezone: str
    paused: bool
    paused_reason: str | None
    schedules: list[ScheduleSetting]
    features: FeatureSettings
    tailoring: TailoringSettings
    budget: BudgetCaps
    limits: SettingsLimits


class ScheduleChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: StrictBool | None = None
    every_minutes: StrictInt | None = None
    at_time: str | None = Field(default=None, max_length=5)
    batch: StrictInt | None = None
    board_batch: StrictInt | None = None


class FeaturesChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tailoring: StrictBool | None = None
    telegram: StrictBool | None = None
    digest: StrictBool | None = None
    gmail: StrictBool | None = None


class TailoringChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: TailorMode | None = None
    min_score: StrictInt | None = None
    max_score: StrictInt | None = None
    daily_limit: StrictInt | None = None
    cover_letter: StrictBool | None = None
    max_pages: StrictInt | None = None
    paper: Paper | None = None
    clear: list[Literal["min_score", "max_score", "daily_limit"]] = Field(default_factory=list, max_length=3)


class BudgetChange(BaseModel):
    """New caps. Each may only be lower than (or equal to) the current one; see settings_service.lower_only."""
    model_config = ConfigDict(extra="forbid")

    monthly_cap_usd: StrictFloat | StrictInt | None = Field(default=None, ge=0, le=100000, allow_inf_nan=False)
    daily_cap_usd: StrictFloat | StrictInt | None = Field(default=None, ge=0, le=100000, allow_inf_nan=False)
    apify_monthly_cap_usd: StrictFloat | StrictInt | None = Field(default=None, ge=0, le=100000, allow_inf_nan=False)
    apify_daily_cap_usd: StrictFloat | StrictInt | None = Field(default=None, ge=0, le=100000, allow_inf_nan=False)


# ---------------------------------------------------------------- version 2c: the search profile
# One document, read and replaced as a whole. The scraper validates it with the CLI's own checks (profile_tools.
# validate_search) and keeps the version (updated_at) so a save fails instead of overwriting a change made elsewhere.
BoardSystem = Literal["greenhouse", "lever", "ashby", "workable", "smartrecruiters"]
EditedBy = Literal["cli", "dashboard"]


class BoardEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str
    name: str | None = None              # a display name, so a posting can show "Notion" rather than "notion"


class SearchProfile(BaseModel):
    search_terms: list[str]
    locations: list[str]
    sources: list[str]
    allow_remote: bool
    min_yoe: int | None
    max_yoe: int | None
    include_keywords: list[str]
    exclude_keywords: list[str]
    exclude_companies: list[str]
    max_age_days: int
    score_threshold: int
    boards: dict[str, list[BoardEntry]]
    board_title_words: list[str]


class SourceRegistry(BaseModel):
    """What the scraper knows about sources (scraper/sources.py), for the page to list them."""
    sources: list[str]
    groups: dict[str, list[str]]
    board_systems: list[str]
    paid: list[str]
    descriptions: dict[str, str]
    limits: dict[str, int]


class SearchPage(BaseModel):
    profile: SearchProfile
    version: str                         # send it back unchanged with the save
    edited_by: EditedBy                  # "dashboard": config/search.json on your computer is behind
    registry: SourceRegistry


class SearchProfileIn(BaseModel):
    """The profile as the page sends it. Only the shapes are checked here; the rules are the scraper's."""
    model_config = ConfigDict(extra="forbid")

    search_terms: list[Annotated[str, Field(max_length=200)]] = Field(max_length=100)
    locations: list[Annotated[str, Field(max_length=200)]] = Field(max_length=100)
    sources: list[Annotated[str, Field(max_length=40)]] = Field(max_length=40)
    allow_remote: StrictBool
    min_yoe: StrictInt | None
    max_yoe: StrictInt | None
    include_keywords: list[Annotated[str, Field(max_length=200)]] = Field(max_length=200)
    exclude_keywords: list[Annotated[str, Field(max_length=200)]] = Field(max_length=200)
    exclude_companies: list[Annotated[str, Field(max_length=200)]] = Field(max_length=500)
    max_age_days: StrictInt
    score_threshold: StrictInt
    boards: dict[BoardSystem, list[BoardEntry]]
    board_title_words: list[Annotated[str, Field(max_length=100)]] = Field(max_length=100)


class SearchSave(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: SearchProfileIn
    version: str = Field(min_length=1, max_length=64)


class FindBoard(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=100)


class BoardMatch(BaseModel):
    system: BoardSystem
    slug: str
    jobs: int


class FindResult(BaseModel):
    name: str
    tried: list[str]
    matches: list[BoardMatch]


class RunRequested(BaseModel):
    requested: ScheduleName
    note: str


# ---------------------------------------------------------------- version 6f: the email inbox
# One dict from dashboard_email_inbox() / dashboard_email_count() / dashboard_email_accept() (migration 025); the shapes are checked
# against the real functions in tests/test_store_pg.py. The subject and the excerpt are text from an email: untrusted, shown as plain
# text only.

EmailState = Literal["pending", "accepted", "dismissed"]
EmailSignal = Literal["acknowledgement", "assessment", "interview", "offer", "rejection"]
# only what an email may propose (the database refuses anything else)
EmailProposed = Literal["applied", "interview", "offer", "rejected"]


class EmailQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    state: EmailState = "pending"
    limit: int = Field(default=50, ge=1, le=100)


class EmailItem(BaseModel):
    id: int
    job_id: int
    company: str | None
    title: str
    job_status: PipelineStatus
    current_status: PipelineStatus       # what the job is now (the application's status, or the job's): what Accept must be told it expects
    received_at: datetime
    from_domain: str
    subject: str
    excerpt: str
    signal: EmailSignal
    proposed_status: EmailProposed
    status_at_proposal: str              # the application's status when this was written ('none' when it had none)
    confidence: Literal["high", "medium", "low"]
    source: Literal["rules", "model"]
    flags: list[str]
    state: EmailState
    created_at: datetime
    decided_at: datetime | None
    event_id: int | None                 # the history entry an accept wrote (what Undo is tied to)
    stale: bool                          # the job moved on: the change no longer makes sense, so it can only be dismissed
    needs_ack: bool                      # a trust flag or low confidence: Accept asks for a second look
    can_accept: bool
    can_undo: bool
    can_restore: bool                    # a dismissed one, or an accepted one whose change was undone or overtaken: it can be reviewed again


class EmailCounts(BaseModel):
    pending: int
    accepted: int
    dismissed: int


class EmailInbox(BaseModel):
    enabled: bool                        # the Gmail part is switched on
    login: Literal["none", "ok", "expired", "error"]
    counts: EmailCounts
    items: list[EmailItem]


class EmailCount(BaseModel):
    enabled: bool
    pending: int


class EmailAccept(BaseModel):
    """Accept one suggestion. `expected_status` is the job's status the page showed (`current_status`); a mismatch is a 409.
    `acknowledged` says the person looked at the sender: required when the suggestion carries a trust flag or low confidence."""
    model_config = ConfigDict(extra="forbid")

    expected_status: PipelineStatus
    acknowledged: StrictBool = False


class EmailAccepted(BaseModel):
    id: int
    job_id: int
    status: EditableStatus
    event_id: int | None = None


class EmailDecided(BaseModel):
    id: int
    state: Literal["pending", "dismissed"]
