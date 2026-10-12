"""Stand-in APIs for testing: a fake Anthropic Messages API, a fake Apify API and a fake of ten public job-board and
remote-feed APIs (StubBoards), served on localhost.

Why: real calls cost money, and the interesting tests are about failure (outages, garbage replies, hung runs,
loops). These servers let the REAL service code run end to end with every call counted and nothing billed.

    with StubAnthropic(scoring_ok()) as ai, StubApify(balances={"tok": 5.0}) as apify:
        anthropic_client.API_URL = ai.url      # or set ANTHROPIC_API_URL
        apify_source.API = apify.url           # or set APIFY_API_URL
        ... run the real code ...
        assert ai.billed == 3 and apify.starts == 1

"Billed" counts the replies that a real API would have charged for (HTTP 200); errors and refusals are free.
Loopback only. The unit tests refuse any other network connection (see tests/conftest.py).
"""
from __future__ import annotations

import base64
import copy
import hashlib
import html
import itertools
import json
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional
from urllib.parse import parse_qs, unquote
from xml.sax.saxutils import escape as _xml_escape

# ----------------------------------------------------------------------------- shared plumbing


class _Server:
    def __init__(self, handler_cls):
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self._httpd.stub = self                      # the handler reaches its owner through this
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self.lock = threading.Lock()
        self.base = f"http://127.0.0.1:{self._httpd.server_address[1]}"

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._httpd.shutdown()
        self._httpd.server_close()


def _json(handler: BaseHTTPRequestHandler, status: int, payload) -> None:
    body = json.dumps(payload).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


# ----------------------------------------------------------------------------- fake Anthropic

# handler(call_number, request_body) -> (http_status, reply_text_or_None, usage_dict)
AnthropicHandler = Callable[[int, dict], tuple]

