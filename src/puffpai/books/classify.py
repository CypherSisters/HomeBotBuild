"""Pure classification of a book into the controlled category vocabulary.

Stdlib only, no I/O, no state: :func:`classify` maps the metadata a book
already carries (title, authors, Library of Congress subject strings, LoCC
classes, Project Gutenberg bookshelves) onto at most
``MAX_CATEGORIES_PER_BOOK`` names from :data:`CATEGORIES`, most specific first.

Signal priority, strongest first:

1. Project Gutenberg bookshelves -- a human already shelved the book.
2. Subject keyword rules over the LoC subject strings.
3. The LoCC class letters, which are only ever a coarse fallback.

Generic buckets ("General Fiction", "Short Stories") are always sorted behind
a real genre, so a genre beats "General Fiction" no matter which signal found
it. When nothing is confident the result is ``[]``: an empty shelf is better
than a wrong one.
"""

from __future__ import annotations

import re
import unicodedata

from .categories import CATEGORIES, MAX_CATEGORIES_PER_BOOK, UMBRELLA_TAGS

__all__ = ["classify", "is_fiction"]

# --- category shorthands -------------------------------------------------

FICTION = "General Fiction"
SCIFI = "Science Fiction"
FANTASY = "Fantasy & Folklore"
MYSTERY = "Mystery & Detective"
HORROR = "Horror & Gothic"
ADVENTURE = "Adventure & Westerns"
ROMANCE = "Romance"
HUMOR = "Humor & Satire"
SHORTS = "Short Stories"
KIDS = "Children's & Young Readers"
POETRY = "Poetry"
DRAMA = "Drama & Plays"
HISTORY = "History"
WAR = "War & Military"
BIOGRAPHY = "Biography & Letters"
TRAVEL = "Travel & Exploration"
PHILOSOPHY = "Philosophy"
RELIGION = "Religion & Spirituality"
SCIENCE = "Science & Nature"
TECH = "Technology & Engineering"
MEDICINE = "Medicine & Health"
HOMESTEAD = "Homestead & Cooking"
SURVIVAL = "Survival & Preparedness"
ART = "Art & Music"
REFERENCE = "Language & Reference"
SOCIETY = "Society & Politics"
PERIODICALS = "Magazines & Periodicals"
COMICS = "Comics"

#: Buckets that must never outrank a real genre. Lower number sorts first.
_GENERIC_RANK = {SHORTS: 1, FICTION: 2}

_UMBRELLA = set(UMBRELLA_TAGS)
_VALID = set(CATEGORIES)


