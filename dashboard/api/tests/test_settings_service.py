"""The settings page's logic, and the guards that keep the dashboard's copies of the scraper's rules honest."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

import models
import settings_service
from settings import SettingsError, load_settings
from store import Invalid
from tests.support import FakeScraper, make_env

SCRAPER = Path(__file__).resolve().parents[3] / "scraper"


def scraper_source(name: str) -> str:
    path = SCRAPER / name
    if not path.is_file():
        pytest.skip("scraper/ is not next to dashboard/ (inside the image)")
    return path.read_text(encoding="utf-8")


def class_fields(source: str, class_name: str) -> set[str]:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {n.target.id for n in node.body if isinstance(n, ast.AnnAssign)}
    raise AssertionError(f"{class_name} not found")


def module_constant(source: str, name: str):
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(getattr(t, "id", None) == name for t in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found")


# ---------------------------------------------------------------- the page and the lower-only rule

def test_page_collects_the_four_reports():
    scraper = FakeScraper()
    page = settings_service.page(scraper)
    assert [c[0] for c in scraper.calls] == ["schedule", "features", "tailoring", "budget"]
    assert page["budget"].keys() == {"monthly_cap_usd", "daily_cap_usd", "apify_monthly_cap_usd", "apify_daily_cap_usd", "warn_pct"}
    assert page["limits"] == settings_service.LIMITS
    models.SettingsPage.model_validate(page)


def test_lower_only_sends_only_the_caps_that_changed_and_nothing_else():
    scraper = FakeScraper()
    settings_service.lower_only(scraper, {"monthly_cap_usd": 5, "daily_cap_usd": None, "warn_pct": 10, "timezone": "UTC"})
    assert scraper.writes() == [("set_budget", {"monthly_cap_usd": 5})]


@pytest.mark.parametrize("changes", [{}, {"monthly_cap_usd": None}, {"warn_pct": 50}])
def test_lower_only_refuses_an_empty_change(changes):
    with pytest.raises(Invalid, match="nothing to change"):
        settings_service.lower_only(FakeScraper(), changes)


def test_lower_only_names_what_it_refused():
    scraper = FakeScraper()
    with pytest.raises(Invalid) as e:
        settings_service.lower_only(scraper, {"monthly_cap_usd": 5, "apify_daily_cap_usd": 1})
    assert "apify daily cap" in str(e.value) and "monthly" not in str(e.value) and scraper.writes() == []


def test_a_float_rounding_edge_is_not_a_raise():
    scraper = FakeScraper()
    scraper.budget_settings["daily_cap_usd"] = 0.1 + 0.2          # 0.30000000000000004
    settings_service.lower_only(scraper, {"daily_cap_usd": 0.3})
    assert scraper.writes() == [("set_budget", {"daily_cap_usd": 0.3})]


# ---------------------------------------------------------------- drift guards against scraper/

def test_the_limits_match_the_scrapers():
    source = scraper_source("schedules.py")
    assert settings_service.LIMITS == {
        "min_every": module_constant(source, "MIN_EVERY"), "max_every": module_constant(source, "MAX_EVERY"),
        "max_batch": module_constant(source, "MAX_BATCH"), "max_board_batch": module_constant(source, "MAX_BOARD_BATCH")}


def test_every_schedule_name_is_one_the_scraper_knows():
    from typing import get_args
    assert set(get_args(models.ScheduleName)) == set(module_constant(scraper_source("schedules.py"), "NAMES"))


def test_what_the_page_can_send_is_a_subset_of_what_the_scraper_accepts():
    app_source = scraper_source("app.py")
    assert set(models.ScheduleChange.model_fields) <= class_fields(app_source, "ScheduleRequest")
    assert set(models.FeaturesChange.model_fields) == class_fields(app_source, "FeaturesRequest")
    assert set(models.TailoringChange.model_fields) <= class_fields(app_source, "TailorSettingsRequest")
    assert set(models.BudgetChange.model_fields) <= class_fields(app_source, "BudgetSettingsRequest")


def test_the_page_cannot_send_what_stays_a_command():
    """Each of these changes how money or approvals flow; the CLI (or .env) is where that decision is made."""
    assert "approval" not in models.TailoringChange.model_fields
    assert "model" not in models.TailoringChange.model_fields and "style_notes" not in models.TailoringChange.model_fields
    assert "model" not in models.ScheduleChange.model_fields, "the score model changes what each scored job costs"
    assert not hasattr(models, "TimezoneChange"), "a time-zone change moves the day a budget counts from"
    assert {"warn_pct", "timezone"}.isdisjoint(models.BudgetChange.model_fields)
    assert "run_now" not in models.ScheduleChange.model_fields


def test_the_scraper_still_reads_the_score_band_clear_list_the_page_sends():
    source = scraper_source("app.py")
    for word in ("min_score", "max_score", "daily_limit", "style_notes"):
        assert word in source
    assert 'k not in ("min_score", "max_score", "daily_limit", "style_notes")' in source


# ---------------------------------------------------------------- SCRAPER_URL

def test_the_default_scraper_address(tmp_path):
    assert load_settings(make_env(tmp_path)).scraper_url == "http://scraper:8000"


def test_a_custom_scraper_address(tmp_path):
    assert load_settings(make_env(tmp_path, SCRAPER_URL="http://127.0.0.1:9000/")).scraper_url == "http://127.0.0.1:9000"


@pytest.mark.parametrize("url", ["scraper:8000", "ftp://scraper", "http://user:pw@scraper"])
def test_a_bad_scraper_address_stops_the_service_with_a_clear_message(tmp_path, url):
    with pytest.raises(SettingsError, match="SCRAPER_URL"):
        load_settings(make_env(tmp_path, SCRAPER_URL=url))


# ---------------------------------------------------------------- version 2c drift guards

def test_the_page_can_send_exactly_the_profile_keys_the_scraper_stores():
    from models import SearchProfile, SearchProfileIn
    keys = tuple(module_constant(scraper_source("profile_tools.py"), "PROFILE_KEYS"))
    assert set(SearchProfileIn.model_fields) == set(keys) == set(SearchProfile.model_fields)


def test_the_board_systems_are_the_scrapers():
    from typing import get_args
    assert set(get_args(models.BoardSystem)) == set(module_constant(scraper_source("sources.py"), "BOARD_SYSTEMS"))


def test_a_company_entry_has_only_the_two_keys_the_scraper_accepts():
    source = scraper_source("profile_tools.py")
    assert 'only "slug" and "name" are allowed' in source
    assert set(models.BoardEntry.model_fields) == {"slug", "name"}


def test_entries_round_trip_between_the_files_format_and_the_pages():
    scraper = FakeScraper()
    page = settings_service.search_page(scraper)
    assert page["profile"]["boards"]["greenhouse"] == [{"slug": "stripe", "name": None}, {"slug": "notion", "name": "Notion"}]
    back = settings_service.to_scraper_profile(page["profile"])
    assert back["boards"] == scraper.profile_state["profile"]["boards"]
    assert back == scraper.profile_state["profile"]
