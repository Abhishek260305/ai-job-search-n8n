"""The three company-board adapters in boards.py: Greenhouse, Lever and Ashby (spec section 2).

Everything runs against the StubBoards stand-in (or an in-process mock transport for per-request failures);
nothing touches the internet. Each test goes through boards.fetch(), so the relevance filter, the cap and the
one-line error handling are exercised exactly as production uses them."""
import json
from datetime import datetime, timedelta, timezone

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


def detail_paths(stub):
    return [path for method, path, query in stub.log if path.startswith("/greenhouse/v1/boards/") and path.count("/") == 6]


def mock_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def json_response(payload, status=200):
    return httpx.Response(status, content=json.dumps(payload).encode())      # content=, so a None payload is the JSON text "null"


# ---- Greenhouse -----------------------------------------------------------------------------------------

def test_greenhouse_maps_a_job(monkeypatch):
    posted = ago(days=2)
    post = stubs.gh_job("Backend Engineer", "Bengaluru, India", posted, 4242, board="acme", company="Acme Corp",
                        description="<p>Build the <b>API</b>.</p><ul><li>Python</li></ul>")
    with stubs.StubBoards(data={("greenhouse", "acme"): [post]}) as stub:
        result = run(monkeypatch, stub, "greenhouse")
    assert result.errors == [] and result.listed == 1 and result.relevant == 1
    (job,) = result.jobs
    assert job["source"] == "greenhouse" and job["source_job_id"] == "acme:4242"
    assert job["title"] == "Backend Engineer" and job["company"] == "Acme Corp"
    assert job["url"] == post["absolute_url"] and job["apply_url"] == post["absolute_url"]
    assert job["location"] == "Bengaluru, India" and job["is_remote"] is None
    assert job["posted_at"] == post["first_published"]
    assert job["description"] == "Build the API.\nPython"
    # raw is a compact subset: ids, urls, dates, locations and department names, never the description
    assert job["raw"]["id"] == 4242 and job["raw"]["departments"] == ["Engineering"]
    assert "content" not in job["raw"] and "Build the" not in json.dumps(job["raw"])


def test_greenhouse_list_call_has_no_content_param(monkeypatch):
    with stubs.StubBoards(data={("greenhouse", "acme"): [stubs.gh_job(id=1)]}) as stub:
        run(monkeypatch, stub, "greenhouse")
    assert stub.log[0] == ("GET", "/greenhouse/v1/boards/acme/jobs", "")


def test_greenhouse_fetches_details_only_for_relevant_jobs(monkeypatch):
    relevant = [stubs.gh_job("Backend Engineer", "Bengaluru, India", ago(days=1 + i), 100 + i) for i in range(3)]
    noise = ([stubs.gh_job("Marketing Manager", "Bengaluru, India", ago(days=1), 200 + i) for i in range(4)]       # wrong title
             + [stubs.gh_job("Backend Engineer", "London, United Kingdom", ago(days=1), 300 + i) for i in range(3)]  # wrong place
             + [stubs.gh_job("Backend Engineer", "Bengaluru, India", ago(days=40), 400 + i) for i in range(2)])     # too old
    with stubs.StubBoards(data={("greenhouse", "acme"): noise[:5] + relevant + noise[5:]}) as stub:
        result = run(monkeypatch, stub, "greenhouse")
    assert result.listed == 12 and result.relevant == 3 and len(result.jobs) == 3 and result.errors == []
    assert sorted(detail_paths(stub)) == [f"/greenhouse/v1/boards/acme/jobs/{100 + i}" for i in range(3)]   # 3, not 12
    assert stub.by_path["/greenhouse/v1/boards/acme/jobs"] == 1


