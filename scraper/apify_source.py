"""LinkedIn jobs via Apify actors, with credit-aware account selection.

Each Apify account (token) is paired with one actor. For every search we:
  1. ask Apify how much monthly credit each account has left,
  2. drop accounts that cannot afford the run, richest account first,
  3. run the actor (hard result cap + max charge), falling through to the next
     account if a run fails,
  4. map the actor's own output to the shared normalised job shape.

Tokens come from env vars (see ACTORS[*].token_env) and are only ever sent in an
Authorization header, never put in a URL or logged.

NOTE: actor input/output field names come from each actor's public Store page and can
change. Mappers use fallback chains and skip unusable items; confirm against a real run
(docs/sources.md).
"""
from __future__ import annotations

import logging
import math
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Mapping, Optional
from urllib.parse import urlencode

import httpx

log = logging.getLogger("scraper.apify")

# APIFY_API_URL points the adapter at a stand-in server (scraper/stubs.py) for tests and rehearsals. Never set it in production.
API = os.environ.get("APIFY_API_URL", "https://api.apify.com/v2")
TERMINAL = {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"}
CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "₹": "INR"}


class ApifyError(Exception):
    """`started` is True when an actor run was actually created before the failure (it may have been billed);
    a rejected start costs nothing. `attempts` (set on the final error) lists the runs that started."""

    def __init__(self, message: str, started: bool = False):
        super().__init__(message)
        self.started = started
        self.attempts: list[dict] = []


# A search tries at most this many accounts. Each attempt can bill, so this bounds the cost of one search
# (the pipeline reserves this many runs' worth of budget before it starts).
MAX_ATTEMPTS = 2


@dataclass
class SearchParams:
    search_term: str
    location: str
    results_wanted: int = 25
    hours_old: int = 72
    is_remote: Optional[bool] = None
    exclude_keywords: list[str] = field(default_factory=list)
    exclude_companies: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- input builders

def linkedin_search_url(p: SearchParams) -> str:
    q = {"keywords": p.search_term, "location": p.location, "f_TPR": f"r{p.hours_old * 3600}"}
    if p.is_remote:
        q["f_WT"] = "2"
    return "https://www.linkedin.com/jobs/search/?" + urlencode(q)


def build_curious(p: SearchParams) -> dict:
    return {
        "urls": [linkedin_search_url(p)],
        "limitPerSource": p.results_wanted,
        "scrapeCompany": False,
    }


def published_bucket(hours: int) -> str:
    """LinkedIn-style recency buckets. The cheap actor only accepts these exact values
    ('', r86400, r604800, r2592000); the window is rounded UP so we never miss fresh jobs,
    and the pipeline's max_age_days filter trims the rest."""
    if hours <= 24:
        return "r86400"
    if hours <= 24 * 7:
        return "r604800"
    return "r2592000"


CHEAP_MIN_ITEMS = 150  # the actor's schema minimum for pay-per-result billing


def build_cheap(p: SearchParams) -> dict:
    inp: dict = {
        "keyword": [p.search_term],
        "locations": [p.location],
        "publishedAt": published_bucket(p.hours_old),
        "maxItems": max(p.results_wanted, CHEAP_MIN_ITEMS),
        "saveOnlyUniqueItems": True,
        "enrichCompanyData": False,
    }
    # These filters run on Apify's side and skipped jobs are not billed.
    if p.exclude_keywords:
        inp["jobTitleExclude"] = p.exclude_keywords
    if p.exclude_companies:
        inp["companyExclude"] = p.exclude_companies
    return inp


def build_bebity(p: SearchParams) -> dict:
    return {
        "titles": [p.search_term],
        "locations": [p.location],
        "rows": p.results_wanted,
        "publishedAt": published_bucket(p.hours_old),
        "companyProfile": False,  # company enrichment is extra-cost
        "enrichCompany": False,
    }


# ---------------------------------------------------------------- mappers

def _first(d: Mapping, *keys: str):
    for k in keys:
        v = d.get(k)
        if v not in (None, "", []):
            return v
    return None


def _num(s) -> Optional[float]:
    if isinstance(s, (int, float)):
        return float(s)
    m = re.search(r"\d[\d,]*(?:\.\d+)?", str(s))
    return float(m.group(0).replace(",", "")) if m else None


