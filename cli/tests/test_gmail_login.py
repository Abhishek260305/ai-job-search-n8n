"""`jobsearch gmail login`: the one-time Google sign-in (Phase 6, step 6b).

The browser is simulated (a thread that answers on the local address the way Google's redirect would) and Google is the
StubGmail stand-in, so nothing here touches Google. What matters most is what must NOT happen: a login that is more than
read-only, a wrong account, an answer meant for something else, or a credential appearing anywhere but the saved .env."""
import hashlib
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scraper"))

import gmail_login as L  # noqa: E402
import jobsearch_cli as C  # noqa: E402
import stubs  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
CLIENT_ID = "123456789012-stubclientid0123456789.apps.googleusercontent.com"
CLIENT_SECRET = "GOCSPX-stub-secret-0123456789"
REFRESH = "1//stub-refresh-token-0123456789"


@pytest.fixture
def google(monkeypatch):
    with stubs.StubGmail(account="jane.doe@gmail.com", refresh_token=REFRESH, client_id=CLIENT_ID, client_secret=CLIENT_SECRET) as s:
        monkeypatch.setenv("GMAIL_API_URL", s.url)
        yield s


class LoginStack:
    """Records what would run on the machine that runs the stack: (args, standard input). Never runs anything."""

    def __init__(self, ssh=None, fail=None):
        self.ssh = ssh
        self.calls = []
        self.composed = []
        self.fail = fail or {}

    def run_repo_script(self, args, input_text=None, timeout=60):
        self.calls.append((list(args), input_text))
        failed = self.fail.get(args[-1])
        return SimpleNamespace(returncode=1 if failed else 0, stdout="", stderr=failed or "")

    def compose(self, args, input_text=None, timeout=120):
        self.composed.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")


def browser(code="stub-auth-code-0123456789", *, state=None, error=None, extra_first=None, delay=0.05):
    """An `open_browser` that answers the way Google's redirect would: a GET to the local address with the code and the state."""
    seen = {}

    def open_browser(url):
        query = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).items()}
        seen.update(url=url, query=query)

        def answer():
            time.sleep(delay)
            base = query["redirect_uri"]
            for extra in (extra_first or []):
                try:
                    urllib.request.urlopen(base.rstrip("/") + extra, timeout=5).read()
                except urllib.error.HTTPError as e:
                    e.read()
            params = {"state": state or query["state"]}
            params.update({"error": error} if error else {"code": code})
            try:
                urllib.request.urlopen(base + "?" + urllib.parse.urlencode(params), timeout=5).read()
            except urllib.error.HTTPError as e:
                e.read()
        threading.Thread(target=answer, daemon=True).start()
        return True
    open_browser.seen = seen
    return open_browser


def run(stack, google_env=True, *, answers=("y",), open_browser=None, **kw):
    """Drive the login. The prompts: client id, then the confirmation (the secret goes through secret_fn)."""
    said = []
    prompts = iter(([CLIENT_ID] if google_env else []) + list(answers))
    out = kw.pop("out", None) or said.append
    ob = open_browser or browser()
    result = L.run_login(stack, env={}, input_fn=lambda prompt="": next(prompts), secret_fn=lambda prompt="": CLIENT_SECRET,
                         open_browser=ob, out=out, wait_seconds=kw.pop("wait_seconds", 10), **kw)
    return result, "\n".join(said), ob


def first_prompt_only():
    """An input_fn that answers the client-id prompt once (and fails on any later question)."""
    answers = iter([CLIENT_ID])
    return lambda prompt="": next(answers)


def all_text(*parts):
    return "\n".join(str(p) for p in parts)


# ---------------------------------------------------------------- the whole flow

