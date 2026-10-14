"""Apply assist, step 10a: the fact sheet (config/apply.json). Pure functions, no I/O, no model calls, nothing sent anywhere.

The fact sheet is the one place the apply assist may take a personal answer from (besides the resume). It is written by you, once, and
only ever read by the scraper. This module:

  validate_fact_sheet   strict reading of the file: unknown fields, wrong types and out-of-range values are problems, never guesses
  missing_fields        what is not filled in, and what that means: a question that depends on it will say "needs you"
  consistency           warnings where the sheet disagrees with your resume or profile (years, email, relocation, graduation year, ...)
  skill_table           years per skill DERIVED from the resume's own dates, with what a person's override says beside it
  skill_years_for       one skill's years for a form question: an override, else the resume dates, else "listed only" / "not on your resume"

The rules that decide everything here:
  * a missing field is "needs you", never a default (the one exception is equal opportunity: "Prefer not to say", which only ever
    stands for NOT answering, and is never inferred from anything);
  * "years with a skill" is never zero and never a guess: a skill the resume does not show is "not on your resume";
  * a year-only date counts as the middle of that year, so a derived figure is neither generous nor stingy.
"""
from __future__ import annotations

import copy
import math
import re
import unicodedata
from datetime import date, timedelta
from typing import Optional
from urllib.parse import urlparse

import tailor_v2 as V2

TOP_FIELDS = ("contact", "right_to_work", "relocation", "terms", "experience", "standing_answers", "equal_opportunity")
EEO_KINDS = ("gender", "ethnicity", "veteran", "disability")
EEO_DEFAULT = "Prefer not to say"
WORK_MODES = ("remote", "hybrid", "on-site")
LINK_KINDS = ("linkedin", "github", "portfolio", "website")
PAY_PERIODS = ("year", "month")
MAX_TEXT = 400
MAX_SKILL_YEARS = 100
MAX_CUSTOM = 50

