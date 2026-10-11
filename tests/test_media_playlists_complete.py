""""what playlists do I have saved" must never truncate silently.

With 25 playlists on Jellyfin, a spoken answer that names 12 (Audiobooks ...
Normal Life, stopping alphabetically at "N") and drops the other 13 with no
"and 13 more" reads as a complete list.

The cap is ``jf[:12]`` in ``media_library.playlists_reply``. Keeping a cap on
a SPOKEN answer is right; not saying so is not.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from puffpai import media_library as ml  # noqa: E402

#: 25 names, in the order Jellyfin returns them.
TWENTY_FIVE = [
    "Audiobooks", "Chill", "Classical", "Comedy", "Dinner", "Driving",
    "Focus", "Halloween", "Jazz", "Kids", "Morning", "Normal Life",
    "On-The-Go 7", "On-The-Go 9", "Party", "Road Trip", "Alex Thumbs Up",
    "Alexs Discoveries", "Recently Added", "Rock", "Sleep",
    "Top 25 Most Played", "TV Shows", "Workout", "Xmas",
]


def test_the_truncation_is_declared_not_silent():
    said = ml.playlists_reply({"jellyfin": list(TWENTY_FIVE), "spotify": [],
                               "errors": []})
    assert "showing 12 of 25" in said
    assert "and 13 more" in said


def test_the_first_twelve_are_still_named():
    said = ml.playlists_reply({"jellyfin": list(TWENTY_FIVE), "spotify": [],
                               "errors": []})
    for name in TWENTY_FIVE[:12]:
        assert name in said
    # The 13th is not named — that is what "and 13 more" is for.
    assert "On-The-Go 7" not in said


def test_a_short_list_is_not_marked_truncated():
    said = ml.playlists_reply({"jellyfin": ["Jazz", "Chill"], "spotify": [],
                               "errors": []})
    assert said == ("Here are your saved playlists. "
                    "On your media server: Jazz, Chill.")
    assert "showing" not in said
    assert "more" not in said


def test_exactly_twelve_is_not_marked_truncated():
    said = ml.playlists_reply({"jellyfin": TWENTY_FIVE[:12], "spotify": [],
                               "errors": []})
    assert "showing" not in said
    assert "more." not in said


def test_spotify_is_counted_separately_and_honestly():
    said = ml.playlists_reply({"jellyfin": ["Jazz"],
                               "spotify": list(TWENTY_FIVE), "errors": []})
    assert "On your media server: Jazz." in said
    assert "On Spotify (showing 12 of 25)" in said
    assert "and 13 more" in said


def test_an_unreachable_source_is_still_reported_not_silently_dropped():
    said = ml.playlists_reply({"jellyfin": [], "spotify": [],
                               "errors": ["jellyfin"]})
    assert "couldn't reach your media server" in said


def test_no_playlists_at_all_is_unchanged():
    said = ml.playlists_reply({"jellyfin": [], "spotify": [], "errors": []})
    assert said.startswith("You don't have any playlists saved yet")


@pytest.mark.parametrize("total", [13, 20, 25, 100])
def test_the_stated_remainder_is_always_arithmetically_right(total):
    names = [f"Playlist {i}" for i in range(total)]
    said = ml.playlists_reply({"jellyfin": names, "spotify": [], "errors": []})
    assert f"showing 12 of {total}" in said
    assert f"and {total - 12} more" in said
