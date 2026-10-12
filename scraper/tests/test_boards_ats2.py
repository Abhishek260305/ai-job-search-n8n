"""The two list-then-detail company-board adapters in boards.py: Workable and SmartRecruiters (spec section 2).

Both APIs list postings WITHOUT descriptions, so each adapter pre-filters on list data and makes one detail call per
surviving job, capped at req.limit. Everything runs against the StubBoards stand-in (its `.log` of (method, path, query)
proves exactly which calls were made) or an in-process mock transport for per-request failures; nothing touches the
internet. Each test goes through boards.fetch(), so the relevance filter, the cap and the one-line error handling are
exercised exactly as production uses them."""
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import httpx
import pytest

import boards
import stubs


@pytest.fixture(autouse=True)
def _fast_retry(monkeypatch):
    monkeypatch.setattr(boards, "RETRY_DELAY_S", 0)


def req(**kw):
    base = dict(terms=["backend engineer"], locations=["Bengaluru"], allow_remote=True, max_age_days=7)
    base.update(kw)
    return boards.BoardRequest(**base)


def ago(days=0, hours=0):
    return datetime.now(timezone.utc) - timedelta(days=days, hours=hours)


def run(monkeypatch, stub, system, slug="acme", **kw):
    """boards.fetch against the stub (the BOARDS_API_URL hook points every system at it)."""
    monkeypatch.setenv("BOARDS_API_URL", stub.url)
    return boards.fetch(system, slug, req(**kw))


def mock_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def json_response(payload, status=200):
    return httpx.Response(status, content=json.dumps(payload).encode())      # content=, so a None payload is the JSON text "null"


# ---- Workable -------------------------------------------------------------------------------------------

LIST_BODY = {"query": "", "location": [], "department": [], "worktype": [], "remote": []}


def wk_list_paths(stub):
    return [e for e in stub.log if e[0] == "POST"]


def wk_detail_paths(stub):
    return [path for method, path, query in stub.log if method == "GET"]


def wk_data(posts, slug="acme"):
    return {("workable", slug): posts}


def test_workable_maps_list_and_detail(monkeypatch):
    post = stubs.workable_job("Backend Engineer", "Bengaluru, Karnataka, India", ago(days=2), "ABC123DEF4")
    post["_detail"] = stubs.workable_detail(
        post, description="<p>Build the <b>API</b>.</p>", requirements="<ul><li>Python</li><li>SQL</li></ul>",
        benefits="<p>Fish &amp; chips</p>")
    with stubs.StubBoards(data=wk_data([post])) as stub:
        result = run(monkeypatch, stub, "workable")
    assert result.errors == [] and result.listed == 1 and result.relevant == 1
    (job,) = result.jobs
    assert job["source"] == "workable" and job["source_job_id"] == "acme:ABC123DEF4"
    assert job["title"] == "Backend Engineer" and job["company"] == "Acme"
    assert job["url"] == "https://apply.workable.com/acme/j/ABC123DEF4/" and job["apply_url"] == job["url"]
    assert job["location"] == "Bengaluru, Karnataka, India" and job["is_remote"] is False
    assert job["posted_at"] == post["published"]
    # description + requirements + benefits, each as text, joined by blank lines
    assert job["description"] == "Build the API.\n\nPython\nSQL\n\nFish & chips"
    # raw is a compact subset (ids, url, date, location), never the description
    assert job["raw"]["shortcode"] == "ABC123DEF4" and job["raw"]["published"] == post["published"]
    assert "Build the" not in json.dumps(job["raw"]) and "description" not in job["raw"]


def test_workable_one_post_for_the_list_and_only_v2_gets_for_details(monkeypatch):
    posts = [stubs.workable_job("Backend Engineer", "Bengaluru, India", ago(days=1 + i), f"SC{i:03d}") for i in range(3)]
    with stubs.StubBoards(data=wk_data(posts)) as stub:
        run(monkeypatch, stub, "workable")
    assert wk_list_paths(stub) == [("POST", "/workable/api/v3/accounts/acme/jobs", "")]
    assert stub.post_bodies == [LIST_BODY]
    assert sorted(wk_detail_paths(stub)) == [f"/workable/api/v2/accounts/acme/jobs/SC{i:03d}" for i in range(3)]   # never v3
    assert all(m == "GET" for m, p, q in stub.log if "/v2/" in p)


def test_workable_detail_only_for_relevant(monkeypatch):
    relevant = [stubs.workable_job("Backend Engineer", "Bengaluru, India", ago(days=1 + i), f"REL{i}") for i in range(3)]
    noise = ([stubs.workable_job("Marketing Manager", "Bengaluru, India", ago(days=1), f"MKT{i}") for i in range(4)]        # wrong title
             + [stubs.workable_job("Backend Engineer", "London, United Kingdom", ago(days=1), f"LON{i}") for i in range(3)]  # wrong place
             + [stubs.workable_job("Backend Engineer", "Bengaluru, India", ago(days=40), f"OLD{i}") for i in range(2)])     # too old
    with stubs.StubBoards(data=wk_data(noise[:5] + relevant + noise[5:])) as stub:
        result = run(monkeypatch, stub, "workable")
    assert result.listed == 12 and result.relevant == 3 and len(result.jobs) == 3 and result.errors == []
    assert sorted(wk_detail_paths(stub)) == [f"/workable/api/v2/accounts/acme/jobs/REL{i}" for i in range(3)]   # 3, not 12
    assert len(wk_list_paths(stub)) == 1


