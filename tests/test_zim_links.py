"""Tests for sanitising a ZIM article and rewriting its internal links.

The dashboard reader depends on the exact anchor shape produced here:
``href="#" data-zim-url="<path>" data-zim-fragment="<fragment>"``.

The sanitiser is an OUTPUT allowlist: whatever the input, the output holds
only allowlisted elements and attributes that this module serialised itself.
The adversarial cases below are checked twice: against the literal output,
and against the output as a spec-compliant HTML parser (html5lib, when it is
installed) builds it, which is what a browser would run.
"""

import re
from html.parser import HTMLParser

import pytest

from puffpai.knowledge.zim_links import (
    resolve_zim_href, rewrite_article_links, sanitize_untrusted_html,
)


CUR = 'C/Moby_Dick'


def _one(html, current=CUR):
    return rewrite_article_links(html, current)


# ── The inertness oracle ──────────────────────────────────────────────────

_ACTIVE_TAGS = {
    'script', 'style', 'iframe', 'object', 'embed', 'applet', 'svg', 'math',
    'base', 'meta', 'link', 'form', 'button', 'input', 'select', 'textarea',
    'noscript', 'template', 'frame', 'frameset', 'set', 'animate',
}
_URLISH = {'href', 'src', 'xlink:href', 'action', 'formaction', 'poster',
           'data', 'background', 'to', 'from', 'values', 'by', 'srcset',
           'ping', 'srcdoc', 'content'}
_BAD_URL = re.compile(r'^(?:javascript|vbscript|data:(?!image/(?:png|jpeg|gif|webp)[;,])|blob|file):',
                      re.IGNORECASE)
_NOISE = re.compile(r'[\x00-\x20\x7f]')


def _problems(tag, attrs):
    out = []
    local = tag.split('}')[-1].lower()
    if local in _ACTIVE_TAGS:
        out.append(f'active element <{local}>')
    for name, value in attrs:
        n = name.split('}')[-1].lower()
        v = _NOISE.sub('', value or '')
        if n.startswith('on'):
            out.append(f'handler {n} on <{local}>')
        if n in {'srcdoc', 'srcset', 'ping', 'target'}:
            out.append(f'{n} on <{local}>')
        if n in _URLISH and (_BAD_URL.match(v) or (
                n in ('src', 'poster') and re.match(r'^(?:https?:)?//', v))):
            out.append(f'{n}={value!r} on <{local}>')
        if n == 'style' and re.search(r'url\(|position|expression', v, re.I):
            out.append(f'style {value!r}')
    return out


