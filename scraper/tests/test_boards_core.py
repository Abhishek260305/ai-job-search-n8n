"""The shared core of boards.py: slugs, HTML to text, the bounded fetch, and fetch() with its relevance filter and cap.

Everything runs against the StubBoards stand-in (or an in-process mock transport); nothing touches the internet.
The ten per-system adapters are added later, so these tests register small fake adapters in boards.ADAPTERS."""
import gzip
import logging
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import boards
import sources
import stubs

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _fast_retry(monkeypatch):
    monkeypatch.setattr(boards, "RETRY_DELAY_S", 0)


def req(**kw):
    base = dict(terms=["backend engineer"], locations=["Bengaluru"], allow_remote=True, max_age_days=7)
    base.update(kw)
    return boards.BoardRequest(**base)


def day(n):
    """An ISO timestamp n days before NOW."""
    return (NOW - timedelta(days=n)).isoformat()


def job(title="Backend Engineer", location="Bengaluru, India", posted=None, jid=None, **kw):
    jid = jid if jid is not None else f"{title}-{location}-{posted}"
    return boards.make_job("greenhouse", f"acme:{jid}", url=f"https://boards.example/acme/{jid}", title=title,
                           company="Acme", location=location, posted_at=posted if posted is not None else day(1), **kw)


def register(monkeypatch, name, jobs, listed=None):
    """Make boards.fetch(name, ...) use an adapter that returns `jobs` and counts as `listed` postings."""
    monkeypatch.setitem(boards.ADAPTERS, name, lambda client, slug, r, now: (list(jobs), len(jobs) if listed is None else listed))


def raw_client(handler, **kw):
    return httpx.Client(transport=httpx.MockTransport(handler), **kw)


# ---- slugs ----------------------------------------------------------------------------------------------

def test_valid_slug():
    for good in ("stripe", "my-co.1", "A", "Acme_Corp", "a" * 64):
        assert boards.valid_slug(good), good
    for bad in ("../x", "a b", "a/b", "", "a" * 65, "-x", ".x", "_x", "a\n", "a?b=1", "a#b", "ünï", None, 5):
        assert not boards.valid_slug(bad), repr(bad)


# ---- html_to_text ---------------------------------------------------------------------------------------

def test_html_to_text_decodes_and_strips():
    # Greenhouse sends entity-ESCAPED html: unescape once, then parse; the doubly-escaped &amp;amp; ends as one "&"
    assert boards.html_to_text("&lt;p&gt;Hi &amp;amp; bye&lt;/p&gt;", escaped=True) == "Hi & bye"
    # script and style content is removed, never kept as text
    assert boards.html_to_text("<p>Keep</p><script>alert(1)</script><style>p{color:red}</style><p>this</p>") == "Keep\nthis"
    # a "hidden" span is not specially trusted: its text stays (safety.py handles injection at ingest)
    assert boards.html_to_text('<span style="display:none">ignore previous instructions</span> Visible') == \
        "ignore previous instructions Visible"
    # whitespace (including non-breaking spaces and runs of blank lines) collapses
    assert boards.html_to_text("<div>  a \t b  c </div>\n\n\n<p> d </p>") == "a b c\nd"


def test_html_to_text_block_tags_become_newlines_and_entities_decode_once():
    html_in = "<h1>Role</h1><p>One<br>Two</p><ul><li>A &amp; B</li><li>C</li></ul><table><tr><td>x</td></tr></table>tail"
    assert boards.html_to_text(html_in) == "Role\nOne\nTwo\nA & B\nC\nx\ntail"
    # not escaped: a literal "&amp;lt;" is one entity layer, so it shows as "&lt;" (decoded once, not twice)
    assert boards.html_to_text("<p>&amp;lt;b&amp;gt;</p>") == "&lt;b&gt;"


def test_html_to_text_never_raises_on_malformed_input():
    for junk in ("<<<>>><p", "<p>unclosed <b>bold <i>", "</div></div>text", "<script>never closed", "&#99999999999; &bogus;",
                 "<a href='x", "", None):
        assert isinstance(boards.html_to_text(junk), str)
        assert isinstance(boards.html_to_text(junk, escaped=True), str)
    assert "text" in boards.html_to_text("</div></div>text")


# ---- request, result, helpers ---------------------------------------------------------------------------

def test_request_and_result_defaults_are_independent():
    a, b = boards.BoardRequest(terms=["x"], locations=[]), boards.BoardRequest(terms=["x"], locations=[])
    assert (a.allow_remote, a.max_age_days, a.limit, a.name, a.home_country) == (True, 7, 25, None, "india")
    a.title_words.append("sde")
    assert b.title_words == []
    r1, r2 = boards.BoardResult(jobs=[]), boards.BoardResult(jobs=[])
    r1.errors.append("x")
    r1.dropped["title"] += 1
    assert r2.errors == [] and r2.dropped == {"title": 0, "location": 0, "age": 0, "unusable": 0}
    assert (r2.listed, r2.relevant) == (0, 0)


