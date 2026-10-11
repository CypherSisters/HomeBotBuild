"""Build guard: no real person's data may be seeded into the source tree.

WHY THIS EXISTS
---------------
A shipped default is installed in every household, and tool docstrings are fed
to the model on every tool-selection turn. A default that seeds a real member
(a name with an age, a school grade, a sport) or a docstring that names a
person discloses that person to every install and to the LLM on every turn.

WHY THE GUARD IS POSITIONAL AND NOT LEXICAL
-------------------------------------------
The obvious guard, "fail the build if these first names appear anywhere in
src/ or tests/", is deliberately NOT built, and the reason is worth writing
down so nobody adds it later and then disables it.

Many first names are ordinary English words. A lexical guard on such a name
matches dozens of unrelated lines in ``src/`` and ``tests/``: regexes,
everyday verbs and nouns, library attributes, trivia answers. A guard with
that many false positives is a guard that gets an allowlist, then a bigger
allowlist, then a ``skip``. It would also require writing the names INTO the
repo to check for them, which is the thing it is supposed to prevent.

So the guard checks SHAPE instead of spelling, which catches the whole class of
defect regardless of whose names are involved:

  * no shipped default may carry a person's identity: an ``age``, birthdate,
    school grade, or contact detail (see IDENTITY_FIELDS for why a bare display
    ``name`` is excluded);
  * no dict literal in ``src/`` may hardcode an ``age``;
  * no tool docstring may carry a possessive proper noun ("Somebody's...") or
    a person-plus-school-grade pairing;
  * the all-members fallback must read the roster from the data file.

Anything that gets past all four is not "a person in the seed"; it is a new
category, and it should get its own test.
"""

import ast
import io
import os
import re

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(REPO_ROOT, "src")

#: Fields that make a seeded record a real PERSON rather than a display
#: placeholder. A bare ``name`` is deliberately NOT in this set: Calendar ships
#: four role-shaped placeholder rows ("Dad", "Mom", and two child slots) whose
#: only purpose is to give the app its colour tags on a fresh box, and emptying
#: them is a UX decision, not a privacy fix. What makes a seed a PERSON is a
#: name joined to an age, a birthdate, a school grade, or a contact detail.
IDENTITY_FIELDS = frozenset({
    "age", "years", "birthday", "birthdate", "dob", "grade",
    "school", "email", "phone", "address",
})

#: Proper nouns that are products, places, or public figures rather than
#: household members. Kept short on purpose — a long allowlist here means the
#: docstrings have drifted back toward naming people.
NON_PERSON_PROPER_NOUNS = frozenset({
    "Assistant", "Home", "PuffPAI", "Puff", "Ollama", "Spotify",
    "Jellyfin", "Calibre", "TuneIn", "SearXNG", "Zigbee", "Wikipedia",
    "Alarmflow", "AlarmFlow", "FitFam", "FreshTable", "Trivia", "Calendar",
    "Today", "Monday", "Tuesday", "Wednesday", "Thursday",
    "Friday", "Saturday", "Sunday", "Linux", "Windows", "Docker", "Python",
    "Sentinel", "Drawbridge", "Archivist", "Concierge", "Toolsmith",
    "BuildBot", "HomeBot", "Node", "Assist",
})


def _py_files(root):
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for f in sorted(files):
            if f.endswith(".py"):
                yield os.path.join(dirpath, f)


def _rel(path):
    return os.path.relpath(path, REPO_ROOT).replace(os.sep, "/")


# ── Guard 1: the shipped defaults must be a schema, not a household ────────

def _identity_hits(node, path=""):
    """Yield (json-path, field) for identity fields carrying a value."""
    if isinstance(node, dict):
        for key, value in node.items():
            here = "%s.%s" % (path, key) if path else str(key)
            if (str(key).lower() in IDENTITY_FIELDS
                    and value not in (None, "", 0, [], {}, False)):
                yield here, str(key)
            yield from _identity_hits(value, here)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            yield from _identity_hits(value, "%s[%d]" % (path, i))


