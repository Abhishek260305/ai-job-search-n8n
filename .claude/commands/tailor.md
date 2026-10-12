---
description: Tailor the resume and write a cover letter for a job (or the next eligible ones), with the honesty checks.
argument-hint: [job id]
---

# /tailor

`$ARGUMENTS` may be a job id; with none, the next eligible shortlisted jobs are tailored.

1. Run `scripts/jobsearch tailor $ARGUMENTS` (this calls the model, so it costs money: roughly 5 cents per application; it is capped by the budget). If the user has not said which job, run `scripts/jobsearch review` or `scripts/jobsearch status` first and ask.
2. Explain the result in plain language: the status, requirement coverage (matched / bridged / gaps), anything flagged "needs attention", and what the honesty checks removed (`summary.md` in the application folder lists it). Offer to read `summary.md` with them.
3. Never claim the documents are "verified true". They passed an automatic word-level audit and a model fact check; the user must still read them. Point out anything flagged.
4. To decide: `scripts/jobsearch approve N`, `skip N`, or `applied N`. Do not approve or mark applied on the user's behalf unless they say so.
5. Files: `scripts/jobsearch files N` shows where the PDFs are. There is **no auto-apply**: the user submits applications themselves.
