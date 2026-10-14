"""The apply assist's fact sheet (step 10a): config/apply.json is read strictly, what is missing says what it means, the years per
skill come from the resume's own dates, and the sheet is compared with the resume and the profile. Pure functions: no network, no model."""
import copy
import json
import random
from datetime import date
from pathlib import Path

import pytest

import apply_sheet as A

TODAY = date(2026, 10, 14)
EXAMPLE = json.loads((Path(__file__).resolve().parents[2] / "config" / "apply.example.json").read_text(encoding="utf-8"))

RESUME = {
    "basics": {"name": "Jane Doe", "email": "jane@example.com", "phone": "+91 98765 43210"},
    "work": [
        {"id": "w1", "name": "Acme", "position": "Backend Engineer", "startDate": "2022-01", "endDate": "",
         "highlights": [{"id": "w1-b1", "text": "Built Go services on Kubernetes.", "group": "Platform"},
                        {"id": "w1-b2", "text": "Ran Kafka consumers."}]},
        {"id": "w2", "name": "Initech", "position": "Software Engineer", "startDate": "2020-01", "endDate": "2022-01",
         "highlights": [{"id": "w2-b1", "text": "Wrote Go and Python tooling."}]},
        {"id": "w3", "name": "Go Daddy Labs", "position": "Intern", "startDate": "2018-06", "endDate": "2018-09",
         "highlights": [{"id": "w3-b1", "text": "Helped the team."}]},
    ],
    "projects": [
        {"id": "p1", "name": "Rust CLI", "startDate": "2019-01", "endDate": "2019-07", "highlights": [{"id": "p1-b1", "text": "A small tool."}]},
        {"id": "p2", "name": "Elixir chat", "highlights": [{"id": "p2-b1", "text": "A hobby project."}]},
    ],
    "education": [{"institution": "Example University", "endDate": "2019"}],
    "skills": [{"name": "Languages", "keywords": ["Go", "Python", "SQL", "Rust", "Elixir", "Kafka", "Kubernetes"]}],
}
PROFILE = {"years_experience": 5, "location": {"home": "Pune, India", "remote_ok": True, "hybrid_ok": True, "relocation": False},
           "work_authorization": [{"country": "India", "status": "citizen"}]}


def ok(obj, today=TODAY):
    clean, problems = A.validate_fact_sheet(obj, today)
    assert problems == [], problems
    return clean


def bad(obj, needle):
    clean, problems = A.validate_fact_sheet(obj, TODAY)
    assert any(needle in p for p in problems), (needle, problems)
    return clean


# ------------------------------------------------------------------ reading the file

def test_the_example_file_is_valid_and_complete():
    clean, problems = A.validate_fact_sheet(EXAMPLE, TODAY)
    assert problems == [] and A.missing_fields(clean) == []
    assert clean["terms"]["notice_period_days"] == 30 and clean["contact"]["links"]["github"].startswith("https://")


def test_an_empty_sheet_is_valid_and_only_holds_the_equal_opportunity_defaults():
    clean = ok({})
    assert clean == {"equal_opportunity": {k: "Prefer not to say" for k in ("gender", "ethnicity", "veteran", "disability")}}
    assert len(A.missing_fields(clean)) == len(A.FIELD_EFFECTS)


@pytest.mark.parametrize("obj", [[], "x", 3, None])
def test_the_sheet_must_be_an_object(obj):
    assert A.validate_fact_sheet(obj, TODAY)[1] == ["the fact sheet must be a JSON object"]


def test_unknown_sections_and_fields_are_problems_so_a_typo_is_not_silently_ignored():
    bad({"contacts": {}}, "contacts: not a section")
    bad({"contact": {"fullname": "Jane"}}, "contact.fullname: not a field")
    bad({"terms": {"notice": 30}}, "terms.notice: not a field")
    bad({"terms": {"expected_pay": {"minimum": 3}}}, "terms.expected_pay.minimum: not a field")
    ok({"_comment": "hi", "contact": {"_note": "x", "email": "a@b.co"}})                     # underscore keys are comments everywhere


def test_a_bad_field_is_left_out_but_the_rest_of_the_sheet_is_kept():
    clean, problems = A.validate_fact_sheet({"contact": {"email": "not-an-email", "full_name": "Jane Doe"}, "terms": {"notice_period_days": 30}}, TODAY)
    assert problems and clean["contact"] == {"full_name": "Jane Doe"} and clean["terms"] == {"notice_period_days": 30}


@pytest.mark.parametrize("email,valid", [("jane@example.com", True), ("a@b.co", True), ("jane@", False), ("jane example@x.com", False),
                                         ("@x.com", False), ("jane@x", False), (5, False)])
def test_email(email, valid):
    clean, problems = A.validate_fact_sheet({"contact": {"email": email}}, TODAY)
    assert (not problems and clean["contact"]["email"] == email) if valid else problems


@pytest.mark.parametrize("phone,valid", [("+91 98765 43210", True), ("+1 (415) 555-0100", True), ("+447911123456", True), ("98765 43210", False),
                                         ("+91", False), ("+91 abc", False), ("+9" + "1" * 30, False)])
def test_phone_needs_the_country_code(phone, valid):
    clean, problems = A.validate_fact_sheet({"contact": {"phone": phone}}, TODAY)
    assert (not problems and "phone" in clean["contact"]) if valid else problems