def test_no_app_default_seeds_a_person(tmp_path, monkeypatch):
    """Every shipped app default must be empty of people.

    A roster default whose members carry ages is exactly what this rejects.
    """
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    import importlib

    from puffpai import app_data as app_data_module
    importlib.reload(app_data_module)
    try:
        ad = app_data_module.AppData()
        offences = []
        for app, factory in ad._defaults.items():
            for where, field in _identity_hits(factory()):
                offences.append("%s default -> %s (%s)" % (app, where, field))
        assert not offences, (
            "shipped app defaults carry a person's identity:\n  "
            + "\n  ".join(offences)
            + "\nA default is a schema. Members come from the data file.")
    finally:
        importlib.reload(app_data_module)


def test_fitfam_default_roster_is_empty(tmp_path, monkeypatch):
    """Named explicitly, because a roster is where people would be seeded."""
    monkeypatch.setenv("PUFFPAI_DATA_DIR", str(tmp_path))
    import importlib

    from puffpai import app_data as app_data_module
    importlib.reload(app_data_module)
    try:
        assert app_data_module.AppData()._defaults["fitfam"]()["users"] == {}
    finally:
        importlib.reload(app_data_module)


# ── Guard 2: no hardcoded age anywhere in src/ ─────────────────────────────

def test_no_dict_literal_in_src_hardcodes_an_age():
    """An age is the field that turns a record into a person, and a minor's
    age is the field that turns it into a children's record."""
    offences = []
    for path in _py_files(SRC):
        try:
            tree = ast.parse(io.open(path, encoding="utf-8").read())
        except SyntaxError:  # pragma: no cover - tree must parse
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            for key, value in zip(node.keys, node.values):
                if (isinstance(key, ast.Constant)
                        and str(key.value).lower() in ("age", "years")
                        and isinstance(value, ast.Constant)
                        and isinstance(value.value, int)
                        and not isinstance(value.value, bool)):
                    # An age literal is the field that turns a record into a
                    # person. ``None`` (rehearsal/simulator shapes that carry
                    # the KEY but not a value) is fine and stays fine.
                    offences.append("%s:%d" % (_rel(path), node.lineno))
    assert not offences, (
        "hardcoded age literal(s) in src/: " + ", ".join(offences))


# ── Guard 3: tool docstrings reach the model; keep people out of them ──────

_POSSESSIVE_PROPER_NOUN = re.compile(r"\b([A-Z][a-z]{2,})['’]s\b")
_SCHOOL_GRADE = re.compile(r"\b\d+(?:st|nd|rd|th)\s+grade\b", re.IGNORECASE)


def _tool_docstrings(path):
    """for every ``@tool``-decorated function in a file."""
    tree = ast.parse(io.open(path, encoding="utf-8").read())
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        names = []
        for dec in node.decorator_list:
            target = dec.func if isinstance(dec, ast.Call) else dec
            names.append(getattr(target, "id", None)
                         or getattr(target, "attr", None))
        if "tool" not in names:
            continue
        doc = ast.get_docstring(node, clean=False)
        if doc:
            yield node.lineno, node.name, doc


@pytest.mark.parametrize("relpath", ["src/puffpai/assistant_tools.py"])
def test_no_tool_docstring_names_a_person(relpath):
    """Tool docstrings are concatenated into the model prompt on every turn.

    A possessive proper noun in one ("Somebody's goals", "Somebody's birth
    certificate") is a household member's name being handed to the LLM whether
    or not the turn is about them.
    """
    path = os.path.join(REPO_ROOT, *relpath.split("/"))
    offences = []
    for lineno, func, doc in _tool_docstrings(path):
        for match in _POSSESSIVE_PROPER_NOUN.finditer(doc):
            if match.group(1) not in NON_PERSON_PROPER_NOUNS:
                offences.append("%s:%d %s() -> %s's"
                                % (relpath, lineno, func, match.group(1)))
    assert not offences, (
        "tool docstring(s) name a person; these reach the model prompt:\n  "
        + "\n  ".join(offences))


