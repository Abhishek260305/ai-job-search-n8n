"""A small, bounded, read-only Gmail client (Phase 6, step 6b; docs/gmail.md).

It does exactly three things: trade the saved refresh token for a short-lived access token, list message ids for a
search, and read one message. Everything about it is built to fail closed:

  * Two fixed hosts (oauth2.googleapis.com and gmail.googleapis.com), compared as parsed origins, never as string
    prefixes. GMAIL_API_URL (tests and rehearsals only, like BOARDS_API_URL; never set it in production) replaces both
    with `{GMAIL_API_URL}/oauth` and `{GMAIL_API_URL}/gmail`, where scraper/stubs.py StubGmail listens.
  * Read-only, and checked: if Google's answer says the login carries any permission beyond `gmail.readonly`, the token
    is refused before it is used. This code has no call that sends, deletes, labels or marks anything.
  * Bounded: every response is streamed and size-limited, every request has a deadline, redirects are never followed,
    a 429 is never retried, a 5xx or a timeout is retried exactly once.
  * Quiet: neither the token, the secret, a search query nor an email's text is ever logged. `redact()` removes
    anything that looks like a Google credential from text that may be stored or shown (an error message).
  * An email is untrusted data. `parse_message` returns plain text capped in size, with invisible characters removed;
    the sender is the address as written (never "verified"), and the only authenticity signal it offers is the
    Authentication-Results header added by Google's own server (a header the sender wrote is ignored).

Pure functions plus one class that makes HTTP calls through an httpx client it is given (tests give it the stand-in
server; production gives it a real one).
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any, Mapping, Optional

from urllib.parse import urlsplit

import httpx

import boards          # the origin check (urllib and httpx must agree on the host)
import email_html      # the text a person sees in an HTML email (hidden elements dropped), and whether it could be read cleanly

log = logging.getLogger("scraper.gmail")
# httpx logs every request URL at INFO, and a Gmail search (company names, from the job search) travels in the URL: keep it,
# and the transport's header traces, out of the logs whoever configured logging first. (The service does the same in app.py.)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
ALLOWED_SCOPES = frozenset({SCOPE})
USER_AGENT = "ai-job-search-n8n/1.0 (personal job search)"

_HOSTS = {"oauth": "https://oauth2.googleapis.com", "gmail": "https://gmail.googleapis.com"}
TOKEN_PATH = "/token"
API_ROOT = "/gmail/v1/users/me"

MAX_RESPONSE_BYTES = 5_000_000        # one message with its text parts; a bigger one is refused, not read
MAX_SMALL_BYTES = 1_000_000           # token, profile and list replies
TIMEOUT_S = 20.0                      # per network read
REQUEST_DEADLINE_S = 40.0             # the whole request: a server that drips bytes cannot hold a run longer
RETRY_DELAY_S = 1.0                   # before the single retry of a 5xx or a timeout; tests set it to 0
MAX_LIST = 100
MAX_QUERY_CHARS = 1000
EXPIRY_MARGIN_S = 60                  # an access token is renewed this long before it ends

MAX_HEADERS = 200
MAX_HEADER_CHARS = 2000
MAX_PARTS = 200
MAX_DEPTH = 8
MAX_PART_BYTES = 400_000
MAX_TEXT_CHARS = 20_000
MAX_SUBJECT_CHARS = 300
MAX_LINKS = 50

_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")                          # always used with fullmatch: `$` would accept a trailing newline
_CONTROL = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029]")     # C0 and C1 controls and the Unicode line separators
# Characters that render as nothing (or reorder text), so they can hide instructions from a reader while a model still sees them:
# zero-width and bidirectional marks and isolates, the soft hyphen, the Arabic letter mark, the Mongolian vowel separator,
# variation selectors, the byte-order mark, and the Unicode "tag" block (the "ASCII smuggling" channel).
_INVISIBLE = re.compile("[\u00ad\u061c\u180e\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufe00-\ufe0f\ufeff\U000e0000-\U000e007f]")
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_DOMAIN = re.compile(rf"(?:{_LABEL}\.)+{_LABEL}")             # at least two labels, each a valid hostname label (punycode included); used with fullmatch


def valid_domain(text: object) -> bool:
    return isinstance(text, str) and len(text) <= 253 and text.isascii() and bool(_DOMAIN.fullmatch(text))


# ---- errors ---------------------------------------------------------------------------------------------------

class GmailError(Exception):
    """A Gmail or Google problem, explained in one line a person can act on. Never contains a credential."""


class NotConfigured(GmailError):
    """The three login values are not all in the environment."""


class LoginExpired(GmailError):
    """Google no longer accepts the saved login (expired, revoked, or the client was deleted): sign in again."""


class Overbroad(GmailError):
    """The login carries more permission than read-only. It is refused and never used."""


class RateLimited(GmailError):
    """HTTP 429 (or a rate-limit 403). Stop for this run; never retried."""

    def __init__(self, message: str, retry_after: Optional[int] = None):
        super().__init__(message)
        self.retry_after = retry_after


class Unavailable(GmailError):
    """Google did not answer (HTTP 5xx, a timeout, a connection error), even after the one retry. Says nothing about the login."""


class MessageGone(GmailError):
    """HTTP 404 for one message: it was deleted since the search."""


class TooLarge(GmailError):
    """One message is larger than MAX_RESPONSE_BYTES. It is skipped, not read."""


class _Transient(Exception):
    """Internal: a 5xx or a network failure, worth one retry."""


# ---- hosts ----------------------------------------------------------------------------------------------------

def _loopback(url: str) -> bool:
    try:
        parts = urlsplit(url)
        return parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost", "::1") and not any(c in parts.netloc for c in "@\\")
    except ValueError:
        return False


def base_url(name: str) -> str:
    """The Google host for `oauth` or `gmail`. GMAIL_API_URL (tests only) replaces both, and is honoured ONLY when it points at this
    machine: the client secret, the refresh token and every access token are sent to whatever this returns."""
    override = os.environ.get("GMAIL_API_URL", "").strip()
    if not override:
        return _HOSTS[name]
    if not _loopback(override):
        raise GmailError("GMAIL_API_URL must be a http://127.0.0.1 or http://localhost address (it is for tests only): unset it")
    return f"{override.rstrip('/')}/{name}"


def _known_origins() -> set:
    """Read at call time so a GMAIL_API_URL test override is honoured; parsed origins, never string prefixes."""
    origins = {boards._origin(base_url(n)) for n in _HOSTS}
    origins.discard(None)
    return origins


# ---- credentials never leave in text --------------------------------------------------------------------------

_SECRETS = (
    re.compile(r"ya29\.[0-9A-Za-z_-]{10,}"),                                  # an access token
    re.compile(r"1/{1,2}[0-9A-Za-z_-]{10,}"),                                 # a refresh token (the long and the old shape)
    re.compile(r"GOCSPX-[0-9A-Za-z_-]{6,}"),                                  # a client secret
    re.compile(r"[0-9]{6,}-[0-9a-z]{10,}\.apps\.googleusercontent\.com"),     # a client id
    re.compile(r"(?i)\bbearer\s+[0-9A-Za-z._~+/=-]{8,}"),
    re.compile(r"(?i)\b(client_secret|refresh_token|access_token|code)=[^\s&\"']{3,}"),
)


def redact(text: object, limit: int = 300) -> str:
    """`text` with anything that looks like a Google credential replaced, on one line, at most `limit` characters.
    For error messages that are stored (gmail_state.last_error) or shown (CLI, dashboard)."""
    out = _CONTROL.sub(" ", str(text))
    for pattern in _SECRETS:
        out = pattern.sub("[removed]", out)
    out = " ".join(out.split())
    return out if len(out) <= limit else out[: limit - 1] + "…"


def mask_address(address: object) -> str:
    """'jane.doe@gmail.com' -> 'j***@gmail.com': enough to see which account signed in, not enough to publish it."""
    if not isinstance(address, str) or address.count("@") != 1:
        return "(unknown)"
    local, domain = address.strip().split("@")
    if not local or not valid_domain(domain.lower()):
        return "(unknown)"
    return f"{local[0]}***@{domain.lower()}"


# ---- the bounded request --------------------------------------------------------------------------------------

def _once(client: httpx.Client, method: str, url: str, headers: dict, data: Optional[dict], max_bytes: int) -> tuple[int, bytes, Mapping]:
    started = time.monotonic()
    try:
        with client.stream(method, url, headers={"User-Agent": USER_AGENT, **headers}, data=data, follow_redirects=False) as resp:
            status = resp.status_code
            if 300 <= status < 400:
                raise GmailError(f"unexpected redirect (HTTP {status}): refused")
            if status >= 500:
                raise _Transient(f"Google answered HTTP {status}")
            too_big = TooLarge(f"a response is larger than {max_bytes} bytes")
            try:
                if int(resp.headers.get("content-length", "")) > max_bytes:
                    raise too_big
            except ValueError:
                pass
            body = bytearray()
            for chunk in resp.iter_bytes():
                if len(body) + len(chunk) > max_bytes:
                    raise too_big
                body.extend(chunk)
                if time.monotonic() - started > REQUEST_DEADLINE_S:
                    raise Unavailable("Google took too long to answer")
            return status, bytes(body), resp.headers
    except httpx.TimeoutException:
        raise _Transient("Google did not answer in time")
    except (httpx.ConnectError, httpx.ReadError) as e:
        raise _Transient(f"network error ({type(e).__name__})")
    except httpx.HTTPError as e:
        raise Unavailable(f"network error ({type(e).__name__})")


def _request(client: httpx.Client, method: str, url: str, *, headers: Optional[dict] = None, data: Optional[dict] = None,
             max_bytes: int = MAX_SMALL_BYTES) -> tuple[int, bytes, Mapping]:
    """One bounded request to a Google host: (status, body, headers) for any status below 500. A URL whose origin is not one
    of the two Google hosts is refused before anything is sent. HTTP 429 raises RateLimited at once; a 5xx, a timeout or a
    connection error is retried exactly once."""
    origin = boards._origin(url)
    if origin is None or origin not in _known_origins():
        raise GmailError("refused: not a Google host")
    try:
        status, body, resp_headers = _once(client, method, url, headers or {}, data, max_bytes)
    except _Transient as first:
        log.info("retrying once: %s", first)
        time.sleep(RETRY_DELAY_S)
        try:
            status, body, resp_headers = _once(client, method, url, headers or {}, data, max_bytes)
        except _Transient as second:
            raise Unavailable(f"{second} (tried twice)") from None
    if status == 429:
        raise RateLimited("Google is limiting requests (HTTP 429)", _retry_after(resp_headers))
    return status, body, resp_headers


def _retry_after(headers: Mapping) -> Optional[int]:
    try:
        return max(0, min(int(headers.get("retry-after", "")), 86400))
    except (ValueError, TypeError):
        return None


def _json(body: bytes) -> Any:
    try:
        return json.loads(body)
    except ValueError:
        raise GmailError("Google returned something that is not JSON") from None


def _error_info(body: bytes) -> tuple[str, str]:
    """(error code, reason) from a Google error body, each reduced to a short safe token; ('', '') when unreadable."""
    try:
        data = json.loads(body)
    except ValueError:
        return "", ""
    code = reason = ""
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, str):
            code = err
        elif isinstance(err, dict):
            first = (err.get("errors") or [{}])[0] if isinstance(err.get("errors"), list) and err.get("errors") else {}
            reason = first.get("reason", "") if isinstance(first, dict) else ""
            code = err.get("status", "") if isinstance(err.get("status"), str) else ""
    clean = lambda s: s if isinstance(s, str) and re.fullmatch(r"[A-Za-z_]{1,60}", s) else ""   # noqa: E731
    return clean(code), clean(reason)


# ---- the client -----------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Message:
    """One email, reduced to what classification needs. `text` and `subject` are untrusted data."""
    id: str
    thread_id: str
    received_at: Optional[datetime]
    from_name: str
    from_address: str            # as written, lower-cased; "" when the header held no usable address
    from_domain: str             # "" when there is none: callers must handle that
    subject: str
    text: str
    auth: Mapping                # {"trusted": bool, "dmarc": ..., "spf": ..., "dkim": ..., "header_from": ...} (see parse_auth_results)
    links: tuple = ()            # the web links in the email, from the plain text AND every HTML part (including the target of a button
                                 # or a link whose visible text says something else), at most MAX_LINKS, each at most 500 characters
    markup_doubt: bool = False   # the HTML could not be read exactly as a browser would (email_html): the text must not be relied on

    @property
    def sender_verified(self) -> bool:
        """True only when Google's own header says DMARC passed for exactly the domain this reader took the sender to be. A
        sender can write any From; this is the one signal that the domain is not forged. It is not proof of intent: a real
        company's domain can still send something unwanted."""
        return bool(self.from_domain and self.auth.get("trusted") and self.auth.get("dmarc") == "pass"
                    and self.auth.get("header_from") == self.from_domain)


