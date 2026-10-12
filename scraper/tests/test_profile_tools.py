import copy
import json
import os

import pytest

import profile_tools as PT

HERE = os.path.dirname(__file__)
CONFIG = os.path.join(HERE, "..", "..", "config")


def load(name):
    with open(os.path.join(CONFIG, name), encoding="utf-8") as f:
        return json.load(f)


# ------------------------------------------------------------------ dates

@pytest.mark.parametrize("raw,expected", [
    ("Jan 2026", "2026-01"), ("Jul 2025", "2025-07"), ("Sept 2024", "2024-09"), ("2022", "2022"),
    ("2024-03", "2024-03"), ("", ""), ("Ongoing", ""), ("Present", ""), ("13/2024", ""), (None, ""),
])
def test_norm_date(raw, expected):
    assert PT.norm_date(raw) == expected


# ------------------------------------------------------------------ resume

def test_example_resume_is_valid():
    PT.validate_resume(load("resume.example.json"))


def test_comment_keys_are_ignored():
    r = load("resume.example.json")
    r["_note"] = 42
    assert "_note" not in PT.validate_resume(r)


@pytest.mark.parametrize("mutate,expect", [
    (lambda r: r["basics"].pop("name"), "basics.name"),
    (lambda r: r["basics"].update(email="nope"), "basics.email"),
    (lambda r: r.update(work=[]), "work"),
    (lambda r: r["work"][0].update(startDate="Jan 2022"), "work[0].startDate"),
    (lambda r: r["work"][0].update(endDate="soon"), "work[0].endDate"),
    (lambda r: r["work"][0].pop("position"), "work[0].position"),
    (lambda r: r["work"][0].update(highlights=[]), "work[0].highlights"),
    (lambda r: r["work"][0]["highlights"][0].update(text=""), "highlights[0].text"),
    (lambda r: r["work"][0]["highlights"][0].update(text="x" * 601), "longer than 600"),
    (lambda r: r["work"][0]["highlights"][1].update(id="w1-b1"), "duplicate id"),
    (lambda r: r["work"][0]["highlights"][0].pop("id"), "highlights[0].id"),
    (lambda r: r.update(skills=[]), "skills"),
    (lambda r: r["skills"][0].update(keywords=[]), "skills[0]"),
    (lambda r: r["education"][0].pop("institution"), "education[0].institution"),
    (lambda r: r.update(projects=[{"id": "p1", "name": "X", "highlights": []}]), "projects[0].highlights"),
    (lambda r: r.update(additional="nope"), "additional"),
    (lambda r: r.update(additional=[{"id": "add-1", "text": ""}]), "additional[0].text"),
    (lambda r: r.update(additional=[{"id": "w1-b1", "text": "dup"}]), "duplicate id 'w1-b1'"),
])
def test_invalid_resumes(mutate, expect):
    bad = copy.deepcopy(load("resume.example.json"))
    mutate(bad)
    with pytest.raises(PT.ProfileToolsError) as e:
        PT.validate_resume(bad)
    assert expect in str(e.value)


def test_ids_must_be_unique_across_sections():
    r = copy.deepcopy(load("resume.example.json"))
    r["projects"] = [{"id": "work-1", "name": "P", "highlights": [{"id": "p-b1", "text": "did a thing"}]}]
    with pytest.raises(PT.ProfileToolsError, match="duplicate id 'work-1'"):
        PT.validate_resume(r)


def test_ongoing_role_may_have_empty_end_date():
    r = copy.deepcopy(load("resume.example.json"))
    r["work"][0]["endDate"] = ""
    PT.validate_resume(r)


# ------------------------------------------------------------------ search settings

def test_example_search_is_valid_and_defaults_fill_in():
    out = PT.validate_search(load("search.example.json"))
    assert out["score_threshold"] == 70 and "indeed" in out["sources"] and out["allow_remote"] is True


@pytest.mark.parametrize("mutate,expect", [
    (lambda s: s.update(search_terms=[]), "search_terms"),
    (lambda s: s.update(locations=[]), "locations"),
    (lambda s: s.update(sources=["myspace"]), "unknown"),
    (lambda s: s.update(min_yoe=9, max_yoe=3), "min_yoe is greater"),
    (lambda s: s.update(score_threshold=101), "score_threshold"),
    (lambda s: s.update(max_age_days=0), "max_age_days"),
    (lambda s: s.update(allow_remote="yes"), "allow_remote"),
    (lambda s: s.update(exclude_keywords="intern"), "exclude_keywords"),
    (lambda s: s.update(min_yoe=True), "min_yoe"),
])
def test_invalid_search(mutate, expect):
    bad = copy.deepcopy(load("search.example.json"))
    mutate(bad)
    with pytest.raises(PT.ProfileToolsError) as e:
        PT.validate_search(bad)
    assert expect in str(e.value)


