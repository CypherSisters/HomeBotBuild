"""Make a ZIM article safe to hand to the browser, and its links openable.

A ZIM is third-party content, and the dashboard renders article HTML with
React's ``dangerouslySetInnerHTML`` on its own origin, under a CSP that allows
inline script. Anything that runs there can call ``/api/pai/*`` with the
appliance key attached. So this module is a security boundary, and it is built
as an OUTPUT ALLOWLIST:

* :class:`html.parser.HTMLParser` is used only to tokenise. Nothing from the
  source is ever copied through as raw bytes. The output is rebuilt entirely
  from our own serialisation: allowlisted elements, allowlisted attributes,
  and text, every value escaped here.
* Because the output is ours, a difference between how Python and a browser
  parse the INPUT cannot matter. ``<!--><img onerror=...>-->``, CDATA
  sections, ``<noscript>`` attribute tricks and unclosed raw-text elements are
  all either dropped or re-emitted as inert escaped text or a sanitised tag.
* Comments, doctypes, declarations, processing instructions and CDATA are
  never emitted.
* ``script``, ``style``, ``noscript``, ``template``, ``svg``, ``math``,
  ``iframe``, ``object``, ``embed``, ``form`` and the other active or raw-text
  elements are dropped TOGETHER WITH THEIR CONTENT (see
  :data:`_DROP_WITH_CONTENT`). An element that is neither allowed nor dropped
  (``html``, ``body``, ``label``, a custom element) loses its tags and keeps
  its text.
* Every kept ``id`` and ``<a name>`` gets the ``zim-`` prefix, and in-article
  ``#x`` links become ``#zim-x``, so article markup cannot clobber the
  dashboard's globals or ``document`` properties. ``name`` is dropped
  everywhere else.
* ``class`` is never emitted: with the pack's stylesheets gone, the only
  classes it could match are the dashboard's own.
* Attributes are per-element allowlists. No ``on*`` handler, ``srcdoc``,
  ``srcset``, ``target``, ``ping`` or SVG animation attribute is on any list.
  A ``style`` attribute survives only if it cannot fetch, escape or position
  anything (:data:`_STYLE_DENY_RE`).

URL POLICY
----------
``<a href>``: ``http:``, ``https:``, ``mailto:`` and pure fragments stay for
the dashboard's external-link guard; a protocol-relative ``//host/x`` becomes
``https://host/x``; no scheme means internal, and the link is rewritten for
the reader:

    <a href="#" data-zim-url="C/Moby_Dick" data-zim-fragment="Chapter_1">

Every other scheme (``javascript:``, ``vbscript:``, ``data:``, ``blob:``,
``file:``, unknown) removes the attribute.

``<img src>``, ``<video src|poster>``, ``<audio src>``, ``<source src>``,
``<track src>``: ZIM-internal relative paths only (the caller points them at
the same-origin asset endpoint), plus ``data:image/png|jpeg|gif|webp`` on
``img``/``source``/``poster``. Remote media and images are removed, so an
article cannot make the browser fetch from the internet.

``<base>``, ``<meta>`` and ``<link>`` never appear in the output, which also
closes the three holes the dashboard CSP does not cover (``base-uri``, a meta
refresh, ``form-action``).
"""

from __future__ import annotations

import html
import posixpath
import re
import urllib.parse
from html.parser import HTMLParser

# Schemes an <a href> may keep. Anything not in here is removed.
ALLOWED_SCHEMES = frozenset({'http', 'https', 'mailto'})

# Attributes allowed on every allowlisted element.
# No 'class': articles lose their own stylesheets, so a class can only ever
# match the DASHBOARD's utility classes ("fixed inset-0 z-50" draws a
# full-screen overlay over the app chrome).
_GLOBAL_ATTRS = frozenset({
    'id', 'title', 'lang', 'dir', 'style', 'align', 'valign',
    'width', 'height', 'role',
})

_CELL_ATTRS = frozenset({'colspan', 'rowspan', 'headers', 'scope', 'abbr',
                         'nowrap', 'bgcolor'})

