"""The stand-in APIs behave like the real ones where it matters, count every call, and the safety net holds."""
import html
import json
import socket
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import anthropic_client as AC
import apify_source as AP
import scoring
import stubs


def _client(stub, **kw):
    saved = AC.API_URL
    AC.API_URL = stub.url
    try:
        return AC.AnthropicClient("stub-key", client=httpx.Client(), sleep=lambda s: None, **kw)
    finally:
        AC.API_URL = saved


def test_the_tests_cannot_reach_the_internet():
    with pytest.raises(RuntimeError, match="real network connection"):
        socket.create_connection(("api.anthropic.com", 443), timeout=1)
    with pytest.raises(RuntimeError, match="real network connection"):
        httpx.get("https://api.apify.com/v2/users/me/limits")


def test_a_scoring_stub_returns_a_reply_the_real_validator_accepts():
    with stubs.StubAnthropic(stubs.scoring_ok()) as ai:
        AC.API_URL = ai.url
        try:
            reply = AC.AnthropicClient("k", client=httpx.Client()).messages(model="claude-haiku-5-5", system="s", user="a posting", max_tokens=900)
        finally:
            AC.API_URL = "https://api.anthropic.com/v1/messages"
    result = scoring.finalize(reply.text, threshold=70)
    assert 0 <= result["score"] <= 100 and reply.usage["input_tokens"] == 2500
    assert ai.requests == 1 and ai.billed == 1 and ai.by_status == {200: 1} and ai.models == ["claude-haiku-5-5"]


def test_outages_are_counted_as_free():
    with stubs.StubAnthropic(stubs.always_status(529)) as ai:
        AC.API_URL = ai.url
        try:
            with pytest.raises(AC.AnthropicError) as e:
                AC.AnthropicClient("k", client=httpx.Client(), retries=2, sleep=lambda s: None).messages(model="m", system="s", user="u", max_tokens=10)
        finally:
            AC.API_URL = "https://api.anthropic.com/v1/messages"
    assert e.value.retryable and ai.requests == 3 and ai.billed == 0, "the client retried twice, and none of it would have been billed"


def test_the_url_override_comes_from_the_environment():
    """Checked in a fresh interpreter: reloading a module in-process would swap its exception classes under other tests."""
    import os
    import subprocess
    import sys
    code = "import anthropic_client as a, apify_source as p; print(a.API_URL, p.API)"
    env = dict(os.environ, ANTHROPIC_API_URL="http://127.0.0.1:9/v1/messages", APIFY_API_URL="http://127.0.0.1:9/v2")
    out = subprocess.run([sys.executable, "-c", code], env=env, cwd=os.path.dirname(os.path.dirname(__file__)), capture_output=True, text=True).stdout.split()
    assert out == ["http://127.0.0.1:9/v1/messages", "http://127.0.0.1:9/v2"]
    clean = {k: v for k, v in os.environ.items() if k not in ("ANTHROPIC_API_URL", "APIFY_API_URL")}
    out = subprocess.run([sys.executable, "-c", code], env=clean, cwd=os.path.dirname(os.path.dirname(__file__)), capture_output=True, text=True).stdout.split()
    assert out == ["https://api.anthropic.com/v1/messages", "https://api.apify.com/v2"], "unset means the real APIs"


def test_the_apify_stub_serves_a_whole_search_and_bills_the_balance():
    with stubs.StubApify({"tok-a": 9.94}, charge=0.047) as apify:
        AP.API = apify.url
        try:
            out = AP.search_linkedin(AP.SearchParams("backend", "Pune"), env={"APIFY_TOKEN_CURIOUS": "tok-a"}, sleep=lambda s: None)
        finally:
            AP.API = "https://api.apify.com/v2"
    assert out["jobs"][0]["title"] == "Backend Engineer" and out["meta"]["actual_cost_usd"] == 0.047
    assert apify.starts == 1 and round(apify.balances["tok-a"], 3) == 9.893


