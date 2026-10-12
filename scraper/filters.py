"""Free rule filters, applied to a job before any model is called. Pure stdlib.

decide(job, settings) -> Decision(keep, reason, yoe_min, yoe_max)

Principle: be conservative. A filter drops a job only on clear evidence; anything ambiguous or
missing keeps the job so the scorer (and you) can judge it. Every drop has a human-readable
reason, stored as jobs.filter_reason, so you can see exactly what was removed and why.

  title keywords    exclude_keywords match whole words in the TITLE only ("lead" does not match
                    "leading", "intern" does not match "international")
  include keywords  if set, at least one must appear in the title or description
  companies         exclude_companies match on a normalised name
  age               postings older than max_age_days are dropped; an unknown date is kept
  remote            dropped only if allow_remote is false and the posting says remote
  experience        years of experience are extracted from the description; the job is dropped only
                    if EVERY stated requirement is above max_yoe (or every stated maximum is below
                    min_yoe). "3+ years Go, 7+ years overall" is kept: the lowest stated minimum is
                    what counts.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Optional

# "years", where the unit may also have a word glued straight onto it by a typo in the posting ("5-8 yearsin software
# engineering"): a strict word boundary would miss the requirement and let the job past the filter. Only a short list
# of ordinary following words is accepted, so "5 yearly", "3 yearbook" and "2 yrsomething" still do not match.
_YEARS = r"(?:years?|yrs?)(?:\b|(?=(?:in|of|as|with|on|at|to|for|and|or|exp\w*|work\w*|hands\w*|plus)\b))"
_RANGE = re.compile(r"(?<![\d.])(\d{1,2})\s*(?:-|–|—|to)\s*(\d{1,2})\s*\+?\s*" + _YEARS, re.I)
_SINGLE = re.compile(r"(?<![\d.\-–])(\d{1,2})\s*\+?\s*" + _YEARS, re.I)
_EXPERIENCE_NEAR = re.compile(r"experience|\bexp\b|experienced", re.I)
# Requirements are often written without the word "experience" ("- 5-8 years in software engineering"). Such a number
# counts when it starts a bullet point, or follows a requirement phrase shortly before it. "Founded 10 years ago" and
# "has 12 years in the industry" have neither, so they are still ignored.
_BULLET_BEFORE = re.compile(r"(?:^|\n)[ \t]*[-•*·▪●–—][ \t]*$")
_REQUIREMENT_CUE = re.compile(r"\brequire[sd]?\b|requirements?|minimum|at least|must have|must be|qualifications?|you (?:have|bring|will have|'ll have)|"
                              r"we(?:'re| are) looking for|candidates? (?:should|must|will)|proficien", re.I)
_NOT_EXPERIENCE = re.compile(r"years?\s+(?:old|ago|in business|of growth|of history|of operation)|since\s+\d{4}", re.I)


@dataclass
class Decision:
    keep: bool
    reason: Optional[str] = None
    yoe_min: Optional[int] = None
    yoe_max: Optional[int] = None


def extract_yoe(text: Optional[str]) -> tuple[Optional[int], Optional[int]]:
    """Years of experience a posting asks for: (lowest stated minimum, highest stated maximum).

    Only numbers that sit next to the word 'experience' count, so "founded 10 years ago" and
    "5 years of growth" are ignored. Returns (None, None) when nothing reliable is found.
    """
    if not text:
        return None, None
    mins: list[int] = []
    maxs: list[int] = []
    taken: list[tuple[int, int]] = []

    def near_experience(start: int, end: int) -> bool:
        window = text[max(0, start - 60):end + 80]
        stated = (bool(_EXPERIENCE_NEAR.search(window)) or bool(_BULLET_BEFORE.search(text[max(0, start - 12):start]))
                  or bool(_REQUIREMENT_CUE.search(text[max(0, start - 70):start])))
        return stated and not _NOT_EXPERIENCE.search(text[max(0, start - 5):end + 30])

    for m in _RANGE.finditer(text):
        lo, hi = int(m.group(1)), int(m.group(2))
        if lo <= hi <= 40 and near_experience(m.start(), m.end()):
            mins.append(lo)
            maxs.append(hi)
            taken.append(m.span())
    for m in _SINGLE.finditer(text):
        if any(a <= m.start() < b for a, b in taken):
            continue
        n = int(m.group(1))
        if 0 < n <= 40 and near_experience(m.start(), m.end()):
            mins.append(n)
    return (min(mins) if mins else None), (max(maxs) if maxs else None)


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").lower()
    s = re.sub(r"\b(pvt|private|ltd|limited|inc|llc|llp|corp|corporation|co)\b\.?", " ", s)
    return re.sub(r"[^\w]+", "", s)


def _word_re(phrase: str) -> re.Pattern:
    return re.compile(r"(?<!\w)" + re.escape(phrase.strip()).replace(r"\ ", r"\s+") + r"(?!\w)", re.I)


def _parse_dt(value) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def decide(job: dict, settings: dict, now: Optional[datetime] = None) -> Decision:
    """Apply the free filters to one job. `settings` is the validated search.json shape."""
    title = job.get("title") or ""
    description = job.get("description") or ""
    now = now or datetime.now(timezone.utc)

    yoe_min, yoe_max = extract_yoe(description)

    for kw in settings.get("exclude_keywords") or []:
        if kw.strip() and _word_re(kw).search(title):
            return Decision(False, f"excluded word in title: '{kw}'", yoe_min, yoe_max)

    company_norm = _norm(job.get("company") or "")
    for c in settings.get("exclude_companies") or []:
        cn = _norm(c)
        if len(cn) >= 3 and company_norm and (cn == company_norm or cn in company_norm):
            return Decision(False, f"excluded company: '{c}'", yoe_min, yoe_max)

    include = [k for k in (settings.get("include_keywords") or []) if k.strip()]
    if include and not any(_word_re(k).search(title) or _word_re(k).search(description) for k in include):
        return Decision(False, "none of your include keywords appear", yoe_min, yoe_max)

    max_age = settings.get("max_age_days")
    posted = _parse_dt(job.get("posted_at"))
    if max_age and posted:
        age = (now - posted).days
        if age > max_age:
            return Decision(False, f"posted {age} days ago (limit {max_age})", yoe_min, yoe_max)

    if settings.get("allow_remote") is False and job.get("is_remote") is True:
        return Decision(False, "remote role and remote is not allowed", yoe_min, yoe_max)

    lo, hi = settings.get("min_yoe"), settings.get("max_yoe")
    if hi is not None and yoe_min is not None and yoe_min > hi:
        return Decision(False, f"asks for {yoe_min}+ years of experience (your limit {hi})", yoe_min, yoe_max)
    if lo is not None and yoe_max is not None and yoe_max < lo:
        return Decision(False, f"asks for at most {yoe_max} years of experience (your minimum {lo})", yoe_min, yoe_max)

    return Decision(True, None, yoe_min, yoe_max)
