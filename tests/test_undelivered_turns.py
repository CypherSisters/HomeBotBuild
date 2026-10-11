"""A reply the model never produced is never stored as the assistant's words.

``_brain_failure`` already made the brainless answer honest on the WIRE: it says
what the request needed and how to fix it instead of "[Error] Cannot connect to
Ollama". Persistence had not caught up. Every chat surface stored that sentence
with ``role: assistant``, so the moment a model was installed the transcript
replayed it as prior context — the appliance teaching the model that "That needs
the language model, which is not reachable yet" is a thing it says — and the
household's own history read as a list of the box's outages.

The turn is now recorded as UNDELIVERED: the user's message is kept with
``meta: {"undelivered": true, "reason": "no_model"}``, no assistant message is
written, and prompt replay skips the turn.

All four surfaces are covered, because they used to drift: the voice path
(``_record_voice_turn``, reached by ``/api/chat``), ``/conversation``,
``/chat/send`` and BOTH legs of ``/chat/stream``.

Nothing here reaches Ollama: the model call is replaced with a refusal, which is
exactly the state a fresh build is in.

Run:
    py -3 -m pytest tests/test_undelivered_turns.py -q
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from puffpai import api_server                       # noqa: E402

AUTH = {"X-API-Key": api_server._API_KEY}

FAILURE = api_server._brain_failure(None)["message"]


@pytest.fixture(autouse=True)
def _empty_history(tmp_path, monkeypatch):
    """An empty in-memory transcript and a disposable history directory."""
    monkeypatch.setattr(api_server, "_get_chat_history_dir",
                        lambda: str(tmp_path / "chat_history"))
    (tmp_path / "chat_history").mkdir(parents=True, exist_ok=True)
    with api_server._chat_history_lock:
        api_server._chat_history[:] = []
    api_server._rate_limit_store.clear()
    api_server.app.config["TESTING"] = True
    yield
    with api_server._chat_history_lock:
        api_server._chat_history[:] = []


@pytest.fixture
def no_model(monkeypatch):
    """A build whose language model is not reachable — the real failure shape."""
    import requests

    def _refuse(*args, **kwargs):
        raise requests.exceptions.ConnectionError("no ollama here")

    monkeypatch.setattr(api_server, "_ollama_chat", _refuse)


def client():
    return api_server.app.test_client()


def stored() -> list:
    """The one conversation's messages, as the transcript holds them."""
    with api_server._chat_history_lock:
        assert len(api_server._chat_history) == 1, api_server._chat_history
        return list(api_server._chat_history[0]['messages'])


def assert_undelivered(messages, *, asked: str) -> None:
    """The stored shape of a turn the model never answered."""
    assert [m['role'] for m in messages] == ['user'], (
        "the failure sentence was stored as the assistant's words")
    turn = messages[0]
    assert turn['content'] == asked
    assert turn['meta'] == {'undelivered': True, 'reason': 'no_model'}
    blob = json.dumps(messages)
    assert api_server._NEEDS_MODEL_SENTENCE not in blob


# ---------------------------------------------------------------------------
# 1. the classifier
# ---------------------------------------------------------------------------

class TestUndeliveredReason:
    @pytest.mark.parametrize("status", [None, 404, 500, 'timeout'])
    def test_every_brain_failure_is_undelivered(self, status):
        message = api_server._brain_failure(status)["message"]
        assert api_server._undelivered_reason(message) == "no_model"

    def test_a_prepended_notice_does_not_hide_it(self):
        """The streaming leg may put a privacy notice in front of the reply."""
        assert api_server._undelivered_reason(
            "Heads up: I did not keep that.\n\n" + FAILURE) == "no_model"

    @pytest.mark.parametrize("text", [
        "The tomatoes want more sun.", "", None, 42, {"a": 1},
    ])
    def test_a_real_reply_is_delivered(self, text):
        assert api_server._undelivered_reason(text) is None


# ---------------------------------------------------------------------------
# 2. the four surfaces
# ---------------------------------------------------------------------------