@pytest.mark.parametrize("url,valid", [("https://github.com/janedoe", True), ("https://jane.dev", True), ("http://jane.dev", False),
                                       ("jane.dev", False), ("javascript:alert(1)", False), ("https://", False), ("https://a b.com", False)])
def test_links_are_https_addresses(url, valid):
    clean, problems = A.validate_fact_sheet({"contact": {"links": {"website": url}}}, TODAY)
    assert (not problems and clean["contact"]["links"]["website"] == url) if valid else problems
    bad({"contact": {"links": {"twitter": "https://x.com/a"}}}, "contact.links.twitter: not a field")


def test_text_is_one_line_has_no_control_characters_and_is_not_too_long():
    assert ok({"contact": {"full_name": "  Jane \n  Doe "}})["contact"]["full_name"] == "Jane Doe"
    bad({"contact": {"full_name": "Jane\x00Doe"}}, "control characters")
    bad({"contact": {"full_name": "x" * 81}}, "longer than 80")
    bad({"contact": {"full_name": 5}}, "must be text")
    assert "full_name" not in ok({"contact": {"full_name": "   "}}).get("contact", {})          # blank = not filled in


def test_right_to_work_is_one_entry_per_country():
    clean = ok({"right_to_work": [{"country": "India", "authorized": True, "needs_sponsorship_now": False}]})
    assert clean["right_to_work"] == [{"country": "India", "authorized": True, "needs_sponsorship_now": False}]
    bad({"right_to_work": [{"country": "India"}, {"country": "india"}]}, "appears twice")
    bad({"right_to_work": [{"authorized": True}]}, "country: required")
    bad({"right_to_work": [{"country": "India", "authorized": "yes"}]}, "must be true or false")
    bad({"right_to_work": "India"}, "a list")
    bad({"right_to_work": [{"country": f"C{i}"} for i in range(11)]}, "at most 10")


def test_relocation_and_work_mode():
    assert ok({"relocation": {"willing": False, "where": []}})["relocation"] == {"willing": False}
    bad({"relocation": {"willing": "no"}}, "true or false")
    bad({"relocation": {"where": "Berlin"}}, "a list")
    assert ok({"terms": {"work_mode": ["remote", "on-site"]}})["terms"]["work_mode"] == ["remote", "on-site"]
    for wm in ([], ["office"], ["remote", "remote"], "remote"):
        bad({"terms": {"work_mode": wm}}, "terms.work_mode")


@pytest.mark.parametrize("days,valid", [(0, True), (30, True), (365, True), (-1, False), (366, False), (30.5, False), ("30", False), (True, False)])
def test_notice_period(days, valid):
    clean, problems = A.validate_fact_sheet({"terms": {"notice_period_days": days}}, TODAY)
    assert (not problems and clean["terms"]["notice_period_days"] == days) if valid else problems


@pytest.mark.parametrize("value,valid", [("immediately", True), ("2026-12-01", True), ("2026-02-30", False), ("2026-13-01", False),
                                         ("soon", False), ("01/12/2026", False), (20261201, False), ("20261201", False), ("2026-W50-1", False)])
def test_earliest_start_date(value, valid):
    clean, problems = A.validate_fact_sheet({"terms": {"earliest_start_date": value}}, TODAY)
    assert (not problems and clean["terms"]["earliest_start_date"] == value) if valid else problems


def test_pay_needs_an_amount_a_currency_and_a_period_so_a_figure_is_never_read_in_the_wrong_unit():
    good = {"min": 2200000, "max": 2600000, "currency": "INR", "period": "year", "negotiable": True}
    assert ok({"terms": {"expected_pay": good}})["terms"]["expected_pay"] == good
    assert ok({"terms": {"current_pay": {"amount": 90000, "currency": "USD", "period": "year"}}})["terms"]["current_pay"]["amount"] == 90000
    bad({"terms": {"expected_pay": dict(good, currency="inr")}}, "three-letter code")
    bad({"terms": {"expected_pay": dict(good, period="week")}}, "year or month")
    bad({"terms": {"expected_pay": {"min": 5, "max": 3, "currency": "INR", "period": "year"}}}, "max is below min")
    bad({"terms": {"expected_pay": {"min": 5}}}, "currency and the period")
    bad({"terms": {"expected_pay": {"currency": "INR", "period": "year"}}}, "give the min")
    bad({"terms": {"current_pay": {"currency": "INR", "period": "year"}}}, "give the amount")
    bad({"terms": {"expected_pay": dict(good, min=-1)}}, "must be a number")
    bad({"terms": {"expected_pay": dict(good, min=float("nan"))}}, "must be a number")
    bad({"terms": {"expected_pay": dict(good, min=True)}}, "must be a number")
    bad({"terms": {"current_pay": {"amount": 5, "currency": "INR", "period": "year", "negotiable": True}}}, "not a field")
    assert "expected_pay" not in ok({"terms": {}}).get("terms", {})
    clean, _ = A.validate_fact_sheet({"terms": {"expected_pay": dict(good, currency="rupees"), "current_pay": {"amount": 5, "currency": "INR", "period": "week"}}}, TODAY)
    assert "expected_pay" not in clean.get("terms", {}) and "current_pay" not in clean.get("terms", {})        # a pay figure with any part wrong is left out whole