# ------------------------------------------------------------------ LinkedIn export

def write(path, text):
    path.write_text(text, encoding="utf-8-sig")


@pytest.fixture
def export(tmp_path):
    write(tmp_path / "Profile.csv",
          'First Name,Last Name,Maiden Name,Address,Birth Date,Headline,Summary,Industry,Zip Code,Websites\n'
          'Jane,Doe,SECRETMAIDEN,"1 Secret Street",1990-01-01,Backend Engineer,"Builds APIs, mostly.",Software,411001,"[PERSONAL:https://x.dev]"\n')
    write(tmp_path / "Positions.csv",
          "Company Name,Title,Description,Location,Started On,Finished On\n"
          "Acme,Software Engineer I,,Pune,Jan 2026,\n"
          'Acme,Software Engineer Intern,"Worked on APIs",Pune,Jul 2025,Jan 2026\n')
    write(tmp_path / "Education.csv",
          "School Name,Start Date,End Date,Notes,Degree Name,Activities\nMIT-WPU,2022,2026,,BTech,\n")
    write(tmp_path / "Skills.csv", "Name\nPython (Programming Language)\nPython\nDocker Products\nGo\n")
    write(tmp_path / "Projects.csv", "Title,Description,Url,Started On,Finished On\nSmashVision,CV project,,Jun 2025,\n")
    write(tmp_path / "messages.csv", "From,Content\nsomeone,TOP SECRET MESSAGE\n")
    write(tmp_path / "Ad_Targeting.csv", "Interest\nsecret interest\n")
    return tmp_path


def test_linkedin_import_parses_whitelisted_data(export):
    d = PT.parse_linkedin_export(str(export))
    assert d["profile"]["headline"] == "Backend Engineer" and d["profile"]["first_name"] == "Jane"
    assert [p["title"] for p in d["positions"]] == ["Software Engineer I", "Software Engineer Intern"]
    assert d["positions"][1]["start"] == "2025-07" and d["positions"][1]["end"] == "2026-01"
    assert d["positions"][0]["end"] == ""
    assert d["education"][0]["start"] == "2022"
    assert d["skills"] == ["Python", "Docker Products", "Go"]  # suffix stripped, duplicates removed
    assert d["projects"][0]["start"] == "2025-06"


def test_linkedin_import_never_reads_sensitive_data(export):
    blob = json.dumps(PT.parse_linkedin_export(str(export)))
    for secret in ("SECRETMAIDEN", "Secret Street", "1990-01-01", "411001", "TOP SECRET", "secret interest"):
        assert secret not in blob
    d = PT.parse_linkedin_export(str(export))
    assert d["files_ignored"] == 2 and set(d["files_read"]) == {
        "Profile.csv", "Positions.csv", "Education.csv", "Skills.csv", "Projects.csv"}


def test_linkedin_import_tolerates_missing_files(tmp_path):
    write(tmp_path / "Skills.csv", "Name\nGo\n")
    d = PT.parse_linkedin_export(str(tmp_path))
    assert d["skills"] == ["Go"] and d["positions"] == [] and "Positions.csv" in d["files_missing"]


def test_linkedin_import_rejects_non_folder(tmp_path):
    with pytest.raises(PT.ProfileToolsError, match="unzip"):
        PT.parse_linkedin_export(str(tmp_path / "missing.zip"))


# ------------------------------------------------------------------ conflicts

def resume_with(position, start, company="Acme (Platform Team)", end=""):
    return {"work": [{"id": "w1", "name": company, "position": position, "startDate": start, "endDate": end,
                      "highlights": [{"id": "h1", "text": "x"}]}]}


LI = {"positions": [
    {"company": "Acme", "title": "Software Engineer I", "start": "2026-01", "end": ""},
    {"company": "Acme", "title": "Software Engineer Intern", "start": "2025-07", "end": "2026-01"}]}


def test_multiple_roles_on_linkedin_vs_one_on_resume_is_surfaced():
    c = PT.find_conflicts(resume_with("Software Development Engineer", "2025-07"), LI)
    assert [x["kind"] for x in c] == ["multiple_roles"]
    assert len(c[0]["linkedin"]) == 2 and "Intern" in c[0]["linkedin"][1]


def test_title_and_start_differences():
    li = {"positions": [{"company": "Acme", "title": "Engineer", "start": "2025-01", "end": ""}]}
    kinds = [x["kind"] for x in PT.find_conflicts(resume_with("Senior Engineer", "2025-09"), li)]
    assert kinds == ["title_differs", "start_differs"]


def test_matching_entry_has_no_conflicts_and_one_month_slack():
    li = {"positions": [{"company": "Acme", "title": "Engineer", "start": "2025-02", "end": ""}]}
    assert PT.find_conflicts(resume_with("engineer", "2025-01"), li) == []