class _StdlibWalk(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.problems = []

    def handle_starttag(self, tag, attrs):
        self.problems += _problems(tag, attrs)

    handle_startendtag = handle_starttag

    def handle_comment(self, data):
        self.problems.append('comment')

    def handle_decl(self, decl):
        self.problems.append('declaration')

    def handle_pi(self, data):
        self.problems.append('processing instruction')

    def unknown_decl(self, data):
        self.problems.append('cdata/unknown declaration')


def _assert_inert(out):
    assert '<!' not in out and '<?' not in out, out
    walker = _StdlibWalk()
    walker.feed(out)
    walker.close()
    assert walker.problems == [], (out, walker.problems)
    try:
        import html5lib
    except ImportError:          # the stdlib walk above is the fallback
        return
    # innerHTML-style fragment parse, the way the dashboard injects it.
    root = html5lib.parseFragment(out, container='div',
                                  namespaceHTMLElements=False)
    found = []
    for el in root.iter():
        if isinstance(el.tag, str):
            found += _problems(el.tag, list(el.attrib.items()))
    assert found == [], (out, found)


# ── Relative forms ────────────────────────────────────────────────────────

@pytest.mark.parametrize("href,expected_url,expected_frag", [
    ('Whale', 'C/Whale', ''),
    ('./Whale', 'C/Whale', ''),
    ('../A/Whale', 'A/Whale', ''),
    ('../C/Whale', 'C/Whale', ''),
    ('../C/Whale#Section', 'C/Whale', 'Section'),
    ('Whale#Chapter_1', 'C/Whale', 'Chapter_1'),
    ('sub/Whale', 'C/sub/Whale', ''),
    ('/C/Whale', 'C/Whale', ''),
    ('Moby%20Dick', 'C/Moby Dick', ''),
    ('../C/Caf%C3%A9', 'C/Café', ''),
    ('Whale#Sec%20One', 'C/Whale', 'Sec One'),
    ('../../C/Whale', 'C/Whale', ''),
])
def test_relative_forms_resolve(href, expected_url, expected_frag):
    out = _one(f'<a href="{href}">x</a>')
    assert 'href="#"' in out
    assert f'data-zim-url="{expected_url}"' in out
    assert f'data-zim-fragment="{expected_frag}"' in out


def test_resolve_helper_matches_rewrite():
    assert resolve_zim_href('../A/Foo#Bar', 'C/Baz') == ('A/Foo', 'Bar')


# ── External links, fragments ─────────────────────────────────────────────

@pytest.mark.parametrize("href", [
    'http://example.org/x',
    'https://example.org/x',
    'HTTPS://EXAMPLE.ORG/x',
    'mailto:someone@example.org',
])
def test_absolute_links_untouched(href):
    html = f'<a href="{href}">x</a>'
    assert _one(html) == html


def test_protocol_relative_link_becomes_https():
    assert (_one('<a href="//cdn.example.org/x">x</a>')
            == '<a href="https://cdn.example.org/x">x</a>')


def test_pure_fragment_moves_into_the_id_namespace():
    assert _one('<a href="#Chapter_2">x</a>') == '<a href="#zim-Chapter_2">x</a>'
    assert _one('<a href="#">x</a>') == '<a href="#">x</a>'


def test_anchor_id_moves_into_the_id_namespace():
    assert _one('<a id="Chapter_2"></a>') == '<a id="zim-Chapter_2"></a>'


# ── javascript: is dropped ────────────────────────────────────────────────

def test_javascript_href_is_dropped():
    out = _one('<a href="javascript:alert(1)" title="t">x</a>')
    assert 'javascript' not in out
    assert 'href' not in out
    assert 'title="t"' in out


def test_javascript_href_alone_leaves_bare_anchor():
    assert _one('<a href="javascript:void(0)">x</a>') == '<a>x</a>'


# ── Attribute quoting variants ────────────────────────────────────────────

@pytest.mark.parametrize("html", [
    "<a href='Whale'>x</a>",
    '<a href=Whale>x</a>',
    '<a  href = "Whale" >x</a>',
    '<a class="mw-redirect" href="Whale" title="Whale">x</a>',
    '<A HREF="Whale">x</A>',
])
def test_quoting_variants(html):
    out = rewrite_article_links(html, CUR)
    assert 'data-zim-url="C/Whale"' in out
    assert 'href="#"' in out


def test_other_attributes_survive():
    out = _one('<a href="Whale" title="Whale" lang="en">w</a>')
    assert 'title="Whale"' in out and 'lang="en"' in out


# ── Assets: internal kept for the asset pass, stylesheets never ───────────

def test_internal_img_src_kept_link_dropped():
    out = _one('<img src="../I/whale.png"><link href="../-/style.css" rel="s">')
    assert out == '<img src="../I/whale.png">'


# ── Multiple anchors, escaping, idempotence ───────────────────────────────

def test_multiple_anchors_all_rewritten():
    out = _one('<a href="Whale">a</a> and <a href="../A/Sea">b</a>')
    assert 'data-zim-url="C/Whale"' in out
    assert 'data-zim-url="A/Sea"' in out


def test_quotes_in_resolved_path_are_escaped():
    # A decoded quote must not be able to break out of the attribute.
    out = _one('<a href="It%27s_a_%22whale%22">x</a>')
    assert '&quot;' in out and '&#x27;' in out
    assert out.count('data-zim-url="') == 1
    assert out.startswith('<a href="#" data-zim-url="C/It&#x27;s_a_&quot;'
                          'whale&quot;" data-zim-fragment=""')


def test_idempotent():
    html = ('<a href="Whale">a</a><a href="#Sec">b</a>'
            '<a href="https://example.org">c</a>'
            '<img src="../I/w.png">')
    once = _one(html)
    assert _one(once) == once


def test_empty_html_passthrough():
    assert rewrite_article_links('', CUR) == ''


def test_text_is_escaped_including_quotes():
    out = _one('<p>a "b" \'c\' &lt;d&gt; &amp; e</p>')
    assert out == '<p>a &quot;b&quot; &#x27;c&#x27; &lt;d&gt; &amp; e</p>'


# ══════════════════════════════════════════════════════════════════════════
#  Regressions from the earlier adversarial pass (regex tokeniser era)
# ══════════════════════════════════════════════════════════════════════════

def test_A1_gt_in_double_quoted_title_before_href():
    out = _one('<a title="a>b" href="javascript:alert(1)">x</a>')
    assert out == '<a title="a&gt;b">x</a>'


def test_A1_gt_in_single_quoted_rel_before_href():
    out = _one("<a rel='>' href='javascript:alert(1)'>x</a>")
    assert out == '<a rel="&gt;">x</a>'


@pytest.mark.parametrize("attr", ['title', 'alt', 'data-thing'])
@pytest.mark.parametrize("quote", ['"', "'"])
def test_A1_gt_in_any_quoted_attribute_before_href(attr, quote):
    out = _one('<a {a}={q}x>y{q} href={q}javascript:z(){q}>t</a>'.format(
        a=attr, q=quote))
    assert 'javascript' not in out
    assert 'href' not in out


@pytest.mark.parametrize("attr", ['title', 'alt', 'data-thing'])
@pytest.mark.parametrize("quote", ['"', "'"])
def test_A1_gt_in_any_quoted_attribute_after_href(attr, quote):
    out = _one('<a href={q}javascript:z(){q} {a}={q}x>y{q}>t</a>'.format(
        a=attr, q=quote))
    assert 'javascript' not in out
    assert 'href' not in out


def test_A1_gt_in_quoted_attribute_does_not_block_a_real_rewrite():
    out = _one('<a title="a>b" href="../C/Whale">x</a>')
    assert 'data-zim-url="C/Whale"' in out
    assert 'title="a&gt;b"' in out


def test_A2_xlink_and_prefixed_href_are_not_emitted():
    assert (_one('<a xlink:href="../C/Foo" href="javascript:alert(1)">x</a>')
            == '<a>x</a>')
    assert _one('<a data-href="../C/Foo">x</a>') == '<a>x</a>'
    out = _one('<a ns:href="../C/Foo" href="../C/Whale">x</a>')
    assert out == ('<a href="#" data-zim-url="C/Whale" '
                   'data-zim-fragment="">x</a>')


def test_A3_data_text_html_href_is_dropped():
    out = _one('<a href="data:text/html,<script>alert(1)</script>">x</a>')
    assert out == '<a>x</a>'


@pytest.mark.parametrize("scheme", [
    'data:text/html,y', 'vbscript:msgbox(1)', 'blob:https://x/y',
    'file:///etc/passwd', 'about:blank', 'chrome://settings',
    'wyciwyg://0/x', 'jav\tascript:alert(1)', '',
])
def test_A3_every_other_scheme_is_removed_from_href(scheme):
    out = _one('<a href="{}" title="k">x</a>'.format(scheme))
    assert out == '<a title="k">x</a>'


# ── Internal resolution corner cases ──────────────────────────────────────

def test_low_gt_inside_the_href_value():
    out = _one('<a href="a>b">x</a>')
    assert out == ('<a href="#" data-zim-url="C/a&gt;b" '
                   'data-zim-fragment="">x</a>')


def test_low_percent_encoded_separators_resolve():
    out = _one('<a href="..%2F..%2FC%2FWhale">x</a>')
    assert out == ('<a href="#" data-zim-url="C/Whale" '
                   'data-zim-fragment="">x</a>')


def test_low_percent_encoded_traversal_to_nothing_goes_inert():
    assert _one('<a href="..%2F..%2F">x</a>') == '<a>x</a>'


def test_low_three_slashes_is_an_internal_path_not_a_host():
    out = _one('<a href="///x">x</a>')
    assert out == '<a href="#" data-zim-url="x" data-zim-fragment="">x</a>'


def test_resolved_path_has_no_control_characters():
    url, frag = resolve_zim_href('../C/Wh%00a%01le#S%00ec', CUR)
    assert url == 'C/Whale' and frag == 'Sec'


# ── javascript: obfuscations ──────────────────────────────────────────────

_OBFUSCATED = [
    'javascript:alert(1)',
    'JaVaScRiPt:alert(1)',
    '  javascript:alert(1)',
    'java\nscript:alert(1)',
    'java\x01script:alert(1)',
    '\x00javascript:alert(1)',
    'java&#115;cript:alert(1)',
    'javascript&#58;alert(1)',
    'javascript&colon;alert(1)',
    '&#x6a;avascript:alert(1)',
    'jav&#x09;ascript:alert(1)',
]


@pytest.mark.parametrize("value", _OBFUSCATED)
def test_obfuscated_script_urls_are_dropped_from_href(value):
    out = _one('<a href="{}" id="k">x</a>'.format(value))
    assert out == '<a id="zim-k">x</a>'


@pytest.mark.parametrize("value", _OBFUSCATED)
@pytest.mark.parametrize("tag,attr", [
    ('img', 'src'), ('video', 'poster'), ('audio', 'src'), ('source', 'src')])
def test_obfuscated_script_urls_are_dropped_from_media(value, tag, attr):
    out = _one('<{t} {a}="{v}" id="k">'.format(t=tag, a=attr, v=value))
    assert out == '<{t} id="zim-k">'.format(t=tag)


def test_slash_separated_attribute_is_still_an_attribute():
    assert _one('<a/href="javascript:alert(1)">x</a>') == '<a>x</a>'


# ── Event handlers come off every tag ─────────────────────────────────────

@pytest.mark.parametrize("handler", [
    'onClick="a()"', 'ONERROR="b()"', 'onmouseover=c()',
    "onfocus='d()'", 'OnLoad="e()"'])
@pytest.mark.parametrize("tag", ['img', 'a', 'div', 'p', 'span', 'td'])
def test_event_handlers_are_removed(tag, handler):
    out = _one('<{t} {h} id="k">'.format(t=tag, h=handler))
    assert out == '<{t} id="zim-k">'.format(t=tag)


def test_handler_removal_does_not_touch_the_rest_of_the_anchor():
    out = _one('<a onclick="steal()" href="../C/Whale" title="c">x</a>')
    assert out == ('<a href="#" data-zim-url="C/Whale" '
                   'data-zim-fragment="" title="c">x</a>')


# ── data: images ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("tag,attr", [
    ('img', 'src'), ('source', 'src'), ('video', 'poster')])
