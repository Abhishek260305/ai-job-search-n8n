# Commands

Everyday control comes from one tested Python core, `scripts/jobsearch`, and thin wrappers over it:

- **Claude Code slash commands** (`.claude/commands/`): `/setup`, `/doctor`, `/status`, `/check`, `/budget`, `/tailor`, `/review`, `/features`, `/jobs`, `/mark`, `/schedule`, `/sources`. Open Claude Code in this repo and type the command. `/setup` is conversational (it reads your resume, asks you to settle conflicts, and writes your config).
- **The CLI** (`scripts/jobsearch`, stdlib-only Python 3.10+) works for anyone, with or without Claude Code.
- **Telegram commands** for day-to-day review, from your phone ([below](#telegram-commands)).

All three act on the same database, so you can mix them: approve on your phone, check the budget at the CLI.

| I want to... | Claude Code | CLI | Telegram |
|---|---|---|---|
| set up my profile | `/setup` | `validate`, `search-profile apply` | |
| check the stack is healthy | `/doctor` | `doctor` | |
| see what was found | `/status` | `status` | `/status` |
| check one posting | `/check` | `check` | |
| see or cap spend | `/budget` | `budget [set]` | `/budget` |
| tailor a job now | `/tailor` | `tailor N` | `/tailor N` |
| see what waits for me | `/review` | `review` | `/review` |
| approve / skip / mark applied | | `approve`, `skip`, `applied` | `/approve`, `/skip`, `/applied` |
| record a later stage by hand | `/mark` | `mark N interview` | `/mark N interview` |
| browse the shortlist, one job, a CSV | `/jobs` | `jobs`, `job N`, `export` | |
| switch optional parts on or off | `/features` | `features [set]` | |
| choose which job sources are used | `/sources` | `sources [on\|off\|only NAMES]` | |
| find a company's job-board name | `/sources` | `boards find "Company"` | |
| change when things run | `/schedule` | `schedule [set]` | |
| find the PDFs | | `files N` | `/files N` |
| change tailoring behaviour | | `tailor-settings [set]` | `/settings`, `/set` |

## Where the CLI runs

It runs next to the compose stack and reaches the containers through `docker compose exec`, so nothing extra is exposed on the network. To drive a stack on another machine, set:

```bash
export JOBSEARCH_SSH="ssh -i '/path/to/key' ubuntu@your-host"   # quote paths that contain spaces
export JOBSEARCH_REMOTE_DIR="~/jobsearch"                        # compose directory on that host
export JOBSEARCH_COMPOSE="sudo docker compose"                   # only if the remote user needs sudo
```

`~/jobsearch` is the default: if you cloned the repository into a folder with another name on the server (for example `ai-job-search-n8n`), set `JOBSEARCH_REMOTE_DIR` to it, or `push-config` would write your config to the wrong place. Only `scripts/jobsearch` uses these variables: the other scripts (`init-env.sh`, `backup.sh`, `n8n-*.sh`, `docker compose`) run on the server itself.

## CLI reference

| Command | What it does |
|---|---|
| `scripts/jobsearch doctor` | Checks containers, database migrations, the scraper, n8n, Apify accounts and credit, `profile.json`, `resume.json`, whether the search profile is still a placeholder, which sources are on, and company boards and feeds that keep failing or are set up inconsistently, and, if you turned it on, the web dashboard (running, published on `127.0.0.1` only, reaching the database). Exit code 1 only on failures; warnings (such as "run /setup") do not fail. |
| `scripts/jobsearch status` | Job counts by status, safety breakdown, top matches (🔥 closing soon, ⚠stale, ⚠medium-risk), applications by status, Apify credit. |
| `scripts/jobsearch budget [set --monthly N --daily N --warn-pct N --apify-monthly N --apify-daily N]` | Claude spend against your monthly and daily caps, the per-model breakdown, the separate Apify spend and caps, and whether anything is paused; `set` changes the caps. See [budget.md](budget.md). |
| `scripts/jobsearch schedule pause [--reason ...]` / `resume` | The emergency brake: nothing scheduled starts (not even `run`) until you resume. Per-stage settings are kept. |
| `scripts/jobsearch parked` / `requeue score\|tailor [JOB_ID]` | Jobs that ran out of attempts (they are skipped, never retried in a loop) and putting them back. See [runaway-protection.md](runaway-protection.md#parked-jobs). |
| `scripts/jobsearch push-config [--dry-run]` | When the stack runs on a server and you edit your files on another computer: checks `config/profile.json` and `config/resume.json`, then copies them (owner-only) to the server's `config/`. Does nothing when the stack runs on this machine. `search.json` needs no copying (`search-profile apply`). See [where the CLI runs](#where-the-cli-runs). |
| `scripts/jobsearch reshortlist [--apply]` | After you **lower** the score threshold, jobs already scored between the new and the old value stay "scored" and are never sent or tailored. This lists them; with `--apply` it shortlists them (the next digest sends them). Gate-excluded, scam-blocked and acted-on jobs are never touched, and nothing is ever demoted. |
| `scripts/jobsearch tailor [JOB_ID] [--limit N]` | Tailor one job now, or the next eligible shortlisted jobs: resume patch, cover letter, two honesty layers, PDFs. Costs money (about 5 cents each) within your budget caps. See [tailoring.md](tailoring.md). |
| `scripts/jobsearch review` | Applications waiting for your decision, with any "needs attention" reason. |
| `scripts/jobsearch approve\|skip\|applied N [--note ...]` | Record your decision on job N. Works with or without tailoring: a job that was never tailored gets a record without files. |
| `scripts/jobsearch mark N STAGE [--note ...]` | Set any stage by hand, from any other (`approved skipped applied in_progress interview offer rejected withdrawn`): the way to track replies without Gmail. |
| `scripts/jobsearch jobs [--status S] [--min-score N] [--days N] [--search TEXT] [--limit N]` | List jobs from the database. Default: your shortlist, best first. `--status all` for everything. |
| `scripts/jobsearch job N` | One job in detail: score, strengths, gaps, safety, gates, link, and your decision so far. |
| `scripts/jobsearch export [same options] [-o FILE]` | The same list as CSV for Excel or Sheets. Cells that start with `=`, `+`, `-` or `@` are neutralised so a hostile job title cannot run as a formula. |
| `scripts/jobsearch features [set --tailoring on\|off --telegram on\|off --digest on\|off]` | Show or change which optional parts are on, and why any is off. See [customization.md](customization.md). |
| `scripts/jobsearch sources` | Every job source, on or off, what it costs and how many jobs it has brought (Apify's LinkedIn jobs are stored as `linkedin`, so they are counted on that line), plus how many company boards and feeds one collect run visits. |
| `scripts/jobsearch sources on\|off\|only NAMES...` | Switch sources on, off, or keep exactly those named. Names: any single source, or a group: `boards` (greenhouse, lever, ashby, workable, smartrecruiters), `feeds` (remotive, remoteok, weworkremotely, himalayas, arbeitnow), `jobspy` (indeed, naukri, linkedin), `apify` (linkedin_apify). Not case-sensitive. Updates **both** the database and `config/search.json` (applying the whole file, not only `sources`), keeps company slugs when a system is switched off, refuses to switch everything off, and takes effect on the next collect run with no restart. `glassdoor`, `ziprecruiter` and `google` belong to no group, so `off jobspy` leaves them alone. Examples: `sources only apify`, `sources off boards`, `sources on remotive greenhouse`. See [customization.md](customization.md#choosing-your-sources). |
| `scripts/jobsearch boards find "Company Name"` | Checks which of the five board systems a company uses (free, read-only, at most 20 requests) and prints the lines to add to `config/search.json`. It does not look on Lever's EU site, so a company hosted there may report "No match" even though the collector reads it fine; add its slug under `lever` by hand. See [sources.md](sources.md#finding-a-companys-slug). |
| `scripts/jobsearch schedule [set NAME --every 6h --at 08:00 --batch N --boards N --model M --on\|--off] [run NAME] [timezone TZ] [pause\|resume]` | When each stage runs and how much it does. `schedule set collect --boards N` sets how many company boards and feeds one collect run fetches (default 30, 1 to 200; separate from the 18 searches). See [customization.md](customization.md#schedules-when-things-run-and-how-much). |
| `scripts/jobsearch files N` | Where the PDFs and `summary.md` for job N are, and the `scp` command to copy them. |
| `scripts/jobsearch tailor-settings [set --mode ... --approval ... --max-score ...]` | Show or change mode, approval, score band, daily limit, cover letter, pages, fact check and model. |
| `scripts/jobsearch migrate` | Applies pending migrations (safe to re-run). Run it **before** rebuilding or restarting the scraper after updating the repo (migrate first, then rebuild; the new scraper fails without it): the company-board settings and the table that remembers when each source was last fetched come from migration 013. |
| `scripts/jobsearch validate` | Validates `config/profile.json`, `resume.json`, `search.json`; lists every problem. |
| `scripts/jobsearch import-linkedin DIR [-o FILE]` | Reads an **unzipped** LinkedIn data export into clean JSON. Only `Profile`, `Positions`, `Education`, `Skills` and `Projects` are read, and only a whitelist of columns: never address, birth date, messages, connections or ad-targeting data. |
| `scripts/jobsearch conflicts --resume FILE (--linkedin-json FILE \| --linkedin-folder DIR)` | Lists disagreements between resume and LinkedIn (different title, different start date, several roles vs one, entries on only one side). Changes nothing. |
| `scripts/jobsearch search-profile apply [--file config/search.json] [--force]` / `show` / `pull` | `apply` writes your search settings into the database; `show` prints what is stored. If you **edited the profile in the dashboard** since the file was written, `apply` (and `sources on/off/only`) refuse to overwrite those edits until you run **`pull`**, which writes the database's profile into `config/search.json` (your `_comment` is kept; if the file said something different it is kept as `config/search.json.before-pull`, never replacing an older backup) and marks the file as current; `apply --force` discards the dashboard's edits instead. See [dashboard.md](dashboard.md#version-2c-the-search-profile). |
| `scripts/jobsearch check [--url URL] [--text FILE\|-] [--company C] [--title T] [--score]` | Scam and source check for one posting. The URL is only **classified, never fetched**. Exit code 1 when risk is HIGH. `--score` also scores fit (needs `ANTHROPIC_API_KEY` in your shell and `config/profile.json`) and refuses on HIGH risk unless `--force`. |

## Telegram commands

**Telegram is optional.** Without it, every command here has a CLI equivalent in the table above. Sent to your own bot; only your chat id is obeyed. The bot checks for messages once a minute. `N` is the job id shown in the digest and in every message. Day-to-day guidance and the settings table are in [daily-use.md](daily-use.md).

| Command | What it does |
|---|---|
| `/help` | list the commands |
| `/review` | applications waiting for your decision |
| `/approve N [note]`, `/skip N [note]`, `/applied N [note]` | record your decision |
| `/tailor N` | tailor job N now (any scored job; costs about 5 cents within your caps) |
| `/files N` | send the PDFs again |
| `/status` | what the pipeline has found and what waits for you |
| `/budget` | Claude spend against your caps |
| `/settings` | show the tailoring settings |
| `/mark N STAGE [note]` | set any stage by hand: `approved skipped applied in_progress interview offer rejected withdrawn` |
| `/set mode auto\|manual` | tailor automatically, or only when asked |
| `/set approval review\|cli\|auto` | ask here, ask only at the command line, or store applications as ready |
| `/set min_score N\|none`, `/set max_score N\|none` | the score band to tailor in; at or above `max_score` the base resume is used as written |
| `/set daily_limit N\|none` | most applications tailored per day |
| `/set cover_letter on\|off`, `/set verify on\|off`, `/set max_pages 1\|2` | the rest |

A command is acknowledged *before* it runs, so a slow `/tailor` is never run twice, and a command that crashes is reported instead of blocking the ones after it.

## Config files (all gitignored; start from the `*.example.json` files)

| File | Used for |
|---|---|
| `config/resume.json` | Source of truth for tailoring: work, education, skills, projects, with a stable `id` on every bullet |
| `config/profile.json` | What the scoring model compares postings against (see [scoring.md](scoring.md)); sent to the Anthropic API with each scoring request |
| `config/search.json` | What to search for, which sources to use, the company boards to read, and the free rule filters; applied to the database with `search-profile apply` (or `sources ...`) |

`/setup` writes all three after you confirm a summary, then validates them.

## `/setup` in one paragraph

You give it a resume (PDF, LaTeX, DOCX or text) and, optionally, an unzipped LinkedIn export. It imports LinkedIn deterministically, drafts `resume.json` with your bullets kept **verbatim**, and lists every conflict between the two sources for you to decide (a common one: LinkedIn shows an internship then a full-time role at one company, while the resume shows a single role from the earlier date). It infers your skills and experience, asks about target roles, location, languages, work authorization and deal-breakers, drafts the search settings (including which job sources you want: JobSpy, Apify, company boards, remote feeds, and which companies to read), shows you everything, and writes files only after you say yes. Facts you state in chat are written into the files in the same turn, so later steps never treat them as unsupported.

## Safety properties

- Nothing is fetched from the internet by `check`.
- The LinkedIn importer cannot read sensitive columns: it has a whitelist, and the tests assert the sensitive values never appear.
- Search settings reach the database as one randomly-tagged dollar-quoted JSON literal, so no value can break out of the statement (tested with hostile input).
- `--score` never calls the model on HIGH-risk postings by default, and the posting is sanitised and fenced before it is sent.
- API keys are read only from your environment, and never printed.

## Development

```bash
pip install -r scraper/requirements-dev.txt
scripts/test.sh                                    # scraper + CLI unit tests
docker compose exec -T postgres sh /tests/run.sh   # SQL tests on throwaway databases
```
