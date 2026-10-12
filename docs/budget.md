# Budget: logging and a hard cap on paid usage

The pipeline calls a paid model API for three things: **scoring** jobs, **tailoring** applications, and the **fact check** on tailored text. Two independent safeguards keep that bounded, and every call is logged (with its purpose) so you can see exactly what was spent.

| Layer | Where | What it does |
|---|---|---|
| **1. App cap** | this repo (migration 005, `budget.py`) | Logs every call's tokens and cost. Before each batch it reserves the **worst-case** cost and claims only as many jobs as the monthly **and** daily remainder can pay for. At a cap it claims nothing. |
| **2. Provider limit** | the Anthropic Console (you set it) | A spend limit enforced by the provider itself, which holds even if something in our code were wrong or another tool used the same key. |

Default caps: **$10 per month**, **$1.50 per day**, warning at **80%** of the month. Change them any time (below).

**Apify has its own caps**, separate from Claude's: **$15 per month** and **$0.50 per day** by default (the free credit of three accounts, paced over the month). A LinkedIn-via-Apify search starts only if its hard charge limit still fits in what is left; otherwise it is *skipped* (the free sources carry on), never failed. See [runaway-protection.md](runaway-protection.md).

## See and change spend

```bash
scripts/jobsearch budget                                   # spend vs caps, and a per-model breakdown for the month
scripts/jobsearch budget set --monthly 10 --daily 1.5      # change the Claude caps (also --warn-pct 80)
scripts/jobsearch budget set --apify-monthly 15 --apify-daily 0.5   # change the Apify caps
scripts/jobsearch status                                   # includes a spend line
scripts/jobsearch doctor                                   # warns at the warning level, and when scoring is paused
```

The daily digest ends with `Claude spend this month: $x of $10`, and a warning is sent on its own when spend passes the warning level or a cap pauses scoring (so a paused pipeline is never silent).

## How the cap works

1. **Actual usage is logged.** After every call, the provider's own token counts (input, output, cache writes, cache reads) are priced from the `model_pricing` table and stored in `llm_usage`. An unusable reply is still logged, because the tokens were spent.
2. **Worst case is reserved before a batch.** Each job is priced as if it used a long prompt (at 3 characters per token, deliberately pessimistic), a cache write, and the full `max_tokens` of output. A batch claims `floor(remaining / worst_case_per_job)` jobs, capped by what you asked for, using exact decimal arithmetic.
3. **Both windows apply.** The lower of the monthly and daily remainder binds. Months and days follow the configured timezone (default `Asia/Kolkata`).
4. **Unknown models fail expensive.** A model missing from `model_pricing` is priced at the most expensive known rates and flagged in the log, so a typo can only make the guard stricter.
5. **Claude and Apify are separate budgets.** Apify spend is logged per run (the larger of the actor's price estimate and any measured balance change, or 1.5 times the estimate when nothing was measured yet, because Apify updates balances late) and counts against the Apify caps, not the Claude caps. Each Apify account also has its own hard credit limit.

Verified on a live system: with the cap set below one worst-case job, a batch request claimed **0** jobs and nothing was spent.

## Measured cost (Claude Haiku 5.5, real runs)

| | Tokens | Cost |
|---|---|---|
| One scoring call | about 2.5k in (most of it cached after the first call), about 340 out | **about $0.0003** |
| 1,000 jobs scored | | about $0.30 |
| Worst case reserved per job | | about $0.0008 |

Prices used (per million tokens, from Anthropic's pricing page, checked 2026-10-11): Haiku 5.5 $0.10 in / $0.50 out / $0.01 cache read; Sonnet 5.5 $2 / $10 / $0.10; Opus 5.5 $4 / $20 / $0.20. Batch requests are 50% cheaper. Haiku 5.5 charges higher rates only for prompts over 100,000 tokens, far beyond ours.

Tailoring on Claude Sonnet 5.5 (measured on real runs): about **$0.04-0.05 per application** for the draft plus one revision, plus about **$0.001** for the Haiku fact check. The fact check is logged as its own purpose (`tailor_verify`), separate from `tailor`.

**What $10 buys.** Scoring alone is well under $2 a month even for hundreds of new jobs a day. At about 5 cents each, $10 comfortably covers scoring plus on the order of 100-150 tailored applications a month. If you tailor far more than that, raise the cap, set `max_score` / `daily_limit`, or use a cheaper model for tailoring.

## Set the provider-side limit (layer 2)

In the Anthropic Console, set a **monthly spend limit** for your organization or, better, create a **workspace** just for this project, give it its own spend limit and its own API key, and use that key. Console labels change over time; look for **Limits** or **Spend limits** under settings. Set it at or slightly above your app cap (for example $12 against a $10 app cap) so the app-level pause normally triggers first and the console is the backstop.

## Keeping prices current

Prices live in `model_pricing`. If Anthropic changes them, or you switch models:

```bash
docker compose exec -T postgres psql -U jobs -d jobsearch -c \
  "INSERT INTO model_pricing VALUES ('claude-haiku-5-5', 0.10, 0.50, 0.125, 0.01, 'pricing page', current_date)
   ON CONFLICT (model) DO UPDATE SET input_per_mtok = EXCLUDED.input_per_mtok, output_per_mtok = EXCLUDED.output_per_mtok,
   cache_write_per_mtok = EXCLUDED.cache_write_per_mtok, cache_read_per_mtok = EXCLUDED.cache_read_per_mtok,
   source = EXCLUDED.source, verified_on = EXCLUDED.verified_on"
```

Columns are dollars per million tokens: input, output, 5-minute cache write, cache read.

## What is not covered

- Calls made outside these workflows (a manual `curl`, another app using the same key) are not in the log. That is exactly what the provider-side limit is for.
- The estimate for prompt size is pessimistic but not exact; real usage, not the estimate, is what gets logged and counted.
- Pricing can change without notice: check `verified_on` in `model_pricing`.

## Model behaviour that affects cost

Some current models **think by default**, producing hidden reasoning tokens that are billed as output. For scoring this is wasteful, so the workflow sends `thinking: {type: "disabled"}`, and the prompt asks for terse single-line JSON. Measured on Claude Haiku 5.5: a pretty-printed, thinking-enabled reply used 1,265 output tokens and overran the old limit; the current setting uses about 340. If you change models, check the reply shape and the token count of one call before scheduling the workflow.
