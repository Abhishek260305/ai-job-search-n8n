"""Fixes from the final whole-branch review of the company-board and feed code (boards.py).

Runs against the StubBoards stand-in or an in-process mock transport; nothing touches the internet."""
import json
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import boards
import filters
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
    monkeypatch.setenv("BOARDS_API_URL", stub.url)
    return boards.fetch(system, slug, req(**kw))


def mock_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def json_response(payload, status=200):
    return httpx.Response(status, content=json.dumps(payload).encode())


# ---- G2: Lever `lists` sections (where "5+ years" lives) ----------------------------------------------------

def test_lever_description_includes_the_lists_sections_between_description_and_additional(monkeypatch):
    post = stubs.lever_job(id="L1", description="<p>Main text</p>")
    post["lists"] = [{"text": "What you'll do", "content": "<li>Build things</li><li>Review code</li>"},
                     {"text": "Requirements", "content": "<li>8+ years of experience</li>"}]
    with stubs.StubBoards(data={("lever", "acme"): [post]}) as stub:
        (job,) = run(monkeypatch, stub, "lever").jobs
    assert job["description"] == ("Main text\n\nWhat you'll do\nBuild things\nReview code\n\n"
                                  "Requirements\n8+ years of experience\n\nAcme is an equal opportunity employer.")


def test_lever_requirement_in_a_list_section_is_seen_by_the_experience_filter(monkeypatch):
    post = stubs.lever_job(id="L2", description="<p>Build APIs.</p>")
    post["lists"] = [{"text": "Requirements", "content": "<li>8+ years of experience in backend development</li>"}]
    with stubs.StubBoards(data={("lever", "acme"): [post]}) as stub:
        (job,) = run(monkeypatch, stub, "lever").jobs
    assert "8+ years of experience" in job["description"]
    settings = {"max_yoe": 5, "min_yoe": None, "max_age_days": 14, "allow_remote": True,
                "exclude_keywords": [], "exclude_companies": [], "include_keywords": []}
    decision = filters.decide(job, settings)
    assert decision.keep is False and "8+ years" in decision.reason


@pytest.mark.parametrize("lists", [
    "text", 5, None, {"text": "x", "content": "<li>y</li>"}, [None, 5, "str", ["nested"]],
    [{"text": 5, "content": 6}], [{"text": ["a"], "content": {"b": 1}}], [{}], [{"text": "Only a heading"}],
    [{"content": "<li>only content</li>"}], [{"text": None, "content": None}]])
def test_lever_odd_lists_shapes_never_raise(monkeypatch, lists):
    post = stubs.lever_job(id="L3", description="<p>Main text</p>")
    post["lists"] = lists
    with stubs.StubBoards(data={("lever", "acme"): [post]}) as stub:
        result = run(monkeypatch, stub, "lever")
    assert result.errors == [] and len(result.jobs) == 1
    assert result.jobs[0]["description"].startswith("Main text")


def test_lever_without_lists_is_unchanged(monkeypatch):
    post = stubs.lever_job(id="L4", description="<p>Main text</p>")
    del post["lists"]
    with stubs.StubBoards(data={("lever", "acme"): [post]}) as stub:
        (job,) = run(monkeypatch, stub, "lever").jobs
    assert job["description"] == "Main text\n\nAcme is an equal opportunity employer."


# ---- G3: NUL characters (Postgres text and jsonb refuse them) ------------------------------------------------

def test_make_job_strips_nul_from_every_string_including_raw():
    j = boards.make_job("lever", "a\x00:1", url="https://x.example/\x00a", apply_url="https://x.example/b\x00", title="Back\x00end",
                        company="Ac\x00me", location="Pu\x00ne", description="de\x00sc", salary_currency="U\x00SD",
                        raw={"id": "i\x00d", "nested": {"k\x00": ["a\x00", {"z": "\x00"}], "n": 3, "none": None},
                             "tags": ("t\x00",)})
    assert "\x00" not in json.dumps(j)
    assert j["title"] == "Backend" and j["company"] == "Acme" and j["location"] == "Pune" and j["description"] == "desc"
    assert j["raw"]["id"] == "id" and j["raw"]["nested"] == {"k": ["a", {"z": ""}], "n": 3, "none": None}
    assert j["source_job_id"] == "a:1"


def test_html_to_text_strips_nul():
    assert "\x00" not in boards.html_to_text("<p>a\x00b</p><p>c&#0;d</p>")
    assert boards.html_to_text("<p>a\x00b</p>") == "ab"
    assert boards.html_to_text("a\x00 &lt;b", escaped=True) == "a <b"


