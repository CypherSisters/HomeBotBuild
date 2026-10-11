"""Things a voice reply must not read out loud, because nobody would say them.

* A quiz question preceded by "[Trivia, general knowledge (an adult
  general-knowledge audience)]".
* "Playing KEXP 90.3 Seattle, WA (AAC 160K) on the speakers."
* A document list with every entry followed by "(userupload, 0 words)".
* "Good morning" at 2:41 PM.
* "Timer set: 2 minute, 2 minutes counting down.", the duration twice.

All of these are SPEECH shaping: the chat surface keeps the label, the
codec and the metadata, which are useful on a screen.
"""

import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from puffpai.voice_text import voice_cosmetics  # noqa: E402

AFTERNOON = datetime(2026, 9, 13, 14, 41)
MORNING = datetime(2026, 9, 13, 7, 5)
EVENING = datetime(2026, 9, 13, 20, 5)


def test_the_trivia_label_is_not_read_out():
    said = voice_cosmetics(
        "[Trivia — General Knowledge (an adult general-knowledge audience)]\n"
        "Which planet is closest to the sun?", now=AFTERNOON)
    assert said == "Which planet is closest to the sun?"


def test_the_codec_suffix_is_dropped_from_a_station_name():
    assert voice_cosmetics("Playing KEXP 90.3 Seattle, WA (AAC 160K) on the "
                           "speakers.", now=AFTERNOON) == \
        "Playing KEXP 90.3 Seattle, WA on the speakers."


@pytest.mark.parametrize("suffix", ["(AAC 160K)", "(MP3 128k)", "(AAC)",
                                    "(128K)", "(Opus 96K)"])
def test_every_codec_shape(suffix):
    assert voice_cosmetics("Playing WOSU %s on the speakers." % suffix,
                           now=AFTERNOON) == "Playing WOSU on the speakers."


def test_the_document_index_metadata_is_dropped():
    assert voice_cosmetics(
        "I have 2 documents: budget.pdf (userupload, 0 words), "
        "notes.docx (userupload, 12 words).", now=AFTERNOON) == \
        "I have 2 documents: budget.pdf, notes.docx."


@pytest.mark.parametrize("now,expected", [
    (MORNING, "Good morning. Here is your briefing."),
    (AFTERNOON, "Good afternoon. Here is your briefing."),
    (EVENING, "Good evening. Here is your briefing."),
])
def test_the_greeting_matches_the_local_time_of_day(now, expected):
    assert voice_cosmetics("Good morning. Here is your briefing.",
                           now=now) == expected


@pytest.mark.parametrize("said", [
    "Playing Classic Vinyl HD on the speakers.",
    "It's 2:39 PM on Sunday, September 13.",
    "The living room is 75 degrees.",
    "Added a task: Sisters coming over, due today.",
])
def test_an_ordinary_reply_is_untouched(said):
    assert voice_cosmetics(said, now=AFTERNOON) == said


def test_an_empty_reply_survives():
    assert voice_cosmetics("") == ""
    assert voice_cosmetics(None) == ""


# ── the timer sentence ──────────────────────────────────────────────────────

def test_the_timer_confirmation_says_the_duration_once(monkeypatch):
    """The store write is stubbed: only the SENTENCE is under test."""
    import threading

    from puffpai.app_data import AppData

    app = AppData.__new__(AppData)
    monkeypatch.setattr(AppData, "_load", lambda self, name: {"items": []})
    monkeypatch.setattr(AppData, "_save", lambda self, name, data: None)
    app._locks = {"alarmflow": threading.Lock()}

    assert app.alarm_create_timer("2 minute", minutes=2) ==         "Timer set for 2 minutes."
    assert app.alarm_create_timer("pasta", minutes=10) ==         "Timer set for 10 minutes: pasta."
    assert app.alarm_create_timer("Timer", seconds=30) ==         "Timer set for 30 seconds."