# Roadmap

We build in phases, each one tested and documented before the next starts. ✅ done, 🚧 in progress, ⬜ planned.

## Where things stand

**Phases 0-5 (with 5b optional parts, 5c customizable schedules and 5d runaway protection) and Phase 7 (company job boards and remote feeds) are done:** the pipeline collects, filters, scores, tailors and sends applications for your review, under a hard spending cap, and every part after scoring is optional. **What remains is optional: Phase 6 (Gmail status tracking; its foundations, step 6a, are built, the mailbox reader is not), Phase 8 (extras) and the later versions of Phase 9 (the web dashboard: versions 1 and 2a to 2c are built, tested and running, see [dashboard.md](dashboard.md)).**

| Phase | What | State |
|---|---|---|
| 0 | Foundation: Docker stack, schema, dedupe, sources | ✅ |
| 1 | Safety: scam and prompt-injection checks | ✅ |
| 2 | Data model and scoring | ✅ |
| 3 | Commands and onboarding (`/setup`, CLI) | ✅ |
| 4 | Collect, filter, score in n8n; budget and spend cap | ✅ |
| 5 | Tailoring, honesty checks, PDFs, Telegram/CLI review | ✅ |
| 5b | Optional parts: switches, no-Telegram path, manual tracking, CSV export | ✅ |
| 5c | Customizable schedules, scoring model, paper size, style notes | ✅ |
| 5d | Runaway protection: counted attempts, Apify caps, global pause, stand-in APIs and a rehearsal | ✅ |
| 6 | Gmail status tracking (optional): suggestions you accept, never automatic changes ([gmail.md](gmail.md)) | 🚧 6a to 6f deployed; 6g planned |
| 7 | More sources: company job boards (five systems) and remote feeds (five feeds) | ✅ |
| 8 | Extras (company research, interview prep) | ⬜ optional |
| 9 | Web dashboard: summary, charts, jobs table, status editing, health, spend, settings and search profile; later Kanban ([dashboard.md](dashboard.md)) | 🟨 versions 1 and 2a to 2c ✅ (optional, deployed 2026-10-12); 3a (history, board, numbers), Undo, 3b (notes, contacts, follow-ups) and 3c (tailor one job on request) built, tested, deployed and on GitHub |

### Known limits (read these)

