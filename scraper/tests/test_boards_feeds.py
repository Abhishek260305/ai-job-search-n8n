"""The three remote-feed adapters in boards.py: Remotive, RemoteOK and We Work Remotely (spec sections 2 and 6).

Feeds are one request for many companies' jobs (no slug; the stub keys them with slug ""). Everything runs against the
StubBoards stand-in (or an in-process mock transport for per-request failures); nothing touches the internet. Each
test goes through boards.fetch(), so the relevance filter, the cap and the one-line error handling are exercised
exactly as production uses them."""
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import boards
import stubs

FEEDS = ("remotive", "remoteok", "weworkremotely")
BUILDERS = {"remotive": stubs.remotive_job, "remoteok": stubs.remoteok_job, "weworkremotely": stubs.wwr_item}
POSTED = datetime(2026, 10, 9, 8, 30, 0, tzinfo=timezone.utc)              # old enough that tests raise max_age_days


@pytest.fixture(autouse=True)
def _fast_retry(monkeypatch):
    monkeypatch.setattr(boards, "RETRY_DELAY_S", 0)


def req(**kw):
    base = dict(terms=["backend engineer"], locations=["Bengaluru"], allow_remote=True, max_age_days=3650)
    base.update(kw)
    return boards.BoardRequest(**base)


def ago(days=0, hours=0):
    return datetime.now(timezone.utc) - timedelta(days=days, hours=hours)


def run(monkeypatch, stub, system, **kw):
    """boards.fetch of a feed against the stub (the BOARDS_API_URL hook points every system at it)."""
    monkeypatch.setenv("BOARDS_API_URL", stub.url)
    return boards.fetch(system, None, req(**kw))


def mock_client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def feed(system, items):
    return stubs.StubBoards(data={(system, ""): items})


# ---- Remotive ---------------------------------------------------------------------------------------------

def test_remotive_maps_a_job(monkeypatch):
    post = stubs.remotive_job("Backend Engineer", "Worldwide", POSTED, 2468, company="Acme Corp", salary="$90k - $120k",
                              description="<p>Build the <b>API</b>.</p><ul><li>Python</li></ul>")
    with feed("remotive", [post]) as stub:
        result = run(monkeypatch, stub, "remotive")
    assert result.errors == [] and result.listed == 1 and result.relevant == 1
    (job,) = result.jobs
    assert job["source"] == "remotive" and job["source_job_id"] == "2468"          # the feed's own id: feeds have no slug
    assert job["title"] == "Backend Engineer" and job["company"] == "Acme Corp"
    assert job["url"] == post["url"] and job["apply_url"] == post["url"]
    assert job["location"] == "Worldwide" and job["is_remote"] is True
    assert job["posted_at"] == "2026-10-09T08:30:00+00:00"                          # naive publication_date is UTC
    assert job["description"] == "Build the API.\nPython"
    assert job["salary_min"] is None and job["salary_max"] is None and job["salary_currency"] is None   # free text: not parsed
    assert job["raw"]["id"] == 2468 and job["raw"]["url"] == post["url"] and job["raw"]["job_type"] == "full_time"
    assert "description" not in job["raw"] and "Build the" not in json.dumps(job["raw"])


def test_remotive_asks_for_the_software_dev_category_once(monkeypatch):
    with feed("remotive", [stubs.remotive_job(id=1)]) as stub:
        run(monkeypatch, stub, "remotive")
    assert stub.log == [("GET", "/remotive/api/remote-jobs", "category=software-dev")]


def test_remotive_keeps_its_own_site_url_and_ignores_the_wrapper_keys(monkeypatch):
    post = stubs.remotive_job(id=7)
    with feed("remotive", [post]) as stub:
        result = run(monkeypatch, stub, "remotive")
    (job,) = result.jobs
    assert job["url"].startswith("https://remotive.com/remote-jobs/")             # the terms: link back to Remotive
    assert job["apply_url"] == job["url"]


def test_remotive_empty_feed_is_not_an_error(monkeypatch):
    with stubs.StubBoards(modes={("remotive", ""): "empty"}) as stub:
        result = run(monkeypatch, stub, "remotive")
    assert result.jobs == [] and result.errors == [] and result.listed == 0


def test_remotive_garbage_body_is_one_error_not_a_crash(monkeypatch):
    with stubs.StubBoards(modes={("remotive", ""): "garbage"}) as stub:
        result = run(monkeypatch, stub, "remotive")
    assert result.jobs == [] and result.errors == ["remotive: returned something that is not JSON"]


