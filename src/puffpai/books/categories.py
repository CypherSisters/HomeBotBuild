"""The controlled category vocabulary for the household book library.

Categories live in Calibre's TAGS field so that
Calibre-web's own Categories page IS this list; the long Library of Congress
subject strings live in the custom column ``#subjects`` instead. A book carries
at most MAX_CATEGORIES_PER_BOOK categories, most specific first.

Umbrella tags are provenance, not categories: they stay in TAGS next to the
categories and are never removed by curation.
"""

CATEGORIES = (
    "General Fiction",
    "Science Fiction",
    "Fantasy & Folklore",
    "Mystery & Detective",
    "Horror & Gothic",
    "Adventure & Westerns",
    "Romance",
    "Humor & Satire",
    "Short Stories",
    "Children's & Young Readers",
    "Poetry",
    "Drama & Plays",
    "History",
    "War & Military",
    "Biography & Letters",
    "Travel & Exploration",
    "Philosophy",
    "Religion & Spirituality",
    "Science & Nature",
    "Technology & Engineering",
    "Medicine & Health",
    "Homestead & Cooking",
    "Survival & Preparedness",
    "Art & Music",
    "Language & Reference",
    "Society & Politics",
    "Magazines & Periodicals",
    "Comics",
)

UMBRELLA_TAGS = ("My Library", "Comics", "Project Gutenberg")

MAX_CATEGORIES_PER_BOOK = 2

#: Calibre rewrites a comma inside a tag to a semicolon, so no category may contain one.
assert not any("," in c for c in CATEGORIES), "category names must be comma-free"

#: Calibre custom column that holds the Library of Congress subject strings.
SUBJECTS_COLUMN_LABEL = "subjects"
