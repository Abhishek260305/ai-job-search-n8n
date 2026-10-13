"""Which of your applications is this email about? (Phase 6, step 6c; docs/gmail.md)

A wrong match files a rejection against the wrong job, so the rule is: match only on evidence, and when the evidence points
at two applications (or at none), say so and match nothing. Evidence, strongest first:

  domain   the sender's domain IS the company (careers.acme.co.uk for "Acme Corp")                         3
           (only its first distinctive word, as in delta.com for "Delta Dental", is worth 2)
  display  the company is the sender's display name ("Acme Recruiting <no-reply@greenhouse.io>")            2
  subject  the company is named in the subject                                                              2
  body     the company is named in the first part of the text (a name deep in a quoted thread is noise)     1
  title    the job title appears in the subject or the first part of the text                               +1

A match needs 3 or more. Names are matched as whole words after normalisation (a short name such as "HP" never matches inside
"PHP", and must be written the way it is stored), placeholder company names ("?", "Confidential", "Stealth") match nothing, and
names too generic to mean a company on their own ("Global", "Digital") count only through the domain.

Ideas adapted from career-ops-hq/career-ops (MIT): whole-word short names, placeholder guard; see THIRD_PARTY_NOTICES.md.
Pure functions, no I/O.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Optional, Sequence

import safety

# The sender domains of applicant-tracking systems and the mail they send for companies: the company is then named in the text,
# not in the domain. (A superset of safety.KNOWN_ATS: those are link hosts, these are also sender hosts.)
ATS_SENDER_DOMAINS = tuple(sorted(set(safety.KNOWN_ATS) | {
    "greenhouse-mail.io", "us.greenhouse-mail.io", "myworkday.com", "wd1.myworkday.com", "hire.lever.co", "eu.hire.lever.co",
    "applytojob.com", "jobs.ashbyhq.com", "talent.icims.com", "candidates.workablemail.com", "workablemail.com",
    "mail.smartrecruiters.com", "recruiterbox.com", "pinpointhq.com", "comeet.co", "hirebridge.com", "jazzhr.com",
    "applicantstack.com", "ultipro.com", "paylocity.com", "dayforcehcm.com", "brassring.com", "zohorecruitmail.com",
    "cloud.oracle.com"}))             # Oracle's recruiting system mails for its customers (JPMorgan and others) from *.cloud.oracle.com: not Oracle's own mail

# Free-mail hosts: a recruiter on one is possible (small firms, agencies) but is also what scammers use.
FREE_MAIL = frozenset({"gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "live.com", "yahoo.com", "yahoo.co.in", "yahoo.co.uk",
                       "aol.com", "icloud.com", "me.com", "proton.me", "protonmail.com", "mail.com", "gmx.com", "gmx.net", "zoho.com",
                       "rediffmail.com", "yandex.com", "qq.com", "163.com", "126.com"})

_LEGAL = {"inc", "incorporated", "llc", "llp", "lp", "ltd", "limited", "pvt", "private", "corp", "corporation", "co", "company",
          "gmbh", "ag", "sa", "sas", "bv", "plc", "pte", "pty", "oy", "ab", "srl", "kk", "the"}
_NOISE = _LEGAL | set(safety._COMPANY_NOISE) | {"holdings", "international"}
# A name that is only one of these says nothing about WHICH company: it counts through the sender's domain or not at all.
GENERIC = frozenset({"global", "digital", "tech", "technology", "technologies", "software", "systems", "solutions", "services", "group", "india",
                     "labs", "data", "cloud", "ai", "consulting", "international", "enterprises", "industries", "partners", "company",
                     "studio", "network", "networks", "online", "web", "mobile", "smart", "next", "first", "one", "new", "best", "top",
                     "recruitment", "staffing", "hr", "jobs", "careers", "talent", "people", "team", "dev", "apps", "app", "media"})
PLACEHOLDERS = frozenset({"unknown", "na", "n a", "none", "null", "confidential", "company confidential", "confidential company", "stealth",
                          "stealth startup", "stealth company", "various", "not disclosed", "undisclosed", "tbd", "anonymous", "hidden",
                          "freelance", "self employed", "client", "our client", "a client", "undisclosed client", "company", "employer",
                          "hiring company", "direct hire", "test", "xxx"})

_CJK = re.compile("[぀-ヿ㐀-䶿一-鿿가-힯]")
_SECOND_LEVEL = {"co", "com", "org", "net", "ac", "gov", "edu", "ne", "or", "go"}
BODY_WINDOW = 700          # characters of the text in which a company counts: a name deep in a quoted thread is noise
MIN_SCORE = 3
RUN_TOGETHER_MIN = 8       # a name of this many letters or more also matches when its words are written run together or spaced apart
SHORT_NAME = 3             # a name of this many letters or fewer must appear as written


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").casefold().replace("&", " and ")
    return " ".join(re.sub(r"[^\w]+", " ", text).replace("_", " ").split())


@dataclass(frozen=True)
class Form:
    """One way the company can be written: its words, whether it may match free text, and whether it must keep its case."""
    words: tuple
    text_ok: bool
    case_sensitive: bool
    original: str = ""

    @property
    def joined(self) -> str:
        return "".join(self.words)


def name_forms(company: Optional[str]) -> list[Form]:
    """The ways a stored company name can appear in an email, longest first. Empty for a placeholder or an empty name."""
    if not isinstance(company, str):
        return []
    original = unicodedata.normalize("NFKC", company).strip()
    if not any(ch.isalnum() for ch in original):
        return []                                                  # "?", "-", "—": punctuation is not a company (any script's letters are)
    words = _norm(original).split()
    if not words or " ".join(words) in PLACEHOLDERS:
        return []
    no_legal = [w for w in words if w not in _LEGAL] or words
    while len(no_legal) > 1 and no_legal[-1] == "and":
        no_legal.pop()                                             # "JPMorgan Chase & Co.": the "&" that joined the legal suffix goes with it
    core = [w for w in no_legal if w not in _NOISE] or no_legal
    forms: list[Form] = []
    for ws in (tuple(no_legal), tuple(core)):
        if ws and ws not in [f.words for f in forms]:
            joined = "".join(ws)
            if len(joined) < 2 and not _CJK.search(joined):
                continue
            generic = len(ws) == 1 and ws[0] in GENERIC
            short = len(joined) <= SHORT_NAME and not _CJK.search(joined)
            # the form as the company was written, for the case-sensitive test of a short name
            orig_words = tuple(w for w in re.split(r"[^\w]+", original) if w and w.casefold() in ws)
            forms.append(Form(ws, text_ok=not generic, case_sensitive=short, original=" ".join(orig_words) or " ".join(ws)))
    return forms


def in_text(form: Form, text: str) -> bool:
    """Is the company named in `text`? Whole words only; CJK names (no word separators) match as a substring; a name of
    three letters or fewer must appear exactly as it is stored ("HP" is not the word "hp", and never part of "PHP")."""
    if not form.text_ok or not text:
        return False
    if _CJK.search(form.joined):
        return form.joined in unicodedata.normalize("NFKC", text).casefold().replace(" ", "")
    if form.case_sensitive:
        pattern = r"(?<![^\W_])" + r"[\s\-]+".join(re.escape(w) for w in form.original.split()) + r"(?![^\W_])"
        return re.search(pattern, unicodedata.normalize("NFKC", text)) is not None
    normalised = _norm(text)
    if re.search(r"(?<![^\W_])" + r"[\s\-]+".join(re.escape(w) for w in form.words) + r"(?![^\W_])", normalised):
        return True
    if len(form.joined) >= RUN_TOGETHER_MIN:
        # a name written run together in one place and spaced in another ("JPMorganChase" stored, "JPMorgan Chase & Co." in the mail, or
        # the other way round): a run of up to three whole words that spells it. Only for long names: "Sun Shine" is not "Sunshine" for a
        # name of seven letters or fewer, but a name of eight or more letters spelled by neighbouring whole words is that name.
        tokens = normalised.split()
        for i in range(len(tokens)):
            for k in (1, 2, 3):
                run = "".join(tokens[i:i + k])
                if len(run) > len(form.joined):
                    break
                if run == form.joined:
                    return True
    return False


def registrable_label(domain: str) -> str:
    """'careers.acme.co.uk' -> 'acme'; 'jobs.acme.com' -> 'acme'. The label that names the owner (without a public-suffix list:
    the common two-part endings are handled, which is all a company match needs)."""
    labels = [l for l in (domain or "").lower().split(".") if l]
    if len(labels) < 2:
        return ""
    if len(labels) == 2 and labels[0] in _SECOND_LEVEL and len(labels[1]) == 2:
        return ""                                     # "co.uk" is an ending, not an owner
    if len(labels) >= 3 and labels[-2] in _SECOND_LEVEL and len(labels[-1]) == 2:
        return labels[-3]
    return labels[-2]


def is_ats(domain: str) -> bool:
    return any(domain == d or domain.endswith("." + d) for d in ATS_SENDER_DOMAINS)


# Endings a real company's mail domain usually has. A match on an unusual one (acme.xyz for "Acme") is not rejected (small companies use
# them) but is flagged and never high confidence: nothing here can tell who registered the domain.
COMMON_ENDINGS = frozenset({
    "com", "org", "net", "io", "co", "ai", "dev", "app", "tech", "edu", "gov", "in", "uk", "us", "ca", "au", "de", "fr", "nl", "se", "no",
    "dk", "fi", "es", "it", "ch", "at", "be", "ie", "pl", "pt", "sg", "jp", "kr", "cn", "hk", "nz", "za", "ae", "il", "br", "mx", "ar", "id",
    "my", "ph", "th", "vn", "eu", "me", "cloud", "bio", "health", "global"})


def domain_names(company: Optional[str]) -> tuple:
    """(full, partial): the owner labels a company's own mail domain can have. `full` is the whole name written together or with
    hyphens, with and without legal suffixes (acme, acmecorp, acme-corp: NOT a-c-m-e). `partial` is only the first distinctive word
    of a longer name (delta for Delta Dental): five letters or more and not a generic word."""
    if not isinstance(company, str):
        return set(), set()
    words = _norm(company).split()
    if not words or " ".join(words) in PLACEHOLDERS:
        return set(), set()
    no_legal = [w for w in words if w not in _LEGAL] or words
    core = [w for w in no_legal if w not in _NOISE] or no_legal
    full = set()
    for ws in (words, no_legal, core):
        full.add("".join(ws))
        full.add("-".join(ws))
    full = {f for f in full if len(f.replace("-", "")) >= 2}
    first = core[0] if core else ""
    partial = {first} if len(core) > 1 and len(first) >= 5 and first not in GENERIC else set()
    return full, partial - full


def domain_kind(company: Optional[str], domain: str) -> Optional[str]:
    """Is the sender's domain this company's? 'full' (3 points), 'partial' (2) or None. An applicant-tracking domain and a free-mail
    host are never a company's, whatever the company is called."""
    label = registrable_label(domain)
    if not label or is_ats(domain) or domain in FREE_MAIL:
        return None
    full, partial = domain_names(company)
    if label in full:
        return "full"
    return "partial" if label in partial else None


