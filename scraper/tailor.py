"""Tailoring: prompt, strict patch validation, grounding audit, repair, and applying a patch. Pure stdlib.

The model (called elsewhere) returns a PATCH over config/resume.json plus a cover letter. It may
reorder, reword and hide, never add. This module turns the patch into a tailored resume view and
makes sure nothing in it is invented:

  prepare()        build the prompt (resume, profile and instructions are the cacheable prefix)
  validate_patch() strict shape/ids/length checks; every id must exist in the resume
  audit()          deterministic grounding audit: numbers, technologies, names, experience claims
                   and style rules, against the resume (the single source of truth)
  repair()         strip whatever the audit rejects, so the final text is always grounded
  apply_patch()    patch + resume -> the view that gets rendered

HARD violations (an invented number or technology, an inflated years-of-experience claim) are
never allowed into the output. SOFT ones (a technology that appears only in the posting, a cliche)
are kept but reported, because a human should look at them.

The drafter/reviewer loop with a factual-grounding audit, requirement coverage, and the writing
rules are adapted from MadsLorentzen/ai-job-search (MIT); see THIRD_PARTY_NOTICES.md.
"""
from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

import safety

SCHEMA_VERSION = 1
MAX_TAILOR_TOKENS = 3500

MAX_HEADLINE = 80
MAX_SUMMARY = 500
MAX_REWRITE = 480
MAX_PARAGRAPH = 900
MAX_COVER_WORDS = 420
MAX_REQUIREMENTS = 14
MAX_EMPHASIZE = 10

STATUSES = {"matched", "bridged", "gap"}

CLICHES = ["passionate", "leverage", "leveraging", "hit the ground running", "drive results", "team player",
           "results-driven", "synergy", "synergies", "go-getter", "self-starter", "think outside the box",
           "rockstar", "ninja", "best-in-class", "cutting-edge", "world-class", "fast-paced environment",
           "proven track record", "dynamic", "i believe i would be a great fit"]

_EM_DASH = re.compile(r"\s*[—–]\s*")
_NUM = re.compile(r"(?<![A-Za-z0-9])\d[\d,]*(?:\.\d+)?")
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9+#.]*")
_YEARS = re.compile(r"\b(\d+(?:\.\d+)?|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|fifteen|twenty)"
                    r"\s*\+?\s*(?:-?\s*(?:plus)\s+)?(?:years?|yrs?)\b", re.I)
_WORD_NUMS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
              "ten": 10, "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20}
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
# A sentence that mentions a number of years while negating, comparing or quoting the posting ("I have about a year,
# not the three you ask for") is an honest acknowledgement of a gap, not a claim of experience.
_ACKNOWLEDGES = re.compile(
    r"\b(not|no|never|short of|fewer|less than|under|below|yet|ask(?:s|ed|ing)? for|require[sd]?|requirement|want(?:s|ed)?|"
    r"looking for|seek(?:s|ing)?|need(?:s|ed)?|posting|you (?:ask|describe|require|list|want|need)|your (?:role|team|posting|requirement))\b|n't",
    re.I)


def _sentence_at(text: str, pos: int) -> str:
    start = max((text.rfind(c, 0, pos) for c in ".!?\n"), default=-1)
    ends = [i for i in (text.find(c, pos) for c in ".!?\n") if i != -1]
    return text[start + 1:(min(ends) + 1 if ends else len(text))]

# Title-case words that are ordinary (greetings, months, languages...) and never a claim.
_SAFE_TITLECASE = {
    "dear", "hiring", "manager", "team", "sincerely", "regards", "thank", "thanks", "best", "kind", "yours",
    "january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
    "november", "december", "monday", "tuesday", "wednesday", "thursday", "friday", "english", "hindi",
    "ms", "mr", "mrs", "dr", "recruiter", "recruitment", "talent", "acquisition", "department", "company",
    "i", "i'm", "i've", "i'd", "i'll", "the", "your", "my", "we", "our",
}


class TailorError(ValueError):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


# ---------------------------------------------------------------- resume helpers

def resume_index(resume: dict) -> dict:
    """id -> {text, group, section, owner} for every bullet-like item in the resume."""
    idx: dict[str, dict] = {}
    for w in resume.get("work", []):
        for h in w.get("highlights", []):
            idx[h["id"]] = {"text": h["text"], "group": h.get("group"), "section": "work", "owner": w["id"]}
    for p in resume.get("projects", []) or []:
        for h in p.get("highlights", []):
            idx[h["id"]] = {"text": h["text"], "group": h.get("group"), "section": "projects", "owner": p["id"]}
    for a in resume.get("additional", []) or []:
        idx[a["id"]] = {"text": a["text"], "group": None, "section": "additional", "owner": a["id"]}
    return idx


def _strings(obj) -> list[str]:
    out: list[str] = []
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if not str(k).startswith("_"):
                out += _strings(v)
    elif isinstance(obj, list):
        for v in obj:
            out += _strings(v)
    return out