def parse_salary_strings(info) -> tuple[Optional[float], Optional[float], Optional[str]]:
    """['$17.00', '$19.00'] -> (17.0, 19.0, 'USD'). Anything unparseable -> Nones."""
    if not info or not isinstance(info, (list, tuple)):
        return None, None, None
    nums = [n for n in (_num(x) for x in info) if n is not None]
    cur = next((CURRENCY_SYMBOLS[c] for x in info for c in str(x) if c in CURRENCY_SYMBOLS), None)
    if not nums:
        return None, None, cur
    return min(nums), (max(nums) if len(nums) > 1 else None), cur


def _job(item: dict, **f) -> Optional[dict]:
    """Shared output shape. Returns None for items too incomplete to be useful."""
    if not f.get("source_job_id") and not f.get("url"):
        return None
    if not f.get("title") or not f.get("company"):
        return None
    return {
        "source": "linkedin",
        "source_job_id": str(f.get("source_job_id") or f["url"]),
        "url": f.get("url"),
        "apply_url": f.get("apply_url"),
        "title": f["title"],
        "company": f["company"],
        "location": f.get("location"),
        "is_remote": f.get("is_remote"),
        "posted_at": f.get("posted_at"),
        "description": f.get("description"),
        "salary_min": f.get("salary_min"),
        "salary_max": f.get("salary_max"),
        "salary_currency": f.get("salary_currency"),
        "raw": item,
    }


def map_curious(item: dict) -> Optional[dict]:
    lo, hi, cur = parse_salary_strings(item.get("salaryInfo"))
    wp = item.get("workplaceTypes")
    return _job(
        item,
        source_job_id=_first(item, "id"),
        url=_first(item, "link", "jobUrl"),
        apply_url=_first(item, "applyUrl"),
        title=_first(item, "title"),
        company=_first(item, "companyName"),
        location=_first(item, "location"),
        is_remote=("remote" in " ".join(map(str, wp)).lower()) if wp else None,
        posted_at=_first(item, "postedAt"),
        description=_first(item, "descriptionText", "descriptionHtml"),
        salary_min=lo, salary_max=hi, salary_currency=cur,
    )


def map_cheap(item: dict) -> Optional[dict]:
    lo, hi, cur = parse_salary_strings(item.get("salaryInfo"))
    return _job(
        item,
        source_job_id=_first(item, "jobId", "id"),
        url=_first(item, "jobUrl"),
        apply_url=_first(item, "applyUrl"),
        title=_first(item, "jobTitle", "title"),
        company=_first(item, "companyName"),
        location=_first(item, "location"),
        is_remote=None,  # its workType field is deprecated and always empty
        posted_at=_first(item, "publishedAt"),
        description=_first(item, "jobDescription", "description"),
        salary_min=lo, salary_max=hi, salary_currency=cur,
    )


def map_bebity(item: dict) -> Optional[dict]:
    wt = item.get("workType")
    return _job(
        item,
        source_job_id=_first(item, "id"),
        url=_first(item, "jobUrl"),
        apply_url=_first(item, "applyUrl"),
        title=_first(item, "title"),
        company=_first(item, "companyName"),
        location=_first(item, "location"),
        is_remote=(str(wt).lower() == "remote") if wt else None,
        posted_at=_first(item, "publishedAt"),
        description=_first(item, "description", "descriptionHtml"),
        salary_min=item.get("salaryMin"),
        salary_max=item.get("salaryMax"),
        salary_currency=item.get("salaryCurrency"),
    )


# ---------------------------------------------------------------- actor registry

@dataclass(frozen=True)
class Actor:
    key: str
    actor_id: str
    token_env: str
    price_per_1000: float   # USD, from the Store page (free-tier price)
    run_fee: float          # USD fixed cost per run (measured; small runs cost more per job than the list price)
    build_input: Callable[[SearchParams], dict]
    map_item: Callable[[dict], Optional[dict]]

    def billed_results(self, p: SearchParams) -> int:
        return max(p.results_wanted, CHEAP_MIN_ITEMS) if self.key == "cheap" else p.results_wanted

    def estimate_cost(self, p: SearchParams) -> float:
        return self.billed_results(p) * self.price_per_1000 / 1000 + self.run_fee


