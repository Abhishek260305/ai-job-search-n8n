---
description: Show or change when each stage runs (collect, score, tailor, digest) and how much each run does.
argument-hint: [collect|score|tailor|digest] [every 6h | at 08:00 | batch N | on | off | run]
---

# /schedule

Run `scripts/jobsearch schedule` and explain it in plain words: what runs how often, how much one run does, when it last ran and runs next, and the timezone.

If `$ARGUMENTS` asks for a change, translate it to one command and run it:

- every N: `scripts/jobsearch schedule set collect --every 6h` (also `30m`, `1d`; collect cannot be faster than every 15 minutes, to be gentle on the job boards)
- digest: daily at a time with `--at 08:00`, or every N hours with `--every 6h` (at least 1 hour; a message goes out only when there are new matches)
- how much per run: `--batch N` (searches for collect, jobs for score, applications for tailor, matches for digest)
- company boards and remote feeds per collect run: `scripts/jobsearch schedule set collect --boards N` (default 30, 1 to 200; separate from the searches; it only matters when the user has switched those sources on, see `/sources`)
- scoring model (score only): `--model claude-haiku-5-5`
- on or off: `--on` / `--off`
- run once now: `scripts/jobsearch schedule run collect` (starts at the next 5-minute tick)
- emergency brake: `scripts/jobsearch schedule pause --reason "..."` stops every scheduled stage (even "run now") until `scripts/jobsearch schedule resume`; do this at once if the user is worried about spend
- timezone: `scripts/jobsearch schedule timezone Asia/Kolkata` (also moves the spending-cap day boundaries)

Changes apply from the next scheduler tick (within 5 minutes); no restart. Explain the trade-off when it matters: a larger `batch` or shorter interval means more searches against the job boards (rate limits) and, for score and tailor, more Claude spend, which the budget caps still bound. If `doctor` says the scheduler workflow is not published, nothing runs on schedule: the fix is `scripts/n8n-import-workflows.sh --publish`.