def test_the_apify_stub_can_hang_fail_and_refuse():
    cases = {"hang": (True, 1), "fail": (True, 0), "start_error": (False, 0)}
    for mode, (started, aborts) in cases.items():
        with stubs.StubApify({"tok-a": 5.0}, {"tok-a": mode}) as apify:
            AP.API = apify.url
            try:
                with pytest.raises(AP.ApifyError) as e:
                    AP.search_linkedin(AP.SearchParams("backend", "Pune"), env={"APIFY_TOKEN_CURIOUS": "tok-a"}, sleep=lambda s: None)
            finally:
                AP.API = "https://api.apify.com/v2"
        assert bool(e.value.attempts) is started and apify.aborts == aborts, mode
        assert (apify.rejected == 1) is (mode == "start_error")
        assert apify.polls <= 130, "polling is bounded"


# ----------------------------------------------------------------------------- StubBoards (company job boards + remote feeds)

def _get(stub, path, **params):
    return httpx.get(stub.url + path, params=params or None, timeout=10)


# one list request per system: (path, how to pull the postings out of the parsed reply)
LIST_CALLS = {
    "greenhouse": ("/greenhouse/v1/boards/acme/jobs", lambda d: d["jobs"]),
    "lever": ("/lever/v0/postings/acme", lambda d: d),
    "lever_eu": ("/lever_eu/v0/postings/acme", lambda d: d),
    "ashby": ("/ashby/posting-api/job-board/acme", lambda d: d["jobs"]),
    "smartrecruiters": ("/smartrecruiters/v1/companies/Acme/postings", lambda d: d["content"]),
    "remotive": ("/remotive/api/remote-jobs", lambda d: d["jobs"]),
    "remoteok": ("/remoteok/api", lambda d: d[1:]),
    "himalayas": ("/himalayas/jobs/api", lambda d: d["jobs"]),
    "arbeitnow": ("/arbeitnow/api/job-board-api", lambda d: d["data"]),
}


def test_every_boards_list_endpoint_serves_default_postings_in_its_own_shape():
    with stubs.StubBoards() as b:
        for system, (path, pull) in LIST_CALLS.items():
            r = _get(b, path)
            assert r.status_code == 200, system
            jobs = pull(r.json())
            assert 2 <= len(jobs) <= 3, system
            assert any("backend engineer" in json.dumps(j).lower() for j in jobs), f"{system}: one clearly relevant default job"
            assert any("backend" not in json.dumps(j).lower() for j in jobs), f"{system}: one irrelevant default job"
        r = httpx.post(b.url + "/workable/api/v3/accounts/acme/jobs", json={"query": "", "location": [], "remote": []}, timeout=10)
        assert r.status_code == 200 and {"results", "total"} <= set(r.json()) and 2 <= len(r.json()["results"]) <= 3
        rss = httpx.get(b.url + "/weworkremotely/rss", timeout=10)
        assert rss.status_code == 200 and "xml" in rss.headers["content-type"]
        items = ET.fromstring(rss.text).findall("./channel/item")
        assert 2 <= len(items) <= 3
    assert b.requests == len(LIST_CALLS) + 2