def test_resume_only_and_linkedin_only():
    li = {"positions": [{"company": "Other Corp", "title": "Dev", "start": "2020", "end": "2021"}]}
    kinds = sorted(x["kind"] for x in PT.find_conflicts(resume_with("Engineer", "2022"), li))
    assert kinds == ["linkedin_only", "resume_only"]


def test_blank_company_on_linkedin_does_not_match_everything():
    li = {"positions": [{"company": "", "title": "Dev", "start": "2020", "end": ""}]}
    kinds = sorted(x["kind"] for x in PT.find_conflicts(resume_with("Engineer", "2022"), li))
    assert kinds == ["linkedin_only", "resume_only"]


def test_highlight_group_and_additional_section_are_kept():
    r = PT.validate_resume(load("resume.example.json"))
    assert r["work"][0]["highlights"][0]["group"] == "Caching layer"
    assert r["additional"][0]["id"] == "add-1"


def two_role_resume(intern_title="Software Engineer Intern", intern_start="2025-07", intern_end="2026-01"):
    def role(i, pos, start, end):
        return {"id": f"w{i}", "name": "Acme", "position": pos, "startDate": start, "endDate": end,
                "highlights": [{"id": f"h{i}", "text": "x"}]}
    return {"work": [role(1, "Software Engineer I", "2026-01", ""), role(2, intern_title, intern_start, intern_end)]}


def test_matching_two_role_histories_have_no_conflicts():
    assert PT.find_conflicts(two_role_resume(), LI) == []


def test_two_role_histories_are_compared_role_by_role():
    kinds = sorted(x["kind"] for x in PT.find_conflicts(two_role_resume("Engineering Intern", "2025-03", "2025-10"), LI))
    assert kinds == ["end_differs", "start_differs", "title_differs"]


def test_ended_vs_ongoing_is_an_end_date_conflict():
    li = {"positions": [{"company": "Acme", "title": "Engineer", "start": "2025-01", "end": "2025-12"}]}
    kinds = [x["kind"] for x in PT.find_conflicts(resume_with("Engineer", "2025-01", end=""), li)]
    assert kinds == ["end_differs"]


# ------------------------------------------------------------------ boards and feeds (Phase 7)

import sources as _sources  # noqa: E402


def _search(**over):
    s = copy.deepcopy(load("search.example.json"))
    s.update(over)
    return s


def _problems(**over):
    with pytest.raises(PT.ProfileToolsError) as e:
        PT.validate_search(_search(**over))
    return e.value.problems


def test_old_example_search_gains_empty_defaults():
    out = PT.validate_search(load("search.example.json"))
    assert out["boards"] == {} and out["board_title_words"] == []
    assert out["sources"] == ["indeed", "naukri", "linkedin"] and out["score_threshold"] == 70


def test_known_sources_is_every_registry_source():
    assert PT.KNOWN_SOURCES == set(_sources.ALL)
    assert {"indeed", "naukri", "linkedin", "glassdoor", "ziprecruiter", "google", "linkedin_apify"} <= PT.KNOWN_SOURCES


@pytest.mark.parametrize("name", list(_sources.BOARD_SYSTEMS) + list(_sources.FEEDS))
def test_every_new_source_name_is_accepted(name):
    assert PT.validate_search(_search(sources=["indeed", name]))["sources"] == ["indeed", name]


@pytest.mark.parametrize("group", ["boards", "feeds", "jobspy", "apify"])
def test_group_names_are_not_valid_in_the_config_file(group):
    assert any("sources: unknown" in p and group in p for p in _problems(sources=[group]))


def test_boards_normalised_keeps_entry_form_and_strips_names():
    out = PT.validate_search(_search(boards={
        "greenhouse": ["cloudflare", "stripe"], "ashby": [{"slug": "openai", "name": "  OpenAI  "}], "lever": []}))
    assert out["boards"] == {"greenhouse": ["cloudflare", "stripe"],
                             "ashby": [{"slug": "openai", "name": "OpenAI"}], "lever": []}


def test_boards_duplicate_slugs_are_dropped_keeping_the_first():
    out = PT.validate_search(_search(boards={"greenhouse": [
        "stripe", {"slug": "stripe", "name": "Stripe"}, "Stripe", "stripe"]}))
    assert out["boards"]["greenhouse"] == ["stripe", "Stripe"]          # case-sensitive
    out = PT.validate_search(_search(boards={"lever": [{"slug": "a", "name": "A"}, "a"]}))
    assert out["boards"]["lever"] == [{"slug": "a", "name": "A"}]
    # the same slug under two systems is fine
    out = PT.validate_search(_search(boards={"lever": ["a"], "ashby": ["a"]}))
    assert out["boards"] == {"lever": ["a"], "ashby": ["a"]}