def test_workable_detail_calls_are_capped_at_the_limit_and_newest_first(monkeypatch):
    posts = [stubs.workable_job("Backend Engineer", "Bengaluru, India", ago(days=1 + i), f"N{i:02d}") for i in range(10)]   # `published` is a date: one day apart
    with stubs.StubBoards(data=wk_data(posts[::-1])) as stub:               # the list is served oldest first: the adapter sorts
        result = run(monkeypatch, stub, "workable", limit=4, max_age_days=30)
    assert len(wk_detail_paths(stub)) == 4
    assert [j["source_job_id"] for j in result.jobs] == ["acme:N00", "acme:N01", "acme:N02", "acme:N03"]   # the 4 newest


def test_workable_limit_zero_makes_no_detail_calls(monkeypatch):
    with stubs.StubBoards(data=wk_data([stubs.workable_job(id="A1")])) as stub:
        result = run(monkeypatch, stub, "workable", limit=0)
    assert result.jobs == [] and result.errors == [] and wk_detail_paths(stub) == []


def test_workable_detail_failure_skips_that_job_and_reports_it():
    """One failed detail costs that job only (it is debug-logged, not an error line); the other jobs still come back."""
    posts = [stubs.workable_job("Backend Engineer", "Bengaluru, India", ago(days=1, hours=i), f"F{i}") for i in range(4)]

    def handler(request: httpx.Request):
        if request.method == "POST":
            return json_response({"results": posts, "total": 4})
        code = request.url.path.rsplit("/", 1)[1]
        if code == "F1":
            return httpx.Response(404, text="gone")
        if code == "F2":
            return httpx.Response(500, text="boom")
        if code == "F3":
            return httpx.Response(200, text="<html>not json</html>")
        return json_response(stubs.workable_detail(posts[0]))

    result = boards.fetch("workable", "acme", req(), client=mock_client(handler))
    assert result.errors == [] and result.listed == 4
    assert [j["source_job_id"] for j in result.jobs] == ["acme:F0"]


def test_workable_every_detail_failing_is_one_error_line():
    posts = [stubs.workable_job("Backend Engineer", "Bengaluru, India", ago(days=1, hours=i), f"E{i}") for i in range(3)]

    def handler(request: httpx.Request):
        if request.method == "POST":
            return json_response({"results": posts})
        return httpx.Response(404, text="gone")

    result = boards.fetch("workable", "acme", req(), client=mock_client(handler))
    assert result.jobs == [] and len(result.errors) == 1 and result.errors[0].startswith("workable/acme: ")


def test_workable_a_rate_limited_detail_stops_the_board():
    calls = []

    def handler(request: httpx.Request):
        calls.append((request.method, request.url.path))
        if request.method == "POST":
            return json_response({"results": [stubs.workable_job(id=f"R{i}") for i in (1, 2, 3)]})
        return httpx.Response(429, text="slow down")

    result = boards.fetch("workable", "acme", req(), client=mock_client(handler))
    assert result.jobs == [] and result.errors == ["workable/acme: rate limited by the source; skipped for this run"]
    assert len(calls) == 2                                         # the list call and ONE detail call, nothing after the 429


@pytest.mark.parametrize("remote,workplace,expected", [
    (True, None, True), (False, None, False), (False, "remote", True), (True, "remote", True),
    (False, "hybrid", False), (False, "on_site", False), (True, "hybrid", False)])
def test_workable_is_remote_from_the_flag_and_workplace(monkeypatch, remote, workplace, expected):
    post = stubs.workable_job("Backend Engineer", "Bengaluru, India", ago(days=1), "RM1", remote=remote)
    if workplace is None:
        del post["workplace"]
    else:
        post["workplace"] = workplace
    with stubs.StubBoards(data=wk_data([post])) as stub:
        result = run(monkeypatch, stub, "workable")
    assert result.errors == [] and result.jobs[0]["is_remote"] is expected


def test_workable_location_is_built_from_the_structured_parts(monkeypatch):
    post = stubs.workable_job("Backend Engineer", "Pune, Maharashtra, India", ago(days=1), "LOC1")
    country_only = stubs.workable_job("Backend Engineer", "India", ago(days=1), "LOC2")
    country_only["location"] = {"city": None, "region": None, "country": "India", "countryCode": "IN"}
    with stubs.StubBoards(data=wk_data([post, country_only])) as stub:
        result = run(monkeypatch, stub, "workable", locations=["India"])
    assert {j["source_job_id"]: j["location"] for j in result.jobs} == {"acme:LOC1": "Pune, Maharashtra, India", "acme:LOC2": "India"}


def test_workable_url_is_built_from_slug_and_shortcode_not_taken_from_the_response(monkeypatch):
    post = stubs.workable_job(id="URL1")
    post["url"] = "https://evil.example/steal"                      # a link in the payload is never used
    post["application_url"] = "https://evil.example/apply"
    with stubs.StubBoards(data=wk_data([post], slug="Acme-Co")) as stub:
        result = run(monkeypatch, stub, "workable", "Acme-Co")
    assert result.jobs[0]["url"] == "https://apply.workable.com/Acme-Co/j/URL1/"
    assert "evil.example" not in json.dumps(result.jobs[0])