def test_make_job_has_exactly_the_pipeline_keys():
    j = boards.make_job("lever", "acme:1", url="https://x/1", title=" Backend Engineer ", company="Acme")
    assert set(j) == {"source", "source_job_id", "url", "apply_url", "title", "company", "location", "is_remote", "posted_at",
                      "description", "salary_min", "salary_max", "salary_currency", "raw"}
    assert j["source"] == "lever" and j["source_job_id"] == "acme:1" and j["title"] == "Backend Engineer"
    assert j["apply_url"] == "https://x/1"                      # defaults to the posting link
    assert j["description"] == "" and j["posted_at"] is None and j["raw"] == {}
    full = boards.make_job("ashby", "a:2", url="https://x/2", apply_url="https://x/apply", title="T", company="C", location="Pune",
                           is_remote=True, posted_at="2026-10-10T00:00:00+00:00", description="d", salary_min=1, salary_max=2,
                           salary_currency="USD", raw={"id": 2})
    assert full["apply_url"] == "https://x/apply" and full["is_remote"] is True and full["raw"] == {"id": 2}
    assert (full["salary_min"], full["salary_max"], full["salary_currency"]) == (1, 2, "USD")


def test_company_name_prefers_payload_then_request_then_slug():
    assert boards.company_name("my-co.x_y", "Given", "Payload Inc") == "Payload Inc"
    assert boards.company_name("my-co.x_y", "Given", None) == "Given"
    assert boards.company_name("my-co.x_y", None, "  ") == "My Co X Y"
    assert boards.company_name("stripe", "", "") == "Stripe"


def test_min_intervals_follow_the_spec():
    assert boards.MIN_INTERVAL_S == {"remotive": 21600, "remoteok": 3600, "weworkremotely": 3600, "himalayas": 3600,
                                     "arbeitnow": 3600, "board": 600}


def test_base_url_is_fixed_per_system_and_honours_the_test_hook(monkeypatch):
    monkeypatch.delenv("BOARDS_API_URL", raising=False)
    assert boards.base_url("greenhouse") == "https://boards-api.greenhouse.io"
    assert boards.base_url("lever") == "https://api.lever.co"
    assert boards.base_url("lever_eu") == "https://api.eu.lever.co"
    assert boards.base_url("ashby") == "https://api.ashbyhq.com"
    assert boards.base_url("workable") == "https://apply.workable.com"
    assert boards.base_url("smartrecruiters") == "https://api.smartrecruiters.com"
    assert boards.base_url("remotive") == "https://remotive.com"
    assert boards.base_url("remoteok") == "https://remoteok.com"
    assert boards.base_url("weworkremotely") == "https://weworkremotely.com"
    assert boards.base_url("himalayas") == "https://himalayas.app"
    assert boards.base_url("arbeitnow") == "https://www.arbeitnow.com"
    monkeypatch.setenv("BOARDS_API_URL", "http://127.0.0.1:9999/")           # read at call time; a trailing slash is harmless
    assert boards.base_url("greenhouse") == "http://127.0.0.1:9999/greenhouse"
    assert boards.base_url("lever_eu") == "http://127.0.0.1:9999/lever_eu"
    with pytest.raises(ValueError):
        boards.base_url("evil.example.com/x")


# ---- get_bytes ------------------------------------------------------------------------------------------

def test_get_bytes_accepts_13mb_and_refuses_33mb(monkeypatch):
    big = stubs.gh_job(description="x" * 13_000_000)                       # the largest real response seen was 13.5 MB
    with stubs.StubBoards(data={("greenhouse", "acme"): [big], ("greenhouse", "huge"): [stubs.gh_job()]},
                          modes={("greenhouse", "huge"): "huge"}) as stub:
        monkeypatch.setenv("BOARDS_API_URL", stub.url)
        with httpx.Client(timeout=httpx.Timeout(30.0)) as client:
            body = boards.get_bytes(client, "GET", boards.base_url("greenhouse") + "/v1/boards/acme/jobs?content=true")
            assert len(body) > 13_000_000
            with pytest.raises(boards.BoardError, match="larger than 32000000 bytes"):
                boards.get_bytes(client, "GET", boards.base_url("greenhouse") + "/v1/boards/huge/jobs")


def test_get_bytes_counts_streamed_bytes_when_there_is_no_content_length(monkeypatch):
    url = hooked_url(monkeypatch)
    def handler(request):
        return httpx.Response(200, content=(b"x" * 400 for _ in range(10)))   # chunked: no Content-Length to check early
    with raw_client(handler) as client:
        assert boards.get_bytes(client, "GET", url, max_bytes=4000) == b"x" * 4000
        with pytest.raises(boards.BoardError, match="larger than 3999 bytes"):
            boards.get_bytes(client, "GET", url, max_bytes=3999)


def test_get_bytes_checks_content_length_before_reading(monkeypatch):
    url = hooked_url(monkeypatch)
    read = []

    def handler(request):
        def body():
            read.append(1)
            yield b"x" * 10
        return httpx.Response(200, headers={"Content-Length": "5000"}, content=body())
    with raw_client(handler) as client:
        with pytest.raises(boards.BoardError, match="larger than 100 bytes"):
            boards.get_bytes(client, "GET", url, max_bytes=100)
    assert read == []


def test_get_bytes_429_raises_rate_limited_without_retry(monkeypatch):
    with stubs.StubBoards(modes={("greenhouse", "acme"): "rate_limited"}) as stub, httpx.Client() as client:
        monkeypatch.setenv("BOARDS_API_URL", stub.url)
        with pytest.raises(boards.RateLimited):
            boards.get_bytes(client, "GET", boards.base_url("greenhouse") + "/v1/boards/acme/jobs")
        assert stub.requests == 1
    assert issubclass(boards.RateLimited, boards.BoardError)


