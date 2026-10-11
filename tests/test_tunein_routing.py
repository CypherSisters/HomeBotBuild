"""TuneIn-named phrasings must reach the radio player, never RAG.

Without a deterministic owner:

    "tune in to NPR" -> Route: rag, and it FABRICATES: it tells the owner to
                             open a non-existent Jellyfin "Internet Radio"
                             section and paste an invented stream URL "into
                             your media player on the Raspberry Pi".
    "play NPR on tunein" -> Route: rag, "I'm not able to start a stream".
    "play npr radio" -> the real player.

The contract pinned here: a PLAY INTENT is always claimed by a deterministic
fastpath, so it can never be answered by the encyclopedia — and a QUESTION
about TuneIn still is.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import puffpai.api_server as api  # noqa: E402

#: The primary phrasings, plus the variants around them.
PLAY_INTENTS = (
    "tune in to NPR",              # the one RAG fabricates on
    "play NPR on tunein",
    "tune into NPR",
    "tunein NPR",
    "tune in NPR",
    "play NPR on tune in",
    "listen to KEXP on tune-in",
    "put on NPR radio",
    "put NPR on",
    "play npr radio",
    "play some jazz radio",
)

#: Must keep their existing (informational / non-media) routes.
NEGATIVES = (
    "tune the guitar",
    "tune my guitar",
    "what is tunein",
    "whats tunein",
    "what's tunein",
    "how does tunein work",
    "is tunein free",
    "tell me about tunein",
    "put the kettle on",
    "put the lights on",
    "put the kids to bed",
)


@pytest.fixture
def fake_radio(monkeypatch):
    """A catalog that always resolves, and a player that always succeeds."""
    started = []

    def _serve(query, tags=(), station=None):
        started.append(query or (station or {}).get("name"))
        return f"Playing {query or 'a station'} on the speakers."

    monkeypatch.setattr(api, "_radio_play_serverside", _serve)
    monkeypatch.setattr("puffpai.media_hub.search_stations",
                        lambda q, limit=20: [{"name": q}])
    monkeypatch.setattr("puffpai.media_hub.tool_radio_play",
                        lambda q, app_data=None: f"Queued {q}.")
    return started


# ── play intents never reach RAG ─────────────────────────────────────────────

@pytest.mark.parametrize("said", PLAY_INTENTS)
def test_play_intent_never_falls_through_to_rag(said, fake_radio):
    """No play-intent may reach the LLM/RAG layer — that is where the
    Raspberry Pi hallucination came from."""
    reply = api._try_tunein_radio(said) or api._try_media_play(said)
    assert reply, (f"{said!r} reached no deterministic fastpath and would "
                   f"fall through to RAG")
    low = reply.lower()
    assert "raspberry pi" not in low
    assert "not able to start" not in low


@pytest.mark.parametrize("said", ("tune in to NPR", "play NPR on tunein",
                                  "tune into NPR", "tunein NPR", "put NPR on"))
def test_tunein_phrasings_reach_the_same_player(said, fake_radio):
    """They must land on the SAME station query as 'play npr radio'."""
    reply = api._try_tunein_radio(said)
    assert reply and "speakers" in reply.lower()
    assert fake_radio and fake_radio[-1].lower() == "npr"


@pytest.mark.parametrize("said", NEGATIVES)
def test_negatives_are_declined(said, fake_radio):
    assert api._try_tunein_radio(said) is None, \
        f"{said!r} must keep its existing route"
    assert not fake_radio, f"{said!r} must not touch the radio player"


def test_put_x_on_requires_a_real_station(monkeypatch):
    """'put X on' names no service, so it needs catalog evidence first."""
    monkeypatch.setattr("puffpai.media_hub.search_stations",
                        lambda q, limit=20: [])
    monkeypatch.setattr(api, "_radio_play_serverside",
                        lambda *a, **k: pytest.fail("must not start a player"))
    assert api._try_tunein_radio("put Dave on") is None


def test_polite_and_wake_word_forms_still_play(fake_radio):
    for said in ("can you tune in to NPR", "please tune in to NPR",
                 "puff, tune in to NPR"):
        assert api._try_tunein_radio(said), said


def test_dispatcher_claims_the_tunein_phrasings(monkeypatch, fake_radio):
    """End-to-end through _try_instant_answer, the real dispatcher."""
    for said in ("tune in to NPR", "play NPR on tunein"):
        reply = api._try_instant_answer(said)
        assert reply, f"{said!r} must be claimed before the router runs"
        assert "raspberry pi" not in reply.lower()
