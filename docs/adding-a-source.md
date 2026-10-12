# Adding a job source

A source is anything that can produce a list of jobs. You only need to produce the normalised shape; dedupe, filtering, scoring and tailoring are shared.

## The normalised shape

```json
{
  "source": "greenhouse",
  "source_job_id": "12345",
  "url": "https://boards.greenhouse.io/acme/jobs/12345",
  "apply_url": "https://acme.com/careers/12345",
  "title": "Backend Engineer",
  "company": "Acme",
  "location": "Bengaluru, India",
  "is_remote": false,
  "posted_at": "2026-10-09T00:00:00Z",
  "description": "Full job description text",
  "salary_min": null,
  "salary_max": null,
  "salary_currency": null,
  "raw": {}
}
```

Required: `source`, `source_job_id`, `title`, `company`. `source_job_id` must be stable for the same listing across runs. `apply_url` should be the employer's direct link when you can get it.

Whatever the source, the jobs must go through the **ingest** step, which runs the scam/safety check, the dedupe, and your free filters. Do not write to the database directly: you would skip all three.

## Option A: n8n only (HTTP APIs)

For a one-off source with a JSON API (Adzuna, ...). For another company job-board system or remote feed, use option C instead:

1. In a workflow, add an **HTTP Request** node for the API.
2. Add a **Code** node mapping the response to the shape above.
3. Add an **HTTP Request** node that POSTs `{"jobs": [...]}` to `http://scraper:8000/jobs/ingest`. That is what workflow 01 does for the built-in sources; look at it for the exact wiring.

n8n holds no database credential in this project, so this is the only route in.

## Option B: scraping libraries (Python)

Add a module like `scraper/apify_source.py` and an endpoint in `scraper/app.py` that returns `{"jobs": [<shape above>], "errors": [...]}`, then call it from n8n with an **HTTP Request** node (`http://scraper:8000/...`) and pass the result to `/jobs/ingest`. If the source should take part in the rotating search plan, add its name to the planner (`scraper/planner.py`) and to `sources` in `search.json`. The name must also be registered in `scraper/sources.py` (it goes in `ALL`, here through the `JOBSPY`, `APIFY` or `OTHER_LEGACY` tuple, plus an entry in `DESCRIPTIONS`): `profile_tools.py` accepts only the names in `ALL` (its `KNOWN_SOURCES`), so an unregistered name is rejected as an unknown source. See Option C. Keep the `raw` payload so you can re-parse later without re-scraping.

## Option C: another company job-board system or remote feed

The ten built-in ones (see [sources.md](sources.md)) share one core, so a new one is a small adapter and a registration:

1. Register the name in `scraper/sources.py`: in `BOARD_SYSTEMS` (needs company slugs) or `FEEDS` (needs nothing), plus its entry in `DESCRIPTIONS`. That one list is what the planner, the settings validator (`profile_tools.py`), the CLI (`sources on|off|only`, the groups), the service (`app.py`) and `boards.py` all read, so the source becomes individually switchable for free. For a board system also add a probe address to `PROBE_URLS` in the same file: `scripts/jobsearch boards find` uses it to test whether a company slug exists.
2. In `scraper/boards.py`, add its fixed host to `_HOSTS` (and, for a feed, its minimum gap to `MIN_INTERVAL_S`: read the feed's terms), write `fetch_<name>(client, slug, req, now)` returning `(jobs, how many were listed)`, and register it in `ADAPTERS`. Every fetch must go through `boards.fetch`, which enforces the host allowlist, the size and time limits, the free relevance filter and the per-request cap, and never lets one failing source stop a run. Do not make requests of your own, and never fetch a URL that came back inside a response.
3. Teach `StubBoards` in `scraper/stubs.py` the new API's real response shape, and test the adapter against it: normal, empty, unknown company, rate limited, server error, garbage, changed layout, huge.
4. Document it in `docs/sources.md`.

## Checklist

- [ ] Stable `source_job_id`
- [ ] Failures are reported in `errors` (and HTTP 502 when nothing came back), never a silent empty list
- [ ] Unit tests for the mapper against a sample item (see `scraper/tests/`)
- [ ] Rate-limit friendly (stagger schedules; do not hammer a board)
- [ ] Documented in `docs/sources.md`