def test_remotive_rate_limit_is_one_error_and_one_request(monkeypatch):
    with stubs.StubBoards(modes={("remotive", ""): "rate_limited"}) as stub:
        result = run(monkeypatch, stub, "remotive")
    assert result.jobs == [] and result.errors == ["remotive: rate limited by the source; skipped for this run"]
    assert stub.requests == 1


def test_remotive_unexpected_shape_is_one_error():
    for payload in ([], {"other": 1}, {"jobs": "nope"}, None):
        result = boards.fetch("remotive", None, req(), client=mock_client(lambda r, p=payload: httpx.Response(200, content=json.dumps(p).encode())))
        assert result.jobs == [] and len(result.errors) == 1 and "unexpected format" in result.errors[0], payload


def test_remotive_missing_fields_are_skipped(monkeypatch):
    good = stubs.remotive_job(id=5)
    no_id, no_title, no_url = stubs.remotive_job(id=1), stubs.remotive_job(id=2), stubs.remotive_job(id=3)
    del no_id["id"]
    no_title["title"] = ""
    del no_url["url"]
    with feed("remotive", [no_id, no_title, no_url, good]) as stub:      # (the stub's search filter needs dict items)
        result = run(monkeypatch, stub, "remotive")
    assert result.errors == [] and [j["source_job_id"] for j in result.jobs] == ["5"]
    assert result.dropped["unusable"] == 0
    body = json.dumps({"jobs": ["junk", None, 7, good]}).encode()
    junk = boards.fetch("remotive", None, req(), client=mock_client(lambda r: httpx.Response(200, content=body)))
    assert junk.errors == [] and [j["source_job_id"] for j in junk.jobs] == ["5"]


def test_remotive_a_bad_date_is_none_not_an_error(monkeypatch):
    post = stubs.remotive_job(id=9)
    post["publication_date"] = "soon"
    with feed("remotive", [post]) as stub:
        result = run(monkeypatch, stub, "remotive")
    assert result.errors == [] and result.jobs[0]["posted_at"] is None


# ---- RemoteOK ---------------------------------------------------------------------------------------------

def test_remoteok_maps_a_job(monkeypatch):
    post = stubs.remoteok_job("Backend Engineer", "Worldwide", POSTED, "991122", company="Acme Corp", salary_min=90000,
                              salary_max=120000, description="<p>Build the <b>API</b>.</p><ul><li>Python</li></ul>")
    with feed("remoteok", [post]) as stub:
        result = run(monkeypatch, stub, "remoteok")
    assert result.errors == [] and result.listed == 1 and result.relevant == 1
    (job,) = result.jobs
    assert job["source"] == "remoteok" and job["source_job_id"] == "991122"
    assert job["title"] == "Backend Engineer" and job["company"] == "Acme Corp"
    assert job["url"] == post["url"] and job["apply_url"] == post["apply_url"]
    assert job["location"] == "Worldwide" and job["is_remote"] is True
    assert job["posted_at"] == "2026-10-09T08:30:00+00:00"                          # from the epoch (seconds)
    assert job["description"] == "Build the API.\nPython"
    assert job["salary_min"] == 90000 and job["salary_max"] == 120000 and job["salary_currency"] is None
    assert job["raw"]["id"] == "991122" and job["raw"]["url"] == post["url"]
    assert "description" not in job["raw"] and "Build the" not in json.dumps(job["raw"])


def test_remoteok_keeps_its_own_site_url(monkeypatch):
    post = stubs.remoteok_job(id="55")
    with feed("remoteok", [post]) as stub:
        result = run(monkeypatch, stub, "remoteok")
    (job,) = result.jobs
    assert job["url"] == post["url"] and "remoteok.com" in job["url"].lower()       # the terms: link back, name RemoteOK


def test_remoteok_skips_the_legal_notice_row(monkeypatch):
    with feed("remoteok", [stubs.remoteok_job(id="1"), stubs.remoteok_job("Backend Engineer II", id="2")]) as stub:
        monkeypatch.setenv("BOARDS_API_URL", stub.url)
        raw = httpx.get(stub.url + "/remoteok/api").json()
        assert "legal" in raw[0] and "id" not in raw[0]                              # the stub sends it first, as the real API does
        result = boards.fetch("remoteok", None, req())
    assert result.errors == [] and result.listed == 2
    assert sorted(j["source_job_id"] for j in result.jobs) == ["1", "2"]


