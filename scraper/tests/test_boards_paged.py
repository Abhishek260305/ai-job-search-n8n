"""The two paged remote-feed adapters in boards.py: Himalayas and Arbeitnow (spec sections 2, 5 item 6 and 6).

Both are huge newest-first feeds (Himalayas lists ~118,000 jobs, 20 a page; Arbeitnow serves 325 jobs and ~2.8 MB a page),
so the paging stop rules are what these tests are about: never a 4th page, and stop at the page in which the first
posting older than max_age_days appears. The StubBoards `bottomless` option serves endless newest-first pages whose dates
step back by a chosen number of hours, and its `.log` proves exactly which requests were made. Per-request failures use an
in-process mock transport. Every test goes through boards.fetch(), as production does."""
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

import httpx
import pytest

import boards
import stubs

POSTED = datetime(2026, 10, 9, 8, 30, 0, tzinfo=timezone.utc)              # old enough that tests raise max_age_days
HIM = "/himalayas/jobs/api"
ARB = "/arbeitnow/api/job-board-api"


@pytest.fixture(autouse=True)
def _fast_retry(monkeypatch):
    monkeypatch.setattr(boards, "RETRY_DELAY_S", 0)


def req(**kw):
    base = dict(terms=["backend engineer"], locations=["Bengaluru"], allow_remote=True, max_age_days=3650)
    base.update(kw)
    return boards.BoardRequest(**base)


def run(monkeypatch, stub, system, **kw):
    """boards.fetch of a feed against the stub (the BOARDS_API_URL hook points every system at it)."""
    monkeypatch.setenv("BOARDS_API_URL", stub.url)
    return boards.fetch(system, None, req(**kw))


def feed(system, items):
    return stubs.StubBoards(data={(system, ""): items})


def pages_of(handler_pages, status_after=None):
    """A mock client serving the given payloads one per request; a request past the end gets `status_after` (or fails the test)."""
    seen = []

    def handler(request):
        seen.append(request)
        n = len(seen) - 1
        if n < len(handler_pages):
            item = handler_pages[n]
            return item if isinstance(item, httpx.Response) else httpx.Response(200, content=json.dumps(item).encode())
        if status_after is not None:
            return httpx.Response(status_after)
        raise AssertionError(f"unexpected request #{n + 1}: {request.url}")
    return httpx.Client(transport=httpx.MockTransport(handler)), seen


def him_page(jobs, cursor="c2"):
    return {"jobs": jobs, "nextCursor": cursor, "totalCount": 118348}


def arb_page(jobs, more=True):
    return {"data": jobs, "links": {"next": "https://evil.example/api/job-board-api?page=2" if more else None}, "meta": {}}


def query_of(entry):
    return {k: v[0] for k, v in parse_qs(entry[2], keep_blank_values=True).items()}


# ---- Himalayas --------------------------------------------------------------------------------------------

def test_himalayas_maps_a_job(monkeypatch):
    post = stubs.himalayas_job("Backend Engineer", ["Germany", "Poland"], POSTED, "g-77", company="Acme Corp",
                               description="<p>Build the <b>API</b>.</p><ul><li>Python</li></ul>")
    with feed("himalayas", [post]) as stub:
        result = run(monkeypatch, stub, "himalayas", locations=["Germany"])
    assert result.errors == [] and result.listed == 1 and result.relevant == 1
    (job,) = result.jobs
    assert job["source"] == "himalayas" and job["source_job_id"] == post["guid"]
    assert job["title"] == "Backend Engineer" and job["company"] == "Acme Corp"
    assert job["url"] == post["applicationLink"] and job["apply_url"] == post["applicationLink"]
    assert job["url"].startswith("https://himalayas.app/")                         # the terms: link back to Himalayas
    assert job["location"] == "Germany, Poland" and job["is_remote"] is True
    assert job["posted_at"] == "2026-10-09T08:30:00+00:00"                          # from pubDate (epoch seconds)
    assert job["description"] == "Build the API.\nPython"
    assert job["salary_min"] == 65000 and job["salary_max"] == 90000 and job["salary_currency"] == "USD"
    assert job["raw"]["guid"] == post["guid"] and job["raw"]["applicationLink"] == post["applicationLink"]
    assert job["raw"]["pubDate"] == post["pubDate"] and job["raw"]["locationRestrictions"] == ["Germany", "Poland"]
    assert "description" not in job["raw"] and "Build the" not in json.dumps(job["raw"])