def _fold(text: str) -> str:
    """Lowercase, strip accents and collapse whitespace/punctuation runs."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower().replace("&", " and ")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text)).strip()


# --- (a) Project Gutenberg bookshelves -----------------------------------
#
# Every bookshelf carrying >= 40 books in pg_catalog.csv is mapped here (keys
# are _fold()ed, the "Category: " prefix stripped). Shelves deliberately left
# out -- "Harvard Classics", "Best Books Ever Listings", "Banned Books...",
# "Nobel Prizes in Literature", "Bestsellers, American, 1895-1923", the plain
# country shelves, "Christmas", "How To ...", "Sports/Hobbies", "DE Sachbuch"
# -- describe provenance or a mixed bag, not a subject: other signals decide.

_SHELVES: dict[str, tuple[str, ...]] = {
    # fiction, generic
    "novels": (FICTION,),
    "british literature": (FICTION,),
    "american literature": (FICTION,),
    "french literature": (FICTION,),
    "german literature": (FICTION,),
    "russian literature": (FICTION,),
    "classics of literature": (FICTION,),
    "literature other": (FICTION,),
    "historical novels": (FICTION,),
    "historical fiction": (FICTION,),
    "fr litterature": (FICTION,),
    "de prosa": (FICTION,),
    "it romanzi": (FICTION,),
    "fr nouveautes": (FICTION,),
    "short stories": (SHORTS,),
    "fr nouvelles": (SHORTS,),
    "it racconti": (SHORTS,),
    # genres
    "adventure": (ADVENTURE,),
    "western": (ADVENTURE,),
    "pirates buccaneers corsairs etc": (ADVENTURE,),
    "science fiction": (SCIFI,),
    "science fiction by women": (SCIFI,),
    "astounding stories": (SCIFI, PERIODICALS),
    "fantasy": (FANTASY,),
    "mythology legends and folklore": (FANTASY,),
    "folklore": (FANTASY,),
    "crime thrillers and mystery": (MYSTERY,),
    "detective fiction": (MYSTERY,),
    "horror": (HORROR,),
    "romance": (ROMANCE,),
    "fr seduction et libertinage": (ROMANCE,),
    "humour": (HUMOR,),
    "humor": (HUMOR,),
    "fr humour": (HUMOR,),
    "punch": (HUMOR, PERIODICALS),
    # form
    "poetry": (POETRY,),
    "fr poesie": (POETRY,),
    "de lyrik": (POETRY,),
    "it poesia": (POETRY,),
    "pt poesia": (POETRY,),
    "plays films dramas": (DRAMA,),
    "one act plays": (DRAMA,),
    "fr theatre": (DRAMA,),
    "de drama": (DRAMA,),
    "it teatro in prosa": (DRAMA,),
    # young readers
    "children and young adult reading": (KIDS,),
    "children s book series": (KIDS,),
    "children s fiction": (KIDS,),
    "children s literature": (KIDS,),
    "children s picture books": (KIDS,),
    "children s instructional books": (KIDS,),
    "children s anthologies": (KIDS,),
    "children s myths fairy tales etc": (KIDS, FANTASY),
    "children s history": (KIDS, HISTORY),
    "school stories": (KIDS,),
    "harper s young people": (KIDS, PERIODICALS),
    "the nursery": (KIDS, PERIODICALS),
    "the girls own paper": (KIDS, PERIODICALS),
    # history and war
    "history modern 1750": (HISTORY,),
    "history american": (HISTORY,),
    "history european": (HISTORY,),
    "history other": (HISTORY,),
    "history british": (HISTORY,),
    "history ancient": (HISTORY,),
    "history early modern c 1450 1750": (HISTORY,),
    "history medieval middle ages": (HISTORY,),
    "history royalty": (HISTORY,),
    "history schools and universities": (HISTORY,),
    "history religious": (HISTORY, RELIGION),
    "archaeology and anthropology": (HISTORY,),
    "anthropology": (HISTORY,),
    "classical antiquity": (HISTORY,),
    "native america": (HISTORY,),
    "slavery": (HISTORY, SOCIETY),
    "fr histoire": (HISTORY,),
    "it storia": (HISTORY,),
    "history warfare": (WAR,),
    "world war i": (WAR,),
    "world war ii": (WAR,),
    "us civil war": (WAR,),
    "boer war": (WAR,),
    # life writing
    "biographies": (BIOGRAPHY,),
    "journals": (BIOGRAPHY,),
    "fr biographie memoires journal intime correspondance": (BIOGRAPHY,),
    "pt biografia": (BIOGRAPHY,),
    # travel
    "travel writing": (TRAVEL,),
    "travel": (TRAVEL,),
    "de aarde en haar volken": (TRAVEL,),
    "fr voyages et pays": (TRAVEL,),
    # thought
    "philosophy and ethics": (PHILOSOPHY,),
    "philosophy": (PHILOSOPHY,),
    "religion spirituality": (RELIGION,),
    "christianity": (RELIGION,),
    "judaism": (RELIGION,),
    "latter day saints": (RELIGION,),
    "fr philosophie religion et morale": (PHILOSOPHY, RELIGION),
    # science, tech, medicine
    "science biology": (SCIENCE,),
    "biology": (SCIENCE,),
    "science physics": (SCIENCE,),
    "science chemistry biochemistry": (SCIENCE,),
    "science earth agricultural farming": (SCIENCE,),
    "nature gardening animals": (SCIENCE,),
    "animal": (SCIENCE,),
    "animals wild": (SCIENCE,),
    "animals wild mammals": (SCIENCE,),
    "animals wild birds": (SCIENCE,),
    "animals domestic": (SCIENCE,),
    "mathematics": (SCIENCE,),
    "environmental issues": (SCIENCE, SOCIETY),
    "women in science technology engineering and mathematics": (SCIENCE,),
    "fr sciences et techniques": (SCIENCE,),
    "scientific american": (SCIENCE, PERIODICALS),
    "engineering and technology": (TECH,),
    "technology": (TECH,),
    "health and medicine": (MEDICINE,),
    "medicine": (MEDICINE,),
    "nutrition": (MEDICINE,),
    "psychiatry psychology": (MEDICINE,),
    "psychology": (MEDICINE,),
    "drugs alcohol pharmacology": (MEDICINE,),
    # homestead
    "cookbooks and cooking": (HOMESTEAD,),
    "cooking and drinking": (HOMESTEAD,),
    "horticulture": (HOMESTEAD,),
    # arts
    "art": (ART,),
    "architecture": (ART,),
    "music": (ART,),
    "opera": (ART,),
    "masterpieces in colour": (ART,),
    "fr illustrateurs": (ART,),
    "fashion": (ART,),
    "crafts": (ART,),
    "movie books": (ART,),
    # society
    "politics": (SOCIETY,),
    "fr politique": (SOCIETY,),
    "law and criminology": (SOCIETY,),
    "economics": (SOCIETY,),
    "business management": (SOCIETY,),
    "sociology": (SOCIETY,),
    "gender and sexuality studies": (SOCIETY,),
    "sexuality and erotica": (SOCIETY,),
    "parenthood and family relations": (SOCIETY,),
    "old age and the elderly": (SOCIETY,),
    "research methods statistics information sys": (SOCIETY,),
    "journalism media writing": (SOCIETY,),
    "fr peuples et societes": (SOCIETY,),
    "fr femmes": (SOCIETY,),
    # reference and language
    "encyclopedias dictionaries reference": (REFERENCE,),
    "language and communication": (REFERENCE,),
    "language education": (REFERENCE,),
    "teaching and education": (REFERENCE,),
    "esperanto": (REFERENCE,),
    "fr langues": (REFERENCE,),
    "reports and conference proceedings": (REFERENCE,),
    # periodicals
    "l illustration": (PERIODICALS,),
    "notes and queries": (PERIODICALS,),
    "the mirror of literature amusement and instruction": (PERIODICALS,),
    "the atlantic monthly": (PERIODICALS,),
    "chambers s edinburgh journal": (PERIODICALS,),
    "blackwood s edinburgh magazine": (PERIODICALS,),
    "the american missionary": (PERIODICALS, RELIGION),
    "lippincott s magazine": (PERIODICALS,),
    "the mentor": (PERIODICALS,),
    "the great round world and what is going on in it": (PERIODICALS,),
    "contemporary reviews": (PERIODICALS,),
    "de kinderbuch": (KIDS,),
    "fr jeunesse": (KIDS,),
}

#: "Science-Fiction & Fantasy" is one shelf for two categories; the subjects
#: decide which one leads.
_SF_FANTASY_SHELF = "science fiction and fantasy"
_FANTASY_HINTS = (
    "fantasy",
    "fairy",
    "magic",
    "mythology",
    "folklore",
    "dragon",
    "wizard",
    "witch",
    "imaginary places",
)

#: A gothic or supernatural-horror signal. The combined shelf must never
#: make Dracula science fiction.
_HORROR_HINTS = (
    "horror",
    "gothic",
    "ghost",
    "vampire",
    "werewolf",
    "supernatural",
    "occult",
    "haunted",
    "monsters",
)

#: A positive science-fiction signal. Without one of these (or a fantasy or
#: horror signal) the combined shelf says nothing and is not used at all.
_SF_HINTS = (
    "science fiction",
    "space",
    "interplanetary",
    "planets",
    "planet",
    "robot",
    "android",
    "time travel",
    "future",
    "utopia",
    "dystopia",
    "invention",
    "scientists",
    "mars ",
    "moon",
    "rocket",
    "extraterrestrial",
)


def _combined_shelf_category(hint_blob: str, sf_blob: str) -> str | None:
    """Resolve "Science-Fiction & Fantasy" against the book's own signals."""
    if any(h in hint_blob for h in _HORROR_HINTS):
        return HORROR
    if any(h in hint_blob for h in _FANTASY_HINTS):
        return FANTASY
    if any(h in sf_blob for h in _SF_HINTS):
        return SCIFI
    return None