def test_get_bytes_retries_once_on_500_then_gives_up(monkeypatch):
    with stubs.StubBoards(modes={("greenhouse", "acme"): "server_error"}) as stub, httpx.Client() as client:
        monkeypatch.setenv("BOARDS_API_URL", stub.url)
        with pytest.raises(boards.BoardError, match="500"):
            boards.get_bytes(client, "GET", boards.base_url("greenhouse") + "/v1/boards/acme/jobs")
        assert stub.requests == 2


def test_get_bytes_retry_can_succeed(monkeypatch):
    url = hooked_url(monkeypatch)
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(503) if len(calls) == 1 else httpx.Response(200, content=b"[1]")
    with raw_client(handler) as client:
        assert boards.get_bytes(client, "GET", url) == b"[1]"
    assert len(calls) == 2


def test_get_bytes_retries_once_on_a_timeout_then_gives_up(monkeypatch):
    with stubs.StubBoards(modes={("greenhouse", "acme"): "slow"}) as stub:
        monkeypatch.setenv("BOARDS_API_URL", stub.url)
        stub.slow_seconds = 1.5
        with httpx.Client(timeout=httpx.Timeout(0.3)) as client:
            with pytest.raises(boards.BoardError, match="timed out|network"):
                boards.get_bytes(client, "GET", boards.base_url("greenhouse") + "/v1/boards/acme/jobs")
        assert stub.requests == 2


def test_get_bytes_retries_once_on_a_connection_error_then_gives_up(monkeypatch):
    url = hooked_url(monkeypatch)
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ConnectError("refused", request=request)
    with raw_client(handler) as client:
        with pytest.raises(boards.BoardError, match="network"):
            boards.get_bytes(client, "GET", url)
    assert len(calls) == 2


def test_get_bytes_404_and_other_4xx(monkeypatch):
    with stubs.StubBoards(data={("greenhouse", "acme"): []}) as stub, httpx.Client() as client:
        monkeypatch.setenv("BOARDS_API_URL", stub.url)
        with pytest.raises(boards.BoardError, match=r"not found \(check the slug\)"):
            boards.get_bytes(client, "GET", boards.base_url("greenhouse") + "/v1/boards/nobody/jobs")
        assert stub.requests == 1                                           # a 404 is not retried
    url = hooked_url(monkeypatch)                                           # the stub's origin is only allowed while it is the hook
    with raw_client(lambda request: httpx.Response(403)) as client:
        with pytest.raises(boards.BoardError, match="403"):
            boards.get_bytes(client, "GET", url)


def test_get_bytes_does_not_follow_redirects(monkeypatch):
    url = hooked_url(monkeypatch, "/start")
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://127.0.0.1/elsewhere"})
    with raw_client(handler, follow_redirects=True) as client:               # even a client that would follow is overridden
        with pytest.raises(boards.BoardError, match="unexpected redirect"):
            boards.get_bytes(client, "GET", url)
    assert seen == [url]


def test_get_bytes_sends_the_honest_user_agent_and_posts_json(monkeypatch):
    url = hooked_url(monkeypatch)
    seen = {}

    def handler(request):
        seen.update(ua=request.headers.get("user-agent"), method=request.method, body=request.content)
        return httpx.Response(200, content=b"{}")
    with raw_client(handler) as client:
        boards.get_bytes(client, "POST", url, json_body={"query": "backend"})
        with pytest.raises(ValueError):
            boards.get_bytes(client, "DELETE", url)
    assert seen["ua"] == "ai-job-search-n8n/1.0 (personal job search)" and seen["method"] == "POST"
    assert b'"query"' in seen["body"]


def test_get_json_turns_garbage_into_a_board_error(monkeypatch):
    with stubs.StubBoards(modes={("greenhouse", "acme"): "garbage"}) as stub, httpx.Client() as client:
        monkeypatch.setenv("BOARDS_API_URL", stub.url)
        with pytest.raises(boards.BoardError, match="not JSON"):
            boards.get_json(client, "GET", boards.base_url("greenhouse") + "/v1/boards/acme/jobs")
    url = hooked_url(monkeypatch)
    with raw_client(lambda request: httpx.Response(200, content=b'{"jobs": []}')) as client:
        assert boards.get_json(client, "GET", url) == {"jobs": []}


# ---- fetch ----------------------------------------------------------------------------------------------