def test_remoteok_zero_or_missing_salary_and_string_values(monkeypatch):
    zero, text, missing, bad = (stubs.remoteok_job(id=str(i)) for i in range(4))
    text["salary_min"], text["salary_max"] = "80000", "100000"
    del missing["salary_min"]
    missing["salary_max"] = 0
    bad["salary_min"], bad["salary_max"] = "lots", -5
    with feed("remoteok", [zero, text, missing, bad]) as stub:
        result = run(monkeypatch, stub, "remoteok")
    got = {j["source_job_id"]: (j["salary_min"], j["salary_max"]) for j in result.jobs}
    assert got == {"0": (None, None), "1": (80000, 100000), "2": (None, None), "3": (None, None)}


def test_remoteok_epoch_may_be_a_string_and_date_is_the_fallback(monkeypatch):
    as_text, by_date, neither = (stubs.remoteok_job("Backend Engineer", "Worldwide", POSTED, str(i)) for i in range(3))
    as_text["epoch"] = str(as_text["epoch"])
    del by_date["epoch"]
    del neither["epoch"], neither["date"]
    with feed("remoteok", [as_text, by_date, neither]) as stub:
        result = run(monkeypatch, stub, "remoteok")
    got = {j["source_job_id"]: j["posted_at"] for j in result.jobs}
    assert got == {"0": "2026-10-09T08:30:00+00:00", "1": "2026-10-09T08:30:00+00:00", "2": None}


def test_remoteok_empty_location_is_none(monkeypatch):
    with feed("remoteok", [stubs.remoteok_job(location="", id="1")]) as stub:
        result = run(monkeypatch, stub, "remoteok")
    assert result.jobs[0]["location"] is None and result.jobs[0]["is_remote"] is True


def test_remoteok_empty_feed_is_not_an_error(monkeypatch):
    with stubs.StubBoards(modes={("remoteok", ""): "empty"}) as stub:
        result = run(monkeypatch, stub, "remoteok")
    assert result.jobs == [] and result.errors == [] and result.listed == 0


def test_remoteok_only_the_legal_notice_is_an_empty_feed():
    body = json.dumps([{"last_updated": 1, "legal": "terms"}]).encode()
    result = boards.fetch("remoteok", None, req(), client=mock_client(lambda r: httpx.Response(200, content=body)))
    assert result.jobs == [] and result.errors == [] and result.listed == 0


def test_remoteok_garbage_body_is_one_error_not_a_crash(monkeypatch):
    with stubs.StubBoards(modes={("remoteok", ""): "garbage"}) as stub:
        result = run(monkeypatch, stub, "remoteok")
    assert result.jobs == [] and result.errors == ["remoteok: returned something that is not JSON"]


def test_remoteok_rate_limit_is_one_error_and_one_request(monkeypatch):
    with stubs.StubBoards(modes={("remoteok", ""): "rate_limited"}) as stub:
        result = run(monkeypatch, stub, "remoteok")
    assert result.jobs == [] and result.errors == ["remoteok: rate limited by the source; skipped for this run"]
    assert stub.requests == 1


def test_remoteok_unexpected_shape_is_one_error():
    for payload in ({"jobs": []}, "text", None, 5):
        result = boards.fetch("remoteok", None, req(), client=mock_client(lambda r, p=payload: httpx.Response(200, content=json.dumps(p).encode())))
        assert result.jobs == [] and len(result.errors) == 1 and "unexpected format" in result.errors[0], payload


def test_remoteok_missing_fields_are_skipped(monkeypatch):
    good = stubs.remoteok_job(id="ok")
    no_id, no_title, no_url = stubs.remoteok_job(id="n1"), stubs.remoteok_job(id="n2"), stubs.remoteok_job(id="n3")
    del no_id["id"]
    del no_title["position"]
    no_url["url"] = ""
    with feed("remoteok", [no_id, no_title, no_url, good, "junk", None]) as stub:
        result = run(monkeypatch, stub, "remoteok")
    assert result.errors == [] and [j["source_job_id"] for j in result.jobs] == ["ok"]
    assert result.dropped["unusable"] == 0


def test_remoteok_requests_the_api_root_once(monkeypatch):
    with feed("remoteok", [stubs.remoteok_job(id="1")]) as stub:
        run(monkeypatch, stub, "remoteok")
    assert stub.log == [("GET", "/remoteok/api", "")]