ACTORS: list[Actor] = [
    Actor("curious", "curious_coder/linkedin-jobs-scraper", "APIFY_TOKEN_CURIOUS",
          1.00, 0.01, build_curious, map_curious),
    Actor("cheap", "cheap_scraper/linkedin-job-scraper", "APIFY_TOKEN_CHEAP",
          0.70, 0.02, build_cheap, map_cheap),
    Actor("bebity", "bebity/linkedin-jobs-scraper", "APIFY_TOKEN_BEBITY",
          1.00, 0.01, build_bebity, map_bebity),
]


@dataclass
class Account:
    actor: Actor
    token: str


def load_accounts(env: Mapping[str, str] = os.environ) -> list[Account]:
    return [Account(a, env[a.token_env].strip()) for a in ACTORS if env.get(a.token_env, "").strip()]


# ---------------------------------------------------------------- selection

def plan_order(accounts: list[Account], balances: dict[str, Optional[float]],
               p: SearchParams, day_ordinal: int) -> list[Account]:
    """Affordable accounts, richest first; accounts with unknown balance go last,
    rotated by day so they share the load."""
    usable = [a for a in accounts
              if balances.get(a.actor.key) is None or balances[a.actor.key] >= a.actor.estimate_cost(p)]
    known = sorted((a for a in usable if balances.get(a.actor.key) is not None),
                   key=lambda a: -balances[a.actor.key])
    unknown = [a for a in usable if balances.get(a.actor.key) is None]
    if unknown:
        r = day_ordinal % len(unknown)
        unknown = unknown[r:] + unknown[:r]
    return known + unknown


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def remaining_usd(client: httpx.Client, token: str) -> Optional[float]:
    """Monthly credit left, or None if Apify's response is not in the expected shape."""
    try:
        r = client.get(f"{API}/users/me/limits", headers=_auth(token))
        r.raise_for_status()
        d = r.json()["data"]
        left = float(d["limits"]["maxMonthlyUsageUsd"]) - float(d["current"]["monthlyUsageUsd"])
        return max(0.0, left)
    except Exception as e:  # unknown balance must not block a search
        log.warning("could not read Apify balance: %s", type(e).__name__)
        return None


# ---------------------------------------------------------------- running an actor

def charge_cap(actor: Actor, p: SearchParams) -> float:
    """The hard per-run charge limit sent to Apify: the estimate plus a little slack."""
    return math.ceil(actor.estimate_cost(p) * 1.25 * 100) / 100


def worst_case_cost(p: SearchParams, env: Mapping[str, str] = os.environ, max_attempts: int = MAX_ATTEMPTS) -> float:
    """The most one search can cost: the dearest configured account's charge cap, for every attempt allowed."""
    accounts = load_accounts(env)
    return round(max((charge_cap(a.actor, p) for a in accounts), default=0.0) * max_attempts, 4)


def run_actor(client: httpx.Client, acct: Account, p: SearchParams, *,
              poll_interval: float = 5.0, timeout_s: float = 600.0,
              sleep: Callable[[float], None] = time.sleep) -> list[dict]:
    actor = acct.actor
    path = actor.actor_id.replace("/", "~")
    cap = charge_cap(actor, p)
    try:
        r = client.post(f"{API}/acts/{path}/runs", headers=_auth(acct.token),
                        params={"maxTotalChargeUsd": cap}, json=actor.build_input(p))
    except httpx.HTTPError as e:
        raise ApifyError(f"start failed: {type(e).__name__}", started=False)
    if r.status_code >= 400:
        raise ApifyError(f"start failed: HTTP {r.status_code} {r.text[:200]}", started=False)
    run = r.json()["data"]
    try:
        return _finish_run(client, acct, run, poll_interval, timeout_s, sleep)
    except ApifyError:
        raise
    except httpx.HTTPError as e:      # the run exists, so whatever it used is billed
        raise ApifyError(f"run failed: {type(e).__name__}", started=True)