@pytest.mark.parametrize("relpath", ["src/puffpai/assistant_tools.py"])
def test_no_tool_docstring_pairs_a_name_with_a_school_grade(relpath):
    """A child's first name and their school grade, in one example
    sentence, in the prompt."""
    path = os.path.join(REPO_ROOT, *relpath.split("/"))
    offences = []
    for lineno, func, doc in _tool_docstrings(path):
        if not _SCHOOL_GRADE.search(doc):
            continue
        for match in _POSSESSIVE_PROPER_NOUN.finditer(doc):
            if match.group(1) not in NON_PERSON_PROPER_NOUNS:
                offences.append("%s:%d %s()" % (relpath, lineno, func))
        # "test <Name> on 8th grade biology" — a bare capitalised word between
        # an instructional verb and the grade phrase.
        for match in re.finditer(
                r"\b(?:test|quiz|ask)\s+(?-i:([A-Z][a-z]{2,}))\s+on\b", doc):
            if match.group(1) not in NON_PERSON_PROPER_NOUNS:
                offences.append("%s:%d %s() -> %s"
                                % (relpath, lineno, func, match.group(1)))
    assert not offences, (
        "tool docstring(s) pair a person with a school grade: "
        + ", ".join(sorted(set(offences))))


# ── Guard 4: the all-members fallback must read the roster ────────────────

def test_all_members_fallback_is_not_a_hardcoded_list():
    """A hardcoded ``users_to_check = [...]`` of first names makes "show
    everyone's PRs" only ever mean those people, and ships their names to
    every other household."""
    path = os.path.join(SRC, "puffpai", "assistant_tools.py")
    source = io.open(path, encoding="utf-8").read()
    match = re.search(r"users_to_check = \(?(.{0,220})", source, re.DOTALL)
    assert match, "users_to_check fallback not found"
    snippet = match.group(1)
    assert "get_data('fitfam')" in snippet or 'get_data("fitfam")' in snippet, (
        "the all-members fallback must read the FitFam roster from the data "
        "file, not hardcode member ids:\n" + snippet)


# ── Guard 5: the concrete strings, when the checker has been given them ────
#
# Guards 1-4 are deliberately SHAPE-based (see the module docstring): a lexical
# guard would have to carry the names in the repo to look for them, which is
# the thing it exists to prevent. But a shape guard stays green while a first
# name sits in many places in ``src/``, including a shipped default, so this
# guard takes the names from OUTSIDE the tree and skips when they are not
# supplied.
#
# WHERE THE NAMES COME FROM (never the repo):
# * ``PUFFPAI_GUARD_NAMES`` — comma-separated, for CI, or
# * a file named by ``PUFFPAI_GUARD_NAMES_FILE``, or
# * ``<repo>/.household_names.local`` — one name per line, gitignored.
# Absent all three the test SKIPS with a message saying how to arm it. A skip is
# the honest outcome: a green assertion here would claim a check that did not
# run.
#
# WHAT IT LOOKS FOR: two shapes only, because first names are often ordinary
# English words and a bare word match is the false-positive swamp the module
# docstring describes:
# A. a POSSESSIVE ("Somebody's vault content") anywhere in the file, which is
# a person rather than the ordinary word;
# B. the name as a COMPLETE quoted string literal (``"somebody"``), which is
# how a seeded default, a roster key or an allowlist entry looks.

NAMES_FILE_DEFAULT = os.path.join(REPO_ROOT, ".household_names.local")

#: Lines that may legitimately carry a household id as a quoted literal, with
#: the reason each is exempt. Every one of these is LIVE CONFIGURATION whose
#: value the running appliance depends on — an app document's ``user`` key
#: (which selects the FitFam roster record) and the Zone-1 non-owner allowlist
#: (which vouched callers and owner-tagged records are keyed to). Changing
#: them is a data migration on an existing install, not a source scrub, so
#: they are exempt here.
#:
#: This list is the guard's whole escape hatch. It is five entries and it should
#: SHRINK. Adding a sixth means a household id reached a new place in the tree.
ACCEPTED_LIVE_CONFIG_LINES = (
    (r"SUPPORTED_NON_OWNER_USERS\s*=\s*frozenset",
     "Zone-1 non-owner allowlist (live config)"),
    (r"^\s*\"user\"\s*:\s*\"",
     "app document default / status projection (live config)"),
    (r"\.get\(\s*'user'\s*,",
     "app status projection fallback (live config)"),
    (r"get\(\s*\"user\"\s*\)\s*or\s*\"",
     "service user() fallback (live config)"),
    (r"user\s*:\s*str\s*=\s*\"",
     "simulator default parameter (must match the fallback above)"),
)

#: Quoted literals in weather code (condition names, icon maps, forecast
#: tests) are vocabulary, not people, and many condition words are also
#: given names.
WEATHER_VOCABULARY = re.compile(
    r"(?i)\b(?:weather|forecast|precip\w*|condition|conditions|icon|"
    r"drizzle|snow|sleet|thunder|cloud\w*|sunny|clear|shower\w*|"
    r"umbrella|wmo|openweather)\b")