# ---- We Work Remotely -------------------------------------------------------------------------------------

def rss_client(body, status=200):
    data = body.encode() if isinstance(body, str) else body
    return mock_client(lambda r: httpx.Response(status, content=data))


def test_wwr_maps_a_job(monkeypatch):
    post = stubs.wwr_item("Backend Engineer", "Anywhere in the World", POSTED, "acme-backend-1", company="Acme Corp",
                          description="<p>Build the <b>API</b>.</p><ul><li>Python</li></ul>")
    with feed("weworkremotely", [post]) as stub:
        result = run(monkeypatch, stub, "weworkremotely")
    assert result.errors == [] and result.listed == 1 and result.relevant == 1
    (job,) = result.jobs
    assert job["source"] == "weworkremotely" and job["source_job_id"] == post["guid"]
    assert job["title"] == "Backend Engineer" and job["company"] == "Acme Corp"
    assert job["url"] == post["link"] and job["apply_url"] == post["link"]
    assert job["location"] == "Anywhere in the World" and job["is_remote"] is True
    assert job["posted_at"] == "2026-10-09T08:30:00+00:00"                          # RFC 822 pubDate -> ISO UTC
    assert job["description"] == "Build the API.\nPython"
    assert job["raw"]["guid"] == post["guid"] and job["raw"]["region"] == "Anywhere in the World" and job["raw"]["category"] == "Back-End Programming"
    assert "Build the" not in json.dumps(job["raw"])


def test_wwr_requests_the_real_feed_path_once(monkeypatch):
    with feed("weworkremotely", [stubs.wwr_item(id="x")]) as stub:
        run(monkeypatch, stub, "weworkremotely")
    assert stub.log == [("GET", "/weworkremotely" + boards.WWR_PATH, "")]
    assert boards.WWR_PATH == "/categories/remote-programming-jobs.rss"


def test_wwr_splits_company_from_role_on_first_colon_only(monkeypatch):
    post = stubs.wwr_item(id="k1", company="Acme", title="Backend Engineer: Payments: Platform")
    with feed("weworkremotely", [post]) as stub:
        result = run(monkeypatch, stub, "weworkremotely", terms=["backend engineer"])
    (job,) = result.jobs
    assert job["company"] == "Acme" and job["title"] == "Backend Engineer: Payments: Platform"


def test_wwr_title_without_a_company_prefix_is_skipped(monkeypatch):
    plain, empty_company, empty_role = stubs.wwr_item(id="p1"), stubs.wwr_item(id="p2"), stubs.wwr_item(id="p3")
    plain["title"] = "Backend Engineer"                                              # no ": " at all
    empty_company["title"] = ": Backend Engineer"
    empty_role["title"] = "Acme: "
    with feed("weworkremotely", [plain, empty_company, empty_role, stubs.wwr_item(id="ok")]) as stub:
        result = run(monkeypatch, stub, "weworkremotely")
    assert result.errors == [] and [j["source_job_id"].rsplit("/", 1)[-1] for j in result.jobs] == ["ok"]


def test_wwr_unparseable_pubdate_is_none_not_an_error(monkeypatch):
    post = stubs.wwr_item(id="d1")
    post["pubDate"] = "last tuesday-ish"
    with feed("weworkremotely", [post]) as stub:
        result = run(monkeypatch, stub, "weworkremotely")
    assert result.errors == [] and result.jobs[0]["posted_at"] is None


def test_wwr_keeps_the_region_as_given(monkeypatch):
    anywhere, blank = stubs.wwr_item(id="r1", location="Anywhere in the World"), stubs.wwr_item(id="r2", location="")
    with feed("weworkremotely", [anywhere, blank]) as stub:
        result = run(monkeypatch, stub, "weworkremotely")
    got = {j["source_job_id"].rsplit("/", 1)[-1]: j["location"] for j in result.jobs}
    assert got == {"r1": "Anywhere in the World", "r2": None}


def test_wwr_empty_feed_is_not_an_error(monkeypatch):
    with stubs.StubBoards(modes={("weworkremotely", ""): "empty"}) as stub:
        result = run(monkeypatch, stub, "weworkremotely")
    assert result.jobs == [] and result.errors == [] and result.listed == 0