def test_workable_an_unsafe_shortcode_is_skipped_and_costs_no_detail_call(monkeypatch):
    good = stubs.workable_job(id="GOOD1")
    bad = [stubs.workable_job(id="a/b"), stubs.workable_job(id="../x"), stubs.workable_job(id="a b")]
    with stubs.StubBoards(data=wk_data(bad + [good])) as stub:
        result = run(monkeypatch, stub, "workable")
    assert result.errors == [] and [j["source_job_id"] for j in result.jobs] == ["acme:GOOD1"]
    assert wk_detail_paths(stub) == ["/workable/api/v2/accounts/acme/jobs/GOOD1"]


def test_workable_company_comes_from_config_else_slug(monkeypatch):
    with stubs.StubBoards(data=wk_data([stubs.workable_job(id="C1")], slug="big-co")) as stub:
        assert run(monkeypatch, stub, "workable", "big-co", name="Big Co (config)").jobs[0]["company"] == "Big Co (config)"
        assert run(monkeypatch, stub, "workable", "big-co").jobs[0]["company"] == "Big Co"


def test_workable_empty_board_is_not_an_error(monkeypatch):
    with stubs.StubBoards(modes={("workable", "acme"): "empty"}) as stub:
        result = run(monkeypatch, stub, "workable")
    assert result.jobs == [] and result.errors == [] and result.listed == 0 and wk_detail_paths(stub) == []


def test_workable_garbage_body_is_one_error_not_a_crash(monkeypatch):
    with stubs.StubBoards(modes={("workable", "acme"): "garbage"}) as stub:
        result = run(monkeypatch, stub, "workable")
    assert result.jobs == [] and result.errors == ["workable/acme: returned something that is not JSON"]


def test_workable_unexpected_shape_is_one_error():
    for payload in ({"results": "nope"}, {"other": []}, ["a list"], None):
        result = boards.fetch("workable", "acme", req(), client=mock_client(lambda r, p=payload: json_response(p)))
        assert result.jobs == [] and len(result.errors) == 1 and "unexpected format" in result.errors[0], payload


def test_workable_missing_fields_are_skipped(monkeypatch):
    good = stubs.workable_job(id="OK1")
    no_code, no_title, bad_loc = stubs.workable_job(id="X2"), stubs.workable_job(id="X3"), stubs.workable_job(id="OK4")
    del no_code["shortcode"]
    no_title["title"] = ""
    bad_loc["location"] = None                                                    # survivable: no location is kept by relevance
    with stubs.StubBoards(data=wk_data([no_code, no_title, bad_loc, good])) as stub:
        result = run(monkeypatch, stub, "workable")
    assert result.errors == [] and sorted(j["source_job_id"] for j in result.jobs) == ["acme:OK1", "acme:OK4"]
    assert len(wk_detail_paths(stub)) == 2                                         # an unusable posting costs no detail call
    assert result.dropped["unusable"] == 0                                         # skipped inside the adapter, not counted


def test_workable_entries_that_are_not_objects_are_skipped():
    good = stubs.workable_job(id="OK1")

    def handler(request: httpx.Request):
        if request.method == "POST":
            return json_response({"results": ["a string", 5, None, [], {}, good]})
        return json_response(stubs.workable_detail(good))

    result = boards.fetch("workable", "acme", req(), client=mock_client(handler))
    assert result.errors == [] and [j["source_job_id"] for j in result.jobs] == ["acme:OK1"]


def test_workable_not_found_is_one_error(monkeypatch):
    with stubs.StubBoards(modes={("workable", "acme"): "not_found"}) as stub:
        result = run(monkeypatch, stub, "workable")
    assert result.jobs == [] and result.errors == ["workable/acme: not found (check the slug)"]


# ---- SmartRecruiters ------------------------------------------------------------------------------------

def sr_list_entries(stub):
    return [(path, parse_qs(query, keep_blank_values=True)) for method, path, query in stub.log
            if method == "GET" and path.endswith("/postings")]


def sr_detail_paths(stub):
    return [path for method, path, query in stub.log if method == "GET" and not path.endswith("/postings")]


def sr_data(posts, slug="Acme"):
    return {("smartrecruiters", slug): posts}


def sr_many(n, *, board="Acme", title="Backend Engineer", location="Bengaluru, Karnataka, India", start_hours=1, step_hours=0.1, base=744000000009000):
    """n postings newest first: posting i was released start_hours + i * step_hours hours ago (6 minutes apart by default, so even 450 stay fresh)."""
    return [stubs.sr_job(title, location, ago(hours=start_hours + i * step_hours), base + i, board=board) for i in range(n)]