def _guarded_names():
    raw = os.environ.get("PUFFPAI_GUARD_NAMES", "")
    if not raw.strip():
        path = os.environ.get("PUFFPAI_GUARD_NAMES_FILE") or NAMES_FILE_DEFAULT
        if os.path.exists(path):
            raw = io.open(path, encoding="utf-8").read().replace("\n", ",")
    names = [n.strip() for n in raw.split(",")]
    return [n for n in names if len(n) >= 3]


def _name_patterns(names):
    alternation = "|".join(re.escape(n) for n in names)
    possessive = re.compile(r"\b(?:%s)'s\b" % alternation, re.IGNORECASE)
    quoted = re.compile(r"""(['"])(?:%s)\1""" % alternation, re.IGNORECASE)
    return possessive, quoted


def _scan_roots():
    yield SRC
    yield os.path.join(REPO_ROOT, "tests")


def _accepted(line):
    for pattern, _why in ACCEPTED_LIVE_CONFIG_LINES:
        if re.search(pattern, line):
            return True
    return bool(WEATHER_VOCABULARY.search(line))


def test_no_household_first_name_as_a_possessive_or_a_quoted_id():
    """The lexical half of the guard, armed from outside the tree.

    Deliberately NOT a bare-word search: a name that is also an everyday
    word, a library attribute or a trivia answer is legitimate in those uses,
    and a guard that flags them is a guard that gets skipped. A possessive and
    a whole quoted literal are not.
    """
    names = _guarded_names()
    if not names:
        pytest.skip(
            "no household names supplied - set PUFFPAI_GUARD_NAMES, or "
            "PUFFPAI_GUARD_NAMES_FILE, or create the gitignored "
            ".household_names.local (one name per line). This guard is a "
            "no-op until one of those exists, by design: the names must "
            "never live in the repo.")

    possessive, quoted = _name_patterns(names)

    offences = []
    for root in _scan_roots():
        for path in _py_files(root):
            if os.path.samefile(path, os.path.abspath(__file__)):
                continue
            with io.open(path, encoding="utf-8", errors="replace") as fh:
                for lineno, line in enumerate(fh, 1):
                    hit = possessive.search(line) or quoted.search(line)
                    if not hit or _accepted(line):
                        continue
                    # Report the LOCATION only. Echoing the matched text would
                    # put the household's name in the pytest output, in CI logs
                    # and in whatever paste follows — the same disclosure the
                    # guard exists to prevent.
                    offences.append("%s:%d" % (_rel(path), lineno))

    assert not offences, (
        "a household member's first name appears as a possessive or a quoted "
        "id at %d location(s) (the names are withheld from this message on "
        "purpose): %s" % (len(offences), ", ".join(offences)))


def test_the_guard_actually_catches_both_shapes():
    """Mutation check: a guard nobody has seen fail is a guard nobody trusts.

    Runs the same two patterns over synthetic lines built from the supplied
    names, so a future edit that loosens them fails here rather than quietly
    stopping catching anything.
    """
    names = _guarded_names()
    if not names:
        pytest.skip("no household names supplied - see the guard above.")
    name = names[0]
    possessive, quoted = _name_patterns(names)

    assert possessive.search("    # never surfaces %s's vault content" % name)
    assert quoted.search('                "user": "%s",' % name)
    assert quoted.search("SUPPORTED_NON_OWNER_USERS = frozenset({'%s'})" % name)
    #...and does not fire on a bare-word use of the same name, which is the
    # reason a bare-word guard was rejected (many names are ordinary words).
    word = name.lower()
    for benign in ("%s the task as done" % word, "@pytest.%s.parametrize" % word,
                   "a low-water %s" % word, "if %s:" % word, "# question %s" % word):
        assert not possessive.search(benign), benign
        assert not quoted.search(benign), benign


def test_the_live_config_exemption_list_is_small_and_explained():
    """An escape hatch that can grow without anyone noticing is not an escape
    hatch, it is the disable switch. At most five entries, each with a reason."""
    assert len(ACCEPTED_LIVE_CONFIG_LINES) <= 5
    for pattern, why in ACCEPTED_LIVE_CONFIG_LINES:
        assert pattern and why and len(why) > 10
