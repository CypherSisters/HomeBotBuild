"""The control-channel stop: ``POST /voice/stop`` and ``GET /voice/state``.

The complaint that started this was that saying "stop"
did not stop her — and every acoustic answer to that is a guess about a room.
This pair is the answer that cannot mis-hear: the API writes a file into the
one directory both units can reach (the same one the announcement queue lives
in), and the voice loop, which polls it every 100 ms while it speaks, kills
playback. Anything holding the API key — the dashboard, the kiosk, a phone —
can silence the speaker.

What is pinned here: the file the voice unit actually reads (name, shape and a
timestamp it can compare against playback start), that the route is behind the
global API-key guard like every other, and that ``/voice/state`` reports what
the voice unit published without inventing anything when the file is absent.
"""

import importlib
import json
import os
import sys

import pytest
from unittest.mock import MagicMock

from tests.modpatch import patched_modules

_src_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "src", "puffpai")
if _src_dir not in sys.path:
    sys.path.insert(0, _src_dir)


@pytest.fixture(scope="module")
def api_app():
    mock_fw = MagicMock()
    mock_fw.get_status.return_value = {"lockdown": False, "blocked_ips": []}
    with patched_modules({"firewall_manager": MagicMock(FirewallManager=lambda: mock_fw)}):
        if "api_server" in sys.modules:
            mod = importlib.reload(sys.modules["api_server"])
        else:
            mod = importlib.import_module("api_server")
        mod.app.config["TESTING"] = True
        yield mod.app, mod._API_KEY, mod


@pytest.fixture()
def client(api_app, tmp_path, monkeypatch):
    app, api_key, mod = api_app
    mod._rate_limit_store.clear()
    # Never write into the real state dir from a test.
    monkeypatch.setattr(mod, "_voice_control_dir", lambda: str(tmp_path))
    with app.test_client() as c:
        yield c, {"X-API-Key": api_key}, mod, tmp_path


def test_a_stop_writes_the_file_the_voice_unit_reads(client):
    c, headers, _mod, tmp_path = client
    resp = c.post("/voice/stop", json={}, headers=headers)

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["ok"] is True and body["ts"] > 0

    record = json.loads((tmp_path / "voice_control.json").read_text())
    assert record["cmd"] == "stop"
    assert record["ts"] == pytest.approx(body["ts"])

    # …and the voice loop agrees that this record stops a sentence that was
    # already playing, and consumes it. Same file, both halves of the feature.
    from puffpai.voice import service
    monkey = os.environ.get("PUFFPAI_DATA_DIR")
    os.environ["PUFFPAI_DATA_DIR"] = str(tmp_path)
    try:
        assert service.read_stop_control(record["ts"] - 1) is not None
        assert not (tmp_path / "voice_control.json").exists()
    finally:
        if monkey is None:
            os.environ.pop("PUFFPAI_DATA_DIR", None)
        else:
            os.environ["PUFFPAI_DATA_DIR"] = monkey


def test_stopping_the_music_too_is_opt_in(client, monkeypatch):
    c, headers, mod, _tmp = client
    called = []
    fake = MagicMock()
    fake.tool_radio_stop = lambda: called.append(True) or "radio stopped"
    with patched_modules({"puffpai.media_hub": fake}):
        c.post("/voice/stop", json={}, headers=headers)
        assert called == [], "a bare stop silences HER, not the room"
        resp = c.post("/voice/stop", json={"music": True}, headers=headers)
    assert called == [True]
    assert resp.get_json()["music"] == "radio stopped"


def test_the_stop_route_is_key_gated_like_everything_else(client):
    c, _headers, _mod, tmp_path = client
    resp = c.post("/voice/stop", json={})
    assert resp.status_code in (401, 403)
    assert not (tmp_path / "voice_control.json").exists()


def test_the_state_route_reports_what_the_voice_unit_published(client):
    c, headers, _mod, tmp_path = client
    # No voice unit on this machine: nothing is speaking, and it says so.
    assert c.get("/voice/state", headers=headers).get_json() == {
        "speaking": False, "since": 0, "voice_service": False}

    (tmp_path / "voice_state.json").write_text(
        json.dumps({"speaking": True, "since": 1758300000.5}), encoding="utf-8")
    body = c.get("/voice/state", headers=headers).get_json()
    assert body["speaking"] is True
    assert body["since"] == pytest.approx(1758300000.5)
    assert body["voice_service"] is True


# ── the spoken reply cap: most interrupts should never be needed ──────────
# A SIXTEEN-SECOND spoken dictionary definition must never be unstoppable.
# Speech gets the answer; the dashboard keeps the essay
# (``_record_voice_turn`` writes the full text, which is what earns the "on the
# screen" tail).
LONG = ("A vector is a quantity with both magnitude and direction. "
        "It is written as an arrow in space. "
        "Vectors add head to tail. "
        "They are used throughout physics and engineering.")


def test_two_sentences_are_spoken_and_the_rest_goes_to_the_screen(api_app):
    _app, _key, mod = api_app
    spoken = mod._cap_spoken_reply(LONG, tail=mod.VOICE_TRUNCATION_TAIL_SCREEN)

    assert spoken.startswith("A vector is a quantity")
    assert "Vectors add head to tail" not in spoken
    assert spoken.endswith(mod.VOICE_TRUNCATION_TAIL_SCREEN)
    assert mod.VOICE_MAX_SENTENCES == 2


def test_a_short_answer_is_left_exactly_alone(api_app):
    _app, _key, mod = api_app
    short = "It is ten past three. Anything else?"
    assert mod._cap_spoken_reply(short, tail=" I put the rest on the screen.")         == short


def test_the_word_ceiling_cuts_the_second_sentence_not_the_first(api_app):
    """One long sentence is still said whole - half a sentence is worse than a
    long one - but a second one that blows the budget is dropped."""
    _app, _key, mod = api_app
    first = " ".join(["word"] * 60) + "."
    text = first + " And then a second sentence. And a third one too."
    spoken = mod._cap_spoken_reply(text, tail="")
    assert spoken.startswith("word word")
    assert "second sentence" not in spoken


def test_a_read_back_the_owner_asked_for_is_never_capped(api_app):
    _app, _key, mod = api_app
    for asked in ["read me my messages", "list my documents",
                  "go through the whole thing"]:
        assert mod._VOICE_LIST_READ_RE.search(asked)
    assert not mod._VOICE_LIST_READ_RE.search("what is a vector")