SCORING_USAGE = {"input_tokens": 2500, "output_tokens": 340, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
TAILOR_USAGE = {"input_tokens": 3000, "output_tokens": 1200, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}


def score_reply(user_text: str) -> str:
    """A valid scoring reply (the shape scoring.finalize accepts), varying a little per posting."""
    h = int(hashlib.md5(user_text.encode()).hexdigest(), 16)
    base = 45 + (h % 50)
    return json.dumps({
        "scores": {"technical": base, "experience": min(100, base + 5), "behavioral": 60, "career": max(0, base - 5)},
        "gates": {"eligibility": {"verdict": "PASS", "note": ""}, "language": {"verdict": "PASS", "note": ""},
                  "location": {"verdict": "PASS", "note": ""}},
        "deadline": None, "posting_language": "English", "yoe": {"min": None, "max": None},
        "strengths": ["stub strength"], "gaps": ["stub gap"], "reason": "stub reply"})


def scoring_ok() -> AnthropicHandler:
    return lambda n, body: (200, score_reply(body["messages"][0]["content"]), SCORING_USAGE)


def always_status(status: int) -> AnthropicHandler:
    """An outage (529) or a refusal (400): nothing is billed."""
    return lambda n, body: (status, None, {})


def garbage_reply() -> AnthropicHandler:
    """A billed reply that is not the JSON we asked for."""
    return lambda n, body: (200, "Sorry, I cannot score this posting as JSON.", SCORING_USAGE)


def tailor_draft(patch: dict, *, then_status: Optional[int] = None, flawed: Optional[dict] = None) -> AnthropicHandler:
    """Replies to a tailoring request with `patch`. A revision request ("rejected by an automatic honesty check")
    gets `then_status` instead (an outage) when given. With `flawed`, the FIRST answer is that flawed patch (so a
    revision is required) and the revision gets the good one, or the outage."""
    def handler(n: int, body: dict):
        text = body["messages"][0]["content"]
        revision = "rejected by an automatic honesty check" in text
        if revision and then_status:
            return then_status, None, {}
        return 200, json.dumps(patch if (revision or not flawed) else flawed), TAILOR_USAGE
    return handler


class StubAnthropic(_Server):
    def __init__(self, handler: AnthropicHandler):
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                stub: StubAnthropic = self.server.stub
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
                with stub.lock:
                    stub.requests += 1
                    n = stub.requests
                    stub.models.append(body.get("model"))
                status, text, usage = stub.handler(n, body)
                with stub.lock:
                    stub.by_status[status] = stub.by_status.get(status, 0) + 1
                    if status == 200:
                        stub.billed += 1
                if status != 200:
                    return _json(self, status, {"type": "error", "error": {"type": "overloaded_error" if status == 529 else "invalid_request_error",
                                                                          "message": f"stub says {status}"}})
                _json(self, 200, {"id": "msg_stub", "type": "message", "role": "assistant", "model": body.get("model"),
                                  "stop_reason": "end_turn", "content": [{"type": "text", "text": text}], "usage": usage})
        super().__init__(H)
        self.handler = handler
        self.requests = 0               # every call that arrived
        self.billed = 0                 # calls a real API would have charged for (HTTP 200)
        self.by_status: dict[int, int] = {}
        self.models: list = []

    @property
    def url(self) -> str:
        return self.base + "/v1/messages"


# ----------------------------------------------------------------------------- fake Apify

APIFY_ITEM = {"id": "111", "link": "https://www.linkedin.com/jobs/view/111", "title": "Backend Engineer", "companyName": "Acme",
              "location": "Bengaluru, India", "postedAt": "2026-10-09", "descriptionText": "Build APIs",
              "applyUrl": "https://acme.com/a"}


class StubApify(_Server):
    """The few Apify endpoints the adapter uses. `modes[token]`: ok | fail (run ends FAILED) | hang (never ends)
    | start_error (the start is refused, nothing billed). A run that finishes charges `charge` against the balance,
    as a real run does when its results are produced."""

    def __init__(self, balances: dict, modes: Optional[dict] = None, charge: float = 0.047, items: Optional[list] = None):
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _token(self):
                return self.headers.get("Authorization", "").removeprefix("Bearer ")

            def do_GET(self):
                stub: StubApify = self.server.stub
                tok, path = self._token(), self.path.split("?")[0]
                if path.endswith("/users/me/limits"):
                    bal = stub.balances.get(tok)
                    if bal is None:
                        return _json(self, 500, {})
                    return _json(self, 200, {"data": {"limits": {"maxMonthlyUsageUsd": 100.0}, "current": {"monthlyUsageUsd": 100.0 - bal}}})
                if "/actor-runs/" in path:
                    with stub.lock:
                        stub.polls += 1
                    mode = stub.modes.get(tok, "ok")
                    status = "RUNNING" if mode == "hang" else ("FAILED" if mode == "fail" else "SUCCEEDED")
                    return _json(self, 200, {"data": {"id": "run-1", "status": status, "defaultDatasetId": f"ds-{tok}"}})
                if "/datasets/" in path:
                    with stub.lock:
                        if stub.balances.get(tok) is not None:
                            stub.balances[tok] = round(stub.balances[tok] - stub.charge, 6)
                            stub.charged += stub.charge
                    return _json(self, 200, stub.items)
                _json(self, 404, {})

            def do_POST(self):
                stub: StubApify = self.server.stub
                tok, path = self._token(), self.path.split("?")[0]
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if path.endswith("/abort"):
                    with stub.lock:
                        stub.aborts += 1
                    return _json(self, 200, {"data": {}})
                if path.endswith("/runs"):
                    if stub.modes.get(tok, "ok") == "start_error":
                        with stub.lock:
                            stub.rejected += 1
                        return _json(self, 402, {"error": "payment required"})
                    with stub.lock:
                        stub.starts += 1
                        if stub.modes.get(tok, "ok") in ("fail", "hang"):
                            stub.charged += stub.charge * 0.5      # a failed or aborted run is still billed for what it used
                            if stub.balances.get(tok) is not None:
                                stub.balances[tok] = round(stub.balances[tok] - stub.charge * 0.5, 6)
                    return _json(self, 201, {"data": {"id": "run-1", "status": "RUNNING", "defaultDatasetId": f"ds-{tok}"}})
                _json(self, 404, {})
        super().__init__(H)
        self.balances, self.modes, self.charge = dict(balances), dict(modes or {}), charge
        self.items = items if items is not None else [APIFY_ITEM]
        self.starts = self.rejected = self.aborts = self.polls = 0
        self.charged = 0.0

    @property
    def url(self) -> str:
        return self.base + "/v2"


# ----------------------------------------------------------------------------- fake job boards and remote feeds
#
# The builders below make ONE posting each, in the raw shape the real public API returns (key names, nesting and
# value types probed from the live APIs on 2026-10-11; every value is invented). Tests use them to put postings into
# StubBoards(data=...), and the stub itself uses them for its default data and its paging feeds.
#
# Common keyword overrides: title, location (a string such as "Pune, India"), posted (a datetime, an ISO date or
# datetime string, or epoch seconds; default: yesterday), id (the id the real API keys the posting by), plus
# company / board where the real shape carries them. Fresh ids are made up when `id` is left out.

_auto_ids = itertools.count(5001)
_AUTO = object()                              # "argument left out", for a keyword whose None is a real value
_CC = {"india": "in", "united states": "us", "usa": "us", "germany": "de", "france": "fr", "united kingdom": "gb",
       "uk": "gb", "canada": "ca", "spain": "es", "poland": "pl", "brazil": "br", "netherlands": "nl"}


def _auto_id() -> int:
    return next(_auto_ids)


def _when(posted=None) -> datetime:
    """A timezone-aware UTC datetime from a datetime, an ISO date/datetime string or epoch seconds (default: yesterday)."""
    if posted is None:
        return (datetime.now(timezone.utc) - timedelta(days=1)).replace(microsecond=0)
    if isinstance(posted, (int, float)):
        return datetime.fromtimestamp(posted, timezone.utc)
    if isinstance(posted, str):
        posted = datetime.fromisoformat(posted.replace("Z", "+00:00"))
    return posted.replace(tzinfo=timezone.utc) if posted.tzinfo is None else posted.astimezone(timezone.utc)


def _iso_ms(d: datetime) -> str:
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (d.microsecond // 1000)


def _uuid(kind: str, key) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"stub:{kind}:{key}"))


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")


def _split_loc(location: str) -> tuple:
    """'City, Region, Country' / 'City, Country' / 'Remote' -> (city, region, country)."""
    parts = [p.strip() for p in (location or "").split(",") if p.strip()]
    if len(parts) >= 3:
        return parts[0], parts[1], parts[-1]
    if len(parts) == 2:
        return parts[0], "", parts[1]
    return (parts[0] if parts else ""), "", ""


def _body_html(title: str, company: str) -> str:
    return (f"<p>{html.escape(company)} is hiring a {html.escape(title)}.</p>"
            "<ul><li>Work with a small, friendly team</li><li>Ship something every week</li></ul>")


def _plain(markup: str) -> str:
    text = re.sub(r"</(p|li|div|h\d)>", "\n", markup)
    return re.sub(r"[ \t]*\n[ \t\n]*", "\n", re.sub(r"<[^>]+>", " ", text)).strip()


