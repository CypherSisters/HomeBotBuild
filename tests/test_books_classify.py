"""Table-driven tests for the book category classifier."""

import pytest

from puffpai.books.categories import CATEGORIES, MAX_CATEGORIES_PER_BOOK, UMBRELLA_TAGS
from puffpai.books.classify import classify, is_fiction

NON_FICTION_ONLY = (
    "History",
    "War & Military",
    "Biography & Letters",
    "Travel & Exploration",
    "Society & Politics",
    "Religion & Spirituality",
    "Science & Nature",
    "Technology & Engineering",
    "Medicine & Health",
    "Homestead & Cooking",
    "Survival & Preparedness",
    "Language & Reference",
    "Magazines & Periodicals",
    "Art & Music",
    "Philosophy",
)


def run(title="", authors="", subjects=(), locc=(), shelves=()):
    return classify(
        title=title,
        authors=authors,
        subjects=list(subjects),
        locc=list(locc),
        bookshelves=list(shelves),
    )


# (case id, kwargs, expected first category, categories that must appear)
CASES = [
    # --- one case per category ------------------------------------------
    ("general fiction", dict(subjects=["Fiction"], locc=["PR"], shelves=["Category: Novels"]),
     "General Fiction", ["General Fiction"]),
    ("science fiction", dict(subjects=["Science fiction"], shelves=["Science Fiction"]),
     "Science Fiction", ["Science Fiction"]),
    ("fantasy", dict(subjects=["Fairy tales"], shelves=["Category: Mythology, Legends & Folklore"]),
     "Fantasy & Folklore", ["Fantasy & Folklore"]),
    ("mystery", dict(subjects=["Detective and mystery stories"], shelves=["Detective Fiction"]),
     "Mystery & Detective", ["Mystery & Detective"]),
    ("horror", dict(subjects=["Ghost stories", "Horror tales"], shelves=["Horror"]),
     "Horror & Gothic", ["Horror & Gothic"]),
    ("gothic is horror", dict(subjects=["Gothic fiction"], locc=["PR"]),
     "Horror & Gothic", ["Horror & Gothic"]),
    ("adventure", dict(subjects=["Sea stories", "Adventure stories"], shelves=["Category: Adventure"]),
     "Adventure & Westerns", ["Adventure & Westerns"]),
    ("western", dict(subjects=["Western stories"], shelves=["Western"]),
     "Adventure & Westerns", ["Adventure & Westerns"]),
    ("romance", dict(subjects=["Love stories"], shelves=["Category: Romance"]),
     "Romance", ["Romance"]),
    ("humor", dict(subjects=["Wit and humor"], shelves=["Category: Humour"]),
     "Humor & Satire", ["Humor & Satire"]),
    ("short stories only", dict(subjects=["Short stories"]),
     "Short Stories", ["Short Stories"]),
    ("children", dict(subjects=["Children's stories"], locc=["PZ"]),
     "Children's & Young Readers", ["Children's & Young Readers"]),
    ("poetry", dict(subjects=["English poetry"], shelves=["Category: Poetry"]),
     "Poetry", ["Poetry"]),
    ("drama", dict(subjects=["English drama"], shelves=["Category: Plays/Films/Dramas"]),
     "Drama & Plays", ["Drama & Plays"]),
    ("history", dict(subjects=["Great Britain -- History"], locc=["DA"], shelves=["Category: History - British"]),
     "History", ["History"]),
    ("war", dict(subjects=["World War, 1914-1918"], locc=["D501"], shelves=["World War I"]),
     "War & Military", ["War & Military"]),
    ("biography", dict(subjects=["Biography"], shelves=["Category: Biographies"]),
     "Biography & Letters", ["Biography & Letters"]),
    ("travel", dict(subjects=["Voyages and travels"], locc=["G"], shelves=["Category: Travel Writing"]),
     "Travel & Exploration", ["Travel & Exploration"]),
    ("philosophy", dict(subjects=["Ethics"], locc=["B"], shelves=["Category: Philosophy & Ethics"]),
     "Philosophy", ["Philosophy"]),
    ("religion", dict(subjects=["Bible -- Commentaries"], locc=["BS"], shelves=["Christianity"]),
     "Religion & Spirituality", ["Religion & Spirituality"]),
    ("science", dict(subjects=["Natural history"], locc=["QH"], shelves=["Category: Science - Biology"]),
     "Science & Nature", ["Science & Nature"]),
    ("technology", dict(subjects=["Steam engines", "Machinery"], locc=["TJ"],
                        shelves=["Category: Engineering & Technology"]),
     "Technology & Engineering", ["Technology & Engineering"]),
    ("medicine", dict(subjects=["Surgery"], locc=["RD"], shelves=["Category: Health & Medicine"]),
     "Medicine & Health", ["Medicine & Health"]),
    ("homestead cooking", dict(subjects=["Cooking (Meat)"], locc=["TX"], shelves=["Cookbooks and Cooking"]),
     "Homestead & Cooking", ["Homestead & Cooking"]),
    ("homestead farm", dict(subjects=["Agriculture"], locc=["S"]),
     "Homestead & Cooking", ["Homestead & Cooking"]),
    ("art", dict(subjects=["Painting, Italian"], locc=["ND"], shelves=["Category: Art"]),
     "Art & Music", ["Art & Music"]),
    ("reference", dict(subjects=["English language -- Dictionaries"], locc=["PE"],
                       shelves=["Category: Encyclopedias/Dictionaries/Reference"]),
     "Language & Reference", ["Language & Reference"]),
    ("society", dict(subjects=["Political science"], locc=["JC"], shelves=["Category: Politics"]),
     "Society & Politics", ["Society & Politics"]),
    ("periodical", dict(subjects=["Periodicals"], locc=["AP"], shelves=["Notes and Queries"]),
     "Magazines & Periodicals", ["Magazines & Periodicals"]),
    ("punch is humor then periodical", dict(shelves=["Punch"]),
     "Humor & Satire", ["Humor & Satire", "Magazines & Periodicals"]),

    # --- Survival & Preparedness ----------------------------------------
    ("survival camping", dict(subjects=["Camping", "Woodcraft"], locc=["GV"]),
     "Survival & Preparedness", ["Survival & Preparedness"]),
    ("survival scouting", dict(subjects=["Boy Scouts", "Outdoor life"]),
     "Survival & Preparedness", ["Survival & Preparedness"]),
    ("survival first aid pairs with medicine",
     dict(subjects=["First aid in illness and injury"], locc=["RC"]),
     "Survival & Preparedness", ["Survival & Preparedness", "Medicine & Health"]),
    ("survival canning pairs with homestead",
     dict(subjects=["Canning and preserving"], locc=["TX"]),
     "Survival & Preparedness", ["Survival & Preparedness", "Homestead & Cooking"]),
    ("survival hunting", dict(subjects=["Hunting"], locc=["SK"]),
     "Survival & Preparedness", ["Survival & Preparedness"]),
    ("survival edible plants", dict(subjects=["Wild plants, Edible"], locc=["QK"]),
     "Survival & Preparedness", ["Survival & Preparedness"]),
    ("survival knots and signalling", dict(subjects=["Knots and splices", "Signals and signaling"]),
     "Survival & Preparedness", ["Survival & Preparedness"]),
    ("survival water supply", dict(subjects=["Water-supply"]),
     "Survival & Preparedness", ["Survival & Preparedness"]),
    ("survival emergency", dict(subjects=["Emergency management", "Disasters"]),
     "Survival & Preparedness", ["Survival & Preparedness"]),

    # --- precedence rules ------------------------------------------------
    ("genre beats general fiction",
     dict(subjects=["Science fiction", "Fiction"], locc=["PS"], shelves=["Category: Novels"]),
     "Science Fiction", ["Science Fiction"]),
    ("genre beats short stories",
     dict(subjects=["Detective and mystery stories", "Short stories"], shelves=["Category: Short Stories"]),
     "Mystery & Detective", ["Mystery & Detective", "Short Stories"]),
    ("short stories can take the second slot",
     dict(subjects=["Short stories", "Fiction"], locc=["PS"]),
     "Short Stories", ["Short Stories", "General Fiction"]),
    ("combined sf-fantasy shelf leans fantasy on a hint",
     dict(subjects=["Magic"], shelves=["Category: Science-Fiction & Fantasy"]),
     "Fantasy & Folklore", ["Fantasy & Folklore"]),
    ("combined sf-fantasy shelf defaults to science fiction",
     dict(subjects=["Space flight"], shelves=["Category: Science-Fiction & Fantasy"]),
     "Science Fiction", ["Science Fiction"]),
    ("natural history is science not history",
     dict(subjects=["Natural history -- Pre-Linnean works"], locc=["QH"]),
     "Science & Nature", ["Science & Nature"]),
    ("locc only fallback", dict(locc=["BX"]), "Religion & Spirituality", ["Religion & Spirituality"]),
    ("locc pz is children", dict(locc=["PZ"]), "Children's & Young Readers", ["Children's & Young Readers"]),
]


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_classify_cases(case):
    _name, kwargs, first, required = case
    result = run(**kwargs)
    assert result, "expected a category for %s" % _name
    assert result[0] == first, result
    for name in required:
        assert name in result, result