def test_greenhouse_detail_calls_are_capped_at_the_limit_and_newest_first(monkeypatch):
    posts = [stubs.gh_job("Backend Engineer", "Bengaluru, India", ago(days=1, hours=i), 500 + i) for i in range(10)]
    with stubs.StubBoards(data={("greenhouse", "acme"): posts}) as stub:
        result = run(monkeypatch, stub, "greenhouse", limit=4)
    assert len(detail_paths(stub)) == 4
    assert [j["source_job_id"] for j in result.jobs] == ["acme:500", "acme:501", "acme:502", "acme:503"]   # the 4 newest
    assert result.relevant == 4 and result.listed == 10          # relevant counts what survived the cap here


def test_greenhouse_limit_zero_makes_no_detail_calls(monkeypatch):
    with stubs.StubBoards(data={("greenhouse", "acme"): [stubs.gh_job(id=1)]}) as stub:
        result = run(monkeypatch, stub, "greenhouse", limit=0)
    assert result.jobs == [] and result.errors == [] and detail_paths(stub) == []


def test_greenhouse_unescapes_content(monkeypatch):
    post = stubs.gh_job(id=7, description="<p>Fish &amp; chips</p><p>Second <i>line</i></p>")
    with stubs.StubBoards(data={("greenhouse", "acme"): [post]}) as stub:
        result = run(monkeypatch, stub, "greenhouse")
    assert result.jobs[0]["description"] == "Fish & chips\nSecond line"


def test_greenhouse_posted_at_falls_back_to_updated_at(monkeypatch):
    post = stubs.gh_job(id=8, posted=ago(days=2))
    del post["first_published"]
    with stubs.StubBoards(data={("greenhouse", "acme"): [post]}) as stub:
        result = run(monkeypatch, stub, "greenhouse")
    assert result.jobs[0]["posted_at"] == post["updated_at"]


def test_greenhouse_company_comes_from_the_payload_else_config_else_slug(monkeypatch):
    with stubs.StubBoards(data={("greenhouse", "big-co"): [stubs.gh_job(id=1, company="Big Co Inc")]}) as stub:
        assert run(monkeypatch, stub, "greenhouse", "big-co").jobs[0]["company"] == "Big Co Inc"
    no_name = stubs.gh_job(id=2)
    del no_name["company_name"]
    with stubs.StubBoards(data={("greenhouse", "big-co"): [no_name]}) as stub:
        assert run(monkeypatch, stub, "greenhouse", "big-co", name="Big Co (config)").jobs[0]["company"] == "Big Co (config)"
        assert run(monkeypatch, stub, "greenhouse", "big-co").jobs[0]["company"] == "Big Co"


def test_greenhouse_a_failing_detail_drops_only_that_job():
    posts = [stubs.gh_job("Backend Engineer", "Bengaluru, India", ago(days=1, hours=i), 600 + i) for i in range(4)]

    def handler(request: httpx.Request):
        path = request.url.path
        if path.endswith("/jobs"):
            return json_response({"jobs": [{k: v for k, v in p.items() if k != "content"} for p in posts]})
        job_id = path.rsplit("/", 1)[1]
        if job_id == "601":
            return httpx.Response(404, text="gone")
        if job_id == "602":
            return httpx.Response(500, text="boom")
        if job_id == "603":
            return httpx.Response(200, text="<html>not json</html>")
        return json_response(posts[0])

    result = boards.fetch("greenhouse", "acme", req(), client=mock_client(handler))
    assert result.errors == [] and result.listed == 4
    assert [j["source_job_id"] for j in result.jobs] == ["acme:600"]


def test_greenhouse_every_detail_failing_is_one_error_line():
    posts = [stubs.gh_job("Backend Engineer", "Bengaluru, India", ago(days=1, hours=i), 700 + i) for i in range(3)]

    def handler(request: httpx.Request):
        if request.url.path.endswith("/jobs"):
            return json_response({"jobs": posts})
        return httpx.Response(404, text="gone")

    result = boards.fetch("greenhouse", "acme", req(), client=mock_client(handler))
    assert result.jobs == [] and len(result.errors) == 1 and result.errors[0].startswith("greenhouse/acme: ")