# --- (b) subject keyword rules -------------------------------------------
#
# Ordered: the first pattern that matches contributes its category, but every
# rule is evaluated so a book can collect two. Patterns run against a folded
# subject string ("Detective and mystery stories" -> "detective and mystery
# stories"), so they are plain lowercase substrings.

_SUBJECT_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    # Survival & Preparedness -- checked first so it can take
    # the lead slot ahead of the Homestead/Medicine bucket it often pairs with.
    ("survival skills", (SURVIVAL,)),
    ("wilderness survival", (SURVIVAL,)),
    ("survival after airplane accidents", (SURVIVAL,)),
    ("camping", (SURVIVAL,)),
    ("campcraft", (SURVIVAL,)),
    ("woodcraft", (SURVIVAL,)),
    ("outdoor life", (SURVIVAL,)),
    ("outdoor recreation", (SURVIVAL,)),
    ("boy scouts", (SURVIVAL,)),
    ("girl scouts", (SURVIVAL,)),
    ("scouting", (SURVIVAL,)),
    ("scouts and scouting", (SURVIVAL,)),
    ("first aid", (SURVIVAL, MEDICINE)),
    ("rescue work", (SURVIVAL,)),
    ("lifesaving", (SURVIVAL,)),
    ("hunting", (SURVIVAL,)),
    ("trapping", (SURVIVAL,)),
    ("fowling", (SURVIVAL,)),
    ("game and game birds", (SURVIVAL,)),
    ("fishing", (SURVIVAL,)),
    ("fisheries", (SURVIVAL,)),
    ("canning and preserving", (SURVIVAL, HOMESTEAD)),
    ("food preservation", (SURVIVAL, HOMESTEAD)),
    ("food supply", (SURVIVAL,)),
    ("drying of food", (SURVIVAL, HOMESTEAD)),
    ("smoked foods", (SURVIVAL, HOMESTEAD)),
    ("emergency", (SURVIVAL,)),
    ("disaster", (SURVIVAL,)),
    ("disasters", (SURVIVAL,)),
    ("civil defense", (SURVIVAL,)),
    ("famines", (SURVIVAL,)),
    ("shipwrecks", (SURVIVAL, ADVENTURE)),
    ("navigation", (SURVIVAL,)),
    ("knots and splices", (SURVIVAL,)),
    ("signals and signaling", (SURVIVAL,)),
    ("orienteering", (SURVIVAL,)),
    ("log cabins", (SURVIVAL,)),
    ("shelters", (SURVIVAL,)),
    ("tents", (SURVIVAL,)),
    ("wild plants edible", (SURVIVAL,)),
    ("edible plants", (SURVIVAL,)),
    ("plants edible", (SURVIVAL,)),
    ("wild foods", (SURVIVAL,)),
    ("foraging", (SURVIVAL,)),
    ("water supply", (SURVIVAL,)),
    ("wells", (SURVIVAL,)),
    ("self reliance", (SURVIVAL,)),
    ("frontier and pioneer life", (SURVIVAL,)),
    # Homestead
    ("cooking", (HOMESTEAD,)),
    ("cookbooks", (HOMESTEAD,)),
    ("cookery", (HOMESTEAD,)),
    ("recipes", (HOMESTEAD,)),
    ("baking", (HOMESTEAD,)),
    ("bread", (HOMESTEAD,)),
    ("brewing", (HOMESTEAD,)),
    ("wine and wine making", (HOMESTEAD,)),
    ("gardening", (HOMESTEAD,)),
    ("gardens", (HOMESTEAD,)),
    ("vegetable gardening", (HOMESTEAD,)),
    ("horticulture", (HOMESTEAD,)),
    ("agriculture", (HOMESTEAD,)),
    ("farm", (HOMESTEAD,)),
    ("farming", (HOMESTEAD,)),
    ("livestock", (HOMESTEAD,)),
    ("poultry", (HOMESTEAD,)),
    ("bee culture", (HOMESTEAD,)),
    ("beekeeping", (HOMESTEAD,)),
    ("dairying", (HOMESTEAD,)),
    ("home economics", (HOMESTEAD,)),
    ("household", (HOMESTEAD,)),
    ("canning", (HOMESTEAD,)),
    ("sewing", (HOMESTEAD,)),
    ("soap", (HOMESTEAD,)),
    # genres
    ("science fiction", (SCIFI,)),
    ("interplanetary voyages", (SCIFI,)),
    ("space flight", (SCIFI,)),
    ("life on other planets", (SCIFI,)),
    ("time travel", (SCIFI,)),
    ("dystopias", (SCIFI,)),
    ("robots", (SCIFI,)),
    ("fantasy fiction", (FANTASY,)),
    ("fairy tales", (FANTASY,)),
    ("folklore", (FANTASY,)),
    ("legends", (FANTASY,)),
    ("mythology", (FANTASY,)),
    ("magic", (FANTASY,)),
    ("imaginary places", (FANTASY,)),
    ("detective and mystery stories", (MYSTERY,)),
    ("detectives", (MYSTERY,)),
    ("mystery fiction", (MYSTERY,)),
    ("crime", (MYSTERY,)),
    ("murder", (MYSTERY,)),
    ("horror tales", (HORROR,)),
    ("ghost stories", (HORROR,)),
    ("ghosts", (HORROR,)),
    ("vampires", (HORROR,)),
    ("gothic fiction", (HORROR,)),
    ("monsters", (HORROR,)),
    ("western stories", (ADVENTURE,)),
    ("adventure stories", (ADVENTURE,)),
    ("sea stories", (ADVENTURE,)),
    ("pirates", (ADVENTURE,)),
    ("treasure troves", (ADVENTURE,)),
    ("love stories", (ROMANCE,)),
    ("courtship", (ROMANCE,)),
    ("man woman relationships", (ROMANCE,)),
    ("humor", (HUMOR,)),
    ("humorous stories", (HUMOR,)),
    ("wit and humor", (HUMOR,)),
    ("satire", (HUMOR,)),
    ("parodies", (HUMOR,)),
    ("short stories", (SHORTS,)),
    ("tales", (SHORTS,)),
    ("children s stories", (KIDS,)),
    ("children s poetry", (KIDS, POETRY)),
    ("children s literature", (KIDS,)),
    ("juvenile fiction", (KIDS,)),
    ("juvenile literature", (KIDS,)),
    ("picture books for children", (KIDS,)),
    ("toy and movable books", (KIDS,)),
    ("poetry", (POETRY,)),
    ("poems", (POETRY,)),
    ("sonnets", (POETRY,)),
    ("epic poetry", (POETRY,)),
    ("drama", (DRAMA,)),
    ("plays", (DRAMA,)),
    ("english drama", (DRAMA,)),
    ("tragedies", (DRAMA,)),
    ("comedies", (DRAMA,)),
    ("theater", (DRAMA,)),
    ("fiction", (FICTION,)),
    # non-fiction
    # Biography needs an explicit FORM signal; a personal-name heading such
    # as "Zola, Emile, 1840-1902" is about a person, not a life story.
    ("biography", (BIOGRAPHY,)),
    ("biographies", (BIOGRAPHY,)),
    ("autobiography", (BIOGRAPHY,)),
    ("autobiographies", (BIOGRAPHY,)),
    ("autobiographical", (BIOGRAPHY,)),
    ("correspondence", (BIOGRAPHY,)),
    ("letters", (BIOGRAPHY,)),
    ("diaries", (BIOGRAPHY,)),
    ("memoirs", (BIOGRAPHY,)),
    ("personal narratives", (BIOGRAPHY,)),
    # History comes only from a C/D/E/F LoCC class or a History bookshelf:
    # "-- History" is a subdivision of somebody else's heading ("Medicine --
    # History", "Spanish literature -- History and criticism") far more often
    # than it is the book's own subject.
    ("war", (WAR,)),
    ("military", (WAR,)),
    ("soldiers", (WAR,)),
    ("battles", (WAR,)),
    ("campaigns", (WAR,)),
    ("naval", (WAR,)),
    ("armies", (WAR,)),
    ("weapons", (WAR,)),
    ("firearms", (WAR,)),
    ("travel", (TRAVEL,)),
    ("voyages and travels", (TRAVEL,)),
    ("description and travel", (TRAVEL,)),
    ("discovery and exploration", (TRAVEL,)),
    ("expeditions", (TRAVEL,)),
    ("philosophy", (PHILOSOPHY,)),
    ("ethics", (PHILOSOPHY,)),
    ("logic", (PHILOSOPHY,)),
    ("metaphysics", (PHILOSOPHY,)),
    ("psychology", (MEDICINE,)),
    ("bible", (RELIGION,)),
    ("christian", (RELIGION,)),
    ("church", (RELIGION,)),
    ("theology", (RELIGION,)),
    ("sermons", (RELIGION,)),
    ("prayer", (RELIGION,)),
    ("religion", (RELIGION,)),
    ("spiritualism", (RELIGION,)),
    ("islam", (RELIGION,)),
    ("buddhism", (RELIGION,)),
    ("hinduism", (RELIGION,)),
    ("judaism", (RELIGION,)),
    ("mysticism", (RELIGION,)),
    ("saints", (RELIGION,)),
    ("science", (SCIENCE,)),
    ("natural history", (SCIENCE,)),
    ("botany", (SCIENCE,)),
    ("zoology", (SCIENCE,)),
    ("birds", (SCIENCE,)),
    ("insects", (SCIENCE,)),
    ("geology", (SCIENCE,)),
    ("astronomy", (SCIENCE,)),
    ("physics", (SCIENCE,)),
    ("chemistry", (SCIENCE,)),
    ("mathematics", (SCIENCE,)),
    ("evolution", (SCIENCE,)),
    ("natural theology", (SCIENCE, RELIGION)),
    ("engineering", (TECH,)),
    ("machinery", (TECH,)),
    ("technology", (TECH,)),
    ("inventions", (TECH,)),
    ("railroads", (TECH,)),
    ("aeronautics", (TECH,)),
    ("automobiles", (TECH,)),
    ("electricity", (TECH,)),
    ("radio", (TECH,)),
    ("photography", (TECH, ART)),
    ("building", (TECH,)),
    ("carpentry", (TECH,)),
    ("medicine", (MEDICINE,)),
    ("medical", (MEDICINE,)),
    ("diseases", (MEDICINE,)),
    ("hygiene", (MEDICINE,)),
    ("surgery", (MEDICINE,)),
    ("nursing", (MEDICINE,)),
    ("health", (MEDICINE,)),
    ("anatomy", (MEDICINE,)),
    ("herbs", (MEDICINE, HOMESTEAD)),
    ("materia medica", (MEDICINE,)),
    ("art", (ART,)),
    ("painting", (ART,)),
    ("sculpture", (ART,)),
    ("architecture", (ART,)),
    ("music", (ART,)),
    ("musicians", (ART,)),
    ("composers", (ART,)),
    ("opera", (ART,)),
    ("decoration and ornament", (ART,)),
    ("politics and government", (SOCIETY,)),
    ("political science", (SOCIETY,)),
    ("social sciences", (SOCIETY,)),
    ("christian science", (RELIGION,)),
    ("domestic science", (HOMESTEAD,)),
    ("law", (SOCIETY,)),
    ("economics", (SOCIETY,)),
    ("commerce", (SOCIETY,)),
    ("finance", (SOCIETY,)),
    ("socialism", (SOCIETY,)),
    ("social", (SOCIETY,)),
    ("labor", (SOCIETY,)),
    ("women s rights", (SOCIETY,)),
    ("slavery", (SOCIETY,)),
    ("education", (REFERENCE,)),
    ("dictionaries", (REFERENCE,)),
    ("encyclopedias", (REFERENCE,)),
    ("language", (REFERENCE,)),
    ("grammar", (REFERENCE,)),
    ("english language", (REFERENCE,)),
    ("bibliography", (REFERENCE,)),
    ("periodicals", (PERIODICALS,)),
    ("magazines", (PERIODICALS,)),
    ("newspapers", (PERIODICALS,)),
    ("comic books strips etc", (COMICS,)),
)