@pytest.mark.parametrize("mime", ['png', 'jpeg', 'gif', 'webp'])
def test_inline_bitmaps_are_kept(tag, attr, mime):
    html = '<{t} {a}="data:image/{m};base64,iVBORw0KGgo=">'.format(
        t=tag, a=attr, m=mime)
    assert _one(html) == html


def test_inline_svg_data_url_is_dropped():
    out = _one('<img src="data:image/svg+xml,<svg onload=alert(1)>">')
    assert out == '<img>'


def test_inline_bitmap_is_not_allowed_on_an_anchor():
    assert _one('<a href="data:image/png;base64,AAA">x</a>') == '<a>x</a>'


# ── sanitize_untrusted_html on its own ────────────────────────────────────

def test_sanitizer_leaves_internal_links_alone():
    html = '<a href="../C/Whale">x</a>'
    assert sanitize_untrusted_html(html) == html


def test_sanitizer_still_strips_scripty_attributes():
    out = sanitize_untrusted_html(
        '<a href="javascript:alert(1)" onclick="x()">y</a>')
    assert out == '<a>y</a>'


# ══════════════════════════════════════════════════════════════════════════
#  Parser-differential payloads
#
#  The previous implementation copied untouched source bytes through and
#  only re-serialised the start tags it changed. Python's HTMLParser and a
#  browser disagree about comments, CDATA, <noscript> and foreign content,
#  so markup Python saw as inert reached the browser as a live <img onerror>.
#  The output is now built entirely by our serialiser; these must all come
#  out inert under a spec-compliant re-parse.
# ══════════════════════════════════════════════════════════════════════════

