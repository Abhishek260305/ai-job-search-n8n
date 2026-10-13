# Testing: stand-in APIs first, real APIs only when needed

Real API calls cost money, and the tests that matter most are about failure: outages, garbage replies, hung runs, loops. So **everything is tested against stand-in APIs, and a real call is made only when no stand-in can answer the question.**

## The rules

1. **No test may reach the internet.** Every unit test runs behind a guard (`tests/conftest.py`) that fails any connection to anything but this machine, with a message pointing here. A bug that tried to call a real API would fail loudly instead of spending credits.
2. **Use a fake or a stub.** Fakes (an in-memory store, a scripted model) for unit tests; the stand-in servers in `scraper/stubs.py` when the real HTTP client code should run.
3. **A real call is justified only to check something a stand-in cannot know**, for example "what does Apify's real balance endpoint return right after a run?". Make it once, as small as possible (a 5-result search costs about a cent), say what it costs and why, and then turn what you learned into a stub behaviour and a test.
4. **Never run the paid pipeline to see if it works.** Run the rehearsal.

## Running the tests

```bash
scripts/test.sh                                      # scraper + CLI unit tests: no network, no cost
docker compose exec -T postgres sh /tests/run.sh     # SQL tests on throwaway databases
```

(`pip install -r scraper/requirements-dev.txt` first. The unit tests run on a laptop; the SQL tests and the rehearsal need the Postgres container. The dashboard's tests: [below](#dashboard).)

## The stand-in APIs (`scraper/stubs.py`)

```python
import stubs, anthropic_client, apify_source

with stubs.StubAnthropic(stubs.scoring_ok()) as ai, stubs.StubApify({"tok": 5.0}, {"tok": "ok"}, charge=0.047) as apify:
    anthropic_client.API_URL = ai.url        # or set ANTHROPIC_API_URL
    apify_source.API = apify.url             # or set APIFY_API_URL
    ...                                      # run the REAL code
    assert ai.billed == 3 and apify.starts == 1
```

| Stand-in | Behaviours |
|---|---|
| `StubAnthropic(handler)` | `scoring_ok()` a reply the real validator accepts; `always_status(529)` an outage; `garbage_reply()` a billed reply that is not JSON; `tailor_draft(patch, then_status=..., flawed=...)` a draft followed by a failing revision |
| `StubApify(balances, modes, charge)` | per account `ok`, `fail` (the run ends FAILED, partly billed), `hang` (never finishes), `start_error` (refused, free); a finished run lowers the balance |
| `StubBoards(data, modes)` | the ten company-board and remote-feed APIs of [sources.md](sources.md), served in each API's own response shape under `{url}/{system}/...`. Per company or feed a mode: `ok`, `empty`, `not_found` (404), `rate_limited` (429), `server_error` (500), `garbage` (HTTP 200 with a page that is not JSON), `huge` (valid JSON of over 33 MB, streamed), `slow` (answers after a delay), and `eu_only` for Lever. `bottomless` turns Himalayas or Arbeitnow into endless newest-first pages, to prove the page limit. Every request is logged, so a test can count them. It is free: it is just a local web server. |

All three count every request. **`billed`** (Claude and Apify) is the number of replies a real API would have charged for (HTTP 200); errors and refusals are free. `ANTHROPIC_API_URL`, `APIFY_API_URL` and `BOARDS_API_URL` (which replaces the eleven job-board and feed hosts at once (Lever's EU host included), and is what points the collector at `StubBoards`) exist only for testing; never set them in production.

## Tests for company boards and remote feeds

These run in `scripts/test.sh` with no network. `StubBoards` is built from the real response shapes of the ten services (checked on 2026-10-11), so the adapters are exercised against realistic answers, including the bad ones.

| Test file (`scraper/tests/`) | What it checks |
|---|---|
| `test_sources.py` | the source registry: names, groups, which sources are paid, slug validation |
| `test_relevance.py` | the free relevance filter: a passing and a failing case for every title, location and age rule, including look-alike titles |
| `test_boards_core.py` | the shared fetch code: the size limit, timeout, one retry for a server error, no retry for a 429, the fixed-host allowlist, no redirects, the 25-job cap, HTML to plain text |
| `test_boards_ats.py`, `test_boards_ats2.py` | the five company-board systems: normal, empty, unknown company, huge, slow, rate limited, server error, garbage, changed layout; detail calls only for jobs that already passed the filter |
| `test_boards_feeds.py`, `test_boards_paged.py` | the five feeds, including the 3-page and "stop at the first old job" rules for Himalayas and Arbeitnow |
| `test_app_boards.py` | the `/fetch/board` endpoint: validation, the minimum interval ("fetched recently"), failures recorded, an unexpected error never becoming a crashed run |
| `test_mail.py` | the optional Gmail part's foundations (Phase 6a): the two settings' rules, the report lines (never any email text or secret), the quiet scheduled stage. `test_app_features.py` and `test_premigration.py` cover its endpoints, the strict request model, and a scraper image that runs before migration 021 is applied |
| `test_gmail_client.py` | the Gmail client against the stand-in Google (`StubGmail` in `stubs.py`): the login and its failures (expired, revoked, wrong client, a token with more than read-only permission, redirects, rate limits, server errors, garbage, slow), only the two Google hosts, nothing logged, and the parser for an email (untrusted text, senders, Google's own authentication verdict and a forged one). `cli/tests/test_gmail_login.py` runs the whole sign-in with the browser simulated |
| `dashboard/api/tests/test_email_inbox.py` and the 6f tests in `test_store_pg.py` | the email inbox (Phase 6f): every route against a fake store (the query, each state, the body of an accept, the CSRF header, bad ids, every refusal as the right HTTP answer), the store against a fake connection (how it tells a missing suggestion from a missing job and a decided one from a job that moved), and, against a real Postgres, the shape of the answers, accepting through the board's own function and taking it back with Undo, a stale suggestion, the second look, and that the dashboard role cannot read or write the Gmail tables. Web: `pages/Email.test.tsx` (the page, plain-text rendering of an email's subject and excerpt, accept with Undo, the second-look checkbox, stale, 409 and 403 answers, dismiss and restore, the menu badge), `lib/email.test.ts`, the Settings and Health pages for the Gmail switch and the mail stage, and axe over the page |
| `test_mail_stage.py` | the scheduled `mail` stage (Phase 6e) with the search and the model step scripted: the order, the counts kept (numbers, flags and short reasons only), every login failure, a rate limit on the login check being an early run and not a login problem, early runs versus caps, the model step skipped without a key, the timeout budget, the client always closed |
| `test_email_html.py` | the text a person sees in an HTML email (Phase 6, `scraper/email_html.py`): about 70 hostile shapes whose hidden text must never be shown without `doubt` (`display:none` in every spelling, `hidden`, tiny or transparent text, off-screen and clipped boxes, scales, a class, id or tag hidden by a `<style>` block, self-closing non-void and raw-text tags, unbalanced and mismatched end tags, elements a browser re-opens or moves, a `<` in an attribute, unterminated comments and tags, CSS escapes, `var()`, entities in the style), about 25 sloppy-but-real shapes whose visible words must survive and raise no doubt (`line-height:0`, implicit `<p>`/`<li>` ends, an unclosed `<head>`, Outlook conditional comments, a font family with a CSS escape, a red `rgb()` colour), each rule pinned on its own, a time bound for each of nine hostile 200,000-character inputs, a fuzz run, and the Gmail parser's use of it. The parser was also compared with a real HTML5 parser (parse5) on about 130,000 random tag soups plus 150 hand-written cases during review: no leak without `doubt` |
| `test_mail_ai.py` | the Gmail model step (Phase 6d): a scripted model and the real Gmail client against the stand-in Gmail, with a fake store that follows migration 023. Every signal maps to its status in code; the model cannot name a status or exceed medium confidence; unusable answers (wrong shape, a quote that is not in the email, a reply cut off) are counted and the email is parked after three; an email carrying instructions is never sent; the application name is one clean line; the money caps, the per-run and per-day limits, refunds for failed calls, Gmail rate limits, an expired login, a rescan during a run, a refused suggestion; and the real Anthropic client against `StubAnthropic` (one billed call, an outage bills nothing) |
| `test_mail_match.py`, `test_mail_rules.py`, `test_mail_search.py` | the Gmail search (Phase 6c): the matcher (whole words, placeholders, ambiguity, look-alike and free-mail senders), the wording rules for every signal and their traps, the transition rules for every signal in every state, the flags (scam wording, links, text aimed at an AI), and the whole search against the stand-in Gmail: every outcome, idempotence, the cursor, the bounds, a hostile mailbox, deeper paging past seen mail, Gmail failing, and attack emails. The fake store refuses any call but its own records, which is how "it never changes an application's status" is checked |
| `test_planner.py` | the plan: a config with no boards or feeds produces exactly the old plan; a board system runs only when it is in `sources` and has slugs |
| `cli/tests/` | `sources` (on, off, only, groups, refusing to switch everything off), `boards find` (at most 20 requests), the doctor's source warnings, `schedule set collect --boards`, and that `config/search.example.json` stays valid |
| `postgres/tests/` | migration 013 and the fetch-politeness functions (run in the Postgres container: see "Running the tests" above) |

The runaway rehearsal (next section) was **not** extended for company boards: there are no rehearsal scenarios for them yet. The bounds listed in [runaway-protection.md](runaway-protection.md#company-boards-and-remote-feeds) are covered by the unit tests above.

## Dashboard

The optional [dashboard](dashboard.md) has its own two suites; `scripts/test.sh` does not run them. Neither makes a real call: the API tests use the same "no connection except this machine" guard (`dashboard/api/tests/conftest.py`), and the frontend tests answer every request from a stand-in API (MSW) that fails on any request it does not know.

```bash
# API: FastAPI, Python 3.12 (with uv; or pip install -r requirements-dev.txt and run pytest)
cd dashboard/api && uv run --no-project --python 3.12 --with-requirements requirements-dev.txt python -m pytest -q

# Frontend: React (Node 22 or later)
cd dashboard/web && npm ci && npm test && npm run typecheck && npm run lint && npm run build
```

`npm run build` writes `dashboard/web/dist/`, which the container serves; the image builds it the same way.

**The 21 tests against a real Postgres** (`dashboard/api/tests/test_store_pg.py`) are skipped unless you point them at a throwaway server. They run every query and route as the real `jobsearch_dashboard` role, and check that it cannot read a table directly. They wipe and re-seed `jobs`, so they refuse any database whose name does not contain `test`. To run them, on any throwaway Postgres 16:

1. create a database such as `jobsearch_test`, apply `postgres/schema.sql`, then every file in `postgres/migrations/` in order (`postgres/migrate.sh` does this inside a Postgres container);
2. give the role a login on that server only: `ALTER ROLE jobsearch_dashboard LOGIN PASSWORD '...'`;
3. run the API command above with `DASHBOARD_TEST_ADMIN_DSN` (the owner role) and `DASHBOARD_TEST_DSN` (the dashboard role) set to `postgresql://...@127.0.0.1:PORT/jobsearch_test` connection strings.

**The settings page (version 2b)** is tested against a fake scraper client (every route, every refusal, the lower-only cap rule, an unreachable scraper) and the client itself against a fake opener (fixed paths, every failure kind). `tests/test_settings_service.py` reads `scraper/schedules.py` and `scraper/app.py` and fails when the dashboard's copy of the limits, or a field the page can send, drifts from them. The real thing was also run once end to end: the real scraper service and the dashboard over a local database copy.

**The Search page (version 2c)** is tested at four levels: the scraper's endpoints (validation, a stale version, the board probe), the CLI (the guard, `pull`, the doctor note), the dashboard API (every shape and refusal) and the page. `tests/test_settings_service.py` fails if the profile fields the page sends drift from `scraper/profile_tools.PROFILE_KEYS`. It was also run once for real: the real scraper and dashboard over a database copy, a live company lookup, and the CLI's own SQL and `pull` against that database.

**SQL:** migrations 014, 016, 017, 018, 019 and 020 (the views, the history trigger, the board and stats, the functions, every alert rule, pause and resume, and the role's grants) are covered in `postgres/tests/test_migrations.sql`, run by `docker compose exec -T postgres sh /tests/run.sh` like the rest.

**The scripts** `scripts/init-env.sh`, `scripts/tunnel.sh`, `scripts/check-secrets.sh`, `scripts/backup.sh` and `dashboard/db-init.sh` are tested in `cli/tests/test_scripts.py` (part of `scripts/test.sh`); `SCRIPTS_SH=dash` runs them under `dash` as well, to catch shell features `/bin/sh` may lack.

**The docs themselves** are tested in `cli/tests/test_docs.py` (also part of `scripts/test.sh`): every relative link and anchor must point at something that exists, every `scripts/jobsearch <command>` and `scripts/*.sh` the docs name must exist, and the files an AI assistant is sent to must be there. A renamed heading or command fails the test instead of leaving a dead link.

## The runaway rehearsal

`scraper/loop_rehearsal.py` runs the **real** service code and the **real** SQL against a throwaway database (`jobsearch_loop_test`, dropped at the end; the real `jobsearch` database is never touched) with the stand-ins misbehaving, and checks that calls and dollars stay inside a hard bound. It makes no real call and costs nothing:

It includes tailoring **on request** (the dashboard's button): a storm of 500 presses in 25 seconds must start one run, and a press every two minutes all day must stay under the daily cap. The unit tests for that path are `scraper/tests/test_ondemand.py` (the run registry: one at a time, the cooldown, nothing leaks) and `scraper/tests/test_app_ondemand.py` (the estimate, every refusal, the agreed cost); the dashboard's are `dashboard/api/tests/test_tailor_page.py` and `dashboard/web/src/components/board/tailor.test.tsx`.

```bash
docker compose run --rm --no-deps -T \
  -e ANTHROPIC_API_KEY=stub-not-a-real-key -e APIFY_TOKEN_CURIOUS=tok-a -e APIFY_TOKEN_CHEAP= -e APIFY_TOKEN_BEBITY= \
  -e TELEGRAM_BOT_TOKEN= -e TELEGRAM_CHAT_ID= \
  -e ANTHROPIC_API_URL=http://127.0.0.1:9/v1/messages -e APIFY_API_URL=http://127.0.0.1:9/v2 -e BOARDS_API_URL=http://127.0.0.1:9 \
  -v "$PWD/postgres:/postgres:ro" scraper python loop_rehearsal.py
```

The blanked keys and the dead default URLs (Claude, Apify and, through `BOARDS_API_URL`, every job-board and feed host) are deliberate: a bug cannot send a real request to any of those services from this container. It ends with `55 of 55 checks within their bounds` (exit code 0) or lists what failed. The results are summarised in [runaway-protection.md](runaway-protection.md#what-the-rehearsal-measured).

**Run it after any change** to scoring, tailoring, Apify, the scheduler, or the budget. **Add a scenario** for any new paid path (see `AGENTS.md`).

## Real API calls made so far (and why)

| What | Cost | Why a stand-in could not answer |
|---|---|---|
| a handful of scoring and tailoring runs during development | under $0.50 in total | the model's real behaviour (thinking defaults, output length, honesty-check quality) cannot be simulated |
| 1 Apify search, 5 results | about $0.01 | how Apify's balance reports cost right after a run: it had **not** changed yet and caught up minutes later, which is why a zero measurement counts as "unknown", not free |

Everything else (the schedules, the guards, the loops, the caps, the pause) was verified without spending.
