"""The text a person sees in an HTML email, and whether it can be trusted to be that text (email_html.py; Phase 6).

The corpus below is hostile and sloppy markup: what a sender could use to put text in an email that only a program reads, and what
real mail does that must not lose its visible words. Fail closed: where a browser's error recovery cannot be followed, `doubt`."""
import time

import pytest

import email_html as E
import gmail_client as G
import stubs

# ---- must never appear in the text (a browser hides it, or the markup could not be followed)
HIDDEN = {
    "display none": "<p>Visible.</p><div style='display:none'>SECRET</div><p>After.</p>",
    "spaced, upper-case, important": "<p>Visible.</p><span style='color:red; DISPLAY : NONE !important'>SECRET</span>",
    "UPPER tags": "<DIV STYLE=\"DISPLAY:NONE\">SECRET</DIV>Visible.",
    "hidden attribute": "<p>Visible.</p><div hidden>SECRET</div>",
    "aria-hidden": "<p>Visible.</p><div aria-hidden='true'>SECRET</div>",
    "zero font": "<p>Visible.</p><span style='font-size:0px'>SECRET</span>",
    "zero font, other spellings": "<span style='font-size:0.0px'>SECRET</span><span style='font-size:00'>SECRET</span><span style='font-size:0%'>SECRET</span>",
    "zero opacity": "<span style='opacity:0'>SECRET</span><span style='opacity:0.0'>SECRET</span><span style='opacity:.0'>SECRET</span>",
    "visibility, nested": "<div style='visibility:hidden'><p>SECRET</p><div>SECRET</div></div><p>Visible.</p>",
    "unbalanced closing tags": "<div style='display:none'><b>SECRET</b></i></p>SECRET</div>Visible.",
    "mismatched inline end tag": "<span style='display:none'><div></span>SECRET",
    "block end tag across a table cell": "<div style='display:none'><table><tr><td></div>SECRET</td></tr></table>",
    "never closed": "<p>Visible.</p><div style='display:none'>SECRET forever",
    "head and title": "<html><head><title>SECRET</title></head><body>Visible.</body></html>",
    "line break inside": "<div style='display:none'>SECRET<br>SECRET</div>Visible.",
    "mso-hide": "<p>Visible.</p><div style='mso-hide:all'>SECRET</div>",
    "pushed off the page": "<p>Visible.</p><div style='position:absolute;left:-9999px'>SECRET</div><div style='text-indent:-9999px'>SECRET</div>",
    "comment inside the style": "<p>Visible.</p><span style='dis/**/play:none'>SECRET</span>",
    "multi-line comment inside the style": "<span style='/* a\nb */display:none'>SECRET</span>",
    "template": "<p>Visible.</p><template>SECRET</template>",
    "self-closing non-void tags are open tags": "<p>ok</p><div style='display:none'/>SECRET</div><template/>SECRET</template><script src=x />SECRET</script>",
    "a < in an attribute": "<div title=\"<\" style=\"display:none\">SECRET</div>Visible.",
    "unterminated comment": "Visible<!-- SECRET",
    "unterminated comment that holds a >": "Visible<!-- SECRET > SECRET",
    "unterminated tag": "Visible<div style='display:none'",
    "details and dialog": "<details><summary>x</summary>SECRET</details><dialog>SECRET</dialog>Visible.",
    "css escapes": "<span style='display:\\6eone'>SECRET</span><span style='d\\isplay:none'>SECRET</span>",
    "entities in the style": "<span style='display&#58;none'>SECRET</span><span style='display&colon;none'>SECRET</span>",
    "slash instead of a space": "<span/style='display:none'>SECRET</span>Visible.",
    "rp, noembed": "<ruby>a<rp>SECRET</rp></ruby><noembed>SECRET</noembed>Visible.",
    "overflow-hidden box of no height": "<div style='height:0;overflow:hidden'>SECRET</div><div style='max-height:0px;overflow:hidden'>SECRET</div>Visible.",
    "scale zero, clip": "<div style='transform:scale(0)'>SECRET</div><div style='clip:rect(0,0,0,0)'>SECRET</div>Visible.",
    "transparent": "<span style='color:transparent'>SECRET</span>Visible.",
    "javascript-style script": "<script>var x='SECRET';</script>Visible.",
    "css in style tags": "<style>.a{display:none} SECRET</style>Visible.",
    # ---- found by a differential run against a real HTML5 parser (parse5) and by review
    "hidden formatting element popped by an outer end tag": "<div><font style=display:none>SECRET</div>SECRET",
    "hidden inline popped by a paragraph end": "<p><a style='display:none'>SECRET</p>SECRET",
    "form end tag leaves its children": "<form hidden><b></form>SECRET",
    "li end tag cannot cross a list": "<li hidden><ol>SECRET</li>SECRET",
    "p end tag cannot cross a button": "<p hidden><button>SECRET</p>SECRET",
    "unclosed noscript": "<div><noscript>SECRET</div>SECRET",
    "unclosed select": "<div><select hidden>SECRET</div>SECRET",
    "head ends a template": "<head><template>SECRET<div>SECRET</div>",
    "body that hides, later": "Visible <div><body hidden>SECRET</div>SECRET",
    "text after </body>": "<body hidden>SECRET</body>SECRET",
    "html that hides": "<html style=display:none>SECRET</html>SECRET",
    "self-closing script in a table": "<table><tr><td><script/>SECRET</td></tr></table>SECRET",
    "self-closing style": "<div><style/>SECRET</div>SECRET",
    "self-closing iframe": "<div><iframe/>SECRET</div>SECRET",
    "table closes a hidden p": "<p hidden><table><tr><td>SECRET</td></tr></table>SECRET",
    "cdata": "<![CDATA[ > <div hidden> ]]> SECRET </div>SECRET",
    "svg title": "<svg><title><div hidden></title></svg>SECRET",
    "foster-parented hidden paragraph": "<table><p hidden='until-found'><form>SECRET",
    "popover": "<div popover>SECRET</div>",
    "later invalid display does not undo": "<div style='display:none;display:bogus'>SECRET</div>",
    "later invalid visibility": "<div style='visibility:hidden;visibility:bogus'>SECRET</div>",
    "var() decides": "<div style='display:var(--h)'>SECRET</div><div style='font-size:calc(0px)'>SECRET</div>",
    "tiny fonts": "<span style='font-size:0.5px'>SECRET</span><span style='font-size:.1em'>SECRET</span><span style='font-size:0.0001px'>SECRET</span><span style='font-size:1px'>SECRET</span>",
    "transparent colours": ("<span style='color:rgba(0,0,0,0)'>SECRET</span><span style='color:#fff0'>SECRET</span><span style='color:#ffffff00'>SECRET</span>"
                            "<span style='color:hsla(0,0%,100%,0)'>SECRET</span><span style='-webkit-text-fill-color:transparent'>SECRET</span>"),
    "boxes of no size that clip": ("<div style='height:0;overflow:clip'>SECRET</div><div style='overflow-y:hidden;max-height:0%'>SECRET</div>"
                                   "<div style='height:1px;overflow:hidden'>SECRET</div><div style='line-height:0;overflow:hidden'>SECRET</div>"),
    "far off the page, other units": ("<div style='left:-500px'>SECRET</div><div style='left:-100vw'>SECRET</div><div style='right:-9999px'>SECRET</div>"
                                      "<div style='text-indent:-999em'>SECRET</div><div style='text-indent:-100%'>SECRET</div><div style='bottom:-9999px'>SECRET</div>"),
    "screen-reader-only recipes": "<div style='clip:rect(1px,1px,1px,1px)'>SECRET</div><div style='clip-path:circle(0)'>SECRET</div>",
    "scales and zoom": ("<div style='transform:scale(0,0)'>SECRET</div><div style='transform:scaleX(0)'>SECRET</div><div style='-webkit-transform:scale(0)'>SECRET</div>"
                        "<div style='zoom:0'>SECRET</div><div style='content-visibility:hidden'>SECRET</div><div style='filter:opacity(0)'>SECRET</div>"),
    "font shorthand with no size": "<span style='font:0px Arial'>SECRET</span><span style='font:0 sans-serif'>SECRET</span><span style='font:italic 0/0 a'>SECRET</span>",
    "a class hidden by a style block": "<style>.pre{display:none !important}</style><div class=pre>SECRET</div>Visible.",
    "an id hidden by a style block": "<style>#x{visibility:hidden}</style><div id=x>SECRET</div>Visible.",
    "a tag hidden by a style block": "<style>b{font-size:0}</style><b>SECRET</b>Visible.",
    "hidden inside an @media block": "<style>@media screen and (max-width:600px){.m{display:none}}</style><div class=m>SECRET</div>Visible.",
    "tag.class selector": "<style>div.x{opacity:0}</style><div class=x>SECRET</div>Visible.",
    "class names are case-insensitive in quirks mode": "<style>.Pre{display:none}</style><div class=pre>SECRET</div>Visible.",
    "a rule with a list of selectors": "<style>.a,.b,.c{display:none}</style><div class=b>SECRET</div>Visible.",
}

