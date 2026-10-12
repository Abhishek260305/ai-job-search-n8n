# Runaway protection: can anything loop and burn my credits?

Short answer: **no loop can spend without limit.** Every path that costs money has at least three independent limits, and a rehearsal against stand-in APIs proves the numbers below on every change (see [testing.md](testing.md)). This page lists each paid path, what stops it, and the emergency brakes.

## The paid paths

| Path | What can spend | What stops a loop |
|---|---|---|
| **Scoring** (Claude Haiku, about $0.0003 a job) | the 30-minute scoring run | 1. **Spending caps** ($10 a month, $1.50 a day by default): the worst-case cost of a batch is reserved *before* any job is claimed, and at a cap nothing is claimed. 2. **Counted claims:** every attempt to score a job is counted; after **6** unfinished attempts, or **3** unusable replies, the job is *parked* and never retried until you re-queue it. 3. **Refunds:** an attempt that cost nothing (the API was down) is handed back, so an outage never parks healthy jobs. 4. A job that scores is never scored again. |
| **Tailoring** (Claude Sonnet + a Haiku fact check, about $0.05 an application) | the 15-minute tailor run | the same spending caps, checked per job; at most **3** claims per job (then parked), at most **2** failed drafts; one draft plus **one** revision per attempt; a job that has an application is never claimed again; `daily_limit` if you set one; a failure *after* a billed draft is counted, a failure before anything was billed is refunded |
| **Tailoring on request** (the dashboard's "Tailor this job": the same Claude calls, started by a person) | one click | the same spending caps, priced at the worst case before it starts; the cost shown and **agreed to** before it runs (the scraper refuses if it would now be more); **one run at a time**; a **two-minute cooldown** per job (kept across a scraper restart) so a double click or a retry cannot pay twice; every condition **checked again inside the tailoring lock** right before spending (it never queues behind a scheduled run); the PDF service checked before a draft is paid for; not while the Tailoring switch is off, nor while paused, nor for an application already submitted. The automatic `daily_limit` and the failure counting that parks jobs do not apply: you asked for it, and the caps still hold |
| **Apify** (LinkedIn search, about $0.01 to $0.05 a run) | the 6-hour collect run | its own **monthly ($15) and daily ($0.50) caps**; a run starts only if its hard charge limit still fits in what is left of the budget; **at most 2 runs per search**; each run carries a hard `maxTotalChargeUsd`; a run that hangs is **aborted after 10 minutes**; a failed run that had started is counted at 1.5 times its estimate; if the budget cannot be read, **nothing is started** |
| **Company boards and remote feeds** | free: nothing is billed; the risk is hammering the sources | see [below](#company-boards-and-remote-feeds) |
| **Scheduler** | starting the stages | a stage is marked as started the instant it is claimed, so a slow run is never started twice; a failed stage is *not* retried until its next turn; minimum intervals (collect at least 15 minutes); and the **global pause** below |
| **Telegram** | `/tailor` commands | a command is acknowledged before it runs, so a retry can never run it twice; polling is single-flight |

## What the rehearsal measured

Real service code and real SQL, a throwaway database, and stand-in APIs set to misbehave, repeated far more often than the scheduler would:

| Scenario | Result |
|---|---|
| Claude API down for 30 hours of scoring runs | **0** billed calls, **0** jobs wrongly parked |
| replies billed but never recorded (the worst case for scoring) | exactly **6** calls per job, then parked (before the guard: unlimited) |
| model returns garbage | exactly **3** calls per job, then parked |
| daily cap of $0.01 with 200 jobs waiting | stops at **$0.009** |
| tailoring: draft billed, then the revision hits an outage, for 10 hours | **3** drafts per job, then parked |
| tailoring: good draft, then the PDF service is down | **2** drafts per job, then parked |
| tailoring on request: 500 presses in 25 seconds | **1** run, **1** billed call |
| tailoring on request: a press every two minutes all day (400 presses) | **$1.42**, never over the $1.50 daily cap |
| Apify: 100 searches in a row (about four days' worth at 6 searches × 4 runs a day) | **10** runs, **$0.47**, then every search is skipped by the cap |
| Apify: three accounts configured, 100 searches | **$0.47**, never over the cap |
| Apify: every run starts and then fails | **9** runs, then skipped |
| Apify: every start refused | **$0** |
| Apify: a run that never finishes | polled **120** times, then aborted once |
| scheduler: 2,000 ticks in the same minute | each stage started **once** |
| scheduler paused, with "run now" requests | **0** starts |

Run it yourself any time (it costs nothing): see [testing.md](testing.md#the-runaway-rehearsal).

## Company boards and remote feeds

Fetching company job boards and remote feeds ([sources.md](sources.md#company-job-boards)) costs **nothing**: no paid API, no key. The risks there are hammering a free service, a runaway download, and flooding the scorer, so they are bounded too:

| What could go wrong | What stops it |
|---|---|
| asking the same source again and again (a short collect schedule, "run now" pressed repeatedly) | every fetch first **claims** its source in the database with a single statement; a source is asked at most once per **minimum interval**: 6 hours for Remotive, 1 hour for the other feeds, 10 minutes for one company board. Inside the interval the answer is "fetched recently", with zero jobs and no request |
| one collect run visiting too many | a run visits at most `board_batch` boards and feeds (default 30, at most 200: `schedule set collect --boards N`) and rotates through the rest on its own cursor, so they never take slots from the searches |
| a huge or never-ending response | every response is read through a **32 MB** cap (refused from its header, or while streaming) with a **30 second** timeout; the two huge feeds (Himalayas, Arbeitnow) stop after **3 pages** or at the first job older than `max_age_days` |
| retrying a failing source in a loop | a server error or timeout is retried **once**; a rate limit (HTTP 429) is **never** retried and stops that source for the run; a source that fails is counted in `fetch_state` and shown by `doctor` after 5 failed runs in a row (the dashboard's Health alert fires after 3) |
| contacting anything else | only the eleven **fixed hosts** (the five job-board APIs, Lever's EU host and the five feeds) are ever requested; a company name must be a valid slug; links inside postings are never fetched |
| a flood of new jobs reaching paid scoring | a free relevance filter runs first and **at most 25 jobs per company or feed per fetch** pass; everything that does pass is subject to the scoring limits above (spending caps, counted attempts, parking) |

So nothing here can loop on money: fetching is free and bounded, and the only paid step that follows is the same capped, counted scoring as for every other source. These bounds are covered by unit tests against stand-in servers ([testing.md](testing.md#the-stand-in-apis-scraperstubspy)); the runaway rehearsal (described under [what the rehearsal measured](#what-the-rehearsal-measured)) does not include company-board scenarios.

## Parked jobs

A parked job is one that **ran out of attempts**. It is skipped (never loops) and shown, not hidden:

```bash
scripts/jobsearch parked                     # what is parked and why
scripts/jobsearch requeue score              # put every parked job of a stage back
scripts/jobsearch requeue tailor 24          # or one job
```

`scripts/jobsearch doctor` and `status` warn when anything is parked. Usually a parked job means a real problem worth looking at first (a posting that breaks the model's reply, a PDF service that is down), not something to retry blindly.

## Emergency brakes

```bash
scripts/jobsearch schedule pause --reason "checking the bill"   # nothing scheduled runs, not even "run now"
scripts/jobsearch schedule resume
scripts/jobsearch budget set --monthly 0                        # Claude: no paid claims at all
scripts/jobsearch budget set --apify-monthly 0                  # Apify: every LinkedIn-via-Apify search is skipped
docker compose stop n8n                                         # the hard stop: the scheduler itself
```

Per-stage settings are kept while paused. `doctor` shows a warning for as long as anything is paused, so a pause is never forgotten.

## Limits you set yourself

| Limit | Default | Change |
|---|---|---|
| Claude, per month / per day | $10 / $1.50 | `budget set --monthly N --daily N` |
| Apify, per month / per day | $15 / $0.50 | `budget set --apify-monthly N --apify-daily N` |
| tailoring per day | none | `tailor-settings set --daily-limit N` |
| searches per collect run | 18 | `schedule set collect --batch N` |
| company boards and feeds per collect run | 30 | `schedule set collect --boards N` |

The Apify daily default is the monthly amount divided by about 30, so the free credit lasts the whole month instead of being used up in the first two weeks. At the current rate (about 6 Apify searches per collect run, 4 runs a day, roughly $0.05 each) that is about $1.20 a day against the $0.50 cap, so the default lets about 10 of the 24 daily searches through (the rehearsal's "10 runs, $0.47") and skips the rest, which keeps the free credit lasting the month.

## What these limits do not cover

- **Calls made outside this project** with the same keys (a script, another tool). They are not in the log. The second layer is the limit you set in the **Anthropic Console** (see [budget.md](budget.md)) and the credit on each Apify account.
- **Apify reports usage late.** Right after a run the account balance may not have changed yet (seen on a real run), so a run is counted at the larger of its estimate and any measured change, or at 1.5 times its estimate when nothing was measured. The cap therefore trips a little early, never late. The per-account hard credit limit on Apify is the final backstop.
- **A model API that bills a request it then fails to deliver.** Counted as spend only if a reply arrived; the Console limit covers the rest.
