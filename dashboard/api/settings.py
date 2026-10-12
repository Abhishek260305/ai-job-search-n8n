"""The dashboard's settings, read once from the environment at startup.

Everything comes from docker-compose.yml, which takes the secrets from .env. A missing or weak value stops the
service before it listens: a dashboard with an empty password, or a session secret anyone could guess, must never
start. Error messages name the variable and the problem, never the value.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from psycopg.conninfo import make_conninfo

from scraper_client import check_url

MIN_PASSWORD = 12
MIN_SECRET = 32
DEFAULT_HOSTS = frozenset({"localhost", "127.0.0.1"})
_HOSTNAME = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)*$")
_PLACEHOLDERS = ("change-me", "changeme", "change_me")


class SettingsError(ValueError):
    """One message listing every problem, so a user fixes .env once instead of once per restart."""


@dataclass(frozen=True)
class Settings:
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str
    password: str                    # what you type on the login page
    secret: str                      # signs the session cookie; changing it logs everyone out
    allowed_hosts: frozenset[str]    # Host header values accepted (DNS-rebinding protection)
    session_days: int
    scraper_url: str                 # where the settings page's changes go (the scraper's own validated endpoints)
    output_dir: Path                 # tailored PDFs, mounted read-only
    static_dir: Path                 # the built React app

    def conninfo(self) -> str:
        return make_conninfo(host=self.db_host, port=str(self.db_port), dbname=self.db_name,
                             user=self.db_user, password=self.db_password,
                             application_name="jobsearch-dashboard")

    def __repr__(self) -> str:       # never print the secrets, even in a traceback
        return (f"Settings(db={self.db_user}@{self.db_host}:{self.db_port}/{self.db_name}, "
                f"allowed_hosts={sorted(self.allowed_hosts)}, session_days={self.session_days})")


def _secret(env, name: str, minimum: int, problems: list[str]) -> str:
    value = env.get(name, "")
    if not value:
        problems.append(f"{name} is not set")
    elif len(value) < minimum:
        problems.append(f"{name} must be at least {minimum} characters")
    elif value.strip().lower() in _PLACEHOLDERS:
        problems.append(f"{name} is still the placeholder value")
    return value


def _int(env, name: str, default: int, low: int, high: int, problems: list[str]) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        problems.append(f"{name} must be a whole number")
        return default
    if not low <= value <= high:
        problems.append(f"{name} must be between {low} and {high}")
    return value


def _hosts(env, problems: list[str]) -> frozenset[str]:
    extra = set()
    for item in env.get("DASHBOARD_ALLOWED_HOSTS", "").split(","):
        name = item.strip().lower()
        if not name:
            continue
        if not _HOSTNAME.match(name):
            problems.append("DASHBOARD_ALLOWED_HOSTS entries must be plain host names (no port, scheme, space or wildcard)")
            break
        extra.add(name)
    return DEFAULT_HOSTS | extra


def load_settings(env=None) -> Settings:
    env = os.environ if env is None else env
    problems: list[str] = []
    password = _secret(env, "DASHBOARD_PASSWORD", MIN_PASSWORD, problems)
    secret = _secret(env, "DASHBOARD_SECRET", MIN_SECRET, problems)
    if password and secret and password == secret:
        problems.append("DASHBOARD_PASSWORD and DASHBOARD_SECRET must be different")
    db_password = env.get("DB_PASSWORD", "")
    if not db_password:
        problems.append("DB_PASSWORD is not set")
    try:
        scraper_url = check_url(env.get("SCRAPER_URL", "").strip() or "http://scraper:8000")
    except ValueError as e:
        problems.append(str(e))
        scraper_url = "http://scraper:8000"
    settings = Settings(
        db_host=env.get("DB_HOST", "postgres").strip() or "postgres",
        db_port=_int(env, "DB_PORT", 5432, 1, 65535, problems),
        db_name=env.get("DB_NAME", "jobsearch").strip() or "jobsearch",
        db_user=env.get("DB_USER", "jobsearch_dashboard").strip() or "jobsearch_dashboard",
        db_password=db_password,
        password=password,
        secret=secret,
        allowed_hosts=_hosts(env, problems),
        session_days=_int(env, "DASHBOARD_SESSION_DAYS", 7, 1, 90, problems),
        scraper_url=scraper_url,
        output_dir=Path(env.get("OUTPUT_DIR", "/output")),
        static_dir=Path(env.get("STATIC_DIR", str(Path(__file__).resolve().parent / "static"))),
    )
    if problems:
        raise SettingsError("; ".join(problems))
    return settings
