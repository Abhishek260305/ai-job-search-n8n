"""The Gmail client (Phase 6, step 6b): the login, a bounded read-only API, and the reading of one email.

Everything runs against the StubGmail stand-in on this machine (the suite refuses any other connection), so nothing here
touches Google. The stand-in fails in every way that matters: an expired or revoked login, a token with more permission than
read-only, redirects, rate limits, server errors, garbage and oversized replies."""
import base64
import logging
from datetime import datetime, timezone

import httpx
import pytest

import gmail_client as G
import stubs
from stubs import gmail_message

FAKE_SECRETS = ("1//stub-refresh-token-0123456789", "GOCSPX-stub-secret-0123456789", "123456789012-stubclientid0123456789.apps.googleusercontent.com")


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    monkeypatch.setattr(G, "RETRY_DELAY_S", 0)
    monkeypatch.delenv("GMAIL_API_URL", raising=False)


@pytest.fixture
def stub(monkeypatch):
    with stubs.StubGmail({"18f0a1": gmail_message("18f0a1"), "18f0a2": gmail_message("18f0a2", subject="Interview")}) as s:
        monkeypatch.setenv("GMAIL_API_URL", s.url)
        yield s


def client_for(stub, **kw):
    return G.GmailClient(stub.client_id, stub.client_secret, stub.refresh_token, **kw)


# ---------------------------------------------------------------- credentials

@pytest.mark.parametrize("values", [("", "s", "t"), ("i", "", "t"), ("i", "s", ""), ("  ", "s", "t"), ("i d", "s", "t"), ("i", "s", "t\nu"), (None, "s", "t")])
def test_a_missing_or_malformed_value_is_refused(values):
    with pytest.raises(G.NotConfigured):
        G.GmailClient(*values)


def test_from_env_names_what_is_missing_and_never_a_value():
    with pytest.raises(G.NotConfigured) as e:
        G.GmailClient.from_env({"GMAIL_CLIENT_ID": "id-value-123", "GMAIL_CLIENT_SECRET": "", "GMAIL_REFRESH_TOKEN": "  "})
    text = str(e.value)
    assert "GMAIL_CLIENT_SECRET" in text and "GMAIL_REFRESH_TOKEN" in text and "GMAIL_CLIENT_ID" not in text and "id-value-123" not in text


def test_a_client_never_shows_its_credentials():
    c = G.GmailClient("the-id", "the-secret", "the-token")
    assert "the-secret" not in repr(c) and "the-token" not in repr(c) and "the-id" not in repr(c)
    assert "the-secret" not in str(vars(c).get("_http", ""))


# ---------------------------------------------------------------- the login

def test_the_login_trades_the_refresh_token_for_an_access_token_and_reads_the_profile(stub):
    out = client_for(stub).profile()
    assert out == {"email": "jane.doe@gmail.com", "total": 2}
    assert len(stub.token_requests) == 1
    form = stub.token_requests[0]
    assert form == {"client_id": stub.client_id, "client_secret": stub.client_secret, "refresh_token": stub.refresh_token, "grant_type": "refresh_token"}
    assert stub.api_log[0][2] == f"Bearer {stub.issued[0]}"


def test_the_access_token_is_kept_until_shortly_before_it_ends(stub):
    now = [1000.0]
    c = client_for(stub, clock=lambda: now[0])
    c.profile(); c.list_messages("in:inbox"); c.profile()
    assert len(stub.token_requests) == 1, "one token for three calls"
    now[0] += 3599 - 59                                   # inside the safety margin of its end
    c.profile()
    assert len(stub.token_requests) == 2 and len(stub.issued) == 2
    assert stub.api_log[-1][2] == f"Bearer {stub.issued[1]}"


def test_a_very_short_lived_token_is_simply_renewed_each_time(stub):
    stub.modes["token"] = "short_lived"
    c = client_for(stub)
    c.profile(); c.profile()
    assert len(stub.issued) == 2


def test_an_expired_or_revoked_login_says_to_sign_in_again_and_names_no_secret(stub):
    stub.modes["token"] = "invalid_grant"
    with pytest.raises(G.LoginExpired, match="sign in again") as e:
        client_for(stub).profile()
    assert not any(s in str(e.value) for s in FAKE_SECRETS)
    assert stub.api_log == [], "no Gmail call was attempted without a token"


def test_a_wrong_client_id_or_secret_is_a_login_problem_too(stub):
    for bad in ({"client_secret": "GOCSPX-wrong"}, {"client_id": "999-wrong.apps.googleusercontent.com"}):
        c = G.GmailClient(bad.get("client_id", stub.client_id), bad.get("client_secret", stub.client_secret), stub.refresh_token)
        with pytest.raises(G.LoginExpired, match="client id or secret"):
            c.profile()
    stub.modes["token"] = "invalid_client"
    with pytest.raises(G.LoginExpired):
        client_for(stub).profile()


def test_a_wrong_refresh_token_is_refused(stub):
    with pytest.raises(G.LoginExpired):
        G.GmailClient(stub.client_id, stub.client_secret, "1//some-other-token-0123456789").profile()


