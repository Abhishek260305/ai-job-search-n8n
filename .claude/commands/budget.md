---
description: Show Claude spend against your monthly and daily caps, and change the caps if asked.
argument-hint: [set --monthly 10 --daily 1.5]
---

# /budget

Run `scripts/jobsearch budget` and explain it in plain language: spend this month and today against the caps, what is left, whether the pipeline is paused, and the per-model breakdown.

- If the user wants different caps, run `scripts/jobsearch budget set --monthly <usd> --daily <usd>` (and `--warn-pct` if asked) after confirming the numbers, then show the result.
- If status is PAUSED, say which cap was reached and when it resets (the daily cap resets at midnight in the configured timezone, the monthly cap on the 1st). Do not raise a cap on your own initiative.
- Mention that Apify has its own separate caps (`--apify-monthly`, `--apify-daily`). Remind the user once that the app cap is layer 1, and that a spend limit in the Anthropic Console is the independent layer 2 (see `docs/budget.md`).
- Never print or ask for an API key.
