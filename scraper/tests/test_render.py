import re

import httpx
import pytest

import render as R
import tailor as T
from tests.test_tailor import RESUME, PROFILE, JOB, GOOD_PATCH


def make_pdf(pages: list[list[str]]) -> bytes:
    """A tiny valid PDF with one text line per entry, enough for pypdf to read back."""
    objs: list[bytes] = []
    n_pages = len(pages)
    font_id = 3 + 2 * n_pages
    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(n_pages))
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n_pages} >>".encode())
    for i, lines in enumerate(pages):
        stream = "BT /F1 10 Tf 40 780 Td 14 TL " + " ".join(
            f"({ln.replace('(', '[').replace(')', ']')}) Tj T*" for ln in lines) + " ET"
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents {4 + 2 * i} 0 R "
                    f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>".encode())
        objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream".encode())
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


LONG = ["Jane Doe jane@example.com Pune India"] + [f"Built a platform line number {i} with Go and PostgreSQL for tenants" for i in range(12)]


# ------------------------------------------------------------------ dates

@pytest.mark.parametrize("raw,expected", [("2026-01", "Jan 2026"), ("2025-12", "Dec 2025"), ("2022", "2022"), ("", ""), ("2026-13", "2026-13")])
def test_fmt_date(raw, expected):
    assert R.fmt_date(raw) == expected


@pytest.mark.parametrize("s,en,expected", [
    ("2026-01", "", "Jan 2026 – Present"), ("2025-07", "2026-01", "Jul 2025 – Jan 2026"),
    ("2022", "2026", "2022 – 2026"), ("2025-06", "2025-06", "Jun 2025"), ("", "", ""), ("", "2024", "2024"),
])
def test_date_range(s, en, expected):
    assert R.date_range(s, en) == expected


# ------------------------------------------------------------------ resume HTML

def view(patch=None):
    return T.apply_patch(RESUME, T.validate_patch(patch or GOOD_PATCH, RESUME), PROFILE)


def test_resume_html_contains_the_content_in_order():
    h = R.resume_html(view())
    assert "<h1>Jane Doe</h1>" in h and "Backend Engineer" in h and "jane@example.com" in h
    assert h.index("Work Experience") < h.index("Projects") < h.index("Education") < h.index("Technical Skills")
    assert "Jan 2026 – Present" in h and "Software Engineer Intern" in h
    assert h.index("Realtime Gateway") < h.index("Probe Platform"), "the patch's bullet order is respected"
    assert '<b class="k">PostgreSQL</b>' in h and "--s: 1.0" in h


def test_everything_is_escaped():
    v = view()
    v["work"][0]["groups"][0]["bullets"][0] = "<script>alert(1)</script> & more"
    v["basics"]["name"] = 'Jane "J" <Doe>'
    v["projects"][0]["url"] = "javascript:alert(1)"
    v["basics"]["profiles"] = [{"network": "x", "url": "javascript:alert(2)"}, {"network": "GitHub", "url": "https://github.com/jane"}]
    h = R.resume_html(v)
    assert "<script>" not in h and "&lt;script&gt;" in h and "&amp; more" in h
    assert "javascript:" not in h and 'href="https://github.com/jane"' in h
    assert "&lt;Doe&gt;" in h


def test_scale_is_applied():
    assert "--s: 0.88" in R.resume_html(view(), 0.88)


def test_sections_that_are_empty_are_left_out():
    v = view(); v["projects"] = []; v["additional"] = []; v["summary"] = None
    h = R.resume_html(v)
    assert "<h2>Projects</h2>" not in h and "<h2>Additional</h2>" not in h and 'class="summary"' not in h


# ------------------------------------------------------------------ cover letter HTML

def test_cover_letter_html():
    cl = T.validate_patch(GOOD_PATCH, RESUME)["cover_letter"]
    h = R.cover_letter_html(RESUME["basics"], {"company": "Globex <Inc>", "title": "Backend & Co"}, cl, "11 October 2026")
    assert "Dear Hiring Manager," in h and "11 October 2026" in h and "Sincerely," in h and h.count("<p>") >= 5
    assert "Globex &lt;Inc&gt;" in h and "Backend &amp; Co" in h and "<Inc>" not in h


# ------------------------------------------------------------------ Gotenberg client

def client_with(handler):
    return R.GotenbergClient("http://gotenberg:3000", httpx.Client(transport=httpx.MockTransport(handler)))


def test_html_to_pdf_posts_the_page_and_returns_the_pdf():
    seen = {}

    def handler(req):
        seen["url"] = str(req.url); seen["ct"] = req.headers["content-type"]; seen["body"] = req.content
        return httpx.Response(200, content=make_pdf([["hello"]]))
    pdf = client_with(handler).html_to_pdf("<html>hi</html>")
    assert pdf.startswith(b"%PDF") and seen["url"].endswith("/forms/chromium/convert/html")
    assert seen["ct"].startswith("multipart/form-data") and b"<html>hi</html>" in seen["body"]
    assert b"preferCssPageSize" in seen["body"]


@pytest.mark.parametrize("response", [httpx.Response(500, text="boom"), httpx.Response(200, content=b"not a pdf")])
def test_bad_responses_raise_a_render_error(response):
    with pytest.raises(R.RenderError):
        client_with(lambda req: response).html_to_pdf("<html/>")


