from datetime import datetime, timedelta, timezone

import pytest

import filters as F

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)
SETTINGS = {"exclude_keywords": ["staff", "principal", "lead", "manager", "head of", "intern", "internship"],
            "exclude_companies": [], "include_keywords": [], "max_age_days": 7, "allow_remote": True,
            "min_yoe": 1, "max_yoe": 4}


def job(**over):
    j = {"title": "Backend Engineer", "company": "Acme", "description": "Build APIs in Go.",
         "posted_at": "2026-10-09T00:00:00Z", "is_remote": False}
    j.update(over)
    return j


# ------------------------------------------------------------------ years of experience

@pytest.mark.parametrize("text,expected", [
    ("3-5 years of experience in Python", (3, 5)),
    ("3 to 5 years experience", (3, 5)),
    ("3–5 yrs of relevant experience", (3, 5)),
    ("5+ years of experience with Go", (5, None)),
    ("Minimum 3 years' experience required", (3, None)),
    ("at least 2 years of hands-on experience", (2, None)),
    ("0-2 years of experience", (0, 2)),
    ("3+ years of Go experience and 7+ years of overall experience", (3, None)),
    ("2-4 years experience, 6-8 years for the senior track", (2, 8)),
])
def test_extract_yoe(text, expected):
    assert F.extract_yoe(text) == expected


@pytest.mark.parametrize("text", [
    "", None, "We were founded 10 years ago and have 5 years of growth ahead.",
    "Our team of 12 years old startup", "Experience with Go and Redis", "Join 2000 engineers",
    "Contract is for 2 years", "7 days a week",
])
def test_extract_yoe_ignores_things_that_are_not_requirements(text):
    assert F.extract_yoe(text) == (None, None)


# ------------------------------------------------------------------ typos that glue words onto "years"

@pytest.mark.parametrize("text,expected", [
    ("- 5–8 yearsin software engineering, including at least 2 years of experience", (2, 8)),
    ("5-8 yearsin software engineering and experience with Go", (5, 8)),
    ("3+ yearsof experience in backend", (3, None)),
    ("5-8 yearsexperience in Python", (5, 8)),
    ("2 yrsof experience required", (2, None)),
    ("Requires 4 yearswith hands-on experience", (4, None)),
    ("7+ yearsas an engineer with proven experience", (7, None)),
])
def test_a_word_glued_onto_years_does_not_hide_the_requirement(text, expected):
    """A real posting wrote '5-8 yearsin software engineering'; a strict word boundary missed it and the job got past the filter."""
    assert F.extract_yoe(text) == expected


@pytest.mark.parametrize("text", [
    "5 yearly bonus and experience reviews", "3 yearbook experience", "Experience the 2 yearsomething plan",
    "10 yearsago experience was different", "We pay 5 yearly experience credits",
])
def test_look_alikes_are_still_not_requirements(text):
    assert F.extract_yoe(text) == (None, None)


@pytest.mark.parametrize("text,expected", [
    ("Requirements:\n- Working knowledge of AWS.\n- 5–8 yearsin software engineering, including at least 18 months building LLM systems.", (5, 8)),
    ("- 3+ years in backend development", (3, None)),
    ("• 2-4 years of Go and Postgres", (2, 4)),
    ("Qualifications: minimum 5 years building distributed systems", (5, None)),
    ("You have at least 3 years working on APIs", (3, None)),
    ("We are looking for someone with 6 years in platform engineering", (6, None)),
])
def test_requirements_written_without_the_word_experience_are_found(text, expected):
    """The real posting that slipped through never used the word 'experience' near its years requirement."""
    assert F.extract_yoe(text) == expected


@pytest.mark.parametrize("text", [
    "We have been around for 15 years in the market and are growing.",
    "Our company has 12 years in the industry. We are looking for people who love Go.",
    "- Founded 10 years ago in Pune",
    "- 10 years in business, 5 years of growth",
    "Benefits: 25 days leave, 2 years of free coaching",
    "The contract runs 3 years. Requirements: Python.",
])
def test_company_boilerplate_with_years_is_still_not_a_requirement(text):
    assert F.extract_yoe(text) == (None, None)


def test_the_filter_now_drops_the_posting_that_slipped_through():
    job = {"title": "AI Engineer (Backend)", "company": "TechGrove", "location": "Bengaluru",
           "description": "About the role. Requirements:\n- Working knowledge of AWS and CI/CD practices.\n- 5–8 yearsin software engineering, including at least 18 months building production LLM/AI systems.\nRecruitment notice: we may use AI tools."}
    settings = {"min_yoe": 1, "max_yoe": 4, "exclude_keywords": [], "include_keywords": [], "exclude_companies": [], "max_age_days": 7,
                "allow_remote": True, "locations": ["Bengaluru"]}
    d = F.decide(job, settings)
    assert d.yoe_min == 5 and d.yoe_max == 8
    assert not d.keep and "experience" in (d.reason or "").lower()


