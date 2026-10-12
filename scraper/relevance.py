"""Free relevance filters for company-board and remote-feed jobs. Pure stdlib, no I/O.

A board or feed lists everything a company has, so these checks run BEFORE ingest and decide whether a
job is worth passing on to the paid scoring stage (spec section 5). check(job, req, now) is the one entry
point: it returns (True, "ok") or (False, reason) with reason "title", "location" or "age", tested in that
order.

Principle (same as filters.py): drop only on clear evidence, keep the ambiguous. An empty, "Hybrid" or bare
"Remote" location is kept (the scoring stage has its own location gate), but a location that clearly names
somewhere else ("Remote - US", "London, United Kingdom" for an India-based user) is dropped. An unknown or
unparseable date counts as "within age": the ingest filter applies the age limit again.

Known limitation: a posting that gives only a bare city or state with no country is recognised for INDIA
only (a small built-in gazetteer: major cities, states and their old names, so "Hyderabad", "Noida, UP" and
"Bengaluru / Remote" are kept for an India user). For every other country a bare city ("Austin, TX" for a
USA user) is not recognised and the job is dropped; boards almost always give "City, Country", and the
planner has no city database to resolve the rest.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, timezone
from typing import Iterable, Optional

import planner

GENERIC_WORDS = frozenset({
    "engineer", "engineers", "developer", "developers", "software", "senior", "junior", "sr", "jr", "staff",
    "principal", "lead", "i", "ii", "iii", "the", "a", "of", "and",
})
# Words in a location that say nothing about WHERE the job is. What is left after removing them (and any user
# place) is a place name; "or"/"and"/"only"/"first" cover "Remote or Pune", "US only", "Remote-first".
_GENERIC_LOCATION_WORDS = frozenset({"remote", "worldwide", "anywhere", "global", "hybrid", "distributed", "in",
                                     "the", "world", "or", "and", "only", "first"})
# Of those, the ones that mean the job can be done from anywhere (so allow_remote decides).
_REMOTE_WORDS = frozenset({"remote", "worldwide", "anywhere", "global", "distributed"})
# Old and new names of the same city, so "Bangalore" matches a wanted "Bengaluru" when the posting gives no country.
# Home-country fallbacks are listed in _user_places; the two tables below only widen what a known country covers.
_CITY_ALIASES = (("bengaluru", "bangalore"), ("mumbai", "bombay"), ("gurugram", "gurgaon"), ("chennai", "madras"),
                 ("kolkata", "calcutta"), ("delhi", "new delhi"), ("ho chi minh city", "saigon"))
# Bare Indian cities, states and union territories (and old names), kept when the user's countries include India.
_GAZETTEER = {"india": (
    "mumbai", "bombay", "navi mumbai", "thane", "pune", "nagpur", "bengaluru", "bangalore", "mysuru", "mysore",
    "hyderabad", "visakhapatnam", "chennai", "madras", "coimbatore", "kolkata", "calcutta", "delhi", "new delhi",
    "ncr", "gurugram", "gurgaon", "noida", "greater noida", "faridabad", "ghaziabad", "ahmedabad", "vadodara",
    "jaipur", "lucknow", "chandigarh", "mohali", "indore", "bhubaneswar", "kochi", "cochin", "thiruvananthapuram",
    "trivandrum", "karnataka", "maharashtra", "telangana", "tamil nadu", "haryana", "uttar pradesh", "up",
    "kerala", "gujarat", "west bengal", "rajasthan", "andhra pradesh", "madhya pradesh", "odisha", "goa")}
# Regions that contain a country, so "Remote - APAC" is kept for an India user but "EMEA" is not. Applied only to
# countries the user is known to want. Southeast/East Asia also contain the word "asia" and so are kept for India:
# that errs in the keep direction.
_EUROPE = ("uk", "germany", "ireland", "netherlands", "france", "spain", "italy", "sweden", "switzerland", "poland",
           "portugal", "austria", "belgium", "denmark", "finland", "norway", "czech republic", "greece", "hungary",
           "romania", "luxembourg", "ukraine")
_EMEA_ONLY = ("united arab emirates", "south africa", "saudi arabia", "israel", "egypt", "morocco", "nigeria",
              "qatar", "kuwait", "bahrain", "oman", "turkey")
_ASIA = ("india", "singapore", "japan", "china", "hong kong", "taiwan", "south korea", "malaysia", "indonesia",
         "philippines", "thailand", "vietnam", "pakistan")
_REGIONS = (
    (("emea", "europe", "eu"), _EUROPE), (("emea",), _EMEA_ONLY),
    (("apac", "asia", "asia pacific"), _ASIA), (("apac", "asia pacific"), ("australia", "new zealand")),
    (("americas", "north america"), ("usa", "canada", "mexico")),
    (("americas", "latam", "latin america"), ("brazil", "argentina", "chile", "colombia", "peru", "mexico")),
)
_REGION_EXTRA = {"india": ("south asia",)}


def _words(text: Optional[str]) -> list[str]:
    """Lower-case alphanumeric words: accents folded, hyphens/punctuation/spaces all alike."""
    folded = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", " ", folded.lower()).split()


# ---- title ----------------------------------------------------------------------------------------------

def core_words(term: str) -> list[str]:
    """The words of a search term that carry meaning ("backend engineer" -> ["backend"]).

    A term made only of generic words ("engineer") falls back to all of its words."""
    words = _words(term)
    return [w for w in words if w not in GENERIC_WORDS] or words


def _title_tokens(title: str) -> set[str]:
    """Whole words of the title plus each adjacent pair glued together, so 'Back-end' also offers 'backend'."""
    words = _words(title)
    return set(words) | {a + b for a, b in zip(words, words[1:])}


def title_matches(title: str, terms: list[str], extra_words: Iterable[str] = ()) -> bool:
    """True when, for at least one term, all its core words appear as whole words in the title.

    Hyphens and spaces are alike and a glued form matches too ('Full-Stack' and 'Fullstack' both match
    'full stack'). `extra_words` are words that pass on their own."""
    tokens = _title_tokens(title)
    if not tokens:
        return False
    for term in terms or []:
        core = core_words(term)
        if core and (all(w in tokens for w in core) or "".join(core) in tokens):
            return True
    for extra in extra_words or ():
        words = _words(extra)
        if words and all(w in tokens for w in words):
            return True
    return False


# ---- location -------------------------------------------------------------------------------------------

def _country_names(country: str) -> set[str]:
    """Every spelling the planner knows for an Indeed country ('uk' -> uk, united kingdom, england, ...)."""
    return {country} | {alias for alias, target in planner.ALIASES.items() if target == country}


def _user_places(user_locations: list[str], home_country: str) -> set[str]:
    """Space-padded phrases for the user's cities, countries and the regions around those countries."""
    places: set[str] = set()
    countries: set[str] = set()
    use_home = not user_locations                 # nothing wanted at all: fall back to the home country
    for loc in user_locations or []:
        words = _words(loc)
        if not words:
            continue
        if words == ["remote"]:
            use_home = True                       # "Remote" means remote from home
            continue
        places.add(" ".join(_words(str(loc).split(",")[0])))
        country = planner.country_for(loc, home_country)
        if country:
            countries.add(country)
        else:
            use_home = True                       # a bare city: assume it is in the home country too
    if use_home:
        home = planner.ALIASES.get((home_country or "").strip().lower(), (home_country or "").strip().lower())
        if home:
            countries.add(home)
    for country in countries:
        places.update(" ".join(_words(name)) for name in _country_names(country))
        places.update(_GAZETTEER.get(country, ()))
        places.update(_REGION_EXTRA.get(country, ()))
        for phrases, members in _REGIONS:
            if country in members:
                places.update(phrases)
    places.discard("")
    for group in _CITY_ALIASES:
        if places & set(group):
            places.update(group)
    return {f" {p} " for p in places}


