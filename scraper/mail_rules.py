"""What does this email say, what could it change, and is it trustworthy? (Phase 6, step 6c; docs/gmail.md)

Three jobs, all deterministic and free (no model is called here):

  classify()   read the wording and name ONE signal: acknowledgement, assessment, interview, offer or rejection; or say it is
               noise (a job alert, a newsletter) or unclear (matched, but the rules cannot decide: step 6d asks Claude).
  propose()    the transition rules. Code, not the classifier, decides which status a signal may propose from the job's
               current state, and when it must not (a rejection after an offer is flagged, never proposed over it).
  assess()     flags for the reader: sender not verified by Google, company named but not the sender, free mail, a scam
               pattern, a link to a look-alike or shortener, text aimed at an AI. An offer with any of them is `phishing_risk`.

Wording lists follow the ones in MadsLorentzen/ai-job-search's /gmail-sync and career-ops-hq/career-ops (MIT; see
THIRD_PARTY_NOTICES.md). A bare "offer" is never a signal: "unable to offer" is a rejection.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

import mail_match as MM
import safety

SIGNALS = ("acknowledgement", "assessment", "interview", "offer", "rejection")
STATUS_FOR = {"acknowledgement": "applied", "assessment": "interview", "interview": "interview", "offer": "offer", "rejection": "rejected"}
SCAN_CHARS = 2000            # the part of the text read for wording: a reply says it near the top, a thread quoted below is noise
FINAL = ("rejected", "withdrawn")

_FLAGS = re.I


def _compile(*patterns: str) -> list:
    return [re.compile(p, _FLAGS) for p in patterns]


# (strong, weak) per signal. Strong: wording that is, in practice, only used to say this. Weak: wording that also appears in
# other kinds of mail, so it counts only together with more of it.
WORDING = {
    "rejection": (_compile(
        r"\bregret to inform\b", r"\bnot (?:been )?selected\b", r"\bhave not selected\b", r"\bnot be (?:moving|proceeding|going) forward\b",
        r"\b(?:won't|will not|can't|cannot|unable to|not able to) (?:be )?(?:able )?(?:move|moving|take|proceed|proceeding|progress) (?:you |your application )?(?:forward|further|with your)\b",
        r"\bnot (?:moving|proceeding|going) forward with your\b",
        r"\b(?:decided|chosen|opted) to (?:move|go|proceed|pursue|continue) (?:forward )?(?:with )?(?:other|another|different) (?:candidates?|applicants?)\b",
        r"\bdecided not to (?:proceed|move forward|continue|progress)\b", r"\b(?:position|role|vacancy|job) (?:has been|was|is now) (?:filled|closed|cancelled|canceled|put on hold)\b",
        r"\bno longer (?:considering|moving forward|being considered)\b", r"\bunable to offer you\b", r"\bwill not be (?:offering|proceeding)\b",
        r"\bother candidates whose (?:experience|skills|background|profiles?|qualifications)\b", r"\bmore closely (?:match|align)",
        r"\bunsuccessful\b", r"\bnot (?:be )?(?:progressing|pursuing)\b", r"\b(?:won't|will not|not|cannot|can't) (?:be )?taking (?:your application|you) (?:any )?further\b",
        r"\b(?:go|going|gone|went) with (?:someone|another|a different) (?:else|candidate|applicant)\b", r"\bunable to progress\b"),
        _compile(r"\bunfortunately\b", r"\bafter careful consideration\b", r"\bother candidates\b", r"\bwish you (?:all )?the best (?:in|with) your\b",
                 r"\bkeep your (?:resume|cv|details|application) on file\b", r"\bfuture opportunities\b", r"\bnot a (?:match|fit)\b")),
    "offer": (_compile(
        r"\bpleased to offer you (?:the |an? )?(?:position|role|job|opportunity|employment|offer)\b", r"\bpleased to extend (?:you )?(?:an?|the) (?:formal |verbal |written )?offer\b",
        r"\b(?:excited|delighted|happy|thrilled|glad) to (?:offer|extend) (?:you )?(?:the |an? )?(?:position|role|job|opportunity|employment|offer)\b", r"\bextend (?:you )?(?:an?|the) (?:formal |verbal |written )?offer\b",
        r"\boffer letter\b", r"\bletter of offer\b", r"\bjob offer\b", r"\boffer of employment\b", r"\bemployment offer\b", r"\bformal offer\b",
        r"\boffer (?:details|package)\b", r"\bplease (?:find|see) (?:attached |below )?(?:your|the) offer\b", r"\bwe(?:'d| would) like to offer you (?:the |an? )?(?:position|role|job|opportunity|employment)\b"),
        _compile(r"\bcongratulations\b", r"\bcompensation (?:package|details)\b", r"\bsalary of\b", r"\b(?:joining|start|starting) date\b", r"\bdate of joining\b",
                 r"\bsign (?:and return )?(?:the|your) (?:offer|attached)\b", r"\baccept (?:this|the|our) offer\b")),
    "interview": (_compile(
        r"\bschedule (?:an?|your|the) (?:interview|call|chat|conversation|meeting)\b", r"\b(?:like|love|want|wish) to (?:invite|schedule)\b",
        r"\binvite you (?:to|for) (?:an? |the )?(?:interview|call|chat|conversation|screen|meeting)\b", r"\binterview (?:invitation|invite|request|schedule|details|confirmation)\b",
        r"\b(?:phone|video|virtual|technical|panel|onsite|on-site|hiring manager) (?:screen|interview|call|round)\b", r"\b(?:first|second|third|final|next) interview\b",
        r"\b(?:next|first|second|third|final) (?:round|stage) (?:interview|of (?:the )?(?:interviews?|interview process|process))\b", r"\b(?:final|onsite|on-site|panel) round\b",
        r"\bset up a (?:time|call|chat|meeting|quick)\b", r"\bbook a (?:time|slot)\b", r"\b(?:select|choose|pick) a (?:time|slot|date)\b",
        r"\bcalendly\.com\b", r"\bgoodtime\.io\b", r"\bcal\.com\b", r"\bmove (?:you )?forward (?:to|with) (?:the )?(?:next|interview)\b", r"\bproceed(?:ing)? to (?:the )?(?:next|interview)\b"),
        _compile(r"\binterview\b", r"\bzoom\.us/", r"\bmeet\.google\.com/", r"\bteams\.microsoft\.com/", r"\brecruiter (?:call|screen)\b", r"\bnext round\b",
                 r"\b(?:like|love) to (?:speak|talk|chat|meet)\b", r"\byour availability\b")),
    "assessment": (_compile(
        r"\bonline assessment\b", r"\bcoding (?:challenge|assessment|test|exercise)\b", r"\btake[- ]home\b", r"\bhackerrank\b", r"\bcodility\b", r"\bcodesignal\b",
        r"\btestgorilla\b", r"\b(?:complete|take|start) (?:the|this|your|an?) (?:assessment|test|challenge|exercise)\b", r"\bskills? (?:assessment|test|evaluation)\b",
        r"\btechnical assessment\b", r"\bassessment link\b", r"\bhirevue\b", r"\bpymetrics\b", r"\bmettl\b", r"\bcase study\b"),
        _compile(r"\bassessment\b", r"\bevaluation\b")),
    "acknowledgement": (_compile(
        r"\bwe(?:'ve| have) received your application\b", r"\bthank(?:s| you) for applying\b", r"\bapplication (?:received|submitted|confirmation)\b",
        r"\bsuccessfully (?:submitted|applied)\b", r"\byour application (?:to|for|has been (?:received|submitted))\b", r"\bwe(?:'ve| have) got your application\b",
        r"\bconfirm(?:ing)? (?:the )?receipt of your application\b", r"\bthanks for your (?:application|submission)\b", r"\bthank you for submitting your application\b"),
        _compile(r"\bthank(?:s| you) for your interest\b", r"\bwe(?:'ll| will) be in touch\b", r"\b(?:will|shall) review your (?:application|profile|resume|cv|qualifications)\b",
                 r"\bunder review\b", r"\bapplication (?:status|update)\b")),
}

# Wording of mail that is about OTHER jobs. Strong: only alerts say it. Weak: also common in the footers of real replies (an
# applicant-tracking mail often ends with "view all jobs" or "subscribe to our newsletter"), so it never overrides a signal.
STRONG_NOISE = _compile(
    r"\bjob alerts?\b", r"\bjobs? (?:you may|that match|recommended|for you|matching)\b", r"\brecommended jobs?\b", r"\bnew jobs? (?:in|for|at|matching)\b",
    r"\bunsubscribe from (?:these |this )?(?:job )?(?:alerts?|emails?)\b", r"\bweekly (?:digest|job|roundup)\b", r"\bpeople also viewed\b",
    r"\bjobs? you might\b", r"\btop (?:jobs|picks)\b")
WEAK_NOISE = _compile(
    r"\bnewsletter\b", r"\bsimilar jobs?\b", r"\bcomplete your profile\b", r"\byour profile (?:was )?viewed\b", r"\bapply (?:now )?to these\b",
    r"\bview all jobs\b", r"\bmanage (?:your )?(?:job )?alerts?\b")


@dataclass
class Classification:
    signal: Optional[str]                 # one of SIGNALS, or None
    confidence: str = "low"               # high | medium | low
    evidence: list = field(default_factory=list)       # the wording that decided it (short)
    noise: bool = False                   # an alert or newsletter: no signal, nothing to propose
    unclear: bool = False                 # matched, but the rules cannot decide: for step 6d
    flags: list = field(default_factory=list)


def _clean(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return text


def _hits(patterns: list, text: str) -> list:
    out = []
    for p in patterns:
        m = p.search(text)
        if m:
            out.append(m.group(0).lower()[:40])
    return out


def classify(subject: str, text: str) -> Classification:
    """Name the signal in an email, or say it is noise or unclear. Reads the subject and the first SCAN_CHARS of the text."""
    subject = _clean(subject)
    body = _clean(text)[:SCAN_CHARS]
    full = subject + "\n" + body
    strong, weak = {}, {}
    for sig, (s, w) in WORDING.items():
        strong[sig], weak[sig] = _hits(s, full), _hits(w, full)
    alerts, footers = _hits(STRONG_NOISE, full), _hits(WEAK_NOISE, full)
    strong_sigs = [s for s in SIGNALS if strong[s]]

    if not strong_sigs and (alerts or footers):
        return Classification(None, "high", (alerts + footers)[:3], noise=True)
    # a subject that says "newsletter" (or similar) is a newsletter, whatever advice its body gives about interviews
    subject_footers = _hits(WEAK_NOISE, subject)
    if subject_footers and not [s for s in strong_sigs if _hits(WORDING[s][0], subject)]:
        return Classification(None, "high", subject_footers[:3], noise=True)
    # a job alert that mentions "interview" is noise, unless the subject itself carries the signal; footer wording never overrides one
    if alerts:
        in_subject = [s for s in strong_sigs if _hits(WORDING[s][0], subject)]
        if not in_subject:
            return Classification(None, "high", alerts[:3], noise=True)
        strong_sigs = in_subject

    if strong_sigs == ["acknowledgement"] and weak["rejection"]:
        # "Thank you for applying ... unfortunately ...": a thank-you template that goes on to say no. An acknowledgement never
        # carries a regret; if it reads like one, it is not filed as "applied".
        return Classification("rejection", "medium", weak["rejection"][:3], flags=["template_mixed_signals"])
    if len(strong_sigs) == 1:
        sig = strong_sigs[0]
        return Classification(sig, "high" if (_hits(WORDING[sig][0], subject) or len(strong[sig]) >= 2) else "medium", strong[sig][:3])
    if len(strong_sigs) > 1:
        both = set(strong_sigs)
        if both == {"acknowledgement", "rejection"}:
            # "Thank you for applying ... unfortunately we are not moving forward": the template of a rejection
            return Classification("rejection", "medium", strong["rejection"][:3], flags=["template_mixed_signals"])
        if both == {"interview", "assessment"}:
            sig = "assessment" if _hits(WORDING["assessment"][0], subject) else "interview"
            return Classification(sig, "medium", (strong["assessment"] + strong["interview"])[:3])
        if "acknowledgement" in both and len(both) == 2:
            other = next(s for s in both if s != "acknowledgement")
            return Classification(other, "medium", strong[other][:3], flags=["template_mixed_signals"])
        return Classification(None, "low", sum((strong[s] for s in strong_sigs), [])[:3], unclear=True, flags=["conflicting_wording"])

    # no strong wording: weak wording only counts in numbers, and one weak hint is for step 6d to judge
    if len(weak["rejection"]) >= 2:
        return Classification("rejection", "medium", weak["rejection"][:3])
    if len(weak["offer"]) >= 3 or ("congratulations" in weak["offer"] and any(w in weak["offer"] for w in ("compensation package", "compensation details", "salary of", "start date", "joining date", "date of joining", "starting date"))):
        return Classification("offer", "medium", weak["offer"][:3])
    if any(weak[s] for s in SIGNALS) and len(body.strip()) > 0:
        return Classification(None, "low", [w for s in SIGNALS for w in weak[s]][:3], unclear=True)
    return Classification(None, "low", [], unclear=bool(body.strip() or subject.strip()))


# ---- the transition rules (code decides, never the classifier) ----------------------------------------------------

@dataclass(frozen=True)
class Proposal:
    kind: str                       # propose | conflict | nochange
    status: Optional[str]           # the status to propose (propose and conflict), else None
    current: str                    # the application's status now ('none' when it has none)
    reason: str = ""


def propose(signal: str, app_status: Optional[str], job_status: Optional[str] = None) -> Proposal:
    """What a signal may do to an application in its current state. Nothing here ever reaches 'hired' or 'offer declined': the
    only statuses are applied, interview, offer and rejected, and a signal that contradicts a final status is a `conflict`
    (shown to a person, never accepted automatically) instead of an overwrite."""
    if signal not in STATUS_FOR:
        raise ValueError(f"unknown signal {signal!r}")
    cur = app_status or "none"
    if signal == "acknowledgement":
        if cur in ("none", "drafted", "approved"):
            return Proposal("propose", "applied", cur, "an acknowledgement shows the application was submitted")
        return Proposal("nochange", None, cur, "already submitted")
    if signal in ("interview", "assessment"):
        if cur in ("none", "drafted", "approved", "applied", "in_progress"):
            return Proposal("propose", "interview", cur)
        if cur in ("interview", "offer"):
            return Proposal("nochange", None, cur, "already at or past interview")
        return Proposal("conflict", "interview", cur, f"an interview signal for an application marked {cur}")
    if signal == "offer":
        if cur == "offer":
            return Proposal("nochange", None, cur, "already an offer")
        if cur in FINAL:
            return Proposal("conflict", "offer", cur, f"an offer signal for an application marked {cur}")
        return Proposal("propose", "offer", cur)
    if signal == "rejection":
        if cur in FINAL:
            return Proposal("nochange", None, cur, f"already {cur}")
        if cur == "offer":
            return Proposal("conflict", "rejected", cur, "a rejection signal for an application with an offer: never overwritten")
        return Proposal("propose", "rejected", cur)
    raise AssertionError("unreachable: every signal is handled above")


# ---- flags: is this email what it claims to be? -------------------------------------------------------------------

_URL = re.compile(r"https?://[^\s<>\"')\]]+", re.I)
_RISKY_FLAGS = ("unverified_sender", "sender_domain_not_the_company", "free_mail_sender", "display_name_only", "suspicious_link", "scam_pattern")
CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2, "none": -1}


def assess(message, match, classification: Classification, company: Optional[str]) -> list:
    """The reader-facing flags for one matched email. Reuses safety.py (scam wording, link hosts, instructions aimed at an AI)."""
    flags = list(match.flags) + list(classification.flags)
    if not message.sender_verified:
        flags.append("unverified_sender")
    text = (message.subject or "") + "\n" + (message.text or "")
    scam = [f for f in safety.scan_text(text) if f["severity"] >= 2]
    if scam:
        flags.append("scam_pattern")
    urls = list(getattr(message, "links", ()) or ()) or _URL.findall(message.text or "")[:50]
    for url in urls[:50]:
        info = safety.classify_host(url, company)
        bad = info["category"] in ("suspicious", "lookalike", "messaging", "form")
        # "acme.evil.example" contains the company's name but is not the company's: the registrable name must be it
        if info["category"] == "company_site" and not MM.domain_matches(company, info.get("host") or ""):
            bad = True
        if bad:
            flags.append("suspicious_link")
            break
    if any(f["code"] in ("PROMPT_INJECTION", "HIDDEN_CONTENT") for f in safety.injection_signals(message.text or "")):
        flags.append("injection_attempt")
    if getattr(message, "markup_doubt", False):
        flags.append("markup_doubt")                       # the HTML could not be read the way a person sees it (email_html)
    # an offer is the email a scammer most wants you to believe: any doubt about who sent it is called out
    if classification.signal == "offer" and any(f in flags for f in _RISKY_FLAGS):
        flags.append("phishing_risk")
    elif any(f in flags for f in ("scam_pattern", "suspicious_link")):
        flags.append("phishing_risk")
    seen, out = set(), []
    for f in flags:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out[:10]


def combined_confidence(match_confidence: str, class_confidence: str, flags: list) -> str:
    """The lower of the two, and `low` when the email looks like phishing."""
    level = min(CONFIDENCE_ORDER.get(match_confidence, 0), CONFIDENCE_ORDER.get(class_confidence, 0))
    if "unverified_sender" in flags:
        level = min(level, 1)                          # a forged From looks exactly like a real one: never high on its say-so
    if "phishing_risk" in flags or "markup_doubt" in flags or "role_mismatch" in flags:
        level = 0                                      # a role the email names that is not this job's: the reply may belong to another job
    return {0: "low", 1: "medium", 2: "high"}.get(max(level, 0), "low")


# ---- what is kept of the email ------------------------------------------------------------------------------------

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_BARE_LINK = re.compile(r"\b(?:www\.)?[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}(?:/\S*)", re.I)
_CODE = re.compile(r"\b(?=[A-Za-z0-9-]*\d)[A-Za-z0-9-]{4,}\b")
_YEAR = re.compile(r"^(?:19|20)\d\d$")
_PHONE = re.compile(r"\+?\d[\d\s().-]{5,}\d")


def _mask_code(m) -> str:
    return m.group(0) if _YEAR.match(m.group(0)) else "[code]"


def make_excerpt(text: str, evidence: list, limit: int = 400) -> str:
    """A short piece of the email for the person deciding: around the wording that decided it when there is any. Links (with or
    without a scheme), addresses, phone numbers and anything that looks like a code (a word with a digit in it, four characters or
    more: one-time codes, tokens, ids) are removed from the WHOLE text first, then the piece is cut, so no fragment can survive a
    cut. Never the whole message."""
    body = " ".join((text or "").split())
    body = _URL.sub("[link]", body)
    body = _BARE_LINK.sub("[link]", body)
    body = _EMAIL.sub("[email]", body)
    body = _PHONE.sub("[number]", body)
    body = _CODE.sub(_mask_code, body)
    start = 0
    for phrase in evidence or []:
        i = body.lower().find(str(phrase).lower())
        if i >= 0:
            start = max(0, i - limit // 3)
            break
    piece = body[start:start + limit]
    return (piece + "…") if len(piece) == limit and start + limit < len(body) else piece