def domain_matches(company: Optional[str], domain: str) -> bool:
    return domain_kind(company, domain) == "full"


def _ending(domain: str) -> str:
    return (domain or "").rsplit(".", 1)[-1].lower()


# ---- candidates and the result --------------------------------------------------------------------------------

@dataclass(frozen=True)
class Candidate:
    job_id: int
    company: str
    title: str
    job_status: str
    app_status: Optional[str]            # None when the job has no application yet

    @classmethod
    def from_row(cls, row: Mapping) -> "Candidate":
        return cls(int(row["job_id"]), str(row.get("company") or ""), str(row.get("title") or ""), str(row.get("job_status") or ""),
                   row.get("app_status"))

    @property
    def submitted(self) -> bool:
        return self.app_status in ("applied", "in_progress", "interview", "offer", "rejected", "withdrawn")


@dataclass
class Match:
    candidate: Optional[Candidate]
    confidence: str = "none"                      # high | medium | none
    reason: str = ""                              # why nothing matched, in a line
    evidence: list = field(default_factory=list)  # ["domain", "subject", ...] for the winner
    sender_kind: str = "other"                    # company | ats | other (free mail is "other" with the flag)
    flags: list = field(default_factory=list)


def _title_in(title: str, text: str) -> bool:
    words = [w for w in _norm(title).split() if len(w) > 2 and w not in {"and", "the", "for", "with", "remote", "senior", "junior", "lead"}]
    if len(words) < 2:
        return False                                  # a one-word title ("Manager", "Developer") is ordinary prose: no point for it
    nt = _norm(text)
    phrase = r"(?<![^\W_])" + r"\s+".join(re.escape(w) for w in words[:3]) + r"(?![^\W_])"
    return re.search(phrase, nt) is not None or sum(1 for w in words if re.search(r"(?<![^\W_])" + re.escape(w) + r"(?![^\W_])", nt)) >= max(2, len(words) - 1)