def tokens(text: str) -> list[str]:
    """Word-like tokens: letters/digits with + # . inside (C++, Node.js), split at hyphens and slashes."""
    out = []
    for m in _TOKEN.finditer(text or ""):
        t = m.group(0).rstrip(".")
        if t:
            out.append(t)
    return out


def vocabulary(resume: dict) -> set[str]:
    """Every lower-cased token that appears anywhere in the resume."""
    vocab: set[str] = set()
    for s in _strings(resume):
        vocab.update(t.lower() for t in tokens(s))
    return vocab


def numbers_in(text: str) -> set[str]:
    return {m.group(0).replace(",", "").rstrip(".") for m in _NUM.finditer(text or "")}


def candidate_years(resume: dict, profile: Optional[dict]) -> float:
    if profile and isinstance(profile.get("years_experience"), (int, float)):
        return float(profile["years_experience"])
    starts = [w.get("startDate", "") for w in resume.get("work", []) if w.get("startDate")]
    if not starts:
        return 0.0
    y = min(int(s[:4]) for s in starts)
    return max(0.0, date.today().year - y)


# ---------------------------------------------------------------- prompt

_SCHEMA_FULL = {
    "headline": "short role label for this application, max 6 words",
    "summary": "at most 2 sentences, or null",
    "work": [{"id": "w1", "show": True, "order": ["bullet ids to show, best first"], "rewrite": {"bullet id": "reworded text"}}],
    "skills": {"order": ["skill group names, in order"], "emphasize": ["existing keywords the posting asks for"]},
    "projects": {"include": ["project ids to show"]},
    "additional": {"include": ["ids to show"]},
    "requirements": [{"requirement": "what the posting asks for", "status": "matched|bridged|gap",
                      "evidence": "bullet id or null", "note": "short honest note"}],
    "cover_letter": {"greeting": "Dear Hiring Manager,", "paragraphs": ["p1", "p2", "p3"], "closing": "Sincerely,"},
}
_SCHEMA_COVER_ONLY = {
    "requirements": _SCHEMA_FULL["requirements"], "cover_letter": _SCHEMA_FULL["cover_letter"],
}

_INSTRUCTIONS = """You tailor one job application for one candidate. You never invent anything: every employer, job title, date, number, technology, tool and achievement you write must already appear in the candidate's resume below. You may reorder, reword and hide. You may never add.

OUTPUT: a single JSON object on ONE line (compact, no markdown fences, nothing before or after it) with exactly this shape:
{schema}
{mode_rules}
REQUIREMENTS COVERAGE
List every requirement the posting states (at most 12). For each give a status: matched (the resume shows it; give the bullet id), bridged (adjacent experience; give the id and say honestly how it connects), or gap (not in the resume; say so plainly). Never hide a gap.
{cover_rules}
STYLE
No em dashes or en dashes (use commas or full stops). No cliches or filler: passionate, leverage, hit the ground running, drive results, team player, results-driven, synergy, proven track record. No unsupported superlatives. First person, active voice, concrete and specific.

CANDIDATE PROFILE
{profile}

RESUME (JSON; bullets carry ids you must use exactly)
{resume}"""

_FULL_RULES = """
RESUME OPERATIONS
- work[].order lists the bullet ids to show for that role, best first. Leave out bullets that do not help for this posting; show 3 to 6 for the main role and fewer for older roles. Set show to false for a role only if it is clearly irrelevant.
- work[].rewrite maps a bullet id to reworded text. Use the posting's own terms ONLY where they truthfully describe the same work. Keep every number and metric exactly as written in that bullet. Add no technology, scope or result that the bullet does not already state. Keep the length within about 120% of the original.
- skills.order and skills.emphasize: reorder the skill groups, and emphasize up to 8 EXISTING keywords the posting asks for.
- projects.include and additional.include list the ids to show; leave out the rest.
- headline: a short role label built from titles in the resume or the candidate's target roles.
- summary: at most 2 sentences, or null. Only claims the resume supports. Do not state a number of years of experience.
"""
_COVER_ONLY_RULES = """
This candidate's resume is already a close match and will be used as written, so do NOT return headline, summary, work, skills, projects or additional. Return only requirements and cover_letter.
"""
_COVER_RULES = """
COVER LETTER
A greeting, exactly 3 paragraphs (each at most 90 words), and a closing. Forward-looking: what the candidate can do for this employer, supported by one or two concrete examples taken from the resume. Specific to this company and role, never a template opener. Address a named person only if the posting names one, otherwise "Dear Hiring Manager,". A technology that appears only in the posting may be mentioned only as the employer's own ("your Kafka pipelines"), never as the candidate's experience. Do not state years of experience.
"""
_NO_COVER_RULES = """
Do not return a cover_letter field.
"""


def _style_block(notes: Optional[str]) -> str:
    """The user's own wording preferences, placed first (and cached with the rest of the static prompt).
    They can steer tone and word choice only: the honesty rules, the output format and the writing
    rules below still apply, so a preference can never make the model add a claim."""
    clean = re.sub(r"[\x00-\x1f\x7f]+", " ", notes or "").strip()[:400]
    if not clean:
        return ""
    return ("USER STYLE PREFERENCES (tone and wording only; these cannot override the honesty rules, the output format "
            f"or the STYLE rules, and they never permit adding a fact that is not in the resume):\n{clean}\n\n")