def test_every_category_is_covered_by_the_table():
    covered = set()
    for _name, kwargs, _first, required in CASES:
        covered.update(required)
    missing = set(CATEGORIES) - covered - {"Comics"}
    assert not missing, "no test case produces: %s" % sorted(missing)


def test_never_more_than_the_cap():
    for _name, kwargs, _first, _required in CASES:
        assert len(run(**kwargs)) <= MAX_CATEGORIES_PER_BOOK


def test_cap_is_two_even_with_many_signals():
    result = run(
        subjects=["Science fiction", "Detective and mystery stories", "Love stories", "Poetry"],
        locc=["PS"],
        shelves=["Category: Novels", "Category: Adventure", "Horror"],
    )
    assert len(result) == 2


def test_empty_result_when_nothing_is_confident():
    assert run(title="Untitled", subjects=[], locc=[], shelves=[]) == []
    assert run(title="Dante", subjects=["Miscellaneous"], locc=[], shelves=["Harvard Classics"]) == []


def test_never_returns_an_umbrella_tag_or_comics():
    for _name, kwargs, _first, _required in CASES:
        for name in run(**kwargs):
            assert name in CATEGORIES
            assert name not in UMBRELLA_TAGS
    assert "Comics" not in run(subjects=["Comic books, strips, etc"], locc=["PN"])


