# Daily use

Once [setup](setup.md) is done, the pipeline runs on its own. This page is the day-to-day view: what happens without you, what you do, and the knobs you can turn. It assumes everything is on; if you skip tailoring or Telegram, see [your daily routine without them](#without-telegram-or-without-tailoring) below and [customization.md](customization.md).

## What runs by itself

| When | What | Costs money? |
|---|---|---|
| every **6 hours** | **Collect:** searches the next 18 of your (role × location × source) combinations, reads the next 30 company boards and remote feeds if you switched them on ([sources.md](sources.md#company-job-boards)), runs the scam check, merges duplicates, applies your free filters | no |
| every 30 minutes | **Score:** scores up to 25 new jobs 0-100 against your profile, retires jobs past their deadline | yes, about $0.0003 per job |
| every 15 minutes | **Tailor** *(optional)*: drafts a resume and cover letter for up to 3 shortlisted jobs, checks them, renders PDFs, sends them to you | yes, about $0.05 per application |
| every minute | **Telegram** *(optional)*: reads your commands and acts on them | no |
| 08:00 | **Digest** *(optional, needs Telegram)*: new shortlisted matches, with warnings and your month's spend | no |

These are the **defaults, and every one is a setting**: `scripts/jobsearch schedule` shows them and `schedule set collect --every 3h` (or `--at 07:30` for the digest, `--batch N`, `--off`) changes them within 5 minutes, no restart ([customization.md](customization.md#schedules-when-things-run-and-how-much)).

A full pass over your searches takes a couple of days (for example 4 roles × 13 locations × 4 sources is about 176 combinations after the planner drops combinations that make no sense, 72 of them per day), so coverage builds up over a few days, and every run adds whatever is new.

How many jobs reach "shortlisted" depends on your profile and threshold (default 70). A narrow, honest profile gives few, good matches; if you get none after several days, check `scripts/jobsearch status`: the "by status" line shows how many jobs were collected, filtered out and scored but fell short. Consider adjusting the search terms, filters or the threshold, and use `/tailor <id>` on a near miss you like.

## What you do

**Each day, about five minutes:**

1. Read the **digest** (or send `/review` to see what is waiting). If you switched it on (`scripts/jobsearch follow-up --digest on`), the digest also lists the jobs due for a follow-up.
2. For each application you are interested in, open the PDFs and `summary.md`. **Read them.** The honesty checks are strong, not perfect, and anything flagged "needs attention" says exactly what was removed or doubtful.
3. Decide: `/approve 24` (ready to apply), or `/skip 24` (not interested; you can still approve it later).
4. Apply on the employer's own site yourself, then send `/applied 24` so the system knows. Later, record how it went: `/mark 24 interview`, `/mark 24 rejected`, ... (see [tracking](customization.md#tracking-after-you-apply-without-gmail)).

**Occasionally:** glance at `/budget`, add new facts to `config/resume.json` when your experience changes (so they count as supported), and re-run `/setup` if you change direction.

## Check the dashboard (optional)

If you turned on the [web dashboard](dashboard.md), it is the quickest way to see where things stand. On a server install, open the tunnel from your computer (a local install needs no tunnel) and go to <http://localhost:8080>:

```bash
scripts/tunnel.sh ubuntu@<host> [path/to/ssh-key]
```

- **The cards** count jobs found, shortlisted, new this week, applied and each later stage; clicking one filters the table to those jobs.
- **The charts** show your applications by status, and where jobs drop out between found and offer.
- **The jobs table** opens on your shortlist, best match first. Tabs, filters (status, source, minimum score, remote) and the search box (press `/`) narrow it; click a job title for its score reasons, safety flags, description, links and the resume and cover-letter PDFs.
- **The status dropdown** records each stage, the same as `/applied 24` or `/mark 24 interview`. If the job was changed elsewhere since the page loaded (by Telegram or the CLI), it does not overwrite it: it says "Changed elsewhere" and reloads.

You stay signed in for 7 days. The dashboard has five pages: Jobs (the cards, charts and table above), Health, Spend, Settings (schedules, optional parts, tailoring, spend caps) and Search (the search profile, sources and company boards). Spend caps can only be **lowered** there; raising one, the approval mode, the models and the time zone stay commands ([dashboard.md](dashboard.md#version-2b-settings)). If it does not load: [dashboard troubleshooting](dashboard.md#troubleshooting).

## Seeing and changing which sources are used

```bash
scripts/jobsearch sources                 # (or /sources) every source: on or off, what it costs, how many jobs it has brought
scripts/jobsearch sources off boards      # stop reading company boards (your slugs are kept)
scripts/jobsearch sources on remotive     # start reading a remote feed
```

It takes effect on the next collect run. If a source you expected has brought `0 jobs`, give it a collect run or two (a long company list is visited in rotation), and see [troubleshooting](troubleshooting.md#company-boards-and-remote-feeds). `scripts/jobsearch doctor` also lists what is on and warns about company boards or feeds that keep failing. More: [customization.md](customization.md#choosing-your-sources).

## Telegram commands

Send these to your bot. It only answers your own chat.

| Command | What it does |
|---|---|
| `/help` | the list of commands |
| `/review` | applications waiting for your decision |
| `/approve 24` | mark application 24 ready to apply (add a note after the number if you like) |
| `/skip 24` | not interested |
| `/applied 24` | you submitted it |
| `/mark 24 interview` | set any stage by hand: `approved skipped applied in_progress interview offer rejected withdrawn` (add a note after it) |
| `/tailor 24` | tailor job 24 now: works on any scored job, even below your threshold |
| `/files 24` | send the PDFs again |
| `/status` | what the pipeline has found, and what is waiting |
| `/budget` | Claude spend against your caps |
| `/settings` | the tailoring settings |
| `/set <name> <value>` | change a setting (below) |

The number is the **job id** shown in the digest and in every message. Commands are acknowledged before they run, so a slow `/tailor` is never run twice. The bot checks for new messages once a minute, so replies can take up to a minute.

## Settings you can change

Tailoring is the part with choices. All of them are stored in the database, so they apply immediately and survive restarts. Change them in Telegram (`/set approval auto`) or at the CLI (`scripts/jobsearch tailor-settings set --approval auto`).

| You want | Set |
|---|---|
| tailor everything shortlisted automatically (default) | `mode auto` |
| tailor only when you ask (`/tailor 24`) | `mode manual` |
| be asked on Telegram before anything is marked ready (default) | `approval review` |
| no messages, but still decide yourself, at the command line | `approval cli` |
| no messages: store applications as ready unless the checks flagged them | `approval auto` |
| skip tailoring when the match is already great: use your base resume as written, with only a cover letter | `max_score 80` (at or above 80) |
| ignore weaker matches | `min_score 75` (default: your search threshold) |
| cap the number per day | `daily_limit 5` |
| no cover letters | `cover_letter off` |
| allow a two-page resume | `max_pages 2` |
| skip the second-layer fact check (not recommended) | `verify off` |

`none` clears an optional limit: `/set daily_limit none`. Full explanation of each setting: [tailoring.md](tailoring.md#settings).

To change **what is searched** or the **score threshold**, edit `config/search.json` and run `scripts/jobsearch search-profile apply`, or use the dashboard's **Search** page (then run `scripts/jobsearch search-profile pull` before editing the file again). A new threshold applies to jobs scored from then on; if you **lowered** it, run `scripts/jobsearch reshortlist` to see the already-scored jobs that now qualify, and `--apply` to shortlist them.

## Without Telegram, or without tailoring

You can run any part without the others ([customization.md](customization.md) has the full picture). The routine changes like this:

| If you do not use... | You do this instead |
|---|---|
| **Telegram** (no digest, no phone commands) | once a day: `scripts/jobsearch jobs --days 1` (new shortlisted jobs, best first), `scripts/jobsearch job 24` (one in detail), then `review`, `approve 24`, `applied 24`, `mark 24 interview`. Prefer a spreadsheet? `scripts/jobsearch export -o jobs.csv`. |
| **Tailoring** (stop at scoring) | `jobs` and `job N` show the score, strengths, gaps and link; apply with your own resume; record it with `applied N` and `mark N ...`. The pipeline spends only on scoring. |
| **Gmail tracking** (optional, off by default) | you set the stage yourself with `mark`, or on the dashboard's board. |
| **The approval step** | `scripts/jobsearch tailor-settings set --approval auto`: finished applications are stored ready to apply. Ones the checks flagged still wait for you. |

With no Telegram there is also no budget alert in the digest, so glance at `scripts/jobsearch budget` now and then (the caps still stop paid calls by themselves).

## Spend

```bash
scripts/jobsearch budget          # or /budget
```

Spend is logged for every call. At the monthly or daily cap, paid steps pause by themselves (and the digest tells you); nothing runs up a bill. Raising a cap is always your decision: `scripts/jobsearch budget set --monthly 15`. Details: [budget.md](budget.md).

## Checking a posting yourself

Found a job on your own and want to know if it is a scam, or a good fit?

```bash
scripts/jobsearch check --url "https://..." --text posting.txt --company "Acme" --title "Backend Engineer" --score
```

or `/check` in Claude Code. The link is only classified, never fetched, and posting text is treated as untrusted. See [commands.md](commands.md).

## Pausing and stopping

| You want to | Do |
|---|---|
| stop tailoring but keep collecting and scoring | `/set mode manual` |
| stop everything at once, keeping every setting | `scripts/jobsearch schedule pause --reason "..."` (then `resume`); nothing runs, not even "run now" |
| stop one stage |  `scripts/jobsearch schedule set collect --off` (and `--on` to resume) |
| stop everything scheduled | `docker compose stop n8n` (the schedules live in n8n); start it again with `docker compose start n8n` |
| stop one workflow | `docker compose exec -T n8n n8n unpublish:workflow --id=js-tailor`, then `docker compose restart n8n` |
| stop all paid calls | `scripts/jobsearch budget set --monthly 0`: scoring and tailoring claim nothing until you raise it again |

## Your data

- **Stays on your server:** the database, your config files, and the generated PDFs in `output/`.
- **Sent to the Anthropic API:** for scoring, your profile summary and the (sanitised) posting; for tailoring, your `resume.json`, profile summary and the posting; for the fact check, the generated text and your resume.
- **Sent to Telegram:** the digest, status messages and the PDFs, only to your own chat.
- Nothing else leaves the machine except the job searches themselves. The optional dashboard makes no calls to Claude, Apify or Telegram: it is reachable only on the server's `127.0.0.1`, through your SSH tunnel, and its only outbound requests are the read-only ones of the Search page's "Find a company" box (up to 20, through the scraper).

Back up Postgres now and then with `scripts/backup.sh` (compressed, readable by you only, the newest 14 kept; see [Backups](setup.md#backups)), and keep your `.env` somewhere safe.