def test_himalayas_empty_restrictions_mean_worldwide(monkeypatch):
    with feed("himalayas", [stubs.himalayas_job("Backend Engineer", [], POSTED, 1)]) as stub:
        result = run(monkeypatch, stub, "himalayas")
    assert result.errors == [] and result.jobs[0]["location"] == "Worldwide" and result.jobs[0]["is_remote"] is True


def test_himalayas_odd_but_usable_fields():
    post = stubs.himalayas_job("Backend Engineer", [], POSTED, 2)
    post["pubDate"] = str(post["pubDate"])                                          # the real feed sends numeric strings too
    post["minSalary"], post["maxSalary"] = "65000", None
    post["description"] = ""                                                         # falls back to the excerpt
    del post["guid"]                                                                 # falls back to the application link
    client, _ = pages_of([{"jobs": [post], "nextCursor": None}])
    result = boards.fetch("himalayas", None, req(), client=client)
    assert result.errors == []
    (job,) = result.jobs
    assert job["source_job_id"] == post["applicationLink"] and job["posted_at"] == "2026-10-09T08:30:00+00:00"
    assert job["salary_min"] == 65000 and job["salary_max"] is None and job["salary_currency"] == "USD"
    assert job["description"] == "Acme is hiring a Backend Engineer."


def test_himalayas_no_salary_means_no_currency():
    post = stubs.himalayas_job("Backend Engineer", [], POSTED, 3)
    post["minSalary"] = post["maxSalary"] = None
    client, _ = pages_of([{"jobs": [post], "nextCursor": None}])
    (job,) = boards.fetch("himalayas", None, req(), client=client).jobs
    assert job["salary_min"] is None and job["salary_max"] is None and job["salary_currency"] is None


def test_himalayas_stops_after_three_pages(monkeypatch):
    with stubs.StubBoards(bottomless={"himalayas": 1}) as stub:                     # one job an hour: 60 jobs = 60 h, nothing old
        result = run(monkeypatch, stub, "himalayas", max_age_days=7)
    assert result.errors == [] and result.listed == 60
    assert stub.requests == 3                                                       # never a 4th page, however many jobs remain
    assert [path for _, path, _ in stub.log] == [HIM] * 3
    assert len(result.jobs) == 15                                                   # 1 of 4 stub titles is a backend engineer


def test_himalayas_stops_at_the_first_old_job_on_page_one(monkeypatch):
    with stubs.StubBoards(bottomless={"himalayas": 12}) as stub:                    # 12 h a job: the 5th is two days old
        result = run(monkeypatch, stub, "himalayas", max_age_days=1)
    assert result.errors == [] and stub.requests == 1


def test_himalayas_stops_after_the_page_with_the_first_old_job(monkeypatch):
    with stubs.StubBoards(bottomless={"himalayas": 2}) as stub:                     # page 1 spans 0-38 h, page 2 reaches 48 h
        result = run(monkeypatch, stub, "himalayas", max_age_days=1)
    assert result.errors == [] and stub.requests == 2
    assert all(j["posted_at"] for j in result.jobs)                                 # the old ones are still returned: fetch() filters by age
    assert result.dropped["age"] > 0


def test_himalayas_uses_the_cursor_not_offset(monkeypatch):
    with stubs.StubBoards(bottomless={"himalayas": 1}) as stub:
        run(monkeypatch, stub, "himalayas", max_age_days=7)
    first, second, third = (query_of(e) for e in stub.log)
    assert first == {"limit": "20"}                                                 # page 1: no cursor yet
    assert second["limit"] == "20" and second["cursor"] and "offset" not in second
    assert third["cursor"] and third["cursor"] != second["cursor"] and "offset" not in third
    assert all("offset" not in e[2] for e in stub.log)


def test_himalayas_stops_when_there_is_no_next_cursor(monkeypatch):
    with feed("himalayas", [stubs.himalayas_job("Backend Engineer", [], POSTED, i) for i in range(1, 4)]) as stub:
        result = run(monkeypatch, stub, "himalayas")
    assert result.errors == [] and result.listed == 3 and stub.requests == 1