def test_smartrecruiters_maps_list_and_detail(monkeypatch):
    post = stubs.sr_job("Backend Engineer", "Bengaluru, Karnataka, India", ago(days=2), 744000000000777, company="Acme Corp", board="Acme")
    post["_detail"] = stubs.sr_detail(
        post, description="<p>Build the <b>API</b>.</p>", qualifications="<ul><li>Python</li></ul>",
        additional="<p>Fish &amp; chips</p>", company_description="<p>About the COMPANY, never used.</p>")
    with stubs.StubBoards(data=sr_data([post])) as stub:
        result = run(monkeypatch, stub, "smartrecruiters", "Acme")
    assert result.errors == [] and result.listed == 1 and result.relevant == 1
    (job,) = result.jobs
    assert job["source"] == "smartrecruiters" and job["source_job_id"] == "Acme:744000000000777"
    assert job["title"] == "Backend Engineer" and job["company"] == "Acme Corp"
    assert job["url"] == post["_detail"]["postingUrl"] and job["apply_url"] == post["_detail"]["applyUrl"]
    assert job["apply_url"] != job["url"]
    assert job["location"] == "Bengaluru, Karnataka, India" and job["is_remote"] is False
    assert job["posted_at"] == post["releasedDate"]
    # jobDescription, qualifications, additionalInformation in that order; companyDescription is skipped
    assert job["description"] == "Build the API.\n\nPython\n\nFish & chips"
    assert "COMPANY" not in job["description"]
    assert job["raw"]["id"] == "744000000000777" and job["raw"]["releasedDate"] == post["releasedDate"]
    assert "Build the" not in json.dumps(job["raw"]) and "jobAd" not in job["raw"]


def test_smartrecruiters_sends_q_and_country(monkeypatch):
    with stubs.StubBoards(data=sr_data(sr_many(3))) as stub:
        result = run(monkeypatch, stub, "smartrecruiters", "Acme", terms=["backend engineer"], locations=["Bengaluru, India"])
    assert result.errors == [] and len(result.jobs) == 3
    ((path, query),) = sr_list_entries(stub)
    assert path == "/smartrecruiters/v1/companies/Acme/postings"
    assert query == {"limit": ["100"], "offset": ["0"], "q": ["backend"], "country": ["in"]}     # an India-only user: country=in


def test_smartrecruiters_q_is_the_core_words_of_all_terms_deduplicated():
    seen = []

    def handler(request: httpx.Request):
        seen.append(request.url)
        return json_response({"offset": 0, "limit": 100, "totalFound": 0, "content": []})

    r = req(terms=["backend engineer", "Python Developer", "Backend Developer", "engineer"], locations=["Bengaluru, India"])
    result = boards.fetch("smartrecruiters", "Acme", r, client=mock_client(handler))
    assert result.errors == []
    (url,) = seen
    assert url.params["q"] == "backend python engineer"            # backend, python, then "engineer" (a term of only generic words keeps them)
    assert url.params["limit"] == "100" and url.params["offset"] == "0"


def test_smartrecruiters_q_is_url_encoded():
    seen = []

    def handler(request: httpx.Request):
        seen.append(request.url)
        return json_response({"content": [], "totalFound": 0})

    boards.fetch("smartrecruiters", "Acme", req(terms=["c++ & go/lang"], locations=[]), client=mock_client(handler))
    assert " " not in str(seen[0]) and "&go" not in str(seen[0]) and seen[0].params["q"] == "c go lang"   # core words only, so no stray punctuation
    assert str(seen[0]).count("?") == 1


@pytest.mark.parametrize("locations,expected", [
    (["Bengaluru, India"], "in"), (["India"], "in"), (["Pune, India", "Mumbai, India"], "in"),          # one country, two cities
    (["London, United Kingdom"], "gb"), (["Austin, United States"], "us"), (["Berlin, Germany"], "de"),
    (["Remote", "Bengaluru, India"], "in"),                                                              # Remote is not a country here
    (["Bengaluru, India", "London, UK"], None),                                                          # two countries: no country param
    (["Bengaluru, India", "Berlin, Germany", "Dublin, Ireland"], None),
    (["Remote"], None), ([], None),                                                                      # no country at all
    (["Bengaluru"], None),                                                                               # a bare city does not resolve
    (["Bengaluru", "Bengaluru, India"], None),                                                           # one unresolved place: do not guess
    (["Oslo, Norway"], None),                                                                            # resolves, but has no code in the table
])
def test_smartrecruiters_country_param_only_for_exactly_one_known_country(locations, expected):
    seen = []

    def handler(request: httpx.Request):
        seen.append(request.url)
        return json_response({"content": [], "totalFound": 0})

    boards.fetch("smartrecruiters", "Acme", req(locations=locations), client=mock_client(handler))
    (url,) = seen
    if expected is None:
        assert "country" not in url.params
    else:
        assert url.params["country"] == expected


def test_smartrecruiters_a_multi_country_user_sends_no_country_param(monkeypatch):
    posts = sr_many(2, location="Bengaluru, Karnataka, India") + sr_many(2, location="London, United Kingdom", start_hours=3, base=744000000008000)
    with stubs.StubBoards(data=sr_data(posts)) as stub:
        result = run(monkeypatch, stub, "smartrecruiters", "Acme", locations=["Bengaluru, India", "London, UK"])
    assert result.errors == [] and len(result.jobs) == 4
    ((path, query),) = sr_list_entries(stub)
    assert "country" not in query and query["q"] == ["backend"]


def test_smartrecruiters_pages_stop_at_three(monkeypatch):
    with stubs.StubBoards(data=sr_data(sr_many(450))) as stub:                # a bottomless-style board: 450 fresh postings
        result = run(monkeypatch, stub, "smartrecruiters", "Acme", locations=["Bengaluru, India"], limit=25)
    assert result.errors == [] and result.listed == 300 and len(result.jobs) == 25
    assert [q["offset"] for _, q in sr_list_entries(stub)] == [["0"], ["100"], ["200"]]       # 3 pages, never a 4th
    assert all(q["limit"] == ["100"] for _, q in sr_list_entries(stub))
    assert len(sr_detail_paths(stub)) == 25


