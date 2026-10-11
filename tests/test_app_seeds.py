"""no app page may push a fabricated seed into a real household.

The calendar page seeded itself with invented household RECORD — a
prescription with dosing instructions, a mortgage payment, a child's permission
slip — and then called the sync path, writing all of it into the family's real
calendar the first time anyone opened the tile. Under the voice surface, the
appliance would have read fabricated medication instructions aloud as fact.

These are text assertions over the shipped HTML in BOTH trees, because that is
where the defect lives and there is no JS test runner in this suite. They are
deliberately blunt: a seed may not name a drug, a mortgage or a person, and a
seed function may not reach the push/sync path.
"""

import os
import re

import pytest

_TREES = {
    "dashboard": r"C:/Users/owner/PuffPAI/improve-ft-sync/public",
    "buildbot": r"C:/Users/owner/PuffPAI/improve-ft-bb/dashboard/public",
}
_PAGES = ("alarm-app.html", "calendar-app.html", "fitfam-app.html",
          "freshtable-app.html", "trivia-app.html")

#: Fabricated household record that must never appear in shipped page source.
#: Split so this file does not itself read as a medication list.
_FORBIDDEN = ("Amoxicillin", "Prenatal", "Mortgage", "mortgage",
              "Zyrtec", "permission slip", "Dr. Williams")

_CASES = [(tree, page) for tree in _TREES for page in _PAGES]
_IDS = [f"{tree}/{page}" for tree, page in _CASES]


def _source(tree, page):
    path = os.path.join(_TREES[tree], page)
    if not os.path.exists(path):
        pytest.skip(f"tree not checked out: {path}")
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _seed_bodies(source):
    """Every ``function seed…`` / ``sampleSeed`` body in the page, brace-matched."""
    bodies = []
    for match in re.finditer(r"function\s+(\w*[Ss]eed\w*)\s*\([^)]*\)\s*\{", source):
        depth, i = 0, match.end() - 1
        while i < len(source):
            if source[i] == "{":
                depth += 1
            elif source[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        bodies.append((match.group(1), source[match.end():i]))
    return bodies


@pytest.mark.parametrize("tree,page", _CASES, ids=_IDS)
class TestNoFabricatedSeeds:
    def test_no_invented_household_record_anywhere_on_the_page(self, tree, page):
        source = _source(tree, page)
        found = [word for word in _FORBIDDEN if word in source]
        assert not found, f"{tree}/{page} still ships invented record: {found}"

    def test_no_seed_touches_the_push_or_sync_path(self, tree, page):
        source = _source(tree, page)
        for name, body in _seed_bodies(source):
            offenders = re.findall(r"\b\w*(?:[Pp]ush|[Ss]ync)\w*\s*\(", body)
            # showSampleBanner/saveLocal are local-only by construction.
            offenders = [o for o in offenders if "SampleBanner" not in o]
            assert not offenders, (
                f"{tree}/{page}: {name}() reaches the sync path: {offenders}")

    def test_a_seed_labels_itself_as_sample(self, tree, page):
        source = _source(tree, page)
        if not _seed_bodies(source):
            return   # pages with no seed have nothing to label
        assert "sampleBanner" in source, (
            f"{tree}/{page} seeds data with no on-screen sample label")

    def test_a_seed_only_runs_over_a_store_known_to_be_empty(self, tree, page):
        """Not merely 'the server did not answer' — that is not the same thing."""
        source = _source(tree, page)
        if not _seed_bodies(source):
            return
        assert ("state === 'empty'" in source) or ("serverEmpty" in source), (
            f"{tree}/{page} seeds without checking the server document is empty")


@pytest.mark.parametrize("tree", sorted(_TREES), ids=sorted(_TREES))
def test_the_calendar_ships_no_sample_medications(tree):
    """The one list on that screen an invented row could get acted on."""
    source = _source(tree, "calendar-app.html")
    for _name, body in _seed_bodies(source):
        meds = re.search(r"D\.meds\s*=\s*\[(.*?)\]", body, re.S)
        if meds:
            assert not meds.group(1).strip(), (
                f"{tree}: the calendar seed invents medications")