def test_greenhouse_a_rate_limited_detail_stops_the_board():
    calls = []

    def handler(request: httpx.Request):
        calls.append(request.url.path)
        if request.url.path.endswith("/jobs"):
            return json_response({"jobs": [stubs.gh_job(id=i) for i in (1, 2, 3)]})
        return httpx.Response(429, text="slow down")

    result = boards.fetch("greenhouse", "acme", req(), client=mock_client(handler))
    assert result.jobs == [] and result.errors == ["greenhouse/acme: rate limited by the source; skipped for this run"]
    assert len(calls) == 2                                         # the list call and ONE detail call, nothing after the 429


def test_greenhouse_empty_board_is_not_an_error(monkeypatch):
    with stubs.StubBoards(modes={("greenhouse", "acme"): "empty"}) as stub:
        result = run(monkeypatch, stub, "greenhouse")
    assert result.jobs == [] and result.errors == [] and result.listed == 0 and detail_paths(stub) == []


def test_greenhouse_garbage_body_is_one_error_not_a_crash(monkeypatch):
    with stubs.StubBoards(modes={("greenhouse", "acme"): "garbage"}) as stub:
        result = run(monkeypatch, stub, "greenhouse")
    assert result.jobs == [] and result.errors == ["greenhouse/acme: returned something that is not JSON"]


def test_greenhouse_unexpected_shape_is_one_error(monkeypatch):
    for payload in ({"jobs": "nope"}, {"other": []}, ["a list"], None):
        result = boards.fetch("greenhouse", "acme", req(), client=mock_client(lambda r, p=payload: json_response(p)))
        assert result.jobs == [] and len(result.errors) == 1 and "unexpected format" in result.errors[0], payload


def test_greenhouse_missing_fields_are_skipped(monkeypatch):
    good = stubs.gh_job(id=1)
    no_id, no_title, no_url = stubs.gh_job(id=2), stubs.gh_job(id=3), stubs.gh_job(id=4)
    del no_id["id"]
    no_title["title"] = ""
    del no_url["absolute_url"]
    with stubs.StubBoards(data={("greenhouse", "acme"): [no_id, no_title, no_url, good]}) as stub:
        result = run(monkeypatch, stub, "greenhouse")
    assert result.errors == [] and [j["source_job_id"] for j in result.jobs] == ["acme:1"]
    assert detail_paths(stub) == ["/greenhouse/v1/boards/acme/jobs/1"]       # an unusable posting costs no detail call
    assert result.dropped["unusable"] == 0                                    # skipped inside the adapter, not counted


def test_greenhouse_entries_that_are_not_objects_are_skipped():
    good = stubs.gh_job(id=1)

    def handler(request: httpx.Request):
        if request.url.path.endswith("/jobs"):
            return json_response({"jobs": ["a string", 5, None, [], {}, good]})
        return json_response(good)

    result = boards.fetch("greenhouse", "acme", req(), client=mock_client(handler))
    assert result.errors == [] and [j["source_job_id"] for j in result.jobs] == ["acme:1"]


def test_greenhouse_not_found_is_one_error(monkeypatch):
    with stubs.StubBoards(modes={("greenhouse", "acme"): "not_found"}) as stub:
        result = run(monkeypatch, stub, "greenhouse")
    assert result.jobs == [] and result.errors == ["greenhouse/acme: not found (check the slug)"]


# ---- Lever ----------------------------------------------------------------------------------------------

def test_lever_maps_a_job(monkeypatch):
    post = stubs.lever_job("Backend Engineer", "Pune, India", ago(days=1), "abc-123", board="acme", workplace="remote")
    with stubs.StubBoards(data={("lever", "acme"): [post]}) as stub:
        result = run(monkeypatch, stub, "lever", locations=["Pune"])
    assert result.errors == [] and result.listed == 1
    (job,) = result.jobs
    assert job["source"] == "lever" and job["source_job_id"] == "acme:abc-123"
    assert job["title"] == "Backend Engineer" and job["location"] == "Pune, India" and job["is_remote"] is True
    assert job["url"] == post["hostedUrl"] and job["apply_url"] == post["applyUrl"]
    assert job["company"] == "Acme"                                       # no company in the payload: the slug, title-cased
    assert job["raw"]["id"] == "abc-123" and job["raw"]["team"] == "Platform" and job["raw"]["department"] == "Engineering"
    assert "Acme is hiring" not in json.dumps(job["raw"])