def test_smartrecruiters_stops_at_an_old_posting(monkeypatch):
    fresh, old = sr_many(30, step_hours=2), sr_many(200, start_hours=24 * 30, base=744000000007000)    # newest first: 30 fresh, then 30+ days old
    with stubs.StubBoards(data=sr_data(fresh + old)) as stub:
        result = run(monkeypatch, stub, "smartrecruiters", "Acme", locations=["Bengaluru, India"])
    assert result.errors == [] and len(result.jobs) == 25 and result.listed == 100       # one page listed
    assert len(sr_list_entries(stub)) == 1                                    # the first page already reached an old posting: no 2nd page


def test_smartrecruiters_an_old_posting_on_page_two_stops_after_page_two(monkeypatch):
    fresh, old = sr_many(150), sr_many(150, start_hours=24 * 30, base=744000000007000)
    with stubs.StubBoards(data=sr_data(fresh + old)) as stub:
        result = run(monkeypatch, stub, "smartrecruiters", "Acme", locations=["Bengaluru, India"], limit=10)
    assert result.errors == [] and result.listed == 200 and len(result.jobs) == 10
    assert [q["offset"] for _, q in sr_list_entries(stub)] == [["0"], ["100"]]


def test_smartrecruiters_stops_when_offset_reaches_total_found(monkeypatch):
    with stubs.StubBoards(data=sr_data(sr_many(130))) as stub:
        result = run(monkeypatch, stub, "smartrecruiters", "Acme", locations=["Bengaluru, India"])
    assert result.listed == 130 and [q["offset"] for _, q in sr_list_entries(stub)] == [["0"], ["100"]]   # offset 200 >= 130: no third call
    with stubs.StubBoards(data=sr_data(sr_many(100))) as stub:
        run(monkeypatch, stub, "smartrecruiters", "Acme", locations=["Bengaluru, India"])
    assert len(sr_list_entries(stub)) == 1                                    # exactly one full page: offset 100 >= 100


def test_smartrecruiters_stops_on_an_empty_page():
    seen = []

    def handler(request: httpx.Request):
        seen.append(request.url.params["offset"])
        if request.url.path.endswith("/postings"):
            page = [stubs.sr_job(id=744000000006000 + len(seen) * 10 + i) for i in range(3)] if len(seen) == 1 else []
            return json_response({"offset": 0, "limit": 100, "totalFound": 5000, "content": page})          # a totalFound that lies
        return json_response(stubs.sr_detail(stubs.sr_job(id=1)))

    result = boards.fetch("smartrecruiters", "Acme", req(limit=0), client=mock_client(handler))
    assert result.errors == [] and seen == ["0", "100"]                       # page 2 came back empty: stop


def test_smartrecruiters_detail_calls_are_capped_at_the_limit_and_newest_first(monkeypatch):
    posts = sr_many(40)
    with stubs.StubBoards(data=sr_data(posts[::-1])) as stub:                 # served oldest first: the adapter sorts the survivors
        result = run(monkeypatch, stub, "smartrecruiters", "Acme", locations=["Bengaluru, India"], limit=4)
    assert len(sr_detail_paths(stub)) == 4
    assert [j["source_job_id"] for j in result.jobs] == [f"Acme:{744000000009000 + i}" for i in range(4)]     # the 4 newest


def test_smartrecruiters_detail_only_for_relevant(monkeypatch):
    relevant = sr_many(3, base=744000000005000)
    noise = ([stubs.sr_job("Marketing Manager", "Bengaluru, Karnataka, India", ago(days=1), 744000000004000 + i) for i in range(4)]
             + [stubs.sr_job("Backend Engineer", "London, United Kingdom", ago(days=1), 744000000003000 + i) for i in range(3)])
    with stubs.StubBoards(data=sr_data(noise[:2] + relevant + noise[2:])) as stub:
        result = run(monkeypatch, stub, "smartrecruiters", "Acme")           # no country given by the user: all postings come back
    assert result.listed >= 3 and result.relevant == 3 and len(result.jobs) == 3 and result.errors == []
    assert sorted(sr_detail_paths(stub)) == [f"/smartrecruiters/v1/companies/Acme/postings/{744000000005000 + i}" for i in range(3)]


def test_smartrecruiters_limit_zero_makes_no_detail_calls(monkeypatch):
    with stubs.StubBoards(data=sr_data(sr_many(3))) as stub:
        result = run(monkeypatch, stub, "smartrecruiters", "Acme", limit=0)
    assert result.jobs == [] and result.errors == [] and sr_detail_paths(stub) == []


def test_smartrecruiters_keeps_slug_case(monkeypatch):
    with stubs.StubBoards(data=sr_data(sr_many(2, board="BoschGroup"), slug="BoschGroup")) as stub:
        result = run(monkeypatch, stub, "smartrecruiters", "BoschGroup")
    assert result.errors == [] and len(result.jobs) == 2
    assert all(j["source_job_id"].startswith("BoschGroup:") for j in result.jobs)
    assert stub.log and all("/smartrecruiters/v1/companies/BoschGroup/postings" in path for _, path, _ in stub.log)   # never lower-cased