def _finish_run(client: httpx.Client, acct: Account, run: dict, poll_interval: float, timeout_s: float,
                sleep: Callable[[float], None]) -> list[dict]:
    waited = 0.0
    while run.get("status") not in TERMINAL:
        if waited >= timeout_s:
            client.post(f"{API}/actor-runs/{run['id']}/abort", headers=_auth(acct.token))
            raise ApifyError(f"run timed out after {int(timeout_s)}s", started=True)
        sleep(poll_interval)
        waited += poll_interval
        rr = client.get(f"{API}/actor-runs/{run['id']}", headers=_auth(acct.token))
        rr.raise_for_status()
        run = rr.json()["data"]

    if run["status"] != "SUCCEEDED":
        raise ApifyError(f"run ended {run['status']}", started=True)

    ds = client.get(f"{API}/datasets/{run['defaultDatasetId']}/items",
                    headers=_auth(acct.token), params={"clean": "true", "format": "json"})
    ds.raise_for_status()
    return ds.json()


def search_linkedin(p: SearchParams, *, env: Mapping[str, str] = os.environ,
                    client: Optional[httpx.Client] = None, today: Optional[date] = None,
                    sleep: Callable[[float], None] = time.sleep,
                    poll_interval: float = 5.0, only: Optional[str] = None, max_attempts: int = MAX_ATTEMPTS,
                    budget_left: Optional[float] = None) -> dict:
    """`only` restricts the run to one actor key ('curious' | 'cheap' | 'bebity'); handy for
    testing a single account or pinning a source."""
    accounts = load_accounts(env)
    if only:
        accounts = [a for a in accounts if a.actor.key == only]
        if not accounts:
            raise ApifyError(f"account '{only}' is not configured")
    if not accounts:
        raise ApifyError("no Apify tokens configured (set APIFY_TOKEN_CURIOUS / _CHEAP / _BEBITY)")

    owns_client = client is None
    client = client or httpx.Client(timeout=60)
    try:
        balances = {a.actor.key: remaining_usd(client, a.token) for a in accounts}
        order = plan_order(accounts, balances, p, (today or date.today()).toordinal())
        if not order:
            raise ApifyError(f"no account has enough credit left: {balances}")

        errors: list[str] = []
        attempts: list[dict] = []          # runs that started: each may have been billed
        committed = 0.0                    # the charge caps of runs started so far in this search
        tried = 0
        for acct in order[:max_attempts]:
            key = acct.actor.key
            cap = charge_cap(acct.actor, p)
            if budget_left is not None and committed + cap > budget_left:
                # a run starts only if, even at its hard charge limit, the search still fits the remaining budget
                errors.append(f"{key}: skipped (its ${cap:.2f} charge limit does not fit the ${max(budget_left - committed, 0):.2f} budget left)")
                continue
            tried += 1
            committed += cap
            try:
                items = run_actor(client, acct, p, sleep=sleep, poll_interval=poll_interval)
            except (ApifyError, httpx.HTTPError) as e:
                errors.append(f"{key}: {e}")
                log.warning("apify account %s failed: %s", key, e)
                if getattr(e, "started", True):
                    attempts.append({"account": key, "estimated_cost_usd": round(acct.actor.estimate_cost(p), 4)})
                continue
            # What it really cost: the change in the balance. Apify updates that figure with a delay (a real run showed
            # no change when read straight away, and the full drop a few minutes later), so a zero or unreadable
            # change means "not known yet", never "free"; the caller then counts the run at 1.5x its estimate.
            after = remaining_usd(client, acct.token)
            before = balances.get(key)
            actual = round(before - after, 4) if before is not None and after is not None and before > after else None
            jobs = [j for j in (acct.actor.map_item(i) for i in items) if j]
            return {
                "jobs": jobs,
                "errors": errors,
                "meta": {
                    "account": key,
                    "actor": acct.actor.actor_id,
                    "items_returned": len(items),
                    "items_mapped": len(jobs),
                    "balance_before_usd": balances.get(key),
                    "estimated_cost_usd": round(acct.actor.estimate_cost(p), 4),
                    "actual_cost_usd": actual,
                    "failed_attempts": attempts,
                },
            }
        if tried == 0 and budget_left is not None:
            return {"jobs": [], "errors": [], "meta": None,
                    "skipped": f"not enough Apify budget left for this search (${budget_left:.2f} left)"}
        err = ApifyError("all accounts failed: " + "; ".join(errors), started=bool(attempts))
        err.attempts = attempts
        raise err
    finally:
        if owns_client:
            client.close()