# Element -> extra attributes it may carry. An element missing from this map
# is never emitted.
_TAG_ATTRS: dict[str, frozenset] = {
    'a': frozenset({'href', 'name', 'rel', 'data-zim-url',
                    'data-zim-fragment'}),
    'img': frozenset({'src', 'alt', 'border', 'hspace', 'vspace'}),
    'video': frozenset({'src', 'poster', 'controls', 'loop', 'muted',
                        'preload', 'playsinline'}),
    'audio': frozenset({'src', 'controls', 'loop', 'muted', 'preload'}),
    'source': frozenset({'src', 'type', 'media'}),
    'track': frozenset({'src', 'kind', 'srclang', 'label', 'default'}),
    'table': frozenset({'border', 'cellpadding', 'cellspacing', 'summary',
                        'bgcolor'}),
    'td': _CELL_ATTRS, 'th': _CELL_ATTRS,
    'tr': frozenset({'bgcolor'}),
    'col': frozenset({'span'}), 'colgroup': frozenset({'span'}),
    'ol': frozenset({'start', 'reversed', 'type'}),
    'ul': frozenset({'type'}),
    'li': frozenset({'value', 'type'}),
    'del': frozenset({'datetime'}), 'ins': frozenset({'datetime'}),
    'time': frozenset({'datetime'}),
    'details': frozenset({'open'}),
    'font': frozenset({'color', 'size', 'face'}),
    **{tag: frozenset() for tag in (
        'abbr', 'address', 'article', 'aside', 'b', 'bdi', 'bdo', 'big',
        'blockquote', 'br', 'caption', 'center', 'cite', 'code', 'dd', 'dfn',
        'div', 'dl', 'dt', 'em', 'figcaption', 'figure', 'footer', 'h1', 'h2',
        'h3', 'h4', 'h5', 'h6', 'header', 'hgroup', 'hr', 'i', 'kbd', 'main',
        'mark', 'nav', 'p', 'picture', 'pre', 'q', 'rp', 'rt', 'ruby', 's',
        'samp', 'section', 'small', 'span', 'strike', 'strong', 'sub',
        'summary', 'sup', 'tbody', 'tfoot', 'thead', 'tt', 'u', 'var', 'wbr',
    )},
}

# Elements with no end tag. Never enter skip mode, never emit a close.
_VOID = frozenset({
    'area', 'base', 'basefont', 'bgsound', 'br', 'col', 'embed', 'frame',
    'hr', 'image', 'img', 'input', 'keygen', 'link', 'meta', 'param',
    'source', 'track', 'wbr',
})

# Dropped with everything inside them: active content, foreign content (SVG
# and MathML re-tokenise differently and carry their own script and URL
# attributes), raw-text elements, and forms and their controls.
_DROP_WITH_CONTENT = frozenset({
    'applet', 'button', 'canvas', 'fencedframe', 'form', 'frameset', 'iframe',
    'math', 'noembed', 'noframes', 'noscript', 'object', 'plaintext',
    'portal', 'script', 'select', 'style', 'svg', 'template', 'textarea',
    'title', 'xmp',
})

# Attributes whose value is a URL, and what each may hold.
_LINK_ATTRS = frozenset({('a', 'href')})
_MEDIA_URL_ATTRS = frozenset({
    ('img', 'src'), ('video', 'src'), ('video', 'poster'), ('audio', 'src'),
    ('source', 'src'), ('track', 'src'),
})
_DATA_IMAGE_OK = frozenset({('img', 'src'), ('source', 'src'),
                            ('video', 'poster')})

# Inline bitmaps ZIM packs really do use. Note the absence of svg+xml.
_DATA_IMAGE_RE = re.compile(r'^data:image/(?:png|jpeg|gif|webp)[;,]',
                            re.IGNORECASE)

# A style attribute that could fetch (url(), image-set(), @import), escape
# the tokenizer (backslash escapes, comments), run anything (legacy
# expression(), behavior, -moz-binding, a script URL) or lift the article out
# of its box over the dashboard chrome (position). Any hit drops the whole
# attribute; the article loses a little layout, never more.
_STYLE_DENY_RE = re.compile(
    r'url\s*\(|image-set|image\s*\(|@|\\|/\*|expression|behavior|binding'
    r'|javascript|vbscript|position|[<>]',
    re.IGNORECASE)