# ---- does the email name a DIFFERENT role than the one matched? ----------------------------------------------------
# A company often has several open roles, and a reply about the one you applied for (that the pipeline never collected) can sit next to
# a shortlisted job of the same company. Matching on the company alone would then file the reply against the wrong job, so when the
# email names a role and the matched job's title does not cover it, the match is flagged `role_mismatch` (confidence low: the page asks
# for a second look). Only a role the email names is judged; an email that names none is not flagged. A flag costs one extra click, a
# missed one a status on the wrong job, but a check that cries wolf is clicked through: so wording around the role (process words,
# places, years, ids), spelling variants (Sr./Senior, SDE-2, Front-End) and levels written two ways are not differences.
_ROLE_WORDS = frozenset({
    "engineer", "developer", "manager", "analyst", "designer", "scientist", "architect", "consultant", "intern", "director", "specialist",
    "administrator", "officer", "programmer", "tester", "sdet", "associate", "executive", "recruiter", "representative", "writer",
    "researcher", "accountant", "technician", "coordinator", "strategist", "lead", "head", "advocate", "evangelist", "trainee", "apprentice",
    "dev", "swe", "sre", "devops", "pm", "ux", "ui", "agent", "assistant", "marketer", "nurse", "teacher"})
_PROCESS_WORDS = frozenset({
    "application", "applications", "applied", "applying", "apply", "received", "update", "updates", "status", "interview", "interviews",
    "invitation", "invite", "invited", "schedule", "scheduled", "scheduling", "next", "steps", "step", "regarding", "re", "fwd", "fw", "your",
    "our", "thank", "thanks", "you", "for", "to", "at", "of", "in", "on", "the", "a", "an", "and", "with", "from", "is", "are", "we", "have",
    "has", "been", "was", "were", "got", "sent", "submitted", "successfully", "successful", "reviewing", "interest", "interested", "taking",
    "time", "opportunity", "candidature", "post", "batch", "unfortunately", "congratulations", "hi", "hello", "dear", "message", "position",
    "positions", "role", "roles", "opening", "job", "vacancy", "confirmation", "confirm", "confirmed", "offer", "assessment", "round",
    "screening", "phone", "video", "hiring", "recruiting", "recruitment", "career", "careers", "candidate", "candidacy", "follow", "up",
    "remote", "hybrid", "onsite", "permanent", "temporary", "ref", "reference", "req", "requisition", "id", "number", "no", "new", "grad",
    "early", "graduate", "campus", "fresher", "freshers", "filled", "closed", "regret", "pleased", "happy", "glad", "next", "stage"})
