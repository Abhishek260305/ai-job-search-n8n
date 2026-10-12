"""Job scoring: rubric, prompt builder, strict output validation, final decision.

The model call itself happens elsewhere (n8n or the CLI), so no API key lives here:

    prepare(profile, job)  ->  {system, user}        send these to the model
    finalize(model_text)   ->  validated + computed  store this in Postgres (record_score())

The model only reports raw dimension scores, gate verdicts and extracted facts. The overall
score, the verdict band and every pass/fail decision are computed here in code, so a model
(or a posting trying to manipulate it) cannot talk its way to a different outcome.

The scoring dimensions, weights, score bands and the idea of eligibility / language / location
gates are adapted from MadsLorentzen/ai-job-search (MIT); see THIRD_PARTY_NOTICES.md.
"""
from __future__ import annotations

import json
import re
from datetime import date
from typing import Optional

import safety

SCHEMA_VERSION = 1

WEIGHTS = {"technical": 0.30, "experience": 0.25, "behavioral": 0.15, "career": 0.30}
BANDS = [(75, "strong"), (60, "good"), (45, "moderate"), (30, "weak"), (0, "poor")]
GATE_VERDICTS = {
    "eligibility": {"PASS", "FAIL", "FLAG", "UNKNOWN"},
    "language": {"PASS", "FAIL", "FLAG"},
    "location": {"PASS", "FAIL", "FLAG"},
}
MAX_LIST_ITEMS = 3
MAX_ITEM_CHARS = 200
MAX_NOTE_CHARS = 300
MAX_REASON_CHARS = 400


class ProfileError(ValueError):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


class ScoreError(ValueError):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


# ---------------------------------------------------------------- profile

def _is_num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def validate_profile(p) -> dict:
    """Check the candidate profile (config/profile.json) and return it unchanged."""
    problems: list[str] = []
    if not isinstance(p, dict):
        raise ProfileError(["profile must be a JSON object"])
    if not isinstance(p.get("headline"), str) or not p["headline"].strip():
        problems.append("headline: required non-empty string")
    if not _is_num(p.get("years_experience")) or p["years_experience"] < 0:
        problems.append("years_experience: required non-negative number")
    skills = p.get("skills")
    if not isinstance(skills, dict) or not isinstance(skills.get("strong"), list) or not skills["strong"]:
        problems.append("skills.strong: required non-empty list")
    else:
        for tier in ("moderate", "weak"):
            if tier in skills and not isinstance(skills[tier], list):
                problems.append(f"skills.{tier}: must be a list")
    loc = p.get("location")
    if not isinstance(loc, dict) or not isinstance(loc.get("home"), str) or not loc["home"].strip():
        problems.append("location.home: required non-empty string")
    elif "remote_ok" in loc and not isinstance(loc["remote_ok"], bool):
        problems.append("location.remote_ok: must be true or false")
    langs = p.get("languages")
    if not isinstance(langs, list) or not langs:
        problems.append("languages: required list of {name, level}")
    else:
        for i, l in enumerate(langs):
            if not (isinstance(l, dict) and isinstance(l.get("name"), str) and isinstance(l.get("level"), str)):
                problems.append(f"languages[{i}]: needs string name and level")
    auth = p.get("work_authorization")
    if not isinstance(auth, list):
        problems.append("work_authorization: required list (use [] if there are no constraints)")
    else:
        for i, a in enumerate(auth):
            if not (isinstance(a, dict) and isinstance(a.get("country"), str) and isinstance(a.get("status"), str)):
                problems.append(f"work_authorization[{i}]: needs string country and status")
    for key in ("target_roles", "career_goals", "energizing", "draining", "deal_breakers"):
        if key in p and not (isinstance(p[key], list) and all(isinstance(x, str) for x in p[key])):
            problems.append(f"{key}: must be a list of strings")
    if problems:
        raise ProfileError(problems)
    return p