_BAD_CATEGORIES = ("Cc", "Cf", "Zl", "Zp")                    # control, zero-width / direction marks, line and paragraph separators
_HOST = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}", re.I)
MIN_YEAR = 1950
_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,200}\.[^@\s]{2,24}$")
_PHONE = re.compile(r"^\+\d[\d ()-]{5,18}\d$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_YEAR_MONTH = re.compile(r"(\d{4})(?:-(0[1-9]|1[0-2]))?")


# ---------------------------------------------------------------- reading the file

def _text(v, where: str, problems: list, limit: int = 120) -> Optional[str]:
    if v is None:
        return None
    if not isinstance(v, str):
        problems.append(f"{where}: must be text")
        return None
    v = " ".join(v.split())                                     # one line, single spaces
    if any(unicodedata.category(ch) in _BAD_CATEGORIES for ch in v):
        problems.append(f"{where}: must not contain control characters")
        return None
    if not v:
        return None                                              # an empty string is the same as not filled in
    if len(v) > limit:
        problems.append(f"{where}: longer than {limit} characters")
        return None
    return v


def _bool(v, where: str, problems: list) -> Optional[bool]:
    if v is None:
        return None
    if not isinstance(v, bool):
        problems.append(f"{where}: must be true or false")
        return None
    return v


def _number(v, where: str, problems: list, lo: float, hi: float) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v or not lo <= v <= hi:
        problems.append(f"{where}: must be a number from {lo:g} to {hi:g}")
        return None
    return v


def _int(v, where: str, problems: list, lo: int, hi: int) -> Optional[int]:
    if v is None:
        return None
    if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
        problems.append(f"{where}: must be a whole number from {lo} to {hi}")
        return None
    return v


def _object(obj, where: str, allowed: tuple, problems: list) -> dict:
    if obj is None:
        return {}
    if not isinstance(obj, dict):
        problems.append(f"{where}: must be an object")
        return {}
    for k in obj:
        if not str(k).startswith("_") and k not in allowed:
            problems.append(f"{where}.{k}: not a field of the fact sheet (the fields are {', '.join(allowed)})")
    return obj


def _https_address(u: str) -> bool:
    try:
        parts = urlparse(u)
        host = parts.hostname or ""
        parts.port                                                # raises on a bad port
    except ValueError:
        return False
    return parts.scheme == "https" and bool(_HOST.fullmatch(host)) and not parts.username and not parts.password and " " not in u and "\\" not in u


def _pay(obj, where: str, problems: list, expected: bool) -> Optional[dict]:
    allowed = ("min", "max", "currency", "period", "negotiable") if expected else ("amount", "currency", "period")
    first = len(problems)
    obj = _object(obj, where, allowed, problems)
    if not obj:
        return None
    out: dict = {}
    for key in (("min", "max") if expected else ("amount",)):
        n = _number(obj.get(key), f"{where}.{key}", problems, 0.01, 1e10)
        if n is not None:
            out[key] = n
    cur = obj.get("currency")
    if cur is not None:
        if isinstance(cur, str) and _CURRENCY.fullmatch(cur):
            out["currency"] = cur
        else:
            problems.append(f"{where}.currency: a three-letter code such as INR or USD")
    per = obj.get("period")
    if per is not None:
        if per in PAY_PERIODS:
            out["period"] = per
        else:
            problems.append(f"{where}.period: {' or '.join(PAY_PERIODS)}")
    if expected:
        neg = _bool(obj.get("negotiable"), f"{where}.negotiable", problems)
        if neg is not None:
            out["negotiable"] = neg
        if "min" in out and "max" in out and out["max"] < out["min"]:
            problems.append(f"{where}: max is below min")
    if out and not ({"amount"} <= set(out) if not expected else "min" in out):
        problems.append(f"{where}: give the {'min' if expected else 'amount'}, or leave the whole field out")
    if out and ("currency" not in out or "period" not in out):
        problems.append(f"{where}: give the currency and the period (year or month) too, so a figure is never read in the wrong unit")
    if len(problems) > first:
        return None                      # a pay figure with any part wrong is left out whole: half of one could be read in the wrong unit
    return out or None


def validate_fact_sheet(obj, today: Optional[date] = None) -> tuple:
    """(clean sheet, problems). The clean sheet only holds fields that were filled in and valid, one-line text, plus the equal
    opportunity defaults. A problem is anything a person should fix; the clean sheet is still usable (a bad field is simply left out)."""
    today = today or date.today()
    problems: list[str] = []
    if not isinstance(obj, dict):
        clean, _ = validate_fact_sheet({}, today)                # nothing usable, but the shape of a clean sheet is always the same
        return clean, ["the fact sheet must be a JSON object"]
    for k in obj:
        if not str(k).startswith("_") and k not in TOP_FIELDS:
            problems.append(f"{k}: not a section of the fact sheet (the sections are {', '.join(TOP_FIELDS)})")
    clean: dict = {}

    # ---- contact
    c = _object(obj.get("contact"), "contact", ("full_name", "preferred_name", "email", "phone", "city", "country", "links"), problems)
    contact: dict = {}
    for key in ("full_name", "preferred_name", "city", "country"):
        t = _text(c.get(key), f"contact.{key}", problems, 80)
        if t:
            contact[key] = t
    em = _text(c.get("email"), "contact.email", problems, 254)
    if em:
        if _EMAIL.match(em):
            contact["email"] = em
        else:
            problems.append("contact.email: not a valid email address")
    ph = _text(c.get("phone"), "contact.phone", problems, 24)
    if ph:
        if _PHONE.match(ph):
            contact["phone"] = ph
        else:
            problems.append("contact.phone: with the country code, for example +91 98765 43210")
    links = _object(c.get("links"), "contact.links", LINK_KINDS, problems)
    out_links = {}
    for key in LINK_KINDS:
        u = _text(links.get(key), f"contact.links.{key}", problems, 200)
        if u:
            if _https_address(u):
                out_links[key] = u
            else:
                problems.append(f"contact.links.{key}: a full https:// address (no password or user name in it)")
    if out_links:
        contact["links"] = out_links
    if contact:
        clean["contact"] = contact

    # ---- right to work (one entry per country)
    rtw = obj.get("right_to_work")
    entries: list = []
    if rtw is not None:
        if not isinstance(rtw, list) or len(rtw) > 10:
            problems.append("right_to_work: a list of at most 10 countries")
        else:
            seen = set()
            for i, e in enumerate(rtw):
                where = f"right_to_work[{i}]"
                e = _object(e, where, ("country", "authorized", "needs_sponsorship_now", "needs_sponsorship_future"), problems)
                country = _text(e.get("country"), f"{where}.country", problems, 60)
                if not country:
                    problems.append(f"{where}.country: required")
                    continue
                if country.lower() in seen:
                    problems.append(f"{where}.country: {country} appears twice")
                    continue
                seen.add(country.lower())
                entry = {"country": country}
                for key in ("authorized", "needs_sponsorship_now", "needs_sponsorship_future"):
                    b = _bool(e.get(key), f"{where}.{key}", problems)
                    if b is not None:
                        entry[key] = b
                entries.append(entry)
    if entries:
        clean["right_to_work"] = entries

    # ---- relocation
    r = _object(obj.get("relocation"), "relocation", ("willing", "where"), problems)
    reloc: dict = {}
    w = _bool(r.get("willing"), "relocation.willing", problems)
    if w is not None:
        reloc["willing"] = w
    where_list = r.get("where")
    if where_list is not None:
        if not isinstance(where_list, list) or len(where_list) > 10:
            problems.append("relocation.where: a list of at most 10 places")
        else:
            places = [p for p in (_text(x, f"relocation.where[{i}]", problems, 60) for i, x in enumerate(where_list)) if p]
            if places:
                reloc["where"] = places
    if reloc:
        clean["relocation"] = reloc

    # ---- terms
    t = _object(obj.get("terms"), "terms", ("notice_period_days", "earliest_start_date", "current_pay", "expected_pay", "work_mode"), problems)
    terms: dict = {}
    nd = _int(t.get("notice_period_days"), "terms.notice_period_days", problems, 0, 365)
    if nd is not None:
        terms["notice_period_days"] = nd
    sd = t.get("earliest_start_date")
    if sd is not None:
        if sd == "immediately":
            terms["earliest_start_date"] = sd
        elif isinstance(sd, str) and _ISO_DATE.fullmatch(sd):
            try:
                date.fromisoformat(sd)
                terms["earliest_start_date"] = sd
            except ValueError:
                problems.append("terms.earliest_start_date: not a real date")
        else:
            problems.append("terms.earliest_start_date: YYYY-MM-DD, or the word immediately")
    for key, expected in (("current_pay", False), ("expected_pay", True)):
        p = _pay(t.get(key), f"terms.{key}", problems, expected)
        if p:
            terms[key] = p
    wm = t.get("work_mode")
    if wm is not None:
        if not isinstance(wm, list) or not wm or any(m not in WORK_MODES for m in wm) or len(set(wm)) != len(wm):
            problems.append(f"terms.work_mode: a list of {', '.join(WORK_MODES)} (each at most once)")
        else:
            terms["work_mode"] = list(wm)
    if terms:
        clean["terms"] = terms

    # ---- experience
    x = _object(obj.get("experience"), "experience", ("total_years", "skill_years", "highest_education", "graduation_year", "certifications"), problems)
    exp: dict = {}
    ty = _number(x.get("total_years"), "experience.total_years", problems, 0, 60)
    if ty is not None:
        exp["total_years"] = ty
    sy = x.get("skill_years")
    if sy is not None:
        if not isinstance(sy, dict) or len(sy) > MAX_SKILL_YEARS:
            problems.append(f"experience.skill_years: an object of skill: years (at most {MAX_SKILL_YEARS})")
        else:
            years, seen_skill = {}, set()
            for name, v in sy.items():
                if isinstance(name, str) and name.startswith("_"):
                    continue
                label = _text(name, "experience.skill_years", problems, 40)
                if not label:
                    problems.append("experience.skill_years: every skill needs a name")
                    continue
                if label.startswith("_"):
                    problems.append(f"experience.skill_years.{label}: a skill name cannot start with an underscore")
                    continue
                if label.lower() in seen_skill:
                    problems.append(f"experience.skill_years.{label}: {label} appears twice")
                    continue
                seen_skill.add(label.lower())
                n = _number(v, f"experience.skill_years.{label}", problems, 0.1, 60)       # 0 would claim "no experience" in a form; leave the skill out instead
                if n is not None:
                    years[label] = n
            if years:
                exp["skill_years"] = years
    he = _text(x.get("highest_education"), "experience.highest_education", problems, 120)
    if he:
        exp["highest_education"] = he
    gy = _int(x.get("graduation_year"), "experience.graduation_year", problems, 1950, today.year + 8)
    if gy is not None:
        exp["graduation_year"] = gy
    certs = x.get("certifications")
    if certs is not None:
        if not isinstance(certs, list) or len(certs) > 20:
            problems.append("experience.certifications: a list of at most 20")
        else:
            cl = [c_ for c_ in (_text(v, f"experience.certifications[{i}]", problems, 120) for i, v in enumerate(certs)) if c_]
            if cl:
                exp["certifications"] = cl
    if exp:
        clean["experience"] = exp

    # ---- standing answers
    s = _object(obj.get("standing_answers"), "standing_answers", ("how_did_you_hear", "willing_to_travel", "relative_here_default", "custom"), problems)
    standing: dict = {}
    hh = _text(s.get("how_did_you_hear"), "standing_answers.how_did_you_hear", problems, 120)
    if hh:
        standing["how_did_you_hear"] = hh
    for key in ("willing_to_travel", "relative_here_default"):
        b = _bool(s.get(key), f"standing_answers.{key}", problems)
        if b is not None:
            standing[key] = b
    custom = s.get("custom")
    if custom is not None:
        if not isinstance(custom, list) or len(custom) > MAX_CUSTOM:
            problems.append(f"standing_answers.custom: a list of at most {MAX_CUSTOM} {{phrase, answer}} pairs")
        else:
            pairs, seen_p = [], set()
            for i, item in enumerate(custom):
                where = f"standing_answers.custom[{i}]"
                item = _object(item, where, ("phrase", "answer"), problems)
                phrase = _text(item.get("phrase"), f"{where}.phrase", problems, 120)
                answer = _text(item.get("answer"), f"{where}.answer", problems, MAX_TEXT)
                if not phrase or not answer:
                    problems.append(f"{where}: needs a phrase and an answer")
                    continue
                if phrase.lower() in seen_p:
                    problems.append(f"{where}.phrase: '{phrase}' appears twice")
                    continue
                seen_p.add(phrase.lower())
                pairs.append({"phrase": phrase, "answer": answer})
            if pairs:
                standing["custom"] = pairs
    if standing:
        clean["standing_answers"] = standing

    # ---- equal opportunity: "Prefer not to say" unless you chose otherwise; never inferred
    e = _object(obj.get("equal_opportunity"), "equal_opportunity", EEO_KINDS, problems)
    clean["equal_opportunity"] = {k: _text(e.get(k), f"equal_opportunity.{k}", problems, 80) or EEO_DEFAULT for k in EEO_KINDS}
    return clean, problems


# ---------------------------------------------------------------- what is missing, and what that means

# (path, what a question that depends on it will do). Equal opportunity is never "missing": it has its own default.
FIELD_EFFECTS = (
    ("contact.full_name", "the name fields on every form"),
    ("contact.email", "the email field on every form"),
    ("contact.phone", "the phone field on every form"),
    ("contact.city", "the location and city fields"),
    ("contact.country", "the country field"),
    ("contact.links.linkedin", "a LinkedIn profile field"),
    ("contact.links.github", "a GitHub field"),
    ("right_to_work", "every 'are you authorised to work in ...' and sponsorship question"),
    ("relocation.willing", "every relocation question"),
    ("terms.notice_period_days", "every notice-period question"),
    ("terms.earliest_start_date", "every 'earliest start date' question"),
    ("terms.current_pay", "every 'current salary' question"),
    ("terms.expected_pay", "every 'expected salary' question"),
    ("terms.work_mode", "every remote / hybrid / on-site question"),
    ("experience.highest_education", "every 'highest education' question"),
    ("experience.graduation_year", "every 'year of graduation' question"),
    ("standing_answers.willing_to_travel", "every 'willing to travel' question"),
    ("standing_answers.relative_here_default", "every 'do you have a relative who works here' question"),
)


def _has(sheet: dict, path: str) -> bool:
    cur = sheet
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False
        cur = cur[part]
    return bool(cur) or cur is False or cur == 0


RIGHT_TO_WORK_FIELDS = (
    ("authorized", "right_to_work.authorized", "every 'are you authorised to work in ...' question"),
    ("needs_sponsorship_now", "right_to_work.needs_sponsorship_now", "every 'do you need visa sponsorship now' question"),
    ("needs_sponsorship_future", "right_to_work.needs_sponsorship_future", "every 'will you need sponsorship in the future' question"),
)


def missing_fields(sheet: dict) -> list:
    """[(path, effect)] for what is not filled in. A missing field is 'needs you' for the questions that depend on it."""
    out = [(path, effect) for path, effect in FIELD_EFFECTS if path != "right_to_work" and not _has(sheet, path)]
    entries = sheet.get("right_to_work") or []
    if not entries:
        out.append(("right_to_work", "every 'are you authorised to work in ...' and sponsorship question"))
    else:                                                        # a country with no answers under it is not an answer
        out += [(path, effect) for key, path, effect in RIGHT_TO_WORK_FIELDS if not any(key in e for e in entries)]
    return sorted(out, key=lambda pe: [p for p, _ in FIELD_EFFECTS].index(pe[0]) if pe[0] in [p for p, _ in FIELD_EFFECTS] else 99)


# ---------------------------------------------------------------- dates and years, from the resume

def _month_index(s, today: date, end: bool) -> Optional[int]:
    """Months since year 0 for a resume date. '' as an END date is today (for a job still going); a year alone is the middle of that
    year; anything else, a year before 1950 or a date that is not text is not usable."""
    if s is None or s == "":
        return today.year * 12 + today.month - 1 if end else None
    if not isinstance(s, str):
        return None
    m = _YEAR_MONTH.fullmatch(s)
    if not m or int(m.group(1)) < MIN_YEAR:
        return None
    year, month = int(m.group(1)), m.group(2)
    return year * 12 + (int(month) - 1 if month else 6)


def _span(item, today: date) -> Optional[tuple]:
    """(start, end) in months for a role, or None. The end never goes past today and a start after today is not usable: a resume
    can say 2035, a form must not."""
    if not isinstance(item, dict):
        return None
    start = _month_index(item.get("startDate"), today, end=False)
    end = _month_index(item.get("endDate"), today, end=True)
    if start is None or end is None:
        return None
    end = min(end, today.year * 12 + today.month - 1)
    return (start, end) if end > start else None


def _union_months(spans: list) -> int:
    total, cur_s, cur_e = 0, None, None
    for s, e in sorted(spans):
        if cur_e is None or s > cur_e:
            if cur_e is not None:
                total += cur_e - cur_s
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    if cur_e is not None:
        total += cur_e - cur_s
    return total


def _list(resume, key: str) -> list:
    v = resume.get(key) if isinstance(resume, dict) else None
    return v if isinstance(v, list) else []


def resume_total_years(resume: dict, today: Optional[date] = None) -> Optional[float]:
    """Years of work the resume's dates add up to (overlapping roles counted once), or None if no role has usable dates."""
    today = today or date.today()
    spans = [sp for sp in (_span(w, today) for w in _list(resume, "work")) if sp]
    return round(_union_months(spans) / 12, 1) if spans else None


# Names that are also ordinary words or fragments ("Go live", "Swift delivery", "R&D", "C-suite"): a mention in a BULLET only counts
# when it is not the first word of a sentence and is not glued to a following "&" or "-word". A job title is exempt ("Go Engineer").
_AMBIGUOUS = frozenset(("go", "swift", "rust", "ruby", "spring", "dart", "julia"))
_GLUED = re.compile(r"[&-][A-Za-z]")


def _patterns(name: str) -> list:
    """Whole-word patterns for a skill: the usual one (tailor_v2.skill_pattern) and, for a short name, the exact spelling too (iOS, npm)."""
    out = [V2.skill_pattern(name.strip().lower())]
    if len(name.strip()) <= 3:
        out.append(re.compile(r"(?<![A-Za-z0-9+#])" + re.escape(name.strip()) + r"(?![A-Za-z0-9+#])"))
    return out


def _ambiguous(name: str) -> bool:
    return len(name.strip()) <= 2 or name.strip().lower() in _AMBIGUOUS


def _hit(text: str, patterns: list, strict: bool) -> bool:
    for pat in patterns:
        for m in pat.finditer(text):
            if not strict:
                return True
            before = text[:m.start()].rstrip()
            if before and before[-1] not in ".!?:;•*-–—" and not _GLUED.match(text[m.end():m.end() + 2]):
                return True
    return False


def _mentions(item: dict, patterns: list, is_project: bool, strict: bool) -> bool:
    if _hit(item.get("position") if isinstance(item.get("position"), str) else "", patterns, False):
        return True                                              # a job title names its skill ("Kubernetes Engineer")
    if is_project and _hit(item.get("name") if isinstance(item.get("name"), str) else "", patterns, False):
        return True                                              # a job's `name` is the employer, not a skill
    highlights = item.get("highlights") if isinstance(item.get("highlights"), list) else []
    for h in highlights:
        if isinstance(h, dict):
            for key in ("text", "group"):
                if isinstance(h.get(key), str) and _hit(h[key], patterns, strict):
                    return True
    return False


def _listed(resume: dict, patterns: list) -> bool:
    for g in _list(resume, "skills"):
        if isinstance(g, dict) and isinstance(g.get("keywords"), list):
            if any(isinstance(k, str) and any(pat.search(k) for pat in patterns) for k in g["keywords"]):
                return True
    return False


def derived_skill(resume: dict, name, today: Optional[date] = None) -> dict:
    """What the resume itself shows about a skill:
         where     'worked' (a job or project mentions it), 'listed' (only the skills section) or 'absent'
         years     the dated JOBS that mention it, overlaps counted once, an end after today cut at today; None if no job with usable
                   dates mentions it. Projects show that a skill was used but add no years (a project has no employer, and a
                   hobby project would otherwise lift a figure above the whole career)
         roles     the jobs and projects that mention it, so a person can see where a figure comes from
         projects_only  it was found only in projects
       The figure is the LENGTH OF THE JOBS that mention the skill: it can be generous for a skill mentioned once in a long job, which is
       why a person can override it (experience.skill_years) and why `apply sheet` shows the roles beside it."""
    today = today or date.today()
    if not isinstance(name, str) or not name.strip():
        return {"where": "absent", "years": None, "roles": [], "projects_only": False}
    patterns, strict = _patterns(name), _ambiguous(name)
    spans, roles, in_jobs, in_projects = [], [], False, False
    for is_project, item in [(False, w) for w in _list(resume, "work")] + [(True, pr) for pr in _list(resume, "projects")]:
        if isinstance(item, dict) and _mentions(item, patterns, is_project, strict):
            label = item.get("name") if isinstance(item.get("name"), str) else ""
            roles.append(label or ("a project" if is_project else "a job"))
            if is_project:
                in_projects = True
            else:
                in_jobs = True
                sp = _span(item, today)
                if sp:
                    spans.append(sp)
    if in_jobs or in_projects:
        years = round(_union_months(spans) / 12, 1) if spans else None
        return {"where": "worked", "years": years, "roles": roles[:6], "projects_only": in_projects and not in_jobs}
    return {"where": "listed" if _listed(resume, patterns) else "absent", "years": None, "roles": [], "projects_only": False}


def skill_years_for(name, sheet: dict, resume: dict, today: Optional[date] = None) -> dict:
    """The answer to 'how many years with <name>': {'years': float | None, 'source': 'your override' | 'resume dates' | None,
    'where': 'worked' | 'listed' | 'absent', 'roles': [...], 'note': text}. None years is 'needs you', with the reason in the note:
    never zero, never a guess."""
    derived = derived_skill(resume, name, today)
    base = {"where": derived["where"], "roles": derived["roles"]}
    if not isinstance(name, str) or not name.strip():
        return dict(base, years=None, source=None, note="not on your resume")
    overrides = ((sheet or {}).get("experience") or {}).get("skill_years") or {}
    for label, years in overrides.items():
        if label.strip().lower() == name.strip().lower():
            note = "your own figure; your resume does not show this skill at all" if derived["where"] == "absent" else ""
            return dict(base, years=years, source="your override", note=note)
    if derived["years"] is not None:
        return dict(base, years=derived["years"], source="resume dates", note="")
    note = {"worked": ("only your projects mention it, and a project adds no years" if derived["projects_only"]
                       else "your resume shows the work, but the jobs that mention it have no usable dates"),
            "listed": "your resume only lists it in the skills section, with no role showing it",
            "absent": "not on your resume"}[derived["where"]]
    return dict(base, years=None, source=None, note=note)


def skill_table(sheet: dict, resume: dict, today: Optional[date] = None) -> list:
    """One row per skill keyword in the resume: {'skill', 'years', 'source', 'where', 'roles', 'note', 'derived'} ('derived' is what the
    resume's dates give, whatever an override says, so a person sees both)."""
    rows, seen = [], set()
    for g in _list(resume, "skills"):
        for k in (g.get("keywords") if isinstance(g, dict) and isinstance(g.get("keywords"), list) else []):
            if not isinstance(k, str) or not k.strip() or k.lower() in seen:
                continue
            seen.add(k.lower())
            r = skill_years_for(k, sheet, resume, today)
            rows.append(dict(r, skill=k, derived=derived_skill(resume, k, today)["years"]))
    return rows


# ---------------------------------------------------------------- the sheet against the resume and the profile

def _digits(s) -> str:
    return re.sub(r"\D", "", s) if isinstance(s, str) else ""


def _name_tokens(s) -> list:
    """Lower-case letters only, accents removed, single-letter initials dropped: 'José R. García' -> ['jose', 'garcia']."""
    if not isinstance(s, str):
        return []
    flat = unicodedata.normalize("NFKD", s.lower())
    flat = "".join(ch for ch in flat if not unicodedata.combining(ch))
    return [t for t in re.sub(r"[^a-z\s]", " ", flat).split() if len(t) > 1]


def _same_person(a, b) -> bool:
    ta, tb = _name_tokens(a), _name_tokens(b)
    if not ta or not tb:
        return True                                              # nothing comparable (for example a name in another script): not a disagreement
    return set(ta) <= set(tb) or set(tb) <= set(ta) or (ta[0] == tb[0] and ta[-1] == tb[-1])


def consistency(sheet: dict, resume: dict, profile: dict, today: Optional[date] = None) -> list:
    """Warnings (text) where the sheet disagrees with the resume or the profile. A warning never blocks anything: a person decides."""
    today = today or date.today()
    out: list[str] = []
    resume = resume if isinstance(resume, dict) else {}
    profile = profile if isinstance(profile, dict) else {}
    basics = resume.get("basics") if isinstance(resume.get("basics"), dict) else {}
    contact = sheet.get("contact") or {}
    r_email = basics.get("email").strip() if isinstance(basics.get("email"), str) else ""
    if contact.get("email") and r_email and contact["email"].lower() != r_email.lower():
        out.append(f"contact.email ({contact['email']}) is not the email on your resume ({r_email}): forms and your resume will disagree")
    if contact.get("full_name") and isinstance(basics.get("name"), str) and not _same_person(contact["full_name"], basics["name"]):
        out.append(f"contact.full_name ({contact['full_name']}) is not the name on your resume ({basics['name']})")
    sp, rp = _digits(contact.get("phone")), _digits(basics.get("phone"))
    if sp and rp and not (sp.endswith(rp[-9:]) or rp.endswith(sp[-9:])):
        out.append("contact.phone is not the phone number on your resume")

    exp = sheet.get("experience") or {}
    derived_total = resume_total_years(resume, today)
    if "total_years" in exp:
        if derived_total is not None and abs(exp["total_years"] - derived_total) > 1.0:
            out.append(f"experience.total_years is {exp['total_years']:g}, but your resume's dates add up to {derived_total:g} years")
        py = profile.get("years_experience")
        if isinstance(py, (int, float)) and not isinstance(py, bool):
            try:
                pyf = float(py)
            except OverflowError:
                pyf = float("inf")
            if math.isfinite(pyf) and abs(exp["total_years"] - pyf) > 1.0:
                out.append(f"experience.total_years is {exp['total_years']:g}, but your profile says years_experience {pyf:g}")
    for skill, years in ((exp.get("skill_years") or {}).items() if resume else []):          # without a resume there is nothing to compare a skill with
        d = derived_skill(resume, skill, today)
        if d["where"] == "absent":
            out.append(f"experience.skill_years.{skill} is {years:g}, but {skill} is not on your resume: a form answer would claim what the resume does not show")
        elif d["years"] is not None and years > d["years"] + 1.0:
            out.append(f"experience.skill_years.{skill} is {years:g}, but your resume's dates show about {d['years']:g} years of work with it")
        elif d["years"] is None and d["where"] == "listed":
            out.append(f"experience.skill_years.{skill} is {years:g}, but your resume only lists {skill} in the skills section (no role shows it)")
        elif d["years"] is None and d["projects_only"]:
            out.append(f"experience.skill_years.{skill} is {years:g}, but only your projects mention {skill} (a project adds no years)")
        if derived_total is not None and years > derived_total + 1.0:
            out.append(f"experience.skill_years.{skill} ({years:g}) is more than all the years your resume covers ({derived_total:g})")
    education = resume.get("education") if isinstance(resume.get("education"), list) else []
    grad_years = [int(m.group(1)) for e in education if isinstance(e, dict) and isinstance(e.get("endDate"), str)
                  for m in [_YEAR_MONTH.fullmatch(e["endDate"])] if m]
    if "graduation_year" in exp and grad_years and exp["graduation_year"] not in grad_years:
        out.append(f"experience.graduation_year is {exp['graduation_year']}, but your resume's education ends {', '.join(str(y) for y in sorted(set(grad_years)))}")

    reloc = sheet.get("relocation") or {}
    loc = profile.get("location") if isinstance(profile.get("location"), dict) else {}
    if reloc.get("willing") is False and reloc.get("where"):
        out.append("relocation.where lists places, but relocation.willing is false")
    if "willing" in reloc and isinstance(loc.get("relocation"), bool) and reloc["willing"] != loc["relocation"]:
        out.append(f"relocation.willing is {str(reloc['willing']).lower()}, but your profile says relocation {str(loc['relocation']).lower()}")
    modes = (sheet.get("terms") or {}).get("work_mode") or []
    if "remote" in modes and loc.get("remote_ok") is False:
        out.append("terms.work_mode includes remote, but your profile says remote_ok is false")
    if "hybrid" in modes and loc.get("hybrid_ok") is False:
        out.append("terms.work_mode includes hybrid, but your profile says hybrid_ok is false")

    terms = sheet.get("terms") or {}
    cur, want = terms.get("current_pay"), terms.get("expected_pay")
    if cur and want and cur.get("currency") == want.get("currency") and cur.get("period") == want.get("period") and want["min"] < cur["amount"]:
        out.append("terms.expected_pay starts below terms.current_pay (the same currency and period): check that this is meant")
    sd = terms.get("earliest_start_date")
    if sd and sd != "immediately" and date.fromisoformat(sd) < today:
        out.append(f"terms.earliest_start_date ({sd}) is in the past")
    if "notice_period_days" in terms and sd and sd != "immediately":
        soonest = today + timedelta(days=terms["notice_period_days"])
        if date.fromisoformat(sd) < soonest:
            out.append(f"terms.earliest_start_date ({sd}) is before today plus your notice period ({terms['notice_period_days']} days = {soonest.isoformat()})")

    wa = profile.get("work_authorization") if isinstance(profile.get("work_authorization"), list) else []
    auth = {str(a.get("country") or "").strip().lower(): a for a in wa if isinstance(a, dict)}
    for e in sheet.get("right_to_work") or []:
        a = auth.get(e["country"].lower())
        status = str(a.get("status") or "").strip().lower() if a else ""
        if status in ("citizen", "permanent resident", "permanent_resident"):
            if e.get("authorized") is False:
                out.append(f"right_to_work says you are not authorised in {e['country']}, but your profile says {a.get('status')}")
            if e.get("needs_sponsorship_now") is True or e.get("needs_sponsorship_future") is True:
                out.append(f"right_to_work says you need sponsorship in {e['country']}, but your profile says {a.get('status')}")
    return out


# ---------------------------------------------------------------- the whole check, for the CLI and for the later steps

def check(obj, resume: Optional[dict] = None, profile: Optional[dict] = None, today: Optional[date] = None) -> dict:
    """{'sheet': clean sheet, 'problems': [...], 'missing': [(path, effect)], 'warnings': [...], 'skills': [rows]}"""
    today = today or date.today()
    sheet, problems = validate_fact_sheet(obj, today)
    return {"sheet": copy.deepcopy(sheet), "problems": problems, "missing": missing_fields(sheet),
            "warnings": consistency(sheet, resume or {}, profile or {}, today) if resume or profile else [],
            "skills": skill_table(sheet, resume or {}, today) if resume else []}