@pytest.mark.parametrize("mode", ["overbroad", "no_scope"])
def test_a_token_with_more_than_read_only_permission_is_refused_before_it_is_used(stub, mode):
    stub.modes["token"] = mode
    with pytest.raises(G.Overbroad, match="read-only"):
        client_for(stub).profile()
    assert stub.api_log == [], "the token was never sent anywhere"


def test_a_token_answer_without_a_token_or_with_garbage_is_an_error(stub):
    for mode, message in (("no_token", "no usable access token"), ("garbage", "not JSON")):
        stub.modes["token"] = mode
        with pytest.raises(G.GmailError, match=message):
            client_for(stub).profile()


def test_a_server_error_from_the_login_is_retried_once_then_reported(stub):
    stub.modes["token"] = "server_error"
    with pytest.raises(G.GmailError, match="tried twice"):
        client_for(stub).profile()
    assert len(stub.token_requests) == 2
    stub.token_requests.clear()
    stub.modes["token"] = "server_error_once"
    assert client_for(stub).profile()["email"]
    assert len(stub.token_requests) == 2


def test_a_rate_limit_is_never_retried_and_carries_the_wait(stub):
    stub.modes["token"] = "rate_limited"
    with pytest.raises(G.RateLimited) as e:
        client_for(stub).profile()
    assert e.value.retry_after == 7 and len(stub.token_requests) == 1


def test_a_redirect_is_never_followed(stub):
    for scope in ("token", "api"):
        stub.modes.clear()
        stub.modes[scope] = "redirect"
        with pytest.raises(G.GmailError, match="redirect"):
            client_for(stub).profile()


# ---------------------------------------------------------------- the API

def test_an_access_token_refused_once_is_renewed_and_the_call_succeeds(stub):
    stub.modes["api"] = "unauthorized_once"
    assert client_for(stub).profile()["email"]
    assert len(stub.issued) == 2 and len(stub.api_log) == 2


def test_an_access_token_refused_twice_means_sign_in_again(stub):
    stub.modes["api"] = "unauthorized"
    with pytest.raises(G.LoginExpired):
        client_for(stub).profile()
    assert len(stub.issued) == 2, "one renewal, not a loop"


@pytest.mark.parametrize("mode,error,match", [
    ("rate_limited", G.RateLimited, "limiting"), ("rate_limited_403", G.RateLimited, "limiting"),
    ("forbidden", G.LoginExpired, "lacks permission"), ("api_disabled", G.GmailError, "not enabled"),
    ("garbage", G.GmailError, "not JSON"), ("redirect", G.GmailError, "redirect")])
def test_api_errors_are_named_in_words(stub, mode, error, match):
    stub.modes["api"] = mode
    with pytest.raises(error, match=match):
        client_for(stub).profile()


def test_an_api_rate_limit_carries_the_wait_and_is_not_retried(stub):
    stub.modes["api"] = "rate_limited"
    with pytest.raises(G.RateLimited) as e:
        client_for(stub).profile()
    assert e.value.retry_after == 9 and len(stub.api_log) == 1


def test_an_api_server_error_is_retried_once(stub):
    stub.modes["api"] = "server_error"
    with pytest.raises(G.GmailError, match="tried twice"):
        client_for(stub).profile()
    assert len(stub.api_log) == 2
    stub.api_log.clear()
    stub.modes["api"] = "server_error_once"
    assert client_for(stub).profile()["email"] and len(stub.api_log) == 2


def test_a_slow_google_is_given_up_on(stub):
    stub.modes["api"] = "slow"
    stub.slow_seconds = 1.5
    c = G.GmailClient(stub.client_id, stub.client_secret, stub.refresh_token, http=httpx.Client(timeout=httpx.Timeout(0.3), follow_redirects=False))
    with pytest.raises(G.GmailError, match="tried twice"):
        c.profile()


def test_only_the_two_google_hosts_are_ever_called(monkeypatch):
    monkeypatch.delenv("GMAIL_API_URL", raising=False)
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={})
    http = httpx.Client(transport=httpx.MockTransport(handler))
    for url in ("https://evil.example/token", "https://oauth2.googleapis.com.evil.example/token", "https://gmail.googleapis.com@evil.example/x",
                "http://oauth2.googleapis.com/token", "https://oauth2.googleapis.com:8443/token", "https://127.0.0.1/", "file:///etc/passwd",
                "https://gmail.googleapis.com\\@evil.example/", "https://[::1]/"):
        with pytest.raises(G.GmailError, match="not a Google host"):
            G._request(http, "GET", url)
    assert calls == [], "nothing was sent to any of them"
    G._request(http, "GET", "https://gmail.googleapis.com/gmail/v1/users/me/profile")
    G._request(http, "POST", "https://oauth2.googleapis.com/token")
    assert len(calls) == 2


def test_with_the_test_override_set_only_the_override_is_allowed(stub):
    other = stub.url.replace(str(stub._httpd.server_address[1]), "9")
    with pytest.raises(G.GmailError, match="not a Google host"):
        G._request(httpx.Client(), "GET", other + "/gmail/gmail/v1/users/me/profile")
    with pytest.raises(G.GmailError, match="not a Google host"):
        G._request(httpx.Client(), "GET", "https://gmail.googleapis.com/gmail/v1/users/me/profile")