def test_experience():
    clean = ok({"experience": {"total_years": 4.5, "skill_years": {"Go": 3, "Kubernetes": 2.5}, "highest_education": "B.Tech", "graduation_year": 2021,
                               "certifications": ["CKA"]}})
    assert clean["experience"]["skill_years"] == {"Go": 3, "Kubernetes": 2.5} and clean["experience"]["graduation_year"] == 2021
    bad({"experience": {"total_years": True}}, "must be a number")
    bad({"experience": {"total_years": 61}}, "must be a number")
    bad({"experience": {"skill_years": {"Go": -1}}}, "must be a number")
    bad({"experience": {"skill_years": {"Go": "3"}}}, "must be a number")
    bad({"experience": {"skill_years": ["Go"]}}, "an object")
    bad({"experience": {"skill_years": {f"S{i}": 1 for i in range(101)}}}, "at most 100")
    bad({"experience": {"graduation_year": 1900}}, "whole number")
    bad({"experience": {"graduation_year": TODAY.year + 9}}, "whole number")
    bad({"experience": {"graduation_year": 2021.5}}, "whole number")
    bad({"experience": {"certifications": [f"c{i}" for i in range(21)]}}, "at most 20")


def test_standing_answers():
    clean = ok({"standing_answers": {"how_did_you_hear": "A job board", "willing_to_travel": False, "relative_here_default": False,
                                     "custom": [{"phrase": "Fast-paced?", "answer": "Yes"}]}})
    assert clean["standing_answers"]["willing_to_travel"] is False and clean["standing_answers"]["custom"] == [{"phrase": "Fast-paced?", "answer": "Yes"}]
    bad({"standing_answers": {"custom": [{"phrase": "x"}]}}, "needs a phrase and an answer")
    bad({"standing_answers": {"custom": [{"phrase": "x", "answer": "y"}, {"phrase": "X", "answer": "z"}]}}, "appears twice")
    bad({"standing_answers": {"custom": [{"phrase": "x", "answer": "y" * 401}]}}, "longer than 400")
    bad({"standing_answers": {"custom": [{"phrase": "q", "answer": "a"}] * 51}}, "at most 50")
    bad({"standing_answers": {"willing_to_travel": "yes"}}, "true or false")


def test_equal_opportunity_is_prefer_not_to_say_unless_chosen_and_an_empty_answer_is_the_default_too():
    clean = ok({"equal_opportunity": {"gender": "Woman", "veteran": "", "disability": "   "}})
    assert clean["equal_opportunity"] == {"gender": "Woman", "ethnicity": "Prefer not to say", "veteran": "Prefer not to say", "disability": "Prefer not to say"}
    bad({"equal_opportunity": {"religion": "x"}}, "equal_opportunity.religion: not a field")
    bad({"equal_opportunity": {"gender": "x" * 81}}, "longer than 80")


def test_the_clean_sheet_never_aliases_the_input_and_validating_it_again_changes_nothing():
    raw = copy.deepcopy(EXAMPLE)
    clean = ok(raw)
    clean["contact"]["links"]["github"] = "https://changed.example"
    assert raw == EXAMPLE
    again = ok(clean)
    assert again == clean


def _junk(rng, depth=0):
    kinds = [lambda: None, lambda: rng.choice([True, False]), lambda: rng.randint(-5, 400), lambda: rng.random() * 1e12, lambda: float("nan"),
             lambda: rng.choice(["", " ", "x", "jane@example.com", "+91 98765 43210", "https://a.co", "2026-12-01", "immediately", "INR", "year",
                                 "remote", "a\x00b", "z" * 500, "Prefer not to say"])]
    if depth < 3:
        kinds += [lambda: [_junk(rng, depth + 1) for _ in range(rng.randint(0, 4))],
                  lambda: {rng.choice(["a", "min", "country", "phrase", "_c", "willing"]): _junk(rng, depth + 1) for _ in range(rng.randint(0, 4))}]
    return rng.choice(kinds)()


def _mutate(rng, obj):
    if isinstance(obj, dict) and obj:
        k = rng.choice(list(obj))
        r = rng.random()
        if r < 0.25:
            obj[k] = _junk(rng)
        elif r < 0.35:
            del obj[k]
        else:
            obj[k] = _mutate(rng, obj[k])
    elif isinstance(obj, list) and obj:
        i = rng.randrange(len(obj))
        obj[i] = _junk(rng) if rng.random() < 0.3 else _mutate(rng, obj[i])
    return obj


def test_fuzz_the_validator_never_raises_and_what_it_keeps_is_always_valid():
    rng = random.Random(1234)
    for _ in range(1500):
        raw = copy.deepcopy(EXAMPLE)
        for _ in range(rng.randint(1, 4)):
            raw = _mutate(rng, raw)
        if rng.random() < 0.1:
            raw = _junk(rng)
        clean, _problems = A.validate_fact_sheet(raw, TODAY)
        again, problems_again = A.validate_fact_sheet(clean, TODAY)
        assert problems_again == [] and again == clean, (raw, clean, problems_again)
        A.missing_fields(clean)
        A.consistency(clean, RESUME, PROFILE, TODAY)                    # and the later steps can always read it


# ------------------------------------------------------------------ what is missing

def test_missing_fields_name_the_effect_and_a_false_or_zero_counts_as_filled_in():
    clean = ok({"relocation": {"willing": False}, "terms": {"notice_period_days": 0}, "standing_answers": {"willing_to_travel": False}})
    gone = dict(A.missing_fields(clean))
    assert "terms.notice_period_days" not in gone and "relocation.willing" not in gone and "standing_answers.willing_to_travel" not in gone
    assert "every 'expected salary' question" in gone["terms.expected_pay"]
    assert "right_to_work" in gone and "contact.email" in gone