def test_a_login_saves_the_three_values_through_standard_input_and_shows_none_of_them(google):
    stack = LoginStack()
    result, said, ob = run(stack)
    names = [args[-1] for args, _ in stack.calls]
    assert stack.calls[0][0] == ["scripts/init-env.sh", "--add-gmail"] and stack.calls[0][1] is None
    assert names[1:] == ["GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN"]
    assert [i for _, i in stack.calls[1:]] == [CLIENT_ID + "\n", CLIENT_SECRET + "\n", REFRESH + "\n"], "each value on standard input"
    for args, _ in stack.calls:
        assert not any(v in " ".join(args) for v in (CLIENT_ID, CLIENT_SECRET, REFRESH)), "never part of a command line"
    text = all_text(result, said)
    # the client id is not a secret (it is in the consent link by necessity); the secret, both tokens and the full address never show
    for secret in (CLIENT_SECRET, REFRESH, google.issued[0], "jane.doe@gmail.com", "code_verifier"):
        assert secret not in text, secret
    assert "Signed in as j***@gmail.com" in text and "values not shown" in text and "gmail check" in text and "--gmail on" in text


def test_the_request_to_google_asks_for_exactly_one_read_only_permission_offline_with_pkce(google):
    ob = browser()
    run(LoginStack(), open_browser=ob)
    q = ob.seen["query"]
    assert q["scope"] == "https://www.googleapis.com/auth/gmail.readonly" and q["access_type"] == "offline" and q["prompt"] == "consent"
    assert q["response_type"] == "code" and q["code_challenge_method"] == "S256" and q["include_granted_scopes"] == "false"
    assert ob.seen["url"].startswith("https://accounts.google.com/o/oauth2/v2/auth?")
    assert re.fullmatch(r"http://127\.0\.0\.1:\d+/", q["redirect_uri"]), "the answer comes back to this computer only"
    verifier = google.exchanges[0]["code_verifier"]
    assert base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=") == q["code_challenge"]
    assert google.exchanges[0]["redirect_uri"] == q["redirect_uri"] and google.exchanges[0]["code"] == "stub-auth-code-0123456789"


def test_the_values_can_come_from_the_shell_instead_of_prompts(google):
    stack = LoginStack()
    prompts = []
    L.run_login(stack, env={"GMAIL_CLIENT_ID": CLIENT_ID, "GMAIL_CLIENT_SECRET": CLIENT_SECRET}, input_fn=lambda p="": prompts.append(p) or "y",
                secret_fn=lambda p="": (_ for _ in ()).throw(AssertionError("the secret was asked for")), open_browser=browser(), out=lambda *_: None, wait_seconds=10)
    assert len(prompts) == 1 and "Use this account" in prompts[0] and len(stack.calls) == 4


def test_a_wrong_account_stops_before_anything_is_saved_and_says_the_grant_is_still_at_google(google):
    stack = LoginStack()
    for answer in ("n", "", "no", "maybe"):
        with pytest.raises(L.LoginError, match="not confirmed.*myaccount.google.com/permissions"):
            run(stack, answers=(answer,))
    assert stack.calls == []


@pytest.mark.parametrize("url", ["http://evil.example", "https://127.0.0.1:9", "http://127.0.0.1@evil.example/", "http://127.0.0.1.evil.example"])
def test_the_test_override_is_refused_unless_it_is_this_machine(monkeypatch, url):
    monkeypatch.setenv("GMAIL_API_URL", url)
    with pytest.raises(L.LoginError, match="GMAIL_API_URL"):
        L.api_base("oauth")
    with pytest.raises(L.LoginError, match="GMAIL_API_URL"):
        run(LoginStack())


def test_a_client_id_or_secret_with_a_trailing_newline_is_refused():
    assert L._CLIENT_ID.fullmatch(CLIENT_ID + "\n") is None and L._CLIENT_SECRET.fullmatch(CLIENT_SECRET + "\n") is None
    assert L._CLIENT_ID.fullmatch(CLIENT_ID) and L._CLIENT_SECRET.fullmatch(CLIENT_SECRET)


