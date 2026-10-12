"""Schedules you can change: parsing, validation and plain-English descriptions.

The schedule itself lives in the database (`schedule_settings`, migration 009) and is claimed by
`claim_due_schedules()`; this module only turns what a person types ("6h", "08:00") into valid values and
describes them back. Pure functions, no I/O.

  collect  every N minutes   how many (title x location x source) searches per run, and how many company boards/feeds
  score    every N minutes   how many jobs per run, and which model
  tailor   every N minutes   how many applications per run
  digest   daily at HH:MM, or every N hours   how many matches in the message
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from fractions import Fraction
from typing import Optional

NAMES = ("collect", "score", "tailor", "digest")
DAILY = ("digest",)          # the only stage that may also run at a time of day
MIN_EVERY = {"collect": 15, "score": 5, "tailor": 5, "digest": 60}      # collect is gentler on the job boards
MAX_EVERY = 10080                                          # 7 days
MAX_BATCH = {"collect": 50, "score": 50, "tailor": 10, "digest": 50}
MAX_BOARD_BATCH = 200                                      # collect only; matches the CHECK in migration 013

_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(m|min|mins|minutes?|h|hr|hrs|hours?|d|days?)?\s*$", re.I)
_TIME = re.compile(r"^\s*([01]?\d|2[0-3]):([0-5]\d)\s*$")


def parse_duration(text: str) -> int:
    """'6h' -> 360, '90m' -> 90, '1d' -> 1440, '45' -> 45 (a bare number means minutes). Raises ValueError."""
    m = _DURATION.match(str(text))
    if not m:
        raise ValueError(f"'{text}' is not a duration: use for example 30m, 6h or 1d")
    unit = (m.group(2) or "m").lower()
    minutes = Fraction(m.group(1)) * (1 if unit.startswith("m") else 60 if unit.startswith("h") else 1440)   # exact: 1.1h is 66 minutes
    if minutes.denominator != 1:
        raise ValueError(f"'{text}' is not a whole number of minutes")
    return int(minutes)


def parse_time(text: str) -> str:
    """'8:30' -> '08:30'. Raises ValueError."""
    m = _TIME.match(str(text))
    if not m:
        raise ValueError(f"'{text}' is not a time: use 24-hour HH:MM, for example 08:00 or 20:30")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def format_minutes(minutes: int) -> str:
    if minutes % 1440 == 0:
        d = minutes // 1440
        return f"{d} day" + ("s" if d != 1 else "")
    if minutes % 60 == 0:
        h = minutes // 60
        return f"{h} hour" + ("s" if h != 1 else "")
    return f"{minutes} minutes"


def validate_update(name: str, fields: dict) -> dict:
    """Check a change to one schedule and return the clean column values. Raises ValueError."""
    if name not in NAMES:
        raise ValueError(f"unknown schedule '{name}' (one of: {', '.join(NAMES)})")
    out: dict = {}
    for key, value in fields.items():
        if value is None:
            continue
        if key == "enabled":
            if not isinstance(value, bool):
                raise ValueError("enabled must be on or off")
            out["enabled"] = value
        elif key == "every_minutes":
            minutes = parse_duration(value) if isinstance(value, str) else value
            if not isinstance(minutes, int) or isinstance(minutes, bool):
                raise ValueError("every must be a duration such as 30m or 6h")
            lo = MIN_EVERY[name]
            if not lo <= minutes <= MAX_EVERY:
                raise ValueError(f"{name} can run every {format_minutes(lo)} to 7 days"
                                 + (" (kept gentle on the job boards)" if name == "collect" else ""))
            out["every_minutes"] = minutes
            if name in DAILY:
                out["at_time"] = None          # a schedule is an interval or a daily time, never both
        elif key == "at_time":
            if name not in DAILY:
                raise ValueError(f"{name} runs every N minutes (use --every), not at a time of day")
            out["at_time"] = parse_time(value)
            out["every_minutes"] = None
        elif key == "batch":
            if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= MAX_BATCH[name]:
                raise ValueError(f"batch for {name} must be a whole number from 1 to {MAX_BATCH[name]}")
            out["batch"] = value
        elif key == "board_batch":
            if name != "collect":
                raise ValueError("only the collect schedule has board_batch (how many company boards and feeds one run visits; use --boards on collect)")
            if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= MAX_BOARD_BATCH:
                raise ValueError(f"board_batch must be a whole number from 1 to {MAX_BOARD_BATCH}")
            out["board_batch"] = value
        elif key == "model":
            if name != "score":
                raise ValueError("only the score schedule has a model (tailoring has its own: tailor-settings --model)")
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9._-]{3,80}", value):
                raise ValueError("model must be a model name such as claude-haiku-5-5")
            out["model"] = value
        else:
            raise ValueError(f"unknown schedule setting '{key}'")
    return out


def _when(ts: Optional[datetime], now: datetime) -> str:
    if ts is None:
        return "never"
    delta = now - ts
    if delta < timedelta(0):                       # in the future (a next run)
        left = -delta
        if left < timedelta(minutes=1):
            return "within a minute"
        mins = int(left.total_seconds() // 60)
        return ("in " + (f"{mins} min" if mins < 90 else f"{mins // 60} h {mins % 60:02d} min"))
    mins = int(delta.total_seconds() // 60)
    if mins < 1:
        return "just now"
    return (f"{mins} min ago" if mins < 90 else f"{mins // 60} h {mins % 60:02d} min ago" if mins < 2880 else f"{mins // 1440} days ago")


def describe(rows: list[dict], now: Optional[datetime] = None) -> list[str]:
    """One line per schedule: what it does, how often, how much, and when it last and next runs."""
    now = now or datetime.now().astimezone()
    work = {"collect": "searches", "score": "jobs scored", "tailor": "applications", "digest": "matches listed"}
    lines = []
    for r in rows:
        every = f"daily at {str(r['at_time'])[:5]}" if r.get("at_time") is not None else f"every {format_minutes(r['every_minutes'])}"
        state = "on " if r["enabled"] else "OFF"
        extra = f", model {r['model']}" if r.get("model") else ""
        nxt = "disabled" if not r["enabled"] and not r.get("run_now") else ("at the next tick (run requested)" if r.get("run_now") else _when(r.get("next_run_at"), now))
        if r["enabled"] and not r.get("run_now") and r.get("next_run_at") is not None and r["next_run_at"] <= now:
            nxt = "due now (starts within 5 minutes)"
        lines.append(f"{r['name']:<8} {state} {every:<22} {r['batch']:>2} {work[r['name']]} per run{extra}"
                     f"   last: {_when(r.get('last_run_at'), now)}   next: {nxt}")
    return lines
