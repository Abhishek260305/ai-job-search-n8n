"""The jobs table as a CSV file for a spreadsheet.

Titles, companies and links come from job postings, so from the internet. A spreadsheet runs a cell that starts with
= + - @ (or a tab or carriage return, OWASP's list) as a formula, so such a cell gets a leading ' (the CLI's
export_csv does the same for the first four). The file starts with a UTF-8 byte order mark, without which Excel
reads it as Latin-1 and garbles rupee signs and non-Latin names.
"""
from __future__ import annotations

import csv
import io
import math
from datetime import date, datetime
from decimal import Decimal

BOM = "\ufeff"
FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")
HEADER = ["id", "company", "title", "location", "remote", "salary", "score", "verdict", "pipeline status",
          "sources", "apply link", "posted", "first seen", "applied at"]


def safe_cell(value) -> str:
    if value is None:
        return ""
    text = str(value)
    return "'" + text if text.startswith(FORMULA_STARTS) else text


def _number(value) -> str:
    # Postgres numeric allows NaN and Infinity, and int() raises on both
    if isinstance(value, (Decimal, float)) and math.isfinite(value) and value == int(value):
        return str(int(value))
    return str(value)


def salary(row: dict) -> str:
    low, high, currency = row.get("salary_min"), row.get("salary_max"), row.get("salary_currency")
    if low is None and high is None:
        return ""
    if low is not None and high is not None and low != high:
        amount = f"{_number(low)}-{_number(high)}"
    else:
        amount = _number(low if low is not None else high)
    return f"{amount} {currency}" if currency else amount


def _when(value) -> str:
    if isinstance(value, datetime):
        return value.isoformat(timespec="minutes")
    if isinstance(value, date):
        return value.isoformat()
    return "" if value is None else str(value)


def _remote(value) -> str:
    return {True: "yes", False: "no"}.get(value, "")


def to_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    buf.write(BOM)
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(HEADER)
    for r in rows:
        writer.writerow(safe_cell(v) for v in (
            r.get("id"), r.get("company"), r.get("title"), r.get("location"), _remote(r.get("is_remote")),
            salary(r), r.get("score"), r.get("verdict"), r.get("pipeline_status"), " ".join(r.get("sources") or []),
            r.get("apply_url"), _when(r.get("posted_at")), _when(r.get("first_seen")), _when(r.get("applied_at")),
        ))
    return buf.getvalue()