def test_wwr_garbage_body_is_one_error_not_a_crash(monkeypatch):
    with stubs.StubBoards(modes={("weworkremotely", ""): "garbage"}) as stub:
        result = run(monkeypatch, stub, "weworkremotely")
    assert result.jobs == [] and len(result.errors) == 1 and result.errors[0].startswith("weworkremotely: ")
    for body in ("<rss><channel><item><title>x", "not xml at all", "", "{}"):         # malformed XML of every kind
        result = boards.fetch("weworkremotely", None, req(), client=rss_client(body))
        assert result.jobs == [] and len(result.errors) == 1, body


def test_wwr_xml_that_is_not_rss_is_one_error():
    for body in ("<html><body>hi</body></html>", "<rss version='2.0'></rss>"):
        result = boards.fetch("weworkremotely", None, req(), client=rss_client(body))
        assert result.jobs == [] and len(result.errors) == 1 and "unexpected format" in result.errors[0], body


def test_wwr_rate_limit_is_one_error_and_one_request(monkeypatch):
    with stubs.StubBoards(modes={("weworkremotely", ""): "rate_limited"}) as stub:
        result = run(monkeypatch, stub, "weworkremotely")
    assert result.jobs == [] and result.errors == ["weworkremotely: rate limited by the source; skipped for this run"]
    assert stub.requests == 1


def test_wwr_missing_fields_are_skipped(monkeypatch):
    good = stubs.wwr_item(id="ok")
    no_guid, no_title, no_link, blank_guid = (stubs.wwr_item(id=f"n{i}") for i in range(4))
    del no_guid["guid"]
    del no_title["title"]
    del no_link["link"]
    blank_guid["guid"] = "   "
    with feed("weworkremotely", [no_guid, no_title, no_link, blank_guid, good]) as stub:
        result = run(monkeypatch, stub, "weworkremotely")
    assert result.errors == [] and [j["source_job_id"].rsplit("/", 1)[-1] for j in result.jobs] == ["ok"]
    assert result.dropped["unusable"] == 0


DOCTYPE_DOC = '<?xml version="1.0"?>\n<!DOCTYPE rss [<!ELEMENT rss ANY>]>\n<rss version="2.0"><channel><title>x</title></channel></rss>'
ENTITY_BOMB = ('<?xml version="1.0"?>\n<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">]>\n'
               '<rss version="2.0"><channel><item><title>&lol2;: Role</title><guid>g</guid><link>https://x/y</link></item></channel></rss>')


@pytest.mark.parametrize("body", [DOCTYPE_DOC, ENTITY_BOMB, DOCTYPE_DOC.replace("DOCTYPE", "doctype"),
                                  '<rss version="2.0"><channel><!ENTITY x "y"></channel></rss>'])
def test_wwr_rejects_a_doctype_document(monkeypatch, body):
    def explode(*a, **k):
        raise AssertionError("the document must not be parsed")
    monkeypatch.setattr(boards.ET, "fromstring", explode)
    monkeypatch.setattr(boards.ET, "XMLParser", explode)
    result = boards.fetch("weworkremotely", None, req(), client=rss_client(body))
    assert result.jobs == [] and result.listed == 0 and len(result.errors) == 1
    assert result.errors[0].startswith("weworkremotely: ") and "DOCTYPE" in result.errors[0]


WIDE_DOC = DOCTYPE_DOC.replace('<?xml version="1.0"?>', '<?xml version="1.0" encoding="{enc}"?>') + "\n"
GOOD_ITEM = ("<item><title>Acme: Backend Engineer</title><guid>g1</guid><link>https://weworkremotely.com/remote-jobs/g1</link>"
             "<region>Anywhere in the World</region><description>&lt;p&gt;Caf\u00e9 job&lt;/p&gt;</description></item>")


def feed_xml(decl):
    return f"{decl}<rss version=\"2.0\"><channel><title>t</title>{GOOD_ITEM}</channel></rss>"


@pytest.mark.parametrize("codec,enc", [("utf-16-le", "UTF-16"), ("utf-16-be", "UTF-16"), ("utf-16", "UTF-16"),
                                       ("utf-32", "UTF-32"), ("utf-32-le", "UTF-32"), ("utf-32-be", "UTF-32")])
def test_wwr_refuses_a_wide_encoding_before_parsing(monkeypatch, codec, enc):
    def explode(*a, **k):
        raise AssertionError("the document must not be parsed")
    monkeypatch.setattr(boards.ET, "fromstring", explode)
    monkeypatch.setattr(boards.ET, "XMLParser", explode)
    body = WIDE_DOC.format(enc=enc).replace("\n<rss", "<rss").encode(codec)
    result = boards.fetch("weworkremotely", None, req(), client=rss_client(body))
    assert result.jobs == [] and result.listed == 0
    assert result.errors == ["weworkremotely: returned a feed in a not supported RSS encoding (refused)"]