@pytest.mark.parametrize("cursor", ["x" * 201, 12345, {"a": 1}, ["c"], "", None])
def test_himalayas_a_bad_cursor_ends_paging(cursor):
    client, seen = pages_of([him_page([stubs.himalayas_job("Backend Engineer", [], POSTED, 1)], cursor)])
    result = boards.fetch("himalayas", None, req(), client=client)
    assert result.errors == [] and len(result.jobs) == 1 and len(seen) == 1


def test_himalayas_the_cursor_is_passed_as_a_query_parameter_and_urls_are_never_followed(monkeypatch):
    monkeypatch.setenv("BOARDS_API_URL", "http://stub.invalid")
    evil = "a&b=c d/../?x"                                                          # opaque, odd but short: must stay one escaped parameter
    page1 = him_page([stubs.himalayas_job("Backend Engineer", [], POSTED, 1)], evil)
    page1["links"] = {"next": "https://evil.example/jobs/api?cursor=zzz"}
    client, seen = pages_of([page1, him_page([], None)])
    result = boards.fetch("himalayas", None, req(), client=client)
    assert result.errors == [] and len(seen) == 2
    assert {r.url.host for r in seen} == {"stub.invalid"} and {r.url.path for r in seen} == {HIM}
    assert dict(seen[1].url.params) == {"limit": "20", "cursor": evil}


def test_himalayas_empty_feed_is_not_an_error(monkeypatch):
    with stubs.StubBoards(modes={("himalayas", ""): "empty"}) as stub:
        result = run(monkeypatch, stub, "himalayas")
    assert result.jobs == [] and result.errors == [] and result.listed == 0 and stub.requests == 1


def test_himalayas_garbage_body_is_one_error_not_a_crash(monkeypatch):
    with stubs.StubBoards(modes={("himalayas", ""): "garbage"}) as stub:
        result = run(monkeypatch, stub, "himalayas")
    assert result.jobs == [] and result.errors == ["himalayas: returned something that is not JSON"]


def test_himalayas_rate_limit_is_one_error_and_one_request(monkeypatch):
    with stubs.StubBoards(modes={("himalayas", ""): "rate_limited"}) as stub:
        result = run(monkeypatch, stub, "himalayas")
    assert result.jobs == [] and result.errors == ["himalayas: rate limited by the source; skipped for this run"]
    assert stub.requests == 1


def test_himalayas_unexpected_shape_is_one_error():
    for payload in ([], {"other": 1}, {"jobs": "nope"}, None):
        client, _ = pages_of([payload])
        result = boards.fetch("himalayas", None, req(), client=client)
        assert result.jobs == [] and len(result.errors) == 1 and "unexpected format" in result.errors[0], payload


def test_himalayas_missing_fields_are_skipped():
    good = stubs.himalayas_job("Backend Engineer", [], POSTED, 5)
    no_id, no_title, no_url = (stubs.himalayas_job("Backend Engineer", [], POSTED, i) for i in (1, 2, 3))
    del no_id["guid"], no_id["applicationLink"]
    no_title["title"] = ""
    del no_url["applicationLink"]
    client, _ = pages_of([{"jobs": ["junk", None, 7, no_id, no_title, no_url, good], "nextCursor": None}])
    result = boards.fetch("himalayas", None, req(), client=client)
    assert result.errors == [] and [j["source_job_id"] for j in result.jobs] == [good["guid"]]
    assert result.dropped["unusable"] == 0


def test_himalayas_a_bad_date_is_none_not_an_error():
    post = stubs.himalayas_job("Backend Engineer", [], POSTED, 9)
    post["pubDate"] = "soon"
    client, _ = pages_of([{"jobs": [post], "nextCursor": None}])
    result = boards.fetch("himalayas", None, req(), client=client)
    assert result.errors == [] and result.jobs[0]["posted_at"] is None


def test_himalayas_a_page_two_failure_keeps_page_one():
    one = him_page([stubs.himalayas_job("Backend Engineer", [], POSTED, 1)])
    client, seen = pages_of([one], status_after=500)
    result = boards.fetch("himalayas", None, req(), client=client)
    assert result.errors == [] and len(result.jobs) == 1 and result.listed == 1
    assert len(seen) == 3                                                           # page 2, retried once (a 5xx), then given up