def test_boards_not_an_object():
    for bad in (["greenhouse"], "greenhouse", 3, None):
        assert any(p.startswith("boards: must be an object like") for p in _problems(boards=bad))


def test_boards_unknown_system_names_the_valid_ones():
    (p,) = _problems(boards={"myspace": ["x"]})
    assert "boards: unknown system 'myspace'" in p
    assert all(s in p for s in _sources.BOARD_SYSTEMS)


def test_boards_value_must_be_a_list():
    (p,) = _problems(boards={"greenhouse": "stripe"})
    assert p == "boards.greenhouse: must be a list of slugs"


@pytest.mark.parametrize("slug", ["../x", "a b", "a" * 65, "-x", "acme\n", "ac\x00me", "x/y", "café"])
def test_boards_bad_slug(slug):
    (p,) = _problems(boards={"greenhouse": [slug]})
    assert p.startswith("boards.greenhouse: invalid slug ")
    assert "\n" not in p and "\x00" not in p                       # control characters never echoed raw
    assert repr(slug[:40]) in p                                    # safe, truncated, repr-style
    assert "letters, digits" in p


def test_boards_bad_slug_never_echoes_more_than_40_characters():
    long = "x" * 30 + "y" * 70
    (p,) = _problems(boards={"lever": [long]})
    assert "y" * 11 not in p and "x" * 30 in p
    (p,) = _problems(boards={"lever": [{"slug": long, "name": "N"}]})
    assert "y" * 11 not in p


def test_boards_non_string_slug():
    for bad in (7, None, ["a"], {"x": 1}, True):
        (p,) = _problems(boards={"workable": [bad]})
        assert p.startswith("boards.workable: ")


def test_boards_object_entry_rules():
    (p,) = _problems(boards={"ashby": [{"slug": "ok", "name": ""}]})
    assert "name" in p and "non-empty" in p
    (p,) = _problems(boards={"ashby": [{"slug": "ok", "name": "   "}]})
    assert "non-empty" in p
    (p,) = _problems(boards={"ashby": [{"slug": "ok", "name": "N" * 101}]})
    assert "100" in p
    (p,) = _problems(boards={"ashby": [{"slug": "ok"}]})
    assert "name" in p
    (p,) = _problems(boards={"ashby": [{"slug": "ok", "name": "N", "url": "x"}]})
    assert "unexpected" in p and "url" in p
    (p,) = _problems(boards={"ashby": [{"name": "N"}]})
    assert "slug" in p
    assert PT.validate_search(_search(boards={"ashby": [{"slug": "ok", "name": "N" * 100}]}))


def test_boards_at_most_300_slugs_in_total():
    ok = {"greenhouse": [f"g{i}" for i in range(150)], "lever": [f"l{i}" for i in range(150)]}
    assert sum(len(v) for v in PT.validate_search(_search(boards=ok))["boards"].values()) == 300
    ok["ashby"] = ["one-more"]
    (p,) = _problems(boards=ok)
    assert "300" in p and p.startswith("boards:")


def test_boards_cap_counts_after_dropping_duplicates():
    assert PT.validate_search(_search(boards={"greenhouse": ["same"] * 400}))["boards"]["greenhouse"] == ["same"]


def test_board_title_words():
    out = PT.validate_search(_search(board_title_words=["software", " sde "]))
    assert out["board_title_words"] == ["software", "sde"]
    assert any(p.startswith("board_title_words:") for p in _problems(board_title_words="software"))
    assert any(p.startswith("board_title_words:") for p in _problems(board_title_words=["ok", ""]))
    assert any("50" in p for p in _problems(board_title_words=[f"w{i}" for i in range(51)]))
    assert any("50" in p for p in _problems(board_title_words=["w" * 51]))
    assert PT.validate_search(_search(board_title_words=["w" * 50] * 1 + [f"w{i}" for i in range(49)]))


# ---------------------------------------------------------------- follow-up batch (Task F)

@pytest.mark.parametrize("name", ["Open\x00AI", "Open\nAI", "OpenAI\n", "\tOpenAI", "Open\x7fAI", "Open\x1bAI"])
def test_board_name_with_control_characters_is_a_problem(name):
    (p,) = _problems(boards={"ashby": [{"slug": "ok", "name": name}]})
    assert p == "boards.ashby: name must not contain control characters"


def test_board_name_with_ordinary_punctuation_is_still_fine():
    out = PT.validate_search(_search(boards={"ashby": [{"slug": "ok", "name": "Acme, Inc. (EU) - Ünïcode"}]}))
    assert out["boards"]["ashby"] == [{"slug": "ok", "name": "Acme, Inc. (EU) - Ünïcode"}]
