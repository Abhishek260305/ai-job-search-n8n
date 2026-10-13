# n8n workflows

Seven workflows run the pipeline. **00 is the scheduler; 01, 02 and 04 are the pipeline; 03 and 05 exist only for Telegram** and are not published if you have no Telegram ([customization.md](customization.md)). **06 belongs to the optional Gmail part** ([gmail.md](gmail.md)) and does nothing until that part is switched on. **When each stage runs and how much it does are settings you change with one command** (`scripts/jobsearch schedule`), not edits to these workflows. n8n only schedules, loops, calls the model and sends messages; all the logic (safety, filters, dedupe, scoring rules, tailoring, the database) lives in the Python service and is unit-tested. That is why n8n needs just **two** credentials and no database credential.

```
00 Scheduler      every 5 min   what is due? ─► start the workflows below (the times are YOUR settings)   none
01 Collect jobs   default 6 h   plan ─► scrape ─► ingest (safety, dedupe, free filters)                  no credentials
02 Score jobs     default 30 m  sweep ─► claim ─► Claude ─► validate + store                              Anthropic API key
03 Daily digest   default 08:00 build ─► Telegram ─► mark as sent                                         Telegram bot
04 Tailor         default 15 m  next shortlisted jobs ─► draft, honesty checks, PDFs ─► you               none (the service holds the keys)
05 Telegram       every minute  read your commands (/approve, /skip, /tailor, ...) and act               none
06 Check mail     default 30 m  (only with the Gmail part on) read replies to your applications ─► suggestions you accept   none (the service holds the Google login)
```

