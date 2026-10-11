"""The address gate after a CONFIRMED wake.

A ``wake=confirmed`` event means stage 2 has
already heard "Hey Puff Pie" in the trigger window, but if
``parse_address`` re-requires an exact ``_ADDRESS_KEYS`` substring with none
of the tolerance stage 2 just applied, most of those turns would end
``wake=fired addressed=false`` and the owner would get no
reply at all: the second, longer Whisper decode of the same audio can write
the wake phrase differently ("Pufpie", "puf pie", the name after an "um").

``resolve_address(text, confirmed)`` is the gate now: exact → fuzzy → trusted
when confirmed, strict-only when not.
"""

from puffpai.voice.service import (
    fuzzy_parse_address,
    parse_address,
    resolve_address,
    _fuzzy_name_span,
    _words,
)


# ── a. exact path is untouched ─────────────────────────────────────────────
def test_exact_address_unchanged_when_confirmed():
    text = "Hey puff pie, what time is it?"
    assert parse_address(text) == (True, "what time is it")
    assert resolve_address(text, True) == (True, "what time is it", "exact")


def test_exact_address_unchanged_when_unconfirmed():
    assert resolve_address("Hey puff, what time is it?", False) == (
        True, "what time is it", "exact")


def test_glued_and_filler_forms_still_land_on_the_strict_parser():
    """Both of these already carry the literal "puff", so exact takes them."""
    assert resolve_address("Hey, puffpai what's the weather", True) == (
        True, "what's the weather", "exact")
    assert resolve_address("um puff pie turn on the lamp", True) == (
        True, "turn on the lamp", "exact")


# ── b. fuzzy path: the name spelled as stage 2 would have accepted it ──────
def test_fuzzy_glued_misspelling():
    assert resolve_address("Hey Pufpie, what time is it", True) == (
        True, "what time is it", "fuzzy")


def test_fuzzy_split_misspelling():
    assert resolve_address("puf pie set a timer", True) == (
        True, "set a timer", "fuzzy")


def test_fuzzy_strips_leading_fillers_from_the_command():
    assert fuzzy_parse_address("hey pufpi, um turn on the lamp") == (
        True, "turn on the lamp")


def test_fuzzy_only_looks_at_the_opening_of_the_utterance():
    """A name eight words in is a mention, not an address — trusted covers it."""
    late = "so I told him about the thing and puf pie was there"
    assert fuzzy_parse_address(late) == (False, "")


# ── c. trusted path: confirmed is the evidence ─────────────────────────────
def test_confirmed_with_no_recognisable_name_is_still_addressed():
    assert resolve_address("what time is it", True) == (
        True, "what time is it", "trusted")


def test_trusted_strips_leading_fillers_only():
    assert resolve_address("um, turn on the kitchen light", True) == (
        True, "turn on the kitchen light", "trusted")


def test_confirmed_but_empty_is_not_addressed():
    assert resolve_address("   ", True) == (False, "", "none")


# ── d. the UNCONFIRMED path keeps the strict behaviour exactly ─────────────
def test_unconfirmed_bare_command_is_still_rejected():
    assert resolve_address("what time is it", False) == (False, "", "none")


def test_unconfirmed_fuzzy_name_is_still_rejected():
    """Without a confirm nothing has proved her name; the tolerance is off."""
    assert resolve_address("Hey Pufpie, what time is it", False) == (False, "", "none")
    assert resolve_address("puf pie set a timer", False) == (False, "", "none")


def test_unconfirmed_ambient_speech_is_still_rejected():
    for ambient in ("apple pie and ice cream",
                    "no I told him the closing is on the fourteenth"):
        assert resolve_address(ambient, False) == (False, "", "none")


def test_fuzzy_matcher_rejects_half_a_name():
    """The shared span helper keeps both halves mandatory.

    (``parse_address`` itself still takes "puffy jacket" on the bare "puff"
    substring — pre-existing strict behaviour, deliberately left alone here.)
    """
    assert _fuzzy_name_span(_words("apple pie and ice cream")) is None
    assert _fuzzy_name_span(_words("puffy jacket")) is None
    assert _fuzzy_name_span(_words("hey pufpie what time is it")) == (1, 2)
    assert _fuzzy_name_span(_words("puf pie set a timer")) == (0, 2)