#: Phrases that must NOT satisfy the bare needle they contain: a book about
#: "Natural history" is Science, not History, and "Political science" is
#: Society, not Science. The phrase is blanked out before the needle is tried.
_SUBJECT_EXCLUSIONS: dict[str, tuple[str, ...]] = {
    "history": ("natural history", "life history", "case history", "family history"),
    "science": (
        "military art and science",
        "naval art and science",
        "art and science",
        "political science",
        "social science",
        "social sciences",
        "christian science",
        "domestic science",
        "mental science",
        "moral science",
    ),
    "art": ("art of war", "military art and science", "naval art and science", "art and science"),
    "economics": ("home economics", "domestic economics"),
    # The combined BISAC-style subject is resolved like the combined shelf.
    "science fiction": (_SF_FANTASY_SHELF,),
}

#: Fiction genre phrases that must never feed a NON-fiction rule: "Science
#: fiction" is not Science, "Domestic fiction" is not Homestead, "Sea stories"
#: is not Travel, "Time travel -- Fiction" is not Travel. They are blanked out
#: of the blob before any non-fiction needle is tried.
_FICTION_PHRASES = (
    "science fiction",
    "domestic fiction",
    "medical fiction",
    "legal stories",
    "sea stories",
    "western stories",
    "war stories",
    "historical fiction",
    "detective and mystery stories",
    "time travel",
    "religious fiction",
    "political fiction",
    "psychological fiction",
    "christmas stories",
    "love stories",
)

