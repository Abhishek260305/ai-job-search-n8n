"""Collection planner: turns search settings into scrape requests, in rotating slices.

With 4 titles x 13 locations x 4 sources the full set is far too much to scrape in one go (and
would hammer the job boards), so each run takes the next `batch` combinations and a database
cursor remembers where it stopped (see next_collect_slice in migration 003).

Combinations that make no sense are never generated: Naukri only for India, Indeed only for
countries it supports. The order puts the SOURCE innermost, so a batch mixes different boards
instead of hitting one board several times in a row.

Company boards and remote feeds (Phase 7) are NOT search combinations: they list everything a company or feed has, so
they get their own requests (build_board_requests) and their own rotation, and never appear in build_combos.
"""
from __future__ import annotations

from typing import Iterator, Optional

import sources

# Country names JobSpy's Indeed accepts (lower-case). "uk" and "usa" are its own short forms.
INDEED_COUNTRIES = {
    "argentina", "australia", "austria", "bahrain", "belgium", "brazil", "canada", "chile", "china", "colombia",
    "costa rica", "czech republic", "denmark", "ecuador", "egypt", "finland", "france", "germany", "greece",
    "hong kong", "hungary", "india", "indonesia", "ireland", "israel", "italy", "japan", "kuwait", "luxembourg",
    "malaysia", "mexico", "morocco", "netherlands", "new zealand", "nigeria", "norway", "oman", "pakistan",
    "panama", "peru", "philippines", "poland", "portugal", "qatar", "romania", "saudi arabia", "singapore",
    "south africa", "south korea", "spain", "sweden", "switzerland", "taiwan", "thailand", "turkey", "ukraine",
    "united arab emirates", "uk", "usa", "uruguay", "venezuela", "vietnam",
}
ALIASES = {
    "united kingdom": "uk", "great britain": "uk", "england": "uk", "scotland": "uk", "wales": "uk",
    "united states": "usa", "united states of america": "usa", "us": "usa", "u.s.": "usa", "america": "usa",
    "uae": "united arab emirates", "korea": "south korea",
}
LINKEDIN_SOURCES = {"linkedin", "linkedin_apify"}
JOBSPY_SOURCES = {"indeed", "naukri", "linkedin"}


def country_for(location: str, home_country: str = "india") -> Optional[str]:
    """Best-effort Indeed country for a 'City, Country' (or bare country) location."""
    loc = (location or "").strip().lower()
    if not loc:
        return None
    if loc == "remote":
        return home_country
    candidate = loc.split(",")[-1].strip()
    candidate = ALIASES.get(candidate, candidate)
    if candidate in INDEED_COUNTRIES:
        return candidate
    whole = ALIASES.get(loc, loc)
    return whole if whole in INDEED_COUNTRIES else None


def is_remote_location(location: str) -> bool:
    return (location or "").strip().lower() == "remote"


def compatible(source: str, country: Optional[str]) -> bool:
    if source in sources.BOARD_SYSTEMS or source in sources.FEEDS:
        return False        # boards and feeds are planned by build_board_requests, never as (term, location) searches
    if source == "indeed":
        return country is not None
    if source == "naukri":
        return country == "india"
    return source in LINKEDIN_SOURCES


def build_combos(settings: dict, home_country: str = "india") -> list[dict]:
    """All sensible (term, location, source) combinations, in rotation order."""
    out: list[dict] = []
    for term in settings.get("search_terms", []):
        for loc in settings.get("locations", []):
            country = country_for(loc, home_country)
            for source in settings.get("sources", []):
                if compatible(source, country):
                    out.append({"term": term, "location": loc, "source": source, "country": country})
    return out


def to_request(combo: dict, settings: dict, home_country: str = "india") -> dict:
    """The HTTP call n8n should make for one combination."""
    remote = is_remote_location(combo["location"])
    query_location = combo["country"].title() if remote and combo["country"] else combo["location"]
    hours = int(settings.get("max_age_days") or 7) * 24
    label = f"{combo['term']} | {combo['location']} | {combo['source']}"
    if combo["source"] == "linkedin_apify":
        body = {"search_term": combo["term"], "location": query_location, "results_wanted": 25, "hours_old": hours,
                "exclude_keywords": settings.get("exclude_keywords", []),
                "exclude_companies": settings.get("exclude_companies", [])}
        if remote:
            body["is_remote"] = True
        return {"label": label, "source": "linkedin_apify", "endpoint": "/search/linkedin", "body": body}
    body = {"site_names": [combo["source"]], "search_term": combo["term"], "location": query_location,
            "results_wanted": 15, "hours_old": hours, "country_indeed": combo["country"] or home_country,
            "linkedin_fetch_description": True}
    if remote:
        body["is_remote"] = True
    return {"label": label, "source": combo["source"], "endpoint": "/search", "body": body}


