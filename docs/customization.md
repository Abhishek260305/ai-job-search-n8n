# Customization: use only what you want

Nothing in this project is forced on you. **Collecting, deduplicating, filtering and scoring jobs is the core.** Everything after that is optional, and every optional part has a documented fallback, so skipping it never leaves you stuck.

```
core (always on)                         optional (you choose)
collect → safety → dedupe → filter → score ──► tailoring ──► review ──► apply ──► track
                                               resume +     Telegram    (you)     by hand
                                               cover letter  or CLI               or Gmail (planned)
```

## Pick a setup

| | **A. Scoring only** | **B. Tailor, review at the CLI** | **C. Everything** |
|---|---|---|---|
| Jobs collected, deduped, filtered, scored | ✅ | ✅ | ✅ |
| Resume + cover letter generated | ❌ | ✅ | ✅ |
| Telegram (digest, review, commands) | ❌ | ❌ | ✅ |
| Where you look | the database: `jobs`, `job N`, `export` | `review` and the PDFs in `output/` | your phone, or the CLI |
| Approval step | none | none (`auto`) or at the CLI (`cli`) | on Telegram (`review`), or any other choice |
| Status after applying | by hand (`mark`) | by hand (`mark`) | by hand (`mark`); Gmail planned |
| Needs | Anthropic key | Anthropic key | Anthropic key + a Telegram bot |
| Typical cost | about $0.0003 per scored job | + about $0.05 per application | same as B |
| Switch to it with | `features set --tailoring off --telegram off` | `features set --telegram off` | `features set --tailoring on --telegram on` |