def test_lever_description_joins_plain_and_additional(monkeypatch):
    with_extra = stubs.lever_job(id="a", description="<p>Main text</p>")
    without = stubs.lever_job(id="b", description="<p>Other text</p>")
    del without["additionalPlain"]
    with stubs.StubBoards(data={("lever", "acme"): [with_extra, without]}) as stub:
        jobs = {j["source_job_id"]: j for j in run(monkeypatch, stub, "lever").jobs}
    lists = "What you'll do\nBuild things people use\nReview code"      # the stub's `lists` section sits between the two texts
    assert jobs["acme:a"]["description"] == f"Main text\n\n{lists}\n\nAcme is an equal opportunity employer."
    assert jobs["acme:b"]["description"] == f"Other text\n\n{lists}"


@pytest.mark.parametrize("workplace, expected", [("remote", True), ("hybrid", False), ("on-site", False), ("onsite", False),
                                                 ("", None), (None, None), ("unheard-of", None)])
def test_lever_workplace_type_sets_is_remote(monkeypatch, workplace, expected):
    post = stubs.lever_job(id="w", workplace=workplace)
    with stubs.StubBoards(data={("lever", "acme"): [post]}) as stub:
        result = run(monkeypatch, stub, "lever")
    assert result.jobs[0]["is_remote"] is expected


def test_lever_maps_epoch_ms_dates(monkeypatch):
    when = datetime(2026, 10, 9, 8, 30, 15, 250000, tzinfo=timezone.utc)
    post = stubs.lever_job(id="d", posted=ago(days=1))
    post["createdAt"] = int(when.timestamp() * 1000)
    with stubs.StubBoards(data={("lever", "acme"): [post]}) as stub:
        result = run(monkeypatch, stub, "lever", max_age_days=0)
    posted = result.jobs[0]["posted_at"]
    assert isinstance(posted, str) and posted.startswith("2026-10-09T08:30:15.250") and posted.endswith("+00:00")
    assert datetime.fromisoformat(posted) == when


def test_lever_a_bad_created_at_is_no_date_not_a_crash(monkeypatch):
    posts = []
    for i, bad in enumerate((None, "soon", 10 ** 30, True)):
        post = stubs.lever_job(id=f"x{i}")
        post["createdAt"] = bad
        posts.append(post)
    with stubs.StubBoards(data={("lever", "acme"): posts}) as stub:
        result = run(monkeypatch, stub, "lever")
    assert result.errors == [] and len(result.jobs) == 4 and all(j["posted_at"] is None for j in result.jobs)


def test_lever_falls_back_to_the_eu_host_once(monkeypatch):
    with stubs.StubBoards(data={("lever", "euco"): [stubs.lever_job(id="e1", board="euco")]},
                          modes={("lever", "euco"): "eu_only"}) as stub:
        result = run(monkeypatch, stub, "lever", "euco")
    assert result.errors == [] and [j["source_job_id"] for j in result.jobs] == ["euco:e1"]
    assert stub.log == [("GET", "/lever/v0/postings/euco", "mode=json"), ("GET", "/lever_eu/v0/postings/euco", "mode=json")]


def test_lever_global_host_is_tried_first_and_the_eu_host_is_not_touched_on_success(monkeypatch):
    with stubs.StubBoards(data={("lever", "acme"): [stubs.lever_job(id="g1")]}) as stub:
        run(monkeypatch, stub, "lever")
    assert [path for _, path, _ in stub.log] == ["/lever/v0/postings/acme"]