class GmailClient:
    def __init__(self, client_id: str, client_secret: str, refresh_token: str, *, http: Optional[httpx.Client] = None,
                 clock=time.monotonic):
        for name, value in (("client id", client_id), ("client secret", client_secret), ("refresh token", refresh_token)):
            if not isinstance(value, str) or not value.strip() or re.search(r"\s", value.strip()):
                raise NotConfigured(f"the {name} is missing or malformed")
        self._id, self._secret, self._refresh = client_id.strip(), client_secret.strip(), refresh_token.strip()
        self._owns_http = http is None
        self._http = http or httpx.Client(timeout=httpx.Timeout(TIMEOUT_S, connect=10.0), follow_redirects=False)
        self._clock = clock
        self._token: Optional[str] = None
        self._expires = 0.0

    @classmethod
    def from_env(cls, env: Mapping[str, str], **kw) -> "GmailClient":
        missing = [n for n in ("GMAIL_CLIENT_ID", "GMAIL_CLIENT_SECRET", "GMAIL_REFRESH_TOKEN") if not (env.get(n) or "").strip()]
        if missing:
            raise NotConfigured("the Google login is not set up: " + ", ".join(missing) + " missing from .env")
        return cls(env["GMAIL_CLIENT_ID"], env["GMAIL_CLIENT_SECRET"], env["GMAIL_REFRESH_TOKEN"], **kw)

    def __repr__(self) -> str:                      # a credential must not appear in a traceback or a log line
        return "GmailClient(<credentials hidden>)"

    def close(self) -> None:
        """Close the connection pool, when this client made it (one a caller passed in stays the caller's)."""
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> "GmailClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- the access token -------------------------------------------------------------------------------------

    def access_token(self) -> str:
        if self._token and self._clock() < self._expires:
            return self._token
        return self._renew()

    def _renew(self) -> str:
        self._token = None
        status, body, _ = _request(self._http, "POST", base_url("oauth") + TOKEN_PATH, data={
            "client_id": self._id, "client_secret": self._secret, "refresh_token": self._refresh, "grant_type": "refresh_token"})
        if status != 200:
            code, _reason = _error_info(body)
            if code == "invalid_grant":
                raise LoginExpired("Google no longer accepts the saved login (it expired or was revoked): sign in again")
            if code in ("invalid_client", "unauthorized_client", "disabled_client"):
                raise LoginExpired("Google does not recognise, or has disabled, the client id or secret: check them in the Google Cloud console and sign in again")
            raise GmailError(f"Google refused the login (HTTP {status}{', ' + code if code else ''})")
        data = _json(body)
        token = data.get("access_token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token or len(token) > 4096 or re.search(r"\s", token):
            raise GmailError("Google's answer had no usable access token")
        scope = data.get("scope")
        scopes = set(scope.split()) if isinstance(scope, str) else None
        if not scopes or not scopes <= ALLOWED_SCOPES:
            # more than read-only, or no statement of what it allows: refuse before the token is ever used
            raise Overbroad("the login is not limited to read-only Gmail access: it is refused. Sign in again and grant only "
                            "'View your email messages and settings'")
        try:
            lifetime = int(data.get("expires_in", 3600))
        except (TypeError, ValueError, OverflowError):
            lifetime = 3600
        self._token = token
        self._expires = self._clock() + max(60, min(lifetime, 3600)) - EXPIRY_MARGIN_S
        return token

    # -- one API call -----------------------------------------------------------------------------------------

    def _api(self, path: str, params: Optional[dict] = None, *, max_bytes: int = MAX_SMALL_BYTES) -> dict:
        url = base_url("gmail") + API_ROOT + path
        if params:
            url += "?" + str(httpx.QueryParams({k: v for k, v in params.items() if v is not None}))
        for attempt in (0, 1):
            token = self.access_token()
            status, body, _ = _request(self._http, "GET", url, headers={"Authorization": f"Bearer {token}"}, max_bytes=max_bytes)
            if status == 401:
                self._token = None
                if attempt == 0:
                    continue                                # the access token was refused once: get a fresh one and try again
                raise LoginExpired("Gmail refused the login: sign in again")
            break
        if status == 200:
            data = _json(body)
            if not isinstance(data, dict):
                raise GmailError("Gmail returned an unexpected answer")
            return data
        code, reason = _error_info(body)
        if status == 403 and reason in ("rateLimitExceeded", "userRateLimitExceeded", "dailyLimitExceeded"):
            raise RateLimited("Gmail is limiting requests")
        if status == 403 and reason == "accessNotConfigured":
            raise GmailError("the Gmail API is not enabled for this Google project: enable it in the Google Cloud console")
        if status == 403 and reason in ("insufficientPermissions", "forbidden", "authError"):
            raise LoginExpired("Gmail says the login lacks permission: sign in again")
        if status == 403:                                  # a quota or an administrator's policy: signing in again would not help
            raise GmailError("Gmail refused the request (HTTP 403" + (f", {reason}" if reason else "") + ")")
        if status == 404 and path.startswith("/messages/"):
            raise MessageGone("Gmail has no such message")
        raise GmailError(f"Gmail answered HTTP {status}")

    # -- what it can ask --------------------------------------------------------------------------------------

    def profile(self) -> dict:
        """{"email": address, "total": messages in the mailbox}. Also the cheapest proof that the login works."""
        data = self._api("/profile")
        address = data.get("emailAddress")
        if not isinstance(address, str) or "@" not in address:
            raise GmailError("Gmail's profile had no address")
        total = data.get("messagesTotal")
        return {"email": address, "total": total if isinstance(total, int) and not isinstance(total, bool) else None}

    def list_messages(self, query: str, *, max_results: int = 50, page_token: Optional[str] = None) -> tuple[list[dict], Optional[str]]:
        """([{"id", "thread_id"}], next page token or None) for a Gmail search. Spam and trash are never included."""
        if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_CHARS:
            raise ValueError(f"a search is 1 to {MAX_QUERY_CHARS} characters")
        if not isinstance(max_results, int) or isinstance(max_results, bool) or not 1 <= max_results <= MAX_LIST:
            raise ValueError(f"max_results is 1 to {MAX_LIST}")
        data = self._api("/messages", {"q": query, "maxResults": max_results, "pageToken": page_token, "includeSpamTrash": "false"})
        out = []
        for m in data.get("messages") or []:
            if isinstance(m, dict) and isinstance(m.get("id"), str) and _ID.fullmatch(m["id"]):
                tid = m.get("threadId")
                out.append({"id": m["id"], "thread_id": tid if isinstance(tid, str) and _ID.fullmatch(tid) else ""})
        nxt = data.get("nextPageToken")
        return out, nxt if isinstance(nxt, str) and 0 < len(nxt) <= 512 else None

    def message_date(self, message_id: str) -> datetime:
        """When one message was received (Gmail's own internal date), without reading it: a minimal request that returns the date only."""
        if not isinstance(message_id, str) or not _ID.fullmatch(message_id):
            raise ValueError("not a message id")
        data = self._api(f"/messages/{message_id}", {"format": "minimal", "fields": "internalDate"})
        try:
            return datetime.fromtimestamp(int(data["internalDate"]) / 1000, tz=timezone.utc)
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            raise GmailError("Gmail gave no date for a message") from None

    def get_message(self, message_id: str) -> Message:
        if not isinstance(message_id, str) or not _ID.fullmatch(message_id):
            raise ValueError("not a message id")
        return parse_message(self._api(f"/messages/{message_id}", {"format": "full"}, max_bytes=MAX_RESPONSE_BYTES))


# ---- reading one message --------------------------------------------------------------------------------------

def _scrub(text: str) -> str:
    """Text that can be stored: a lone surrogate (invalid in UTF-8 and in JSON) becomes '?' instead of failing a database write later."""
    return text.encode("utf-8", "replace").decode("utf-8")


def _clean_line(text: object, limit: int) -> str:
    if not isinstance(text, str):
        return ""
    return _scrub(" ".join(_INVISIBLE.sub("", _CONTROL.sub(" ", text)).split()))[:limit]


def _header_map(payload: dict) -> dict:
    """{lower-cased name: [values in order]} from payload.headers, bounded."""
    out: dict = {}
    for h in (payload.get("headers") or [])[:MAX_HEADERS]:
        if isinstance(h, dict) and isinstance(h.get("name"), str) and isinstance(h.get("value"), str):
            out.setdefault(h["name"].strip().lower(), []).append(h["value"][:MAX_HEADER_CHARS])
    return out


_QUOTED = re.compile(r'"(?:[^"\\]|\\.)*"')


def _strip_comments(text: str) -> str:
    """The header with quoted strings and (nested) parenthesised comments removed. What a sender can influence (the local part of
    a Return-Path, a DKIM selector, a comment) sits in those; the verdicts do not."""
    text = _QUOTED.sub(" ", text)
    out, depth = [], 0
    for ch in text:
        if ch == "(":
            depth += 1
        elif ch == ")" and depth:
            depth -= 1
        elif depth == 0:
            out.append(ch)
    return "".join(out)


def parse_auth_results(headers: Mapping) -> dict:
    """What Google itself says about the sender (SPF, DKIM, DMARC), or nothing.

    Google's receiving server adds ONE Authentication-Results header whose authserv-id is mx.google.com. A message that carries
    two or more such headers has one that the sender wrote, and which is which cannot be told, so it is treated as untrusted
    (as is one whose authserv-id is anything else, or none at all). In the trusted header the verdicts are read per
    `;`-separated segment, at the start of the segment, after quoted strings and comments are removed, so text a sender can
    influence inside the header (a Return-Path local part, a DKIM selector) cannot supply a verdict. `header_from` is read only
    from the dmarc segment.

    -> {"trusted": bool, "dmarc": str, "spf": str, "dkim": str, "header_from": str}; every verdict 'unknown' when untrusted."""
    result = {"trusted": False, "dmarc": "unknown", "spf": "unknown", "dkim": "unknown", "header_from": ""}
    ours = [v for v in (headers.get("authentication-results") or [])
            if _strip_comments(v).split(";", 1)[0].strip().lower() == "mx.google.com"]
    if len(ours) != 1:
        return result
    result["trusted"] = True
    found = {}
    for segment in _strip_comments(ours[0]).split(";")[1:]:
        m = re.match(r"\s*(dmarc|spf|dkim)\s*=\s*([A-Za-z]+)", segment, re.I)
        if m and m.group(1).lower() not in found:
            found[m.group(1).lower()] = (m.group(2).lower(), segment)
    for method in ("dmarc", "spf", "dkim"):
        result[method] = found[method][0] if method in found else "none"
    if "dmarc" in found:
        hf = re.search(r"header\.from\s*=\s*([A-Za-z0-9.-]{1,253})", found["dmarc"][1])
        if hf and valid_domain(hf.group(1).lower().rstrip(".")):
            result["header_from"] = hf.group(1).lower().rstrip(".")
    return result


# The charsets an email is read as. Anything else (a name a sender made up, or a codec that is not for mail such as idna, punycode
# or utf-7, which can hide text) is read as UTF-8, so a sender's choice of charset can neither crash the reader nor change what it sees.
_CHARSETS = re.compile(r"(utf-?8|us-ascii|ascii|latin-?1|iso-?8859-\d{1,2}|windows-?125\d|cp125\d|koi8-[ru]|gb2312|gbk|gb18030|big5|"
                       r"shift[_-]?jis|euc-(?:jp|kr)|iso-2022-(?:jp|kr))", re.I)


def _decode_part(part: dict) -> str:
    body = part.get("body") if isinstance(part.get("body"), dict) else {}
    data = body.get("data")
    if not isinstance(data, str) or not data or len(data) > MAX_PART_BYTES * 2:
        return ""
    try:
        raw = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))[:MAX_PART_BYTES]
    except (binascii.Error, ValueError):
        return ""
    charset = "utf-8"
    for value in _header_map(part).get("content-type", []):
        m = re.search(r"charset=\"?([A-Za-z0-9_.:-]{1,40})", value, re.I)
        if m:
            charset = m.group(1) if _CHARSETS.fullmatch(m.group(1)) else "utf-8"
            break
    try:
        return raw.decode(charset, errors="replace")
    except (LookupError, UnicodeError):
        return raw.decode("utf-8", errors="replace")


