"""The text a person sees in an HTML email, and whether it can be trusted to be that text (Phase 6; docs/gmail.md).

A reader of mail (here: the rules of step 6c and the model of step 6d) must see what a person sees. A sender can put text in an
email that only a program sees: "we are pleased to offer you the position" inside a display:none block. So the content of an
element that the markup itself hides is dropped.

Matching a browser's error recovery exactly is not possible, so the design is to FAIL CLOSED: where this reader cannot tell what a
browser would show, it reports `doubt`, and the callers then treat the email as unreadable-for-a-model (it is never sent to the
model; the rules path flags it and caps its confidence). Doubt is raised when:
  * a hiding element is closed while other elements are still open inside it, or a hidden element is closed implicitly by an outer
    end tag (a browser may re-open such elements around later text, or keep them open), or its end tag is one a browser would ignore;
  * a hiding element is never closed, or <body>/<html> itself hides;
  * the markup uses constructs a browser reads differently from Python's parser: a self-closing raw-text element (`<script/>`), CDATA,
    <svg>, <math>, <frameset>, <iframe>, <noembed>, <noframes>, <xmp>, <textarea>, <plaintext>, a table that closes a hidden paragraph;
  * a <style> block hides things by a selector this reader cannot evaluate (anything but `.class`, `#id` or `tag`);
  * the markup nests deeper than MAX_DEPTH or costs more than MAX_WORK steps to read (hostile, or just broken);
  * an unterminated comment or tag ends the markup (a browser hides the rest; text after it is cut here);
  * the parser itself gives up.
Hiding that a style block applies by `.class`, `#id` or tag name, and every inline-style trick listed in `style_hides`, is applied.
Text hidden by colour alone (white on white, a colour the background happens to match) cannot be told from visible text; the rest of
the pipeline never trusts the text.

Dropping is the safe direction for a reader: a legitimate preheader (display:none) loses nothing, and visible text that is wrongly
dropped (a font-size:0 wrapper around a child with its own size, <noscript> content) only means less is read.

Pure functions, no I/O. Never raises.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Optional

import boards

MAX_CHARS = boards.MAX_HTML_CHARS      # of markup read per email (the stdlib parser is quadratic on hostile markup)
MAX_DEPTH = 400                        # open elements; a real email nests a table or two inside a few dozen others
MAX_WORK = 2_000_000                   # stack entries looked at in all; ordinary mail needs a few thousand
MAX_STYLE_CHARS = 100_000              # of <style> blocks read for hiding rules

_VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"})
_HIDING_TAGS = frozenset({"script", "style", "head", "title", "template", "noscript", "object", "iframe", "svg", "math", "rp",
                          "noembed", "noframes", "datalist", "select"})
# elements whose content a browser reads as raw text (or foreign content), which Python's parser reads as markup: never trusted
_DOUBTFUL_TAGS = frozenset({"svg", "math", "frameset", "iframe", "noembed", "noframes", "xmp", "textarea", "plaintext"})
_RAW_SELF_CLOSING = frozenset({"script", "style", "title", "iframe", "noembed", "noframes", "xmp", "textarea", "noscript", "plaintext"})
# the HTML standard's "special" elements: what an end tag of an inline element cannot close across
_SPECIAL = frozenset("""address applet area article aside base basefont bgsound blockquote body br button caption center col colgroup dd
details dir div dl dt embed fieldset figcaption figure footer form frame frameset h1 h2 h3 h4 h5 h6 head header hgroup hr html iframe
img input li link listing main marquee menu meta nav noembed noframes noscript object ol p param plaintext pre script section select
source style summary table tbody td template textarea tfoot th thead title tr track ul wbr xmp""".split())
_SCOPE = frozenset({"applet", "caption", "html", "table", "td", "th", "marquee", "object", "template"})   # what a block end tag cannot close across
_TABLE_SCOPE = frozenset({"html", "table", "template"})                                                   # ... and what a table-part end tag cannot
_TABLE_PARTS = frozenset({"table", "tbody", "tfoot", "thead", "tr", "td", "th", "caption", "colgroup"})
_TABLE_STRUCTURE = frozenset({"table", "tbody", "tfoot", "thead", "tr"})                                  # what may only hold table parts
_TABLE_CHILDREN = frozenset({"caption", "colgroup", "col", "tbody", "thead", "tfoot", "tr", "td", "th", "style", "script", "template"})
_CLOSES_P = frozenset("""address article aside blockquote center details dir div dl fieldset figcaption figure footer form h1 h2 h3 h4
h5 h6 header hgroup hr main menu nav ol p pre section ul li dd dt""".split())
_IMPLIED = frozenset({"p", "li", "dd", "dt", "tr", "td", "th", "option"})      # a start tag that closes an open one of its own kind
_HEAD_OK = frozenset({"title", "meta", "link", "style", "script", "base", "noscript", "template"})

_NUM = re.compile(r"^([+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?)([a-z%]*)$")
_TRANSPARENT = re.compile(r"transparent|(?:rgba|hsla)\([^)]*,\s*0*(?:\.0*)?%?\s*\)|(?:rgba?|hsla?)\([^)]*/\s*0*(?:\.0*)?%?\s*\)|#[0-9a-f]{3}0$|#[0-9a-f]{6}00$")
_CLIP_RECT = re.compile(r"rect\(\s*(?:[01](?:px)?\s*,?\s*){4}\)")
_SCALE_ZERO = re.compile(r"scale(?:[xyz3d]*)\(\s*[+-]?0*\.?0+(?:px)?\s*[,)]")
_FAR_TRANSLATE = re.compile(r"translate(?:[xy3d]*)\(\s*-\d{4,}")
_RELEVANT = ("display", "visibility", "opacity", "font-size", "font", "color", "-webkit-text-fill-color")


def _strip_css_comments(value: str) -> str:
    """Remove /* ... */ in one linear pass. An unterminated comment runs to the end of the value, as in a browser."""
    out, i = [], 0
    while True:
        j = value.find("/*", i)
        if j < 0:
            out.append(value[i:])
            break
        out.append(value[i:j])
        k = value.find("*/", j + 2)
        if k < 0:
            break
        i = k + 2
    return "".join(out)


def _num(value: str):
    m = _NUM.match(value.strip())
    if not m:
        return None
    try:
        return float(m.group(1)), m.group(2)
    except ValueError:
        return None


def _tiny(value: str, *, font: bool = False) -> bool:
    """Is a length too small to show anything? Zero in any unit, up to 1.5px for a font (1px for a box), a tenth of an em."""
    n = _num(value)
    if n is None:
        return False
    number, unit = n
    if number == 0:
        return True
    if number < 0:
        return False
    if unit in ("px", "pt"):
        return number <= (1.5 if font else 1.0)
    if unit == "%":
        return number <= (10.0 if font else 1.0)
    if unit:
        return number <= 0.1
    return number <= 0.01


def _far(value: str) -> bool:
    n = _num(value)
    if n is None:
        return False
    number, unit = n
    return number <= (-300 if unit in ("px", "pt", "") else -50)


def style_hides(style: str) -> bool:
    """Does an inline style (or a style rule's body) hide the element's content? Declaration by declaration (never a search of the
    whole string), so `line-height:0` or `background:url(x?display:none)` hide nothing; a property that is declared more than once
    hides if ANY declaration hides (a later invalid value does not override a valid one). A backslash where a CSS escape could spell a
    property or keyword, and `var()`, `calc()` or `env()` where the value decides visibility, count as hiding: fail closed."""
    decls = []
    for decl in _strip_css_comments(style).split(";"):
        prop, sep, value = decl.partition(":")
        if not sep:
            continue
        prop = prop.strip().lower()
        value = re.sub(r"\s*!\s*important\s*$", "", value.strip().lower())
        if "\\" in prop:
            return True
        if prop in _RELEVANT and ("\\" in value or "var(" in value or "calc(" in value or "env(" in value):
            return True
        decls.append((prop, value))

    def values(*props):
        return [v for p, v in decls if p in props]

    if any(v == "none" for v in values("display")) or any(v in ("hidden", "collapse") for v in values("visibility")):
        return True
    if any(v == "all" for v in values("mso-hide")) or any(_tiny(v) for v in values("zoom")):
        return True
    if any(_tiny(v, font=True) for v in values("font-size")) or any(n is not None and n[0] <= 0.05 for n in (_num(v) for v in values("opacity"))):
        return True
    if any(_TRANSPARENT.search(v) for v in values("color", "-webkit-text-fill-color")):
        return True
    for shorthand in values("font"):                           # `font: 0px Arial`, `font:0/0 a`
        for token in re.split(r"[\s/]+", shorthand):
            if token[:1].isdigit() or token[:1] == ".":
                if _tiny(token, font=True):
                    return True
                break
    if any(v == "hidden" for v in values("content-visibility")) or any("opacity(0" in v for v in values("filter")):
        return True
    if any(v in ("hidden", "clip") for v in values("overflow", "overflow-x", "overflow-y")) and any(
            _tiny(v) for v in values("height", "max-height", "width", "max-width", "line-height")):
        return True
    if any(_far(v) for v in values("text-indent", "left", "right", "top", "bottom", "margin-left", "margin-top")):
        return True
    clips = values("clip") + values("clip-path")
    if any(_CLIP_RECT.search(v) or v.startswith(("inset(50%", "inset(100%", "circle(0")) for v in clips):
        return True
    return any(_SCALE_ZERO.search(v) or _FAR_TRANSLATE.search(v) for v in values("transform", "-webkit-transform"))


# ---- what the <style> blocks hide ---------------------------------------------------------------------------------

_SIMPLE = re.compile(r"^(?:[a-z][a-z0-9]*)?(?:\.[\w-]+|#[\w-]+)?$")


def style_block_hiding(markup: str) -> tuple:
    """-> (classes, ids, tags, doubt): the names a <style> block hides, and whether it hides by a selector this reader cannot
    evaluate. Every rule is applied, @media rules included (a hidden-on-mobile block is hidden here: safe for a reader)."""
    classes, ids, tags, doubt = set(), set(), set(), False
    low = markup.lower()
    read, pos = 0, 0
    while read < MAX_STYLE_CHARS:
        start = low.find("<style", pos)
        if start < 0:
            break
        open_end = low.find(">", start)
        if open_end < 0:
            break
        end = low.find("</style", open_end)
        css = low[open_end + 1:end if end >= 0 else len(low)]
        pos = end if end >= 0 else len(low)
        read += len(css) + 1
        css = _strip_css_comments(css[:MAX_STYLE_CHARS])
        prelude_from = 0
        stack = []
        for i, ch in enumerate(css):
            if ch == "{":
                stack.append((css[prelude_from:i].strip(), i + 1))
                prelude_from = i + 1
            elif ch == "}" and stack:
                selector, body_from = stack.pop()
                body = css[body_from:i]
                prelude_from = i + 1
                if "{" in body or selector.startswith("@"):
                    continue                                      # a container (@media ...) or a rule holding rules: its inner rules were read
                if not style_hides(body):
                    continue
                for part in selector.split(","):
                    part = part.strip()
                    if not part:
                        continue
                    if not _SIMPLE.match(part):
                        doubt = True
                    elif part.startswith("."):
                        classes.add(part[1:])
                    elif part.startswith("#"):
                        ids.add(part[1:])
                    elif "." in part:
                        classes.add(part.split(".", 1)[1])
                    elif "#" in part:
                        ids.add(part.split("#", 1)[1])
                    else:
                        tags.add(part)
    return classes, ids, tags, doubt


class _Reader(HTMLParser):
    def __init__(self, classes=(), ids=(), tags=()) -> None:
        super().__init__(convert_charrefs=True)
        self.classes, self.ids, self.tags = set(classes), set(ids), set(tags)
        self.parts: list = []
        self.stack: list = []          # (tag, hides)
        self.hidden = 0                # open elements that hide their content
        self.work = 0
        self.doubt = False
        self.stopped = False           # nothing more is read (depth or work limit)

    # -- helpers
    def _fail_closed(self) -> None:
        self.doubt = self.stopped = True

    def _hides(self, tag: str, attrs) -> bool:
        names = {}
        for name, value in attrs:
            names.setdefault((name or "").lower(), (value or "").lower())      # the first of a repeated attribute counts, as in a browser
        if tag in _HIDING_TAGS or tag in self.tags or "hidden" in names or names.get("aria-hidden") in ("true", ""):
            return True
        if tag in ("details", "dialog") and "open" not in names:
            return True
        if "popover" in names or (names.get("id") or "\0") in self.ids or any(c in self.classes for c in (names.get("class") or "").split()):
            return True
        return "style" in names and style_hides(names["style"])

    def _pop_to(self, index: int) -> None:
        closed = self.stack[index:]
        if len(closed) > 1 and any(h for _t, h in closed):
            self.doubt = True                                   # a hidden element closed with others open inside it, or closed implicitly
        for _tag, hides in closed:
            self.hidden -= bool(hides)
        del self.stack[index:]

    def _implicit_close(self, tag: str) -> None:
        if tag not in _HEAD_OK and any(t == "head" for t, _h in self.stack):
            for i in range(len(self.stack) - 1, -1, -1):        # <body> or any body content ends an unclosed <head>
                if self.stack[i][0] == "head":
                    self._pop_to(i)
                    break
        wanted = tag if tag in _IMPLIED else ("p" if tag in _CLOSES_P else None)
        if tag == "table" and self.hidden:
            for t, h in self.stack:
                if t == "p" and h:
                    self.doubt = True                           # a table closes a paragraph only outside quirks mode, which mail is usually in
        if wanted is None:
            return
        for i in range(len(self.stack) - 1, -1, -1):
            self.work += 1
            name = self.stack[i][0]
            if name == wanted:
                self._pop_to(i)
                return
            if name in _SPECIAL:
                return

    # -- parser callbacks
    def handle_starttag(self, tag, attrs):
        if self.stopped:
            return
        if tag in _VOID:
            if tag == "br" and not self.hidden:
                self.parts.append("\n")
            return
        if tag in _DOUBTFUL_TAGS:
            self.doubt = True
        self._implicit_close(tag)
        if len(self.stack) >= MAX_DEPTH or self.work > MAX_WORK:
            return self._fail_closed()
        hides = self._hides(tag, attrs)
        if (hides or self.hidden) and self.stack and self.stack[-1][0] in _TABLE_STRUCTURE and tag not in _TABLE_CHILDREN:
            self.doubt = True                                   # a browser moves this element out of the table (foster parenting)
        if hides and tag in ("body", "html"):
            self.doubt = True                                   # a later <body>/<html> merges its attributes onto the real one
        self.stack.append((tag, hides))
        self.hidden += hides
        if not hides and tag in boards._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_startendtag(self, tag, attrs):
        # `<div/>` is an open div to a browser; only the void elements are really self-closing. A raw-text element
        # (`<script/>`) starts raw text in a browser but not in Python's parser: doubt.
        if tag in _RAW_SELF_CLOSING:
            self.doubt = True
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if self.stopped:
            return
        if tag == "br":
            if not self.hidden:
                self.parts.append("\n")
            return
        if tag in _VOID or tag in ("body", "html"):             # a browser keeps <body> open: text after </body> is still in it
            return
        i = len(self.stack) - 1
        while i >= 0 and self.stack[i][0] != tag:
            i -= 1
            self.work += 1
        if self.work > MAX_WORK:
            return self._fail_closed()
        if i < 0:
            return                                                # an end tag with nothing to close: ignored, as in a browser
        between = [t for t, _h in self.stack[i + 1:]]
        self.work += len(between)
        if tag in _TABLE_PARTS:
            blocked = any(t in _TABLE_SCOPE for t in between)
        elif tag in _SPECIAL:
            boundary = _SCOPE | ({"ol", "ul"} if tag == "li" else {"button"} if tag == "p" else set())
            blocked = any(t in boundary for t in between)
        else:
            blocked = any(t in _SPECIAL for t in between)
        if blocked:
            if self.stack[i][1]:
                self.doubt = True                                 # a browser ignores this end tag: the element stays hidden; so do we
            return
        self._pop_to(i)
        if not self.hidden and tag in boards._BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden and not self.stopped:
            self.parts.append(data)


def visible_text(markup: Optional[str]) -> tuple:
    """-> (text, doubt). The text a person would see: tags stripped, hidden elements dropped, block tags as line breaks, entities
    decoded, whitespace collapsed, zero-width characters removed. `doubt` says this reader could not follow the markup exactly."""
    text = str(markup or "")[:MAX_CHARS].replace("\x00", "")
    doubt = False
    cut = text.rfind("<!--")
    if cut >= 0 and "-->" not in text[cut + 4:]:                  # an unterminated comment: a browser hides the rest
        text, doubt = text[:cut], True
    tail = text.rfind("<")
    if tail >= 0 and ">" not in text[tail:]:                      # an unterminated tag at the end
        text, doubt = text[:tail], True
    if "<![cdata[" in text.lower():                               # Python reads to `]]>`; a browser ends the bogus comment at the first `>`
        doubt = True
    classes, ids, tags, style_doubt = style_block_hiding(text)
    reader = _Reader(classes, ids, tags)
    try:
        reader.feed(text)
        reader.close()
    except Exception:
        reader.doubt = True
    if reader.hidden:                                             # a hiding element that was never closed
        reader.doubt = True
    out = "".join(reader.parts)
    lines = (boards._SPACES.sub(" ", line).strip() for line in boards._ZERO_WIDTH.sub("", out).split("\n"))
    return "\n".join(line for line in lines if line).replace("\x00", ""), doubt or style_doubt or reader.doubt


_WORD = re.compile(r"[a-z]{4,}")


def parts_differ(plain: str, html_text: str) -> bool:
    """Does the plain-text part of an email say things the HTML part (what most mail clients show) does not? A sender could put
    one message in each. True when the plain part has more than a few words of its own."""
    plain_words = set(_WORD.findall((plain or "").lower()))
    html_words = set(_WORD.findall((html_text or "").lower()))
    missing = plain_words - html_words
    return len(missing) > max(5, int(0.2 * len(plain_words)))