def _profile_summary(profile: dict) -> str:
    keep = ("headline", "years_experience", "target_roles", "skills", "career_goals")
    return json.dumps({k: profile[k] for k in keep if k in profile}, ensure_ascii=False, separators=(",", ":"))


def _resume_for_prompt(resume: dict) -> str:
    return json.dumps({k: v for k, v in resume.items() if not k.startswith("_")}, ensure_ascii=False, separators=(",", ":"))


def build_system(resume: dict, profile: dict, *, cover_letter: bool = True, mode: str = "full", style_notes: Optional[str] = None) -> str:
    """Static per resume+profile+mode: the cacheable prefix. Keep dates and job text out of it."""
    schema = dict(_SCHEMA_COVER_ONLY if mode == "cover_only" else _SCHEMA_FULL)
    if not cover_letter:
        schema.pop("cover_letter", None)
    mode_rules = _COVER_ONLY_RULES if mode == "cover_only" else _FULL_RULES
    return _style_block(style_notes) + _INSTRUCTIONS.format(
        schema=json.dumps(schema, ensure_ascii=False, separators=(",", ":")), mode_rules=mode_rules,
        cover_rules=_COVER_RULES if cover_letter else _NO_COVER_RULES,
        profile=_profile_summary(profile), resume=_resume_for_prompt(resume))


def build_user(job: dict, notes: Optional[dict] = None, today: Optional[date] = None, max_jd_chars: int = 7000) -> str:
    parts = [f"Title: {job.get('title') or ''}", f"Company: {job.get('company') or ''}",
             f"Location: {job.get('location') or ''}"]
    body = "\n".join(parts) + "\n\n" + (job.get("description") or "")
    fenced = safety.wrap_untrusted(body, "job_posting", max_chars=max_jd_chars)
    extra = ""
    if notes:
        bits = []
        if notes.get("strengths"):
            bits.append("strengths found by our scorer: " + "; ".join(notes["strengths"]))
        if notes.get("gaps"):
            bits.append("gaps found by our scorer: " + "; ".join(notes["gaps"]))
        if bits:
            extra = "\nOur own assessment (trusted, use it to focus): " + " | ".join(bits) + "\n"
    return f"Today is {(today or date.today()).isoformat()}.{extra}\nTailor the application for this posting.\n{fenced}"


def prepare(resume: dict, profile: dict, job: dict, *, cover_letter: bool = True, mode: str = "full",
            notes: Optional[dict] = None, today: Optional[date] = None, style_notes: Optional[str] = None) -> dict:
    return {"system": safety.GUARD_INSTRUCTIONS + "\n\n" + build_system(resume, profile, cover_letter=cover_letter, mode=mode, style_notes=style_notes),
            "user": build_user(job, notes, today), "max_tokens": MAX_TAILOR_TOKENS,
            "mode": mode, "cover_letter": cover_letter, "schema_version": SCHEMA_VERSION}


def revision_user(draft_text: str, hard: list[dict], soft: list[dict]) -> str:
    """Second-pass request: show the model its own draft and exactly what was rejected."""
    def line(v: dict) -> str:
        return f"- {v['where']}: {v['detail']}"
    lines = ["Your previous answer was rejected by an automatic honesty check. Fix ONLY the problems below and return the full corrected JSON object again, in the same format, on one line.", ""]
    if hard:
        lines.append("MUST FIX (not supported by the resume):")
        lines += [line(v) for v in hard]
    if soft:
        lines += ["", "SHOULD FIX:"] + [line(v) for v in soft]
    lines += ["", "Your previous answer:", draft_text[:9000]]
    return "\n".join(lines)


# ---------------------------------------------------------------- validation

def _is_str(v, n: int) -> bool:
    return isinstance(v, str) and 0 < len(v.strip()) <= n


