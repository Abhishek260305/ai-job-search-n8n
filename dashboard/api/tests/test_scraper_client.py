"""ScraperClient against a fake opener: the calls it makes, and how every kind of failure becomes a store error."""
from __future__ import annotations

import http.client
import io
import json
import urllib.error

import pytest

from scraper_client import MAX_BODY, ScraperClient, check_url
from store import Invalid, Unavailable


class Response:
    def __init__(self, body: bytes):
        self._body = body

    def read(self, n=-1):
        return self._body if n < 0 else self._body[:n]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def client_with(answer):
    """A client whose opener records requests and returns `answer` (bytes), or raises it when it is an exception."""
    seen = []

    def opener(request, timeout):
        seen.append((request.get_method(), request.full_url, request.data, request.headers, timeout))
        if isinstance(answer, Exception):
            raise answer
        return Response(answer)

    return ScraperClient("http://scraper:8000/", timeout=3, opener=opener), seen


def http_error(code, body=b"{}"):
    return urllib.error.HTTPError("http://scraper:8000/x", code, "err", {}, io.BytesIO(body))


def test_reads_and_writes_use_fixed_paths_and_json():
    c, seen = client_with(b'{"ok": true}')
    c.schedule(), c.features(), c.tailoring(), c.budget()
    c.set_schedule("score", {"batch": 5}), c.run_schedule("digest")
    c.set_features({"digest": False}), c.set_tailoring({"mode": "manual"}), c.set_budget({"daily_cap_usd": 1})
    assert [(m, u) for m, u, *_ in seen] == [
        ("GET", "http://scraper:8000/schedule"), ("GET", "http://scraper:8000/features"),
        ("GET", "http://scraper:8000/tailor/settings"), ("GET", "http://scraper:8000/budget"),
        ("POST", "http://scraper:8000/schedule/score"), ("POST", "http://scraper:8000/schedule/digest/run"),
        ("POST", "http://scraper:8000/features"),
        ("POST", "http://scraper:8000/tailor/settings"), ("POST", "http://scraper:8000/budget/settings")]
    assert json.loads(seen[4][2]) == {"batch": 5}
    assert seen[0][2] is None and seen[0][4] == 3
    assert seen[4][3]["Content-type"] == "application/json"


def test_a_refusal_keeps_the_scrapers_message():
    c, _ = client_with(http_error(422, b'{"detail": {"errors": ["collect can run every 15 minutes to 7 days"]}}'))
    with pytest.raises(Invalid, match="collect can run every 15 minutes to 7 days"):
        c.set_schedule("collect", {"every_minutes": 5})


@pytest.mark.parametrize("body, message", [
    (b'{"detail": {"errors": ["a", "b"]}}', "a; b"),
    (b'{"detail": "plain text"}', "plain text"),
    (b'{"detail": [{"msg": "field required"}, {"msg": "bad"}]}', "field required; bad"),
    (b"not json", "the scraper refused that value"),
    (b'{"detail": {"errors": []}}', "the scraper refused that value"),
    (b"", "the scraper refused that value")])
def test_every_shape_of_422_becomes_a_readable_message(body, message):
    c, _ = client_with(http_error(422, body))
    with pytest.raises(Invalid) as e:
        c.features()
    assert str(e.value) == message


def test_a_long_refusal_is_cut():
    c, _ = client_with(http_error(422, json.dumps({"detail": "x" * 1000}).encode()))
    with pytest.raises(Invalid) as e:
        c.features()
    assert len(str(e.value)) == 300


def test_a_missing_endpoint_is_the_scrapers_problem_not_a_missing_job():
    c, _ = client_with(http_error(404))
    with pytest.raises(Unavailable, match="SCRAPER_URL"):
        c.features()


@pytest.mark.parametrize("failure", [http_error(500, b"Traceback secret"), http_error(503), urllib.error.URLError("refused"),
                                     TimeoutError(), ConnectionResetError(), http.client.IncompleteRead(b"par"),
                                     http.client.BadStatusLine("garbage"), http.client.RemoteDisconnected("closed")])