def test_smartrecruiters_remote_flags_set_is_remote(monkeypatch):
    remote = stubs.sr_job("Backend Engineer", "Remote", ago(days=1), 744000000002001, remote=True)
    hybrid = stubs.sr_job("Backend Engineer", "Bengaluru, India", ago(days=1), 744000000002002)
    hybrid["location"]["hybrid"] = True
    plain = stubs.sr_job("Backend Engineer", "Bengaluru, India", ago(days=1), 744000000002003)
    with stubs.StubBoards(data=sr_data([remote, hybrid, plain])) as stub:
        result = run(monkeypatch, stub, "smartrecruiters", "Acme")
    flags = {j["source_job_id"]: j["is_remote"] for j in result.jobs}
    assert flags == {"Acme:744000000002001": True, "Acme:744000000002002": False, "Acme:744000000002003": False}


def test_smartrecruiters_never_follows_urls_from_the_response(monkeypatch):
    post = stubs.sr_job(id=744000000001001)
    post["ref"] = "https://evil.example/v1/companies/Acme"
    post["_detail"] = stubs.sr_detail(post)
    post["_detail"]["ref"] = "https://evil.example/x"
    with stubs.StubBoards(data=sr_data([post])) as stub:
        result = run(monkeypatch, stub, "smartrecruiters", "Acme")
    assert result.errors == [] and len(result.jobs) == 1
    assert all(path.startswith("/smartrecruiters/") for _, path, _ in stub.log)       # only the stub was ever called


def test_smartrecruiters_company_comes_from_the_payload_else_config_else_slug(monkeypatch):
    with stubs.StubBoards(data=sr_data([stubs.sr_job(id=744000000001002, company="Bosch Group GmbH")], slug="BoschGroup")) as stub:
        assert run(monkeypatch, stub, "smartrecruiters", "BoschGroup").jobs[0]["company"] == "Bosch Group GmbH"
    nameless = stubs.sr_job(id=744000000001003)
    nameless["company"] = {"identifier": "BoschGroup"}                        # no company name in the list ...
    detail = stubs.sr_detail(stubs.sr_job(id=744000000001003, board="BoschGroup"))
    detail["company"] = {"identifier": "BoschGroup"}                          # ... nor in the detail
    nameless["_detail"] = detail
    with stubs.StubBoards(data=sr_data([nameless], slug="BoschGroup")) as stub:
        assert run(monkeypatch, stub, "smartrecruiters", "BoschGroup", name="Bosch (config)").jobs[0]["company"] == "Bosch (config)"
        assert run(monkeypatch, stub, "smartrecruiters", "BoschGroup").jobs[0]["company"] == "Boschgroup"


def test_smartrecruiters_detail_failure_skips_that_job_and_reports_it():
    posts = [stubs.sr_job("Backend Engineer", "Bengaluru, India", ago(days=1, hours=i), 744000000000100 + i) for i in range(4)]

    def handler(request: httpx.Request):
        path = request.url.path
        if path.endswith("/postings"):
            return json_response({"offset": 0, "limit": 100, "totalFound": 4, "content": posts})
        pid = path.rsplit("/", 1)[1]
        if pid.endswith("101"):
            return httpx.Response(404, text="gone")
        if pid.endswith("102"):
            return httpx.Response(500, text="boom")
        if pid.endswith("103"):
            return httpx.Response(200, text="<html>not json</html>")
        return json_response(stubs.sr_detail(posts[0]))

    result = boards.fetch("smartrecruiters", "Acme", req(), client=mock_client(handler))
    assert result.errors == [] and result.listed == 4
    assert [j["source_job_id"] for j in result.jobs] == ["Acme:744000000000100"]


def test_smartrecruiters_a_detail_without_a_posting_url_drops_that_job():
    posts = [stubs.sr_job("Backend Engineer", "Bengaluru, India", ago(days=1, hours=i), 744000000000200 + i) for i in range(2)]

    def handler(request: httpx.Request):
        if request.url.path.endswith("/postings"):
            return json_response({"offset": 0, "limit": 100, "totalFound": 2, "content": posts})
        detail = stubs.sr_detail(posts[0])
        if request.url.path.endswith("201"):
            del detail["postingUrl"]
        return json_response(detail)

    result = boards.fetch("smartrecruiters", "Acme", req(), client=mock_client(handler))
    assert result.errors == [] and [j["source_job_id"] for j in result.jobs] == ["Acme:744000000000200"]


def test_smartrecruiters_every_detail_failing_is_one_error_line():
    posts = [stubs.sr_job("Backend Engineer", "Bengaluru, India", ago(days=1, hours=i), 744000000000300 + i) for i in range(3)]

    def handler(request: httpx.Request):
        if request.url.path.endswith("/postings"):
            return json_response({"offset": 0, "limit": 100, "totalFound": 3, "content": posts})
        return httpx.Response(404, text="gone")

    result = boards.fetch("smartrecruiters", "Acme", req(), client=mock_client(handler))
    assert result.jobs == [] and len(result.errors) == 1 and result.errors[0].startswith("smartrecruiters/Acme: ")


def test_smartrecruiters_a_rate_limited_detail_stops_the_board():
    calls = []

    def handler(request: httpx.Request):
        calls.append(request.url.path)
        if request.url.path.endswith("/postings"):
            return json_response({"offset": 0, "limit": 100, "totalFound": 3, "content": [stubs.sr_job(id=744000000000400 + i) for i in range(3)]})
        return httpx.Response(429, text="slow down")

    result = boards.fetch("smartrecruiters", "Acme", req(), client=mock_client(handler))
    assert result.jobs == [] and result.errors == ["smartrecruiters/Acme: rate limited by the source; skipped for this run"]
    assert len(calls) == 2


