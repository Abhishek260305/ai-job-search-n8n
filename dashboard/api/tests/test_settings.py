"""Settings refuse to start the dashboard on a missing or weak secret, and never echo a value back."""
from __future__ import annotations

import pytest

import app as dashboard_app
from settings import DEFAULT_HOSTS, SettingsError, load_settings
from tests.support import PASSWORD, SECRET, make_env


def test_a_complete_environment_loads_with_safe_defaults(tmp_path):
    s = load_settings(make_env(tmp_path))
    assert s.password == PASSWORD and s.secret == SECRET
    assert (s.db_host, s.db_port, s.db_name, s.db_user) == ("postgres", 5432, "jobsearch", "jobsearch_dashboard")
    assert s.allowed_hosts == DEFAULT_HOSTS
    assert s.session_days == 7


@pytest.mark.parametrize("overrides, problem", [
    ({"DASHBOARD_PASSWORD": None}, "DASHBOARD_PASSWORD is not set"),
    ({"DASHBOARD_PASSWORD": ""}, "DASHBOARD_PASSWORD is not set"),
    ({"DASHBOARD_PASSWORD": "short-pw-11"}, "DASHBOARD_PASSWORD must be at least 12 characters"),
    ({"DASHBOARD_SECRET": None}, "DASHBOARD_SECRET is not set"),
    ({"DASHBOARD_SECRET": "x" * 31}, "DASHBOARD_SECRET must be at least 32 characters"),
    ({"DB_PASSWORD": None}, "DB_PASSWORD is not set"),
    ({"DASHBOARD_PASSWORD": SECRET}, "DASHBOARD_PASSWORD and DASHBOARD_SECRET must be different"),
    ({"DB_PORT": "five"}, "DB_PORT must be a whole number"),
    ({"DB_PORT": "70000"}, "DB_PORT must be between 1 and 65535"),
    ({"DASHBOARD_SESSION_DAYS": "0"}, "DASHBOARD_SESSION_DAYS must be between 1 and 90"),
    ({"DASHBOARD_SESSION_DAYS": "91"}, "DASHBOARD_SESSION_DAYS must be between 1 and 90"),
    ({"DASHBOARD_SESSION_DAYS": "a week"}, "DASHBOARD_SESSION_DAYS must be a whole number"),
])
def test_each_bad_value_is_refused_by_name(tmp_path, overrides, problem):
    with pytest.raises(SettingsError) as e:
        load_settings(make_env(tmp_path, **overrides))
    assert problem in str(e.value)


def test_the_placeholder_from_env_example_is_refused(tmp_path):
    # "change-me" is shorter than the minimum anyway; a space-padded variant must still be caught as the placeholder
    with pytest.raises(SettingsError, match="placeholder"):
        load_settings(make_env(tmp_path, DASHBOARD_PASSWORD="    CHANGE-ME    "))


def test_every_problem_is_reported_at_once(tmp_path):
    with pytest.raises(SettingsError) as e:
        load_settings(make_env(tmp_path, DASHBOARD_PASSWORD=None, DASHBOARD_SECRET="short", DB_PASSWORD=None))
    msg = str(e.value)
    assert "DASHBOARD_PASSWORD is not set" in msg and "DASHBOARD_SECRET must be" in msg and "DB_PASSWORD is not set" in msg


def test_messages_never_contain_the_values(tmp_path):
    weak_password, weak_secret = "tiny-pw-xyz", "tiny-secret-value-xyz"
    with pytest.raises(SettingsError) as e:
        load_settings(make_env(tmp_path, DASHBOARD_PASSWORD=weak_password, DASHBOARD_SECRET=weak_secret))
    assert weak_password not in str(e.value) and weak_secret not in str(e.value)


def test_repr_hides_the_secrets(tmp_path):
    s = load_settings(make_env(tmp_path))
    text = repr(s)
    assert PASSWORD not in text and SECRET not in text and "db-pass-for-tests" not in text
    assert "jobsearch_dashboard@postgres:5432/jobsearch" in text


def test_extra_allowed_hosts_are_added_lowercased(tmp_path):
    s = load_settings(make_env(tmp_path, DASHBOARD_ALLOWED_HOSTS=" MyVM.tailnet.ts.net , dashboard.local ,"))
    assert s.allowed_hosts == DEFAULT_HOSTS | {"myvm.tailnet.ts.net", "dashboard.local"}


@pytest.mark.parametrize("bad", ["localhost:8080", "http://evil.example", "*.example.com", "two words", "-bad.example"])
def test_allowed_hosts_must_be_plain_names(tmp_path, bad):
    with pytest.raises(SettingsError, match="DASHBOARD_ALLOWED_HOSTS"):
        load_settings(make_env(tmp_path, DASHBOARD_ALLOWED_HOSTS=bad))


def test_conninfo_targets_the_dashboard_role(tmp_path):
    info = load_settings(make_env(tmp_path, DB_HOST="db.internal")).conninfo()
    assert "host=db.internal" in info and "dbname=jobsearch" in info and "user=jobsearch_dashboard" in info
    assert "application_name=jobsearch-dashboard" in info


def test_build_exits_cleanly_without_printing_secrets(tmp_path, monkeypatch, capsys):
    for k, v in make_env(tmp_path, DASHBOARD_PASSWORD="leaky-short").items():   # 11 characters
        monkeypatch.setenv(k, v)
    with pytest.raises(SystemExit) as e:
        dashboard_app.build()
    assert e.value.code == 1
    err = capsys.readouterr().err
    assert "dashboard: cannot start: DASHBOARD_PASSWORD must be at least 12 characters" in err
    assert "leaky-short" not in err


def test_build_returns_an_app_when_settings_are_valid(tmp_path, monkeypatch):
    for k, v in make_env(tmp_path).items():
        monkeypatch.setenv(k, v)
    assert dashboard_app.build().title == "jobsearch dashboard"