DIFFERENTIAL_PAYLOADS = [
    '<!--><img src=x onerror=alert(1)>-->',
    '<!--!><img src=x onerror=alert(2)>-->',
    '<!---><img src=x onerror=alert(2)>-->',
    '<? x ><img src=x onerror=alert(3)> ?>',
    '<![CDATA[><img src=x onerror=alert(4)>]]>',
    '<!DOCTYPE x [<!ENTITY a "<img src=x onerror=alert(5)>">]>',
    '<noscript><p title="</noscript><img src=x onerror=alert(6)>"></p></noscript>',
    '<noscript><img src=x onerror=alert(6)></noscript>',
    '<style><img src=x onerror=alert(7)></style>',
    '<style></style ><img src=x onerror=alert(7)></style>',
    '<textarea><img src=x onerror=alert(8)></textarea>',
    '<title><img src=x onerror=alert(8)></title>',
    '<xmp><img src=x onerror=alert(8)></xmp>',
    '<iframe/><img src=x onerror=alert(9)></iframe>',
    '<noembed><img src=x onerror=alert(10)></noembed>',
    '<template><img src=x onerror=alert(11)></template>',
    '<plaintext><img src=x onerror=alert(12)>',
    '<script>x</script ><img src=x onerror=alert(13)>',
    '<svg><a><set attributeName="href" to="javascript:alert(1)"/>'
    '<text>x</text></a></svg>',
    '<svg><a><animate attributeName="href" values="javascript:alert(1)"/>'
    '<text>x</text></a></svg>',
    '<svg><a xlink:href="javascript:alert(1)"><text>x</text></a></svg>',
    '<svg><style><img src=x onerror=alert(14)></style></svg>',
    '<svg><![CDATA[><img src=x onerror=alert(15)>]]></svg>',
    '<svg/><img src=x onerror=alert(16)>',
    '<math><mtext><table><mglyph><style><img src=x onerror=alert(17)>',
    '<math href="javascript:alert(1)"><mi>x</mi></math>',
    '<form><math><mtext></form><form><mglyph><style></math>'
    '<img src onerror=alert(18)>',
    '<a href="&#106;avascript:alert(1)">x</a>',
    '<a href="javascript&colon;alert(1)">x</a>',
    '<img src="x" ONERROR="alert(1)">',
    '<img src=x onerror=alert(1)//>',
    '<div style="background:url(https://evil.example/p)">x</div>',
    '<div style="position:fixed;inset:0">phish</div>',
    '<div style="width:1\\00px">x</div>',
    '<a href="https://ok.example" target="_blank" ping="https://evil/p">x</a>',
    '<img srcset="javascript:alert(1) 1x, https://evil.example/a.png 2x">',
    '<base href="https://evil.example/">',
    '<meta http-equiv="refresh" content="0;url=javascript:alert(1)">',
    '<link rel="stylesheet" href="https://evil.example/x.css">',
    '<object data="javascript:alert(1)"></object>',
    '<embed src="javascript:alert(1)">',
    '<form action="https://evil/"><input name=a><button formaction="javascript:x">b</button></form>',
    '<iframe srcdoc="<script>alert(1)</script>"></iframe>',
    '<video src="https://evil.example/t.mp4" autoplay></video>',
    '<video poster="https://evil.example/p.png"></video>',
    '<audio autoplay><source src="https://evil.example/t.mp3"></audio>',
    '<video><track src="//evil.example/t.vtt"></video>',
    '<img src="https://evil.example/px.gif">',
    '<img src="//evil.example/px.gif">',
    '<table background="https://evil.example/b"><tr><td>x</td></tr></table>',
    '<a href="#" data-zim-url="../../etc/passwd">x</a>',
    '<p <img src=x onerror=alert(19)>>',
    '<img """><script>alert(20)</script>">',
    '<img src="x`<script>alert(21)</script>"`>',
    '<a href="x" <img src=x onerror=alert(22)>>y</a>',
    '</p ><img src=x onerror=alert(23)>',
    '<img src=x onerror=alert(24)',
    '<body onload=alert(25)><html onmouseover=alert(26)>',
]


