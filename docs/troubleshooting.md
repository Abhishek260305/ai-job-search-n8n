# Troubleshooting

**Start here:** `scripts/jobsearch doctor`. It checks the containers, the database migrations, the Python service, n8n, your config files, the spending caps and (if configured) the Apify accounts, and says what to do about each problem. Then find your symptom below.

```bash
docker compose ps                      # are postgres, n8n, scraper, gotenberg all up (and healthy)?
docker compose logs --tail 50 scraper # the Python service: every request, with errors
docker compose logs --tail 50 n8n     # the scheduler
```

## Setup and stack

| Symptom | Likely cause and fix |
|---|---|
| `docker compose ps` shows a container restarting | read its log (`docker compose logs <name>`). A missing or malformed `.env` value is the usual cause: re-check step 4 of [setup](setup.md#4-fill-in-env). |
| I changed `.env` but nothing changed | containers read `.env` at start: `docker compose up -d` again. |
| I cannot open `http://localhost:5678` | n8n listens on the server's loopback only. From your own computer run `scripts/tunnel.sh user@host [key]` first and keep it open (a local install needs no tunnel). (The dashboard: [below](#dashboard).) |
| `bind [127.0.0.1]:5678: Address already in use` from the tunnel | something on your computer uses 5678: `N8N_LOCAL_PORT=15678 scripts/tunnel.sh ...`, then open `http://localhost:15678`. |
| `docker compose up` says port 5678 is already allocated or in use | something else on the machine that runs the stack holds 5678 (often another n8n). Stop it, or change the left-hand number of the `"127.0.0.1:5678:5678"` line under `n8n` in `docker-compose.yml` (for example `15678`) and `docker compose up -d n8n`. `scripts/tunnel.sh` assumes 5678 on the server, so on a server forward the new port yourself with `ssh -N -L 5678:127.0.0.1:15678 user@host`. |
| n8n says a credential cannot be decrypted | `N8N_ENCRYPTION_KEY` in `.env` changed since the credential was saved. Restore the old key, or re-run `scripts/n8n-credentials.sh`. |
| `Python 3 is missing` in the n8n log | harmless: the optional Python task runner is not used. |
| `Postgres 16 is outside the supported range` in the n8n log | harmless: n8n 2.x recommends 17+ but still supports 16. Upgrading means a dump and restore of the data volume. |
| The schema looks old after an update | run `scripts/jobsearch migrate`. `postgres/schema.sql` only runs on a brand-new database volume. **Order matters: run `scripts/jobsearch migrate` first, then rebuild or restart the scraper** (migrations only add, so the old code keeps working after one; new code before its migration does not). Company boards and remote feeds need migration 013 (it adds the `boards` and `board_title_words` settings, the per-collect board count and the table that remembers when each source was last fetched); if the new scraper runs without it, collect and ingest fail for everyone, not only for people who use boards (the database has no such columns yet): run `scripts/jobsearch migrate`. |

## Workflows and schedules

| Symptom | Likely cause and fix |
|---|---|
| `Node "…" uses invalid credential` | the credential does not exist yet, or its name is not exactly `Anthropic API key` / `Telegram bot`. Run `scripts/n8n-credentials.sh`, then re-import. |
| Nothing runs on schedule | the **00 Scheduler** workflow (which starts all the others) is not published. `scripts/jobsearch doctor` says so; fix with `scripts/n8n-import-workflows.sh --publish` (it restarts n8n, which is what makes it take effect). Then `scripts/jobsearch schedule` shows each stage's last and next run. |
| One stage never runs | it is switched off (`schedule` shows `OFF`; turn on with `schedule set NAME --on`), or its part is off (tailoring, digest need their switches, see `features`). A stage that failed is not retried until its next turn: `schedule run NAME`. |
| I changed a schedule and nothing happened | changes apply at the next scheduler tick, within 5 minutes. A collect interval cannot be shorter than 15 minutes. |
| Executions listed as "running" forever in n8n | older versions of the Telegram workflow left such rows; re-run `scripts/n8n-import-workflows.sh --publish` to update it, and they are pruned after 3 days. |
| The digest never arrives | the digest and Telegram commands need `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`, and the Telegram and digest switches on: `scripts/jobsearch features` shows which is missing. Then `docker compose up -d scraper` and re-run `scripts/n8n-import-workflows.sh --publish`. Also check you messaged the bot first. No Telegram on purpose? Use `scripts/jobsearch jobs --days 1` instead. |
| Tailoring does nothing | `scripts/jobsearch features` (is tailoring switched off?), then `tailor-settings` (`mode manual` waits for `/tailor N`; a score band or daily limit may exclude your jobs). |
| I get no messages but expected them | Telegram is off or unconfigured, or `approval` is `cli` (which never pushes). `features` and `tailor-settings` show both. |
| Applications are marked ready without my review | `approval` is `auto`, or it is `review` with no Telegram (it behaves as `auto`). Choose `cli` to review at the command line. |
| After a restart some executions show "running" forever | restarting n8n cuts off executions in flight; those rows are leftovers, not work in progress. Safe to ignore or delete. The pipeline is built to be re-run: leases expire and the work is picked up again. |
| Collection finds nothing from LinkedIn | JobSpy's LinkedIn is rate-limited at times (HTTP 429); workflow 01 reports it and carries on. Retry later, or enable the Apify source ([sources.md](sources.md)). |
| Duplicate jobs look unmerged | two **named, different** cities merge only when the descriptions are nearly identical ([dedupe.md](dedupe.md)). |

## Company boards and remote feeds

| Symptom | Likely cause and fix |
|---|---|
| `scripts/jobsearch sources` shows a board system or feed with `0 jobs` | it may not have run yet (a long list is visited in rotation, 30 a run by default: `schedule set collect --boards N`), or it is not really on. A board system needs to be in `sources` **and** to have at least one slug under `boards`; check the `on`/`off` and company count on its line. Also check that you ran `scripts/jobsearch search-profile apply` after editing `config/search.json`. If `apply` or `sources on/off/only` refuses, you saved the profile on the dashboard since: run `scripts/jobsearch search-profile pull` first ([dashboard.md](dashboard.md#which-is-the-source-of-truth)). |
| No jobs from a company board I added | the usual causes, most likely first: **the relevance filter is strict on purpose**: no job title contains a core word of your search terms (with the term `backend engineer`, "Software Engineer, Infrastructure" does not match: add `"software"` to `board_title_words`); no job is in your locations (for a user outside India, a bare city without its country is not recognised); the jobs are older than `max_age_days`; the board was fetched in the last 10 minutes; the slug is wrong (`scripts/jobsearch boards find "Name"` checks; SmartRecruiters slugs are case-sensitive); or the company is on a system that is not supported ([sources.md](sources.md#what-is-not-covered)). The Scrape node of a collect run in n8n shows what the fetch found and dropped (`meta`: `listed`, `relevant`, `dropped`) and any error line. |
| `doctor` warns "greenhouse/foo failed 5 runs in a row" | that company's board returned an error five collect runs in a row: usually the slug is wrong or the company moved to another system. Run `scripts/jobsearch boards find "Foo"` and fix or remove the slug (then `search-profile apply`; if that refuses after a dashboard edit, `search-profile pull` first). It clears itself after one good fetch. For a feed (no slug) it means the feed may be down or have changed its format. The dashboard's Health page raises its `sources_failing` alert earlier, after **3** failed runs in a row; `doctor` waits for 5. |
| `doctor` warns about slugs for a switched-off system, or a switched-on system with no slugs | the switches and the list disagree. Either `scripts/jobsearch sources on NAME` (slugs are listed but the system is off) or add companies under `boards` / `scripts/jobsearch sources off NAME` (the system is on but has no slugs). Nothing is broken, it just does nothing. |
| A run reports `rate limited by the source; skipped for this run` | that source answered HTTP 429. It is not retried and the rest of the run carries on; the next run asks again after the minimum gap. If it keeps happening, lower `schedule set collect --boards N` or switch that source off. |
| A feed returns nothing and the reply says `skipped: fetched recently` | working as intended: Remotive is asked at most every 6 hours, the other feeds hourly, a company board every 10 minutes. |
| Himalayas misses a job I saw on the site | only its newest 60 jobs are read per fetch (it lists over a hundred thousand), so on a busy day a job can be posted and buried between two fetches. |
| A command says `config/search.json has problems` | a slug or setting is invalid; `scripts/jobsearch validate` names it (a slug uses letters, digits, `.`, `_`, `-`). |

## Scoring

| Symptom | Likely cause and fix |
|---|---|
| 02 runs but nothing gets scored | `scripts/jobsearch doctor`: it checks the profile, the credential, and whether a cap has paused scoring. |
| Scoring does nothing and `budget` says PAUSED | a cap was reached. The daily cap resets at midnight (configured timezone), the monthly cap on the 1st. Raise it yourself with `scripts/jobsearch budget set` if you want. |
| Many jobs stuck in `new` with 3 attempts | the model keeps returning unusable replies (cut off, or not JSON). Check one call's `stop_reason` and output tokens; thinking should be disabled and `max_tokens` should leave headroom ([budget.md](budget.md#model-behaviour-that-affects-cost)). After fixing the cause, run `scripts/jobsearch parked` to see them, then `scripts/jobsearch requeue score` ([runaway-protection.md](runaway-protection.md#parked-jobs)). |
| A job is claimed but never scored | an API error: it is retried after the 30-minute lease and is not penalised. |
| No jobs reach shortlisted | the threshold may be high for your profile, or the profile too narrow. `scripts/jobsearch status` shows how many were scored. Adjust `config/search.json` and `search-profile apply`, or `/tailor <id>` a near miss. |

## Tailoring and Telegram

| Symptom | Likely cause and fix |
|---|---|
| The bot does not answer | check that **05 Telegram commands** is published and n8n is running; that `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are right; and that you messaged the bot first. Telegram allows only one consumer of a bot's updates, so do not run another program with the same token. `docker compose logs scraper \| grep telegram` shows each poll. |
| The bot answers some commands slowly | it polls once a minute, so replies take up to a minute. |
| `/tailor` says "resume not found" | run `/setup`, or put a valid `config/resume.json` in place; `scripts/jobsearch doctor` checks it. |
| "ANTHROPIC_API_KEY is not set" | add it to `.env`, then `docker compose up -d scraper`. |
| "The PDF service is unreachable" | `docker compose ps` should list `gotenberg` as healthy; `docker compose logs gotenberg`. |
| Every application says "needs attention" | open its `summary.md`: it lists what was removed and why. If the same fact keeps appearing, it is probably true but missing from `resume.json`; add it there (the checks compare against that file, and they are deliberately not loosened). |
| The resume spills to two pages | hide bullets in your base resume, or accept two with `/set max_pages 2`. |
| A model rejects the request | models differ in how "no thinking" is requested; the client tries the accepted forms and remembers which one worked. If a new model fails, see [budget.md](budget.md#model-behaviour-that-affects-cost). |
| I got test messages I did not want | `/skip <id>` clears an application. Test runs create real application rows. |
| `mark` or `approve` says "no job N" | the id is not in the database (it must be a job the pipeline collected). `scripts/jobsearch jobs --status all --search "text"` finds ids. |
| `files N` says the job has no files | its decision was recorded by hand (never tailored). `scripts/jobsearch tailor N` creates files, if tailoring is on. |

## Safety and false alarms

| Symptom | Likely cause and fix |
|---|---|
| A legitimate job was marked medium or high risk | the checks are heuristics ([safety.md](safety.md)). `scripts/jobsearch check` explains which rule fired. High-risk jobs are blocked before any model call. If a rule is wrong for your market, open an issue with the (anonymised) sentence. |
| A scam got through | please report it with the posting text; rules are added with a test for both the positive and negative case. |

## Parked jobs, Apify skips and pauses

| Symptom | Likely cause and fix |
|---|---|
| `doctor` says "N jobs parked" | those jobs ran out of attempts (scoring: 6 unfinished attempts or 3 unusable replies; tailoring: 3 claims or 2 failed drafts) and are skipped on purpose. `scripts/jobsearch parked` lists them. Look at why first (a posting that breaks the reply format, a PDF service that is down), then `scripts/jobsearch requeue score` or `requeue tailor`. See [runaway-protection.md](runaway-protection.md#parked-jobs). |
| LinkedIn-via-Apify searches return nothing and the n8n summary lists `skipped_by_budget` | the Apify daily or monthly cap is reached (`scripts/jobsearch budget` shows both). The free sources carry on. It resets at midnight (configured timezone) or on the 1st; raise it yourself with `budget set --apify-daily N`. |
| `doctor` says "schedules paused" | someone ran `schedule pause`. Nothing scheduled runs until `scripts/jobsearch schedule resume`. |
| `doctor` says scoring looks stalled | many jobs have waited over 3 hours and nothing was scored: check `budget` (a cap?), `schedule` (paused? scoring off?), `parked`, and the n8n executions for the Score workflow. |

## Dashboard

Only if you turned on the optional [web dashboard](dashboard.md). `scripts/jobsearch doctor` has a `dashboard` line that says whether it runs, answers and reaches the database. The full list is in [dashboard.md](dashboard.md#troubleshooting).

| Symptom | Likely cause and fix |
|---|---|
| I cannot open `http://localhost:8080` | it listens on the server's loopback only, like n8n. Run `scripts/tunnel.sh user@host [key]` (it forwards both 5678 and 8080) and keep it open. |
| `bind [127.0.0.1]:8080: Address already in use` | something on your computer uses 8080: `DASHBOARD_LOCAL_PORT=18080 scripts/tunnel.sh ...`, then open `http://localhost:18080`. If instead the stack itself cannot publish 8080 (another program holds it on that machine), set `DASHBOARD_PORT` in `.env`, run `docker compose up -d --no-deps dashboard`, and give the tunnel the same `DASHBOARD_PORT`. |
| `channel 2: open failed: connect failed: Connection refused` in the tunnel | the dashboard container is not running: `docker compose ps dashboard`, `docker compose logs dashboard`. |
| "Too many attempts. Try again in N minutes." | 10 failed logins within 15 minutes lock every login for 15 minutes, even with the right password. Wait. |
| Logged in, but the numbers show an error | migration 014 is missing (`scripts/jobsearch migrate`) or `dashboard-db-init` failed (`docker compose logs dashboard-db-init`). |
| `dashboard-db-init: DASHBOARD_DB_PASSWORD must be set in .env` | `scripts/init-env.sh --add-dashboard`, then `docker compose up -d --no-deps dashboard-db-init dashboard`. |
| `docker compose up -d` wants to recreate `postgres` | your running container differs from `docker-compose.yml` (for example an extra published port). Start only the dashboard: `docker compose up -d --no-deps dashboard-db-init dashboard`, and check first with `docker compose --dry-run up -d --no-deps ...`. |

## Cost

| Symptom | Likely cause and fix |
|---|---|
| Spend is higher than expected | `scripts/jobsearch budget` shows spend per model and purpose. Calls from outside this project on the same key are not in the log: that is what the Anthropic Console limit is for. |
| A paid step stopped by itself | a cap was reached; see above. This is the intended behaviour. |

## Still stuck

Run `scripts/jobsearch doctor` and `docker compose logs --tail 100 scraper`, remove anything personal (keys, your name, resume text), and open an issue with the output.
