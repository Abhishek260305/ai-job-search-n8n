# n8n workflows

| File | ID | Started by | Purpose |
|---|---|---|---|
| `00-scheduler.json` | `js-scheduler` | its own timer, every 5 min | ask the service what is due and start the workflows below |
| `01-collect-jobs.json` | `js-collect` | the scheduler (default every 6 h) | plan, scrape, then ingest (safety, dedupe, free rule filters); the plan names the endpoint of each request, so company boards and remote feeds (`/fetch/board`) need no change to this file |
| `02-score-jobs.json` | `js-score` | the scheduler (default every 30 min) | claim unscored jobs, ask Claude, validate and store the result |
| `03-daily-digest.json` | `js-digest` | the scheduler (default daily 08:00) | send new shortlisted matches to Telegram, mark them as sent |
| `04-tailor-applications.json` | `js-tailor` | the scheduler (default every 15 min) | tailor the next shortlisted jobs (settings live in the database) |
| `05-telegram-commands.json` | `js-telegram` | its own timer, every minute | act on your Telegram commands |
| `06-check-mail.json` | `js-mail` | the scheduler (default every 30 min, only while the optional Gmail part is on) | for the optional Gmail part: will look for replies to your applications and propose status changes (suggestions only); today it reads nothing ([docs/gmail.md](../docs/gmail.md)) |

**The schedules are settings, not part of these files**: `scripts/jobsearch schedule`. Workflows 01-04 and 06 have no schedule trigger of their own; they are started by `00` through their *Run on demand* (Execute Workflow Trigger) node, so they can also be run by hand from the n8n UI.

Full guide (credentials, import, testing, scheduling, troubleshooting): [docs/workflows.md](../docs/workflows.md).

```bash
scripts/n8n-credentials.sh                  # create the Anthropic / Telegram credentials from .env (or use the n8n UI)
scripts/n8n-import-workflows.sh --publish   # import (filling in your Telegram chat id), schedule, restart n8n
```

## Notes for contributors

- The files are generated-looking JSON on purpose: keep them stable (fixed workflow `id`s, fixed node ids) so re-importing updates in place and diffs stay readable.
- Each workflow also has a **Run on demand** (Execute Workflow Trigger) node: it is how the scheduler starts 01-04 and 06, and n8n 2.x's CLI can only start workflows that have one.
- **No credentials are stored in these files.** They reference credentials by id and name (`js-anthropic-key` / `Anthropic API key`, `js-telegram-bot` / `Telegram bot`). Never export with the "include credentials" option.
- Keep logic out of the workflows. Anything with rules (filters, scoring, dedupe, safety) belongs in the Python service where it is unit-tested; the workflows should stay thin HTTP calls.
