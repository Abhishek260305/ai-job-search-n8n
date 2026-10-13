# Costs

The aim is as close to $0 as possible. Only one thing costs money: **Claude API usage**, and it is capped.

| Item | Approach | Cost |
|---|---|---|
| Hosting | free-tier VM (for example Oracle Cloud Always Free) | $0 |
| n8n, Postgres, PDF rendering | self-hosted, open source | $0 |
| Scraping | JobSpy (free); Apify's free monthly credit for LinkedIn is optional and capped per run | $0 (Apify: about $0.70-$1.00 per 1,000 jobs if you use it) |
| Company job boards and remote feeds | public APIs and feeds, no key ([sources.md](sources.md)) | $0 to fetch; scoring the new jobs they bring is the only cost (below) |
| Notifications and review | Telegram bot | $0 |
| Web dashboard (optional) | self-hosted, through the SSH tunnel; no calls to Claude, Apify or Telegram ([dashboard.md](dashboard.md)) | $0 |
| Email tracking (optional, [gmail.md](gmail.md)) | Gmail API: $0. Classifying the few unclear replies with Haiku (step 6d) is the only cost: about 0.02 cents per email, at most 40 calls a day (about **$0.01 a day** at the very most), under the same caps and the same Console limit |
| **Scoring** | Claude Haiku 5.5 | **about $0.0003 per job** (about $0.30 per 1,000) |
| **Tailoring** | Claude Sonnet 5.5 (draft + one revision) plus a Haiku fact check | **about $0.04-0.05 per application** |

Both Claude numbers are **measured on real runs**, not estimates. Prices change; check Anthropic's pricing page and the `verified_on` column in `model_pricing`.

## Cost by setup

| Setup ([customization.md](customization.md)) | Claude spend |
|---|---|
| **A. Scoring only** (tailoring and Telegram off) | scoring only: about $0.0003 per job, a few dollars a month at most |
| **B. + tailoring** | A, plus about $0.05 per application |
| **C. + Telegram** | the same as B (Telegram is free) |

## Company boards and remote feeds

No paid API is involved in fetching a company board or a remote feed. The only cost is Claude **scoring of the new jobs** they bring: about **$0.0003 each** at the default model, under the same spending caps as everything else. Apify spend is unaffected.

- A free relevance filter (title, location, age, then your usual filters) runs **before** anything reaches scoring, and at most **25 jobs per company or feed per fetch** get through, newest first, so the volume is bounded before it can cost anything ([sources.md](sources.md#how-the-jobs-are-filtered-before-anything-is-scored)).
- A job already seen is never scored again, so reading the same board every few hours adds only what is new. Each fetch returns only the **newest 25** relevant jobs per company or feed, so a company with many matching openings shows its older ones only after newer ones are taken down from the company's board: nothing here promises that every opening is eventually seen.
- **The first run with many companies is the big one.** Many companies together can add a few hundred jobs at once: about **$0.15 per 500 jobs**. The scorer works through **25 jobs every 30 minutes** (about 1,200 a day), so a big first batch is spread over a day or so rather than all scored at once. One collect run visits 30 boards and feeds by default, so a run adds at most 750 jobs (about $0.23), and usually far fewer.
- Prefer to start small: add 3 to 5 companies and one feed, watch one collect run, then widen.

## What a month looks like

| Usage | Rough cost |
|---|---|
| 300 jobs scored a day for a month | about $2.70 |
| 100 tailored applications | about $5 |
| both | about $8, inside the default $10 cap |

Most people will spend far less: filters and the threshold mean only a fraction of collected jobs are scored, and fewer still are tailored. Setting `max_score` (use your base resume when the match is already excellent) and `daily_limit` lowers it further.

## The caps

- **App cap:** $10 a month and $1.50 a day by default. Every call is priced at its worst case before it runs and refused if it would break a cap; real usage is logged afterwards. Check with `scripts/jobsearch budget`.
- **Provider cap:** set a monthly spend limit in the Anthropic Console, slightly above the app cap (for example $12). This also covers calls made outside this project with the same key.

Both are explained in [budget.md](budget.md). Only you can raise a cap.

## Keeping spend low

- Dedupe and the free filters run first, so the model sees few jobs. This holds for company boards and remote feeds too: they get their own strict free filter first.
- Score with the smallest capable model and store the result so a job is never re-scored.
- Tailor only above the score threshold; the model returns a JSON **patch**, not a whole resume.
- The static part of each prompt (instructions plus your resume or profile) goes first so prompt caching can apply.
- Cap results per scrape run and the number of applications per day (`daily_limit`).
