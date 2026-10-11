""""What radio sources do you have" is answered from the real door states.

Without a dedicated reader, with door ``tunein`` OPEN, through ``POST /api/chat``:
  "what radio sources do you have?" -> asked back as a cut-off sentence
  "can you use TuneIn" -> a model answer saying it cannot
  "which radio sources can you use" -> a library answer about quasars
The answer is a fact about two egress doors (``radio_browser`` for World Radio,
``tunein`` for TuneIn), so it is built from those doors and nothing else.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import puffpai.api_server as api  # noqa: E402

ASKS = [
    "what radio sources do you have?",
    "which radio sources can you use",
    "can you use TuneIn",
    "can you play TuneIn",
    "what radio can you play",
    "Hey Puff, can you use tune in?",
    "do you have world radio",
    "what radio services are available",
]

NOT_MINE = [
    "tune in to NPR",
    "play NPR on tunein",
    "can you play the radio",
    "can you play jazz radio",
    "what's playing",
    "what are radio sources in space",
    "what radio sources are strongest in the sky",
    "what is tunein",
    "who invented the radio",
    "how does radio work",
    "what's a radio",
    "what radio station is this",
]


def _doors(monkeypatch, world, tunein):
    state = {"radio_browser": world, "tunein": tunein}
    monkeypatch.setattr(api, "_radio_door_open", lambda d: state[d])


@pytest.mark.parametrize("text", ASKS)
def test_both_doors_open_names_both(text, monkeypatch):
    _doors(monkeypatch, True, True)
    reply = api._try_radio_sources(text)
    assert reply is not None, text
    assert "World Radio" in reply and "TuneIn" in reply
    assert "Radio Browser" in reply
    assert "switched off" not in reply


@pytest.mark.parametrize("text", ASKS)
def test_tunein_shut_is_said_plainly(text, monkeypatch):
    _doors(monkeypatch, True, False)
    reply = api._try_radio_sources(text)
    assert reply is not None, text
    assert "World Radio" in reply
    assert "TuneIn is switched off in Settings" in reply


@pytest.mark.parametrize("text", ASKS)
def test_world_radio_shut_is_said_plainly(text, monkeypatch):
    _doors(monkeypatch, False, True)
    reply = api._try_radio_sources(text)
    assert reply is not None, text
    assert "World Radio" in reply and "switched off in Settings" in reply


@pytest.mark.parametrize("text", ASKS)
def test_both_shut_says_internet_radio_is_off(text, monkeypatch):
    _doors(monkeypatch, False, False)
    reply = api._try_radio_sources(text)
    assert reply is not None, text
    assert reply.startswith("Internet radio is switched off")


@pytest.mark.parametrize("text", NOT_MINE)
def test_play_commands_and_other_questions_are_left_alone(text, monkeypatch):
    calls = []
    monkeypatch.setattr(api, "_radio_door_open",
                        lambda d: calls.append(d) or True)
    assert api._try_radio_sources(text) is None, text
    assert calls == []


def test_no_em_dashes_in_any_answer(monkeypatch):
    for world in (True, False):
        for tunein in (True, False):
            _doors(monkeypatch, world, tunein)
            reply = api._try_radio_sources("what radio sources do you have")
            assert "—" not in reply and "–" not in reply


def test_the_door_read_fails_shut(monkeypatch):
    import puffpai.security.egress_doors as doors

    def boom(_door):
        raise RuntimeError("registry unreadable")
    monkeypatch.setattr(doors, "egress_allowed", boom)
    assert api._radio_door_open("tunein") is False


def test_the_dispatcher_answers_before_the_library(monkeypatch):
    """Through _try_instant_answer, not only the helper: otherwise the question
    is asked back as a cut, and its siblings reach the model/library."""
    _doors(monkeypatch, True, True)
    for text in ("what radio sources do you have?", "can you use TuneIn",
                 "which radio sources can you use"):
        reply = api._try_instant_answer(text)
        assert reply is not None and "TuneIn" in reply, (text, reply)
        assert "cut off" not in reply


def test_the_streaming_bypass_gives_the_same_answer(monkeypatch):
    _doors(monkeypatch, True, False)
    text = "can you play TuneIn"
    assert api._try_media_bypass_rules(text) == api._try_radio_sources(text)
