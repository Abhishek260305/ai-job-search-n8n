"""Rendering: HTML templates -> PDF through Gotenberg (headless Chromium), plus PDF checks.

  resume_html(view, scale)          the tailored (or base) resume as one self-contained HTML page
  cover_letter_html(...)            the cover letter
  GotenbergClient.html_to_pdf()     HTML -> PDF bytes
  verify_pdf()                      page count and ATS-style text-layer check (what a parser would see)
  render_fit()                      render, and shrink the layout step by step until it fits the page limit

All text is HTML-escaped: resume bullets are the user's own, but job titles and company names in the
cover letter come from third-party postings.

The PDF text-layer and page-count checks are adapted from MadsLorentzen/ai-job-search (MIT); see
THIRD_PARTY_NOTICES.md.
"""
from __future__ import annotations

import html
import io
import re
from typing import Callable, Optional

import httpx

SCALES = [1.0, 0.98, 0.96, 0.94, 0.92, 0.90, 0.88, 0.86, 0.84, 0.82, 0.80, 0.78, 0.76]   # largest first
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


class RenderError(RuntimeError):
    pass


def e(s) -> str:
    return html.escape(str(s) if s is not None else "", quote=True)


def fmt_date(d: str) -> str:
    """'2026-01' -> 'Jan 2026', '2022' -> '2022', '' -> ''."""
    if not d:
        return ""
    m = re.fullmatch(r"(\d{4})-(\d{2})", d)
    if m and 1 <= int(m.group(2)) <= 12:
        return f"{MONTHS[int(m.group(2)) - 1]} {m.group(1)}"
    return d


def date_range(start: str, end: str) -> str:
    s, en = fmt_date(start), fmt_date(end)
    if not s and not en:
        return ""
    if s and not end:
        return f"{s} – Present"
    if s == en or not s:
        return en or s
    return f"{s} – {en}"


PAPERS = {"a4": "A4", "letter": "Letter"}


def _paper_css(paper: str) -> str:
    """The CSS page size for a setting value. Anything unknown falls back to A4 rather than failing a render."""
    return PAPERS.get(str(paper).lower(), "A4")


_CSS = """
@page { size: %(paper)s; margin: 13mm 14mm 13mm 14mm; }
:root { --s: %(scale)s; }
* { box-sizing: border-box; }
html { -webkit-print-color-adjust: exact; }
body { font-family: "Helvetica Neue", Helvetica, Arial, "Liberation Sans", sans-serif; color: #111;
       font-size: calc(10pt * var(--s)); line-height: 1.32; margin: 0; }
h1 { font-size: calc(22pt * var(--s)); margin: 0; letter-spacing: .2px; }
.headline { font-size: calc(11pt * var(--s)); color: #333; margin-top: 1px; }
.contact { font-size: calc(9pt * var(--s)); color: #333; margin-top: 3px; }
.contact a { color: #1a4f8b; text-decoration: none; }
h2 { font-size: calc(10pt * var(--s)); text-transform: uppercase; letter-spacing: .8px; border-bottom: 1px solid #999;
     margin: calc(9pt * var(--s)) 0 calc(4pt * var(--s)); padding-bottom: 1px; }
.summary { margin: calc(6pt * var(--s)) 0 0; }
.entry { margin-bottom: calc(5pt * var(--s)); break-inside: avoid-page; }
.row { display: flex; justify-content: space-between; gap: 10px; }
.title { font-weight: 700; }
.when { white-space: nowrap; color: #333; }
.sub { color: #333; font-style: italic; }
.group { font-weight: 600; font-size: calc(9.4pt * var(--s)); margin: calc(3pt * var(--s)) 0 0; color: #222; }
ul { margin: calc(1.5pt * var(--s)) 0 0; padding-left: 15px; }
li { margin: 0 0 calc(1.2pt * var(--s)); }
.skills div { margin: 0 0 calc(1.5pt * var(--s)); }
.skills b.k { font-weight: 700; }
.cl p { margin: 0 0 calc(8pt * var(--s)); text-align: left; }
.cl .date { margin: calc(14pt * var(--s)) 0 calc(10pt * var(--s)); }
"""


def _contact_line(basics: dict) -> str:
    loc = basics.get("location") or {}
    parts = [e(basics.get("email")), e(basics.get("phone")),
             e(", ".join(x for x in (loc.get("city"), loc.get("countryCode")) if x))]
    for p in basics.get("profiles", []) or []:
        url = p.get("url")
        if url and re.match(r"^https?://", url):
            parts.append(f'<a href="{e(url)}">{e(p.get("network") or url)}</a>')
    return " &middot; ".join(x for x in parts if x)