def test_a_network_failure_raises_a_render_error_without_leaking_details():
    def handler(req):
        raise httpx.ConnectError("secret-host-details")
    with pytest.raises(R.RenderError) as e:
        client_with(handler).html_to_pdf("<html/>")
    assert "ConnectError" in str(e.value) and "secret-host" not in str(e.value)


def test_health():
    assert client_with(lambda req: httpx.Response(200)).health() is True
    assert client_with(lambda req: httpx.Response(503)).health() is False


# ------------------------------------------------------------------ PDF verification

def test_pdf_info_reads_pages_and_text():
    info = R.pdf_info(make_pdf([["Jane Doe", "Go and PostgreSQL"], ["page two"]]))
    assert info["pages"] == 2 and "Jane Doe" in info["text"] and "page two" in info["text"]


def test_verify_passes_a_good_pdf():
    v = R.verify_pdf(make_pdf([LONG]), expect=["Jane Doe", "jane@example.com"], max_pages=1)
    assert v["ok"] and v["pages"] == 1 and v["problems"] == []


def test_verify_reports_every_problem():
    v = R.verify_pdf(make_pdf([LONG, LONG]), expect=["Jane Doe", "missing-person@nowhere.com"], max_pages=1)
    assert not v["ok"] and any("2 pages" in p for p in v["problems"]) and any("missing-person" in p for p in v["problems"])
    short = R.verify_pdf(make_pdf([["tiny"]]), expect=[], max_pages=1)
    assert not short["ok"] and "extractable text" in short["problems"][0]


def test_verify_ignores_case_and_spacing():
    v = R.verify_pdf(make_pdf([["Jane   Doe  s resume " + "x " * 120]]), expect=["JANE DOE S RESUME"], max_pages=1)
    assert v["ok"], v["problems"]


def test_typographic_characters_are_normalised_before_comparing():
    # PDF text layers often hold curly quotes, en dashes and ligatures where the source had plain ones
    assert R._norm("Jane\u2019s \u201cresume\u201d 2022\u20132026 \ufb01nal") == "jane's \"resume\" 2022-2026 final"


def test_garbage_is_a_render_error():
    with pytest.raises(R.RenderError, match="unreadable"):
        R.pdf_info(b"%PDF-1.4 this is not a pdf")


# ------------------------------------------------------------------ fit search

class FakeFit:
    """Pretends the layout needs `fits_at` scale or smaller to fit on one page."""

    def __init__(self, fits_at):
        self.fits_at, self.calls = fits_at, []

    def html_to_pdf(self, html):
        scale = float(re.search(r"--s: ([0-9.]+)", html).group(1))
        self.calls.append(scale)
        return make_pdf([LONG] if scale <= self.fits_at else [LONG, LONG])


def fit(client, max_pages=1):
    return R.render_fit(client, lambda s: R.resume_html(view(), s), max_pages=max_pages, expect=["Jane Doe"])


def test_fit_stops_at_full_size_when_it_already_fits():
    c = FakeFit(1.0)
    r = fit(c)
    assert r["scale"] == 1.0 and r["pages"] == 1 and c.calls == [1.0] and r["notes"] == []


@pytest.mark.parametrize("fits_at,expected", [(0.97, 0.96), (0.9, 0.90), (0.881, 0.88), (0.85, 0.84), (0.76, 0.76), (0.995, 0.98)])
def test_fit_finds_the_largest_scale_that_fits(fits_at, expected):
    c = FakeFit(fits_at)
    r = fit(c)
    assert r["scale"] == expected and r["pages"] == 1
    assert f"{round(expected * 100)}%" in r["notes"][0]
    assert len(c.calls) <= 6, f"binary search, not a walk: {c.calls}"


def test_fit_never_renders_the_same_scale_twice():
    c = FakeFit(0.9)
    fit(c)
    assert len(c.calls) == len(set(c.calls))


def test_fit_gives_the_smallest_attempt_with_a_note_when_nothing_fits():
    c = FakeFit(0.1)
    r = fit(c)
    assert r["scale"] == R.SCALES[-1] and r["pages"] == 2 and "does not fit" in r["notes"][-1]
    assert c.calls == [1.0, R.SCALES[-1]], "it gives up after checking the two extremes"


def test_a_two_page_limit_accepts_a_two_page_result():
    c = FakeFit(0.1)
    assert fit(c, max_pages=2)["scale"] == 1.0


# ------------------------------------------------------------------ paper size

def test_paper_size_is_a_setting_and_defaults_to_a4():
    assert "size: A4;" in R.resume_html(view())
    assert "size: Letter;" in R.resume_html(view(), 1.0, "letter") and "size: A4;" in R.resume_html(view(), 1.0, "a4")
    assert "size: A4;" in R.resume_html(view(), 1.0, "tabloid"), "an unknown value falls back instead of failing a render"
    cl = {"greeting": "Dear Hiring Manager,", "paragraphs": ["One.", "Two.", "Three."], "closing": "Regards,"}
    basics = view()["basics"]
    assert "size: Letter;" in R.cover_letter_html(basics, {"company": "C", "title": "T"}, cl, "1 January 2026", 1.0, "letter")
    assert "size: A4;" in R.cover_letter_html(basics, {"company": "C", "title": "T"}, cl, "1 January 2026")