def test_the_raw_shapes_carry_the_documented_keys_and_types():
    with stubs.StubBoards() as b:
        gh = _get(b, "/greenhouse/v1/boards/acme/jobs").json()
        j = gh["jobs"][0]
        assert gh["meta"]["total"] == len(gh["jobs"]) and isinstance(j["id"], int)
        assert {"absolute_url", "location", "updated_at", "first_published", "title", "company_name"} <= set(j) and "name" in j["location"]
        lv = _get(b, "/lever/v0/postings/acme").json()[0]
        assert isinstance(lv["createdAt"], int) and lv["createdAt"] > 10 ** 12, "epoch milliseconds"
        assert {"text", "categories", "descriptionPlain", "workplaceType", "hostedUrl", "applyUrl"} <= set(lv) and "location" in lv["categories"]
        ab = _get(b, "/ashby/posting-api/job-board/acme").json()
        assert ab["apiVersion"] == "1"
        assert {"publishedAt", "isRemote", "location", "secondaryLocations", "isListed", "descriptionPlain", "jobUrl", "applyUrl"} <= set(ab["jobs"][0])
        w3 = httpx.post(b.url + "/workable/api/v3/accounts/acme/jobs", json={}, timeout=10).json()["results"][0]
        assert {"shortcode", "published", "remote", "workplace", "location"} <= set(w3) and {"city", "country", "countryCode"} <= set(w3["location"])
        sr = _get(b, "/smartrecruiters/v1/companies/Acme/postings").json()
        assert {"offset", "limit", "totalFound", "content"} <= set(sr)
        assert {"releasedDate", "location", "company"} <= set(sr["content"][0]) and "fullLocation" in sr["content"][0]["location"]
        rm = _get(b, "/remotive/api/remote-jobs").json()
        assert {"id", "url", "title", "company_name", "publication_date", "candidate_required_location", "description"} <= set(rm["jobs"][0])
        assert rm["job-count"] == len(rm["jobs"])
        ro = _get(b, "/remoteok/api").json()
        assert "legal" in ro[0] and "position" not in ro[0], "the first element is the legal notice"
        assert {"id", "slug", "epoch", "date", "company", "position", "description", "location", "url", "apply_url"} <= set(ro[1])
        hi = _get(b, "/himalayas/jobs/api").json()
        assert {"totalCount", "offset", "limit", "nextCursor" , "jobs"} <= set(hi)
        assert {"title", "companyName", "locationRestrictions", "pubDate", "description", "applicationLink", "guid"} <= set(hi["jobs"][0])
        assert isinstance(hi["jobs"][0]["pubDate"], int)
        ar = _get(b, "/arbeitnow/api/job-board-api").json()
        assert {"data", "links", "meta"} <= set(ar) and ar["meta"]["per_page"] == 325
        assert {"slug", "company_name", "title", "description", "remote", "url", "location", "created_at"} <= set(ar["data"][0])


def test_greenhouse_lists_without_descriptions_unless_content_is_asked_for_and_escapes_the_html():
    with stubs.StubBoards() as b:
        plain = _get(b, "/greenhouse/v1/boards/acme/jobs").json()["jobs"]
        full = _get(b, "/greenhouse/v1/boards/acme/jobs", content="true").json()["jobs"]
        assert all("content" not in j for j in plain) and all("content" in j for j in full)
        text = full[0]["content"]
        assert "&lt;" in text and "<" not in text, "entity-ESCAPED HTML, like the real API"
        assert "<p>" in html.unescape(text)
        one = _get(b, "/greenhouse/v1/boards/acme/jobs/%s" % full[0]["id"]).json()
        assert one["id"] == full[0]["id"] and one["content"] == text
        assert _get(b, "/greenhouse/v1/boards/acme/jobs/999999999").status_code == 404
    assert b.by_path["/greenhouse/v1/boards/acme/jobs"] == 2


def test_lever_has_an_eu_host_and_eu_only_mode_hides_the_plain_path():
    with stubs.StubBoards(modes={("lever", "acme"): "eu_only"}) as b:
        assert _get(b, "/lever/v0/postings/acme", mode="json").status_code == 404
        r = _get(b, "/lever_eu/v0/postings/acme", mode="json")
        assert r.status_code == 200 and r.json()[0]["text"]
    assert b.log == [("GET", "/lever/v0/postings/acme", "mode=json"), ("GET", "/lever_eu/v0/postings/acme", "mode=json")]


