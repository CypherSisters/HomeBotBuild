"""Four short voice turns that must be answered directly.

* "Do I need a jacket outside today?" must not take 25 seconds, read the
  whole weather snapshot aloud and put the actual answer ("a jacket isn't
  necessary") last. It is a yes/no question.
* "Thanks, Puff-Pies!" (the hyphenated plural of the wake word) must reach the
  thanks rule, not be read as PASTRY and offered help with "pastry or kitchen
  questions".
* "I love you" must not get "I'm just a program — I don't have the capacity
  to feel emotions."
* "open the vault" must not spend 16 seconds on the agent graph to say the one
  thing that is always true: it needs the PIN.
"""

import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import puffpai.api_server as api  # noqa: E402
from puffpai import chat_activity, chitchat  # noqa: E402

FORECAST = "the default city: currently 87F, clear, 10% chance of rain."
COLD = "the default city: currently 45F, cloudy, 10% chance of rain."
NOW = datetime(2026, 9, 13, 14, 51)


@pytest.fixture()
def weather(monkeypatch):
    """The forecast tool, plus a summarizer that is loud if it is used."""
    state = {"raw": FORECAST, "calls": []}

    class _Tool:
        def invoke(self, arg):
            state["calls"].append(arg)
            return state["raw"]

    class _Tools:
        tool_dict = {"auto_weather_forecast": _Tool()}

    monkeypatch.setattr(api, "_get_assistant_tools", lambda: _Tools())
    monkeypatch.setattr(api, "_summarize_tool_result",
                        lambda message, raw: "SNAPSHOT<%s>" % raw)
    return state


# ── the jacket question gets a yes/no ───────────────────────────────────────

@pytest.mark.parametrize("said", [
    "Do I need a jacket outside today?",      # the primary phrasing
    "do I need a jacket out there",
    "do I need a jacket out",
    "do I need a jacket right now",
    "do I need a jacket today",               # the short form
])
def test_the_jacket_question_is_answered_directly(weather, said):
    reply = api._try_weather_answer(said)
    assert reply == ("No — it's 87 degrees out, so you won't need a jacket.")
    assert "SNAPSHOT" not in reply
    # The forecast is still READ — the numbers in the answer are real.
    assert weather["calls"] == [""]


def test_a_cold_day_says_take_one(weather):
    weather["raw"] = COLD
    assert api._try_weather_answer("do I need a jacket outside today") == \
        "Yes — it's 45 degrees out, so take a jacket."


@pytest.mark.parametrize("said,expected", [
    ("will I need an umbrella today",
     "No — only a 10% chance of rain today, so you won't need an umbrella."),
    ("should I wear a coat", "No — it's 87 degrees out, so you won't need a coat."),
    ("do we need sunscreen today",
     "Yes — it's 87 degrees and clear out, so sunscreen is a good idea."),
])
def test_the_other_attire_questions(weather, said, expected):
    assert api._try_weather_answer(said) == expected


def test_an_unreadable_forecast_keeps_the_full_answer(weather):
    weather["raw"] = "the weather service is not responding"
    reply = api._try_weather_answer("do I need a jacket outside today")
    assert reply.startswith("SNAPSHOT<")


def test_a_plain_weather_question_still_gets_the_full_answer(weather):
    assert api._try_weather_answer("what's the weather like right now") == \
        "SNAPSHOT<%s>" % FORECAST


def test_the_non_weather_phrasings_still_decline(weather):
    assert api._try_weather_answer("where is my jacket") is None
    assert api._try_weather_answer("add a jacket to the shopping list") is None


# ── the wake name, however it was heard ────────────────────────────────────

@pytest.mark.parametrize("said", [
    "Thanks, Puff-Pies!",                     # the primary phrasing
    "thanks puff pie",
    "thanks, puffpie",
    "thank you puff pi",
    "thanks puffpai",
    "thanks",
])
def test_thanks_is_recognised_whatever_the_name_came_back_as(said):
    assert chitchat.match_social(said, now=NOW) == "You're welcome."


def test_a_greeting_with_the_name_still_greets():
    assert chitchat.match_social("hi puff pies", now=NOW).startswith("Afternoon")


# ── "I love you" ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("said", [
    "I love you", "i love you puff pie", "love you", "I love you so much",
])
def test_i_love_you_gets_a_warm_line(said):
    reply = chitchat.match_social(said, now=NOW)
    assert reply in chitchat._LOVE_REPLIES


@pytest.mark.parametrize("minute", range(0, 60, 7))
def test_no_variant_disclaims_what_it_is(minute):
    reply = chitchat.match_social("I love you",
                                  now=NOW.replace(minute=minute))
    low = reply.lower()
    for banned in ("just a program", "as an ai", "language model",
                   "capacity to feel", "i don't have feelings"):
        assert banned not in low
    assert "love you" in low


def test_a_sentence_about_loving_something_else_is_not_small_talk():
    assert chitchat.match_social("I love this recipe", now=NOW) is None


# ── the vault line ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("said", [
    "open the vault", "unlock the vault", "show me the vault",
    "let me in the vault", "open my vault please", "get into the vault",
])
def test_opening_the_vault_says_the_pin_is_required(said):
    assert api._try_vault_open(said) == (
        "The vault stays locked until you enter your PIN on the dashboard.")


@pytest.mark.parametrize("said", [
    "is the vault locked",          # vault.status owns this, unchanged
    "is my vault still locked",
    "what's in my vault",           # vault.secrets owns this
    "vault status",
    "how many items are in the vault",
    "unlock the front door",
    "show me the weather",
])
def test_every_other_vault_sentence_is_left_where_it_was(said):
    assert api._try_vault_open(said) is None


def test_the_rule_only_speaks(monkeypatch):
    """It must not touch the vault, the PIN slot, or any tool."""
    monkeypatch.setattr(api, "_get_assistant_tools",
                        lambda: pytest.fail("the vault line called a tool"))
    monkeypatch.setattr(api, "_get_app_data",
                        lambda: pytest.fail("the vault line read app data"))
    assert api._try_vault_open("open the vault")


def test_the_turn_is_credited_to_the_vault_status_capability():
    assert chat_activity.FASTPATH_CAPABILITY["_try_vault_open"] == "vault.status"