def test_smartrecruiters_a_rate_limited_list_page_stops_the_board():
    def handler(request: httpx.Request):
        if request.url.params["offset"] == "0":
            return json_response({"offset": 0, "limit": 100, "totalFound": 500, "content": [stubs.sr_job(id=744000000000500 + i) for i in range(100)]})
        return httpx.Response(429, text="slow down")

    result = boards.fetch("smartrecruiters", "Acme", req(), client=mock_client(handler))
    assert result.jobs == [] and result.errors == ["smartrecruiters/Acme: rate limited by the source; skipped for this run"]


def test_smartrecruiters_a_failing_later_page_keeps_what_page_one_gave():
    def handler(request: httpx.Request):
        if request.url.path.endswith("/postings"):
            if request.url.params["offset"] == "0":
                return json_response({"offset": 0, "limit": 100, "totalFound": 500, "content": [stubs.sr_job(id=744000000000600 + i) for i in range(100)]})
            return httpx.Response(200, text="<html>not json</html>")
        return json_response(stubs.sr_detail(stubs.sr_job(id=744000000000600)))

    result = boards.fetch("smartrecruiters", "Acme", req(limit=1), client=mock_client(handler))
    assert result.errors == [] and len(result.jobs) == 1 and result.listed == 100


def test_smartrecruiters_empty_board_is_not_an_error(monkeypatch):
    with stubs.StubBoards(modes={("smartrecruiters", "Acme"): "empty"}) as stub:
        result = run(monkeypatch, stub, "smartrecruiters", "Acme")
    assert result.jobs == [] and result.errors == [] and result.listed == 0 and sr_detail_paths(stub) == []


def test_smartrecruiters_garbage_body_is_one_error_not_a_crash(monkeypatch):
    with stubs.StubBoards(modes={("smartrecruiters", "Acme"): "garbage"}) as stub:
        result = run(monkeypatch, stub, "smartrecruiters", "Acme")
    assert result.jobs == [] and result.errors == ["smartrecruiters/Acme: returned something that is not JSON"]


def test_smartrecruiters_unexpected_shape_is_one_error():
    for payload in ({"content": "nope"}, {"other": []}, ["a list"], None):
        result = boards.fetch("smartrecruiters", "Acme", req(), client=mock_client(lambda r, p=payload: json_response(p)))
        assert result.jobs == [] and len(result.errors) == 1 and "unexpected format" in result.errors[0], payload


def test_smartrecruiters_missing_fields_are_skipped(monkeypatch):
    good = stubs.sr_job(id=744000000000701)
    no_id, no_name, no_date = stubs.sr_job(id=744000000000702), stubs.sr_job(id=744000000000703), stubs.sr_job(id=744000000000704)
    del no_id["id"]
    no_name["name"] = ""
    del no_date["releasedDate"]                                               # an unknown date counts as within age: kept
    with stubs.StubBoards(data=sr_data([no_id, no_name, no_date, good])) as stub:
        result = run(monkeypatch, stub, "smartrecruiters", "Acme")
    assert result.errors == [] and sorted(j["source_job_id"] for j in result.jobs) == ["Acme:744000000000701", "Acme:744000000000704"]
    assert len(sr_detail_paths(stub)) == 2                                    # an unusable posting costs no detail call
    assert result.dropped["unusable"] == 0


def test_smartrecruiters_entries_that_are_not_objects_are_skipped():
    good = stubs.sr_job(id=744000000000801)

    def handler(request: httpx.Request):
        if request.url.path.endswith("/postings"):
            return json_response({"offset": 0, "limit": 100, "totalFound": 6, "content": ["a string", 5, None, [], {}, good]})
        return json_response(stubs.sr_detail(good))

    result = boards.fetch("smartrecruiters", "Acme", req(), client=mock_client(handler))
    assert result.errors == [] and [j["source_job_id"] for j in result.jobs] == ["Acme:744000000000801"]


def test_smartrecruiters_not_found_is_one_error(monkeypatch):
    with stubs.StubBoards(modes={("smartrecruiters", "Acme"): "not_found"}) as stub:
        result = run(monkeypatch, stub, "smartrecruiters", "Acme")
    assert result.jobs == [] and result.errors == ["smartrecruiters/Acme: not found (check the slug)"]


# ---- both -----------------------------------------------------------------------------------------------

BUILDERS = {"workable": stubs.workable_job, "smartrecruiters": stubs.sr_job}


@pytest.mark.parametrize("system", sorted(BUILDERS))
def test_adapters_are_registered(system):
    assert callable(boards.ADAPTERS.get(system))


@pytest.mark.parametrize("system", sorted(BUILDERS))
def test_slug_case_is_preserved_in_every_path(monkeypatch, system):
    post = BUILDERS[system](id="CASE1" if system == "workable" else 744000000000901)
    with stubs.StubBoards(data={(system, "AcmeCo"): [post]}) as stub:
        result = run(monkeypatch, stub, system, "AcmeCo")
    assert result.errors == [] and result.jobs[0]["source_job_id"].startswith("AcmeCo:")
    assert len(stub.log) == 2 and all("/AcmeCo/" in path or path.endswith("/AcmeCo") for _, path, _ in stub.log)


