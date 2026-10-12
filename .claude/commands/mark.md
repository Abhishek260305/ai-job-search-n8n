---
description: Record where an application stands by hand (applied, interview, offer, rejected, ...): the no-Gmail way to track status.
argument-hint: <job id> <stage> [note]
---

# /mark

Stages: `approved`, `skipped`, `applied`, `in_progress`, `interview`, `offer`, `rejected`, `withdrawn`.

Run `scripts/jobsearch mark N STAGE [--note "..."]` **only** for the job and stage the user named. It works for any job, tailored or not, from any previous stage (the user is the source of truth). Then run `scripts/jobsearch job N` to show the result.

Never mark a job `applied` or anything later on your own initiative, and never guess a stage from a posting or an email: the user tells you what happened.
