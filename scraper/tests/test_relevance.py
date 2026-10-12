from datetime import datetime, timedelta, timezone

import pytest

import relevance as R

NOW = datetime(2026, 10, 11, 12, 0, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("term,words", [
    ("backend engineer", ["backend"]),
    ("full stack engineer", ["full", "stack"]),
    ("AI engineer", ["ai"]),
    ("engineer", ["engineer"]),                      # nothing left: fall back to the whole term
    ("Senior Software Engineer", ["senior", "software", "engineer"]),
    ("", []),
])
def test_core_words(term, words):
    assert R.core_words(term) == words


@pytest.mark.parametrize("title,terms,extra,expected", [
    ("Senior Backend Developer", ["backend engineer"], [], True),
    ("Full-Stack Engineer", ["full stack engineer"], [], True),
    ("Fullstack Engineer", ["full stack engineer"], [], True),
    ("Back-end Engineer", ["backend engineer"], [], True),
    ("Account Executive", ["backend engineer"], [], False),
    ("Backendless Chef", ["backend engineer"], [], False),          # whole words only
    ("Software Engineer, Infrastructure", ["backend engineer"], [], False),
    ("Software Engineer, Infrastructure", ["backend engineer"], ["software"], True),
    ("Machine Learning Engineer", ["AI engineer", "backend engineer"], [], False),
    ("Staff AI Engineer", ["AI engineer"], [], True),
    ("Platform Engineer", ["backend engineer", "platform engineer"], [], True),   # any term will do
    ("Full Engineer", ["full stack engineer"], [], False),          # every core word is needed
    ("", ["backend engineer"], [], False),
    ("Backend Engineer", [], [], False),
    ("Backend Engineer", [""], [], False),
])
def test_title_matches(title, terms, extra, expected):
    assert R.title_matches(title, terms, extra) is expected


INDIA = ["Pune, India"]


@pytest.mark.parametrize("loc,remote,user,allow,expected", [
    ("Bengaluru, Karnataka, India", False, ["Bengaluru, India"], True, True),
    ("Remote - US", True, INDIA, True, False),
    ("Remote", True, INDIA, True, True),
    ("Remote", True, INDIA, False, False),                           # remote not wanted
    ("Remote, India", True, INDIA, True, True),
    ("Hybrid", None, INDIA, True, True),                             # ambiguous: kept
    ("", None, INDIA, True, True),
    (None, None, INDIA, True, True),
    ("", True, INDIA, False, False),                                 # empty but flagged remote, remote off
    ("London, United Kingdom", False, INDIA, True, False),
    ("Berlin, Germany", False, ["Berlin, Germany"], True, True),
    ("Mumbai, India", False, INDIA, True, True),                     # same country as a wanted place
    ("London, UK", False, ["London, United Kingdom"], True, True),   # alias of the wanted country
    ("Remote - UK", True, ["London, United Kingdom"], True, True),
    ("Worldwide", True, INDIA, True, True),
    ("Anywhere", True, INDIA, False, False),
    ("Remote (U.S. only)", True, INDIA, True, False),
    ("United States; India", False, INDIA, True, True),              # lists one of our places
    ("Pune", False, ["Pune"], True, True),                           # city-only user location
    ("Zürich, Switzerland", False, INDIA, True, False),
    ("Remote", True, ["Remote"], True, True),
    ("Remote", True, [], True, True),
    ("Paris, France", False, [], True, False),
    ("Bangalore", False, ["Bengaluru, India"], True, True),         # old name of the same city
    ("Bengaluru", False, ["Bangalore"], True, True),
    # KNOWN LIMITATION (not desired behaviour): outside India a bare city/state with no country cannot be
    # resolved (no city database), so it is dropped. Boards almost always give "City, Country".
    ("Austin, TX", False, ["Dallas, United States"], True, False),
    ("Austin, TX, United States", False, ["Dallas, United States"], True, True),
])
def test_location_matches(loc, remote, user, allow, expected):
    assert R.location_matches(loc, remote, user, allow) is expected


# The home country counts as a wanted place when the user's locations are "Remote", empty or have no country.
@pytest.mark.parametrize("loc,remote,user,expected", [
    ("Remote - India", True, ["Remote"], True),
    ("Remote (India)", True, ["Remote"], True),
    ("India", False, ["Remote"], True),
    ("India", False, [], True),
    ("India", False, ["Pune"], True),
    ("Remote - US", True, ["Remote"], False),
    ("Remote - US", True, ["Pune"], False),
    ("Germany", False, [], False),
])
def test_home_country_is_a_wanted_place(loc, remote, user, expected):
    assert R.location_matches(loc, remote, user, True) is expected


# A small region map, applied to the countries the user wants: APAC/Asia contain India, EMEA/Europe do not.
@pytest.mark.parametrize("loc,user,expected", [
    ("Remote - APAC", INDIA, True), ("APAC", INDIA, True), ("Asia", INDIA, True), ("South Asia", INDIA, True),
    ("Asia-Pacific", INDIA, True), ("Remote - Asia Pacific", ["Remote"], True),
    ("Remote - EMEA", INDIA, False), ("Europe", INDIA, False), ("Remote - EU", INDIA, False),
    ("Remote - Americas", INDIA, False), ("North America", INDIA, False),
    ("Remote - EMEA", ["London, United Kingdom"], True), ("Europe", ["Berlin, Germany"], True),
    ("EU", ["Dublin, Ireland"], True), ("Remote - Europe", ["Amsterdam, Netherlands"], True),
    ("Remote - APAC", ["London, United Kingdom"], False),
    ("Remote - Americas", ["Austin, United States"], True), ("North America", ["Toronto, Canada"], True),
    ("Remote - EMEA", ["Austin, United States"], False),
    ("Remote - EMEA", ["Dubai, UAE"], True), ("Remote - Americas", ["Dubai, UAE"], False),
])
def test_regions_around_the_users_countries(loc, user, expected):
    assert R.location_matches(loc, True, user, True) is expected


# A bare Indian city or state (no country) is kept for a user whose places include India; other cities are not.
@pytest.mark.parametrize("loc,expected", [
    ("Hyderabad", True), ("Hyderabad, Telangana", True), ("Mumbai", True), ("Noida, UP", True),
    ("Bengaluru / Remote", True), ("Mumbai, IN", True), ("Gurgaon, Haryana", True), ("Chennai, Tamil Nadu", True),
    ("Bombay", True), ("Calcutta", True), ("Delhi NCR", True), ("Kerala", True), ("West Bengal", True),
    ("Hyderabad, India", True),
    ("Remote in Germany", False), ("Sydney", False),
    ("Paris", False),
])
def test_bare_indian_places_are_recognised(loc, expected):
    assert R.location_matches(loc, False, INDIA, True) is expected


def test_gazetteer_is_only_for_india():
    assert R.location_matches("Hyderabad", False, ["Berlin, Germany"], True) is False


def test_remote_with_a_wanted_place_is_kept_even_when_remote_is_off():
    # Intended: the location clause matches the user's country first, so allow_remote is never consulted. The
    # ingest filter's own remote rule (filters.py) is what enforces allow_remote=False for such a posting.
    assert R.location_matches("Remote - India", True, INDIA, False) is True
    assert R.location_matches("Remote", True, INDIA, False) is False


def _iso(days):
    return (NOW - timedelta(days=days)).isoformat()


@pytest.mark.parametrize("posted,limit,expected", [
    (_iso(3), 7, True),
    (_iso(10), 7, False),
    (_iso(3).replace("+00:00", "Z"), 7, True),
    (_iso(10).replace("+00:00", "Z"), 7, False),
    (NOW - timedelta(days=3), 7, True),
    (NOW - timedelta(days=10), 7, False),
    (datetime(2026, 10, 8), 7, True),                                # naive counts as UTC
    (int((NOW - timedelta(days=3)).timestamp()), 7, True),           # epoch seconds
    (int((NOW - timedelta(days=10)).timestamp()), 7, False),
    (int((NOW - timedelta(days=3)).timestamp() * 1000), 7, True),    # epoch milliseconds
    (int((NOW - timedelta(days=10)).timestamp() * 1000), 7, False),
    (str(int((NOW - timedelta(days=10)).timestamp() * 1000)), 7, False),   # numeric string
    (str(int((NOW - timedelta(days=3)).timestamp())), 7, True),
    ((NOW + timedelta(days=2)).isoformat(), 7, True),                # slightly in the future
    (None, 7, True),                                                 # unknown date passes
    ("", 7, True),
    ("not a date", 7, True),
    (float("nan"), 7, True),
    (10 ** 30, 7, True),
    (_iso(100), None, True),                                         # no limit
])
def test_within_age(posted, limit, expected):
    assert R.within_age(posted, limit, NOW) is expected


REQ = {"terms": ["backend engineer"], "locations": ["Pune, India"], "allow_remote": True,
       "max_age_days": 7, "title_words": []}


def _job(**kw):
    base = {"title": "Backend Engineer", "location": "Pune, India", "is_remote": False, "posted_at": _iso(1)}
    base.update(kw)
    return base


def test_check_accepts_a_good_job():
    assert R.check(_job(), REQ, NOW) == (True, "ok")


def test_check_reports_the_first_failing_reason():
    assert R.check(_job(title="Chef", location="Paris, France", posted_at=_iso(30)), REQ, NOW) == (False, "title")
    assert R.check(_job(location="Paris, France", posted_at=_iso(30)), REQ, NOW) == (False, "location")
    assert R.check(_job(posted_at=_iso(30)), REQ, NOW) == (False, "age")


def test_check_uses_title_words_and_tolerates_missing_keys():
    req = dict(REQ, title_words=["software"])
    assert R.check(_job(title="Software Engineer, Infra"), req, NOW)[0] is True
    assert R.check({"title": "Backend Engineer"}, {"terms": ["backend engineer"]}, NOW) == (True, "ok")
    assert R.check({}, REQ, NOW) == (False, "title")
