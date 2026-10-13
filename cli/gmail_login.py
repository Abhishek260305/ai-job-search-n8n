"""The one-time Google sign-in for the optional Gmail part (Phase 6, step 6b): `jobsearch gmail login` (docs/gmail.md).

Standard library only, run on YOUR computer (the one with a browser), whether the stack runs there or on a server:

  1. It asks for the client id and secret of the OAuth client you created in the Google Cloud console (the secret is typed
     hidden; or they are read from GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET in your shell).
  2. It starts a tiny web server on 127.0.0.1 only, on a random port, opens Google's consent page and waits for the answer.
     The request carries a random `state` (an answer for anything else is ignored) and a PKCE challenge (so a stolen code is
     useless), asks for exactly one permission (read-only Gmail), and for offline access (the refresh token).
  3. It trades the code for tokens. It REFUSES the result unless the permission is exactly read-only, and unless there is a
     refresh token.
  4. It shows which account signed in, masked, and asks you to confirm. A wrong account stops here and nothing is saved.
  5. It saves the three values into `.env` on the machine that runs the stack, by feeding each one to scripts/set-env.sh on
     standard input. A value is never an argument of any command, never printed, never written to any file but that .env,
     and never sent anywhere but Google and that .env.

Nothing is installed or changed on the stack beyond those `.env` lines (scripts/init-env.sh --add-gmail adds the blank lines
first when an older .env lacks them). It prints what to do next instead of restarting anything, unless you pass --restart.
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable, Mapping, Optional

SCOPE = "https://www.googleapis.com/auth/gmail.readonly"       # tests check this equals scraper/gmail_client.SCOPE
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_HOSTS = {"oauth": "https://oauth2.googleapis.com", "gmail": "https://gmail.googleapis.com"}
WAIT_SECONDS = 300
HANDLER_TIMEOUT = 5                  # seconds a connection may sit silent before the receiver drops it (tests shorten it)
MAX_BYTES = 1_000_000
VALUE_NAMES = ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN")

# used with fullmatch (`$` would accept a trailing newline). The refresh token's alphabet is wide on purpose: a character set that was
# too tight would refuse a real token with a misleading message; set-env.sh refuses anything unsafe for .env anyway.
_CLIENT_ID = re.compile(r"[0-9]{6,}-[0-9a-z]{10,}\.apps\.googleusercontent\.com")
_CLIENT_SECRET = re.compile(r"[A-Za-z0-9_-]{8,128}")
_REFRESH_TOKEN = re.compile(r"[A-Za-z0-9._~+/-]{20,2048}")


class LoginError(Exception):
    """Something went wrong that is explained in one line a person can act on. Never contains a credential."""


def api_base(name: str) -> str:
    """The Google host for `oauth` or `gmail`. GMAIL_API_URL (tests only, like the scraper's) replaces both, and only when it is
    a loopback address: the client secret, the code and the tokens go to whatever this returns."""
    override = os.environ.get("GMAIL_API_URL", "").strip()
    if not override:
        return _HOSTS[name]
    parts = urllib.parse.urlsplit(override)
    if parts.scheme != "http" or parts.hostname not in ("127.0.0.1", "localhost", "::1") or any(c in parts.netloc for c in "@\\"):
        raise LoginError("GMAIL_API_URL must be a http://127.0.0.1 or http://localhost address (it is for tests only): unset it")
    return f"{override.rstrip('/')}/{name}"


# ---- the request to Google's consent page ---------------------------------------------------------------------

def pkce_pair() -> tuple[str, str]:
    """(verifier, S256 challenge)."""
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
    return verifier, challenge


def auth_url(client_id: str, redirect_uri: str, state: str, challenge: str) -> str:
    return AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect_uri, "response_type": "code", "scope": SCOPE,
        "access_type": "offline", "prompt": "consent", "include_granted_scopes": "false",
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})


# ---- the answer, received on this computer only ---------------------------------------------------------------

_PAGE = ("<!doctype html><meta charset=utf-8><title>jobsearch</title><body style='font:16px system-ui;margin:3em'>"
         "<h2>{title}</h2><p>{body}</p></body>")


class Receiver:
    """A one-shot web server on 127.0.0.1 (never any other address), on a random port. It accepts the first answer that carries
    the expected `state`; an answer without it, or for another path, is refused and ignored. It prints nothing and echoes nothing
    it received."""

    def __init__(self, state: str):
        receiver = self

        class Handler(http.server.BaseHTTPRequestHandler):
            timeout = HANDLER_TIMEOUT        # a connection that sends nothing must not hold the (single-threaded) server past its deadline

            def log_message(self, *a):
                pass

            def _send(self, status: int, title: str, body: str) -> None:
                page = _PAGE.format(title=title, body=body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(page)

            def do_GET(self):
                parts = urllib.parse.urlsplit(self.path)
                if parts.path != "/":
                    return self._send(404, "Not found", "Nothing here.")
                query = {k: v[0] for k, v in urllib.parse.parse_qs(parts.query).items()}
                if not secrets.compare_digest(query.get("state", "").encode("utf-8", "replace"), receiver.state.encode("utf-8")):
                    return self._send(400, "Not accepted", "This answer was not for the sign-in in progress. Nothing was changed.")
                if receiver.result is not None:
                    return self._send(409, "Already received", "The sign-in already has its answer. You can close this tab.")
                if query.get("error"):
                    receiver.result = {"error": query["error"][:60]}
                    return self._send(200, "Not approved", "Nothing was changed. You can close this tab and return to the terminal.")
                if query.get("code"):
                    receiver.result = {"code": query["code"][:2048]}
                    return self._send(200, "Approved", "You can close this tab and return to the terminal.")
                self._send(400, "Not accepted", "No answer in this request.")

        self.state = state
        self.result: Optional[dict] = None
        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.timeout = 0.25

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/"

    def wait(self, seconds: float) -> dict:
        deadline = time.monotonic() + seconds
        try:
            while self.result is None and time.monotonic() < deadline:
                self.httpd.handle_request()
        finally:
            self.httpd.server_close()
        if self.result is None:
            raise LoginError(f"no answer from the browser within {int(seconds)} seconds: run the command again")
        return self.result


# ---- talking to Google (fixed hosts, no redirects, bounded) ---------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _call(request: urllib.request.Request, opener=None) -> tuple[int, dict]:
    try:
        resp = (opener or _OPENER).open(request, timeout=30)
        status, raw = resp.status, resp.read(MAX_BYTES + 1)
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read(MAX_BYTES + 1)
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise LoginError(f"could not reach Google ({type(e).__name__}): check your internet connection") from None
    if len(raw) > MAX_BYTES:
        raise LoginError("Google's answer was unexpectedly large: refused")
    try:
        data = json.loads(raw)
    except ValueError:
        data = {}
    return status, data if isinstance(data, dict) else {}


def exchange_code(client_id: str, client_secret: str, code: str, verifier: str, redirect_uri: str, *, opener=None) -> dict:
    """Trade the code for tokens and refuse anything that is not exactly read-only with a refresh token.
    -> {"access_token", "refresh_token"}."""
    body = urllib.parse.urlencode({"client_id": client_id, "client_secret": client_secret, "code": code, "code_verifier": verifier,
                                   "redirect_uri": redirect_uri, "grant_type": "authorization_code"}).encode()
    request = urllib.request.Request(api_base("oauth") + "/token", data=body, method="POST",
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
    status, data = _call(request, opener)
    if status != 200:
        error = data.get("error") if isinstance(data.get("error"), str) else ""
        if error == "invalid_client":
            raise LoginError("Google does not recognise the client id or secret: check them in the Google Cloud console")
        if error == "invalid_grant":
            raise LoginError("Google refused the sign-in code (it was used already or took too long): run the command again")
        if error == "redirect_uri_mismatch":
            raise LoginError("the OAuth client must be of type 'Desktop app': create a Desktop client in the Google Cloud console")
        raise LoginError(f"Google refused the sign-in (HTTP {status}{', ' + re.sub(r'[^a-z_]', '', error)[:40] if error else ''})")
    scope = data.get("scope")
    scopes = set(scope.split()) if isinstance(scope, str) else set()
    if not scopes or not scopes <= {SCOPE}:
        raise LoginError("Google granted more than read-only Gmail access, or did not say what it granted: nothing was saved. "
                         "Run the command again and approve only 'View your email messages and settings'")
    refresh = data.get("refresh_token")
    if not isinstance(refresh, str) or not _REFRESH_TOKEN.fullmatch(refresh):
        raise LoginError("Google did not give a refresh token. Remove this app at https://myaccount.google.com/permissions, then run the "
                         "command again (and make sure the OAuth client is of type 'Desktop app')")
    access = data.get("access_token")
    if not isinstance(access, str) or not access or re.search(r"\s", access):
        raise LoginError("Google's answer had no usable access token")
    return {"access_token": access, "refresh_token": refresh}


def account_address(access_token: str, *, opener=None) -> str:
    request = urllib.request.Request(api_base("gmail") + "/gmail/v1/users/me/profile", headers={"Authorization": f"Bearer {access_token}"})
    status, data = _call(request, opener)
    address = data.get("emailAddress")
    if status != 200 or not isinstance(address, str) or address.count("@") != 1:
        raise LoginError("signed in, but Gmail would not say which account this is: nothing was saved")
    return address


def mask(address: str) -> str:
    local, _, domain = address.partition("@")
    return f"{local[:1]}***@{domain.lower()}" if local and domain else "(unknown)"


# ---- saving the three values ----------------------------------------------------------------------------------

def store_values(stack, values: Mapping[str, str]) -> None:
    """Write each value into .env on the machine that runs the stack, through standard input. The names are fixed, the values
    are never arguments, and a failure names the value that was not saved (never its content)."""
    r = stack.run_repo_script(["scripts/init-env.sh", "--add-gmail"])
    if r.returncode != 0:
        raise LoginError("could not prepare .env: " + (r.stderr or r.stdout or "failed").strip()[:200])
    saved = []
    for name in VALUE_NAMES:
        r = stack.run_repo_script(["scripts/set-env.sh", name], input_text=values[name] + "\n")
        if r.returncode != 0:
            done = f" ({', '.join(saved)} already saved)" if saved else ""
            raise LoginError(f"could not save {name}{done}: " + (r.stderr or r.stdout or "failed").strip()[:200])
        saved.append(name)


# ---- the whole flow -------------------------------------------------------------------------------------------

def run_login(stack, *, env: Optional[Mapping[str, str]] = None, input_fn: Callable = input, secret_fn: Optional[Callable] = None,
              open_browser: Optional[Callable] = None, out: Callable = print, wait_seconds: float = WAIT_SECONDS,
              no_browser: bool = False, restart: bool = False, opener=None) -> str:
    import getpass
    import webbrowser
    env = os.environ if env is None else env
    secret_fn = secret_fn or getpass.getpass
    open_browser = open_browser or webbrowser.open

    client_id = (env.get("GMAIL_CLIENT_ID") or "").strip() or input_fn("Google client id (ends with .apps.googleusercontent.com): ").strip()
    if not _CLIENT_ID.fullmatch(client_id):
        raise LoginError("that does not look like a Google client id (digits, a dash, letters and digits, then .apps.googleusercontent.com)")
    client_secret = (env.get("GMAIL_CLIENT_SECRET") or "").strip() or secret_fn("Google client secret (typing is hidden): ").strip()
    if not _CLIENT_SECRET.fullmatch(client_secret):
        raise LoginError("that does not look like a Google client secret (letters, digits, '-' and '_' only, no spaces)")

    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(24)
    receiver = Receiver(state)
    url = auth_url(client_id, receiver.redirect_uri, state, challenge)
    try:
        out("Waiting for you to approve in the browser (up to %d minutes). Approve only the read-only permission." % (wait_seconds // 60 or 1))
        out("If no browser window opened, open this link yourself:\n\n  " + url + "\n")
        if not no_browser:
            try:
                open_browser(url)
            except Exception:                                      # a missing browser is not an error: the link is printed above
                pass
        answer = receiver.wait(wait_seconds)
    except BaseException:
        receiver.httpd.server_close()
        raise
    if "error" in answer:
        raise LoginError("you did not approve (Google said: %s): nothing was saved" % re.sub(r"[^a-z_]", "", answer["error"])[:40])

    tokens = exchange_code(client_id, client_secret, answer["code"], verifier, receiver.redirect_uri, opener=opener)
    address = account_address(tokens["access_token"], opener=opener)
    out(f"Signed in as {mask(address)}.")
    if input_fn("Use this account? [y/N] ").strip().lower() not in ("y", "yes"):
        raise LoginError("not confirmed: nothing was saved here. Google still lists this sign-in as approved: remove the app at "
                         "https://myaccount.google.com/permissions if you do not want it")

    store_values(stack, {"GMAIL_CLIENT_ID": client_id, "GMAIL_CLIENT_SECRET": client_secret, "GMAIL_REFRESH_TOKEN": tokens["refresh_token"]})
    where = "the server" if getattr(stack, "ssh", None) else "this computer"
    lines = [f"Saved {', '.join(VALUE_NAMES)} in .env on {where} (values not shown).", ""]
    if restart:
        r = stack.compose(["up", "-d", "scraper"], timeout=300)
        if r.returncode != 0:
            raise LoginError("saved, but the scraper could not be restarted: " + (r.stderr or r.stdout or "failed").strip()[:200])
        lines.append("The scraper was restarted, so it sees the login.")
        lines += ["Next:", "  1. Check the login:                  scripts/jobsearch gmail check",
                  "  2. Switch the part on when you want it:  scripts/jobsearch features set --gmail on"]
    else:
        lines += ["Next:", "  1. Restart the scraper so it sees them (on the machine that runs the stack):  docker compose up -d scraper",
                  "     (or run this command again with --restart)",
                  "  2. Check the login:                  scripts/jobsearch gmail check",
                  "  3. Switch the part on when you want it:  scripts/jobsearch features set --gmail on"]
    return "\n".join(lines)