@pytest.mark.parametrize("html", DIFFERENTIAL_PAYLOADS)
def test_differential_payloads_are_inert(html):
    out = _one(html)
    _assert_inert(out)
    _assert_inert(sanitize_untrusted_html(html))
    assert _one(out) == out, "the output must be a fixed point"


@pytest.mark.parametrize("html", [
    '<!--><img src=x onerror=alert(1)>-->',
    '<![CDATA[><img src=x onerror=alert(4)>]]>',
    '<noscript><p title="</noscript><img src=x onerror=alert(6)>"></p></noscript>',
    '<svg><a><set attributeName="href" to="javascript:alert(1)"/></a></svg>',
])
def test_verifier_payloads_through_the_api_pipeline(html):
    from puffpai import api_server
    out = api_server._rewrite_zim_article_html(html, 'coll', 'A/Main')
    _assert_inert(out)
    assert 'onerror' not in out.lower() and 'javascript' not in out.lower()


def test_active_elements_are_dropped_with_their_content():
    out = _one('<p>a</p><script>var s="<a href=x>"</script>'
               '<svg><text>svgtext</text></svg><math><mi>m</mi></math>'
               '<form><input value="v">formtext</form><p>b</p>')
    assert out == '<p>a</p><p>b</p>'


def test_unknown_elements_are_unwrapped_keeping_text():
    out = _one('<html><body class="x"><my-el data-a="1">hi <b>there</b>'
               '</my-el></body></html>')
    assert out == 'hi <b>there</b>'


def test_remote_media_dropped_internal_media_kept():
    out = _one('<video src="../I/v.webm" poster="../I/p.jpg" controls '
               'autoplay><source src="https://evil/x.mp4" type="video/mp4">'
               '<track src="../I/s.vtt" kind="subtitles"></video>')
    assert out == ('<video src="../I/v.webm" poster="../I/p.jpg" controls>'
                   '<source type="video/mp4">'
                   '<track src="../I/s.vtt" kind="subtitles"></video>')


def test_benign_style_survives_hostile_style_does_not():
    assert (_one('<td style="text-align:center;color:#333">x</td>')
            == '<td style="text-align:center;color:#333">x</td>')
    for bad in ('background:url(x)', 'position:fixed', 'color:red\\;',
                'width:expression(alert(1))', 'x:/**/y', '@import "x"'):
        assert _one(f'<div style="{bad}">x</div>') == '<div>x</div>'


