# Job sources

Every source is an adapter that returns the normalised job shape (see [architecture.md](architecture.md)). Use several and let dedupe merge them.

**Every source is one switch:** its name in `sources` in `config/search.json`. Switch sources on and off with `scripts/jobsearch sources` ([how](customization.md#choosing-your-sources)). Nothing new runs unless you switch it on, and a config written before company boards and remote feeds existed behaves exactly as it did (once the database is up to date: run `scripts/jobsearch migrate` first, then rebuild the scraper). Groups such as `boards` and `feeds` are shorthand for the `sources` command; `config/search.json` itself lists single names only, and a group name there is rejected by validation.

## Status of sources

| Source | Name in `sources` | Method | Cost | Notes |
|---|---|---|---|---|
| Indeed | `indeed` | JobSpy | free | ✅ verified from a cloud VM |
| Naukri | `naukri` | JobSpy | free | ✅ verified from a cloud VM |
| LinkedIn | `linkedin` | JobSpy | free | ✅ worked on 4 consecutive test runs from a cloud VM; ⚠️ one earlier run got HTTP 429 (see below) |
| LinkedIn | `linkedin_apify` | Apify actors | pay-per-result, about $0.70-$1.00 per 1,000 jobs | ✅ all three actors verified against real accounts (see below) |
| Google Jobs | `google` | JobSpy | free | 🚧 to verify |
| Greenhouse, Lever, Ashby, Workable, SmartRecruiters | `greenhouse` `lever` `ashby` `workable` `smartrecruiters` | the public job-board JSON APIs; you list company slugs | free | ✅ built ([company job boards](#company-job-boards)); tested against stand-ins built from the real response shapes (checked 2026-10-11) |
| Remotive, RemoteOK, We Work Remotely, Himalayas, Arbeitnow | `remotive` `remoteok` `weworkremotely` `himalayas` `arbeitnow` | public JSON and RSS feeds | free | ✅ built ([remote feeds](#remote-job-feeds)); remote-focused, nothing to configure |
| Adzuna, Jooble | | free API key | free | 🚧 planned |

Verify each project's current README before relying on it; scrapers break often.

`glassdoor` and `ziprecruiter` (JobSpy) are also accepted names, but they are not in any group, so `sources off jobspy` does not touch them.

## Company job boards

Many companies publish their openings on a hosted job board (an "applicant tracking system"), and five of those systems offer a public, key-less JSON listing of a company's jobs. You give the name of each company's board (its **slug**); the collector reads that board, keeps what is relevant to you, and passes it to the same safety check, dedupe, filters and scoring as every other job.

| Name in `sources` | System | The call (one line) |
|---|---|---|
| `greenhouse` | Greenhouse | `GET boards-api.greenhouse.io/v1/boards/<slug>/jobs`, then `GET .../jobs/<id>` for each job that passed the relevance filter (the listing has no descriptions) |
| `lever` | Lever | `GET api.lever.co/v0/postings/<slug>?mode=json` (if the company is not there, one try on `api.eu.lever.co`, where EU-hosted companies live) |
| `ashby` | Ashby | `GET api.ashbyhq.com/posting-api/job-board/<slug>` (one response, descriptions included; a big company can be about 14 MB) |
| `workable` | Workable | `POST apply.workable.com/api/v3/accounts/<slug>/jobs` for the listing, then `GET apply.workable.com/api/v2/accounts/<slug>/jobs/<shortcode>` for each job that passed |
| `smartrecruiters` | SmartRecruiters | `GET api.smartrecruiters.com/v1/companies/<slug>/postings?limit=100&offset=...` (at most 3 pages, filtered on the server by your search words and, for a one-country user, the country), then `GET .../postings/<id>` for each job that passed |

A board system runs only when **both** are true: its name is in `sources`, **and** it has at least one company slug under `boards`. Switching a system off keeps your slugs, so switching it on again is instant.

**Terms and limits.** These are the endpoints the companies switch on for exactly this purpose, and they need no key. Still: one company board is fetched at most every **10 minutes** (enforced in code, see [polite fetching](#polite-fetching-and-attribution)), a response is read only up to 32 MB, and Workable's and SmartRecruiters' detail calls are not part of any published promise, so they can change without notice (the collector reports a changed answer as an error for that company and carries on).

### Finding a company's slug

The slug is the company's name inside its job-board address. Open the company's careers page, click a job, and read the address:

| System | Address | Slug |
|---|---|---|
| Greenhouse | `boards.greenhouse.io/<slug>` or `job-boards.greenhouse.io/<slug>` | `<slug>` |
| Lever | `jobs.lever.co/<slug>` | `<slug>` |
| Ashby | `jobs.ashbyhq.com/<slug>` | `<slug>` |
| Workable | `apply.workable.com/<slug>` | `<slug>` |
| SmartRecruiters | `jobs.smartrecruiters.com/<Slug>` or `careers.smartrecruiters.com/<Slug>` | `<Slug>`: **case-sensitive** |

Or let the tool look it up (free, read-only, at most 20 requests):

```bash
scripts/jobsearch boards find "Stripe"
```

It tries a few likely slugs on all five systems, tells you which ones answer like a real board and how many jobs they list, and prints the line to add. Check that it is the right company before you add it (a short name can belong to someone else). `boards find` does not look on Lever's EU site (`api.eu.lever.co`): a company hosted there is read fine by the collector, but `boards find` may report "No match" for it. If you know the company uses Lever's EU site, add its slug under `lever` by hand.

### Adding companies

Edit `config/search.json` (or let `/setup` do it), then apply it:

```json
"sources": ["indeed", "naukri", "linkedin", "greenhouse", "lever", "ashby"],
"boards": {
  "greenhouse": ["cloudflare", "stripe"],
  "lever": ["spotify"],
  "ashby": [{"slug": "openai", "name": "OpenAI"}]
},
"board_title_words": []
```

```bash
scripts/jobsearch search-profile apply
```

If you have since edited the profile on the dashboard's Search page, `search-profile apply` refuses until you run `scripts/jobsearch search-profile pull` (see [Known limits](#known-limits)).

A slug is a plain string, or `{"slug": "...", "name": "..."}` when you want a display name that matches how the same company appears on other boards (so duplicates merge). A slug uses letters, digits, `.`, `_` and `-` (at most 64 characters, not starting with a symbol); you can list at most 300 in total. A bad entry is rejected by `scripts/jobsearch validate` with a message saying which one.

### What is not covered

Companies with **their own career site**, or on **Workday, iCIMS, Taleo or Oracle**, have no open listing these five systems can read. That includes Amazon, Google, Meta, Apple and Netflix (checked 2026-10-11: none has a board on a supported system). Indeed and LinkedIn searches remain the way to see their roles.

## Remote job feeds

Five public feeds list remote jobs. They need no key, no slugs and no company list: switch the name on and the collector reads the feed.

| Name in `sources` | Feed | The call (one line) | Terms, and what this project does |
|---|---|---|---|
| `remotive` | Remotive | `GET remotive.com/api/remote-jobs?category=software-dev` | Asks for **at most 4 fetches a day** and a link back naming Remotive as the source. Fetched at most every **6 hours**; the link stays Remotive's. Software-development category only. |
| `remoteok` | Remote OK | `GET remoteok.com/api` | Asks for a link back naming Remote OK. Fetched at most hourly. About 100 jobs per fetch. |
| `weworkremotely` | We Work Remotely | `GET weworkremotely.com/categories/remote-programming-jobs.rss` | Programming feed (RSS). Fetched at most hourly. |
| `himalayas` | Himalayas | `GET himalayas.app/jobs/api?limit=20`, then the next page by cursor, at most 3 pages | Lists very many jobs, newest first: only the **newest 60** are read per fetch (see below). Fetched at most hourly. |
| `arbeitnow` | Arbeitnow | `GET www.arbeitnow.com/api/job-board-api?page=N`, at most 3 pages | Asks users not to abuse it and to link back. Fetched at most hourly. **Germany-heavy**: for anyone not looking in Germany the location filter drops most of it. |

Things to know:

- **Mostly remote.** Remotive, Remote OK, We Work Remotely and Himalayas list remote jobs only. Arbeitnow is a general feed, mostly German jobs, remote or not (it passes its own `remote` flag through). The location rule applies to all of them: a job whose location names one of your places (for example "Remote, India" for a user in India) is kept whatever else it says; otherwise a remote job is kept when `allow_remote` is on in your search settings and its location is generic ("Remote", "Worldwide"); a remote job restricted to somewhere else (for example "Remote - US" for a user in India) is dropped.
- **Himalayas covers the newest 60 jobs per fetch.** It lists over a hundred thousand jobs, so the collector reads 3 pages of 20 and stops as soon as a posting is older than your `max_age_days`. On a very busy day a job that was posted and buried between two fetches is never seen.
- **Arbeitnow** serves hundreds of jobs a page; at most 3 pages are read, newest first, stopping early at the first job that is too old.

## How the jobs are filtered before anything is scored

A company lists *everything* it has, and a feed lists everything it has, so deciding what is worth sending on is done **first, for free, with plain rules** (no AI). Only jobs that pass reach the safety check, dedupe, your usual filters and, last, paid scoring (the exact order is below).

1. **Title.** Each of your search terms is reduced to its core words by dropping generic ones (`engineer`, `developer`, `software`, `senior`, and similar). A title passes when, for at least one of your terms, all of its core words appear as whole words. Hyphens and spaces are treated alike (`full-stack` matches `full stack`). Words you list under `board_title_words` pass on their own.
2. **Location.** The job's location must mention one of your places, or the job must be remote (with `allow_remote` on) with a generic location ("Remote", "Worldwide", "Anywhere") or one of your places. Ambiguous locations are kept; a location that clearly names somewhere else is dropped. For a user based in India, bare Indian cities and states ("Pune", "Noida, UP") and "APAC" or "Asia" count as a match. For another country a bare city without the country name ("Austin, TX" for a US user) is a **known limitation**: it is not recognised, so the job is dropped; boards usually write "City, Country".
3. **Age.** Older than `max_age_days` (when the posting has a date) is dropped.
4. **At most 25 jobs per company or feed per fetch**, newest first. Steps 1 to 4 all happen inside the fetch, before anything is handed on.

Only then do the jobs reach ingest, where **your existing filters** run as for every source: excluded words, included keywords, years of experience and excluded companies, after the safety check and dedupe.

Because each fetch returns only the **newest 25 relevant jobs** per company or feed, a company with, say, 60 matching openings does not show the older 35 until newer ones are taken down from the company's board.

**This is strict on purpose**, because every job that gets through costs a little to score. The consequence: with the search term `backend engineer`, a posting titled "Software Engineer, Infrastructure" does **not** match, because `backend` is not in the title. If you would accept roles like that, add the word to `board_title_words`:

```json
"board_title_words": ["software", "platform"]
```

What each fetch dropped (title, location, age) is counted in the `meta` of the reply, which you can read in the **Scrape** node of the collect workflow's execution in n8n, so you can see when a filter is cutting too much.

## Polite fetching and attribution

- **Minimum gaps, enforced in code and remembered in the database** (not left to the schedule): Remotive at most every **6 hours** (its terms ask for at most 4 a day); RemoteOK, We Work Remotely, Himalayas and Arbeitnow at most **hourly**; a single company board at most every **10 minutes**. A request inside the gap returns zero jobs and `"skipped": "fetched recently"` in the reply's `meta`, not an error. This holds even if you shorten the collect schedule or press "run now" repeatedly.
- **Each fetch sends an honest `User-Agent`**, follows no redirect, and stops that source for the run on an HTTP 429 (a rate limit is never retried).
- **Attribution.** Each job keeps its **original link on the source site**, and the source name is stored with the job. The jobs are used only for your own private search and are never re-published anywhere.

## Safety of the fetching itself

- Only **fixed hosts** are ever contacted: the five job-board APIs, Lever's EU host for EU-hosted companies, and the five feeds (eleven in all). A URL is a fixed host plus a validated slug; nothing in a company's settings or in a posting can change the host, and links inside postings are never fetched.
- Company slugs are validated before use.
- A response is read through a **32 MB** limit, a **30 second** timeout for each wait and a **one minute** limit for the whole request; a server error or timeout is retried **once**, a 429 never (a request that runs over the one-minute limit is skipped for this run, not retried).
- A company or feed that fails is reported in the run's `errors` and skipped; it never stops the run, and an answer that is not the expected format (an error page, a changed layout) becomes one clear error line, not a crash.
- `scripts/jobsearch doctor` warns about a company or feed that has failed 5 runs in a row (the dashboard's Health alert fires after 3), and about slugs listed for a switched-off system or a switched-on system with none ([troubleshooting](troubleshooting.md#company-boards-and-remote-feeds)).
- Posting text is untrusted data, as for every source: the injection defence runs on it at ingest.

## Known limits

- A slow source can hold one request up to a minute (the whole-request limit); it never costs money.
- You can also change the sources, the companies and every other search setting in the dashboard (the **Search** page). That writes the database, not your file: `scripts/jobsearch search-profile pull` brings `config/search.json` up to date, and `doctor` warns while the file is behind. The commands that write the file's profile refuse to overwrite a dashboard edit until you pull.
- The `sources` check of `scripts/jobsearch doctor` reads `config/search.json` on the machine where you run the command, not the database. After hand-editing that file, run `scripts/jobsearch search-profile apply` so the database matches it.

## Counting jobs per source

`scripts/jobsearch sources` shows how many jobs each source has brought. Apify's LinkedIn results are stored under the same source name as the JobSpy LinkedIn scraper (`linkedin`), so the report counts them together on the `linkedin` line.

## LinkedIn via JobSpy: works, but is rate-limited

JobSpy reads LinkedIn's logged-out job pages. In testing from an Oracle Cloud VM the first request got `429 Too Many Requests`, and four later runs (10 jobs each) succeeded. LinkedIn throttles bursts, and datacenter IPs are watched more closely than home ones, so expect occasional 429s. Treat LinkedIn as best-effort:

- Keep volumes small and stagger schedules (a few runs a day, not a burst).
- The scraper serialises requests (one scrape at a time).
- When a board is throttled the API reports it in `errors` (or HTTP 502 if nothing came back) instead of silently returning nothing.
- Use the Apify adapter below as a fallback, or run LinkedIn from a home machine.

Do **not** supply your own LinkedIn login to a scraper.

## LinkedIn via Apify actors

`POST /search/linkedin` on the scraper service. Three actors are supported; each Apify account (token) is paired with one actor:

| Env var | Actor | Price (free tier, from the Store page) |
|---|---|---|
| `APIFY_TOKEN_CURIOUS` | `curious_coder/linkedin-jobs-scraper` | about $1.00 / 1,000 results |
| `APIFY_TOKEN_CHEAP` | `cheap_scraper/linkedin-job-scraper` | $0.70 / 1,000 + $0.02 per run; minimum `maxItems` is 150 |
| `APIFY_TOKEN_BEBITY` | `bebity/linkedin-jobs-scraper` | about $1.00 / 1,000; company enrichment is an extra paid add-on (left off) |

Leave a token blank to disable that account. For each search the adapter:

1. reads each account's remaining monthly credit from Apify,
2. drops accounts that cannot afford the run and tries the richest first (unknown balances go last, rotated by day),
3. starts the actor with a hard result cap and a `maxTotalChargeUsd` limit,
4. falls through to the next account if a run fails,
5. maps the actor's own output into the shared job shape (keeping the raw item).

The response includes `meta` (which account ran, items returned/mapped, balance before, estimated cost) so you can see where credit goes.

**Verified (2026-10-11, one small run per actor, query "software engineer" / Bangalore):**

| Actor | Result | Notes |
|---|---|---|
| curious | 4 of 5 returned, 4 mapped | full descriptions; `applyUrl` is empty (Easy Apply) so the LinkedIn link is the only URL |
| bebity | 5 returned, 5 mapped | full descriptions; some salary data (`salary_min/max`); `applyUrl` present on about 3 of 5 |
| cheap | 150 returned, 150 mapped | `maxItems` has a minimum of 150, so every run costs about $0.12 at the list price (measured: about $0.076, below); all 150 had descriptions and a direct apply URL |

- Measured spend: curious about $0.008 for 4 jobs, bebity about $0.015 for 10 rows over two runs, cheap about $0.076 for 150 jobs. Small runs cost more per job than the list price because each run has a fixed cost, so prefer fewer, larger runs. The adapter assumes about $0.01 fixed per run for curious and bebity when setting its max-charge cap.
- The balance endpoint (`/v2/users/me/limits`) matched the shape the adapter expects.
- The cheap actor rejects any `publishedAt` other than `""`, `r86400`, `r604800`, `r2592000` (HTTP 400, not billed); the adapter rounds `hours_old` up to the nearest of these. The pipeline's `max_age_days` filter trims the rest.
- Because of that 150-result minimum, the cheap actor suits one big daily sweep, while curious and bebity suit small targeted runs.
- Actors change their schemas over time. The mappers use fallback field names and skip unusable items, and the balance check degrades to "unknown" instead of failing. Watch `items_returned` vs `items_mapped` in `meta`.

Tests (no tokens needed): `cd scraper && pip install -r requirements-dev.txt && pytest`.

## Failure handling

A blocked or failing source must not stop the run. Each source call is independent: the scraper API returns `{"jobs": [...], "errors": [...]}`, and HTTP 502 with `errors` when nothing came back. The n8n workflow should log errors, continue with the other sources, and surface a summary in the run notification.

The same holds for company boards and remote feeds: one company or feed that fails (wrong slug, a service that is down, a rate limit, a changed answer format) is one line in `errors` for that request, and the rest of the run carries on. A source that keeps failing is shown by `scripts/jobsearch doctor`.