@pytest.mark.parametrize("system", sorted(BUILDERS))
def test_a_missing_company_is_one_error(monkeypatch, system):
    with stubs.StubBoards(modes={(system, "nobody"): "not_found"}) as stub:
        result = run(monkeypatch, stub, system, "nobody")
    assert result.jobs == [] and result.errors == [f"{system}/nobody: not found (check the slug)"]


# ---- hardening batch (Task 9b): SmartRecruiters paging/ids and the shared detail loop ---------------------------

def sr_server(page_for, *, total):
    """A mock SmartRecruiters whose list pages come from `page_for(offset, limit)`; a detail request is answered from the postings
    it has served. Returns (client, list_calls, detail_ids)."""
    served: dict = {}
    list_calls: list = []
    detail_ids: list = []

    def handler(request: httpx.Request):
        if request.url.path.endswith("/postings"):
            offset, limit = int(request.url.params["offset"]), int(request.url.params["limit"])
            list_calls.append((offset, limit))
            page = page_for(offset, limit)
            for p in page:
                served[p["id"]] = p
            return json_response({"offset": offset, "limit": limit, "totalFound": total, "content": page})
        pid = request.url.path.rsplit("/", 1)[1]
        detail_ids.append(pid)
        return json_response(stubs.sr_detail(served[pid]))
    return mock_client(handler), list_calls, detail_ids


def test_smartrecruiters_a_server_that_ignores_offset_yields_each_posting_once():
    page = sr_many(100)
    client, list_calls, detail_ids = sr_server(lambda offset, limit: page, total=5000)
    result = boards.fetch("smartrecruiters", "Acme", req(limit=100, locations=["Bengaluru, India"]), client=client)
    assert result.errors == [] and len(list_calls) == 3                       # the 3-page cap still holds
    ids = [j["source_job_id"] for j in result.jobs]
    assert len(ids) == 100 and len(set(ids)) == 100                          # no posting twice
    assert len(detail_ids) == 100 and len(set(detail_ids)) == 100            # one detail call per unique survivor


@pytest.mark.parametrize("bad", ["..", ".", "a/b", "", "a b", "x" * 65, "id?x=1", "%2e%2e", "a\\b"])
def test_smartrecruiters_an_unsafe_id_is_skipped_and_costs_no_detail_call(bad):
    good = stubs.sr_job(id=744000000001001)
    evil = stubs.sr_job(id=744000000001002)
    evil["id"] = bad
    client, list_calls, detail_ids = sr_server(lambda offset, limit: [evil, good], total=2)
    result = boards.fetch("smartrecruiters", "Acme", req(), client=client)
    assert result.errors == [] and [j["source_job_id"] for j in result.jobs] == ["Acme:744000000001001"]
    assert detail_ids == ["744000000001001"]


def test_smartrecruiters_id_characters_and_numeric_ids_are_accepted():
    ok = [stubs.sr_job(id="abc_DEF-123"), stubs.sr_job(id="a" * 64), stubs.sr_job(id=744000000001003)]
    client, _, detail_ids = sr_server(lambda offset, limit: ok, total=3)
    result = boards.fetch("smartrecruiters", "Acme", req(), client=client)
    assert len(result.jobs) == 3 and sorted(detail_ids) == sorted(["abc_DEF-123", "a" * 64, "744000000001003"])


def test_each_detail_drops_only_the_job_whose_build_raises_any_exception(caplog):
    def build(item, detail):
        if item == 2:
            raise TypeError("bad shape")
        if item == 4:
            raise KeyError("missing")
        return {"id": item}
    client = mock_client(lambda request: json_response({"ok": True}))
    jobs = boards._each_detail(client, "x/y", [1, 2, 3, 4, 5], lambda item: f"{boards.base_url('workable')}/{item}", build)
    assert [j["id"] for j in jobs] == [1, 3, 5]


def test_each_detail_still_propagates_a_rate_limit_and_still_fails_when_everything_fails():
    client = mock_client(lambda request: json_response({"ok": True}))

    def limited(item, detail):
        raise boards.RateLimited("rate limited (HTTP 429)")
    with pytest.raises(boards.RateLimited):
        boards._each_detail(client, "x/y", [1, 2], lambda item: f"{boards.base_url('workable')}/{item}", limited)

    def broken(item, detail):
        raise AttributeError("nope")
    with pytest.raises(boards.BoardError, match="none of the 2 job-detail requests worked"):
        boards._each_detail(client, "x/y", [1, 2], lambda item: f"{boards.base_url('workable')}/{item}", broken)


def test_workable_and_smartrecruiters_drop_a_job_whose_mapping_raises(monkeypatch):
    real = boards.make_job

    def flaky(source, source_job_id, **kw):
        if source_job_id.endswith(":BAD"):
            raise TypeError("hostile payload")
        return real(source, source_job_id, **kw)
    monkeypatch.setattr(boards, "make_job", flaky)
    posts = [stubs.workable_job("Backend Engineer", "Bengaluru, India", ago(days=1), "GOOD"),
             stubs.workable_job("Backend Engineer", "Bengaluru, India", ago(days=2), "BAD")]
    with stubs.StubBoards(data=wk_data(posts)) as stub:
        result = run(monkeypatch, stub, "workable")
    assert result.errors == [] and [j["source_job_id"] for j in result.jobs] == ["acme:GOOD"]