# ---------------------------------------------------------------- an answer that is not the right one

def test_an_answer_with_the_wrong_state_is_ignored_and_the_real_one_still_works(google):
    stack = LoginStack()
    ob = browser(extra_first=["/?code=stolen&state=wrong", "/favicon.ico", "/other?code=x"])
    run(stack, open_browser=ob)
    assert len(stack.calls) == 4 and google.exchanges[0]["code"] == "stub-auth-code-0123456789"


def test_an_answer_with_only_a_wrong_state_never_completes_the_login(google):
    with pytest.raises(L.LoginError, match="no answer"):
        run(LoginStack(), open_browser=browser(state="not-the-state"), wait_seconds=1)
    assert google.exchanges == []


def test_the_page_never_repeats_what_it_was_sent():
    receiver = L.Receiver("the-state")
    t = threading.Thread(target=lambda: receiver.wait(3), daemon=True)
    t.start()
    evil = "<script>alert(1)</script>"
    for query in (f"?state={urllib.parse.quote(evil)}", f"?state=the-state&error={urllib.parse.quote(evil)}"):
        try:
            body = urllib.request.urlopen(receiver.redirect_uri + query, timeout=5).read().decode()
        except urllib.error.HTTPError as e:
            body = e.read().decode()
        assert "<script>" not in body and "alert" not in body
        if "error" in query:
            break
    t.join(5)
    assert receiver.result == {"error": "<script>alert(1)</script>"[:60]}, "only the answer, and it is shortened and never echoed"


def test_the_receiver_listens_on_the_loopback_address_only():
    receiver = L.Receiver("s")
    try:
        assert receiver.httpd.server_address[0] == "127.0.0.1"
    finally:
        receiver.httpd.server_close()


def test_only_the_first_valid_answer_counts():
    receiver = L.Receiver("s")
    done = threading.Thread(target=lambda: receiver.wait(2), daemon=True)
    done.start()
    first = urllib.request.urlopen(receiver.redirect_uri + "?state=s&code=first", timeout=5).read().decode()
    assert "Approved" in first
    done.join(5)
    assert not done.is_alive(), "the receiver stops as soon as it has the answer"
    with pytest.raises((urllib.error.URLError, OSError)):
        urllib.request.urlopen(receiver.redirect_uri + "?state=s&code=second", timeout=2)      # nothing listens any more
    assert receiver.result == {"code": "first"}


def test_a_silent_connection_cannot_hold_the_receiver_past_the_real_answer(monkeypatch):
    """A local process (or a browser's speculative pre-connection) that connects and sends nothing must not block the single-threaded
    server: it is dropped after HANDLER_TIMEOUT and the real redirect is then served."""
    import socket
    monkeypatch.setattr(L, "HANDLER_TIMEOUT", 0.5)
    receiver = L.Receiver("s")
    port = receiver.httpd.server_address[1]
    result = {}
    waiter = threading.Thread(target=lambda: result.update(receiver.wait(10)), daemon=True)
    waiter.start()
    idle = socket.create_connection(("127.0.0.1", port))                   # connects, says nothing
    try:
        started = time.monotonic()
        body = urllib.request.urlopen(receiver.redirect_uri + "?state=s&code=real", timeout=8).read().decode()
        assert "Approved" in body and time.monotonic() - started < 5
    finally:
        idle.close()
    waiter.join(5)
    assert result == {"code": "real"}


def test_a_state_with_non_ascii_letters_is_refused_cleanly_without_a_traceback(capfd):
    receiver = L.Receiver("the-state")
    waiter = threading.Thread(target=lambda: receiver.wait(3), daemon=True)
    waiter.start()
    for state in ("%C3%A9", "%E2%80%AE", "%ff", "a%00b"):
        try:
            urllib.request.urlopen(receiver.redirect_uri + f"?state={state}&code=x", timeout=5).read()
            raise AssertionError("an answer with the wrong state was accepted")
        except urllib.error.HTTPError as e:
            assert e.code == 400
            e.read()
    urllib.request.urlopen(receiver.redirect_uri + "?state=the-state&code=ok", timeout=5).read()
    waiter.join(5)
    assert receiver.result == {"code": "ok"}
    assert "Traceback" not in capfd.readouterr().err