# Where a role is: a qualifier after the role that names only a place is not a different role.
_PERSON_ROLES = frozenset({"manager", "recruiter", "lead", "head", "director", "associate", "executive", "officer", "representative", "agent",
                           "assistant", "specialist", "coordinator"})    # on their own these name the person writing, not the job
_PLACES = frozenset({
    "india", "us", "usa", "uk", "emea", "apac", "europe", "canada", "singapore", "dubai", "uae", "germany", "london", "berlin", "ncr", "asia",
    "bengaluru", "bangalore", "mumbai", "delhi", "gurgaon", "gurugram", "noida", "hyderabad", "pune", "chennai", "kolkata", "ahmedabad", "jaipur",
    "kochi", "chandigarh", "indore", "coimbatore", "remote", "worldwide", "global", "americas", "amer", "latam", "york", "francisco", "seattle",
    "austin", "boston", "toronto", "dublin", "paris", "amsterdam", "sydney", "tokyo", "karnataka", "maharashtra", "telangana", "haryana"})
_SYNONYMS = {"sr": "senior", "jr": "junior", "ii": "2", "iii": "3", "iv": "4", "engineering": "engineer", "developers": "developer",
             "engineers": "engineer"}
_PHRASES = re.compile(
    r"\b(?:full|part)[\s-]time\b|\b(?:coding|online|technical|written|aptitude|take[\s-]home)\s+(?:test|assessment|challenge|round)\b"
    r"|\bapplication\s+review\b|\bphone\s+call\b", re.I)
