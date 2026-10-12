"""Company job boards and remote feeds: the shared core every source adapter plugs into (spec sections 3, 5, 6, 8).

This module owns what is common to all ten sources: the bounded HTTP fetch, HTML to plain text, slug validation,
the request/result dataclasses, the helper that builds the normalised job, and fetch(), the one entry point.
A per-source adapter does only two things, fetch a listing and map it:

    def fetch_<name>(client, slug, req, now) -> tuple[list[dict], int]     # (normalised jobs, how many were listed)

and registers itself in ADAPTERS. fetch() then re-applies the free relevance filter (relevance.check, idempotent, so
an adapter that must make a second call per job may pre-filter on its list data and cap at req.limit), sorts newest
first, applies the per-request cap, and turns ANY problem in a source into one line in `errors`: a failing company or
feed never stops the run and never raises.

Safety: posting text is untrusted DATA (safety.py still runs on it at ingest). Every URL is a FIXED host per system
(base_url) plus a validated slug, so nothing a user or a posting supplies can change the host; posting links are never
fetched. Responses are streamed and size-limited, redirects are never followed, a 429 is never retried, and a 5xx or
a timeout is retried exactly once.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from typing import Any, Callable, Optional
from urllib.parse import quote, urlsplit

import httpx

import planner
import relevance
import sources

log = logging.getLogger("scraper.boards")

USER_AGENT = "ai-job-search-n8n/1.0 (personal job search)"
MAX_BYTES = 32_000_000                    # the largest real response seen was 13.5 MB (an Ashby board)
TIMEOUT_S = 30.0                          # per network READ (connect, and each wait for the next bytes), not for the whole request
REQUEST_DEADLINE_S = 60.0                 # the whole request: a server that drips bytes cannot hold a run longer than this
MAX_HTML_CHARS = 200_000                  # html_to_text reads at most this much of a posting (the stdlib parser is quadratic on hostile markup)
MAX_LIMIT = 100                           # fetch() never returns more than this many jobs, whatever the request asks for
DEFAULT_LIMIT = 25
RETRY_DELAY_S = 1.0                       # pause before the single retry of a 5xx or a timeout; tests set it to 0

# The fixed host of each system. BOARDS_API_URL (tests and rehearsals only, like APIFY_API_URL; never set it in
# production) replaces them all with `{BOARDS_API_URL}/{system}`, where scraper/stubs.py StubBoards listens.
_HOSTS = {
    "greenhouse": "https://boards-api.greenhouse.io",
    "lever": "https://api.lever.co",
    "lever_eu": "https://api.eu.lever.co",             # EU-hosted Lever companies; an adapter detail, not a configurable source
    "ashby": "https://api.ashbyhq.com",
    "workable": "https://apply.workable.com",
    "smartrecruiters": "https://api.smartrecruiters.com",
    "remotive": "https://remotive.com",
    "remoteok": "https://remoteok.com",
    "weworkremotely": "https://weworkremotely.com",
    "himalayas": "https://himalayas.app",
    "arbeitnow": "https://www.arbeitnow.com",
}

# Seconds that must pass between two fetches of the same source (spec section 6; enforced by the caller with
# fetch_state): Remotive allows at most 4 fetches a day; one company board is 10 minutes.
MIN_INTERVAL_S: dict[str, int] = {"remotive": 21600, "remoteok": 3600, "weworkremotely": 3600, "himalayas": 3600,
                                  "arbeitnow": 3600, "board": 600}

# source name -> fetch_<name>(client, slug, req, now) -> (jobs, listed). The adapter tasks add their entries.
ADAPTERS: dict[str, Callable] = {}

_BLOCK_TAGS = frozenset({"p", "div", "br", "li", "ul", "ol", "tr", "table", "h1", "h2", "h3", "h4", "h5", "h6",
                         "td", "th", "section", "article", "blockquote", "pre", "hr", "dt", "dd"})
_SKIP_TAGS = frozenset({"script", "style"})
# A "<" that does not open a complete tag (no ">" before the next "<"): the stdlib parser re-scans such a tail again and again
# (quadratic time on a run of "<a"), so it is written as an entity first; the parser then decodes it back to a plain "<".
# (A "<" inside a quoted attribute value, as in <a title="a<b">, therefore reads as text: accepted for hostile-input safety.)
_STRAY_LT = re.compile(r"<(?![A-Za-z/!?][^<>]*>)")
_ZERO_WIDTH = re.compile("[\u200b\ufeff]")
_SPACES = re.compile("[ \t\r\f\v\u00a0\u2000-\u200a\u3000]+")
_DROPPED_REASONS = ("title", "location", "age")


class BoardError(Exception):
    """A source problem that is explained to the user as one line (the text becomes the tail of the error line)."""


class NotFound(BoardError):
    """HTTP 404: no such company board (Lever tries its EU host before it reports this)."""


class RateLimited(BoardError):
    """HTTP 429: stop this source for this run; never retried."""


class _Transient(Exception):
    """Internal: a 5xx or a network failure, worth one retry."""


# A company slug: letters, digits, '.', '_' and '-', not starting with a symbol, at most 64 characters.
# The rule lives in sources.py so the stdlib-only settings validator can share it.
valid_slug = sources.valid_slug


@dataclass
class BoardRequest:
    terms: list[str]
    locations: list[str]
    allow_remote: bool = True
    max_age_days: int = 7
    title_words: list[str] = field(default_factory=list)
    limit: int = 25
    name: Optional[str] = None            # the display name from config/search.json, for company names
    home_country: str = "india"           # kept for the planner; relevance.check does not take it (it assumes India)


@dataclass
class BoardResult:
    jobs: list[dict]
    listed: int = 0                       # postings the source listed
    relevant: int = 0                     # how many passed the free relevance filter (before the limit)
    dropped: dict[str, int] = field(default_factory=lambda: {"title": 0, "location": 0, "age": 0, "unusable": 0})
    errors: list[str] = field(default_factory=list)


# ---- small helpers every adapter shares -----------------------------------------------------------------

def base_url(system: str) -> str:
    """`https://<fixed host>` for a system, or `{BOARDS_API_URL}/{system}` when the test hook is set (read at call time)."""
    if system not in _HOSTS:
        raise ValueError(f"unknown board system '{system}'")
    override = os.environ.get("BOARDS_API_URL")
    return f"{override.rstrip('/')}/{system}" if override else _HOSTS[system]


def company_name(slug: Optional[str], req_name: Optional[str], payload_name: Optional[str]) -> str:
    """The posting's own company name, else the display name from the config, else the slug title-cased."""
    for candidate in (payload_name, req_name):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return re.sub(r"[-._]+", " ", str(slug or "")).strip().title()


def make_job(source: str, source_job_id: str, *, url: str, title: str, company: str, location: Optional[str] = None,
             is_remote: Optional[bool] = None, posted_at: Optional[str] = None, description: str = "",
             apply_url: Optional[str] = None, salary_min: Optional[float] = None, salary_max: Optional[float] = None,
             salary_currency: Optional[str] = None, raw: Optional[dict] = None) -> dict:
    """The normalised job exactly as the existing pipeline expects it (see normalise() in app.py).

    `posted_at` is an ISO-8601 string or None; `description` is plain text (html_to_text); `raw` is a compact subset
    (ids, urls, dates, locations), not the description.

    No string in the result holds a NUL character (Postgres refuses it in text and jsonb, and one bad posting would fail the
    whole ingest batch): they are removed from every field and from every string inside `raw`."""
    return _no_nul({
        "source": source,
        "source_job_id": source_job_id,
        "url": url,
        "apply_url": apply_url or url,
        "title": str(title or "").strip(),
        "company": company,
        "location": location,
        "is_remote": is_remote,
        "posted_at": posted_at,
        "description": description or "",
        "salary_min": salary_min,
        "salary_max": salary_max,
        "salary_currency": salary_currency,
        "raw": raw if raw is not None else {},
    })