def test_workable_needs_a_second_call_for_the_description_and_the_v3_detail_path_is_a_404():
    with stubs.StubBoards() as b:
        res = httpx.post(b.url + "/workable/api/v3/accounts/acme/jobs", json={"query": "backend", "location": [], "remote": []}, timeout=10).json()
        assert res["total"] == len(res["results"]) >= 1
        assert all("backend" in j["title"].lower() for j in res["results"]), "the query filter is honoured"
        assert all("description" not in j for j in res["results"])
        code = res["results"][0]["shortcode"]
        d = _get(b, "/workable/api/v2/accounts/acme/jobs/" + code).json()
        assert d["shortcode"] == code and {"description", "requirements", "benefits"} <= set(d)
        assert d["description"].startswith("<p>"), "HTML, not escaped"
        assert _get(b, "/workable/api/v3/accounts/acme/jobs/" + code).status_code == 404
        assert _get(b, "/workable/api/v2/accounts/acme/jobs/NOSUCHCODE").status_code == 404
    assert b.post_bodies == [{"query": "backend", "location": [], "remote": []}]
    assert [m for m, _, _ in b.log] == ["POST", "GET", "GET", "GET"]


def test_smartrecruiters_filters_pages_and_needs_a_detail_call():
    jobs = [stubs.sr_job(title="Backend Engineer %d" % i, id=str(7000 + i), location="Pune, MH, India", country="in") for i in range(5)]
    jobs += [stubs.sr_job(title="Chef", id="7100", location="Paris, France", country="fr")]
    with stubs.StubBoards(data={("smartrecruiters", "Acme"): jobs}) as b:
        path = "/smartrecruiters/v1/companies/Acme/postings"
        first = _get(b, path, limit=2, offset=0).json()
        assert (first["offset"], first["limit"], first["totalFound"], len(first["content"])) == (0, 2, 6, 2)
        assert all("jobAd" not in j for j in first["content"]), "the list has no description"
        third = _get(b, path, limit=2, offset=4).json()
        assert [j["id"] for j in third["content"]] == ["7004", "7100"]
        q = _get(b, path, q="backend", country="in").json()
        assert q["totalFound"] == 5
        assert _get(b, path, country="fr").json()["totalFound"] == 1
        assert _get(b, path, q="chef", country="in").json() == {"offset": 0, "limit": 10, "totalFound": 0, "content": []}
        d = _get(b, path + "/7001").json()
        sections = d["jobAd"]["sections"]
        assert {"jobDescription", "qualifications", "additionalInformation", "companyDescription"} <= set(sections)
        assert sections["jobDescription"]["text"].startswith("<p>") and d["postingUrl"] and d["applyUrl"] and d["id"] == "7001"
        assert _get(b, path + "/nope").status_code == 404
        # the slug is case sensitive: a different case is a different (empty) company, as on the real API
        assert _get(b, "/smartrecruiters/v1/companies/acme/postings").json()["totalFound"] == 0
    assert ("GET", path, "q=backend&country=in") in b.log


@pytest.mark.parametrize("mode,status", [("not_found", 404), ("rate_limited", 429), ("server_error", 500)])
def test_board_modes_produce_error_statuses(mode, status):
    with stubs.StubBoards(modes={("greenhouse", "acme"): mode, ("remotive", ""): mode}) as b:
        assert _get(b, "/greenhouse/v1/boards/acme/jobs").status_code == status
        assert _get(b, "/greenhouse/v1/boards/other/jobs").status_code == 200, "modes are per (system, slug)"
        assert _get(b, "/remotive/api/remote-jobs").status_code == status, "feeds use the slug ''"
        assert _get(b, "/arbeitnow/api/job-board-api").status_code == 200


def test_the_garbage_mode_answers_200_with_a_body_that_is_not_json():
    with stubs.StubBoards(modes={(s, ""): "garbage" for s in ("remoteok", "weworkremotely")}) as b:
        for path in ("/remoteok/api", "/weworkremotely/rss"):
            r = _get(b, path)
            assert r.status_code == 200
            with pytest.raises(ValueError):
                r.json()
        with pytest.raises(ET.ParseError):
            ET.fromstring(r.text)