def load_profile(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return validate_profile(json.load(f))
    except FileNotFoundError:
        raise ProfileError([f"profile not found at {path}; copy config/profile.example.json to "
                            "config/profile.json and fill it in (or run /setup)"])
    except json.JSONDecodeError as e:
        raise ProfileError([f"profile is not valid JSON: {e}"])


# ---------------------------------------------------------------- prompt

_SCHEMA_EXAMPLE = {
    "scores": {"technical": 0, "experience": 0, "behavioral": 0, "career": 0},
    "gates": {
        "eligibility": {"verdict": "PASS|FAIL|FLAG|UNKNOWN", "note": ""},
        "language": {"verdict": "PASS|FAIL|FLAG", "note": ""},
        "location": {"verdict": "PASS|FAIL|FLAG", "note": ""},
    },
    "deadline": "YYYY-MM-DD or null",
    "posting_language": "language the posting is written in",
    "yoe": {"min": "integer or null", "max": "integer or null"},
    "strengths": ["up to 3 short phrases"],
    "gaps": ["up to 3 short phrases"],
    "reason": "one short sentence",
}

MAX_OUTPUT_TOKENS = 900   # headroom: a compact reply is about 300 tokens

_RUBRIC = """You triage job postings for one candidate. Score from the posting text and the candidate profile only; do no outside research. Be honest: a poor fit gets a low score even if the company is prestigious, and gaps are stated plainly. If the posting gives no signal for a dimension, score it 50 and say what is missing under gaps.

DIMENSIONS (integers 0-100):
- technical: how well the required and preferred skills match the candidate's skills. 80+ core requirements are primary skills; 60-79 most match with 1-2 learnable gaps; 40-59 partial match needing real upskilling; below 40 fundamental mismatch.
- experience: does the work history match the role's function (judge the work, not the title). 80+ same domain and role type; 60-79 related and transferable; 40-59 adjacent, would need to make the case; below 40 unrelated. Seniority well above the candidate's years is a gap.
- behavioral: fit with the working style the posting reveals (autonomy, pace, collaboration, on-call, travel) against what energizes and drains the candidate.
- career: does the role advance the candidate's goals and contain tasks that energize them. 80+ strongly aligned; 60-79 good but partial; 40-59 neutral; below 40 a step backwards.

GATES (give a verdict and a short note quoting the posting):
- eligibility: judge by the COUNTRY the job is in. If the candidate's work_authorization lists that country, PASS (FAIL only if the posting names a requirement they do not meet, such as a security clearance or a different citizenship). If the job is in a country the candidate is not authorized in: PASS when the posting offers visa sponsorship or relocation support; FAIL when it requires local work rights, citizenship or residency; UNKNOWN when it says nothing about work rights. If the job is remote with no country stated, UNKNOWN.
- language: compare languages the job itself requires (not the language the ad is written in) with the candidate's languages. FAIL if a required language is not listed at all. FLAG if the stated bar looks above the candidate's declared level. Otherwise PASS.
- location: FAIL if the role requires relocation the candidate rules out or an on-site location they cannot reach. FLAG for heavy travel or unclear arrangements. PASS for remote or a workable commute.

ALSO EXTRACT: the application deadline if the posting states one; the language the posting is written in; the minimum and maximum years of experience it asks for (integers or null); up to 3 strengths and up to 3 gaps, each grounded in the posting text; and a one-sentence reason.

BE TERSE. Gate notes are empty for PASS; for FAIL, FLAG or UNKNOWN give one short sentence (at most 20 words) quoting the posting. Strengths and gaps are short phrases (at most 12 words each). The reason is one sentence of at most 25 words.

OUTPUT: a single JSON object on ONE line (compact, no indentation, no line breaks, no markdown fences) and nothing before or after it, with exactly this shape (replace the placeholders):
"""


def _profile_for_prompt(profile: dict) -> str:
    keep = ("headline", "years_experience", "target_roles", "skills", "experience_domains", "career_goals",
            "energizing", "draining", "deal_breakers", "location", "languages", "work_authorization")
    return json.dumps({k: profile[k] for k in keep if k in profile}, ensure_ascii=False, separators=(",", ":"))


def build_system(profile: dict) -> str:
    """Static per profile, so it is a good prompt-caching prefix (keep dates out of it)."""
    return (safety.GUARD_INSTRUCTIONS + "\n\n" + _RUBRIC + json.dumps(_SCHEMA_EXAMPLE, separators=(",", ":"))
            + "\n\nCANDIDATE PROFILE:\n" + _profile_for_prompt(profile))


def build_user(job: dict, max_jd_chars: int = 6000, today: Optional[date] = None) -> str:
    """The posting, sanitised and fenced. Title/company/location come from the posting too."""
    parts = [f"Title: {job.get('title') or ''}", f"Company: {job.get('company') or ''}",
             f"Location: {job.get('location') or ''}"]
    posted = job.get("posted_at")
    if posted:
        parts.append(f"Posted: {posted}")
    body = "\n".join(parts) + "\n\n" + (job.get("description") or "")
    fenced = safety.wrap_untrusted(body, "job_posting", max_chars=max_jd_chars)
    return f"Today is {(today or date.today()).isoformat()}.\nEvaluate this posting.\n{fenced}"


def prepare(profile: dict, job: dict, max_jd_chars: int = 6000, today: Optional[date] = None) -> dict:
    validate_profile(profile)
    return {"system": build_system(profile), "user": build_user(job, max_jd_chars, today),
            "max_tokens": MAX_OUTPUT_TOKENS, "schema_version": SCHEMA_VERSION}


# ---------------------------------------------------------------- parsing + validation

def parse_model_json(text: str):
    """Pull the first JSON object out of a model reply (tolerates code fences and chatter)."""
    if not isinstance(text, str) or "{" not in text:
        raise ScoreError(["no JSON object found in the model output"])
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[text.index("{"):])
    except json.JSONDecodeError as e:
        raise ScoreError([f"model output is not valid JSON: {e.msg}"])
    return obj


def _clip(s: str, n: int) -> str:
    return re.sub(r"\s+", " ", s).strip()[:n]