# ---- must keep their visible words
VISIBLE = {
    "line-height 0": "<div style='line-height:0'>KEEP</div>",
    "zero border width": "<td style='border-bottom-width:0'>KEEP</td>",
    "min-height and min-width 0": "<td style='min-height:0'>KEEP</td><td style='min-width:0'>KEEP</td>",
    "background url that says display:none": "<div style='background:url(x?display:none)'>KEEP</div>",
    "small and faint text": "<span style='font-size:2px'>KEEP</span><span style='opacity:.5'>KEEP</span><span style='font-size:0.9em'>KEEP</span>",
    "a box of no height without overflow:hidden": "<div style='height:0'>KEEP</div>",
    "an unterminated comment in a style": "<span style='/* display:none'>KEEP</span>",
    "multi-line comment that holds display:none": "<span style='/* display:none\n */'>KEEP</span>",
    "table": "<table><tr><td>KEEP</td><td>KEEP</td></tr></table>",
    "a stray end tag": "</div>KEEP<b>KEEP</b>",
    "implicit paragraph ends": "<p style='display:none'>hid<p>KEEP",
    "implicit list item ends": "<ul><li style='display:none'>hid<li>KEEP</ul>",
    "a head that is never closed": "<html><head><title>t</title><body>KEEP</body></html>",
    "outlook conditional comments": "<!--[if !mso]><!-->KEEP<!--<![endif]-->",
    "the first of two style attributes is the one used": "<span style='color:red' style='display:none'>KEEP</span>",
    "details that are open": "<details open><summary>x</summary>KEEP</details>",
    "aria-hidden false": "<div aria-hidden='false'>KEEP</div>",
    "a font family with a CSS escape": "<span style=\"font-family:'\\5FAE\\8F6F'\">KEEP</span>",
    "entities": "<p>Caf&eacute; &amp; KEEP</p>",
    "a red rgb colour": "<span style='color:rgb(255,0,0)'>KEEP</span><span style='color:rgba(255,0,0,0.5)'>KEEP</span>",
    "a style block that hides an unrelated class": "<style>.pre{display:none}</style><div class=other>KEEP</div>",
    "a style rule that does not hide": "<style>.pre{color:red}</style><div class=pre>KEEP</div>",
    "a negative margin that is small": "<div style='margin-left:-10px;left:-20px'>KEEP</div>",
    "a spacer cell with text beside it": "<table><tr><td style='height:1px;line-height:1px;font-size:2px'>&nbsp;</td><td>KEEP</td></tr></table>",
    "a br end tag": "KEEP</br>KEEP",
}