def test_lever_404_on_both_hosts_is_one_error(monkeypatch):
    with stubs.StubBoards(modes={("lever", "ghost"): "not_found", ("lever_eu", "ghost"): "not_found"}) as stub:
        result = run(monkeypatch, stub, "lever", "ghost")
    assert result.jobs == [] and result.errors == ["lever/ghost: board not found on either Lever host (check the slug)"]
    assert [path for _, path, _ in stub.log] == ["/lever/v0/postings/ghost", "/lever_eu/v0/postings/ghost"]    # one try each


def test_lever_other_errors_are_not_retried_on_the_eu_host(monkeypatch):
    for mode, line in (("server_error", "server error (HTTP 500) (tried twice)"), ("garbage", "returned something that is not JSON"),
                       ("rate_limited", None)):
        with stubs.StubBoards(modes={("lever", "acme"): mode}) as stub:
            result = run(monkeypatch, stub, "lever")
        assert result.jobs == [] and len(result.errors) == 1, mode
        if line:
            assert result.errors[0] == f"lever/acme: {line}"
        assert not any(path.startswith("/lever_eu") for _, path, _ in stub.log), mode


def test_lever_empty_board_is_not_an_error(monkeypatch):
    with stubs.StubBoards(modes={("lever", "acme"): "empty"}) as stub:
        result = run(monkeypatch, stub, "lever")
    assert result.jobs == [] and result.errors == [] and result.listed == 0


def test_lever_garbage_body_is_one_error_not_a_crash(monkeypatch):
    with stubs.StubBoards(modes={("lever", "acme"): "garbage"}) as stub:
        result = run(monkeypatch, stub, "lever")
    assert result.jobs == [] and result.errors == ["lever/acme: returned something that is not JSON"]


def test_lever_unexpected_shape_is_one_error():
    for payload in ({"jobs": []}, "text", None, 5):
        result = boards.fetch("lever", "acme", req(), client=mock_client(lambda r, p=payload: json_response(p)))
        assert result.jobs == [] and len(result.errors) == 1 and "unexpected format" in result.errors[0], payload


def test_lever_missing_fields_are_skipped(monkeypatch):
    good = stubs.lever_job(id="ok")
    no_id, no_title, no_url = stubs.lever_job(id="n1"), stubs.lever_job(id="n2"), stubs.lever_job(id="n3")
    del no_id["id"]
    del no_title["text"]
    del no_url["hostedUrl"]
    with stubs.StubBoards(data={("lever", "acme"): [no_id, no_title, no_url, good, "junk", 7, None]}) as stub:
        result = run(monkeypatch, stub, "lever")
    assert result.errors == [] and [j["source_job_id"] for j in result.jobs] == ["acme:ok"]
    assert result.listed == 7                                             # everything the source listed, usable or not
    assert result.dropped["unusable"] == 0


def test_lever_survives_missing_optional_parts(monkeypatch):
    bare = {"id": "bare", "text": "Backend Engineer", "hostedUrl": "https://jobs.lever.co/acme/bare"}
    with stubs.StubBoards(data={("lever", "acme"): [bare]}) as stub:
        result = run(monkeypatch, stub, "lever")
    (job,) = result.jobs
    assert job["location"] is None and job["posted_at"] is None and job["description"] == "" and job["apply_url"] == job["url"]


# ---- Ashby ----------------------------------------------------------------------------------------------

def test_ashby_maps_a_job(monkeypatch):
    post = stubs.ashby_job("Backend Engineer", "Bengaluru, India", ago(days=1), "u-1", board="acme", description="<p>Do <b>things</b></p>")
    with stubs.StubBoards(data={("ashby", "acme"): [post]}) as stub:
        result = run(monkeypatch, stub, "ashby")
    assert result.errors == [] and result.listed == 1
    (job,) = result.jobs
    assert job["source"] == "ashby" and job["source_job_id"] == "acme:u-1" and job["company"] == "Acme"
    assert job["title"] == "Backend Engineer" and job["location"] == "Bengaluru, India"
    assert job["url"] == post["jobUrl"] and job["apply_url"] == post["applyUrl"]
    assert job["posted_at"] == post["publishedAt"] and job["description"] == post["descriptionPlain"]
    assert job["raw"]["id"] == "u-1" and job["raw"]["department"] == "Engineering" and job["raw"]["team"] == "Platform"
    assert "descriptionPlain" not in job["raw"] and "Do things" not in json.dumps(job["raw"])