BODY_LIST_MAX = 50     # the /fetch/board request bounds (BoardFetchRequest in app.py): items per list, characters per string
BODY_TEXT_MAX = 200
BOARD_LIMIT = 25   # jobs one board/feed request may return (the endpoint accepts 1..100; see boards.fetch)


def _slug_entries(value) -> Iterator[tuple]:
    """(slug, display name or None) for each usable entry of one system's slug list; anything malformed is skipped.
    The slug pattern is NOT checked here (profile_tools validates on write and boards.fetch refuses a bad one)."""
    if not isinstance(value, (list, tuple)):
        return
    for entry in value:
        if isinstance(entry, str):
            yield entry, None
        elif isinstance(entry, dict) and isinstance(entry.get("slug"), str):
            name = entry.get("name")
            yield entry["slug"], name if isinstance(name, str) and name else None


def build_board_requests(settings: dict) -> list[dict]:
    """The /fetch/board calls for the company boards and remote feeds the user switched on.

    A board system runs only when its name is in `sources` AND it has at least one slug; a feed runs when named in
    `sources`. Order: systems in sources.BOARD_SYSTEMS order (slugs in config order), then feeds in sources.FEEDS order.
    Settings without `boards` / `board_title_words` yield [] (nothing new runs for existing users)."""
    enabled = settings.get("sources") or []
    boards = settings.get("boards")
    boards = boards if isinstance(boards, dict) else {}
    if not any(name in enabled for name in tuple(sources.BOARD_SYSTEMS) + tuple(sources.FEEDS)):
        return []                               # nothing board-like is on: do not even look at the other fields
    common = {"terms": _clamp_list(settings.get("search_terms")), "locations": _clamp_list(settings.get("locations")),
              "allow_remote": True if settings.get("allow_remote") is None else bool(settings["allow_remote"]),
              "max_age_days": _clamp_days(settings.get("max_age_days")),
              "title_words": _clamp_list(settings.get("board_title_words")), "limit": BOARD_LIMIT}
    out: list[dict] = []
    for system in sources.BOARD_SYSTEMS:
        if system not in enabled:
            continue
        for slug, name in _slug_entries(boards.get(system)):
            body = {"source": system, "slug": slug, **_copy(common)}
            if name:
                body["name"] = name
            out.append({"label": f"boards | {system} | {slug}", "source": system, "endpoint": "/fetch/board", "body": body})
    for feed in sources.FEEDS:
        if feed in enabled:
            out.append({"label": f"feeds | {feed}", "source": feed, "endpoint": "/fetch/board",
                        "body": {"source": feed, **_copy(common)}})
    return out


def _clamp_list(value) -> list:
    """At most BODY_LIST_MAX non-empty strings of at most BODY_TEXT_MAX characters (the first ones kept), so a legal
    config can never be refused by POST /fetch/board (BoardFetchRequest in app.py has the same bounds)."""
    if not isinstance(value, (list, tuple)):
        return []
    out = [v[:BODY_TEXT_MAX] for v in value if isinstance(v, str) and v]
    return out[:BODY_LIST_MAX]


def _clamp_days(value) -> int:
    """max_age_days as the endpoint wants it (1..90); unset, zero or unreadable means the default of 7."""
    try:
        days = int(value or 7)
    except (TypeError, ValueError, OverflowError):
        days = 7
    return max(1, min(90, days))


def _copy(common: dict) -> dict:
    """A copy whose lists are not shared between requests (or with the settings)."""
    return {k: list(v) if isinstance(v, list) else v for k, v in common.items()}


def slice_combos(combos: list, start: int, batch: int) -> list:
    """`batch` items starting at `start`, wrapping around the end (never repeating an item)."""
    n = len(combos)
    if n == 0 or batch <= 0:
        return []
    batch = min(batch, n)
    return [combos[(start + i) % n] for i in range(batch)]