#: Topic needles that only mean a GENRE inside a work of fiction. "Space
#: flight" and "Life on other planets" are Science in a NASA report; "Crime"
#: is criminology, not a whodunnit, outside fiction.
_FICTION_ONLY_NEEDLES = frozenset(
    {
        "interplanetary voyages",
        "space flight",
        "life on other planets",
        "time travel",
        "dystopias",
        "robots",
        "magic",
        "ghosts",
        "monsters",
        "vampires",
        "crime",
        "murder",
        "pirates",
        "treasure troves",
        "courtship",
        "man woman relationships",
    }
)

#: The only categories a work of fiction may ever receive (rule 1).
_FICTION_FAMILY = frozenset(
    {FICTION, SCIFI, FANTASY, MYSTERY, HORROR, ADVENTURE, ROMANCE, HUMOR, SHORTS, KIDS, POETRY, DRAMA}
)

#: needle -> (compiled word-boundary matcher, categories, is a fiction rule)
_SUBJECT_MATCHERS = tuple(
    (
        needle,
        re.compile(r"\b%s\b" % re.escape(needle).replace(r"\ ", " ")),
        names,
        all(name in _FICTION_FAMILY for name in names),
    )
    for needle, names in _SUBJECT_RULES
)


def _mask(blob: str, phrases) -> str:
    for phrase in phrases:
        blob = blob.replace(phrase, " ")
    return blob