def test_ashby_skips_unlisted_jobs(monkeypatch):
    listed, hidden = stubs.ashby_job(id="in"), stubs.ashby_job(id="out")
    hidden["isListed"] = False
    with stubs.StubBoards(data={("ashby", "acme"): [hidden, listed]}) as stub:
        result = run(monkeypatch, stub, "ashby")
    assert [j["source_job_id"] for j in result.jobs] == ["acme:in"] and result.errors == []
    assert result.listed == 1                                             # a hidden posting is not "listed"


def test_ashby_a_posting_without_is_listed_is_kept(monkeypatch):
    post = stubs.ashby_job(id="k")
    del post["isListed"]
    with stubs.StubBoards(data={("ashby", "acme"): [post]}) as stub:
        assert len(run(monkeypatch, stub, "ashby").jobs) == 1


@pytest.mark.parametrize("workplace, expected", [("Remote", True), ("Hybrid", False), ("OnSite", False), (None, None)])
def test_ashby_remote_follows_workplace_type_not_is_remote(monkeypatch, workplace, expected):
    # the real feed pairs isRemote=true with workplaceType "Hybrid" (509 of 815 jobs), so isRemote alone is not "remote"
    post = stubs.ashby_job(id="r", workplace=workplace)
    if workplace == "Hybrid":
        assert post["isRemote"] is True                                   # the builder reproduces the real quirk
    with stubs.StubBoards(data={("ashby", "acme"): [post]}) as stub:
        assert run(monkeypatch, stub, "ashby").jobs[0]["is_remote"] is expected


@pytest.mark.parametrize("is_remote, expected", [(True, True), (False, False), (None, None), ("yes", None)])
def test_ashby_without_workplace_type_uses_is_remote(monkeypatch, is_remote, expected):
    post = stubs.ashby_job(id="f")
    post["workplaceType"], post["isRemote"] = None, is_remote
    with stubs.StubBoards(data={("ashby", "acme"): [post]}) as stub:
        assert run(monkeypatch, stub, "ashby").jobs[0]["is_remote"] is expected


def test_ashby_location_joins_secondary_locations(monkeypatch):
    post = stubs.ashby_job(id="l", location="Bengaluru, India")
    post["secondaryLocations"] = ["Pune, India", {"location": "Remote - India", "address": {}}, {"address": {}}, "Bengaluru, India", ""]
    with stubs.StubBoards(data={("ashby", "acme"): [post]}) as stub:
        result = run(monkeypatch, stub, "ashby")
    assert result.jobs[0]["location"] == "Bengaluru, India; Pune, India; Remote - India"


def test_ashby_secondary_location_only_when_primary_is_missing(monkeypatch):
    post = stubs.ashby_job(id="m")
    post["location"], post["secondaryLocations"] = None, ["Mumbai, India"]
    with stubs.StubBoards(data={("ashby", "acme"): [post]}) as stub:
        assert run(monkeypatch, stub, "ashby", locations=["Mumbai"]).jobs[0]["location"] == "Mumbai, India"


def test_ashby_13mb_board_is_accepted(monkeypatch):
    filler = "We build reliable backend systems and we like clear writing. " * 9000          # about 0.5 MB of text
    posts = [stubs.ashby_job("Backend Engineer", "Bengaluru, India", ago(days=1, hours=i), f"big-{i}", description=f"<p>{filler}{i}</p>")
             for i in range(13)]
    size = len(json.dumps({"jobs": posts}))
    assert 13_000_000 <= size < boards.MAX_BYTES                          # the real largest board was 13.5 MB
    with stubs.StubBoards(data={("ashby", "huge"): posts}) as stub:
        result = run(monkeypatch, stub, "ashby", "huge")
    assert result.errors == [] and result.listed == 13 and len(result.jobs) == 13
    assert all(len(j["description"]) > 400_000 for j in result.jobs)


