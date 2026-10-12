"""Profile tooling used by /setup and the CLI. Pure stdlib.

  validate_resume()   strict check of config/resume.json (the source of truth for tailoring).
                      Highlights may carry an optional "group" (a sub-heading such as a project name).
  validate_search()   check of config/search.json (the free rule filters + what to search for, plus the
                      optional company "boards" and "board_title_words")
  parse_linkedin_export()
                      read a LinkedIn "Get a copy of your data" export deterministically.
                      Only a whitelist of columns from five files is read; everything else
                      (address, birth date, messages, connections, ad targeting, ...) is ignored.
  find_conflicts()    compare a resume with the LinkedIn data and list disagreements for the
                      user to resolve, instead of silently picking a side.
"""
from __future__ import annotations

import csv
import re
from pathlib import Path
from typing import Optional

import sources  # stdlib-only registry; boards.py is deliberately NOT imported (it pulls in httpx)

# Every single source name (group names like "boards" or "feeds" are accepted by the CLI only, not in the file).
KNOWN_SOURCES = set(sources.ALL)
MAX_BOARD_SLUGS = 300
MAX_TITLE_WORDS = 50
MAX_TITLE_WORD_LEN = 50
MAX_BOARD_NAME_LEN = 100

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
_DATE = re.compile(r"^\d{4}(-(0[1-9]|1[0-2]))?$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ProfileToolsError(ValueError):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


# ---------------------------------------------------------------- dates

def norm_date(s: Optional[str]) -> str:
    """'Jan 2026' -> '2026-01', '2022' -> '2022', 'Ongoing'/'' -> ''. Unparseable -> ''."""
    if not s:
        return ""
    s = s.strip()
    if re.fullmatch(r"\d{4}", s):
        return s
    m = re.fullmatch(r"([A-Za-z]{3})[A-Za-z]*\.?\s+(\d{4})", s)
    if m and m.group(1).lower() in _MONTHS:
        return f"{m.group(2)}-{_MONTHS[m.group(1).lower()]:02d}"
    if _DATE.match(s):
        return s
    return ""


def _month_index(d: str) -> Optional[int]:
    """Months since year 0 for 'YYYY' or 'YYYY-MM' (a bare year counts as January)."""
    if not d:
        return None
    y, _, m = d.partition("-")
    return int(y) * 12 + (int(m) - 1 if m else 0)


# ---------------------------------------------------------------- resume

def _clean(d: dict) -> dict:
    return {k: v for k, v in d.items() if not k.startswith("_")}


def validate_resume(r) -> dict:
    """Validate config/resume.json. Keys starting with '_' (comments) are ignored."""
    problems: list[str] = []
    if not isinstance(r, dict):
        raise ProfileToolsError(["resume must be a JSON object"])
    r = _clean(r)
    seen_ids: set[str] = set()

    def need_id(obj: dict, where: str) -> None:
        i = obj.get("id")
        if not isinstance(i, str) or not i.strip():
            problems.append(f"{where}.id: required non-empty string")
        elif i in seen_ids:
            problems.append(f"{where}.id: duplicate id '{i}'")
        else:
            seen_ids.add(i)

    def check_highlights(obj: dict, where: str) -> None:
        hl = obj.get("highlights")
        if not isinstance(hl, list) or not hl:
            problems.append(f"{where}.highlights: required non-empty list")
            return
        for j, h in enumerate(hl):
            w = f"{where}.highlights[{j}]"
            if not isinstance(h, dict):
                problems.append(f"{w}: must be an object with id and text")
                continue
            need_id(h, w)
            t = h.get("text")
            if not isinstance(t, str) or not t.strip():
                problems.append(f"{w}.text: required non-empty string")
            elif len(t) > 600:
                problems.append(f"{w}.text: longer than 600 characters")

    b = r.get("basics")
    if not isinstance(b, dict):
        problems.append("basics: required object")
    else:
        if not isinstance(b.get("name"), str) or not b["name"].strip():
            problems.append("basics.name: required non-empty string")
        if not isinstance(b.get("email"), str) or not _EMAIL.match(b["email"]):
            problems.append("basics.email: required valid email")

    work = r.get("work")
    if not isinstance(work, list) or not work:
        problems.append("work: required non-empty list")
    else:
        for i, w in enumerate(work):
            where = f"work[{i}]"
            if not isinstance(w, dict):
                problems.append(f"{where}: must be an object")
                continue
            need_id(w, where)
            for key in ("name", "position"):
                if not isinstance(w.get(key), str) or not w[key].strip():
                    problems.append(f"{where}.{key}: required non-empty string")
            if not _DATE.match(str(w.get("startDate", ""))):
                problems.append(f"{where}.startDate: required YYYY or YYYY-MM")
            end = w.get("endDate", "")
            if end not in ("", None) and not _DATE.match(str(end)):
                problems.append(f"{where}.endDate: must be '' (ongoing), YYYY or YYYY-MM")
            check_highlights(w, where)

    edu = r.get("education")
    if not isinstance(edu, list):
        problems.append("education: required list")
    else:
        for i, e in enumerate(edu):
            if not isinstance(e, dict) or not isinstance(e.get("institution"), str) or not e["institution"].strip():
                problems.append(f"education[{i}].institution: required non-empty string")

    skills = r.get("skills")
    if not isinstance(skills, list) or not skills:
        problems.append("skills: required non-empty list of {name, keywords}")
    else:
        for i, s in enumerate(skills):
            if not (isinstance(s, dict) and isinstance(s.get("name"), str) and s["name"].strip()
                    and isinstance(s.get("keywords"), list) and s["keywords"]
                    and all(isinstance(k, str) for k in s["keywords"])):
                problems.append(f"skills[{i}]: needs a name and a non-empty list of keyword strings")

    projects = r.get("projects", [])
    if not isinstance(projects, list):
        problems.append("projects: must be a list")
    else:
        for i, p in enumerate(projects):
            where = f"projects[{i}]"
            if not isinstance(p, dict):
                problems.append(f"{where}: must be an object")
                continue
            need_id(p, where)
            if not isinstance(p.get("name"), str) or not p["name"].strip():
                problems.append(f"{where}.name: required non-empty string")
            check_highlights(p, where)

    additional = r.get("additional", [])
    if not isinstance(additional, list):
        problems.append("additional: must be a list of {id, text}")
    else:
        for i, a in enumerate(additional):
            where = f"additional[{i}]"
            if not isinstance(a, dict):
                problems.append(f"{where}: must be an object with id and text")
                continue
            need_id(a, where)
            if not isinstance(a.get("text"), str) or not a["text"].strip():
                problems.append(f"{where}.text: required non-empty string")

    if problems:
        raise ProfileToolsError(problems)
    return r


# ---------------------------------------------------------------- search settings

def _str_list(obj: dict, key: str, problems: list[str]) -> list[str]:
    v = obj.get(key, [])
    if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
        problems.append(f"{key}: must be a list of non-empty strings")
        return []
    if any(ord(ch) < 32 or ord(ch) == 127 for x in v for ch in x):    # a NUL cannot be stored in text; the rest cannot be a title or place
        problems.append(f"{key}: must not contain control characters")
        return []
    return [x.strip() for x in v]


def _safe(value, limit: int = 40) -> str:
    """A short repr-style form of an untrusted value for a problem message: truncated, control characters escaped."""
    return repr(str(value)[:limit])


def _board_entry(system: str, entry, problems: list[str]):
    """One entry of boards[system]: a slug string or {"slug", "name"}. Returns the normalised entry or None."""
    where = f"boards.{system}"
    if isinstance(entry, dict):
        extra = sorted(str(k)[:40] for k in entry if k not in ("slug", "name"))
        if extra:
            problems.append(f'{where}: unexpected key(s) {extra} in an entry; only "slug" and "name" are allowed')
            return None
        slug, name = entry.get("slug"), entry.get("name")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > MAX_BOARD_NAME_LEN:
            problems.append(f'{where}: "name" must be a non-empty string of at most {MAX_BOARD_NAME_LEN} characters')
            return None
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):    # checked on the raw text: strip() must not hide an edge one
            problems.append(f"{where}: name must not contain control characters")
            return None
    elif isinstance(entry, str):
        slug, name = entry, None
    else:
        problems.append(f'{where}: each entry must be a slug string or {{"slug": ..., "name": ...}}, not {type(entry).__name__}')
        return None
    if not isinstance(slug, str):
        problems.append(f'{where}: "slug" must be a string, not {type(slug).__name__}')
        return None
    if not sources.valid_slug(slug):
        problems.append(f"{where}: invalid slug {_safe(slug)}; use letters, digits, '.', '_' or '-' "
                        "(at most 64 characters, starting with a letter or digit)")
        return None
    return slug if name is None else {"slug": slug, "name": name.strip()}