def gh_job(title: str = "Backend Engineer", location: str = "Bengaluru, India", posted=None, id=None, *,
           company: str = "Acme", board: str = "acme", description: Optional[str] = None) -> dict:
    """Greenhouse (boards-api): a job as `?content=true` and the one-job endpoint return it; `content` is entity-ESCAPED HTML."""
    jid, when = id if id is not None else _auto_id(), _when(posted)
    return {
        "absolute_url": f"https://boards.greenhouse.io/{board}/jobs/{jid}",
        "data_compliance": [{"type": "gdpr", "requires_consent": False, "requires_processing_consent": False,
                             "requires_retention_consent": False, "retention_period": None,
                             "demographic_data_consent_applies": False}],
        "internal_job_id": int(jid) * 7 + 3 if isinstance(jid, int) else 4000001,
        "location": {"name": location},
        "metadata": [{"id": 825338, "name": "Cost Center", "value": "1200 - Engineering", "value_type": "single_select"}],
        "id": jid,
        "updated_at": (when + timedelta(hours=5)).isoformat(timespec="seconds"),
        "requisition_id": f"REQ-{jid}",
        "title": title,
        "company_name": company,
        "first_published": when.isoformat(timespec="seconds"),
        "language": "en",
        "application_deadline": None,
        "content": html.escape(description if description is not None else _body_html(title, company)),
        "departments": [{"id": 70660, "name": "Engineering", "child_ids": [], "parent_id": 29066}],
        "offices": [{"id": 19994, "name": location, "location": location, "child_ids": [], "parent_id": 48752}],
        "ai_disclaimer": None, "include_ai_disclaimer": None, "ai_opt_out_request_url": None,
    }


def lever_job(title: str = "Backend Engineer", location: str = "Bengaluru, India", posted=None, id=None, *,
              board: str = "acme", company: str = "Acme", description: Optional[str] = None, workplace: str = "onsite",
              country: str = "IN") -> dict:
    """Lever: `createdAt` is epoch MILLISECONDS; the description comes in HTML and plain forms; `workplaceType` is lower-case."""
    jid = id if id is not None else _uuid("lever", _auto_id())
    markup = description if description is not None else _body_html(title, company)
    plain = _plain(markup)
    return {
        "additionalPlain": f"{company} is an equal opportunity employer.",
        "additional": f"<div>{html.escape(company)} is an equal opportunity employer.</div>",
        "categories": {"commitment": "Full-time", "department": "Engineering", "location": location, "team": "Platform",
                       "allLocations": [location]},
        "createdAt": int(_when(posted).timestamp() * 1000),
        "descriptionPlain": plain,
        "description": f"<div>{markup}</div>",
        "id": jid,
        "lists": [{"text": "What you'll do", "content": "<li>Build things people use</li><li>Review code</li>"}],
        "text": title,
        "country": country,
        "workplaceType": workplace,
        "opening": "", "openingPlain": "",
        "descriptionBody": f"<div>{markup}</div>",
        "descriptionBodyPlain": plain,
        "hostedUrl": f"https://jobs.lever.co/{board}/{jid}",
        "applyUrl": f"https://jobs.lever.co/{board}/{jid}/apply",
    }


def ashby_job(title: str = "Backend Engineer", location: str = "Bengaluru, India", posted=None, id=None, *,
              board: str = "acme", company: str = "Acme", description: Optional[str] = None,
              remote: Optional[bool] = None, workplace: Optional[str] = _AUTO) -> dict:
    """Ashby (posting-api): `publishedAt` is ISO with milliseconds; `isRemote` is true/false/null; plain and HTML descriptions.

    `workplace` ("Remote" | "Hybrid" | "OnSite" | None) sets `workplaceType` and derives `isRemote` the way the real data pairs
    them: Remote -> true, Hybrid -> true (509 of 815 real jobs), OnSite -> false, None -> null (an explicit `remote=` wins).
    Left out, the older behaviour stays: `workplaceType` is "Remote" when `remote` is true, else null."""
    jid = id if id is not None else _uuid("ashby", _auto_id())
    markup = description if description is not None else _body_html(title, company)
    city, region, country = _split_loc(location)
    if workplace is _AUTO:
        workplace = "Remote" if remote else None
    else:
        remote = remote if remote is not None else {"Remote": True, "Hybrid": True, "OnSite": False}.get(workplace)
    return {
        "id": jid, "title": title, "department": "Engineering", "team": "Platform", "employmentType": "FullTime",
        "location": location, "secondaryLocations": [],
        "publishedAt": _when(posted).isoformat(timespec="milliseconds"),
        "isListed": True, "isRemote": remote,
        "workplaceType": workplace,
        "address": {"postalAddress": {"addressRegion": region, "addressCountry": country or city, "addressLocality": city}},
        "jobUrl": f"https://jobs.ashbyhq.com/{board}/{jid}",
        "applyUrl": f"https://jobs.ashbyhq.com/{board}/{jid}/application",
        "descriptionHtml": markup,
        "descriptionPlain": _plain(markup),
    }


def workable_job(title: str = "Backend Engineer", location: str = "Bengaluru, India", posted=None, id=None, *,
                 remote: bool = False, workplace: Optional[str] = None) -> dict:
    """Workable v3 LIST shape (no description; the v2 detail call has it). `id` is the shortcode the detail path takes."""
    shortcode = id if id is not None else "%010X" % (0xA00000000 + _auto_id())
    city, region, country = _split_loc(location)
    place = {"city": city, "country": country, "countryCode": _CC.get(country.lower(), "").upper(), "region": region or None}
    return {
        "id": int(hashlib.md5(str(shortcode).encode()).hexdigest()[:7], 16),
        "shortcode": shortcode, "title": title, "remote": remote,
        "location": dict(place), "locations": [dict(place, hidden=False)],
        "state": "published", "isInternal": False, "code": "",
        "published": _when(posted).strftime("%Y-%m-%dT00:00:00.000Z"),
        "type": "full", "language": "en", "department": ["Engineering"],
        "accountUid": _uuid("workable-account", "acme"), "approvalStatus": "approved",
        "workplace": workplace or ("remote" if remote else "on_site"),
    }


