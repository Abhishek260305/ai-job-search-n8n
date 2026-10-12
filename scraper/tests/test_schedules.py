"""Schedules you can change: parsing what a person types, validation, and the plain-English description."""
from datetime import datetime, timedelta, timezone

import pytest

import schedules as S


@pytest.mark.parametrize("text,minutes", [
    ("6h", 360), ("6 h", 360), ("6hours", 360), ("6 hours", 360), ("1hr", 60), ("90m", 90), ("90 min", 90), ("45", 45),
    ("1d", 1440), ("2 days", 2880), ("1.5h", 90), ("1.1h", 66), ("0.7h", 42), (" 30m ", 30), ("30M", 30), ("6H", 360)])
def test_parse_duration(text, minutes):
    assert S.parse_duration(text) == minutes


@pytest.mark.parametrize("bad", ["", "soon", "-5m", "5x", "h6", "1.5m", "1e3", "6 hours later", "0.37h"])
def test_parse_duration_rejects_nonsense(bad):
    with pytest.raises(ValueError):
        S.parse_duration(bad)


@pytest.mark.parametrize("text,out", [("08:00", "08:00"), ("8:30", "08:30"), ("0:00", "00:00"), ("23:59", "23:59"), (" 7:05 ", "07:05")])
def test_parse_time(text, out):
    assert S.parse_time(text) == out


@pytest.mark.parametrize("bad", ["", "24:00", "8", "8am", "08:60", "8:5", "noon", "08:00:00"])
def test_parse_time_rejects_nonsense(bad):
    with pytest.raises(ValueError):
        S.parse_time(bad)


@pytest.mark.parametrize("minutes,text", [(30, "30 minutes"), (90, "90 minutes"), (60, "1 hour"), (360, "6 hours"), (1440, "1 day"), (2880, "2 days")])
def test_format_minutes(minutes, text):
    assert S.format_minutes(minutes) == text


def test_valid_updates_are_cleaned():
    assert S.validate_update("collect", {"every_minutes": "6h", "batch": 18, "enabled": True}) == {"every_minutes": 360, "batch": 18, "enabled": True}
    assert S.validate_update("collect", {"every_minutes": 360}) == {"every_minutes": 360}
    assert S.validate_update("digest", {"at_time": "8:30"}) == {"at_time": "08:30", "every_minutes": None}, "switching to daily clears the interval"
    assert S.validate_update("digest", {"every_minutes": "6h"}) == {"every_minutes": 360, "at_time": None}, "switching to an interval clears the time"
    assert S.validate_update("score", {"model": "claude-haiku-5-5", "batch": 25}) == {"model": "claude-haiku-5-5", "batch": 25}
    assert S.validate_update("tailor", {"batch": 10, "every_minutes": 5}) == {"batch": 10, "every_minutes": 5}
    assert S.validate_update("collect", {"batch": None}) == {}, "unset values are ignored"


@pytest.mark.parametrize("name,fields,message", [
    ("mystery", {"batch": 1}, "unknown schedule"),
    ("collect", {"every_minutes": 14}, "15 minutes"),                # gentle on the job boards
    ("collect", {"every_minutes": 10081}, "7 days"),
    ("score", {"every_minutes": 4}, "5 minutes"),
    ("tailor", {"every_minutes": 0}, "5 minutes"),
    ("digest", {"every_minutes": 30}, "1 hour"),
    ("collect", {"at_time": "08:00"}, "not at a time of day"),
    ("digest", {"at_time": "8"}, "not a time"),
    ("collect", {"batch": 51}, "1 to 50"),
    ("tailor", {"batch": 11}, "1 to 10"),
    ("score", {"batch": 0}, "from 1 to 50"),
    ("score", {"batch": True}, "whole number"),
    ("score", {"batch": "ten"}, "whole number"),
    ("collect", {"model": "claude-haiku-5-5"}, "only the score schedule"),
    ("score", {"model": "bad model!"}, "model must be a model name"),
    ("score", {"model": "x"}, "model must be a model name"),
    ("score", {"enabled": "yes"}, "on or off"),
    ("score", {"colour": "blue"}, "unknown schedule setting")])
def test_invalid_updates_say_why(name, fields, message):
    with pytest.raises(ValueError, match=message):
        S.validate_update(name, fields)


def row(name="collect", **kw):
    base = {"name": "collect", "enabled": True, "every_minutes": 360, "at_time": None, "batch": 18, "model": None, "run_now": False,
            "last_run_at": None, "next_run_at": None}
    base.update(kw, name=name)
    return base