def test_ashby_a_response_over_the_cap_is_one_error(monkeypatch):
    with stubs.StubBoards(modes={("ashby", "acme"): "huge"}) as stub:
        result = run(monkeypatch, stub, "ashby")
    assert result.jobs == [] and len(result.errors) == 1 and "larger than" in result.errors[0]


def test_ashby_empty_board_is_not_an_error(monkeypatch):
    with stubs.StubBoards(modes={("ashby", "acme"): "empty"}) as stub:
        result = run(monkeypatch, stub, "ashby")
    assert result.jobs == [] and result.errors == [] and result.listed == 0


def test_ashby_garbage_body_is_one_error_not_a_crash(monkeypatch):
    with stubs.StubBoards(modes={("ashby", "acme"): "garbage"}) as stub:
        result = run(monkeypatch, stub, "ashby")
    assert result.jobs == [] and result.errors == ["ashby/acme: returned something that is not JSON"]


def test_ashby_unexpected_shape_is_one_error():
    for payload in ([], {"jobs": "nope"}, {"other": 1}, None):
        result = boards.fetch("ashby", "acme", req(), client=mock_client(lambda r, p=payload: json_response(p)))
        assert result.jobs == [] and len(result.errors) == 1 and "unexpected format" in result.errors[0], payload


def test_ashby_missing_fields_are_skipped(monkeypatch):
    good = stubs.ashby_job(id="ok")
    no_id, no_title, no_url = stubs.ashby_job(id="n1"), stubs.ashby_job(id="n2"), stubs.ashby_job(id="n3")
    del no_id["id"]
    no_title["title"] = None
    del no_url["jobUrl"]
    with stubs.StubBoards(data={("ashby", "acme"): [no_id, no_title, no_url, good, "junk", None]}) as stub:
        result = run(monkeypatch, stub, "ashby")
    assert result.errors == [] and [j["source_job_id"] for j in result.jobs] == ["acme:ok"]
    assert result.dropped["unusable"] == 0


def test_ashby_not_found_is_one_error(monkeypatch):
    with stubs.StubBoards(modes={("ashby", "acme"): "not_found"}) as stub:
        result = run(monkeypatch, stub, "ashby")
    assert result.jobs == [] and result.errors == ["ashby/acme: not found (check the slug)"]


# ---- all three ------------------------------------------------------------------------------------------

BUILDERS = {"greenhouse": stubs.gh_job, "lever": stubs.lever_job, "ashby": stubs.ashby_job}


@pytest.mark.parametrize("system", sorted(BUILDERS))
def test_adapters_are_registered(system):
    assert callable(boards.ADAPTERS.get(system))


@pytest.mark.parametrize("system", sorted(BUILDERS))
def test_slug_case_is_preserved_in_the_url(monkeypatch, system):
    with stubs.StubBoards(data={(system, "AcmeCo"): [BUILDERS[system](id=11)]}) as stub:
        result = run(monkeypatch, stub, system, "AcmeCo")
    assert result.errors == [] and result.jobs[0]["source_job_id"] == "AcmeCo:11"
    assert all("/AcmeCo" in path for _, path, _ in stub.log)


@pytest.mark.parametrize("system", sorted(BUILDERS))
def test_an_unknown_company_is_one_error(monkeypatch, system):
    with stubs.StubBoards(data={}) as stub:                                # data given, this company absent: a 404
        result = run(monkeypatch, stub, system, "nobody")
    assert result.jobs == [] and len(result.errors) == 1 and result.errors[0].startswith(f"{system}/nobody: ")
    assert "not found" in result.errors[0] and "no adapter" not in result.errors[0]


# ---- hardening batch (Task 9b): Lever and Ashby ------------------------------------------------------------