@pytest.mark.parametrize("name", sorted(HIDDEN))
def test_text_a_person_would_not_see_is_never_in_the_text_without_doubt(name):
    """The invariant: hidden text is dropped, or the email is marked in doubt (a browser's recovery cannot be followed)."""
    text, doubt = E.visible_text(HIDDEN[name])
    assert doubt or "SECRET" not in text, text


# the shapes that must be dropped outright, with no doubt at all: the ordinary ways to hide text
DROPPED_CLEANLY = ["display none", "spaced, upper-case, important", "UPPER tags", "hidden attribute", "aria-hidden", "zero font", "zero opacity",
                   "visibility, nested", "line break inside", "mso-hide", "pushed off the page", "comment inside the style", "template",
                   "details and dialog", "rp, noembed", "overflow-hidden box of no height", "scale zero, clip", "transparent",
                   "a class hidden by a style block", "an id hidden by a style block", "a tag hidden by a style block", "hidden inside an @media block",
                   "tag.class selector", "class names are case-insensitive in quirks mode", "a rule with a list of selectors", "tiny fonts",
                   "transparent colours", "boxes of no size that clip", "far off the page, other units", "screen-reader-only recipes", "scales and zoom",
                   "font shorthand with no size", "later invalid display does not undo", "later invalid visibility", "popover", "var() decides"]


@pytest.mark.parametrize("name", DROPPED_CLEANLY)
def test_the_ordinary_ways_to_hide_text_are_dropped_outright(name):
    text, doubt = E.visible_text(HIDDEN[name])
    assert "SECRET" not in text, text
    if name not in ("template", "details and dialog", "rp, noembed"):
        assert doubt is False, name


@pytest.mark.parametrize("name", sorted(VISIBLE))
def test_text_a_person_sees_is_kept(name):
    text, doubt = E.visible_text(VISIBLE[name])
    assert "KEEP" in text and text.count("KEEP") == VISIBLE[name].count("KEEP"), text
    assert "hid" not in text.replace("hidden", "")


