---
description: Show or change which job sources are used (Indeed, LinkedIn, Apify, company job boards, remote feeds), and look up a company's job-board name.
argument-hint: [on|off|only NAMES... | find COMPANY]
---

# /sources

Every job source is one switch: its name in `sources` in `config/search.json`. Nothing new runs unless the user switches it on.

1. Run `scripts/jobsearch sources` and explain it in plain words: which sources are on, which are off, what each costs (only `linkedin_apify` uses money, from the Apify credit; everything else is free), and how many jobs each has brought. Apify's LinkedIn jobs are stored as `linkedin`, so they are counted on the `linkedin` line. For a company-board system, say how many companies it has (a board system runs only when it is on **and** has at least one company).
2. If `$ARGUMENTS` asks for a change, translate it to one command and run it:
   - `scripts/jobsearch sources on remotive greenhouse` (add), `sources off boards` (remove what is named), `sources only apify` (keep exactly what is named, switch everything else off). Names may be single sources or groups: `boards` (greenhouse, lever, ashby, workable, smartrecruiters), `feeds` (remotive, remoteok, weworkremotely, himalayas, arbeitnow), `jobspy` (indeed, naukri, linkedin), `apify` (linkedin_apify). `glassdoor`, `ziprecruiter` and `google` are in no group.
   - Ask before switching on `apify` (it uses Apify credit; the Apify caps from `scripts/jobsearch budget` still apply).
   - **If the command refuses because the dashboard edited the search profile** (it says so and names `search-profile pull`): stop and tell the user. Running `scripts/jobsearch search-profile pull` replaces `config/search.json` with the database's profile (the old file is kept as `config/search.json.before-pull`), so run it only after the user agrees. **Never pass `--force` without the user's explicit approval:** it discards what they changed in the dashboard.
   - It updates the database **and** `config/search.json` together (applying the whole file), keeps company names when a system is switched off, refuses to switch everything off, and takes effect on the next collect run with no restart.
3. If `$ARGUMENTS` is `find COMPANY` (or the user wants to add a company), run `scripts/jobsearch boards find "Company Name"`. It is free and read-only. It says which of the five systems the company uses and prints the line to add under `boards` in `config/search.json`. Have the user check it is the right company; then add the slug (read the file first, keep its other settings), run `scripts/jobsearch search-profile apply` (ask first: it changes the database), and make sure the system is on.
4. Explain what matters:
   - Company boards and feeds cost nothing to fetch; the only cost is scoring the new jobs they bring (about $0.0003 each, under the spending caps). A first run with many companies can add a few hundred jobs.
   - A free relevance filter runs first and is strict on purpose: a job title must contain a core word of one of the user's search terms (the optional `board_title_words` setting adds words that qualify on their own, for example `software`), the location must fit, and the posting must be recent.
   - Not covered: companies with their own career site or on Workday, iCIMS, Taleo or Oracle (Amazon, Google, Meta, Apple, Netflix, ...). Indeed and LinkedIn remain the way to see those roles.
   - How many boards and feeds one collect run visits: `scripts/jobsearch schedule set collect --boards N` (default 30).

Details: `docs/sources.md` and `docs/customization.md` (Choosing your sources).
