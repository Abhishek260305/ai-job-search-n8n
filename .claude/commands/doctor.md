---
description: Check the health of the whole job-search stack and explain how to fix anything that is wrong.
---

# /doctor

Run `scripts/jobsearch doctor` and report the result.

For each ⚠ or ✗ line, explain in one or two sentences what it means and the exact fix:
- container not running: `docker compose up -d` (and `docker compose logs <service>` to see why)
- pending migrations: `scripts/jobsearch migrate`
- missing `profile.json` / `resume.json` / placeholder search profile: run `/setup`
- low Apify credit: that account will be skipped automatically; mention which ones have credit left
- a failure the CLI cannot explain: read the relevant `docker compose logs` before guessing

If the CLI cannot reach docker at all, explain that it runs next to the compose stack, and that for a remote host `JOBSEARCH_SSH` and `JOBSEARCH_REMOTE_DIR` can point it there (see `docs/commands.md`).

Only run a fix command after the user agrees. Never print the contents of `.env`.