_SPLIT = re.compile(r"\s[-–—|:•·]\s|[|:•·()\[\]]|\sat\s|\s@\s", re.I)
_BODY_ROLE = (
    re.compile(r"\b(?:for|to)\s+(?:the\s+)?([^.\n;!?]{3,80}?)\s+(?:position|role|opening|vacancy|opportunity|post)\b", re.I),
    re.compile(r"\b(?:application|applied|applying)\s+(?:for|to)\s+(?:the\s+)?([^.\n;!?]{3,80}?)(?=\s+(?:at|with|on|via|position|role)\b|[.!?\n]|$)", re.I))
_LEAD_IN = re.compile(r"\b(?:for|to|in|of|the|our|your|a|an|as)\b")


def _tokens(text: str) -> list:
    text = _PHRASES.sub(" ", str(text or ""))
    text = re.sub(r"\bfront[\s-]?end\b", "frontend", text, flags=re.I)
    text = re.sub(r"\bback[\s-]?end\b", "backend", text, flags=re.I)
    text = re.sub(r"\bfull[\s-]?stack\b", "fullstack", text, flags=re.I)
    text = re.sub(r"\bsde\b", "software development engineer", text, flags=re.I)
    text = re.sub(r"\bml\b", "machine learning", text, flags=re.I)
    text = re.sub(r"\bqa\b", "quality assurance", text, flags=re.I)
    out = []
    for w in _norm(text).split():
        w = _SYNONYMS.get(w, w)
        if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.append(w)
    return out


def _role_words(text: str, company_words: set) -> set:
    """What a piece of text says about a role: its words, without the wording around a role, the company's name, places, years and ids."""
    return {w for w in _tokens(text) if (len(w) > 1 or w.isdigit()) and w not in _PROCESS_WORDS and w not in _PLACES and w not in company_words and w not in _LEGAL
            and not re.fullmatch(r"\d{4,}|[a-z]\d+", w)}


def _names_a_role(words: list) -> bool:
    """Is there a role word that is the role itself (not a person's: 'Message from Priya, Recruiter', 'Interview with the Manager')?"""
    for i, w in enumerate(words):
        if w in _ROLE_WORDS:
            return not any(x in ("from", "with") for x in words[:i]) and not all(
                x in _PERSON_ROLES or x in _PROCESS_WORDS for x in words)           # a bare "Manager" says nothing about the role
    return False


def _after_greeting(seg: str) -> str:
    """'Sam, your application for X': a short opening before a comma that holds no role word is a greeting, not part of the role."""
    head, sep, rest = seg.partition(",")
    if sep and len(head.split()) <= 3 and not any(w in _ROLE_WORDS for w in _norm(head).split()):
        return rest
    return seg


def role_mismatch(candidate: Candidate, subject: str, text: str) -> bool:
    """Does the email name a role (in its subject, else in the first part of its text) that the matched job's title does not cover?"""
    if not isinstance(candidate.title, str):
        return False
    company_words = {w for f in name_forms(candidate.company) for w in f.words}
    title_words = _role_words(candidate.title, company_words)
    if not title_words:
        return False                                     # nothing to compare against
    named = []
    segments = [_after_greeting(x) for x in _SPLIT.split(str(subject or ""))]
    for i, seg in enumerate(segments):
        if _names_a_role(_norm(seg).split()):
            words = _role_words(seg, company_words)
            nxt = segments[i + 1] if i + 1 < len(segments) else ""
            if nxt and len(nxt.split()) <= 4:
                words |= _role_words(nxt, company_words) - _PERSON_ROLES   # "Software Engineer - Rulesets", "Software Engineer (Intern)": a qualifier is part of the role
            if words:
                named.append(words)
    if not named:                                        # the subject names no role: the first part of the text may ("for the X position")
        for pattern in _BODY_ROLE:
            for m in pattern.finditer(str(text or "")[:BODY_WINDOW]):
                phrase = _LEAD_IN.split(m.group(1))[-1]   # "your interest in the Software Engineer": the role is what follows the last lead-in
                if _names_a_role(_norm(phrase).split()):
                    named.append(_role_words(phrase, company_words))
        named = [n for n in named if n]
    if not named:
        return False
    # the role is the job's when every word the email puts on it is in the job's title (an email may shorten a title, not extend it)
    return not any(n <= title_words for n in named)


