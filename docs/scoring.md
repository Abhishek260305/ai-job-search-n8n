# Scoring

Each job that survives the safety check and the free rule filters is scored once against your profile by a cheap model. The model only **reports**; the score, verdict and every pass/fail decision are **computed in code** (`scraper/scoring.py`), so neither the model nor a posting trying to manipulate it can change the outcome.

```
sanitised + fenced posting ──► model (Haiku-class) ──► JSON ──► validate ──► compute ──► record_score() ──► jobs row
        /score/prepare             n8n or CLI                    /score/finalize                           (Postgres)
```

Scoring never uses the scraper service's model API key (the service holds one only for tailoring and the fact check). `/score/prepare` returns the `system` and `user` messages, you (n8n or the CLI) send them to the model, and `/score/finalize` validates the reply.

## Your profile

`config/profile.json` (gitignored; start from [`config/profile.example.json`](../config/profile.example.json), or let `/setup` build it). It is validated on every call; required fields are `headline`, `years_experience`, `skills.strong`, `location.home`, `languages` and `work_authorization` (use `[]` if you have no work-authorization constraints). Optional fields sharpen the scoring: `target_roles`, `experience_domains`, `career_goals`, `energizing`, `draining`, `deal_breakers`. Be honest: gaps you leave out here turn into bad matches later.

## Dimensions and weights

| Dimension | Weight | Question |
|---|---|---|
| technical | 30% | Do the required and preferred skills match yours? |
| experience | 25% | Does your work history match the role's function (not just the title)? |
| behavioral | 15% | Does the working style the posting reveals fit what energizes and drains you? |
| career | 30% | Does it advance your goals and contain tasks that energize you? |

Each is an integer 0-100. A posting with no signal for a dimension scores 50 and the missing information is listed under gaps. `behavioral` is the weakest dimension from posting text alone (no company research happens at triage), so its weight is the lowest.

**Verdict bands** on the weighted overall score: strong 75+, good 60-74, moderate 45-59, weak 30-44, poor under 30.

## Gates

A gate is a hard filter, separate from the score. The model gives a verdict and a note quoting the posting:

| Gate | FAIL means | FLAG / UNKNOWN means |
|---|---|---|
| eligibility | judged by the **country the job is in**: a country you are authorized in passes (FAIL only if the posting names a requirement you do not meet, such as a clearance); in another country, FAIL if it requires local work rights or citizenship | UNKNOWN: the job is in a country you are not authorized in and the posting says nothing about work rights (verify on the employer's site), or it is remote with no country stated. FLAG: unclear |
| language | a language the job itself requires is not in your profile at all | FLAG: the stated bar looks above your declared level |
| location | relocation you ruled out, or on-site somewhere unreachable | FLAG: heavy travel or unclear arrangement |

A **FAIL excludes the job** (verdict `excluded`, status `filtered_out`, reason recorded) however high the score. FLAG and UNKNOWN stay in the ranking with a warning for you to judge. The number is still stored so you can see what you excluded.

## Extracted facts

The same call extracts the application `deadline`, `posting_language`, required years of experience (`yoe_min` / `yoe_max`), up to three `strengths` and three `gaps`, and a one-sentence `reason`. These feed the deadline sweep, the years-of-experience filter, and the review messages.

## Output contract

The model must return one JSON object. `scoring.validate_result()` is strict: wrong types, out-of-range numbers, bad dates and missing gates are rejected with every problem listed (HTTP 422), unknown keys are dropped, lists are capped at three items and clipped, and the model's own `score`/`verdict`/`status` fields are ignored. On a 422, retry once, then mark the job for manual review instead of storing anything.

## Storing a result

`record_score(job_id, finalize_output)` (migration 002) writes everything in one call and returns the new status:

| Outcome | Status |
|---|---|
| gate failed | `filtered_out` (with `filter_reason`) |
| score at or above `score_threshold` | `shortlisted` |
| otherwise | `scored` |

It never moves a job that is already `tailored`, `approved`, `applied`, `scam_blocked` or `expired`.

## Deadlines and staleness

- `sweep_expired()` retires open jobs whose `deadline` has passed. A job with no deadline is never touched.
- `v_shortlist` lists `scored`/`shortlisted` jobs that are not excluded, with `closing_soon` (deadline within 7 days) and `stale` (posted more than 30 days ago). Staleness is a flag, not a veto: old postings are often still open.

## Cost

Measured on real runs with Claude Haiku 5.5: about **$0.0003 per scored job** (roughly 2.5k input tokens, mostly served from the prompt cache after the first call, and about 340 output tokens). The static rubric and profile come first in the system prompt and are marked for caching; Haiku 5.5's minimum cacheable prompt is only 286 tokens, so caching applies, and a 10-job run showed about 17,500 cached input tokens. The posting is capped at 6,000 characters. Safety and rule filters run first, so the model only sees jobs worth scoring. Spending is logged and hard-capped: see [budget.md](budget.md).

**Request settings that matter.** The workflow sends `thinking: {type: "disabled"}` (some current models think by default, which bills hidden reasoning tokens as output) and `max_tokens: 900`; the prompt asks for terse, single-line JSON with empty notes on passing gates. In the first real run, thinking plus pretty-printed JSON produced about 1,265 output tokens per call and overran the earlier 700 limit, so every reply was cut off mid-JSON and rejected. The validator and the logging caught it, and cost only half a cent.

## Tuning

The outcome is decided once, with the threshold of the moment. If you later lower the threshold, `scripts/jobsearch reshortlist --apply` shortlists the jobs that were scored in between.

Score a few dozen jobs you have opinions about and compare. If the model is too generous or harsh, adjust the wording in `_RUBRIC` or `score_threshold` in your search profile, not the code that decides outcomes.

## The free years-of-experience filter (runs before scoring)

Before any model is called, the free filters read the years of experience a posting asks for (`scraper/filters.py`) and drop a job whose requirements do not overlap your `min_yoe` / `max_yoe`. It is deliberately conservative: it only counts a number as a requirement when the text really looks like one, so "founded 10 years ago" or "we have 12 years in the industry" never drop a job. A number counts when it is next to the word "experience", when it starts a bullet point ("- 5-8 years in software engineering"), or when it follows a requirement phrase ("requires", "at least", "minimum", "you have"). A word glued onto "years" by a typo in the posting ("5-8 yearsin") is tolerated.

This matters because **the score is a blend, not a cutoff**: a strong technical and career fit can outweigh a seniority gap (a job asking for 5-8 years scored 73 for someone with 1.25 years). The filter is what stops clearly out-of-range jobs from reaching scoring at all. When it misses one, the posting's years still appear in the job's gaps (`scripts/jobsearch job N`), and the score reflects them only partly.

It applies when a job is first collected. Jobs already in the database are not re-filtered.