def test_the_empty_mode_serves_a_valid_empty_reply_in_each_shape():
    with stubs.StubBoards(modes={(s, "acme" if s not in ("remotive", "remoteok", "himalayas", "arbeitnow") else ""): "empty" for s in LIST_CALLS}) as b:
        for system, (path, pull) in LIST_CALLS.items():
            if system == "smartrecruiters":
                path = path.replace("Acme", "acme")
            assert pull(_get(b, path).json()) == [], system
        assert _get(b, "/himalayas/jobs/api").json()["nextCursor"] is None
        assert _get(b, "/arbeitnow/api/job-board-api").json()["links"]["next"] is None


def test_the_huge_mode_streams_more_than_33_million_bytes_of_valid_looking_json():
    with stubs.StubBoards(modes={("ashby", "acme"): "huge"}) as b:
        size, head = 0, b""
        with httpx.stream("GET", b.url + "/ashby/posting-api/job-board/acme", timeout=30) as r:
            assert r.status_code == 200
            for chunk in r.iter_bytes():
                head = head or chunk[:20]
                size += len(chunk)
        assert size > 33_000_000 and head.startswith(b'{"jobs"')
        r = _get(b, "/ashby/posting-api/job-board/acme")
        assert json.loads(r.content)["jobs"][0]["title"], "and it really is parseable JSON"


def test_the_slow_mode_outlasts_a_short_timeout_and_the_server_still_shuts_down_promptly():
    t0 = time.monotonic()
    with stubs.StubBoards(modes={("lever", "acme"): "slow"}) as b:
        with pytest.raises(httpx.ReadTimeout):
            httpx.get(b.url + "/lever/v0/postings/acme", timeout=0.4)
    assert time.monotonic() - t0 < 2.5, "leaving the with-block interrupts the sleeping handler"
    with stubs.StubBoards(modes={("lever", "acme"): "slow"}) as b:
        b.slow_seconds = 1.0
        r = _get(b, "/lever/v0/postings/acme")
        assert r.status_code == 200 and r.json(), "it answers, late"


def test_unknown_slugs_are_a_404_once_data_is_given_but_any_slug_works_with_the_defaults():
    with stubs.StubBoards() as b:
        assert _get(b, "/greenhouse/v1/boards/anything-at-all/jobs").status_code == 200
        assert _get(b, "/no-such-system/x").status_code == 404
    with stubs.StubBoards(data={("greenhouse", "acme"): [stubs.gh_job(title="Only One", id=42)]}) as b:
        ok = _get(b, "/greenhouse/v1/boards/acme/jobs").json()
        assert [j["title"] for j in ok["jobs"]] == ["Only One"] and ok["jobs"][0]["id"] == 42
        assert _get(b, "/greenhouse/v1/boards/Acme/jobs").status_code == 404, "slug case matters"
        assert _get(b, "/lever/v0/postings/acme").status_code == 404


def test_himalayas_pages_are_capped_at_20_and_use_a_cursor():
    data = [stubs.himalayas_job(title="Engineer %d" % i, id=i, posted=datetime(2026, 10, 1, tzinfo=timezone.utc) - timedelta(hours=i)) for i in range(45)]
    with stubs.StubBoards(data={("himalayas", ""): data}) as b:
        p1 = _get(b, "/himalayas/jobs/api", limit=100).json()
        assert len(p1["jobs"]) == 20 and p1["limit"] == 20 and p1["totalCount"] == 45 and p1["nextCursor"]
        p2 = _get(b, "/himalayas/jobs/api", limit=20, cursor=p1["nextCursor"]).json()
        p3 = _get(b, "/himalayas/jobs/api", cursor=p2["nextCursor"]).json()
        titles = [j["title"] for p in (p1, p2, p3) for j in p["jobs"]]
        assert titles == ["Engineer %d" % i for i in range(45)], "newest first, never the same job twice"
        assert len(p3["jobs"]) == 5 and p3["nextCursor"] is None
    assert [q for _, _, q in b.log] == ["limit=100", "limit=20&cursor=" + p1["nextCursor"], "cursor=" + p2["nextCursor"]]
    assert b.by_path == {"/himalayas/jobs/api": 3} and b.requests == 3