def score(candidate: Candidate, *, domain: str, from_name: str, subject: str, text: str) -> tuple[int, list]:
    """(score, evidence names) of one candidate against one email."""
    forms = name_forms(candidate.company)
    if not forms:
        return 0, []
    evidence = []
    kind = domain_kind(candidate.company, domain)
    if kind == "full":
        evidence.append("domain")
    elif kind == "partial":
        evidence.append("domain_partial")
    if any(in_text(f, from_name) for f in forms):
        evidence.append("display")
    if any(in_text(f, subject) for f in forms):
        evidence.append("subject")
    if any(in_text(f, text[:BODY_WINDOW]) for f in forms):
        evidence.append("body")
    points = {"domain": 3, "domain_partial": 2, "display": 2, "subject": 2, "body": 1}
    total = sum(points[e] for e in evidence)
    if total and candidate.title and (_title_in(candidate.title, subject) or _title_in(candidate.title, text[:BODY_WINDOW])):
        evidence.append("title")
        total += 1
    return total, evidence


def match_message(message, candidates: Sequence[Candidate]) -> Match:
    """The application an email is about, or why none. `message` is a gmail_client.Message."""
    domain, from_name, subject, text = message.from_domain, message.from_name, message.subject, message.text
    if not domain:
        return Match(None, reason="the sender has no usable address")
    scored = []
    for c in candidates:
        s, ev = score(c, domain=domain, from_name=from_name, subject=subject, text=text)
        if s >= MIN_SCORE:
            scored.append((s, ev, c))
    if not scored:
        return Match(None, reason="no application's company is named by this email")
    scored.sort(key=lambda t: -t[0])
    best = scored[0][0]
    leaders = [t for t in scored if t[0] == best]
    # a second COMPANY that is as good (or one point behind) makes the email ambiguous: never pick
    keys = {_company_key(t[2].company) for t in scored if t[0] >= best - 1}
    if len(keys) > 1:
        return Match(None, reason="the email fits applications at more than one company: " + ", ".join(sorted(k or "?" for k in keys)[:4]))
    if len(leaders) > 1:
        # several applications at the same company: the job title, then a single submitted application, decide
        titled = [t for t in leaders if "title" in t[1]]
        pool = titled if len(titled) == 1 else (titled if titled else leaders)
        if len(pool) > 1:
            submitted = [t for t in pool if t[2].submitted]
            pool = submitted if len(submitted) == 1 else pool
        if len(pool) > 1:
            return Match(None, reason=f"{len(pool)} applications at this company fit equally well, and the email does not say which role")
        leaders = pool
    _, evidence, winner = leaders[0]
    has_domain = "domain" in evidence or "domain_partial" in evidence
    sender_kind = "company" if has_domain else "ats" if is_ats(domain) else "other"
    flags = []
    if "domain_partial" in evidence and "domain" not in evidence:
        flags.append("domain_matches_first_word_only")
    if has_domain and _ending(domain) not in COMMON_ENDINGS:
        flags.append("unfamiliar_domain_ending")
    if sender_kind == "other" and domain in FREE_MAIL:
        flags.append("free_mail_sender")
    elif sender_kind == "other":
        flags.append("sender_domain_not_the_company")             # the company is named, but the mail does not come from it or its ATS
    if "display" in evidence and sender_kind == "other":
        flags.append("display_name_only")
    # high needs the sender to BE the company (its domain) or the company's recruiting system with several pieces of text evidence;
    # a sender that is neither can name the company all it likes, and is at most medium
    if role_mismatch(winner, subject, text):
        flags.append("role_mismatch")
    confidence = "high" if ("domain" in evidence or (best >= 4 and sender_kind == "ats")) and "unfamiliar_domain_ending" not in flags else "medium"
    return Match(winner, confidence, "", evidence, sender_kind, flags)


def _company_key(company: str) -> str:
    forms = name_forms(company)
    return forms[-1].joined if forms else ""