# --- regressions: the four systematic faults found in the first plan -----

FICTION_GUARD_CASES = [
    # (id, kwargs, expected result)
    (
        "historical novel is not History",  # "St. George and St. Michael"
        dict(
            title="St. George and St. Michael, Volume 2",
            subjects=["Great Britain -- History -- Civil War, 1642-1649 -- Fiction"],
            locc=["PR"],
            shelves=["Category: Novels", "Category: Historical Novels", "Category: History - British"],
        ),
        ["General Fiction"],
    ),
    (
        "boarding-school juvenile fiction is not History",  # "The Master of the Shell"
        dict(
            title="The Master of the Shell",
            subjects=["Boarding schools -- England -- Juvenile fiction", "Students -- Juvenile fiction"],
            locc=["PZ"],
            shelves=["Category: Novels", "Category: History - Schools & Universities"],
        ),
        ["Children's & Young Readers", "General Fiction"],
    ),
    (
        "a novel about homesteading is not Survival",  # "The Homesteader: A Novel"
        dict(
            title="The Homesteader: A Novel",
            subjects=["Frontier and pioneer life -- Fiction", "African Americans -- Fiction"],
            locc=["PS"],
            shelves=["Category: Novels"],
        ),
        ["General Fiction"],
    ),
    (
        "social-life fiction is not Society",  # "Doctor Pascal"
        dict(
            title="Doctor Pascal",
            subjects=["France -- Social life and customs -- 19th century -- Fiction"],
            locc=["PQ"],
            shelves=["Category: Novels", "Category: French Literature"],
        ),
        ["General Fiction"],
    ),
    (
        "war stories are Adventure, never War & Military",
        dict(
            title="With Kitchener in the Soudan",
            subjects=["War stories", "Sudan -- History -- 1899-1956 -- Fiction"],
            locc=["PZ"],
            shelves=["Category: Adventure", "Category: History - Warfare"],
        ),
        ["Adventure & Westerns", "Children's & Young Readers"],
    ),
]