def test_not_approving_is_reported_and_nothing_is_saved(google):
    stack = LoginStack()
    with pytest.raises(L.LoginError, match="did not approve.*access_denied"):
        run(stack, open_browser=browser(error="access_denied"))
    assert stack.calls == [] and google.exchanges == []


def test_no_answer_in_time_is_an_error(google):
    with pytest.raises(L.LoginError, match="no answer from the browser"):
        run(LoginStack(), open_browser=lambda url: True, wait_seconds=1)


def test_a_browser_that_cannot_be_opened_is_not_an_error_because_the_link_is_printed(google):
    ob = browser()

    def open_then_fail(url):
        ob(url)                                               # stands for the person opening the printed link themselves
        raise RuntimeError("no browser")
    result, said, _ = run(LoginStack(), open_browser=open_then_fail)
    assert "https://accounts.google.com/o/oauth2/v2/auth?" in said and "Saved" in result


def test_no_browser_prints_the_link_and_does_not_try_to_open_one(google):
    opened, out = [], []
    with pytest.raises(L.LoginError, match="no answer"):
        L.run_login(LoginStack(), env={}, input_fn=first_prompt_only(), secret_fn=lambda p="": CLIENT_SECRET,
                    open_browser=lambda url: opened.append(url), out=out.append, wait_seconds=1, no_browser=True)
    assert opened == [] and any("https://accounts.google.com/o/oauth2/v2/auth?" in line for line in out)


# ---------------------------------------------------------------- what Google may answer

def test_a_login_that_is_more_than_read_only_is_refused_and_nothing_is_saved(google):
    google.modes["auth"] = "overbroad"
    stack = LoginStack()
    with pytest.raises(L.LoginError, match="more than read-only"):
        run(stack)
    assert stack.calls == []


def test_a_missing_refresh_token_says_how_to_get_one_and_makes_no_false_claim(google):
    google.modes["auth"] = "no_refresh"
    stack = LoginStack()
    with pytest.raises(L.LoginError, match="refresh token.*myaccount.google.com/permissions") as e:
        run(stack)
    assert "first time" not in str(e.value) and "Desktop app" in str(e.value)
    assert stack.calls == []


@pytest.mark.parametrize("token", ["1/0AbCdEfGhIjKlMnOpQrStUvWxYz", "1//0gAbCdEf.GhIjKl~MnOpQr+StUvWxYz-_", "1//" + "a" * 200])
def test_other_real_refresh_token_shapes_are_accepted(google, token):
    google.refresh_token = token
    stack = LoginStack()
    run(stack)
    assert stack.calls[-1][1] == token + "\n"


@pytest.mark.parametrize("token", ["short", "has a space in the middle 1234567890", "1//abc\"def1234567890123", "1//abc'def1234567890123", "1//abc\\def1234567890123", "1//a$b1234567890123456"])
def test_a_refresh_token_that_could_not_be_stored_safely_is_refused(google, token):
    google.refresh_token = token
    stack = LoginStack()
    with pytest.raises(L.LoginError, match="refresh token"):
        run(stack)
    assert stack.calls == []


def test_an_expired_code_and_a_wrong_client_are_explained(google):
    google.modes["auth"] = "invalid_grant"
    with pytest.raises(L.LoginError, match="refused the sign-in code"):
        run(LoginStack())
    google.modes.clear()
    google.client_secret = "GOCSPX-someone-elses-secret"
    with pytest.raises(L.LoginError, match="does not recognise the client id or secret"):
        run(LoginStack())