def _subject_hit(needle: str, matcher: "re.Pattern[str]", blob: str) -> bool:
    excluded = _SUBJECT_EXCLUSIONS.get(needle)
    if excluded:
        blob = _mask(blob, excluded)
    return bool(matcher.search(blob))


# --- (c) LoCC classes ----------------------------------------------------

_LOCC_TWO: dict[str, str] = {
    "AP": PERIODICALS,
    "AN": PERIODICALS,
    "AC": REFERENCE,
    "AE": REFERENCE,
    "AG": REFERENCE,
    "AI": REFERENCE,
    "AM": ART,
    "AS": REFERENCE,
    "AY": REFERENCE,
    "AZ": REFERENCE,
    "BL": RELIGION,
    "BM": RELIGION,
    "BP": RELIGION,
    "BQ": RELIGION,
    "BR": RELIGION,
    "BS": RELIGION,
    "BT": RELIGION,
    "BV": RELIGION,
    "BX": RELIGION,
    "CT": BIOGRAPHY,
    "GR": FANTASY,
    "GN": HISTORY,
    "GV": SOCIETY,
    "PB": REFERENCE,
    "PC": REFERENCE,
    "PD": REFERENCE,
    "PE": REFERENCE,
    "PF": REFERENCE,
    "PZ": KIDS,
    "SB": HOMESTEAD,
    "SD": HOMESTEAD,
    "SF": HOMESTEAD,
    "SH": HOMESTEAD,
    "SK": SURVIVAL,
    "TT": HOMESTEAD,
    "TX": HOMESTEAD,
}