def test_himalayas_a_page_one_failure_is_one_error():
    client, seen = pages_of([], status_after=500)
    result = boards.fetch("himalayas", None, req(), client=client)
    assert result.jobs == [] and result.errors == ["himalayas: server error (HTTP 500) (tried twice)"]


def test_himalayas_a_rate_limit_on_page_two_stops_the_feed():
    one = him_page([stubs.himalayas_job("Backend Engineer", [], POSTED, 1)])
    client, seen = pages_of([one], status_after=429)
    result = boards.fetch("himalayas", None, req(), client=client)
    assert result.jobs == [] and result.errors == ["himalayas: rate limited by the source; skipped for this run"]
    assert len(seen) == 2                                                           # a 429 is never retried and never followed by page 3


def test_himalayas_deduplicates_across_pages():
    job = stubs.himalayas_job("Backend Engineer", [], POSTED, 1)                    # the feed shifted while paging: page 2 repeats it
    other = stubs.himalayas_job("Backend Engineer", [], POSTED, 2)
    client, _ = pages_of([him_page([job]), him_page([job, other], None)])
    result = boards.fetch("himalayas", None, req(), client=client)
    assert result.errors == [] and sorted(j["source_job_id"] for j in result.jobs) == sorted([job["guid"], other["guid"]])
    assert result.listed == 2


# ---- Arbeitnow --------------------------------------------------------------------------------------------

def test_arbeitnow_maps_a_job(monkeypatch):
    post = stubs.arbeitnow_job("Backend Engineer", "Berlin, Germany", POSTED, "backend-engineer-acme-991", company="Acme GmbH",
                               remote=True, description="<p>Build the <b>API</b>.</p><ul><li>Python</li></ul>")
    with feed("arbeitnow", [post]) as stub:
        result = run(monkeypatch, stub, "arbeitnow", locations=["Berlin"])
    assert result.errors == [] and result.listed == 1 and result.relevant == 1
    (job,) = result.jobs
    assert job["source"] == "arbeitnow" and job["source_job_id"] == "backend-engineer-acme-991"
    assert job["title"] == "Backend Engineer" and job["company"] == "Acme GmbH"
    assert job["url"] == post["url"] and job["apply_url"] == post["url"]
    assert job["url"].startswith("https://www.arbeitnow.com/")                      # the terms: link back to Arbeitnow
    assert job["location"] == "Berlin, Germany" and job["is_remote"] is True
    assert job["posted_at"] == "2026-10-09T08:30:00+00:00"                          # from created_at (epoch seconds)
    assert job["description"] == "Build the API.\nPython"
    assert job["salary_min"] is None and job["salary_max"] is None and job["salary_currency"] is None
    assert job["raw"]["slug"] == "backend-engineer-acme-991" and job["raw"]["url"] == post["url"]
    assert job["raw"]["created_at"] == post["created_at"] and job["raw"]["location"] == "Berlin, Germany"
    assert job["raw"]["remote"] is True and "description" not in job["raw"] and "Build the" not in json.dumps(job["raw"])


def test_arbeitnow_the_remote_flag_is_used_as_given(monkeypatch):
    on_site = stubs.arbeitnow_job("Backend Engineer", "Berlin, Germany", POSTED, "a-1", remote=False)
    unknown = stubs.arbeitnow_job("Backend Engineer", "Berlin, Germany", POSTED, "a-2")
    del unknown["remote"]
    with feed("arbeitnow", [on_site, unknown]) as stub:
        result = run(monkeypatch, stub, "arbeitnow", locations=["Berlin"])
    assert {j["source_job_id"]: j["is_remote"] for j in result.jobs} == {"a-1": False, "a-2": None}


def test_arbeitnow_a_numeric_slug_is_stringified():
    post = stubs.arbeitnow_job("Backend Engineer", "Berlin, Germany", POSTED, 12345)
    client, _ = pages_of([arb_page([post], more=False)])
    result = boards.fetch("arbeitnow", None, req(locations=["Berlin"]), client=client)
    assert [j["source_job_id"] for j in result.jobs] == ["12345"]