def test_a_posting_with_nul_characters_arrives_clean(monkeypatch):
    post = stubs.lever_job("Backend\x00 Engineer", "Bengaluru,\x00 India", ago(days=1), "N1", description="<p>Build\x00 APIs</p>")
    post["categories"]["team"] = "Plat\x00form"
    with stubs.StubBoards(data={("lever", "acme"): [post]}) as stub:
        result = run(monkeypatch, stub, "lever", name="Ac\x00me")
    assert result.errors == [] and len(result.jobs) == 1
    assert "\x00" not in json.dumps(result.jobs[0])
    assert result.jobs[0]["title"] == "Backend Engineer" and result.jobs[0]["raw"]["team"] == "Platform"


# ---- G4: posted_at is a valid date or None, for every adapter that passes a string through ------------------------

GARBAGE = ["not a date", "2026-13-45T99:99:99Z"]


@pytest.mark.parametrize("garbage", GARBAGE)
def test_greenhouse_garbage_date_is_none_and_the_job_stays(monkeypatch, garbage):
    post = stubs.gh_job("Backend Engineer", "Bengaluru, India", ago(days=1), 1)
    post["first_published"] = post["updated_at"] = garbage
    with stubs.StubBoards(data={("greenhouse", "acme"): [post]}) as stub:
        result = run(monkeypatch, stub, "greenhouse")
    assert result.errors == [] and len(result.jobs) == 1 and result.jobs[0]["posted_at"] is None


@pytest.mark.parametrize("garbage", GARBAGE)
def test_ashby_garbage_date_is_none_and_the_job_stays(monkeypatch, garbage):
    post = stubs.ashby_job("Backend Engineer", "Bengaluru, India", ago(days=1), "a1")
    post["publishedAt"] = garbage
    with stubs.StubBoards(data={("ashby", "acme"): [post]}) as stub:
        result = run(monkeypatch, stub, "ashby")
    assert result.errors == [] and len(result.jobs) == 1 and result.jobs[0]["posted_at"] is None


@pytest.mark.parametrize("garbage", GARBAGE)
def test_workable_garbage_date_is_none_and_the_job_stays(monkeypatch, garbage):
    post = stubs.workable_job("Backend Engineer", "Bengaluru, India", ago(days=1), "WK0001")
    post["published"] = garbage
    with stubs.StubBoards(data={("workable", "acme"): [post]}) as stub:
        result = run(monkeypatch, stub, "workable")
    assert result.errors == [] and len(result.jobs) == 1 and result.jobs[0]["posted_at"] is None


@pytest.mark.parametrize("garbage", GARBAGE)
def test_smartrecruiters_garbage_date_is_none_and_the_job_stays(monkeypatch, garbage):
    post = stubs.sr_job("Backend Engineer", "Bengaluru, Karnataka, India", ago(days=1), 744000000000001, board="Acme")
    post["releasedDate"] = garbage
    with stubs.StubBoards(data={("smartrecruiters", "Acme"): [post]}) as stub:
        result = run(monkeypatch, stub, "smartrecruiters", "Acme")
    assert result.errors == [] and len(result.jobs) == 1 and result.jobs[0]["posted_at"] is None


def test_a_date_that_overflows_utc_conversion_is_none():
    assert boards._iso_utc("0001-01-01T00:00:00+05:00") is None


# ---- G5: a future-dated posting cannot jump the queue -------------------------------------------------------------

def test_a_posting_dated_more_than_a_day_ahead_is_stored_without_a_date(monkeypatch):
    now = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
    future = boards.make_job("greenhouse", "acme:1", url="https://b.example/1", title="Backend Engineer", company="Acme",
                             location="Bengaluru, India", posted_at=(now + timedelta(days=30)).isoformat())
    soon = boards.make_job("greenhouse", "acme:2", url="https://b.example/2", title="Backend Engineer", company="Acme",
                           location="Bengaluru, India", posted_at=(now + timedelta(hours=5)).isoformat())    # clock skew: believed
    year9999 = boards.make_job("greenhouse", "acme:3", url="https://b.example/3", title="Backend Engineer", company="Acme",
                               location="Bengaluru, India", posted_at="9999-12-31T00:00:00+00:00")
    older = boards.make_job("greenhouse", "acme:4", url="https://b.example/4", title="Backend Engineer", company="Acme",
                            location="Bengaluru, India", posted_at=(now - timedelta(days=2)).isoformat())
    monkeypatch.setitem(boards.ADAPTERS, "ashby", lambda client, slug, r, n: ([future, older, year9999, soon], 4))
    result = boards.fetch("ashby", "acme", req(limit=4), now=now)
    by_id = {j["source_job_id"]: j for j in result.jobs}
    assert by_id["acme:1"]["posted_at"] is None and by_id["acme:3"]["posted_at"] is None
    assert by_id["acme:2"]["posted_at"] == (now + timedelta(hours=5)).isoformat()
    # newest first, unknown dates last: the undated ones cannot jump ahead of a real date
    assert [j["source_job_id"] for j in result.jobs] == ["acme:2", "acme:4", "acme:1", "acme:3"]