def workable_detail(job: Optional[dict] = None, *, description: Optional[str] = None, requirements: Optional[str] = None,
                    benefits: Optional[str] = None, **job_kw) -> dict:
    """Workable v2 DETAIL: the list shape plus HTML `description`, `requirements`, `benefits`. Pass a list job, or the workable_job keywords."""
    base = {k: copy.deepcopy(v) for k, v in (job if job is not None else workable_job(**job_kw)).items() if k != "_detail"}
    base["description"] = description if description is not None else _body_html(base["title"], "Acme")
    base["requirements"] = requirements if requirements is not None else "<p>3+ years of relevant experience.</p>"
    base["benefits"] = benefits if benefits is not None else "<p>Flexible hours and a learning budget.</p>"
    return base


def sr_job(title: str = "Backend Engineer", location: str = "Bengaluru, Karnataka, India", posted=None, id=None, *,
           company: str = "Acme", board: str = "Acme", country: Optional[str] = None, remote: bool = False) -> dict:
    """SmartRecruiters LIST shape (no description; the detail call has `jobAd.sections`). `country` is a lower-case ISO code."""
    pid = str(id if id is not None else 744000000000000 + _auto_id())
    city, region, country_name = _split_loc(location)
    code = (country or _CC.get(country_name.lower(), "")).lower()
    return {
        "id": pid, "name": title, "uuid": _uuid("sr", pid), "jobAdId": _uuid("sr-ad", pid), "defaultJobAd": True,
        "refNumber": f"REF{pid[-6:]}", "company": {"identifier": board, "name": company},
        "releasedDate": _iso_ms(_when(posted)),
        "location": {"city": city, "region": region, "country": code, "address": "1 Example Street", "remote": remote,
                     "hybrid": False, "latitude": "12.9715987", "longitude": "77.5945627", "fullLocation": location},
        "industry": {"id": "internet", "label": "Internet"}, "department": {},
        "function": {"id": "information_technology", "label": "Information Technology"},
        "typeOfEmployment": {"id": "permanent", "label": "Full-time"},
        "experienceLevel": {"id": "mid_senior_level", "label": "Mid-Senior Level"},
        "customField": [{"fieldId": "5abe2a4ab60a5e486a2d9b1d", "fieldLabel": "Contract type",
                         "valueId": "1e46492b-d24f-460c-b095-38cf08a778d8", "valueLabel": "Unlimited"}],
        "visibility": "PUBLIC", "ref": f"https://api.smartrecruiters.com/v1/companies/{board}",
        "language": {"code": "en", "label": "English", "labelNative": "English"},
    }


def sr_detail(job: Optional[dict] = None, *, description: Optional[str] = None, qualifications: Optional[str] = None,
              additional: Optional[str] = None, company_description: Optional[str] = None, **job_kw) -> dict:
    """SmartRecruiters DETAIL: list fields plus `postingUrl`, `applyUrl` and `jobAd.sections` (HTML). Pass a list job, or the sr_job keywords."""
    base = job if job is not None else sr_job(**job_kw)
    keep = ("id", "name", "uuid", "jobAdId", "defaultJobAd", "refNumber", "company", "location", "customField",
            "releasedDate", "visibility", "industry", "function", "experienceLevel", "typeOfEmployment", "language")
    d = {k: copy.deepcopy(base[k]) for k in keep if k in base}
    ident, pid = base["company"]["identifier"], base["id"]
    url = f"https://jobs.smartrecruiters.com/{ident}/{pid}-{_slugify(base['name'])}"
    d.update({
        "jobId": _uuid("sr-job", pid), "creator": {"name": "", "avatarUrl": ""},
        "postingUrl": url, "applyUrl": url + "?oga=true", "referralUrl": "https://jobs.smartrecruiters.com/external-referral",
        "jobAd": {"sections": {
            "companyDescription": {"title": "Company Description", "text": company_description or f"<p>{html.escape(base['company']['name'])} builds useful software.</p>"},
            "jobDescription": {"title": "Job Description", "text": description or _body_html(base["name"], base["company"]["name"])},
            "qualifications": {"title": "Qualifications", "text": qualifications or "<p>3+ years of relevant experience.</p>"},
            "additionalInformation": {"title": "Additional Information", "text": additional or "<p>We value diverse teams.</p>"}}},
        "active": True,
    })
    return d


def remotive_job(title: str = "Backend Engineer", location: str = "Worldwide", posted=None, id=None, *,
                 company: str = "Acme", category: str = "Software Development", salary: str = "",
                 description: Optional[str] = None) -> dict:
    """Remotive: `publication_date` is a naive ISO timestamp; `candidate_required_location` is free text; HTML description."""
    jid = id if id is not None else _auto_id()
    return {
        "id": jid, "url": f"https://remotive.com/remote-jobs/{_slugify(category)}/{_slugify(title)}-{jid}",
        "title": title, "company_name": company, "company_logo": f"https://remotive.com/job/{jid}/logo",
        "category": category, "tags": ["python", "api"], "job_type": "full_time",
        "publication_date": _when(posted).strftime("%Y-%m-%dT%H:%M:%S"),
        "candidate_required_location": location, "salary": salary,
        "description": description if description is not None else _body_html(title, company),
        "company_logo_url": f"https://remotive.com/job/{jid}/logo",
    }


def remoteok_job(title: str = "Backend Engineer", location: str = "Worldwide", posted=None, id=None, *,
                 company: str = "Acme", description: Optional[str] = None, salary_min: int = 0, salary_max: int = 0,
                 tags: Optional[list] = None) -> dict:
    """RemoteOK: the id is a STRING; `epoch` (seconds) plus `date`; the URL host is spelled `remoteOK.com`."""
    jid = str(id if id is not None else 1100000 + _auto_id())
    when = _when(posted)
    slug = f"remote-{_slugify(title)}-{_slugify(company)}-{jid}"
    return {
        "slug": slug, "id": jid, "epoch": int(when.timestamp()), "date": when.isoformat(timespec="seconds"),
        "company": company, "company_logo": "", "position": title,
        "tags": tags if tags is not None else ["dev", "engineer"],
        "description": description if description is not None else _body_html(title, company),
        "location": location, "apply_url": f"https://remoteOK.com/remote-jobs/{slug}",
        "salary_min": salary_min, "salary_max": salary_max, "logo": "",
        "url": f"https://remoteOK.com/remote-jobs/{slug}",
    }