def test_a_redirect_from_the_token_endpoint_is_never_followed(google):
    google.modes["token"] = "redirect"
    with pytest.raises(L.LoginError, match="refused the sign-in"):
        run(LoginStack())


@pytest.mark.parametrize("client_id", ["", "not-a-client-id", "12345-abc.apps.googleusercontent.com", "123456789012-ABCDEFGHIJKLMN.apps.googleusercontent.com",
                                        "123456789012-abcdefghijkl.apps.evil.example", "https://evil.example"])
def test_a_client_id_that_is_not_one_is_refused_before_any_network(google, client_id):
    prompts = iter([client_id])
    with pytest.raises(L.LoginError, match="client id"):
        L.run_login(LoginStack(), env={}, input_fn=lambda p="": next(prompts), secret_fn=lambda p="": CLIENT_SECRET, open_browser=browser(), out=lambda *_: None)
    assert google.exchanges == [] and google.token_requests == []


@pytest.mark.parametrize("secret", ["", "short", "has a space in it", "x" * 200, "semi;colon-secret-value"])
def test_a_client_secret_that_is_not_one_is_refused(google, secret):
    with pytest.raises(L.LoginError, match="client secret"):
        L.run_login(LoginStack(), env={}, input_fn=first_prompt_only(), secret_fn=lambda p="": secret, open_browser=browser(), out=lambda *_: None)


# ---------------------------------------------------------------- saving

def test_a_failure_while_saving_names_what_was_saved_and_never_a_value(google):
    stack = LoginStack(fail={"GMAIL_REFRESH_TOKEN": "GMAIL_REFRESH_TOKEN is not a line in .env."})
    with pytest.raises(L.LoginError) as e:
        run(stack)
    assert "could not save GMAIL_REFRESH_TOKEN (GMAIL_CLIENT_ID, GMAIL_CLIENT_SECRET already saved)" in str(e.value)
    assert not any(v in str(e.value) for v in (CLIENT_ID, CLIENT_SECRET, REFRESH))


def test_a_failure_preparing_env_stops_before_any_value_is_sent(google):
    stack = LoginStack(fail={"--add-gmail": "No .env yet: run scripts/init-env.sh first."})
    with pytest.raises(L.LoginError, match="could not prepare .env"):
        run(stack)
    assert len(stack.calls) == 1


def test_restart_recreates_the_scraper_and_otherwise_it_only_says_how(google):
    stack = LoginStack()
    result, _, _ = run(stack, restart=True)
    assert stack.composed == [["up", "-d", "scraper"]] and "was restarted" in result and "docker compose up -d scraper" not in result
    stack = LoginStack()
    result, _, _ = run(stack)
    assert stack.composed == [] and "docker compose up -d scraper" in result and "--restart" in result


def test_the_summary_says_where_the_values_went(google):
    assert "on this computer" in run(LoginStack())[0]
    assert "on the server" in run(LoginStack(ssh=["ssh", "host"]))[0]


# ---------------------------------------------------------------- the real scripts, in a temporary repository

def test_the_values_really_land_in_env_through_the_real_scripts_and_nowhere_else(google, tmp_path):
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    for name in ("init-env.sh", "set-env.sh"):
        shutil.copy(REPO / "scripts" / name, repo / "scripts" / name)
    (repo / ".env").write_text("POSTGRES_PASSWORD=x\nANTHROPIC_API_KEY=\n")
    stack = C.Stack(env={}, repo=repo)
    result, said, _ = run(stack)
    values = dict(line.split("=", 1) for line in (repo / ".env").read_text().splitlines() if "=" in line)
    assert values["GMAIL_CLIENT_ID"] == CLIENT_ID and values["GMAIL_CLIENT_SECRET"] == CLIENT_SECRET and values["GMAIL_REFRESH_TOKEN"] == REFRESH
    assert values["POSTGRES_PASSWORD"] == "x", "nothing else changed"
    assert (repo / ".env").stat().st_mode & 0o777 == 0o600 and not list(repo.glob(".env.*")), "set-env.sh writes under umask 077"
    for secret in (CLIENT_SECRET, REFRESH):                    # the client id is in the consent link by necessity
        assert secret not in all_text(result, said)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["repo"], "nothing was written anywhere else"