def test_forged_data_zim_attrs_are_canonical_or_gone():
    # A pack-supplied data-zim-url survives only in canonical form, and never
    # alongside a real rewrite.
    assert (_one('<a href="#" data-zim-url="C/Ok" data-zim-fragment="S">x</a>')
            == '<a href="#" data-zim-url="C/Ok" data-zim-fragment="S">x</a>')
    assert _one('<a href="#" data-zim-url="../x">x</a>') == '<a href="#">x</a>'
    out = _one('<a data-zim-url="C/Evil" href="../C/Whale">x</a>')
    assert out.count('data-zim-url') == 1 and 'C/Whale' in out


# ── class never reaches the dashboard ─────────────────────────────────────

@pytest.mark.parametrize("html,expected", [
    ('<div class="fixed inset-0 z-50">x</div>', '<div>x</div>'),
    ('<a class="fixed inset-0 z-[9999]" href="#t">x</a>', '<a href="#zim-t">x</a>'),
    ('<span CLASS="sr-only">x</span>', '<span>x</span>'),
    ('<table class="wikitable"><tr><td class="bg-red-500">x</td></tr></table>',
     '<table><tr><td>x</td></tr></table>'),
])
def test_class_is_never_emitted(html, expected):
    out = _one(html)
    assert out == expected
    assert 'class' not in out.lower()


def test_no_class_survives_a_spec_parse():
    html5lib = pytest.importorskip('html5lib')
    html = ('<div class="fixed inset-0 z-50"><p class=a>x<img class="b" '
            'src="../I/a.png"><a class="c" href="Whale">w</a></p></div>')
    root = html5lib.parseFragment(_one(html), container='div',
                                  namespaceHTMLElements=False)
    assert all('class' not in el.attrib for el in root.iter()
               if isinstance(el.tag, str))


# ── DOM clobbering: ids and anchor names are namespaced ───────────────────

@pytest.mark.parametrize("html,expected", [
    ('<div id="__NEXT_DATA__">x</div>', '<div id="zim-__NEXT_DATA__">x</div>'),
    ('<img id="location" src="../I/a.png">',
     '<img id="zim-location" src="../I/a.png">'),
    ('<a name="cookie">x</a>', '<a name="zim-cookie">x</a>'),
    ('<img name="cookie" src="../I/a.png">', '<img src="../I/a.png">'),
    ('<p id="">x</p>', '<p>x</p>'),
    ('<p id="zim-already">x</p>', '<p id="zim-already">x</p>'),
])
def test_ids_and_anchor_names_cannot_clobber(html, expected):
    out = _one(html)
    assert out == expected
    assert _one(out) == out


def test_fragment_link_and_its_target_still_meet():
    out = _one('<a href="#Early_life">jump</a><h2 id="Early_life">E</h2>')
    assert out == ('<a href="#zim-Early_life">jump</a>'
                   '<h2 id="zim-Early_life">E</h2>')


def test_no_unprefixed_id_or_name_survives_a_spec_parse():
    html5lib = pytest.importorskip('html5lib')
    html = ('<div id="__NEXT_DATA__"><form name="cookie"><input name="x">'
            '</form><img name="location" id="domain"><a name="body" '
            'href="#top">t</a><span id=" spaced ">s</span></div>')
    root = html5lib.parseFragment(_one(html), container='div',
                                  namespaceHTMLElements=False)
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        for attr in ('id', 'name'):
            value = el.attrib.get(attr)
            if value is not None:
                assert value.startswith('zim-'), (el.tag, attr, value)
        if el.tag != 'a':
            assert 'name' not in el.attrib


# ── The three holes the dashboard CSP does not cover ──────────────────────

@pytest.mark.parametrize("html", [
    '<base href="https://evil.example/">',
    '<BASE HREF="https://evil.example/">',
    '<base href="../C/" target="_blank">',
    '<base>', '<base/>',
    '<meta http-equiv="refresh" content="0;url=https://evil.example">',
    '<meta HTTP-EQUIV=" Refresh " content="0;url=https://evil.example">',
    '<meta charset="utf-8">',
    "<meta http-equiv='set-cookie' content='a=b'>",
    '<form action="https://evil.example/collect"><input name="a"></form>',
    '<form action="search" method="get"></form>',
])
def test_base_meta_and_forms_never_appear(html):
    assert _one(html) == ''


def test_base_removal_leaves_the_surrounding_markup():
    out = _one('<p>before</p><base href="https://evil.example/"><p>after</p>')
    assert out == '<p>before</p><p>after</p>'