def validate_patch(obj, resume: dict, *, cover_letter: bool = True, mode: str = "full") -> dict:
    """Strictly validate the model's patch against the resume. Unknown keys are dropped."""
    problems: list[str] = []
    if not isinstance(obj, dict):
        raise TailorError(["top level must be a JSON object"])
    idx = resume_index(resume)
    work_ids = [w["id"] for w in resume.get("work", [])]
    proj_ids = [p["id"] for p in resume.get("projects", []) or []]
    add_ids = [a["id"] for a in resume.get("additional", []) or []]
    skill_names = [s["name"] for s in resume.get("skills", [])]
    skill_kw = {k.lower(): k for s in resume.get("skills", []) for k in s["keywords"]}
    out: dict = {}

    if mode == "full":
        h = obj.get("headline")
        if h is not None and not _is_str(h, MAX_HEADLINE):
            problems.append(f"headline: must be 1-{MAX_HEADLINE} characters")
        out["headline"] = h.strip() if isinstance(h, str) and h.strip() else None

        s = obj.get("summary")
        if s is not None and not _is_str(s, MAX_SUMMARY):
            problems.append(f"summary: must be null or 1-{MAX_SUMMARY} characters")
        out["summary"] = s.strip() if isinstance(s, str) and s.strip() else None

        work_out = []
        seen = set()
        for i, w in enumerate(obj.get("work") or []):
            if not isinstance(w, dict) or w.get("id") not in work_ids:
                problems.append(f"work[{i}].id: must be one of {work_ids}")
                continue
            wid = w["id"]
            if wid in seen:
                problems.append(f"work[{i}]: role {wid} appears twice")
            seen.add(wid)
            own = {h["id"] for h in next(x for x in resume["work"] if x["id"] == wid)["highlights"]}
            order = w.get("order")
            if order is not None:
                if not isinstance(order, list) or not all(isinstance(x, str) for x in order):
                    problems.append(f"work[{i}].order: must be a list of bullet ids")
                    order = None
                else:
                    bad = [x for x in order if x not in own]
                    if bad:
                        problems.append(f"work[{i}].order: ids {bad} do not belong to {wid}")
                    if len(set(order)) != len(order):
                        problems.append(f"work[{i}].order: duplicate ids")
                    order = [x for x in order if x in own]
            rewrite = w.get("rewrite") or {}
            if not isinstance(rewrite, dict):
                problems.append(f"work[{i}].rewrite: must be an object")
                rewrite = {}
            clean_rw = {}
            for bid, text in rewrite.items():
                if bid not in own:
                    problems.append(f"work[{i}].rewrite: {bid} does not belong to {wid}")
                elif not _is_str(text, MAX_REWRITE):
                    problems.append(f"work[{i}].rewrite.{bid}: must be 1-{MAX_REWRITE} characters")
                elif len(text) > 1.4 * len(idx[bid]["text"]) + 30:
                    problems.append(f"work[{i}].rewrite.{bid}: much longer than the original")
                elif re.sub(r"\s+", " ", text).strip() == re.sub(r"\s+", " ", idx[bid]["text"]).strip():
                    continue   # not actually a rewrite: keep the original and do not report it as a change
                else:
                    clean_rw[bid] = text.strip()
            show = w.get("show", True)
            if not isinstance(show, bool):
                problems.append(f"work[{i}].show: must be true or false")
                show = True
            work_out.append({"id": wid, "show": show, "order": order, "rewrite": clean_rw})
        if work_out and not any(w["show"] for w in work_out):
            problems.append("work: at least one role must be shown")
        out["work"] = work_out

        sk = obj.get("skills") or {}
        if not isinstance(sk, dict):
            problems.append("skills: must be an object")
            sk = {}
        order = sk.get("order") or []
        bad = [x for x in order if x not in skill_names] if isinstance(order, list) else ["?"]
        if bad:
            problems.append(f"skills.order: unknown groups {bad}")
            order = [x for x in order if x in skill_names] if isinstance(order, list) else []
        emph = sk.get("emphasize") or []
        if not isinstance(emph, list) or not all(isinstance(x, str) for x in emph):
            problems.append("skills.emphasize: must be a list of keywords")
            emph = []
        unknown = [x for x in emph if x.lower() not in skill_kw]
        if unknown:
            problems.append(f"skills.emphasize: {unknown} are not skills in the resume")
        out["skills"] = {"order": order, "emphasize": [skill_kw[x.lower()] for x in emph if x.lower() in skill_kw][:MAX_EMPHASIZE]}

        for key, valid in (("projects", proj_ids), ("additional", add_ids)):
            sec = obj.get(key) or {}
            inc = sec.get("include") if isinstance(sec, dict) else None
            if inc is not None:
                if not isinstance(inc, list) or not all(isinstance(x, str) for x in inc):
                    problems.append(f"{key}.include: must be a list of ids")
                    inc = None
                else:
                    bad = [x for x in inc if x not in valid]
                    if bad:
                        problems.append(f"{key}.include: unknown ids {bad}")
                    inc = [x for x in inc if x in valid]
            out[key] = {"include": inc}

    reqs = obj.get("requirements")
    clean_reqs = []
    if reqs is not None:
        if not isinstance(reqs, list):
            problems.append("requirements: must be a list")
            reqs = []
        for i, r in enumerate(reqs[:MAX_REQUIREMENTS]):
            if not isinstance(r, dict) or not _is_str(r.get("requirement"), 240):
                problems.append(f"requirements[{i}].requirement: required text")
                continue
            if r.get("status") not in STATUSES:
                problems.append(f"requirements[{i}].status: must be one of {sorted(STATUSES)}")
                continue
            ev = r.get("evidence")
            if r["status"] in ("matched", "bridged"):
                if not isinstance(ev, str) or ev not in idx:
                    problems.append(f"requirements[{i}].evidence: a {r['status']} requirement needs a real bullet id")
                    continue
            else:
                ev = None
            note = r.get("note")
            clean_reqs.append({"requirement": r["requirement"].strip(), "status": r["status"], "evidence": ev,
                               "note": note.strip()[:300] if isinstance(note, str) else ""})
    out["requirements"] = clean_reqs

    if cover_letter:
        cl = obj.get("cover_letter")
        if not isinstance(cl, dict):
            problems.append("cover_letter: required object")
        else:
            paras = cl.get("paragraphs")
            if not isinstance(paras, list) or not 2 <= len(paras) <= 5 or not all(_is_str(p, MAX_PARAGRAPH) for p in paras):
                problems.append(f"cover_letter.paragraphs: 2-5 paragraphs of up to {MAX_PARAGRAPH} characters")
                paras = []
            greeting = cl.get("greeting") or "Dear Hiring Manager,"
            closing = cl.get("closing") or "Sincerely,"
            if not _is_str(greeting, 120) or not _is_str(closing, 60):
                problems.append("cover_letter.greeting/closing: short text required")
                greeting, closing = "Dear Hiring Manager,", "Sincerely,"
            if sum(len(p.split()) for p in paras) > MAX_COVER_WORDS:
                problems.append(f"cover_letter: longer than {MAX_COVER_WORDS} words")
            out["cover_letter"] = {"greeting": greeting.strip(), "paragraphs": [p.strip() for p in paras], "closing": closing.strip()}
    else:
        out["cover_letter"] = None

    if problems:
        raise TailorError(problems)
    out["mode"] = mode
    return out