def validate_result(obj) -> dict:
    """Strictly validate the model's JSON. Unknown keys are dropped; wrong types are errors."""
    problems: list[str] = []
    if not isinstance(obj, dict):
        raise ScoreError(["top level must be a JSON object"])

    scores: dict[str, int] = {}
    raw_scores = obj.get("scores")
    if not isinstance(raw_scores, dict):
        problems.append("scores: required object")
    else:
        for dim in WEIGHTS:
            v = raw_scores.get(dim)
            if not _is_num(v) or not 0 <= v <= 100:
                problems.append(f"scores.{dim}: required number between 0 and 100")
            else:
                scores[dim] = int(round(v))

    gates: dict[str, dict] = {}
    raw_gates = obj.get("gates")
    if not isinstance(raw_gates, dict):
        problems.append("gates: required object")
    else:
        for name, allowed in GATE_VERDICTS.items():
            g = raw_gates.get(name)
            verdict = g.get("verdict") if isinstance(g, dict) else None
            if not isinstance(verdict, str) or verdict.upper() not in allowed:
                problems.append(f"gates.{name}.verdict: must be one of {sorted(allowed)}")
                continue
            note = g.get("note", "")
            gates[name] = {"verdict": verdict.upper(),
                           "note": _clip(note, MAX_NOTE_CHARS) if isinstance(note, str) else ""}

    deadline = obj.get("deadline")
    if deadline is not None:
        try:
            if not (isinstance(deadline, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", deadline)):
                raise ValueError
            date.fromisoformat(deadline)
        except ValueError:
            problems.append("deadline: must be YYYY-MM-DD or null")

    lang = obj.get("posting_language")
    if lang is not None and not isinstance(lang, str):
        problems.append("posting_language: must be a string or null")

    yoe = obj.get("yoe")
    ymin = ymax = None
    if yoe is not None:
        if not isinstance(yoe, dict):
            problems.append("yoe: must be an object {min, max} or null")
        else:
            for key in ("min", "max"):
                v = yoe.get(key)
                if v is not None and not (_is_num(v) and 0 <= v <= 60):
                    problems.append(f"yoe.{key}: must be a number 0-60 or null")
            ymin = int(yoe["min"]) if _is_num(yoe.get("min")) and 0 <= yoe["min"] <= 60 else None
            ymax = int(yoe["max"]) if _is_num(yoe.get("max")) and 0 <= yoe["max"] <= 60 else None
            if ymin is not None and ymax is not None and ymin > ymax:
                problems.append("yoe: min is greater than max")

    lists: dict[str, list[str]] = {}
    for key in ("strengths", "gaps"):
        v = obj.get(key)
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            problems.append(f"{key}: required list of strings")
        else:
            lists[key] = [_clip(x, MAX_ITEM_CHARS) for x in v if x.strip()][:MAX_LIST_ITEMS]

    reason = obj.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        problems.append("reason: required non-empty string")

    if problems:
        raise ScoreError(problems)
    return {"scores": scores, "gates": gates, "deadline": deadline,
            "posting_language": _clip(lang, 40) if lang else None, "yoe_min": ymin, "yoe_max": ymax,
            "strengths": lists["strengths"], "gaps": lists["gaps"], "reason": _clip(reason, MAX_REASON_CHARS)}


# ---------------------------------------------------------------- decision (computed in code)

def overall_score(scores: dict) -> int:
    return int(round(sum(WEIGHTS[d] * scores[d] for d in WEIGHTS)))


def band(score: int) -> str:
    return next(name for floor, name in BANDS if score >= floor)


def finalize(model_text, threshold: int = 70) -> dict:
    """Validate a model reply and compute everything that decides what happens to the job."""
    obj = parse_model_json(model_text) if isinstance(model_text, str) else model_text
    r = validate_result(obj)
    score = overall_score(r["scores"])

    failed = [f"{g}: {v['note'] or 'failed'}" for g, v in r["gates"].items() if v["verdict"] == "FAIL"]
    warnings = [f"{g}: {v['note'] or v['verdict'].lower()}" for g, v in r["gates"].items()
                if v["verdict"] in ("FLAG", "UNKNOWN")]
    excluded = bool(failed)
    verdict = "excluded" if excluded else band(score)
    above = (not excluded) and score >= threshold

    return {
        "score": score, "verdict": verdict, "scores": r["scores"], "gates": r["gates"],
        "strengths": r["strengths"], "gaps": r["gaps"], "deadline": r["deadline"],
        "posting_language": r["posting_language"], "yoe_min": r["yoe_min"], "yoe_max": r["yoe_max"],
        "score_reason": r["reason"], "excluded": excluded, "exclusion_reasons": failed,
        "warnings": warnings, "threshold": threshold, "above_threshold": above,
        "next_status": "filtered_out" if excluded else ("shortlisted" if above else "scored"),
        "filter_reason": "gate failed: " + "; ".join(failed) if excluded else None,
        "schema_version": SCHEMA_VERSION,
    }