# Browsers strip ASCII whitespace and C0 controls out of a URL before they
# look at the scheme, so "java\tscript:" and "\x01javascript:" both run.
# We strip the same set before classifying.
_URL_NOISE_RE = re.compile(r'[\x00-\x20\x7f]')

_SCHEME_RE = re.compile(r'^([A-Za-z][A-Za-z0-9+.\-]*):')

# A host-looking first segment after exactly two slashes.
_HOST_RE = re.compile(r'[A-Za-z0-9._~%\[\]:-]+')

# Control characters have no business in a ZIM path.
_PATH_CTRL_RE = re.compile(r'[\x00-\x1f\x7f]')

# Every id (and <a name>) the article keeps is moved into this namespace, and
# every in-article "#x" link with it. Unprefixed, an article element named
# "__NEXT_DATA__", "location" or "cookie" would shadow the dashboard's own
# globals and document properties (DOM clobbering). The dashboard reader
# applies the same mapping when it scrolls to a fragment.
ID_PREFIX = 'zim-'


def namespaced_id(value: str) -> str:
    """``x`` -> ``zim-x``; already-prefixed values are left alone, so the
    mapping is idempotent. Empty stays empty."""
    value = (value or '').strip()
    if not value:
        return ''
    return value if value.startswith(ID_PREFIX) else ID_PREFIX + value


# ── Scheme classification ─────────────────────────────────────────────────

def _scheme_of(probe: str) -> str | None:
    """Scheme of ``probe`` (already noise-stripped), or ``None`` if relative.

    ``C/Foo:Bar`` is a path, not a ``c/foo:`` URL: a colon only starts a
    scheme when it comes before any ``/``, ``?`` or ``#``.
    """
    match = _SCHEME_RE.match(probe)
    if match is None:
        return None
    colon = match.end() - 1
    for sep in '/?#':
        index = probe.find(sep)
        if 0 <= index < colon:
            return None
    return match.group(1).lower()


def _is_protocol_relative(probe: str) -> bool:
    """``//host/x`` yes; ``///x`` and ``//`` no (those are internal paths)."""
    if not probe.startswith('//') or probe.startswith('///'):
        return False
    host = re.split(r'[/?#]', probe[2:], maxsplit=1)[0]
    return bool(host) and _HOST_RE.fullmatch(host) is not None


def classify_url(value: str) -> str:
    """One URL value: ``'fragment'``, ``'external'``, ``'protocol-relative'``,
    ``'data-image'``, ``'internal'`` or ``'deny'``."""
    probe = _URL_NOISE_RE.sub('', value or '')
    if not probe:
        return 'deny'
    if probe.startswith('#'):
        return 'fragment'
    scheme = _scheme_of(probe)
    if scheme is not None:
        if scheme in ALLOWED_SCHEMES:
            return 'external'
        if _DATA_IMAGE_RE.match(probe):
            return 'data-image'
        return 'deny'
    if probe.startswith('//') and _is_protocol_relative(probe):
        return 'protocol-relative'
    return 'internal'


# ── Internal path resolution ──────────────────────────────────────────────

def resolve_zim_href(href: str, current_path: str) -> tuple[str, str]:
    """Resolve one relative href against ``current_path``.

    Returns ``(zim_path, fragment)``, both percent-decoded; ``zim_path`` is
    ``''`` when the href resolves to nothing usable. The path never starts
    with ``/`` and never contains a ``.``/``..`` segment or a control
    character, whatever the pack wrote (including ``..%2F..%2F`` forms,
    which decode to separators only *after* unquoting).
    """
    target, _, fragment = href.partition('#')
    fragment = _PATH_CTRL_RE.sub('', urllib.parse.unquote(fragment))
    if not target:
        return ('', fragment)

    target = _PATH_CTRL_RE.sub('', urllib.parse.unquote(target))
    if target.startswith('/'):
        resolved = target.lstrip('/')
    else:
        base = posixpath.dirname(current_path or '')
        resolved = posixpath.normpath(posixpath.join(base, target))
    # normpath can walk above the archive root ("../../x"); those segments
    # have no meaning in a ZIM, so drop them rather than emit a bad path.
    # The explicit segment filter also catches the forms normpath cannot
    # fold ("a/../../b" mid-path) and any empty segment from "///x".
    segments = [part for part in resolved.split('/')
                if part not in ('', '.', '..')]
    return ('/'.join(segments), fragment)