# ---------------------------------------------------------------- semantic fact check

_VERIFY_INSTRUCTIONS = """You are a strict fact checker for a job application. You are given a candidate's RESUME (JSON) and TEXT that was written for an application using that resume. Find claims in the TEXT that the resume does not back up.

Give each problem a verdict:
- "unsupported": the TEXT states something the resume does not contain, for example a technology or domain presented as "production" or "professional" experience when the resume only lists it as a skill; seniority, ownership, team size, scale, results or years the resume does not state; employers, projects or achievements that are not in the resume.
- "exaggerated": the claim has a basis in the resume but is stretched in degree or frequency (for example "every day", "extensive", "led" for something the resume only shows contributing to).

Do NOT list anything that IS supported by the resume: treat reasonable paraphrases and generalisations of work the resume shows as supported (for example, a bullet about a platform that integrates LLM engines supports "integrates model APIs into production software"). Also skip anything about the employer or the role, and forward-looking statements ("I would bring..."). Your verdict must agree with your reason: if the resume does support the claim, leave it out entirely. List only real problems, at most 6, each with the exact words quoted from the text and a reason of at most 15 words.

OUTPUT: one JSON object on ONE line, nothing else: {"issues":[{"where":"<label exactly as given>","quote":"<exact words from the text>","verdict":"unsupported|exaggerated","reason":"<short reason>"}]}. Use {"issues":[]} when there are no problems.

RESUME
"""

MAX_VERIFY_TOKENS = 700

# A small checker sometimes labels a claim "unsupported" while its own reason says the claim is fine.
# Such self-contradicting items are ignored, so they can never delete good text. ("is not supported" does not match.)
_SELF_CONTRADICTION = re.compile(
    r"\b(this is supported|that is supported|is supported by|so supported|not (?:a |an )?(?:true |real |actual )?issue|"
    r"no issue|no problem|resume (?:does )?(?:state|say)s? this)\b", re.I)


def texts_for_verification(patch: dict) -> dict[str, str]:
    """Label -> generated text, using the same labels as the audit."""
    out: dict[str, str] = {}
    if patch.get("headline"):
        out["headline"] = patch["headline"]
    if patch.get("summary"):
        out["summary"] = patch["summary"]
    for w in patch.get("work", []) or []:
        for bid, text in (w.get("rewrite") or {}).items():
            out[f"bullet {bid}"] = text
    cl = patch.get("cover_letter")
    if cl:
        for i, para in enumerate(cl["paragraphs"], 1):
            out[f"cover letter paragraph {i}"] = para
    return out


def verify_prompt(resume: dict, patch: dict) -> Optional[dict]:
    """The fact-check request, or None when the patch has no generated text to check."""
    texts = texts_for_verification(patch)
    if not texts:
        return None
    body = "\n".join(f"[{label}] {text}" for label, text in texts.items())
    return {"system": safety.GUARD_INSTRUCTIONS + "\n\n" + _VERIFY_INSTRUCTIONS + _resume_for_prompt(resume),
            "user": "TEXT to check (each line starts with its label):\n" + safety.wrap_untrusted(body, "generated_text", max_chars=12000),
            "max_tokens": MAX_VERIFY_TOKENS, "labels": list(texts)}