You can also mix: tailoring **on** with **no** Telegram, or Telegram **on** just for the daily digest and your commands with tailoring **off**. The choices are independent. Any of the three can add the [web dashboard](#the-web-dashboard-optional) as one more place to look.

You do not have to decide up front. The default install turns everything on, and [`/setup`](commands.md#setup-in-one-paragraph) asks which parts you want. You can change your mind at any time; changes apply immediately and survive restarts.

## Choosing your sources

Every job source is **one switch**: its name in `sources` in `config/search.json`. The names:

| Group | Names | Cost | Needs |
|---|---|---|---|
| `jobspy` | `indeed`, `naukri`, `linkedin` | free | nothing |
| `apify` | `linkedin_apify` | uses your Apify credit (capped, see [budget.md](budget.md)) | an Apify token in `.env` |
| `boards` | `greenhouse`, `lever`, `ashby`, `workable`, `smartrecruiters` | free | company slugs under `boards` ([sources.md](sources.md#company-job-boards)) |
| `feeds` | `remotive`, `remoteok`, `weworkremotely`, `himalayas`, `arbeitnow` | free | nothing |

(`glassdoor`, `ziprecruiter` and `google` are accepted names too, but they belong to no group, so `off jobspy` does not touch them.)

**Groups are shorthand for the command line only.** `config/search.json` lists single source names, never a group: a group name such as `boards` in `sources` is rejected by validation.

Nothing new runs unless you switch it on, and a config written before boards and feeds existed behaves exactly as before (once the database is up to date: run `scripts/jobsearch migrate` first, then rebuild the scraper). A board system runs only when it is in `sources` **and** has at least one company slug; a feed runs when it is in `sources`.

```bash
scripts/jobsearch sources                       # every source: on or off, what it costs, how many jobs it has brought
scripts/jobsearch sources on remotive greenhouse
scripts/jobsearch sources off boards            # the five company-board systems (your slugs are kept)
scripts/jobsearch sources off feeds
scripts/jobsearch sources only apify            # keep exactly these, switch everything else off
```

- Name a single source or a group (`boards`, `feeds`, `jobspy`, `apify`); names are not case-sensitive. `on` adds, `off` removes what you name, `only` keeps exactly what you name.
- The command updates **both** the database and `config/search.json`, so a later `search-profile apply` does not undo it, and it takes effect on the next collect run, with no restart. Because it applies the **whole** `config/search.json` to the database (not only `sources`), any other edit you have made to that file goes in with it.
- It refuses to switch **everything** off (a collect run would do nothing).
- If you have saved the search profile on the [dashboard](#the-web-dashboard-optional), `sources on/off/only` (like `search-profile apply`) refuses until you run `scripts/jobsearch search-profile pull`, which brings the file up to date first.
- Switching a board system off keeps its slugs; switching it on again is instant.

**Four combinations people use**

| You want | Run |
|---|---|
| Only Apify's LinkedIn search | `scripts/jobsearch sources only apify` |
| Apify plus the remote feeds | `scripts/jobsearch sources only apify feeds` |
| Everything except company boards | `scripts/jobsearch sources on jobspy apify feeds`, then `scripts/jobsearch sources off boards` (if everything is already on, just the second command) |
| Everything | `scripts/jobsearch sources on jobspy apify boards feeds` |

`apify` uses your Apify credit (the Apify caps still apply). `only` takes several names at once, so the second row is a single command.

**Company boards need slugs.** Add them under `boards` in `config/search.json`, then `scripts/jobsearch search-profile apply` (or on the dashboard's **Search** page, which also has a "Find a company" box); `scripts/jobsearch boards find "Company Name"` tells you which board system a company uses and prints the line to add. A worked example is in [sources.md](sources.md#adding-companies).

### The settings behind it

```json
"sources": ["indeed", "naukri", "linkedin", "greenhouse", "ashby", "remotive"],
"boards": {"greenhouse": ["cloudflare", "stripe"], "ashby": [{"slug": "openai", "name": "OpenAI"}]},
"board_title_words": []
```

| Setting | Meaning |
|---|---|
| `sources` | which sources are on (above) |
| `boards` | company slugs per system, each a string or `{"slug": "...", "name": "..."}` (a display name so the company matches its name on other boards); at most 300 in total. Optional. |
| `board_title_words` | extra words that make a **company-board or feed** job title qualify on their own (for example `["software", "platform"]`). Optional, default empty. See below. |

**Why `board_title_words` exists.** Company boards and feeds list everything, so a free filter keeps only jobs whose title contains a core word of one of your search terms (generic words such as `engineer`, `developer`, `software` and `senior` are ignored). It is strict on purpose, because every job that passes costs a little to score. With the search term `backend engineer`, a job titled "Software Engineer, Infrastructure" does **not** pass; add `"software"` to `board_title_words` if you want roles like that. The full rules (title, location, age) are in [sources.md](sources.md#how-the-jobs-are-filtered-before-anything-is-scored).

**How many boards and feeds one collect run visits** is its own setting, separate from the 18 searches:

```bash
scripts/jobsearch schedule set collect --boards 30      # default 30, from 1 to 200
```

A long list is visited in rotation, so with 100 companies and the default of 30, every company is visited about once a day (at the default 6-hour collect). The collect workflow spaces its requests 8 seconds apart, so 30 add about 4 minutes to a run.

**Cost.** Fetching boards and feeds is free (no paid API). The only cost is scoring the **new** jobs they bring, about $0.0003 each: see [costs.md](costs.md#company-boards-and-remote-feeds).

**Coverage.** Companies with their own career site, or on Workday, iCIMS, Taleo or Oracle (Amazon, Google, Meta, Apple, Netflix, ...) are not covered; keep Indeed and LinkedIn on for them.

## Schedules: when things run, and how much

Each stage runs on a schedule you control. Nothing is fixed inside n8n: one small scheduler workflow checks every 5 minutes what is due, so a change applies within 5 minutes and needs no restart.

```bash
scripts/jobsearch schedule                                  # what runs when, how much, last and next run
scripts/jobsearch schedule set collect --every 6h           # search every 6 hours (the default)
scripts/jobsearch schedule set collect --every 12h --batch 36
scripts/jobsearch schedule set collect --boards 50              # company boards and feeds visited per collect run
scripts/jobsearch schedule set digest --at 07:30            # the digest, daily at a time
scripts/jobsearch schedule set digest --every 6h            # or every N hours: a message whenever there are new matches
scripts/jobsearch schedule set score --every 15m --batch 20 --model claude-haiku-5-5
scripts/jobsearch schedule set tailor --off                 # pause one stage
scripts/jobsearch schedule run collect                      # run it once now (starts within 5 minutes)
scripts/jobsearch schedule timezone Asia/Kolkata            # times of day follow this timezone
```

| Stage | Default | Runs | One run does (`--batch`) | Limits |
|---|---|---|---|---|
| **collect** | every **6 hours** | searches the job boards, and reads the company boards and feeds you switched on | 18 searches (title × location × source), plus `--boards` company boards and feeds (default 30) | at most every 15 minutes (kind to the boards), up to 50 searches and 200 boards and feeds per run |
| **score** | every 30 minutes | scores new jobs with Claude | 25 jobs, model `claude-haiku-5-5` | every 5 minutes to 7 days, up to 50 per run |
| **tailor** | every 15 minutes | tailors shortlisted jobs | 3 applications | up to 10 per run; only while tailoring is on |
| **digest** | daily at **08:00** | the Telegram message listing new matches | 10 matches | a time of day **or** every N hours (at least 1 hour); only while Telegram and the digest are on |

**Scoring keeps up.** One collect run can add a couple of hundred new jobs, and scoring clears 25 every 30 minutes (about 1,200 a day, at about $0.0003 each; the spending caps still apply). If a backlog builds up, raise `schedule set score --batch`.

**Do I get a message every 6 hours?** Collect finds jobs every 6 hours, and they appear in your database (`scripts/jobsearch jobs`) once scored. A Telegram message arrives when something is *worth* telling you: the digest (new matches at or above your threshold; daily at 08:00 by default, or every N hours if you set `schedule set digest --every 6h`) and each tailored application. No new matches means no message.

**How much a "full pass" takes.** Your search settings define a number of combinations (for example 4 roles × 13 locations × 4 sources is about 176 after the planner drops combinations that make no sense). Collect works through them in rotation, `batch` at a time. At the default (18 every 6 hours) that is 72 a day, so a full pass takes about 2.5 days, and new jobs trickle in at every run. Searching more often, or more per run, finds new postings sooner but sends more requests to the boards (LinkedIn in particular rate-limits) and, through scoring, costs more. The spending caps still bound the cost.

**Details worth knowing**
- A stage is marked as started the moment it is claimed, so a slow run is never started twice. If a run fails, it is not retried until its next turn; use `schedule run NAME` to retry now.
- Switching a stage off (`schedule set NAME --off`) stops it; `schedule run NAME` still runs it once, because that is an explicit request.
- Stages whose part is switched off (tailoring, the digest) are left alone, and start promptly when you switch the part back on.
- If the server was down at digest time, the digest goes out when it is back, once that day.
- Times of day follow the timezone set with `schedule timezone`, which is the same timezone that decides where the spending-cap days and months begin.
- `scripts/jobsearch doctor` warns if the scheduler workflow is not published (then nothing runs on schedule).

## The switches

```bash
scripts/jobsearch features                                   # what is on, and why anything is off
scripts/jobsearch features set --tailoring off               # stop at scoring
scripts/jobsearch features set --telegram off --digest off   # no Telegram, even if it is configured
scripts/jobsearch features set --tailoring on                # turn it back on
```

| Switch | On means | Off means (the fallback) |
|---|---|---|
| **tailoring** | shortlisted jobs get a tailored resume and cover letter (in `mode auto`, by themselves; in `mode manual`, when you ask) | the pipeline **stops at scoring**. Shortlisted jobs simply wait in the database with their score, strengths and gaps. No resume or cover-letter model calls are made, and `/tailor` explains that it is switched off. Use `jobs`, `job N`, `export` to see them. |
| **telegram** | you get review messages, commands and (if the digest is on) the digest | no messages and no commands. Everything Telegram does has a CLI equivalent (below). It is also **off automatically** if `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` are not in `.env`, so you never have to touch it if you never set Telegram up. |
| **digest** | a morning message lists new matches | no morning message. Telegram commands still work. It needs Telegram, so it is off whenever Telegram is. |

Scoring itself is not a switch: it is the point of the pipeline. (It does need your Anthropic key; see [limits](#what-is-not-optional).)

### What each Telegram feature becomes without Telegram

| With Telegram | Without it |
|---|---|
| the morning digest of new matches | `scripts/jobsearch jobs` (your shortlist, best first; `--days 1` for "what came in today") or `scripts/jobsearch export -o jobs.csv` for a spreadsheet |
| a message with the PDFs for each application | files in `output/applications/<id>-<company>-<title>/`, and `scripts/jobsearch files N` tells you where (and the `scp` command on a remote host) |
| `/review`, `/approve`, `/skip`, `/applied` | `scripts/jobsearch review`, `approve`, `skip`, `applied` |
| `/mark 24 interview` | `scripts/jobsearch mark 24 interview` |
| `/tailor 24` | `scripts/jobsearch tailor 24` |
| `/status`, `/budget`, `/settings`, `/set` | `status`, `budget`, `tailor-settings` |
| the budget alert in the digest | `scripts/jobsearch budget` and `doctor` (check now and then) |

## Other things you can customize

| What | Where | Example |
|---|---|---|
| search terms, locations, sources, experience range, include/exclude words, companies to skip, how old a posting may be, score threshold | `config/search.json`, then `scripts/jobsearch search-profile apply` | [setup.md](setup.md#8-describe-yourself-and-what-you-want) |
| **which job sources** are used (Indeed, LinkedIn, Apify, company boards, remote feeds) | `scripts/jobsearch sources ...` | `sources off boards`, `sources only apify` ([above](#choosing-your-sources)) |
| **company job boards** to read, and extra title words for them | `boards` and `board_title_words` in `config/search.json`, then `search-profile apply` | `scripts/jobsearch boards find "Stripe"` |
| the scoring model and how many jobs per run | `schedule set score --model ... --batch ...` | `--model claude-haiku-5-5 --batch 25` |
| the tailoring model | `tailor-settings set --model ...` | `--model claude-sonnet-5-5` |
| tailoring mode, approval, score band (`--min-score`, `--max-score`), daily limit | `tailor-settings set ...` | `--max-score 80` |
| cover letter on/off, resume pages (1 or 2), fact check on/off | `tailor-settings set ...` | `--cover-letter off --max-pages 2` |
| **paper size** of the PDFs | `tailor-settings set --paper a4\|letter` | `--paper letter` |
| **tone and wording** of the writing | `tailor-settings set --style-notes "..."` (at most 400 characters; `none` clears) | `--style-notes "Warm but formal. Short sentences."` |
| spending caps and the warning level | `budget set --monthly N --daily N --warn-pct N` | `--monthly 5` |
| **Apify** spending caps | `budget set --apify-monthly N --apify-daily N` | `--apify-daily 0.25` |
| the emergency brake | `schedule pause` / `resume` | see [runaway-protection.md](runaway-protection.md) |
| which optional parts are on | `features set ...` | `--telegram off` |
| timezone | `schedule timezone ...` | `Europe/London` |

**Style notes** steer tone and word choice only. They are placed in the prompt as preferences that cannot override the honesty rules, the output format or the writing rules, and the audit and the fact check run exactly as before, so a note that tries to push in a claim your resume does not support gets that claim removed. To make a fact usable, add it to `config/resume.json`.

## Approval: three choices

Only relevant when tailoring is on. Set it with `scripts/jobsearch tailor-settings set --approval ...` or `/set approval ...`.

| `approval` | What happens to a finished application | Needs Telegram |
|---|---|---|
| `review` (default) | pushed to Telegram; waits there for `/approve` or `/skip` | yes |
| `cli` | no message; waits as a draft until you `approve` or `skip` at the command line | no |
| `auto` | stored as **ready to apply** immediately, nothing to wait for | no |

**No Telegram and approval `review`?** It behaves as `auto`: there is nobody to ask, so applications are stored ready. (`scripts/jobsearch features` says so.) If you want a human decision without Telegram, choose `cli`.

Whatever you choose, an application the honesty checks **flagged** ("needs attention") is never stored as ready automatically: it waits for you in the review queue. See [tailoring.md](tailoring.md).

## Tracking after you apply, without Gmail

Gmail tracking is planned (Phase 6) and will also be optional. Until then, and always as an alternative, you set the stage yourself, from any stage to any other:

```bash
scripts/jobsearch applied 24 --note "applied on the company site"
scripts/jobsearch mark 24 interview --note "phone screen Tuesday"
scripts/jobsearch mark 24 offer
scripts/jobsearch mark 24 rejected
scripts/jobsearch job 24            # see where it stands
```

Stages: `approved`, `skipped`, `applied`, `in_progress`, `interview`, `offer`, `rejected`, `withdrawn`. These work for **any job in the database**, including one that was never tailored (for example because tailoring is off): a "base" record without files is created. A job you found somewhere else is not in the database, so it cannot be marked; use `/check` for a one-off look at it, and keep a note of your own. In Telegram: `/mark 24 interview phone screen Tuesday`. On the [web dashboard](#the-web-dashboard-optional), the status dropdown in the jobs table does the same. The status line of `scripts/jobsearch status` counts applications by stage.

## The web dashboard (optional)

A page in your browser (Jobs, Health, Spend, Settings and Search pages) with the summary cards, two charts, every job with filters and its details, and a status dropdown to record each stage. It is a view over the same database, not a separate copy: a stage you set there shows up in `scripts/jobsearch jobs` and Telegram at once, and the other way round.

| | |
|---|---|
| Turn it on | `scripts/init-env.sh --add-dashboard`, then `docker compose up -d --build --no-deps dashboard-db-init dashboard` ([dashboard.md](dashboard.md#turn-it-on)) |
| Open it | `scripts/tunnel.sh user@host [key]`, then <http://localhost:8080> ([dashboard.md](dashboard.md#open-the-dashboard)) |
| Turn it off | `docker compose stop dashboard`, and remove `dashboard` from `COMPOSE_PROFILES` in `.env` |
| Costs | nothing: it makes no calls to Claude, Apify or Telegram and stores no data of its own (the "Find a company" box on the Search page sends up to 20 read-only requests to the company-board sites, through the scraper) |
| Without it | the CLI (`jobs`, `job N`, `mark`, `export`) and Telegram do everything it does |

From the dashboard you can change the schedules, the optional parts (tailoring, Telegram, the digest), the tailoring options, the spend caps (**lower only**), the search profile, the sources, the company boards and the pause switch. These stay commands: raising a cap, the approval mode, the models, the time zone and the style notes. After you save the search profile there, `search-profile apply` and `sources on/off/only` refuse until you run `scripts/jobsearch search-profile pull` (see [dashboard.md](dashboard.md#which-is-the-source-of-truth)).

## Recipes

**"I only want a ranked list of jobs in a spreadsheet."**
```bash
scripts/jobsearch features set --tailoring off --telegram off
scripts/jobsearch export --min-score 70 -o jobs.csv       # open it in Excel or Sheets
```
Leave the Telegram lines in `.env` empty. You will pay for scoring only (cents).

**"I want my phone to tell me about matches, but I write my own applications."**
```bash
scripts/jobsearch features set --tailoring off           # Telegram and the digest stay on
```

**"Tailor everything, but never message me."**
```bash
scripts/jobsearch features set --telegram off
scripts/jobsearch tailor-settings set --approval cli     # or auto, to skip the review step too
scripts/jobsearch review                                  # when you are ready to look
```

**"Only tailor when I ask."**
```bash
scripts/jobsearch tailor-settings set --mode manual      # then: scripts/jobsearch tailor 24, or /tailor 24
```
On the dashboard: Settings, Tailoring, mode **Manual**, then open any job and press **Tailor this job…** (it shows the cost and asks first).

**"Tailor only the jobs that need it."**
```bash
scripts/jobsearch tailor-settings set --max-score 80     # at 80+ your base resume is used as written
```

**"Also read some company career pages and remote feeds."**
```bash
scripts/jobsearch boards find "Stripe"              # which system a company uses, and the line to add
# add the slugs under "boards" in config/search.json, then:
scripts/jobsearch sources on greenhouse remotive
scripts/jobsearch search-profile apply
```

**"Stop reading company boards and feeds."** `scripts/jobsearch sources off boards feeds` (nothing else changes; your slugs stay in the file).

**"Search every 6 hours."** That is the default. Check with `scripts/jobsearch schedule`.

**"Search more often, but gently."** `scripts/jobsearch schedule set collect --every 3h --batch 12` (same coverage per day as the default, in smaller, friendlier bursts).

**"Send my digest at 7:30."** `scripts/jobsearch schedule set digest --at 07:30`.

**"Tell me whenever new matches show up."** `scripts/jobsearch schedule set digest --every 6h` (or `--every 1h`): a message goes out only when there are new matches.

**"Pause one stage, or everything, for a while."** `scripts/jobsearch schedule set collect --off` (and `--on` to resume); or `docker compose stop n8n` stops every schedule at once and `docker compose start n8n` resumes them. See [daily-use.md](daily-use.md#pausing-and-stopping).

**"Add Telegram later."** Create the bot ([setup.md](setup.md#3-create-the-telegram-bot-optional)), put the token and chat id in `.env`, then:
```bash
docker compose up -d scraper
scripts/n8n-credentials.sh
scripts/n8n-import-workflows.sh --publish    # publishes the Telegram workflows now that they can work
```

**"Remove Telegram."** Blank the two lines in `.env`, `docker compose up -d scraper`, then `scripts/n8n-import-workflows.sh --publish` (it unpublishes the two Telegram workflows). Or just `features set --telegram off` and change nothing else.

## What is not optional

- **The Anthropic API key and scoring.** Scoring is the core; it uses a cheap model and costs about $0.0003 per job. A mode that shortlists by the free filters alone, with no AI at all, is not built yet. It is a reasonable future option; open an issue if you want it.
- **The safety checks and the spending caps.** They protect you and cost nothing; they are not switches.
- **The human at the end.** There is no auto-apply, in any setup.

## Where the settings live

| Setting | Stored in | Change with |
|---|---|---|
| tailoring, telegram, digest switches | database (`feature_settings`) | `scripts/jobsearch features set ...` |
| when each stage runs, how much (including company boards and feeds per collect run), the scoring model, the timezone | database (`schedule_settings`) | `scripts/jobsearch schedule set ...` |
| tailoring behaviour (mode, approval, score band, daily limit, cover letter, pages, fact check, model) | database (`tailor_settings`) | `scripts/jobsearch tailor-settings set ...` or `/set` |
| what to search for, filters, score threshold, company boards and their slugs, extra title words | `config/search.json` → database | edit, then `scripts/jobsearch search-profile apply` (or the dashboard's Search page; once it has saved, run `search-profile pull` before editing the file again) |
| which sources are on | `sources` in `config/search.json` **and** the database | `scripts/jobsearch sources ...` (updates both) |
| spend caps | database (`budget_settings`) | `scripts/jobsearch budget set ...` |
| Telegram token and chat id, API keys | `.env` (never in git) | edit, then `docker compose up -d` |
| the dashboard: on or off, its password and secrets | `.env` (`COMPOSE_PROFILES`, `DASHBOARD_*`) | `scripts/init-env.sh --add-dashboard`, or edit, then `docker compose up -d --no-deps dashboard` |