def test_equal_opportunity_is_never_reported_missing():
    assert not any(p.startswith("equal_opportunity") for p, _ in A.missing_fields(ok({})))


# ------------------------------------------------------------------ the resume's dates

def test_total_years_count_overlapping_roles_once_and_a_year_alone_is_the_middle_of_the_year():
    r = {"work": [{"startDate": "2020-01", "endDate": "2022-01"}, {"startDate": "2021-01", "endDate": "2023-01"}]}
    assert A.resume_total_years(r, TODAY) == 3.0                                          # 2020-01 .. 2023-01
    assert A.resume_total_years({"work": [{"startDate": "2020", "endDate": "2022"}]}, TODAY) == 2.0
    assert A.resume_total_years({"work": [{"startDate": "2020", "endDate": "2022-01"}]}, TODAY) == 1.5          # 2020-07 .. 2022-01
    assert A.resume_total_years({"work": [{"startDate": "2020-01", "endDate": "2022"}]}, TODAY) == 2.5          # 2020-01 .. 2022-07
    assert A.resume_total_years({"work": [{"startDate": "2025-10", "endDate": ""}]}, TODAY) == 1.0           # still working: until today
    assert A.resume_total_years({"work": [{"startDate": "2022-01", "endDate": "2020-01"}]}, TODAY) is None   # end before start: not usable
    assert A.resume_total_years({"work": []}, TODAY) is None and A.resume_total_years({}, TODAY) is None and A.resume_total_years(None, TODAY) is None
    assert A.resume_total_years({"work": [{"startDate": "bad", "endDate": ""}]}, TODAY) is None


def test_years_for_a_skill_are_the_dated_roles_that_mention_it():
    go = A.derived_skill(RESUME, "Go", TODAY)
    assert go["where"] == "worked" and go["years"] == 6.8          # Acme 2022-01..now is 57 months, Initech 2020-01..2022-01 is 24: 81 months = 6.75, shown as 6.8


def test_go_the_language_is_not_go_the_verb_or_the_employer():
    r = {"work": [{"name": "Go Daddy Labs", "position": "Intern", "startDate": "2018-06", "endDate": "2018-09",
                   "highlights": [{"text": "Let's go ahead and helped the team go faster."}]}], "skills": [{"name": "L", "keywords": ["Go"]}]}
    d = A.derived_skill(r, "Go", TODAY)
    assert (d["where"], d["years"], d["roles"]) == ("listed", None, [])


def test_a_skill_in_a_role_title_or_a_group_heading_counts_and_so_does_a_dated_project():
    r = {"work": [{"name": "Acme", "position": "Kubernetes Engineer", "startDate": "2024-01", "endDate": "2025-01", "highlights": [{"text": "Did things."}]},
                  {"name": "B", "position": "Dev", "startDate": "2023-01", "endDate": "2023-07", "highlights": [{"text": "x", "group": "Redis cache"}]}],
         "projects": [{"name": "Rust CLI", "startDate": "2019-01", "endDate": "2019-07", "highlights": [{"text": "tool"}]}]}
    kube = A.derived_skill(r, "Kubernetes", TODAY)
    assert (kube["where"], kube["years"], kube["roles"]) == ("worked", 1.0, ["Acme"])
    assert (A.derived_skill(r, "Redis", TODAY)["where"], A.derived_skill(r, "Redis", TODAY)["years"]) == ("worked", 0.5)
    rust = A.derived_skill(r, "Rust", TODAY)                                               # a project shows the skill but adds no years
    assert (rust["where"], rust["years"], rust["projects_only"], rust["roles"]) == ("worked", None, True, ["Rust CLI"])


def test_work_with_no_usable_dates_is_worked_with_no_years_and_never_zero():
    el = A.derived_skill(RESUME, "Elixir", TODAY)
    assert (el["where"], el["years"]) == ("worked", None)
    undated = {"work": [{"name": "Acme", "position": "Dev", "startDate": "", "endDate": "", "highlights": [{"text": "Built Kafka consumers."}]}]}
    assert A.derived_skill(undated, "Kafka", TODAY)["years"] is None


def test_a_skill_only_in_the_skills_list_is_listed_and_one_nowhere_is_absent():
    for name, where in (("SQL", "listed"), ("Terraform", "absent"), ("  ", "absent"), (None, "absent"), (5, "absent")):
        d = A.derived_skill(RESUME, name, TODAY)
        assert (d["where"], d["years"]) == (where, None), name
    assert A.derived_skill({}, "Go", TODAY)["where"] == "absent" and A.derived_skill(None, "Go", TODAY)["where"] == "absent"


def test_a_skill_listed_in_the_skills_section_with_different_case_or_alias_still_matches():
    r = {"work": [], "skills": [{"name": "x", "keywords": ["PostgreSQL"]}]}
    assert A.derived_skill(r, "postgresql", TODAY)["where"] == "listed"