def test_ordinary_visible_text_comes_through_and_ordinary_markup_raises_no_doubt():
    html = ("<html><head><title>x</title><style>p{color:red}</style></head><body><table><tr><td><p>Hello,</p>"
            "<p>we have received your application.</p><div style='display:none'>preheader</div></td></tr></table></body></html>")
    text, doubt = E.visible_text(html)
    assert text == "Hello,\nwe have received your application." and doubt is False


@pytest.mark.parametrize("html", [
    pytest.param("<span style='display:none'><div></span>x", id="an end tag a browser would ignore"),
    pytest.param("<div style='display:none'>never closed", id="a hidden element never closed"),
    pytest.param("text<!-- never closed", id="unterminated comment"),
    pytest.param("text <b", id="unterminated tag"),
    pytest.param("<div>" * (E.MAX_DEPTH + 5), id="deeper than the limit"),
    pytest.param("<a>" * 30000 + "</b>" * 27000, id="30000 open, 27000 stray ends"),
    pytest.param("Visible<!-- one > two", id="unterminated comment that holds a >"),
    pytest.param("<span style='display:none'><div></span>SECRET</div></span>after", id="the ignored end tag alone"),
    pytest.param("<a>" * 300 + "</b>" * 50000, id="work limit"),
    pytest.param("<div><font hidden>x</div>after", id="a hidden element popped implicitly"),
    pytest.param("<form hidden><b></form>x", id="a hidden element closed with others open"),
    pytest.param("<body hidden>x</body>", id="a body that hides"),
    pytest.param("<div><script/>x</div>", id="a self-closing raw-text element"),
    pytest.param("<![CDATA[ x ]]>", id="cdata"),
    pytest.param("<svg><path/></svg>", id="svg"),
    pytest.param("<style>.a .b{display:none}</style><div>x</div>", id="a hiding rule with a selector it cannot evaluate"),
    pytest.param("<style>a[x]{display:none}</style><div>x</div>", id="an attribute selector"),
    pytest.param("<p hidden><table><tr><td>x</td></tr></table>", id="a table closing a hidden paragraph"),
], )
def test_markup_that_cannot_be_followed_raises_doubt(html):
    assert E.visible_text(html)[1] is True


def test_the_work_limit_stops_reading_and_is_fast():
    started = time.monotonic()
    text, doubt = E.visible_text("<a>" * 300 + "x" + "</b>" * 50000 + "TAIL")
    assert doubt and "TAIL" not in text and time.monotonic() - started < 3.0


def test_nothing_after_the_depth_limit_is_read():
    text, doubt = E.visible_text("<div>" * (E.MAX_DEPTH + 50) + "DEEP" + "</div>" * (E.MAX_DEPTH + 50) + "<p>After.</p>")
    assert "DEEP" not in text and "After." not in text and doubt


@pytest.mark.parametrize("name,html", [
    ("a-b-mix", "<a>" * 30000 + "</b>" * 27000),
    ("unterminated css comments", "<span style='" + "/* " * 30000 + "display:none'>x</span>"),
    ("deep divs", "<div>" * 60000),
    ("paragraphs", "<p>x" * 60000),
    ("list items", "<ul>" + "<li>x" * 60000),
    ("hidden spans", "<span style='display:none'>" * 30000 + "x" + "</span>" * 30000),
    ("end tags only", "</div>" * 30000),
    ("attributes", "<div " + "a=b " * 50000 + ">x</div>"),
    ("stray less-thans", "a < b " * 40000),
])
def test_hostile_markup_is_read_in_bounded_time(name, html):
    started = time.monotonic()
    E.visible_text(html)
    assert time.monotonic() - started < 3.0, name


def test_never_raises_on_random_markup():
    import random
    rng = random.Random(7)
    pieces = ["<div", "<span", "<p>", "</p>", "</div>", "</span>", "<td>", "<table>", " style='display:none'", " hidden", ">", "<", "/>", "<!--", "-->",
              "x", " ", "&amp;", "&#0;", "<br>", "<script>", "</script>", "\x00", "\\", "'", '"', "=", "<svg>", "</svg>", "<head>", "<li>"]
    for _ in range(400):
        E.visible_text("".join(rng.choice(pieces) for _ in range(rng.randint(1, 120))))


def test_none_and_empty_input():
    assert E.visible_text(None) == ("", False) and E.visible_text("") == ("", False)


# ---- as the Gmail parser uses it

