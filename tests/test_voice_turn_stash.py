"""The stashed full voice reply belongs to ONE turn.

A spoken directions answer stashes the full step list on a thread-local so
the transcript keeps every step while the speaker says two sentences. Worker
threads are reused, and some turns never reach the code that sets the stash
(a PIN answer, a pure PII disclosure, a stubbed pipeline), so every turn must
start with the stash empty. Otherwise the next turn on that thread would be
recorded with the directions list as its own text.
"""

import pytest

from tests.test_maps_directions import engines  # noqa: F401  (fixture)

OWN_REPLY = "It is three o'clock."


@pytest.fixture
def api():
    from puffpai import api_server
    return api_server


@pytest.fixture
def recorded(api, monkeypatch):
    """Every (user, assistant) pair the voice transcript would record."""
    calls = []
    api.app.config.update(TESTING=True)
    api._rate_limit_store.clear()
    api._reset_vault_turn_privacy()
    api._live_turn_clear()
    monkeypatch.setattr(api, "_record_voice_turn",
                        lambda user, assistant, **k: calls.append((user, assistant)))
    monkeypatch.setattr(api, "_apply_pii_honesty", lambda text, *a, **k: text)
    monkeypatch.setattr(api, "_pii_deterministic_reply", lambda *a, **k: None)
    yield calls
    api._reset_vault_turn_privacy()
    api._live_turn_clear()
    api._voice_full_tl.text = None


def _spoken_directions_turn(api):
    spoken = api._process_voice_query("how do I get to the library",
                                      conversation_id="home-owner")
    assert "Library Lane" in (api._voice_full_tl.text or "")
    return spoken


def test_a_later_voice_turn_records_its_own_reply_not_the_directions(
        api, recorded, engines, monkeypatch):  # noqa: F811
    _spoken_directions_turn(api)
    # The next turn's pipeline never touches the stash (as a PIN answer or a
    # disclosure notice does not); the request must still start clean.
    monkeypatch.setattr(api, "_process_voice_query", lambda *a, **k: OWN_REPLY)
    with api.app.test_client() as c:
        r = c.post("/api/chat", headers={"X-API-Key": api._API_KEY},
                   json={"messages": [{"role": "user",
                                       "content": "what time is it"}]})
    assert r.status_code == 200, r.get_data(as_text=True)
    assert recorded, "the voice turn was not recorded"
    user, assistant = recorded[-1]
    assert user == "what time is it"
    assert assistant == OWN_REPLY
    assert "Library Lane" not in assistant


def test_every_request_clears_the_stash_before_any_handler(api):
    """App-wide, so chat, voice, Home Assistant and Send to Puff all start
    clean, whichever route a later turn arrives on."""
    hooks = api.app.before_request_funcs.get(None, [])
    assert api._clear_voice_full_text in hooks
    api._voice_full_tl.text = "a stale reply"
    with api.app.test_request_context("/api/chat", method="POST"):
        api._clear_voice_full_text()
        assert api._voice_full_tl.text is None


def test_an_early_return_in_the_voice_pipeline_leaves_no_stale_stash(
        api, recorded, engines, monkeypatch):  # noqa: F811
    _spoken_directions_turn(api)
    monkeypatch.setattr(api, "_ha_pin_intercept", lambda *a, **k: "Done.")
    assert api._process_voice_query("4 8 2 1", conversation_id="home-owner") \
        .endswith("Done.")
    assert api._voice_full_tl.text is None