def test_arbeitnow_stops_after_three_pages(monkeypatch):
    with stubs.StubBoards(bottomless={"arbeitnow": 1}) as stub:                     # 975 jobs at an hour apart: ~40 days, nothing old
        result = run(monkeypatch, stub, "arbeitnow", max_age_days=365, locations=["Berlin"])
    assert result.errors == [] and result.listed == 3 * 325
    assert stub.requests == 3                                                       # never a 4th page
    assert [(e[1], query_of(e)) for e in stub.log] == [(ARB, {"page": str(n)}) for n in (1, 2, 3)]


def test_arbeitnow_stops_at_the_first_old_job_on_page_one(monkeypatch):
    with stubs.StubBoards(bottomless={"arbeitnow": 1}) as stub:                     # the 49th job is two days old, on page 1
        result = run(monkeypatch, stub, "arbeitnow", max_age_days=1, locations=["Berlin"])
    assert result.errors == [] and stub.requests == 1


def test_arbeitnow_stops_after_the_page_with_the_first_old_job(monkeypatch):
    with stubs.StubBoards(bottomless={"arbeitnow": 0.1}) as stub:                   # 6 minutes a job: page 1 is 32 h, the 481st job is 48 h old
        result = run(monkeypatch, stub, "arbeitnow", max_age_days=1, locations=["Berlin"])
    assert result.errors == [] and stub.requests == 2 and result.listed == 650
    assert result.dropped["age"] > 0


def test_arbeitnow_never_follows_links_next(monkeypatch):
    monkeypatch.setenv("BOARDS_API_URL", "http://stub.invalid")
    jobs = [stubs.arbeitnow_job("Backend Engineer", "Berlin, Germany", POSTED, f"j-{i}") for i in range(3)]
    client, seen = pages_of([arb_page(jobs[:1]), arb_page(jobs[1:2]), arb_page(jobs[2:])])     # every links.next points at evil.example
    result = boards.fetch("arbeitnow", None, req(locations=["Berlin"]), client=client)
    assert result.errors == [] and len(seen) == 3                                   # 3 pages even though links.next never ends
    assert {r.url.host for r in seen} == {"stub.invalid"} and {r.url.path for r in seen} == {ARB}
    assert [dict(r.url.params) for r in seen] == [{"page": "1"}, {"page": "2"}, {"page": "3"}]


def test_arbeitnow_every_stub_request_stays_under_the_arbeitnow_route(monkeypatch):
    with stubs.StubBoards(bottomless={"arbeitnow": 1}) as stub:                     # the stub's links.next carries an absolute URL
        run(monkeypatch, stub, "arbeitnow", max_age_days=365, locations=["Berlin"])
    assert stub.log and all(method == "GET" and path == ARB for method, path, _ in stub.log)


def test_arbeitnow_stops_when_links_next_says_there_is_no_more(monkeypatch):
    with feed("arbeitnow", [stubs.arbeitnow_job("Backend Engineer", "Berlin, Germany", POSTED, "only")]) as stub:
        result = run(monkeypatch, stub, "arbeitnow", locations=["Berlin"])
    assert result.errors == [] and result.listed == 1 and stub.requests == 1


def test_arbeitnow_germany_jobs_are_dropped_for_an_india_user_by_relevance(monkeypatch):
    posts = [stubs.arbeitnow_job("Backend Engineer", "Berlin, Germany", POSTED, "b-1"),
             stubs.arbeitnow_job("Backend Engineer", "Munich, Bavaria, Germany", POSTED, "m-1"),
             stubs.arbeitnow_job("Backend Engineer", "Hamburg, Germany", POSTED, "h-1", remote=False)]
    with feed("arbeitnow", posts) as stub:
        india = run(monkeypatch, stub, "arbeitnow", locations=["Bengaluru", "India"])
        berlin = run(monkeypatch, stub, "arbeitnow", locations=["Berlin"])
    assert india.errors == [] and india.listed == 3 and india.jobs == [] and india.dropped["location"] == 3
    assert [j["source_job_id"] for j in berlin.jobs] == ["b-1"]                     # the adapter itself adds no location logic


def test_arbeitnow_empty_feed_is_not_an_error(monkeypatch):
    with stubs.StubBoards(modes={("arbeitnow", ""): "empty"}) as stub:
        result = run(monkeypatch, stub, "arbeitnow")
    assert result.jobs == [] and result.errors == [] and result.listed == 0 and stub.requests == 1