def test_fetch_rejects_bad_slug_without_any_request(monkeypatch):
    with stubs.StubBoards() as stub:
        monkeypatch.setenv("BOARDS_API_URL", stub.url)
        monkeypatch.setitem(boards.ADAPTERS, "greenhouse", lambda client, slug, r, now: client.get(f"{stub.url}/greenhouse/x") and ([], 0))
        monkeypatch.setitem(boards.ADAPTERS, "remotive", lambda client, slug, r, now: client.get(f"{stub.url}/remotive/x") and ([], 0))
        cases = [("greenhouse", "../etc"), ("greenhouse", "a b"), ("greenhouse", "a/b"), ("greenhouse", ""), ("greenhouse", None),
                 ("greenhouse", "-x"), ("greenhouse", "a" * 65), ("remotive", "acme"),          # a feed must not have a slug
                 ("nosuchsource", "acme"), ("linkedin", "acme"), ("lever_eu", "acme"), ("", None)]
        for source, slug in cases:
            result = boards.fetch(source, slug, req())
            assert result.jobs == [] and len(result.errors) == 1, (source, slug, result.errors)
            assert (result.listed, result.relevant) == (0, 0)
        assert stub.requests == 0
        assert "slug" in boards.fetch("greenhouse", "../etc", req()).errors[0]
        assert "slug" in boards.fetch("remotive", "acme", req()).errors[0]
        assert "unknown source" in boards.fetch("nosuchsource", "acme", req()).errors[0]


def test_fetch_reports_a_source_without_an_adapter_instead_of_crashing(monkeypatch):
    monkeypatch.delitem(boards.ADAPTERS, "ashby", raising=False)
    result = boards.fetch("ashby", "acme", req())
    assert result.jobs == [] and len(result.errors) == 1 and "ashby/acme" in result.errors[0]


def test_fetch_applies_relevance_and_limit(monkeypatch):
    relevant = [job(posted=day(1 + i % 6), jid=f"ok{i}") for i in range(30)]           # 1..6 days old: all within 7
    for i, j in enumerate(relevant):                                                    # distinct minutes so the order is strict
        j["posted_at"] = (NOW - timedelta(days=1 + i % 6, minutes=i)).isoformat()
    bad_title = [job(title="Marketing Manager", jid=f"t{i}") for i in range(4)]
    bad_loc = [job(location="London, United Kingdom", jid=f"l{i}") for i in range(3)]
    too_old = [job(posted=day(30), jid=f"a{i}") for i in range(3)]
    everything = relevant[:10] + bad_title + relevant[10:25] + bad_loc + too_old + relevant[25:]
    assert len(everything) == 40
    register(monkeypatch, "greenhouse", everything, listed=40)
    result = boards.fetch("greenhouse", "acme", req(limit=25), now=NOW)
    assert result.errors == []
    assert len(result.jobs) == 25
    assert (result.listed, result.relevant) == (40, 30)
    assert result.dropped == {"title": 4, "location": 3, "age": 3, "unusable": 0}
    dates = [j["posted_at"] for j in result.jobs]
    assert dates == sorted(dates, reverse=True)                                         # newest first
    newest_25 = sorted(relevant, key=lambda j: j["posted_at"], reverse=True)[:25]
    assert [j["source_job_id"] for j in result.jobs] == [j["source_job_id"] for j in newest_25]


def test_fetch_sorts_unknown_dates_last_and_counts_unusable_items(monkeypatch):
    undated = job(jid="u")
    undated["posted_at"] = None
    mixed = [undated, job(posted=day(3), jid="old"), job(posted=day(1), jid="new"),
             {"title": "Backend Engineer"}, "garbage", None,                             # no id/url, not a dict
             dict(job(jid="notitle"), title="")]
    register(monkeypatch, "lever", mixed)
    result = boards.fetch("lever", "acme", req(), now=NOW)
    assert [j["source_job_id"] for j in result.jobs] == ["acme:new", "acme:old", "acme:u"]
    assert result.dropped["unusable"] == 4 and result.listed == 7 and result.relevant == 3


def test_fetch_uses_title_words_terms_and_age_from_the_request(monkeypatch):
    register(monkeypatch, "lever", [job(title="Platform SDE", jid="1"), job(title="Backend Engineer", jid="2", posted=day(20))])
    result = boards.fetch("lever", "acme", req(title_words=["sde"], terms=["data scientist"], max_age_days=30), now=NOW)
    assert [j["source_job_id"] for j in result.jobs] == ["acme:1"] and result.dropped["title"] == 1
    result = boards.fetch("lever", "acme", req(title_words=["sde"], terms=["data scientist"], max_age_days=7), now=NOW)
    assert result.dropped == {"title": 1, "location": 0, "age": 0, "unusable": 0} and len(result.jobs) == 1
    result = boards.fetch("lever", "acme", req(allow_remote=False, locations=["London, United Kingdom"], title_words=["sde"]), now=NOW)   # nothing is in London
    assert result.jobs == [] and result.dropped["location"] == 2


def test_fetch_limit_zero_returns_nothing_but_still_counts(monkeypatch):
    register(monkeypatch, "lever", [job(jid="1"), job(jid="2")])
    result = boards.fetch("lever", "acme", req(limit=0), now=NOW)
    assert result.jobs == [] and result.relevant == 2


def test_fetch_makes_and_closes_a_default_client_and_leaves_a_given_one_open(monkeypatch):
    seen = {}

    def adapter(client, slug, r, now):
        seen["client"] = client
        return [], 0
    monkeypatch.setitem(boards.ADAPTERS, "lever", adapter)
    boards.fetch("lever", "acme", req())
    made = seen["client"]
    assert made.is_closed and made.follow_redirects is False
    assert made.headers["user-agent"] == "ai-job-search-n8n/1.0 (personal job search)"
    assert made.timeout == httpx.Timeout(30.0)
    mine = httpx.Client()
    try:
        boards.fetch("lever", "acme", req(), client=mine)
        assert seen["client"] is mine and not mine.is_closed
    finally:
        mine.close()