def location_matches(location: Optional[str], is_remote: Optional[bool], user_locations: list[str],
                     allow_remote: bool, home_country: str = "india") -> bool:
    """Should a job in `location` go on to scoring?

    Keep when it mentions one of the user's cities or countries; when it is remote (or generic) and remote
    work is allowed; or when it says nothing about a place ("", "Hybrid"). Drop when it names somewhere
    else, or is remote and allow_remote is off."""
    text = re.sub(r"\bu\.s\.(?:a\.)?", "us", str(location or ""), flags=re.I)   # "U.S." would split into "u s"
    words = _words(text)
    padded = f" {' '.join(words)} "
    remote_like = bool(is_remote) or any(w in _REMOTE_WORDS for w in words)
    if any(place in padded for place in _user_places(user_locations, home_country)):
        return True
    if any(w not in _GENERIC_LOCATION_WORDS for w in words):
        return False                       # names a place that is not one of the user's
    return allow_remote if remote_like else True


# ---- age ------------------------------------------------------------------------------------------------

_NUMERIC = re.compile(r"\d{9,}(?:\.\d+)?")


def _parse_posted(value) -> Optional[datetime]:
    """datetime, date, ISO string (trailing Z ok), epoch seconds or milliseconds (number or numeric string)."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    number = None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if _NUMERIC.fullmatch(text):
            number = float(text)
        elif text:
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    if number is None:
        return None
    if number > 10 ** 11:                  # milliseconds (10**11 seconds is the year 5138)
        number /= 1000.0
    try:
        return datetime.fromtimestamp(number, tz=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def within_age(posted_at, max_age_days: Optional[int], now: Optional[datetime] = None) -> bool:
    """False only when the posting date is known and older than max_age_days."""
    if not max_age_days:
        return True
    posted = _parse_posted(posted_at)
    if posted is None:
        return True
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    return (now - posted).days <= max_age_days


# ---- all three ------------------------------------------------------------------------------------------

def check(job: dict, req: dict, now: Optional[datetime] = None) -> tuple[bool, str]:
    """(True, "ok") or (False, "title" | "location" | "age"), checked in that order."""
    if not title_matches(job.get("title") or "", req.get("terms") or [], req.get("title_words") or []):
        return False, "title"
    if not location_matches(job.get("location"), job.get("is_remote"), req.get("locations") or [],
                            req.get("allow_remote", True)):
        return False, "location"
    if not within_age(job.get("posted_at"), req.get("max_age_days"), now):
        return False, "age"
    return True, "ok"