@pytest.mark.parametrize("case", FICTION_GUARD_CASES, ids=[c[0] for c in FICTION_GUARD_CASES])
def test_fiction_guard(case):
    _name, kwargs, expected = case
    result = run(**kwargs)
    assert result == expected
    assert is_fiction(
        subjects=list(kwargs.get("subjects", ())),
        locc=list(kwargs.get("locc", ())),
        bookshelves=list(kwargs.get("shelves", ())),
    )
    for name in result:
        assert name not in NON_FICTION_ONLY, result


def test_science_fiction_never_means_science_and_nature():
    # "Wedding Day" and "Tybalt": Science fiction + Short stories / Time travel.
    assert run(
        title="Wedding Day",
        subjects=["Science fiction", "Short stories", "Psychological fiction", "Women -- Fiction"],
        locc=["PS"],
        shelves=["Category: Science-Fiction & Fantasy"],
    ) == ["Science Fiction", "Short Stories"]
    assert run(
        title="Tybalt",
        subjects=["Science fiction", "Time travel -- Fiction", "Teachers -- Fiction"],
        locc=["PS"],
        shelves=["Category: Science-Fiction & Fantasy", "Category: Novels"],
    ) == ["Science Fiction", "General Fiction"]  # "Time travel" is not Travel
    # And the same care for the other fiction phrases.
    assert "Homestead & Cooking" not in run(subjects=["Domestic fiction"], locc=["PS"])
    assert "Travel & Exploration" not in run(subjects=["Sea stories"], locc=["PZ"])
    assert "Society & Politics" not in run(subjects=["Legal stories"], locc=["PS"])


def test_history_only_from_its_own_locc_or_shelf():
    # "-- History" as a subdivision of someone else's heading must not count.
    assert "History" not in run(
        subjects=["Spanish literature -- History and criticism"], locc=["PQ"]
    )
    assert "History" not in run(subjects=["Medicine -- History"], locc=["R"])
    # The real thing still works, from LoCC or from a History bookshelf.
    assert run(subjects=["Rome -- Antiquities"], locc=["DG"])[0] == "History"
    assert run(subjects=["Anything"], shelves=["Category: History - American"])[0] == "History"


def test_biography_needs_a_form_signal():
    # A personal-name heading, and Calibre's comma-split fragments of one.
    assert "Biography & Letters" not in run(
        subjects=["Zola, Emile", "1840-1902", "France -- Intellectual life"], locc=["PQ"]
    )
    assert run(subjects=["Fiction"], locc=["PS"]) == ["General Fiction"]
    # Explicit form signals still qualify.
    assert run(subjects=["Lincoln, Abraham -- Biography"], locc=["E457"])[0] == "Biography & Letters"
    # A soldier's war memoir legitimately earns both, War leading on support.
    assert "Biography & Letters" in run(subjects=["Soldiers -- Personal narratives"], locc=["D501"])
    assert run(subjects=["Anybody"], locc=["CT"])[0] == "Biography & Letters"


def test_comma_split_tag_fragments_are_ignored_entirely():
    assert run(subjects=["1840-1902", "b 1809", "Jr", "A"], locc=[]) == []
    assert not is_fiction(subjects=["1840-1902"], locc=["PR"])


def test_locc_tx_and_tt_reach_homestead():
    # "Guide to Hotel Housekeeping": TX plus Home economics, against a
    # "Technology" and a "Business" bookshelf.
    assert run(
        title="Guide to Hotel Housekeeping",
        subjects=["Home economics", "Hotels"],
        locc=["TX"],
        shelves=["Technology", "Category: How To ...", "Category: Business/Management"],
    )[0] == "Homestead & Cooking"
    assert run(subjects=["Handicraft"], locc=["TT"])[0] == "Homestead & Cooking"


def test_survival_is_nonfiction_only():
    assert run(subjects=["Camping", "Wilderness survival"], locc=["GV"]) == [
        "Survival & Preparedness"
    ]
    assert "Survival & Preparedness" not in run(
        subjects=["Camping -- Fiction", "Adventure stories"], locc=["PZ"]
    )


