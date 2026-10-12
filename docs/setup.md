# Setup

A linear guide from nothing to a working pipeline. Allow about 30 minutes. Each step ends with a way to check that it worked.

> **Prefer to let an AI assistant do it?** Open this repository in Claude Code, Cursor or similar and say: *"Set this project up for me. Read `AGENTS.md` and `docs/setup-with-ai.md`, then ask me for whatever you need."* It will ask where you want to run it, for your resume and what jobs you want, and tell you which few things only you can do (create an Anthropic key, type it into `.env`). See [setup-with-ai.md](setup-with-ai.md).

## Choose where to run it

| | **A. This computer** | **B. An always-on server** |
|---|---|---|
| What | Docker on your Mac, Linux, or Windows (inside WSL 2) | A Linux server you rent or own, for example a free Oracle Cloud VM |
| Setup effort | least: no server, no tunnel | create the server first: [vm-oracle.md](vm-oracle.md) |
| It runs | only while the computer is on and awake | all day |
| You open n8n and the dashboard at | <http://localhost:5678>, <http://localhost:8080> | the same addresses, through `scripts/tunnel.sh` (or Tailscale for your phone) |
| Good for | trying it | daily use |

Everything below works for both. Where they differ, the text says so.

**Windows:** the scripts are POSIX shell, so work inside WSL 2. In an administrator PowerShell run `wsl --install` (restart if asked), install [Docker Desktop](https://www.docker.com/products/docker-desktop/) and, in its Settings, Resources, WSL integration, switch on your Linux distribution. Then open the Ubuntu terminal and do everything there. Clone the repository inside WSL (your home folder), not on the Windows drive, and if you will connect to a server, copy its SSH key into WSL (`cp /mnt/c/Users/<you>/Downloads/<key> ~/.ssh/ && chmod 600 ~/.ssh/<key>`): ssh refuses keys that other users can read, and Windows folders cannot be locked down.

**You will need**

- Docker with Docker Compose v2 on the machine that runs it (Docker Desktop on macOS and Windows; on a Linux server see step 1). It needs about 4 GB of free memory (in Docker Desktop: Settings, Resources); every image used supports both `arm64` and Intel/AMD
- `git`, `openssl` and `python3` (3.10 or newer) on the machine where you type the commands: `git` fetches the code, `openssl` generates the secrets, and `scripts/jobsearch` is a Python program. macOS and most Linux systems have all three
- an [Anthropic API key](https://console.anthropic.com) (scoring needs it). The key belongs to the **API Console** and is paid by prepaid credit (add a card and buy a small amount, $5 to $10 lasts a long time at about $0.0003 per scored job). A Claude or ChatGPT *subscription* is a different product and does not give you a key
- your resume (PDF, LaTeX, DOCX or text) and, optionally, your LinkedIn data export
- **optional:** a Telegram account, to create a free bot (review from your phone, daily digest)
- **optional:** Apify tokens for LinkedIn ([sources.md](sources.md)), and [Claude Code](https://claude.com/claude-code) to run `/setup` conversationally

**You do not have to use everything.** Tailoring, Telegram and the digest are optional, and each has a fallback. Skip step 3 and leave the Telegram lines in `.env` empty if you do not want Telegram; turn tailoring off later with one command if you want the pipeline to stop at scoring. [customization.md](customization.md) lays out the three common setups and what each costs; the steps below mark what is optional.

> **Secrets stay on the host.** Type API keys into `.env` or the n8n UI yourself. Never paste them into a chat, an issue or a commit.

## 1. Install Docker

**macOS and Windows:** install [Docker Desktop](https://www.docker.com/products/docker-desktop/) and start it (Windows: enable the WSL 2 backend and work inside WSL). **Linux:** use Docker's official apt repository, not the distro `docker.io` package: <https://docs.docker.com/engine/install/ubuntu/> (the commands are also in [vm-oracle.md](vm-oracle.md#4-install-docker)). Optionally add your user to the `docker` group so you do not need `sudo`.

Check: `docker compose version` prints a version.

## 2. Get the code and create `.env`

```bash
git clone https://github.com/Abhishek260305/ai-job-search-n8n.git
cd ai-job-search-n8n
scripts/init-env.sh        # writes .env with a random database password, n8n encryption key and dashboard secrets
```

Use `git clone`, not GitHub's "Download ZIP" (a ZIP loses the permission that lets the scripts run). On a server, run these on the server. If you will later run `scripts/jobsearch` **from your laptop** against that server, clone it into a folder named `jobsearch` there (`git clone <url> jobsearch`): that is where the command looks by default (otherwise set `JOBSEARCH_REMOTE_DIR`, see [commands.md](commands.md#where-the-cli-runs)).

**Back up `N8N_ENCRYPTION_KEY`** (it is in `.env`). Without it n8n cannot decrypt the credentials it has saved.

## 3. Create the Telegram bot (optional)

**Skip this step if you do not want Telegram.** Everything it does has a command-line equivalent ([customization.md](customization.md#what-each-telegram-feature-becomes-without-telegram)), and you can add it any time later.

1. In Telegram, message **@BotFather**, send `/newbot` and follow the prompts. Copy the **token** it gives you.
2. Open a chat with your new bot and send it any message (for example `/start`). Telegram only lets a bot message you after you have messaged it.
3. Find your numeric **chat id**: message **@userinfobot**, or open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a private browser tab and look for `"chat":{"id": ...}`.

The bot only ever acts on messages from this chat id. Anyone else who finds the bot is ignored.

## 4. Fill in `.env`

```bash
nano .env
```

| Variable | Set it to | Required |
|---|---|---|
| `TZ` | your timezone, for example `America/New_York` or `Asia/Kolkata`: the clock of the containers and n8n. The digest time and the spending-cap days follow a separate setting, which you set in [step 5](#5-choose-which-parts-you-want-optional) | yes |
| `ANTHROPIC_API_KEY` | your key (starts with `sk-ant-`); it needs credit on the account, see above | yes |
| `TELEGRAM_BOT_TOKEN` | the token from step 3 | **optional**: leave empty for no Telegram |
| `TELEGRAM_CHAT_ID` | the number from step 3 | **optional**: leave empty for no Telegram |
| `APIFY_TOKEN_CURIOUS`, `_CHEAP`, `_BEBITY` | Apify tokens, one per account; leave blank to disable | no |

Paste only the value: no quotes, no spaces. `POSTGRES_*` and `N8N_ENCRYPTION_KEY` were generated for you; leave them. To type a key without it showing on screen, run `scripts/set-env.sh ANTHROPIC_API_KEY` (it asks for the value with the typing hidden and writes it into `.env`).

**Set a spending limit at Anthropic as well.** The app caps your Claude spend at $10 a month by default, but a limit enforced by the provider also covers mistakes outside this project. In the Console, set a monthly limit slightly above the app cap (for example $12), ideally on a workspace and key used only for this project. See [budget.md](budget.md).

## 5. Choose which parts you want (optional)

The default turns everything on. To change that now, or later:

```bash
scripts/jobsearch features                       # what is on
scripts/jobsearch features set --tailoring off   # stop at scoring: no resume or cover-letter generation
scripts/jobsearch features set --telegram off    # even if Telegram is configured
```

**Set your time zone for the schedules now** (this is separate from `TZ` in `.env`; it decides when the 08:00 digest arrives and when each spending-cap day starts, and defaults to India time): `scripts/jobsearch schedule timezone America/New_York` (use your own).

If you left Telegram empty in `.env`, it is already off. If you want applications without a review step, `scripts/jobsearch tailor-settings set --approval auto` (or `cli` to review at the command line). Run these once the stack is up (after step 6); they are listed here so you know the choice exists. Full guide: [customization.md](customization.md).

## 6. Start the stack

```bash
docker compose up -d --build
docker compose ps
```

You should see `postgres`, `n8n`, `scraper` and `gotenberg` running (the first start downloads images and builds the service: about 5 minutes, longer on a small or ARM machine). On a fresh database volume, Postgres creates the `jobsearch` database, applies `postgres/schema.sql` and then every migration. To start over from nothing, `docker compose down -v` (this **deletes all data**).

If you change `.env` later, run `docker compose up -d` again so the containers pick it up.

## 7. Open n8n and create the owner account

n8n listens on `127.0.0.1:5678` only; nothing is exposed to the internet. On a remote host, open an SSH tunnel from your own computer:

```bash
scripts/tunnel.sh ubuntu@<host> [path/to/ssh-key]
# without the script (it needs a copy of this repository on your computer; Windows PowerShell, for example, has no such script):
ssh -N -L 5678:127.0.0.1:5678 -L 8080:127.0.0.1:8080 -i path/to/ssh-key ubuntu@<host>
```

Leave that running, then open <http://localhost:5678>. The same tunnel also forwards the optional dashboard ([step 11](#11-enable-the-dashboard-optional)) to <http://localhost:8080>. If something on your computer already uses port 8080 the tunnel refuses to start: use another local port, `DASHBOARD_LOCAL_PORT=18080 scripts/tunnel.sh ...` (or `-L 18080:127.0.0.1:8080` in the raw command) and open <http://localhost:18080>. *A local install needs none of this.*

On first launch n8n shows a "Set up owner account" form: your email, name and a password of your choice (8+ characters with a number and a capital). It is local to your instance (it is not an n8n.com account) and is free; if it then offers a licence key or a survey, skip them.

## 8. Describe yourself and what you want

The easy way is **`/setup`** in Claude Code, run in this repo. It reads your resume (and a LinkedIn export if you give it one), points out where the two disagree for you to decide, asks about target roles, locations, languages and work authorization, shows you a summary, and writes the three config files only after you say yes. Details: [commands.md](commands.md#setup-in-one-paragraph).

By hand:

```bash
cp config/resume.example.json  config/resume.json    # your resume as data; keep an id on every bullet
cp config/profile.example.json config/profile.json   # what scoring compares postings to
cp config/search.example.json  config/search.json    # what to search for, plus the free filters
# edit all three, then:
scripts/jobsearch validate                           # lists every problem
scripts/jobsearch search-profile apply               # writes search.json into the database
```

The example search file switches on only Indeed, Naukri and LinkedIn. To also read company job boards (Greenhouse, Lever, Ashby, Workable, SmartRecruiters) or remote feeds, see [choosing your sources](customization.md#choosing-your-sources); it is optional and nothing changes unless you ask for it.

All three files are gitignored. **If the stack runs on a server but you edit these files on your own computer,** run the commands from your computer with `JOBSEARCH_SSH` set ([where the CLI runs](commands.md#where-the-cli-runs)) and copy the two files the containers read to the server with:

```bash
scripts/jobsearch push-config        # checks profile.json and resume.json, then copies them (owner-only) to the server
```

(`search.json` needs no copying: `search-profile apply` sends it to the database.) If you work on the server itself, there is nothing to copy.

## 9. Create the n8n credentials and install the workflows

```bash
scripts/n8n-credentials.sh                    # creates "Anthropic API key" (and "Telegram bot", if you set a token) inside n8n
scripts/n8n-import-workflows.sh --publish     # imports the six workflows (a scheduler plus the pipeline), starts them, restarts n8n
```

The schedules (when each stage runs and how much it does) are not part of the workflows: they are settings you can change later with `scripts/jobsearch schedule` ([customization.md](customization.md#schedules-when-things-run-and-how-much)); the defaults are to search every **6 hours**, score every 30 minutes, tailor every 15 minutes, and send the digest at 08:00.

The script publishes the scheduler, collect, score and tailor always, and the two Telegram workflows (daily digest and commands) **only if** both `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set; without them it leaves those two unpublished and says so. The tailor workflow does nothing while tailoring is switched off.

Use `JOBSEARCH_COMPOSE="sudo docker compose"` in front of either command if your user is not in the `docker` group. Both are safe to re-run. Prefer the n8n UI for credentials? See [workflows.md](workflows.md#adding-the-credentials-in-the-n8n-ui-instead).

No database credential is needed: n8n only schedules and calls the Python service, which talks to Postgres itself.

## 10. Check everything

```bash
scripts/jobsearch doctor
```

You want no failures. Warnings tell you what is still to do (for example "run /setup" if you skipped step 8). `doctor` also lists which optional parts are on. Then try the pieces once by hand, in this order:

1. **Collect:** in n8n, open **01 Collect jobs** and click **Execute workflow** (or `docker compose exec -T -e N8N_RUNNERS_BROKER_PORT=5690 n8n n8n execute --id=js-collect`). Then `scripts/jobsearch status` should show new jobs.
2. **Score:** run **02 Score jobs** the same way and check the scores look sensible (`scripts/jobsearch status` lists the top matches). Real, small cost: about 3 cents for 100 jobs.
3. **See the results without Telegram:** `scripts/jobsearch jobs` lists your shortlist, `scripts/jobsearch job <id>` shows one in detail.
4. **Telegram** *(only if you set it up)*: send `/help` to your bot. It should answer within a minute.
5. **Tailor** *(only if tailoring is on)*: `scripts/jobsearch tailor <job id>` on one scored job. About 5 cents. The PDFs appear under `output/applications/` (`scripts/jobsearch files <id>` says where), and, with Telegram, in your chat.

After that the schedules run on their own. Next: [daily-use.md](daily-use.md).

## 11. Enable the dashboard (optional)

A web page with five parts: **Jobs** (your numbers, charts, every job, and where you record each stage of an application), **Health** (is everything running, with a pause button), **Spend** (Claude and Apify against their caps), **Settings** (schedules, optional parts, tailoring, caps you can lower) and **Search** (what to search for, which sources, which companies). It also fits a phone. **Skip it if the CLI and Telegram are enough**: they do everything it does, and you can add it any time.

```bash
scripts/init-env.sh --add-dashboard                                   # adds the secrets and COMPOSE_PROFILES=dashboard; prints names only
docker compose up -d --build --no-deps dashboard-db-init dashboard    # builds and starts only the dashboard
scripts/jobsearch doctor                                              # "dashboard: ok on 127.0.0.1:8080"
```

It listens on `127.0.0.1:8080` only. On the same computer open <http://localhost:8080>; on a server open it through the same tunnel as n8n (`scripts/tunnel.sh`). The password is `DASHBOARD_PASSWORD` in `.env` (you can change that line, then `docker compose --profile dashboard up -d --no-deps dashboard`). Details, troubleshooting and how to turn it off: [dashboard.md](dashboard.md#open-the-dashboard).

## 12. Open the dashboard on your phone (optional)

With the dashboard on a server, [Tailscale](https://tailscale.com) (free) gives your phone a private way in without opening any port: install it on the server, your phone and your computer, then publish the dashboard to your own devices only with `tailscale serve`. Step by step, including the one setting that must also be allowed in the dashboard: [dashboard.md](dashboard.md#opening-it-from-a-phone-without-ssh-tailscale).

## Backups

Your database holds your jobs, scores and applications, and your `config/*.json` files exist only on this machine. Nothing backs them up for you, so set that up now (two minutes):

```bash
scripts/backup.sh                    # backups/ gets the jobs database, n8n's database and your config files, readable by you only; the newest 14 of each are kept
crontab -e                           # then add this line, with your own folder, to do it every night at 03:15:
# 15 3 * * * cd /home/you/ai-job-search-n8n && scripts/backup.sh >> backups/backup.log 2>&1
```

(On a Mac, cron has a bare-bones `PATH`; put `PATH=/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin` on its own line above the schedule line, or run the script by hand now and then.)

What is in `backups/`: `jobsearch-<time>.sql.gz` (the jobs database), `n8n-<time>.sql.gz` (n8n's workflows, owner account and saved credentials, the credentials in encrypted form) and `config-<time>.tar.gz` (`profile.json`, `resume.json`, `search.json`). It does **not** contain `.env`. Keep a copy of `.env` somewhere safe, above all `N8N_ENCRYPTION_KEY`: without it n8n cannot read its saved credentials. **Copy `backups/` and `.env` off the machine** now and then (to another computer, or encrypted cloud storage): a backup next to the data it protects is lost with it.

**Restore the jobs database** (this replaces it):

```bash
docker compose stop scraper n8n dashboard
docker compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d postgres -c "DROP DATABASE jobsearch" -c "CREATE DATABASE jobsearch"'
gunzip -c backups/jobsearch-<date>.sql.gz | docker compose exec -T postgres sh -c 'psql -U "$POSTGRES_USER" -d jobsearch'
docker compose up -d
scripts/jobsearch doctor
```

**The whole server is gone** (the Oracle instance was reclaimed, or you moved): on a new server, [create it](vm-oracle.md), `git clone` the repository, put your saved `.env` back (the same `N8N_ENCRYPTION_KEY`), unpack `config-<date>.tar.gz` in the repository folder (`tar -xzf config-<date>.tar.gz`), `docker compose up -d --build`, restore the database as above (and n8n's the same way, with `-d n8n` and `n8n-<date>.sql.gz`, after stopping n8n), `docker compose up -d`, then `scripts/jobsearch doctor`. If you did not keep the n8n backup, run `scripts/n8n-credentials.sh` and `scripts/n8n-import-workflows.sh --publish` instead and create the owner account again.

## Upgrading

Schema changes ship as numbered files in `postgres/migrations/`. After pulling an update:

```bash
scripts/backup.sh                                                        # backup first
scripts/jobsearch migrate                                                # FIRST: applies only what is pending (safe to re-run)
docker compose build --pull scraper                                      # then rebuild the scraper image on a fresh base image
docker compose up -d --build                                             # and start the new code and mounts
scripts/n8n-import-workflows.sh --publish                                # updates the workflows in place
scripts/jobsearch doctor
```

**Migrate first, then rebuild the scraper.** A migration only adds things (new columns with defaults, new tables), so the old code keeps working after it. The reverse order does not: new code running before its migration fails to read settings the database does not have yet. For example, the company-board release needs migration 013, and without it the collect run and the ingest step fail for everyone, including people who never use company boards. Scoring is affected too: if the new scraper image runs before the migration, a scoring step can fail after the model call was already paid for. The scraper now copes with the missing columns, but the order is still migrate first, then rebuild.

`python:3.12-slim` gets security fixes through new image pulls; rebuild with `--pull`.

With the dashboard on (`COMPOSE_PROFILES=dashboard`), `docker compose up -d --build` rebuilds and restarts it too. To update only the dashboard: `docker compose up -d --build --no-deps dashboard`.

The SQL tests (`docker compose exec -T postgres sh /tests/run.sh`) run on throwaway databases and never touch yours.

## Oracle Cloud notes

Creating the free server, installing Docker on it and looking after it: [vm-oracle.md](vm-oracle.md). In short: keep the default firewall (SSH only, nothing here needs an inbound port), back up the database ([above](#backups)), and remember Always Free instances can be reclaimed if they sit idle for a long time (see the note in [vm-oracle.md](vm-oracle.md#looking-after-it)).