# ---------------------------------------------------------------- the command and the stack method

def test_the_stack_runs_a_repo_script_locally_or_over_ssh_with_the_value_on_standard_input(monkeypatch):
    seen = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: seen.append((cmd, kw)) or SimpleNamespace(returncode=0, stdout="", stderr=""))
    C.Stack(env={}, repo=Path("/r")).run_repo_script(["scripts/set-env.sh", "GMAIL_CLIENT_ID"], input_text="value\n")
    assert seen[-1][0] == ["sh", "scripts/set-env.sh", "GMAIL_CLIENT_ID"] and seen[-1][1]["input"] == "value\n" and seen[-1][1]["cwd"] == Path("/r")
    C.Stack(env={"JOBSEARCH_SSH": "ssh -i /k ubuntu@host", "JOBSEARCH_REMOTE_DIR": "~/jobsearch"}).run_repo_script(["scripts/set-env.sh", "GMAIL_CLIENT_ID"], input_text="value\n")
    cmd, kw = seen[-1]
    assert cmd == ["ssh", "-i", "/k", "ubuntu@host", "cd ~/jobsearch && sh scripts/set-env.sh GMAIL_CLIENT_ID"] and kw["input"] == "value\n"
    assert "value" not in " ".join(cmd)


def test_main_runs_the_login_and_turns_a_failure_into_a_message(google, capsys):
    stack = LoginStack()
    prompts = iter([CLIENT_ID, "y"])
    orig = L.run_login
    try:
        L.run_login = lambda st, **kw: orig(st, env={}, input_fn=lambda p="": next(prompts), secret_fn=lambda p="": CLIENT_SECRET, open_browser=browser(),
                                            out=lambda *_: None, wait_seconds=10, **{k: v for k, v in kw.items() if k in ("no_browser", "restart")})
        assert C.main(["gmail", "login", "--restart"], stack=stack) == 0
        assert "Saved GMAIL_CLIENT_ID" in capsys.readouterr().out and stack.composed == [["up", "-d", "scraper"]]
        google.modes["auth"] = "overbroad"
        prompts = iter([CLIENT_ID, "y"])
        assert C.main(["gmail", "login"], stack=LoginStack()) == 2
        assert "more than read-only" in capsys.readouterr().err
    finally:
        L.run_login = orig


def test_the_login_flags_belong_to_login(capsys):
    for flag in ("--no-browser", "--restart"):
        assert C.main(["gmail", "status", flag], stack=LoginStack()) == 2
        assert "belong to gmail login" in capsys.readouterr().err


# ---------------------------------------------------------------- drift guards and imports

def test_the_constants_match_the_scrapers():
    import features
    import gmail_client as G
    assert L.SCOPE == G.SCOPE and L._HOSTS == G._HOSTS and L.VALUE_NAMES == features.GMAIL_VARS


def test_pkce_pairs_are_valid_and_never_repeat():
    seen = set()
    for _ in range(20):
        verifier, challenge = L.pkce_pair()
        assert 43 <= len(verifier) <= 128 and re.fullmatch(r"[A-Za-z0-9_-]+", verifier)
        assert challenge == base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        seen.add(verifier)
    assert len(seen) == 20


def test_the_command_line_tool_loads_with_only_the_standard_library():
    code = ("import sys; sys.modules['httpx'] = None; sys.modules['psycopg'] = None; sys.modules['fastapi'] = None\n"
            f"sys.path.insert(0, {str(REPO / 'cli')!r}); import jobsearch_cli, gmail_login; print('loaded')")
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "loaded" in r.stdout, r.stderr