def _boards(s: dict, problems: list[str]) -> dict:
    """The optional "boards" map: {system: [slug | {"slug", "name"}]}. Duplicate slugs per system are dropped (first wins)."""
    raw = s.get("boards", {})
    if not isinstance(raw, dict):
        problems.append('boards: must be an object like {"greenhouse": ["slug", ...]}')
        return {}
    out: dict = {}
    total = 0
    for system, entries in raw.items():
        if system not in sources.BOARD_SYSTEMS:
            problems.append(f"boards: unknown system {_safe(system)}; choose from {list(sources.BOARD_SYSTEMS)}")
            continue
        if not isinstance(entries, list):
            problems.append(f"boards.{system}: must be a list of slugs")
            continue
        seen: set = set()
        kept: list = []
        for entry in entries:
            norm = _board_entry(system, entry, problems)
            if norm is None:
                continue
            slug = norm if isinstance(norm, str) else norm["slug"]
            if slug in seen:
                continue
            seen.add(slug)
            kept.append(norm)
        out[system] = kept
        total += len(kept)
    if total > MAX_BOARD_SLUGS:
        problems.append(f"boards: at most {MAX_BOARD_SLUGS} slugs in total across all systems, found {total}")
    return out


def _title_words(s: dict, problems: list[str]) -> list[str]:
    words = _str_list(s, "board_title_words", problems)
    if len(words) > MAX_TITLE_WORDS:
        problems.append(f"board_title_words: at most {MAX_TITLE_WORDS} words")
    if any(len(w) > MAX_TITLE_WORD_LEN for w in words):
        problems.append(f"board_title_words: each word must be at most {MAX_TITLE_WORD_LEN} characters")
    return words