def parse(html, text=""):
    return G.parse_message(stubs.gmail_message(text=text, html=html))


def test_an_html_only_email_is_read_as_what_a_person_sees_and_a_clean_one_has_no_doubt():
    m = parse("<p>Visible.</p><div style='display:none'>SECRET</div>")
    assert m.text == "Visible." and m.markup_doubt is False


def test_hidden_text_in_hostile_markup_is_dropped_and_the_email_is_marked_as_in_doubt():
    m = parse("<p>Visible.</p><span style='display:none'><div></span>SECRET")
    assert "SECRET" not in m.text and m.markup_doubt is True


def test_the_plain_part_is_what_is_read_when_there_is_one():
    m = parse("<p>Html words.</p><!-- never closed", text="Plain words.")
    assert m.text == "Plain words." and m.markup_doubt is False


def test_all_the_html_parts_of_one_email_share_one_budget():
    big = "<p>" + "x" * (E.MAX_CHARS - 20) + "</p>"
    raw = stubs.gmail_message(text="", html=big)
    # a second html part would make the email read twice the budget
    raw["payload"]["parts"].append(dict(raw["payload"]["parts"][-1]))
    m = G.parse_message(raw)
    assert m.markup_doubt is True and len(m.text) <= G.MAX_TEXT_CHARS


def test_a_hiding_style_block_with_only_simple_selectors_raises_no_doubt():
    text, doubt = E.visible_text("<style>.pre{display:none !important} @media (max-width:600px){.m{display:none}}</style>"
                                 "<div class=pre>preheader</div><div class=m>mobile</div><p>Hello.</p>")
    assert text == "Hello." and doubt is False


def test_ordinary_markup_with_a_stray_end_tag_after_a_cell_raises_no_doubt():
    html = "<table><tr><td><font size=2>Hello</td></font></tr></table><p>Thanks<p>Bye"
    text, doubt = E.visible_text(html)
    assert "Hello" in text and "Bye" in text and doubt is False


@pytest.mark.parametrize("html", [
    "<table><tr><td><span style='display:none'>pre</span>Hello</td></tr></table>",
    "<div hidden><table><tr><td>x</td></tr></table></div>after",
])
def test_a_hidden_element_closed_properly_is_no_doubt(html):
    assert E.visible_text(html)[1] is False


def test_the_plain_part_is_compared_with_the_html_part():
    plain_only_words = "Dear candidate, we are pleased to offer you the position of Backend Engineer. Please sign the attached contract."
    html = "<p>Thanks for applying. We will be in touch about the next steps soon.</p>"
    differ = G.parse_message(stubs.gmail_message(text=plain_only_words, html=html))
    same = G.parse_message(stubs.gmail_message(text="Thanks for applying. We will be in touch about the next steps soon.", html=html))
    assert differ.markup_doubt is True and same.markup_doubt is False
    assert E.parts_differ("short", "") is False and E.parts_differ("", "anything at all") is False


# ---- each rule on its own (several rules overlap: one alone would already raise doubt, so these pin them down)

@pytest.mark.parametrize("html", [
    "<li hidden><ol>SECRET</li>SECRET",                           # a list-item end tag cannot cross a list: the li stays open and hidden
    "<p hidden><button>SECRET</p>SECRET",                         # a paragraph end tag cannot cross a button
    "<body><div hidden>SECRET</body>SECRET",                      # </body> closes nothing: the text after it is still in the hidden div
])
def test_an_end_tag_a_browser_ignores_keeps_the_element_open_and_its_text_hidden(html):
    text, doubt = E.visible_text(html)
    assert "SECRET" not in text and doubt is True


def test_a_table_end_tag_closes_the_cells_inside_it_without_doubt():
    text, doubt = E.visible_text("<div hidden><table><tr><td>SECRET</table>SECRET</div>Visible.")
    assert text == "Visible." and doubt is False


def test_a_self_closing_raw_text_element_alone_raises_doubt():
    assert E.visible_text("<div><title/>x</title></div>")[1] is True


def test_a_table_that_closes_a_hidden_paragraph_alone_raises_doubt():
    assert E.visible_text("<p hidden><table><tr><td>x</td></tr></table></p>after")[1] is True


def test_the_options_of_a_select_are_not_text():
    text, doubt = E.visible_text("<select><option>SECRET</option></select>Visible.")
    assert text == "Visible." and doubt is False


def test_a_stray_br_end_tag_is_a_line_break():
    assert E.visible_text("KEEP</br>KEEP")[0] == "KEEP\nKEEP"