def test_ashby_location_deduplicates_a_huge_list_quickly():
    import time
    post = stubs.ashby_job(id="big", location="Bengaluru, India")
    post["secondaryLocations"] = [f"City {i}, India" for i in range(30_000)] + ["Bengaluru, India", "City 5, India"] * 100
    started = time.perf_counter()
    location = boards._ashby_location(post)
    assert time.perf_counter() - started < 1.0                            # a `not in list` test per item takes several seconds here
    parts = location.split("; ")
    assert len(parts) == 30_001 and parts[0] == "Bengaluru, India" and parts[1:3] == ["City 0, India", "City 1, India"]
    assert len(set(parts)) == len(parts)


def test_ashby_raw_location_goes_through_the_text_normaliser(monkeypatch):
    hostile = stubs.ashby_job(id="h1")
    hostile["location"] = {"nested": {"deep": ["x"] * 3}, "location": "Pune, India"}
    listy = stubs.ashby_job(id="h2")
    listy["location"] = ["Mumbai", "Pune"]
    fine = stubs.ashby_job(id="h3", location="  Bengaluru, India  ")
    with stubs.StubBoards(data={("ashby", "acme"): [hostile, listy, fine]}) as stub:
        result = run(monkeypatch, stub, "ashby")
    raws = {j["source_job_id"]: j["raw"] for j in result.jobs}
    assert raws["acme:h1"]["location"] == "" and raws["acme:h2"]["location"] == ""       # never an object or a list
    assert raws["acme:h3"]["location"] == "Bengaluru, India"
    assert all(isinstance(r["location"], str) for r in raws.values())


def test_lever_raw_categories_go_through_the_text_normaliser(monkeypatch):
    post = stubs.lever_job(id="hostile")
    post["categories"].update(department={"evil": ["x"] * 5}, team=["Platform", "Core"], commitment={"k": {"v": 1}})
    fine = stubs.lever_job(id="fine")
    with stubs.StubBoards(data={("lever", "acme"): [post, fine]}) as stub:
        result = run(monkeypatch, stub, "lever")
    raws = {j["source_job_id"]: j["raw"] for j in result.jobs}
    assert (raws["acme:hostile"]["department"], raws["acme:hostile"]["team"], raws["acme:hostile"]["commitment"]) == ("", "", "")
    assert (raws["acme:fine"]["department"], raws["acme:fine"]["team"], raws["acme:fine"]["commitment"]) == ("Engineering", "Platform", "Full-time")


@pytest.mark.parametrize("workplace, is_remote, expected", [
    ("Remote", False, True), ("Remote", None, True), ("Remote", True, True),
    ("Hybrid", None, False), ("Hybrid", True, False), ("Hybrid", False, False),
    ("OnSite", None, False), ("OnSite", True, False), ("On-Site", None, False),
    ("Flexible", True, True), ("Flexible", False, False), ("Flexible", None, None),
    (None, True, True), (None, False, False), (None, None, None)])
def test_ashby_remote_full_table(monkeypatch, workplace, is_remote, expected):
    post = stubs.ashby_job(id="t")
    post["workplaceType"], post["isRemote"] = workplace, is_remote         # set directly: the builder would derive isRemote
    with stubs.StubBoards(data={("ashby", "acme"): [post]}) as stub:
        assert run(monkeypatch, stub, "ashby").jobs[0]["is_remote"] is expected


def test_integer_ids_are_stringified_for_lever_and_ashby(monkeypatch):
    lever_post, ashby_post = stubs.lever_job(id=424242), stubs.ashby_job(id=777001)
    with stubs.StubBoards(data={("lever", "acme"): [lever_post], ("ashby", "acme"): [ashby_post]}) as stub:
        lever = run(monkeypatch, stub, "lever")
        ashby = run(monkeypatch, stub, "ashby")
    assert lever.errors == [] and [j["source_job_id"] for j in lever.jobs] == ["acme:424242"] and lever.jobs[0]["raw"]["id"] == "424242"
    assert ashby.errors == [] and [j["source_job_id"] for j in ashby.jobs] == ["acme:777001"] and ashby.jobs[0]["raw"]["id"] == "777001"