# The keys of a search profile, in the order config/search.json lists them (and `search-profile pull` writes them).
PROFILE_KEYS = ("search_terms", "locations", "sources", "allow_remote", "min_yoe", "max_yoe", "include_keywords",
                "exclude_keywords", "exclude_companies", "max_age_days", "score_threshold", "boards", "board_title_words")


def settings_from_row(row: dict) -> dict:
    """A search_profile row as the dict validate_search() returns (and config/search.json holds)."""
    out = {k: row[k] for k in PROFILE_KEYS if k in row}
    for k in ("search_terms", "locations", "sources", "include_keywords", "exclude_keywords", "exclude_companies", "board_title_words"):
        out[k] = list(out.get(k) or [])
    out["boards"] = dict(out.get("boards") or {})
    return out


def collectable_problem(enabled: list, boards: dict) -> Optional[str]:
    """A message when the sources that are on could not find anything: only company boards are on and none lists a
    company, so every collect run would do nothing. (Feeds and the job-site scrapers need no slugs.) None when fine."""
    if enabled and all(name in sources.BOARD_SYSTEMS for name in enabled) \
            and not any(boards.get(system) for system in sources.BOARD_SYSTEMS):
        return ("sources: only company boards are on and none lists a company, so a collect run would find nothing: "
                "add a company, or switch another source on")
    return None