def test_fetch_closes_the_default_client_even_when_the_adapter_fails(monkeypatch):
    seen = {}

    def adapter(client, slug, r, now):
        seen["client"] = client
        raise RuntimeError("boom")
    monkeypatch.setitem(boards.ADAPTERS, "lever", adapter)
    result = boards.fetch("lever", "acme", req())
    assert seen["client"].is_closed and len(result.errors) == 1 and result.errors[0].startswith("lever/acme: ")


def fetch_through(monkeypatch, stub, system="greenhouse", slug="acme", source=None):
    """An adapter that just downloads the stub's JSON, so the failure modes of the HTTP layer reach fetch()."""
    path = {"greenhouse": f"/v1/boards/{slug}/jobs"}[system]

    def adapter(client, s, r, now):
        data = boards.get_json(client, "GET", boards.base_url(system) + path)
        return [job(jid=str(j["id"])) for j in data["jobs"]], len(data["jobs"])
    monkeypatch.setenv("BOARDS_API_URL", stub.url)
    monkeypatch.setitem(boards.ADAPTERS, source or system, adapter)
    return boards.fetch(source or system, slug, req(), now=NOW)


def test_fetch_turns_adapter_exceptions_into_errors(monkeypatch):
    with stubs.StubBoards(data={("greenhouse", "acme"): [stubs.gh_job(id=1)]},
                          modes={("greenhouse", "garbage"): "garbage", ("greenhouse", "boom"): "server_error"}) as stub:
        ok = fetch_through(monkeypatch, stub)
        assert ok.errors == [] and [j["source_job_id"] for j in ok.jobs] == ["acme:1"]     # the helper itself works

        garbage = fetch_through(monkeypatch, stub, slug="garbage")
        assert garbage.jobs == [] and garbage.errors == ["greenhouse/garbage: returned something that is not JSON"]

        missing = fetch_through(monkeypatch, stub, slug="nobody")
        assert missing.jobs == [] and missing.errors == ["greenhouse/nobody: not found (check the slug)"]

        broken = fetch_through(monkeypatch, stub, slug="boom")
        assert len(broken.errors) == 1 and broken.errors[0].startswith("greenhouse/boom: ") and "500" in broken.errors[0]


def test_fetch_turns_a_rate_limit_into_a_skip_message(monkeypatch):
    with stubs.StubBoards(modes={("greenhouse", "acme"): "rate_limited"}) as stub:
        result = fetch_through(monkeypatch, stub)
        assert stub.requests == 1 and result.jobs == []
        assert len(result.errors) == 1 and result.errors[0].startswith("greenhouse/acme: ")
        assert "rate limited" in result.errors[0] and "skipped for this run" in result.errors[0]


def test_fetch_survives_every_kind_of_adapter_failure(monkeypatch):
    failures = [KeyError("jobs"), TypeError("'NoneType' object is not iterable"), AttributeError("x"), IndexError(0),
                ValueError("bad json"), httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), RuntimeError("boom"),
                boards.BoardError("custom problem")]
    for exc in failures:
        def adapter(client, slug, r, now, exc=exc):
            raise exc
        monkeypatch.setitem(boards.ADAPTERS, "remotive", adapter)
        result = boards.fetch("remotive", None, req())
        assert result.jobs == [] and len(result.errors) == 1, exc
        assert result.errors[0].startswith("remotive: "), result.errors      # a feed has no slug in its label
    # the schema-change family reads as such, not as a crash
    monkeypatch.setitem(boards.ADAPTERS, "remotive", lambda *a: {}["jobs"])
    assert "unexpected format" in boards.fetch("remotive", None, req()).errors[0]


def test_fetch_a_feed_works_without_a_slug(monkeypatch):
    register(monkeypatch, "remotive", [job(jid="r1")])
    result = boards.fetch("remotive", None, req(), now=NOW)
    assert result.errors == [] and len(result.jobs) == 1


# ---- hardening batch (Task 9b) ----------------------------------------------------------------------------
# These tests build their URLs from boards.base_url() under a BOARDS_API_URL hook, so they stay valid when the
# fetch layer only accepts the known job-board origins.

def hooked_url(monkeypatch, path="/x", system="greenhouse"):
    monkeypatch.setenv("BOARDS_API_URL", "http://127.0.0.1:9")
    return boards.base_url(system) + path


# A1: a gzip body that decodes to far more than the limit is refused BEFORE it is buffered

def test_get_bytes_refuses_an_oversized_gzip_chunk_before_buffering_it(monkeypatch):
    peak = []

    class Counting(bytearray):
        def extend(self, data):
            super().extend(data)
            peak.append(len(self))
    monkeypatch.setattr(boards, "bytearray", Counting, raising=False)      # the buffer get_bytes accumulates the body in
    packed = gzip.compress(b"\0" * 5_000_000)                              # a few KB on the wire, 5 MB decoded in ONE chunk
    assert len(packed) < 100_000

    def handler(request):
        return httpx.Response(200, headers={"Content-Encoding": "gzip"}, content=packed)
    url = hooked_url(monkeypatch)
    with raw_client(handler) as client:
        with pytest.raises(boards.BoardError, match="larger than 100000 bytes"):
            boards.get_bytes(client, "GET", url, max_bytes=100_000)
    assert max(peak or [0]) <= 100_000                                     # the decoded chunk never entered the buffer


