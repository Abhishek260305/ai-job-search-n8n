"""Input validation, and the guards that keep the fixed lists in step with the database and the scraper."""
from __future__ import annotations

import re
import sys
import typing
from pathlib import Path

import pytest
from pydantic import ValidationError

from models import ByStatus, EditableStatus, JobsQuery, PipelineStatus, SourceName, StatusChange

REPO = Path(__file__).resolve().parents[3]
MIGRATIONS = REPO / "postgres" / "migrations"


def _check_list(sql: str, constraint: str) -> set[str]:
    m = re.search(constraint + r" CHECK \(status IN \((.*?)\)\)", sql, re.S)
    assert m, constraint
    return set(re.findall(r"'([a-z_]+)'", m.group(1)))


# ---------------------------------------------------------------- JobsQuery

def test_defaults():
    q = JobsQuery()
    assert (q.tab, q.sort, q.order, q.page, q.page_size, q.remote) == ("shortlisted", "score", "desc", 1, 50, False)
    assert q.status == [] and q.source == [] and q.q is None


def test_query_string_values_are_coerced_like_a_browser_sends_them():
    q = JobsQuery.model_validate({"page": "2", "page_size": "100", "min_score": "60", "remote": "true",
                                  "posted_days": "7", "status": ["interview", "offer"], "source": ["greenhouse"]})
    assert (q.page, q.page_size, q.min_score, q.remote, q.posted_days) == (2, 100, 60, True, 7)


@pytest.mark.parametrize("bad", [
    {"page_size": 1000}, {"page_size": "10"}, {"page": 0}, {"page": 10_001}, {"min_score": 101}, {"min_score": -1},
    {"posted_days": 0}, {"posted_days": 366}, {"sort": "salary; DROP TABLE jobs"}, {"order": "sideways"},
    {"tab": "everything"}, {"status": ["hired"]}, {"source": ["monster"]}, {"q": "x" * 101},
    {"unknown_param": "1"}, {"remote": "maybe"},
])
def test_bad_query_values_are_refused(bad):
    with pytest.raises(ValidationError):
        JobsQuery.model_validate(bad)


def test_status_change_validation():
    ok = StatusChange.model_validate({"status": "interview", "expected_status": "applied", "note": "phone screen"})
    assert ok.status == "interview"
    for bad in ({"status": "drafted", "expected_status": "approved"},
                {"status": "hired", "expected_status": "applied"},
                {"status": "applied", "expected_status": "nonsense"},
                {"status": "applied"},
                {"status": "applied", "expected_status": "shortlisted", "note": "x" * 501},
                {"status": "applied", "expected_status": "shortlisted", "extra": 1}):
        with pytest.raises(ValidationError):
            StatusChange.model_validate(bad)


# ---------------------------------------------------------------- the fixed lists stay in step

def test_source_names_match_the_scraper_registry():
    scraper = REPO / "scraper"
    if not (scraper / "sources.py").is_file():
        pytest.skip("scraper/ is not next to dashboard/ (inside the image)")
    sys.path.insert(0, str(scraper))
    try:
        import sources
    finally:
        sys.path.remove(str(scraper))
    assert set(typing.get_args(SourceName)) == set(sources.ALL)


def test_pipeline_statuses_cover_both_status_columns():
    if not MIGRATIONS.is_dir():
        pytest.skip("postgres/ is not next to dashboard/")
    jobs = _check_list((MIGRATIONS / "001_safety_scoring.sql").read_text(), "jobs_status_check")
    applications = _check_list((MIGRATIONS / "006_tailoring.sql").read_text(), "applications_status_check")
    assert set(typing.get_args(PipelineStatus)) == jobs | applications


def test_editable_statuses_match_the_guarded_function():
    if not MIGRATIONS.is_dir():
        pytest.skip("postgres/ is not next to dashboard/")
    sql = (MIGRATIONS / "014_dashboard.sql").read_text()
    m = re.search(r"p_status NOT IN \((.*?)\)", sql, re.S)
    assert m
    assert set(typing.get_args(EditableStatus)) == set(re.findall(r"'([a-z_]+)'", m.group(1)))


def test_by_status_matches_the_summary_function():
    if not MIGRATIONS.is_dir():
        pytest.skip("postgres/ is not next to dashboard/")
    sql = (MIGRATIONS / "014_dashboard.sql").read_text()
    m = re.search(r"'by_status', jsonb_build_object\((.*?)\)", sql, re.S)
    assert m
    assert set(ByStatus.model_fields) == set(re.findall(r"'([a-z_]+)',", m.group(1)))