def validate_search(s) -> dict:
    """Validate config/search.json and return a normalised dict with defaults filled in."""
    problems: list[str] = []
    if not isinstance(s, dict):
        raise ProfileToolsError(["search settings must be a JSON object"])
    s = _clean(s)
    out = {
        "search_terms": _str_list(s, "search_terms", problems),
        "locations": _str_list(s, "locations", problems),
        "sources": _str_list(s, "sources", problems) or ["indeed", "naukri"],
        "include_keywords": _str_list(s, "include_keywords", problems),
        "exclude_keywords": _str_list(s, "exclude_keywords", problems),
        "exclude_companies": _str_list(s, "exclude_companies", problems),
        "boards": _boards(s, problems),
        "board_title_words": _title_words(s, problems),
    }
    if not out["search_terms"]:
        problems.append("search_terms: at least one job title or keyword is required")
    if not out["locations"]:
        problems.append("locations: at least one location is required")
    bad = [x for x in out["sources"] if x not in KNOWN_SOURCES]
    if bad:
        problems.append(f"sources: unknown {bad}; choose from {sorted(KNOWN_SOURCES)}")

    allow_remote = s.get("allow_remote", True)
    if not isinstance(allow_remote, bool):
        problems.append("allow_remote: must be true or false")
    out["allow_remote"] = allow_remote

    for key, default, lo, hi in (("min_yoe", None, 0, 60), ("max_yoe", None, 0, 60),
                                 ("max_age_days", 7, 1, 90), ("score_threshold", 70, 0, 100)):
        v = s.get(key, default)
        if v is not None and not (isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi):
            problems.append(f"{key}: must be an integer {lo}-{hi}" + (" or null" if default is None else ""))
        out[key] = v
    if isinstance(out["min_yoe"], int) and isinstance(out["max_yoe"], int) and out["min_yoe"] > out["max_yoe"]:
        problems.append("min_yoe is greater than max_yoe")

    if problems:
        raise ProfileToolsError(problems)
    return out


# ---------------------------------------------------------------- LinkedIn export

_LINKEDIN_FILES = {
    "profile": ("Profile.csv", ("First Name", "Last Name", "Headline", "Summary", "Industry", "Websites")),
    "positions": ("Positions.csv", ("Company Name", "Title", "Description", "Location", "Started On", "Finished On")),
    "education": ("Education.csv", ("School Name", "Start Date", "End Date", "Notes", "Degree Name", "Activities")),
    "skills": ("Skills.csv", ("Name",)),
    "projects": ("Projects.csv", ("Title", "Description", "Url", "Started On", "Finished On")),
}


def _read_csv(path: Path, columns: tuple) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        return [{c: (row.get(c) or "").strip() for c in columns} for row in csv.DictReader(f)]


def parse_linkedin_export(folder: str) -> dict:
    """Parse a LinkedIn data export folder. Reads only the whitelisted files and columns."""
    root = Path(folder)
    if not root.is_dir():
        raise ProfileToolsError([f"{folder} is not a folder (unzip the LinkedIn export first)"])
    data: dict = {"source": "linkedin_export", "files_read": [], "files_missing": []}
    raw: dict[str, list[dict]] = {}
    for key, (fname, cols) in _LINKEDIN_FILES.items():
        path = root / fname
        if path.is_file():
            raw[key] = _read_csv(path, cols)
            data["files_read"].append(fname)
        else:
            raw[key] = []
            data["files_missing"].append(fname)
    data["files_ignored"] = sum(1 for p in root.iterdir() if p.name not in {f for f, _ in _LINKEDIN_FILES.values()})

    p = raw["profile"][0] if raw["profile"] else {}
    data["profile"] = {"first_name": p.get("First Name", ""), "last_name": p.get("Last Name", ""),
                       "headline": p.get("Headline", ""), "summary": p.get("Summary", ""),
                       "industry": p.get("Industry", ""), "websites": p.get("Websites", "")}
    data["positions"] = [{"company": r["Company Name"], "title": r["Title"], "description": r["Description"],
                          "location": r["Location"], "start": norm_date(r["Started On"]),
                          "end": norm_date(r["Finished On"])} for r in raw["positions"]]
    data["education"] = [{"school": r["School Name"], "degree": r["Degree Name"], "start": norm_date(r["Start Date"]),
                          "end": norm_date(r["End Date"]), "notes": r["Notes"]} for r in raw["education"]]
    seen: set[str] = set()
    skills = []
    for r in raw["skills"]:
        name = re.sub(r"\s*\([^)]*\)\s*", " ", r["Name"]).strip()
        if name and name.lower() not in seen:
            seen.add(name.lower())
            skills.append(name)
    data["skills"] = skills
    data["projects"] = [{"title": r["Title"], "description": r["Description"], "url": r["Url"],
                         "start": norm_date(r["Started On"]), "end": norm_date(r["Finished On"])} for r in raw["projects"]]
    return data