def test_get_bytes_still_accepts_a_gzip_body_within_the_limit(monkeypatch):
    packed = gzip.compress(b"y" * 50_000)

    def handler(request):
        return httpx.Response(200, headers={"Content-Encoding": "gzip"}, content=packed)
    url = hooked_url(monkeypatch)
    with raw_client(handler) as client:
        assert boards.get_bytes(client, "GET", url, max_bytes=50_000) == b"y" * 50_000


# A2: html_to_text caps its input (the stdlib parser is quadratic on hostile markup on some Pythons)

def test_html_to_text_never_feeds_the_parser_more_than_the_cap(monkeypatch):
    fed = []
    monkeypatch.setattr(boards._TextExtractor, "feed", lambda self, data: fed.append(len(data)))   # records, parses nothing
    boards.html_to_text("x" * 5_000_000)
    assert boards.MAX_HTML_CHARS == 200_000 and fed == [boards.MAX_HTML_CHARS]


def test_html_to_text_truncates_a_posting_over_the_cap():
    out = boards.html_to_text("y" * 300_000)
    assert len(out) == boards.MAX_HTML_CHARS
    long_text = "word " * 100_000                                         # 500,000 characters of ordinary text
    assert 0 < len(boards.html_to_text(long_text)) <= boards.MAX_HTML_CHARS


def test_html_to_text_leaves_text_under_the_cap_unchanged():
    body = "<p>Build the <b>API</b>.</p>" + "<li>item</li>" * 5000         # about 70,000 characters, under the cap
    assert len(body) < boards.MAX_HTML_CHARS
    out = boards.html_to_text(body)
    assert out.startswith("Build the API.\nitem\nitem") and out.count("item") == 5000
    assert boards.html_to_text("<p>a &amp; b</p>") == "a & b"