def test_the_answer_for_a_skill_is_an_override_then_the_dates_then_needs_you_with_the_reason():
    sheet = {"experience": {"skill_years": {"go": 9, "Rust": 2}}}
    go = A.skill_years_for("Go", sheet, RESUME, TODAY)
    assert (go["years"], go["source"], go["where"], go["note"]) == (9, "your override", "worked", "") and go["roles"] == ["Acme", "Initech"]
    assert A.skill_years_for("Rust", sheet, RESUME, TODAY)["years"] == 2
    kube = A.skill_years_for("Kubernetes", {}, RESUME, TODAY)
    assert kube["source"] == "resume dates" and kube["years"] == 4.8 and kube["note"] == ""
    sql = A.skill_years_for("SQL", {}, RESUME, TODAY)
    assert sql["years"] is None and sql["where"] == "listed" and "only lists it in the skills section" in sql["note"]
    none = A.skill_years_for("Terraform", {}, RESUME, TODAY)
    assert none["years"] is None and none["note"] == "not on your resume"
    el = A.skill_years_for("Elixir", {}, RESUME, TODAY)
    assert el["years"] is None and "only your projects mention it" in el["note"]
    undated = {"work": [{"name": "Acme", "position": "Dev", "startDate": "", "highlights": [{"text": "Built Kafka consumers."}]}]}
    assert "no usable dates" in A.skill_years_for("Kafka", {}, undated, TODAY)["note"]
    assert A.skill_years_for(None, {}, RESUME, TODAY)["note"] == "not on your resume"


def test_the_skill_table_has_one_row_per_resume_keyword_with_both_the_override_and_the_dates():
    sheet = {"experience": {"skill_years": {"Kubernetes": 2.5}}}
    rows = A.skill_table(sheet, RESUME, TODAY)
    assert [r["skill"] for r in rows] == ["Go", "Python", "SQL", "Rust", "Elixir", "Kafka", "Kubernetes"]
    kube = next(r for r in rows if r["skill"] == "Kubernetes")
    assert kube["years"] == 2.5 and kube["source"] == "your override" and kube["derived"] == 4.8
    assert next(r for r in rows if r["skill"] == "SQL")["years"] is None
    dup = dict(RESUME, skills=[{"name": "a", "keywords": ["Go", "go"]}, {"name": "b", "keywords": ["Go", 5]}])
    assert [r["skill"] for r in A.skill_table({}, dup, TODAY)] == ["Go"]


# ------------------------------------------------------------------ the sheet against the resume and the profile

def sheet(**sections):
    return ok(sections)


def warnings(s, resume=RESUME, profile=PROFILE):
    return A.consistency(s, resume, profile, TODAY)


def test_a_sheet_that_agrees_with_the_resume_and_the_profile_has_no_warnings():
    s = sheet(contact={"full_name": "Jane  Doe", "email": "JANE@example.com", "phone": "+91 98765 43210"},
              experience={"total_years": 6, "graduation_year": 2019, "skill_years": {"Go": 6.8}},
              relocation={"willing": False}, terms={"work_mode": ["remote", "hybrid"], "earliest_start_date": "2026-12-01", "notice_period_days": 30},
              right_to_work=[{"country": "India", "authorized": True}])
    assert warnings(s) == []


def test_contact_that_differs_from_the_resume_is_flagged():
    w = warnings(sheet(contact={"full_name": "Janet Doe", "email": "j@other.com", "phone": "+44 7911 123456"}))
    assert any("full_name" in x for x in w) and any("contact.email" in x for x in w) and any("phone" in x for x in w)


def test_total_years_are_compared_with_the_resume_dates_and_the_profile():
    w = warnings(sheet(experience={"total_years": 12}))
    assert any("dates add up to 7 years" in x for x in w) and any("years_experience 5" in x for x in w)
    assert warnings(sheet(experience={"total_years": 6})) == []                           # one year of difference is not a disagreement


def test_a_skill_override_that_the_resume_does_not_support_is_flagged_in_each_way():
    w = warnings(sheet(experience={"skill_years": {"Terraform": 3, "SQL": 4, "Go": 12, "Kafka": 0.5}}))
    assert any("Terraform" in x and "not on your resume" in x for x in w)
    assert any("SQL" in x and "only lists" in x for x in w)
    assert any("Go" in x and "show about 6.8" in x for x in w)
    assert any("Go" in x and "more than all the years" in x for x in w)
    assert not any("Kafka" in x for x in w)                                              # fewer years than the resume shows is never a claim


def test_graduation_year_is_compared_with_the_education_dates():
    assert any("graduation_year is 2020" in x for x in warnings(sheet(experience={"graduation_year": 2020})))
    assert warnings(sheet(experience={"graduation_year": 2019})) == []


def test_relocation_and_work_mode_are_compared_with_the_profile():
    w = warnings(sheet(relocation={"willing": True, "where": ["Berlin"]}, terms={"work_mode": ["remote"]}),
                 profile=dict(PROFILE, location={"relocation": False, "remote_ok": False, "hybrid_ok": False}))
    assert any("relocation.willing is true" in x for x in w) and any("remote_ok is false" in x for x in w)
    assert any("hybrid_ok" in x for x in warnings(sheet(terms={"work_mode": ["hybrid"]}), profile=dict(PROFILE, location={"hybrid_ok": False})))
    assert any("lists places" in x for x in warnings(sheet(relocation={"willing": False, "where": ["Berlin"]})))