@pytest.mark.parametrize("html", [
    '<img src="../I/w.png" srcset="../I/w2.png 2x">',
    '<img src="../I/w.png" srcset="javascript:alert(1) 1x">',
    '<img src="../I/w.png" srcset="data:image/png;base64,AAA 1x">',
])
def test_srcset_is_never_emitted(html):
    assert _one(html) == '<img src="../I/w.png">'


# ── A realistic page ──────────────────────────────────────────────────────

WIKI_FRAGMENT = '''<!-- a ZIM page, roughly -->
<!DOCTYPE html>
<html><head>
<style>
a[href^="http"]:after { content: "<a href=Whale>"; }
</style>
<script type="text/javascript">
  var s = '<a href="Whale">not a link</a>';
  if (1 < 2 && 3 > 2) { document.title = "<a href=javascript:alert(1)>"; }
</script>
</head>
<body class="mw-body">
<h1 id="firstHeading">Whale &amp; Dolphin &mdash; an overview</h1>
<p>The whale<sup id="cite_ref-1" class="reference"><a href="#cite_note-1">[1]</a></sup>
is a marine mammal. See <a href="//cdn.example.org/x">the CDN</a>,
<a href="https://example.org/x" rel="nofollow">an outside page</a> and
<a href="mailto:someone@example.org">mail</a>.</p>
<table class="wikitable"><tbody>
<tr><th>Species</th><th>Length</th></tr>
<tr><td>Blue whale</td><td>30&nbsp;m</td></tr>
<tr><td>Narwhal<br></td><td>5&nbsp;m
</tbody></table>
<div class="thumb"><img src="../I/whale.png" alt="a whale" width="220"
 srcset="../I/whale2x.png 2x">
<p class="caption">A whale &lt;in the wild&gt;
</div>
<ul><li>one<li>two<li>three</ul>
<a href="#Chapter_2">jump</a>
<a href="Whale">internal</a>
<a href="#" data-zim-url="C/Already" data-zim-fragment="Done">already done</a>
<link href="../-/style.css" rel="stylesheet">
<!--[if IE]><p>conditional<![endif]-->
<p>Text with a stray &lt; and an unclosed <em>emphasis
</body></html>
'''


def test_wiki_fragment_keeps_the_article_and_drops_the_rest():
    out = _one(WIKI_FRAGMENT)
    _assert_inert(out)
    for kept in ('<h1 id="zim-firstHeading">Whale &amp; Dolphin \u2014 an overview</h1>',
                 '<sup id="zim-cite_ref-1">',
                 '<a href="#zim-cite_note-1">[1]</a>',
                 '<a href="https://cdn.example.org/x">the CDN</a>',
                 '<a href="https://example.org/x" rel="nofollow">',
                 '<table>', '<td>Blue whale</td>',
                 '<img src="../I/whale.png" alt="a whale" width="220">',
                 'A whale &lt;in the wild&gt;',
                 '<ul><li>one<li>two<li>three</ul>',
                 '<a href="#" data-zim-url="C/Whale" data-zim-fragment="">internal</a>',
                 '<a href="#" data-zim-url="C/Already" data-zim-fragment="Done">',
                 'an unclosed <em>emphasis'):
        assert kept in out, kept
    for gone in ('<!--', 'DOCTYPE', '<style', '<script', 'not a link',
                 'document.title', '<link', 'srcset', 'conditional',
                 '<html', '<body', '<head'):
        assert gone not in out, gone


def test_idempotent_on_the_wiki_fragment():
    once = _one(WIKI_FRAGMENT)
    assert _one(once) == once
    assert sanitize_untrusted_html(once) == once


def _big_fragment(target_bytes):
    out = []
    size = 0
    while size < target_bytes:
        out.append(WIKI_FRAGMENT)
        size += len(WIKI_FRAGMENT)
    return ''.join(out)


def test_large_article_is_rewritten_quickly():
    # A 1.5 MB article is the realistic worst case for a maxi wiki page. The
    # bound is generous: this guards against an accidental quadratic.
    import time as _time
    blob = _big_fragment(1_500_000)
    started = _time.perf_counter()
    out = rewrite_article_links(blob, CUR)
    elapsed = _time.perf_counter() - started
    assert out.count('<td>Blue whale</td>') == blob.count('<td>Blue whale</td>')
    assert elapsed < 3.0, "1.5 MB took {:.2f}s".format(elapsed)


# ── The asset endpoint and the asset base ─────────────────────────────────

@pytest.mark.parametrize("mime,inert", [
    ('image/png', True), ('image/jpeg', True), ('video/webm', True),
    ('audio/ogg', True), ('font/woff2', True),
    ('text/css', False), ('text/vtt', True), ('application/pdf', False),
    ('text/html', False), ('text/html; charset=utf-8', False),
    ('image/svg+xml', False), ('application/xhtml+xml', False),
    ('text/xml', False), ('application/javascript', False),
    ('application/octet-stream', False), ('', False), (None, False),
])
def test_asset_types_that_could_run_are_attachments(mime, inert):
    from puffpai import api_server
    assert api_server._zim_asset_renders_inert(mime) is inert