def parse_verification(raw, patch: dict) -> list[dict]:
    """Turn the checker's JSON into violations. Only the two verdicts are acted on: "unsupported" becomes a
    hard violation (the text is removed), "exaggerated" a soft one (kept, but shown to the human). Anything
    else, an unknown label, or an empty quote is ignored, so a checker that wrongly lists a supported claim
    cannot delete good text."""
    texts = texts_for_verification(patch)
    items = raw.get("issues") if isinstance(raw, dict) else None
    if not isinstance(items, list):
        raise TailorError(["fact check: reply has no 'issues' list"])
    out = []
    for it in items[:12]:
        if not isinstance(it, dict):
            continue
        where, quote, verdict, reason = it.get("where"), it.get("quote"), it.get("verdict"), it.get("reason")
        if where not in texts or verdict not in ("unsupported", "exaggerated") or not isinstance(quote, str) or not quote.strip():
            continue
        if isinstance(reason, str) and _SELF_CONTRADICTION.search(reason):
            continue
        q = quote.strip()[:200]
        why = f" ({str(reason).strip()[:120]})" if reason else ""
        if verdict == "unsupported":
            out.append({"where": where, "kind": "claim", "token": q,
                        "detail": f'fact check: "{q[:140]}" is not supported by the resume{why}'})
        else:
            out.append({"where": where, "kind": "stretch", "token": q,
                        "detail": f'fact check: "{q[:140]}" may overstate the resume{why}'})
    return out


# ---------------------------------------------------------------- audit

@dataclass
class Audit:
    hard: list[dict] = field(default_factory=list)
    soft: list[dict] = field(default_factory=list)
    fixed: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.hard

    def to_dict(self) -> dict:
        return {"ok": self.ok, "hard": self.hard, "soft": self.soft, "fixed": self.fixed}


def _titlecase_claims(text: str) -> list[str]:
    """Words that look like proper nouns or technologies: internal capitals, ALLCAPS, digits with
    letters, + # . inside, or Title-case words that do not start a sentence."""
    found = []
    for m in _TOKEN.finditer(text):
        raw = m.group(0)
        t = raw.rstrip(".")
        if not t or t.lower() in _SAFE_TITLECASE or re.fullmatch(r"\d[\d.,]*\+?%?", t):
            continue   # empty, an ordinary word, or a number (numbers are audited separately)
        before = text[:m.start()].rstrip()
        sentence_start = not before or before[-1] in ".!?:\n" or before.endswith(("(",))
        if re.search(r"[a-z][A-Z]", t) or (t.isupper() and len(t) >= 2 and not t.isdigit()):
            found.append(t)
        elif any(c in t for c in "+#") or ("." in t and re.search(r"[A-Za-z]", t)):
            found.append(t)
        elif re.search(r"[A-Za-z]\d|\d[A-Za-z]", t):
            found.append(t)
        elif t[0].isupper() and t[1:].islower() and not sentence_start and len(t) > 1:
            found.append(t)
    return found


def _is_technical(tok: str) -> bool:
    return bool(re.search(r"[a-z][A-Z]", tok) or (tok.isupper() and len(tok) >= 2) or any(c in tok for c in "+#.")
                or re.search(r"[A-Za-z]\d|\d[A-Za-z]", tok))


def _is_common_word(tok: str, job_text: str) -> bool:
    """A Title-case word is an ordinary noun (not a technology or a name) if the posting also uses it in
    lowercase somewhere: "applications" yes, "Kafka" no (technologies are capitalised everywhere)."""
    return bool(re.search(r"(?<![A-Za-z0-9])" + re.escape(tok.lower()) + r"(?![A-Za-z0-9])", job_text))


def _check_text(where: str, text: str, allowed: set[str], allowed_numbers: set[str], job_vocab: set[str],
                job_numbers: set[str], years: float, audit: Audit, *, soft_job_terms: bool,
                strict_numbers_to: Optional[set[str]] = None, soft_plain_job_terms: bool = False, job_text: str = "") -> None:
    """Audit one generated text. `allowed` are lower-cased tokens the text may use; terms found only
    in the posting are soft (cover letter) or hard (resume content) depending on soft_job_terms."""
    for tok in _titlecase_claims(text):
        low = tok.lower()
        parts = [p for p in re.split(r"[.]", low) if p] or [low]
        if low in allowed or all(p in allowed for p in parts):
            continue
        if low in job_vocab or all(p in job_vocab for p in parts):
            # an ordinary noun from the posting ("applications") is not a skill claim; a technology ("Kafka") is
            soft = soft_job_terms or (soft_plain_job_terms and not _is_technical(tok) and _is_common_word(tok, job_text))
            (audit.soft if soft else audit.hard).append(
                {"where": where, "kind": "term", "token": tok,
                 "detail": f"'{tok}' appears only in the job posting, not in the resume"
                           + (" (fine only if framed as the employer's own)" if soft_job_terms else "")})
        else:
            audit.hard.append({"where": where, "kind": "term", "token": tok,
                               "detail": f"'{tok}' is not in the resume (do not add technologies, tools or names)"})
    for n in numbers_in(text):
        base_allowed = strict_numbers_to if strict_numbers_to is not None else allowed_numbers
        if n in base_allowed:
            continue
        if n in job_numbers and soft_job_terms:
            audit.soft.append({"where": where, "kind": "number", "token": n,
                               "detail": f"the number {n} appears only in the job posting, not in the resume"})
        else:
            audit.hard.append({"where": where, "kind": "number", "token": n,
                               "detail": f"the number {n} is not supported by the resume"
                                         + (" (a rewrite must keep only the numbers of its own bullet)" if strict_numbers_to is not None else "")})
    for m in _YEARS.finditer(text):
        raw = m.group(1).lower()
        val = _WORD_NUMS.get(raw) if raw in _WORD_NUMS else float(raw)
        if val is not None and val > years + 0.5:
            if _ACKNOWLEDGES.search(_sentence_at(text, m.start())):
                audit.soft.append({"where": where, "kind": "years", "token": m.group(0),
                                   "detail": f"mentions {m.group(0)} (read as the posting's requirement or a stated gap; check the wording)"})
            else:
                audit.hard.append({"where": where, "kind": "years", "token": m.group(0),
                                   "detail": f"claims {m.group(0)} of experience; the resume supports about {years:g}"})
    low = text.lower()
    for phrase in CLICHES:
        if re.search(r"(?<![a-z])" + re.escape(phrase) + r"(?![a-z])", low):
            audit.soft.append({"where": where, "kind": "style", "token": phrase, "detail": f"cliche or filler: '{phrase}'"})


