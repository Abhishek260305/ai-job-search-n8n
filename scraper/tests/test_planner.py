import pytest

import planner as P

SETTINGS = {
    "search_terms": ["backend engineer", "platform engineer"],
    "locations": ["Pune, India", "Remote", "London, United Kingdom", "Singapore", "United States", "Atlantis"],
    "sources": ["indeed", "naukri", "linkedin", "linkedin_apify"],
    "max_age_days": 7, "exclude_keywords": ["intern"], "exclude_companies": [],
}


@pytest.mark.parametrize("loc,country", [
    ("Pune, India", "india"), ("London, United Kingdom", "uk"), ("Berlin, Germany", "germany"),
    ("Dubai, United Arab Emirates", "united arab emirates"), ("Dubai, UAE", "united arab emirates"),
    ("Singapore", "singapore"), ("United States", "usa"), ("Toronto, Canada", "canada"),
    ("Dublin, Ireland", "ireland"), ("Remote", "india"), ("remote", "india"),
    ("Atlantis", None), ("Pune", None), ("", None), (None, None),
])
def test_country_for(loc, country):
    assert P.country_for(loc) == country


def test_remote_follows_the_home_country():
    assert P.country_for("Remote", home_country="germany") == "germany"


def test_incompatible_combinations_are_never_generated():
    combos = P.build_combos(SETTINGS)
    pairs = {(c["location"], c["source"]) for c in combos}
    assert ("London, United Kingdom", "naukri") not in pairs      # naukri is India only
    assert ("Singapore", "naukri") not in pairs
    assert ("Atlantis", "indeed") not in pairs                    # indeed needs a known country
    assert ("Atlantis", "linkedin") in pairs and ("Atlantis", "linkedin_apify") in pairs
    assert ("Pune, India", "naukri") in pairs and ("Remote", "naukri") in pairs
    assert ("London, United Kingdom", "indeed") in pairs


def test_source_varies_fastest_so_a_batch_mixes_boards():
    combos = P.build_combos(SETTINGS)
    first = combos[:4]
    assert {c["location"] for c in first} == {"Pune, India"} and len({c["source"] for c in first}) == 4


def test_counts_for_the_users_real_shape():
    s = {"search_terms": ["a", "b", "c", "d"], "sources": ["indeed", "naukri", "linkedin", "linkedin_apify"],
         "locations": ["Pune, India", "Bengaluru, India", "Hyderabad, India", "Mumbai, India", "Remote",
                       "London, United Kingdom", "Dublin, Ireland", "Berlin, Germany", "Amsterdam, Netherlands",
                       "Singapore", "Dubai, United Arab Emirates", "United States", "Toronto, Canada"]}
    combos = P.build_combos(s)
    # 5 india/remote locations x 4 sources + 8 abroad locations x 3 sources = 44, times 4 terms
    assert len(combos) == 4 * (5 * 4 + 8 * 3)


def test_request_for_a_jobspy_source():
    c = {"term": "backend engineer", "location": "London, United Kingdom", "source": "indeed", "country": "uk"}
    r = P.to_request(c, SETTINGS)
    assert r["endpoint"] == "/search" and r["source"] == "indeed"
    assert r["body"] == {"site_names": ["indeed"], "search_term": "backend engineer", "location": "London, United Kingdom",
                         "results_wanted": 15, "hours_old": 168, "country_indeed": "uk", "linkedin_fetch_description": True}
    assert r["label"] == "backend engineer | London, United Kingdom | indeed"


def test_remote_requests_search_the_home_country_and_flag_remote():
    c = {"term": "t", "location": "Remote", "source": "linkedin", "country": "india"}
    b = P.to_request(c, SETTINGS)["body"]
    assert b["location"] == "India" and b["is_remote"] is True and b["country_indeed"] == "india"


def test_request_for_apify_carries_the_filters_and_hits_the_apify_endpoint():
    c = {"term": "backend engineer", "location": "Pune, India", "source": "linkedin_apify", "country": "india"}
    r = P.to_request(c, dict(SETTINGS, exclude_companies=["Evil Corp"]))
    assert r["endpoint"] == "/search/linkedin"
    assert r["body"]["exclude_keywords"] == ["intern"] and r["body"]["exclude_companies"] == ["Evil Corp"]
    assert "site_names" not in r["body"] and r["body"]["results_wanted"] == 25 and r["body"]["hours_old"] == 168


def test_hours_follow_max_age_days():
    c = {"term": "t", "location": "Pune, India", "source": "indeed", "country": "india"}
    assert P.to_request(c, dict(SETTINGS, max_age_days=3))["body"]["hours_old"] == 72


def test_slice_wraps_without_repeating():
    items = list(range(5))
    assert P.slice_combos(items, 0, 2) == [0, 1]
    assert P.slice_combos(items, 4, 3) == [4, 0, 1]
    assert P.slice_combos(items, 3, 99) == [3, 4, 0, 1, 2]   # batch larger than the list: each item once
    assert P.slice_combos([], 0, 3) == [] and P.slice_combos(items, 0, 0) == []