_LOCC_ONE: dict[str, str] = {
    "A": REFERENCE,
    "B": PHILOSOPHY,
    "C": HISTORY,
    "D": HISTORY,
    "E": HISTORY,
    "F": HISTORY,
    "G": TRAVEL,
    "H": SOCIETY,
    "J": SOCIETY,
    "K": SOCIETY,
    "L": SOCIETY,
    "M": ART,
    "N": ART,
    # P literature classes: the books filed here are the literature itself,
    # not language study, so they land in General Fiction and let a genre
    # signal outrank them. The pure language classes (PB-PF) are above.
    "P": FICTION,
    "Q": SCIENCE,
    "R": MEDICINE,
    "S": HOMESTEAD,
    "T": TECH,
    "U": WAR,
    "V": WAR,
    "Z": REFERENCE,
}

#: Classes too broad to outrank a real signal: GV (recreation -- sport,
#: games, camping, dance) and, for non-fiction, P* (all of literature).
_WEAK_LOCC_CLASSES = frozenset({"GV"})

_LOCC_SPLIT = re.compile(r"^([A-Za-z]+)\s*(\d*)")


def _locc_is_weak(raw: str) -> bool:
    match = _LOCC_SPLIT.match((raw or "").strip())
    return bool(match) and match.group(1).upper()[:2] in _WEAK_LOCC_CLASSES


def _locc_category(raw: str) -> str | None:
    match = _LOCC_SPLIT.match((raw or "").strip())
    if not match:
        return None
    letters = match.group(1).upper()
    number = int(match.group(2)) if match.group(2) else 0
    # D501-D680 is World War I, D731-D838 World War II.
    if letters == "D" and (501 <= number <= 680 or 731 <= number <= 838):
        return WAR
    if letters[:2] in _LOCC_TWO:
        return _LOCC_TWO[letters[:2]]
    return _LOCC_ONE.get(letters[:1])


# --- the classifier ------------------------------------------------------


def _shelf_key(shelf: str) -> str:
    folded = _fold(shelf)
    if folded.startswith("category "):
        folded = folded[len("category "):]
    return folded


#: Bookshelves that say "this is a work of fiction" on their own.
_FICTION_SHELVES = frozenset(
    {
        "novels",
        "historical novels",
        "historical fiction",
        "science fiction",
        "science fiction and fantasy",
        "science fiction by women",
        "astounding stories",
        "fantasy",
        "horror",
        "romance",
        "western",
        "adventure",
        "detective fiction",
        "crime thrillers and mystery",
        "short stories",
        "children s fiction",
        "children s book series",
        "children s picture books",
        "school stories",
        "fr nouvelles",
        "it racconti",
        "it romanzi",
        "de prosa",
        "fr nouveautes",
        "pirates buccaneers corsairs etc",
        "mythology legends and folklore",
    }
)

#: Words that mark a subject heading as describing fiction.
_FICTION_SUBJECT_RE = re.compile(r"\b(fiction|stories|tales|novel|novels)\b")

#: Bare date ranges and other fragments left behind when Calibre split a
#: personal-name heading ("Zola, Emile, 1840-1902") on its commas.
_TAG_FRAGMENT_RE = re.compile(r"^(?:\d{3,4}(?:\s*-\s*\d{0,4})?|b \d{3,4}|d \d{3,4}|jr|sr|[a-z])$")


def _clean_subjects(subjects) -> list[str]:
    """Drop comma-split junk fragments; keep real headings."""
    kept = []
    for raw in subjects or []:
        folded = _fold(raw)
        if not folded or _TAG_FRAGMENT_RE.match(folded):
            continue
        kept.append(raw)
    return kept