def test_arbeitnow_an_empty_page_ends_paging():
    job = stubs.arbeitnow_job("Backend Engineer", "Berlin, Germany", POSTED, "x-1")
    client, seen = pages_of([arb_page([job]), arb_page([])])
    result = boards.fetch("arbeitnow", None, req(locations=["Berlin"]), client=client)
    assert result.errors == [] and len(result.jobs) == 1 and len(seen) == 2


def test_arbeitnow_garbage_body_is_one_error_not_a_crash(monkeypatch):
    with stubs.StubBoards(modes={("arbeitnow", ""): "garbage"}) as stub:
        result = run(monkeypatch, stub, "arbeitnow")
    assert result.jobs == [] and result.errors == ["arbeitnow: returned something that is not JSON"]


def test_arbeitnow_rate_limit_is_one_error_and_one_request(monkeypatch):
    with stubs.StubBoards(modes={("arbeitnow", ""): "rate_limited"}) as stub:
        result = run(monkeypatch, stub, "arbeitnow")
    assert result.jobs == [] and result.errors == ["arbeitnow: rate limited by the source; skipped for this run"]
    assert stub.requests == 1


def test_arbeitnow_unexpected_shape_is_one_error():
    for payload in ([], {"other": 1}, {"data": "nope"}, None):
        client, _ = pages_of([payload])
        result = boards.fetch("arbeitnow", None, req(), client=client)
        assert result.jobs == [] and len(result.errors) == 1 and "unexpected format" in result.errors[0], payload


def test_arbeitnow_missing_fields_are_skipped():
    good = stubs.arbeitnow_job("Backend Engineer", "Berlin, Germany", POSTED, "good-1")
    no_slug, no_title, no_url = (stubs.arbeitnow_job("Backend Engineer", "Berlin, Germany", POSTED, f"s-{i}") for i in (1, 2, 3))
    del no_slug["slug"]
    no_title["title"] = ""
    del no_url["url"]
    client, _ = pages_of([arb_page(["junk", None, 7, no_slug, no_title, no_url, good], more=False)])
    result = boards.fetch("arbeitnow", None, req(locations=["Berlin"]), client=client)
    assert result.errors == [] and [j["source_job_id"] for j in result.jobs] == ["good-1"]
    assert result.dropped["unusable"] == 0


def test_arbeitnow_a_bad_date_is_none_not_an_error():
    post = stubs.arbeitnow_job("Backend Engineer", "Berlin, Germany", POSTED, "d-1")
    post["created_at"] = "soon"
    client, _ = pages_of([arb_page([post], more=False)])
    result = boards.fetch("arbeitnow", None, req(locations=["Berlin"]), client=client)
    assert result.errors == [] and result.jobs[0]["posted_at"] is None


def test_arbeitnow_a_page_two_failure_keeps_page_one():
    one = arb_page([stubs.arbeitnow_job("Backend Engineer", "Berlin, Germany", POSTED, "p-1")])
    client, seen = pages_of([one], status_after=500)
    result = boards.fetch("arbeitnow", None, req(locations=["Berlin"]), client=client)
    assert result.errors == [] and [j["source_job_id"] for j in result.jobs] == ["p-1"] and result.listed == 1
    assert len(seen) == 3


def test_arbeitnow_a_page_one_failure_is_one_error():
    client, _ = pages_of([], status_after=500)
    result = boards.fetch("arbeitnow", None, req(), client=client)
    assert result.jobs == [] and result.errors == ["arbeitnow: server error (HTTP 500) (tried twice)"]


def test_arbeitnow_a_rate_limit_on_page_two_stops_the_feed():
    one = arb_page([stubs.arbeitnow_job("Backend Engineer", "Berlin, Germany", POSTED, "p-1")])
    client, seen = pages_of([one], status_after=429)
    result = boards.fetch("arbeitnow", None, req(locations=["Berlin"]), client=client)
    assert result.jobs == [] and result.errors == ["arbeitnow: rate limited by the source; skipped for this run"]
    assert len(seen) == 2


def test_arbeitnow_deduplicates_across_pages():
    job = stubs.arbeitnow_job("Backend Engineer", "Berlin, Germany", POSTED, "dup-1")
    other = stubs.arbeitnow_job("Backend Engineer", "Berlin, Germany", POSTED, "dup-2")
    client, _ = pages_of([arb_page([job]), arb_page([job, other], more=False)])
    result = boards.fetch("arbeitnow", None, req(locations=["Berlin"]), client=client)
    assert result.errors == [] and sorted(j["source_job_id"] for j in result.jobs) == ["dup-1", "dup-2"] and result.listed == 2