# ------------------------------------------------------------------ title keywords

@pytest.mark.parametrize("title,dropped", [
    ("Staff Software Engineer", True), ("Principal Engineer", True), ("Tech Lead - Backend", True),
    ("Engineering Manager", True), ("Head of Platform", True), ("Software Engineering Intern", True),
    ("Backend Engineer Internship", True),
    ("Leading Backend Engineer", False), ("International Backend Engineer", False),
    ("Senior Backend Engineer", False), ("Backend Engineer", False), ("Platform Engineer (Go)", False),
])
def test_exclude_keywords_match_whole_words_in_the_title_only(title, dropped):
    d = F.decide(job(title=title), SETTINGS, NOW)
    assert d.keep is not dropped
    if dropped:
        assert "excluded word in title" in d.reason


def test_exclude_keyword_in_the_description_does_not_matter():
    assert F.decide(job(description="You will lead a small team. Intern mentoring."), SETTINGS, NOW).keep


def test_multi_word_keyword_tolerates_extra_whitespace():
    assert not F.decide(job(title="Head   of Engineering"), SETTINGS, NOW).keep


# ------------------------------------------------------------------ companies / include / remote / age

def test_excluded_companies_use_a_normalised_name():
    s = dict(SETTINGS, exclude_companies=["Initech"])
    assert not F.decide(job(company="INITECH Pvt. Ltd."), s, NOW).keep
    assert F.decide(job(company="Acme"), s, NOW).keep
    assert F.decide(job(company="Co"), dict(SETTINGS, exclude_companies=["co"]), NOW).keep  # too short to be safe


def test_include_keywords_need_at_least_one_hit():
    s = dict(SETTINGS, include_keywords=["golang", "go"])
    assert F.decide(job(description="We use Go and Redis"), s, NOW).keep
    assert F.decide(job(title="Golang Developer", description="x"), s, NOW).keep
    d = F.decide(job(description="We use Python"), s, NOW)
    assert not d.keep and "include keywords" in d.reason
    assert F.decide(job(description="Going places"), dict(SETTINGS, include_keywords=["go"]), NOW).keep is False


def test_age_filter():
    old = (NOW - timedelta(days=9)).isoformat()
    d = F.decide(job(posted_at=old), SETTINGS, NOW)
    assert not d.keep and "9 days ago" in d.reason
    assert F.decide(job(posted_at=(NOW - timedelta(days=7)).isoformat()), SETTINGS, NOW).keep
    assert F.decide(job(posted_at=None), SETTINGS, NOW).keep          # unknown date is kept
    assert F.decide(job(posted_at="garbage"), SETTINGS, NOW).keep


def test_remote_filter_only_when_not_allowed():
    assert F.decide(job(is_remote=True), SETTINGS, NOW).keep
    d = F.decide(job(is_remote=True), dict(SETTINGS, allow_remote=False), NOW)
    assert not d.keep and "remote" in d.reason
    assert F.decide(job(is_remote=None), dict(SETTINGS, allow_remote=False), NOW).keep


# ------------------------------------------------------------------ experience range

@pytest.mark.parametrize("desc,keep", [
    ("5-8 years of experience", False),
    ("10+ years of experience", False),
    ("5+ years of experience", False),
    ("3-5 years of experience", True),     # overlaps your 1-4
    ("0-2 years of experience", True),
    ("1-3 years experience", True),
    ("4+ years of experience", True),      # exactly your limit
    ("3+ years of Go experience and 7+ years of overall experience", True),  # lowest minimum counts
    ("Freshers welcome", True),            # nothing stated, keep
    ("", True),
])
def test_experience_range_overlap(desc, keep):
    assert F.decide(job(description=desc), SETTINGS, NOW).keep is keep


def test_decision_reports_extracted_years_even_when_kept():
    d = F.decide(job(description="2-4 years of experience"), SETTINGS, NOW)
    assert d.keep and (d.yoe_min, d.yoe_max) == (2, 4)


def test_ceiling_below_your_minimum_drops_the_job():
    d = F.decide(job(description="0 to 0 years of experience; freshers only"), dict(SETTINGS, min_yoe=2), NOW)
    assert not d.keep and "at most 0" in d.reason
    d = F.decide(job(description="0-1 years of experience"), dict(SETTINGS, min_yoe=3), NOW)
    assert not d.keep and "at most 1" in d.reason


def test_no_limits_configured_means_no_experience_filter():
    s = {k: v for k, v in SETTINGS.items() if k not in ("min_yoe", "max_yoe")}
    assert F.decide(job(description="15+ years of experience"), s, NOW).keep


def test_filters_never_crash_on_missing_fields():
    assert F.decide({}, SETTINGS, NOW).keep
    assert F.decide({"title": None, "company": None, "description": None}, {}, NOW).keep