def test_the_client_can_only_read():
    public = {n for n in dir(G.GmailClient) if not n.startswith("_")}
    assert public == {"access_token", "close", "from_env", "get_message", "list_messages", "message_date", "profile"}


def test_nothing_the_client_sends_can_change_a_mailbox(stub):
    c = client_for(stub)
    c.profile(); c.list_messages("in:inbox"); c.get_message("18f0a1")
    assert all(path.startswith("/gmail/gmail/v1/users/me/") for path, _q, _a in stub.api_log)


# ---------------------------------------------------------------- listing and reading

def test_a_search_lists_ids_and_pages_through_the_rest(stub):
    c = client_for(stub)
    first, token = c.list_messages("newer_than:30d", max_results=1)
    assert [m["id"] for m in first] == ["18f0a2"] and token == "p1"
    second, token = c.list_messages("newer_than:30d", max_results=1, page_token=token)
    assert [m["id"] for m in second] == ["18f0a1"] and token is None
    assert stub.queries == ["newer_than:30d", "newer_than:30d"]


def test_spam_and_trash_are_never_asked_for(stub):
    client_for(stub).list_messages("in:anywhere")
    query = next(q for path, q, _ in stub.api_log if path.endswith("/messages"))
    assert query["includeSpamTrash"] == ["false"]


@pytest.mark.parametrize("query,max_results", [("", 10), ("   ", 10), ("x" * 1001, 10), (None, 10), ("ok", 0), ("ok", 101), ("ok", True), ("ok", "5")])
def test_a_bad_search_is_refused_before_any_request(stub, query, max_results):
    with pytest.raises(ValueError):
        client_for(stub).list_messages(query, max_results=max_results)
    assert stub.token_requests == [] and stub.api_log == []


def test_ids_that_are_not_ids_are_dropped_from_a_listing(stub):
    stub.messages = {"18f0a1": gmail_message("18f0a1")}
    c = client_for(stub)
    c.profile()
    import json as _json
    original = c._api
    c._api = lambda path, params=None, **kw: {"messages": [{"id": "../../etc/passwd"}, {"id": 5}, {"id": "ok_id-1", "threadId": "bad id"}, "x", None],
                                              "nextPageToken": "t" * 600}
    ids, nxt = c.list_messages("q")
    assert ids == [{"id": "ok_id-1", "thread_id": ""}] and nxt is None
    c._api = original


def test_one_message_is_read_and_parsed(stub):
    stub.messages["18f0a3"] = gmail_message("18f0a3", subject="Interview invitation", text="We would like to schedule a call.",
                                            sender="Priya Sharma <priya@acme.com>")
    m = client_for(stub).get_message("18f0a3")
    assert (m.id, m.subject, m.from_name, m.from_address, m.from_domain) == ("18f0a3", "Interview invitation", "Priya Sharma", "priya@acme.com", "acme.com")
    assert m.text == "We would like to schedule a call." and m.received_at == datetime(2026, 10, 12, 9, 30, tzinfo=timezone.utc)
    assert m.auth["trusted"] and m.auth["dmarc"] == "pass"


@pytest.mark.parametrize("bad", ["../etc/passwd", "a b", "", "x" * 65, "id?format=raw", "a/b", None, 5])
def test_a_message_id_that_could_change_the_path_is_refused_before_any_request(stub, bad):
    with pytest.raises(ValueError):
        client_for(stub).get_message(bad)
    assert stub.api_log == []


def test_a_deleted_message_is_its_own_error(stub):
    stub.modes["message"] = "gone"
    with pytest.raises(G.MessageGone):
        client_for(stub).get_message("18f0a1")
    with pytest.raises(G.MessageGone):
        stub.modes.clear()
        client_for(stub).get_message("0000")


def test_an_oversized_message_is_refused_without_being_buffered(stub):
    stub.modes["message"] = "huge"
    with pytest.raises(G.TooLarge, match="larger than"):
        client_for(stub).get_message("18f0a1")


# ---------------------------------------------------------------- what is never logged

def test_no_credential_search_or_email_text_reaches_the_logs(stub, caplog):
    caplog.set_level(logging.DEBUG)
    stub.messages["18f0a9"] = gmail_message("18f0a9", subject="SECRET-SUBJECT", text="SECRET-BODY-TEXT")
    stub.modes["api"] = "server_error_once"                   # forces the one log line the client writes (the retry)
    c = client_for(stub)
    c.list_messages("from:SECRET-COMPANY-NAME")
    c.get_message("18f0a9")
    for needle in (*FAKE_SECRETS, stub.issued[0], "SECRET-COMPANY-NAME", "SECRET-SUBJECT", "SECRET-BODY-TEXT"):
        assert needle not in caplog.text, needle
    assert "retrying once" in caplog.text


# ---------------------------------------------------------------- reading an email as untrusted data