# ---- hardening batch (Task 9b): repeated cursors, and where paging ends inside a page ---------------------------

def ago(days):
    return datetime.now(timezone.utc) - timedelta(days=days)


def test_himalayas_stops_when_the_same_cursor_comes_back_on_two_consecutive_pages():
    one = stubs.himalayas_job("Backend Engineer", [], POSTED, 1)
    two = stubs.himalayas_job("Backend Engineer", [], POSTED, 2)
    three = stubs.himalayas_job("Backend Engineer", [], POSTED, 3)
    # page 1 says "next: c2", page 2 (fetched with c2) says "next: c2" again: a stuck cursor, so no third request
    client, seen = pages_of([him_page([one], "c2"), him_page([two], "c2"), him_page([three], "c3")])
    result = boards.fetch("himalayas", None, req(), client=client)
    assert result.errors == [] and len(seen) == 2
    assert sorted(j["source_job_id"] for j in result.jobs) == sorted([one["guid"], two["guid"]])
    assert [dict(r.url.params) for r in seen] == [{"limit": "20"}, {"limit": "20", "cursor": "c2"}]


def test_himalayas_different_cursors_keep_paging_to_the_cap():
    pages = [him_page([stubs.himalayas_job("Backend Engineer", [], POSTED, i)], f"c{i + 1}") for i in range(1, 5)]
    client, seen = pages_of(pages)
    result = boards.fetch("himalayas", None, req(), client=client)
    assert result.errors == [] and len(seen) == 3 and len(result.jobs) == 3
    assert [r.url.params.get("cursor") for r in seen] == [None, "c2", "c3"]


@pytest.mark.parametrize("system", ["himalayas", "arbeitnow"])
def test_an_old_job_in_the_middle_of_a_page_lets_the_rest_of_the_page_through_and_ends_paging(system):
    build = (lambda i, days: stubs.himalayas_job("Backend Engineer", [], ago(days), i)) if system == "himalayas" else \
            (lambda i, days: stubs.arbeitnow_job("Backend Engineer", "Berlin, Germany", ago(days), f"j-{i}", remote=True))
    page = [build(1, 1), build(2, 2), build(3, 30), build(4, 1), build(5, 2)]       # the 30-day-old job is third of five
    wrap = him_page if system == "himalayas" else arb_page
    client, seen = pages_of([wrap(page), wrap([build(9, 1)])])                       # a second page exists but must not be asked for
    result = boards.fetch(system, None, req(max_age_days=7, locations=["Berlin", "Bengaluru"]), client=client)
    assert result.errors == [] and len(seen) == 1
    ids = sorted(j["source_job_id"] for j in result.jobs)
    expected = sorted(j["guid"] if system == "himalayas" else j["slug"] for j in (page[0], page[1], page[3], page[4]))
    assert ids == expected                                                           # both fresh jobs after the old one are kept
    assert result.listed == 5 and result.dropped["age"] == 1


@pytest.mark.parametrize("system", ["himalayas", "arbeitnow"])
def test_an_unusable_item_that_is_also_old_still_ends_paging(system):
    if system == "himalayas":
        fresh, unusable = stubs.himalayas_job("Backend Engineer", [], ago(1), 1), stubs.himalayas_job("Backend Engineer", [], ago(40), 2)
        unusable["title"] = ""                                                       # unusable (no title) and old
        wrap = him_page
    else:
        fresh = stubs.arbeitnow_job("Backend Engineer", "Berlin, Germany", ago(1), "f-1", remote=True)
        unusable = stubs.arbeitnow_job("Backend Engineer", "Berlin, Germany", ago(40), "u-1", remote=True)
        unusable["title"] = ""
        wrap = arb_page
    client, seen = pages_of([wrap([fresh, unusable]), wrap([fresh])])                # page 2 must not be requested
    result = boards.fetch(system, None, req(max_age_days=7, locations=["Berlin", "Bengaluru"]), client=client)
    assert result.errors == [] and len(seen) == 1 and len(result.jobs) == 1