def test_combined_sf_fantasy_shelf_never_makes_gothic_horror_science_fiction():
    # Dracula: the PG "Science-Fiction & Fantasy" shelf must lose to the
    # book's own horror/gothic subjects.
    dracula = run(
        title="Dracula",
        subjects=["Horror tales", "Epistolary fiction", "Gothic fiction", "Vampires -- Fiction"],
        locc=["PR"],
        shelves=["Horror", "Category: Science-Fiction & Fantasy", "Category: Novels"],
    )
    assert dracula[0] == "Horror & Gothic"
    assert "Science Fiction" not in dracula
    # "The Old English Baron: a Gothic Story" -- shelf but no SF signal at all.
    baron = run(
        title="The Old English Baron: a Gothic Story",
        subjects=["Gothic fiction", "Great Britain -- History -- Fiction"],
        locc=["PR"],
        shelves=["Category: Science-Fiction & Fantasy", "Category: Novels"],
    )
    assert baron[0] == "Horror & Gothic"
    assert "Science Fiction" not in baron
    # A bare shelf with no signal whatsoever must not invent Science Fiction.
    assert "Science Fiction" not in run(
        title="Untitled", subjects=["Fiction"], locc=["PR"],
        shelves=["Category: Science-Fiction & Fantasy"],
    )
    # A positive SF signal still works, and a fantasy signal still wins.
    assert run(subjects=["Interplanetary voyages -- Fiction"],
               shelves=["Category: Science-Fiction & Fantasy"])[0] == "Science Fiction"
    assert run(subjects=["Wizards -- Fiction", "Magic"],
               shelves=["Category: Science-Fiction & Fantasy"])[0] == "Fantasy & Folklore"


def test_science_idioms_are_not_science_and_nature():
    # Jomini, The Art of War.
    jomini = run(
        title="The Art of War",
        subjects=["Military art and science -- Early works to 1800", "War -- Early works to 1800"],
        locc=["U"],
        shelves=["Category: History - Warfare", "Category: History - Ancient"],
    )
    assert "Science & Nature" not in jomini
    assert jomini[0] == "War & Military"
    assert "Art & Music" not in jomini  # "Military ART and science" is not art
    assert run(subjects=["Military art and science"], locc=["U"]) == ["War & Military"]
    assert "Science & Nature" not in run(subjects=["Naval art and science"], locc=["V"])
    assert run(subjects=["Social sciences"], locc=["H"])[0] == "Society & Politics"
    assert run(subjects=["Christian Science"], locc=["BX"])[0] == "Religion & Spirituality"
    assert run(subjects=["Domestic science"], locc=["TX"])[0] == "Homestead & Cooking"


def test_walden_and_leaves_of_grass():
    # "Wilderness areas" is a conservation heading, not a survival skill.
    walden = run(
        title="Walden, and On The Duty Of Civil Disobedience",
        subjects=[
            "Civil disobedience",
            "Authors, American -- 19th century -- Biography",
            "Wilderness areas -- Massachusetts -- Walden Woods",
            "Natural history",
        ],
        locc=["PS"],
        shelves=["Category: American Literature", "Category: Philosophy & Ethics"],
    )
    assert "Survival & Preparedness" not in walden
    assert walden[0] in ("Philosophy", "Science & Nature")
    # A generic "this is literature" shelf must not add General Fiction to a
    # book of poetry.
    assert run(
        title="Leaves of Grass",
        subjects=["American poetry -- 19th century"],
        locc=["PS"],
        shelves=["Poetry", "Category: Poetry", "Category: American Literature",
                 "Category: Classics of Literature"],
    ) == ["Poetry"]


def test_no_category_or_umbrella_tag_contains_a_comma():
    # Calibre rewrites a comma inside a tag to a semicolon, so a comma in the
    # vocabulary would silently split every book's category in two.
    assert [c for c in CATEGORIES if "," in c] == []
    assert [t for t in UMBRELLA_TAGS if "," in t] == []


def test_results_are_always_in_the_vocabulary():
    result = run(subjects=["Cooking", "Hunting", "Poetry"], locc=["TX"], shelves=["Category: Poetry"])
    assert all(name in CATEGORIES for name in result)