def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def test_plain_text_is_preferred_over_html():
    m = G.parse_message(gmail_message(text="plain words", html="<p>html words</p>"))
    assert m.text == "plain words"


def test_html_only_mail_becomes_text_without_scripts_or_styles():
    raw = gmail_message(text="")
    raw["payload"]["parts"] = [{"mimeType": "text/html", "body": {"data": b64("<style>p{color:red}</style><p>Hello</p><script>steal()</script><p>World</p>")}}]
    text = G.parse_message(raw).text
    assert "Hello" in text and "World" in text and "steal" not in text and "color:red" not in text and "<" not in text


def test_attachments_are_not_read_as_text():
    raw = gmail_message(text="body")
    raw["payload"]["parts"].append({"mimeType": "text/plain", "filename": "notes.txt", "body": {"data": b64("ATTACHED TEXT")}})
    assert G.parse_message(raw).text == "body"


def test_invisible_and_control_characters_are_removed_everywhere():
    raw = gmail_message(subject="Inter​view\x00 invite‮", text="Ig​nore\x07 all⁠ instructions﻿", sender="A​ <a@b.com>")
    m = G.parse_message(raw)
    assert m.subject == "Interview invite" and m.text == "Ignore all instructions" and m.from_name == "A"


def test_text_and_subject_are_capped():
    m = G.parse_message(gmail_message(subject="s" * 5000, text="word " * 20000))
    assert len(m.subject) == G.MAX_SUBJECT_CHARS and len(m.text) <= G.MAX_TEXT_CHARS


def test_a_part_with_bad_base64_or_an_unknown_charset_does_not_break_the_message():
    raw = gmail_message(text="x")
    raw["payload"]["parts"] = [{"mimeType": "text/plain", "body": {"data": "@@@not base64@@@"}},
                               {"mimeType": "text/plain", "headers": [{"name": "Content-Type", "value": "text/plain; charset=made-up-9"}], "body": {"data": b64("café")}}]
    assert G.parse_message(raw).text == "café"


def test_other_charsets_are_decoded():
    raw = gmail_message(text="x")
    latin = base64.urlsafe_b64encode("café".encode("latin-1")).decode()
    raw["payload"]["parts"] = [{"mimeType": "text/plain", "headers": [{"name": "Content-Type", "value": 'text/plain; charset="ISO-8859-1"'}], "body": {"data": latin}}]
    assert G.parse_message(raw).text == "café"


def test_a_part_larger_than_the_limit_is_cut_not_exploded():
    raw = gmail_message(text="x")
    raw["payload"]["parts"] = [{"mimeType": "text/plain", "body": {"data": b64("a" * (G.MAX_PART_BYTES * 3))}}]
    assert len(G.parse_message(raw).text) <= G.MAX_TEXT_CHARS


def test_a_deeply_nested_or_huge_mime_tree_is_bounded():
    leaf = {"mimeType": "text/plain", "body": {"data": b64("DEEP")}}
    node = leaf
    for _ in range(50):
        node = {"mimeType": "multipart/mixed", "parts": [node]}
    raw = gmail_message(text="")
    raw["payload"]["parts"] = [node]
    assert "DEEP" not in G.parse_message(raw).text, "nesting beyond the limit is not followed"
    raw["payload"]["parts"] = [{"mimeType": "text/plain", "body": {"data": b64("part")}} for _ in range(5000)]
    assert len(G.parse_message(raw).text) < 5000 * 6, "only the first parts are read"


@pytest.mark.parametrize("sender,address,domain", [
    ("Jane <jane@acme.com>", "jane@acme.com", "acme.com"), ("JANE@ACME.COM", "jane@acme.com", "acme.com"),
    ("jane@acme.com", "jane@acme.com", "acme.com"), ('"Doe, Jane" <jane@mail.acme.co.uk>', "jane@mail.acme.co.uk", "mail.acme.co.uk"),
    ("no address at all", "", ""), ("", "", ""), ("<>", "", ""), ("a@b@c.com", "", ""), ("a@under_score.com", "", ""),
    ("a@-bad.com", "", ""), ("a@bad-.com", "", ""), ("a@", "", ""), ("@acme.com", "@acme.com", "acme.com"),
    ("x@acme.com, y@evil.com", "", ""), ("a b@acme.com", "", ""),       # two senders in one header is ambiguous: no address
    ("a@localhost", "", ""), ("a@acme..com", "", ""), ("a@.acme.com", "", ""), ("a@acme.com.", "", ""), ("a@" + "x" * 64 + ".com", "", ""),
    # a domain written in non-ASCII letters is "no domain" and is never converted (IDNA maps look-alikes onto real domains);
    # a punycode domain is kept exactly as written
    ("a@m\u00fcnchen.de", "", ""), ("b@stra\u00dfe.de", "", ""), ("a@acme\u00ad.com", "", ""), ("a@\uff41cme.com", "", ""),
    ("a@xn--mnchen-3ya.de", "a@xn--mnchen-3ya.de", "xn--mnchen-3ya.de")])
def test_the_sender_address_and_domain(sender, address, domain):
    m = G.parse_message(gmail_message(sender=sender))
    assert (m.from_address, m.from_domain) == (address, domain)