def _no_nul(value: Any) -> Any:
    """The value with every NUL character removed from its strings (dict keys and values, list and tuple items, recursively)."""
    if isinstance(value, str):
        return value.replace("\x00", "") if "\x00" in value else value
    if isinstance(value, dict):
        return {_no_nul(k): _no_nul(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_no_nul(v) for v in value]
    return value


# ---- HTML to text ---------------------------------------------------------------------------------------

class _TextExtractor(HTMLParser):
    """Collects visible text: script/style content is skipped and block-level tags become line breaks."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)       # decodes the entities that remain after the optional unescape
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip += 1
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        if tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            self._skip = max(0, self._skip - 1)
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def html_to_text(html: Optional[str], *, escaped: bool = False) -> str:
    """Plain text from HTML: tags stripped, script/style content dropped, block tags as line breaks, entities decoded once,
    whitespace collapsed, zero-width characters removed and unicode spaces made plain. `escaped=True` is for entity-ESCAPED html
    (Greenhouse): unescaped once, then parsed. Only the first MAX_HTML_CHARS characters are read. Never raises."""
    text = str(html or "")[:MAX_HTML_CHARS].replace("\x00", "")
    if escaped:
        text = unescape(text)
    text = _STRAY_LT.sub("&lt;", text)
    parser = _TextExtractor()
    try:
        parser.feed(text)
        parser.close()
        out = "".join(parser.parts)
    except Exception:                                  # malformed beyond what the parser tolerates: strip tags crudely
        out = unescape(re.sub(r"<[^>]*>", " ", text))
    lines = (_SPACES.sub(" ", line).strip() for line in _ZERO_WIDTH.sub("", out).split("\n"))
    return "\n".join(line for line in lines if line).replace("\x00", "")     # a numeric entity (&#0;) can decode to NUL


# ---- the bounded fetch ----------------------------------------------------------------------------------

def _once(client: httpx.Client, method: str, url: str, json_body: Any, max_bytes: int) -> bytes:
    started = time.monotonic()
    try:
        with client.stream(method, url, json=json_body, headers={"User-Agent": USER_AGENT}, follow_redirects=False) as resp:
            status = resp.status_code
            if status == 429:
                raise RateLimited("rate limited (HTTP 429)")
            if 300 <= status < 400:
                raise BoardError(f"unexpected redirect (HTTP {status})")
            if status == 404:
                raise NotFound("not found (check the slug)")
            if status >= 500:
                raise _Transient(f"server error (HTTP {status})")
            if status >= 400:
                raise BoardError(f"HTTP {status}")
            too_big = BoardError(f"response is larger than {max_bytes} bytes")
            try:
                if int(resp.headers.get("content-length", "")) > max_bytes:
                    raise too_big                       # refuse before reading a byte
            except ValueError:
                pass                                    # no (or a bad) Content-Length: the streamed count decides
            body = bytearray()
            for chunk in resp.iter_bytes():
                if len(body) + len(chunk) > max_bytes:      # a decoded gzip chunk can be far larger than the wire bytes: refuse before buffering
                    raise too_big
                body.extend(chunk)
                if time.monotonic() - started > REQUEST_DEADLINE_S:    # the timeout is per read: a drip-feeding server needs this
                    raise BoardError("response took too long")         # not a _Transient: a slow source is skipped for this run, not retried
            return bytes(body)
    except httpx.TimeoutException:
        raise _Transient("timed out")
    except (httpx.ConnectError, httpx.ReadError) as e:
        raise _Transient(f"network error ({type(e).__name__})")
    except httpx.HTTPError as e:                       # a proxy or protocol error, an undecodable body, ...: not worth a retry
        raise BoardError(f"network error ({type(e).__name__})")


_DEFAULT_PORTS = {"http": 80, "https": 443}


def _origin(url: str):
    """(scheme, host, port) of a URL, lower-cased with the default port filled in; None when the URL is not trustworthy.

    The origin is read by BOTH parsers that matter (urllib's urlsplit and httpx.URL, the one the client really uses) and must be the
    same in both: they disagree on odd hosts (urlsplit takes the part between "[" and "]" as the host, httpx sends the whole string), and
    such a difference would let a host through the check that is not the host that gets called. An authority with "[", "]", "@" (user
    info) or a backslash is refused outright, and so is a URL either parser cannot read."""
    try:
        parts = urlsplit(url)
        if any(ch in parts.netloc for ch in "[]@\\"):
            return None
        scheme = parts.scheme.lower()
        mine = (scheme, (parts.hostname or "").lower(), parts.port or _DEFAULT_PORTS.get(scheme))
        theirs = httpx.URL(url)
        clients = (theirs.scheme.lower(), theirs.host.lower(), theirs.port or _DEFAULT_PORTS.get(theirs.scheme.lower()))
    except (ValueError, TypeError, httpx.InvalidURL):
        return None
    return mine if mine == clients else None


def _known_origins() -> set:
    """The origins of every job-board host, taken from base_url() AT CALL TIME so a BOARDS_API_URL test override is honoured."""
    origins = {_origin(base_url(system)) for system in tuple(sources.BOARD_SYSTEMS) + tuple(sources.FEEDS) + ("lever_eu",)}
    origins.discard(None)
    return origins


def get_bytes(client: httpx.Client, method: str, url: str, *, json_body: Any = None, max_bytes: int = MAX_BYTES) -> bytes:
    """One bounded request. Streams the body and aborts once it passes max_bytes (the Content-Length is checked first and the
    streamed bytes counted too, chunk by chunk, so a gzip bomb never enters the buffer). Redirects are never followed. HTTP 429 raises
    RateLimited at once (no retry); a 5xx, a timeout, a connection error or a read error is retried exactly once after RETRY_DELAY_S;
    404 and every other error status or transport error raise BoardError at once. A URL whose origin (scheme, host, port) is not that
    of a known job-board host (base_url of every system) is refused before any request is made."""
    method = method.upper()
    if method not in ("GET", "POST"):
        raise ValueError("method must be GET or POST")
    origin = _origin(url)
    if origin is None or origin not in _known_origins():        # parsed origins are compared, never string prefixes
        raise BoardError("refused: not a known job-board host")
    try:
        return _once(client, method, url, json_body, max_bytes)
    except _Transient as first:
        log.info("retrying once: %s", first)
        time.sleep(RETRY_DELAY_S)
        try:
            return _once(client, method, url, json_body, max_bytes)
        except _Transient as second:
            raise BoardError(f"{second} (tried twice)") from None


def get_json(client: httpx.Client, method: str, url: str, *, json_body: Any = None, max_bytes: int = MAX_BYTES) -> Any:
    """get_bytes plus json.loads; a body that is not JSON (an HTML error page) is a BoardError, not a crash."""
    body = get_bytes(client, method, url, json_body=json_body, max_bytes=max_bytes)
    try:
        return json.loads(body)
    except ValueError:                                 # JSONDecodeError and UnicodeDecodeError
        raise BoardError("returned something that is not JSON") from None


# ---- fetch ----------------------------------------------------------------------------------------------

def _error_line(label: str, exc: Exception) -> str:
    if isinstance(exc, RateLimited):
        return f"{label}: rate limited by the source; skipped for this run"
    if isinstance(exc, BoardError):
        return f"{label}: {exc}"
    if isinstance(exc, ValueError):                    # get_json already turns bad JSON into a BoardError, so this is some other bad value
        return f"{label}: returned an unexpected value"
    if isinstance(exc, httpx.HTTPError):
        return f"{label}: network error ({type(exc).__name__})"
    if isinstance(exc, (KeyError, TypeError, AttributeError, IndexError)):
        # Usually a changed schema, but it is also what a bug in an adapter looks like: keep the traceback in the log.
        log.warning("%s: unexpected format (%s)", label, type(exc).__name__, exc_info=exc)
        return f"{label}: returned an unexpected format (the source may have changed)"
    log.warning("%s: unexpected %s", label, type(exc).__name__, exc_info=True)
    return f"{label}: unexpected error ({type(exc).__name__})"


def _relevance_rules(req: BoardRequest) -> dict:
    """The request as the dict relevance.check takes (used by fetch() and by an adapter that pre-filters its list)."""
    return {"terms": req.terms, "locations": req.locations, "allow_remote": req.allow_remote,
            "max_age_days": req.max_age_days, "title_words": req.title_words}


def _newest_first_key(job: dict, now: Optional[datetime] = None):
    """Sort key, newest first, unknown dates last. With `now`, a date more than a day ahead of it is not believed (a clock
    skew of hours is normal, a year-9999 posting is not) and counts as unknown, so it cannot take a top slot."""
    posted = relevance._parse_posted(job.get("posted_at"))   # the same date parser the age filter uses
    if posted is not None and now is not None:
        now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
        if posted > now + timedelta(days=1):
            posted = None
    return (posted is None, -posted.timestamp() if posted is not None else 0.0)


def _drop_future_date(job: dict, now: datetime) -> None:
    """A `posted_at` more than a day after `now` is not believed (a clock skew of hours is normal, a year-9999 posting is not): the job
    is stored with no date, so it cannot take a top slot in the newest-first scoring queue."""
    posted = relevance._parse_posted(job.get("posted_at"))
    now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    if posted is not None and posted > now + timedelta(days=1):
        job["posted_at"] = None


def _usable(job: Any) -> bool:
    return isinstance(job, dict) and bool(job.get("title")) and bool(job.get("source_job_id")) and bool(job.get("url"))


def fetch(source: str, slug: Optional[str], req: BoardRequest, *, client: Optional[httpx.Client] = None,
          now: Optional[datetime] = None) -> BoardResult:
    """Fetch one company board (source = a system, slug required) or one remote feed (no slug), filter, sort and cap it.

    Never raises for a source problem: an unknown source, a bad slug, a network failure, a rate limit or a changed schema
    comes back as one line in `errors`, and a bad request makes NO network call. `req.limit` None means 25 and more than 100 is cut to
    100; limit <= 0 returns nothing (the safe direction); the HTTP endpoint validates 1..100."""
    result = BoardResult(jobs=[])
    is_board, is_feed = source in sources.BOARD_SYSTEMS, source in sources.FEEDS
    if not (is_board or is_feed):
        result.errors.append(f"unknown source '{str(source)[:40]}'")
        return result
    if is_feed and slug:
        result.errors.append(f"{source}: this is a feed and takes no company slug")
        return result
    if is_board and slug is None:
        result.errors.append(f"{source}: a company slug is required")
        return result
    if is_board and not valid_slug(slug):
        result.errors.append(f"{source}: invalid company slug (letters, digits, '.', '_' and '-'; at most 64 characters)")
        return result
    label = f"{source}/{slug}" if is_board else source
    adapter = ADAPTERS.get(source)
    if adapter is None:
        result.errors.append(f"{label}: no adapter is installed for this source")
        return result

    now = now or datetime.now(timezone.utc)
    limit = req.limit if isinstance(req.limit, int) and not isinstance(req.limit, bool) else DEFAULT_LIMIT
    req = replace(req, limit=min(limit, MAX_LIMIT))                 # a copy: the caller's request is left alone; adapters see the clamp too
    own_client = client is None
    try:
        if own_client:                                              # inside the try: a broken proxy environment makes this raise
            client = httpx.Client(timeout=httpx.Timeout(TIMEOUT_S), follow_redirects=False, headers={"User-Agent": USER_AGENT})
        jobs, listed = adapter(client, slug, req, now)
        result.listed = int(listed)
        rules = _relevance_rules(req)
        kept: list[dict] = []
        for job in jobs:
            if not _usable(job):
                result.dropped["unusable"] += 1
                continue
            _drop_future_date(job, now)
            ok, reason = relevance.check(job, rules, now)
            if ok:
                kept.append(job)
            else:
                result.dropped[reason if reason in _DROPPED_REASONS else "title"] += 1
        result.relevant = len(kept)
        kept.sort(key=lambda j: _newest_first_key(j, now))
        result.jobs = kept[:max(0, req.limit)]
    except Exception as exc:                           # a source problem must never stop the run
        result.jobs, result.relevant = [], 0
        result.errors.append(_error_line(label, exc))
    finally:
        if own_client and client is not None:
            client.close()
    return result


# ---- company boards: Greenhouse, Lever, Ashby (spec section 2) --------------------------------------------
# Each adapter reads one company's public board and maps it with make_job. A posting that lacks an id, a title or a
# url is skipped (not counted anywhere, debug log only). The adapters pre-filter nothing except Greenhouse, whose
# list has no description: it needs a second call per job, so it filters on list data first (relevance.check, the same
# rules fetch() applies) and reads the details of the newest req.limit survivors only.

def _postings(payload: Any, key: Optional[str] = None) -> list:
    """The list of postings: the payload itself (key=None) or payload[key]; anything else means the format changed."""
    items = payload.get(key) if key is not None and isinstance(payload, dict) else (payload if key is None else None)
    if not isinstance(items, list):
        raise BoardError("returned an unexpected format (the source may have changed)")
    return items


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _post_id(value: Any) -> str:
    """The id as a string ('' when absent or not a string or a whole number)."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return ""
    return str(value).strip()


# An id that goes into a URL path: letters, digits, '_' and '-' only, at most 64 characters (".." or "a/b" would change the path).
_SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _names(items: Any) -> list[str]:
    """The `name` of each dict in a list (Greenhouse departments), skipping anything else."""
    return [n for n in (_text(i.get("name")) for i in items if isinstance(i, dict)) if n] if isinstance(items, list) else []


def _ms_to_iso(value: Any) -> Optional[str]:
    """Epoch MILLISECONDS (Lever's createdAt) -> ISO-8601 UTC, or None for anything that is not a sane number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc).isoformat(timespec="milliseconds")
    except (ValueError, OverflowError, OSError):
        return None


def _valid_posted(value: Any) -> Optional[str]:
    """A source's own ISO-8601 date string, kept as the source wrote it, when it is a real date (the same rule _iso_utc applies); else None.
    A garbage date must never reach the database (the whole ingest batch would fail on it)."""
    return value.strip() if _iso_utc(value) is not None else None


def _skipped(label: str, count: int) -> None:
    if count:
        log.debug("%s: skipped %d posting(s) without an id, title or url", label, count)


# -- Greenhouse: GET /v1/boards/{slug}/jobs (list, no descriptions) and /jobs/{id} (one job with its escaped-HTML content)

def _gh_location(post: dict) -> Optional[str]:
    loc = post.get("location")
    return (_text(loc.get("name")) or None) if isinstance(loc, dict) else None


def fetch_greenhouse(client: httpx.Client, slug: str, req: BoardRequest, now: datetime) -> tuple[list[dict], int]:
    root = f"{base_url('greenhouse')}/v1/boards/{slug}/jobs"
    posts = _postings(get_json(client, "GET", root), "jobs")
    rules = _relevance_rules(req)
    survivors: list[tuple[dict, dict, str]] = []                    # (the fields relevance.check reads, the list posting, its id)
    skipped = 0
    for post in posts:
        pid = _post_id(post.get("id")) if isinstance(post, dict) else ""
        if not (_SAFE_ID.fullmatch(pid) and _text(post.get("title")) and _text(post.get("absolute_url"))):
            skipped += 1
            continue
        probe = {"title": _text(post["title"]), "location": _gh_location(post), "is_remote": None,
                 "posted_at": post.get("first_published") or post.get("updated_at")}
        if relevance.check(probe, rules, now)[0]:
            survivors.append((probe, post, pid))
    _skipped(f"greenhouse/{slug}", skipped)
    survivors.sort(key=lambda s: _newest_first_key(s[0], now))
    survivors = survivors[:max(0, req.limit)]                       # the detail-request count can never exceed the limit

    jobs: list[dict] = []
    last_error: Optional[Exception] = None
    for probe, post, pid in survivors:
        try:
            detail = get_json(client, "GET", f"{root}/{quote(pid, safe='')}")
            if not isinstance(detail, dict):
                raise BoardError("returned an unexpected format")
        except RateLimited:
            raise                                                   # a 429 stops the whole board for this run
        except BoardError as exc:                                   # one bad job costs that job only
            log.debug("greenhouse/%s: job %s dropped: %s", slug, pid, exc)
            last_error = exc
            continue
        title = _text(detail.get("title")) or probe["title"]
        url = _text(detail.get("absolute_url")) or _text(post.get("absolute_url"))
        location = _gh_location(detail) or probe["location"]
        posted = detail.get("first_published") or detail.get("updated_at") or probe["posted_at"]
        jobs.append(make_job(
            "greenhouse", f"{slug}:{pid}", url=url, title=title,
            company=company_name(slug, req.name, _text(detail.get("company_name")) or _text(post.get("company_name"))),
            location=location, posted_at=_valid_posted(posted),
            description=html_to_text(detail.get("content"), escaped=True),
            raw={"id": detail.get("id", post.get("id")), "url": url, "first_published": detail.get("first_published"),
                 "updated_at": detail.get("updated_at"), "location": location,
                 "departments": _names(detail.get("departments") or post.get("departments"))}))
    if survivors and not jobs:
        raise BoardError(f"none of the {len(survivors)} job-detail requests worked (last: {last_error})")
    return jobs, len(posts)


# -- Lever: GET /v0/postings/{slug}?mode=json on api.lever.co, or api.eu.lever.co for EU-hosted companies

_LEVER_WORKPLACE = {"remote": True, "hybrid": False, "on-site": False, "onsite": False}


def _lever_lists(lists: Any) -> list[str]:
    """Each `lists` section ([{"text": heading, "content": "<li>..</li>"}]) as "heading\ntext of the content"; anything odd is skipped."""
    out: list[str] = []
    for item in lists if isinstance(lists, list) else []:
        if not isinstance(item, dict):
            continue
        body = html_to_text(item["content"]) if isinstance(item.get("content"), str) else ""
        out.append("\n".join(p for p in (_text(item.get("text")), body) if p))
    return [t for t in out if t]


def fetch_lever(client: httpx.Client, slug: str, req: BoardRequest, now: datetime) -> tuple[list[dict], int]:
    path = f"/v0/postings/{slug}?mode=json"
    try:
        payload = get_json(client, "GET", base_url("lever") + path)
    except NotFound:                                                # maybe an EU company: ask the EU host once, nothing else
        try:
            payload = get_json(client, "GET", base_url("lever_eu") + path)
        except NotFound:
            raise BoardError("board not found on either Lever host (check the slug)") from None
    posts = _postings(payload)
    jobs: list[dict] = []
    skipped = 0
    for post in posts:
        pid = _post_id(post.get("id")) if isinstance(post, dict) else ""
        title, url = (_text(post.get("text")), _text(post.get("hostedUrl"))) if isinstance(post, dict) else ("", "")
        if not (pid and title and url):
            skipped += 1
            continue
        cats = post.get("categories") if isinstance(post.get("categories"), dict) else {}
        location = _text(cats.get("location")) or None
        workplace = _text(post.get("workplaceType")).lower()
        # the `lists` sections ("Requirements", "What you'll do") hold the experience a posting asks for: they go between the two texts
        description = "\n\n".join(p for p in ([_text(post.get("descriptionPlain"))] + _lever_lists(post.get("lists"))
                                              + [_text(post.get("additionalPlain"))]) if p)
        jobs.append(make_job(
            "lever", f"{slug}:{pid}", url=url, apply_url=_text(post.get("applyUrl")) or None, title=title,
            company=company_name(slug, req.name, None), location=location, is_remote=_LEVER_WORKPLACE.get(workplace),
            posted_at=_ms_to_iso(post.get("createdAt")), description=description,
            raw={"id": pid, "hostedUrl": url, "applyUrl": post.get("applyUrl"), "createdAt": post.get("createdAt"),
                 "location": location, "department": _text(cats.get("department")), "team": _text(cats.get("team")),
                 "commitment": _text(cats.get("commitment")), "workplaceType": post.get("workplaceType")}))
    _skipped(f"lever/{slug}", skipped)
    return jobs, len(posts)


# -- Ashby: GET /posting-api/job-board/{slug}: one response with every listed job, descriptions included (up to ~14 MB)

def _ashby_remote(post: dict) -> Optional[bool]:
    """isRemote is true for 509 of the 815 real jobs that are workplaceType "Hybrid", so workplaceType decides when it is known."""
    workplace = re.sub(r"[\s_-]+", "", _text(post.get("workplaceType")).lower())
    if workplace == "remote":
        return True
    if workplace in ("hybrid", "onsite"):
        return False
    flag = post.get("isRemote")
    return flag if isinstance(flag, bool) else None


def _ashby_location(post: dict) -> Optional[str]:
    """`location` plus any `secondaryLocations` (each a string or an object with a `location` key), joined with '; '."""
    parts: list[str] = []
    seen: set[str] = set()                                          # kept beside the list: a `not in list` test is quadratic on a huge list
    extra = post.get("secondaryLocations")
    for item in [post.get("location")] + (extra if isinstance(extra, list) else []):
        text = _text(item.get("location") if isinstance(item, dict) else item)
        if text and text not in seen:
            seen.add(text)
            parts.append(text)
    return "; ".join(parts) or None


def fetch_ashby(client: httpx.Client, slug: str, req: BoardRequest, now: datetime) -> tuple[list[dict], int]:
    posts = _postings(get_json(client, "GET", f"{base_url('ashby')}/posting-api/job-board/{slug}"), "jobs")
    jobs: list[dict] = []
    listed = skipped = 0
    for post in posts:
        if isinstance(post, dict) and post.get("isListed") is False:    # hidden from the public board; not "listed", not skipped
            continue
        listed += 1
        pid = _post_id(post.get("id")) if isinstance(post, dict) else ""
        title, url = (_text(post.get("title")), _text(post.get("jobUrl"))) if isinstance(post, dict) else ("", "")
        if not (pid and title and url):
            skipped += 1
            continue
        location = _ashby_location(post)
        secondary = post.get("secondaryLocations") if isinstance(post.get("secondaryLocations"), list) else []
        jobs.append(make_job(
            "ashby", f"{slug}:{pid}", url=url, apply_url=_text(post.get("applyUrl")) or None, title=title,
            company=company_name(slug, req.name, None), location=location, is_remote=_ashby_remote(post),
            posted_at=_valid_posted(post.get("publishedAt")),
            description=_text(post.get("descriptionPlain")),
            raw={"id": pid, "jobUrl": url, "applyUrl": post.get("applyUrl"), "publishedAt": post.get("publishedAt"),
                 "location": _text(post.get("location")), "secondaryLocations": [_text(s.get("location") if isinstance(s, dict) else s) for s in secondary],
                 "department": post.get("department"), "team": post.get("team"), "employmentType": post.get("employmentType"),
                 "workplaceType": post.get("workplaceType"), "isRemote": post.get("isRemote")}))
    _skipped(f"ashby/{slug}", skipped)
    return jobs, listed


# ---- company boards that list without descriptions: Workable, SmartRecruiters (spec section 2) -------------
# Both lists carry no description, so each adapter pre-filters on its list data (relevance.check, the same rules
# fetch() applies), keeps the newest req.limit survivors and makes ONE detail call per survivor: the detail-request
# count can never exceed req.limit or the number of survivors. One failed detail drops that job only; when every
# detail fails the board is an error; a 429 stops the board. No URL taken from a response is ever requested.

def _each_detail(client: httpx.Client, label: str, survivors: list, url_of: Callable, build: Callable) -> list[dict]:
    """GET url_of(item) for each survivor and map it with build(item, detail); any failure of one job (a BoardError, or a
    hostile payload that makes build raise) costs that job only, except a 429, which stops the board."""
    jobs: list[dict] = []
    last_error: Optional[Exception] = None
    for item in survivors:
        try:
            detail = get_json(client, "GET", url_of(item))
            if not isinstance(detail, dict):
                raise BoardError("returned an unexpected format")
            jobs.append(build(item, detail))
        except RateLimited:
            raise                                                   # a 429 stops the whole board for this run
        except Exception as exc:                                    # one bad job costs that job only
            log.debug("%s: a job was dropped: %s", label, exc, exc_info=not isinstance(exc, BoardError))
            last_error = exc
    if survivors and not jobs:
        raise BoardError(f"none of the {len(survivors)} job-detail requests worked (last: {last_error})")
    return jobs


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _texts(items: Any) -> list[str]:
    """The non-empty strings of a list (Workable departments), skipping anything else."""
    return [t for t in (_text(i) for i in items) if t] if isinstance(items, list) else []


# -- Workable: POST /api/v3/accounts/{slug}/jobs (one page of listings), GET /api/v2/accounts/{slug}/jobs/{shortcode} (the v3 detail 404s)

_WK_LIST_BODY = {"query": "", "location": [], "department": [], "worktype": [], "remote": []}
_WK_SHORTCODE = _SAFE_ID
_WK_WORKPLACE = {"remote": True, "hybrid": False, "onsite": False}


def _wk_place(post: dict) -> dict:
    """The structured `location` object, else the first of `locations`."""
    place = post.get("location")
    if not isinstance(place, dict) and isinstance(post.get("locations"), list):
        place = next((p for p in post["locations"] if isinstance(p, dict)), None)
    return _as_dict(place)


def _wk_location(post: dict) -> Optional[str]:
    """'City, Region, Country' from the structured parts that are present."""
    place = _wk_place(post)
    parts: list[str] = []
    for key in ("city", "region", "country"):
        text = _text(place.get(key))
        if text and text not in parts:
            parts.append(text)
    return ", ".join(parts) or None


def _wk_remote(post: dict) -> Optional[bool]:
    """`workplace` decides when it is known (the `remote` flag is not enough for a hybrid role); else the `remote` flag."""
    workplace = _WK_WORKPLACE.get(re.sub(r"[\s_-]+", "", _text(post.get("workplace")).lower()))
    if workplace is not None:
        return workplace
    return post["remote"] if isinstance(post.get("remote"), bool) else None


def fetch_workable(client: httpx.Client, slug: str, req: BoardRequest, now: datetime) -> tuple[list[dict], int]:
    api = f"{base_url('workable')}/api"
    posts = _postings(get_json(client, "POST", f"{api}/v3/accounts/{slug}/jobs", json_body=dict(_WK_LIST_BODY)), "results")
    rules = _relevance_rules(req)
    survivors: list[tuple[dict, dict, str]] = []                    # (the fields relevance.check reads, the list posting, its shortcode)
    skipped = 0
    for post in posts:
        code = _post_id(post.get("shortcode")) if isinstance(post, dict) else ""
        if not (_WK_SHORTCODE.fullmatch(code) and _text(post.get("title"))):
            skipped += 1
            continue
        probe = {"title": _text(post["title"]), "location": _wk_location(post), "is_remote": _wk_remote(post),
                 "posted_at": post.get("published")}
        if relevance.check(probe, rules, now)[0]:
            survivors.append((probe, post, code))
    _skipped(f"workable/{slug}", skipped)
    survivors.sort(key=lambda s: _newest_first_key(s[0], now))
    survivors = survivors[:max(0, req.limit)]                       # the detail-request count can never exceed the limit

    def build(item: tuple, detail: dict) -> dict:
        probe, post, code = item
        url = f"https://apply.workable.com/{slug}/j/{quote(code, safe='')}/"       # a link for the user: built, never taken from the payload
        location = _wk_location(detail) or probe["location"]
        remote = _wk_remote(detail) if ("workplace" in detail or "remote" in detail) else probe["is_remote"]
        posted = detail.get("published") or probe["posted_at"]
        description = "\n\n".join(t for t in (html_to_text(detail.get(k)) for k in ("description", "requirements", "benefits")) if t)
        return make_job(
            "workable", f"{slug}:{code}", url=url, title=_text(detail.get("title")) or probe["title"],
            company=company_name(slug, req.name, None), location=location, is_remote=remote,
            posted_at=_valid_posted(posted), description=description,
            raw={"id": _post_id(detail.get("id", post.get("id"))), "shortcode": code, "url": url,
                 "published": _text(detail.get("published")) or _text(post.get("published")), "location": location,
                 "remote": remote, "workplace": _text(detail.get("workplace") or post.get("workplace")),
                 "type": _text(detail.get("type") or post.get("type")),
                 "department": _texts(detail.get("department") or post.get("department"))})

    jobs = _each_detail(client, f"workable/{slug}", survivors, lambda it: f"{api}/v2/accounts/{slug}/jobs/{quote(it[2], safe='')}", build)
    return jobs, len(posts)


# -- SmartRecruiters: GET /v1/companies/{slug}/postings?limit&offset&q&country (newest first), then .../postings/{id}

_SR_PAGE_SIZE, _SR_MAX_PAGES = 100, 3
_SR_ID = _SAFE_ID                                           # the id goes into a URL path: nothing else (".." or "a/b" would change the path)
_SR_SECTIONS = ("jobDescription", "qualifications", "additionalInformation")      # companyDescription is the same boilerplate on every job
# The list endpoint's `country` takes exactly ONE lower-case ISO-2 code (two values return nothing), so it is sent only
# for a user whose locations all resolve to one of these countries (planner names -> codes).
_SR_COUNTRY = {"india": "in", "uk": "gb", "usa": "us", "germany": "de", "ireland": "ie", "netherlands": "nl",
               "singapore": "sg", "canada": "ca", "france": "fr", "spain": "es", "australia": "au",
               "united arab emirates": "ae", "switzerland": "ch", "sweden": "se", "poland": "pl", "brazil": "br",
               "japan": "jp"}


def _sr_country(req: BoardRequest) -> Optional[str]:
    """The code to send as `country`: the user's ONE country when every non-remote location resolves to it, else None.

    A bare "Remote" is no country; a location that does not resolve (a bare city) means the country is not known, so
    nothing is sent rather than a guess that would hide the other places."""
    countries: set[str] = set()
    for location in req.locations:
        if not str(location or "").strip() or planner.is_remote_location(location):
            continue
        country = planner.country_for(location, req.home_country)
        if country is None:
            return None
        countries.add(country)
    return _SR_COUNTRY.get(next(iter(countries))) if len(countries) == 1 else None


def _sr_query(req: BoardRequest) -> str:
    """The core words of ALL search terms, de-duplicated (the API's `q` ORs its words)."""
    words: list[str] = []
    for term in req.terms:
        words.extend(w for w in relevance.core_words(term) if w not in words)
    return " ".join(words)


def _sr_remote(location: dict) -> Optional[bool]:
    if location.get("hybrid") is True:
        return False
    if location.get("remote") is True:
        return True
    return False if location.get("remote") is False else None


def _label(value: Any) -> str:
    return _text(value.get("label")) if isinstance(value, dict) else ""


def fetch_smartrecruiters(client: httpx.Client, slug: str, req: BoardRequest, now: datetime) -> tuple[list[dict], int]:
    """One company's board: up to three list pages, then one detail call per surviving posting. `country=<code>` is sent only for a single-country user and also hides remote postings that are
    listed under other countries (known and intended)."""
    root = f"{base_url('smartrecruiters')}/v1/companies/{slug}/postings"
    country, query = _sr_country(req), _sr_query(req)
    rules = _relevance_rules(req)
    survivors: list[tuple[dict, dict, str]] = []                    # (the fields relevance.check reads, the list posting, its id)
    seen: set[str] = set()                                          # a server that ignores `offset` repeats a page: each id counts once
    listed = skipped = 0
    for page in range(_SR_MAX_PAGES):
        offset = page * _SR_PAGE_SIZE
        params = {"limit": _SR_PAGE_SIZE, "offset": offset}
        if query:
            params["q"] = query
        if country:
            params["country"] = country
        try:
            payload = get_json(client, "GET", f"{root}?{httpx.QueryParams(params)}")
            posts = _postings(payload, "content")
        except RateLimited:
            raise
        except BoardError:
            if page == 0:
                raise
            log.debug("smartrecruiters/%s: page %d failed; keeping the earlier pages", slug, page + 1, exc_info=True)
            break                                                   # an earlier page's postings are still worth keeping
        if not posts:
            break
        listed += len(posts)
        reached_old = False
        for post in posts:
            if not isinstance(post, dict):
                skipped += 1
                continue
            if not relevance.within_age(post.get("releasedDate"), req.max_age_days, now):
                reached_old = True                                  # lists are newest first: everything after this is older still
            pid, location = _post_id(post.get("id")), _as_dict(post.get("location"))
            if not (_SR_ID.fullmatch(pid) and _text(post.get("name"))):
                skipped += 1
                continue
            if pid in seen:
                continue
            seen.add(pid)
            probe = {"title": _text(post["name"]), "location": _text(location.get("fullLocation")) or None,
                     "is_remote": _sr_remote(location), "posted_at": post.get("releasedDate")}
            if relevance.check(probe, rules, now)[0]:
                survivors.append((probe, post, pid))
        total = payload.get("totalFound")
        if reached_old or (isinstance(total, int) and not isinstance(total, bool) and offset + _SR_PAGE_SIZE >= total):
            break
    _skipped(f"smartrecruiters/{slug}", skipped)
    survivors.sort(key=lambda s: _newest_first_key(s[0], now))
    survivors = survivors[:max(0, req.limit)]                       # the detail-request count can never exceed the limit

    def build(item: tuple, detail: dict) -> dict:
        probe, post, pid = item
        url = _text(detail.get("postingUrl"))
        if not url:
            raise BoardError("the job has no postingUrl")
        loc = _as_dict(detail.get("location"))
        location = _text(loc.get("fullLocation")) or probe["location"]
        remote = _sr_remote(loc) if loc else probe["is_remote"]
        posted = detail.get("releasedDate") or probe["posted_at"]
        sections = _as_dict(_as_dict(detail.get("jobAd")).get("sections"))
        description = "\n\n".join(t for t in (html_to_text(_as_dict(sections.get(k)).get("text")) for k in _SR_SECTIONS) if t)
        company = _text(_as_dict(detail.get("company")).get("name")) or _text(_as_dict(post.get("company")).get("name"))
        return make_job(
            "smartrecruiters", f"{slug}:{pid}", url=url, apply_url=_text(detail.get("applyUrl")) or None, title=_text(detail.get("name")) or probe["title"],
            company=company_name(slug, req.name, company), location=location, is_remote=remote,
            posted_at=_valid_posted(posted), description=description,
            raw={"id": pid, "postingUrl": url, "applyUrl": _text(detail.get("applyUrl")), "releasedDate": _text(posted),
                 "location": location, "remote": loc.get("remote"), "hybrid": loc.get("hybrid"),
                 "function": _label(detail.get("function") or post.get("function")),
                 "typeOfEmployment": _label(detail.get("typeOfEmployment") or post.get("typeOfEmployment")),
                 "experienceLevel": _label(detail.get("experienceLevel") or post.get("experienceLevel"))})

    jobs = _each_detail(client, f"smartrecruiters/{slug}", survivors, lambda it: f"{root}/{quote(it[2], safe='')}", build)
    return jobs, listed


# ---- remote feeds: Remotive, RemoteOK, We Work Remotely (spec sections 2 and 6) -------------------------------
# A feed is ONE request that lists many companies' remote jobs: no slug, no pre-filtering (fetch() applies relevance and
# the limit), every job is remote. `source_job_id` is the feed's own id (nothing to prefix it with). The usage terms
# are kept by never replacing a job's URL with anything else: the link stays on the feed's own site, and `source`
# names it. The minimum interval between fetches (Remotive: 6 h) is enforced by the caller with fetch_state.

def _iso_utc(value: Any) -> Optional[str]:
    """An ISO-8601 string (with or without an offset, a naive one is UTC) -> ISO-8601 UTC, else None."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        when = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        return (when.replace(tzinfo=timezone.utc) if when.tzinfo is None else when.astimezone(timezone.utc)).isoformat()
    except (ValueError, OverflowError):                  # not a date, or one UTC cannot represent (year 1 with a positive offset)
        return None


def _epoch_to_iso(value: Any) -> Optional[str]:
    """Epoch SECONDS (a number or a numeric string) -> ISO-8601 UTC, or None for anything that is not a sane number.
    Zero and negative epochs are placeholders, not dates (1970 would read as a 55-year-old posting): None, so the caller falls back."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
        if not number > 0:                                             # also catches nan
            return None
        return datetime.fromtimestamp(number, tz=timezone.utc).isoformat()
    except (ValueError, OverflowError, OSError):
        return None


# -- Remotive: GET /api/remote-jobs?category=software-dev (at most 4 fetches a day; the wrapper's legal/warning keys are ignored)

def fetch_remotive(client: httpx.Client, slug: Optional[str], req: BoardRequest, now: datetime) -> tuple[list[dict], int]:
    url = f"{base_url('remotive')}/api/remote-jobs?{httpx.QueryParams({'category': 'software-dev'})}"
    posts = _postings(get_json(client, "GET", url), "jobs")
    jobs: list[dict] = []
    skipped = 0
    for post in posts:
        pid = _post_id(post.get("id")) if isinstance(post, dict) else ""
        title, link = (_text(post.get("title")), _text(post.get("url"))) if isinstance(post, dict) else ("", "")
        if not (pid and title and link):
            skipped += 1
            continue
        location = _text(post.get("candidate_required_location")) or None       # "Worldwide" is a real value
        jobs.append(make_job(
            "remotive", pid, url=link, title=title, company=company_name(None, None, _text(post.get("company_name"))),
            location=location, is_remote=True, posted_at=_iso_utc(post.get("publication_date")),    # naive: UTC
            description=html_to_text(post.get("description")),                  # `salary` is free text: ignored, not parsed
            raw={"id": post.get("id"), "url": link, "publication_date": post.get("publication_date"), "location": location,
                 "job_type": post.get("job_type"), "category": post.get("category")}))
    _skipped("remotive", skipped)
    return jobs, len(posts)


# -- RemoteOK: GET /api, a JSON array whose first element is a legal notice (not a posting)

def _positive(value: Any) -> Optional[float]:
    """A salary figure (a number or a numeric string) when it is > 0, else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return number if number > 0 and number != float("inf") else None


def fetch_remoteok(client: httpx.Client, slug: Optional[str], req: BoardRequest, now: datetime) -> tuple[list[dict], int]:
    rows = _postings(get_json(client, "GET", f"{base_url('remoteok')}/api"))
    posts = [r for r in rows if isinstance(r, dict) and "id" in r and "position" in r]      # drops the legal-notice row
    jobs: list[dict] = []
    skipped = 0
    for post in posts:
        pid, title, link = _post_id(post.get("id")), _text(post.get("position")), _text(post.get("url"))
        if not (pid and title and link):
            skipped += 1
            continue
        location = _text(post.get("location")) or None                          # often empty
        jobs.append(make_job(
            "remoteok", pid, url=link, apply_url=_text(post.get("apply_url")) or None, title=title,
            company=company_name(None, None, _text(post.get("company"))), location=location, is_remote=True,
            posted_at=_epoch_to_iso(post.get("epoch")) or _iso_utc(post.get("date")),
            description=html_to_text(post.get("description")),
            salary_min=_positive(post.get("salary_min")), salary_max=_positive(post.get("salary_max")),
            raw={"id": pid, "url": link, "apply_url": post.get("apply_url"), "epoch": post.get("epoch"),
                 "date": post.get("date"), "location": location, "tags": post.get("tags")}))
    _skipped("remoteok", skipped)
    return jobs, len(posts)


# -- We Work Remotely: GET /categories/remote-programming-jobs.rss (RSS 2.0; `title` is "Company: Role")

WWR_PATH = "/categories/remote-programming-jobs.rss"          # the stub serves the same path under /weworkremotely
_XML_DECLARATIONS = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)", re.IGNORECASE)
# The DOCTYPE guard reads bytes, so it is only sound for ASCII-compatible encodings (in UTF-16 or UTF-32 the letters are
# separated by NUL bytes and expat would still honour the declared encoding). Only these encodings are accepted.
_XML_ENCODING = re.compile(rb"\A(?:\xef\xbb\xbf)?\s*<\?xml[^>]*?\sencoding\s*=\s*[\"']([^\"']*)[\"']", re.IGNORECASE)
_SAFE_ENCODINGS = frozenset({"utf-8", "utf8", "us-ascii", "ascii", "iso-8859-1", "latin-1", "latin1", "windows-1252", "cp1252"})
_WIDE_BOMS = (b"\xff\xfe", b"\xfe\xff")                   # UTF-16 and UTF-32 (little endian) / UTF-16 (big endian); the UTF-32 big-endian BOM has NULs


def _child_text(item: Any, tag: str) -> str:
    node = item.find(tag)
    return (node.text or "").strip() if node is not None else ""


def _rfc822_to_iso(value: str) -> Optional[str]:
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    return (when.replace(tzinfo=timezone.utc) if when.tzinfo is None else when.astimezone(timezone.utc)).isoformat()


def fetch_weworkremotely(client: httpx.Client, slug: Optional[str], req: BoardRequest, now: datetime) -> tuple[list[dict], int]:
    body = get_bytes(client, "GET", f"{base_url('weworkremotely')}{WWR_PATH}")
    # The XML is untrusted. Before anything is parsed: (1) a wide encoding (a UTF-16/32 byte-order mark or any NUL byte) is
    # refused, (2) so is a declared encoding outside the ASCII-compatible allow-list, which keeps (3) sound: a DOCTYPE or
    # ENTITY declaration (entity-expansion bombs, external entities) is refused; a plain RSS feed needs neither.
    if body.startswith(_WIDE_BOMS) or b"\x00" in body:
        raise BoardError("returned a feed in a not supported RSS encoding (refused)")
    declared = _XML_ENCODING.match(body)
    if declared and declared.group(1).decode("ascii", "replace").strip().lower() not in _SAFE_ENCODINGS:
        raise BoardError("returned a feed in a not supported RSS encoding (refused)")
    # By design a feed containing the text "<!DOCTYPE" or "<!ENTITY" ANYWHERE (even inside CDATA or a comment) is refused: the check
    # is a plain byte search, so it cannot be fooled by how a parser would read the document, at the price of the odd false refusal.
    if _XML_DECLARATIONS.search(body):
        raise BoardError("returned a document with a DOCTYPE or ENTITY declaration (refused)")
    try:
        root = ET.fromstring(body)
    except ET.ParseError:
        raise BoardError("returned something that is not valid RSS/XML") from None
    channel = root.find("channel") if root.tag == "rss" else None
    if channel is None:
        raise BoardError("returned an unexpected format (the source may have changed)")
    items = channel.findall("item")
    jobs: list[dict] = []
    skipped = 0
    for item in items:
        guid, link, full_title = _child_text(item, "guid"), _child_text(item, "link"), _child_text(item, "title")
        company, sep, role = full_title.partition(": ")                         # the FIRST ": " only: a role may contain colons
        company, role = company.strip(), role.strip()
        if not (guid and link and sep and company and role):
            skipped += 1
            continue
        region = _child_text(item, "region") or None
        text = (item.find("description").text or "") if item.find("description") is not None else ""
        # The XML layer has already decoded the escaping once, so `text` is HTML; a double-escaped body (&lt;p&gt;) is unescaped too.
        jobs.append(make_job(
            "weworkremotely", guid, url=link, title=role, company=company, location=region, is_remote=True,
            posted_at=_rfc822_to_iso(_child_text(item, "pubDate")),
            description=html_to_text(text, escaped="<" not in text and "&lt;" in text),
            raw={"guid": guid, "link": link, "region": region, "pubDate": _child_text(item, "pubDate"),
                 "category": _child_text(item, "category")}))
    _skipped("weworkremotely", skipped)
    return jobs, len(items)


# ---- paged remote feeds: Himalayas, Arbeitnow (spec sections 2, 5 item 6 and 6) -----------------------------
# Both are huge, newest-first and paged (Himalayas lists ~118,000 jobs, Arbeitnow serves 325 jobs and ~2.8 MB a page), so
# the stop rules are the point: never more than FEED_MAX_PAGES pages, and stop after the page in which the first posting
# older than max_age_days appears (everything after it is older still). A request is ALWAYS built from base_url() plus a
# page number or an opaque cursor passed as a query parameter; a URL that comes back in a response (Arbeitnow's
# links.next, Himalayas' links) is data and is never requested. Postings are de-duplicated by id across pages because a
# newest-first feed shifts while it is being paged.

FEED_MAX_PAGES = 3
_HIM_PAGE_SIZE = 20                                          # Himalayas caps its page size at 20
_HIM_MAX_CURSOR = 200                                        # a longer (or non-string) cursor is not a cursor: paging ends


def _feed_pages(client: httpx.Client, label: str, url_for: Callable, key: str, next_token: Callable):
    """Yield the posts of up to FEED_MAX_PAGES pages. `next_token(payload)` boils a page's payload down to what the next request
    needs (a cursor, a "there is more" flag) and only that token is carried to `url_for(page_index, token)` (the token is None for
    page 0), which returns the next URL, or None when the feed has no more. Paging ends on an empty page. A failure on page 1
    propagates; a 429 anywhere propagates (the feed stops for this run); any other failure on a later page keeps what the
    earlier pages gave."""
    token = None
    for page in range(FEED_MAX_PAGES):
        url = url_for(page, token)
        if url is None:
            return
        try:
            payload = get_json(client, "GET", url)
            posts = _postings(payload, key)
        except RateLimited:
            raise
        except BoardError:
            if page == 0:
                raise
            log.debug("%s: page %d failed; keeping the earlier pages", label, page + 1, exc_info=True)
            return
        if not posts:
            return
        token = next_token(payload)
        del payload                                                   # the whole page (megabytes) is not kept while the caller works
        yield posts


# -- Himalayas: GET /jobs/api?limit=20, then &cursor={nextCursor} (cursor paging; offset is deprecated)

def _him_location(post: dict) -> Optional[str]:
    restrictions = post.get("locationRestrictions")
    if not isinstance(restrictions, list):
        return None
    names = [n for n in (_text(r) for r in restrictions) if n]
    if names:
        return ", ".join(names)
    if not restrictions:
        return "Worldwide"                                           # an empty list means worldwide
    return None


def fetch_himalayas(client: httpx.Client, slug: Optional[str], req: BoardRequest, now: datetime) -> tuple[list[dict], int]:
    root = f"{base_url('himalayas')}/jobs/api"

    asked: list = []                                                 # the cursor each request used, so a stuck cursor is noticed

    def url_for(page: int, cursor: Any) -> Optional[str]:
        params: dict = {"limit": _HIM_PAGE_SIZE}
        if page > 0:
            if not (isinstance(cursor, str) and 0 < len(cursor) <= _HIM_MAX_CURSOR):
                return None                                           # the last page, or a token we do not trust
            if asked and cursor == asked[-1]:
                return None                                           # the same nextCursor on two consecutive pages: it is stuck
            asked.append(cursor)
            params["cursor"] = cursor
        return f"{root}?{httpx.QueryParams(params)}"

    jobs: list[dict] = []
    seen: set[str] = set()
    listed = skipped = 0
    for posts in _feed_pages(client, "himalayas", url_for, "jobs", lambda payload: payload.get("nextCursor") if isinstance(payload, dict) else None):
        reached_old = False
        for post in posts:
            if not isinstance(post, dict):
                skipped += 1
                continue
            posted = _epoch_to_iso(post.get("pubDate"))
            if not relevance.within_age(posted, req.max_age_days, now):
                reached_old = True                                    # newest first: everything after this is older still
            link = _text(post.get("applicationLink"))
            pid, title = _post_id(post.get("guid")) or link, _text(post.get("title"))
            if not (pid and title and link):
                skipped += 1
                continue
            if pid in seen:
                continue
            seen.add(pid)
            listed += 1
            minimum, maximum = _positive(post.get("minSalary")), _positive(post.get("maxSalary"))
            location = _him_location(post)
            jobs.append(make_job(
                "himalayas", pid, url=link, title=title, company=company_name(None, None, _text(post.get("companyName"))),
                location=location, is_remote=True, posted_at=posted,       # every Himalayas job is remote
                description=html_to_text(post.get("description")) or html_to_text(post.get("excerpt")),
                salary_min=minimum, salary_max=maximum,
                salary_currency=(_text(post.get("currency")) or None) if (minimum or maximum) else None,
                raw={"guid": pid, "applicationLink": link, "pubDate": post.get("pubDate"), "locationRestrictions": post.get("locationRestrictions"),
                     "employmentType": post.get("employmentType"), "companySlug": post.get("companySlug")}))
        if reached_old:
            break
    _skipped("himalayas", skipped)
    return jobs, listed


# -- Arbeitnow: GET /api/job-board-api?page=N (Germany-centred: no location logic here, fetch()'s relevance filter drops
# what a user outside Germany cannot use; the `remote` flag is passed on as given)

def fetch_arbeitnow(client: httpx.Client, slug: Optional[str], req: BoardRequest, now: datetime) -> tuple[list[dict], int]:
    root = f"{base_url('arbeitnow')}/api/job-board-api"

    def more(payload: Any) -> bool:
        links = payload.get("links") if isinstance(payload, dict) else None
        return not (isinstance(links, dict) and not links.get("next"))   # only a signal that this was the last page: the URL itself is never used

    def url_for(page: int, has_more: Any) -> Optional[str]:
        return None if page > 0 and not has_more else f"{root}?{httpx.QueryParams({'page': page + 1})}"

    jobs: list[dict] = []
    seen: set[str] = set()
    listed = skipped = 0
    for posts in _feed_pages(client, "arbeitnow", url_for, "data", more):
        reached_old = False
        for post in posts:
            if not isinstance(post, dict):
                skipped += 1
                continue
            posted = _epoch_to_iso(post.get("created_at"))
            if not relevance.within_age(posted, req.max_age_days, now):
                reached_old = True
            pid, title, link = _post_id(post.get("slug")), _text(post.get("title")), _text(post.get("url"))
            if not (pid and title and link):
                skipped += 1
                continue
            if pid in seen:
                continue
            seen.add(pid)
            listed += 1
            location = _text(post.get("location")) or None
            remote = post.get("remote") if isinstance(post.get("remote"), bool) else None
            tags = post.get("tags")
            jobs.append(make_job(
                "arbeitnow", pid, url=link, title=title, company=company_name(None, None, _text(post.get("company_name"))),
                location=location, is_remote=remote, posted_at=posted, description=html_to_text(post.get("description")),
                raw={"slug": pid, "url": link, "created_at": post.get("created_at"), "location": location, "remote": remote,
                     "tags": [t for t in tags if isinstance(t, str)][:20] if isinstance(tags, list) else []}))
        if reached_old:
            break
    _skipped("arbeitnow", skipped)
    return jobs, listed


ADAPTERS["greenhouse"] = fetch_greenhouse
ADAPTERS["lever"] = fetch_lever
ADAPTERS["ashby"] = fetch_ashby
ADAPTERS["workable"] = fetch_workable
ADAPTERS["smartrecruiters"] = fetch_smartrecruiters
ADAPTERS["remotive"] = fetch_remotive
ADAPTERS["remoteok"] = fetch_remoteok
ADAPTERS["weworkremotely"] = fetch_weworkremotely
ADAPTERS["himalayas"] = fetch_himalayas
ADAPTERS["arbeitnow"] = fetch_arbeitnow