# ---------------------------------------------------------------- conflicts

def _company_key(name: str) -> str:
    n = re.sub(r"\([^)]*\)", " ", (name or "").lower())
    n = re.sub(r"\b(pvt|private|ltd|limited|inc|llc|llp|corp|corporation|co)\b\.?", " ", n)
    return re.sub(r"[^a-z0-9]+", "", n)


def _span(start: str, end: str) -> str:
    return f"{start or '?'} to {end or 'present'}"


def find_conflicts(resume: dict, linkedin: dict) -> list[dict]:
    """Disagreements between resume work history and LinkedIn positions, for the user to resolve.

    Roles are grouped by company. A company on only one side is reported; differing numbers of
    roles are reported as one 'multiple_roles' item; equal numbers are paired by start date and
    compared on title, start and end. Each item: {"kind", "message", "resume", "linkedin"}.
    Nothing is changed automatically.
    """
    out: list[dict] = []
    resume = _clean(resume)

    def group(items, company_of):
        groups: dict[str, list] = {}
        for n, it in enumerate(items):
            # an entry with no usable company name can never match another; keep it visible
            k = _company_key(company_of(it)) or f"?unnamed{n}"
            groups.setdefault(k, []).append(it)
        return groups

    rg = group(resume.get("work", []), lambda w: w.get("name", ""))
    lg = group(linkedin.get("positions", []), lambda p: p["company"])

    def merged_key(key, other):  # tolerate "acme" vs "acmeplatform"
        if key.startswith("?"):
            return None
        return next((o for o in other if not o.startswith("?") and (o == key or o in key or key in o)), None)

    used_l: set[str] = set()
    for rkey, rolls in rg.items():
        lkey = merged_key(rkey, lg)
        r_descs = [f"{w.get('position')} at {w.get('name')} ({_span(w.get('startDate', ''), w.get('endDate', ''))})" for w in rolls]
        if lkey is None:
            for d in r_descs:
                out.append({"kind": "resume_only", "resume": d, "linkedin": None,
                            "message": f"Resume lists '{d}' but LinkedIn has no position at that company."})
            continue
        used_l.add(lkey)
        lrolls = lg[lkey]
        l_descs = [f"{p['title']} ({_span(p['start'], p['end'])})" for p in lrolls]
        if len(rolls) != len(lrolls):
            out.append({"kind": "multiple_roles", "resume": r_descs, "linkedin": l_descs,
                        "message": f"The resume shows {len(rolls)} role(s) at this company and LinkedIn shows "
                                   f"{len(lrolls)}. Check titles and dates are accurate for each period."})
            continue
        for w, p, rd in zip(sorted(rolls, key=lambda x: x.get("startDate", "")), sorted(lrolls, key=lambda x: x["start"]),
                            sorted(r_descs)):
            ld = f"{p['title']} ({_span(p['start'], p['end'])})"
            if w.get("position", "").strip().lower() != p["title"].strip().lower():
                out.append({"kind": "title_differs", "resume": rd, "linkedin": [ld],
                            "message": f"Job title differs: resume '{w.get('position')}' vs LinkedIn '{p['title']}'."})
            a, b = _month_index(w.get("startDate", "")), _month_index(p["start"])
            if a is not None and b is not None and abs(a - b) > 1:
                out.append({"kind": "start_differs", "resume": rd, "linkedin": [ld],
                            "message": f"Start date differs: resume {w.get('startDate')} vs LinkedIn {p['start']}."})
            re_, le = _month_index(w.get("endDate", "")), _month_index(p["end"])
            if (re_ is None) != (le is None) or (re_ is not None and abs(re_ - le) > 1):
                out.append({"kind": "end_differs", "resume": rd, "linkedin": [ld],
                            "message": f"End date differs: resume {w.get('endDate') or 'present'} vs LinkedIn {p['end'] or 'present'}."})

    for lkey, lrolls in lg.items():
        if lkey not in used_l:
            for p in lrolls:
                d = f"{p['title']} at {p['company']} ({_span(p['start'], p['end'])})"
                out.append({"kind": "linkedin_only", "resume": None, "linkedin": d,
                            "message": f"LinkedIn lists '{p['title']}' at {p['company']} but the resume does not."})
    return out
