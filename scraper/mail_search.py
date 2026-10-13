"""The search loop of the Gmail part (Phase 6, step 6c; docs/gmail.md).

One run: build Gmail searches from the applications that could get a reply, list the matching message ids, drop the ones already
looked at, open at most `batch` of the rest, decide for each what it is (matcher, rules, transition rules, flags) and record the
outcome. It writes SUGGESTIONS and sighting records, never an application's status, and it only ever reads Gmail.

Bounds, all hard: at most MAX_QUERIES searches of MAX_QUERY_CHARS each, MAX_PAGES pages of PAGE_SIZE ids per search, `batch`
messages opened, and a wall-clock deadline. Results are read newest first and the ones already looked at are skipped, so a run
goes as deep as it needs to find `batch` new emails (within MAX_PAGES), and a mailbox bigger than one run is finished over several.
A run that did not cover everything leaves the search cursor where it was, so the next run looks at the same window again (the
sightings table makes that harmless). The first search looks back `lookback_days`; with
`from_now` it only starts the clock.

The search is deliberately wide (it is a Gmail search, not a filter): the matcher decides, and anything it is not sure of is
recorded as unmatched or unclear, never guessed.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional, Sequence

import gmail_client
import mail_match as MM
import mail_rules as MR

# Senders of the mail an applicant-tracking system sends for a company (the company is then named in the text).
ATS_QUERY_DOMAINS = ("greenhouse.io", "greenhouse-mail.io", "lever.co", "myworkday.com", "myworkdayjobs.com", "ashbyhq.com", "smartrecruiters.com",
                     "workable.com", "workablemail.com", "icims.com", "bamboohr.com", "jobvite.com", "teamtailor.com", "recruitee.com",
                     "taleo.net", "successfactors.com", "personio.de", "breezy.hr", "applytojob.com", "pinpointhq.com", "comeet.co")
MAX_QUERY_CHARS = 900                     # of the 1000 the client allows (the date bounds are counted in)
MAX_QUERIES = 8                           # one for the ATS senders, the rest for company names
MAX_PAGES = 20                            # pages of PAGE_SIZE ids read per search window: 1000 results, mostly already-seen ones skipped
MAX_LIST_CALLS = 120                      # Gmail calls that list or date results, per run, in all
PAGE_SIZE = 50
OVERLAP = timedelta(days=1)               # each search starts a day before the last one began; the sightings table drops repeats
OVERLAP_FROM_NOW = timedelta(minutes=10)  # in "only mail from now on" mode the overlap is small: a day would read the day before it was switched on
DEADLINE_S = 240.0
CANDIDATE_LIMIT = 100                     # the most active applications first; every name fits in the searches (see build_queries)


# ---- the searches ---------------------------------------------------------------------------------------------

def query_name(company: Optional[str]) -> Optional[str]:
    """How a company is written in a search, or None when it should not be searched for by name (a placeholder, or too short or
    too generic to be worth a search: a precise match is the matcher's job, this only has to find the mail)."""
    forms = MM.name_forms(company)
    if not forms:
        return None
    form = forms[-1]                                  # the core name: without legal suffixes and filler words
    if not form.text_ok or len(form.joined) < 3:
        return None
    name = " ".join(form.words)
    name = re.sub(r"[\"{}()\\]", " ", name)
    name = " ".join(name.split())[:60]
    return name or None


def _name_terms(name: str) -> str:
    """The search terms for one company: it must be the SENDER or in the SUBJECT. A name that is merely mentioned in the body of an
    unrelated email (every Amazon order for a company called Amazon) does not match, which keeps a search from filling up with noise.
    The matcher gives a body mention one point and needs three, so mail found only by a body mention could never match anyway."""
    words = name.split()
    group = words[0] if len(words) == 1 else "(" + " ".join(words) + ")"
    return f"from:{group} subject:{group}"


def window(after: datetime, before: Optional[datetime] = None) -> str:
    """The date part of a search, in epoch seconds. Sent mail and drafts are always excluded."""
    return f"after:{int(after.timestamp())} " + (f"before:{int(before.timestamp())} " if before else "") + "-in:sent -in:drafts"


def build_queries(candidates: Sequence[MM.Candidate]) -> tuple:
    """The search bodies for one run, and what they leave out. -> ([body, ...], {"names": n searched, "skipped": n not searched}).

    A body is everything but the date window (`render` adds that). The first is the applicant-tracking senders (their mail names the
    company in the text), the others are company names as sender-or-subject terms, packed into as few searches as fit under
    MAX_QUERY_CHARS (counting the longest date window), most active application first. At most MAX_QUERIES in all; names that do not
    fit are skipped for this run and counted, never silently."""
    room = MAX_QUERY_CHARS - len(window(datetime(2026, 1, 1, tzinfo=timezone.utc), datetime(2026, 1, 1, tzinfo=timezone.utc))) - 4
    ats = "{" + " ".join(f"from:{d}" for d in ATS_QUERY_DOMAINS) + "}"
    names, seen = [], set()
    for c in candidates:
        n = query_name(c.company)
        if n and n not in seen:
            seen.add(n)
            names.append(n)
    chunks, current, size = [], [], 0
    for n in names:
        term = _name_terms(n)
        if current and size + len(term) + 1 > room:
            chunks.append(current)
            current, size = [], 0
        current.append(term)
        size += len(term) + 1
    if current:
        chunks.append(current)
    allowed = chunks[:MAX_QUERIES - 1]
    searched = sum(len(c) for c in allowed)
    return [ats] + ["{" + " ".join(c) + "}" for c in allowed], {"names": searched, "skipped": len(names) - searched}


def render(body: str, after: datetime, before: Optional[datetime] = None) -> str:
    return window(after, before) + " " + body


# ---- what one email becomes -----------------------------------------------------------------------------------

@dataclass
class Outcome:
    outcome: str                                      # suggested | noise | unmatched | unclear | failed
    job_id: Optional[int] = None
    suggestion: Optional[dict] = None
    note: str = ""
    match: Optional[MM.Match] = None
    classification: Optional[MR.Classification] = None


def process_message(message, candidates: Sequence[MM.Candidate]) -> Outcome:
    """Decide what an email is. Pure: no store, no network."""
    if message.received_at is None:
        return Outcome("failed", note="the email has no date")
    match = MM.match_message(message, candidates)
    if match.candidate is None:
        return Outcome("unmatched", note=match.reason, match=match)
    cand = match.candidate
    cls = MR.classify(message.subject, message.text)
    if cls.noise:
        return Outcome("noise", cand.job_id, note="an alert or newsletter", match=match, classification=cls)
    if cls.signal is None:
        return Outcome("unclear", cand.job_id, note="the wording does not decide it", match=match, classification=cls)
    proposal = MR.propose(cls.signal, cand.app_status, cand.job_status)
    if proposal.kind == "nochange":
        return Outcome("noise", cand.job_id, note=proposal.reason, match=match, classification=cls)
    flags = MR.assess(message, match, cls, cand.company)
    if proposal.kind == "conflict":
        flags = (flags + ["conflict"])[:10]
    elif cls.signal == "acknowledgement":
        flags = (flags + ["date_from_email"])[:10]        # the real submission date is unknown: the email's date is an upper bound
    confidence = MR.combined_confidence(match.confidence, cls.confidence, flags)
    suggestion = {
        "received_at": message.received_at.astimezone(timezone.utc).isoformat(), "from_domain": message.from_domain,
        "subject": message.subject, "excerpt": MR.make_excerpt(message.text, cls.evidence), "signal": cls.signal,
        "proposed_status": proposal.status, "status_at_proposal": proposal.current, "confidence": confidence, "source": "rules", "flags": flags}
    return Outcome("suggested", cand.job_id, suggestion, note=proposal.reason, match=match, classification=cls)


# ---- one run --------------------------------------------------------------------------------------------------

def _gather(client, store, bodies: Sequence[str], after: datetime, want: int, deadline: float) -> tuple:
    """The ids not yet looked at, newest first and without repeats, how many ids the searches listed, and whether every result of every
    search was read. Reading stops as soon as `want` new ids are found (a later run continues past the ones now recorded).

    A search lists at most MAX_PAGES pages at a time. When it fills up with more behind it, the date of its last result is read
    and the search continues from just before that date, so every result of a mailbox of any size is reached exactly once, oldest
    mail included, with no re-reading of the newest. If a date cannot move the search on (more than a window's worth of mail
    in one second) the search is reported incomplete rather than repeated. Gmail calls per run are capped."""
    new, listed_ids, complete, calls = [], set(), True, 0
    for body in bodies:
        hi = None
        while True:
            token, last, query = None, None, render(body, after, hi)
            for _page in range(MAX_PAGES):
                if time.monotonic() > deadline or calls >= MAX_LIST_CALLS:
                    return new, len(listed_ids), False
                found, token = client.list_messages(query, max_results=PAGE_SIZE, page_token=token)
                calls += 1
                page = [m["id"] for m in found if m["id"] not in listed_ids]
                listed_ids.update(page)
                new.extend(store.gmail_unseen(page))
                last = found[-1]["id"] if found else last
                if not token or len(new) >= want:
                    break
            if len(new) >= want:
                return new, len(listed_ids), False
            if not token:
                break                                          # this search is exhausted
            if time.monotonic() > deadline or calls >= MAX_LIST_CALLS:
                return new, len(listed_ids), False
            oldest = client.message_date(last) + timedelta(seconds=1)      # the search was full: carry on from just before its last result
            calls += 1
            if (hi is not None and oldest >= hi) or oldest <= after:
                complete = oldest <= after and complete        # nothing older than the window, or no progress possible
                break
            hi = oldest
    return new, len(listed_ids), complete


def run_search(client, store, *, now: Optional[datetime] = None, batch: int = 20, deadline_s: float = DEADLINE_S) -> dict:
    """One scheduled search. -> a summary: opened, suggested, noise, unmatched, unclear, failed, complete, stopped (why it ended
    early, or ""), and what the searches left out. LoginExpired and Overbroad are raised for the caller to record; every other
    Gmail problem ends the run with `stopped` set and nothing half-written."""
    now = now or datetime.now(timezone.utc)
    deadline = time.monotonic() + deadline_s
    summary = {"opened": 0, "suggested": 0, "noise": 0, "unmatched": 0, "unclear": 0, "failed": 0, "listed": 0, "complete": False, "stopped": "",
               "names_searched": 0, "names_skipped": 0}
    state = store.gmail_state_row()
    cursor = state["search_cursor"]
    if cursor is None and state["from_now"]:
        store.record_gmail_search(now)                          # "only mail from now on": the first run starts the clock and reads nothing
        return {**summary, "complete": True, "started_clock": True}
    overlap = OVERLAP_FROM_NOW if state["from_now"] else OVERLAP
    after = (cursor - overlap) if cursor else now - timedelta(days=int(state["lookback_days"]))
    candidates = [MM.Candidate.from_row(r) for r in store.gmail_candidates(CANDIDATE_LIMIT)]
    if not candidates:
        # nothing to look for yet: do NOT move the cursor, or the lookback window would be used up before the first application exists
        store.record_gmail_search(None)
        return {**summary, "complete": True, "note": "no application could get a reply yet"}
    bodies, info = build_queries(candidates)
    summary["names_searched"], summary["names_skipped"] = info["names"], info["skipped"]
    try:
        unseen, listed, complete = _gather(client, store, bodies, after, batch + 1, deadline)
    except gmail_client.RateLimited:
        store.record_gmail_search(None)
        return {**summary, "stopped": "Gmail is limiting requests: it will try again at the next run"}
    except (gmail_client.LoginExpired, gmail_client.Overbroad):
        raise
    except gmail_client.GmailError as e:
        store.record_gmail_search(None)
        return {**summary, "stopped": gmail_client.redact(e)}
    summary["listed"] = listed
    if len(unseen) > batch or info["skipped"]:
        complete = False                                       # more than this run opens, or names that did not fit a search
    for message_id in unseen[:batch]:
        if time.monotonic() > deadline:
            complete, summary["stopped"] = False, "ran out of time: the rest waits for the next run"
            break
        try:
            message = client.get_message(message_id)
        except gmail_client.MessageGone:
            store.record_gmail_result(message_id, None, "noise", None)
            summary["noise"] += 1
            continue
        except gmail_client.TooLarge:
            store.record_gmail_result(message_id, None, "failed", None)
            summary["failed"] += 1
            continue
        except gmail_client.RateLimited:
            complete, summary["stopped"] = False, "Gmail is limiting requests: it will try again at the next run"
            break
        except (gmail_client.LoginExpired, gmail_client.Overbroad):
            raise
        except gmail_client.GmailError as e:
            complete, summary["stopped"] = False, gmail_client.redact(e)
            break
        summary["opened"] += 1
        try:
            outcome = process_message(message, candidates)
        except Exception:                                      # an email the decision code cannot handle: recorded, never retried forever
            outcome = Outcome("failed", note="could not be read")
        try:
            stored = store.record_gmail_result(message.id, message.thread_id or None, outcome.outcome, outcome.job_id, outcome.suggestion)
        except Exception:
            if outcome.suggestion is None:
                raise                                          # the database itself is unwell: end the run, change nothing more
            # the store refused this suggestion (a value it cannot hold): keep the sighting as failed rather than meet it on every run
            stored = store.record_gmail_result(message.id, message.thread_id or None, "failed", None, None)
        summary[stored if stored in summary else outcome.outcome] += 1
    summary["complete"] = complete and not summary["stopped"]
    store.record_gmail_search(now if summary["complete"] else None)
    return summary
