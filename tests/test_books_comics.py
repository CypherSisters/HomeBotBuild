"""Table-driven tests for the comic filename parser.

Every title below is a real title taken from the library's CBR books.
"""

import pytest

from puffpai.books.comics import parse_comic

# (title, expected series, expected index, expected clean title)
CASES = [
    ("Ultimate X-Men 054", "Ultimate X-Men", 54.0, "Ultimate X-Men 054"),
    ("Ultimate Spider-Man 094", "Ultimate Spider-Man", 94.0, "Ultimate Spider-Man 094"),
    ("Ultimate Spider-Man #131", "Ultimate Spider-Man", 131.0, "Ultimate Spider-Man 131"),
    ("Ultimate Human 3", "Ultimate Human", 3.0, "Ultimate Human 3"),
    ("Cable 001", "Cable", 1.0, "Cable 001"),
    ("Ultimates Saga (2007) After 013", "Ultimates Saga After", 13.0, "Ultimates Saga After 013"),
    (
        "The Walking Dead 113 (2013) (Digital) (Zone-Empire)",
        "The Walking Dead",
        113.0,
        "The Walking Dead 113",
    ),
    (
        "Ultimate Comics Spider-Man 009 (2012) (digital) (Minutemen-InnerDemons)",
        "Ultimate Comics Spider-Man",
        9.0,
        "Ultimate Comics Spider-Man 009",
    ),
    (
        "X-Force 003 (Regular&Variant Cover) (2008) (The Ramones-DCP)",
        "X-Force",
        3.0,
        "X-Force 003",
    ),
    ("Ultimate Daredevil & Elektra 01 (of 4)", "Ultimate Daredevil & Elektra", 1.0,
     "Ultimate Daredevil & Elektra 01"),
    ("Ultimate Fantastic Four Annual 01", "Ultimate Fantastic Four Annual", 1.0,
     "Ultimate Fantastic Four Annual 01"),
    ("Ultimate Spider-Man Annual 1 (after 86)", "Ultimate Spider-Man Annual", 1.0,
     "Ultimate Spider-Man Annual 1"),
    ("Ultimate Comics Spider-Man 019 (153) (2011)", "Ultimate Comics Spider-Man", 19.0,
     "Ultimate Comics Spider-Man 019"),
    ("Ultimates 2 005 (2005)", "Ultimates 2", 5.0, "Ultimates 2 005"),
    ("Ultimates 2 001 (2005) (retailer sketch variant)", "Ultimates 2", 1.0, "Ultimates 2 001"),
    ("Ultimate Vision Part 4", "Ultimate Vision", 4.0, "Ultimate Vision 4"),
    ("Ultimate Power 01 (director's cut", "Ultimate Power", 1.0, "Ultimate Power 01"),
    ("Ultimate Wolverine vs. Hulk 01 & 02 (Director's Cut)", "Ultimate Wolverine vs. Hulk", 1.0,
     "Ultimate Wolverine vs. Hulk 01"),
    (
        "Ultimate Comics X-Men 016 (2012) (2048px) (theProletariat-DCP-HD)",
        "Ultimate Comics X-Men",
        16.0,
        "Ultimate Comics X-Men 016",
    ),
    ("001 Wolverine", "Wolverine", 1.0, "Wolverine 001"),
    ("00 X-Men", "X-Men", 0.0, "X-Men 00"),
    ("01 - Paradise X", "Paradise X", 1.0, "Paradise X 01"),
    ("All-New X-Men 030 (2014) (Digital) (Zone-Empire)", "All-New X-Men", 30.0, "All-New X-Men 030"),
    ("Ultimate Adventures #6", "Ultimate Adventures", 6.0, "Ultimate Adventures 6"),
    ("Ultimate Elektra 01 (of 5)", "Ultimate Elektra", 1.0, "Ultimate Elektra 01"),
]

# Titles with no issue number at all: series must stay None, and nothing may
# be invented into the clean title.
UNPARSEABLE = [
    # leading punctuation is trimmed, but no word is added or changed
    (". dcp archive edition", "dcp archive edition"),
    ("X-Men", "X-Men"),
    ("Ultimate Secrets", "Ultimate Secrets"),
    ("Fantastic Four & Spider-Man", "Fantastic Four & Spider-Man"),
    ("X-MEN Second Coming TPB", "X-MEN Second Coming TPB"),
    ("Ultimate Marvel Sampler (2007)", "Ultimate Marvel Sampler"),
    ("Ultimate Spider-Man Super Special", "Ultimate Spider-Man Super Special"),
]


@pytest.mark.parametrize("case", CASES, ids=[c[0][:40] for c in CASES])
def test_parses_real_titles(case):
    title, series, index, clean = case
    info = parse_comic(title, "Unknown/%s (1)" % title)
    assert info.series == series
    assert info.index == index
    assert info.clean_title == clean


@pytest.mark.parametrize("case", UNPARSEABLE, ids=[c[0][:40] for c in UNPARSEABLE])
def test_unparseable_titles_get_no_series(case):
    title, clean = case
    info = parse_comic(title, "Unknown/%s (1)" % title)
    assert info.series is None
    assert info.index is None
    assert info.clean_title == clean


def test_clean_title_never_invents_words():
    for title, _series, _index, clean in CASES:
        words = set(w.lower() for w in clean.replace("-", " ").split())
        source = set(w.lower() for w in title.replace("-", " ").split())
        assert words <= source | {w.lstrip("#") for w in source}, (title, clean)


def test_falls_back_to_the_path_when_the_title_is_useless():
    info = parse_comic("   ", "Unknown/Cable 010 (123)")
    assert info.series == "Cable"
    assert info.index == 10.0


def test_is_pure_and_repeatable():
    first = parse_comic("Cable 010", "Unknown/Cable 010 (1)")
    second = parse_comic("Cable 010", "Unknown/Cable 010 (1)")
    assert first == second