def _walk(payload: dict):
    """Yield (mime type, part) for every non-attachment text part, breadth-bounded and depth-bounded."""
    stack, seen = [(payload, 0)], 0
    while stack:
        part, depth = stack.pop()
        seen += 1
        if seen > MAX_PARTS or not isinstance(part, dict):
            return
        mime = part.get("mimeType") if isinstance(part.get("mimeType"), str) else ""
        if depth < MAX_DEPTH and isinstance(part.get("parts"), list):
            stack.extend((p, depth + 1) for p in reversed(part["parts"][:MAX_PARTS]))
        if mime.lower() in ("text/plain", "text/html") and not part.get("filename"):
            yield mime.lower(), part


_URL = re.compile(r"https?://[^\s<>\"')\]]+", re.I)
_HREF = re.compile(r"""\bhref\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", re.I)


def _links(plain: list, markup: list) -> tuple:
    """Every http(s) link a reader could click: URLs written in the plain text, URLs in the HTML text, and the TARGET of every HTML
    href (a button whose label says 'View offer' points somewhere else). Both parts count: a harmless plain part beside a harmful
    HTML part is a known trick, and the person sees the HTML."""
    found, seen = [], set()
    candidates = []
    for text in plain:
        candidates.extend(_URL.findall(text))
    for html_text in markup:
        for m in _HREF.finditer(html_text[:MAX_PART_BYTES]):
            candidates.append(next(g for g in m.groups() if g is not None))
        candidates.extend(_URL.findall(html_text[:MAX_PART_BYTES]))
    for url in candidates:
        url = _scrub(" ".join(url.split()))[:500].rstrip(".,;:")
        if url.lower().startswith(("http://", "https://")) and url not in seen:
            seen.add(url)
            found.append(url)
            if len(found) >= MAX_LINKS:
                break
    return tuple(found)


def _body_text(payload: dict) -> tuple:
    """(text, links, markup_doubt). The text is the plain part when there is one, else the HTML as the text a person sees (hidden
    elements dropped: email_html); the links come from every part. `markup_doubt` is true when the HTML could not be read
    exactly as a browser would, so callers must not rely on the text (it is never sent to a model)."""
    plain: list[str] = []
    markup: list[str] = []
    for mime, part in _walk(payload):
        text = _decode_part(part)
        if text:
            (plain if mime == "text/plain" else markup).append(text)
    doubt = False
    if plain:
        text = "\n\n".join(plain)
        if markup:                                              # most mail clients show the HTML part: the two must say the same
            shown = "\n".join(email_html.visible_text(m[:email_html.MAX_CHARS])[0] for m in markup[:3])
            doubt = email_html.parts_differ(text, shown)
    else:
        pieces, budget = [], email_html.MAX_CHARS
        for m in markup:                                        # one budget for all the HTML parts of one email
            if budget <= 0:
                doubt = True
                break
            piece, d = email_html.visible_text(m[:budget])
            doubt = doubt or d or len(m) > budget              # part of the markup was not read
            budget -= min(len(m), budget)
            pieces.append(piece)
        text = "\n\n".join(pieces)
    text = _INVISIBLE.sub("", _CONTROL.sub(" ", text.replace("\r\n", "\n").replace("\r", "\n")))
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return _scrub(text[:MAX_TEXT_CHARS]), _links(plain, markup), doubt


def parse_message(raw: object) -> Message:
    """A Gmail `messages.get(format=full)` reply as a Message. Raises GmailError for something that is not one."""
    if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not _ID.fullmatch(raw["id"]):
        raise GmailError("Gmail returned a message that could not be read")
    payload = raw.get("payload") if isinstance(raw.get("payload"), dict) else {}
    headers = _header_map(payload)

    name, address = parseaddr((headers.get("from") or [""])[0])
    address = address.strip().lower()
    domain = ""
    if address.count("@") == 1 and not re.search(r"[\s<>,;\"\\]", address):
        candidate = address.split("@", 1)[1]
        # ASCII only, and never converted: IDNA conversion maps look-alikes onto real domains (straße.de -> strasse.de), so a domain
        # written in non-ASCII letters is "no domain" (the email is then never matched to a company). A punycode (xn--) domain is
        # kept exactly as written, so it never equals the real one.
        if valid_domain(candidate):
            domain = candidate
    if not domain:
        address = ""

    received: Optional[datetime] = None
    try:
        received = datetime.fromtimestamp(int(raw["internalDate"]) / 1000, tz=timezone.utc)
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        try:
            received = parsedate_to_datetime((headers.get("date") or [""])[0])
            if received.tzinfo is None:
                received = received.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError, IndexError, OverflowError):
            received = None

    thread = raw.get("threadId")
    text, links, doubt = _body_text(payload)
    return Message(
        id=raw["id"], thread_id=thread if isinstance(thread, str) and _ID.fullmatch(thread) else "",
        received_at=received, from_name=_clean_line(name, 120), from_address=address, from_domain=domain,
        subject=_clean_line((headers.get("subject") or [""])[0], MAX_SUBJECT_CHARS),
        text=text, auth=parse_auth_results(headers), links=links, markup_doubt=doubt)