def _clean_zim_url(value: str) -> str:
    """A pre-existing ``data-zim-url`` value, if it is already canonical.

    Canonical means a plain internal ZIM path: anything with a scheme
    (``javascript:``, ``data:``, even ``https:``), a ``//host`` form or a
    fragment is dropped, so the attribute can only ever name an article.
    """
    value = (value or '').strip()
    if not value or _PATH_CTRL_RE.search(value):
        return ''
    if classify_url(value) != 'internal':
        return ''
    segments = value.split('/')
    if any(part in ('', '.', '..') for part in segments):
        return ''
    return value


# ── The serialiser ────────────────────────────────────────────────────────

def _esc(value: str) -> str:
    # quote=True for text as well as attributes: the output then contains no
    # bare quote outside our own attribute delimiters, which is what keeps the
    # caller's regex asset pass from ever matching inside text.
    return html.escape(value, quote=True)


class _Serializer(HTMLParser):
    """Tokenise the source; emit only what the allowlists permit."""

    def __init__(self, current_path: str, rewrite_links: bool):
        super().__init__(convert_charrefs=True)
        self.current_path = current_path or ''
        self.rewrite_links = rewrite_links
        self.out: list[str] = []
        self._skip_tag: str | None = None
        self._skip_depth = 0

    # -- attributes ----------------------------------------------------
    def _url_attr(self, tag: str, name: str, value: str):
        """Return the attribute list to emit in place of ``name=value``."""
        verdict = classify_url(value)
        if (tag, name) in _LINK_ATTRS:
            if verdict == 'fragment':
                return [(name, '#' + namespaced_id(value.strip()[1:]))]
            if verdict == 'external':
                return [(name, value.strip())]
            if verdict == 'protocol-relative':
                return [(name, 'https:' + _URL_NOISE_RE.sub('', value))]
            if verdict != 'internal':
                return []
            if not self.rewrite_links:
                return [(name, value)]
            zim_url, fragment = resolve_zim_href(value, self.current_path)
            if not zim_url:
                # Resolves to nothing inside the archive; an inert anchor
                # beats a live link to the dashboard origin.
                return []
            return [('href', '#'), ('data-zim-url', zim_url),
                    ('data-zim-fragment', fragment)]
        # Media and images: this archive, or an inline bitmap. Never remote.
        if verdict == 'internal':
            return [(name, value)]
        if verdict == 'data-image' and (tag, name) in _DATA_IMAGE_OK:
            return [(name, _URL_NOISE_RE.sub('', value))]
        return []

    def _attrs(self, tag: str, attrs) -> list[tuple[str, str | None]]:
        allowed = _TAG_ATTRS[tag]
        seen: set[str] = set()
        first: list[tuple[str, str | None]] = []
        for raw_name, value in attrs:
            name = (raw_name or '').lower()
            if name in seen:
                continue            # a browser keeps the first; so do we
            seen.add(name)
            first.append((name, value))

        rewrote_link = False
        kept: list[tuple[str, str | None]] = []
        for name, value in first:
            if name not in allowed and name not in _GLOBAL_ATTRS:
                continue
            if (tag, name) in _LINK_ATTRS or (tag, name) in _MEDIA_URL_ATTRS:
                replacement = self._url_attr(tag, name, value or '')
                if any(n == 'data-zim-url' for n, _ in replacement):
                    rewrote_link = True
                kept.extend(replacement)
                continue
            if name == 'style' and (value is None
                                    or _STYLE_DENY_RE.search(value)):
                continue
            if name == 'id' or (tag == 'a' and name == 'name'):
                value = namespaced_id(value or '')
                if not value:
                    continue
            kept.append((name, value))

        if tag == 'a':
            kept = self._settle_zim_attrs(kept, rewrote_link)
        return kept

    @staticmethod
    def _settle_zim_attrs(kept, rewrote_link):
        """data-zim-* written by the link rewrite win. Any the pack carried
        survive only when no rewrite happened and only in canonical form, so
        a second pass over our own output changes nothing."""
        # The rewrite emits href="#", data-zim-url, data-zim-fragment as a
        # contiguous triple; any other data-zim-* came from the source.
        generated: set[int] = set()
        for index in range(1, len(kept) - 1):
            if (kept[index][0] == 'data-zim-url'
                    and kept[index - 1] == ('href', '#')
                    and kept[index + 1][0] == 'data-zim-fragment'):
                generated.update({index, index + 1})
        result = []
        for index, (name, value) in enumerate(kept):
            if name in ('data-zim-url', 'data-zim-fragment')                     and index not in generated:
                if rewrote_link:
                    continue
                value = (_clean_zim_url(value or '') if name == 'data-zim-url'
                         else _PATH_CTRL_RE.sub('', value or ''))
                if name == 'data-zim-url' and not value:
                    continue
            result.append((name, value))
        return result

    def _emit_start(self, tag: str, attrs) -> None:
        if tag not in _TAG_ATTRS:
            return                  # unwrap: the tag goes, its text stays
        parts = ['<', tag]
        for name, value in self._attrs(tag, attrs):
            parts.append(' ')
            parts.append(name)
            if value is not None:
                parts.append('="')
                parts.append(_esc(value))
                parts.append('"')
        parts.append('>')
        self.out.append(''.join(parts))

    # -- HTMLParser hooks ----------------------------------------------
    def handle_starttag(self, tag, attrs):
        if self._skip_tag is not None:
            if tag == self._skip_tag:
                self._skip_depth += 1
            return
        if tag in _DROP_WITH_CONTENT and tag not in _VOID:
            self._skip_tag = tag
            self._skip_depth = 1
            return
        self._emit_start(tag, attrs)

    def handle_startendtag(self, tag, attrs):
        # "<x/>" is a start tag in HTML (the slash is ignored except on void
        # and foreign elements), so emit only the start; never a skip region,
        # because a self-closed element has no content of its own.
        if self._skip_tag is not None or tag in _DROP_WITH_CONTENT:
            return
        self._emit_start(tag, attrs)

    def handle_endtag(self, tag):
        if self._skip_tag is not None:
            if tag == self._skip_tag:
                self._skip_depth -= 1
                if self._skip_depth == 0:
                    self._skip_tag = None
            return
        if tag in _TAG_ATTRS and tag not in _VOID:
            self.out.append(f'</{tag}>')

    def handle_data(self, data):
        if self._skip_tag is None and data:
            self.out.append(_esc(data))

    # Comments, doctypes, declarations, processing instructions and CDATA
    # are never emitted: HTMLParser's defaults for handle_comment,
    # handle_decl, handle_pi and unknown_decl already do nothing, and these
    # overrides say so on purpose.
    def handle_comment(self, data):
        return None

    def handle_decl(self, decl):
        return None

    def handle_pi(self, data):
        return None

    def unknown_decl(self, data):
        return None


def _process(html_text: str, current_path: str, rewrite_links: bool) -> str:
    if not html_text:
        return ''
    # A parser instance per call, not module state: this runs on Flask
    # request threads.
    parser = _Serializer(current_path, rewrite_links)
    parser.feed(html_text)
    parser.close()
    return ''.join(parser.out)


def sanitize_untrusted_html(html_text: str) -> str:
    """Re-serialise third-party HTML through the allowlists, links untouched.

    Same policy as :func:`rewrite_article_links` except that internal
    ``<a href>`` values are kept as written instead of being rewritten for
    the reader.
    """
    return _process(html_text, '', rewrite_links=False)


def rewrite_article_links(html_text: str, current_path: str) -> str:
    """Sanitise an article and rewrite its internal links for the reader.

    ``current_path`` is the ZIM path of the article being displayed
    (``C/Moby_Dick``); relative hrefs resolve against its directory. Running
    the function on its own output changes nothing.
    """
    return _process(html_text, current_path or '', rewrite_links=True)