def test_pay_and_dates_that_look_wrong_are_flagged():
    w = warnings(sheet(terms={"current_pay": {"amount": 3000000, "currency": "INR", "period": "year"},
                              "expected_pay": {"min": 2000000, "currency": "INR", "period": "year"}}))
    assert any("below terms.current_pay" in x for x in w)
    assert warnings(sheet(terms={"current_pay": {"amount": 3000000, "currency": "INR", "period": "year"},
                                 "expected_pay": {"min": 2000000, "currency": "USD", "period": "year"}})) == []      # a different currency is not comparable
    assert any("in the past" in x for x in warnings(sheet(terms={"earliest_start_date": "2026-01-01"})))
    assert any("before today plus your notice period" in x for x in warnings(sheet(terms={"earliest_start_date": "2026-10-20", "notice_period_days": 30})))
    assert warnings(sheet(terms={"earliest_start_date": "immediately", "notice_period_days": 30})) == []


def test_right_to_work_that_contradicts_a_citizen_profile_is_flagged():
    w = warnings(sheet(right_to_work=[{"country": "india", "authorized": False}]))
    assert any("not authorised in india" in x for x in w)
    assert warnings(sheet(right_to_work=[{"country": "Germany", "authorized": False}])) == []


def test_consistency_never_crashes_on_missing_or_odd_inputs():
    s = sheet(experience={"total_years": 5, "skill_years": {"Go": 3}}, terms={"work_mode": ["remote"]})
    assert A.consistency(s, None, None, TODAY) == []              # nothing to compare with: nothing to say
    odd_profile = {"years_experience": "five", "location": "Pune", "work_authorization": "citizen"}
    odd_resume = {"basics": "x", "work": ["bad", {"startDate": 5}], "education": [None, {"endDate": None}], "skills": "x", "projects": [3]}
    assert isinstance(A.consistency(s, odd_resume, odd_profile, TODAY), list)
    assert isinstance(A.skill_table(s, odd_resume, TODAY), list)


def test_check_bundles_everything_and_skips_what_it_has_no_input_for():
    res = A.check(EXAMPLE, RESUME, PROFILE, TODAY)
    assert res["problems"] == [] and res["missing"] == [] and res["skills"] and isinstance(res["warnings"], list)
    only = A.check({"contact": {"email": "x"}})
    assert only["problems"] and only["warnings"] == [] and only["skills"] == [] and only["missing"]


# ------------------------------------------------------------------ the independent review's findings

def _res(**over):
    return dict({"work": [], "skills": []}, **over)


def _job(text="", start="2015", end="2017", position="Dev", name="Acme", **more):
    return dict({"name": name, "position": position, "startDate": start, "endDate": end, "highlights": [{"text": text}]}, **more)


def test_a_project_never_adds_years_even_when_it_has_dates_and_no_end_date_never_means_now():
    r = _res(projects=[{"name": "Rust CLI", "startDate": "2015", "highlights": [{"text": "x"}]}])
    d = A.derived_skill(r, "Rust", TODAY)
    assert d["years"] is None and d["projects_only"] is True
    both = _res(work=[_job("Built Rust services.", "2024-01", "2025-01")], projects=[{"name": "Rust CLI", "startDate": "2010", "endDate": "2015", "highlights": [{"text": "x"}]}])
    assert A.derived_skill(both, "Rust", TODAY)["years"] == 1.0 and A.derived_skill(both, "Rust", TODAY)["projects_only"] is False


def test_dates_after_today_are_cut_at_today_and_a_start_after_today_is_not_usable_and_absurd_years_are_ignored():
    assert A.derived_skill(_res(work=[_job("Used Python.", "2024-01", "2035")]), "Python", TODAY)["years"] == 2.8          # 2024-01 .. 2026-10 is 33 months
    assert A.resume_total_years({"work": [{"startDate": "2020", "endDate": "9999"}]}, TODAY) == 6.2                    # 2020-07 .. 2026-10
    assert A.resume_total_years({"work": [{"startDate": "2027-01", "endDate": ""}]}, TODAY) is None
    assert A.resume_total_years({"work": [{"startDate": "1920", "endDate": "2022"}]}, TODAY) is None                    # before 1950: not a date to trust
    assert A.resume_total_years({"work": [{"startDate": "2025-01", "endDate": "2026"}]}, date(2026, 2, 1)) == 1.1      # the year-only end is cut at today (2026-02)
    assert A.resume_total_years({"work": [{"startDate": "2020", "endDate": 0}]}, TODAY) is None                         # 0 / false is not "still working"
    assert A.resume_total_years({"work": [{"startDate": "2020", "endDate": False}]}, TODAY) is None


@pytest.mark.parametrize("text,skill", [("Led R&D for payments.", "R"), ("Presented to the C-suite.", "C"), ("Go live of billing in March.", "Go"),
                                        ("Swift delivery of the migration.", "Swift"), ("Spring cleaning of the repo.", "Spring")])
def test_a_short_or_everyday_skill_name_is_not_credited_for_an_ordinary_phrase(text, skill):
    assert A.derived_skill(_res(work=[_job(text)]), skill, TODAY)["where"] == "absent"


@pytest.mark.parametrize("text,skill", [("Built Go services for billing.", "Go"), ("Wrote the importer in R and Python.", "R"), ("Maintained C and C++ drivers.", "C"),
                                        ("Shipped Swift apps for iOS.", "Swift"), ("Used Spring Boot for APIs.", "Spring"), ("Wrote Ruby scripts.", "Ruby")])
def test_a_real_mention_of_a_short_or_everyday_name_still_counts(text, skill):
    assert A.derived_skill(_res(work=[_job(text)]), skill, TODAY)["where"] == "worked"


def test_a_job_title_naming_a_short_skill_counts_even_as_the_first_words():
    assert A.derived_skill(_res(work=[_job("Did things.", position="Go Engineer")]), "Go", TODAY)["where"] == "worked"