def is_fiction(
    *,
    title: str = "",
    subjects: list[str] | None = None,
    locc: list[str] | None = None,
    bookshelves: list[str] | None = None,
) -> bool:
    """True when the book is a work of fiction.

    A P* LoCC class alone is NOT enough -- criticism, essays and literary
    history live there too. PZ (juvenile belles lettres) is enough.
    """
    del title
    blob = " | ".join(_fold(s) for s in _clean_subjects(subjects))
    if _FICTION_SUBJECT_RE.search(blob):
        return True
    for shelf in bookshelves or []:
        if _shelf_key(shelf) in _FICTION_SHELVES:
            return True
    for raw in locc or []:
        match = _LOCC_SPLIT.match((raw or "").strip())
        if match and match.group(1).upper().startswith("PZ"):
            return True
    return False


def classify(
    *,
    title: str,
    authors: str,
    subjects: list[str],
    locc: list[str],
    bookshelves: list[str],
) -> list[str]:
    """Return 0-2 category names from CATEGORIES, most specific first."""
    del authors  # accepted for signature stability; no rule uses it today
    candidates: list[str] = []
    #: how many independent signals (shelf / subject / LoCC) back a category
    support: dict[str, int] = {}

    weak_pool: list[str] = []

    def add(*names: str, weak: bool = False) -> None:
        for name in names:
            if name not in _VALID or name == COMICS or name in _UMBRELLA:
                continue
            if weak:
                if name not in weak_pool:
                    weak_pool.append(name)
                continue
            if name not in candidates:
                candidates.append(name)
            support[name] = support.get(name, 0) + 1

    subjects = _clean_subjects(subjects)
    fiction = is_fiction(subjects=subjects, locc=locc, bookshelves=bookshelves)
    folded_subjects = [_fold(s) for s in (subjects or [])]
    subject_blob = " | ".join(folded_subjects)
    folded_title = _fold(title)
    # The combined phrase itself must not count as a "fantasy" hint.
    hint_blob = subject_blob.replace(_SF_FANTASY_SHELF, " ") + " | " + folded_title


    # (a) bookshelves
    for shelf in bookshelves or []:
        key = _shelf_key(shelf)
        if key == _SF_FANTASY_SHELF:
            resolved = _combined_shelf_category(hint_blob, subject_blob)
            if resolved:
                add(resolved)
            continue
        mapped = _SHELVES.get(key)
        if mapped:
            # "American Literature", "Novels" and friends say "this is a
            # book", not what it is about: never let them outrank a form or
            # genre (Leaves of Grass is Poetry, not General Fiction).
            add(*mapped, weak=(mapped == (FICTION,)))

    # (b) subject keywords. The combined "Science fiction & fantasy" subject
    # is disambiguated exactly like the combined bookshelf.
    if _SF_FANTASY_SHELF in subject_blob:
        resolved = _combined_shelf_category(hint_blob, subject_blob)
        if resolved:
            add(resolved)
    # Non-fiction rules read a blob with the fiction genre phrases blanked
    # out, so "Science fiction" can never argue for Science & Nature.
    nonfiction_blob = _mask(subject_blob, _FICTION_PHRASES)
    for needle, matcher, names, is_fiction_rule in _SUBJECT_MATCHERS:
        if needle in _FICTION_ONLY_NEEDLES and not fiction:
            continue
        blob = subject_blob if is_fiction_rule else nonfiction_blob
        if _subject_hit(needle, matcher, blob):
            add(*names)

    # (c) LoCC fallback. The P* -> General Fiction fallback only applies to
    # actual fiction, or to a book nothing else could place.
    for raw in locc or []:
        mapped = _locc_category(raw)
        if mapped is None:
            continue
        # A grab-bag class (GV recreation, P* = all of literature) only ever
        # places a book that nothing else could place.
        add(mapped, weak=_locc_is_weak(raw) or mapped == FICTION)

    if not candidates:
        candidates = list(weak_pool)

    if fiction:
        # Rule 1: a work of fiction only ever gets fiction-family categories.
        candidates = [c for c in candidates if c in _FICTION_FAMILY]
        if not candidates:
            # We know it is fiction even if no genre claimed it.
            candidates = [FICTION]

    if not candidates:
        return []

    # Stable sort: real genres first, then the best-supported candidate, and
    # the generic buckets always sink to the end.
    # Survival & Preparedness only ever fires from an
    # explicit subject rule, so when it fires it leads its pairing.
    ordered = sorted(
        candidates,
        key=lambda c: (_GENERIC_RANK.get(c, 0), 0 if c == SURVIVAL else 1, -support.get(c, 0)),
    )
    return ordered[:MAX_CATEGORIES_PER_BOOK]