def test_a_message_with_no_usable_sender_still_parses_with_an_empty_domain():
    m = G.parse_message(gmail_message(sender="garbage"))
    assert m.from_domain == "" and m.from_address == "" and m.id


def test_the_received_time_comes_from_gmail_and_falls_back_to_the_date_header():
    when = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    assert G.parse_message(gmail_message(when=when)).received_at == when
    raw = gmail_message(when=when)
    del raw["internalDate"]
    assert G.parse_message(raw).received_at == when
    raw["payload"]["headers"] = [h for h in raw["payload"]["headers"] if h["name"] != "Date"]
    assert G.parse_message(raw).received_at is None
    raw["internalDate"] = "9" * 30
    assert G.parse_message(raw).received_at is None


@pytest.mark.parametrize("bad", [None, [], "x", 5, {}, {"id": 5}, {"id": ""}, {"id": "../x"}, {"id": "a b"}])
def test_something_that_is_not_a_message_is_an_error_never_a_crash(bad):
    with pytest.raises(G.GmailError):
        G.parse_message(bad)


def test_odd_but_valid_shapes_do_not_crash():
    for payload in (None, "x", [], {"headers": "nope", "parts": "nope"}, {"headers": [None, 5, {"name": 1}, {"value": "x"}]}, {"parts": [None, 5, "x"]}):
        m = G.parse_message({"id": "abc", "payload": payload, "threadId": 5})
        assert m.id == "abc" and m.thread_id == "" and m.text == "" and m.subject == ""


# ---------------------------------------------------------------- what Google says about the sender

def test_googles_own_authentication_result_is_used():
    m = G.parse_message(gmail_message(dmarc="pass", spf="pass", dkim="pass", header_from="acme.com"))
    assert m.auth == {"trusted": True, "dmarc": "pass", "spf": "pass", "dkim": "pass", "header_from": "acme.com"}
    m = G.parse_message(gmail_message(dmarc="fail", spf="softfail", dkim="none"))
    assert (m.auth["dmarc"], m.auth["spf"], m.auth["dkim"]) == ("fail", "softfail", "none")


def test_a_second_header_that_claims_to_be_googles_makes_the_whole_verdict_untrusted():
    """Google adds exactly one. Two means the sender wrote one, and which is which cannot be told: believe neither."""
    forged = "mx.google.com; dkim=pass; spf=pass; dmarc=pass header.from=paypal.com"
    m = G.parse_message(gmail_message(dmarc="fail", spf="fail", dkim="fail", header_from="evil.example", forged_auth=forged))
    assert m.auth == {"trusted": False, "dmarc": "unknown", "spf": "unknown", "dkim": "unknown", "header_from": ""}
    assert not m.sender_verified


def test_an_authentication_header_from_another_server_is_ignored_whatever_it_claims():
    m = G.parse_message(gmail_message(auth_server="mx.evil.example", dmarc="pass"))
    assert m.auth == {"trusted": False, "dmarc": "unknown", "spf": "unknown", "dkim": "unknown", "header_from": ""}
    m = G.parse_message(gmail_message(auth_server="evil.example (mx.google.com)", dmarc="pass"))
    assert not m.auth["trusted"]


def test_no_authentication_header_means_unknown_not_a_pass():
    raw = gmail_message()
    raw["payload"]["headers"] = [h for h in raw["payload"]["headers"] if h["name"] != "Authentication-Results"]
    assert G.parse_message(raw).auth == {"trusted": False, "dmarc": "unknown", "spf": "unknown", "dkim": "unknown", "header_from": ""}


def test_a_method_missing_from_googles_header_is_none_and_lookalike_names_do_not_count():
    h = {"authentication-results": ["mx.google.com; spf=pass smtp.mailfrom=a.com; x-dmarc=pass; mydmarc=pass"]}
    r = G.parse_auth_results(h)
    assert r["spf"] == "pass" and r["dmarc"] == "none" and r["dkim"] == "none"
    assert G.parse_auth_results({"authentication-results": ["MX.GOOGLE.COM; DMARC=PASS"]})["dmarc"] == "pass"


# ---------------------------------------------------------------- text that is stored or shown

@pytest.mark.parametrize("secret", ["ya29.a0AfH6SMBx-abcdefghijk_LMNOP", "1//0gAbCdEfGhIjKlMnOpQrStUvWxYz-_", "GOCSPX-AbCdEfGhIjKlMnOpQr_-",
                                   "123456789012-" + "abcdefghijklmnopqrstuvwxyz012345" + ".apps.googleusercontent.com", "Bearer abcdefgh12345678",
                                   "refresh_token=abc123def456", "client_secret=abc123", "code=4/0AbCdEfG"])
def test_credentials_are_removed_from_text(secret):
    out = G.redact(f"Google said: {secret} and more")
    assert secret not in out and "[removed]" in out and out.startswith("Google said:")


def test_redact_keeps_one_line_and_a_limit():
    assert G.redact("a\nb\tc\x00d") == "a b c d"
    assert len(G.redact("x" * 1000)) == 300 and G.redact("x" * 1000).endswith("…")
    assert G.redact("x" * 50, limit=10) == "x" * 9 + "…"
    assert G.redact(None) == "None"