def test_exact_spelling_matches_for_short_mixed_case_names_like_ios_and_npm():
    r = _res(work=[_job("Shipped iOS apps and published npm packages.")], skills=[{"name": "x", "keywords": ["iOS", "npm"]}])
    assert A.derived_skill(r, "iOS", TODAY)["where"] == "worked" and A.derived_skill(r, "npm", TODAY)["where"] == "worked"
    assert [x["skill"] for x in A.skill_table({}, r, TODAY)] == ["iOS", "npm"]


def test_roles_are_shown_beside_a_derived_figure_and_blank_keywords_make_no_row():
    rows = A.skill_table({}, dict(RESUME, skills=[{"name": "a", "keywords": ["Go", "  ", ""]}]), TODAY)
    assert [r["skill"] for r in rows] == ["Go"] and rows[0]["roles"] == ["Acme", "Initech"]


@pytest.mark.parametrize("resume", [
    _res(work=[{"position": "x", "startDate": "2020", "endDate": "2021", "highlights": 5}]),
    _res(work=[{"position": "x", "startDate": "2020", "endDate": "2021", "highlights": True}]),
    _res(work=[{"position": 5, "name": 7, "startDate": "2020", "endDate": "2021", "highlights": [None, 3, {"text": 5, "group": ["x"]}]}]),
    _res(work=[{"position": "x", "startDate": ["2020"], "endDate": {}, "highlights": []}]),
    _res(projects=[{"name": ["Go"], "highlights": "text"}], skills=[{"name": "a", "keywords": "Go"}, {"keywords": [None, 5, "Go"]}, 7]),
    {"work": "x", "projects": 3, "skills": {"a": 1}, "education": 9, "basics": [1]},
])
def test_a_malformed_resume_never_crashes_the_years_or_the_warnings(resume):
    A.derived_skill(resume, "Go", TODAY)
    A.skill_table({"experience": {"skill_years": {"Go": 3}}}, resume, TODAY)
    A.resume_total_years(resume, TODAY)
    A.consistency(sheet(contact={"full_name": "Jane", "email": "a@b.co", "phone": "+91 98765 43210"}, experience={"total_years": 3, "graduation_year": 2020,
                                                                                                                 "skill_years": {"Go": 3}}), resume, PROFILE, TODAY)
    A.check({"contact": {"full_name": "Jane"}}, resume, PROFILE, TODAY)


@pytest.mark.parametrize("basics", [{"name": 5, "phone": 5, "email": 5}, {"name": ["Jane"], "phone": {"a": 1}, "email": None}, "x", None, 7])
def test_odd_basics_never_crash_the_contact_comparison(basics):
    A.consistency(sheet(contact={"full_name": "Jane Doe", "email": "a@b.co", "phone": "+91 98765 43210"}), {"basics": basics}, PROFILE, TODAY)


@pytest.mark.parametrize("years", [10 ** 400, float("inf"), float("nan"), "five", True, None, -3])
def test_odd_profile_years_never_crash_and_never_print_infinity(years):
    w = A.consistency(sheet(experience={"total_years": 5}), RESUME, dict(PROFILE, years_experience=years), TODAY)
    assert not any("inf" in x or "nan" in x for x in w)


def test_a_currency_with_a_trailing_newline_is_refused_and_so_is_a_pay_with_a_bad_part():
    clean, problems = A.validate_fact_sheet({"terms": {"expected_pay": {"min": 5, "currency": "USD\n", "period": "year"}}}, TODAY)
    assert problems and "expected_pay" not in clean.get("terms", {})


@pytest.mark.parametrize("value", ["Jane" + chr(0x200B) + "Doe", "a" + chr(0x202E) + "@x.com", "Jane" + chr(0x9B) + "Doe", "Jane" + chr(0x2066) + "Doe"])
def test_invisible_and_direction_characters_are_refused_in_every_text_field(value):
    assert ok({"contact": {"full_name": "Jane\u2028Doe"}})["contact"]["full_name"] == "Jane Doe"          # a line separator is only a space once the text is one line
    for obj in ({"contact": {"full_name": value}}, {"standing_answers": {"how_did_you_hear": value}},
                {"standing_answers": {"custom": [{"phrase": value, "answer": "x"}]}}, {"experience": {"highest_education": value}},
                {"equal_opportunity": {"gender": value}}):
        clean, problems = A.validate_fact_sheet(obj, TODAY)
        assert any("control characters" in p for p in problems), obj
        assert value not in json.dumps(clean, ensure_ascii=False)
    assert ok({"contact": {"full_name": "José García-Ñuñez"}})["contact"]["full_name"] == "José García-Ñuñez"       # real letters are fine


@pytest.mark.parametrize("url", ["https://user:pw@github.com/x", "https://a..b", "https://-.-", "https://github.com:badport/x", "https://git hub.com",
                                 "https://github", "https://github.com\\x", "https:///x"])
def test_a_link_with_a_password_or_a_malformed_host_is_refused(url):
    clean, problems = A.validate_fact_sheet({"contact": {"links": {"github": url}}}, TODAY)
    assert problems and "contact" not in clean