def test_arbeitnow_pages_hold_325_jobs_and_link_to_the_next_page():
    data = [stubs.arbeitnow_job(title="Job %d" % i, id="job-%d" % i) for i in range(700)]
    with stubs.StubBoards(data={("arbeitnow", ""): data}) as b:
        p1 = _get(b, "/arbeitnow/api/job-board-api").json()
        p2 = _get(b, "/arbeitnow/api/job-board-api", page=2).json()
        p3 = _get(b, "/arbeitnow/api/job-board-api", page=3).json()
        p4 = _get(b, "/arbeitnow/api/job-board-api", page=4).json()
    assert [len(p["data"]) for p in (p1, p2, p3, p4)] == [325, 325, 50, 0]
    assert p1["data"][0]["slug"] == "job-0" and p2["data"][0]["slug"] == "job-325"
    assert p1["links"]["next"].endswith("page=2") and p1["links"]["prev"] is None and p3["links"]["next"] is None
    assert p2["meta"]["current_page"] == 2 and p2["meta"]["per_page"] == 325 and p2["links"]["prev"].endswith("page=1")
    assert [q for _, _, q in b.log] == ["", "page=2", "page=3", "page=4"]


def test_bottomless_feeds_never_run_out_and_their_dates_step_back_per_job():
    with stubs.StubBoards(bottomless={"himalayas": 6, "arbeitnow": 2}) as b:
        cursor, dates = None, []
        for _ in range(4):
            page = _get(b, "/himalayas/jobs/api", **({"cursor": cursor} if cursor else {})).json()
            assert len(page["jobs"]) == 20 and page["nextCursor"]
            dates += [j["pubDate"] for j in page["jobs"]]
            cursor = page["nextCursor"]
        assert all(a - c == 6 * 3600 for a, c in zip(dates, dates[1:])), "newest first, 6 hours between jobs, across pages"
        ar = [_get(b, "/arbeitnow/api/job-board-api", page=p).json() for p in (1, 2, 40)]
        assert all(len(a["data"]) == 325 and a["links"]["next"] for a in ar)
        stamps = [j["created_at"] for a in ar[:2] for j in a["data"]]
        assert all(a - c == 2 * 3600 for a, c in zip(stamps, stamps[1:]))
        assert len({j["slug"] for a in ar for j in a["data"]}) == 3 * 325, "every job is distinct"