@pytest.mark.parametrize("encoding", ["UTF-16", "utf-32", "EBCDIC-US", "koi8-r", "x-unknown", ""])
def test_wwr_refuses_a_declared_encoding_outside_the_allow_list(monkeypatch, encoding):
    def explode(*a, **k):
        raise AssertionError("the document must not be parsed")
    monkeypatch.setattr(boards.ET, "fromstring", explode)
    body = feed_xml(f"<?xml version='1.0' encoding = '{encoding}'?>")            # otherwise plain ASCII
    result = boards.fetch("weworkremotely", None, req(), client=rss_client(body))
    assert result.jobs == [] and result.errors == ["weworkremotely: returned a feed in a not supported RSS encoding (refused)"]


@pytest.mark.parametrize("decl,codec", [('<?xml version="1.0" encoding="UTF-8"?>', "utf-8"), ("<?xml version='1.0' encoding='utf-8'?>", "utf-8"),
                                        ('<?xml version="1.0" encoding="ISO-8859-1"?>', "iso-8859-1"),
                                        ('<?xml version="1.0" encoding="windows-1252"?>', "cp1252"),
                                        ('<?xml version="1.0" encoding="US-ASCII"?>', "ascii"), ("", "utf-8")])
def test_wwr_still_parses_the_supported_encodings(decl, codec):
    body = feed_xml(decl).replace("\u00e9", "e" if codec == "ascii" else "\u00e9").encode(codec)
    result = boards.fetch("weworkremotely", None, req(), client=rss_client(body))
    assert result.errors == [] and len(result.jobs) == 1
    assert result.jobs[0]["description"] == ("Cafe job" if codec == "ascii" else "Caf\u00e9 job")


def test_wwr_a_utf8_byte_order_mark_is_fine():
    body = b"\xef\xbb\xbf" + feed_xml('<?xml version="1.0" encoding="UTF-8"?>').encode("utf-8")
    result = boards.fetch("weworkremotely", None, req(), client=rss_client(body))
    assert result.errors == [] and len(result.jobs) == 1


def test_wwr_a_nul_byte_anywhere_is_refused():
    body = feed_xml("").encode("utf-8").replace(b"<region>", b"<region>\x00")
    result = boards.fetch("weworkremotely", None, req(), client=rss_client(body))
    assert result.jobs == [] and result.errors == ["weworkremotely: returned a feed in a not supported RSS encoding (refused)"]


# ---- all three --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("system", FEEDS)
def test_feed_adapters_are_registered(system):
    assert callable(boards.ADAPTERS.get(system))


@pytest.mark.parametrize("system", FEEDS)
def test_a_feed_takes_no_slug(system):
    result = boards.fetch(system, "acme", req(), client=mock_client(lambda r: pytest.fail("no request may be made")))
    assert result.jobs == [] and result.errors == [f"{system}: this is a feed and takes no company slug"]


@pytest.mark.parametrize("system", FEEDS)
def test_the_default_stub_feed_is_fetched_and_filtered(monkeypatch, system):
    with stubs.StubBoards() as stub:
        result = run(monkeypatch, stub, system, max_age_days=30)
    assert result.errors == [] and result.listed == 3
    assert [j["title"] for j in result.jobs] == ["Backend Engineer"] and result.jobs[0]["is_remote"] is True


@pytest.mark.parametrize("system", FEEDS)
def test_the_limit_is_applied_newest_first(monkeypatch, system):
    posts = [BUILDERS[system]("Backend Engineer", "Worldwide", ago(days=d), id=f"{system}-{d}") for d in (5, 1, 3)]
    if system == "remoteok":
        for p in posts:
            p["id"] = p["id"].replace(f"{system}-", "")
    with feed(system, posts) as stub:
        result = run(monkeypatch, stub, system, limit=2)
    assert result.listed == 3 and result.relevant == 3 and len(result.jobs) == 2
    assert [j["posted_at"] for j in result.jobs] == sorted((j["posted_at"] for j in result.jobs), reverse=True)
    newest_two = ["1", "3"] if system == "remoteok" else [f"{system}-1", f"{system}-3"]       # 1 and 3 days old; the 5-day-old one is cut
    assert [j["source_job_id"].rsplit("/", 1)[-1] for j in result.jobs] == newest_two


