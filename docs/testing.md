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

```bash
docker compose run --rm --no-deps -T \
  -e ANTHROPIC_API_KEY=stub-not-a-real-key -e APIFY_TOKEN_CURIOUS=tok-a -e APIFY_TOKEN_CHEAP= -e APIFY_TOKEN_BEBITY= \
  -e TELEGRAM_BOT_TOKEN= -e TELEGRAM_CHAT_ID= \
  -e ANTHROPIC_API_URL=http://127.0.0.1:9/v1/messages -e APIFY_API_URL=http://127.0.0.1:9/v2 -e BOARDS_API_URL=http://127.0.0.1:9 \
  -v "$PWD/postgres:/postgres:ro" scraper python loop_rehearsal.py
```

The blanked keys and the dead default URLs (Claude, Apify and, through `BOARDS_API_URL`, every job-board and feed host) are deliberate: a bug cannot send a real request to any of those services from this container. It ends with `33 of 33 checks within their bounds` (exit code 0) or lists what failed. The results are summarised in [runaway-protection.md](runaway-protection.md#what-the-rehearsal-measured).

**Run it after any change** to scoring, tailoring, Apify, the scheduler, or the budget. **Add a scenario** for any new paid path (see `AGENTS.md`).

## Real API calls made so far (and why)

| What | Cost | Why a stand-in could not answer |
|---|---|---|
| a handful of scoring and tailoring runs during development | under $0.50 in total | the model's real behaviour (thinking defaults, output length, honesty-check quality) cannot be simulated |
| 1 Apify search, 5 results | about $0.01 | how Apify's balance reports cost right after a run: it had **not** changed yet and caught up minutes later, which is why a zero measurement counts as "unknown", not free |

Everything else (the schedules, the guards, the loops, the caps, the pause) was verified without spending.
