"""The source registry: every place a job can come from, the groups people can type, and which cost money.

One list that the collector, the schedules, the CLI and the search settings all import, so a new source is
added in one place. Pure data and one pure function, no I/O, stdlib only.

  jobspy    scraped through JobSpy (no key, no cost)
  apify     paid LinkedIn scraper (uses your Apify credit)
  boards    company job boards (Greenhouse, Lever, Ashby, Workable, SmartRecruiters); each needs company slugs
  feeds     public remote-job feeds; need nothing
"""
from __future__ import annotations

import re
from typing import Any

JOBSPY = ("indeed", "naukri", "linkedin")
APIFY = ("linkedin_apify",)
OTHER_LEGACY = ("glassdoor", "ziprecruiter", "google")          # accepted before Phase 7, not in any group
BOARD_SYSTEMS = ("greenhouse", "lever", "ashby", "workable", "smartrecruiters")
FEEDS = ("remotive", "remoteok", "weworkremotely", "himalayas", "arbeitnow")

ALL: tuple[str, ...] = JOBSPY + APIFY + OTHER_LEGACY + BOARD_SYSTEMS + FEEDS

GROUPS: dict[str, tuple[str, ...]] = {
    "jobspy": JOBSPY,
    "apify": APIFY,
    "boards": BOARD_SYSTEMS,
    "feeds": FEEDS,
}

# A company slug on a board system: ASCII letters, digits, '.', '_' and '-', not starting with a symbol, at most 64
# characters. It lives here (not in boards.py, which needs httpx) so the settings validator and the CLI can use it.
SLUG_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
_SLUG = re.compile(SLUG_PATTERN)

PAID: frozenset[str] = frozenset({"linkedin_apify"})

DESCRIPTIONS: dict[str, str] = {
    "indeed": "Indeed job search through JobSpy; free, no key needed.",
    "naukri": "Naukri job search through JobSpy; free, no key needed.",
    "linkedin": "LinkedIn job search through JobSpy; free, no key needed, but it can be rate limited.",
    "linkedin_apify": "LinkedIn job search through Apify; it uses your Apify credit.",
    "glassdoor": "Glassdoor job search through JobSpy; free, no key needed.",
    "ziprecruiter": "ZipRecruiter job search through JobSpy; free, no key needed.",
    "google": "Google Jobs search through JobSpy; free, no key needed.",
    "greenhouse": "Jobs from company boards hosted on Greenhouse; needs the company slugs you list.",
    "lever": "Jobs from company boards hosted on Lever; needs the company slugs you list.",
    "ashby": "Jobs from company boards hosted on Ashby; needs the company slugs you list.",
    "workable": "Jobs from company boards hosted on Workable; needs the company slugs you list.",
    "smartrecruiters": "Jobs from company boards hosted on SmartRecruiters; needs the company slugs you list.",
    "remotive": "Remotive's public remote-jobs feed; needs nothing.",
    "remoteok": "Remote OK's public remote-jobs feed; needs nothing.",
    "weworkremotely": "We Work Remotely's public remote-jobs feed; needs nothing.",
    "himalayas": "Himalayas' public remote-jobs feed; needs nothing.",
    "arbeitnow": "Arbeitnow's public jobs feed; needs nothing.",
}

# One GET URL per board system with a {slug} placeholder; `boards find` uses it to test whether a company
# slug exists. Not always the call the collector makes (Workable's real list call is a POST).
PROBE_URLS: dict[str, str] = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
    "lever": "https://api.lever.co/v0/postings/{slug}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{slug}",
    "workable": "https://apply.workable.com/api/v1/widget/accounts/{slug}",
    "smartrecruiters": "https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=1",
}


def expand(names: list[str]) -> list[str]:
    """Group names become their members; order is kept and duplicates dropped. Unknown name -> ValueError."""
    out: list[str] = []
    for name in names:
        if name in GROUPS:
            members: tuple[str, ...] = GROUPS[name]
        elif name in ALL:
            members = (name,)
        else:
            raise ValueError(
                f"unknown source '{name}'; valid sources: {', '.join(ALL)}; valid groups: {', '.join(GROUPS)}")
        for member in members:
            if member not in out:
                out.append(member)
    return out


def valid_slug(slug: Any) -> bool:
    """True for a string that is a safe company slug (see SLUG_PATTERN); anything else, including a non-string, is False."""
    return isinstance(slug, str) and _SLUG.fullmatch(slug) is not None