@pytest.mark.parametrize("system", FEEDS)
def test_the_host_comes_only_from_base_url(monkeypatch, system):
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, content=b"[]" if system != "weworkremotely" else b"<rss><channel/></rss>")
    monkeypatch.delenv("BOARDS_API_URL", raising=False)
    boards.fetch(system, None, req(), client=mock_client(handler))
    host = {"remotive": "https://remotive.com", "remoteok": "https://remoteok.com", "weworkremotely": "https://weworkremotely.com"}[system]
    assert len(seen) == 1 and seen[0].startswith(host + "/")


# ---- hardening batch (Task 9b): epoch guard and description decoding ------------------------------------------

@pytest.mark.parametrize("value", [0, 0.0, -1, -86400, "0", "-5", "0.0", True, None, [], "abc"])
def test_epoch_to_iso_rejects_zero_negative_and_junk(value):
    assert boards._epoch_to_iso(value) is None


def test_epoch_to_iso_still_converts_a_sane_epoch():
    assert boards._epoch_to_iso(1_760_000_000) == "2025-10-09T08:53:20+00:00"
    assert boards._epoch_to_iso("1760000000") == "2025-10-09T08:53:20+00:00"
    assert boards._epoch_to_iso(1) == "1970-01-01T00:00:01+00:00"


def test_remoteok_a_zero_epoch_falls_back_to_the_date_field(monkeypatch):
    zero, negative = (stubs.remoteok_job("Backend Engineer", "Worldwide", POSTED, str(i)) for i in range(2))
    zero["epoch"], negative["epoch"] = 0, -100                                     # 1970 would read as "55 years old"
    with feed("remoteok", [zero, negative]) as stub:
        result = run(monkeypatch, stub, "remoteok")
    assert {j["source_job_id"]: j["posted_at"] for j in result.jobs} == {"0": "2026-10-09T08:30:00+00:00", "1": "2026-10-09T08:30:00+00:00"}


def wwr_doc(description_xml):
    return ('<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>t</title>'
            "<item><title>Acme: Backend Engineer</title><guid>g1</guid><link>https://weworkremotely.com/remote-jobs/g1</link>"
            f"<region>Anywhere in the World</region><description>{description_xml}</description></item></channel></rss>")


def wwr_description(description_xml):
    result = boards.fetch("weworkremotely", None, req(), client=rss_client(wwr_doc(description_xml)))
    assert result.errors == [] and len(result.jobs) == 1
    return result.jobs[0]["description"]


def test_wwr_html_is_decoded_exactly_once():
    # CDATA keeps `&amp;amp;` as text after the XML layer: the HTML layer decodes it ONCE, to `&amp;` (not to `&`)
    assert wwr_description("<![CDATA[<p>Tom &amp;amp; Jerry</p>]]>") == "Tom &amp; Jerry"
    # escaped for XML instead of CDATA: the XML layer decodes one layer first, then the HTML layer one more (two layers in all)
    assert wwr_description("&lt;p&gt;Tom &amp;amp; Jerry&lt;/p&gt;") == "Tom & Jerry"
    assert wwr_description("&lt;p&gt;Tom &amp;amp;amp; Jerry&lt;/p&gt;") == "Tom &amp; Jerry"


def test_wwr_a_double_escaped_body_is_unescaped_once_more():
    # After the XML layer the text is `&lt;p&gt;...` (no "<" at all): the heuristic treats it as escaped HTML and unescapes it
    assert wwr_description("&amp;lt;p&amp;gt;Build the API&amp;lt;/p&amp;gt;") == "Build the API"
    assert wwr_description("&amp;lt;p&amp;gt;Tom &amp;amp; Jerry&amp;lt;/p&amp;gt;") == "Tom & Jerry"


def test_wwr_the_heuristic_needs_an_entity_and_no_literal_tag():
    # the decision, pinned: text with "&lt;" and no literal "<" after the XML layer counts as double-escaped html, even without a tag
    assert wwr_description("Tom &amp;lt; Jerry") == "Tom < Jerry"
    assert wwr_description("plain text, no markup") == "plain text, no markup"
    assert wwr_description("<![CDATA[<p>real tag</p> and &lt;b&gt; text]]>") == "real tag\nand <b> text"   # a literal "<": not double-escaped
