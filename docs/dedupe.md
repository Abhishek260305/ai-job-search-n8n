# Cross-source dedupe

The same job often appears on several boards with different IDs and slightly different titles. `upsert_job()` collapses them into one canonical row in `jobs`, keeping every listing in `job_sources`. The live version of `upsert_job()` is the one in [migration 004](../postgres/migrations/004_dedupe_locations.sql) (its layer 3 is `same_posting()`); [`postgres/schema.sql`](../postgres/schema.sql) holds the older baseline, and migrations 001 and 004 replace the functions it defines.

## Matching layers (first hit wins)

1. **Exact.** The `(source, source_job_id)` pair has been seen before.
2. **Fingerprint.** `md5(norm_company | norm_title | norm_city)` matches an existing job.
3. **Same posting.** Same normalised company, `pg_trgm` `similarity(title) > 0.85`, and the locations agree: the same city key, **or** either location is *coarse* (a state code such as `MH`, a country, "Remote"), **or** the two descriptions are nearly identical (similarity above 0.8 on the first 1,500 characters).
4. **Description hash.** Same normalised company and an identical description hash (catches reposts under a new ID).

The location rule exists because boards describe the same place differently. On real data, Indeed returned `MH, India` where LinkedIn returned `Pune Division, Maharashtra, India`, and a remote role was posted once per city. Requiring the same city key missed those. Two **named, different** cities are merged only when the descriptions match, so the same title in two real offices with different descriptions stays two jobs.

## Normalisation

| Field | Rule |
|---|---|
| company | lowercase, drop legal suffixes (`pvt`, `private`, `ltd`, `limited`, `inc`, `llc`, `llp`, `corp`, `corporation`, `co`), strip non-alphanumerics |
| title | lowercase, drop `(parenthetical)` text, strip punctuation, collapse spaces |
| city | first comma-separated token of `location`, lowercased; generic words dropped (`Division`, `District`, `City`, `Area`, a leading `Greater`, a trailing compass word); then alphanumerics only. `Pune Division, Maharashtra`, `Pune City` and `Pune, Hyderabad, Bengaluru, India` all become `pune`. |

Example: `Sr. Software Engineer (Remote)` / `Acme Pvt. Ltd.` / `Pune, Maharashtra, India` and `Sr Software Engineer` / `ACME Private Limited` / `Pune, IN` produce the same fingerprint.

## What happens on a match

- `last_seen` is refreshed (and an `expired` job is revived to `new`).
- A longer description replaces a shorter one.
- A direct apply URL (usually the company's own ATS page) replaces a job-board link as `canonical_url`.
- Missing salary fields are filled in.
- The new listing is recorded in `job_sources`.

## Cleaning up existing duplicates

`SELECT merge_duplicate_jobs();` merges duplicates already stored (keeping the oldest row, moving every listing to it, keeping the longer description). Jobs that are `tailored`, `approved` or `applied` are never merged or deleted. Migration 004 ran it once.

## Known limits

- Different named cities are treated as different jobs unless the descriptions match closely, so a posting listed in two offices with different text stays two rows (both listings are still kept).
- A company that legitimately posts two near-identical titles in the same city may be merged by the fuzzy layer; tune the `0.85` threshold if that bites you.
- Legal-suffix stripping is deliberately conservative (it does not strip words like "technologies") to avoid merging different companies.
