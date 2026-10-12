"""Which job board does a company use? Probe each candidate company slug on each board system (read-only GETs).

Used by `scripts/jobsearch boards find` and, from dashboard version 2c, by the scraper's /boards/find, so the
dashboard's "find this company" and the command give the same answer. Stdlib only. The probe hosts are the fixed
templates in sources.PROBE_URLS and a slug must match sources.valid_slug before it is put into one, so a name can never
steer a request to another host; a redirect is never followed.
"""
from __future__ import annotations

import json
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import sources

PROBE_USER_AGENT = "ai-job-search-n8n/1.0 (personal job search)"
PROBE_TIMEOUT = 10
PROBE_MAX_BYTES = 32 * 1024 * 1024                              # the same response-size limit the collector uses


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: a response must not be able to send this tool somewhere else. The 3xx becomes an HTTPError."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _probe_open(req, timeout=PROBE_TIMEOUT):
    return urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout)


def slug_candidates(name: str) -> list[str]:
    """Likely board slugs for a company name, at most 4, unique, each a valid slug: joined lower-case (`stripeinc`),
    hyphenated (`stripe-inc`), the first word alone (`stripe`), and the joined original case (`StripeInc`; SmartRecruiters
    slugs are case-sensitive). Raises ValueError when nothing usable can be made from the name."""
    words = [w for w in (re.sub(r"[^A-Za-z0-9._-]", "", w) for w in str(name).split()) if w]
    if not words:
        raise ValueError("give a company name, for example: boards find Stripe")
    raw = ["".join(words).lower(), "-".join(words).lower(), words[0].replace("_", "").lower(), "".join(words)]
    out: list[str] = []
    for slug in raw:
        slug = slug.strip("._,")                         # "Stripe, Inc." must not make `stripeinc.`
        if slug and sources.valid_slug(slug) and slug not in out:
            out.append(slug)
    if not out:
        raise ValueError("that company name does not make a usable slug: give a shorter name, for example: boards find Stripe")
    return out[:4]


def _probe_jobs(system: str, body) -> Optional[int]:
    """How many jobs a probe response shows, or None when it is not the shape this system's list has."""
    if system == "lever":
        return len(body) if isinstance(body, list) else None
    if not isinstance(body, dict):
        return None
    if system == "smartrecruiters":
        n = body.get("totalFound")
        # SmartRecruiters answers 200 with totalFound 0 for any slug, so zero cannot tell a real company from a typo
        return n if isinstance(n, int) and not isinstance(n, bool) and n > 0 else None
    jobs = body.get("jobs")
    return len(jobs) if isinstance(jobs, list) else None


def _probe_one(system: str, slug: str, opener, timeout: float) -> Optional[dict]:
    """One probe: the match, or None for anything that is not a real board (a dead host, a 404, a timeout, bad JSON)."""
    req = urllib.request.Request(sources.PROBE_URLS[system].format(slug=slug),
                                 headers={"User-Agent": PROBE_USER_AGENT, "Accept": "application/json"})
    try:
        resp = opener(req, timeout=timeout)
        try:
            code = getattr(resp, "status", None)
            if code is None and hasattr(resp, "getcode"):
                code = resp.getcode()
            if code not in (None, 200):
                return None
            jobs = _probe_jobs(system, json.loads(resp.read(PROBE_MAX_BYTES)))
        finally:
            close = getattr(resp, "close", None)
            if close:
                close()
    except Exception:
        return None
    return None if jobs is None else {"system": system, "slug": slug, "jobs": jobs}


def boards_find(name: str, opener=None, *, timeout: float = PROBE_TIMEOUT, workers: int = 1) -> list[dict]:
    """Probe each candidate slug on each board system (GET, read-only; at most 4 x 5 = 20 requests) and return
    [{"system", "slug", "jobs"}] for every one that answers like a real board. Any error or non-200 means no match.
    Only SmartRecruiters slugs are case-sensitive, so a candidate that is not all lower-case is probed there only; and a
    slug is never reported twice for the other systems (they ignore case). The result is in candidate order whatever the
    number of `workers` (the command probes one at a time, the dashboard in parallel to answer within seconds)."""
    opener = opener or _probe_open
    plan: list[tuple[str, str]] = []
    seen: set = set()
    for slug in slug_candidates(name):
        for system in sources.PROBE_URLS:
            if system != "smartrecruiters" and slug != slug.lower():
                continue
            key = (system, slug if system == "smartrecruiters" else slug.lower())
            if key not in seen:
                seen.add(key)
                plan.append((system, slug))
    if workers <= 1:
        results = [_probe_one(system, slug, opener, timeout) for system, slug in plan]
    else:
        with ThreadPoolExecutor(max_workers=min(workers, len(plan) or 1)) as pool:
            results = list(pool.map(lambda job: _probe_one(job[0], job[1], opener, timeout), plan))
    return [r for r in results if r is not None]