@pytest.mark.parametrize("junk", ["<a", "<a ", '<a href="', "<!--", "<a<", "<script"])
def test_html_to_text_hostile_unterminated_markup_is_fast(junk):
    started = time.perf_counter()
    out = boards.html_to_text(junk * (5_000_000 // len(junk)))              # 5 MB of unterminated tags
    assert time.perf_counter() - started < 2.0
    assert isinstance(out, str) and out


def test_html_to_text_a_stray_less_than_sign_keeps_its_text():
    assert boards.html_to_text("a < b and 3<4 and x<y") == "a < b and 3<4 and x<y"
    assert boards.html_to_text("<p>1 < 2</p><p>ok</p>") == "1 < 2\nok"


# A3: the error wording and logging of fetch(), and what is worth a retry

def test_a_value_error_from_an_adapter_is_an_unexpected_value_not_json(monkeypatch):
    def adapter(client, slug, r, now):
        raise ValueError("bad number")
    monkeypatch.setitem(boards.ADAPTERS, "remotive", adapter)
    assert boards.fetch("remotive", None, req()).errors == ["remotive: returned an unexpected value"]


def test_a_schema_change_error_logs_the_traceback_once(monkeypatch, caplog):
    def adapter(client, slug, r, now):
        return {}["jobs"]                                                  # a programming bug looks exactly like this
    monkeypatch.setitem(boards.ADAPTERS, "greenhouse", adapter)
    with caplog.at_level(logging.WARNING, logger="scraper.boards"):
        result = boards.fetch("greenhouse", "acme", req())
    assert result.errors == ["greenhouse/acme: returned an unexpected format (the source may have changed)"]
    records = [r for r in caplog.records if r.name == "scraper.boards" and r.levelno == logging.WARNING]
    assert len(records) == 1 and records[0].exc_info and records[0].exc_info[0] is KeyError
    assert "greenhouse/acme" in records[0].getMessage()


@pytest.mark.parametrize("exc_type", [KeyError, TypeError, AttributeError, IndexError])
def test_every_schema_change_exception_is_logged_with_its_traceback(monkeypatch, caplog, exc_type):
    def adapter(client, slug, r, now):
        raise exc_type("x")
    monkeypatch.setitem(boards.ADAPTERS, "lever", adapter)
    with caplog.at_level(logging.WARNING, logger="scraper.boards"):
        boards.fetch("lever", "acme", req())
    assert [r.exc_info[0] for r in caplog.records if r.levelno == logging.WARNING] == [exc_type]


def test_get_bytes_retries_once_on_a_read_error_then_gives_up(monkeypatch):
    calls = []

    def handler(request):
        calls.append(1)
        raise httpx.ReadError("connection reset", request=request)
    url = hooked_url(monkeypatch)
    with raw_client(handler) as client:
        with pytest.raises(boards.BoardError, match="network"):
            boards.get_bytes(client, "GET", url)
    assert len(calls) == 2


@pytest.mark.parametrize("exc_type", [httpx.UnsupportedProtocol, httpx.ProxyError, httpx.RemoteProtocolError,
                                      httpx.WriteError, httpx.CloseError, httpx.LocalProtocolError])
def test_get_bytes_does_not_retry_other_transport_errors(monkeypatch, exc_type):
    calls = []

    def handler(request):
        calls.append(1)
        raise exc_type("nope", request=request)
    url = hooked_url(monkeypatch)
    with raw_client(handler) as client:
        with pytest.raises(boards.BoardError, match=f"network error \\({exc_type.__name__}\\)"):
            boards.get_bytes(client, "GET", url)
    assert len(calls) == 1                                                 # failed at once: no retry, no pause


# A5: a broken proxy environment is an error line; the limit is clamped inside fetch()

def test_fetch_survives_a_broken_proxy_environment(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "::bad::")
    monkeypatch.setenv("HTTP_PROXY", "::bad::")
    monkeypatch.setenv("ALL_PROXY", "::bad::")
    register(monkeypatch, "lever", [job(jid="1")])
    result = boards.fetch("lever", "acme", req(), now=NOW)                  # no client given: fetch() builds its own
    assert result.jobs == [] and len(result.errors) == 1 and result.errors[0].startswith("lever/acme: ")


def test_fetch_clamps_a_missing_or_huge_limit(monkeypatch):
    many = [job(posted=(NOW - timedelta(minutes=i + 1)).isoformat(), jid=f"j{i}") for i in range(150)]
    register(monkeypatch, "lever", many)
    seen = []
    real = boards.ADAPTERS["lever"]
    monkeypatch.setitem(boards.ADAPTERS, "lever", lambda client, slug, r, now: (seen.append(r.limit), real(client, slug, r, now))[1])
    assert len(boards.fetch("lever", "acme", req(limit=None), now=NOW).jobs) == 25            # None: the default
    assert len(boards.fetch("lever", "acme", req(limit=10 ** 9), now=NOW).jobs) == 100        # huge: at most 100
    assert len(boards.fetch("lever", "acme", req(limit=100), now=NOW).jobs) == 100
    assert len(boards.fetch("lever", "acme", req(limit=7), now=NOW).jobs) == 7
    assert seen == [25, 100, 100, 7]                                                          # the adapter sees the clamped value too


def test_fetch_does_not_modify_the_callers_request(monkeypatch):
    register(monkeypatch, "lever", [job(jid="1")])
    r = req(limit=None)
    boards.fetch("lever", "acme", r, now=NOW)
    assert r.limit is None


# A6: a posting dated far in the future sorts as "unknown date" (last), so it cannot take a top slot

def test_a_far_future_posting_cannot_take_a_top_slot(monkeypatch):
    future = job(jid="y9999", posted="9999-12-31T00:00:00+00:00")
    tomorrow_ok = job(jid="soon", posted=(NOW + timedelta(hours=20)).isoformat())     # within a day of now: clock skew, still a date
    real = [job(jid="new", posted=day(1)), job(jid="old", posted=day(3))]
    undated = job(jid="u")
    undated["posted_at"] = None
    register(monkeypatch, "lever", [future, undated, real[1], tomorrow_ok, real[0]])
    result = boards.fetch("lever", "acme", req(limit=2), now=NOW)
    assert [j["source_job_id"] for j in result.jobs] == ["acme:soon", "acme:new"]     # not the year-9999 one
    everything = boards.fetch("lever", "acme", req(), now=NOW)
    assert [j["source_job_id"] for j in everything.jobs] == ["acme:soon", "acme:new", "acme:old", "acme:y9999", "acme:u"]


def test_the_newest_first_key_treats_the_future_as_unknown():
    ahead = {"posted_at": (NOW + timedelta(days=2)).isoformat()}
    near = {"posted_at": (NOW + timedelta(hours=23)).isoformat()}
    unknown = {"posted_at": None}
    assert boards._newest_first_key(ahead, NOW) == boards._newest_first_key(unknown, NOW)
    assert boards._newest_first_key(near, NOW)[0] is False
    assert boards._newest_first_key(ahead)[0] is False                                # without a clock, nothing is judged


# A7: more block tags, and unicode spaces

def test_html_to_text_table_cells_and_other_block_tags_are_lines():
    assert boards.html_to_text("<td>Salary</td><td>100k</td>") == "Salary\n100k"
    assert boards.html_to_text("<tr><th>Role</th><th>Pay</th></tr>") == "Role\nPay"
    for tag in ("section", "article", "blockquote", "pre", "dt", "dd"):
        assert boards.html_to_text(f"<{tag}>one</{tag}><{tag}>two</{tag}>") == "one\ntwo", tag
    assert boards.html_to_text("above<hr>below") == "above\nbelow"
    assert boards.html_to_text("above<hr/>below") == "above\nbelow"


def test_html_to_text_collapses_zero_width_and_unicode_spaces():
    assert boards.html_to_text("a​b﻿c") == "abc"                           # ZWSP and BOM: removed
    for space in (" ", " ", " ", " ", "　"):
        assert boards.html_to_text(f"Senior{space}{space}Engineer") == "Senior Engineer", repr(space)
    assert boards.html_to_text("﻿<p>​ Role   here　</p>") == "Role here"


# A4: the fetch layer only talks to the known job-board origins (computed from base_url() at call time)

def counting_client(calls):
    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, content=b"[]")
    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("url", ["https://evil.example/v1/boards/acme/jobs", "http://boards-api.greenhouse.io/v1/x",
                                 "https://boards-api.greenhouse.io:8443/v1/x", "https://boards-api.greenhouse.io.evil.example/",
                                 "https://boards-api.greenhouse.io@evil.example/", "https://evil.example/https://boards-api.greenhouse.io/",
                                 "file:///etc/passwd", "ftp://api.lever.co/x", "//api.lever.co/x", "", "https://api.lever.co:notaport/x",
                                 # urlsplit reads the bracketed part as the host, httpx sends the whole string: a parser differential
                                 "https://x[boards-api.greenhouse.io].attacker.com/v1/boards/acme/jobs", "https://attacker.com[api.lever.co]/x",
                                 "https://x[api.lever.co].attacker.com/x", "https://user:pw@boards-api.greenhouse.io/",
                                 "https://evil.example\\@boards-api.greenhouse.io/", "https://[::1]/x",
                                 "https://api.lever.co/x\x00y"])                  # httpx.URL itself raises InvalidURL for this one
def test_a_url_outside_the_known_origins_is_refused_with_no_request(monkeypatch, url):
    monkeypatch.delenv("BOARDS_API_URL", raising=False)
    calls = []
    with counting_client(calls) as client:
        with pytest.raises(boards.BoardError, match="refused: not a known job-board host"):
            boards.get_bytes(client, "GET", url)
    assert calls == []


def test_every_known_origin_is_accepted(monkeypatch):
    monkeypatch.delenv("BOARDS_API_URL", raising=False)
    calls = []
    with counting_client(calls) as client:
        for system in tuple(sources.BOARD_SYSTEMS) + tuple(sources.FEEDS) + ("lever_eu",):
            assert boards.get_bytes(client, "GET", boards.base_url(system) + "/some/path?x=1") == b"[]"
    assert len(calls) == 11


def test_the_test_hook_origin_is_allowed_at_call_time(monkeypatch):
    monkeypatch.delenv("BOARDS_API_URL", raising=False)
    calls = []
    with counting_client(calls) as client:
        with pytest.raises(boards.BoardError, match="refused"):
            boards.get_bytes(client, "GET", "http://127.0.0.1:9999/greenhouse/x")
        monkeypatch.setenv("BOARDS_API_URL", "http://127.0.0.1:9999")                 # read at call time
        assert boards.get_bytes(client, "GET", "http://127.0.0.1:9999/greenhouse/x") == b"[]"
        with pytest.raises(boards.BoardError, match="refused"):
            boards.get_bytes(client, "GET", "http://127.0.0.1:9998/greenhouse/x")      # another port: another origin
        with pytest.raises(boards.BoardError, match="refused"):
            boards.get_bytes(client, "GET", "https://boards-api.greenhouse.io/x")      # the override replaces the real hosts
    assert len(calls) == 1


def test_a_default_port_is_the_same_origin(monkeypatch):
    monkeypatch.delenv("BOARDS_API_URL", raising=False)
    calls = []
    with counting_client(calls) as client:
        assert boards.get_bytes(client, "GET", "https://API.LEVER.CO:443/v0/postings/x") == b"[]"


def test_a_quoted_less_than_in_an_attribute_value_is_read_as_text():
    # the documented consequence of the unterminated-"<" pre-pass in html_to_text: the tag that holds such a value is not a tag any more;
    # its opening part shows up as text and what follows the stray "<" is parsed on its own (here "<b">" is dropped as a tag)
    assert boards.html_to_text('<a title="a<b">link</a>') == '<a title="alink'
    assert boards.html_to_text('<p>ok</p><a title="x<y">z</a>') == 'ok\n<a title="xz'


_ADAPTER_PATHS = ["/v1/boards/acme/jobs", "/v1/boards/acme/jobs/4242", "/v0/postings/Acme?mode=json", "/posting-api/job-board/a.b_c-1",
                  "/api/v3/accounts/acme/jobs", "/api/v2/accounts/acme/jobs/ABC123DEF4",
                  "/v1/companies/BoschGroup/postings?limit=100&offset=0&q=backend+engineer&country=in",
                  "/v1/companies/BoschGroup/postings/744000000009000", "/api/remote-jobs?category=software-dev", "/api",
                  "/categories/remote-programming-jobs.rss", "/jobs/api?limit=20&cursor=a%26b%3Dc+d", "/api/job-board-api?page=3", "/"]


@pytest.mark.parametrize("hook", [None, "http://127.0.0.1:9999", "http://127.0.0.1:9999/"])
def test_every_url_shape_an_adapter_builds_is_accepted_and_goes_to_the_checked_host(monkeypatch, hook):
    if hook:
        monkeypatch.setenv("BOARDS_API_URL", hook)
    else:
        monkeypatch.delenv("BOARDS_API_URL", raising=False)
    seen = []

    def handler(request):
        seen.append((request.url.scheme, request.url.host, request.url.port))
        return httpx.Response(200, content=b"[]")
    with raw_client(handler) as client:
        for system in tuple(sources.BOARD_SYSTEMS) + tuple(sources.FEEDS) + ("lever_eu",):
            for path in _ADAPTER_PATHS:
                url = boards.base_url(system) + path
                before = len(seen)
                assert boards.get_bytes(client, "GET", url) == b"[]", url
                # the differential check: the host the transport was asked for is the host the allowlist judged
                parsed = httpx.URL(url)
                assert seen[before] == (parsed.scheme, parsed.host, parsed.port), url
    assert len(seen) == 11 * len(_ADAPTER_PATHS)