def audit(resume: dict, patch: dict, job: dict, profile: Optional[dict] = None) -> Audit:
    """Deterministic grounding audit of a validated patch. Also normalises dashes (recorded in .fixed)."""
    result = Audit()
    idx = resume_index(resume)
    vocab = vocabulary(resume)
    nums = numbers_in(" ".join(_strings(resume)))
    years = candidate_years(resume, profile)
    job_text = " ".join([job.get("title") or "", job.get("company") or "", job.get("description") or ""])
    job_vocab = {t.lower() for t in tokens(job_text)}
    job_numbers = numbers_in(job_text)
    own_job_tokens = {t.lower() for t in tokens(f"{job.get('title') or ''} {job.get('company') or ''}")}
    # the profile was confirmed by the user, so its role titles and skills are as grounded as the resume
    profile_vocab = {t.lower() for s_ in _strings({k: (profile or {}).get(k) for k in ("headline", "target_roles", "skills")})
                     for t in tokens(s_)}

    # rewrites: the tokens/numbers of the original bullet, its group, and the same role only
    for w in patch.get("work", []) or []:
        role = next((x for x in resume["work"] if x["id"] == w["id"]), None)
        role_text = " ".join(_strings(role)) if role else ""
        role_vocab = {t.lower() for t in tokens(role_text)}
        for bid, text in (w.get("rewrite") or {}).items():
            orig = idx[bid]["text"]
            allowed = role_vocab
            allowed_nums = numbers_in(orig) | numbers_in(idx[bid].get("group") or "")
            _check_text(f"bullet {bid}", text, allowed, nums, job_vocab, job_numbers, years, result,
                        soft_job_terms=False, strict_numbers_to=allowed_nums)
    texts = []
    if patch.get("headline"):
        texts.append(("headline", patch["headline"]))
    if patch.get("summary"):
        texts.append(("summary", patch["summary"]))
    for where, text in texts:
        _check_text(where, text, vocab | own_job_tokens | profile_vocab, nums, job_vocab, job_numbers, years, result,
                    soft_job_terms=False, soft_plain_job_terms=True, job_text=job_text)
    cl = patch.get("cover_letter")
    if cl:
        for i, para in enumerate(cl["paragraphs"], 1):
            _check_text(f"cover letter paragraph {i}", para, vocab | own_job_tokens | profile_vocab | _SAFE_TITLECASE, nums,
                        job_vocab, job_numbers, years, result, soft_job_terms=True)
        _check_text("cover letter greeting", cl["greeting"], vocab | own_job_tokens | profile_vocab | _SAFE_TITLECASE, nums,
                    job_vocab, job_numbers, years, result, soft_job_terms=True)
    # a requirement marked "matched"/"bridged" must cite a bullet that really exists (validated earlier)
    return result


def normalise_dashes(patch: dict) -> tuple[dict, list[str]]:
    """Replace em/en dashes in generated text with commas. Returns (patch, notes)."""
    notes: list[str] = []
    p = copy.deepcopy(patch)

    def fix(s: str, where: str) -> str:
        if _EM_DASH.search(s):
            notes.append(f"replaced a dash in {where}")
            return _EM_DASH.sub(", ", s)
        return s

    if p.get("headline"):
        p["headline"] = fix(p["headline"], "headline")
    if p.get("summary"):
        p["summary"] = fix(p["summary"], "summary")
    for w in p.get("work", []) or []:
        for bid, t in list((w.get("rewrite") or {}).items()):
            w["rewrite"][bid] = fix(t, f"bullet {bid}")
    cl = p.get("cover_letter")
    if cl:
        cl["paragraphs"] = [fix(t, f"cover letter paragraph {i}") for i, t in enumerate(cl["paragraphs"], 1)]
    return p, notes


# ---------------------------------------------------------------- repair

def _hits(sentence: str, tok: str) -> bool:
    """Does this sentence contain the offending token or quoted claim?"""
    if " " in tok.strip() or len(tok) > 24:      # a quoted phrase: compare loosely
        norm = lambda x: re.sub(r"\s+", " ", x).strip().lower().strip(" .,;:!?\"'")
        return norm(tok) in norm(sentence)
    return bool(re.search(r"(?<![A-Za-z0-9])" + re.escape(tok) + r"(?![A-Za-z0-9])", sentence))


