"""The CSV export: formula cells defused, the byte order mark, and the column formatting."""
from __future__ import annotations

import csv
import io
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

import csv_export
from tests.support import job_row


def parse(text: str) -> list[list[str]]:
    assert text.startswith(csv_export.BOM)
    return list(csv.reader(io.StringIO(text[1:], newline="")))


def test_header_and_bom():
    rows = parse(csv_export.to_csv([]))
    assert rows == [csv_export.HEADER]
    assert csv_export.HEADER == ["id", "company", "title", "location", "remote", "salary", "score", "verdict",
                                 "pipeline status", "sources", "apply link", "posted", "first seen", "applied at"]


def test_a_full_row():
    row = job_row(7, company="Acme", title="Backend Engineer", location="Pune, India", is_remote=True,
                  salary_min=Decimal("1200000"), salary_max=Decimal("1800000.00"), salary_currency="INR", score=82,
                  verdict="strong", pipeline_status="interview", sources=["greenhouse", "indeed"],
                  posted_at=datetime(2026, 10, 1, 8, 15, tzinfo=timezone.utc),
                  applied_at=datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc))
    _, line = parse(csv_export.to_csv([row]))
    assert line == ["7", "Acme", "Backend Engineer", "Pune, India", "yes", "1200000-1800000 INR", "82", "strong",
                    "interview", "greenhouse indeed", "https://example.com/jobs/7", "2026-10-01T08:15+00:00",
                    "2026-10-12T09:30+00:00", "2026-10-05T12:00+00:00"]


def test_empty_values_are_blank():
    row = job_row(1, company=None, location=None, is_remote=None, score=None, verdict=None, sources=[],
                  apply_url=None, posted_at=None, applied_at=None)
    _, line = parse(csv_export.to_csv([row]))
    assert line[1] == line[3] == line[4] == line[5] == line[6] == line[9] == line[10] == line[11] == line[13] == ""


@pytest.mark.parametrize("danger", ["=", "+", "-", "@", "\t", "\r"])
def test_every_formula_start_is_defused(danger):
    title = danger + "HYPERLINK(\"http://evil.example\",\"click\")"
    _, line = parse(csv_export.to_csv([job_row(1, title=title, company=danger + "cmd", location=danger + "1+1")]))
    assert line[2] == "'" + title
    assert line[1] == "'" + danger + "cmd" and line[3] == "'" + danger + "1+1"


def test_normal_text_is_untouched():
    _, line = parse(csv_export.to_csv([job_row(1, title="Senior Engineer (Python) - Remote", company="A=B Labs")]))
    assert line[2] == "Senior Engineer (Python) - Remote" and line[1] == "A=B Labs"


def test_commas_quotes_and_newlines_stay_in_their_cell():
    _, line = parse(csv_export.to_csv([job_row(1, title='Engineer, "Platform"\nteam')]))
    assert line[2] == 'Engineer, "Platform"\nteam'


def test_non_latin_text_and_the_rupee_sign_survive():
    text = csv_export.to_csv([job_row(1, company="टाटा", salary_min=Decimal("50000"), salary_max=None,
                                      salary_currency="₹")])
    _, line = parse(text)
    assert line[1] == "टाटा" and line[5] == "50000 ₹"
    assert text.encode("utf-8").startswith(b"\xef\xbb\xbf")


@pytest.mark.parametrize("low, high, currency, expected", [
    (None, None, "INR", ""),
    (Decimal("100000"), None, None, "100000"),
    (None, Decimal("90000"), "USD", "90000 USD"),
    (Decimal("50000"), Decimal("50000"), "EUR", "50000 EUR"),
    (Decimal("12.5"), Decimal("20"), "USD", "12.5-20 USD"),
    (Decimal("NaN"), None, None, "NaN"),
    (Decimal("Infinity"), None, None, "Infinity"),
    (75000.0, 95000.0, "GBP", "75000-95000 GBP"),
])
def test_salary(low, high, currency, expected):
    assert csv_export.salary({"salary_min": low, "salary_max": high, "salary_currency": currency}) == expected


def test_a_date_and_a_plain_value():
    assert csv_export._when(date(2026, 10, 12)) == "2026-10-12"
    assert csv_export._when(None) == ""