NOW = datetime(2026, 10, 11, 12, 0, tzinfo=timezone.utc)


def test_describe_interval_and_daily_rows():
    lines = S.describe([
        row(last_run_at=NOW - timedelta(minutes=45), next_run_at=NOW + timedelta(hours=5, minutes=15)),
        row("score", every_minutes=30, batch=10, model="claude-haiku-5-5", last_run_at=NOW - timedelta(minutes=5), next_run_at=NOW + timedelta(minutes=25)),
        row("digest", every_minutes=None, at_time="08:00:00", batch=10, last_run_at=NOW - timedelta(hours=20), next_run_at=NOW + timedelta(hours=20))], NOW)
    assert "collect  on  every 6 hours" in lines[0] and "18 searches per run" in lines[0] and "last: 45 min ago" in lines[0] and "next: in 5 h 15 min" in lines[0]
    assert "model claude-haiku-5-5" in lines[1] and "10 jobs scored per run" in lines[1] and "next: in 25 min" in lines[1]
    assert "daily at 08:00" in lines[2] and "10 matches listed per run" in lines[2] and "last: 20 h 00 min ago" in lines[2]


def test_describe_states():
    off = S.describe([row(enabled=False, last_run_at=NOW - timedelta(days=3))], NOW)[0]
    assert "OFF" in off and "next: disabled" in off and "last: 3 days ago" in off
    assert "next: at the next tick (run requested)" in S.describe([row(run_now=True)], NOW)[0]
    assert "last: never" in S.describe([row(next_run_at=NOW - timedelta(minutes=1))], NOW)[0]
    assert "next: due now (starts within 5 minutes)" in S.describe([row(last_run_at=NOW - timedelta(hours=7), next_run_at=NOW - timedelta(hours=1))], NOW)[0]
    assert "next: within a minute" in S.describe([row(next_run_at=NOW + timedelta(seconds=30))], NOW)[0]
    assert "last: just now" in S.describe([row(last_run_at=NOW - timedelta(seconds=10), next_run_at=NOW + timedelta(hours=6))], NOW)[0]


# ---- board_batch (Phase 7, Task 13): how many company boards / feeds one collect run visits

def test_board_batch_is_accepted_for_collect():
    assert S.validate_update("collect", {"board_batch": 30}) == {"board_batch": 30}
    assert S.validate_update("collect", {"board_batch": 1}) == {"board_batch": 1}
    assert S.validate_update("collect", {"board_batch": 200, "batch": 10}) == {"board_batch": 200, "batch": 10}


@pytest.mark.parametrize("name", ["score", "tailor", "digest"])
def test_board_batch_is_refused_for_the_other_stages(name):
    with pytest.raises(ValueError, match="only the collect schedule"):
        S.validate_update(name, {"board_batch": 5})


@pytest.mark.parametrize("value", [0, 201, -3, True, False, 2.5, "30", 10000])
def test_board_batch_range_and_type(value):
    with pytest.raises(ValueError, match="board_batch must be a whole number from 1 to 200"):
        S.validate_update("collect", {"board_batch": value})


def test_the_database_layer_writes_board_batch_to_its_column(monkeypatch):
    """update_schedule only writes columns it knows: a cleaned board_batch must reach schedule_settings, not vanish.
    db.py is loaded under a private name with psycopg stubbed out, so this runs without a database driver."""
    import importlib.util
    import sys
    import types
    from contextlib import contextmanager
    from pathlib import Path

    for mod in ("psycopg", "psycopg.conninfo", "psycopg.rows", "psycopg.types", "psycopg.types.json"):
        monkeypatch.setitem(sys.modules, mod, types.SimpleNamespace(connect=None, make_conninfo=None, dict_row=None, Json=None))
    spec = importlib.util.spec_from_file_location("db_under_test", Path(__file__).resolve().parents[1] / "db.py")
    db = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(db)

    calls = []

    class Cur:
        def execute(self, sql, params=None):
            calls.append((sql, params))

    store = db.PgStore("host=x")

    @contextmanager
    def fake_cursor():
        yield Cur()
    store.cursor = fake_cursor
    store.update_schedule("collect", **S.validate_update("collect", {"board_batch": 45, "batch": 18}))
    sql, params = calls[0]
    assert "board_batch = %s" in sql and "batch = %s" in sql and params == [45, 18, "collect"]