def repair(patch: dict, result: Audit) -> tuple[dict, list[str]]:
    """Remove whatever the audit (or the fact check) rejected so what remains is grounded.
    Returns (patch, notes).

    Rewrites with a hard violation are dropped (the original bullet is used); a summary or headline
    with one is dropped; cover-letter sentences with one are removed, and a paragraph left empty is
    removed. Soft findings are kept.
    """
    p = copy.deepcopy(patch)
    notes: list[str] = []
    bad_where: dict[str, list[str]] = {}
    for v in result.hard:
        bad_where.setdefault(v["where"], []).append(v["token"])
    for w in p.get("work", []) or []:
        for bid in list((w.get("rewrite") or {}).keys()):
            if f"bullet {bid}" in bad_where:
                del w["rewrite"][bid]
                notes.append(f"kept the original wording of bullet {bid} (the rewrite added unsupported content)")
    if "summary" in bad_where and p.get("summary"):
        p["summary"] = None
        notes.append("removed the summary (it contained unsupported claims)")
    if "headline" in bad_where and p.get("headline"):
        p["headline"] = None
        notes.append("removed the headline (it contained unsupported content)")
    cl = p.get("cover_letter")
    if cl:
        kept = []
        for i, para in enumerate(cl["paragraphs"], 1):
            toks = bad_where.get(f"cover letter paragraph {i}")
            if not toks:
                kept.append(para)
                continue
            sentences = _SENTENCE_END.split(para)
            good = [s for s in sentences if not any(_hits(s, t) for t in toks)]
            if len(good) == len(sentences):
                good = []   # flagged, but the quote matches no single sentence: drop the paragraph rather than keep a claim
            removed = len(sentences) - len(good)
            if removed:
                notes.append(f"removed {removed} unsupported sentence(s) from cover letter paragraph {i}")
            if good:
                kept.append(" ".join(good))
        cl["paragraphs"] = kept
        if "cover letter greeting" in bad_where:
            cl["greeting"] = "Dear Hiring Manager,"
            notes.append("reset the cover letter greeting")
    return p, notes


# ---------------------------------------------------------------- apply

def _group_bullets(items: list[tuple[str, Optional[str]]]) -> list[dict]:
    """[(text, group)] -> [{"title": group|None, "bullets": [...]}], groups in first-appearance order."""
    groups: list[dict] = []
    for text, group in items:
        g = next((x for x in groups if x["title"] == group), None)
        if g is None:
            g = {"title": group, "bullets": []}
            groups.append(g)
        g["bullets"].append(text)
    return groups


def apply_patch(resume: dict, patch: Optional[dict], profile: Optional[dict] = None) -> dict:
    """Resume + patch -> the view to render. With no patch this is the base resume, unchanged."""
    r = {k: v for k, v in resume.items() if not k.startswith("_")}
    patch = patch or {}
    wmap = {w["id"]: w for w in patch.get("work", []) or []}
    work_view = []
    for role in r.get("work", []):
        spec = wmap.get(role["id"], {})
        if spec.get("show") is False:
            continue
        by_id = {h["id"]: h for h in role["highlights"]}
        order = spec.get("order")
        ids = order if order else [h["id"] for h in role["highlights"]]
        rewrites = spec.get("rewrite") or {}
        items = [(rewrites.get(i, by_id[i]["text"]), by_id[i].get("group")) for i in ids if i in by_id]
        work_view.append({"name": role["name"], "position": role["position"], "description": role.get("description"),
                          "location": role.get("location"), "start": role.get("startDate", ""), "end": role.get("endDate", ""),
                          "groups": _group_bullets(items)})
    sk = patch.get("skills") or {}
    emph = {e.lower() for e in sk.get("emphasize") or []}
    groups = {s["name"]: s for s in r.get("skills", [])}
    order = [n for n in (sk.get("order") or []) if n in groups] + [n for n in groups if n not in (sk.get("order") or [])]
    skills_view = [{"name": n, "keywords": groups[n]["keywords"],
                    "emphasize": [k for k in groups[n]["keywords"] if k.lower() in emph]} for n in order]
    inc_p = (patch.get("projects") or {}).get("include")
    projects_view = []
    for p in r.get("projects", []) or []:
        if inc_p is not None and p["id"] not in inc_p:
            continue
        projects_view.append({"name": p["name"], "url": p.get("url"), "start": p.get("startDate", ""), "end": p.get("endDate", ""),
                              "groups": _group_bullets([(h["text"], h.get("group")) for h in p["highlights"]])})
    inc_a = (patch.get("additional") or {}).get("include")
    additional = [a["text"] for a in r.get("additional", []) or [] if inc_a is None or a["id"] in inc_a]
    basics = dict(r.get("basics", {}))
    return {"basics": basics, "headline": patch.get("headline") or basics.get("label"), "summary": patch.get("summary"),
            "work": work_view, "projects": projects_view, "education": r.get("education", []),
            "skills": skills_view, "additional": additional}