| Workflow | What it does |
|---|---|
| **00 Scheduler** | Every 5 minutes it asks the service which stages are due (`POST /schedule/tick`), which claims them atomically so nothing starts twice, and starts the matching workflows. It leaves a stage alone while its part is switched off (tailoring, digest, Gmail). You never edit it: change the settings instead (`scripts/jobsearch schedule`, [customization.md](customization.md#schedules-when-things-run-and-how-much)). |
| **01 Collect jobs** | Takes the next slice (18 by default) of your (title × location × source) searches from a rotating cursor, scrapes each, and sends the results to the ingest step: scam/safety check, cross-board dedupe, then the free rule filters (excluded title words, years of experience, age, companies). If you switched on company boards or remote feeds ([sources.md](sources.md#company-job-boards)), the plan also contains a slice (30 by default, `schedule set collect --boards N`) of `POST /fetch/board` requests, one per company board or feed, which go through the very same Scrape and Ingest steps; they have their own rotation, so they never take slots from the searches. **The workflow file itself is unchanged**: it calls whatever endpoint the plan names. Nothing here calls an AI. |
| **02 Score jobs** | Retires jobs past their deadline, claims unscored jobs (25 by default), using the model from the schedule settings, asks Claude to score each against your `profile.json`, then validates the reply strictly and stores the computed result. An unusable reply is counted (3 strikes) and retried; an API outage is not counted against the job. |
| **03 Daily digest** *(Telegram only)* | Sends your new shortlisted matches (score at or above your threshold) to Telegram with warnings (closing soon, medium safety risk, unverified eligibility) and your month's spend, then marks them sent so you never get one twice. If you switch it on (`scripts/jobsearch follow-up --digest on`), it also lists the jobs due for a follow-up, and goes out on its own on a day with no new matches. |
| **04 Tailor applications** | Tailors the next shortlisted jobs (3 per run by default) in `auto` mode: resume patch and cover letter, two honesty layers, PDFs, and in `review` mode a Telegram message with the files. Does nothing in `manual` mode, or when tailoring is switched off (it stays published and simply returns "skipped"). See [tailoring.md](tailoring.md). |
| **05 Telegram commands** *(Telegram only)* | Polls Telegram every minute and acts on commands from your chat only. n8n keeps executions for 3 days, so the 1,440 daily polls do not pile up. |
| **06 Check mail** *(Gmail part only)* | Started by the scheduler (default every 30 minutes) only while the optional Gmail part is on and a Google login is in `.env`: one call to `POST /mail/run`. That call checks the login, searches the mailbox (read-only), asks Claude about the few replies the rules could not decide, and writes status *suggestions* ([gmail.md](gmail.md#the-scheduled-stage-6e)). It never changes a status. |

The workflow files carry **fixed ids** (`js-scheduler`, `js-collect`, `js-score`, `js-digest`, `js-tailor`, `js-telegram`, `js-mail`), so re-importing updates them in place instead of creating duplicates.

## Installing them

If you followed [setup.md](setup.md) you have already done this:

```bash
scripts/n8n-credentials.sh                  # creates the two credentials from .env
scripts/n8n-import-workflows.sh --publish   # imports, publishes (schedules) and restarts n8n
```

Create the credentials **first**: n8n checks every node's credentials before it lets a workflow run. The import script writes your Telegram chat id into the digest workflow in a temporary copy, so the files in `workflows/` stay free of personal data. It publishes the two Telegram workflows only when both `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set (and unpublishes them otherwise), so a no-Telegram install has no failing schedules. Without `--publish` the workflows are imported **unpublished** (not scheduled).

n8n's CLI only changes the database; a running server picks the change up on restart, which the script does for you. By hand: `docker compose exec -T n8n n8n import:workflow --separate --input=/workflows/`, then `n8n publish:workflow --id=js-collect` (and the others), then `docker compose restart n8n`. In the UI use **Workflows → Import from file** and **Publish**. Pause one with `n8n unpublish:workflow --id=...` and a restart.

### Credential names matter

The workflows refer to credentials by these **exact names**:

| Credential name | Type | Used by |
|---|---|---|
| `Anthropic API key` | Header Auth (name `x-api-key`) | 02 Score jobs |
| `Telegram bot` | Telegram API | 03 Daily digest |

### Adding the credentials in the n8n UI instead

If you would rather not keep keys in `.env`:

1. Open n8n and go to **Credentials** (left sidebar, or the Credentials tab on the Overview page).
2. Click **Create** (top right) and choose **Credential**.
3. Search for **Header Auth**, select it, and continue. Set **Name** to `x-api-key` and **Value** to your Anthropic API key.
4. Rename the credential (click its title at the top of the dialog) to exactly **`Anthropic API key`**, then **Save**.
5. For Telegram: create a credential of type **Telegram API**, paste your bot token as the **Access Token**, and name it exactly **`Telegram bot`**.

Button labels differ slightly between n8n versions; what matters is the credential type and the exact name. Note that the Python service still reads `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` from `.env` for tailoring and the Telegram bot, so put them there too.

## Test before you rely on them

Run each once by hand and look at the result. In the UI open the workflow and click **Execute workflow**; or from the host:

```bash
docker compose exec -T -e N8N_RUNNERS_BROKER_PORT=5690 n8n n8n execute --id=js-collect
```

(The extra port variable stops the CLI from clashing with the running n8n server.) Then:

```bash
scripts/jobsearch status
```

Suggested order: **01** (free), then **02** (scores up to 25 jobs; check the scores make sense), then **03**, then **04** and **05** (`scripts/jobsearch tailor <job id>` is a good first manual test).

## Tuning

| Want to change | Where |
|---|---|
| How many company boards and feeds one collect run reads | `scripts/jobsearch schedule set collect --boards 30` (default 30, 1 to 200). Each request adds about 8 seconds to the run, because the Scrape step spaces requests out. |
| How often jobs are collected, and how many searches per run | `scripts/jobsearch schedule set collect --every 6h --batch 18`. With 4 titles × 13 locations × 4 sources (about 176 after the planner drops combinations that make no sense) the default covers a full pass in about 2.5 days; raise `--batch` or shorten `--every` for a faster pass, gently (the boards rate-limit). |
| Which model scores, and jobs scored per run | `scripts/jobsearch schedule set score --model ... --batch ...` |
| Jobs tailored per run, and how often | `scripts/jobsearch schedule set tailor --batch 3 --every 15m` |
| What to search, which sources, company boards, filters, threshold | `config/search.json`, then `scripts/jobsearch search-profile apply` (sources: `scripts/jobsearch sources ...`, see [customization.md](customization.md#choosing-your-sources)). After a dashboard edit these refuse until `scripts/jobsearch search-profile pull` ([dashboard.md](dashboard.md#which-is-the-source-of-truth)) |
| Tailoring behaviour | `scripts/jobsearch tailor-settings set ...` or `/set` in Telegram ([tailoring.md](tailoring.md#settings)) |
| Digest time or interval, and size | `scripts/jobsearch schedule set digest --at 08:00 --batch 10` (or `--every 6h`) |
| Pause or run one now | `schedule set NAME --off`, `schedule run NAME` |
| Timezone for the times of day | `scripts/jobsearch schedule timezone Asia/Kolkata` |

## Cost and the spending cap

Scoring one job costs about **$0.0003** with Claude Haiku 5.5 (measured); tailoring about **$0.05**. Reading company boards and feeds in the collect step is free; only scoring the new jobs they bring costs ([costs.md](costs.md#company-boards-and-remote-feeds)). Spend is logged and hard-capped at $10 a month and $1.50 a day by default: before each batch the service reserves the worst-case cost and claims only what the caps allow, and at a cap the workflows simply do nothing. See [budget.md](budget.md).

## Safety

Postings are untrusted: the safety check runs before filters and scoring, the prompts fence the posting and forbid following instructions inside it, and every model reply is validated strictly before anything is stored ([safety.md](safety.md), [scoring.md](scoring.md)). The workflows never send, apply or post anything on your behalf; they only message **you**.

## Problems

See [troubleshooting.md](troubleshooting.md#workflows-and-schedules).