def test_everything_else_is_unavailable_without_leaking_details(failure):
    c, _ = client_with(failure)
    with pytest.raises(Unavailable) as e:
        c.features()
    assert "secret" not in str(e.value) and "refused" not in str(e.value)


@pytest.mark.parametrize("answer", [b"<html>", b"[1, 2]", b"null", b'"text"', b"", b'{"a": '])
def test_an_unreadable_answer_is_unavailable(answer):
    c, _ = client_with(answer)
    with pytest.raises(Unavailable):
        c.features()


def test_an_oversized_answer_is_refused():
    c, _ = client_with(b'{"x": "' + b"a" * MAX_BODY + b'"}')
    with pytest.raises(Unavailable, match="too much data"):
        c.features()


def test_no_proxy_from_the_environment_is_used(monkeypatch):
    """The scraper is an address inside the compose network; HTTP_PROXY must not send the settings anywhere else."""
    import urllib.request
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example:3128")
    monkeypatch.setenv("http_proxy", "http://proxy.example:3128")
    seen = []
    real_build = urllib.request.build_opener

    def build(*handlers):
        seen.extend(handlers)
        return real_build(*handlers)

    monkeypatch.setattr(urllib.request, "build_opener", build)
    ScraperClient("http://scraper:8000")
    assert any(isinstance(h, urllib.request.ProxyHandler) and h.proxies == {} for h in seen)


def test_an_error_body_that_dies_mid_read_is_still_a_clean_refusal():
    class Broken(io.BytesIO):
        def read(self, *a):
            raise http.client.IncompleteRead(b"x")
    c, _ = client_with(urllib.error.HTTPError("http://scraper:8000/x", 422, "err", {}, Broken()))
    with pytest.raises(Invalid, match="refused that value"):
        c.features()


def test_a_redirect_is_not_followed():
    """The scraper never redirects; if something answers with one, it is not to be trusted with the request."""
    from scraper_client import _NoRedirect
    assert _NoRedirect().redirect_request(None, None, 302, "Found", {}, "http://elsewhere/") is None


@pytest.mark.parametrize("url", ["http://scraper:8000", "http://scraper:8000/", "https://scraper.internal", "http://127.0.0.1:8000"])
def test_good_urls(url):
    assert check_url(url) == url.rstrip("/")


@pytest.mark.parametrize("url", ["", "scraper:8000", "ftp://scraper", "file:///etc/passwd", "http://", "http://user:pw@scraper:8000",
                                 "javascript:alert(1)"])
def test_bad_urls_are_refused(url):
    with pytest.raises(ValueError):
        check_url(url)


def test_a_stale_profile_save_is_the_profile_changed_error_with_the_scrapers_words():
    from store import ProfileChanged
    c, _ = client_with(http_error(409, b'{"detail": {"errors": ["the search profile was changed somewhere else: reload to see it"]}}'))
    with pytest.raises(ProfileChanged, match="changed somewhere else"):
        c.save_profile({"search_terms": ["x"]}, "2026-10-12T09:30:00+00:00")


def test_the_profile_calls_use_fixed_paths_and_find_gets_a_longer_timeout():
    c, seen = client_with(b'{"ok": true}')
    c.profile(), c.save_profile({"a": 1}, "v1"), c.find_board("Stripe")
    assert [(m, u) for m, u, *_ in seen] == [("GET", "http://scraper:8000/profile"), ("POST", "http://scraper:8000/profile"),
                                              ("POST", "http://scraper:8000/boards/find")]
    assert json.loads(seen[1][2]) == {"profile": {"a": 1}, "version": "v1"} and json.loads(seen[2][2]) == {"name": "Stripe"}
    assert seen[0][4] == 3 and seen[2][4] == 30.0, "the probe of five public APIs may take longer than a settings read"
