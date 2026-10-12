"""The settings page's logic, apart from the routes: build the page from the scraper's reports and apply a change.

Pure apart from the calls on the client it is given, so it is tested with a fake client.
"""
from __future__ import annotations

from scraper_client import ScraperClient
from store import Invalid

# scraper/schedules.py (MIN_EVERY, MAX_EVERY, MAX_BATCH, MAX_BOARD_BATCH); tests/test_settings_service.py fails when
# the two drift apart. They only shape hints on the page, the scraper enforces them.
LIMITS = {
    "min_every": {"collect": 15, "score": 5, "tailor": 5, "digest": 60},
    "max_every": 10080,
    "max_batch": {"collect": 50, "score": 50, "tailor": 10, "digest": 50},
    "max_board_batch": 200,
}

CAP_FIELDS = ("monthly_cap_usd", "daily_cap_usd", "apify_monthly_cap_usd", "apify_daily_cap_usd")


def page(client: ScraperClient) -> dict:
    schedule = client.schedule()
    features = client.features()
    tailoring = client.tailoring()
    budget = client.budget()["settings"]
    rows = [{**r, "board_batch": r.get("board_batch")} for r in schedule["schedules"]]
    return {
        "timezone": budget["timezone"],
        "paused": bool(schedule.get("paused")),
        "paused_reason": schedule.get("paused_reason"),
        "schedules": rows,
        "features": {"flags": features["flags"], "effective": features["effective"], "approval": features["approval"],
                     "approval_effective": features["approval_effective"]},
        "tailoring": tailoring,
        "budget": {k: budget[k] for k in (*CAP_FIELDS, "warn_pct")},
        "limits": LIMITS,
    }


def _entry(e) -> dict:
    return {"slug": e, "name": None} if isinstance(e, str) else {"slug": e["slug"], "name": e.get("name")}


def search_page(client: ScraperClient) -> dict:
    """The scraper's profile report, with every company entry in one shape ({"slug", "name"})."""
    report = client.profile()
    profile = dict(report["profile"])
    profile["boards"] = {system: [_entry(e) for e in entries] for system, entries in profile["boards"].items()}
    return {"profile": profile, "version": report["version"], "edited_by": report["edited_by"], "registry": report["registry"]}


def to_scraper_profile(profile: dict) -> dict:
    """The page's company entries ({"slug", "name"}) as the file format: a bare slug when there is no name."""
    out = dict(profile)
    out["boards"] = {system: [e["slug"] if not e.get("name") else {"slug": e["slug"], "name": e["name"]} for e in entries]
                     for system, entries in profile["boards"].items()}
    return out


def lower_only(client: ScraperClient, changes: dict) -> dict:
    """Send new budget caps to the scraper, but only if none is HIGHER than the current one.

    Lowering a cap can only stop spending sooner; raising one is the decision that lets money flow, so it stays a CLI
    (`budget set`) or .env action. The comparison is against what the scraper holds at this moment."""
    wanted = {k: v for k, v in changes.items() if v is not None and k in CAP_FIELDS}
    if not wanted:
        raise Invalid("nothing to change")
    current = client.budget()["settings"]
    raised = [k for k, v in wanted.items() if v > float(current[k]) + 1e-9]
    if raised:
        names = ", ".join(k.removesuffix("_usd").replace("_", " ") for k in raised)
        raise Invalid(f"a cap can only be lowered here (not {names}): to raise one, run scripts/jobsearch budget set")
    return client.set_budget(wanted)
