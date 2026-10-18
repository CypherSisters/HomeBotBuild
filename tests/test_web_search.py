"""Web search: only when the owner asks, Wikipedia first, results are data.

All network goes to the LOCAL stub vendor through the real doors and seam.
"""

from __future__ import annotations

import json
import os

import pytest

from keys_support import (canary, fast_vault, grep_tree, install_vendor,
                          reset_keys, set_posture)
from puffpai import api_server, plain_words, web_search
from puffpai.security import egress_doors, egress_ledger, keys, outside_switches

AUTH = {"Authorization": f"Bearer {api_server._API_KEY}"}

#: The six test strangers, as the search they would ask for.
STRANGERS = {
    "G1": ("search the web for the newest pictures from the james webb telescope",
           "the newest pictures from the james webb telescope"),
    "G2": ("can you look up the biggest dinosaur ever found online?",
           "the biggest dinosaur ever found"),
    "G3": ("search the internet for 2014 Honda Civic recalls",
           "2014 Honda Civic recalls"),
    "G4": ("look online for when to plant tomatoes in Ohio",
           "when to plant tomatoes in Ohio"),
    "G5": ("do a web search for easy granny square crochet patterns",
           "easy granny square crochet patterns"),
    "G6": ("search wikipedia for Radiohead albums", "Radiohead albums"),
}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for d in egress_doors.DOORS:
        monkeypatch.delenv(d.env_var, raising=False)
    reset_keys()
    set_posture("enumerated")
    yield
    reset_keys()


def _wiki_on():
    outside_switches.set_switch("web_search", True, by="owner")
    egress_doors.reset_cache()


# ── only the owner's own words start a search ────────────────────────────────

@pytest.mark.parametrize("who", sorted(STRANGERS))
def test_each_strangers_ask_is_understood(who):
    said, words = STRANGERS[who]
    got = web_search.search_ask(said)
    assert got is not None and got[0] == words
    assert got[1] == ("web_search" if who == "G6" else None)


@pytest.mark.parametrize("said", [
    "tell me about dinosaurs", "what is a web search?",
    "I searched the web yesterday", "how do I knit a scarf",
    "my car needs an oil change", "search results are boring",
])
def test_ordinary_chat_never_searches(said, monkeypatch):
    vendor = install_vendor(monkeypatch)
    _wiki_on()
    assert web_search.search_ask(said) is None
    assert web_search.answer(said) is None
    assert vendor.calls == []


# ── provider 0: Wikipedia, keyless ───────────────────────────────────────────

def test_an_offline_bot_never_searches(monkeypatch):
    vendor = install_vendor(monkeypatch)
    set_posture("island")
    _wiki_on()
    reply = web_search.answer("search the web for volcanoes")
    assert vendor.calls == [] and "could not search" in reply.lower()


# ── private details come out first ───────────────────────────────────────────

def test_a_search_with_a_high_risk_detail_is_not_sent(monkeypatch):
    vendor = install_vendor(monkeypatch)
    _wiki_on()
    reply = web_search.answer("search the web for my ssn 123-45-6789 and "
                              "email a@example.org")
    assert reply.startswith("That search had private details in it")
    assert vendor.calls == []


# ── keys first, and pauses said plainly ──────────────────────────────────────

# ── saved chats keep a note, never the results, never a pasted key ───────────

def test_history_keeps_a_note_instead_of_the_answer():
    reply = ("Trilobites lived long ago.\n\nWhere this came from:\n1. T - "
             "en.wikipedia.org\n   https://en.wikipedia.org/wiki/Trilobite\n\n"
             "I searched Brave for: trilobites\n12 of 900 searches used this month.")
    conv = {"messages": [{"role": "user", "content": "search the web for trilobites"},
                         {"role": "assistant", "content": reply}]}
    web_search.history_scrub(conv)
    kept = conv["messages"][1]["content"]
    assert kept == ("[Web search answer not kept: search results are not saved "
                    "in chats. I searched Brave for: trilobites]")


def test_a_pasted_key_is_answered_and_never_saved(monkeypatch):
    vendor = install_vendor(monkeypatch)
    value = canary()
    said = f"here is my brave key {value}"
    called = []
    reply = web_search.answer(said, ask_model=lambda m: called.append(m))
    assert reply == web_search.KEY_PASTE_REPLY and called == []
    assert vendor.calls == []
    conv = {"messages": [{"role": "user", "content": said},
                         {"role": "assistant", "content": reply}]}
    web_search.history_scrub(conv)
    assert value not in json.dumps(conv)


# ── what the bot can do, in plain words, per surface ─────────────────────────

def test_nothing_about_a_search_is_written_but_the_log_line(monkeypatch):
    install_vendor(monkeypatch)
    _wiki_on()
    web_search.answer("search wikipedia for zyzzyvaword",
                      ask_model=lambda m: "zyzzyvaword answer text")
    from puffpai.config import get_state_dir
    assert grep_tree(get_state_dir(), "zyzzyvaword") == []
    lines = egress_ledger.read(1.0)
    assert lines and lines[0]["host"] == "en.wikipedia.org"


def test_a_busy_service_is_named_plainly(monkeypatch):
    vendor = install_vendor(monkeypatch)
    vendor.status_override = 429
    outside_switches.set_switch("marginalia_search", True, by="owner")
    egress_doors.reset_cache()
    got = web_search.search("linear b", provider="marginalia_search",
                            use_test_key=True)
    assert got["ok"] is False
    assert any("too many searches just now" in n for n in got["notes"])
    assert "PuffPAI-HomeBot/" in vendor.calls[0]["_headers"]["User-Agent"]


def test_a_pause_is_said_once_in_plain_words():
    line = web_search._paused(  # noqa: SLF001
        "brave_search", "Paused: your vault is locked. Brave Search asks for "
        "a payment card, so its key never works while the vault is locked.")
    assert line.startswith("Brave search is paused: your vault is locked.")
    assert line.lower().count("paused") == 1
    assert web_search._paused(  # noqa: SLF001
        "brave_search", "Paused until 1 October: this month's searches are "
        "used up.") == ("Brave search is paused until 1 October: this "
                        "month's searches are used up.")
    assert not plain_words.check(line)