- **The honesty checks are strong, not perfect.** A model can still phrase something misleadingly in a way no word list or second model catches. Read every document before you send it.
- **No auto-apply, by design.** You submit applications and tell the system with `/applied`. Until Phase 6, you record what happens after that yourself with `mark` (interview, offer, rejected, ...).
- **Scoring needs the Claude API.** Tailoring, Telegram and the digest are optional; scoring is the core. A filters-only mode with no AI at all is not built (an option for later).
- **LinkedIn via JobSpy is rate-limited** and may return nothing on some runs; the Apify source is the more reliable option but costs a little ([sources.md](sources.md)).
- **Company boards cover only five job-board systems.** Companies with their own career site, or on Workday, iCIMS, Taleo or Oracle (Amazon, Google, Meta, Apple, Netflix, ...) are not covered; Indeed and LinkedIn remain the way to see their roles ([sources.md](sources.md#what-is-not-covered)).
- **The company-board and feed filter is strict by design.** A title such as "Software Engineer, Infrastructure" does not match the term "backend engineer" unless `board_title_words` includes `software`. Himalayas lists very many jobs and only its newest 60 are read per fetch; Arbeitnow is Germany-heavy; a bare city without a country is recognised for India only ([sources.md](sources.md#how-the-jobs-are-filtered-before-anything-is-scored)).
- **Scam and eligibility checks are heuristics.** Expect some false alarms and some misses ([safety.md](safety.md)). Work-authorization gates are only as good as the facts in your profile.
- **Postgres 16 works but n8n recommends 17+.** Harmless; upgrading needs a dump and restore.
- **One user, one instance.** Telegram is locked to a single chat id; there is no multi-user mode.
- **Scraping may breach a board's terms.** You decide what to enable ([README](../README.md#responsible-use)).

### Things only you can do

- Set a spend limit in the Anthropic Console (the second layer of the cap): [budget.md](budget.md).
- Read and approve each application; mark it applied once you submit it.
- Keep `.env` (especially `N8N_ENCRYPTION_KEY`) backed up, and keep personal files out of git.

## Phase 0: Foundation ✅
- ✅ Docker stack: n8n + Postgres + scraper service, free-tier friendly
- ✅ Postgres schema with cross-source dedupe (`upsert_job()`)
- ✅ JobSpy sources (Indeed, Naukri, LinkedIn verified from a cloud VM), failures reported in `errors`
- ✅ Apify LinkedIn adapter: three actors, credit-aware account selection, hard result and charge caps
- ✅ Public-repo structure: README, docs, LICENSE, `.gitignore`, example configs

## Phase 1: Safety ✅
- ✅ Link/host verification (known boards and ATS, look-alikes, user-info tricks, shorteners, chat links)
- ✅ Scam red-flag rules (fees, earn-per-day, ID/bank requests, chat-app contact, free-email recruiters, ...)
- ✅ Prompt-injection defence for postings (hidden-content stripping, injection signals, fenced untrusted text)
- ✅ Risk label (`low | medium | high`) attached to every job the scraper returns
- ✅ Measured on 50 live postings: 48 low, 2 medium, 0 high; one false alarm fixed
- See [safety.md](safety.md)

## Phase 2: Data model and scoring schema ✅
- ✅ Migrations mechanism (`postgres/migrate.sh`, `schema_migrations`); fresh installs and existing databases both tested
- ✅ New `jobs` columns: `scam_risk`, `scam_score`, `scam_flags`, `deadline`, `posting_language`, `yoe_min/max`, `scores`, `gates`, `verdict`, `strengths`, `gaps`; new status `scam_blocked`
- ✅ `upsert_job` v2 (safety and deadline fields; high-risk postings are blocked on insert); old call shape still works
- ✅ Scoring rubric (technical 30 / experience 25 / behavioral 15 / career 30), bands, and eligibility/language/location gates, all computed in code
- ✅ Strict JSON output validation; the model cannot set the score, verdict or status
- ✅ `/score/prepare` and `/score/finalize` endpoints, `record_score()` SQL function, profile validation
- ✅ Deadline sweep (`sweep_expired()`) and `v_shortlist` view with closing-soon and stale flags
- ✅ Fix: non-Latin company and title names no longer collapse to empty in dedupe
- See [scoring.md](scoring.md)

## Phase 3: Commands and onboarding ✅
One tested Python core, with thin wrappers on top.
- ✅ `scripts/jobsearch` CLI: `doctor`, `status`, `migrate`, `validate`, `import-linkedin`, `conflicts`, `search-profile`, `check`
- ✅ `/setup` (Claude Code): resume + LinkedIn export + interview; conflicts surfaced for you to decide; bullets kept verbatim; files written only after you confirm
- ✅ `/doctor`, `/status`, `/check` slash commands wrapping the CLI; `AGENTS.md` (imported by `CLAUDE.md`) with the repo's working rules and the AI-assisted setup guide
- ✅ Deterministic LinkedIn importer (whitelisted files and columns only), resume/search validators, resume-vs-LinkedIn conflict finder
- ✅ Remote-stack support (`JOBSEARCH_SSH`), verified against the real VM
- ✅ Run for real on the maintainer's own resume + LinkedIn export: profile, resume and search settings written, applied to the database, `doctor` all green
- ⬜ Moved to later phases: `/tailor` (Phase 5, needs the tailoring pipeline) and `/outcome` (Phase 6, needs email status tracking)
- See [commands.md](commands.md)

## Phase 4: Collect, filter, score (n8n) ✅
- ✅ Migrations 003 (rotation cursor, scoring claims, digest tracking) and 004 (location-aware dedupe, duplicate merge)
- ✅ Free rule filters in Python with a reason for every drop: excluded title words (whole words), include keywords, excluded companies, posting age, remote, and a conservative years-of-experience extractor (49 tests)
- ✅ Planner: rotating slices of (title × location × source); Naukri only for India, Indeed only for supported countries; source varies fastest so a batch mixes boards
- ✅ Service endpoints: `/pipeline/plan`, `/jobs/ingest`, `/jobs/prepare-batch`, `/jobs/{id}/score`, `/digest`, `/digest/sent`, `/maintenance/sweep`, `/maintenance/reassess`
- ✅ Three n8n workflows (collect, score, digest) with stable ids; only Anthropic and Telegram credentials needed; `scripts/n8n-credentials.sh`
- ✅ Run live on the maintainer's stack: collection through n8n (105 jobs from three boards), the scoring loop against an API stub (valid reply, unusable reply, API outage), digest endpoints
- ✅ Fixes found on real data: fraud-warning paragraphs no longer flagged as scams; cross-board duplicate detection for state codes and multi-city postings
- ✅ Real Claude calls verified (credential, model, caching): first live run exposed a truncation bug (thinking on by default, verbose JSON), fixed and re-run: 10 jobs scored, about $0.0003 each
- ✅ Telegram digest verified live (real send through n8n, sent-marker prevents repeats); all three workflows published and scheduled
- ✅ **Budget** (migration 005): usage logging, `model_pricing`, hard monthly and daily caps with worst-case reservation, alerts in the digest, `jobsearch budget`; see [budget.md](budget.md)
- See [workflows.md](workflows.md)

## Phase 5: Tailoring ✅
- ✅ Resume as JSON; the model returns a **patch** (reorder, hide, reword, emphasise), never a rewrite; every change is traceable by bullet id
- ✅ Two honesty layers: a deterministic grounding audit (numbers, technologies, names, experience claims, rewrites held to their own bullet) with one revision and automatic repair, then a Haiku **fact check** of claims against the resume (unsupported = removed, exaggerated = shown to you)
- ✅ Requirement coverage (matched / bridged / gap), writing-style rules, em-dash cleanup
- ✅ HTML to PDF through Gotenberg; page-count and text-layer verification; binary-search auto-fit to the page limit
- ✅ Settings in the database: mode (auto/manual), approval (review/auto), score band (min/max), daily limit, cover letter, pages, fact check, model
- ✅ Review by Telegram commands **and** CLI (`/approve`, `/skip`, `/applied`, `/tailor`, `/files`, `/settings`), authorised chat only; polling is single-flight and at-most-once
- ✅ Budget-integrated: every call priced at its worst case first, usage logged per purpose (tailor / tailor_verify)
- ✅ Run live on real data: first real application cost about $0.048 (draft + revision) on Sonnet 5.5; the fact check caught an overclaim the word audit could not
- ✅ Fixes found live: Sonnet 5.5 rejects `thinking: disabled` (the client adapts per model); a poison-message bug in Telegram polling; a self-contradicting checker (filtered)
- See [tailoring.md](tailoring.md)

## Phase 5b: Optional parts ✅
Nothing after scoring is forced.
- ✅ `feature_settings` (migration 008) with switches for **tailoring**, **Telegram** and the **digest**, and `scraper/features.py` that combines a switch with what is configured and says why a part is off
- ✅ Tailoring off = the pipeline stops at scoring: scheduled runs are quiet no-ops, an explicit `/tailor` is refused with how to turn it on, no model calls are made
- ✅ Telegram is off when not configured (no token needed anywhere); the digest endpoint returns nothing instead of failing; the install script publishes the Telegram workflows only when Telegram is set up and unpublishes them otherwise
- ✅ Approval gains `cli` (no message, decide at the command line); `review` without Telegram behaves as `auto`; flagged applications are never auto-approved
- ✅ Manual tracking without Gmail: `set_application_status()` and `jobsearch mark` / `/mark` (any stage from any stage); `approve`, `skip` and `applied` work on jobs that were never tailored
- ✅ The database is usable on its own: `jobs`, `job N`, `export` (CSV, formula-safe)
- ✅ `/setup` asks which parts you want; `/features`, `/jobs`, `/mark` slash commands; the doctor no longer nags about parts you do not use
- ✅ 612 service tests (including HTTP-level tests of the switches), 84 CLI tests, SQL tests for the migration
- See [customization.md](customization.md)

## Phase 5c: Customizable schedules and more settings ✅
- ✅ `schedule_settings` (migration 009) and `claim_due_schedules()`: each stage runs every N minutes (or daily at a time), with its own batch size, on/off and run-now; the scoring model is a setting; one timezone
- ✅ One scheduler workflow (`00`) ticks every 5 minutes and starts 01-04 through their *Run on demand* trigger; the others lost their hard-coded timers, so a change applies within 5 minutes with no restart
- ✅ Defaults: collect every **6 hours** (18 searches per run, about 72 a day), score 30 min, tailor 15 min, digest 08:00; collect cannot be faster than every 15 minutes (kind to the boards)
- ✅ Stages whose part is switched off are not claimed, so no empty runs and they start promptly when switched on
- ✅ `scripts/jobsearch schedule [set|run|timezone]`, `/schedule`; the doctor shows the schedule and warns when the scheduler workflow is not published
- ✅ Tailoring: paper size (A4 / Letter) and style notes (tone and wording, never lets the writing add facts)
- ✅ Fixed: the Telegram workflow left a row stuck at "running" for every minute-poll; n8n now prunes executions after 3 days
- ✅ 698 service tests, 103 CLI tests, SQL tests for the claim logic
- See [customization.md](customization.md#schedules-when-things-run-and-how-much)

## Phase 5d: Runaway protection ✅
Nothing can loop on money (see [runaway-protection.md](runaway-protection.md)).
- ✅ Audit of every paid path found two gaps: **no app-level Apify cap** (a collect run was costing about $0.2 to $0.3, which at four runs a day would have used the free credit in about two weeks), and **uncounted scoring and tailoring claims** (a job whose reply was never recorded could be re-paid for every lease period, forever)
- ✅ Migration 012: counted claims with refunds for attempts that cost nothing, parked jobs (`parked`, `requeue`), Apify monthly and daily caps (default $15 and $0.50), and a global pause (`schedule pause`/`resume`)
- ✅ Apify: a run starts only if its hard charge limit fits the remaining budget; at most 2 runs per search; failed runs that started are counted; a hung run is aborted; a budget that cannot be read means nothing is started
- ✅ A real Apify call showed the balance lags right after a run, so an unchanged balance counts as "unknown" (1.5 times the estimate), not free
- ✅ Scoring hands a claim back when the model gave no reply; the collect summary reports searches skipped by the budget; doctor warns about parked jobs, paused schedules, Apify limits and a stalled scorer
- ✅ Testing: stand-in Anthropic and Apify servers (`scraper/stubs.py`), a guard that makes any real network call in a unit test fail, and a rehearsal (`scraper/loop_rehearsal.py`) that runs the real code and SQL against a throwaway database with the stand-ins misbehaving: **55 of 55 checks within their bounds**
- ✅ 736 service tests, 111 CLI tests, SQL tests for the guards
- See [testing.md](testing.md)

## Phase 6: Gmail status tracking (optional) 🚧 6a to 6d deployed
The reader proposes status changes from the replies in your Gmail; you accept or dismiss each one. Nothing changes without you. Full description, rules and privacy: [gmail.md](gmail.md).
- ✅ **6a: foundations** (migration 021; built, tested and deployed 2026-10-12): the `gmail` switch (off by default), a `mail` stage in the scheduler and workflow `06 Check mail`, the tables `gmail_state`, `gmail_seen` and `email_suggestions`, `scripts/jobsearch gmail`, a doctor line, the dashboard API ready for the new switch and stage (the pages leave them out until 6f). The stage reads no mailbox yet
- ✅ **6b** (deployed 2026-10-12; not yet tried against real Google): the Gmail client (two fixed Google hosts, read-only and checked, bounded, never logs a credential or an email) and its parser (an email is untrusted data; only Google's own authentication verdict is trusted), `scripts/jobsearch gmail login` (loopback + PKCE sign-in that saves the login without showing it) and `gmail check`, the stand-in Google in `scraper/stubs.py`, Google patterns in the secrets scanner, `scripts/init-env.sh --add-gmail`
- ✅ **6c** (deployed 2026-10-12, migration 022; the scheduled stage does not run it until 6e): the search (your companies as sender-or-subject terms and the ATS senders, bounded, newest first, carrying on by date when a search fills up, `gmail rescan` for old mail), the matcher (evidence points, whole-word short names, no placeholder companies, ambiguity is never guessed, unfamiliar domain endings flagged), the rules classifier (acknowledgement, assessment, interview, offer, rejection, noise, unclear), the transition rules in code, and the flags (unverified sender, look-alike sender, free mail, scam wording, suspicious links, text aimed at an AI, phishing risk for offers)
- ✅ **6d** (deployed 2026-10-13, migration 023; the scheduled stage does not run it until 6e): Claude (Haiku) classification for the unclear cases under the spending caps: one-line JSON the code validates (the quote must be in the email), the model never chooses the status and is never more than medium confidence, counted claims (three, then parked, refunded when nothing was spent), at most 10 calls a run and 40 a day, an email that carries instructions is never sent. See [gmail.md](gmail.md#the-model-step-6d).
- ✅ **6e** (deployed 2026-10-13, migration 024): the scheduled `mail` stage reads mail (login check, search, model step, counts recorded), a rate limit is an early run and not a login failure, the doctor reports a login that expired / runs that keep ending early / parked emails, and the Telegram digest announces new suggestions once (company, job, what the reply looks like, never the subject or text) and an expired login once. See [gmail.md](gmail.md#the-scheduled-stage-6e).
- ✅ **6f** (deployed 2026-10-13, migration 025): the dashboard's Email page: review, accept (the board's own change function: history and Undo), dismiss and bring back; stale suggestions only dismissable; a second look for flagged or low-confidence ones; the Gmail switch and the `mail` schedule on the Settings page, the `mail` stage and two Gmail alerts on Health, a pending count on the menu. See [dashboard.md](dashboard.md#version-6f-the-email-inbox).
- ⬜ **6g:** docs, independent review, deploy, one real run on your mailbox
- Rules it is built to: an email can only ever suggest *applied*, *interview*, *offer* or *rejected* (never anything after an offer: accepting or declining stays your decision); a signal that contradicts a final status is flagged, not proposed; offers get a phishing check (sender and company mismatch, failed authentication, payment requests); only matched emails are sent to Claude; read-only against Gmail; the manual path (`mark`) stays
- The statuses are the ones the system already has (what `set_application_status()` accepts): `approved, skipped, applied, in_progress, interview, offer, rejected, withdrawn`; the database refuses any other proposal, and fixes which status each kind of email maps to

## Phase 7: Company job boards and remote feeds ✅
Free, key-less sources next to Indeed, Naukri and LinkedIn. See [sources.md](sources.md).
- ✅ **Company job boards:** Greenhouse, Lever, Ashby, Workable and SmartRecruiters, read through their public JSON APIs for the companies whose slugs you list (`boards` in `config/search.json`; `scripts/jobsearch boards find "Name"` looks a slug up)
- ✅ **Remote feeds:** Remotive, RemoteOK, We Work Remotely, Himalayas and Arbeitnow
- ✅ **Every source is one switch** (its name in `sources`): `scripts/jobsearch sources`, `sources on|off|only` with the groups `boards`, `feeds`, `jobspy`, `apify`. Nothing new runs unless you switch it on; an old config behaves exactly as before (after `scripts/jobsearch migrate`; migrate first, then rebuild the scraper)
- ✅ A **free relevance filter** (title, location, age) runs before anything reaches scoring, with at most 25 jobs per company or feed per fetch; `board_title_words` widens the title rule. The jobs then go through the same safety check, dedupe, filters and scoring as every other source
- ✅ **Polite fetching enforced in code:** Remotive at most every 6 hours, the other feeds hourly, one company board every 10 minutes; fixed hosts only, validated slugs, 32 MB and 30 s limits, one retry, a 429 never retried; a source that fails is reported and skipped
- ✅ `schedule set collect --boards N` (default 30) sets how many boards and feeds one collect run visits; the doctor shows what is on and warns about slugs that keep failing or do not match the switches
- ✅ Migration 013 (`search_profile.boards` and `board_title_words`, `schedule_settings.board_batch`, the `fetch_state` table); the collect workflow file is unchanged
- ✅ Unit tests for every adapter against stand-in servers for all ten APIs, CLI tests, SQL tests for the migration
- Known limits: [above](#known-limits-read-these). Not built yet: Adzuna and Jooble (they need a free API key), Google Jobs via JobSpy (still to verify), a per-company reader for sites on Workday and similar

## Phase 8: Extras ⬜ optional
- ⬜ Company research cache with a TTL, reused for interview prep
- ⬜ Interview prep (STAR candidates mined from your resume)
- Follow-up reminders, weekly stats and the dashboard moved to Phase 9

## Phase 9: Web dashboard 🟨 versions 1 and 2 done
A React + Vite + TypeScript app with a Python (FastAPI) API in its own opt-in container, reached through an SSH tunnel and protected by a password. The full spec, the data mapping and the later versions are in [dashboard.md](dashboard.md).
- ✅ **Version 1 (deployed 2026-10-12):** summary cards (found, shortlisted, applied, in progress, interview, offer, rejected, new this week), a status donut and a funnel, the jobs table (server-side filters and sorting, tabs, CSV export), status editing through `set_application_status()`, a job details panel, PDF links, dark mode (the default) with a light option. Python (FastAPI) API with its own low-privilege database role. One migration (014) that adds views, three functions and the role; no new tables
- ✅ Turned on with `scripts/init-env.sh --add-dashboard` and one compose profile; `scripts/tunnel.sh` forwards it next to n8n; `scripts/jobsearch doctor` checks it
- ✅ Migration 015: `reshortlist` shortlists scored jobs that are above a threshold lowered after they were scored
- ✅ Hundreds of API and frontend tests, plus SQL tests for the migrations ([testing.md](testing.md#dashboard))
- ✅ **Version 2a (built, tested and deployed 2026-10-12):** health page (alerts, schedules, scoring queue, parked jobs, failing boards), spend page (Claude and Apify against their caps, 30 days by purpose, by model) and the pause/resume switch. Migration 016
- ✅ **Version 2b (built, tested and deployed 2026-10-12):** settings page: optional parts, schedules (with run now), tailoring and spending caps (lower only), all through the scraper's own validated endpoints. No migration; the scraper reports `board_batch`
- ✅ **Version 2c (built, tested and deployed 2026-10-12):** the Search page: search profile, sources and company boards, with "find a company". The database is the source of truth once the dashboard edits it; the CLI refuses to overwrite a dashboard edit until `search-profile pull`. Migration 017
- ⬜ **Requeue button** for parked jobs
- ✅ **Version 3a** (built and tested; install: [dashboard.md](dashboard.md#adding-version-3a-to-an-existing-install)): status history (`application_events`), Kanban board, the numbers (response rate, days to a first reply, weekly)
- ✅ **Undo** (built and tested, migration 020): take back a mistaken move so the history and the numbers never see it ([dashboard.md](dashboard.md#undo))
- ✅ **Version 3b** (built and tested; install: [dashboard.md](dashboard.md#adding-version-3b-to-an-existing-install)): notes, contacts and referral on any job, a follow-up date, the Follow up list and card badges, an optional section in the Telegram digest, `scripts/jobsearch follow-up`
- ✅ **Version 3c** (built and tested, no migration; install: [dashboard.md](dashboard.md#adding-version-3c-to-an-existing-install)): "Tailor this job" on request, with the cost confirmed first, one run at a time and a cooldown per job
- ⬜ **Later:** Gmail suggestions inbox (with Phase 6), match feedback, saved filters (phone access through Tailscale and the phone layout are done)

## Principles
- Free tier first; every paid call sits behind a cheaper filter.
- A human approves anything that leaves the system; there is no auto-apply.
- Every claim in a tailored document must be grounded in the user's own resume source.
- Posting text is untrusted data, always.
- Each phase ships with tests and docs.
- Everything beyond scoring is optional and has a fallback; no part is a prerequisite for another.