def test_skill_year_keys_are_checked_like_the_values():
    bad({"experience": {"skill_years": {"Python": 3, "python": 5}}}, "appears twice")
    assert ok({"experience": {"skill_years": {"Python": 3}}})["experience"]["skill_years"] == {"Python": 3}
    bad({"experience": {"skill_years": {" _Python": 3}}}, "cannot start with an underscore")
    bad({"experience": {"skill_years": {"": 3}}}, "every skill needs a name")
    bad({"experience": {"skill_years": {"Go": 0}}}, "must be a number")                      # an override of zero would be a claim of "no experience"
    assert ok({"experience": {"skill_years": {"_note": "x", "Go": 2}}})["experience"]["skill_years"] == {"Go": 2}


def test_an_override_for_a_skill_the_resume_does_not_show_is_kept_but_says_so():
    r = A.skill_years_for("Terraform", {"experience": {"skill_years": {"Terraform": 3}}}, RESUME, TODAY)
    assert r["years"] == 3 and r["source"] == "your override" and "does not show this skill" in r["note"]


def test_names_are_compared_without_accents_or_initials_and_a_name_in_another_script_is_not_a_disagreement():
    for sheet_name, resume_name in (("Jose Garcia", "José García"), ("Ravi Kumar", "Ravi K. Kumar"), ("Ravi Kumar Singh", "Ravi Singh"), ("Kumar Ravi", "Ravi Kumar"), ("Ravi Kumar", "Kumar"), ("李雷", "韩梅梅")):
        w = A.consistency(sheet(contact={"full_name": sheet_name}), dict(RESUME, basics={"name": resume_name}), PROFILE, TODAY)
        assert not any("full_name" in x for x in w), (sheet_name, resume_name)
    w = A.consistency(sheet(contact={"full_name": "Jane Doe"}), dict(RESUME, basics={"name": "John Smith"}), PROFILE, TODAY)
    assert any("full_name" in x for x in w)


def test_the_resume_email_is_compared_without_surrounding_spaces():
    assert not any("contact.email" in x for x in A.consistency(sheet(contact={"email": "a@b.co"}), dict(RESUME, basics={"email": "  A@B.co "}), PROFILE, TODAY))


def test_right_to_work_is_compared_with_the_profile_ignoring_case_and_spaces_and_sponsorship_is_checked_too():
    profile = dict(PROFILE, work_authorization=[{"country": " India ", "status": "Citizen "}])
    w = A.consistency(sheet(right_to_work=[{"country": "india", "needs_sponsorship_now": True, "authorized": False}]), RESUME, profile, TODAY)
    assert any("not authorised" in x for x in w) and any("need sponsorship" in x for x in w)
    assert A.consistency(sheet(right_to_work=[{"country": "india", "authorized": True, "needs_sponsorship_now": False}]), RESUME, profile, TODAY) == []


def test_a_country_with_no_answers_under_it_is_still_missing_those_answers():
    gone = dict(A.missing_fields(sheet(right_to_work=[{"country": "India"}])))
    assert "right_to_work" not in gone and {"right_to_work.authorized", "right_to_work.needs_sponsorship_now", "right_to_work.needs_sponsorship_future"} <= set(gone)
    full = dict(A.missing_fields(sheet(right_to_work=[{"country": "India", "authorized": True, "needs_sponsorship_now": False, "needs_sponsorship_future": False}])))
    assert not any(p.startswith("right_to_work") for p in full)
    assert "right_to_work" in dict(A.missing_fields(ok({})))


def test_missing_fields_keep_the_documented_order():
    paths = [p for p, _ in A.missing_fields(ok({}))]
    assert paths == [p for p, _ in A.FIELD_EFFECTS]


def _mutate_keys(rng, obj):
    if isinstance(obj, dict) and obj:
        k = rng.choice(list(obj))
        if rng.random() < 0.3:
            obj[rng.choice(["", " ", "_x", " _y", k.upper() if isinstance(k, str) else k, "Go", "go", "a\x00b", chr(0x200B)])] = obj.pop(k)
        else:
            obj[k] = _mutate_keys(rng, obj[k])
    elif isinstance(obj, list) and obj:
        i = rng.randrange(len(obj))
        obj[i] = _mutate_keys(rng, obj[i])
    return obj


def test_fuzz_keys_too_and_random_resumes_and_profiles_never_crash_the_comparison():
    rng = random.Random(99)
    for _ in range(1500):
        raw = copy.deepcopy(EXAMPLE)
        raw["experience"]["skill_years"] = {"Go": 3, "Python": 2, " _Py": 1, "": 4, "go": 5}
        for _ in range(rng.randint(1, 4)):
            raw = _mutate_keys(rng, _mutate(rng, raw))
        clean, _problems = A.validate_fact_sheet(raw, TODAY)
        again, problems_again = A.validate_fact_sheet(clean, TODAY)
        assert problems_again == [] and again == clean, (raw, clean, problems_again)
        resume, profile = copy.deepcopy(RESUME), copy.deepcopy(PROFILE)
        for _ in range(rng.randint(0, 4)):
            resume, profile = _mutate(rng, resume), _mutate(rng, profile)
        if rng.random() < 0.1:
            resume = _junk(rng)
        A.consistency(clean, resume, profile, TODAY)
        A.skill_table(clean, resume, TODAY)
        A.check(raw, resume, profile, TODAY)


def test_an_override_for_a_skill_only_a_project_shows_is_flagged():
    w = A.consistency(sheet(experience={"skill_years": {"Rust": 3}}), RESUME, PROFILE, TODAY)
    assert any("Rust" in x and "only your projects mention Rust" in x for x in w)