def himalayas_job(title: str = "Backend Engineer", location="Worldwide", posted=None, id=None, *,
                  company: str = "Acme", description: Optional[str] = None) -> dict:
    """Himalayas: `pubDate` is epoch seconds; `locationRestrictions` is a list (empty = worldwide); no id, only `guid`."""
    jid = id if id is not None else _auto_id()
    when = _when(posted)
    link = f"https://himalayas.app/companies/{_slugify(company)}/jobs/{_slugify(title)}-{jid}"
    restrictions = list(location) if isinstance(location, (list, tuple)) else ([location] if location and location != "Worldwide" else [])
    return {
        "title": title, "excerpt": f"{company} is hiring a {title}.", "companyName": company,
        "companySlug": _slugify(company), "companyLogo": "", "employmentType": "Full Time",
        "minSalary": 65000, "maxSalary": 90000, "salaryPeriod": "annual", "seniority": ["Mid-level"], "currency": "USD",
        "locationRestrictions": restrictions, "timezoneRestrictions": [],
        "categories": ["Software-Engineer"], "parentCategories": ["Engineering"],
        "description": description if description is not None else _body_html(title, company),
        "pubDate": int(when.timestamp()), "expiryDate": int((when + timedelta(days=60)).timestamp()),
        "applicationLink": link, "guid": link,
    }


def arbeitnow_job(title: str = "Backend Engineer", location: str = "Berlin, Germany", posted=None, id=None, *,
                  company: str = "Acme", remote: bool = False, description: Optional[str] = None) -> dict:
    """Arbeitnow: `slug` is the id; `created_at` is epoch seconds; `tags` and `job_types` are lists; HTML description."""
    slug = id if id is not None else f"{_slugify(title)}-{_slugify(company)}-{_auto_id()}"
    return {
        "slug": slug, "company_name": company, "title": title,
        "description": description if description is not None else _body_html(title, company),
        "remote": remote, "url": f"https://www.arbeitnow.com/jobs/companies/{_slugify(company)}/{slug}",
        "tags": ["Software Development"], "job_types": ["full time"], "location": location,
        "created_at": int(_when(posted).timestamp()),
    }


def wwr_item(title: str = "Backend Engineer", location: str = "Anywhere in the World", posted=None, id=None, *,
             company: str = "Acme", category: str = "Back-End Programming", description: Optional[str] = None) -> dict:
    """One We Work Remotely RSS <item> as a dict of its child tags (values UNescaped; wwr_rss escapes them).
    As in the real feed the title is `Company: Role`."""
    slug = id if id is not None else f"{_slugify(company)}-{_slugify(title)}-{_auto_id()}"
    link = f"https://weworkremotely.com/remote-jobs/{slug}"
    return {"title": f"{company}: {title}", "region": location, "category": category,
            "description": description if description is not None else _body_html(title, company),
            "pubDate": format_datetime(_when(posted)), "guid": link, "link": link}


def wwr_rss(items: list, ttl: int = 60) -> str:
    """The RSS 2.0 document the feed serves for a list of wwr_item dicts."""
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:media="http://search.yahoo.com/mrss">',
           "  <channel>", "    <title>We Work Remotely: Remote jobs in design, programming, marketing and more</title>",
           "    <link>https://weworkremotely.com/categories/remote-programming-jobs.rss</link>",
           "    <description>We Work Remotely: Remote jobs in design, programming, marketing and more</description>",
           "    <language>en-US</language>", f"    <ttl>{ttl}</ttl>"]
    for it in items:
        out.append("    <item>")
        for tag in ("title", "region", "category", "description", "pubDate", "guid", "link"):
            if tag in it:
                out.append(f"      <{tag}>{_xml_escape(str(it[tag]))}</{tag}>")
        out.append("    </item>")
    out += ["  </channel>", "</rss>", ""]
    return "\n".join(out)


# ----------------------------------------------------------------------------- StubBoards

BOARD_SYSTEMS = ("greenhouse", "lever", "lever_eu", "ashby", "workable", "smartrecruiters")
FEED_SYSTEMS = ("remotive", "remoteok", "weworkremotely", "himalayas", "arbeitnow")
ARBEITNOW_PAGE = 325
HIMALAYAS_PAGE = 20
HUGE_BYTES = 34_000_000                       # a reply this large is over the adapter's 33 MB cap
_PAD = "@@PAD@@"                              # where the huge mode splices its filler
_PAD_FIELD = {"greenhouse": "title", "lever": "text", "lever_eu": "text", "ashby": "title", "workable": "title",
              "smartrecruiters": "name", "remotive": "title", "remoteok": "position", "weworkremotely": "title",
              "himalayas": "title", "arbeitnow": "title"}
_DEFAULT_JOBS = [("Backend Engineer", "Bengaluru, Karnataka, India", 1), ("Marketing Manager", "London, United Kingdom", 3),
                 ("Office Administrator", "Berlin, Germany", 20)]       # one clearly relevant to "backend engineer", two not
_FEED_LOCATIONS = ["Worldwide", "Europe", "USA Only"]
_BOTTOMLESS_TITLES = ["Backend Engineer", "Product Designer", "Account Executive", "Data Analyst"]


def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clean(post: dict) -> dict:
    """A posting as served in a list: without the private `_detail` override a test may attach for the second-call systems."""
    return {k: v for k, v in post.items() if k != "_detail"}


def _cursor(n: int) -> str:
    return base64.urlsafe_b64encode(f"stub|{n}".encode()).decode().rstrip("=")


def _uncursor(text: str) -> int:
    kind, _, n = base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)).decode().partition("|")
    if kind != "stub":
        raise ValueError(text)
    return int(n)