# A second type smuggled after a comma or inside quotes, a header split, a
# bare type the list does not name: each must come out an attachment.
_SMUGGLED_MIMES = [
    'image/png;a,text/html',
    'text/css;,text/html',
    'video/mp4;x="a",text/html',
    'image/png,text/html',
    'image/png, text/html',
    'image/png;charset="x,text/html"',
    'image/png;"',
    'audio/ogg;codecs=opus,text/html',
    'font/woff2;x=1,application/xhtml+xml',
    'image/png\r\nContent-Type: text/html',
    'image/png\nX: y',
    'image/png\x00',
    'image/png\x0btext/html',
    'image/pngx',
    'image/png+xml',
    'image /png',
    'image/svg+xml;foo=image/png',
    'text/html;image/png',
    ';image/png',
    'video/',
    'application/pdf',
    'application/pdf; charset=binary',
    'text/vtt;,text/html',
    'text/vtt,text/html',
    'text/vtt;x="a",text/html',
    'text/vtt\r\nContent-Type: text/html',
    'text/vttx',
]


@pytest.mark.parametrize("mime", _SMUGGLED_MIMES)
def test_smuggled_or_unlisted_mime_is_an_octet_stream_attachment(mime):
    from puffpai import api_server
    assert api_server._zim_asset_content_type(mime) == (
        'application/octet-stream', False)


@pytest.mark.parametrize("mime,clean", [
    ('image/png', 'image/png'),
    (' IMAGE/PNG ', 'image/png'),
    ('image/png; q=1', 'image/png'),
    ('image/jpg', 'image/jpeg'),
    ('video/mp4; codecs=avc1.42E01E', 'video/mp4'),
    ('audio/x-wav', 'audio/wav'),
    ('application/font-woff', 'font/woff'),
    ('text/vtt', 'text/vtt'),
    ('TEXT/VTT; charset=utf-8', 'text/vtt'),
])
def test_inline_type_is_the_clean_bare_type_never_the_packs_string(mime,
                                                                   clean):
    from puffpai import api_server
    assert api_server._zim_asset_content_type(mime) == (clean, True)


def test_every_inline_type_is_image_audio_video_font_or_vtt():
    from puffpai import api_server
    for bare, clean in api_server._ZIM_INLINE_ASSET_TYPES.items():
        for value in (bare, clean):
            assert value == value.strip().lower()
            assert ';' not in value and ',' not in value
        assert (clean.split('/')[0] in ('image', 'audio', 'video', 'font')
                or clean == 'text/vtt')
        assert clean != 'image/svg+xml'


@pytest.mark.parametrize("value", [
    'javascript:alert(1)', ' JaVa\tscript:alert(1)', 'data:text/html,x',
    'vbscript:x', 'https://evil.example/x', '//evil.example/x', '#frag',
])
def test_a_pack_data_zim_url_with_a_scheme_is_dropped(value):
    import html as _h
    out = _one('<a data-zim-url="%s">x</a>' % _h.escape(value, quote=True))
    assert 'data-zim-url' not in out
    assert out == '<a>x</a>'


def test_a_pack_data_zim_url_that_is_a_plain_path_survives():
    assert _one('<a data-zim-url="A/Foo">x</a>') == (
        '<a data-zim-url="A/Foo">x</a>')


@pytest.mark.parametrize("name,encoded", [
    ('a&b.png', 'a%26b.png'),
    ("it's.png", 'it%27s.png'),
    ('q"x.png', 'q%22x.png'),
    ('a<b>.png', 'a%3Cb%3E.png'),
])
def test_asset_names_with_markup_characters_survive_the_rewrite(name,
                                                               encoded):
    import html as _h
    from puffpai import api_server
    out = api_server._rewrite_zim_article_html(
        '<img src="../I/%s">' % _h.escape(name, quote=True), 'wiki', 'A/Main')
    assert out == ('<img src="/api/pai/knowledge/library/wiki/asset?path=I/%s">'
                   % encoded)
    assert '%26amp' not in out and '%26quot' not in out and '%26%2339' not in out


def test_collection_name_cannot_inject_into_the_asset_base():
    from puffpai import api_server
    out = api_server._rewrite_zim_article_html(
        '<img src="../I/a.png">', 'c"><img src=x onerror=alert(1)>', 'A/Main')
    _assert_inert(out)
    assert out.count('<img') == 1