@pytest.mark.parametrize("address,masked", [("jane.doe@gmail.com", "j***@gmail.com"), ("A@B.COM", "A***@b.com"), ("", "(unknown)"), ("nope", "(unknown)"),
                                            ("a@b@c.com", "(unknown)"), (None, "(unknown)"), ("@x.com", "(unknown)"), ("a@bad domain.com", "(unknown)")])
def test_an_address_is_masked_for_display(address, masked):
    assert G.mask_address(address) == masked


# ---------------------------------------------------------------- the findings of the independent review

def _msg_with_part(charset: str, text: str = "hello +AGEAYgBj-"):
    raw = gmail_message(text="x")
    raw["payload"]["parts"] = [{"mimeType": "text/plain", "headers": [{"name": "Content-Type", "value": f"text/plain; charset={charset}"}], "body": {"data": b64(text)}}]
    return raw


@pytest.mark.parametrize("charset", ["undefined", "idna", "punycode", "utf-7", "made-up-9", "rot13", "base64", "unicode-escape", "raw-unicode-escape", "mbcs", "../x"])
def test_a_charset_a_sender_chooses_can_neither_crash_the_reader_nor_hide_text(charset):
    m = G.parse_message(_msg_with_part(charset))
    assert m.text == "hello +AGEAYgBj-", "read as UTF-8 whatever the sender claimed (utf-7 would have decoded +AGEAYgBj- into hidden letters)"


@pytest.mark.parametrize("charset", ["UTF-8", "utf8", "us-ascii", "ISO-8859-1", "iso-8859-15", "windows-1252", "koi8-r", "gbk", "big5", "shift_jis", "euc-kr"])
def test_the_real_mail_charsets_are_still_honoured(charset):
    assert G.parse_message(_msg_with_part(charset, "plain text")).text == "plain text"


@pytest.mark.parametrize("hidden", ["\U000e0041", "\U000e007f", "\u2066", "\u2069", "\u00ad", "\ufe0f", "\ufe00", "\u180e", "\u061c", "\u200e", "\u202e", "\x85", "\u2028", "\u2029", "\x9f"])
def test_every_kind_of_invisible_character_is_removed_from_text_and_subject(hidden):
    m = G.parse_message(gmail_message(subject=f"in{hidden}vite", text=f"ign{hidden}ore"))
    assert m.subject == "invite" or m.subject == "in vite", m.subject
    assert hidden not in m.text and hidden not in m.subject and m.text.replace(" ", "").replace("\n", "") == "ignore"


def test_ordinary_text_in_other_scripts_and_emoji_survives():
    text = "Interview \u9762\u8bd5 \u0645\u0631\u062d\u0628\u0627 \u0928\u092e\u0938\u094d\u0924\u0947 \U0001f600"
    assert G.parse_message(gmail_message(text=text)).text == text


@pytest.mark.parametrize("value", ["abc\n", "abc\n\n", "a\nb", "\nabc"])
def test_an_id_with_a_newline_is_not_an_id(stub, value):
    assert G._ID.fullmatch(value) is None
    with pytest.raises(ValueError):
        client_for(stub).get_message(value)


@pytest.mark.parametrize("domain,ok", [("example.com", True), ("example.com\n", False), ("EXAMPLE.com", False), ("m\u00fcnchen.de", False),
                                       ("xn--mnchen-3ya.de", True), ("a.b", True), ("localhost", False), ("", False), (None, False), (5, False)])
def test_domain_validity(domain, ok):
    assert G.valid_domain(domain) is ok


def test_a_verdict_cannot_be_supplied_by_text_a_sender_controls_inside_googles_header():
    """The review's example: a Return-Path local part and a DKIM selector that contain verdict-looking text."""
    nasty = ('mx.google.com; dkim=fail header.i=@evil.com header.s=dmarc=pass; '
             'spf=pass (google.com: domain of "x dmarc=pass header.from=victim.com"@evil.com designates 1.2.3.4 as permitted sender) smtp.mailfrom="y dmarc=pass"@evil.com; '
             'dmarc=fail (p=NONE sp=NONE dis=NONE) header.from=evil.com')
    r = G.parse_auth_results({"authentication-results": [nasty]})
    assert r == {"trusted": True, "dmarc": "fail", "spf": "pass", "dkim": "fail", "header_from": "evil.com"}


def test_a_semicolon_inside_text_a_sender_controls_cannot_start_a_fake_segment():
    """The sharper attack: a comment or quoted local part that contains `;` so that splitting on `;` would invent a dmarc=pass segment."""
    in_comment = "mx.google.com; spf=pass (google.com: domain of x@evil.com; dmarc=pass header.from=victim.com designates 1.2.3.4); dmarc=fail header.from=evil.com"
    in_quotes = 'mx.google.com; spf=pass smtp.mailfrom="a; dmarc=pass header.from=victim.com"@evil.com; dmarc=fail header.from=evil.com'
    for header in (in_comment, in_quotes):
        r = G.parse_auth_results({"authentication-results": [header]})
        assert (r["dmarc"], r["header_from"], r["spf"]) == ("fail", "evil.com", "pass"), header