def test_the_builders_produce_realistic_postings_and_take_overrides():
    when = datetime(2026, 10, 9, 8, 30, tzinfo=timezone.utc)
    gh = stubs.gh_job(title="Staff Engineer", location="Pune, India", posted=when, id=123, board="zeta")
    assert gh["title"] == "Staff Engineer" and gh["location"] == {"name": "Pune, India"} and gh["id"] == 123
    assert gh["first_published"].startswith("2026-10-09T08:30") and gh["absolute_url"].endswith("/zeta/jobs/123")
    lv = stubs.lever_job(title="SRE", location="Remote", posted=when, id="abc")
    assert lv["id"] == "abc" and lv["text"] == "SRE" and lv["categories"]["location"] == "Remote" and lv["createdAt"] == int(when.timestamp() * 1000)
    ab = stubs.ashby_job(title="SRE", location="Berlin", posted="2026-10-09T08:30:00+00:00", id="u1")
    assert ab["location"] == "Berlin" and ab["publishedAt"].startswith("2026-10-09T08:30:00") and ab["id"] == "u1"
    w = stubs.workable_job(title="Dev", location="Paris, France", posted="2026-10-09", id="ABCDEF1234")
    assert w["shortcode"] == "ABCDEF1234" and w["location"]["city"] == "Paris" and w["location"]["country"] == "France" and w["published"].startswith("2026-10-09")
    wd = stubs.workable_detail(w)
    assert {"description", "requirements", "benefits"} <= set(wd) and wd["shortcode"] == "ABCDEF1234" and "description" not in w
    sr = stubs.sr_job(title="Dev", location="Lyon, France", posted=when, id="555")
    assert sr["id"] == "555" and sr["name"] == "Dev" and sr["location"]["fullLocation"] == "Lyon, France"
    sd = stubs.sr_detail(sr)
    assert sd["id"] == "555" and sd["jobAd"]["sections"]["jobDescription"]["text"]
    rm = stubs.remotive_job(title="Dev", location="Worldwide", posted=when, id=9)
    assert rm["id"] == 9 and rm["candidate_required_location"] == "Worldwide" and rm["publication_date"] == "2026-10-09T08:30:00"
    ro = stubs.remoteok_job(title="Dev", location="Anywhere", posted=when, id="77")
    assert ro["position"] == "Dev" and ro["id"] == "77" and ro["epoch"] == int(when.timestamp())
    hi = stubs.himalayas_job(title="Dev", location="Canada", posted=when, id=3)
    assert hi["title"] == "Dev" and hi["locationRestrictions"] == ["Canada"] and hi["pubDate"] == int(when.timestamp())
    ar = stubs.arbeitnow_job(title="Dev", location="Munich", posted=when, id="dev-munich")
    assert ar["slug"] == "dev-munich" and ar["location"] == "Munich" and ar["created_at"] == int(when.timestamp())
    ww = stubs.wwr_item(title="Dev", location="USA Only", posted=when, id="acme-dev", company="Acme")
    assert ww["title"] == "Acme: Dev" and ww["region"] == "USA Only" and ww["guid"].endswith("/acme-dev") and ww["pubDate"].startswith("Fri, 09 Oct 2026 08:30:00")
    assert stubs.gh_job()["id"] != stubs.gh_job()["id"], "default ids are unique"


def test_weworkremotely_renders_rss_with_escaped_html_and_company_colon_title():
    items = [stubs.wwr_item(title="Backend Engineer", company="Acme", id="acme-backend"), stubs.wwr_item(title="Chef", company="Foo & Sons", id="foo-chef")]
    with stubs.StubBoards(data={("weworkremotely", ""): items}) as b:
        r = _get(b, "/weworkremotely/rss")
    root = ET.fromstring(r.text)
    assert root.tag == "rss" and root.find("./channel/ttl").text == "60"
    got = root.findall("./channel/item")
    assert [i.find("title").text for i in got] == ["Acme: Backend Engineer", "Foo & Sons: Chef"]
    assert [i.find("region") is not None and i.find("pubDate") is not None and i.find("guid") is not None and i.find("link") is not None for i in got] == [True, True]
    assert "<p>" in html.unescape(got[0].find("description").text)
    assert ET.fromstring(stubs.wwr_rss(items)).findall("./channel/item")[1].find("title").text == "Foo & Sons: Chef"
    assert "<item>" not in stubs.wwr_rss([]) and ET.fromstring(stubs.wwr_rss([])).find("./channel/ttl").text == "60"


def test_the_log_records_method_path_and_query_in_arrival_order():
    with stubs.StubBoards() as b:
        _get(b, "/arbeitnow/api/job-board-api", page=2)
        _get(b, "/remotive/api/remote-jobs", category="software-dev", limit=5)
        _get(b, "/greenhouse/v1/boards/Acme/jobs")
        _get(b, "/greenhouse/v1/boards/Acme/jobs")
    assert b.log == [("GET", "/arbeitnow/api/job-board-api", "page=2"), ("GET", "/remotive/api/remote-jobs", "category=software-dev&limit=5"),
                     ("GET", "/greenhouse/v1/boards/Acme/jobs", ""), ("GET", "/greenhouse/v1/boards/Acme/jobs", "")]
    assert b.requests == 4 and b.by_path["/greenhouse/v1/boards/Acme/jobs"] == 2 and b.url.startswith("http://127.0.0.1:") and not b.url.endswith("/")