class TestTheSurfaces:
    def test_chat_send(self, no_model):
        resp = client().post('/chat/send', headers=AUTH, json={
            'message': 'why are my tomatoes not ripening',
            'conversation_id': 'c1'})
        assert resp.status_code == 200
        # The owner is still TOLD, verbatim — only persistence changed.
        assert api_server._NEEDS_MODEL_SENTENCE in resp.get_json()['response']
        assert_undelivered(stored(), asked='why are my tomatoes not ripening')

    def test_chat_stream(self, no_model):
        resp = client().post('/chat/stream', headers=AUTH, json={
            'message': 'why are my tomatoes not ripening',
            'conversation_id': 'c1'})
        assert resp.status_code == 200
        body = resp.get_data(as_text=True)
        assert api_server._NEEDS_MODEL_SENTENCE in body
        assert_undelivered(stored(), asked='why are my tomatoes not ripening')

    def test_conversation(self, monkeypatch):
        """The tool/agent surface.

        Driven at its own seam (``AssistantTools.run``) rather than by unplugging
        Ollama, because this route reaches the model through the agent graph,
        whose HTTP fallback has a legacy "[Error] LLM request failed" string of
        its own that never goes through ``_brain_failure``. That is a separate
        honesty gap; what is under test here is that when this route DOES hand
        back the brainless sentence, it is not persisted as the bot's words.
        """
        from puffpai.assistant_tools import AssistantTools

        monkeypatch.setattr(AssistantTools, "run",
                            lambda self, text, **kwargs: FAILURE)
        resp = client().post('/conversation', headers=AUTH, json={
            'text': 'why are my tomatoes not ripening',
            'conversation_id': 'c1'})
        assert resp.status_code == 200
        assert api_server._NEEDS_MODEL_SENTENCE in resp.get_json()['response']
        assert_undelivered(stored(), asked='why are my tomatoes not ripening')

    def test_the_voice_transcript(self):
        """``_record_voice_turn`` is what the voice /api/chat path records with."""
        api_server._record_voice_turn('why are my tomatoes not ripening', FAILURE)
        assert_undelivered(stored(), asked='why are my tomatoes not ripening')

    def test_a_real_reply_is_still_stored_as_the_assistant(self):
        api_server._record_voice_turn('hello', 'Hello back.')
        messages = stored()
        assert [m['role'] for m in messages] == ['user', 'assistant']
        assert messages[1]['content'] == 'Hello back.'
        assert 'meta' not in messages[0]


# ---------------------------------------------------------------------------
# 3. replay
# ---------------------------------------------------------------------------

class TestReplaySkipsUndeliveredTurns:
    def test_an_undelivered_turn_is_not_replayed(self):
        messages = [
            {'role': 'user', 'content': 'q1',
             'meta': {'undelivered': True, 'reason': 'no_model'}},
            {'role': 'user', 'content': 'q2'},
            {'role': 'assistant', 'content': 'a2'},
        ]
        assert api_server._history_pairs(messages) == [
            {'user': 'q2', 'assistant': 'a2'}]

    def test_an_unpaired_turn_does_not_shift_every_later_pair(self):
        """The stride-two loop this replaced would have mis-paired everything
        after an unpaired user turn — one outage corrupting the whole history."""
        messages = [
            {'role': 'user', 'content': 'q1'},
            {'role': 'assistant', 'content': 'a1'},
            {'role': 'user', 'content': 'q2',
             'meta': {'undelivered': True, 'reason': 'no_model'}},
            {'role': 'user', 'content': 'q3'},
            {'role': 'assistant', 'content': 'a3'},
        ]
        assert api_server._history_pairs(messages) == [
            {'user': 'q1', 'assistant': 'a1'},
            {'user': 'q3', 'assistant': 'a3'},
        ]

    def test_the_next_real_turn_sees_no_outage_in_its_prompt(self, no_model,
                                                            monkeypatch):
        """End to end: ask with no model, then ask again with one, and check what
        the second call is handed as prior context."""
        c = client()
        c.post('/chat/send', headers=AUTH,
               json={'message': 'first question', 'conversation_id': 'c1'})
        assert_undelivered(stored(), asked='first question')

        seen = {}

        def _capture(message, history, *args, **kwargs):
            seen['history'] = history
            return {'response': 'Answered.', 'sources': [], 'suggestions': []}

        monkeypatch.setattr(api_server, "_process_chat_query", _capture)
        c.post('/chat/send', headers=AUTH,
               json={'message': 'second question', 'conversation_id': 'c1'})

        assert seen['history'] == [], (
            "the model was handed the outage sentence as its own prior words")