def test_comments_quoted_strings_and_odd_spacing_in_googles_own_header_are_read_correctly():
    r = G.parse_auth_results({"authentication-results": ['mx.google.com (an (inner) comment); dkim = pass header.i=@a.com ; spf=neutral; dmarc = pass (p=REJECT) header.from = a.com']})
    assert r == {"trusted": True, "dmarc": "pass", "spf": "neutral", "dkim": "pass", "header_from": "a.com"}
    assert G.parse_auth_results({"authentication-results": ["mx.google.com; unbalanced ( dmarc=pass header.from=a.com"]})["dmarc"] == "none", "an unclosed comment hides the rest"


@pytest.mark.parametrize("values", [[], ["mx.google.com; dmarc=pass", "mx.google.com; dmarc=pass"], ["; dmarc=pass"], ["dmarc=pass"],
                                    ["mx.google.com.evil.example; dmarc=pass"], ["evil (mx.google.com); dmarc=pass"]])
def test_no_google_header_or_more_than_one_is_never_trusted(values):
    assert G.parse_auth_results({"authentication-results": values})["trusted"] is False


def test_only_a_header_on_googles_own_server_is_one_of_ours_when_others_are_present():
    r = G.parse_auth_results({"authentication-results": ["mx.example.net; dmarc=fail", "mx.google.com; dmarc=pass header.from=a.com"]})
    assert r["trusted"] and r["dmarc"] == "pass", "another server's header beside Google's one is simply ignored"


def test_the_sender_is_verified_only_when_google_vouched_for_the_domain_that_was_read():
    assert G.parse_message(gmail_message(sender="a@acme.com", dmarc="pass", header_from="acme.com")).sender_verified is True
    assert G.parse_message(gmail_message(sender="a@acme.com", dmarc="pass", header_from="evil.example")).sender_verified is False
    assert G.parse_message(gmail_message(sender="a@acme.com", dmarc="fail", header_from="acme.com")).sender_verified is False
    assert G.parse_message(gmail_message(sender="a@acme.com", dmarc="pass", header_from="acme.com", auth_server="mx.evil.example")).sender_verified is False
    assert G.parse_message(gmail_message(sender="garbage", dmarc="pass", header_from="acme.com")).sender_verified is False
    assert G.parse_message(gmail_message(sender="a@stra\u00dfe.de", dmarc="pass", header_from="strasse.de")).sender_verified is False, "the look-alike is not the real domain"


def test_a_huge_date_offset_and_an_infinite_lifetime_do_not_crash():
    raw = gmail_message()
    del raw["internalDate"]
    for h in raw["payload"]["headers"]:
        if h["name"] == "Date":
            h["value"] = "Mon, 12 Oct 2026 09:30:00 +99999999999999999999"
    assert G.parse_message(raw).received_at is None


def test_an_infinite_token_lifetime_is_treated_as_the_default(stub):
    stub.modes["token"] = "inf_expiry"
    assert client_for(stub).profile()["email"]


def test_a_disabled_client_is_a_login_problem(stub):
    stub.modes["token"] = "disabled_client"
    with pytest.raises(G.LoginExpired, match="disabled"):
        client_for(stub).profile()


def test_a_403_that_signing_in_again_cannot_fix_is_not_called_a_login_problem(stub):
    stub.modes["api"] = "forbidden_other"
    with pytest.raises(G.GmailError, match="refused the request.*quotaExceeded") as e:
        client_for(stub).profile()
    assert not isinstance(e.value, G.LoginExpired)


def test_a_404_is_a_missing_message_only_for_a_message(stub):
    stub.modes["api"] = "not_found"
    with pytest.raises(G.GmailError) as e:
        client_for(stub).profile()
    assert not isinstance(e.value, G.MessageGone)
    with pytest.raises(G.MessageGone):
        client_for(stub).get_message("18f0a1")


@pytest.mark.parametrize("url", ["http://evil.example", "https://127.0.0.1:9", "http://127.0.0.1@evil.example/", "http://evil.example/127.0.0.1", "ftp://127.0.0.1",
                                 "http://127.0.0.1.evil.example", "http://[::2]:9", "http://0.0.0.0:9"])
def test_the_test_override_is_refused_unless_it_is_this_machine(monkeypatch, url):
    monkeypatch.setenv("GMAIL_API_URL", url)
    with pytest.raises(G.GmailError, match="GMAIL_API_URL"):
        G.base_url("oauth")
    c = G.GmailClient("1-abcdefghijk.apps.googleusercontent.com", "GOCSPX-abcdefgh", "1//abcdefghijklmnopqrstuvwxyz")
    with pytest.raises(G.GmailError, match="GMAIL_API_URL"):
        c.profile()


def test_the_test_override_is_accepted_for_loopback(monkeypatch):
    for url in ("http://127.0.0.1:5000", "http://localhost:5000/", "http://[::1]:5000"):
        monkeypatch.setenv("GMAIL_API_URL", url)
        assert G.base_url("gmail").endswith("/gmail") and G.base_url("gmail").startswith("http://")