def _bullets(groups: list[dict]) -> str:
    out = []
    for g in groups:
        if g.get("title"):
            out.append(f'<div class="group">{e(g["title"])}</div>')
        out.append("<ul>" + "".join(f"<li>{e(b)}</li>" for b in g["bullets"]) + "</ul>")
    return "".join(out)


def resume_html(view: dict, scale: float = 1.0, paper: str = "a4") -> str:
    b = view.get("basics", {})
    body = [f"<h1>{e(b.get('name'))}</h1>"]
    if view.get("headline"):
        body.append(f'<div class="headline">{e(view["headline"])}</div>')
    body.append(f'<div class="contact">{_contact_line(b)}</div>')
    if view.get("summary"):
        body.append(f'<p class="summary">{e(view["summary"])}</p>')

    if view.get("work"):
        body.append("<h2>Work Experience</h2>")
        for w in view["work"]:
            company = e(w["name"]) + (f" ({e(w['description'])})" if w.get("description") else "")
            body.append('<div class="entry">'
                        f'<div class="row"><span class="title">{e(w["position"])}</span><span class="when">{e(date_range(w["start"], w["end"]))}</span></div>'
                        f'<div class="row"><span class="sub">{company}</span><span class="sub">{e(w.get("location"))}</span></div>'
                        f'{_bullets(w["groups"])}</div>')
    if view.get("projects"):
        body.append("<h2>Projects</h2>")
        for p in view["projects"]:
            link = f' <a href="{e(p["url"])}">link</a>' if p.get("url") and re.match(r"^https?://", p["url"]) else ""
            body.append('<div class="entry">'
                        f'<div class="row"><span class="title">{e(p["name"])}{link}</span><span class="when">{e(date_range(p["start"], p["end"]))}</span></div>'
                        f'{_bullets(p["groups"])}</div>')
    if view.get("education"):
        body.append("<h2>Education</h2>")
        for ed in view["education"]:
            degree = ", ".join(x for x in (ed.get("studyType"), ed.get("area")) if x)
            extra = f" &middot; {e(ed['score'])}" if ed.get("score") else ""
            body.append('<div class="entry">'
                        f'<div class="row"><span class="title">{e(ed.get("institution"))}</span>'
                        f'<span class="when">{e(date_range(ed.get("startDate", ""), ed.get("endDate", "")))}</span></div>'
                        f'<div class="sub">{e(degree)}{extra}</div></div>')
    if view.get("skills"):
        body.append('<h2>Technical Skills</h2><div class="skills">')
        for s in view["skills"]:
            kws = []
            emph = {k.lower() for k in s.get("emphasize") or []}
            for k in s["keywords"]:
                kws.append(f'<b class="k">{e(k)}</b>' if k.lower() in emph else e(k))
            body.append(f"<div><b>{e(s['name'])}:</b> {', '.join(kws)}</div>")
        body.append("</div>")
    if view.get("additional"):
        body.append("<h2>Additional</h2><ul>" + "".join(f"<li>{e(a)}</li>" for a in view["additional"]) + "</ul>")
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<title>{e(b.get("name"))} - Resume</title><style>{_CSS % {"scale": scale, "paper": _paper_css(paper)}}</style></head>'
            f'<body>{"".join(body)}</body></html>')


def cover_letter_html(basics: dict, job: dict, cover: dict, today: str, scale: float = 1.0, paper: str = "a4") -> str:
    body = [f"<h1>{e(basics.get('name'))}</h1>", f'<div class="contact">{_contact_line(basics)}</div>',
            '<div class="cl">', f'<div class="date">{e(today)}</div>']
    if job.get("company"):
        body.append(f"<p>{e(job['company'])}<br>{e(job.get('title'))}</p>")
    body.append(f"<p>{e(cover['greeting'])}</p>")
    body += [f"<p>{e(p)}</p>" for p in cover["paragraphs"]]
    body.append(f"<p>{e(cover['closing'])}<br>{e(basics.get('name'))}</p></div>")
    css = _CSS % {"scale": scale, "paper": _paper_css(paper)}
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<title>{e(basics.get("name"))} - Cover letter</title><style>{css} body {{ font-size: calc(10.6pt * var(--s)); line-height: 1.42; }}</style></head>'
            f'<body>{"".join(body)}</body></html>')


