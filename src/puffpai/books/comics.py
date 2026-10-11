"""Pure parsing of comic-book filenames into series + issue number.

The personal comics in the library are CBR rips whose titles are the original
filenames: ``Ultimate X-Men 054``, ``The Walking Dead 113 (2013) (Digital)
(Zone-Empire)``, ``01 - Paradise X``, ``00 X-Men``. This module turns those
into a ``(series, index, clean_title)`` triple so Calibre can group them as a
series. Nothing here touches the filesystem or the database.

Design rule: never invent words. ``clean_title`` only ever contains text that
was already in the title, minus scanner/group noise in parentheses.
"""

from __future__ import annotations

import os
import re
from typing import NamedTuple

__all__ = ["ComicInfo", "parse_comic"]


class ComicInfo(NamedTuple):
    series: str | None
    index: float | None
    clean_title: str


#: Words that only ever appear in scan-group / release noise.
_NOISE_WORDS = (
    "digital",
    "dcp",
    "c2c",
    "cover",
    "covers",
    "scan",
    "empire",
    "minutemen",
    "stormtroopers",
    "thegroup",
    "repack",
    "re-edit",
    "webrip",
    "noads",
    "hd",
    "rescan",
    "fixed",
    "archive edition",
    "variant",
    "director",
)

#: "(after 86)" cross-references the issue it follows; "(153)" is the legacy
#: numbering next to the new one. Both are noise, not part of the name.
_AFTER_RE = re.compile(r"^after\s*\d+$", re.I)
_BARE_NUM_RE = re.compile(r"^\d{1,4}$")
#: "(2048px)" is a scan resolution; "(- Nem -)" is a releaser's signature.
_PIXELS_RE = re.compile(r"^\d+\s*px$", re.I)
_DASHED_TAG_RE = re.compile(r"^-.*-$")

_YEAR_RE = re.compile(r"^(19|20)\d{2}$")
_OF_N_RE = re.compile(r"^of\s*\d+$", re.I)
#: "Zone-Empire", "Minutemen-InnerDemons", "The Ramones-DCP": a hyphen joining
#: two name-ish chunks with no spaces around it is a release-group signature.
_GROUP_RE = re.compile(r"^[A-Za-z0-9'&.! ]+-[A-Za-z0-9'&.! ]+$")
_PAREN_RE = re.compile(r"\(([^()]*)\)")


def _is_noise(inner: str) -> bool:
    text = inner.strip()
    if not text:
        return True
    low = text.lower()
    if _YEAR_RE.match(low) or _OF_N_RE.match(low) or _AFTER_RE.match(low):
        return True
    if _BARE_NUM_RE.match(low) or _PIXELS_RE.match(low) or _DASHED_TAG_RE.match(text):
        return True
    if any(word in low for word in _NOISE_WORDS):
        return True
    if _GROUP_RE.match(text) and not text.isdigit():
        return True
    return False


def _strip_noise(text: str) -> str:
    """Drop parenthesised scanner/release noise anywhere in the title."""
    # Some filenames lost their closing paren: "Ultimate Power 01 (director's cut".
    if text.count("(") > text.count(")"):
        cut = text.rfind("(")
        if cut > 0 and _is_noise(text[cut + 1:]):
            text = text[:cut]
    previous = None
    while previous != text:
        previous = text
        text = _PAREN_RE.sub(lambda m: "" if _is_noise(m.group(1)) else m.group(0), text)
    return re.sub(r"\s+", " ", text).strip(" -_.")


# Trailing issue number: "Ultimate X-Men 054", "Ultimate Spider-Man #131",
# "Ultimate Human 3", "Walking Dead 11 - 15" (range -> first issue).
_TRAILING_RE = re.compile(
    r"^(?P<series>.*?[^\s#\-])\s*[#]?\s*(?P<num>\d{1,4})(?:\.(?P<frac>\d+))?"
    r"(?:\s*-\s*\d{1,4})?\s*$"
)
# A double issue in one file: "Ultimate Wolverine vs. Hulk 01 & 02" -> 1.
_PAIR_RE = re.compile(
    r"^(?P<series>.*?[^\s#\-])\s*[#]?\s*(?P<num>\d{1,4})(?P<frac>)\s*(?:&|and)\s*[#]?\d{1,4}\s*$",
    re.I,
)
# Leading issue number: "001 Wolverine", "00 X-Men", "01 - Paradise X".
_LEADING_RE = re.compile(r"^(?P<num>\d{1,4})(?:\.(?P<frac>\d+))?\s*[-_.]?\s+(?P<series>\S.*)$")

_SERIES_TRIM = re.compile(r"[\s\-_,:#]+$")


_SERIES_COUNTER = re.compile(r"\s+(part|pt|vol|volume|book|no|issue)\.?$", re.I)


def _tidy_series(series: str) -> str | None:
    series = _SERIES_TRIM.sub("", series).strip()
    # "Ultimate Vision Part 4" -> series "Ultimate Vision": the counter word
    # belongs to the number, not to the series name.
    series = _SERIES_COUNTER.sub("", series).strip()
    # A "series" made of digits/punctuation only is not a series.
    if not re.search(r"[A-Za-z]", series):
        return None
    return series


def parse_comic(title: str, path: str) -> ComicInfo:
    """Parse one comic title.

    ``path`` is the Calibre ``books.path`` value; it is used only as a fallback
    source of a filename when the title carries no usable text.
    """
    raw = (title or "").strip()
    if not re.search(r"[A-Za-z0-9]", raw):
        # Fall back to the last path segment, minus Calibre's " (id)" suffix.
        tail = os.path.basename((path or "").replace("\\", "/")).strip()
        raw = re.sub(r"\s*\(\d+\)$", "", tail)

    cleaned = _strip_noise(raw)
    if not cleaned:
        return ComicInfo(None, None, raw)

    for pattern in (_PAIR_RE, _TRAILING_RE, _LEADING_RE):
        match = pattern.match(cleaned)
        if not match:
            continue
        series = _tidy_series(match.group("series"))
        if not series:
            continue
        digits = match.group("num")
        index = float(f"{int(digits)}.{match.group('frac')}") if match.group("frac") else float(int(digits))
        # Keep the number exactly as written so no words are invented.
        number_text = digits if not match.group("frac") else f"{digits}.{match.group('frac')}"
        return ComicInfo(series, index, f"{series} {number_text}")

    return ComicInfo(None, None, cleaned)