def test_every_combination_is_visited_over_a_full_cycle():
    combos = P.build_combos(SETTINGS)
    n, batch, pos, seen = len(combos), 5, 0, []
    for _ in range(-(-n // batch)):
        seen += [c["term"] + c["location"] + c["source"] for c in P.slice_combos(combos, pos, batch)]
        pos = (pos + batch) % n
    assert set(seen) == {c["term"] + c["location"] + c["source"] for c in combos}


# ------------------------------------------------------------------ board and feed requests (Phase 7, Task 10)

import sources

BOARD_SETTINGS = {
    "search_terms": ["backend engineer", "platform engineer"],
    "locations": ["Pune, India", "Remote"],
    "sources": ["indeed", "greenhouse", "lever", "remotive"],
    "boards": {"greenhouse": ["stripe", "cloudflare"], "lever": ["spotify"], "ashby": ["openai"]},
    "max_age_days": 14, "allow_remote": False, "board_title_words": ["sde"],
}


def test_new_sources_never_become_search_combos():
    names = list(sources.BOARD_SYSTEMS) + list(sources.FEEDS)
    for name in names:
        for country in (None, "india", "uk", "usa"):
            assert P.compatible(name, country) is False
    s = dict(SETTINGS, sources=["indeed"] + names, boards={"greenhouse": ["stripe"]})
    combos = P.build_combos(s)
    assert combos and {c["source"] for c in combos} == {"indeed"}
    assert P.build_combos(dict(SETTINGS, sources=names)) == []
    # the existing names keep their answers
    assert P.compatible("linkedin", None) and P.compatible("linkedin_apify", None)
    assert not P.compatible("naukri", "uk") and P.compatible("naukri", "india")


def test_board_requests_respect_the_switch():
    slugs = {"greenhouse": ["stripe", "cloudflare"], "lever": ["spotify"]}
    base = {"search_terms": ["a"], "locations": ["Pune, India"]}
    # slugs but the system is not in sources: nothing
    assert P.build_board_requests(dict(base, sources=["indeed"], boards=slugs)) == []
    # in sources but no slugs (missing, empty, or only another system's slugs): nothing
    assert P.build_board_requests(dict(base, sources=["greenhouse"])) == []
    assert P.build_board_requests(dict(base, sources=["greenhouse"], boards={"greenhouse": []})) == []
    assert P.build_board_requests(dict(base, sources=["greenhouse"], boards=None)) == []
    assert P.build_board_requests(dict(base, sources=["lever"], boards={"greenhouse": ["x"]})) == []
    # both: one per slug, in config order
    out = P.build_board_requests(dict(base, sources=["greenhouse", "lever"], boards=slugs))
    assert [r["label"] for r in out] == ["boards | greenhouse | stripe", "boards | greenhouse | cloudflare",
                                         "boards | lever | spotify"]
    assert P.build_board_requests({}) == []


def test_board_systems_then_feeds_in_registry_order():
    s = {"search_terms": ["a"], "locations": ["x"],
         "sources": ["remoteok", "arbeitnow", "lever", "greenhouse", "remotive"],      # order in `sources` is irrelevant
         "boards": {"lever": ["l1"], "greenhouse": ["g1", "g2"]}}
    assert [r["label"] for r in P.build_board_requests(s)] == [
        "boards | greenhouse | g1", "boards | greenhouse | g2", "boards | lever | l1",
        "feeds | remotive", "feeds | remoteok", "feeds | arbeitnow"]


def test_board_request_body_shape():
    req = P.build_board_requests(BOARD_SETTINGS)[0]
    assert set(req) == {"label", "source", "endpoint", "body"}
    assert req["endpoint"] == "/fetch/board" and req["source"] == "greenhouse"
    assert req["body"] == {"source": "greenhouse", "slug": "stripe", "terms": ["backend engineer", "platform engineer"],
                           "locations": ["Pune, India", "Remote"], "allow_remote": False, "max_age_days": 14,
                           "title_words": ["sde"], "limit": 25}
    assert "name" not in req["body"]


def test_board_request_defaults_when_settings_are_sparse():
    req = P.build_board_requests({"search_terms": ["a"], "locations": ["x"], "sources": ["lever"],
                                  "boards": {"lever": ["l1"]}})[0]
    assert req["body"]["allow_remote"] is True and req["body"]["max_age_days"] == 7
    assert req["body"]["title_words"] == [] and req["body"]["limit"] == 25
    assert req["body"]["terms"] == ["a"] and req["body"]["locations"] == ["x"]
    # an explicit null / zero falls back the same way to_request does for searches
    req = P.build_board_requests({"sources": ["lever"], "boards": {"lever": ["l1"]}, "max_age_days": None,
                                  "board_title_words": None})[0]
    assert req["body"]["max_age_days"] == 7 and req["body"]["title_words"] == []
    assert req["body"]["terms"] == [] and req["body"]["locations"] == []


def test_slug_object_form_carries_the_display_name():
    s = dict(BOARD_SETTINGS, sources=["ashby", "greenhouse"],
             boards={"ashby": [{"slug": "openai", "name": "OpenAI"}, {"slug": "ramp"}], "greenhouse": ["stripe"]})
    out = P.build_board_requests(s)
    assert [r["label"] for r in out] == ["boards | greenhouse | stripe", "boards | ashby | openai", "boards | ashby | ramp"]
    assert out[1]["body"]["slug"] == "openai" and out[1]["body"]["name"] == "OpenAI"
    assert "name" not in out[0]["body"] and "name" not in out[2]["body"]


def test_malformed_slug_entries_are_ignored_silently():
    s = {"search_terms": ["a"], "locations": ["x"], "sources": ["greenhouse", "lever"],
         "boards": {"greenhouse": ["ok", 7, None, {"name": "no slug"}, {"slug": 5}, ["nested"], {"slug": "ok2", "name": 9}],
                    "lever": "not-a-list"}}
    out = P.build_board_requests(s)
    assert [r["body"]["slug"] for r in out] == ["ok", "ok2"]
    assert "name" not in out[1]["body"]            # a non-string display name is not carried
    assert P.build_board_requests(dict(s, boards=["not", "a", "dict"])) == []


def test_feed_request_has_no_slug():
    s = dict(BOARD_SETTINGS, sources=["remotive"])
    out = P.build_board_requests(s)
    assert len(out) == 1
    assert out[0]["label"] == "feeds | remotive" and out[0]["source"] == "remotive" and out[0]["endpoint"] == "/fetch/board"
    assert "slug" not in out[0]["body"] and "name" not in out[0]["body"]
    assert out[0]["body"]["source"] == "remotive" and out[0]["body"]["limit"] == 25
    # feeds are not tied to the boards config
    assert P.build_board_requests(dict(s, boards={"remotive": ["x"]})) == out


def test_board_request_does_not_share_lists_with_the_settings():
    s = dict(BOARD_SETTINGS)
    req = P.build_board_requests(s)[0]
    req["body"]["terms"].append("mutated")
    assert "mutated" not in s["search_terms"]


# ------------------------------------------------------------------ follow-up batch (Task F): clamp to the endpoint's bounds

def _clamp_settings(**over):
    return dict({"search_terms": ["a"], "locations": ["x"], "sources": ["lever"], "boards": {"lever": ["l1"]}}, **over)


def test_board_request_lists_are_capped_at_50_keeping_the_first():
    s = _clamp_settings(search_terms=[f"t{i}" for i in range(60)], locations=[f"l{i}" for i in range(80)],
                        board_title_words=[f"w{i}" for i in range(51)])
    body = P.build_board_requests(s)[0]["body"]
    assert body["terms"] == [f"t{i}" for i in range(50)]
    assert body["locations"] == [f"l{i}" for i in range(50)]
    assert body["title_words"] == [f"w{i}" for i in range(50)]


def test_board_request_strings_are_truncated_to_200_and_empties_dropped():
    s = _clamp_settings(search_terms=["y" * 300, "", "ok"], locations=["", "z" * 201], board_title_words=["", "w"])
    body = P.build_board_requests(s)[0]["body"]
    assert body["terms"] == ["y" * 200, "ok"] and body["locations"] == ["z" * 200] and body["title_words"] == ["w"]
    s = _clamp_settings(search_terms=["", "x" * 10])
    assert P.build_board_requests(s)[0]["body"]["terms"] == ["x" * 10]


def test_board_request_max_age_days_is_clamped_to_1_90():
    assert P.build_board_requests(_clamp_settings(max_age_days=500))[0]["body"]["max_age_days"] == 90
    assert P.build_board_requests(_clamp_settings(max_age_days=-3))[0]["body"]["max_age_days"] == 1
    assert P.build_board_requests(_clamp_settings(max_age_days=0))[0]["body"]["max_age_days"] == 7     # 0 is "unset", as before
    assert P.build_board_requests(_clamp_settings(max_age_days=30))[0]["body"]["max_age_days"] == 30
    assert P.build_board_requests(_clamp_settings(max_age_days=90.9))[0]["body"]["max_age_days"] == 90


def test_feed_requests_are_clamped_too():
    s = {"search_terms": ["t"] * 60, "locations": [], "sources": ["remotive"], "max_age_days": 999}
    body = P.build_board_requests(s)[0]["body"]
    assert len(body["terms"]) == 50 and body["max_age_days"] == 90


def test_junk_settings_without_a_board_source_do_not_raise():
    s = {"search_terms": ["a"] * 60, "locations": ["x"], "sources": ["indeed"], "max_age_days": "junk",
         "boards": {"greenhouse": ["stripe"]}, "board_title_words": [1, None]}
    assert P.build_board_requests(s) == []
    assert P.build_board_requests(dict(s, sources=["greenhouse"], boards={})) == []     # enabled but no slugs: still nothing, still no raise