# ---------------------------------------------------------------- Gotenberg

class GotenbergClient:
    def __init__(self, base_url: str, client: Optional[httpx.Client] = None, timeout: float = 90.0):
        self.base_url = base_url.rstrip("/")
        self.client = client or httpx.Client(timeout=timeout)

    def html_to_pdf(self, html_doc: str) -> bytes:
        try:
            r = self.client.post(f"{self.base_url}/forms/chromium/convert/html",
                                 files={"files": ("index.html", html_doc.encode("utf-8"), "text/html")},
                                 data={"preferCssPageSize": "true", "printBackground": "true", "emulatedMediaType": "print"})
        except httpx.HTTPError as ex:
            raise RenderError(f"could not reach the PDF service: {type(ex).__name__}")
        if r.status_code != 200 or not r.content.startswith(b"%PDF"):
            raise RenderError(f"PDF service returned HTTP {r.status_code}: {r.text[:160]!r}")
        return r.content

    def health(self) -> bool:
        try:
            return self.client.get(f"{self.base_url}/health", timeout=5).status_code == 200
        except httpx.HTTPError:
            return False


# ---------------------------------------------------------------- PDF checks

def pdf_info(pdf: bytes) -> dict:
    """Page count and extracted text (the text layer an ATS parser reads)."""
    from pypdf import PdfReader
    try:
        reader = PdfReader(io.BytesIO(pdf))
        text = "\n".join((p.extract_text() or "") for p in reader.pages)
        return {"pages": len(reader.pages), "text": text}
    except Exception as ex:
        raise RenderError(f"unreadable PDF: {type(ex).__name__}")


def _norm(s: str) -> str:
    s = s.translate({0x2018: "'", 0x2019: "'", 0x201C: '"', 0x201D: '"', 0x2013: "-", 0x2014: "-", 0xA0: " ", 0xFB01: "fi", 0xFB02: "fl"})
    return re.sub(r"\s+", " ", s).strip().lower()


def verify_pdf(pdf: bytes, *, expect: list[str], max_pages: int, min_text_chars: int = 200) -> dict:
    """Check a rendered PDF the way a parser would see it. Returns {"ok", "pages", "problems"}."""
    info = pdf_info(pdf)
    problems: list[str] = []
    text = _norm(info["text"])
    if info["pages"] > max_pages:
        problems.append(f"{info['pages']} pages (limit {max_pages})")
    if len(text) < min_text_chars:
        problems.append(f"only {len(text)} characters of extractable text (the PDF may be an image)")
    flat = text.replace(" ", "")
    for want in expect:
        if want and _norm(want).replace(" ", "") not in flat:
            problems.append(f"missing from the text layer: {want[:60]!r}")
    return {"ok": not problems, "pages": info["pages"], "problems": problems, "chars": len(text)}


# ---------------------------------------------------------------- fit

def render_fit(client, make_html: Callable[[float], str], *, max_pages: int, expect: list[str]) -> dict:
    """Render at the LARGEST scale that fits `max_pages`.

    Tries full size, then the smallest size, then binary-searches the steps in between (page count only
    grows as the scale grows), so it takes about 4 renders instead of walking every step.

    Returns {"pdf", "scale", "pages", "verify", "notes"}. If nothing fits, the smallest scale is
    returned with a note, so the caller can flag it instead of failing.
    """
    notes: list[str] = []
    cache: dict[int, dict] = {}

    def attempt(i: int) -> dict:
        if i not in cache:
            pdf = client.html_to_pdf(make_html(SCALES[i]))
            v = verify_pdf(pdf, expect=expect, max_pages=max_pages)
            cache[i] = {"pdf": pdf, "scale": SCALES[i], "pages": v["pages"], "verify": v, "notes": notes}
        return cache[i]

    if attempt(0)["pages"] <= max_pages:
        return cache[0]
    last = len(SCALES) - 1
    if attempt(last)["pages"] > max_pages:
        notes.append(f"does not fit {max_pages} page(s) even at {round(SCALES[last] * 100)}% ({cache[last]['pages']} pages)")
        return cache[last]
    lo, hi = 0, last            # lo does not fit, hi fits
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if attempt(mid)["pages"] <= max_pages:
            hi = mid
        else:
            lo = mid
    best = cache[hi]
    notes.append(f"shrunk the layout to {round(best['scale'] * 100)}% to fit {max_pages} page(s)")
    return best