def test_a_client_closes_the_pool_it_made_and_leaves_one_it_was_given():
    own = G.GmailClient("i", "s", "t")
    with own as c:
        assert not c._http.is_closed
    assert own._http.is_closed
    lent = httpx.Client()
    G.GmailClient("i", "s", "t", http=lent).close()
    assert not lent.is_closed
    lent.close()


def test_a_server_that_drips_bytes_is_cut_off_at_the_deadline_and_not_retried(stub, monkeypatch):
    stub.modes["api"] = "drip"
    monkeypatch.setattr(G, "REQUEST_DEADLINE_S", 0.4)
    c = G.GmailClient(stub.client_id, stub.client_secret, stub.refresh_token, http=httpx.Client(timeout=httpx.Timeout(5.0), follow_redirects=False))
    started = __import__("time").monotonic()
    with pytest.raises(G.GmailError, match="took too long"):
        c.profile()
    assert __import__("time").monotonic() - started < 3 and len(stub.api_log) == 1, "a slow source is skipped for this run, not retried"


@pytest.mark.parametrize("secret", ["1/0AbCdEfGhIjKlMnOpQrStUvWx", "1//0AbCdEfGhIjKlMnOpQrStUvWx"])
def test_both_refresh_token_shapes_are_redacted(secret):
    assert secret not in G.redact(f"failed with {secret} ok")


# ---------------------------------------------------------------- links and unstorable text (the review of 6c)

def test_links_come_from_the_plain_text_and_from_every_html_part_including_button_targets():
    raw = gmail_message(text="Plain: https://one.example/a and https://two.example/b.", html='<a href="https://three.example/c">Click</a> <a href=\'https://four.example/d\'>x</a> <a href=https://five.example/e>y</a>')
    m = G.parse_message(raw)
    assert set(m.links) == {"https://one.example/a", "https://two.example/b", "https://three.example/c", "https://four.example/d", "https://five.example/e"}


def test_only_web_links_are_kept_and_each_once():
    raw = gmail_message(text="x", html='<a href="javascript:alert(1)">a</a><a href="mailto:a@b.com">b</a><a href="/relative">c</a><a href="https://a.example">d</a><a href="https://a.example">e</a><a href="data:text/html,x">f</a>')
    assert G.parse_message(raw).links == ("https://a.example",)


def test_links_are_bounded_in_number_and_length():
    many = " ".join(f"https://h{i}.example/{'x' * 600}" for i in range(200))
    m = G.parse_message(gmail_message(text=many))
    assert len(m.links) == G.MAX_LINKS and all(len(u) <= 500 for u in m.links)


def test_a_lone_surrogate_in_header_text_becomes_a_question_mark_so_it_can_be_stored():
    """Gmail's JSON can carry a \\ud800 escape; json.loads turns it into a lone surrogate that cannot be written to UTF-8 or jsonb."""
    raw = gmail_message(text="body")
    for h in raw["payload"]["headers"]:
        if h["name"] == "Subject":
            h["value"] = "bad \ud800 subject"
        if h["name"] == "From":
            h["value"] = "N\ud800ame <a@acme.com>"
    m = G.parse_message(raw)
    for field in (m.subject, m.from_name, m.text):
        field.encode("utf-8")                                   # would raise on a lone surrogate
    assert "?" in m.subject and m.from_domain == "acme.com"
    assert G._scrub("a\udfffb") == "a?b" and G._scrub("fine \u00e9\U0001f600") == "fine \u00e9\U0001f600"


def test_a_messages_date_is_read_without_reading_the_message(stub):
    when = datetime(2026, 9, 1, 8, 30, 15, tzinfo=timezone.utc)
    stub.messages["18f0b1"] = gmail_message("18f0b1", when=when)
    assert client_for(stub).message_date("18f0b1") == when
    path, query, _auth = stub.api_log[-1]
    assert path.endswith("/messages/18f0b1") and query["format"] == ["minimal"] and query["fields"] == ["internalDate"]


@pytest.mark.parametrize("bad", ["../x", "a b", "", None, "abc\n"])
def test_a_message_date_of_something_that_is_not_an_id_is_refused_before_any_request(stub, bad):
    with pytest.raises(ValueError):
        client_for(stub).message_date(bad)
    assert stub.api_log == []


def test_a_message_with_no_date_or_a_missing_one_is_an_error(stub):
    stub.messages["18f0b2"] = {"id": "18f0b2"}
    with pytest.raises(G.GmailError, match="no date"):
        client_for(stub).message_date("18f0b2")
    with pytest.raises(G.MessageGone):
        client_for(stub).message_date("0000")


def test_a_button_target_with_characters_the_plain_url_pattern_would_cut_is_kept_whole():
    raw = gmail_message(text="x", html='<a href="https://a.example/path(1)/x?q=[2]">Open</a>')
    assert "https://a.example/path(1)/x?q=[2]" in G.parse_message(raw).links, "read from the href itself, not cut at a bracket"