def _reply(payload, status: int = 200) -> tuple:
    return status, "application/json", json.dumps(payload).encode()


class StubBoards(_Server):
    """The ten public job APIs of spec section 2, served under `{url}/{system}/...`:

      greenhouse   GET  /greenhouse/v1/boards/{slug}/jobs[?content=true]   and   .../jobs/{id}
      lever        GET  /lever/v0/postings/{slug}          (lever_eu: the same under /lever_eu/, the EU host)
      ashby        GET  /ashby/posting-api/job-board/{slug}
      workable     POST /workable/api/v3/accounts/{slug}/jobs   (list)   and   GET /workable/api/v2/accounts/{slug}/jobs/{shortcode}
      smartrecruiters  GET /smartrecruiters/v1/companies/{slug}/postings[?limit&offset&q&country]   and   .../postings/{id}
      feeds        GET  /remotive/api/remote-jobs   /remoteok/api   /weworkremotely/rss (also its real path /categories/remote-programming-jobs.rss)   /himalayas/jobs/api   /arbeitnow/api/job-board-api

    `data[(system, slug)]` is a list of postings in that API's own raw shape (use the builders above; feeds use slug "").
    With `data=None` every system and slug serves a small default (one backend job, two others). Once `data` is given,
    a key it lacks is an unknown company: 404 for greenhouse/lever/ashby/workable, an empty list for SmartRecruiters
    and the feeds (as the real APIs answer). For Workable and SmartRecruiters the detail reply is derived from the list
    posting; a posting may carry a `_detail` dict to be served verbatim instead (it never appears in lists).

    `modes[(system, slug)]`: ok | empty | not_found (404) | rate_limited (429) | server_error (500) | garbage (HTTP 200,
    not JSON) | huge (valid JSON of more than 33 MB, streamed) | slow (answers after `slow_seconds`, default 3, and
    stops early if the stub shuts down) | eu_only (lever only: the plain /lever path is a 404, /lever_eu serves).

    `bottomless={"himalayas": hours, "arbeitnow": hours}` replaces that feed with endless newest-first pages whose
    posting dates step back by that many hours per job. Himalayas pages are capped at 20 jobs (cursor via
    `nextCursor`), Arbeitnow pages hold 325 (`page=N`, `links.next`).

    Counters, all lock-protected: `requests`, `by_path` (path without the query string) and `log`, a list of
    (method, path, query_string) in arrival order; `post_bodies` keeps the parsed JSON body of each POST."""

    def __init__(self, data: Optional[dict] = None, modes: Optional[dict] = None, bottomless: Optional[dict] = None):
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                self.server.stub._serve(self, "GET")

            def do_POST(self):
                self.server.stub._serve(self, "POST")
        super().__init__(H)
        self.data = dict(data) if data is not None else None
        self.modes = dict(modes or {})
        self.bottomless = dict(bottomless or {})
        self.slow_seconds = 3.0
        self.requests = 0
        self.by_path: dict[str, int] = {}
        self.log: list[tuple[str, str, str]] = []
        self.post_bodies: list = []
        self._stop = threading.Event()               # set on exit so a sleeping "slow" handler wakes up
        self._t0 = int(time.time())                  # the bottomless feeds count their dates back from here
        self._defaults: dict = {}

    @property
    def url(self) -> str:
        return self.base

    def __exit__(self, *exc):
        self._stop.set()
        return super().__exit__(*exc)

    # -- routing ------------------------------------------------------------------------------------------------

    @staticmethod
    def _match(method: str, path: str):
        """(system, slug, kind, arg) for a known route, else None. kind: list | detail | v3_detail."""
        parts = [unquote(p) for p in path.split("/")[1:]]
        if parts and parts[-1] == "":
            parts.pop()
        if not parts:
            return None
        system, rest = parts[0], parts[1:]
        if system == "greenhouse" and rest[:2] == ["v1", "boards"] and method == "GET":
            if len(rest) == 4 and rest[3] == "jobs":
                return system, rest[2], "list", None
            if len(rest) == 5 and rest[3] == "jobs":
                return system, rest[2], "detail", rest[4]
        elif system in ("lever", "lever_eu") and rest[:2] == ["v0", "postings"] and method == "GET":
            if len(rest) == 3:
                return system, rest[2], "list", None
            if len(rest) == 4:
                return system, rest[2], "detail", rest[3]
        elif system == "ashby" and rest[:2] == ["posting-api", "job-board"] and len(rest) == 3 and method == "GET":
            return system, rest[2], "list", None
        elif system == "workable" and rest[:1] == ["api"] and len(rest) >= 5 and rest[2] == "accounts" and rest[4] == "jobs":
            version, slug = rest[1], rest[3]
            if version == "v3" and len(rest) == 5 and method == "POST":
                return system, slug, "list", None
            if version == "v3" and len(rest) == 6 and method == "GET":
                return system, slug, "v3_detail", rest[5]            # the real v3 detail path is a 404
            if version == "v2" and len(rest) == 6 and method == "GET":
                return system, slug, "detail", rest[5]
        elif system == "smartrecruiters" and rest[:2] == ["v1", "companies"] and method == "GET":
            if len(rest) == 4 and rest[3] == "postings":
                return system, rest[2], "list", None
            if len(rest) == 5 and rest[3] == "postings":
                return system, rest[2], "detail", rest[4]
        elif method == "GET" and (system, rest) in (("remotive", ["api", "remote-jobs"]), ("remoteok", ["api"]),
                                                     ("weworkremotely", ["rss"]), ("himalayas", ["jobs", "api"]),
                                                     ("weworkremotely", ["categories", "remote-programming-jobs.rss"]),   # the real feed path
                                                     ("arbeitnow", ["api", "job-board-api"])):
            return system, "", "list", None
        return None

    def _serve(self, h: BaseHTTPRequestHandler, method: str) -> None:
        try:
            self._serve_inner(h, method)
        except (BrokenPipeError, ConnectionResetError):
            pass                                     # the client gave up (a timeout test, or the size cap)

    def _serve_inner(self, h: BaseHTTPRequestHandler, method: str) -> None:
        path, _, query = h.path.partition("?")
        body = None
        if method == "POST":
            raw = h.rfile.read(int(h.headers.get("Content-Length", 0)))
            try:
                body = json.loads(raw or b"{}")
            except ValueError:
                body = None
        with self.lock:
            self.requests += 1
            self.by_path[path] = self.by_path.get(path, 0) + 1
            self.log.append((method, path, query))
            if method == "POST":
                self.post_bodies.append(body)
        route = self._match(method, path)
        if route is None:
            return _json(h, 404, {"error": "stub: no such route"})
        system, slug, kind, arg = route
        if kind == "v3_detail":
            return self._send(h, 404, "text/plain", b"Not Found")
        mode = self.modes.get((system, slug), "ok")
        if mode == "eu_only" and system == "lever":
            return _json(h, 404, {"ok": False, "error": "Document not found"})
        if mode == "not_found":
            return _json(h, 404, {"error": "stub: not found"})
        if mode == "rate_limited":
            return _json(h, 429, {"error": "stub: rate limited"})
        if mode == "server_error":
            return _json(h, 500, {"error": "stub: server error"})
        if mode == "garbage":
            return self._send(h, 200, "text/html", b"<!doctype html><html><body>Service temporarily unavailable. Please retry <<<")
        if mode == "slow" and self._stop.wait(self.slow_seconds):
            return                                   # shutting down: drop the connection
        q = {k: v[0] for k, v in parse_qs(query, keep_blank_values=True).items()}
        plan = self._plan(system, slug, kind, arg, q, body if isinstance(body, dict) else {}, empty=(mode == "empty"))
        if plan is None:
            return _json(h, 404, {"error": "stub: unknown company"})
        selected, render = plan
        if mode == "huge":
            return self._send_huge(h, system, slug, selected, render)
        status, ctype, payload = render(selected)
        self._send(h, status, ctype, payload)

    @staticmethod
    def _send(h: BaseHTTPRequestHandler, status: int, ctype: str, payload: bytes) -> None:
        h.send_response(status)
        h.send_header("Content-Type", ctype)
        h.send_header("Content-Length", str(len(payload)))
        h.end_headers()
        h.wfile.write(payload)

    def _send_huge(self, h, system: str, slug: str, selected: list, render) -> None:
        """The normal reply, with one text field swollen to ~34 MB and streamed in chunks (so the stub itself stays small)."""
        post = dict((selected or self._default_postings(system, slug))[0])
        post[_PAD_FIELD[system]] = _PAD
        status, ctype, payload = render([post])
        head, _, tail = payload.decode().partition(_PAD)
        head, tail = head.encode(), tail.encode()
        h.send_response(status)
        h.send_header("Content-Type", ctype)
        h.send_header("Content-Length", str(len(head) + HUGE_BYTES + len(tail)))
        h.end_headers()
        h.wfile.write(head)
        chunk, left = b"x" * (1 << 20), HUGE_BYTES
        while left > 0 and not self._stop.is_set():
            h.wfile.write(chunk[:left])
            left -= len(chunk)
        h.wfile.write(tail)

    # -- data ---------------------------------------------------------------------------------------------------

    def _default_postings(self, system: str, slug: str) -> list:
        key = (system, slug)
        with self.lock:
            if key not in self._defaults:
                self._defaults[key] = self._make_defaults(system, slug)
            return self._defaults[key]

    @staticmethod
    def _make_defaults(system: str, slug: str) -> list:
        out = []
        for i, (title, location, days) in enumerate(_DEFAULT_JOBS):
            posted, board = _when(None) - timedelta(days=days - 1), slug or "acme"
            feed_loc = _FEED_LOCATIONS[i]
            if system == "greenhouse":
                out.append(gh_job(title, location, posted, 1001 + i, board=board))
            elif system in ("lever", "lever_eu"):
                out.append(lever_job(title, location, posted, _uuid("lever", f"{board}:{i}"), board=board))
            elif system == "ashby":
                out.append(ashby_job(title, location, posted, _uuid("ashby", f"{board}:{i}"), board=board))
            elif system == "workable":
                out.append(workable_job(title, location, posted, "%010X" % (0xACE000 + i)))
            elif system == "smartrecruiters":
                out.append(sr_job(title, location, posted, 744000000001000 + i, board=slug or "Acme"))
            elif system == "remotive":
                out.append(remotive_job(title, feed_loc, posted, 2001 + i))
            elif system == "remoteok":
                out.append(remoteok_job(title, feed_loc, posted, 3001 + i))
            elif system == "weworkremotely":
                out.append(wwr_item(title, ["Anywhere in the World", "Europe Only", "USA Only"][i], posted, f"acme-{_slugify(title)}"))
            elif system == "himalayas":
                out.append(himalayas_job(title, feed_loc, posted, 4001 + i))
            elif system == "arbeitnow":
                out.append(arbeitnow_job(title, location, posted, f"{_slugify(title)}-acme-{5001 + i}"))
        return out

    def _postings(self, system: str, slug: str, empty: bool) -> Optional[list]:
        """The raw postings for a (system, slug), or None for an unknown company that should be a 404."""
        if empty:
            return []
        if self.data is None:
            return list(self._default_postings(system, slug))
        for key in ((system, slug), ("lever", slug)) if system == "lever_eu" else ((system, slug),):
            if key in self.data:
                return list(self.data[key])
        return [] if system == "smartrecruiters" or system in FEED_SYSTEMS else None

    def _plan(self, system: str, slug: str, kind: str, arg, q: dict, body: dict, empty: bool):
        """(selected_postings, render) where render(list) -> (status, content_type, bytes); None means 404."""
        posts = self._postings(system, slug, empty)
        if posts is None:
            return None
        if kind == "detail":
            key = {"greenhouse": "id", "lever": "id", "lever_eu": "id", "workable": "shortcode", "smartrecruiters": "id"}[system]
            posts = [p for p in posts if str(p.get(key)) == arg]
            if system == "workable":
                one = lambda p: p.get("_detail") or workable_detail(p)
            elif system == "smartrecruiters":
                one = lambda p: p.get("_detail") or sr_detail(p)
            else:
                one = _clean
            return posts, (lambda sel: _reply(one(sel[0])) if sel else _reply({"error": "stub: no such posting"}, 404))
        if system == "greenhouse":
            with_content = q.get("content") == "true"
            return posts, (lambda sel: _reply({"jobs": [p if with_content else {k: v for k, v in p.items() if k != "content"} for p in sel],
                                               "meta": {"total": len(sel)}}))
        if system in ("lever", "lever_eu"):
            skip, limit = _int(q.get("skip"), 0), _int(q.get("limit"), 0)
            return posts[skip:skip + limit if limit else None], (lambda sel: _reply(sel))
        if system == "ashby":
            return posts, (lambda sel: _reply({"jobs": sel, "apiVersion": "1"}))
        if system == "workable":
            word = str(body.get("query") or "").lower()
            sel = [_clean(p) for p in posts if word in str(p.get("title", "")).lower()]
            return sel, (lambda sel: _reply({"results": sel, "total": len(sel)}))
        if system == "smartrecruiters":
            return self._plan_sr(posts, q)
        if system == "remotive":
            word = q.get("search", "").lower()
            hits = [p for p in posts if word in (p["title"] + " " + p["company_name"]).lower()]
            limit = _int(q.get("limit"), 0)
            sel = hits[:limit] if limit > 0 else hits
            return sel, (lambda sel: _reply({"00-warning": "Stub of the Remotive API.",
                                             "0-legal-notice": "Stub: link back to the Remotive URL of each job and name Remotive as the source.",
                                             "job-count": len(sel), "total-job-count": len(hits), "jobs": sel}))
        if system == "remoteok":
            legal = {"last_updated": self._t0, "legal": "Stub: API Terms of Service: link back to the URL on Remote OK and mention Remote OK as a source."}
            return posts, (lambda sel: _reply([legal] + sel))
        if system == "weworkremotely":
            return posts, (lambda sel: (200, "application/rss+xml; charset=utf-8", wwr_rss(sel).encode()))
        if system == "himalayas":
            return self._plan_himalayas(posts, q, empty)
        return self._plan_arbeitnow(posts, q, empty)

    @staticmethod
    def _plan_sr(posts: list, q: dict):
        words, country = q.get("q", "").lower().split(), q.get("country", "").lower()
        hits = [_clean(p) for p in posts if all(w in p["name"].lower() for w in words)
                and (not country or p["location"]["country"].lower() == country)]
        offset, limit = max(0, _int(q.get("offset"), 0)), min(100, max(1, _int(q.get("limit"), 10)))
        return hits[offset:offset + limit], (lambda sel: _reply({"offset": offset, "limit": limit, "totalFound": len(hits), "content": sel}))

    def _bottomless_job(self, system: str, i: int, hours: float) -> dict:
        title, posted = _BOTTOMLESS_TITLES[i % len(_BOTTOMLESS_TITLES)], self._t0 - i * hours * 3600
        if system == "himalayas":
            return himalayas_job(f"{title}", "Worldwide", posted, f"gen-{i}")
        return arbeitnow_job(title, "Berlin, Germany", posted, f"gen-{_slugify(title)}-{i}")

    def _plan_himalayas(self, posts: list, q: dict, empty: bool):
        limit = min(HIMALAYAS_PAGE, max(1, _int(q.get("limit"), HIMALAYAS_PAGE)))
        try:
            start = _uncursor(q["cursor"]) if q.get("cursor") else max(0, _int(q.get("offset"), 0))
        except ValueError:
            return [], (lambda sel: _reply({"error": "invalid cursor"}, 400))
        hours = self.bottomless.get("himalayas")
        if hours is not None and not empty:
            page, total, more = [self._bottomless_job("himalayas", i, hours) for i in range(start, start + limit)], 118348, True
        else:
            page, total = posts[start:start + limit], len(posts)
            more = start + limit < total
        return page, (lambda sel: _reply({"comments": "Stub of the Himalayas API: cursor paging, pages capped at 20.",
                                          "updatedAt": self._t0, "offset": start, "limit": limit, "totalCount": total,
                                          "nextCursor": _cursor(start + limit) if more else None, "jobs": sel}))

    def _plan_arbeitnow(self, posts: list, q: dict, empty: bool):
        page_no = max(1, _int(q.get("page"), 1))
        start = (page_no - 1) * ARBEITNOW_PAGE
        hours = self.bottomless.get("arbeitnow")
        if hours is not None and not empty:
            page, more = [self._bottomless_job("arbeitnow", i, hours) for i in range(start, start + ARBEITNOW_PAGE)], True
        else:
            page = posts[start:start + ARBEITNOW_PAGE]
            more = start + ARBEITNOW_PAGE < len(posts)
        path = f"{self.base}/arbeitnow/api/job-board-api"
        return page, (lambda sel: _reply({
            "data": sel,
            "links": {"first": f"{path}?page=1", "last": None, "prev": f"{path}?page={page_no - 1}" if page_no > 1 else None,
                      "next": f"{path}?page={page_no + 1}" if more else None},
            "meta": {"current_page": page_no, "current_page_url": f"{path}?page={page_no}", "from": start + 1 if sel else None,
                     "path": path, "per_page": ARBEITNOW_PAGE, "to": start + len(sel) if sel else None,
                     "terms": "Stub of the Arbeitnow API: please do not abuse, link back.",
                     "info": "Jobs are ordered by the `created_at` timestamp, newest first. Use `?page=` to paginate."}}))