# ---- G6: a whole-request deadline (the timeout is per read, so a drip-feeding server could hold a request for minutes) ----

def drip_client(chunks, pause, seen=None):
    """A mock server that sends `chunks` one at a time with `pause` seconds between them."""
    def body():
        for chunk in chunks:
            time.sleep(pause)
            yield chunk

    def handler(request: httpx.Request):
        if seen is not None:
            seen.append(request.url.path)
        return httpx.Response(200, content=body(), headers={"Content-Type": "application/json"})
    return mock_client(handler)


def test_the_deadline_constant_is_a_minute():
    assert boards.REQUEST_DEADLINE_S == 60.0


def test_a_drip_feeding_response_is_aborted_at_the_deadline_and_not_retried(monkeypatch):
    monkeypatch.setattr(boards, "REQUEST_DEADLINE_S", 0.3)
    seen = []
    chunks = [b'{"jobs": [] '] + [b" "] * 200 + [b"}"]                  # would take ~10 s at 0.05 s a chunk
    started = time.monotonic()
    result = boards.fetch("greenhouse", "acme", req(), client=drip_client(chunks, 0.05, seen))
    elapsed = time.monotonic() - started
    assert result.jobs == [] and result.errors == ["greenhouse/acme: response took too long"]
    assert elapsed < 3.0
    assert seen == ["/v1/boards/acme/jobs"]                             # one request only: a slow source is skipped, not retried


def test_get_bytes_raises_a_board_error_that_is_not_transient_on_a_slow_response(monkeypatch):
    monkeypatch.setattr(boards, "REQUEST_DEADLINE_S", 0.2)
    calls = []
    client = drip_client([b" "] * 100, 0.05, calls)
    with pytest.raises(boards.BoardError, match="response took too long"):
        boards.get_bytes(client, "GET", "https://boards-api.greenhouse.io/v1/boards/acme/jobs")
    assert len(calls) == 1


def test_a_fast_response_is_unaffected_by_the_deadline(monkeypatch):
    monkeypatch.setattr(boards, "REQUEST_DEADLINE_S", 5.0)
    chunks = [b'{"jobs": ', b'[]', b"}"]
    result = boards.fetch("greenhouse", "acme", req(), client=drip_client(chunks, 0.0))
    assert result.errors == [] and result.jobs == [] and result.listed == 0


# ---- G7: the Greenhouse detail id goes into a URL path, so it gets the same rule as the other systems ----------------

@pytest.mark.parametrize("bad_id", ["..", "a/b", "", "a b", "x" * 65, "a?b=1", "%2e%2e", "a\\b"])
def test_greenhouse_a_malformed_id_is_skipped_and_never_requested(bad_id):
    good = stubs.gh_job("Backend Engineer", "Bengaluru, India", ago(days=1), 77)
    bad = stubs.gh_job("Backend Engineer", "Bengaluru, India", ago(days=1), 78)
    bad["id"] = bad_id
    paths = []

    def handler(request: httpx.Request):
        paths.append(request.url.raw_path.decode())
        if request.url.path.endswith("/jobs"):
            return json_response({"jobs": [bad, good]})
        return json_response(good)

    result = boards.fetch("greenhouse", "acme", req(), client=mock_client(handler))
    assert result.errors == [] and [j["source_job_id"] for j in result.jobs] == ["acme:77"]
    assert paths == ["/v1/boards/acme/jobs", "/v1/boards/acme/jobs/77"]


def test_greenhouse_ids_that_are_numbers_or_safe_strings_still_work():
    a = stubs.gh_job("Backend Engineer", "Bengaluru, India", ago(days=1), 5)
    b = stubs.gh_job("Backend Engineer", "Bengaluru, India", ago(days=1), "abc_DEF-9")

    def handler(request: httpx.Request):
        if request.url.path.endswith("/jobs"):
            return json_response({"jobs": [a, b]})
        return json_response(a if request.url.path.endswith("/5") else b)

    result = boards.fetch("greenhouse", "acme", req(), client=mock_client(handler))
    assert sorted(j["source_job_id"] for j in result.jobs) == ["acme:5", "acme:abc_DEF-9"]
