---
description: List tailored applications waiting for a decision, and approve or skip them.
---

# /review

Run `scripts/jobsearch review`. For each waiting application summarise: job, score, variant, and any "needs attention" reason. Offer `scripts/jobsearch files N` (where the PDFs are) and read `summary.md` from the folder if the user wants the details (coverage, what changed, what the honesty checks removed).

Applications that were tailored appear here. If tailoring or Telegram is off, use `/jobs` to see the shortlist and `scripts/jobsearch approve|skip|applied N` still works on any job (a decision is recorded without files).

Only run `approve`, `skip` or `applied` when the user clearly asks for that specific job. Never submit anything anywhere: there is no auto-apply.
